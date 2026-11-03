# Operator runbook: first real KRE execution (A01 gate)

**Status: INTERIM developer-assisted test path.** The commands below work
against the current build for a supervised acceptance run. They do **not**
replace the required product installer, which must provision dependencies
and services automatically (see `docs/packaging-status.md` — installer is
still PARTIAL). Nothing in this runbook installs, upgrades, licenses, or
modifies KRE: it is installed, licensed, and maintained separately by you.

**Services that must already be running** because the complete installer
is not yet delivered — each is started manually per the sections below:

| Service | Started by | Section |
|---|---|---|
| Portal (Waitress on 127.0.0.1:18080, or Caddy→Waitress in deployment) | `waitress-serve wsgi:app` | §2 |
| Durable worker | `flask worker` | §2 |
| Authenticated Dagu with the workflow installed | `dagu start-all` / provision script | §3 |
| Agent — local mode: spawned per run by the Dagu step; remote mode: a daemon you start | `python -m testorchestrator.agent` | §6 |
| KRE + browsers/drivers | **you** — installed, licensed, maintained outside this product | §5 |

## Prerequisites checklist for real-KRE A01

- [ ] Licensed, working KRE binary (absolute path), verified by `flask kre-smoke`
- [ ] Working browsers/drivers per KRE vendor docs
- [ ] A Katalon project reachable by the runner host (Git binding or local path)
- [ ] Portal + worker + Dagu (basic auth) + workflow running per §2–§3
- [ ] `flask dagu-config ... --check` → `workflow_installed=True` (§4)
- [ ] Runner registered + `toa_…` token issued + `agent.toml` (0600) on the executing host (§5–§6)
- [ ] Runtime registered; **Test connection** probe version matches your licensed install (§5)
- [ ] `flask kre-exec` against the real project/suite proves the console flags for YOUR KRE version (§7)
- [ ] A safe, operator-approved smoke suite/case set and environment (§9)

Conventions used below:

- `TO_ROOT` — the checkout/install root (e.g. `/opt/testorchestrator` or a
  dev clone). All `flask`/`python` commands run from `TO_ROOT` with the
  project venv: `.venv/bin/flask`, `.venv/bin/python`.
- `INSTANCE` — instance dir: `$TO_INSTANCE_DIR` or default
  `TO_ROOT/instance` (deployed: `/var/lib/testorchestrator`).
- Placeholders in `<angle brackets>` are values only you know. No secret
  value ever goes on a command line — secrets are entered in the portal
  Credentials page or written into 0600 files.

## 1. Environment and launcher

```bash
cd "$TO_ROOT"
export FLASK_APP=wsgi:app
export TO_SECURE_COOKIES=0        # dev path only: plain loopback HTTP
# production: TO_SECRET_KEY + config in /etc/testorchestrator/web.env,
# instance data in /var/lib/testorchestrator, certs handled by the UI.
.venv/bin/flask db upgrade        # create/upgrade the SQLite schema
```

Configuration locations:

| What | Where |
|---|---|
| Persistent settings (endpoints, flags) | DB `settings` table — via UI or `flask dagu-config` |
| Credentials (Dagu password/API key, Git, RP) | System → Credentials (Fernet store, key `$INSTANCE/secret.key` 0600) |
| Agent token + portal URL on a runner host | `agent.toml` (0600), path passed via `--config` |
| Dagu server config + credentials | `server.yaml` + `credentials` (0600), provisioned per §3 |
| Dagu workflow | `<DAGU_HOME>/dags/testorchestrator-execution.yaml` |

## 2. Start the portal and the worker

Two long-running processes, both from `TO_ROOT`:

```bash
# terminal 1 — portal (production listener is Caddy->Waitress; this dev
# path serves Waitress directly on loopback)
.venv/bin/waitress-serve --listen=127.0.0.1:18080 wsgi:app

# terminal 2 — durable worker: outbox dispatch, reconciliation.
# REQUIRED before creating executions or syncing bindings.
.venv/bin/flask worker
```

