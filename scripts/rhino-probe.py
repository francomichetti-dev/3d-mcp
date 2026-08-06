"""Feasibility probe for a Rhino 8 bridge. Run once from Rhino's ScriptEditor.

Answers the three questions that decide whether the Fusion architecture ports:

  1. Does an HTTP listener on a background thread SURVIVE inside Rhino, or does
     Rhino tear the thread down / block on it?
  2. Does RhinoApp.InvokeOnUiThread actually marshal work from that thread onto
     the UI thread, and can the background thread get a RESULT back?
  3. Are document writes from a marshaled call real and persistent?

Changes nothing permanent: it adds one point, reads it back, then deletes it,
and shuts the listener down before returning. Prints a verdict.

To run: Rhino -> type `ScriptEditor` -> new Python 3 script -> paste -> Run.
"""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --- output capture -------------------------------------------------------
# rhinocode runs this inside Rhino, so print() goes to Rhino's console and the
# caller sees nothing. Mirror everything to a file next to this script.
import os as _os
_OUT_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                          "rhino-probe-output.txt")
_out_file = open(_OUT_PATH, "w", encoding="utf-8")
_real_print = print


def print(*args, **kwargs):          # noqa: A001 - deliberate shadow
    _real_print(*args, **kwargs)
    try:
        _out_file.write(" ".join(str(a) for a in args) + "\n")
        _out_file.flush()
    except Exception:
        pass


RESULTS = []


def record(label, ok, detail=""):
    RESULTS.append((label, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


print("Rhino bridge feasibility probe")
print("=" * 60)

# ---------------------------------------------------------------- 1. env ----
print("\nEnvironment")
record("python 3", sys.version_info[0] == 3, sys.version.split()[0])

try:
    import Rhino
    import scriptcontext
    record("RhinoCommon importable", True, f"Rhino {Rhino.RhinoApp.Version}")
except Exception as exc:                                   # noqa: BLE001
    record("RhinoCommon importable", False, repr(exc))
    Rhino = None

try:
    import rhinoscriptsyntax as rs
    record("rhinoscriptsyntax importable", True)
except Exception as exc:                                   # noqa: BLE001
    record("rhinoscriptsyntax importable", False, repr(exc))
    rs = None

if Rhino is not None:
    doc = Rhino.RhinoDoc.ActiveDoc
    record("an active document", doc is not None,
           f"units={doc.ModelUnitSystem}, tol={doc.ModelAbsoluteTolerance}" if doc else "")
    # Fusion is always centimetres; Rhino's unit system is user-configurable,
    # which the adapter will have to respect rather than assume.
    record("InvokeOnUiThread exists", hasattr(Rhino.RhinoApp, "InvokeOnUiThread"))

# ------------------------------------------------- 2. UI-thread marshaling ----
print("\nMarshaling from a background thread")

MAIN_THREAD = threading.get_ident()
marshal_result = {}


def marshal_and_wait(fn, timeout=10.0):
    """The core primitive: run fn on the UI thread, get its value back here."""
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
    if not done.wait(timeout):
        box["error"] = f"timed out after {timeout}s"
    return box


def background_probe():
    marshal_result["worker_thread"] = threading.get_ident()

    # a) does it run at all, and on a different thread than the caller?
    box = marshal_and_wait(lambda: threading.get_ident())
    marshal_result["ui_thread"] = box.get("value")
    marshal_result["marshal_error"] = box.get("error")

    # b) can a marshaled call touch the document and return a value?
    if Rhino is not None:
        def add_and_count():
            d = Rhino.RhinoDoc.ActiveDoc
            before = d.Objects.Count
            guid = d.Objects.AddPoint(Rhino.Geometry.Point3d(1.0, 2.0, 3.0))
            d.Views.Redraw()
            return {"guid": str(guid), "before": before, "after": d.Objects.Count}

        marshal_result["doc_write"] = marshal_and_wait(add_and_count)


worker = threading.Thread(target=background_probe, daemon=True)
worker.start()

# The UI thread must stay responsive for InvokeOnUiThread to be serviced, so
# pump here rather than blocking on join() — this is exactly the constraint the
# real bridge will live under.
deadline = time.time() + 20
while worker.is_alive() and time.time() < deadline:
    if Rhino is not None:
        Rhino.RhinoApp.Wait()
    time.sleep(0.05)

record("background thread ran", not worker.is_alive() or "worker_thread" in marshal_result)
record("marshal returned a value", marshal_result.get("ui_thread") is not None,
       marshal_result.get("marshal_error") or "")
record("marshaled onto a DIFFERENT thread than the worker",
       marshal_result.get("ui_thread") not in (None, marshal_result.get("worker_thread")),
       f"ui={marshal_result.get('ui_thread')} worker={marshal_result.get('worker_thread')}")
record("marshaled onto the thread this script runs on",
       marshal_result.get("ui_thread") == MAIN_THREAD)

write = marshal_result.get("doc_write") or {}
value = write.get("value") or {}
record("a marshaled document write took effect",
       value.get("after", 0) == value.get("before", -1) + 1,
       write.get("error") or f"{value.get('before')} -> {value.get('after')}")

# clean up the probe point
if Rhino is not None and value.get("guid"):
    try:
        d = Rhino.RhinoDoc.ActiveDoc
        removed = d.Objects.Delete(__import__("System").Guid(value["guid"]), True)
        record("probe geometry removed", removed)
    except Exception as exc:                                # noqa: BLE001
        record("probe geometry removed", False, repr(exc))

# --------------------------------------------------- 3. HTTP listener life ----
print("\nHTTP listener inside Rhino")

PORT = 0
served = {"hits": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                       # noqa: N802
        served["hits"] += 1
        body = json.dumps({"ok": True, "thread": threading.get_ident()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


httpd = None
try:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    record("listener bound on loopback", True, f"127.0.0.1:{PORT}")

    time.sleep(0.3)
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        sock.sendall(b"GET /ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        reply = sock.recv(4096).decode("utf-8", "replace")
    record("listener answered a request", "200" in reply and '"ok": true' in reply)

    # survive a spell of the UI thread being busy, as during modelling
    for _ in range(20):
        if Rhino is not None:
            Rhino.RhinoApp.Wait()
        time.sleep(0.05)
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        sock.sendall(b"GET /ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        reply2 = sock.recv(4096).decode("utf-8", "replace")
    record("still answering a second later", "200" in reply2)
    record("served more than one request", served["hits"] >= 2, f"{served['hits']} hits")
except Exception as exc:                                    # noqa: BLE001
    record("listener bound on loopback", False, repr(exc))
finally:
    if httpd is not None:
        try:
            httpd.shutdown()
            httpd.server_close()
            record("listener shut down cleanly", True)
        except Exception as exc:                            # noqa: BLE001
            record("listener shut down cleanly", False, repr(exc))

# -------------------------------------------------------------- verdict ----
print("\n" + "=" * 60)
passed = sum(1 for _, ok, _ in RESULTS if ok)
print(f"{passed}/{len(RESULTS)} checks passed")

blocking = [label for label, ok, _ in RESULTS if not ok and (
    "marshal" in label or "listener" in label or "RhinoCommon" in label)]
if blocking:
    print("\nBLOCKING failures — the Fusion architecture does not port as-is:")
    for label in blocking:
        print(f"  - {label}")
else:
    print("\nVERDICT: the architecture ports. A background listener survives inside")
    print("Rhino, InvokeOnUiThread marshals work onto the UI thread and returns a")
    print("value, and a marshaled document write is real.")

print("\nPaste this whole output back.")

try:
    _out_file.close()
except Exception:
    pass
