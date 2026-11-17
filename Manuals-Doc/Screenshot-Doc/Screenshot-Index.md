# Screenshot Index — TestOrchestrator 2.0.2

- **Source version:** released 2.0.2 (tag `v2.0.2`, commit
  `574860d08124411692b141643da0ad87e576100d`; package
  `testorchestrator-2.0.2-linux-x86_64.tar.gz`,
  SHA-256 `af08a60d…f7e`)
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

**Not captured:** the licence page (excluded per documentation rules —
licence details are not published), and destructive-action
confirmations (deletion/cancel dialogs were exercised via code paths,
not staged for capture).
