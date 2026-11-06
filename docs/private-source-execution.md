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

## Worker-side prerequisites (administrator action required)

The portal cannot provision the deploy key. The administrator must:
1. Install a keypair on the WORKER host and authorize its public key
   on the repository host (deploy key / SSH access — GitHub/GitLab
   docs).
2. Supply the key's on-worker path as `git_ssh_key_path` in the run
   configuration (launch-time option); the spec references the path,
   never the material.
3. Keep the repository host in the worker's `known_hosts` — SSH host
   verification stays ON; it is never disabled for convenience.
Key-material changes at the same path are invisible to the portal —
re-verify after rotating keys; the worker is the source of truth for
key validity. Portal-side secret-rotation tests do NOT cover worker
key changes.

## Verification semantics

- "Source binding resolved" (portal): clone+ref-resolution via the
  portal backend — proves PORTAL-side access only.
- Remote capability additionally requires: ssh URL + an eligible
  worker + a verified engine on it. Actual repository access on the
  worker (key readable, deploy-key authorized, host verified) is
  launch-verified by the worker — not claimed by portal setup.
- The publishing write probe is VERIFICATION evidence (one marker
  ref created/read-back/deleted); an execution's evidence export is
  a different, later operation. Do not conflate them.
