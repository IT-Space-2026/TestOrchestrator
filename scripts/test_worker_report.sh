#!/usr/bin/env bash
# Worker -> generated report -> portal view/download — v2 FOR APPROVAL.
# Loopback-only fixture: Dagu coordinator + labelled worker + isolated
# portal + Dulwich git fixture + fixture KRE (unlicensed).
#
# Proves: portal -> coordinator -> labelled worker -> checkout ->
# fixture KRE invocation -> artifacts return -> normalize ->
# report ready -> /report (200) + /report.zip + /artifacts.zip.
# Does NOT prove: licensed KRE, real-KRE Reports layout, Allure, or
# private-ssh checkout — fixture KRE and file:// repo only.
set -euo pipefail
umask 077
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN="wr$(date +%s)-$$"
ROOT="$(mktemp -d /tmp/to-wr-XXXXXX)"
HTTP_PORT=29987; GRPC_PORT=50055; WEB_PORT=39992
ADMIN_PASS='DevOnly-Admin!1'
EVID="$ROOT/evidence"; mkdir -p "$EVID"
FAILED=0
fail() { echo "FAIL: $*" | tee -a "$EVID/assertions.txt"; FAILED=1; }
note() { echo "== $*" | tee -a "$EVID/assertions.txt"; }

# --- environment isolation ----------------------------------------------
# No inherited app/dagu/git/credential config can redirect this run.
for v in $(env | grep -oE '^(TO_|DAGU_|GIT_|SSH_)[A-Z_]*' \
          | grep -vE 'TO_INSTANCE|TO_DATABASE|TO_SECURE|TO_SECRET|FLASK_APP'); do
  unset "$v"; done
export GIT_CONFIG_NOSYSTEM=1 HOME="$ROOT/home"
mkdir -p "$ROOT"/{home,coord/{bin,home/dags},worker,ws,portal,proj,kre}
git config --global user.email "fixture@localhost" \
             --global user.name "fixture" 2>/dev/null || true

# --- tool + dependency checks -------------------------------------------
for t in tar curl ss unzip python3; do
  command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }
done
[ -x "$REPO/.venv/bin/waitress-serve" ] \
  && [ -x "$REPO/.venv/bin/flask" ] \
  || { echo "venv executables missing"; exit 1; }
[ -f "$REPO/3rdparty/dagu/dagu_2.25.8_linux_amd64.tar.gz" ] \
  || { echo "dagu archive missing"; exit 1; }
ss -tln | grep -qE ":(29987|50055|39992) " \
  && { echo "COLLISION: fixture port in use"; exit 1; }

# --- owned process tracking: per-run process groups ----------------------
PIDS=()
spawn() { # spawn NAME CMD...
  local name="$1"; shift
  setsid env "TO_RUN=$RUN" "$@" &
  local pid=$!
  PIDS+=("$pid")
  echo "$pid $name" >> "$EVID/processes.txt"
}
cleanup() {
  set +e
  while read -r pid name; do
    [ -n "$pid" ] && [ -e "/proc/$pid/cmdline" ] && \
      grep -qa "$RUN" "/proc/$pid/environ" 2>/dev/null && \
      kill -- "-$pid" 2>/dev/null
  done < "$EVID/processes.txt" 2>/dev/null
  sleep 1
  local survivors=0
  while read -r pid name; do
    [ -e "/proc/$pid/cmdline" ] 2>/dev/null && { survivors=1;
      echo "survivor: $pid $name"; }
  done < "$EVID/processes.txt" 2>/dev/null
  [ "$survivors" = 0 ] && echo "[cleanup] all fixture processes stopped" \
    || echo "[cleanup] WARNING survivors remain (above)"
  echo "[cleanup] evidence preserved in $EVID (0600)"
}
trap cleanup EXIT

# --- coordinator on loopback, per-run home ------------------------------
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
tar xzf "$REPO/3rdparty/dagu/dagu_2.25.8_linux_amd64.tar.gz" \
  -C "$ROOT/coord/bin" dagu
spawn coordinator env DAGU_HOME="$ROOT/coord/home" \
  "$ROOT/coord/bin/dagu" start-all --port $HTTP_PORT \
  --host 127.0.0.1 --config "$ROOT/coord/server.yaml" \
  --dags "$ROOT/coord/home/dags" >>"$ROOT/coord/coord.log" 2>&1
# bounded readiness poll — never a fixed sleep
for i in $(seq 1 20); do
  curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/health" \
    -o /dev/null && break; sleep 1
done
curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/health" \
  -o /dev/null || { echo "coordinator never healthy"; exit 1; }
ss -tln | grep -q ":$GRPC_PORT " \
  || { echo "coordinator gRPC listener missing"; exit 1; }
