"""FusionBridge implementation — all logic lives here so it can be hot-reloaded.

Architecture
------------
A ``ThreadingHTTPServer`` listens on 127.0.0.1:7654 from a daemon thread.
Fusion's API is main-thread-only, so *every* ``adsk`` touch -- ``/execute`` and
``/screenshot`` alike -- is marshaled to the main thread through one registered
CustomEvent.  Each marshaled job carries a uuid; replies land in a dict keyed by
that uuid, each entry holding its own ``threading.Event``.

Serialization is enforced by the single-flight guard (``_active_job`` under
``_state_lock``), *not* by the socket: only one job may occupy Fusion's main
thread at a time and a second one is refused with 409 immediately.  Handling
connections concurrently is what lets ``/health`` stay answerable while a long
execute is in flight -- the whole point of a liveness probe -- and lets that
409 come back at once instead of queueing behind a 60 s wait.

Only the standard library may be imported here: this module runs inside Fusion's
embedded CPython.
"""

import base64
import builtins
import contextlib
import hmac
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import adsk.core
import adsk.fusion


# --------------------------------------------------------------------------- #
# Protocol constants — the MCP server pins these exactly.
# --------------------------------------------------------------------------- #

BRIDGE_PROTOCOL_VERSION = "1"

BIND_HOST = "127.0.0.1"
BIND_PORT = 7654
ALLOWED_HOSTS = frozenset(("127.0.0.1:7654", "localhost:7654"))
AUTH_HEADER = "X-Fusion-Bridge-Token"
VERSION_HEADER = "X-Bridge-Version"

EVENT_ID = "FusionBridgeMarshalEvent"

MARSHAL_TIMEOUT_S = 60.0
SOCKET_TIMEOUT_S = 70.0
WAIT_SLICE_S = 0.2
SHUTDOWN_JOIN_S = 5.0

MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_STDOUT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_TRACEBACK_BYTES = 64 * 1024

SCREENSHOT_DEFAULT_WIDTH = 1200
SCREENSHOT_DEFAULT_HEIGHT = 800
SCREENSHOT_MAX_WIDTH = 1920
SCREENSHOT_MAX_HEIGHT = 1440
SCREENSHOT_MIN_PIXELS = 64
SCREENSHOT_VIEWS = ("front", "top", "right", "iso", "fit")

STATE_DIR = os.path.join(os.path.expanduser("~"), ".fusion-mcp")
TOKEN_PATH = os.path.join(STATE_DIR, "token")
LOG_PATH = os.path.join(STATE_DIR, "addin.log")
LOG_ROTATE_BYTES = 5 * 1024 * 1024

NO_DESIGN_ERROR = (
    "no active Fusion design — open or create one and switch to the Design workspace"
)
BUSY_ERROR = "previous execution still running"
TIMEOUT_GUIDANCE = (
    "code may still be executing; do not resend; check fusion_state/screenshot"
)

# Executed source is only ever logged as a short preview, and only when this is
# flipped on by hand while debugging.
LOG_EXECUTED_CODE = False


# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #

_app = None
_ui = None
_custom_event = None

# Fusion holds event handlers weakly: a garbage-collected handler makes
# fireCustomEvent silently do nothing.  This list is the only strong reference.
_handlers = []

_httpd = None
_server_thread = None
_shutting_down = False

_state_lock = threading.Lock()
_pending = {}
_active_job = None

_log_lock = threading.Lock()

_exec_globals = None

_cached_app_version = None
_cached_document = None
_bootstrap_alert_shown = False

# Values that must survive importlib.reload(): the live handler registration and
# the user's persistent exec namespace.  reload() re-executes module code into
# this same __dict__, so the already-registered handler instance keeps working
# and simply dispatches into the new code.
_CARRIED_ATTRS = (
    "_app",
    "_ui",
    "_custom_event",
    "_handlers",
    "_exec_globals",
    "_cached_app_version",
    "_cached_document",
    "_bootstrap_alert_shown",
    # Carried so a reload performed while an abandoned job still occupies the
    # main thread keeps its single-flight guard instead of silently dropping it.
    "_state_lock",
    "_pending",
    "_active_job",
)


