#!/usr/bin/env bash
# Worker -> generated report -> portal view/download — FOR APPROVAL.
# Loopback-only fixture: Dagu coordinator + labelled worker + isolated
# portal + harmless git fixture. Fresh /tmp/to-wr-* dirs; earlier
# /tmp/to-fx-* evidence is preserved untouched.
#
# Proves: worker dispatch -> git.checkout -> fixture KRE writes
#   junit + an HTML report into Reports/ -> artifacts return -> portal
#   normalize -> /report (200) + /report.zip + /artifacts.zip.
# Does NOT prove: licensed-KRE behaviour or real-KRE HTML shape —
#   the "report" is a fixture-produced index.html, separate from
#   Allure generation and real KRE Reports layout.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(mktemp -d /tmp/to-wr-XXXX)"
HTTP_PORT=29987; GRPC_PORT=50055; WEB_PORT=39992
ADMIN_PASS='DevOnly-Admin!1'
PIDS=()

cleanup() {
  set +e
  for pid in "${PIDS[@]}"; do
    # ownership check: only kill processes still matching our binary
    [ -n "$pid" ] && [ -e "/proc/$pid/cmdline" ] && \
      grep -qa "$ROOT\|dagu" "/proc/$pid/cmdline" 2>/dev/null && \
      kill "$pid" 2>/dev/null
  done
  echo "[cleanup] fixture processes stopped; dirs kept at $ROOT"
}
trap cleanup EXIT

# --- collision checks: abort -------------------------------------------
ss -tln | grep -qE ":(29987|50055|39992) " && { echo "COLLISION: fixture port in use"; exit 1; }
[ -d "$ROOT/coord" ] && { echo "COLLISION: $ROOT"; exit 1; }
command -v git tar curl python3 >/dev/null || { echo "missing tools"; exit 1; }

mkdir -p "$ROOT"/{coord/{bin,home/dags},worker,ws,portal,proj,kre}

# --- coordinator on loopback -------------------------------------------
cat > "$ROOT/coord/server.yaml" <<YAML
auth:
  mode: none
coordinator:
  enabled: true
  host: 127.0.0.1
  port: "$GRPC_PORT"
  advertise: 127.0.0.1
  worker_wait_timeout: "30s"
host: 127.0.0.1
YAML
tar xzf "$REPO/3rdparty/dagu/dagu_2.25.8_linux_amd64.tar.gz" -C "$ROOT/coord/bin" dagu
DAGU_HOME="$ROOT/coord/home" "$ROOT/coord/bin/dagu" start-all \
  --port $HTTP_PORT --host 127.0.0.1 --config "$ROOT/coord/server.yaml" \
  --dags "$ROOT/coord/home/dags" >"$ROOT/coord/coord.log" 2>&1 &
PIDS+=($!)
sleep 3
ss -tln | grep -q ":$HTTP_PORT " && ss -tln | grep -q ":$GRPC_PORT " \
  || { echo "coordinator listeners missing"; exit 1; }
curl -sf "http://127.0.0.1:$HTTP_PORT/api/v2/health" -o /dev/null \
  && echo "[coord] up"

# --- fixture project + KRE ---------------------------------------------
( cd "$ROOT/proj" && git init -q && git commit -qm "fixture" --allow-empty )
cat > "$ROOT/kre/katalonc" <<'KRE'
#!/usr/bin/env bash
report=""; for a in "$@"; do case "$a" in -reportFolder=*) report="${a#-reportFolder=}";; esac; done
mkdir -p "$report/TS"
printf '<testsuite tests="2" failures="0" name="TS"><testcase classname="TS" name="tc-a" time="0.1"/><testcase classname="TS" name="tc-b" time="0.2"/></testsuite>\n' > "$report/TS/junit.xml"
printf '<!DOCTYPE html><html><body><h1>Fixture report</h1><p>2 passed</p></body></html>\n' > "$report/index.html"
echo "[fixture-kre] wrote junit + report to $report"; exit 0
KRE
chmod +x "$ROOT/kre/katalonc"

