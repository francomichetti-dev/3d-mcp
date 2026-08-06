"""The Rhino side of the bridge: a timer that asks a broker for work.

Everything here is shaped by what this spike measured on real hardware, so the
constraints are worth stating rather than discovering again:

  * A rhinocode script runs ON Rhino's UI thread (InvokeRequired is False), so
    it must never block and must never call InvokeOnUiThread - that is
    synchronous and deadlocks against itself.
  * A background thread outliving the script context appears to abort the
    embedded CPython: ucrtbase.dll, 0xc0000409, three crashes in five minutes.
    So there are NO threads here at all.
  * RhinoApp.Idle fires zero times when nobody is touching Rhino, so it cannot
    drive anything. Eto.Forms.UITimer fires reliably - 148 ticks over 74s with
    Rhino stable - and runs on the UI thread.

The result: one Eto UITimer, no threads, no listener, no marshaling. Whatever
the timer runs is already on the right thread, which is the entire reason this
shape was chosen over the Fusion one.

Start:  RhinoCode.exe script rhino-poller.py
Stop:   RhinoCode.exe script rhino-poller-stop.py  (or restart Rhino)
"""

import json
import os
import time
import traceback
import urllib.error
import urllib.request

import Rhino
import scriptcontext

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "rhino-poller.log")

BROKER = os.environ.get("FUSION_BROKER_URL") or "http://127.0.0.1:7656"
TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".fusion-mcp", "token")
AUTH_HEADER = "X-Fusion-Bridge-Token"

# The timer runs on the UI thread, so every request it makes blocks Rhino for
# its duration. Both of these are therefore deliberately tiny: a claim that
# waits would freeze the application.
POLL_SECONDS = 0.4
HTTP_TIMEOUT = 2.0

STATE_KEY = "_3d_mcp_rhino_poller"


def log(text):
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')}  {text}\n")
    except Exception:
        pass


def _token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BROKER + path, data=data, method=method)
    request.add_header(AUTH_HEADER, _token())
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


# --------------------------------------------------------------------------
# Jobs. These run on the UI thread already - no marshaling anywhere.
# --------------------------------------------------------------------------

_namespace = {}


def run_execute(payload):
    """Run the caller's Python with Rhino's API in scope.

    The namespace persists between calls, matching the Fusion bridge, so a
    variable set in one call is still there in the next.
    """
    global _namespace
    if payload.get("reset") or not _namespace:
        import rhinoscriptsyntax as rs

        _namespace = {
            "__name__": "rhino_bridge_exec",
            "Rhino": Rhino,
            "rs": rs,
            "scriptcontext": scriptcontext,
        }
    _namespace["doc"] = Rhino.RhinoDoc.ActiveDoc
    _namespace.pop("result", None)

    import io
    from contextlib import redirect_stdout

    stream = io.StringIO()
    try:
        with redirect_stdout(stream):
            exec(payload.get("code") or "", _namespace)     # noqa: S102
    except BaseException:                                    # noqa: BLE001
        return {"ok": False, "traceback": traceback.format_exc()[:64000],
                "stdout": stream.getvalue()[:64000]}
    Rhino.RhinoDoc.ActiveDoc.Views.Redraw()
    return {"ok": True, "result": _namespace.get("result"),
            "stdout": stream.getvalue()[:64000]}


def run_state(_payload):
    doc = Rhino.RhinoDoc.ActiveDoc
    if doc is None:
        return {"ok": False, "error": "no active Rhino document"}
    return {
        "ok": True,
        "document": doc.Name or "(unsaved)",
        "path": doc.Path or "",
        "modified": doc.Modified,
        "units": str(doc.ModelUnitSystem),
        "tolerance": doc.ModelAbsoluteTolerance,
        "objects": doc.Objects.Count,
        "layers": [layer.Name for layer in doc.Layers][:50],
    }


HANDLERS = {"execute": run_execute, "state": run_state}


# --------------------------------------------------------------------------
# The timer
# --------------------------------------------------------------------------

_stats = {"polls": 0, "jobs": 0, "errors": 0, "quiet_since": 0.0}


def _tick(*_args):
    """One poll. Must stay fast: this is Rhino's UI thread."""
    _stats["polls"] += 1
    try:
        # wait=0 so the broker answers immediately. Long-polling here would
        # freeze Rhino for the length of the wait.
        job = _request("GET", "/claim?wait=0")
    except (urllib.error.URLError, OSError, ValueError):
        # Broker not running is the normal state before anyone starts a chat.
        # Stay silent rather than filling the log with it.
        _stats["quiet_since"] = _stats["quiet_since"] or time.time()
        return

    if _stats["quiet_since"]:
        log(f"broker reachable again after {time.time() - _stats['quiet_since']:.0f}s")
        _stats["quiet_since"] = 0.0

    if not job:
        return

    kind = job.get("kind", "")
    handler = HANDLERS.get(kind)
    if handler is None:
        result = {"ok": False, "error": f"unknown job kind '{kind}'"}
    else:
        try:
            result = handler(job.get("payload") or {})
        except BaseException:                                # noqa: BLE001
            # A job must never take the poller down with it - or Rhino.
            result = {"ok": False, "traceback": traceback.format_exc()[:64000]}
            _stats["errors"] += 1

    _stats["jobs"] += 1
    try:
        _request("POST", "/result", {"id": job["id"], "result": result})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"could not return the result for {job['id']}: {exc!r}")


try:
    log("=" * 50)
    log(f"poller starting, broker {BROKER}")

    previous = scriptcontext.sticky.get(STATE_KEY)
    if previous is not None:
        try:
            previous.Stop()
            log("stopped a poller from an earlier run")
        except Exception:                                    # noqa: BLE001
            pass

    import Eto.Forms

    timer = Eto.Forms.UITimer()
    timer.Interval = POLL_SECONDS
    timer.Elapsed += _tick
    timer.Start()
    scriptcontext.sticky[STATE_KEY] = timer

    log(f"polling every {POLL_SECONDS}s on the UI thread, no threads used")
    print(f"Rhino poller started - {BROKER}, every {POLL_SECONDS}s")
    print(f"log: {LOG}")

except Exception:                                            # noqa: BLE001
    log("FAILED to start:\n" + traceback.format_exc())
    print("poller failed to start - see " + LOG)
