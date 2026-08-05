"""Phase 0 probe: verifies Fusion loads a symlinked add-in folder on this Mac.

On load it writes ~/.fusion-mcp/symlink-test.json with the Fusion version and a
timestamp. No UI, no listener — if the file appears after launching Fusion, the
symlink install mechanism works and Phase 0 step 2 is verified.
"""

import datetime
import json
import os


def _write_marker(info):
    d = os.path.expanduser("~/.fusion-mcp")
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "symlink-test.json")
    with open(path, "w") as f:
        json.dump(info, f, indent=2)
    os.chmod(path, 0o600)


def run(context):
    info = {"loaded_at": datetime.datetime.now().isoformat(), "via": "symlink"}
    try:
        import adsk.core

        app = adsk.core.Application.get()
        info["fusion_version"] = app.version
    except Exception as e:  # still prove the load happened even if adsk fails
        info["error"] = repr(e)
    try:
        _write_marker(info)
    except Exception:
        pass  # nothing to report to — Fusion swallows add-in exceptions


def stop(context):
    pass