First login at `http://127.0.0.1:18080`: bootstrap `Admin`/`Admin` forces a
strong password change (headless alternative: `flask set-admin-password`
— prefer the UI so the new password never appears in shell history).

## 3. Start authenticated Dagu and install the workflow

Dagu 2.25.2 is bundled at `3rdparty/dagu/dagu_2.25.2_linux_amd64.tar.gz`.
Either run `python3 scripts/provision_dagu.py` (writes binary, `server.yaml`
with `auth.mode: basic`, a 0600 `credentials` file, the workflow, and a
systemd unit you then enable), or do it by hand:

```bash
mkdir -p /tmp/dagu/{bin,home/dags}
tar xzf 3rdparty/dagu/dagu_2.25.2_linux_amd64.tar.gz -C /tmp/dagu/bin
cat > /tmp/dagu/home/server.yaml <<'EOF'
auth:
  mode: basic
  basic:
    username: testorchestrator
    password: <choose-a-strong-password>
EOF
chmod 600 /tmp/dagu/home/server.yaml
cp deploy/dags/testorchestrator-execution.yaml /tmp/dagu/home/dags/
```

For a **dev** run, edit the workflow's step first so the agent resolves:

```yaml
# /tmp/dagu/home/dags/testorchestrator-execution.yaml
params:
  - execution_id: ""
steps:
  - name: execute-assigned-task
    command: >-
      <TO_ROOT>/.venv/bin/python -m testorchestrator.agent --once
      --execution-id "$execution_id" --config <path>/agent.toml
    working_dir: <TO_ROOT>
```

Start it:

```bash
DAGU_HOME=/tmp/dagu/home DAGU_COORDINATOR_ENABLED=false \
  /tmp/dagu/bin/dagu start-all --port 29987 \
  --config /tmp/dagu/home/server.yaml --dags /tmp/dagu/home/dags
```

## 4. Configure the portal's Dagu connection

1. Portal → System → Credentials → add: name `dagu-api`, kind `dagu`,
   value = the Dagu **password** (basic mode) — never on a command line.
2. Then record the endpoint and verify auth + workflow in one step:

```bash
.venv/bin/flask dagu-config \
    --endpoint http://127.0.0.1:29987 \
    --username testorchestrator \
    --credential-ref dagu-api \
    --check
# expected: health: healthy, version 2.25.2, workflow_installed=True
```

(Omit `--username` when the Dagu credential is a builtin-mode Bearer API
key instead — the same SecretRef then holds that key.)

## 5. Register a runner and an existing runtime

Portal → System → Runners & runtimes:

- **Runner**: name, connection_mode `local` (same host) or `remote`,
  workspace root, slots, allowed projects.
- **Runtime connection**: pick the runner, absolute `executable_path` of
  your installed KRE (e.g. `/opt/katalon/katalonc`), a scratch
  `workspace_root`.
- Press **Test connection**: runs the `<exe> -version` probe only. Expect
  "Probe OK — detected version X.Y.Z". **Verify X.Y.Z equals the version
  you installed and licensed** — the portal does not know your licence.

## 6. Issue and configure the agent token; start the agent

An agent is required in **both** modes — it is the only executor:

- **Local mode**: the Dagu workflow step itself launches
  `python -m testorchestrator.agent --once --execution-id <id>` on the
  Dagu host per run. Its `agent.toml` must hold the **local** runner's
  token. You do not start a daemon; Dagu spawns it.
- **Remote mode**: a persistent agent daemon you start on the remote host
  polls `/api/agent/tasks` every 5 s and picks up executions assigned to
  its runner. Its `agent.toml` holds the **remote** runner's token.
  The Dagu workflow step still runs on the Dagu host: its agent finds no
  task for the remote runner, then `--monitor` keeps polling the
  execution state until a terminal outcome (`completed`/`failed`/
  `canceled` → step success) or explicit uncertainty (`unknown`/timeout →
  step fails honestly). The dagu run therefore mirrors the remote run's
  fate — it never finishes after mere dispatch. The local-runner token in
  the step's `agent.toml` is the orchestration credential that may read
  any execution's state label for this purpose.

