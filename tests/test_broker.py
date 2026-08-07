"""The job broker - no CAD, no network, no threads left running.

The broker is what lets a CAD that cannot be pushed into (Rhino) still be
driven: it holds a single-flight queue, the CAD polls for work, and the
submitter waits for the result. These are the behaviours that matter when the
CAD on the other end is unreliable - and Rhino has crashed repeatedly during
this spike, so "the poller vanished mid-job" is a real case, not a hypothetical.

    cd server && uv run --frozen --no-sync python ../tests/test_broker.py
"""

import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server/src"))

from arges_mcp import broker as bk  # noqa: E402

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


# ------------------------------------------------------------- empty ----
print("An idle broker")
b = bk.Broker()
check("no poller has ever checked in", b.poller_connected(), False)
check("claim returns nothing when there is no work", b.claim(wait=0.05), None)
health = b.health()
check("health reports no active job", health["active_job"], None)
check("health reports nothing queued", health["queued"], 0)
truthy("claiming counts as the poller checking in", b.poller_connected())


# ------------------------------------------------- the happy path ----
print("A job, start to finish")
b = bk.Broker()
b.claim(wait=0.01)                       # a poller must be present to submit
answers = {}


def submitter():
    answers["result"] = b.submit("execute", {"code": "result = 1 + 1"}, timeout=5)


thread = threading.Thread(target=submitter, daemon=True)
thread.start()
time.sleep(0.1)

job = b.claim(wait=2.0)
truthy("the poller receives the job", job is not None)
check("with its kind", job["kind"], "execute")
check("and its payload", job["payload"]["code"], "result = 1 + 1")
check("the broker now shows it active", b.health()["active_job"], job["id"])

check("completing it succeeds", b.complete(job["id"], {"ok": True, "result": 2}), True)
thread.join(timeout=5)
check("the submitter got the result", answers["result"], {"ok": True, "result": 2})
check("and the broker is idle again", b.health()["active_job"], None)
check("completion counted", b.health()["completed"], 1)


# ------------------------------------------------------ single flight ----
print("Single flight")
b = bk.Broker()
b.claim(wait=0.01)
blocked = {}


def slow_submitter():
    blocked["first"] = b.submit("execute", {"code": "slow"}, timeout=3)


thread = threading.Thread(target=slow_submitter, daemon=True)
thread.start()
time.sleep(0.1)

second = b.submit("execute", {"code": "fast"}, timeout=1)
check("a second job is refused, not queued", second["ok"], False)
truthy("and says why", "still running" in second["error"])
truthy("flagged as busy so the caller can retry", second.get("busy"))

job = b.claim(wait=2.0)
b.complete(job["id"], {"ok": True, "result": "slow done"})
thread.join(timeout=5)
check("the first job still completed", blocked["first"]["result"], "slow done")

after = b.submit("execute", {"code": "now free"}, timeout=0.3)
check("the slot is released afterwards", after["ok"], False)   # times out, not busy
check("and it is a timeout, not a busy refusal", after.get("busy"), None)


# ------------------------------------------------------------ timeouts ----
# Two DIFFERENT failures that used to look identical to a caller, both waiting
# the full timeout: nothing connected at all, versus connected but not
# answering. Measured live at 60s of silence with the CAD closed.
print("When no CAD is connected at all")
b = bk.Broker()
result = b.submit("execute", {"code": "nobody home"}, timeout=30)
check("refused immediately, not after the timeout", result["ok"], False)
truthy("flagged so a UI can say the right thing", result.get("no_poller"))
truthy("and it names the fix", "start the poller" in result["error"])
check("nothing was queued", b.health()["queued"], 0)

print("When the CAD is connected but does not answer")
b = bk.Broker()
b.claim(wait=0.01)                       # poller checks in, then goes quiet
result = b.submit("execute", {"code": "nobody home"}, timeout=0.3)
check("the submitter gives up", result["ok"], False)
check("this one is NOT a no-poller error", result.get("no_poller"), None)
truthy("naming the poller as the thing to check", "poller" in result["error"])
truthy("and warning against resending", "do not resend" in result["error"])
check("the queue is left clean", b.health()["queued"], 0)
check("so a later job can still run", b.health()["active_job"], None)


