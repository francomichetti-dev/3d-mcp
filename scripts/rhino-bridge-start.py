"""Start a mini-bridge inside Rhino and RETURN immediately.

Why this shape, and why the earlier attempts hung:

    rhinocode runs a script ON RHINO'S UI THREAD (InvokeRequired == False).

So any script that starts a worker and then waits for InvokeOnUiThread
deadlocks by construction - the callback needs the very thread the script is
blocking. Three probes hung on exactly that, and it was never Rhino's fault.

The real bridge never has this problem: FusionBridge's run() starts a listener
and returns, leaving the main thread free to service marshaled work. This does
the same. It starts the listener on a background thread, writes the port out,
and exits - handing the UI thread back to Rhino's message loop.

The listener is then driven from OUTSIDE (curl over SSH), which is the honest
test: an HTTP request arriving while Rhino sits idle, marshaled onto a free UI
thread. That is precisely the production path.

Run rhino-bridge-stop.py to shut it down.
"""

import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import Rhino
import scriptcontext

HERE = os.path.dirname(os.path.abspath(__file__))
INFO = os.path.join(HERE, "rhino-bridge-info.json")
LOG = os.path.join(HERE, "rhino-bridge-start-output.txt")
LAYER = "3d-mcp-bridge-test"

_log = open(LOG, "w", encoding="utf-8")


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


# scriptcontext.sticky is Rhino's own store for state that must outlive a single
# script run - which is exactly what a listener has to do. Attaching to the
# Rhino module instead fails: it is a .NET namespace and rejects setattr with
# "type does not support setting attributes".
_STATE_KEY = "_3d_mcp_rhino_bridge"


def marshal(fn, timeout=20.0):
    """Run fn on Rhino's UI thread from a background thread, return its value.

    Safe here precisely because the caller is a listener thread and the UI
    thread is idle - the opposite of the situation that deadlocked.
    """
    done = threading.Event()
    box = {}

    def wrapper():
        try:
            box["value"] = fn()
        except BaseException as exc:                        # noqa: BLE001
            box["error"] = repr(exc)
        finally:
            done.set()

    Rhino.RhinoApp.InvokeOnUiThread(wrapper)
    box["serviced"] = done.wait(timeout)
    return box


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

        if path == "/state":
            out = marshal(lambda: {
                "document": Rhino.RhinoDoc.ActiveDoc.Name or "(unsaved)",
                "objects": Rhino.RhinoDoc.ActiveDoc.Objects.Count,
                "units": str(Rhino.RhinoDoc.ActiveDoc.ModelUnitSystem),
                "ui_thread": threading.get_ident(),
            })
            self._reply(200 if out.get("serviced") else 504,
                        {"ok": bool(out.get("serviced")), **(out.get("value") or {}),
                         "error": out.get("error")})
            return

        if path == "/make":
            def build():
                doc = Rhino.RhinoDoc.ActiveDoc
                before = doc.Objects.Count
                layer = doc.Layers.FindName(LAYER, -1)
                if layer < 0:
                    new_layer = Rhino.DocObjects.Layer()
                    new_layer.Name = LAYER
                    layer = doc.Layers.Add(new_layer)
                attr = Rhino.DocObjects.ObjectAttributes()
                attr.LayerIndex = layer
                sphere = Rhino.Geometry.Sphere(Rhino.Geometry.Point3d(0, 0, 20), 20)
                guid = doc.Objects.AddSphere(sphere, attr)
                doc.Views.Redraw()
                return {"guid": str(guid), "before": before,
                        "after": doc.Objects.Count,
                        "ui_thread": threading.get_ident()}

            out = marshal(build)
            self._reply(200 if out.get("serviced") else 504,
                        {"ok": bool(out.get("serviced")), **(out.get("value") or {}),
                         "error": out.get("error")})
            return

        if path == "/cleanup":
            def clean():
                doc = Rhino.RhinoDoc.ActiveDoc
                index = doc.Layers.FindName(LAYER, -1)
                removed = 0
                if index >= 0:
                    for obj in list(doc.Objects):
                        if obj.Attributes.LayerIndex == index:
                            doc.Objects.Delete(obj.Id, True)
                            removed += 1
                    doc.Layers.Delete(index, True)
                doc.Views.Redraw()
                return {"removed": removed, "objects": doc.Objects.Count}

            out = marshal(clean)
            self._reply(200, {"ok": bool(out.get("serviced")), **(out.get("value") or {})})
            return

        self._reply(404, {"ok": False, "error": "unknown path"})

    def log_message(self, *args):
        pass


try:
    say("starting the Rhino mini-bridge")
    say(f"  script runs on the UI thread : {not Rhino.RhinoApp.InvokeRequired}")
    say("  (which is why it must NOT wait here - it returns instead)")

    # Replace any listener left by a previous run.
    existing = scriptcontext.sticky.get(_STATE_KEY)
    if existing is not None:
        try:
            existing.shutdown()
            existing.server_close()
            say("  stopped a listener left by an earlier run")
        except Exception:                                   # noqa: BLE001
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever,
                              kwargs={"poll_interval": 0.25},
                              daemon=True, name="RhinoBridgeHTTP")
    thread.start()

    scriptcontext.sticky[_STATE_KEY] = httpd

    with open(INFO, "w", encoding="utf-8") as handle:
        json.dump({"port": port, "pid": os.getpid()}, handle)

    say(f"  listening on 127.0.0.1:{port}")
    say(f"  wrote {INFO}")
    say("  returning now, so Rhino's UI thread is free to service callbacks")
    say("STARTED")

except Exception:                                           # noqa: BLE001
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    try:
        _log.close()
    except Exception:
        pass
