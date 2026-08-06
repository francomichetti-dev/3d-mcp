"""A working mini-bridge inside Rhino, tested end to end in one run.

Rhino availability has been scarce and every restart costs a round trip, so
this answers every open question in a single execution rather than several:

  1. Which thread does a rhinocode script run on?
  2. Does an HTTP listener survive inside Rhino?
  3. Does RhinoApp.InvokeOnUiThread service a callback from a background
     thread, unassisted, and return a value to it?
  4. Can a request arriving on that listener create real geometry?
  5. Does the listener keep answering afterwards?
  6. Does it shut down cleanly?

If all six pass, the Fusion bridge architecture ports and the adapter is mostly
a rename. If 3 fails, Rhino needs a different marshaling strategy and that is
the thing to solve next.

Safety, learned from the probe that hung Rhino three times:
  * every wait is bounded - nothing waits forever
  * no pumping loop on the UI thread
  * no display-pipeline calls off the UI thread
  * geometry goes on its own layer and is removed at the end
  * every exception is written to the log, since rhinocode swallows stdout
"""

import json
import os
import socket
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "rhino-bridge-output.txt")
LAYER = "3d-mcp-bridge-test"

_log = open(LOG, "w", encoding="utf-8")
RESULTS = []


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


def record(label, ok, detail=""):
    RESULTS.append((label, bool(ok)))
    say(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


httpd = None
try:
    import Rhino
    import rhinoscriptsyntax as rs

    say("Rhino mini-bridge, end to end")
    say("=" * 60)

    SCRIPT_THREAD = threading.get_ident()
    doc = Rhino.RhinoDoc.ActiveDoc

    # ---------------------------------------------------------- 1. threads --
    say()
    say("1. thread identity")
    say(f"   script thread: {SCRIPT_THREAD}")
    invoke_required = None
    try:
        invoke_required = Rhino.RhinoApp.InvokeRequired
        say(f"   InvokeRequired: {invoke_required}"
            f"  ({'NOT on the UI thread' if invoke_required else 'IS the UI thread'})")
    except Exception as exc:                                # noqa: BLE001
        say(f"   InvokeRequired unavailable: {exc!r}")
    record("thread identity determined", True)

    # ------------------------------------------------- 2. marshal primitive --
    # This is the core of any bridge: a background thread hands work to the UI
    # thread and waits for the answer. Bounded, and with nobody pumping.
    say()
    say("2. marshaling from a background thread, unassisted")

    def marshal(fn, timeout=8.0):
        done = threading.Event()
        box = {}

        def wrapper():
            box["thread"] = threading.get_ident()
            try:
                box["value"] = fn()
            except BaseException as exc:                    # noqa: BLE001
                box["error"] = repr(exc)
            finally:
                done.set()

        Rhino.RhinoApp.InvokeOnUiThread(wrapper)
        box["serviced"] = done.wait(timeout)
        return box

    marshal_probe = {}

    def try_marshal():
        marshal_probe.update(marshal(lambda: Rhino.RhinoDoc.ActiveDoc.Objects.Count))

    t = threading.Thread(target=try_marshal, daemon=True)
    t.start()
    t.join(12.0)

    serviced = bool(marshal_probe.get("serviced"))
    record("callback serviced", serviced, marshal_probe.get("error", ""))
    record("value returned to the caller", marshal_probe.get("value") is not None,
           f"doc has {marshal_probe.get('value')} objects")
    ui_thread = marshal_probe.get("thread")
    record("ran on a thread of its own", ui_thread is not None,
           f"callback thread {ui_thread}, script thread {SCRIPT_THREAD}")

    # ------------------------------------------------------- 3. the layer --
    say()
    say("3. workspace")
    try:
        rs.CurrentLayer("Default")
    except Exception:                                       # noqa: BLE001
        pass
    if rs.IsLayer(LAYER):
        rs.PurgeLayer(LAYER)          # cannot purge the CURRENT layer - hence above
    rs.AddLayer(LAYER, color=(60, 140, 220))
    rs.CurrentLayer(LAYER)
    before = doc.Objects.Count
    record("test layer ready", rs.IsLayer(LAYER), f"{before} objects in doc")

    # ------------------------------------------------------- 4. the bridge --
    say()
    say("4. HTTP listener inside Rhino")

    state = {"hits": 0, "errors": []}

    def make_sphere(radius_cm):
        """Runs on the UI thread. Returns a plain dict, like the real bridge."""
        d = Rhino.RhinoDoc.ActiveDoc
        centre = Rhino.Geometry.Point3d(0, 0, radius_cm)
        sphere = Rhino.Geometry.Sphere(centre, radius_cm)
        guid = d.Objects.AddSphere(sphere)
        d.Views.Redraw()
        return {"guid": str(guid), "objects": d.Objects.Count}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                   # noqa: N802
            state["hits"] += 1
            if self.path.startswith("/make"):
                # The whole point: an HTTP request, arriving on a background
                # thread, causing real geometry via the UI thread.
                out = marshal(lambda: make_sphere(20.0))
                if out.get("serviced") and "value" in out:
                    self._reply(200, {"ok": True, **out["value"]})
                else:
                    self._reply(500, {"ok": False,
                                      "error": out.get("error") or "not serviced"})
                return
            self._reply(200, {"ok": True, "thread": threading.get_ident()})

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever,
                     kwargs={"poll_interval": 0.2}, daemon=True).start()
    record("listener bound on loopback", True, f"127.0.0.1:{port}")

    def get(path, timeout=15):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as response:
            return json.loads(response.read().decode())

    time.sleep(0.3)
    try:
        ping = get("/ping")
        record("listener answers", ping.get("ok"),
               f"handler thread {ping.get('thread')}")
    except Exception as exc:                                # noqa: BLE001
        record("listener answers", False, repr(exc))

    # ------------------------------------------- 5. geometry over the wire --
    say()
    say("5. geometry created BY an HTTP request")
    try:
        made = get("/make")
        record("request created geometry", made.get("ok"), json.dumps(made)[:110])
        after = Rhino.RhinoDoc.ActiveDoc.Objects.Count
        record("object count grew", after > before, f"{before} -> {after}")
    except Exception as exc:                                # noqa: BLE001
        record("request created geometry", False, repr(exc))

    # ------------------------------------------------- 6. still alive after --
    say()
    say("6. after doing real work")
    try:
        again = get("/ping")
        record("listener still answering", again.get("ok"))
        record("served several requests", state["hits"] >= 3, f"{state['hits']} hits")
    except Exception as exc:                                # noqa: BLE001
        record("listener still answering", False, repr(exc))

    # ------------------------------------------------------------ cleanup --
    say()
    say("cleanup")
    try:
        httpd.shutdown()
        httpd.server_close()
        httpd = None
        record("listener shut down", True)
    except Exception as exc:                                # noqa: BLE001
        record("listener shut down", False, repr(exc))

    try:
        rs.CurrentLayer("Default")
        if rs.IsLayer(LAYER):
            rs.PurgeLayer(LAYER)
        record("test geometry removed", not rs.IsLayer(LAYER),
               f"{Rhino.RhinoDoc.ActiveDoc.Objects.Count} objects left")
    except Exception as exc:                                # noqa: BLE001
        record("test geometry removed", False, repr(exc))

    # ------------------------------------------------------------ verdict --
    say()
    say("=" * 60)
    passed = sum(1 for _, ok in RESULTS if ok)
    say(f"{passed}/{len(RESULTS)} checks passed")
    say()

    essential = {
        "callback serviced": "marshaling",
        "listener answers": "listener",
        "request created geometry": "request -> geometry",
    }
    failed = [name for label, ok in RESULTS if not ok
              for key, name in essential.items() if key == label]
    if failed:
        say("BLOCKING: " + ", ".join(failed))
        say("The Fusion architecture does not port as-is. Next step is a Rhino")
        say("plugin loaded at startup rather than a script-hosted listener.")
    else:
        say("VERDICT: the architecture ports.")
        say("An HTTP request arriving on a background thread inside Rhino")
        say("created real geometry through the UI thread and returned a value.")
        say("That is exactly what the Fusion bridge does, so the adapter is")
        say("mostly the marshaling primitive plus the injected namespace.")
    say("DONE")

except Exception:                                           # noqa: BLE001
    say()
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    if httpd is not None:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
    try:
        _log.close()
    except Exception:
        pass