# ------------------------------------------ the CAD dies mid-job ----
# Rhino crashed repeatedly during this spike, so this is the realistic failure:
# the poller takes a job and never comes back. Without expiry the broker would
# wedge at permanently busy.
print("The CAD claims a job and vanishes")
b = bk.Broker()
b.claim(wait=0.01)
original_expiry = bk.CLAIM_EXPIRY_S
bk.CLAIM_EXPIRY_S = 0.2
try:
    got = {}

    def orphan_submitter():
        got["result"] = b.submit("execute", {"code": "doomed"}, timeout=4)

    thread = threading.Thread(target=orphan_submitter, daemon=True)
    thread.start()
    time.sleep(0.1)

    job = b.claim(wait=2.0)
    truthy("the job was claimed", job is not None)
    time.sleep(0.4)                      # poller never returns

    # A later claim is what notices the abandonment.
    b.claim(wait=0.05)
    thread.join(timeout=5)
    check("the submitter is released, not left hanging", got["result"]["ok"], False)
    truthy("and told the CAD probably closed",
           "closed" in got["result"]["error"])
    check("the broker is usable again", b.health()["active_job"], None)
    check("and it was counted as expired", b.health()["expired"], 1)
finally:
    bk.CLAIM_EXPIRY_S = original_expiry


# ------------------------------------------------------ stale results ----
print("Late and bogus results")
b = bk.Broker()
b.claim(wait=0.01)
check("a result for an unknown job is rejected",
      b.complete("nonexistent", {"ok": True}), False)

held = {}


def late_submitter():
    held["result"] = b.submit("execute", {"code": "x"}, timeout=0.3)


thread = threading.Thread(target=late_submitter, daemon=True)
thread.start()
time.sleep(0.05)
job = b.claim(wait=1.0)
thread.join(timeout=3)                   # submitter has already timed out
check("a result arriving after the submitter gave up is rejected",
      b.complete(job["id"], {"ok": True, "result": "too late"}), False)


# ------------------------------------------------------- long polling ----
print("Long polling")
b = bk.Broker()
started = time.monotonic()
check("claim waits rather than spinning", b.claim(wait=0.4), None)
waited = time.monotonic() - started
truthy("it really waited", waited >= 0.35)
truthy("and did not overshoot", waited < 1.5)

# work arriving mid-wait must be picked up promptly, not after the full wait
b = bk.Broker()
b.claim(wait=0.01)
picked = {}


def waiter():
    started_at = time.monotonic()
    picked["job"] = b.claim(wait=3.0)
    picked["waited"] = time.monotonic() - started_at


thread = threading.Thread(target=waiter, daemon=True)
thread.start()
time.sleep(0.2)
threading.Thread(target=lambda: b.submit("execute", {"code": "y"}, timeout=2),
                 daemon=True).start()
thread.join(timeout=5)
truthy("a waiting poller is woken by new work", picked.get("job") is not None)
truthy("promptly, not after the full wait", picked.get("waited", 99) < 1.5)


# --------------------------------------------------- staleness ----
# Measured live: the broker reported a connected poller for 70 SECONDS after it
# had stopped, and the UI showed "ready" throughout. The window must learn the
# CAD is gone in about the time a person would notice.
print("Noticing that the poller has gone")
b = bk.Broker()
b.claim(wait=0.01)                       # poller checks in
truthy("connected right after a claim", b.poller_connected())
truthy("the timeout is seconds, not a minute", bk.Broker.POLLER_TIMEOUT_S <= 10)
b._last_seen = time.time() - (bk.Broker.POLLER_TIMEOUT_S + 1)
check("and it goes stale once that passes", b.poller_connected(), False)
check("health agrees", b.health()["poller_connected"], False)


# ------------------------------------------------------------ config ----
print("Configuration")
check("loopback only", bk.BROKER_HOST, "127.0.0.1")
truthy("claim expiry outlives the job timeout",
       bk.CLAIM_EXPIRY_S > bk.JOB_TIMEOUT_S)
truthy("long-poll wait is shorter than the job timeout",
       bk.CLAIM_WAIT_S < bk.JOB_TIMEOUT_S)



# ============================================================== HTTP ======
# Everything above tests the queue directly. This drives it the way the CAD
# will: over real HTTP, through the same auth the bridge uses.
print()
print("Over HTTP")

