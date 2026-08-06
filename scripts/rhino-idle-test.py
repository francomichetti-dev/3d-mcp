"""Does RhinoApp.Idle fire, and is subscribing to it safe?

Deliberately the smallest possible test. Rhino has crashed repeatedly during
this spike - ucrtbase.dll, 0xc0000409, a C-runtime abort - and the prime
suspect is a background thread outliving the rhinocode script context. So this
has:

    no threads          nothing can outlive the script
    no sockets          nothing to block on
    no InvokeOnUiThread nothing to deadlock
    no geometry         nothing to undo

It attaches one handler that appends a line to a file, then returns. That is
all. If the file grows while Rhino sits untouched, Idle fires on its own loop
and the polling design is viable. If Rhino is still alive a minute later, an
Idle subscription is safe to hold.

Stop it with rhino-idle-stop.py, or just restart Rhino.
"""

import os
import threading
import time
import traceback

import Rhino
import scriptcontext

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS = os.path.join(HERE, "rhino-idle-ticks.txt")
LOG = os.path.join(HERE, "rhino-idle-test-output.txt")
KEY = "_3d_mcp_idle_probe"

_log = open(LOG, "w", encoding="utf-8")


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


# Module-level rather than closed over, so the stop script can find the same
# object and detach the exact handler that was attached.
_state = {"ticks": 0, "started": time.time(), "last_write": 0.0}


def on_idle(sender, args):
    """Runs on Rhino's UI thread, raised by Rhino's own message loop.

    Kept trivially cheap: Idle fires constantly, and anything slow here would
    make Rhino feel sluggish to whoever is using it.
    """
    _state["ticks"] += 1
    now = time.time()
    # Write at most once a second - the count matters, not every tick.
    if now - _state["last_write"] < 1.0:
        return
    _state["last_write"] = now
    try:
        with open(TICKS, "a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%H:%M:%S')}\tticks={_state['ticks']}\t"
                f"thread={threading.get_ident()}\t"
                f"uptime={now - _state['started']:.1f}s\n")
    except Exception:
        pass


try:
    if os.path.exists(TICKS):
        os.remove(TICKS)

    say("Idle probe")
    say("=" * 50)
    say(f"script thread : {threading.get_ident()}")
    say(f"on UI thread  : {not Rhino.RhinoApp.InvokeRequired}")

    # Detach a handler from an earlier run so ticks do not double up.
    previous = scriptcontext.sticky.get(KEY)
    if previous is not None:
        try:
            Rhino.RhinoApp.Idle -= previous
            say("  detached a handler from an earlier run")
        except Exception:                                   # noqa: BLE001
            pass

    Rhino.RhinoApp.Idle += on_idle
    scriptcontext.sticky[KEY] = on_idle
    say("  handler attached")
    say()
    say(f"  ticks will be appended to: {TICKS}")
    say("  returning now - nothing of ours is left running except the")
    say("  subscription itself, which is Rhino's own event")
    say("ATTACHED")

except Exception:                                           # noqa: BLE001
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    try:
        _log.close()
    except Exception:
        pass
