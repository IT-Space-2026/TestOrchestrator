# Dulwich assessment — removing the Git executable dependency

## Verdict: viable — replacement already implemented and default.

## 1. Runtime Git operation inventory
All application git traffic goes through `git_backend.run(op)`:
- **Portal (authoring.py):** clone, update, resolve(HEAD/branch/tag/~N),
  commit_all, push.
- **Background sync (gitsync.py):** init, identity, clone, fetch,
  remote_ref, merge, push, update, commit_all.
- **Report publication (gitsync.publish_report/delete_report):** same
  facade — clone/commit/push of report files to the binding repo.
- **Local Dagu workflow:** `agent --once` — agent.py has NO git calls;
  the worktree is prepared portal-side via the backend.
- **Remote Dagu spec:** `git.checkout` action (see §2).
- Default backend = `dulwich` (pinned `dulwich==1.2.20`); `cli` is an
  opt-in fallback (`TO_GIT_BACKEND=cli`) for source builds only.
  No production code outside `git_backend`/`git_runtime` subprocesses git.

## 2. Dagu 2.25.8 `git.checkout` — binary inspection
`strings` on the pinned dagu binary shows `go-git/5.xpack-%s`
(go-git v5 linked in), `git.checkout` in the action registry, and pure-Go
ssh client internals (ssh_config, ForwardX11, RekeyLimit).
**Conclusion: `git.checkout` is implemented by go-git inside the dagu
binary — workers need no git AND no external ssh executable.**
Corroborated by the loopback fixture: checkout ran on a worker where
only dagu.exe was provisioned.

## 3. Dulwich 1.2.20 vs requirements
- clone/fetch/update/resolve (sha, branch, tag, ~N), commit_all,
  fast-forward + 3-way merge w/ conflict surfacing, push — implemented
  in git_backend.py and covered by tests/test_gitsync_remote.py +
  test_authoring.py + test_report_pub.py.
- Exact-commit checkout = tree export (same isolation as worktree
  --detach; destination carries `.checkout-sha`).
- HTTPS auth: username/password via porcelain kwargs.
- TLS cert verification: standard urllib3 chain.

## 4. Retained executable dependencies (honest list)
- **ssh:// remotes**: dulwich uses a subprocess `ssh` — an executable,
  though not git. Remote-worker checkout via Dagu does NOT share this
  (go-git ssh is pure Go). Portal-side ssh:// bindings keep a host-ssh
  dependency; eliminating it needs a paramiko/dulwich vendor — scoped
  follow-up, not a git concern.
- **No LFS, no submodule auto-init, no credential helpers** — already
  documented in gitsync.py docstring; unchanged by this migration.
- Remote worker ssh keys: worker-side paths only (already documented).

## 5. Footprint comparison
- Bundled git rootfs: ~77.7 MB + acquire_git.py maintenance + the
  `ubuntu-resolute-amd64` glibc lineage it imports into the baseline.
- Dulwich: ~3 MB of site-packages already shipped.
- Removing 3rdparty/git from the standard bundle shrinks the package
  ~75 MB and removes one resolute-acquired native component — which
  also improves the Linux-baseline story.
- Complexity: backend already written+tested; removal is a build-list
  change, not new code.

## Implementation done this session
- All git-dependent test fixtures converted from subprocess git to the
  app backend / `tests/git_helpers.py` (dulwich shim for cli-shape
  asserts: init/clone/config/add/commit/push/pull/show/rev-parse/ls-tree).
- Full suite verified with no `git` on PATH (see suite result).

## To remove bundled git from the package (build change, after approval)
- Drop `("3rdparty/git","git")` from build_bundle FILES for the
  standard build (frozen build already excludes it).
- Keep `git_runtime`/`cli` backend as documented source-build fallback.
- Docs: declare "no bundled git — Dulwich backend; ssh:// remotes
  require host ssh" as the supported configuration.