ss -tln | awk '{print $4}' | grep -qvE "127.0.0.1|::1" \
  && echo "[coord] listeners loopback-checked" || true
note "coordinator up ($HTTP_PORT/$GRPC_PORT)"

# --- fixture project via Dulwich helper (no git executable dependency) --
( cd "$REPO" && .venv/bin/python - "$ROOT/proj" <<'PY'
import sys
from tests.git_helpers import init_repo, commit_file
init_repo(sys.argv[1])
commit_file(sys.argv[1], "fixture.ts",
            "[fixture test suite]\n")
PY
)

# --- fixture KRE (unlicensed; per-run marker in all outputs) -------------
cat > "$ROOT/kre/katalonc" <<KRE
#!/usr/bin/env bash
report=""; for a in "\$@"; do
  case "\$a" in -reportFolder=*) report="\${a#-reportFolder=}";; esac; done
mkdir -p "\$report/TS"
printf '<testsuite tests="2" failures="0" name="TS">\
<testcase classname="TS" name="tc-a-$RUN" time="0.1"/>\
<testcase classname="TS" name="tc-b-$RUN" time="0.2"/>\
</testsuite>\n' > "\$report/TS/junit.xml"
printf '<!DOCTYPE html><html><body><h1>Fixture report $RUN</h1>\
<p>2 passed</p></body></html>\n' > "\$report/index.html"
echo "[fixture-kre $RUN] wrote junit + report"; exit 0
KRE
chmod +x "$ROOT/kre/katalonc"

# --- worker on loopback, per-run id/home --------------------------------
spawn worker env DAGU_HOME="$ROOT/worker/home" \
  "$ROOT/coord/bin/dagu" worker \
  --worker.coordinators=127.0.0.1:$GRPC_PORT \
  --worker.labels "kre=$RUN" --worker.id "wr-$RUN" \
  --worker.health-port 0 >>"$ROOT/worker/worker.log" 2>&1
for i in $(seq 1 20); do
  curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/workers" \
    | grep -q "wr-$RUN" && break; sleep 1
done
curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/workers" \
  | grep -q "wr-$RUN" \
  || { echo "worker not registered"; tail -8 "$ROOT/worker/worker.log"; exit 1; }
note "worker wr-$RUN registered (label kre=$RUN)"

# --- isolated portal bound to THIS coordinator ---------------------------
export TO_INSTANCE_DIR="$ROOT/portal" \
       TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" \
       TO_SECURE_COOKIES=0 TO_SECRET_KEY="wr-$RUN-secret" \
       TO_DAGU_URL="http://127.0.0.1:$HTTP_PORT" \
       FLASK_APP="$REPO/wsgi.py"
( cd "$REPO" && .venv/bin/flask db upgrade > /dev/null )
( cd "$REPO" && .venv/bin/flask set-admin-password "$ADMIN_PASS" \
    > /dev/null )
spawn portal env TO_INSTANCE_DIR="$ROOT/portal" \
  TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" \
  TO_SECURE_COOKIES=0 TO_SECRET_KEY="wr-$RUN-secret" \
  TO_DAGU_URL="http://127.0.0.1:$HTTP_PORT" \
  FLASK_APP="$REPO/wsgi.py" \
  "$REPO/.venv/bin/waitress-serve" --listen="127.0.0.1:$WEB_PORT" wsgi:app \
  >"$ROOT/portal/portal.log" 2>&1
for i in $(seq 1 25); do
  curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$WEB_PORT/login" \
    && break; sleep 1
done
curl -sf --max-time 2 -o /dev/null "http://127.0.0.1:$WEB_PORT/login" \
  || { echo "portal never ready"; tail -8 "$ROOT/portal/portal.log"; exit 1; }
note "portal ready ($WEB_PORT)"

# --- login (CSRF by field name, verify success) --------------------------
CJ="$ROOT/cookies"
TOK=$(curl -sf --max-time 5 -c "$CJ" "http://127.0.0.1:$WEB_PORT/login" \
  | grep -oP 'name="csrf_token"[^>]*value="\K[^"]+' | head -1)
[ -n "$TOK" ] || { echo "no csrf token"; exit 1; }
code=$(curl -s --max-time 5 -b "$CJ" -c "$CJ" -o /dev/null \
  -w "%{http_code}" -X POST "http://127.0.0.1:$WEB_PORT/login" \
  -d "csrf_token=$TOK&username=Admin&password=$ADMIN_PASS")
[ "$code" = 302 ] || [ "$code" = 200 ] \
  || { echo "login failed ($code)"; exit 1; }
note "admin login ok"

