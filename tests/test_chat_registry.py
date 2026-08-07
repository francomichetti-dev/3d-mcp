"""Offline checks for the per-design chat logic — no Fusion, no network.

    cd agent && uv run --frozen --no-sync python ../tests/test_chat_registry.py
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

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
]:
    check(f"rejects {bad[:44]}", svc.ATTACHMENT_ID.match(bad) is None, True)

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

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
