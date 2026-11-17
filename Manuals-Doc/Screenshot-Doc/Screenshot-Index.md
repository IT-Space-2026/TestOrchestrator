# Screenshot Index — TestOrchestrator 2.0.2

- **Source version:** released 2.0.2 (tag `v2.0.2`, commit
  `574860d08124411692b141643da0ad87e576100d`; package
  `testorchestrator-2.0.2-linux-x86_64.tar.gz`,
  SHA-256 `af08a60d…f7e`). **Exceptions:** `admin/install-*.png`
  (fresh 2.0.3 install), `admin/licence.png` and
  `user/release-detail.png` were captured on released **2.0.3**
  (tag `v2.0.3`, commit `be5d1335bb82e9e49566a4d1f42fa710dd2cf6de`)
  after the licence-label and scope-picker fixes.
- **Captured:** 2026-11-17, Chromium headless at 1440×900, real
  running installation at `http://127.0.0.1:8095`
- **Environment:** isolated documentation install
  (`/home/szefi/to202-docs`) — see
  [../Validation-Record.md](../Validation-Record.md)
- **Data notes:** project "Katalon-cert Demo" is a real Katalon
  project with **genuine Katalon 11.7.0 report output** (imported);
  the live run used a clearly-labelled **fixture** KRE stand-in.
  All users, credentials, and values are demonstration-only — no
  real secrets appear in any frame.

## user/ — application usage

| File | Shows |
|---|---|
| `login.png` | Sign-in page |
| `dashboard.png` | Dashboard: live tiles, project summary, outcome/duration charts |
| `projects.png` | Projects list |
| `project-detail.png` | Project dashboard tab: executions stats, inventory state (94 cases · 14 suites · 3 collections), synced SHA |
| `project-dashboard.png` | Project overview (tabs: Dashboard, Releases, Test, Schedules-Executions, Inventory, Git bindings) |
| `import-new.png` | Historical-result import form (binding, revision, result directory) |
| `import-preview-duplicates.png` | Import preview: resolved commit, `duplicate` decisions with matched executions, collection-grouping warning |
| `discovered-suite-run.png` | Suite item: member cases + Run form (profile, release, worker, runtime, timeout); authoring-disabled notice |
| `discovered-collection.png` | Test-suite collection item |
| `execution-history.png` | Executions list with full filter bar (imported + live origins, outcomes) |
| `execution-detail-imported.png` | Imported execution: provenance (importer, repo, commit, source path, parser), 4 results incl. supplemented suite |
| `execution-detail-failed.png` | Imported rerun with one failed case (`<failure>` message) |
| `execution-detail-live.png` | Live worker run (fixture KRE): worker, dagu run id, runtime, results |
| `execution-rlabs.png` | reportingLabs generated report (4 passed, environment card) |
| `suite-new.png` | New suite form: name, description, source binding, two-panel case selector (6 selected) |
| `suite-detail.png` | Suite detail: ordered entries, version/hash, run controls |
| `release-new.png` | New release form: name, description, start/target dates |
| `release-detail.png` | Release detail: scope table (1 suite + 1 collection), `not yet executed` states, scopeable-only picker *(2.0.3)* |
| `schedule-new.png` | New schedule form: target selector, kind, UTC first run, interval, profile/browser/runner/runtime/overlap |
| `schedules.png` | Schedules list: next run, enabled toggle, run-now, edit |
| `about.png` | About page |

## admin/ — system pages

| File | Shows |
|---|---|
| `settings.png` | Settings landing: task-oriented configuration areas |
| `health.png` | Health & services status |
| `network.png` | Network & certificates configuration |
| `users.png` | User administration: roles, temp-password reset, deactivate, create |
| `infrastructure.png` | Workers table (runner, mode, slots, agent, drain) + KRE installations (remote executable, `Remote — not verified`) |
| `secrets.png` | Credentials: named secret references, system-managed coordinator credential — values never shown |
| `integrations.png` | Integrations configuration |
| `logs.png` | Operational logs |
| `audit.png` | Audit events |
| `changelog.png` | Changelog view |
| `runner-new.png` | Add worker form: name, description, OS/arch, connection mode, workspace, slots, allowed projects |
| `runtime-new.png` | Register-KRE form: execution location, remote runner, worker label, executable/workspace paths, credentials, concurrency |
| `user-new.png` | Create user form: username, display name, role radios, temporary password |
| `licence.png` | Licence administration: status, activated-licence table, add-code field — informational status display *(2.0.3)*; demonstration licence only |
| `login-unsupported.png` | Login page with no valid application licence — exact notice *"This version is not supported"*, form fully usable *(2.0.4 candidate)* |
| `install-transcript.png` | Real `install.sh` output: optional licence seed, payload/data/config paths, Dagu provisioning, staged units *(2.0.3 dev-mode install)* |
| `install-dashboard.png` | Fresh install — dashboard at first login, no data yet *(2.0.3)* |
| `install-users.png` | Fresh install — user list containing only the bootstrap Admin *(2.0.3)* |
| `install-infrastructure.png` | Fresh install — empty workers/KRE tables; worker download card with pinned version + SHA-256 *(2.0.3)* |
| `install-health.png` | Fresh install — services shown `not installed` until staged units are enabled *(2.0.3)* |
| `reporting.png` | Reporting settings: reportingLabs toggle, log retention bounds, git evidence-export fields |
| `storage-layout.png` | Real `du`/`ls` of the instance data dir — per-directory footprint for capacity planning |
| `evidence-prune.png` | `toctl evidence-prune` dry-run output — the supported orphan-evidence cleanup |
| `dr-restored-executions.png` | DR restore proof — execution history intact on a restored copy of the instance *(2.0.3)* |
| `dr-restored-release.png` | DR restore proof — release + scope intact on the restored instance *(2.0.3)* |
| `dataflow-diagram.png` | Rendered data-flow diagram for §1 — **generated asset, not a UI capture**; source `dataflow-diagram.mmd` (also embedded as a mermaid block in the manual) |
| `architecture-diagram.png` | Rendered component/deployment diagram for §1 — **generated asset, not a UI capture**; source `architecture-diagram.mmd` |

**Not captured:** destructive-action
confirmations (deletion/cancel dialogs were exercised via code paths,
not staged for capture).
