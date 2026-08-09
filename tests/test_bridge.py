"""Offline exercise of the Arges add-in — no Fusion, no network.

Loads the REAL arges_impl.py (not a copy) with a stub `adsk` on the
path, redirects its state directory into a temp dir and binds a free port, then
drives the HTTP surface for real. This is the file that accepts arbitrary code
into Fusion, so its auth, host pinning, size limits, single-flight guard and
document fencing are the things most worth pinning down.

    cd agent && uv run --frozen --no-sync python ../tests/test_bridge.py
"""

import http.client
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
IMPL = REPO / "server/src/arges_mcp/addin/Arges/arges_impl.py"
LOADER = REPO / "server/src/arges_mcp/addin/Arges/Arges.py"

sys.path.insert(0, str(HERE / "stubs"))

PASS = FAIL = 0
STATE = Path(tempfile.mkdtemp(prefix="fusion-bridge-test-"))
TOKEN = "a" * 64


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------- load ----
PORT = free_port()
os.environ["FUSION_BRIDGE_PORT"] = str(PORT)
os.environ["FUSION_BRIDGE_STATE_DIR"] = str(STATE)
STATE.mkdir(parents=True, exist_ok=True)
(STATE / "token").write_text(TOKEN, encoding="utf-8")
os.chmod(STATE / "token", 0o600)

spec = importlib.util.spec_from_file_location("arges_impl", IMPL)
impl = importlib.util.module_from_spec(spec)
sys.modules["arges_impl"] = impl
spec.loader.exec_module(impl)

print(f"loaded the real add-in from {IMPL.relative_to(REPO)}")
check("state dir redirected", impl.STATE_DIR, str(STATE))
check("port redirected", impl.BIND_PORT, PORT)
truthy("never touches the real ~/.fusion-mcp", "/.fusion-mcp" not in str(STATE))


# -------------------------------------------------------- fake Fusion ----
class FakeAttributes:
    def __init__(self):
        self._items = {}

    def itemByName(self, group, name):
        return self._items.get((group, name))

    def add(self, group, name, value):
        attribute = type("Attr", (), {"value": value})()
        self._items[(group, name)] = attribute
        return attribute

    @property
    def count(self):
        return len(self._items)


class FakeDesign:
    _is_design = True

    def __init__(self):
        self.attributes = FakeAttributes()


class FakeDataFile:
    def __init__(self, file_id):
        self.id = file_id


class FakeDocument:
    def __init__(self, name, data_file=None, design=None):
        self.name = name
        self.dataFile = data_file
        self._design = design if design is not None else FakeDesign()
        self.products = self

    def itemByProductType(self, kind):
        return self._design


class FakeUI:
    def __init__(self):
        self.activeCommand = "SelectCommand"
        self.messages = []

    def messageBox(self, text, title=""):
        self.messages.append((title, text))

    @property
    def commandDefinitions(self):
        return self

    def itemById(self, ident):
        return None


class FakeApp:
    """Just enough application for the bridge to start and marshal jobs."""

    def __init__(self):
        self.version = "9999.9.9"
        self.userInterface = FakeUI()
        self.unsaved = FakeDocument("Untitled")
        self.saved = FakeDocument("widget", FakeDataFile("urn:test:widget"))
        self.activeDocument = self.unsaved
        self.documents = [self.unsaved, self.saved]
        self.event = None
        self.documentActivated = FakeEvent()
        self.documentClosing = FakeEvent()

    @property
    def activeProduct(self):
        return self.activeDocument._design

    def registerCustomEvent(self, event_id):
        self.event = FakeCustomEvent()
        return self.event

    def unregisterCustomEvent(self, event_id):
        self.event = None
        return True


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)
        return True

    def remove(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)
        return True


class FakeCustomEvent(FakeEvent):
    """fireCustomEvent runs the handler on a worker, standing in for Fusion's
    main thread — the bridge blocks the HTTP thread until it replies."""

    def fire(self, payload):
        for handler in list(self.handlers):
            threading.Thread(
                target=handler.notify,
                args=(type("Args", (), {"additionalInfo": payload})(),),
                daemon=True,
            ).start()


import adsk.core  # noqa: E402

APP = FakeApp()
adsk.core.Application._set(APP)


def fire_custom_event(event_id, payload):
    if APP.event is not None:
        APP.event.fire(payload)
    return True


