# Report-origin isolation — corrected design candidate

## Current exposure (unchanged)
Allure reports are served same-origin under the portal host. Report
JavaScript runs with the portal origin → it can issue authenticated
portal requests (session cookie applies) and read their responses.
Role/path auth still gates actions, but there is no *origin* boundary.

## Corrected design — separate report origin
The earlier sketch misstated two mechanisms. Corrected here:

### Serving
Report files are served from a **separate host/origin** — `reports.<base>`
resolved by the app's configured report-serving host (a real distinct
origin, so the same-origin policy *itself* blocks report-script reads of
portal responses — not a "different cookie → 401" heuristic).

### Cookie scope
The portal session cookie is set **without a `Domain` attribute**
(host-only). It is therefore never sent to `reports.<base>` — the portal
credential does not reach the report origin at all. This is the actual
mechanism that prevents authenticated request initiation: report scripts
can still `fetch` the portal, but those requests carry **no session
cookie** → they are anonymous → they get no session data.

### Report access authz
Report reads use a **short-lived HMAC-signed URL** (`/report/<id>/<sig>`,
sig = HMAC(report_id + expiry)), not a login session. GET-only, no CSRF
surface on the report origin. The portal issues the signed link after a
normal authenticated `role_required` + `project_allowed` check.

### CSRF
Portal mutating routes stay protected by Flask-WTF (session-bound tokens).
Because report scripts receive no portal cookie, they cannot forge a
credentialed POST — the CSRF token is also session-bound.

### What this does NOT change
- Report JS on `reports.*` can still read *its own* origin's files.
- A compromised report payload could still exfiltrate report content —
  this isolates the *portal*, not the report data itself.
- No portal/report cross-origin API is needed (read-only file serving).

## Verification criteria (when implemented)
1. Report JS on `reports.*` `fetch(portal_url, {credentials:"include"})`
   → receives no session (cookie not sent cross-origin) → 401/redirect.
2. `document.cookie` on the report origin → no portal session.
3. Signed report URL valid; tampered/expired sig → 403.
4. Portal POST from report origin lacks the session cookie → CSRF/token
   rejection (no credentialed mutation possible).

## Status
Design candidate only — no implementation or risk acceptance authorized.
