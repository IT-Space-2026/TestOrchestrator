#!/bin/sh
# TestOrchestrator installer bootstrap — locates payload, validates
# platform/permissions, dispatches to the packaged implementation.
#
# Two layouts are supported:
#   frozen package  — app/toctl binary present: `toctl install` does
#                     everything (no external python/pip/venv at all).
#   source package  — bundled python-runtime: legacy scripts/install.py
#                     path (kept as working fallback until the frozen
#                     candidate replaces it).
#
# Flags are forwarded to the active implementation. Exit codes:
#   0 ok · 1 usage/platform/permission · 2 install failure · 3 readiness
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

echo "IT-Space TestOrchestrator installer" >&2

fail() { echo "install.sh: $*" >&2; exit "${2:-1}"; }

[ "$(uname -s)" = "Linux" ] || fail "unsupported OS: $(uname -s) (package targets Linux)"
[ "$(uname -m)" = "x86_64" ] || fail "unsupported arch: $(uname -m) (package is linux-x86_64)"

# Frozen layout: ./app/toctl
if [ -x "$DIR/app/toctl" ]; then
    case " $* " in
        *" --dry-run "*|*" --help "*) ;;
        *) [ "$(id -u)" = "0" ] || echo "install.sh: NOTE — not root; service enable/start skipped." >&2 ;;
    esac
    exec "$DIR/app/toctl" install "$@"
fi

# Source layout: bundled python + install.py
PY="$DIR/3rdparty/python-runtime/python/bin/python3"
[ -x "$PY" ] || fail "no installer entry: expected app/toctl or bundled Python" 2
[ -f "$DIR/scripts/install.py" ] || fail "scripts/install.py missing — run inside the extracted package" 2
case " $* " in
    *" --dry-run "*|*" --help "*) ;;
    *) [ "$(id -u)" = "0" ] || echo "install.sh: NOTE — not root; service enable/start skipped (dev mode)." >&2 ;;
esac
exec "$PY" "$DIR/scripts/install.py" "$@"
