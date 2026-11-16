# Decision: enhanced reporting backend (v1.9.0)

> **Superseded for 2.0** — see `docs/reporting-labs-migration.md`:
> reporting-labs `0.1.14` is the sole enhanced backend; Allure 3 +
> bundled Node.js are removed. Historical Allure report files are
> preserved on disk but not openable in the 2.0 interface.

**Decision (v1.9.0):** Retain **Allure** as the only shipped enhanced reporting
backend. **pyrept 1.3.3 is NOT integrated** and is not a replacement at
this time.

## What was evaluated

Hands-on conversion (isolated env, `pyrept convert --from allure` and
`--from junit`) on synthetic fixtures with passed/failed/skipped cases,
failure traces, durations, suite names, a screenshot attachment, and a
retry pair (same `historyId`). Sample outputs:
`docs/eval/pyrept-sample-allure.html`, `docs/eval/pyrept-sample-junit.html`.

## Why pyrept is not integrated

| Capability | Result |
|---|---|
| Outcomes / durations / suite names | Convert correctly |
| Failure message + traceback | Preserved |
| Retry lineage | Degrades: folded by `historyId` into `retries`/`flaky` — per-attempt detail lost |
| **Attachments / screenshots** | **Lost: not imported at all** |
| KRE-native reports | Not an input path (allure-results / JUnit only) |
| Offline HTML / Java-free / MIT license | Works / yes / compatible |

Attachment loss is disqualifying today: screenshots and logs are
release-blocking evidence in this product (§9). Per-attempt traceback
loss conflicts with the retry-evidence requirement.

## Re-evaluation triggers

- pyrept gains attachment import (`*-attachment*` in allure-results);
- per-attempt retry detail in the folded model;
- then: frozen-build generation test (pure-Python wheel, jinja2 only —
  expected packagable).

Frozen pyrept generation is **not** a release gate while pyrept is
outside delivery scope. Allure remains the sole enhanced backend;
native KRE reports and canonical JUnit/artifact evidence are unaffected
and always-on.
