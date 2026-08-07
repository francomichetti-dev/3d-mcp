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
import io
import json
import math
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
# Overridable only so tests can run against a scratch dir and a free port.
# Monkeypatching these after import does not work: /reload re-executes the
# module body and silently reverts them, which would point a test at the real
# ~/.fusion-mcp and the production port mid-run.
BIND_PORT = int(os.environ.get("FUSION_BRIDGE_PORT") or 7654)
ALLOWED_HOSTS = frozenset(("127.0.0.1:%d" % BIND_PORT, "localhost:%d" % BIND_PORT))
AUTH_HEADER = "X-Fusion-Bridge-Token"
VERSION_HEADER = "X-Bridge-Version"

EVENT_ID = "FusionBridgeMarshalEvent"

MARSHAL_TIMEOUT_S = 60.0
# Per socket operation, not per request: the 60 s marshal wait is a pure-Python
# wait that performs no socket I/O, so this only bounds how long an unauthorized
# peer can hold a connection thread while dribbling out headers.
SOCKET_TIMEOUT_S = 15.0
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

STATE_DIR = os.environ.get("FUSION_BRIDGE_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".fusion-mcp"
)
TOKEN_PATH = os.path.join(STATE_DIR, "token")
LOG_PATH = os.path.join(STATE_DIR, "addin.log")
LOG_ROTATE_BYTES = 5 * 1024 * 1024

NO_DESIGN_ERROR = (
    "no active Fusion design — open or create one and switch to the Design workspace"
)
BUSY_ERROR = "previous execution still running"
# A chat is bound to one design, but adsk always acts on whatever document is
# active *now*.  A turn that outlived a tab switch would therefore start editing
# the design the user just moved to, so pinned turns are refused outright.
DOC_MISMATCH_ERROR = (
    "refused: this turn belongs to a design that is no longer active in Fusion. "
    "Switch back to it, or start a new message in the design you want to change."
)

# Identity for a document's chat.  Attributes are stored in the document and
# survive Save, Save As and rename, which document.name emphatically does not:
# every unsaved document reports the name "Untitled" — the "(1)"/"(3)" in the
# tab strip is decoration Fusion adds for display and never reaches the API.
DOC_ATTR_GROUP = "FusionChat"
DOC_ATTR_NAME = "chatKey"
# Covers both causes: a genuine stop() and a /reload in flight.  Naming only the
# first would send a user hunting for an add-in that never went away.
SHUTTING_DOWN_ERROR = "bridge is stopping or reloading — retry in a moment"
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

# Serializes the whole of _start/_shutdown so the "is a listener already up?"
# check and the bind cannot race — otherwise a reload finishing just as the user
# hits Stop can leave a live listener behind a stopped add-in, which is the one
# off switch this arbitrary-code-execution endpoint has.
_lifecycle_lock = threading.RLock()
# Bumped on every genuine stop(); a reload worker that scheduled before a stop
# sees the change and abandons instead of resurrecting the listener.
_generation = 0

# One thread per connection is unbounded by default, and everything up to full
# header parsing happens before auth — so an unauthenticated local peer could
# exhaust threads and memory inside Fusion's own process and take unsaved CAD
# work with it.  Connections beyond this cap are refused at accept time.
MAX_CONCURRENT_CONNECTIONS = 8
# Grace period for a slot before refusing; see _BridgeServer.process_request.
SLOT_GRACE_S = 0.5
_conn_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)

_state_lock = threading.Lock()
_pending = {}
_active_job = None

_log_lock = threading.Lock()

_exec_globals = None

_cached_app_version = None
_cached_document = None
_bootstrap_alert_shown = False

# Per-document chat state.  _active_doc is a descriptor of the document Fusion
# is showing, kept warm by the documentActivated handler so GET /document can be
# answered off the HTTP thread without ever occupying the main thread — the chat
# service polls it, and must not have to queue behind a modelling job to notice
# that the user changed tabs.
_active_doc = None
_closed_docs = []       # keys awaiting delivery to the chat service
_pinned_doc = None      # key a running turn is fenced to; see DOC_MISMATCH_ERROR
_doc_handlers = []      # Fusion holds event handlers weakly — keep them alive
_doc_lock = threading.Lock()

