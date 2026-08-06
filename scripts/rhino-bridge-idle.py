"""A Rhino mini-bridge that marshals through RhinoApp.Idle instead of Invoke.

WHY NOT InvokeOnUiThread: it is synchronous, like Control.Invoke. From the UI
thread it deadlocks instantly, and a rhinocode script IS the UI thread. From a
background thread it never serviced the callback either. Five probes hung on
that, and each hang froze Rhino - which is why Rhino kept "closing" on the test
machine. Nothing here calls it, so nothing here can reproduce that.

INSTEAD: Rhino raises RhinoApp.Idle from its own message loop, so a handler on
that event already runs on the UI thread with nobody marshaling anything. The
listener thread leaves work in a queue and waits on a per-request Event; the
Idle handler drains the queue and sets it. Structurally identical to what
FusionBridge does with registerCustomEvent / fireCustomEvent.

Start it:
    "C:\\Program Files\\Rhino 8\\System\\RhinoCode.exe" script rhino-bridge-idle.py

It writes the port to rhino-bridge-info.json and RETURNS, handing the UI thread
back. Then drive it from outside:

    curl http://127.0.0.1:<port>/ping        no marshaling at all
    curl http://127.0.0.1:<port>/idle        did Idle fire? how often?
    curl http://127.0.0.1:<port>/state       marshaled read
    curl http://127.0.0.1:<port>/make        marshaled geometry
    curl http://127.0.0.1:<port>/cleanup     remove the test layer
    curl http://127.0.0.1:<port>/stop        shut the listener down

/idle is the one that matters first: if Idle never fires while Rhino sits
unfocused, this approach needs a nudge and the answer is a real plugin instead.
"""

import json
import os
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import Rhino
import scriptcontext

HERE = os.path.dirname(os.path.abspath(__file__))
INFO = os.path.join(HERE, "rhino-bridge-info.json")
LOG = os.path.join(HERE, "rhino-bridge-idle-output.txt")
LAYER = "3d-mcp-bridge-test"
STATE_KEY = "_3d_mcp_rhino_idle_bridge"

_log = open(LOG, "w", encoding="utf-8")


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


# --------------------------------------------------------------------------
# The marshal queue
# --------------------------------------------------------------------------

_jobs = queue.Queue()
_stats = {"idle_ticks": 0, "jobs_done": 0, "ui_thread": None, "last_idle": 0.0}


def _on_idle(sender, args):
    """Runs on the UI thread, courtesy of Rhino's own loop.

    Kept deliberately cheap: it fires constantly, so anything expensive here
    would slow Rhino for the user. Drains whatever is waiting and returns.
    """
    _stats["idle_ticks"] += 1
    _stats["ui_thread"] = threading.get_ident()
    _stats["last_idle"] = time.time()
    while True:
        try:
            fn, done, box = _jobs.get_nowait()
        except queue.Empty:
            return
        try:
            box["value"] = fn()
        except BaseException as exc:                        # noqa: BLE001
            box["error"] = repr(exc)
        finally:
            _stats["jobs_done"] += 1
            done.set()


