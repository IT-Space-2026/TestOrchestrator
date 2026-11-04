# Runbook: authorized-host system-service validation

Scope: closes the remaining environment gates that cannot be proven on
a user-level host. Run on an **existing authorized test machine** —
never provision paid infrastructure, never place credentials or sudo
passwords in chat, files, logs, or scripts.

## Prerequisites

| Requirement | Notes |
|---|---|
| Authorized Linux x86_64 host (VM or bare metal) | dedicated to this test |
| Root access — `sudo` without interactive password, or an authorized sudoers line | no password in files/scripts |
| Candidate tarball `testorchestrator-1.9.0-linux-x86_64-frozen-<rev>.tar.gz` + `.sha256` | verify `sha256sum -c` in `dist/` before transport |
| Service account `testorchestrator` (or as decided) | created by install or beforehand |
| Optional: a licensed KRE installation | only if the KRE execution gate is also being exercised — separate authorization required |

## Validation steps

1. **Transport + integrity.** Copy the tarball to the host;
   `sha256sum -c <file>.sha256`. Record SHA-256 in the report.
2. **Service-account install.**
   `sudo ./install.sh` (or `toctl install --payload /opt/testorchestrator
   --data /var/lib/testorchestrator --config /etc/testorchestrator`).
   Record: file ownership (`testorchestrator:testorchestrator` on
   payload/data/config), env file mode `0640`, data dir writable by the
   service account, payload read-only to it.
3. **Unit enablement + startup.**
   `systemctl enable --now testorchestrator-web testorchestrator-worker
   testorchestrator-caddy`. Record `systemctl status` per unit —
   `User=` is the service account, `Restart=` behavior, no
   `flask run`/dev-server.
4. **Config + DB access from the units.** Journal shows migrations
   complete to head; `sqlite3 <data>/testorchestrator.db
   "SELECT version_num FROM alembic_version"` matches head
   (`c4d5e6f7a8b9` at time of writing). DB writable by the service
   account only.
5. **Readiness attribution.** `toctl readiness` returns ready;
   `/healthz` reports this install's `TO_INSTALL_ID` and the payload
   build. Restart one unit, verify the same install_id re-answers.
6. **Writable surfaces.** As the service account: `touch` a file in
   `logs/`, `workspaces/`, `artifacts/`, `backups/`, temp dir.
7. **Java/Allure probe under the service account.**
   `sudo -u testorchestrator toctl java-probe` — record JSON outcome;
   Allure enabled only when a real generation probe passed.
8. **Stop/start + reboot persistence.** `systemctl stop`, confirm
   clean exit; `systemctl start`, readiness re-verified. Reboot the
   host; units come up without intervention, DB intact.
9. **Backup/recovery.** `toctl backup --to <dir>` with services running
   (documented service-state requirement — no stop required for
   sqlite backup integrity per `updater.py`); restore into a scratch
   instance and verify `toctl recover` + login.
10. **Evidence.** `journalctl -u testorchestrator-*` excerpts,
    `ls -l` ownership output, healthz JSON, systemctl status — attach
    to the report.

## Explicitly out of scope here

Licensed KRE execution (separate authorization), second-host remote
worker (separate gate), Azure DevOps interop, Windows build.