class _HttpError(Exception):
    """An error the HTTP layer reports with a specific status code."""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Filesystem, logging
# --------------------------------------------------------------------------- #


def _ensure_state_dir():
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass


def _rotate_log_if_needed():
    try:
        if os.path.getsize(LOG_PATH) < LOG_ROTATE_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def _log(message, level="INFO"):
    """Append one line to ~/.fusion-mcp/addin.log.

    The log is the only debugging window into the add-in (Fusion swallows
    exceptions silently), so this must never raise.  The token and raw request
    headers are never written here.
    """
    line = "%s %-5s %s\n" % (datetime.now().isoformat(timespec="seconds"), level, message)
    try:
        with _log_lock:
            _ensure_state_dir()
            _rotate_log_if_needed()
            fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
    except Exception:
        pass


def _alert_once(message):
    """Show a single message box for a fatal bootstrap condition."""
    global _bootstrap_alert_shown
    if _bootstrap_alert_shown:
        return
    if threading.current_thread() is not threading.main_thread():
        # Reachable from the reload worker; touching adsk off the main thread is
        # undefined behaviour.  Both callers have already logged the detail.
        _log("suppressed off-main-thread alert: %s" % message.replace("\n", " "), "ERROR")
        return
    _bootstrap_alert_shown = True
    try:
        if _ui is not None:
            _ui.messageBox(message, "FusionBridge")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #


class _TokenCache:
    """Reads ~/.fusion-mcp/token, re-reading only when its stat signature changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._signature = None
        self._token = None

    def get(self):
        with self._lock:
            try:
                st = os.stat(TOKEN_PATH)
            except OSError:
                self._signature = None
                self._token = None
                return None
            signature = (st.st_ino, st.st_size, st.st_mtime_ns)
            if signature != self._signature:
                self._signature = signature
                self._token = self._read()
            return self._token

    @staticmethod
    def _read():
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError:
            return None
        return token or None


_tokens = _TokenCache()


# --------------------------------------------------------------------------- #
# Truncation helpers
# --------------------------------------------------------------------------- #


def _truncate_head(text, limit):
    """Keep the beginning of ``text``; mark what was dropped from the end."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    dropped = len(data) - limit
    kept = data[:limit].decode("utf-8", "ignore")
    return "%s\n[truncated %d bytes]" % (kept, dropped)


def _truncate_tail(text, limit):
    """Keep the end of ``text`` — tracebacks are only useful from the tail."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    dropped = len(data) - limit
    kept = data[dropped:].decode("utf-8", "ignore")
    return "[truncated %d bytes]\n%s" % (dropped, kept)


def _to_jsonable(value):
    if value is None:
        return None
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


def _cap_result(value):
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        encoded = repr(value)
    if len(encoded.encode("utf-8", "replace")) <= MAX_RESULT_BYTES:
        return value
    text = value if isinstance(value, str) else encoded
    return _truncate_head(text, MAX_RESULT_BYTES)


def _format_user_traceback(exc):
    """Format ``exc`` without the bridge's own exec frame."""
    tb = exc.__traceback__
    if tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    text = "".join(traceback.format_exception(type(exc), exc, tb))
    return _truncate_tail(text, MAX_TRACEBACK_BYTES)


# --------------------------------------------------------------------------- #
# Main-thread marshaling
# --------------------------------------------------------------------------- #


class _PendingJob:
    def __init__(self, job_id, kind):
        self.id = job_id
        self.kind = kind
        self.event = threading.Event()
        self.reply = None
        self.abandoned = False
        self.started = time.monotonic()