# Values that must survive a reload: the live handler registration, the user's
# persistent exec namespace, and every lock/counter whose identity matters.  The
# module is re-executed into this same __dict__, so the already-registered
# handler instance keeps working and simply dispatches into the new code.
_CARRIED_ATTRS = (
    "_app",
    "_ui",
    "_custom_event",
    "_handlers",
    "_exec_globals",
    "_cached_app_version",
    "_cached_document",
    "_bootstrap_alert_shown",
    # Document events are registered on the real start/stop cycle, so the
    # handler instances and everything they maintain must outlive a reload.
    # Dropping _pinned_doc in particular would unfence a turn mid-flight.
    "_active_doc",
    "_closed_docs",
    "_pinned_doc",
    "_doc_handlers",
    "_doc_lock",
    # Carried so a reload performed while an abandoned job still occupies the
    # main thread keeps its single-flight guard instead of silently dropping it.
    "_state_lock",
    "_pending",
    "_active_job",
    # Re-executing the module would otherwise mint fresh locks and a fresh
    # semaphore, dropping every guarantee they carry across the reload.  A
    # rebound _log_lock in particular voids the mutual exclusion that log
    # rotation relies on, so two threads could both rotate and destroy a
    # generation of the only forensic record the bridge has.
    "_lifecycle_lock",
    "_generation",
    "_conn_slots",
    "_log_lock",
    "_tokens",
    # An HTTP thread already waiting in _marshal must keep seeing the shutdown
    # signal across a reload instead of waiting out the full 60 s.
    "_shutting_down",
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


def _claim_job(kind):
    """Take the single-flight slot, or raise 409/503.

    Claimed *before* the request body is read so a request that is going to be
    refused anyway never buffers up to MAX_BODY_BYTES first — otherwise N
    concurrent callers each allocate megabytes inside Fusion's own process only
    to be turned away.
    """
    global _active_job

    job = _PendingJob(uuid.uuid4().hex, kind)
    with _state_lock:
        if _shutting_down:
            raise _HttpError(503, SHUTTING_DOWN_ERROR)
        if _active_job is not None:
            raise _HttpError(409, BUSY_ERROR)
        _pending[job.id] = job
        _active_job = job
    return job


def _release_job(job):
    """Give the slot back for a job that never reached the main thread."""
    global _active_job

    with _state_lock:
        _pending.pop(job.id, None)
        if _active_job is not None and _active_job.id == job.id:
            _active_job = None


def _marshal(job, payload):
    """Run ``job`` on Fusion's main thread and return its reply dict.

    Raises _HttpError(504) when the wait is abandoned.
    """
    kind = job.kind
    envelope = json.dumps({"id": job.id, "kind": kind, "payload": payload})
    try:
        _app.fireCustomEvent(EVENT_ID, envelope)
    except Exception:
        _release_job(job)
        _log("fireCustomEvent failed for %s:\n%s" % (kind, traceback.format_exc()), "ERROR")
        raise _HttpError(500, "could not hand the request to Fusion's main thread")

    deadline = time.monotonic() + MARSHAL_TIMEOUT_S
    while True:
        if job.event.wait(WAIT_SLICE_S):
            return job.reply
        if _shutting_down:
            with _state_lock:
                job.abandoned = True
            raise _HttpError(503, SHUTTING_DOWN_ERROR)
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
    """Runs on Fusion's main thread; the single door to the ``adsk`` API.

    Kept to a bare delegation on purpose.  ``/reload`` re-executes this module
    into its own ``__dict__``, but the handler instance Fusion registered is an
    instance of the *pre-reload* class and keeps running the old ``notify`` code
    object — so anything written here would be frozen until an add-in Stop/Run.
    Everything real lives in the module-level function below, which the reload
    genuinely replaces.  (Swapping in a fresh handler instead would risk two live
    handlers dispatching the same job and executing the user's code twice.)
    """

    def notify(self, args):
        _dispatch_marshal_event(args)


def _dispatch_marshal_event(args):
    """The real main-thread dispatcher — hot-reloadable, unlike notify()."""
    kind = "?"
    try:
        envelope = json.loads(args.additionalInfo)
        job_id = envelope["id"]
        kind = envelope["kind"]
        payload = envelope.get("payload") or {}
    except Exception:
        _log("unparseable custom event payload:\n%s" % traceback.format_exc(), "ERROR")
        # The bridge is single-flight: leaving the active job uncompleted would
        # refuse every later request with 409 until the add-in is restarted, so
        # fail it explicitly instead.
        _fail_active_job("bridge internal error — unparseable event payload")
        return

    try:
        _terminate_active_command()
        app = adsk.core.Application.get()
        _refresh_cached_state(app)
        violation = _fence_violation() if kind in ("execute", "screenshot") else None
        if violation is not None:
            reply = {"ok": False, "error": violation}
        elif kind == "execute":
            reply = _job_execute(app, payload)
        elif kind == "screenshot":
            reply = _job_screenshot(app, payload)
        elif kind == "document":
            # The only job that may stamp an identity, so it is the only one
            # that can mark an otherwise-clean document as modified.
            reply = {"ok": True, "active": _refresh_active_doc(app, create=True)}
        else:
            reply = {"ok": False, "error": "unknown job kind '%s'" % kind}
    except BaseException:
        # Deliberately broader than Exception: generated code raising a bare
        # BaseException (KeyboardInterrupt, GeneratorExit, ...) must not escape
        # and leave the job uncompleted.
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
    # Belt and braces for the event-driven cache: a job is already on the main
    # thread, so refreshing costs one attribute lookup and covers any transition
    # documentActivated does not report.
    _refresh_active_doc(app, create=False)


# --------------------------------------------------------------------------- #
# Document identity — main thread only
# --------------------------------------------------------------------------- #


def _design_of(document):
    """The Design product of a specific document.

    Deliberately not ``app.activeProduct``: documentClosing has to identify the
    document going away, which by then is not necessarily the active one.
    """
    try:
        return adsk.fusion.Design.cast(
            document.products.itemByProductType("DesignProductType")
        )
    except Exception:
        return None


def _document_key(document, create):
    """Stable identity for a document's chat, or None if it has none yet.

    Resolution order matters:

    1. An attribute we stamped earlier — checked *first* so a document that is
       saved after its chat began keeps that chat, rather than being renamed
       into a second identity by the dataFile that just appeared.
    2. ``dataFile.id`` for a saved document — free, and stamping nothing means
       merely chatting about a saved design never marks it modified.
    3. Only with ``create``: a fresh UUID stamped into the document. Reached
       only for unsaved documents, which carry unsaved edits anyway.
    """
    design = _design_of(document)
    if design is not None:
        try:
            attribute = design.attributes.itemByName(DOC_ATTR_GROUP, DOC_ATTR_NAME)
            if attribute is not None and attribute.value:
                return attribute.value
        except Exception:
            _log("could not read the chat key attribute:\n%s" % traceback.format_exc(), "WARN")

    try:
        data_file = document.dataFile
    except Exception:
        data_file = None
    if data_file is not None:
        try:
            return "file:" + data_file.id
        except Exception:
            pass

    if not create or design is None:
        return None

    key = "doc:" + uuid.uuid4().hex
    try:
        design.attributes.add(DOC_ATTR_GROUP, DOC_ATTR_NAME, key)
    except Exception:
        _log("could not stamp the chat key attribute:\n%s" % traceback.format_exc(), "ERROR")
        return None
    return key


def _describe_document(document, create=False):
    if document is None:
        return None
    try:
        name = document.name
    except Exception:
        name = None
    try:
        saved = bool(document.dataFile)
    except Exception:
        saved = False
    return {
        "key": _document_key(document, create),
        "name": name,
        "saved": saved,
        "design": _design_of(document) is not None,
    }


def _refresh_active_doc(app, create=False):
    global _active_doc
    try:
        _active_doc = _describe_document(app.activeDocument, create)
    except Exception:
        _active_doc = None
    return _active_doc


def _set_pinned_doc(key):
    """Fence the main thread to one design, or unfence it with None.

    A module-level setter rather than ``global`` inside the request handler:
    that handler reads _pinned_doc in an earlier branch, and a global statement
    after a read in the same scope is a SyntaxError.
    """
    global _pinned_doc
    _pinned_doc = key
    return _pinned_doc


def _fence_violation():
    """Non-None when a pinned turn is aimed at a design that is no longer active.

    The chat service also interrupts the turn when it notices the switch, but it
    only polls once a second; this closes the window where a tool call is already
    in flight.
    """
    pinned = _pinned_doc
    if not pinned:
        return None
    if (_active_doc or {}).get("key") == pinned:
        return None
    return DOC_MISMATCH_ERROR


# --------------------------------------------------------------------------- #
# Document events
# --------------------------------------------------------------------------- #


class _DocumentActivatedHandler(adsk.core.DocumentEventHandler):
    """Bare delegation for the same reason as _MarshalEventHandler: the instance
    Fusion registered belongs to the pre-reload class, so real logic here would
    be frozen until an add-in Stop/Run."""

    def notify(self, args):
        _on_document_activated(args)


class _DocumentClosingHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        _on_document_closing(args)


def _on_document_activated(args):
    try:
        app = adsk.core.Application.get()
        # create=False: merely looking at a design must never modify it.
        _refresh_active_doc(app, create=False)
    except Exception:
        _log("documentActivated handler failed:\n%s" % traceback.format_exc(), "WARN")


def _on_document_closing(args):
    """Queue the closing document's key so the chat service can compress it."""
    try:
        document = getattr(args, "document", None)
        key = _document_key(document, create=False) if document is not None else None
        if not key:
            return          # never chatted about — nothing to compress
        with _doc_lock:
            if key not in _closed_docs:
                _closed_docs.append(key)
        _log("document closing, chat queued for compression: %s" % key)
    except Exception:
        _log("documentClosing handler failed:\n%s" % traceback.format_exc(), "WARN")


def _register_document_events():
    del _doc_handlers[:]
    for event_name, handler_class in (
        ("documentActivated", _DocumentActivatedHandler),
        ("documentClosing", _DocumentClosingHandler),
    ):
        try:
            event = getattr(_app, event_name)
            handler = handler_class()
            event.add(handler)
            _doc_handlers.append((event_name, handler))
        except Exception:
            _log("could not register %s:\n%s" % (event_name, traceback.format_exc()), "ERROR")


def _unregister_document_events():
    for event_name, handler in list(_doc_handlers):
        try:
            getattr(_app, event_name).remove(handler)
        except Exception:
            pass
    del _doc_handlers[:]


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
    if design is None and not payload.get("allow_no_design"):
        # Refused by default so the usual cause (nothing open, or a non-Design
        # workspace) gets one clear message instead of an AttributeError on None
        # from somewhere deep in the caller's script.  The opt-in exists because
        # otherwise the bridge cannot bootstrap: creating a document is exactly
        # what you need to do when there is no document.
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


# Direction the camera looks FROM, and its up vector, per named view.
# Verified against Fusion 2704 rather than assumed: setting
# ``camera.viewOrientation`` on the camera copy takes the value, but assigning
# the camera back to the viewport silently reverts it to
# ArbitraryViewOrientation — so every "named view" quietly returned whatever the
# user was already looking at.  Positioning eye/upVector explicitly is the only
# mechanism that actually moves the camera.
_VIEW_VECTORS = {
    "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "iso": ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
}


def _model_center_and_span(design):
    """Centre and largest extent of the design, in cm.

    Falls back to the origin and a unit span for an empty design, where the
    bounding box is degenerate or unavailable.
    """
    try:
        box = design.rootComponent.boundingBox
        lo, hi = box.minPoint, box.maxPoint
        span = max(hi.x - lo.x, hi.y - lo.y, hi.z - lo.z)
        if span > 0:
            return ((lo.x + hi.x) / 2.0, (lo.y + hi.y) / 2.0, (lo.z + hi.z) / 2.0), span
    except Exception:
        pass
    return (0.0, 0.0, 0.0), 1.0


def _aim_camera(app, viewport, view):
    """Point the camera at the model along ``view``'s axis.  'fit' keeps the
    current orientation and only reframes."""
    vectors = _VIEW_VECTORS.get(view)
    if vectors is None:
        return
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return

    direction, up = vectors
    (cx, cy, cz), span = _model_center_and_span(design)
    length = math.sqrt(sum(c * c for c in direction))
    distance = span * 5.0  # fit() sets the final framing; this only needs to clear the model

    camera = viewport.camera
    camera.target = adsk.core.Point3D.create(cx, cy, cz)
    camera.eye = adsk.core.Point3D.create(
        cx + direction[0] / length * distance,
        cy + direction[1] / length * distance,
        cz + direction[2] / length * distance,
    )
    camera.upVector = adsk.core.Vector3D.create(*up)
    # Defaults to True; a smooth transition animates the move and the capture
    # would land mid-flight.
    camera.isSmoothTransition = False
    viewport.camera = camera


def _job_screenshot(app, payload):
    view = payload["view"]
    width = payload["width"]
    height = payload["height"]

    viewport = app.activeViewport
    if viewport is None:
        return {"ok": False, "error": "no active Fusion viewport — open a document first"}

    _aim_camera(app, viewport, view)
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
    allow_no_design = body.get("allow_no_design", False)
    if not isinstance(allow_no_design, bool):
        raise _HttpError(400, "'allow_no_design' must be a boolean")
    return {"code": code, "reset": reset, "allow_no_design": allow_no_design}


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
            elif method == "GET" and path == "/document":
                # Answered from the event-warmed cache. The chat service polls
                # this once a second, and must never be able to starve the main
                # thread or queue behind a long modelling job to learn that the
                # user switched tabs.
                with _doc_lock:
                    closed = list(_closed_docs)
                    del _closed_docs[:]
                status, payload = 200, {
                    "ok": True, "active": _active_doc, "closed": closed,
                    "pinned": _pinned_doc,
                }
                note = "active=%s closed=%d" % ((_active_doc or {}).get("key"), len(closed))
            elif method == "POST" and path == "/document":
                # Resolving with create=True can stamp an attribute, so it is a
                # real main-thread job rather than a cache read.
                job = _claim_job("document")
                try:
                    self._read_body()
                except BaseException:
                    _release_job(job)
                    raise
                status, payload = 200, _marshal(job, {})
                note = "key=%s" % (payload.get("active") or {}).get("key")
            elif method == "POST" and path == "/pin":
                body, body_size = self._read_json()
                key = body.get("key")
                if key is not None and not isinstance(key, str):
                    raise _HttpError(400, "'key' must be a string or null")
                pinned = _set_pinned_doc(key or None)
                status, payload = 200, {"ok": True, "pinned": pinned}
                note = "pinned=%s" % pinned
            elif method == "POST" and path == "/execute":
                job = _claim_job("execute")
                try:
                    body, body_size = self._read_json()
                    params = _parse_execute(body)
                except BaseException:
                    _release_job(job)
                    raise
                if LOG_EXECUTED_CODE:
                    _log("code: %s" % _truncate_head(params["code"], 200).replace("\n", " | "))
                status, payload = 200, _marshal(job, params)
                note = "ok=%s" % payload.get("ok")
            elif method == "POST" and path == "/screenshot":
                job = _claim_job("screenshot")
                try:
                    body, body_size = self._read_json()
                    params = _parse_screenshot(body)
                except BaseException:
                    _release_job(job)
                    raise
                status, payload = 200, _marshal(job, params)
                note = "view=%s %dx%d ok=%s" % (
                    params["view"], params["width"], params["height"], payload.get("ok"),
                )
            elif method == "POST" and path == "/reload":
                self._read_body()
                if _custom_event is None:
                    raise _HttpError(
                        503, "the add-in is stopped — run it from Utilities → Add-Ins"
                    )
                # Reloading mid-job would swap the module dict under a job that
                # is still running on the main thread.  The endpoint exists for
                # an edit-reload loop, which is never legitimately concurrent
                # with a live execution.
                with _state_lock:
                    if _active_job is not None:
                        raise _HttpError(409, BUSY_ERROR)
                # Validated here rather than in the worker so a syntax error is
                # reported to the caller instead of only reaching the log.
                try:
                    _precompile_self()
                except SyntaxError as err:
                    raise _HttpError(400, "reload refused, bridge untouched: %s" % err)
                status, payload = 200, {"ok": True}
            else:
                raise _HttpError(404, "unknown endpoint %s %s" % (method, path))
            self._send_json(status, payload)
            if status == 200 and path == "/reload":
                _schedule_reload()
        except _HttpError as err:
            status = err.status
            note = err.message
            # Guarded: a BrokenPipeError raised here is a sibling of the clause
            # below, so it would escape _dispatch entirely and the request would
            # never reach the log line at the end.
            try:
                self._send_json(err.status, {"ok": False, "error": err.message})
            except Exception as send_err:
                note = "%s (reply not delivered: %r)" % (err.message, send_err)
        except Exception:
            note = "unhandled bridge error"
            _log("unhandled error on %s %s:\n%s" % (method, path, traceback.format_exc()), "ERROR")
            try:
                self._send_json(500, {"ok": False, "error": "internal bridge error"})
            except Exception:
                pass
        # The chat panel polls /document once a second for as long as it is
        # open, and logging every successful poll drowns the file: measured at
        # 10,976 of 11,046 lines, i.e. 99%, which cost this log its only real
        # job — being the one place an add-in failure is visible, since Fusion
        # swallows those silently. Anything that is not a routine successful
        # poll is still logged, so a 401, a 503 or a closed-document event is
        # never hidden by this.
        routine_poll = (
            method == "GET" and path == "/document" and status == 200
            and "closed=0" in note
        )
        if not routine_poll:
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

    def process_request(self, request, client_address):
        """Refuse connections past the cap instead of spawning a thread.

        The slot is released in shutdown_request, which socketserver calls
        exactly once per accepted request on both the success and error paths.
        The acquire here and that release are only paired because
        ``verify_request`` is never overridden — socketserver calls
        shutdown_request *without* process_request when it returns False, which
        would release a slot that was never acquired.  Add request filtering and
        this pairing has to be revisited.
        """
        # Wait briefly rather than refusing the instant the cap is reached.
        # The slot is released in shutdown_request, on the handler thread,
        # AFTER the client has read its response and moved on — so a purely
        # sequential caller can outrun the release and be refused despite never
        # opening two connections at once. Measured against the broker, which
        # has the identical shape: 1000 rapid sequential requests on an idle
        # machine produced zero refusals, while 500 under CPU contention
        # produced 29 (5.8%). It surfaced as a CI failure here, in this suite,
        # as a 503 with an empty body where a /health payload was expected.
        # The bound is unchanged — never more than MAX_CONCURRENT_CONNECTIONS
        # threads — and a genuine flood holds the slots past the grace period
        # and is still refused.
        if not _conn_slots.acquire(timeout=SLOT_GRACE_S):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            # Closes the socket without going through shutdown_request, so no
            # slot is released for a connection that never took one.
            self.close_request(request)
            return
        # No try/except releasing the slot here: socketserver's
        # _handle_request_noblock already calls shutdown_request() on both
        # exception paths out of process_request, so releasing again would be a
        # second release for one acquire — and BoundedSemaphore only detects
        # that when idle, so under load it would silently raise the cap.
        ThreadingHTTPServer.process_request(self, request, client_address)

    def shutdown_request(self, request):
        try:
            ThreadingHTTPServer.shutdown_request(self, request)
        finally:
            try:
                _conn_slots.release()
            except ValueError:
                # BoundedSemaphore guards against an over-release; never let
                # bookkeeping kill the serving thread.
                _log("connection slot over-released", "WARN")

    def handle_error(self, request, client_address):
        """Keep failures in addin.log instead of Fusion's stderr.

        The default implementation prints a traceback to stderr, which inside
        Fusion goes nowhere the user will ever look — and this log is the only
        debugging window the bridge has.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return  # the client hung up; not our problem and not worth a line
        _log("unhandled error in a request thread:\n%s" % traceback.format_exc(), "ERROR")


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
    global _httpd, _server_thread

    try:
        _httpd.serve_forever(poll_interval=0.25)
    except Exception:
        _log("HTTP serve loop crashed:\n%s" % traceback.format_exc(), "ERROR")
    finally:
        if not _shutting_down:
            # The accept loop died on its own.  Leaving the socket bound would
            # let clients connect via the kernel backlog and then hang for their
            # full timeout — including /health, the one probe meant to reveal
            # this — which looks identical to "Fusion's main thread is stuck".
            # Closing it gives ECONNREFUSED, which the MCP server already maps
            # to an actionable message, and lets a later start rebind.
            _log("HTTP serve loop exited unexpectedly; closing the socket", "ERROR")
            server, _httpd = _httpd, None
            _server_thread = None  # not _shutdown(): it would join this thread
            try:
                if server is not None:
                    server.server_close()
            except Exception:
                _log("server_close() after a crashed loop failed:\n%s"
                     % traceback.format_exc(), "WARN")


# --------------------------------------------------------------------------- #
# Reload
# --------------------------------------------------------------------------- #


def _schedule_reload():
    """Reload this module from a worker thread once the response is on the wire.

    The teardown calls ``httpd.shutdown()``, which cannot run on the serving
    thread itself.
    """
    with _lifecycle_lock:
        generation = _generation
    threading.Thread(
        target=_reload_worker, args=(generation,), name="FusionBridgeReload", daemon=True
    ).start()


def _source_path():
    path = os.path.abspath(__file__)
    if path.endswith(".pyc"):
        path = path[:-1]
    return path


def _precompile_self():
    """Byte-compile the on-disk source before tearing anything down.

    A SyntaxError in the edited file is the single most likely outcome of the
    edit-reload loop this endpoint exists for; catching it here keeps the running
    bridge untouched instead of leaving it dead until a manual add-in restart.
    """
    path = _source_path()
    with open(path, "r", encoding="utf-8") as handle:
        compile(handle.read(), path, "exec")


def _reexec_self(module):
    """Re-run this module's source into its own __dict__.

    Deliberately not ``importlib.reload``: that re-resolves the module *by name*
    through ``sys.path``, which this add-in intentionally never joins (it is
    loaded by explicit path so a generic module name cannot collide with another
    add-in's).  reload() therefore either fails outright or, if some other path
    entry happens to hold a same-named file, loads a foreign module — including
    a different file than ``_precompile_self`` just validated.  The module's own
    pinned spec reads the original path and never consults ``sys.path``.
    """
    spec = getattr(module, "__spec__", None)
    if spec is None or spec.loader is None:
        raise ImportError("fusion_bridge_impl has no loadable spec")
    spec.loader.exec_module(module)


def _reload_worker(generation):
    module = sys.modules[__name__]
    try:
        _precompile_self()
    except Exception as exc:
        _log("reload refused, keeping the running bridge: %r" % exc, "ERROR")
        return

    with _lifecycle_lock:
        if _generation != generation:
            _log("reload abandoned: the add-in was stopped while it was pending", "WARN")
            return
        try:
            # Keep the CustomEvent registration alive across the reload: the
            # module is re-executed into this same __dict__, so the handler
            # instance already registered with Fusion dispatches into new code.
            _shutdown(unregister_event=False)
            # A local sentinel, deliberately not a module-level one: the module
            # dict is about to be re-executed, so any name looked up through it
            # afterwards is a *different* object and every `is` test below would
            # silently go the wrong way.
            missing = object()
            # State introduced by the edit being loaded does not exist on the
            # running module.  Carrying getattr(..., None) would then clobber the
            # new module body's own initialisers — turning a fresh Lock() into
            # None and crashing the first caller that tries to hold it — so a
            # name that was absent before is left at whatever the new body set.
            carried = {name: getattr(module, name, missing) for name in _CARRIED_ATTRS}
            # Held across the swap so a job completing on the main thread can
            # never observe the half-rebuilt module dict — re-executing the
            # module body mints a fresh _pending/_active_job, and a _complete()
            # landing in that window would be dropped as "unknown job" while the
            # restore loop puts the stale job back, wedging single-flight.
            with carried["_state_lock"]:
                try:
                    _reexec_self(module)
                finally:
                    # Restored even when the re-exec raises partway through.
                    # Re-executing the body rebinds _app, _custom_event and
                    # _handlers to their empty defaults, and _handlers is the
                    # ONLY strong reference to the handler Fusion holds weakly —
                    # so skipping this on the error path leaves a listener that
                    # answers /health but can never reach the main thread again,
                    # and /reload itself starts refusing with 503.  A pre-compile
                    # cannot prevent this: it catches SyntaxError, not a NameError
                    # or bad import raised while the module body runs.
                    for name, value in carried.items():
                        if value is not missing:
                            setattr(module, name, value)
            module._start(register_event=False)
            module._log("fusion_bridge_impl reloaded")
        except Exception:
            _log("reload failed:\n%s" % traceback.format_exc(), "ERROR")
            # The listener is already down at this point; bring it back on
            # whatever code is now loaded so a failed reload is not a dead
            # bridge.
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
    # Timed because "Fusion is slow to quit" is otherwise unfalsifiable: this
    # runs on the main thread during shutdown, so any time spent here is time
    # the application appears frozen. The number in the log says whether the
    # add-in is responsible or merely present while something else is slow.
    started = time.monotonic()
    try:
        _shutdown(unregister_event=True)
    finally:
        _log("stop() took %.2fs" % (time.monotonic() - started))


def _start(register_event):
    with _lifecycle_lock:
        _start_locked(register_event)


def _start_locked(register_event):
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
        # UI lives on the real start/stop cycle only — a /reload must not tear
        # down and rebuild the toolbar button underneath the user.
        _install_panel()

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
        # documentActivated does not fire for the document that is already open
        # when the add-in starts, so the cache above is what seeds it.
        _register_document_events()
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

    # A crashed accept loop leaves _httpd None, so "crash, then hit Run" reaches
    # here a second time.  Without this the list accumulates a dead handler per
    # cycle, and if a re-registered event ever came back carrying its previous
    # handlers, two live handlers would dispatch the same job and execute the
    # user's CAD code twice.
    del _handlers[:]
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
    with _lifecycle_lock:
        _shutdown_locked(unregister_event)


def _shutdown_locked(unregister_event):
    global _httpd, _server_thread, _shutting_down, _generation

    if unregister_event:
        # A genuine stop() — not a reload's internal teardown.  Any reload
        # already pending must abandon rather than resurrect the listener behind
        # a stopped add-in.
        _generation += 1

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
        _unregister_document_events()
        _uninstall_panel()
    _log("listener stopped")


def _panel_module():
    """The chat panel, or None when it isn't installed alongside the add-in."""
    import importlib.util
    import sys

    existing = sys.modules.get("chat_panel")
    if existing is not None:
        return existing
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_panel.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("chat_panel", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chat_panel"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("chat_panel", None)
        raise
    return module


def _find_repo():
    """Walk up looking for the checkout that owns this add-in.

    Not a fixed number of dirname() calls: the add-in is nested differently
    depending on how it got here — symlinked out of a checkout by install.sh, or
    copied out of an installed wheel by `arges-mcp install`, where there is
    no checkout above it at all.  Identified by the agent/ directory because that
    is the thing the panel actually needs.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
        if os.path.isdir(os.path.join(here, "agent")):
            return here
    return None


def _install_panel():
    try:
        panel = _panel_module()
        if panel is None:
            return
        repo = _find_repo()
        if repo is None:
            # Installed from PyPI rather than a checkout: the MCP tools work
            # exactly as before, there is simply no chat service to launch.
            _log("chat panel skipped — no checkout found above the add-in")
            return
        panel.install(repo)
        _log("chat panel installed")
    except Exception:
        # The bridge is useful without the panel; never let UI setup stop it.
        _log("could not install the chat panel:\n%s" % traceback.format_exc(), "WARN")


def _uninstall_panel():
    try:
        import sys
        panel = sys.modules.get("chat_panel")
        if panel is not None:
            panel.uninstall()
    except Exception:
        _log("could not remove the chat panel:\n%s" % traceback.format_exc(), "WARN")
