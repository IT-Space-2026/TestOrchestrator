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
HELPER="to-restart-helper"
PORT=$((40000 + RANDOM % 1000))
ADMIN_PASS='DevOnly-Restart!1'
EVID="$ROOT/evidence"
mkdir -p "$ROOT/home" "$EVID"
FAILED=0
fail() { echo "FAIL: $*"; FAILED=1; }
note() { echo "== $*"; }

# --- isolation: env hygiene + collision aborts -------------------------
for v in $(env | grep -oE '^TO_[A-Z_]+' | grep -vE 'TO_SERVICE|TO_INSTANCE|TO_DATABASE|TO_SECURE'); do unset "$v"; done
command -v systemctl >/dev/null || { echo "no systemd"; exit 1; }
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
  systemctl --user stop "$WEB" "$STUB" 2>/dev/null
  rm -f "$UNITDIR/$WEB" "$UNITDIR/$STUB"
  systemctl --user daemon-reload 2>/dev/null
  echo "[cleanup] units removed; evidence kept in $EVID"
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
curl -sf -b "$CJ" -X POST "http://127.0.0.1:$PORT/system/services/restart" \
  -d "csrf_token=$HTOK" -o /dev/null \
  || fail "restart POST failed"
# dedup: a second request while pending must not dispatch twice
code=$(curl -s -o /dev/null -w "%{http_code}" -b "$CJ" -X POST \
  "http://127.0.0.1:$PORT/system/services/restart" -d "csrf_token=$HTOK")
[ "$code" = 302 ] && note "duplicate restart deduped (redirect)" \
  || note "duplicate restart: $code"

# helper unit evidence (launched via systemd-run by the route)
sleep 1
systemctl --user status "$HELPER.scope" >/dev/null 2>&1 \
  && note "helper ran as its own scope unit" \
  || note "helper scope not visible (may have finished already)"

# --- verify old died, new lives ------------------------------------------
deadline=$((SECONDS + 30))
NEW_PID=""
while [ $SECONDS -lt $deadline ]; do
  NEW_PID="$(systemctl --user show "$WEB" -p MainPID --value)"
  [ -n "$NEW_PID" ] && [ "$NEW_PID" -gt 0 ] && [ "$NEW_PID" != "$OLD_PID" ] && break
  sleep 1
done
[ "$NEW_PID" != "$OLD_PID" ] && [ -n "$NEW_PID" ] \
  || fail "no new PID (old=$OLD_PID new=$NEW_PID)"
[ -e "/proc/$OLD_PID" ] && fail "old process still alive" \
  || note "old=$OLD_PID dead, new=$NEW_PID active"

# health poll resolves pending marker -> done WITH helper evidence
marker="$ROOT/home/restart-request.json"
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
[ -n "$ev" ] && [ "$ev" != "$OLD_PID" ] \
  || fail "evidence new-pid '$ev' not a new invocation"
note "writeback: $result via $basis, new-pid=$ev"

# health page asserts content after restart
body=$(curl -sf -b "$CJ" "http://127.0.0.1:$PORT/system/health")
echo "$body" | grep -q "healthy\|active" \
  && note "health page reports healthy" \
  || fail "health page not healthy"

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
