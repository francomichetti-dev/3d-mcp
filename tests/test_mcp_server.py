"""The MCP server's own logic - no Fusion, no bridge, no network.

The important one here is _resolve_export_path: it is the only thing stopping a
model-chosen filename writing anywhere on the disk, so it gets the same
treatment as the bridge's auth. _resolve_download_path is the other half of
that job and defends differently — it takes a name rather than a path and
builds the path itself — so what is tested is that nothing a caller writes can
become a directory, and that no existing file is ever overwritten.

    cd agent && uv run --frozen --no-sync python ../tests/test_mcp_server.py
"""

import os
import stat
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


# ------------------------------------------- where saves and downloads go ----
# The Save and Download buttons and the MCP tools share one module, because the
# panel's service and the MCP server are different processes and a copy of this
# logic in each would drift. What is tested here is that nothing a caller writes
# can become a directory, that a finished file is never overwritten, and that
# the save mirror is the one thing that IS overwritten.
print("Save and download destinations")

from arges_mcp import common  # noqa: E402

# Never read or write the operator's real ~/.arges/config.json.
CONFIG_HOME = Path(tempfile.mkdtemp(prefix="fusion-config-test-"))
common.state_dir = lambda: CONFIG_HOME
DEST = Path(tempfile.mkdtemp(prefix="fusion-dest-test-")) / "out"

check("f3d is one of the formats", "f3d" in common.FORMATS, True)
check("and it is what a save writes", common.SAVE_FORMAT, "f3d")
truthy("the export body knows how to write one",
       "createFusionArchiveExportOptions" in common.EXPORT_BODY)

target = common.output_path(DEST, "", "scooter-grip-kids", "stl")
truthy("with no filename, named after the document",
       target.name.startswith("scooter-grip-kids_"))
check("in the folder it was given", target.parent, DEST)
check("with the format's extension", target.suffix, ".stl")
truthy("and the folder is created", DEST.is_dir())

check("a plain name is used as given",
      common.output_path(DEST, "bracket", "x", "stl").name, "bracket.stl")
# People type the extension; slugging it would produce "bracket_stl.stl".
check("a typed extension is not doubled",
      common.output_path(DEST, "bracket.stl", "x", "stl").name, "bracket.stl")
check("and the check is case-insensitive",
      common.output_path(DEST, "Bracket.STL", "x", "stl").name, "Bracket.stl")
check("a different extension stays part of the name",
      common.output_path(DEST, "part.step", "x", "stl").name, "part_step.stl")

# --- the ones that matter: a path offered as a filename must become a filename
for label, attempt in [
    ("parent traversal", "../escaped"),
    ("deep traversal", "../../../../../../tmp/escaped"),
    ("absolute path", "/tmp/escaped"),
    ("absolute etc", "/etc/passwd"),
    ("home expansion", "~/escaped"),
    ("home of another user", "~root/escaped"),
    ("a nested path", "sub/dir/part"),
    ("a windows path", "..\\..\\escaped"),
]:
    got = common.output_path(DEST, attempt, "x", "stl")
    check(f"{label} stays in the folder: {attempt}", got.parent, DEST)
    truthy(f"{label} carries no separator: {attempt}",
           not any(c in got.stem for c in "/\\") and ".." not in got.name)

# --- never overwrite a file somebody asked for
first = common.output_path(DEST, "part", "x", "stl")
first.write_text("existing")
check("an existing file is not chosen again",
      common.output_path(DEST, "part", "x", "stl").name, "part-1.stl")
(DEST / "part-1.stl").write_text("also existing")
check("and it keeps counting",
      common.output_path(DEST, "part", "x", "stl").name, "part-2.stl")
check("the existing file is left alone", first.read_text(), "existing")

# A symlink is worse than a file: writing "through" it lands the export outside
# the folder entirely, and a dangling one does not answer exists().
os.symlink(str(Path(tempfile.gettempdir()) / "fusion-no-such-target"),
           DEST / "linked.stl")
