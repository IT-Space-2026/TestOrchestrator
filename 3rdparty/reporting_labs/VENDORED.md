# Vendored: reporting-labs 0.1.14

Upstream package : reporting-labs == 0.1.14 (PyPI, `reporting_labs` import name)
Upstream project : https://reportinglabs.dev (author: Naveen Khunteta)
Licence          : MIT — see LICENSE in this directory (retained verbatim)
Vendored on      : 2026-11-16, branch feature/2.0-reporting-labs
Reason           : Offline, reproducible installer — TestOrchestrator 2.0 must
                   not download reporting-labs from PyPI at install time.

Contents are the unmodified upstream wheel source (py3-none-any) plus this
file and LICENSE. The upstream pytest entry point
(`[pytest11] reporting_labs`) is not registered by vendoring; the repo's
pytest.ini already disables that plugin (`-p no:reporting_labs`).

Upgrade procedure: replace 3rdparty/reporting_labs/ with the new wheel's `reporting_labs`
package contents, update the version above and LICENSE if upstream re-issued
it, and update the pinned version assertion in scripts/validate_package.py.