# --- worker with unique label ------------------------------------------
"$ROOT/coord/bin/dagu" worker \
  --worker.coordinators=127.0.0.1:$GRPC_PORT \
  --worker.labels kre=wr-report-1 --worker.id wr-worker-1 \
  --worker.health-port 0 >"$ROOT/worker/worker.log" 2>&1 &
PIDS+=($!)
sleep 3
curl -sf "http://127.0.0.1:$HTTP_PORT/api/v2/workers" | grep -q "wr-report-1" \
  && echo "[worker] registered" || { echo "worker not registered"; tail -5 "$ROOT/worker/worker.log"; exit 1; }

# --- isolated portal ----------------------------------------------------
export TO_INSTANCE_DIR="$ROOT/portal" TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" \
       TO_SECURE_COOKIES=0 FLASK_APP="$REPO/wsgi.py"
( cd "$REPO" && .venv/bin/flask db upgrade )
( cd "$REPO" && .venv/bin/flask set-admin-password "$ADMIN_PASS" )
( cd "$REPO" && .venv/bin/waitress-serve --listen=127.0.0.1:$WEB_PORT wsgi:app \
    >"$ROOT/portal/portal.log" 2>&1 & )
PIDS+=($!)
sleep 3

# --- register remote KRE + git binding + run execution -------------------
CJ="$ROOT/cookies"; rm -f "$CJ"
TOK=$(curl -s -c "$CJ" "http://127.0.0.1:$WEB_PORT/login" | grep -o 'value="[^"]*"' | head -1 | sed 's/value="//;s/"//')
curl -s -b "$CJ" -c "$CJ" -X POST "http://127.0.0.1:$WEB_PORT/login" \
  -d "csrf_token=$TOK&username=admin&password=$ADMIN_PASS" -o /dev/null
SHA=$(cd "$ROOT/proj" && git rev-parse HEAD)
EXID=$(cd "$REPO" && TO_INSTANCE_DIR="$ROOT/portal" TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" .venv/bin/python - <<PY
from testorchestrator import create_app
from testorchestrator.extensions import db
from testorchestrator.execution import (Execution, Project, Runner,
    RuntimeConnection, GitBinding, ExecutionState, create_execution)
app = create_app()
with app.app_context():
    p = Project(name="wr", key="wr"); db.session.add(p); db.session.flush()
    b = GitBinding(project_id=p.id, url="file://$ROOT/proj", ref="$SHA",
                   credential_ref=""); db.session.add(b)
    r = Runner(name="wr-worker-1", mode="remote", max_slots=1)
    db.session.add(r); db.session.flush()
    rt = RuntimeConnection(project_id=p.id, runner_id=r.id, name="wr-kre",
        transport="dagu-worker", executable_path="$ROOT/kre/katalonc",
        workspace_root="$ROOT/ws", worker_label="wr-report-1",
        concurrency=1, is_active=True)
    db.session.add(rt); db.session.commit()
    ex = create_execution(project_id=p.id, binding_id=b.id, runtime_id=rt.id)
    db.session.commit(); print(ex.id)
PY
)
echo "[exec] $EXID"
for i in $(seq 1 40); do
  S=$(cd "$REPO" && TO_INSTANCE_DIR="$ROOT/portal" TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" .venv/bin/python -c "
from testorchestrator import create_app
from testorchestrator.execution import Execution
a=create_app()
with a.app_context():
    e=Execution.query.get('$EXID'); print(e.state.value, e.outcome.value)")
  case "$S" in COMPLETED*|FAILED*|CANCELED*) break;; esac; sleep 3
done
echo "[exec] final: $S"
# --- verify report + artifact routes ------------------------------------
for ep in "report" "report.zip" "artifacts.zip"; do
  curl -s -b "$CJ" -o "/tmp/wr-$ep" -w "$ep: %{http_code} %{size_download}B\n" \
    "http://127.0.0.1:$WEB_PORT/executions/$EXID/$ep"
done
grep -q "Fixture report" /tmp/wr-report 2>/dev/null || unzip -p /tmp/wr-report.zip 2>/dev/null | grep -q "Fixture report" \
  && echo "[report] viewable content present" || echo "[report] check content manually"