def marshal(fn, timeout=30.0):
    """Hand work to the UI thread. Call ONLY from a listener thread."""
    done, box = threading.Event(), {}
    _jobs.put((fn, done, box))
    box["serviced"] = done.wait(timeout)
    if not box["serviced"]:
        box["error"] = (f"Idle did not drain the queue within {timeout}s "
                        f"(ticks so far: {_stats['idle_ticks']})")
    return box


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                       # noqa: N802
        path = self.path.split("?")[0]

        if path == "/ping":
            self._reply(200, {"ok": True, "thread": threading.get_ident()})
            return

        if path == "/idle":
            # The decisive one. No marshaling - just report whether Rhino's own
            # loop has been raising Idle at all.
            age = time.time() - _stats["last_idle"] if _stats["last_idle"] else None
            self._reply(200, {
                "ok": _stats["idle_ticks"] > 0,
                "idle_ticks": _stats["idle_ticks"],
                "jobs_done": _stats["jobs_done"],
                "ui_thread": _stats["ui_thread"],
                "seconds_since_last_idle": round(age, 2) if age is not None else None,
                "queue_depth": _jobs.qsize(),
            })
            return

        if path == "/state":
            out = marshal(lambda: {
                "document": Rhino.RhinoDoc.ActiveDoc.Name or "(unsaved)",
                "objects": Rhino.RhinoDoc.ActiveDoc.Objects.Count,
                "units": str(Rhino.RhinoDoc.ActiveDoc.ModelUnitSystem),
                "ran_on_thread": threading.get_ident(),
            })
            self._reply(200 if out.get("serviced") else 504, {
                "ok": bool(out.get("serviced")),
                "error": out.get("error"),
                **(out.get("value") or {}),
            })
            return

        if path == "/make":
            def build():
                doc = Rhino.RhinoDoc.ActiveDoc
                before = doc.Objects.Count
                index = doc.Layers.FindName(LAYER, -1)
                if index is None or index < 0:
                    layer = Rhino.DocObjects.Layer()
                    layer.Name = LAYER
                    index = doc.Layers.Add(layer)
                attr = Rhino.DocObjects.ObjectAttributes()
                attr.LayerIndex = index
                sphere = Rhino.Geometry.Sphere(Rhino.Geometry.Point3d(0, 0, 20), 20)
                guid = doc.Objects.AddSphere(sphere, attr)
                doc.Views.Redraw()
                return {"guid": str(guid), "before": before,
                        "after": doc.Objects.Count,
                        "ran_on_thread": threading.get_ident()}

            out = marshal(build)
            self._reply(200 if out.get("serviced") else 504, {
                "ok": bool(out.get("serviced")),
                "error": out.get("error"),
                **(out.get("value") or {}),
            })
            return

        if path == "/cleanup":
            def clean():
                doc = Rhino.RhinoDoc.ActiveDoc
                index = doc.Layers.FindName(LAYER, -1)
                removed = 0
                if index is not None and index >= 0:
                    for obj in list(doc.Objects):
                        if obj.Attributes.LayerIndex == index:
                            doc.Objects.Delete(obj.Id, True)
                            removed += 1
                    doc.Layers.Delete(index, True)
                doc.Views.Redraw()
                return {"removed": removed, "objects": doc.Objects.Count}

            out = marshal(clean)
            self._reply(200, {"ok": bool(out.get("serviced")),
                              "error": out.get("error"),
                              **(out.get("value") or {})})
            return

        if path == "/stop":
            self._reply(200, {"ok": True, "stopping": True})
            threading.Thread(target=_shutdown, daemon=True).start()
            return

        self._reply(404, {"ok": False, "error": "unknown path"})

    def log_message(self, *args):
        pass


def _shutdown():
    server = scriptcontext.sticky.get(STATE_KEY)
    if server is not None:
        try:
            server.shutdown()
            server.server_close()
        except Exception:                                   # noqa: BLE001
            pass
        scriptcontext.sticky.pop(STATE_KEY, None)
    try:
        Rhino.RhinoApp.Idle -= _on_idle
    except Exception:                                       # noqa: BLE001
        pass


# --------------------------------------------------------------------------

try:
    say("Rhino mini-bridge (Idle-driven)")
    say(f"  on the UI thread : {not Rhino.RhinoApp.InvokeRequired}")
    say("  marshaling via RhinoApp.Idle - InvokeOnUiThread is never called")

    previous = scriptcontext.sticky.get(STATE_KEY)
    if previous is not None:
        try:
            previous.shutdown()
            previous.server_close()
            say("  replaced a listener from an earlier run")
        except Exception:                                   # noqa: BLE001
            pass
        try:
            Rhino.RhinoApp.Idle -= _on_idle
        except Exception:                                   # noqa: BLE001
            pass

    Rhino.RhinoApp.Idle += _on_idle
    say("  Idle handler attached")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25},
                     daemon=True, name="RhinoIdleBridge").start()
    scriptcontext.sticky[STATE_KEY] = httpd

    with open(INFO, "w", encoding="utf-8") as handle:
        json.dump({"port": port, "mode": "idle"}, handle)

    say(f"  listening on 127.0.0.1:{port}")
    say("  returning so Rhino's loop can raise Idle")
    say("STARTED")

except Exception:                                           # noqa: BLE001
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    try:
        _log.close()
    except Exception:
        pass
