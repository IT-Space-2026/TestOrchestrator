# Operator prerequisites for live acceptance testing

Everything here is supplied and maintained by the operator. TestOrchestrator
never installs, licenses, upgrades, or removes KRE, and no installer or
updater may alter it.

## For A01/A02 (real execution evidence)

| Input | Used by | Form |
|---|---|---|
| KRE executable (installed, licensed) | RuntimeConnection | absolute path, e.g. `/opt/katalon/katalonc` |
| Working browsers/drivers | KRE | per KRE vendor docs |
| Representative Katalon project | GitBinding URL | repo cloneable by the host |
| Approved execution environment | runner host | OS user allowed to launch KRE |
| Runner host | Agent | reachable `flask agent` runtime |
| Git remote + credential (optional) | `git_export_repo` + SecretRef | HTTPS/SSH URL + scoped token |
| Dagu endpoint (optional seam) | `dagu_endpoint` + SecretRef | version to pin and verify |
| Report runtime | bundled — none required | Bundled Node.js + Allure 3; no customer Java/Node/npm. |

## Smoke checks the operator can run first

```bash
# Probe an existing KRE install (version probe only — never runs tests,
# no licence action taken):
flask kre-smoke /opt/katalon/katalonc

# Optional direct smoke EXECUTION of an explicitly selected suite
# (bypasses portal/Dagu/agent — not the A01 acceptance run):
flask kre-exec /opt/katalon/katalonc --project /srv/your-project \
    --suite "Test Suites/Smoke"

# Register runtime in the UI, then use "Test connection" — it probes the
# executable only, never installs or licenses.
```

## Explicitly NOT required

- No KRE download/import/licence provisioning through the product
- No Node/npm, no Rust toolchain, no external pip/CDN access at runtime
- No external public-IP discovery or firewall changes by the product
