"""The MCP server's own logic - no Fusion, no bridge, no network.

The important one here is _resolve_export_path: it is the only thing stopping a
model-chosen filename writing anywhere on the disk, so it gets the same
treatment as the bridge's auth.

    cd agent && uv run --frozen --no-sync python ../tests/test_mcp_server.py
"""

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "server/src"))

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


def refuses(label, fn):
    """The call must raise, i.e. the path was rejected."""
    global PASS, FAIL
    try:
        result = fn()
    except Exception:                                      # noqa: BLE001
        PASS += 1
        return
    FAIL += 1
    print(f"  FAIL {label}: allowed, resolved to {result}")


from arges_mcp import server as srv  # noqa: E402

# Confine everything to a temp dir; never touch the real exports folder.
SANDBOX = Path(tempfile.mkdtemp(prefix="fusion-export-test-"))
srv.EXPORT_DIR = SANDBOX
ROOT = srv._export_root()


# ------------------------------------------------------------- slugging ----
print("Filename slugging")
check("spaces and case", srv._slug("Housing Top"), "Housing_Top")
check("dots cannot survive", "." in srv._slug("a.b.c"), False)
check("a traversal attempt becomes inert", "/" in srv._slug("../../etc/passwd"), False)
check("leading/trailing junk stripped", srv._slug("__weird__"), "weird")
check("length is capped", len(srv._slug("x" * 200)) <= 60, True)
check("an empty name stays empty", srv._slug("!!!"), "")
# The docstring claims dots are unsafe precisely so a stem cannot read as a
# path component; verify rather than trust it.
truthy("no slug can contain a separator",
       all(c not in srv._slug("a/b\\c..d") for c in "/\\."))


# ------------------------------------------------- export confinement ----
print("Export path confinement")

target = srv._resolve_export_path("", "stl", "Widget")
truthy("empty path -> generated name", target.name.startswith("Widget_"))
check("generated inside the root", target.parent, ROOT)
check("generated has the right suffix", target.suffix, ".stl")

target = srv._resolve_export_path("part", "stl", "x")
check("relative name lands in the root", target.parent, ROOT)
check("extension is appended", target.name, "part.stl")

target = srv._resolve_export_path("part.stl", "stl", "x")
check("an already-correct extension is not doubled", target.name, "part.stl")

target = srv._resolve_export_path("part.step", "stl", "x")
check("a mismatched extension is corrected", target.name, "part.step.stl")

target = srv._resolve_export_path("sub/dir/part", "stl", "x")
check("subdirectories are allowed inside the root", target.name, "part.stl")
truthy("and stay under it", str(target).startswith(str(ROOT)))

# --- the ones that matter
for label, attempt in [
    ("parent traversal", "../escaped"),
    ("deep traversal", "../../../../../../tmp/escaped"),
    ("absolute path", "/tmp/escaped"),
    ("absolute etc", "/etc/passwd"),
    ("home expansion", "~/escaped"),
    ("home of another user", "~root/escaped"),
    ("traversal in the middle", "sub/../../escaped"),
    ("trailing traversal", "sub/dir/../../../escaped"),
]:
    refuses(f"refuses {label}: {attempt}", lambda a=attempt: srv._resolve_export_path(a, "stl", "x"))

refuses("refuses the root itself", lambda: srv._resolve_export_path(str(ROOT), "stl", "x"))

# a symlink pointing out of the sandbox must not be a way through
escape_target = Path(tempfile.mkdtemp(prefix="fusion-export-outside-"))
link = ROOT / "sneaky"
ROOT.mkdir(parents=True, exist_ok=True)
if not link.exists():
    os.symlink(escape_target, link)
refuses("refuses a symlink that leaves the root",
        lambda: srv._resolve_export_path("sneaky/escaped", "stl", "x"))


# -------------------------------------------------------------- limits ----
print("Declared limits")
check("views", set(srv.VIEWS), {"front", "top", "right", "iso", "fit"})
check("formats", set(srv.FORMATS), {"stl", "step", "3mf", "usd"})
truthy("screenshot bounds are sane",
       srv.SCREENSHOT_MIN_SIDE < srv.SCREENSHOT_MAX_HEIGHT <= srv.SCREENSHOT_MAX_WIDTH)
