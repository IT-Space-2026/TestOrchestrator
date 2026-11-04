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