check("a dangling symlink is not written through",
      common.output_path(DEST, "linked", "x", "stl").name, "linked-1.stl")

# --- the save mirror: one file per document, and never somebody else's file
#
# The mirror used to be "<folder>/<document name>.f3d, overwritten". On
# 2026-10-08 that destroyed a real file: the person had downloaded a motor as
# Untitled.f3d into Downloads (the default save folder), asked for it to be
# imported, and the first successful call saved the open, empty "Untitled"
# design over it. Every unsaved Fusion document is called Untitled, so the
# name alone could never have been safe. Now the mirror only ever replaces a
# file this tool wrote, for the same document, unchanged since it wrote it.
print("The save mirror never overwrites somebody else's file")

MIRROR = Path(tempfile.mkdtemp(prefix="fusion-mirror-test-"))
theirs = MIRROR / "Untitled.f3d"
theirs.write_bytes(b"the motor: 200 KB of somebody's real work")

a = common.mirror_path(MIRROR, "Untitled", "doc:a")
check("a file the person put there is never the target", a.name, "Untitled-1.f3d")
a.write_bytes(b"state of A")
common.record_mirror(a, "doc:a")
check("the same document keeps writing to its own file",
      common.mirror_path(MIRROR, "Untitled", "doc:a"), a)

b = common.mirror_path(MIRROR, "Untitled", "doc:b")
check("another document with the same name gets a file of its own", b.name, "Untitled-2.f3d")
b.write_bytes(b"state of B")
common.record_mirror(b, "doc:b")
check("and the person's file is untouched throughout",
      theirs.read_bytes(), b"the motor: 200 KB of somebody's real work")

a.write_bytes(b"edited by hand since the tool wrote it")
moved = common.mirror_path(MIRROR, "Untitled", "doc:a")
check("a mirror somebody changed is no longer the tool's to replace", moved == a, False)
check("so the state goes to a free name instead", moved.name, "Untitled-3.f3d")
moved.write_bytes(b"x")
common.record_mirror(moved, "doc:a")
moved.unlink()
check("a mirror the person deleted is simply written again",
      common.mirror_path(MIRROR, "Untitled", "doc:a"), moved)

(MIRROR / "tower.f3d").write_text("theirs")
check("with no document identity at all, a foreign file is still never the target",
      common.mirror_path(MIRROR, "tower", None).name, "tower-1.f3d")

# Review finding (Codex, 2026-10-08): with no identity, "" became a shared
# owner, so a second unidentified document overwrote the first one's mirror.
lone = common.mirror_path(MIRROR, "lonely", None)
lone.write_bytes(b"first unidentified design")
common.record_mirror(lone, None)
check("without an identity, a save never replaces an earlier file",
      common.mirror_path(MIRROR, "lonely", None) == lone, False)

# Review finding: two processes could both pick the same unwritten path
# before either exported, and the second export overwrote the first. A path
# is now reserved the moment it is picked.
r1 = common.mirror_path(MIRROR, "racer", "doc:r1")
r2 = common.mirror_path(MIRROR, "racer", "doc:r2")
check("a path picked for one document is reserved before anything is written", r1 == r2, False)
check("and the same document asking again gets its own reservation",
      common.mirror_path(MIRROR, "racer", "doc:r1"), r1)
common.release_mirror(r2, "doc:r2")
check("a reservation whose save failed is released, not left as an empty file", r2.exists(), False)
r1.write_bytes(b"saved")
common.record_mirror(r1, "doc:r1")
common.release_mirror(r1, "doc:r1")
check("but a real save is never released", r1.read_bytes(), b"saved")
check("an unnamed document still gets a file", common.mirror_path(MIRROR, "", "doc:z").name,
      "design.f3d")
check("the mirror is a Fusion archive", a.suffix, ".f3d")

ledger = common.state_dir() / common.LEDGER_NAME
check("the ledger of what the tool wrote is private",
      stat.S_IMODE(ledger.stat().st_mode), 0o600)
ledger.write_text("{ corrupt")
check("a corrupt ledger makes nothing look like the tool's",
      common.mirror_path(MIRROR, "Untitled", "doc:b") == b, False)

