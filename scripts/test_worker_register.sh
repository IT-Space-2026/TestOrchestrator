#!/usr/bin/env bash
# REGISTRATION-ONLY diagnostic (corrected cleanup).
# Evidence lives OUTSIDE the cleanup pattern; cleanup removes ONLY the
# recorded attempt working dir by exact path — never a glob.
set -euo pipefail
umask 077
REPO="/home/szefi/Documents/TestOrchestrator"
RUN="reg$(date +%s)-$$"
ROOT="$(mktemp -d /tmp/to-work-XXXXXX)"          # attempt working dir (removed)
EVID="$(mktemp -d /tmp/to-evid-XXXXXX)"          # evidence — OUTSIDE to-work-* pattern
chmod 700 "$EVID"
HTTP_PORT=29987; GRPC_PORT=50055
: > "$EVID/processes.txt"
DEADLINE=$((SECONDS + 100))
for v in $(env | grep -oE '^(TO_|DAGU_|GIT_|SSH_|FLASK_|XDG_)[A-Z_]*'); do unset "$v"; done
export GIT_CONFIG_NOSYSTEM=1 HOME="$ROOT/home"
mkdir -p "$ROOT"/{home,coord/{bin,home/dags},worker}
for t in tar curl ss python3 setsid jq; do command -v "$t" >/dev/null || { echo "missing: $t"; exit 1; }; done
[ -f "$REPO/3rdparty/dagu/dagu_2.25.8_linux_amd64.tar.gz" ] || { echo "archive missing"; exit 1; }
ss -tln | grep -qE ":(29987|50055) " && { echo COLLISION; exit 1; }
spawn() { local n="$1"; shift; setsid env "TO_RUN=$RUN" "$@" & echo "$! $n" >> "$EVID/processes.txt"; }
owned() { for pid in $(pgrep -g "$1" 2>/dev/null); do tr '\0' '\n' </proc/$pid/environ 2>/dev/null | grep -qx "TO_RUN=$RUN" && echo "$pid"; done; }

# SAFE cleanup: remove only the recorded working dir by exact path, after
# verifying it's a to-work-* dir we own — never a wildcard.
safe_cleanup() { set +e
  for g in $(awk '{print $1}' "$EVID/processes.txt" 2>/dev/null); do for pid in $(owned "$g"); do kill "$pid" 2>/dev/null; done; done
  local left=999; for i in $(seq 1 8); do sleep 0.5
    left=$(for g in $(awk '{print $1}' "$EVID/processes.txt"); do owned "$g"; done | wc -l); [ "$left" = 0 ] && break; done
  cp "$ROOT/coord/coord.log" "$EVID/coordinator.log" 2>/dev/null||true
  cp "$ROOT/worker/worker.log" "$EVID/worker.log" 2>/dev/null||true
  # remove ONLY the recorded ROOT (exact, owned, to-work-* prefix)
  case "$ROOT" in
    /tmp/to-work-*) rm -rf -- "$ROOT";;
    *) echo "cleanup: refusing unexpected target $ROOT" >> "$EVID/result.txt";;
  esac
  echo "cleanup: $left survivors; evidence $EVID (preserved)"; }
trap safe_cleanup EXIT

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
"$ROOT/coord/bin/dagu" version | tee "$EVID/dagu-version.txt"
spawn coordinator env DAGU_HOME="$ROOT/coord/home" "$ROOT/coord/bin/dagu" start-all --port $HTTP_PORT --host 127.0.0.1 --config "$ROOT/coord/server.yaml" --dags "$ROOT/coord/home/dags" >>"$ROOT/coord/coord.log" 2>&1
for i in $(seq 1 15); do curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/health" -o /dev/null && break; sleep 1; [ $SECONDS -ge $DEADLINE ] && { echo timeout; exit 1; }; done
curl -sf --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v2/health" -o /dev/null || { echo "coordinator never healthy"; exit 1; }
echo "== coordinator up" | tee -a "$EVID/result.txt"
spawn worker env DAGU_HOME="$ROOT/worker/home" "$ROOT/coord/bin/dagu" worker --worker.coordinators=127.0.0.1:$GRPC_PORT --worker.labels "kre=$RUN" --worker.id "wr-$RUN" --worker.health-port 0 >>"$ROOT/worker/worker.log" 2>&1

RAW=""; OK=0
for i in $(seq 1 60); do
  [ $SECONDS -ge $DEADLINE ] && break
  RAW=$(curl -s --max-time 2 "http://127.0.0.1:$HTTP_PORT/api/v1/workers" || true)
  echo "$RAW" > "$EVID/workers-body.json"
  [ -n "$RAW" ] && echo "$RAW" | jq -e . >/dev/null 2>&1 && {
    HIT=$(echo "$RAW" | jq -r --arg w "wr-$RUN" --arg k "$RUN" '
      def wlist: (if type=="array" then . else (.workers // .items // .data // .results // []) end);
      [ wlist[]? | objects | select((.id // .worker_id // .workerId // .name)==$w) | select((.labels // {})["kre"]==$k) ] | length' 2>/dev/null || echo 0)
    [ "${HIT:-0}" -ge 1 ] && { OK=1; break; }
  }
  sleep 1
done
echo "== same-object match: ${HIT:-0}" | tee -a "$EVID/result.txt"
[ "$OK" = 1 ] && { echo "REGISTERED wr-$RUN kre=$RUN"; exit 0; } || { echo "not registered"; exit 1; }
