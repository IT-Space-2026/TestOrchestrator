# Release-candidate artifact manifest

## Payload (application)
- File: dist/testorchestrator-1.0.0-pre.7-linux-x86_64.tar.gz
- sha256: 13a60a201780beb73417e276c36b40e8687d5600b24f78e5d71ebf53733dacac
- Built: `.venv/bin/python scripts/build_bundle.py` at git `5e42d53` (clean worktree)
- Embedded: version `1.0.0-pre.7`, build/source_rev `5e42d53`, built_at
  now-stamped per build (build_bundle.py stamps the packaged
  release.json — real source revision + build timestamp, not the old
  static `bf8a7d5a3155`/`2026-11-06`)
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
- Stub source: scripts/build_linux_installer.py at git `04ebbbe` (pgrep fail-fast before
  extraction; snapshot-PID killtree collects descendant pids before TERM
  so the KILL pass reaps processes reparented when a parent dies; INT
  and TERM handled). Host prerequisite: pgrep (procps) — installer
  aborts before creating temp state when absent; disclosed limitation.
  Wrapper tests: 8 passing (spaces/args, nonzero-exit, scoped cleanup,
  SIGTERM+SIGINT descendant kill, TERM-ignore, orphan grandchild,
  missing-pgrep abort).
- sha256: 6df965fa5b24e5d4e3a08e498037edb658745e3c951c0a2a840356851e06cc85

## Verification status
- Wrapper behavior: 4/4 fixture tests (spaces+args, nonzero exit,
  scoped cleanup, interruption descendant-kill).
- Installed operation / clean-VM / report-generation-and-viewing:
  UNVERIFIED — staged procedure requires a disposable VM + approval.