ids = common.IDENTITY_BODY
truthy("the identity snippet reads the same attribute the add-in stamps",
       "attributes.itemByName" in ids and "dataFile" in ids)

# Review finding: identity and export are two bridge calls, and a tab switch
# between them exported the new tab into the old tab's mirror. The export now
# checks, on Fusion's main thread, that the document is still the one the
# path was chosen for.
from types import SimpleNamespace as _NS  # noqa: E402

_exported = []
_other = _NS(attributes=_NS(itemByName=lambda g, n: _NS(value="doc:B")),
             rootComponent=_NS(), exportManager=_NS(execute=lambda o: _exported.append(o)))
_ns = {"adsk": _NS(fusion=_NS(Design=_NS(cast=lambda p: p))), "ui": _NS(), "design": _other,
       "app": _NS(activeProduct=_other, activeDocument=_NS(dataFile=None))}
exec(compile(common.snippet(common.EXPORT_BODY, {"format": "f3d", "name": "", "path": "/x/a.f3d",
                                                 "expect_key": "doc:A"}), "<t>", "exec"), _ns, _ns)
check("a save aimed at one document refuses to write another", _ns["result"]["ok"], False)
truthy("saying the document changed", "changed" in _ns["result"]["error"])
check("and nothing was exported", _exported, [])

# ------------------------------------------------------------------ config ----
print("Stored configuration")

check("with no file at all, the format is stl", common.load_config()["format"], "stl")
check("and files go to Downloads", common.save_dir_from().name, "Downloads")

os.environ["ARGES_SAVE_DIR"] = str(DEST / "via-env")
check("an operator can set the folder at launch",
      common.save_dir_from(), DEST / "via-env")
os.environ.pop("ARGES_SAVE_DIR", None)

stored = common.write_config(save_dir=str(DEST), fmt="step")
check("a stored format is read back", common.format_from(), "step")
check("and a stored folder wins", common.save_dir_from(), DEST)
check("the write reports what now holds", stored["format"], "step")
truthy("and the file is where the state dir is",
       common.config_path().parent == CONFIG_HOME)

refuses("a format the exporter cannot produce is refused",
        lambda: common.write_config(fmt="obj"))
check("and the stored one is untouched", common.format_from(), "step")
refuses("a folder that cannot be created is refused",
        lambda: common.write_config(save_dir="/System/arges-should-not-exist"))
check("blanking the folder goes back to the default",
      common.write_config(save_dir="")["save_dir"], "")
check("which is Downloads again", common.save_dir_from().name, "Downloads")

# A config file is not worth failing a save over.
common.config_path().write_text("{ not json", encoding="utf-8")
check("a corrupt config reads as no preference", common.load_config()["format"], "stl")
common.write_config(save_dir=str(DEST), fmt="stl")


# -------------------------------------------------------------- limits ----
print("Declared limits")
check("views", set(srv.VIEWS), {"front", "top", "right", "iso", "fit"})
check("formats", set(srv.FORMATS), {"stl", "step", "3mf", "usd", "f3d"})
# The tools and the panel must offer the same list, or the panel's format menu
# grows an option the exporter cannot produce.
check("and the tools use the shared list", srv.FORMATS, common.FORMATS)
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
# This section is about folding the look into the call; autosave has its own
# below. Left on, its extra bridge call would ride along in every assertion
# here and a failure would point at the wrong feature.
os.environ["ARGES_AUTOSAVE"] = "0"
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
    os.environ.pop("ARGES_AUTOSAVE", None)


# ------------------------------------------------------------- autosave -----
# Saving after every change is the point, and it does NOT go through Fusion's
# own save: on an expired subscription Document.save() returns True and saves
# nothing, verified live. So a save is an export of the whole design to a local
# .f3d, and what is tested here is the wiring — that it happens after a success,
# never after a failure, that it targets the configured folder, and above all
# that a save which fails cannot make a change that worked look failed.
print("Autosave")

