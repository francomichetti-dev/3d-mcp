"""A job broker for CAD applications that cannot be pushed into.

Fusion lets an add-in run an HTTP listener and marshal work onto its main
thread with registerCustomEvent/fireCustomEvent. Rhino does not: its
InvokeOnUiThread is synchronous, so calling it from a script deadlocks (a
rhinocode script IS the UI thread), and calling it from a background thread
never serviced the callback at all.

This inverts the direction rather than fighting it. The CAD stops being a
server and becomes a client: a small poller inside the application asks this
broker for work, runs it, and posts the result back. Because that poller is
already executing on the UI thread, whatever it runs is on the right thread by
construction — there is no marshaling primitive to find, and nothing to
deadlock.

    MCP server  ──POST /submit──▶  broker  ◀──GET /claim───  poller in the CAD
                ◀───result─────                ──POST /result──▶

Single-flight, exactly like the bridge: one job in the CAD at a time. Loopback
only, same token, same fail-closed behaviour.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

BROKER_HOST = "127.0.0.1"
BROKER_PORT = int(os.environ.get("ARGES_BROKER_PORT")
                  or os.environ.get("FUSION_BROKER_PORT") or 7656)

# How long a submitter waits for the CAD to finish. Matches the bridge's
# marshal timeout so behaviour is the same whichever transport is in use.
JOB_TIMEOUT_S = 60.0
# How long GET /claim blocks waiting for work. Long-polling rather than busy
# polling: one request per minute when idle instead of one per 200ms, and a job
# still starts within milliseconds of being submitted.
CLAIM_WAIT_S = 25.0
# A claimed job whose poller never came back. Slightly beyond JOB_TIMEOUT_S so
# the submitter always gives up first and sees a real error rather than a
# silently vanished job.
CLAIM_EXPIRY_S = 75.0

STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"


@dataclass
class Job:
    """One unit of work handed to the CAD."""

    id: str
    kind: str
    payload: dict[str, Any]
    status: str = STATUS_QUEUED
    result: dict[str, Any] | None = None
    created: float = field(default_factory=time.monotonic)
    claimed_at: float | None = None
    done = None  # threading.Event, created in __post_init__

    def __post_init__(self) -> None:
        self.done = threading.Event()

    def age(self) -> float:
        return time.monotonic() - self.created


class Broker:
    """The queue itself, with no HTTP attached so it can be tested directly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._work = threading.Condition(self._lock)
        self._queued: list[Job] = []
        self._active: Job | None = None
        self._last_seen: float | None = None      # when a poller last checked in
        self._stats = {"submitted": 0, "completed": 0, "failed": 0, "expired": 0}

    # -- submitter side ---------------------------------------------------- #

    def submit(self, kind: str, payload: dict[str, Any],
               timeout: float = JOB_TIMEOUT_S) -> dict[str, Any]:
        """Queue a job and wait for the CAD to finish it.

        Single-flight: refuses immediately rather than queueing behind another
        job, because a CAD operation can take a minute and silently waiting
        twice as long is worse than a clear "busy".
        """
        job = Job(id=uuid.uuid4().hex, kind=kind, payload=payload)
        with self._work:
            # Fail fast when nothing is there to do the work. Measured: with the
            # CAD closed, a submit sat for the full 60s timeout before saying so,
            # and the caller has no way to tell "Rhino is busy" from "Rhino is
            # gone". The poller checks in constantly, so its absence is known
            # immediately and there is no reason to make anyone wait for it.
            if not self.poller_connected():
                return {"ok": False, "no_poller": True, "error": (
                    "the CAD is not connected — open Rhino and start the "
                    "poller, then try again")}
            if self._active is not None or self._queued:
                return {"ok": False, "error": "previous job still running",
                        "busy": True}
            self._queued.append(job)
            self._stats["submitted"] += 1
            self._work.notify_all()

        if not job.done.wait(timeout):
            with self._work:
                self._drop(job)
                self._stats["failed"] += 1
            return {"ok": False, "error": (
                f"the CAD did not answer within {timeout:.0f}s. It may still be "
                "working; do not resend. Check that the poller is running.")}

        return job.result or {"ok": False, "error": "job finished without a result"}

    # -- CAD poller side --------------------------------------------------- #

    def claim(self, wait: float = CLAIM_WAIT_S) -> dict[str, Any] | None:
        """Block until there is work, or `wait` elapses. Returns one job."""
        deadline = time.monotonic() + wait
        with self._work:
            self._last_seen = time.time()
            while True:
                self._expire_locked()
                if self._queued and self._active is None:
                    job = self._queued.pop(0)
                    job.status = STATUS_CLAIMED
                    job.claimed_at = time.monotonic()
                    self._active = job
                    return {"id": job.id, "kind": job.kind, "payload": job.payload}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._work.wait(min(remaining, 1.0))

    def complete(self, job_id: str, result: dict[str, Any]) -> bool:
        """Hand back a result. False if the job is unknown or already timed out."""
        with self._work:
            job = self._active
            if job is None or job.id != job_id:
                return False
            job.result = result
            job.status = STATUS_DONE
            self._active = None
            self._stats["completed"] += 1
            self._work.notify_all()
        job.done.set()
        return True

    # -- state ------------------------------------------------------------- #

    # The poller claims every ~0.4s, so anything beyond a couple of seconds of
    # silence means it is gone. 60s was far too generous: measured live, the
    # broker went on reporting a connected poller for 70 SECONDS after it was
    # stopped, and the window faithfully showed "ready" the whole time. A status
    # that lies for a minute is worse than no status.
    POLLER_TIMEOUT_S = 5.0

    def poller_connected(self, within: float | None = None) -> bool:
        """Has a CAD poller checked in recently? Drives the 'is it up' message."""
        within = self.POLLER_TIMEOUT_S if within is None else within
        if self._last_seen is None:
            return False
        return (time.time() - self._last_seen) < within

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "poller_connected": self.poller_connected(),
                "seconds_since_poll": (
                    round(time.time() - self._last_seen, 1)
                    if self._last_seen else None),
                "active_job": self._active.id if self._active else None,
                "queued": len(self._queued),
                **self._stats,
            }

    # -- internals ---------------------------------------------------------- #

    def _drop(self, job: Job) -> None:
        """Caller must hold the lock."""
        if self._active is job:
            self._active = None
        if job in self._queued:
            self._queued.remove(job)

    def _expire_locked(self) -> None:
        """Release a job whose poller took it and never returned.

        Without this a CAD that is closed mid-job wedges the broker at
        permanently busy — the same failure the bridge's stranded-job guard
        exists to prevent.
        """
        job = self._active
        if job is None or job.claimed_at is None:
            return
        if time.monotonic() - job.claimed_at > CLAIM_EXPIRY_S:
            job.result = {"ok": False, "error": (
                "the CAD claimed this job and never returned a result — "
                "it was probably closed mid-operation")}
            self._active = None
            self._stats["expired"] += 1
            job.done.set()


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