APP.fireCustomEvent = fire_custom_event
impl.adsk.core.Application._set(APP)
# The bridge fires through adsk.core.Application.get(), so routing is already
# in place; this only makes the call explicit for readers.


# ------------------------------------------------------------- client ----
def request(method, path, body=None, token=TOKEN, host=None, headers=None,
            timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    head = dict(headers or {})
    if token is not None:
        head["X-Fusion-Bridge-Token"] = token
    head["Host"] = host or f"127.0.0.1:{PORT}"
    payload = None
    if body is not None:
        payload = body if isinstance(body, (bytes, str)) else json.dumps(body)
        head["Content-Type"] = "application/json"
    try:
        conn.request(method, path, payload, head)
        response = conn.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw
    except (BrokenPipeError, ConnectionResetError):
        # Refusing an oversized body means closing the connection before the
        # client has finished sending it — the point is not to buffer megabytes
        # inside Fusion just to reject them. From here that looks like a broken
        # pipe, and it counts as "refused", not as a test failure.
        return None, {"error": "connection closed by the bridge"}
    finally:
        conn.close()


impl._start(register_event=True)
time.sleep(0.4)

try:
    # ------------------------------------------------------------ auth ----
    print("Authentication")
    check("valid token -> 200", request("GET", "/health")[0], 200)
    check("no token -> 401", request("GET", "/health", token=None)[0], 401)
    check("wrong token -> 401", request("GET", "/health", token="b" * 64)[0], 401)
    check("empty token -> 401", request("GET", "/health", token="")[0], 401)
    # A prefix of the real token must not pass: compare_digest, not startswith
    check("token prefix -> 401", request("GET", "/health", token=TOKEN[:32])[0], 401)
    check("token + suffix -> 401", request("GET", "/health", token=TOKEN + "x")[0], 401)

    print("Host pinning (DNS rebinding)")
    check("evil host -> 403", request("GET", "/health", host="evil.example")[0], 403)
    check("localhost allowed", request("GET", "/health", host=f"localhost:{PORT}")[0], 200)
    check("wrong port in host -> 403", request("GET", "/health", host="127.0.0.1:1")[0], 403)

    print("Routing")
    check("unknown path -> 404", request("GET", "/nope")[0], 404)
    check("wrong method -> 404", request("GET", "/execute")[0], 404)

    # ---------------------------------------------------------- health ----
    print("Health")
    status, payload = request("GET", "/health")
    check("ok", payload["ok"], True)
    check("reports app version", payload["app_version"], "9999.9.9")
    check("reports protocol version", payload["bridge_version"], impl.BRIDGE_PROTOCOL_VERSION)

    # --------------------------------------------------------- execute ----
    print("Execute")
    status, payload = request("POST", "/execute", {"code": "result = 6 * 7"})
    check("runs code", payload.get("result"), 42)
    status, payload = request("POST", "/execute", {"code": "print('hi')\nresult = 1"})
    check("captures stdout", payload.get("stdout").strip(), "hi")

    status, payload = request("POST", "/execute", {"code": "raise ValueError('boom')"})
    check("a raising script is ok=False", payload.get("ok"), False)
    truthy("traceback returned verbatim", "ValueError: boom" in (payload.get("traceback") or ""))
    truthy("a script error is still HTTP 200", status == 200)

    status, payload = request("POST", "/execute", {"code": "x = ("})
    check("syntax error is ok=False", payload.get("ok"), False)
    truthy("syntax error reported", "SyntaxError" in (payload.get("traceback") or ""))

    print("Namespace persistence")
    request("POST", "/execute", {"code": "carried = 'kept'"})
    status, payload = request("POST", "/execute", {"code": "result = carried"})
    check("namespace persists between calls", payload.get("result"), "kept")
    status, payload = request("POST", "/execute", {"code": "result = 'carried' in dir()",
                                                   "reset": True})
    check("reset clears it", payload.get("result"), False)

    status, payload = request("POST", "/execute", {"code": "result = 1"})
    check("a stale result is not reported as this call's", payload.get("result"), 1)
    status, payload = request("POST", "/execute", {"code": "pass"})
    check("no result assigned -> None", payload.get("result"), None)

    print("Input validation")
    check("missing code -> 400", request("POST", "/execute", {})[0], 400)
    check("code not a string -> 400", request("POST", "/execute", {"code": 5})[0], 400)
    check("body not an object -> 400", request("POST", "/execute", "[1,2]")[0], 400)
    check("malformed JSON -> 400", request("POST", "/execute", "{not json")[0], 400)

    oversized = json.dumps({"code": "x" * (impl.MAX_BODY_BYTES + 1000)})
    status, _ = request("POST", "/execute", oversized)
    truthy("oversized body refused (status or dropped connection)",
           status in (400, 413, None))
    check("and the bridge survives it", request("GET", "/health")[0], 200)

    print("Screenshot bounds are rejected, never clamped")
    check("width too small -> 400",
          request("POST", "/screenshot", {"view": "iso", "width": 1, "height": 600})[0], 400)
    check("width too large -> 400",
          request("POST", "/screenshot", {"view": "iso", "width": 99999, "height": 600})[0], 400)
    check("bad view -> 400",
          request("POST", "/screenshot", {"view": "sideways"})[0], 400)

    # -------------------------------------------------------- document ----
    print("Document identity")
    status, payload = request("GET", "/document")
    truthy("GET /document answers", payload.get("ok"))

    APP.activeDocument = APP.saved
    status, payload = request("POST", "/document")
    key_saved = payload["active"]["key"]
    check("a saved design keys off its dataFile", key_saved, "file:urn:test:widget")
    check("and is NOT stamped (stays unmodified)",
          APP.saved._design.attributes.count, 0)

    APP.activeDocument = APP.unsaved
    status, payload = request("POST", "/document")
    key_unsaved = payload["active"]["key"]
    truthy("an unsaved design is stamped", key_unsaved.startswith("doc:"))
    check("stamping wrote one attribute", APP.unsaved._design.attributes.count, 1)

    status, payload = request("POST", "/document")
    check("stamping is idempotent", payload["active"]["key"], key_unsaved)
    truthy("the two designs differ", key_saved != key_unsaved)

    # the attribute must win over a dataFile that appears later (Save)
    APP.unsaved.dataFile = FakeDataFile("urn:test:saved-later")
    status, payload = request("POST", "/document")
    check("saving a design keeps its chat identity", payload["active"]["key"], key_unsaved)
    APP.unsaved.dataFile = None

    print("Turn fencing")
    check("pin accepts a key",
          request("POST", "/pin", {"key": key_saved})[1]["pinned"], key_saved)
    status, payload = request("POST", "/execute", {"code": "result = 1"})
    check("execute refused while pinned elsewhere", payload.get("ok"), False)
    truthy("and says why", "no longer active" in (payload.get("error") or ""))
    status, payload = request("POST", "/screenshot", {"view": "iso"})
    check("screenshot fenced too", payload.get("ok"), False)

    check("pin to the active design", request("POST", "/pin", {"key": key_unsaved})[1]["pinned"],
          key_unsaved)
    status, payload = request("POST", "/execute", {"code": "result = 2"})
    check("allowed when it matches", payload.get("result"), 2)

    check("unpin", request("POST", "/pin", {"key": None})[1]["pinned"], None)
    check("bad pin type -> 400", request("POST", "/pin", {"key": 123})[0], 400)

    print("Close queue")
    impl._on_document_closing(type("Args", (), {"document": APP.unsaved})())
    status, payload = request("GET", "/document")
    check("a closed design is reported once", payload["closed"], [key_unsaved])
    status, payload = request("GET", "/document")
    check("and drained", payload["closed"], [])

    # ---------------------------------------------------- single flight ----
    print("Single flight")
    results = []

    def slow_call():
        results.append(request("POST", "/execute",
                               {"code": "import time; time.sleep(1.2); result = 'slow'"},
                               timeout=20))

    worker = threading.Thread(target=slow_call)
    worker.start()
    time.sleep(0.4)
    status, payload = request("POST", "/execute", {"code": "result = 'fast'"})
    check("a second concurrent execution -> 409", status, 409)
    truthy("and names the reason", "still running" in (payload.get("error") or ""))
    check("health stays answerable while busy", request("GET", "/health")[0], 200)
    worker.join(timeout=25)
    check("the first call still succeeded", results[0][1].get("result"), "slow")

    status, payload = request("POST", "/execute", {"code": "result = 'after'"})
    check("the slot is released afterwards", payload.get("result"), "after")

    # ---------------------------------------------------------- reload ----
    print("Reload")
    status, payload = request("POST", "/reload")
    check("valid source reloads", status, 200)
    time.sleep(0.6)
    check("bridge still answers after reload", request("GET", "/health")[0], 200)

    # ------------------------------------------------------------ logs ----
    print("Logging")
    log_text = (STATE / "addin.log").read_text(encoding="utf-8", errors="replace")
    check("the token is NEVER logged", TOKEN in log_text, False)
    truthy("requests are logged", "/execute" in log_text)
    truthy("a 401 is logged", "401" in log_text)
    # The panel polls once a second for as long as it is open; logging every
    # successful poll drowned the file (measured at 99% of its lines). Silence
    # applies ONLY to routine polls — one that carries a closed design still has
    # to appear, or the compression trail is invisible.
    check("routine polls (closed=0) are not logged",
          log_text.count("GET /document -> 200 body=0B") -
          log_text.count("closed=1"), 0)
    truthy("but a poll reporting a close IS logged", "closed=1" in log_text)

    print("State directory permissions")
    check("state dir is 0700", oct(os.stat(STATE).st_mode & 0o777), "0o700")

    print("The panel finds its checkout through a symlink")
    # Fusion discovers the add-in through the AddIns/Arges symlink, so
    # __file__ is the ~/Library path — abspath() walked up from there, found
    # no checkout, and the panel silently skipped itself (2026-08-09, live).
    # The docstring had promised the symlink case all along; the old direct
    # registration meant it was never actually exercised.
    fake_home = Path(tempfile.mkdtemp(prefix="panel-home-"))
    checkout = fake_home / "code" / "myrepo"
    (checkout / "agent").mkdir(parents=True)
    addin_real = checkout / "server/src/arges_mcp/addin/Arges"
    addin_real.mkdir(parents=True)
    addin_link = fake_home / "Library" / "AddIns" / "Arges"
    addin_link.parent.mkdir(parents=True)
    addin_link.symlink_to(addin_real)

    real_file = impl.__file__
    # resolve() on the expectation too: on macOS the temp dir itself sits
    # behind /var -> /private/var, so realpath changes the prefix as well.
    checkout_resolved = str(checkout.resolve())
    try:
        # Loaded through the symlink, as Fusion does:
        impl.__file__ = str(addin_link / "arges_impl.py")
        check("a symlinked add-in still finds the checkout",
              impl._find_repo(), checkout_resolved)
        # Loaded from the real path, as the old registration did:
        impl.__file__ = str(addin_real / "arges_impl.py")
        check("and a direct load finds the same one",
              impl._find_repo(), checkout_resolved)
        # Copied out of a wheel: no checkout above, and that is a normal state.
        wheel_copy = fake_home / "Library" / "AddIns2" / "Arges"
        wheel_copy.mkdir(parents=True)
        impl.__file__ = str(wheel_copy / "arges_impl.py")
        check("a PyPI install reports no checkout rather than guessing",
              impl._find_repo(), None)
    finally:
        impl.__file__ = real_file
        shutil.rmtree(fake_home, ignore_errors=True)

    print("Geometry does not outlive the modelling kernel")
    # Fusion destroys the kernel BEFORE it finalises the embedded interpreter,
    # so an adsk object still reachable from the add-in at that point is freed
    # too late: its destructor calls into a dead ASM and Fusion crashes on quit.
    # That is not hypothetical -- it is the 2026-08-08 20:42:26 crash, whose
    # stack ran _Py_Finalize -> _PyGC_Collect -> ~Cylinder -> api_del_entity.
    # A script's top-level names live in _exec_globals for the session, so that
    # dict is what holds them, and stop() is the last moment it is safe to let
    # go.  The stand-in records its own release the way a real adsk destructor
    # would call into the kernel.
    released = []

    class _HeldGeometry:
        def __del__(self):
            released.append(True)

    impl._reset_namespace()
    impl._exec_globals["cyl"] = _HeldGeometry()
    check("a script's objects stay reachable between calls",
          "cyl" in impl._exec_globals, True)
    check("and are still held while the add-in runs", released, [])

finally:
    try:
        impl._shutdown(unregister_event=True)
    except Exception as exc:                              # noqa: BLE001
        print(f"  (shutdown raised: {exc!r})")

    check("stop() releases geometry while the kernel is alive", released, [True])
    check("and empties the namespace rather than leaving it live",
          impl._exec_globals, None)

    print("Shutdown")
    try:
        request("GET", "/health", timeout=2)
        check("listener stopped", "still answering", "refused")
    except (OSError, http.client.HTTPException):
        PASS += 1

    shutil.rmtree(STATE, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