os.environ.pop("ARGES_AUTOSAVE", None)
truthy("on by default — losing work is the failure that matters", srv._autosave_enabled())
for value in ("0", "false", "no", "off", "OFF", "False"):
    os.environ["ARGES_AUTOSAVE"] = value
    check(f"{value!r} turns it off", srv._autosave_enabled(), False)
for value in ("1", "true", "yes", "on", ""):
    os.environ["ARGES_AUTOSAVE"] = value
    check(f"{value!r} leaves it on", srv._autosave_enabled(), True)
# An operator typo must not silently stop saving.
os.environ["ARGES_AUTOSAVE"] = "flase"
check("an unrecognised value stays on", srv._autosave_enabled(), True)
os.environ.pop("ARGES_AUTOSAVE", None)
os.environ["FUSION_AUTOSAVE"] = "0"
check("the pre-rename variable is honoured", srv._autosave_enabled(), False)
os.environ.pop("FUSION_AUTOSAVE", None)

bridge_log = []


def fake_state_bridge(document="tower", execute=None):
    """Answer /health and the identity snippet with a document, and /execute
    with a canned result."""
    def request(method, path, payload=None):
        bridge_log.append((path, payload))
        if path == "/health":
            return {"ok": True, "document": document, "bridge_version": "1"}
        if "_fx_identity" in (payload or {}).get("code", ""):
            return {"ok": True, "stdout": "",
                    "result": {"name": document or "", "key": "doc:tower"}}
        if isinstance(execute, Exception):
            raise execute
        return execute or {"ok": True, "result": {
            "ok": True, "format": "f3d", "path": "/x/tower.f3d", "bytes": 2956382,
            "target": "whole design (root component)"}, "stdout": ""}
    return request


real_bridge = srv._bridge_request
try:
    bridge_log.clear()
    srv._bridge_request = fake_state_bridge()
    out = srv._write_state_file()
    check("a state file reports itself saved", out["saved"], True)
    check("and reports where it went", out["path"], "/x/tower.f3d")

    code = [p for path, p in bridge_log if path == "/execute"][-1]["code"]
    truthy("it ran the shared export body", "_fx_export" in code)
    truthy("as a Fusion archive", '\\"format\\": \\"f3d\\"' in code)
    truthy("of the whole design, not one body", '\\"name\\": \\"\\"' in code)
    truthy("into the configured folder", str(DEST) in code)
    truthy("named after the open document", "tower" in code)

    # The document's name AND identity are asked for first: the name alone is
    # "Untitled" for every unsaved document, which is how one overwrote a file.
    first = [p for path, p in bridge_log if path == "/execute"][0]["code"]
    truthy("the document's identity is asked for first", "_fx_identity" in first)
    truthy("and the export is told which document it must be",
           "doc:tower" in [p for path, p in bridge_log if path == "/execute"][-1]["code"])

    # Nothing open: it must still write something rather than raise.
    bridge_log.clear()
    srv._bridge_request = fake_state_bridge(document=None)
    srv._write_state_file()
    code = [p for path, p in bridge_log if path == "/execute"][-1]["code"]
    truthy("with no document name it falls back to a fixed one", "design" in code)

    # The incident itself: a file of the same name already in the folder.
    (DEST / "tower.f3d").write_bytes(b"somebody else's tower")
    bridge_log.clear()
    srv._bridge_request = fake_state_bridge(document="tower")
    srv._write_state_file()
    code = [p for path, p in bridge_log if path == "/execute"][-1]["code"]
    check("an autosave never targets a file it did not write",
          "tower.f3d" in code.replace("tower-", "TOWER-"), False)
    check("which is still there, unchanged",
          (DEST / "tower.f3d").read_bytes(), b"somebody else's tower")
finally:
    srv._bridge_request = real_bridge

snippets = []


def record_snippet(answer):
    def run(body, params):
        snippets.append((body, params))
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)
    return run


