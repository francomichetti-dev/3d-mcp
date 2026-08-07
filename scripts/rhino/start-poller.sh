#!/usr/bin/env bash
# Start the poller inside a running Rhino (macOS / Linux).
#
# Rhino must already be open AND the ScriptEditor command run once this
# session - that is what loads Rhino's scripting host. Without it RhinoCode
# reports it cannot find a running Rhino.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_rhinocode() {
    for p in "/Applications/Rhino 8.app/Contents/Resources/bin/RhinoCode" \
             "/Applications/Rhino 8.app/Contents/MacOS/RhinoCode" \
             "$HOME/Applications/Rhino 8.app/Contents/Resources/bin/RhinoCode"; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    command -v rhinocode && return 0
    return 1
}

RC="$(find_rhinocode)" || {
    cat >&2 <<'MSG'

  RhinoCode was not found.

  It ships with Rhino 8. If Rhino is installed somewhere unusual, run the
  poller from inside Rhino instead: open ScriptEditor and run
  scripts/rhino-poller.py directly.

MSG
    exit 1
}

echo "starting the poller via $RC"
"$RC" script "$HERE/rhino-poller.py" || {
    cat >&2 <<'MSG'

  RhinoCode could not reach Rhino.

  Open Rhino and run the ScriptEditor command once, then run this again. The
  first ever run builds Rhino's Python environment and takes about a minute -
  it is not frozen.

MSG
    exit 1
}
