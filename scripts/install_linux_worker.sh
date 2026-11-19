#!/usr/bin/env bash
# TestOrchestrator Buildbot worker — native Linux host install.
#
# Reproducible, offline-capable worker installation: creates a python
# venv, installs the pinned buildbot-worker, creates the basedir
# (buildbot.tac) bound to the controller's PB endpoint, and registers
# a systemd unit. Registration in the portal (Admin -> Execution
# Targets) is a separate step — the worker name + password must match
# the target record the administrator created.
#
# Usage (as root, or with sudo):
#   install_linux_worker.sh --master portal.example.com:9989 \
#       --name bb-linux-01 --passwd-file /etc/testorchestrator/worker-pw
#
# Options:
#   --master HOST:PORT      controller PB endpoint (required)
#   --name NAME             worker name registered in the portal (required)
#   --passwd-file PATH      file whose first line is the worker password
#                           (required; not echoed, not stored verbatim —
#                           it is baked into buildbot.tac by create-worker)
#   --wheels DIR            offline wheels dir (default: ./bb-wheels)
#   --online                resolve deps from PyPI instead of --wheels
#   --user USER             service account (default: to-worker)
#   --home DIR              install root (default: /opt/to-buildbot-worker)
#   --data DIR              worker basedir (default: /var/lib/to-buildbot-worker)
#   --no-start              install but do not enable/start the unit
#
# Uninstall: systemctl disable --now to-buildbot-worker; remove the unit
# and $HOME/$DATA (config/workspaces only deleted explicitly).
set -euo pipefail

BUILDBOT_VERSION="4.3.0"
MASTER="" NAME="" PASSWD_FILE=""
WHEELS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bb-wheels"
ONLINE=0 SVC_USER="to-worker"
HOME_DIR="/opt/to-buildbot-worker"
DATA_DIR="/var/lib/to-buildbot-worker"
NO_START=0

while [ $# -gt 0 ]; do
  case "$1" in
    --master) MASTER="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --passwd-file) PASSWD_FILE="$2"; shift 2;;
    --wheels) WHEELS="$2"; shift 2;;
    --online) ONLINE=1; shift;;
    --user) SVC_USER="$2"; shift 2;;
    --home) HOME_DIR="$2"; shift 2;;
    --data) DATA_DIR="$2"; shift 2;;
    --no-start) NO_START=1; shift;;
    -h|--help) sed -n '2,33p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

for req in MASTER NAME PASSWD_FILE; do
  eval "v=\$$req"
  [ -n "$v" ] || { echo "missing --${req,,/_/-}" >&2; exit 2; }
done
[ -r "$PASSWD_FILE" ] || { echo "passwd file unreadable: $PASSWD_FILE" >&2; exit 2; }
[ -s "$PASSWD_FILE" ] || { echo "passwd file empty: $PASSWD_FILE — put the one-time worker password issued by the portal on its first line" >&2; exit 2; }

command -v python3 >/dev/null || { echo "python3 >=3.10 required" >&2; exit 2; }
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" || { echo "python3 >=3.10 required (bundled wheels cover 3.10-3.12)" >&2; exit 2; }
if [ "$ONLINE" -eq 0 ] && [ ! -d "$WHEELS" ]; then
  echo "offline wheels dir missing: $WHEELS (or pass --online)" >&2; exit 2
fi

# Dedicated least-privilege account — never reuse root or a login user.
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin \
      --comment "TestOrchestrator Buildbot worker" "$SVC_USER"
fi
mkdir -p "$HOME_DIR" "$DATA_DIR"
chown "$SVC_USER":"$SVC_USER" "$DATA_DIR"

# Persistent install log — stdout/stderr continue to the console too.
exec > >(tee "$DATA_DIR/worker-install.log") 2>&1

# venv + pinned worker (offline wheels or PyPI — same pin either way).
# Debian/Ubuntu hosts may lack ensurepip (python3-venv): fall back to
# --without-pip + the bundled get-pip.py (offline — it embeds pip).
if ! python3 -m venv "$HOME_DIR/venv" 2>/dev/null; then
  GET_PIP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/get-pip.py"
  [ -f "$GET_PIP" ] || { echo "venv ensurepip missing and no bundled get-pip.py" >&2; exit 2; }
  python3 -m venv --without-pip "$HOME_DIR/venv"
  "$HOME_DIR/venv/bin/python" "$GET_PIP" --quiet
fi
PIP=("$HOME_DIR/venv/bin/pip" --no-input)
if [ "$ONLINE" -eq 1 ]; then
  "${PIP[@]}" install "buildbot-worker==$BUILDBOT_VERSION"
else
  "${PIP[@]}" install --no-index --find-links "$WHEELS" \
      "buildbot-worker==$BUILDBOT_VERSION"
fi

BBW="$HOME_DIR/venv/bin/buildbot-worker"
if [ ! -f "$DATA_DIR/buildbot.tac" ]; then
  PASSWD="$(head -n1 "$PASSWD_FILE" | tr -d '\r\n')"
  sudo -u "$SVC_USER" "$BBW" create-worker --keepalive=60 \
      "$DATA_DIR" "$MASTER" "$NAME" "$PASSWD"
else
  echo "[worker] existing buildbot.tac preserved — identity unchanged"
fi
unset PASSWD
chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"

UNIT=/etc/systemd/system/to-buildbot-worker.service
cat > "$UNIT" <<EOF
[Unit]
Description=TestOrchestrator Buildbot worker ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$DATA_DIR
ExecStart=$HOME_DIR/venv/bin/buildbot-worker start --nodaemon $DATA_DIR
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

if [ "$NO_START" -eq 0 ] && command -v systemctl >/dev/null; then
  systemctl daemon-reload
  systemctl enable --now to-buildbot-worker
  echo "[worker] enabled+started: to-buildbot-worker"
  echo "[worker] logs: journalctl -u to-buildbot-worker -f"
else
  echo "[worker] unit staged: $UNIT — systemctl enable --now to-buildbot-worker"
fi
echo "[worker] '$NAME' -> $MASTER. Portal shows Awaiting registration "
echo "         until the PB connection lands (Admin -> Execution Targets)."