def _marshal(kind, payload):
    """Run ``kind`` on Fusion's main thread and return its reply dict.

    Raises _HttpError(409) if a previous job is still occupying the main thread,
    and _HttpError(504) when the wait is abandoned.
    """
    global _active_job

    job = _PendingJob(uuid.uuid4().hex, kind)
    with _state_lock:
        if _shutting_down:
            raise _HttpError(503, "bridge is shutting down")
        if _active_job is not None:
            raise _HttpError(409, BUSY_ERROR)
        _pending[job.id] = job
        _active_job = job

    envelope = json.dumps({"id": job.id, "kind": kind, "payload": payload})
    try:
        _app.fireCustomEvent(EVENT_ID, envelope)
    except Exception:
        with _state_lock:
            _pending.pop(job.id, None)
            _active_job = None
        _log("fireCustomEvent failed for %s:\n%s" % (kind, traceback.format_exc()), "ERROR")
        raise _HttpError(500, "could not hand the request to Fusion's main thread")

    deadline = time.monotonic() + MARSHAL_TIMEOUT_S
    while True:
        if job.event.wait(WAIT_SLICE_S):
            return job.reply
        if _shutting_down:
            with _state_lock:
                job.abandoned = True
            raise _HttpError(503, "bridge is shutting down")
        if time.monotonic() >= deadline:
            break

    with _state_lock:
        if job.event.is_set():
            return job.reply
        job.abandoned = True
    _log("job %s (%s) abandoned after %.0fs" % (job.id, kind, MARSHAL_TIMEOUT_S), "WARN")
    raise _HttpError(
        504,
        "timed out after %.0fs waiting for Fusion's main thread — %s"
        % (MARSHAL_TIMEOUT_S, TIMEOUT_GUIDANCE),
    )


def _complete(job_id, reply):
    """Deliver a main-thread reply.  Late replies are dropped, never re-used."""
    global _active_job

    with _state_lock:
        job = _pending.pop(job_id, None)
        if _active_job is not None and _active_job.id == job_id:
            _active_job = None
    if job is None:
        _log("dropped reply for unknown job %s" % job_id, "WARN")
        return
    if job.abandoned:
        _log("dropped late reply for abandoned job %s (%s)" % (job_id, job.kind), "WARN")
        return
    job.reply = reply
    job.event.set()


def _fail_active_job(message):
    """Complete whatever job currently holds the main thread with an error."""
    with _state_lock:
        job = _active_job
    if job is not None:
        _complete(job.id, {"ok": False, "error": message})


def _clear_marshal_state(reason):
    """Drop any jobs stranded by a stop/run toggle.

    A request in flight when ``stop()`` runs is abandoned by its waiter, but the
    queued custom event never dispatches once the event is unregistered, so
    ``_complete`` never clears ``_active_job``.  Since the module dict survives
    the toggle, every later /execute would get 409 forever — and toggling the
    add-in is exactly how a user recovers from a hung request.
    """
    global _active_job

    with _state_lock:
        stranded = len(_pending)
        _pending.clear()
        _active_job = None
    if stranded:
        _log("cleared %d stranded job(s) on %s" % (stranded, reason), "WARN")


def _busy_description():
    job = _active_job
    if job is None:
        return None
    return "%s running %.1fs" % (job.kind, time.monotonic() - job.started)


class _MarshalEventHandler(adsk.core.CustomEventHandler):
    """Runs on Fusion's main thread; the single door to the ``adsk`` API."""

    def notify(self, args):
        kind = "?"
        try:
            envelope = json.loads(args.additionalInfo)
            job_id = envelope["id"]
            kind = envelope["kind"]
            payload = envelope.get("payload") or {}
        except Exception:
            _log("unparseable custom event payload:\n%s" % traceback.format_exc(), "ERROR")
            # The bridge is single-flight: leaving the active job uncompleted
            # would refuse every later request with 409 until the add-in is
            # restarted, so fail it explicitly instead.
            _fail_active_job("bridge internal error — unparseable event payload")
            return

        try:
            _terminate_active_command()
            app = adsk.core.Application.get()
            _refresh_cached_state(app)
            if kind == "execute":
                reply = _job_execute(app, payload)
            elif kind == "screenshot":
                reply = _job_screenshot(app, payload)
            else:
                reply = {"ok": False, "error": "unknown job kind '%s'" % kind}
        except BaseException:
            # Deliberately broader than Exception: generated code raising a bare
            # BaseException (KeyboardInterrupt, GeneratorExit, ...) must not
            # escape and leave the job uncompleted.
            detail = _truncate_tail(traceback.format_exc(), MAX_TRACEBACK_BYTES)
            reply = {"ok": False, "traceback": detail, "stdout": ""}
            _log("main-thread job %s failed:\n%s" % (kind, detail), "ERROR")
        _complete(job_id, reply)


