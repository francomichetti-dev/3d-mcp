"""Stop the probe timer and detach the Idle handler left by the tests."""

import os
import traceback

import Rhino
import scriptcontext

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "rhino-timer-stop-output.txt")

with open(LOG, "w", encoding="utf-8") as log:
    def say(text):
        print(text)
        log.write(str(text) + "\n")
        log.flush()

    try:
        timer = scriptcontext.sticky.pop("_3d_mcp_timer_probe", None)
        if timer is not None:
            timer.Stop()
            say("timer stopped")
        else:
            say("no timer was running")

        handler = scriptcontext.sticky.pop("_3d_mcp_idle_probe", None)
        if handler is not None:
            try:
                Rhino.RhinoApp.Idle -= handler
                say("idle handler detached")
            except Exception as exc:
                say(f"idle detach failed: {exc!r}")
        else:
            say("no idle handler attached")
        say("CLEAN")
    except Exception:
        say("!!! FAILED")
        say(traceback.format_exc())
