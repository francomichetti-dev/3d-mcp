#!/usr/bin/env bash
# Start the job broker (macOS / Linux). Leave this window open.
#
# Paths are DISCOVERED rather than hardcoded: Rhino's embedded Python moves
# between versions and installs, and a wrong hardcoded path fails with a
# confusing "no such file" instead of something actionable.
set -u

find_python() {
    for p in "$HOME"/.rhinocode/py*-rh*/bin/python3 \
             "$HOME"/.rhinocode/py*-rh*/python3 \
             "$HOME"/.rhinocode/py*-rh*/python; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    command -v python3 && return 0      # any Python 3 will do; it only speaks HTTP
    return 1
}

PY="$(find_python)" || {
    cat >&2 <<'MSG'

  No Python found.

  Open Rhino once and run the ScriptEditor command - that builds Rhino's
  Python. Or install any Python 3; the broker only needs the standard library.

MSG
    exit 1
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BROKER_PY="$HERE/../server/src/fusion_mcp/broker.py"
[ -f "$BROKER_PY" ] || BROKER_PY="$HERE/broker.py"
[ -f "$BROKER_PY" ] || { echo "cannot find broker.py next to this script" >&2; exit 1; }

echo "broker starting with $PY"
echo "leave this window open; press Ctrl-C to stop"
exec "$PY" -c "
import sys, time, os
sys.path.insert(0, os.path.dirname('$BROKER_PY'))
import broker
broker.serve()
print('broker listening on 127.0.0.1:%d' % broker.BROKER_PORT, flush=True)
while True:
    time.sleep(3600)
"
