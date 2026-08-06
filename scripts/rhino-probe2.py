"""Minimal, deadlock-proof follow-up probe for Rhino 8.

The first probe hung at the marshaling test, three runs running. The suspicion
is the probe rather than Rhino: it pumped `RhinoApp.Wait()` in a loop while
waiting for `InvokeOnUiThread`, so if rhinocode runs the script ON the UI
thread, the loop occupies the very thread the callback needs and deadlocks.

This one answers the question underneath that, and cannot hang:
  * which thread does a rhinocode script actually run on?
  * does a socket listener survive inside Rhino, with no UI marshaling at all?
  * does InvokeOnUiThread run from a background thread WITHOUT anyone pumping?

Every wait is short and bounded. Nothing blocks the UI thread in a loop.
"""

import os
import socket
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "rhino-probe2-output.txt")
_log = open(LOG, "w", encoding="utf-8")


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


try:
    import Rhino

    say("Rhino follow-up probe")
    say("=" * 58)

    # --- 1. which thread are we on? --------------------------------------
    say()
    say("1. thread identity")
    script_thread = threading.get_ident()
    say(f"   script runs on thread : {script_thread}")

    # RhinoApp exposes whether the calling thread is the one owning the UI.
    on_ui = None
    for attr in ("InvokeRequired",):
        if hasattr(Rhino.RhinoApp, attr):
            try:
                on_ui = not getattr(Rhino.RhinoApp, attr)
            except Exception as exc:                        # noqa: BLE001
                say(f"   {attr} raised: {exc!r}")
    say(f"   is this the UI thread : {on_ui}")
    say("   (if False, a rhinocode script is ALREADY off the UI thread, which")
    say("    is the same position the bridge's HTTP handler will be in)")

    # --- 2. a listener, with no marshaling involved -----------------------
    say()
    say("2. socket listener inside Rhino")
    served = {"hits": 0, "thread": None}

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.5)
    port = listener.getsockname()[1]
    say(f"   bound 127.0.0.1:{port}")

    stop = threading.Event()

    def serve():
        served["thread"] = threading.get_ident()
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.recv(1024)
                    served["hits"] += 1
                    conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
                except OSError:
                    pass

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    time.sleep(0.3)

    def hit():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                return b"200" in s.recv(256)
        except OSError as exc:
            say(f"   request failed: {exc!r}")
            return False

    say(f"   request 1 answered   : {hit()}")
    time.sleep(1.0)
    say(f"   request 2 answered   : {hit()}  (after a 1s pause)")
    say(f"   served on thread     : {served['thread']} (script thread {script_thread})")
    say(f"   total hits           : {served['hits']}")

    # --- 3. marshaling, with nobody pumping ------------------------------
    # The point: if the script is NOT on the UI thread, InvokeOnUiThread should
    # be serviced by Rhino's own message loop with no help from us. If it IS on
    # the UI thread, this returns immediately or not at all - either way the
    # wait is bounded, so it cannot hang like the first probe did.
    say()
    say("3. InvokeOnUiThread from a background thread, nobody pumping")
    outcome = {}

    def from_background():
        done = threading.Event()
        box = {}

        def on_ui_thread():
            box["thread"] = threading.get_ident()
            try:
                doc = Rhino.RhinoDoc.ActiveDoc
                box["objects"] = doc.Objects.Count if doc else None
            except Exception as exc:                        # noqa: BLE001
                box["error"] = repr(exc)
            done.set()

        started = time.time()
        Rhino.RhinoApp.InvokeOnUiThread(on_ui_thread)
        got = done.wait(6.0)          # bounded: no infinite wait, ever
        outcome["serviced"] = got
        outcome["seconds"] = round(time.time() - started, 2)
        outcome.update(box)

    worker = threading.Thread(target=from_background, daemon=True)
    worker.start()
    worker.join(10.0)                 # bounded again

    say(f"   callback serviced    : {outcome.get('serviced')}")
    say(f"   waited               : {outcome.get('seconds')}s")
    say(f"   callback thread      : {outcome.get('thread')}")
    say(f"   read the document    : {outcome.get('objects')} objects")
    if outcome.get("error"):
        say(f"   callback error       : {outcome['error']}")

    # --- tidy up ---------------------------------------------------------
    stop.set()
    try:
        listener.close()
    except OSError:
        pass
    server_thread.join(2.0)
    say()
    say(f"   listener closed      : {not server_thread.is_alive()}")

    # --- verdict ----------------------------------------------------------
    say()
    say("=" * 58)
    listener_ok = served["hits"] >= 2
    marshal_ok = bool(outcome.get("serviced"))
    say(f"listener survives inside Rhino : {listener_ok}")
    say(f"marshaling works unassisted    : {marshal_ok}")
    if listener_ok and marshal_ok:
        say("VERDICT: the Fusion architecture ports directly.")
    elif listener_ok:
        say("VERDICT: a listener works, but marshaling needs the UI thread free.")
        say("The adapter must not block it - which a bridge never does anyway,")
        say("since it waits on a background thread, not the UI one.")
    else:
        say("VERDICT: a background listener does NOT survive. Needs a real plugin.")
    say("DONE")

except Exception:                                           # noqa: BLE001
    say()
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    try:
        _log.close()
    except Exception:
        pass