# --------------------------------------------------------------------------
# HTTP
#
# Same posture as the bridge, for the same reason: this endpoint hands
# arbitrary code to a CAD application. Loopback bind, token on every request
# compared with compare_digest, Host pinned so a web page cannot reach it, and
# a body cap. Fails closed when there is no token.
# --------------------------------------------------------------------------

STATE_DIR_NAME = ".arges"
# See server.py: prefer the new directory, fall back to the pre-rename one when
# only it exists. The broker is often the oldest process on a machine — it runs
# as a service and survives every other component's upgrade.
LEGACY_STATE_DIR_NAME = ".fusion-mcp"


def _state_dir() -> Path:
    home = Path("~").expanduser()
    if not (home / STATE_DIR_NAME).is_dir() and (home / LEGACY_STATE_DIR_NAME).is_dir():
        return home / LEGACY_STATE_DIR_NAME
    return home / STATE_DIR_NAME


TOKEN_PATH = _state_dir() / "token"
AUTH_HEADER = "X-Arges-Bridge-Token"
# A server: it accepts the pre-rename header as well, so a Rhino half that has
# not been re-zipped yet still authenticates against an updated broker.
LEGACY_AUTH_HEADER = "X-Fusion-Bridge-Token"
ALLOWED_HOSTS = frozenset((
    f"127.0.0.1:{BROKER_PORT}", f"localhost:{BROKER_PORT}",
))
MAX_BODY_BYTES = 5 * 1024 * 1024

# Matches the Fusion listener's cap. Nothing legitimate comes close: the poller
# claims with wait=0 so it never holds a connection, and the MCP server is
# single-flight, which puts real usage at two or three at once.
MAX_CONCURRENT_CONNECTIONS = 8
# How long to wait for a slot before refusing. Absorbs the lag between a client
# finishing and the server releasing its slot; see _BrokerServer.process_request.
SLOT_GRACE_S = 0.5


