# Incident: stale executable payload in revision-tagged candidates

**Date:** 2026-11-04 · **Status:** corrected + quarantined · **Severity:** packaging defect — evidence-integrity, not a shipped release (nothing was published).

## What happened

`scripts/build_bundle.py` collected the PyInstaller onedir from
`dist/frozen/testorchestrator` — a stale directory last written at
~08:35 local — while `packaging/pyinstaller/app.spec` outputs to
`dist/testorchestrator`. Every `--frozen` bundle built after that point
packaged the same stale binary; only source-side files (release.json,
docs) changed between archives.

**Detection:** smoke checks on the `-d310119f` candidate printed
pre-`0c18a0c` installer strings; PYZ extraction confirmed the binary
lacked `_verify_install`/`_claim_for_start`. Embedded
`app/_internal/release/release.json` reads `"build":
"source-checkout"` in all four archives — the executable was never
stamped and predates every claimed revision.

## Affected archives (quarantined at `dist/quarantine/`)

| archive | bytes | sha-256 | claimed revision (name) | embedded build | verdict |
|---|---|---|---|---|---|
| `-frozen-22fe094` | 143,561,854 | `840346d0b116048b8d4592edb7c36e4e67ad6a76528b63acf1c3124b90bcb8b8` | `22fe094` | `source-checkout` | REJECTED |
| `-frozen-0c18a0c4` | 143,566,585 | `448b165b045c0283ebbabc49aa3e929c4fe62fffc260753f1bc351698db766aa` | `0c18a0c4` | `source-checkout` | REJECTED |
| `-frozen-d3effdec` | 143,568,470 | `60af46deda19b8715d962304a6b2f6fdeda6141f2c92b5180e52aea2a0216899` | `d3effdec` | `source-checkout` | REJECTED |
| `-frozen-d310119f` | 143,623,661 | `302f650091e2197fda14760647118cc5e3adff2ccfb88b35ec40aa7e8ee97f38` | `d310119f` | `source-checkout` | REJECTED |

All four embedded builds were re-read from the archived payload
(`app/_internal/release/release.json`) — every one is
`source-checkout`, i.e. the onedir was never stamped and predates the
claimed revision. The claimed revision is the archive's declared name;
it must not be conflated with what the defective payload contains.
The earlier `678c33f4…` candidate bytes no longer exist (overwritten) —
its payload cannot be independently inspected.

## Evidence accounting

**Valid (source-level, unaffected):** all pytest runs — 194 / 198 /
208 / 219 pass sequences exercise repository source directly, never a
frozen binary. Every feature/behaviour they cover stands at the
revisions they ran.

**Withdrawn for the four archives:** any packaged-runtime claim for
behaviour introduced after ~08:35 — installer readiness attribution
(`/healthz`, install_id, build verification), dispatch-boundary guard,
KRE reject-on-gate + gating artifact, CAS schedule claim. Those code
paths were not inside the shipped binaries.

**Retained, attributed to the actual stale executable (~pre-`0c18a0c`
code):** migration chain to `c4d5e6f7a8b9` under scrubbed PATH;
fixture worker dispatch → completed execution + JUnit/log artifacts;
Dulwich git-sync → synced binding. These features existed in that
binary and are evidence only for it.

**Re-verified on `-frozen-10300832`** (embedded = claimed build):
`install-info` offline; `install` → explicit INCOMPLETE on a free
port; fixture worker `processed: 2` (suite passed + artifacts;
KRE-gated run rejected with `logs/kre-gating.log` artifact, no
invocation log — no launch); Dulwich sync `status: synced` — all with
system git/python scrubbed.

## Fix + guard

`build_bundle.py` now collects `dist/testorchestrator` and
`check_frozen_fresh()` aborts the bundle when the onedir's embedded
`release.json` build ≠ repo stamp. `dist/frozen/` deleted.

**Limits of the guard:** a build-stamp match is an *identity* check —
it proves the onedir was built after the stamp, not that every bundled
code file is current. It must be paired with the smoke checks that
exercise the packaged binary.
