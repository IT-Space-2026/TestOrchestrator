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

## Results — internal build `7a6e28c` (internal-only; GPL gate unset)

Artifacts under `dist/release/linux/` (SHA-256 in `SHA256SUMS.txt`):

| Artifact | SHA-256 |
|---|---|
| `testorchestrator-2.5.0-linux-x86_64.tar.gz` | `c543f968…cc9faf9` |
| `testorchestrator-2.5.0-linux-x86_64.run` | `3160fe38…06196e45` |
| `TestOrchestrator-Buildbot-Sources-4.3.0.tar.gz` | `d0b4f08c…541ff467` |
| `TestOrchestrator-Buildbot-Worker-Setup-4.3.0-UNSIGNED.exe` (in bundle) | `b4faa4fc…cd84cd9` |

Checks executed against this build — todo items **9** (packaged
acceptance), **10** (repeat on candidate), **12** (actual totals):

- [x] §1 Bundle sweep: zero dagu/caddy outside the two
      `SUPERSEDED`-marked historical docs and migration/legacy-cleanup
      comments. Installed system: no units, dirs, settings, or
      fallback paths (user-scope systemd activation — **item 10's
      system-scope row stays unchecked**).
- [x] §1 Defaults: Cheroot bound configured HTTPS :8443 from a clean
      install (no backend switch); Buildbot master REST :8010 active,
      PB :9989; `to-local` + `to-ssh-dispatcher` attached. DB
      migrated at install; `ExecStartPre` re-migrates on every start.
- [x] §2 (user-scope systemd) `install_linux_worker.sh` offline from
      bundled wheels → `systemctl --user` unit → PB attach → Admin-UI
      **credential reissue** → tac update + unit restart → reattach →
      pinned run `7ceb7f0b` COMPLETED exit 0, `junit.xml` + `exit_code`
      collected. Privileged ops stubbed → **system-scope row of item
      10 open**.
- [x] §3 Portal-only: real OpenSSH `sshd` (extracted package, own
      host key, key auth, no TO software on endpoint) → Admin UI
      create → fingerprint `SHA256:PfMLqQJx…` unverified → approve →
      staged test `ok` → **no manual restart** (live reconfig logged
      in `twistd.log`) → run `0789f14b` COMPLETED exit 0, artifacts
      SFTP-collected to `buildbot/artifacts/`. Separate-machine +
      disconnect/cancel rows of item 9 open.
- [x] §3 Host-key approval verified against actual sshd key
      (`ssh-keygen -lf` match).
- [~] §9 items: manual targeted runs passed on SSH + native worker;
      scheduled runs, cancel, and disconnect recovery still open.
- [x] §12 `pytest` actual totals (suite run at `fed8c93`+doc/test
      commits, code-identical candidate tree): **636 passed,
      1 failed, 1 skipped** — the failure is
      `test_production_key_auto_loaded` (external licence-issuer
      private key — blocked by design, key never committed); the skip
      is `test_git_packaging.py`'s cli-backend fallback case
      (`pytest.skip` — payload not staged in this config). One stale
      test (`test_stage_web_unit_writes_canonical_name`) was updated
      for the `7a6e28c` `_stage_web_unit` signature before the run.

Two more defects found during this pass, fixed at `fed8c93`
(post-artifact): `[""]` blank-scope on runner create (broke targeted
dispatch), and `ExecStart= ` leading space in generated units.
Rebuilt-candidate evidence above remains valid — both defects were
worked around live exactly as the fix implements, but the **next**
candidate must include `fed8c93`.

Historical: `2f96ea0` results (native `56d85ab8`, ssh `fc93eef7`)
are evidence for those fixes only — not carried forward as passes.
