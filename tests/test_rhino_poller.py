"""The poller that runs inside Rhino — the only code in this repo that
executes inside somebody's CAD session, and the last piece with no tests.

It could not be tested before because it imports RhinoCommon at module scope.
tests/stubs/ now carries a Rhino stub alongside the existing adsk one, so the
poller can be exercised on a machine with no Rhino at all.

What matters most here is not any single handler. It is that **a tick must
never raise**. The tick is an Eto UITimer callback on Rhino's UI thread; an
exception escaping it kills the timer, and the failure mode is the worst one
this project has: the broker still reports a connected poller, the window still
says ready, and nothing happens. Several tests below exist only to establish
that, under conditions that would each plausibly throw.

The module is imported from a copy in a temp directory so its log file lands
there rather than in the checkout.

    python3 tests/test_rhino_poller.py
"""

import base64
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "stubs"))

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


TMP = Path(tempfile.mkdtemp(prefix="rhino-poller-test-"))
copy = TMP / "rhino_poller.py"
shutil.copy(REPO / "scripts" / "rhino" / "rhino-poller.py", copy)

spec = importlib.util.spec_from_file_location("rhino_poller_under_test", copy)
poller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poller)

import Rhino  # noqa: E402  (the stub, via tests/stubs)
import Eto.Forms  # noqa: E402


# ------------------------------------------------------------- start-up ----
# The import above ran the module's start-up block. If that had thrown, the
# poller would have caught it and logged "FAILED to start", so the timer being
# armed is the real evidence that start-up works.
print("Start-up")
truthy("a timer was created", Eto.Forms.UITimer.instances)
timer = Eto.Forms.UITimer.instances[-1]
truthy("it was started", timer.started)
check("it polls on the documented interval", timer.Interval, poller.POLL_SECONDS)
truthy("a tick handler is attached", poller._tick in timer.Elapsed.handlers)

import scriptcontext  # noqa: E402
truthy("the timer is parked in sticky so a rerun can stop it",
       scriptcontext.sticky.get(poller.STATE_KEY) is timer)


# --------------------------------------------------------------- state ----
print("State")
Rhino.RhinoDoc.ActiveDoc = Rhino._Doc(name="bracket.3dm", path="/tmp/bracket.3dm",
                                      objects=7, layers=("Default", "Holes"))
state = poller.run_state({})
check("reports the document", state["document"], "bracket.3dm")
check("and the object count", state["objects"], 7)
check("and the layers", state["layers"], ["Default", "Holes"])
check("ok", state["ok"], True)

# An unsaved document reports an empty name, and "" would render as nothing at
# all in the chat window.
Rhino.RhinoDoc.ActiveDoc = Rhino._Doc(name="", objects=0)
check("an unsaved document is named rather than blank",
      poller.run_state({})["document"], "(unsaved)")

Rhino.RhinoDoc.ActiveDoc = None
check("no document is an error, not a crash", poller.run_state({})["ok"], False)
Rhino.RhinoDoc.ActiveDoc = Rhino._Doc(name="bracket.3dm", objects=7)


# ------------------------------------------------------------- execute ----
print("Execute")
result = poller.run_execute({"code": "result = 6 * 7"})
check("runs code", result["result"], 42)
check("ok", result["ok"], True)

result = poller.run_execute({"code": "print('hello from Rhino')"})
truthy("captures stdout", "hello from Rhino" in result["stdout"])

# The namespace persists across calls, matching the Fusion bridge. Without it a
# multi-step modelling session would lose its variables between turns.
poller.run_execute({"code": "radius = 12.5"})
check("the namespace persists between calls",
      poller.run_execute({"code": "result = radius * 2"})["result"], 25.0)

check("reset clears it",
      poller.run_execute({"code": "result = 'radius' in dir()", "reset": True})["result"],
      False)

# A failing script is a RESULT, not an exception: Claude reads the traceback
# and fixes its own code.
result = poller.run_execute({"code": "1 / 0"})
check("a failure comes back as a result", result["ok"], False)
truthy("with the traceback", "ZeroDivisionError" in result["traceback"])

# Output written before the failure is still worth having.
result = poller.run_execute({"code": "print('got this far')\nraise ValueError('no')"})
truthy("stdout before the failure survives", "got this far" in result["stdout"])

# SystemExit and KeyboardInterrupt do not derive from Exception. Catching only
# Exception here would let generated code take the poller down — and it would
# take this test file down with it, exiting silently mid-run rather than
# failing, so the escape is caught explicitly and reported.
for escaping in ("raise SystemExit(1)", "raise KeyboardInterrupt"):
    try:
        result = poller.run_execute({"code": escaping})
    except BaseException as exc:                             # noqa: BLE001
        result = {"ok": "escaped as %s" % type(exc).__name__}
    check(f"{escaping.split()[1]} is contained, not propagated", result["ok"], False)

