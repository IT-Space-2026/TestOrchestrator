# TestOrchestrator 2.5.0 — release-candidate acceptance checklist

Gate order: **GPL redistribution approval → one checksummed RC → all
checks below recorded against that same artifact.** No merge or
publish until every gate passes. `TO_BUILDBOT_GPL_APPROVED` stays
unset until approval is explicit.

## 0. Build the candidate (after GPL approval)

- [ ] `TO_BUILDBOT_GPL_APPROVED=1` set for the build only
- [ ] `python3 build-scripts/build_linux.py` from the tagged commit —
      produces the tarball + `.run`, stages `3rdparty/buildbot-wheels/`
      (buildbot, buildbot-worker, buildbot-www, paramiko pinned), and
      builds `TestOrchestrator-Buildbot-Worker-Setup-<ver>-UNSIGNED.exe`
      into `packaging/windows/` (requires `makensis` on the build host —
      **not currently installed on this machine**)
- [ ] `python3 scripts/validate_package.py <extracted-package>` —
      offline deps, vendored reporting-labs, worker .exe, buildbot wheels
- [ ] Record exact commit + SHA-256 of every shipped artifact in the
      PR matrix

## 1. Package & installed-system inspection

- [ ] Bundle: no `dagu*` binaries, archives, or config; no `caddy*`
      binaries, Caddyfile, or units; no dagu-worker installer; no
      portal download link to the old installer
- [ ] Bundle contains: `packaging/windows/TestOrchestrator-Buildbot-
      Worker-Setup-*.exe`, `3rdparty/buildbot-wheels/`,
      `install_linux_worker.sh`, `deploy/testorchestrator-web.service`
      (Cheroot), `-buildbot-master`, `-buildbot-local`, `-buildbot-ssh`
      units
- [ ] Clean installed system: `systemctl list-units` shows no
      dagu/caddy units; no `dagu/`, `caddy/` dirs under config/data;
      no `dagu_endpoint`/`dagu_*` settings; no legacy fallback code
- [ ] Portal + Buildbot start by default — no backend-switch command,
      no `flask buildbot-config` required

## 2. Native Linux worker (separate host, real systemd)

- [ ] `install_linux_worker.sh` on a second Linux host — **real**
      `useradd`, `sudo`, `systemctl enable --now` (previous run
      stubbed privileged ops)
- [ ] Worker registers outbound over PB; target shows online in
      Admin UI
- [ ] Targeted run → COMPLETED; artifacts collected (junit, logs)
- [ ] `systemctl restart` → reconnect without re-registration;
      reboot → auto-start + reconnect
- [ ] Cancellation kills the full remote process tree; run reports
      CANCELED only after confirmation
- [ ] Reinstall preserves worker identity (no duplicate target);
      uninstall removes the unit without touching KRE

## 3. Agentless SSH (separate `sshd` host, portal-only)

- [ ] Target host runs `sshd` + the test runtime only — **no** TO
      agent, no buildbot worker (previous run used a live Paramiko
      server, not `sshd`)
- [ ] Admin UI: add SSH target → first-contact fingerprint → approve
      host key → staged Test Connection all-pass
- [ ] Changed host key rejected until re-verified
- [ ] Job dispatch → remote run → SFTP artifact retrieval
- [ ] Disconnect mid-run → reattach, recover logs + exit status, no
      duplicate launch
- [ ] Cancel → CANCELED only after remote process-tree death
      confirmed; otherwise explicit unconfirmed status

## 4. Windows worker (owner: user)

- [ ] Download the exact bundled `.exe` from Admin UI (version +
      SHA-256 shown match the recorded artifact)
- [ ] Offline install on real Windows — no preinstalled Python, no
      controller, no KRE/licence bundled
- [ ] Registration → `Awaiting registration` clears → runners listed
- [ ] Service restart, reboot, reinstall (identity preserved — no
      duplicate), uninstall (startup removed, KRE/files untouched)
- [ ] Targeted execution, logs, artifacts, child-process cancellation

## 5. Licensed KRE (separate gate)

- [ ] One successful licensed KRE run on each target type — local,
      remote buildbot-worker, SSH — before claiming KRE parity
- [ ] Controlled-runner results do not substitute for licensed runs

## 6. Suite & evidence

- [ ] `pytest tests/` on the RC commit — record **actual** totals
      (last recorded: 633/2/1 at `548d284`; regression tests added at
      `2a86ab2` → expected 635/1/1 pending the RC run)
- [ ] Live-browser checks against the RC package, not a dev tree
- [ ] `test_production_key_auto_loaded` requires the issuer private
      key — blocked unless the key is provided
- [ ] Final matrix update on PR #3 with commit, artifact SHA-256s,
      commands, and per-check passed/failed/blocked
- [ ] Clean up all disposable installs, services, and processes
