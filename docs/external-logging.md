# External logging — vendor-neutral collector integration

TestOrchestrator produces structured events for both log categories:

```
Structured application events
    ├── Local storage + Admin pages (authoritative)
    ├── CSV export          (Admin → Audit Trail / Operational Logs)
    └── Optional JSON output (NDJSON file / stdout)
            → Customer-managed collector → customer's log server
```

The database remains the **authoritative** store. JSON output is an
opt-in copy; a sink failure is counted and stdlib-logged, never raised
into `emit()` and never allowed to weaken audit persistence or test
dispatch.

## 1. Event schema — `to.log.event/1.0`

One line = one event = one JSON object (`logschema.py`).

| Field | Meaning |
|---|---|
| `schema_version` | `to.log.event/1.0`. Additive fields may appear in patch releases — ignore unknown keys. Removing/retyping a field bumps the version. |
| `event_id` | Unique per event (uuid4 hex; `audit-<id>`/`op-<id>` for DB rows). Preserved end-to-end — **deduplicate on this**. |
| `timestamp_utc` | Event time, ISO-8601, explicit `Z`. |
| `log_type` | `audit` \| `operational` — always distinguishable. |
| `severity` | `debug` `info` `warning` `error` `critical` (audit = `info`). |
| `category` | Category within the type's vocabulary (operational: system/dispatch/execution/report/worker/auth/config/storage; audit: `audit`). |
| `event_name` | Dotted action name (audit rows) or `""`. |
| `message` | Human summary — redacted before persistence. |
| `outcome` | `success`/`failure`/`denied`/`""` — blank when not modelled. |
| `component` | Emitting component (portal, scheduler, dispatch, …). |
| `host`, `instance`, `revision` | Hostname, instance-dir name, deployed git head / build stamp. |
| `actor_id`, `actor_name` | Accountable actor when the source row has one. |
| `project_id`, `execution_id`, `parent_execution_id`, `unit_id`, `attempt_id`, `target_id`, `buildbot_id`, `request_id` | Correlation identifiers — emitted only when present; never invented. |
| `trace_id`, `span_id` | Optional — only when a caller already holds tracing context. |
| `details` | Structured object. Secret-keyed values are redacted. Request bodies and execution environments are **not** included. |

The shape is straightforward to map to OpenTelemetry (timestamp,
severity, attributes, trace/span). It is **not** OTLP — do not feed it
to an OTLP parser.

## 2. Optional JSON output

Disabled until configured. Enable via Admin-managed settings or env:

| Setting | Env | Default | Meaning |
|---|---|---|---|
| `logs_json_enabled` | `TO_LOGS_JSON_ENABLED` | off | Write NDJSON to a rotating file. |
| `logs_json_dir` | `TO_LOGS_JSON_DIR` | `<instance>/logs` | Output directory. |
| `logs_json_max_bytes` | — | 10 MiB | Rotate `events.jsonl` past this. |
| `logs_json_max_files` | — | 5 | Rotated files kept (disk bound). |
| `logs_stdout_enabled` | `TO_LOGS_STDOUT_ENABLED` | off | Also write each line to stdout (container/systemd collection). |

- Active file: `events.jsonl`; rotated:
  `events-<UTCts>-<ns>-<pid>-<seq>.jsonl` (nanosecond + sequence suffix
  — two rotations inside one second cannot collide and clobber a file).
- **Multi-process**: each line is one O_APPEND write — atomic across
  processes. Concurrent rotation may yield one extra rotated file —
  bounded by `max_files`.
- **Per-file bound**: a file rotates once it reaches `max_bytes`; a
  single event larger than the cap is still written whole, so a file
  may exceed `max_bytes` by at most one record.
- **Health**: `log_output.status()` reports `written`, `dropped`,
  `rotated`, `last_error`, `last_write_at` — **per-process counters**
  (not aggregated). The Operational Logs page footer shows the serving
  process's counters when a sink is enabled; each process's own values
  also appear in its stderr on failure — never through `emit()`, which
  is what prevents recursive logging-failure loops.
- **Delivery guarantees — precisely**: best-effort, at-least-once
  *while the file exists*. Lost events: (a) a sink write failure —
  detectable via the `dropped` counter; (b) a file rotated/deleted
  before the collector read it — **not detectable downstream**; bound
  retention generously or accept the gap (the database is
  authoritative — CSV export can always recover history). Duplicate
  events: possible when the collector loses its checkpoint and
  re-tails a file — **the filelog receiver does not deduplicate**;
  consumers must deduplicate on `event_id` (preserved as a log
  attribute). Our example config does not add a dedup stage.