truthy("Rhino's API is in scope",
       poller.run_execute({"code": "result = Rhino is not None"})["result"])
truthy("and rhinoscriptsyntax as rs",
       poller.run_execute({"code": "result = rs.AddSphere((0,0,0), 5)"})["result"])
truthy("and the document as doc",
       poller.run_execute({"code": "result = doc.Name"})["result"])


# ---------------------------------------------------------- screenshot ----
print("Screenshot")
shot = poller.run_screenshot({"view": "top", "width": 800, "height": 600})
check("ok", shot["ok"], True)
check("the view is reported back", shot["view"], "top")
check("the requested size is honoured", (shot["width"], shot["height"]), (800, 600))
capture = Rhino.Display.ViewCapture.last
check("and actually passed to the capture", (capture.Width, capture.Height), (800, 600))
truthy("the PNG decodes", base64.b64decode(shot["png_base64"]).startswith(b"\x89PNG"))

# Bounds are rejected, not clamped: silently returning a different size than
# asked for hides the bug rather than reporting it.
for width, height, why in [(10, 600, "too narrow"), (800, 10, "too short"),
                           (5000, 600, "too wide"), (800, 5000, "too tall")]:
    result = poller.run_screenshot({"view": "top", "width": width, "height": height})
    check(f"{why} is refused", result["ok"], False)
    truthy(f"{why} says the allowed range", "must be" in result["error"])

result = poller.run_screenshot({"view": "sideways"})
check("an unknown view is refused", result["ok"], False)
truthy("and lists the real ones", "perspective" in result["error"])

# "fit" means "leave the projection alone and zoom to extents", so it must not
# be rejected as an unknown view even though it is not a projection.
check("fit is accepted", poller.run_screenshot({"view": "fit"})["ok"], True)

Rhino.Display.ViewCapture.fail = True
result = poller.run_screenshot({"view": "top"})
check("a capture returning nothing is an error, not a crash", result["ok"], False)
Rhino.Display.ViewCapture.fail = False


# ---------------------------------------------------------------- tick ----
# The point of the whole file. A tick that raises kills the timer, and the
# symptom is a poller that reports healthy and does nothing.
print("A tick never raises")

posted = []
claims = []


def fake_request(method, path, body=None):
    if path.startswith("/claim"):
        return claims.pop(0) if claims else None
    posted.append((path, body))
    return {"ok": True}


poller._request = fake_request

claims.append({"id": "j1", "kind": "state", "payload": {}})
poller._tick()
check("a state job posts a result", posted[-1][0], "/result")
check("with the job's id", posted[-1][1]["id"], "j1")
check("and the state in it", posted[-1][1]["result"]["document"], "bracket.3dm")

claims.append({"id": "j2", "kind": "nonsense", "payload": {}})
poller._tick()
check("an unknown kind is reported, not raised", posted[-1][1]["result"]["ok"], False)
truthy("naming the kind", "nonsense" in posted[-1][1]["result"]["error"])

# A handler that throws must become a result. This is what stops one bad job
# ending the session.
def exploding(_payload):
    raise RuntimeError("handler blew up")


poller.HANDLERS["execute"] = exploding
claims.append({"id": "j3", "kind": "execute", "payload": {"code": "x"}})
poller._tick()
check("a throwing handler becomes a result", posted[-1][1]["result"]["ok"], False)
truthy("with its traceback", "handler blew up" in posted[-1][1]["result"]["traceback"])

# A job with no payload at all must not KeyError.
claims.append({"id": "j4", "kind": "state"})
poller._tick()
check("a job with no payload still works", posted[-1][1]["id"], "j4")

# The broker being down is the normal state before anyone opens a chat, and
# must be silent rather than fatal.
def broker_down(method, path, body=None):
    raise OSError("connection refused")


poller._request = broker_down
before = len(posted)
poller._tick()
check("a missing broker posts nothing", len(posted), before)

# And the result POST failing must not raise either — the job already ran.
def claim_ok_post_fails(method, path, body=None):
    if path.startswith("/claim"):
        return {"id": "j5", "kind": "state", "payload": {}}
    raise OSError("broker went away mid-job")


poller._request = claim_ok_post_fails
poller._tick()
PASS += 1          # reaching here at all is the assertion

print()
print(f"  (a tick raising would have ended this run, not failed a check)")

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