def _terminate_active_command():
    """Autodesk's threading guidance: end any running command before API work."""
    try:
        if _ui is None:
            return
        if _ui.activeCommand and _ui.activeCommand != "SelectCommand":
            _ui.commandDefinitions.itemById("SelectCommand").execute()
    except Exception:
        _log("could not terminate the active command:\n%s" % traceback.format_exc(), "WARN")


def _refresh_cached_state(app):
    """Piggyback the document name onto every marshaled job for /health."""
    global _cached_document
    try:
        document = app.activeDocument
        _cached_document = document.name if document else None
    except Exception:
        _cached_document = None


# --------------------------------------------------------------------------- #
# Main-thread jobs
# --------------------------------------------------------------------------- #


def _reset_namespace():
    global _exec_globals
    _exec_globals = {
        "__name__": "fusion_bridge_exec",
        "__builtins__": builtins,
        "adsk": adsk,
    }
    return _exec_globals


def _job_execute(app, payload):
    global _exec_globals

    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return {"ok": False, "error": NO_DESIGN_ERROR}

    if payload.get("reset") or _exec_globals is None:
        _reset_namespace()

    namespace = _exec_globals
    namespace["adsk"] = adsk
    namespace["app"] = app
    namespace["ui"] = app.userInterface
    namespace["design"] = design
    # A leftover `result` from an earlier call must never be reported as this
    # call's result.
    namespace.pop("result", None)

    stream = io.StringIO()
    failure = None
    try:
        with contextlib.redirect_stdout(stream):
            exec(compile(payload["code"], "<fusion_execute>", "exec"), namespace, namespace)
    except Exception as exc:
        failure = _format_user_traceback(exc)
    except SystemExit as exc:
        failure = "SystemExit: %s (a script must not call sys.exit())" % exc

    stdout = _truncate_head(stream.getvalue(), MAX_STDOUT_BYTES)
    if failure is not None:
        return {"ok": False, "traceback": failure, "stdout": stdout}
    result = _cap_result(_to_jsonable(namespace.get("result")))
    return {"ok": True, "result": result, "stdout": stdout}


def _view_orientation(view):
    orientations = {
        "front": adsk.core.ViewOrientations.FrontViewOrientation,
        "top": adsk.core.ViewOrientations.TopViewOrientation,
        "right": adsk.core.ViewOrientations.RightViewOrientation,
        "iso": adsk.core.ViewOrientations.IsoTopRightViewOrientation,
    }
    return orientations.get(view)


def _job_screenshot(app, payload):
    view = payload["view"]
    width = payload["width"]
    height = payload["height"]

    viewport = app.activeViewport
    if viewport is None:
        return {"ok": False, "error": "no active Fusion viewport — open a document first"}

    camera = viewport.camera
    orientation = _view_orientation(view)
    if orientation is not None:
        camera.viewOrientation = orientation
    # Defaults to True; a smooth transition animates the move and the capture
    # would land mid-flight.
    camera.isSmoothTransition = False
    viewport.camera = camera
    viewport.fit()
    viewport.refresh()
    adsk.doEvents()

    directory = tempfile.mkdtemp(prefix="fusion-bridge-")
    try:
        path = os.path.join(directory, "viewport.png")
        # The 3-argument form only: saveAsImageFileWithOptions is broken on
        # Fusion 2702+.
        if not viewport.saveAsImageFile(path, width, height):
            return {"ok": False, "error": "Fusion could not save the viewport image"}
        with open(path, "rb") as handle:
            data = handle.read()
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    return {
        "ok": True,
        "png_base64": base64.b64encode(data).decode("ascii"),
        "view": view,
    }


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #


def _parse_execute(body):
    code = body.get("code")
    if not isinstance(code, str):
        raise _HttpError(400, "'code' must be a string")
    reset = body.get("reset", False)
    if not isinstance(reset, bool):
        raise _HttpError(400, "'reset' must be a boolean")
    return {"code": code, "reset": reset}


def _parse_screenshot(body):
    view = body.get("view", "iso")
    if not isinstance(view, str) or view not in SCREENSHOT_VIEWS:
        raise _HttpError(400, "'view' must be one of: %s" % ", ".join(SCREENSHOT_VIEWS))
    width = _parse_dimension(body, "width", SCREENSHOT_DEFAULT_WIDTH, SCREENSHOT_MAX_WIDTH)
    height = _parse_dimension(body, "height", SCREENSHOT_DEFAULT_HEIGHT, SCREENSHOT_MAX_HEIGHT)
    return {"view": view, "width": width, "height": height}


def _parse_dimension(body, name, default, maximum):
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _HttpError(400, "'%s' must be an integer" % name)
    if value < SCREENSHOT_MIN_PIXELS:
        raise _HttpError(400, "'%s' must be at least %d" % (name, SCREENSHOT_MIN_PIXELS))
    # Refused rather than silently clamped: a caller sizing a screenshot needs to
    # learn the limit, not receive a smaller image it may reason about wrongly.
    if value > maximum:
        raise _HttpError(400, "'%s' must be at most %d" % (name, maximum))
    return value


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "FusionBridge/" + BRIDGE_PROTOCOL_VERSION
    sys_version = ""
    timeout = SOCKET_TIMEOUT_S

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        """Silence stderr access logs — the add-in log is the record."""

    def send_error(self, code, message=None, explain=None):
        self._send_json(code, {"ok": False, "error": message or explain or "request error"})

    # -- plumbing --------------------------------------------------------- #

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(VERSION_HEADER, BRIDGE_PROTOCOL_VERSION)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _check_host(self):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            raise _HttpError(403, "invalid Host header")

    def _check_token(self):
        expected = _tokens.get()
        if not expected:
            raise _HttpError(401, "bridge token unavailable — see ~/.fusion-mcp/addin.log")
        presented = self.headers.get(AUTH_HEADER, "")
        if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
            raise _HttpError(401, "invalid or missing %s" % AUTH_HEADER)

    def _read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise _HttpError(400, "chunked bodies are not supported; send Content-Length")
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            length = int(raw)
        except (TypeError, ValueError):
            raise _HttpError(400, "invalid Content-Length header")
        if length < 0:
            raise _HttpError(400, "invalid Content-Length header")
        if length > MAX_BODY_BYTES:
            raise _HttpError(413, "request body exceeds the %d byte limit" % MAX_BODY_BYTES)
        if length == 0:
            return b""
        data = self.rfile.read(length)
        if len(data) != length:
            raise _HttpError(400, "incomplete request body")
        return data

    def _read_json(self):
        data = self._read_body()
        if not data:
            raise _HttpError(400, "a JSON object body is required")
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _HttpError(400, "malformed JSON body")
        if not isinstance(body, dict):
            raise _HttpError(400, "request body must be a JSON object")
        return body, len(data)

    # -- routing ---------------------------------------------------------- #

    def _dispatch(self, method):
        started = time.monotonic()
        path = urlsplit(self.path).path
        status = 500
        body_size = 0
        note = ""
        try:
            # Auth is validated before any body is read.
            self._check_host()
            self._check_token()
            if method == "GET" and path == "/health":
                status, payload = 200, _health_payload()
            elif method == "POST" and path == "/execute":
                body, body_size = self._read_json()
                params = _parse_execute(body)
                if LOG_EXECUTED_CODE:
                    _log("code: %s" % _truncate_head(params["code"], 200).replace("\n", " | "))
                status, payload = 200, _marshal("execute", params)
                note = "ok=%s" % payload.get("ok")
            elif method == "POST" and path == "/screenshot":
                body, body_size = self._read_json()
                params = _parse_screenshot(body)
                status, payload = 200, _marshal("screenshot", params)
                note = "view=%s %dx%d ok=%s" % (
                    params["view"], params["width"], params["height"], payload.get("ok"),
                )
            elif method == "POST" and path == "/reload":
                self._read_body()
                status, payload = 200, {"ok": True}
            else:
                raise _HttpError(404, "unknown endpoint %s %s" % (method, path))
            self._send_json(status, payload)
            if status == 200 and path == "/reload":
                _schedule_reload()
        except _HttpError as err:
            status = err.status
            note = err.message
            self._send_json(err.status, {"ok": False, "error": err.message})
        except Exception:
            note = "unhandled bridge error"
            _log("unhandled error on %s %s:\n%s" % (method, path, traceback.format_exc()), "ERROR")
            try:
                self._send_json(500, {"ok": False, "error": "internal bridge error"})
            except Exception:
                pass
        _log(
            "%s %s -> %d body=%dB %.3fs %s"
            % (method, path, status, body_size, time.monotonic() - started, note)
        )


