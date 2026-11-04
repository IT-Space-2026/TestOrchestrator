# Two-host acceptance runbook (remote-KRE deployment)

Revisions required: server **and** agent built from the same source —
`ea9acb6`+ (agent must include the `tasks/<id>/claim` call; older
agents bypass the pickup claim and are unsupported for concurrent
remote execution). Candidate `10300832` predates all of this —
installer-baseline only, do NOT pair with the new agent protocol.

## Stage A — infrastructure + fixture execution (no licensed KRE)
1. Central app on host A; agent on host B (authorized machines,
   outbound connectivity to portal `/api/agent/*`).
2. Register remote runner → issue agent token (Admin → Infrastructure).
3. Runtime on the agent host = a fixture executable (`/bin/sh`
   marker/sleep script) — NOT KRE.
4. Verify: agent registers → polls `/api/agent/tasks` → claims
   (200; duplicate/other-runner claim → 409) → launches fixture →
   heartbeat lease → result + artifact upload → dashboard/audit.
5. Cases: 1 pass, 1 intentional failure, 2 jobs / 1 slot (sequential),
   duplicate pickup, timeout, cancel, agent disconnect→UNKNOWN →
   Resolve UNKNOWN → retry.

## Stage B — real KRE (BLOCKED until licensed execution authorized)
Real repo+rev, suite/profile/browser, licensed KRE on host B, licence
behaviour, native reports + screenshots + Allure.

## Prerequisites
Two authorized hosts; KRE install + licence (Stage B); agent token
(secret, issued per runner, never in chat); Java/Allure optional;
no sudo beyond service scope.

## Exact pinned revision
- Server + agent: **`57e2211d7aa1ed52a5b0a863ef28745e51afe07b`**
  (pinned — not "or later"; both sides MUST run this exact revision
  for the acceptance record).
- Agent must register with `capabilities: ["task-claim"]` and
  `agent_version: "0.2"` — anything else is refused work.
- Deployment MUST include migration `d5e6f7a8b9c0`
  (`runners.agent_capabilities`) — run `flask db upgrade` during
  install; without it the capability gate cannot read the column.

## Host prerequisites
- **Central:** source install + DB + portal listening; admin account.
- **Agent host:** Python + same source; outbound HTTPS to the portal
  (`/api/agent/*` only); no inbound required. Fixture executable for
  Stage A; licensed KRE for Stage B (blocked).

## Network direction
Agent → portal (outbound only): register, tasks, claim, heartbeat,
cancel poll, result upload. Portal never connects to the agent.

## Token issuance
Admin → Infrastructure → runner → **Issue token** — the token is shown
once, stored as SHA-256. Deploy it on the agent host only; never in
chat or logs.

## Fixture setup (Stage A)
`/bin/sh` executable that sleeps + writes marker files — registered
as a Runtime on the agent's runner. No licensed KRE.

## Expected Stage A evidence
- register → tasks → claim 200 (duplicate/foreign claim → 409,
  incompatible agent → `incompatible` marker + empty tasks)
- fixture launch marker → heartbeat lease renewal → result + artifacts
- timeout kill (marker stops), cancel-requested → tree stops
- agent disconnect → UNKNOWN → admin Resolve UNKNOWN (confirm + note)
  → slot released → safe retry
- dashboard live-update + audit entries per transition
- cleanup: stop agent, mark fixture executions resolved, remove
  fixture runtime


## Consolidated Stage A build proposal (NOT built yet)

Prior pin `57e2211` predates the execution-controls round. Proposed
consolidated revision: **a063e12a79144b10c2e927ff0bd81de9cca0b090**
  (run-now idempotency + scope-guarded explicit targets +
  dedupe/safe-return — final tested application revision). Server AND
  agent from the same checkout.

- **Migration head:** `f7a8b9c0d1e2` (`submit_tokens` table —
  `db-upgrade` during install; requires app stopped or single-writer
  — SQLite).
- **Agent requirement:** `AGENT_VERSION "0.2"` + `task-claim`
  capability at register (older agents refused work by design).
- **Source/metadata:** build from `1f6c0a1`; stamp build metadata via
  `scripts/build_bundle.py` (release.json embedded).
- **Packaging:** `python scripts/build_bundle.py` →
  `dist/testorchestrator-<ver>-<platform>-<sha>.tar.gz` + standard
  (non-frozen) fallback bundle.
- **Fresh-extraction checks:** release.json SHA == package name;
  `toctl db-upgrade` reaches `e6f7a8b9c0d1`; agent registers with
  `task-claim`; smoke login + one fixture run.
- **Baseline reuse:** install/service/security evidence from
  `10300832` still applies to installer mechanics ONLY; all
  execution-path, dispatch, agent-protocol, UI, and browser tests
  must re-run on the new build.
- **Two-host updates:** Stage A steps unchanged; expected evidence
  now includes Run-now, overlap skip, history filters, bulk cancel.
