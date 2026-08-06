"""Stop the Rhino poller started by rhino-poller.py."""

import os
import time
import traceback

import scriptcontext

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rhino-poller.log")

try:
    timer = scriptcontext.sticky.pop("_3d_mcp_rhino_poller", None)
    if timer is not None:
        timer.Stop()
        message = "poller stopped"
    else:
        message = "no poller was running"
except Exception:                                            # noqa: BLE001
    message = "stop failed:\n" + traceback.format_exc()

try:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%H:%M:%S')}  {message}\n")
except Exception:
    pass
print(message)