- **Raw framework stdout/stderr is not forwarded** here. Execution
  output stays in the run's evidence download; enabling `logs_stdout_*`
  forwards only the structured schema events. Forwarding raw test
  output is a separate future opt-in — it carries volume and
  sensitivity risks (tests print secrets) and is off by design.

## 3. Reference collector

`deploy/otel-collector.example.yaml` — file_log receiver → JSON parse →
timestamp/severity mapping → batch → your exporter. Requires
opentelemetry-collector-contrib.

**Verified against the real distribution (otelcol-contrib v0.165.0)** —
the shipped config, unmodified except test paths: `timestamp_utc` parsed
to the record timestamp, `severity` mapped (`warning`→`Warn(13)`,
`info`→`Info(9)`), and `event_id` + all correlation fields preserved as
log attributes. Restart/checkpoint behavior, measured:

- Events appended to a file the receiver already tracks are recovered
  from the file_storage checkpoint after a collector restart.
- A **new** file created while the collector is down starts at `end` —
  content already in it is skipped (that's the documented
  "rotated-before-read is lost" gap; the DB is the recovery path).
- Live appends to a discovered file are picked up continuously.
- The config provides **no deduplication** — downstream consumers
  deduplicate on `event_id`. (The unit-suite "test collector" fixture
  verifies our producer contract; it does not stand in for these
  real-collector results.)

The collector needs **read-only** access to the output directory.
Permissions on the log server itself are administered there —
TestOrchestrator project membership does **not** propagate to external
systems.

## 4. CSV export

Admin → Audit Trail / Admin → Operational Logs → **Export CSV** exports
**all records matching the current filters** (time range, search,
category, severity, actor, project, execution, correlation — as
applicable), not just the rendered page. Scope + matching count are
shown on the page; large exports run as background jobs whose status
page shows filters, cutoff, row count, and expiry.

- Fixed `cutoff_utc` captured at request time — rows arriving
  mid-export are excluded; ordering is `occurred_at DESC, id DESC`.
- UTF-8 (BOM) CSV with headers; timestamps explicit UTC. Commas,
  quotes, Unicode and multiline values are quoted; cells beginning
  `= + - @ <tab> <CR>` are additionally `'`‑prefixed — spreadsheet
  formula injection protection beyond ordinary quoting.
- Limits: `logs_export_inline_max` (10 000) streams inline; above that
  a background job writes a `0600` file under `<instance>/log-exports`,
  downloadable for `logs_export_ttl_minutes` (60 min) by an
  Administrator (role re-checked at download). Above
  `logs_export_max_rows` (250 000) the request is **refused** — narrow
  the filters; exports never silently truncate.
- File protection: the `0600` mode is the Linux mechanism, verified by
  test. On Windows the export directory sits under the instance dir,
  which is protected by the service-account ACL the installer applies —
  the Windows W-matrix check verifies export-file ACLs on the real host
  (the Linux `0600` test is not counted as Windows evidence).
- Every request, completion/failure and download is audited with
  filters, count, actor and export id — not exported contents.

Columns: `event_id, timestamp_utc, log_type, severity, category,
event_name, actor_id, actor_name, project_id, execution_id,
parent_execution_id, unit_id, attempt_id, target_id, request_id,
outcome, message, details_json` — adjusted by category (fields a log
type does not model are empty).

## 5. Security & privacy

- Secrets are redacted **before persistence** (`logs.redact`), so local
  rows, CSV and JSON all share the same redaction: passwords, tokens,
  API keys, private/signing/licence keys, client secrets, credentials,
  authorization/cookie/bearer values — including quoted/JSON-embedded
  keys and `Bearer <token>` free text. `details` objects are redacted by
  key recursively.
- **Limit**: redaction is pattern-based. It covers the key names and
  shapes listed above; a secret stored under an unrecognized key name,
  or in a format the patterns do not recognize, is not guaranteed to be
  caught. Do not log credential values at all where avoidable —
  redaction is a safety net, not a substitute.
- Personal data: audit rows carry `actor_name`; operational rows carry
  project/execution identifiers. Retention is `logs_retention_days`
  (30 d) + `logs_max_rows` (20 000) for operational logs; audit rows
  are retained with the database. Exported CSVs expire per
  `logs_export_ttl_minutes`; forwarded copies follow the customer's
  log-server retention, administered separately.
- Global Audit Trail / Operational Logs views and exports are
  Administrator-only; backend queries, export creation, job status and
  downloads are all authorized. An export never exposes more than its
  authorized source query. Role revocation prevents subsequent export
  access (including downloads of previously created jobs).
