# Report-serving trust boundary (honest)

`Content-Security-Policy: sandbox allow-scripts allow-same-origin`
on the hosted report is **not** an origin boundary.

- `allow-scripts` → report JS executes.
- `allow-same-origin` → the report keeps the portal's real origin.
- Together they do **not** isolate it: report scripts share the
  portal origin and *can* issue `fetch()` to portal endpoints — those
  requests carry the viewer's session cookie and are limited only by
  the viewer's role, same as any same-origin page. There is no
  sandbox-derived origin separation on this host.

## What report content can contain
Generated output + the execution's attachments. Allure's own
`index.html`/JS/CSS/`data/`/`widgets/`/`plugins/` must be served
inline for the app to run — it is treated as generated content, not
user-uploaded files.

## Authenticated reach
A malicious attachment/report (if attacker-controlled input reaches
the report subtree) could issue same-origin authenticated requests.
Mitigations today: report files resolve strictly under the run's
`report_dir` (`safe_under` path-traversal rejection); project-level
authorization on the execution gates every view/asset/zip request;
`as_attachment` is still applied to `text/html`/`image/svg+xml`
under the **native** report's arbitrary-file route.

## Open risk / option
If a stronger isolation is required, the report must be served from a
separate origin (distinct host/port/subdomain or `sandbox` with only
`allow-scripts` — which breaks same-origin asset fetches, so it needs
asset URLs rewritten or a separate asset origin). That is a
deployment-topology decision, not a code toggle.