real_snippet = srv._run_snippet
try:
    # Rides along in the result, the way a screenshot does.
    snippets.clear()
    srv._bridge_request = fake_state_bridge()
    srv._run_snippet = record_snippet({"ok": True, "path": "/x/tower.f3d", "bytes": 9})
    out = {"ok": True, "result": 1}
    srv._autosave(out)
    check("a save attaches where it went", out["autosave"]["path"], "/x/tower.f3d")
    check("the document's identity is read, then the export body runs",
          [body for body, _ in snippets], [common.IDENTITY_BODY, srv._EXPORT_BODY])

    # The one that matters: a broken save must not break a change that worked.
    snippets.clear()
    srv._run_snippet = record_snippet(RuntimeError("disk full"))
    out = {"ok": True, "result": 1}
    srv._autosave(out)                      # must not raise
    check("the change still reads as successful", out["ok"], True)
    check("the save reports its own failure", out["autosave"]["ok"], False)
    truthy("naming the cause", "disk full" in out["autosave"]["error"])
    truthy("and saying the change survived",
           "succeeded" in out["autosave"]["error"])

    # Off means off: nothing sent, not a call that declines to save.
    snippets.clear()
    os.environ["ARGES_AUTOSAVE"] = "0"
    out = {"ok": True, "result": 1}
    srv._autosave(out)
    check("disabled means nothing is sent", snippets, [])
    check("and nothing is reported", "autosave" in out, False)
    os.environ.pop("ARGES_AUTOSAVE", None)
finally:
    srv._run_snippet = real_snippet
    srv._bridge_request = real_bridge

# Wired into fusion_execute: after success, never after failure.
executed = []


def fake_execute_bridge(answer):
    def request(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1"}
        code = (payload or {}).get("code", "")
        executed.append("save" if "_fx_export" in code
                        else "identity" if "_fx_identity" in code else "user")
        return dict(answer)
    return request


try:
    executed.clear()
    srv._bridge_request = fake_execute_bridge({"ok": True, "result": 1, "stdout": ""})
    out = srv.fusion_execute(code="x=1")
    check("a successful change is saved", executed, ["user", "identity", "save"])
    truthy("and the caller is told", "autosave" in out)

    # A failed script may have left the design half-changed; the traceback is
    # what the caller needs, and saving that state is not obviously right.
    executed.clear()
    srv._bridge_request = fake_execute_bridge({"ok": False, "traceback": "boom"})
    srv.fusion_execute(code="x=1")
    check("a failed script is not saved", executed, ["user"])
finally:
    srv._bridge_request = real_bridge


# ------------------------------------------------------ download and save ----
print("The download and save tools")

try:
    calls = []

    def capture(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1"}
        calls.append((payload or {}).get("code", ""))
        return {"ok": True, "result": {"ok": True, "path": "/x/f", "bytes": 1},
                "stdout": ""}

    srv._bridge_request = capture
    common.write_config(save_dir=str(DEST), fmt="step")

    calls.clear()
    srv.fusion_download()
    truthy("a bare download uses the configured format",
           '\\"format\\": \\"step\\"' in calls[0])
    truthy("and the configured folder", str(DEST) in calls[0])
    truthy("naming the file after the document", "tower" in calls[0])

    calls.clear()
    srv.fusion_download(format="3mf", body_or_component="half_pos", filename="grip")
    truthy("an explicit format overrides it", '\\"3mf\\"' in calls[0])
    truthy("the body is passed through", "half_pos" in calls[0])
    truthy("and the filename is honoured", "grip.3mf" in calls[0])

    refuses("a format the exporter cannot produce is refused",
            lambda: srv.fusion_download(format="obj"))
    check("and nothing was sent for it", len(calls), 1)

    calls.clear()
    saved = srv.fusion_save()
    check("fusion_save reports a save", saved["saved"], True)
    truthy("as an archive", '\\"f3d\\"' in calls[-1])
finally:
    srv._bridge_request = real_bridge


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


os.environ["ARGES_AUTOSAVE"] = "0"          # proving the protocol, not the save
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
    os.environ.pop("ARGES_AUTOSAVE", None)


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)
shutil.rmtree(escape_target, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
