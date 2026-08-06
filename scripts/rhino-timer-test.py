"""Can a UI-thread timer drive the poller, since Idle does not?

Idle was attached for 25 seconds while Rhino sat unfocused and fired ZERO
times. That is consistent with how .NET raises it - after the message loop
processes messages - so an application with no input never becomes "idle" in
that sense. Rhino did survive the subscription, so the mechanism is safe, just
inert.

A timer is the obvious replacement: it posts messages to the UI thread itself,
so its handler runs there without any thread of ours, and it fires whether or
not anyone is touching Rhino.

Two candidates, tried in order:

  Eto.Forms.UITimer      Rhino 8's own UI toolkit, cross-platform (Windows+Mac)
  System.Windows.Forms.Timer   Windows only, the fallback

Same safety rules as before: no threads, no sockets, no geometry. It appends a
line to a file and nothing else.
"""

import os
import threading
import time
import traceback

import Rhino
import scriptcontext

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS = os.path.join(HERE, "rhino-timer-ticks.txt")
LOG = os.path.join(HERE, "rhino-timer-test-output.txt")
KEY = "_3d_mcp_timer_probe"

_log = open(LOG, "w", encoding="utf-8")


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


_state = {"ticks": 0, "started": time.time()}


def tick(*args):
    """Runs on the UI thread if the timer is a UI timer - which is the point."""
    _state["ticks"] += 1
    try:
        with open(TICKS, "a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%H:%M:%S')}\ttick={_state['ticks']}\t"
                f"thread={threading.get_ident()}\t"
                f"uptime={time.time() - _state['started']:.1f}s\n")
    except Exception:
        pass


try:
    if os.path.exists(TICKS):
        os.remove(TICKS)

    say("UI timer probe")
    say("=" * 50)
    say(f"script thread : {threading.get_ident()}")
    say(f"on UI thread  : {not Rhino.RhinoApp.InvokeRequired}")
    say()

    # Stop a timer left by an earlier run, or ticks double up.
    previous = scriptcontext.sticky.get(KEY)
    if previous is not None:
        try:
            previous.Stop()
            say("  stopped a timer from an earlier run")
        except Exception:                                   # noqa: BLE001
            pass

    timer = None
    which = None

    # --- 1. Eto UITimer: Rhino's own toolkit, works on Mac too ------------
    try:
        import Eto.Forms

        timer = Eto.Forms.UITimer()
        timer.Interval = 0.5                                # seconds
        timer.Elapsed += lambda sender, args: tick()
        timer.Start()
        which = "Eto.Forms.UITimer"
        say(f"  started {which} at 0.5s")
    except Exception as exc:                                # noqa: BLE001
        say(f"  Eto.Forms.UITimer unavailable: {exc!r}")

    # --- 2. WinForms timer: Windows-only fallback --------------------------
    if timer is None:
        try:
            import clr

            clr.AddReference("System.Windows.Forms")
            import System.Windows.Forms as WinForms

            timer = WinForms.Timer()
            timer.Interval = 500                            # milliseconds
            timer.Tick += lambda sender, args: tick()
            timer.Start()
            which = "System.Windows.Forms.Timer"
            say(f"  started {which} at 500ms")
        except Exception as exc:                            # noqa: BLE001
            say(f"  WinForms.Timer unavailable: {exc!r}")

    if timer is None:
        say()
        say("NEITHER TIMER AVAILABLE - a compiled plugin is the remaining option")
    else:
        scriptcontext.sticky[KEY] = timer
        say()
        say(f"  ticks will be appended to: {TICKS}")
        say("  returning now - the timer belongs to Rhino's UI, not to us")
        say(f"STARTED {which}")

except Exception:                                           # noqa: BLE001
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    try:
        _log.close()
    except Exception:
        pass
