#!/bin/sh
# TestOrchestrator installer bootstrap (§13) — minimal shell wrapper.
# Python (scripts/install.py) remains the implementation for install,
# validation, configuration, and recovery; this wrapper only locates
# the payload, validates platform/permissions, and invokes the BUNDLED
# Python — no manually created venv, no host python needed.
#
# Options are forwarded to scripts/install.py:
#   --payload DIR --data DIR --config DIR --https-port N --http-port N
#   --java-home DIR | --no-allure | --no-service-start | --dry-run
#   --yes (noninteractive: never prompts; fails instead)
#
# Exit codes: 0 ok · 1 usage/platform/permission · 2 install failure
#             3 services failed to reach ready state
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY="$DIR/3rdparty/python-runtime/python/bin/python3"

fail() { echo "install.sh: $*" >&2; exit "${2:-1}"; }

[ "$(uname -s)" = "Linux" ] || fail "unsupported OS: $(uname -s) (this package targets Linux)"
[ "$(uname -m)" = "x86_64" ] || fail "unsupported arch: $(uname -m) (package is linux-x86_64)"
[ -x "$PY" ] || fail "bundled Python missing at $PY — package incomplete" 2
[ -f "$DIR/scripts/install.py" ] || fail "scripts/install.py missing — run from inside the extracted package" 2

case " $* " in
    *" --dry-run "*|*" --help "*) ;;
    *)
        if [ "$(id -u)" != "0" ]; then
            echo "install.sh: NOTE — not root; service enable/start and the" >&2
            echo "  service-account probe will be skipped (dev mode)." >&2
        fi ;;
esac

exec "$PY" "$DIR/scripts/install.py" "$@"