In both cases:

1. Portal → System → Runners & runtimes → **Issue token** on the runner.
   The `toa_...` plaintext flashes **once** — copy it immediately.
2. On the host where the agent runs, write `agent.toml` (0600):

```toml
portal = "http://127.0.0.1:18080"   # or https://<portal> for remote
token = "toa_..."                   # value issued in step 1
work_root = "/var/lib/testorchestrator/agent"   # any scratch dir
```

```bash
chmod 600 agent.toml
# remote mode — long-running daemon:
.venv/bin/python -m testorchestrator.agent --config agent.toml
```

3. Verify heartbeat: System → Runners & runtimes — the runner row shows
   `health = ok`, a fresh `Heartbeat` timestamp, and the agent version.
   (Local mode shows this only while a run is executing; to check earlier,
   run `--once` by hand — it registers without work.)

## 7. Probe semantics — exact subprocess arguments

Two different checks; do not conflate them:

| Command | Exact argv | What it proves |
|---|---|---|
| `flask kre-smoke <exe>` | `["<exe>", "-version"]` | Executable present, executable bit, version probe — **never starts project tests** |
| `flask kre-exec <exe> --project <dir> --suite "<path>" [--profile p] [--browser b] [--timeout s]` | `[<exe>, -noSplash, -runMode=console, -projectPath=<dir>, -testSuitePath=<suite>, -executionProfile=<p>, -browserType=<b>, -reportFolder=<dir>]` | Real console invocation + report collection |

- `kre-smoke` has **no** `--project`/`--suite` options — it cannot run
  tests by construction.
- `kre-exec` requires an explicitly selected `--suite`; it bypasses
  portal/Dagu/agent entirely and is **not** the A01 acceptance run.
- The invocation flags are the Katalon-documented console form but are
  **verified only against a fixture executable** (A14). Before A01, run
  `flask kre-exec` against your real project/suite and confirm your KRE
  version accepts them; record per-version differences in
  `docs/compatibility.md` — do not assume a flag is safe because the
  fixture accepts it.

## 8. Readiness checks

| Component | Check | Expected result | Corrective action |
|---|---|---|---|
| Portal app | `flask health` (TO_ROOT) | prints `ok`, exit 0 | run `flask db upgrade`; check `TO_DATABASE_URI` |
| Portal UI | open `http://127.0.0.1:18080`, log in | dashboard loads | bootstrap `Admin`/`Admin` or `flask set-admin-password`; check waitress process |
| Worker | `flask worker --once` | `processed: N`, exit 0 | start `flask worker`; check outbox `last_error` |
| Dagu service | `flask dagu-config --endpoint ... --check` | `healthy`, version, `workflow_installed=True` | see §3/§4: service up? credentials correct? workflow in `dags/`? |
| Dagu auth | `curl -u testorchestrator http://127.0.0.1:29987/api/v1/health` (curl prompts for the password) | `{"status":"healthy",...}` | fix `server.yaml` `auth.basic`; re-store SecretRef |
| Workflow | `curl -u testorchestrator http://127.0.0.1:29987/api/v1/dags` | `testorchestrator-execution` listed | copy `deploy/dags/testorchestrator-execution.yaml` into the dags dir, restart |
| Agent | Infrastructure → runner row | `health=ok`, heartbeat < 90 s old, version shown | token mismatch → agent gets 401 and exits; wrong `--config` path; TLS trust for remote HTTPS |
| KRE | `flask kre-smoke <exe>` or **Test connection** | `probe: detected version X.Y.Z` | path must be absolute+executable; verify it is your licensed version |

"Reachable" alone is not sufficient — the `--check` flag verifies
authentication **and** workflow availability in one step.

## 9. A01 — the real acceptance run (numbered UI steps)

