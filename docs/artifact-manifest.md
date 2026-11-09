# Release-candidate artifact manifest

## Payload (application)
- File: dist/testorchestrator-1.0.0-pre.7-linux-x86_64.tar.gz
- sha256: 2067e746e76f4b371a122f40f436bed7d345a8a841e5741354201c4b509b193e
- Built: `.venv/bin/python scripts/build_bundle.py` at git `687f541` (clean worktree)
- Embedded identity: version `1.0.0-pre.7`, build `bf8a7d5a3155`
  - `bf8a7d5a3155` is the packaging stamp written into release.json at
    bundle-build time by scripts/build_bundle.py (records which build
    produced the artifacts). It identifies the built artifact set; the
    git revision `687f541` identifies its source tree. Both are recorded
    here — the stamp is an identifier, not independently-verified
    reproducibility evidence.
- Bundled: allure3 + node_modules (10.3MB), node 182MB, dagu_2.25.8
  (linux+windows), caddy, python-runtime 101MB, vendored py-deps.

## Wrapper (self-extracting installer)
- File: dist/testorchestrator-1.0.0-pre.7-linux-x86_64.run
- Built: `.venv/bin/python scripts/build_linux_installer.py <tarball> <run>`
- Stub source: scripts/build_linux_installer.py (pgrep fail-fast before
  extraction; snapshot-PID killtree collects descendant pids before TERM
  so the KILL pass reaps processes reparented when a parent dies; INT
  and TERM handled). Host prerequisite: pgrep (procps) — installer
  aborts before creating temp state when absent; disclosed limitation.
  Wrapper tests: 8 passing (spaces/args, nonzero-exit, scoped cleanup,
  SIGTERM+SIGINT descendant kill, TERM-ignore, orphan grandchild,
  missing-pgrep abort).
- sha256: 18676e6a5d1a518116f34eef5af93b9948a1536d40e9fa2776ed75a1f06aacce

## Verification status
- Wrapper behavior: 4/4 fixture tests (spaces+args, nonzero exit,
  scoped cleanup, interruption descendant-kill).
- Installed operation / clean-VM / report-generation-and-viewing:
  UNVERIFIED — staged procedure requires a disposable VM + approval.
