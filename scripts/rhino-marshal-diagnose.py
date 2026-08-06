"""Does InvokeOnUiThread's callback ever run at all?

The mini-bridge showed a listener DOES survive inside Rhino, but any request
that marshaled work never came back. Two very different explanations:

  A. the callback never runs   -> InvokeOnUiThread is not usable from here
  B. it runs, but the caller's wait never completes -> a threading problem

Telling them apart decides whether a Rhino bridge is possible at all, so this
never waits on the callback. It fires and returns; the callback writes to a file
by itself. Checking the file afterwards gives the answer with no chance of the
deadlock that hung three earlier attempts.

Three ways of firing it are compared, because they may not behave alike:
  1. directly from this script (which IS the UI thread)
  2. from a background thread, nobody waiting
  3. via RhinoApp.Idle, an event Rhino raises on its own loop
"""

import os
import threading
import time
import traceback

import Rhino

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "rhino-marshal-output.txt")
MARK = os.path.join(HERE, "rhino-marshal-marks.txt")

_log = open(LOG, "w", encoding="utf-8")


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


def mark(tag):
    """Called from inside a callback. Appends, so nothing is lost."""
    try:
        with open(MARK, "a", encoding="utf-8") as handle:
            handle.write(f"{tag}\tthread={threading.get_ident()}\t{time.time():.3f}\n")
    except Exception:
        pass


try:
    if os.path.exists(MARK):
        os.remove(MARK)

    say("InvokeOnUiThread diagnosis")
    say("=" * 58)
    say(f"script thread   : {threading.get_ident()}")
    say(f"InvokeRequired  : {Rhino.RhinoApp.InvokeRequired}")
    say("")

    # --- 1. fired from the UI thread itself ------------------------------
    say("1. fired directly from this script (the UI thread)")
    try:
        Rhino.RhinoApp.InvokeOnUiThread(lambda: mark("direct"))
        say("   call returned without raising")
    except Exception as exc:                                # noqa: BLE001
        say(f"   RAISED: {exc!r}")

    # --- 2. fired from a background thread, nobody waiting ----------------
    say("")
    say("2. fired from a background thread, nobody waiting on it")

    def fire_from_thread():
        mark("worker-alive")
        try:
            Rhino.RhinoApp.InvokeOnUiThread(lambda: mark("from-thread"))
            mark("worker-called-invoke")
        except Exception as exc:                            # noqa: BLE001
            mark(f"worker-raised {exc!r}")

    worker = threading.Thread(target=fire_from_thread, daemon=True)
    worker.start()
    worker.join(5.0)
    say(f"   worker finished : {not worker.is_alive()}")

    # --- 3. via the Idle event -------------------------------------------
    # If direct invokes are never serviced, Idle is the usual fallback: Rhino
    # raises it from its own loop, so a handler there runs on the UI thread
    # without anyone having to marshal.
    say("")
    say("3. via RhinoApp.Idle")
    idle_state = {"handler": None}

    def on_idle(sender, args):
        mark("idle")
        try:
            Rhino.RhinoApp.Idle -= idle_state["handler"]
        except Exception:
            pass

    try:
        idle_state["handler"] = on_idle
        Rhino.RhinoApp.Idle += on_idle
        say("   handler attached")
    except Exception as exc:                                # noqa: BLE001
        say(f"   could not attach: {exc!r}")

    # Give Rhino a moment of its own loop. Deliberately NOT a pumping loop:
    # this script has to end for the UI thread to be free at all.
    say("")
    say("returning now - Rhino's loop gets the thread back.")
    say(f"marks so far are in {MARK}")
    say("DONE")

except Exception:                                           # noqa: BLE001
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    try:
        _log.close()
    except Exception:
        pass