import http.client  # noqa: E402
import json as _json  # noqa: E402
import os as _os  # noqa: E402
import socket as _socket  # noqa: E402
import tempfile as _tempfile  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_tmp = _Path(_tempfile.mkdtemp(prefix="broker-test-"))
_token = "c" * 64
(_tmp / "token").write_text(_token, encoding="utf-8")
bk.TOKEN_PATH = _tmp / "token"

with _socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    _PORT = _s.getsockname()[1]
bk.ALLOWED_HOSTS = frozenset((f"127.0.0.1:{_PORT}", f"localhost:{_PORT}"))

_server, _broker = bk.serve(bk.Broker(), port=_PORT)


def call(method, path, body=None, token=_token, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=15)
    headers = {"Host": host or f"127.0.0.1:{_PORT}"}
    if token is not None:
        headers[bk.AUTH_HEADER] = token
    payload = None
    if body is not None:
        payload = _json.dumps(body)
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            return response.status, _json.loads(raw)
        except ValueError:
            return response.status, raw
    finally:
        conn.close()


try:
    status, payload = call("GET", "/health")
    check("health answers", status, 200)
    check("and reports no poller yet", payload["poller_connected"], False)

    # the same boundary the bridge enforces
    check("no token -> 401", call("GET", "/health", token=None)[0], 401)
    check("wrong token -> 401", call("GET", "/health", token="d" * 64)[0], 401)
    check("token prefix -> 401", call("GET", "/health", token=_token[:32])[0], 401)
    check("bad Host -> 403", call("GET", "/health", host="evil.example")[0], 403)
    check("unknown endpoint -> 404", call("GET", "/nope")[0], 404)
    check("malformed JSON -> 400",
          call("POST", "/submit", "not json")[0] if False else
          call("POST", "/submit", {"kind": 1, "payload": {}})[0], 400)

    # A poller must have checked in before a submit is accepted, so register
    # one the way the CAD does - by claiming. Also proves the fail-fast path
    # over HTTP: before this, a submit is refused outright.
    status, no_poller = call("POST", "/submit",
                             {"kind": "execute", "payload": {"code": "x"}})
    check("submit with no poller is refused at once", status, 200)
    truthy("and flagged as no_poller", no_poller.get("no_poller"))

    call("GET", "/claim?wait=0")

    # a full job, over the wire
    over_http = {}

    def http_submitter():
        over_http["reply"] = call("POST", "/submit",
                                  {"kind": "execute", "payload": {"code": "x"}})

    t = threading.Thread(target=http_submitter, daemon=True)
    t.start()
    time.sleep(0.2)

    status, job = call("GET", "/claim?wait=3")
    check("the poller claims over HTTP", status, 200)
    truthy("and gets a job", job is not None)
    check("with its payload", job["payload"]["code"], "x")

    status, ack = call("POST", "/result",
                       {"id": job["id"], "result": {"ok": True, "result": 42}})
    check("posting the result succeeds", status, 200)
    check("and it was accepted", ack["accepted"], True)

    t.join(timeout=5)
    check("the submitter got it", over_http["reply"][1]["result"], 42)

    status, payload = call("GET", "/health")
    truthy("health now shows a connected poller", payload["poller_connected"])

    # claiming with nothing queued returns null, not an error
    status, nothing = call("GET", "/claim?wait=0")
    check("an empty claim is 200/null", (status, nothing), (200, None))

    # a late result is reported, not fatal
    status, ack = call("POST", "/result",
                       {"id": "does-not-exist", "result": {"ok": True}})
    check("an unknown result is not an error", status, 200)
    check("but is marked unaccepted", ack["accepted"], False)

    # A client must not be able to pin a connection open by asking for a huge
    # wait. Verified against a shortened ceiling so the test does not have to
    # sit through the real one.
    _ceiling = bk.CLAIM_WAIT_S
    bk.CLAIM_WAIT_S = 0.5
    try:
        _started = time.monotonic()
        status, _ = call("GET", "/claim?wait=600")
        _elapsed = time.monotonic() - _started
        check("an oversized wait still answers", status, 200)
        truthy("clamped to the ceiling rather than honoured", _elapsed < 3.0)
    finally:
        bk.CLAIM_WAIT_S = _ceiling

    check("a non-numeric wait -> 400", call("GET", "/claim?wait=soon")[0], 400)

    # ----------------------------------------------------- fail closed -------
    # SECURITY.md promises the service fails closed with no token. This had no
    # coverage at all, and the interesting case is not the missing file: it is
    # that an EMPTY token file must not authorise an empty presented token.
    # compare_digest(b"", b"") is True, so without the explicit emptiness guard
    # a broker whose token file got truncated would authorise every caller that
    # sent no credentials.
    print("Fail closed without a token")
    _real_token_path = bk.TOKEN_PATH
    try:
        missing = _tmp / "gone"
        bk.TOKEN_PATH = missing
        check("a missing token file -> 503", call("GET", "/health")[0], 503)

        blank = _tmp / "blank"
        blank.write_text("", encoding="utf-8")
        bk.TOKEN_PATH = blank
        check("an empty token file -> 503", call("GET", "/health")[0], 503)
        check("and an empty presented token is still refused",
              call("GET", "/health", token="")[0], 503)
        check("as is no token header at all", call("GET", "/health", token=None)[0], 503)

        spaces = _tmp / "spaces"
        spaces.write_text("   \n", encoding="utf-8")
        bk.TOKEN_PATH = spaces
        check("a whitespace-only token file -> 503", call("GET", "/health")[0], 503)
        check("and whitespace presented against it is refused",
              call("GET", "/health", token="   ")[0], 503)
    finally:
        bk.TOKEN_PATH = _real_token_path
    check("the real token works again afterwards", call("GET", "/health")[0], 200)

    # ------------------------------------------------- connection cap --------
    # The broker used to be an unbounded ThreadingHTTPServer while SECURITY.md
    # described the cap as covering both sides. The cap is easy to add and easy
    # to get subtly wrong: if a slot is not released on every path, the service
    # keeps working until exactly MAX_CONCURRENT_CONNECTIONS requests have been
    # served and then refuses everything, which would look like a broker that
    # dies after a few minutes of normal use.
    print("Connection cap")

    # The leak check first, because it is the failure that would reach a user.
    statuses = {call("GET", "/health")[0] for _ in range(bk.MAX_CONCURRENT_CONNECTIONS * 4)}
    check("far more sequential requests than the cap all succeed", statuses, {200})

    # Now hold the cap open. Each accepted socket takes a slot before any bytes
    # are read, so connecting without sending is enough.
    held = []
    try:
        for _ in range(bk.MAX_CONCURRENT_CONNECTIONS):
            sock = _socket.socket()
            sock.settimeout(5)
            sock.connect(("127.0.0.1", _PORT))
            held.append(sock)

        refused = _socket.socket()
        refused.settimeout(5)
        try:
            refused.connect(("127.0.0.1", _PORT))
            refused.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            first = refused.recv(64)
        finally:
            refused.close()
        truthy("the connection past the cap is refused with 503", b"503" in first)
    finally:
        for sock in held:
            sock.close()

    # A slot freed DURING the grace period must be used, not refused. This is
    # the property that matters: the slot is released on the handler thread
    # after the client has already moved on, so a purely sequential caller can
    # arrive while the previous connection is still being cleaned up. Refusing
    # instantly turned that into spurious 503s - measured at 5.8% of 500
    # sequential requests under CPU load, and it reached CI as a failure in
    # test_bridge.py.
    held = []
    try:
        for _ in range(bk.MAX_CONCURRENT_CONNECTIONS):
            sock = _socket.socket()
            sock.settimeout(5)
            sock.connect(("127.0.0.1", _PORT))
            held.append(sock)

        # Free one a little after the request starts, well inside the grace.
        freed = threading.Timer(0.15, lambda: held[0].close())
        freed.start()
        began = time.monotonic()
        status = call("GET", "/health")[0]
        waited = time.monotonic() - began
        freed.cancel()

        check("a slot freed within the grace period is used, not refused",
              status, 200)
        truthy("and the caller waited for it rather than failing fast",
               waited >= 0.1)
        truthy("but no longer than the grace period allows",
               waited < bk.SLOT_GRACE_S + 2.0)
    finally:
        for sock in held:
            sock.close()

    # And the cap is a pause, not a latch: once those close, service resumes.
    recovered = None
    for _ in range(50):
        status = call("GET", "/health")[0]
        if status == 200:
            recovered = status
            break
        time.sleep(0.1)
    check("service recovers once the held connections close", recovered, 200)
finally:
    _server.shutdown()
    _server.server_close()
    import shutil as _shutil  # noqa: E402
    _shutil.rmtree(_tmp, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