1. Portal → **System → Credentials**: store `dagu-api` (kind `dagu`,
   value = Dagu password). Then `flask dagu-config --check` →
   `workflow_installed=True`.
2. **System → Runners & runtimes**: register the runner → **Issue token**
   → write `agent.toml` (0600) on the host that executes (§6).
3. Same page: register the runtime (real `katalonc` path + scratch
   workspace) → **Test connection** → probe version confirmed against
   your licensed install.
4. **Projects** → create/open project → add Git binding (repo URL/path,
   ref) → **Sync** → wait for `status=synced` + pinned SHA
   (requires `flask worker` running).
5. Same page, **Create suite**: name it, pick the binding in **Source
   binding**, and select a **safe** smoke case set (approved for this
   environment) from the **Test cases** multi-select — it lists the synced
   worktree's `Test Cases`.
6. Open the **suite detail** page: review the pinned source (binding +
   SHA), the suite version/hash, and the entry list — this is the
   immutable snapshot that will run. Set **Execution profile** and
   **Browser** (defaults `default` / `Chrome (headless)`), then
   **Run suite**.
7. You land on the **execution detail** page. Refresh and follow:

   | Step | Where | Field |
   |---|---|---|
   | execution ID | page title/URL | `Execution.id` |
   | Dagu run ID | detail table | `Dagu run` (`dispatch_id`) |
   | assigned runner/runtime | detail table | `Runner` / `Runtime` |
   | actual KRE invocation | Logs | `kre-invocation.log` (recorded argv) |
   | console output | Logs | `kre-stdout.log` / `kre-stderr.log` |
   | collected results | Results table | per-case status/attempt/duration |
   | final state + outcome | detail table | `state` / `outcome` |

8. Cross-check on the Dagu side (optional — password prompted):
   `curl -u testorchestrator http://127.0.0.1:29987/api/v1/dags/testorchestrator-execution/dag-runs/<Dagu run>` → `statusLabel`.

**A01 pass criteria:** `state=completed` with `outcome` matching the JUnit
evidence (a test assertion failure must still produce `completed` +
`outcome=failed`), one result row per case, and artifacts (JUnit, logs,
screenshots) present with sha256s. `rp_state`/`git_state` may read
`none`/`not-configured` — A01 does **not** depend on ReportPortal publication or
Git synchronization; those are separate state fields and separate gates.

## 10. Stop conditions, timeouts, diagnostics

- **Stop:** the **Cancel execution** button → `cancel_requested` → the
  agent's cancel poll fires → process-tree SIGTERM (SIGKILL escalation) →
  `canceled`, `outcome=incomplete`. Cancellation stays pending until
  termination is acknowledged.
- **Timeout:** `run_invocation` default 3600 s per invocation
  (`flask kre-exec --timeout` for the CLI check). The dispatch lease is
  60 s, renewed to +90 s by the agent's 30 s heartbeats; an expired lease
  reconciles to `unknown` — **never** a silent re-dispatch. The worker
  also reconciles Dagu runs each cycle: a finished dagu run with no
  portal result → `unknown`. A *running* dagu run never extends the
  lease — an agent disconnect still surfaces as `unknown` within ~60 s.
- **Diagnostics:** `error_kind`/`error_text` on the execution page;
  `kre-*.log` links and artifacts; System → Audit rows
  (`execution.*`, `runner.*`, `runtime.probe`); on the runner host
  `<work_root>/<execution_id>/{logs,Reports}`; on the portal
  `$INSTANCE/agent-evidence/<id>/` for uploaded remote evidence; the Dagu
  dag-run log for the step's own output. None of these contain credential
  values — agent tokens live only as SHA-256 digests.

## 11. Boundaries (explicitly NOT provided here)

Licence activation, KRE installation/updates, ReportPortal credentials,
and remote-host firewall rules are operator-owned. The product installer
may install its own runner connector — never KRE. If KRE is missing or
incompatible, the run must fail with an actionable prerequisite error,
not a fabricated pass.