check("bridge protocol version is pinned", srv.BRIDGE_PROTOCOL_VERSION, "1")
check("talks only to loopback", srv.BRIDGE_HOST, "127.0.0.1")
truthy("the MCP server never binds anything itself",
       not hasattr(srv, "BIND_HOST"))

# The server waits longer than the add-in, or a completed job would look like a
# timeout to the caller.
truthy("server timeout exceeds the add-in's marshal timeout",
       srv.HTTP_READ_TIMEOUT > 60.0)


# ------------------------------------------------------------- messages ----
print("Error messages are actionable")
for name in ("MSG_BRIDGE_DOWN", "MSG_TIMEOUT", "MSG_NO_TOKEN", "MSG_BAD_TOKEN",
             "MSG_TOO_LARGE", "MSG_NOT_BRIDGE"):
    text = getattr(srv, name, "")
    truthy(f"{name} exists and is not a stub", len(text) > 30)

truthy("the no-token message names the fix",
       "install" in srv.MSG_NO_TOKEN.lower())
truthy("the timeout message warns against resending",
       "do not resend" in srv.MSG_TIMEOUT.lower())
# A token in an error string would leak it into transcripts and logs.
truthy("no message could contain a token value",
       all("token" not in m.lower() or "not found" in m.lower() or
           "rejected" in m.lower() or "re-run" in m.lower()
           for m in [srv.MSG_NO_TOKEN, srv.MSG_BAD_TOKEN]))

# ------------------------------------------------- screenshot budget --------
# An image reaches the model as base64 inside one protocol message, and every
# transport in between bounds how long a single message may be. A dense
# viewport is what pushes it: measured live, 1200x800 is around 310 KB of
# base64 and 1920x1440 reaches 631 KB, and several captures in one turn is
# ordinary because the whole method is look-then-correct. Going over used to
# kill the turn with the geometry half-built.
print("Screenshot size is bounded")

captures = []


def fake_capture(size_for):
    """Stand in for the bridge, returning whatever size the test dictates."""
    def capture(view, width, height):
        captures.append((view, width, height))
        return b"\x89PNG" + b"x" * (size_for(width, height) - 4)
    return capture


