#!/usr/bin/env bash
# REGISTRATION-ONLY diagnostic — coordinator + worker on loopback.
# Asserts /api/v1/workers contains THIS worker-id AND its kre=$RUN label
# structurally. No portal, dispatch, KRE, execution, or external access.
set -euo pipefail
umask 077
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN="reg$(date +%s)-$$"
ROOT="$(mktemp -d /tmp/to-reg-XXXXXX)"
HTTP_PORT=29987; GRPC_PORT=50055
EVID="$ROOT/evidence"; mkdir -p "$EVID"; chmod 700 "$EVID"
: > "$EVID/processes.txt"
DEADLINE=$((SECONDS + 100))   # hard 120s incl. cleanup
for v in $(env | grep -oE '^(TO_|DAGU_|GIT_|SSH_|FLASK_|XDG_)[A-Z_]*'); do unset "$v"; done
export GIT_CONFIG_NOSYSTEM=1 HOME="$ROOT/home"
mkdir -p "$ROOT"/{home,coord/{bin,home/dags},worker}
for t in tar curl ss python3 setsid jq; do
  command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }
done
[ -f "$REPO/3rdparty/dagu/dagu_2.25.8_linux_amd64.tar.gz" ] || { echo "dagu archive missing"; exit 1; }
ss -tln | grep -qE ":(29987|50055) " && { echo "COLLISION"; exit 1; }
spawn() { local name="$1"; shift
  setsid env "TO_RUN=$RUN" "$@" & echo "$! $name" >> "$EVID/processes.txt"; }
owned_members() { for pid in $(pgrep -g "$1" 2>/dev/null); do
  tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx "TO_RUN=$RUN" && echo "$pid"; done; }
cleanup() { set +e
  for pgid in $(awk '{print $1}' "$EVID/processes.txt" 2>/dev/null); do
    for pid in $(owned_members "$pgid"); do kill "$pid" 2>/dev/null; done; done
  local left=999 i
  for i in $(seq 1 8); do sleep 0.5
    left=$(for pgid in $(awk '{print $1}' "$EVID/processes.txt"); do owned_members "$pgid"; done | wc -l)
    [ "$left" = 0 ] && break; done
  cp "$ROOT/coord/coord.log" "$EVID/coordinator.log" 2>/dev/null || true
  cp "$ROOT/worker/worker.log" "$EVID/worker.log" 2>/dev/null || true
  echo "cleanup: $left survivors; evidence $EVID"; }
trap cleanup EXIT

cat > "$ROOT/coord/server.yaml" <<YAML
auth:
  mode: none
coordinator:
  enabled: true
  host: 127.0.0.1
  port: "$GRPC_PORT"
  advertise: 127.0.0.1
host: 127.0.0.1
YAML
tar xzf "$REPO/3rdparty/dagu/dagu_2.25.8_linux_amd64.tar.gz" -C "$ROOT/coord/bin" dagu
"$ROOT/coord/bin/dagu" --version | tee "$EVID/dagu-version.txt"
spawn coordinator env DAGU_HOME="$ROOT/coord/home" \
  "$ROOT/coord/bin/dagu" start-all --port $HTTP_PORT \
  --host 127.0.0.1 --config "$ROOT/coord/server.yaml" \
  --dags "$ROOT/coord/home/dags" >>"$ROOT/coord/coord.log" 2>&1
for i in $(seq 1 15); do
  curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/health" -o /dev/null && break; sleep 1
  [ $SECONDS -ge $DEADLINE ] && { echo "timeout"; exit 1; }
done
curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/health" -o /dev/null \
  || { echo "coordinator never healthy"; exit 1; }
echo "== coordinator up" | tee -a "$EVID/result.txt"

spawn worker env DAGU_HOME="$ROOT/worker/home" \
  "$ROOT/coord/bin/dagu" worker \
  --worker.coordinators=127.0.0.1:$GRPC_PORT \
  --worker.labels "kre=$RUN" --worker.id "wr-$RUN" \
  --worker.health-port 0 >>"$ROOT/worker/worker.log" 2>&1

# structural check: worker-id AND labels, not substring-anywhere
OK=0
for i in $(seq 1 60); do
  [ $SECONDS -ge $DEADLINE ] && break
  BODY=$(curl -s --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v1/workers" || true)
  echo "$BODY" > "$EVID/workers-body.json"
  if [ -n "$BODY" ] && echo "$BODY" | jq -e \
     'type=="object" or type=="array"' >/dev/null 2>&1; then
    # match the exact worker-id AND the kre=$RUN label in the schema
    WID=$(echo "$BODY" | jq -r \
      '[.. | objects | .id? // .worker_id? // .workerId? // empty] | map(select(.=="wr-'"$RUN"'")) | length' 2>/dev/null || echo 0)
    LBL=$(echo "$BODY" | jq -r \
      '[.. | objects | .labels? // empty | to_entries[]? | select(.key=="kre" and .value=="'"$RUN"'")] | length' 2>/dev/null || echo 0)
    if [ "${WID:-0}" -ge 1 ] && [ "${LBL:-0}" -ge 1 ]; then OK=1; break; fi
  fi
  sleep 1
done
echo "== worker-id match:$WID label match:$LBL" | tee -a "$EVID/result.txt"
[ "$OK" = 1 ] && { echo "REGISTERED wr-$RUN kre=$RUN"; exit 0; } \
  || { echo "not registered"; exit 1; }