def read_token() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def make_handler(broker: Broker):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # -- plumbing --------------------------------------------------- #

        def _send(self, status: int, payload: Any) -> None:
            body = encode(payload) if payload is not None else b"null"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> None:
            if self.headers.get("Host", "") not in ALLOWED_HOSTS:
                raise _HttpError(403, "invalid Host header")
            expected = read_token()
            if not expected:
                raise _HttpError(503, "no broker token — run the installer")
            # Which header carried it is not a secret; only the comparison of
            # the value itself has to be constant-time.
            presented = (self.headers.get(AUTH_HEADER)
                         or self.headers.get(LEGACY_AUTH_HEADER) or "")
            if not hmac.compare_digest(presented.encode(), expected.encode()):
                raise _HttpError(401, f"invalid or missing {AUTH_HEADER}")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise _HttpError(413, "request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                parsed = json.loads(raw or b"{}")
            except ValueError:
                raise _HttpError(400, "body must be JSON") from None
            if not isinstance(parsed, dict):
                raise _HttpError(400, "body must be a JSON object")
            return parsed

        # -- routes ------------------------------------------------------ #

        def do_GET(self) -> None:                            # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:                           # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            split = urlsplit(self.path)
            path = split.path
            try:
                self._guard()

                if method == "GET" and path == "/health":
                    self._send(200, broker.health())
                    return

                if method == "GET" and path == "/claim":
                    # The CAD asking for work. Long-polls so an idle CAD makes
                    # one request a minute rather than several a second, while
                    # a submitted job still starts within milliseconds.
                    query = parse_qs(split.query)
                    try:
                        wait = float(query.get("wait", [CLAIM_WAIT_S])[0])
                    except (TypeError, ValueError):
                        raise _HttpError(400, "'wait' must be a number") from None
                    wait = max(0.0, min(wait, CLAIM_WAIT_S))
                    self._send(200, broker.claim(wait=wait))
                    return

                if method == "POST" and path == "/result":
                    body = self._body()
                    job_id = body.get("id")
                    result = body.get("result")
                    if not isinstance(job_id, str) or not isinstance(result, dict):
                        raise _HttpError(400, "'id' (string) and 'result' (object) required")
                    accepted = broker.complete(job_id, result)
                    # Not an error: the submitter may simply have timed out
                    # first. Say so plainly rather than failing the poller.
                    self._send(200, {"ok": True, "accepted": accepted})
                    return

                if method == "POST" and path == "/submit":
                    body = self._body()
                    kind = body.get("kind")
                    payload = body.get("payload")
                    if not isinstance(kind, str) or not isinstance(payload, dict):
                        raise _HttpError(400, "'kind' (string) and 'payload' (object) required")
                    self._send(200, broker.submit(kind, payload))
                    return

                raise _HttpError(404, f"unknown endpoint {method} {path}")

            except _HttpError as err:
                self._send(err.status, {"ok": False, "error": err.message})
            except Exception as exc:                          # noqa: BLE001
                self._send(500, {"ok": False, "error": f"broker error: {exc!r}"})

        def log_message(self, *args) -> None:
            """Silent by default.

            /claim is polled continuously for as long as a CAD is connected;
            logging each one buried the add-in log at 99% noise when the chat
            panel did the same thing.
            """

    return Handler


class _BrokerServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a bound on how many threads it will spawn.

    The Fusion listener has had this since it was written; the broker did not,
    and SECURITY.md described the cap as applying to both. An unbounded
    ThreadingHTTPServer spawns a thread per accepted connection, so a client
    looping on connect - a buggy poller, not an attacker, since this is
    loopback behind a token - can exhaust memory rather than being refused.

    The acquire/release pairing is the subtle part, and it is the same one the
    Fusion side documents at length:

      * the slot is taken in process_request and released in shutdown_request,
        which socketserver calls exactly once per accepted request on both the
        success and error paths;
      * a refused connection is closed with close_request, NOT shutdown_request,
        so it never releases a slot it did not take;
      * no try/except releases here, because socketserver already calls
        shutdown_request when process_request raises - releasing again would be
        a second release for one acquire, and BoundedSemaphore only detects that
        while idle, so under load it would silently raise the cap.

    This pairing holds only while ``verify_request`` is not overridden:
    socketserver calls shutdown_request *without* process_request when it
    returns False. Add request filtering and this has to be revisited.
    """

    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._conn_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)

    def process_request(self, request, client_address):
        # Wait briefly rather than refusing the instant the cap is reached.
        #
        # A slot is released in shutdown_request, which runs on the handler
        # thread AFTER the client has already read its response and moved on.
        # So a purely sequential client - one that never opens two connections
        # at once - can outrun the release and be refused. Measured on this
        # machine: 1000 rapid sequential requests with the CPU idle produced
        # zero refusals, but 500 of the same requests under CPU contention
        # produced 29 (5.8%). A CI runner is exactly that contended, which is
        # how this was found.
        #
        # The bound is unchanged: never more than MAX_CONCURRENT_CONNECTIONS
        # threads. This only stops a queue of finished-but-not-yet-cleaned-up
        # connections from being mistaken for load. A genuine flood still fills
        # the slots for longer than the grace period and is still refused.
        if not self._conn_slots.acquire(timeout=SLOT_GRACE_S):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.close_request(request)
            return
        ThreadingHTTPServer.process_request(self, request, client_address)

    def handle_error(self, request, client_address):
        """A client hanging up is normal, not an error worth a traceback.

        socketserver's default prints the whole stack to stderr. A client that
        disconnects mid-response - the poller when Rhino closes, a cancelled
        tool call - produced 844 tracebacks in one load probe, which is noise
        that buries anything real. Anything else still gets reported.
        """
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError,
                                              ConnectionAbortedError)):
            ThreadingHTTPServer.handle_error(self, request, client_address)

    def shutdown_request(self, request):
        try:
            ThreadingHTTPServer.shutdown_request(self, request)
        finally:
            try:
                self._conn_slots.release()
            except ValueError:
                # An over-release means the pairing above has been broken. Do
                # not let it kill the serving thread; the cap is a safety net,
                # not a correctness invariant of the request itself.
                pass


def serve(broker: Broker | None = None, port: int = BROKER_PORT):
    """Start the broker listener. Returns (server, broker)."""
    broker = broker or Broker()
    server = _BrokerServer((BROKER_HOST, port), make_handler(broker))
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.25},
                              daemon=True, name="BrokerHTTP")
    thread.start()
    return server, broker
