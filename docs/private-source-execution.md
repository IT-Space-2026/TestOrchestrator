# Private-repository execution — supported path

Three distinct auth contexts — each configured and consumed separately:

| Stage | Where auth is configured | How it's consumed |
|---|---|---|
| Portal-side git ops (binding sync, case/suite listing) | stored `user:token` SecretRef (HTTPS) or host ssh | Dulwich `auth=(user,pass)` — portal host only |
| Worker-side source checkout | `git_ssh_key_path` on the RuntimeConnection — a path ON THE WORKER | worker's go-git SSH checkout; key material never crosses the wire |
| Evidence publishing | `publish_repo_url` + publish credential (portal-side) | portal `gitsync.push_export` write+read-back+delete probe |

## Remote execution from a private repo — the supported path

- `ssh://` or `git@host:` binding URL.
- Worker carries its own deploy key; the runtime's `git_ssh_key_path`
  names that file. The job spec carries only the PATH reference — no
  secret enters the spec, argv, logs, or artifacts.
- The worker checks out directly — the portal doesn't proxy content.

## What is NOT supported (and why)

- HTTPS private repo + remote dispatch: the stored `user:token` secret
  cannot be embedded in the stored workflow spec without exposing it —
  dispatch fails explicitly (this is the documented current limit,
  not a silent fallback). Portal-side HTTPS private repos work for
  portal-side operations only.
- Requiring public repos — not the proposal; the ssh+worker-key path
  covers private repos.

## Verification semantics

- "Source binding resolved" (portal): clone+ref-resolution via the
  portal backend — proves PORTAL-side access.
- Remote-execution readiness additionally requires: an eligible
  worker AND a runtime whose `git_ssh_key_path` is set for ssh://
  bindings. Portal-side auth alone never marks remote readiness.
