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
          {"transcript": [], "core_context": None, "busy": False})

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
            session.client = _DeadClient(error)   # a fresh one
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
    recovery = [e for e in session.events if e.get("type") == "notice"]
    truthy("the notice says the conversation survived",
           recovery and "intact" in recovery[0]["message"])

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

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
