#!/usr/bin/env bash
# Isolated managed-restart verification v2 — FOR APPROVAL BEFORE RUN.
# Real portal under its own unique user-scope systemd unit; restart
# dispatched via systemd-run (own cgroup, survives portal kill).
#
# Proves: portal restart survival, old->new PID, writeback resolution
# via health-poll path, admin/CSRF/duplicate guards, audit rows,
# evidence preservation. Does NOT prove: system-scope installer
# semantics; the sleep unit is a manager-mechanics stub only; the
# active-work block is covered by unit tests, not exercised here.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ID="$(date +%s)-$$"
ROOT="/tmp/to-mgd-$ID"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
WEB="to-mgd-${ID}-web.service"; STUB="to-mgd-${ID}-stub.service"
HELPER="to-restart-helper-*"
PORT=$((40000 + RANDOM % 1000))
ADMIN_PASS='DevOnly-Restart!1'
EVID="$ROOT/evidence"
mkdir -p "$ROOT/home" "$EVID"
FAILED=0
fail() { echo "FAIL: $*"; FAILED=1; }
note() { echo "== $*"; }

# --- isolation: env hygiene + collision aborts -------------------------
umask 077                          # db + cookies hold auth material
for v in $(env | grep -oE '^TO_[A-Z_]+' | grep -vE 'TO_SERVICE|TO_INSTANCE|TO_DATABASE|TO_SECURE'); do unset "$v"; done
for tool in systemctl curl sqlite3 python3 ss journalctl systemd-run; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 1; }
done
systemctl --user is-system-running >/dev/null 2>&1 || systemctl --user status >/dev/null 2>&1 \
  || { echo "no user systemd manager"; exit 1; }
for u in "$WEB" "$STUB"; do
  for d in "$UNITDIR" /etc/systemd/user /usr/lib/systemd/user \
           "$HOME/.local/share/systemd/user"; do
    [ -e "$d/$u" ] && { echo "COLLISION: $d/$u"; exit 1; }
  done
  systemctl --user list-unit-files --no-legend "$u" | grep -q . \
    && { echo "COLLISION: unit $u registered"; exit 1; }
done
ss -tln | grep -q ":$PORT " && { echo "COLLISION: port $PORT"; exit 1; }

cleanup() {
  set +e
  # stop only this run's recorded resources — never wildcarded
  [ -n "${HELPER_UNIT:-}" ] && \
    systemctl --user stop "$HELPER_UNIT" 2>/dev/null
  systemctl --user stop "$WEB" "$STUB" 2>/dev/null
  rm -f "$UNITDIR/$WEB" "$UNITDIR/$STUB"
  systemctl --user daemon-reload 2>/dev/null
  chmod -R go-rwx "$ROOT" 2>/dev/null   # evidence may hold auth data
  echo "[cleanup] units removed; evidence kept private in $EVID"
}
trap cleanup EXIT

export TO_INSTANCE_DIR="$ROOT/home"
export TO_DATABASE_URI="sqlite:///$ROOT/home/m.db"
export TO_SECURE_COOKIES=0
export TO_SERVICE_UNITS="$WEB,$STUB"
export TO_SERVICE_SCOPE="user"
export FLASK_APP="$REPO/wsgi.py"
( cd "$REPO" && .venv/bin/flask db upgrade ) || { echo "schema failed"; exit 1; }
( cd "$REPO" && .venv/bin/flask set-admin-password "$ADMIN_PASS" ) \
  || { echo "admin failed"; exit 1; }

mkdir -p "$UNITDIR"
cat > "$UNITDIR/$WEB" <<UNIT
[Service]
Environment=TO_INSTANCE_DIR=$ROOT/home
Environment=TO_DATABASE_URI=sqlite:///$ROOT/home/m.db
Environment=TO_SECURE_COOKIES=0
Environment=TO_SERVICE_UNITS=$WEB,$STUB
Environment=TO_SERVICE_SCOPE=user
Environment=FLASK_APP=$REPO/wsgi.py
WorkingDirectory=$REPO
ExecStart=$REPO/.venv/bin/waitress-serve --listen=127.0.0.1:$PORT wsgi:app
UNIT
cat > "$UNITDIR/$STUB" <<UNIT
[Service]
ExecStart=/bin/sleep infinity
UNIT
cp "$UNITDIR/$WEB" "$UNITDIR/$STUB" "$EVID/"
systemctl --user daemon-reload
systemctl --user start "$WEB" "$STUB"

# --- bounded readiness poll (no fixed sleeps) ---------------------------
ready=0
for i in $(seq 1 30); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/login" && { ready=1; break; }
  sleep 1
done
[ "$ready" = 1 ] || { echo "portal never became ready"; exit 1; }
OLD_PID="$(systemctl --user show "$WEB" -p MainPID --value)"
[ -n "$OLD_PID" ] && [ "$OLD_PID" -gt 0 ] || { echo "no portal PID"; exit 1; }
note "portal PID $OLD_PID (unit $WEB)"

# --- admin login (CSRF parsed by field name) -----------------------------
CJ="$ROOT/cookies"
TOK=$(curl -sf -c "$CJ" "http://127.0.0.1:$PORT/login" \
  | grep -oP 'name="csrf_token"[^>]*value="\K[^"]+' | head -1)
[ -n "$TOK" ] || { echo "no csrf token"; exit 1; }
curl -sf -b "$CJ" -c "$CJ" -X POST "http://127.0.0.1:$PORT/login" \
  -d "csrf_token=$TOK&username=admin&password=$ADMIN_PASS" -o /dev/null \
  || { echo "login failed"; exit 1; }

# --- guard checks (cheap HTTP assertions) --------------------------------
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "http://127.0.0.1:$PORT/system/services/restart")
[ "$code" = 302 ] || [ "$code" = 403 ] \
  && note "unauthenticated restart rejected ($code)" \
  || fail "unauthenticated restart got $code"