class _BridgeServer(ThreadingHTTPServer):
    # Daemon threads so a request still waiting on Fusion's main thread can never
    # hold up add-in reload or Fusion's own quit; block_on_close follows suit so
    # server_close() does not join them.
    daemon_threads = True
    block_on_close = False


def _health_payload():
    """Answered straight from the HTTP thread using cached data only."""
    return {
        "ok": True,
        "app_version": _cached_app_version,
        "bridge_version": BRIDGE_PROTOCOL_VERSION,
        "document": _cached_document,
        "busy": _busy_description(),
    }


def _serve():
    try:
        _httpd.serve_forever(poll_interval=0.25)
    except Exception:
        _log("HTTP serve loop crashed:\n%s" % traceback.format_exc(), "ERROR")


# --------------------------------------------------------------------------- #
# Reload
# --------------------------------------------------------------------------- #


def _schedule_reload():
    """Reload this module from a worker thread once the response is on the wire.

    The teardown calls ``httpd.shutdown()``, which cannot run on the serving
    thread itself.
    """
    threading.Thread(target=_reload_worker, name="FusionBridgeReload", daemon=True).start()


def _precompile_self():
    """Byte-compile the on-disk source before tearing anything down.

    A SyntaxError in the edited file is the single most likely outcome of the
    edit-reload loop this endpoint exists for; catching it here keeps the running
    bridge untouched instead of leaving it dead until a manual add-in restart.
    """
    path = os.path.abspath(__file__)
    if path.endswith(".pyc"):
        path = path[:-1]
    with open(path, "r", encoding="utf-8") as handle:
        compile(handle.read(), path, "exec")


def _reload_worker():
    module = sys.modules[__name__]
    try:
        _precompile_self()
    except Exception as exc:
        _log("reload refused, keeping the running bridge: %r" % exc, "ERROR")
        return

    try:
        # Keep the CustomEvent registration alive across the reload: reload()
        # re-executes into this same module __dict__, so the handler instance
        # already registered with Fusion starts dispatching into the new code.
        _shutdown(unregister_event=False)
        carried = {name: getattr(module, name, None) for name in _CARRIED_ATTRS}
        importlib.reload(module)
        for name, value in carried.items():
            setattr(module, name, value)
        module._start(register_event=False)
        module._log("fusion_bridge_impl reloaded")
    except Exception:
        _log("reload failed:\n%s" % traceback.format_exc(), "ERROR")
        # The listener is already down at this point; bring it back on whatever
        # code is now loaded so a failed reload is not a dead bridge.
        try:
            module._start(register_event=False)
            module._log("listener restored after a failed reload", "WARN")
        except Exception:
            _log("could not restore the listener after a failed reload:\n%s"
                 % traceback.format_exc(), "ERROR")


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def run(context=None):
    """Add-in entry point — always called on Fusion's main thread."""
    _start(register_event=True)