real_capture = srv._capture
try:
    # In budget: returned as-is, and captured exactly once. A resize that fires
    # when it is not needed costs a second per screenshot for nothing.
    captures.clear()
    srv._capture = fake_capture(lambda w, h: 100 * 1024)
    image = srv.fusion_screenshot(view="iso", width=1200, height=800)
    check("an image inside the budget is captured once", len(captures), 1)
    check("at the size asked for", captures[0], ("iso", 1200, 800))
    check("and comes back whole", len(image.data), 100 * 1024)

    # Over budget: recaptured smaller rather than sent.
    captures.clear()
    sizes = iter([2 * 1024 * 1024, 200 * 1024])
    srv._capture = fake_capture(lambda w, h: next(sizes))
    image = srv.fusion_screenshot(view="iso", width=1920, height=1440)
    check("an oversized image is recaptured", len(captures), 2)
    truthy("at a smaller size", captures[1][1] < captures[0][1]
           and captures[1][2] < captures[0][2])
    truthy("and what is returned is within budget",
           len(image.data) <= srv.MAX_SCREENSHOT_BYTES)

    # Bytes scale with area, so one square-root step should land near the
    # budget rather than creeping toward it over many captures.
    captures.clear()
    srv._capture = fake_capture(lambda w, h: max(1024, (w * h) // 2))
    srv.fusion_screenshot(view="iso", width=1920, height=1440)
    truthy("shrinking converges in one step, not several", len(captures) <= 2)

    # A viewport that stays oversized however small it gets must not loop.
    captures.clear()
    srv._capture = fake_capture(lambda w, h: 5 * 1024 * 1024)
    image = srv.fusion_screenshot(view="iso", width=1920, height=1440)
    check("retries are bounded", len(captures), srv.SCREENSHOT_SHRINK_ATTEMPTS + 1)
    truthy("and it still returns a picture rather than failing", image.data)
    truthy("never shrinking below the documented minimum",
           all(w >= srv.SCREENSHOT_MIN_SIDE and h >= srv.SCREENSHOT_MIN_SIDE
               for _, w, h in captures))
finally:
    srv._capture = real_capture


# ------------------------------------------------- execute + screenshot -----
# Every modeling step used to cost two tool calls - run the code, then look at
# it - and the expensive half is the extra model turn between them, not the
# 0.6s capture. fusion_execute(screenshot=...) folds the look into the same
# call. The semantics under test: capture only after success, a capture
# problem never masquerades as a code failure, and an invalid view is refused
# before the code runs rather than after.
print("Execute with a screenshot in the same call")

bridge_calls = []


def fake_bridge(answer):
    """Stand in for the add-in, recording what reaches it."""
    def request(method, path, payload=None):
        bridge_calls.append((method, path))
        return dict(answer)
    return request


real_bridge = srv._bridge_request
try:
    # Success: one execute, one capture, both halves in the return.
    captures.clear()
    bridge_calls.clear()
    srv._bridge_request = fake_bridge({"ok": True, "result": 7, "stdout": ""})
    srv._capture = fake_capture(lambda w, h: 100 * 1024)
    out = srv.fusion_execute(code="x=1", screenshot="iso")
    check("the return carries two parts", isinstance(out, list) and len(out), 2)
    check("the first is the execute result", out[0]["ok"], True)
    truthy("the second is the image", isinstance(out[1], srv.Image))
    check("captured once, at the default size", captures, [("iso", 1200, 800)])

    # Without the parameter nothing changes.
    captures.clear()
    out = srv.fusion_execute(code="x=1")
    check("no screenshot means the plain dict, as before", out["ok"], True)
    check("and no capture at all", captures, [])

    # Failed code: the traceback is the story; no picture of it.
    captures.clear()
    srv._bridge_request = fake_bridge({"ok": False, "traceback": "boom"})
    out = srv.fusion_execute(code="x=1", screenshot="iso")
    check("a failing script returns its dict alone", out["ok"], False)
    check("and is not photographed", captures, [])

    # Capture failure after a successful run must not read as a code failure —
    # a tool error here would push the model into re-running code that worked.
    def broken_capture(view, width, height):
        raise RuntimeError("viewport gone")

    srv._bridge_request = fake_bridge({"ok": True, "result": 1, "stdout": ""})
    srv._capture = broken_capture
    out = srv.fusion_execute(code="x=1", screenshot="iso")
    check("the code's success survives a capture failure", out["ok"], True)
    truthy("with the reason attached",
           "viewport gone" in out.get("screenshot_error", ""))

    # An unknown view is refused BEFORE anything runs: rejecting it after
    # would leave the geometry changed under a call that reported failure.
    bridge_calls.clear()
    refuses("an unknown view is refused",
            lambda: srv.fusion_execute(code="x=1", screenshot="back"))
    check("and the code was never sent to the bridge", bridge_calls, [])
finally:
    srv._bridge_request = real_bridge
    srv._capture = real_capture


# The mixed return - a dict and an Image in one list - relies on fastmcp
# turning it into two content blocks. That conversion is fastmcp's own, so it
# is proven through a real in-memory client rather than assumed.
print("The combined return survives the protocol")

import asyncio  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402

from fastmcp import Client  # noqa: E402


async def call_over_protocol(args):
    async with Client(srv.mcp) as client:
        return await client.call_tool("fusion_execute", args)


try:
    srv._bridge_request = fake_bridge({"ok": True, "result": 7, "stdout": ""})
    srv._capture = fake_capture(lambda w, h: 10 * 1024)
    result = asyncio.run(call_over_protocol({"code": "x=1", "screenshot": "iso"}))
    blocks = result.content
    check("two blocks arrive", len(blocks), 2)
    check("the first is text", blocks[0].type, "text")
    check("holding the execute result", json.loads(blocks[0].text)["ok"], True)
    check("the second is a PNG image",
          (blocks[1].type, blocks[1].mimeType), ("image", "image/png"))
    truthy("whose data decodes back to the capture",
           base64.b64decode(blocks[1].data).startswith(b"\x89PNG"))

    result = asyncio.run(call_over_protocol({"code": "x=1"}))
    check("without screenshot, one block as before", len(result.content), 1)
    check("still the JSON result", json.loads(result.content[0].text)["ok"], True)
finally:
    srv._bridge_request = real_bridge
    srv._capture = real_capture


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)
shutil.rmtree(escape_target, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
