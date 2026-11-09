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
- Stub source: scripts/build_linux_installer.py at git `0398c69` (two-pass
  TERM+KILL killtree: kills install.sh descendant tree on INT/TERM, then
  removes the extract dir, exits with child's status).
- sha256: 6f322bbe309dab852a6ee920f0e3d4cd9f0698a6313ca085df425b72a70c8f2b

## Verification status
- Wrapper behavior: 4/4 fixture tests (spaces+args, nonzero exit,
  scoped cleanup, interruption descendant-kill).
- Installed operation / clean-VM / report-generation-and-viewing:
  UNVERIFIED — staged procedure requires a disposable VM + approval.
