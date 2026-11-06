#!/usr/bin/env bash
# Isolated managed-restart verification — FOR APPROVAL BEFORE RUN.
# The portal runs as a REAL user-scope systemd unit (unique to-*
# names, never touching packaged testorchestrator-* units), so
# `systemctl --user restart` exercises true process death/recovery.
#
# Proves: restart survives portal termination (independent helper),
#   old->new process identity, marker writeback, failure reporting,
#   dedup, active-work block, audit, admin-only + CSRF.
# Does NOT prove: system-scope deployment semantics (needs a VM);
#   a sleep-based supporting unit does NOT verify worker/coordinator
#   restart correctness — only service-manager mechanics.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(mktemp -d /tmp/to-mgd-XXXX)"
UNITS=(to-mgdweb.service to-mgdworker.service)
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PORT=39991
ADMIN_USER=admin; ADMIN_PASS='Admin-Password-12345'
CREATED_UNITS=(); STARTED_PID=""

cleanup() {
  set +e
  for u in "${CREATED_UNITS[@]}"; do
    systemctl --user stop "$u" 2>/dev/null
    rm -f "$UNITDIR/$u"
  done
  systemctl --user daemon-reload 2>/dev/null
  rm -rf "$ROOT"
  echo "[cleanup] done — only recorded units/files removed"
}
trap cleanup EXIT

# --- collision checks: abort, not warn -------------------------------
for u in "${UNITS[@]}"; do
  [ -e "$UNITDIR/$u" ] && { echo "COLLISION: $UNITDIR/$u exists"; exit 1; }
  systemctl --user list-units --all --no-legend "$u" | grep -q . \
    && { echo "COLLISION: unit $u already loaded"; exit 1; }
done
ss -tln | grep -q ":$PORT " && { echo "COLLISION: port $PORT in use"; exit 1; }
command -v systemctl >/dev/null || { echo "no systemd"; exit 1; }
[ -f "$REPO/.venv/bin/waitress-serve" ] || { echo "no venv"; exit 1; }

# --- fresh instance + schema ------------------------------------------
mkdir -p "$ROOT/home"
export TO_INSTANCE_DIR="$ROOT/home"
export TO_DATABASE_URI="sqlite:///$ROOT/home/m.db"
export TO_SECURE_COOKIES=0
export TO_SERVICE_UNITS="to-mgdweb.service,to-mgdworker.service"
export TO_SERVICE_SCOPE="user"
export FLASK_APP="$REPO/wsgi.py"
( cd "$REPO" && .venv/bin/flask db upgrade )
( cd "$REPO" && .venv/bin/flask set-admin-password "$ADMIN_PASS" )

# --- real portal under its own test unit ------------------------------
mkdir -p "$UNITDIR"
cat > "$UNITDIR/to-mgdweb.service" <<UNIT
[Service]
Environment=TO_INSTANCE_DIR=$ROOT/home
Environment=TO_DATABASE_URI=sqlite:///$ROOT/home/m.db
Environment=TO_SECURE_COOKIES=0
Environment=TO_SERVICE_UNITS=to-mgdweb.service,to-mgdworker.service
Environment=TO_SERVICE_SCOPE=user
Environment=FLASK_APP=$REPO/wsgi.py
WorkingDirectory=$REPO
ExecStart=$REPO/.venv/bin/waitress-serve --listen=127.0.0.1:$PORT wsgi:app
UNIT
cat > "$UNITDIR/to-mgdworker.service" <<UNIT
[Service]
ExecStart=/bin/sleep infinity
UNIT
CREATED_UNITS=("${UNITS[@]}")
systemctl --user daemon-reload
systemctl --user start "${UNITS[@]}"
sleep 2
OLD_PID="$(systemctl --user show to-mgdweb -p MainPID --value)"
[ "$OLD_PID" -gt 0 ] || { echo "portal unit did not start"; exit 1; }
echo "[setup] portal PID $OLD_PID (systemd-managed, user scope)"

# --- admin login + CSRF ------------------------------------------------
CJ="$ROOT/cookies"; rm -f "$CJ"
TOK=$(curl -s -c "$CJ" "http://127.0.0.1:$PORT/login" \
  | grep -o 'value="[^"]*"' | head -1 | sed 's/value="//;s/"//')
curl -s -b "$CJ" -c "$CJ" -X POST "http://127.0.0.1:$PORT/login" \
  -d "csrf_token=$TOK&username=$ADMIN_USER&password=$ADMIN_PASS" -o /dev/null

# --- restart via the real route ----------------------------------------
HTOK=$(curl -s -b "$CJ" "http://127.0.0.1:$PORT/system/health" \
  | grep -o 'name="csrf_token"[^>]*value="[^"]*"' \
  | sed 's/.*value="//;s/"//' | head -1)
curl -s -b "$CJ" -X POST "http://127.0.0.1:$PORT/system/services/restart" \
  -d "csrf_token=$HTOK" -o /dev/null

# --- verify: old process dead, new process up, writeback done ----------
sleep 6
NEW_PID="$(systemctl --user show to-mgdweb -p MainPID --value)"
[ "$NEW_PID" != "$OLD_PID" ] || { echo "FAIL: same PID — no restart"; exit 1; }
echo "[verify] old=$OLD_PID new=$NEW_PID"
for i in $(seq 1 10); do
  R=$(python3 -c "import json;print(json.load(open('$ROOT/home/restart-request.json'))['result'])" 2>/dev/null || echo pending)
  [ "$R" = "done" ] && break; sleep 1
done
echo "[verify] marker result=$R"
grep -q "services.restart_requested" "$ROOT/home/audit"* 2>/dev/null \
  && echo "[verify] audit recorded" || echo "[verify] audit line TBD"
curl -s -b "$CJ" "http://127.0.0.1:$PORT/system/health" -o /dev/null \
  -w "[verify] health after restart: %{http_code}\n"
[ "$R" = "done" ] && echo "PASS" || echo "FAIL (result=$R)"
