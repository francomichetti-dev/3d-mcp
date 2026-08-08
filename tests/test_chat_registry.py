"""Offline checks for the per-design chat logic — no Fusion, no network.

    cd agent && uv run --frozen --no-sync python ../tests/test_chat_registry.py
"""

import asyncio
import socket as _socket
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent"))

import agent_service as svc  # noqa: E402

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


# ---------------------------------------------------------------- Store ----
print("Store")
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "sub" / "chats.json"
    s = svc.Store(path)
    check("starts empty", s.data["designs"], {})
    check("get missing -> {}", s.get("nope"), {})

    s.update("file:abc", name="scooter", session_id="sid-1")
    check("update writes", json.loads(path.read_text())["designs"]["file:abc"]["name"], "scooter")
    check("file mode 0600", stat.S_IMODE(os.stat(path).st_mode), 0o600)
    check("parent dir 0700", stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
    check("no temp left behind", list(path.parent.glob("*.tmp")), [])

    s2 = svc.Store(path)
    check("reload round-trips", s2.get("file:abc")["session_id"], "sid-1")

    s2.forget("file:abc")
    check("forget removes", svc.Store(path).get("file:abc"), {})

    # a corrupt file must not take the service down with it
    path.write_text("{ this is not json")
    check("corrupt -> clean start", svc.Store(path).data["designs"], {})

    # a valid-JSON-but-wrong-shape file is equally hostile
    path.write_text('["not", "a", "dict"]')
    check("wrong shape -> clean start", svc.Store(path).data["designs"], {})


# ------------------------------------------------------------- Registry ----
print("Registry")


def fresh_registry(tmpdir):
    r = svc.Registry.__new__(svc.Registry)          # skip __init__'s httpx client
    r.store = svc.Store(Path(tmpdir) / "chats.json")
    r.sessions = {}
    r.subscribers = set()
    r.current = None
    r.bridge_ok = None
    r._dirty = False
    r._compressing = set()
    return r


with tempfile.TemporaryDirectory() as d:
    r = fresh_registry(d)

    # -- transcript cap
    for i in range(svc.MAX_TRANSCRIPT_EVENTS + 50):
        r.append_transcript("k", {"type": "text", "text": str(i)})
    tr = r.store.get("k")["transcript"]
    check("transcript capped", len(tr), svc.MAX_TRANSCRIPT_EVENTS)
    check("cap keeps the NEWEST", tr[-1]["text"], str(svc.MAX_TRANSCRIPT_EVENTS + 49))
    check("cap drops the oldest", tr[0]["text"], "50")

    # -- dirty/flush
    truthy("append marks dirty", r._dirty)
    r.flush()
    check("flush clears dirty", r._dirty, False)
    check("flush persisted", len(svc.Store(Path(d) / "chats.json").get("k")["transcript"]),
          svc.MAX_TRANSCRIPT_EVENTS)

    # -- snapshot
    snap = r.snapshot("k")
    check("snapshot transcript", len(snap["transcript"]), svc.MAX_TRANSCRIPT_EVENTS)
    check("snapshot core_context absent", snap["core_context"], None)
    check("snapshot not busy", snap["busy"], False)
    check("snapshot of None key", r.snapshot(None),
          {"transcript": [], "core_context": None, "plan": [], "busy": False})

    # -- _changed: the switch detector
    r.current = None
    truthy("None -> doc is a change", r._changed({"key": "a", "name": "X", "saved": True, "design": True}))
    r.current = {"key": "a", "name": "X", "saved": True, "design": True}
    check("identical is not a change",
          r._changed({"key": "a", "name": "X", "saved": True, "design": True}), False)
    truthy("different key is a change",
           r._changed({"key": "b", "name": "X", "saved": True, "design": True}))
    truthy("rename is a change (header must update)",
           r._changed({"key": "a", "name": "Y", "saved": True, "design": True}))
    truthy("save flips is a change",
           r._changed({"key": "a", "name": "X", "saved": False, "design": True}))
    truthy("closing the last doc is a change", r._changed(None))
    # two unstamped documents both key None: no chat exists for either, so
    # showing the same empty panel is correct, not a bug
    r.current = {"key": None, "name": "Untitled", "saved": False, "design": True}
    check("unstamped -> unstamped, same name: no switch",
          r._changed({"key": None, "name": "Untitled", "saved": False, "design": True}), False)


# -------------------------------------------------------- event fan-out ----
print("Event fan-out")


async def fanout_checks():
    r = svc.Registry.__new__(svc.Registry)
    r.subscribers = set()

    a, b = r.subscribe(), r.subscribe()
    r.publish({"type": "text", "text": "hello"})
    # The bug this replaces: a single shared asyncio.Queue hands each event to
    # exactly ONE getter, so two viewers each saw about half the conversation.
    check("both subscribers get it", (a.qsize(), b.qsize()), (1, 1))
    check("same payload", (await a.get())["text"], "hello")
    check("independent queues", (await b.get())["text"], "hello")

    r.unsubscribe(a)
    r.publish({"type": "text", "text": "after"})
    check("unsubscribed stops receiving", a.qsize(), 0)
    check("remaining still receives", b.qsize(), 1)

    # a viewer that stopped reading must not stall a turn
    c = r.subscribe()
    for i in range(svc.EVENT_QUEUE_LIMIT + 25):
        r.publish({"type": "text", "text": str(i)})
    check("stalled viewer is bounded", c.qsize(), svc.EVENT_QUEUE_LIMIT)
    check("stalled viewer keeps the NEWEST", (await c.get())["text"], "25")


asyncio.run(fanout_checks())


# ------------------------------------------- destructive gate (unchanged) ----
print("Destructive gate: only the unrecoverable operations ask")
cases = [
    # Recoverable through the timeline / undo — deliberately NOT gated, so the
    # chat does not interrupt ordinary subtractive modelling.
    ("body.deleteMe()", False),
    ("design.timeline.deleteAllAfterMarker()", False),
    ("tl.markerPosition = 3", False),
    ("design.designType = adsk.fusion.DesignTypes.DirectDesignType", False),
    ("root.features.combineFeatures.add(inp)", False),
    ("adsk.fusion.FeatureOperations.CutFeatureOperation", False),
    ("sketches.remove(s)", False),
    # Not recoverable by any means — these still ask.
    ("app.documents.item(0).close(False)", True),
    ("app.activeDocument.close(False)", True),
    ("doc.close(True)", True),
    ("doc.saveAs('x', f, '', '')", True),
    # The pattern is `.saveAs(` OR `.save(`; only the first half had a case, so
    # a plain save was gated by a branch nothing exercised.
    ("doc.save()", True),
    ("doc.save( )", True),
    # ...and the near-misses that must NOT ask, because a gate that fires on
    # ordinary API calls trains people to click through it without reading.
    ("doc.saveAsAlias(name)", False),
    ("if closeEnough(a, b): pass", False),
    ("handler = doc.close", False),
    ("extrudes.add(inp)", False),
    ("sk = root.sketches.add(plane)", False),
    ("result = design.rootComponent.bRepBodies.count", False),
]
for code, expect_blocked in cases:
    reason = svc.destructive_reason("mcp__fusion__fusion_execute", {"code": code})
    check(f"gate {'blocks' if expect_blocked else 'allows'}: {code[:38]}",
          reason is not None, expect_blocked)

check("gate ignores non-execute tools",
      svc.destructive_reason("mcp__fusion__fusion_screenshot", {"code": "x.deleteMe()"}), None)


# ----------------------------------------------------------- attachments ----
print("Attachments")

# ids are generated, never user-supplied, so they are matched against an exact
# shape rather than sanitised
good = "a0e4b165d25040c1/dd6ec782b14048beab65bc43d7147249.png"
truthy("accepts a real id", svc.ATTACHMENT_ID.match(good))
for bad in [
    "../../../../etc/passwd",
    "a0e4b165d25040c1/../../../etc/passwd",
    "AAAA/BBBB.png",
    "a0e4b165d25040c1/dd6ec782b14048beab65bc43d7147249.exe",
    "a0e4b165d25040c1/dd6ec782b14048beab65bc43d7147249.png/../x",
    "/etc/passwd",
    "a0e4b165d25040c1\\dd6ec782b14048beab65bc43d7147249.png",
    "A0E4B165D25040C1/dd6ec782b14048beab65bc43d7147249.png",   # uppercase hex
    # Python's `$` also matches just before a trailing newline, and a newline
    # really does arrive here: a request for ...png%0A reaches match_info with
    # a literal newline in it, measured against aiohttp. The pattern uses \Z
    # for that reason, and this is what would notice it going back to `$`.
    "a0e4b165d25040c1/dd6ec782b14048beab65bc43d7147249.png\n",
    "a0e4b165d25040c1/dd6ec782b14048beab65bc43d7147249.png\r\n",
]:
    check(f"rejects {bad[:44]!r}", svc.ATTACHMENT_ID.match(bad) is None, True)

# the design key never becomes a path component: a saved key is a URN of colons
folder = svc.design_folder("file:urn:adsk.wipprod:dm.lineage:dQ6U4Jnt")
check("key hashed into one safe component", len(folder.name), 16)
truthy("hash is hex", all(c in "0123456789abcdef" for c in folder.name))
check("stays under the attachments dir", folder.parent, svc.ATTACH_DIR)
check("traversal in a key cannot escape",
      svc.design_folder("../../etc").parent, svc.ATTACH_DIR)

# upload validation
def dec(images):
    return svc._decode_images(images)

check("no images -> empty", dec(None), ([], None))
check("not a list", dec("nope")[1], "attachments must be a list")
truthy("too many images", dec([{"media_type": "image/png", "data": "AA=="}] * 99)[1])
truthy("bad media type", dec([{"media_type": "image/svg+xml", "data": "AA=="}])[1])
truthy("bad base64", dec([{"media_type": "image/png", "data": "not!base64"}])[1])
truthy("empty payload", dec([{"media_type": "image/png", "data": ""}])[1])
import base64 as _b64
big = _b64.b64encode(b"x" * (svc.MAX_IMAGE_BYTES + 1)).decode()
truthy("oversized rejected", dec([{"media_type": "image/png", "data": big, "name": "big.png"}])[1])
ok_images, err = dec([{"media_type": "image/png", "data": _b64.b64encode(b"hello").decode(),
                       "name": "a.png"}])
check("valid image accepted", err, None)
check("decoded to bytes", ok_images[0]["raw"], b"hello")
truthy("every allowed type has an extension",
       all(svc.ALLOWED_IMAGE_TYPES.values()))


# ------------------------------------------------------------ constants ----
print("Wiring")
truthy("user events are persisted", "user" in svc.PERSISTED_EVENTS)
check("turn_end is not persisted", "turn_end" in svc.PERSISTED_EVENTS, False)
check("permission is not persisted", "permission" in svc.PERSISTED_EVENTS, False)
truthy("compress prompt mentions core context", "CORE CONTEXT" in svc.COMPRESS_PROMPT)
truthy("system prompt warns about other designs",
       "other tabs" in svc.SYSTEM_APPEND or "other designs" in svc.SYSTEM_APPEND)

# ------------------------------------------------------ DNS rebinding -------
# This service holds the bridge token and forwards to the bridge, so anything
# that reaches it gets authenticated arbitrary code execution inside Fusion.
# Binding to loopback does not stop a browser: a page can have its DNS rebound
# to 127.0.0.1 and POST here.
#
# Two measured facts make that a real request rather than a theoretical one:
# aiohttp's request.json() ignores Content-Type, so a text/plain body parses
# identically to application/json; and text/plain is a CORS "simple request"
# type, so no preflight is sent. The attacker cannot read the reply, but the
# geometry has already changed by then.
print("Host is pinned against DNS rebinding")


async def rebinding_checks():
    from aiohttp import web, ClientSession

    app = svc.build_app(7655)
    # These reach for Fusion; the middleware runs before any of it.
    app.on_startup.clear()
    app.on_cleanup.clear()
    runner = web.AppRunner(app)
    await runner.setup()
    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    await web.TCPSite(runner, "127.0.0.1", port).start()

    async def post(host, content_type):
        async with ClientSession() as session:
            async with session.post(
                    f"http://127.0.0.1:{port}/send", data='{"prompt":"x"}',
                    headers={"Content-Type": content_type, "Host": host}) as reply:
                return reply.status

    try:
        # The attack, in the shape that needs no preflight.
        check("a rebound host is refused, text/plain",
              await post("evil.example.com", "text/plain"), 403)
        check("a rebound host is refused, application/json",
              await post("evil.example.com", "application/json"), 403)
        check("a bare hostname is refused",
              await post("evil.example.com:7655", "text/plain"), 403)
        # Right host, wrong port: a second service on the same machine must not
        # be able to borrow this one's origin.
        check("the right host on the wrong port is refused",
              await post("127.0.0.1:9999", "application/json"), 403)
        check("an absent Host is refused", await post("", "application/json"), 403)

        # And the panel itself still gets through. It is served from this
        # origin, so its Host is one of these two. Anything but 403 means the
        # middleware passed it on - the handler then fails on its own for want
        # of a registry, which is this harness's doing, not the guard's.
        for host in ("127.0.0.1:7655", "localhost:7655"):
            status = await post(host, "application/json")
            check(f"the panel's own Host ({host}) is not blocked", status != 403, True)
    finally:
        await runner.cleanup()


asyncio.run(rebinding_checks())

# Every route is behind the middleware, not just /send. A guard applied per
# handler is one new route away from being incomplete.
check("the guard is middleware, so it covers every route",
      svc.pin_host in svc.build_app(7655).middlewares, True)

# ------------------------------------------------ transport recovery --------
# A screenshot-heavy turn produced an NDJSON line over the SDK's 1 MiB default
# and the transport raised CLIJSONDecodeError mid-build. The turn was caught
# and `busy` cleared, so the panel went on accepting messages — into a client
# whose read stream had already ended. Every one of them vanished, which looked
# exactly like the chat had died.
#
# Two behaviours matter and they differ: a TRANSPORT failure means this client
# is finished and must be replaced; an ordinary turn failure does not, and
# reconnecting there would throw away a working conversation for nothing.
print("A dead transport is replaced, an ordinary failure is not")


class _WorkingClient:
    """A replacement client that answers normally."""

    async def receive_response(self):
        yield {"ok": True}

    async def disconnect(self):
        pass


class _DeadClient:
    """A client whose response stream fails the way a real one does."""

    def __init__(self, error):
        self.error = error
        self.disconnected = False

    async def receive_response(self):
        raise self.error
        yield  # pragma: no cover - makes this an async generator

    async def disconnect(self):
        self.disconnected = True


async def recovery_checks():
    from claude_agent_sdk import CLIJSONDecodeError

    def build(error):
        registry = svc.Registry.__new__(svc.Registry)
        registry.subscribers = set()
        registry.store = svc.Store(Path(tempfile.mkdtemp()) / "chats.json")
        registry._dirty = False

        async def pin(_key):
            return None

        registry.pin = pin
        registry.publish = lambda event: None
        registry.append_transcript = lambda *a, **k: None

        session = svc.Session.__new__(svc.Session)
        session.key = "k"
        session.name = "design"
        session.registry = registry
        session.client = _DeadClient(error)
        session.pending = {}
        session.lock = asyncio.Lock()
        session.ready = asyncio.Event()
        session.ready.set()
        session.start_error = None
        session.busy = False
        session.transport_failures = 0
        session.sdk_session_id = None
        session.plan = []

        session.events = []

        async def emit(event):
            session.events.append(event)

        session.emit = emit

        async def send(_prompt, _images):
            return None

        session._send = send

        session.restarts = 0

        async def start():
            session.restarts += 1
            # A working replacement: this block is about the rebuild itself,
            # not about what happens when the replacement is broken too (the
            # retry bound below covers that).
            session.client = _WorkingClient()
            session.ready.set()

        session.start = start
        return session

    # The real thing: the exact error the SDK raises past its buffer limit.
    boom = CLIJSONDecodeError("JSON message exceeded maximum buffer size of 1048576 bytes",
                              ValueError("Buffer size 1200000 exceeds limit 1048576"))
    session = build(boom)
    old_client = session.client
    await session.run_turn("build a tower")

    check("a transport failure rebuilds the client", session.restarts, 1)
    truthy("and the dead one is disconnected", old_client.disconnected)
    truthy("the new client is a different object", session.client is not old_client)
    check("and the turn is not left marked busy", session.busy, False)
    kinds = [e.get("type") for e in session.events]
    truthy("the failure is reported", "error" in kinds)
    truthy("and so is the recovery", "notice" in kinds)
    # This client fails before yielding anything, so nothing ever reached the
    # model and the correct recovery is to resend rather than to resume. The
    # resume path is covered separately below.
    recovery = [e for e in session.events if e.get("type") == "notice"]
    truthy("the notice says what it is doing about it",
           recovery and "resending" in recovery[0]["message"])

    # An ordinary failure must NOT throw the conversation away.
    session = build(RuntimeError("a tool blew up"))
    old_client = session.client
    await session.run_turn("build a tower")

    check("an ordinary turn failure does not reconnect", session.restarts, 0)
    check("and keeps the same client", session.client is old_client, True)
    check("still not busy", session.busy, False)
    truthy("and still reports the error",
           any(e.get("type") == "error" for e in session.events))


asyncio.run(recovery_checks())


# Three more ways this path could still leave somebody typing into nothing.
print("The failure modes around the reconnect")


async def hardening_checks():
    from claude_agent_sdk import CLIJSONDecodeError

    def bare_session():
        registry = svc.Registry.__new__(svc.Registry)
        registry.subscribers = set()
        registry.store = svc.Store(Path(tempfile.mkdtemp()) / "chats.json")
        registry._dirty = False

        async def pin(_key):
            return None

        registry.pin = pin
        registry.publish = lambda event: None
        registry.append_transcript = lambda *a, **k: None

        s = svc.Session.__new__(svc.Session)
        s.key = "k"
        s.name = "design"
        s.registry = registry
        s.client = None
        s.pending = {}
        s.lock = asyncio.Lock()
        s.ready = asyncio.Event()
        s.start_error = None
        s.busy = False
        s.transport_failures = 0
        s.sdk_session_id = None
        s.plan = []
        s.events = []

        async def emit(event):
            s.events.append(event)

        s.emit = emit
        return s

    # 1. A session that already failed to start must say so at once. Waiting
    #    the full 60s for an Event that nothing will ever set reproduces the
    #    exact symptom this whole fix is about: type, and nothing happens.
    s = bare_session()
    s.start_error = "agent failed to start: claude not found"
    began = asyncio.get_event_loop().time()
    await asyncio.wait_for(s.run_turn("hello"), timeout=5)
    took = asyncio.get_event_loop().time() - began
    truthy("a known-broken session answers immediately, not after the timeout",
           took < 2)
    truthy("and says what actually went wrong",
           any("claude not found" in (e.get("message") or "") for e in s.events))

    # 2. The race: ready is set, so a queued turn gets past the readiness check
    #    while a reconnect is in flight, and finds no client by the time it
    #    holds the lock. That used to be an assert, which reports as
    #    AttributeError on NoneType and vanishes entirely under -O.
    s = bare_session()
    s.ready.set()          # looks ready...
    s.client = None        # ...but the reconnect already tore the client down
    await asyncio.wait_for(s.run_turn("hello"), timeout=5)
    check("a turn with no client is refused, not crashed", s.busy, False)
    messages = [e.get("message") or "" for e in s.events]
    truthy("and the person is told to try again rather than shown a traceback",
           any("restarting" in m for m in messages))
    truthy("with no NoneType error leaking out",
           not any("NoneType" in m for m in messages))

    # 3. A transport that fails every time must stop respawning and ask for a
    #    restart. Retrying forever is a subprocess per message and no signal.
    s = bare_session()
    s.ready.set()
    boom = CLIJSONDecodeError("oversized", ValueError("oversized"))
    s.client = _DeadClient(boom)
    s.starts = 0

    async def start():
        s.starts += 1
        s.client = _DeadClient(boom)      # still broken
        s.ready.set()

    s.start = start

    async def send(_p, _i):
        return None

    s._send = send

    for _ in range(svc.MAX_TRANSPORT_RETRIES + 3):
        await asyncio.wait_for(s.run_turn("go"), timeout=5)

    check("it stops rebuilding after the threshold",
          s.starts, svc.MAX_TRANSPORT_RETRIES)
    truthy("and says how to recover",
           any("Restart the service" in (e.get("message") or "") for e in s.events))
    truthy("promising the conversation is kept",
           any("conversation is saved" in (e.get("message") or "") for e in s.events))

    # A turn that reaches the model clears the streak, so an isolated blip
    # never accumulates toward the give-up threshold.
    s = bare_session()
    s.ready.set()
    s.transport_failures = 2
    s.client = _DeadClient(RuntimeError("an ordinary tool failure"))

    async def send2(_p, _i):
        return None

    s._send = send2
    await asyncio.wait_for(s.run_turn("go"), timeout=5)
    check("an ordinary failure resets the streak", s.transport_failures, 0)


asyncio.run(hardening_checks())


# After a drop the service resumes the work itself rather than waiting to be
# told. Typing "continue" by hand was the workaround, not the design.
print("A dropped turn is resumed automatically")


class _FlakyClient:
    """Fails once part-way through, then behaves."""

    def __init__(self, error, fail_after: int):
        self.error = error
        self.fail_after = fail_after
        self.disconnected = False

    async def receive_response(self):
        for i in range(self.fail_after):
            yield {"n": i}
        raise self.error

    async def disconnect(self):
        self.disconnected = True


class _GoodClient:
    async def receive_response(self):
        yield {"n": "done"}

    async def disconnect(self):
        pass


async def auto_resume_checks():
    from claude_agent_sdk import CLIJSONDecodeError

    def session_with(first_client):
        registry = svc.Registry.__new__(svc.Registry)
        registry.subscribers = set()
        registry.store = svc.Store(Path(tempfile.mkdtemp()) / "chats.json")
        registry._dirty = False

        async def pin(_key):
            return None

        registry.pin = pin
        registry.publish = lambda event: None
        registry.append_transcript = lambda *a, **k: None

        s = svc.Session.__new__(svc.Session)
        s.key, s.name, s.registry = "k", "d", registry
        s.client = first_client
        s.pending, s.lock = {}, asyncio.Lock()
        s.ready = asyncio.Event(); s.ready.set()
        s.start_error, s.busy = None, False
        s.transport_failures = 0
        s.sdk_session_id = None
        s.plan = []
        s.events, s.sent = [], []

        async def emit(event):
            s.events.append(event)

        s.emit = emit

        async def send(prompt, images):
            s.sent.append((prompt, list(images)))

        s._send = send

        async def start():
            s.client = _GoodClient()
            s.ready.set()

        s.start = start
        return s

    boom = CLIJSONDecodeError("oversized", ValueError("oversized"))

    # The reported case: the model was mid-build when the transport died.
    s = session_with(_FlakyClient(boom, fail_after=3))
    await asyncio.wait_for(s.run_turn("build a lego tower"), timeout=5)

    check("the turn is retried without being asked", len(s.sent), 2)
    check("the first send is what was actually typed", s.sent[0][0], "build a lego tower")
    check("and the second resumes rather than restarts", s.sent[1][0], svc.RESUME_PROMPT)
    truthy("the resume tells it to re-read the design first",
           "current state" in svc.RESUME_PROMPT)
    truthy("and not to build anything twice",
           "not start over" in svc.RESUME_PROMPT and "repeat" in svc.RESUME_PROMPT)
    truthy("the person is told it carried on",
           any("carrying on" in (e.get("message") or "") for e in s.events))
    check("and the turn ends clean", s.busy, False)

    # The other half: the transport died before the model saw anything. There
    # is nothing to resume, so resuming would silently drop what was asked for.
    s = session_with(_FlakyClient(boom, fail_after=0))
    await asyncio.wait_for(s.run_turn("build a lego tower",
                                      [{"name": "ref.png", "media_type": "image/png",
                                        "b64": "AA=="}]), timeout=5)

    check("the original message is sent again", len(s.sent), 2)
    check("verbatim, not as a resume", s.sent[1][0], "build a lego tower")
    check("with its attachments intact", len(s.sent[1][1]), 1)
    truthy("and says it is resending",
           any("resending" in (e.get("message") or "") for e in s.events))

    # A turn that never fails must not be sent twice.
    s = session_with(_GoodClient())
    await asyncio.wait_for(s.run_turn("hello"), timeout=5)
    check("an ordinary turn is sent exactly once", len(s.sent), 1)


asyncio.run(auto_resume_checks())

# The SDK's 1 MiB default is what broke; the service must raise it. A 1920x1440
# viewport measured 631 KB of base64 on its own, so a turn that looks at the
# model from several angles crosses the default without doing anything odd.
truthy("the service raises the SDK's message-size ceiling",
       svc.MAX_SDK_MESSAGE_BYTES > 1024 * 1024)
truthy("with real headroom over a handful of screenshots",
       svc.MAX_SDK_MESSAGE_BYTES >= 8 * 1024 * 1024)
truthy("and passes it to the SDK rather than just defining it",
       "max_buffer_size=MAX_SDK_MESSAGE_BYTES" in
       (REPO_ROOT / "agent" / "agent_service.py").read_text(encoding="utf-8"))

# ------------------------------------------------------------- the plan -----
# The plan is what the person reads to answer "what is coming, what is done,
# and where did it stop?" — so it has to survive a closed design, and it must
# never be able to break the panel however malformed the model's version is.
print("The build plan")

# Whatever arrives is normalised into something renderable. The model is asked
# for a shape but not trusted to produce it.
check("a plain list of strings still works",
      svc._clean_plan(["baseplate", "tower"]),
      [{"title": "baseplate", "status": "todo"},
       {"title": "tower", "status": "todo"}])
check("status case does not matter",
      svc._clean_plan([{"title": "a", "status": "DOING"}])[0]["status"], "doing")
check("an unknown status falls back to todo",
      svc._clean_plan([{"title": "a", "status": "halfway"}])[0]["status"], "todo")
check("blank titles are dropped", svc._clean_plan([{"title": "   "}]), [])
check("junk entries are skipped", svc._clean_plan([42, None, {"title": "ok"}]),
      [{"title": "ok", "status": "todo"}])
check("not a list at all -> nothing", svc._clean_plan("do the thing"), [])
check("the list is capped", len(svc._clean_plan(
    [{"title": f"s{i}"} for i in range(svc.MAX_PLAN_STEPS + 10)])), svc.MAX_PLAN_STEPS)
truthy("a long title is trimmed rather than dropped",
       len(svc._clean_plan([{"title": "x" * 500}])[0]["title"]) <= 120)


async def plan_checks():
    with tempfile.TemporaryDirectory() as d:
        registry = svc.Registry.__new__(svc.Registry)
        registry.store = svc.Store(Path(d) / "chats.json")
        registry.sessions = {}
        registry.subscribers = set()
        registry.current = None
        registry.bridge_ok = None
        registry._dirty = False
        registry._compressing = set()

        s = svc.Session.__new__(svc.Session)
        s.key, s.name, s.registry = "design-1", "tower", registry
        s.plan = []
        s.events = []

        async def emit(event):
            s.events.append(event)

        s.emit = emit

        told = await s.apply_plan([
            {"title": "6x6 studded baseplate", "status": "done"},
            {"title": "round tower body", "status": "doing"},
            {"title": "battlements", "status": "todo"},
        ])
        check("the plan is held on the session", len(s.plan), 3)
        truthy("the model is told how it landed", "1/3 done" in told)
        shown = [e for e in s.events if e.get("type") == "plan"]
        check("and the panel is sent it", len(shown), 1)
        check("with the steps in order",
              [x["title"] for x in shown[0]["steps"]][1], "round tower body")

        # The point of persisting: reopening a design tomorrow still shows
        # where its build got to.
        check("it is written to disk",
              len(svc.Store(Path(d) / "chats.json").get("design-1")["plan"]), 3)
        check("and comes back in the snapshot the panel redraws from",
              [x["status"] for x in registry.snapshot("design-1")["plan"]],
              ["done", "doing", "todo"])

        # A design that never had one must not show an empty box.
        check("a design with no plan reports none", registry.snapshot("other")["plan"], [])

        # Updating replaces rather than appends — the model sends the whole
        # list every time, so anything else would double it.
        await s.apply_plan([{"title": "6x6 studded baseplate", "status": "done"},
                            {"title": "round tower body", "status": "done"}])
        check("an update replaces the previous plan", len(s.plan), 2)
        check("and persists the replacement",
              len(svc.Store(Path(d) / "chats.json").get("design-1")["plan"]), 2)

        # A malformed update must not wipe a good plan.
        before = list(s.plan)
        told = await s.apply_plan("not a list")
        check("junk leaves the plan alone", s.plan, before)
        truthy("and says so rather than failing silently", "No usable steps" in told)


asyncio.run(plan_checks())


# A checklist left standing from an earlier build reads as the plan for what is
# happening now. Reported from a real session: the panel showed 5/6 of a wheel
# build while a completely different question was being asked.
print("A new instruction clears the previous checklist")


async def plan_lifecycle_checks():
    from claude_agent_sdk import CLIJSONDecodeError

    def session_with(client):
        registry = svc.Registry.__new__(svc.Registry)
        registry.subscribers = set()
        registry.store = svc.Store(Path(tempfile.mkdtemp()) / "chats.json")
        registry._dirty = False

        async def pin(_key):
            return None

        registry.pin = pin
        registry.publish = lambda event: None
        registry.append_transcript = lambda *a, **k: None

        s = svc.Session.__new__(svc.Session)
        s.key, s.name, s.registry = "k", "d", registry
        s.client = client
        s.pending, s.lock = {}, asyncio.Lock()
        s.ready = asyncio.Event(); s.ready.set()
        s.start_error, s.busy = None, False
        s.transport_failures = 0
        s.sdk_session_id = None
        s.plan = []
        s.plan = [{"title": "an old step", "status": "done"}]
        s.registry.store.update("k", plan=s.plan)
        s.events, s.sent = [], []

        async def emit(event):
            s.events.append(event)

        s.emit = emit

        async def send(prompt, images):
            s.sent.append(prompt)

        s._send = send

        async def start():
            s.client = _GoodClient()
            s.ready.set()

        s.start = start
        return s

    s = session_with(_GoodClient())
    await asyncio.wait_for(s.run_turn("something completely different"), timeout=5)
    check("the old checklist is gone", s.plan, [])
    check("and the panel is told to hide it",
          [e for e in s.events if e.get("type") == "plan"][0]["steps"], [])
    check("which also persists, so switching back does not resurrect it",
          s.registry.store.get("k").get("plan"), [])

    # The turn resumed after a dropped connection is the SAME build continuing.
    # Clearing there would wipe the checklist exactly when it is most useful —
    # it is the only record of where the interruption landed.
    boom = CLIJSONDecodeError("oversized", ValueError("oversized"))
    s = session_with(_FlakyClient(boom, fail_after=2))
    await asyncio.wait_for(s.run_turn("build a tower"), timeout=5)
    check("the resume did not clear it a second time",
          len([e for e in s.events if e.get("type") == "plan"]), 1)
    check("and the turn really did resume", len(s.sent), 2)
    check("with the resume prompt, not the original", s.sent[1], svc.RESUME_PROMPT)

    # A design with no checklist must not emit a pointless event on every turn.
    s = session_with(_GoodClient())
    s.plan = []
    s.registry.store.update("k", plan=[])
    s.events.clear()
    await asyncio.wait_for(s.run_turn("hello"), timeout=5)
    check("no checklist, no event",
          [e for e in s.events if e.get("type") == "plan"], [])


asyncio.run(plan_lifecycle_checks())


# ------------------------------------------------------- the working banner --
# What the banner says while a turn runs. The tool log already carries the
# code; this is for somebody watching the viewport rather than reading Python.
print("What the banner says it is doing")
for code, expected in [
    ("fillets.add(inp)", "Filleting"),
    ("chamferFeats.add(c)", "Chamfering"),
    ("shellFeats.add(shellInput)", "Shelling"),
    ("holes.addSimple(...)", "Cutting holes"),
    ("revolves.add(rev)", "Revolving"),
    ("ext = extrudes.addSimple(prof, dist, op)", "Extruding"),
    ("root.features.combineFeatures.add(inp)", "Combining bodies"),
    ("mirrorFeats.add(m)", "Mirroring"),
    ("sk = root.sketches.add(root.xYConstructionPlane)", "Sketching"),
    ("cam = app.activeViewport.camera", "Setting the view"),
    ("x = 1 + 1", "Building"),
]:
    check(f"{code[:34]:36} -> {expected}",
          svc._activity_verb("mcp__fusion__fusion_execute", {"code": code}), expected)

check("a screenshot says it is looking",
      svc._activity_verb("mcp__fusion__fusion_screenshot", {}), "Looking at the result")
check("state says it is checking",
      svc._activity_verb("mcp__fusion__fusion_state", {}), "Checking the design")
check("the plan tool says planning",
      svc._activity_verb("mcp__plan__plan", {}), "Planning")

# A fillet inside a longer script still reads as filleting: the first match
# wins, and it is ordered so the interesting operation beats the sketch that
# set it up.
truthy("the operation beats the sketch that set it up",
       svc._activity_verb("mcp__fusion__fusion_execute",
                          {"code": "sk = sketches.add(p)\nfillets.add(inp)"}) == "Filleting")


# ------------------------------------------------------------ settings ------
# Model and effort are both fixed when a session's client connects, so both
# force a rebuild. What matters is that the rebuild resumes rather than
# restarts, and that it never lands on a turn already running.
print("Choosing a model and an effort level")

check("the offered models and efforts are non-empty",
      bool(svc.MODELS) and bool(svc.EFFORTS), True)
check("effort levels are exactly what the SDK accepts",
      [e for e, _ in svc.EFFORTS], ["low", "medium", "high", "xhigh", "max"])


async def settings_checks():
    with tempfile.TemporaryDirectory() as d:
        r = fresh_registry(d)
        r.publish = lambda event: None

        check("defaults before anything is chosen",
              (r.model(), r.effort()), (svc.DEFAULT_MODEL, svc.DEFAULT_EFFORT))

        # A value dropped from the list in some later upgrade must not leave the
        # panel unable to start a session at all.
        r.store.data["model"] = "claude-removed-in-2027"
        r.store.data["effort"] = "extreme"
        check("an unknown stored model falls back", r.model(), svc.DEFAULT_MODEL)
        check("an unknown stored effort falls back", r.effort(), svc.DEFAULT_EFFORT)

        r.store.data.pop("model"); r.store.data.pop("effort")
        check("a change is applied and persisted",
              await r.apply_settings(model="claude-sonnet-5", effort="max"), True)
        check("both took", (r.model(), r.effort()), ("claude-sonnet-5", "max"))
        check("and survive a reload",
              svc.Store(Path(d) / "chats.json").data.get("model"), "claude-sonnet-5")

        check("re-applying the same values changes nothing",
              await r.apply_settings(model="claude-sonnet-5", effort="max"), False)
        check("and junk is ignored rather than stored",
              await r.apply_settings(model="nonsense", effort="nonsense"), False)
        check("leaving the real values alone",
              (r.model(), r.effort()), ("claude-sonnet-5", "max"))

        # The reason both live behind one call: each rebuilds every session, so
        # changing them separately would tear each conversation down twice.
        rebuilt = []

        class _Session:
            def __init__(self, busy):
                self.busy = busy

            async def _reconnect(self):
                rebuilt.append(self)
                return True

        idle, running = _Session(False), _Session(True)
        r.sessions = {"a": idle, "b": running}
        await r.apply_settings(model="claude-opus-5", effort="low")
        check("one rebuild for a combined change", len(rebuilt), 1)
        check("the idle session was rebuilt", rebuilt[0], idle)
        truthy("and the running turn was left alone", running not in rebuilt)


asyncio.run(settings_checks())

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
