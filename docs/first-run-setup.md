# Guided first-run setup — mapping + screen design (REVIEW, not built)

## Component mapping

| Setup step | Required component | Auto-configured | User input | Validation | Blocks readiness |
|---|---|---|---|---|---|
| 1. Admin credential | Portal account | bootstrap Admin | new password (≥12) | strength + match | yes — gate exists |
| 2. Orchestration | Dagu coordinator (bundled 2.25.8) | endpoint, ports, worker token, durable config | none | coordinator reachable + worker path | yes |
| 3. Worker | ≥1 Worker (local in-process OR remote Dagu worker) | local worker registration if present | remote: worker label + coordinator URL printed for them | worker visible in health | yes — ≥1 worker REQUIRED for any execution |
| 4. Execution engine | KRE install on a worker | none (external, licensed) | KRE executable path + which worker | probe (version check) | yes — ≥1 registered+verified KRE needed to run tests |
| 5. Source control | Project git binding + credentials | none | repo URL, ref, credential name | clone+resolve via backend | yes — ≥1 binding to run |
| 6. Report publishing | Report storage + optional git publish | local artifact store | publish repo (optional — NOT a required component; only if they want remote publication) | dir writable; publish clone if chosen | storage: yes / publish-repo: conditional |
| 7. Storage | Instance dir + DB | migration done at install | none (managed) | writable + migrated | yes |
| 8. Security | HTTPS / listening address | loopback default | bind addr, TLS cert if external | connect+handshake | external exposure: yes; loopback dev: no |

## Readiness contract
"Ready" = all *blocking* components verified (not just configured):
coordinator up, ≥1 worker connected, ≥1 KRE verified on a worker,
≥1 binding resolved, storage writable, admin set. Non-blocking
(report publication repo, external TLS) are presented as conditional.

## Mandatory-behavior changes identified (backend, not labels)
- `dispatch` currently allows executions with zero workers → fails at
  submit, not at setup. Proposal: readiness check surfaces before the
  Run button; submit returns 409 with the missing component named.
- KRE registration currently allows saving a path that never passed
  its probe → shown as configured, never verified. Proposal: status
  field "configured/verified/failed" separate; only verified counts.
- "optional" wording in secrets/credentials + integrations pages
  misrepresents worker-required flows — reworded, not re-required.
- First-run resume: persist `setup_state` JSON per completed step —
  re-entering setup resumes at the first incomplete blocking step.

## Screen sequence (7 steps + summary)
1. Welcome + Admin credential
2. Orchestration (auto — shows verified green check, no input)
3. Worker — choice: "local worker (this machine)" default or "remote"
   → shows coordinator URL + label to copy; Verify button polls
   /workers until the label registers
4. Execution engine (KRE) — path + target worker + Probe button;
   verified=version captured, unverified blocks Next with reason
5. Source binding — URL/ref/credential + Test button (clone+resolve)
6. Reporting & security — storage health check auto; optional publish
   repo + optional TLS/bind (marked conditional, not required)
7. Summary — readiness checklist with per-component
   verified/configured/missing state → "Finish" enabled only when all
   blocking items verified

Reuses the existing forms/validators (network, infrastructure, secrets)
rather than parallel logic. Incomplete system → Settings → "Setup &
diagnostics" entry point re-opens the same flow.
