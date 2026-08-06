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
print("Destructive gate still intact")
cases = [
    ("body.deleteMe()", True),
    ("design.timeline.deleteAllAfterMarker()", True),
    ("tl.markerPosition = 3", True),
    ("design.designType = adsk.fusion.DesignTypes.DirectDesignType", True),
    ("root.features.combineFeatures.add(inp)", True),
    ("adsk.fusion.FeatureOperations.CutFeatureOperation", True),
    ("sketches.remove(s)", True),
    ("app.documents.item(0).close(False)", True),
    ("app.activeDocument.close(False)", True),
    ("doc.close(True)", True),
    ("doc.saveAs('x', f, '', '')", True),
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