def stop(context=None):
    _shutdown(unregister_event=True)


def _start(register_event):
    global _app, _ui, _custom_event, _httpd, _server_thread, _shutting_down
    global _cached_app_version

    if _httpd is not None:
        _log("start requested but the listener is already running", "WARN")
        return

    _ensure_state_dir()

    if register_event:
        # A fresh session start: no HTTP thread exists yet, so no waiter can be
        # live and anything left in the marshal state is stranded from a
        # previous stop().
        _clear_marshal_state("add-in start")
        _app = adsk.core.Application.get()
        _ui = _app.userInterface if _app else None

    if not _tokens.get():
        _log("refusing to start: %s is missing, empty or unreadable" % TOKEN_PATH, "ERROR")
        _alert_once(
            "FusionBridge did not start: no token found at ~/.fusion-mcp/token.\n"
            "Run scripts/install.sh, then restart the add-in "
            "(Utilities → Add-Ins → stop/run)."
        )
        return

    if register_event:
        try:
            _cached_app_version = _app.version
        except Exception:
            _cached_app_version = None
        _refresh_cached_state(_app)
        if not _register_event():
            return

    try:
        _httpd = _BridgeServer((BIND_HOST, BIND_PORT), _BridgeHandler)
    except OSError as err:
        _httpd = None
        _log("could not bind %s:%d — %s" % (BIND_HOST, BIND_PORT, err), "ERROR")
        _alert_once(
            "FusionBridge could not bind 127.0.0.1:%d (%s).\n"
            "Another instance may still hold the port — see ~/.fusion-mcp/addin.log."
            % (BIND_PORT, err)
        )
        if register_event:
            _unregister_event()
        return

    _shutting_down = False
    _server_thread = threading.Thread(target=_serve, name="FusionBridgeHTTP", daemon=True)
    _server_thread.start()
    _log(
        "listening on %s:%d (protocol v%s, Fusion %s)"
        % (BIND_HOST, BIND_PORT, BRIDGE_PROTOCOL_VERSION, _cached_app_version)
    )


def _register_event():
    global _custom_event
    try:
        # A crashed session can leave a stale registration behind.
        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_event = _app.registerCustomEvent(EVENT_ID)
        handler = _MarshalEventHandler()
        _custom_event.add(handler)
        _handlers.append(handler)
        return True
    except Exception:
        _custom_event = None
        _log("could not register the marshal custom event:\n%s" % traceback.format_exc(), "ERROR")
        _alert_once(
            "FusionBridge could not register its main-thread event — see "
            "~/.fusion-mcp/addin.log."
        )
        return False


def _unregister_event():
    global _custom_event
    for handler in list(_handlers):
        try:
            if _custom_event is not None:
                _custom_event.remove(handler)
        except Exception:
            pass
    del _handlers[:]
    _custom_event = None
    try:
        if _app is not None:
            _app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        _log("unregisterCustomEvent failed:\n%s" % traceback.format_exc(), "WARN")


def _shutdown(unregister_event):
    global _httpd, _server_thread, _shutting_down

    _shutting_down = True
    if _httpd is not None:
        try:
            _httpd.shutdown()
        except Exception:
            _log("httpd.shutdown() failed:\n%s" % traceback.format_exc(), "WARN")
        try:
            _httpd.server_close()
        except Exception:
            _log("httpd.server_close() failed:\n%s" % traceback.format_exc(), "WARN")
        _httpd = None
    if _server_thread is not None:
        _server_thread.join(timeout=SHUTDOWN_JOIN_S)
        if _server_thread.is_alive():
            _log("HTTP thread did not exit within %.0fs" % SHUTDOWN_JOIN_S, "WARN")
        _server_thread = None
    if unregister_event:
        _unregister_event()
    _log("listener stopped")