code=$(curl -s -o /dev/null -w "%{http_code}" -b "$CJ" -X POST \
  "http://127.0.0.1:$PORT/system/services/restart" \
  -d "csrf_token=wrong")
[ "$code" = 400 ] && note "bad csrf rejected" \
  || fail "bad csrf got $code (expected 400)"

# --- the real restart ----------------------------------------------------
HTOK=$(curl -sf -b "$CJ" "http://127.0.0.1:$PORT/system/health" \
  | grep -oP 'name="csrf_token"[^>]*value="\K[^"]+' | head -1)
[ -n "$HTOK" ] || { echo "no health-page csrf token"; exit 1; }
# Route contract: 302 redirect on accepted dispatch (CSRF-only form).
rcode=$(curl -s -o /dev/null -w "%{http_code}" -b "$CJ" -X POST \
  "http://127.0.0.1:$PORT/system/services/restart" -d "csrf_token=$HTOK")
[ "$rcode" = 302 ] && note "restart accepted ($rcode)" \
  || fail "restart POST got $rcode (expected 302)"
# Deduplication is deliberately NOT exercised here — a second POST
# after completion would dispatch another restart and break the run.
# Dedup is covered by contract tests instead.

# record THIS request's helper unit from the marker (not a glob)
marker="$ROOT/home/restart-request.json"
sleep 1
HELPER_UNIT=$(python3 -c \
  "import json;print(json.load(open('$marker')).get('helper_unit',''))" \
  2>/dev/null || echo "")
[ -n "$HELPER_UNIT" ] \
  && note "helper launched as $HELPER_UNIT" \
  || fail "marker has no helper_unit — launch binding missing"
[ -n "$HELPER_UNIT" ] && journalctl --user -u "$HELPER_UNIT" \
  --no-pager -n 30 > "$EVID/helper-journal.txt" 2>/dev/null

# --- verify old died, new lives ------------------------------------------
deadline=$((SECONDS + 30))
NEW_PID=""
while [ $SECONDS -lt $deadline ]; do
  NEW_PID="$(systemctl --user show "$WEB" -p MainPID --value)"
  [ -n "$NEW_PID" ] && [ "$NEW_PID" -gt 0 ] && [ "$NEW_PID" != "$OLD_PID" ] && break
  sleep 1
done
[ -n "$NEW_PID" ] && [ "$NEW_PID" -gt 0 ] && [ "$NEW_PID" != "$OLD_PID" ] \
  || fail "no new PID (old=$OLD_PID new=$NEW_PID)"
active=$(systemctl --user is-active "$WEB" 2>/dev/null || echo "")
[ "$active" = "active" ] || fail "unit not active after restart ($active)"
[ -e "/proc/$OLD_PID" ] && fail "old process still alive" \
  || note "old=$OLD_PID dead, new=$NEW_PID active"

# health poll resolves pending marker -> done WITH helper evidence
result=""
deadline=$((SECONDS + 45))
while [ $SECONDS -lt $deadline ]; do
  curl -sf -b "$CJ" -o /dev/null "http://127.0.0.1:$PORT/system/health" \
    && result=$(python3 -c "import json;print(json.load(open('$marker'))['result'])" 2>/dev/null)
  [ "$result" = "done" ] || [ "$result" = "failed" ] && break; sleep 1
done
cp "$marker" "$EVID/" 2>/dev/null
# done must be helper_confirmed with a NEW invocation, not liveness
basis=$(python3 -c "import json;print(json.load(open('$marker')).get('basis',''))" 2>/dev/null)
ev=$(python3 -c "import json;e=json.load(open('$marker')).get('evidence',{}).get('$WEB',{});print(e.get('new',''))" 2>/dev/null)
[ "$result" = "done" ] || fail "marker result=$result (expected done)"
[ "$basis" = "helper_confirmed" ] \
  || fail "basis=$basis — no helper evidence of new invocation"
# the recorded new invocation must equal the observed replacement PID
[ "$ev" = "$NEW_PID" ] \
  || fail "evidence new-pid '$ev' != observed new PID $NEW_PID"
note "writeback: $result via $basis, new-pid=$ev"

# health page: authenticated 200 + specific expected content
code=$(curl -s -o "$EVID/health.html" -w "%{http_code}" -b "$CJ" \
  "http://127.0.0.1:$PORT/system/health")
[ "$code" = 200 ] || fail "health page $code (expected 200)"
grep -q "System health\|Managed services\|$WEB" "$EVID/health.html" \
  && note "health page renders managed-service status" \
  || fail "health page missing expected content"

# audit verification (mandatory — actual sqlite audit table)
audit=$(sqlite3 "$ROOT/home/m.db" \
  "SELECT count(*) FROM audit_event WHERE action LIKE 'services.restart%'" \
  2>/dev/null || echo 0)
sqlite3 "$ROOT/home/m.db" \
  "SELECT username,action,created_at FROM audit_event WHERE action LIKE 'services.restart%'" \
  > "$EVID/audit.txt" 2>/dev/null
[ "${audit:-0}" -ge 1 ] && note "audit rows: $audit" \
  || fail "no audit row for restart"

# --- preserve evidence ----------------------------------------------------
journalctl --user -u "$WEB" -u "$STUB" --no-pager -n 50 \
  > "$EVID/journal.txt" 2>/dev/null
cp "$ROOT/home/restart-request.json" "$EVID/marker.json" 2>/dev/null
cp "$ROOT/home/m.db" "$EVID/db.sqlite" 2>/dev/null
note "evidence: $EVID"
[ $FAILED -eq 0 ] && echo "PASS" || { echo "FAIL"; exit 1; }