# --- register binding+worker+runtime, create+dispatch execution ----------
SHA=$(cd "$REPO" && .venv/bin/python - "$ROOT/proj" <<'PY'
import sys
from tests.git_helpers import head_sha
print(head_sha(sys.argv[1]))
PY
)
EXID=$(cd "$REPO" && TO_INSTANCE_DIR="$ROOT/portal" TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" .venv/bin/python - <<PY
from testorchestrator import create_app
from testorchestrator.extensions import db
from testorchestrator.execution import (Execution, Project, Runner,
    RuntimeConnection, GitBinding, create_execution)
app = create_app()
with app.app_context():
    p = Project(name="wr"); db.session.add(p); db.session.flush()
    b = GitBinding(project_id=p.id, name="wr",
                   url="file://$ROOT/proj", ref="$SHA")
    db.session.add(b)
    r = Runner(name="wr-$RUN", connection_mode="remote",
               os_arch="linux/amd64", max_slots=1)
    db.session.add(r); db.session.flush()
    rt = RuntimeConnection(name="wr-kre", runner_id=r.id,
        transport="dagu-worker",
        executable_path="$ROOT/kre/katalonc",
        workspace_root="$ROOT/ws", worker_label="$RUN",
        enabled=True)
    db.session.add(rt); db.session.commit()
    ex = create_execution(project_id=p.id, binding_id=b.id,
                          runtime_id=rt.id,
                          run_config={"worker_label": "$RUN"})
    db.session.commit(); print(ex.id)
PY
)
note "execution $EXID dispatched (run marker $RUN)"

# --- bounded terminal-state poll -----------------------------------------
S=""
for i in $(seq 1 40); do
  S=$(cd "$REPO" && TO_INSTANCE_DIR="$ROOT/portal" TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" .venv/bin/python -c "
from testorchestrator import create_app
from testorchestrator.execution import Execution
a=create_app()
with a.app_context():
    e=Execution.query.get('$EXID')
    print(e.state.value, e.outcome.value, e.report_status)")
  case "$S" in completed*|failed*|canceled*) break;; esac; sleep 3
done
echo "[exec] final: $S" | tee -a "$EVID/execution.txt"
# assert SUCCESS not merely terminal
case "$S" in
  completed*ready*|*"completed passed ready"*)
    note "completed + report ready";;
  *) fail "expected completed+ready, got '$S'";;
esac
# two passed test cases from THIS run (run marker in names)
CASES=$(cd "$REPO" && TO_INSTANCE_DIR="$ROOT/portal" TO_DATABASE_URI="sqlite:///$ROOT/portal/p.db" .venv/bin/python -c "
from testorchestrator import create_app
from testorchestrator.execution import TestResult
a=create_app()
with a.app_context():
    n=TestResult.query.filter_by(execution_id='$EXID',
                                 status='passed').count()
    print(n)")
[ "$CASES" = "2" ] && note "2 passed test cases (run-specific)" \
  || fail "expected 2 passed cases, got $CASES"

# --- routes: assert 200 + run-specific content ---------------------------
for ep in "report" "report.zip" "artifacts.zip"; do
  code=$(curl -s --max-time 10 -b "$CJ" -o "$ROOT/$ep" -w "%{http_code}" \
    "http://127.0.0.1:$WEB_PORT/executions/$EXID/$ep")
  [ "$code" = 200 ] && note "$ep: 200" \
    || fail "$ep: got $code"
done
grep -q "$RUN" "$ROOT/report" \
  && note "/report contains run marker" \
  || fail "/report lacks run marker $RUN"
unzip -l "$ROOT/report.zip" | grep -q "index.html" \
  && note "report.zip has index.html" \
  || fail "report.zip missing index.html"
unzip -p "$ROOT/report.zip" index.html 2>/dev/null | grep -q "$RUN" \
  && note "report.zip content is run-specific" \
  || fail "report.zip index.html lacks run marker"
unzip -l "$ROOT/artifacts.zip" | grep -q "junit.xml" \
  && note "artifacts.zip has junit.xml" \
  || fail "artifacts.zip missing junit.xml"
unzip -p "$ROOT/artifacts.zip" "TS/junit.xml" 2>/dev/null \
  | grep -q "tc-a-$RUN" \
  && note "junit contains run-specific test names" \
  || fail "junit lacks run marker"

# --- preserve evidence ----------------------------------------------------
cp "$ROOT/coord/coord.log" "$EVID/coordinator.log" 2>/dev/null
cp "$ROOT/worker/worker.log" "$EVID/worker.log" 2>/dev/null
cp "$ROOT/portal/portal.log" "$EVID/portal.log" 2>/dev/null
cp "$ROOT/report" "$EVID/report.html" 2>/dev/null
cp "$ROOT"/{report.zip,artifacts.zip} "$EVID/" 2>/dev/null
[ "$FAILED" = 0 ] && echo "PASS (evidence: $EVID)" \
  || { echo "FAIL (evidence: $EVID)"; exit 1; }
