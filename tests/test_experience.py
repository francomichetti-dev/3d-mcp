"""The experience log: what the tool remembers between sessions.

Nothing here needs Fusion. The log has to be useful and harmless at the same
time. Useful means a recurring error gets the same signature however its
numbers vary, and a helper retyped under a new name is still recognised.
Harmless means a log that cannot be written never turns a call that worked
into one that failed.

    cd server && uv run --frozen --no-sync python ../tests/test_experience.py
"""

import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

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


from arges_mcp import common  # noqa: E402

# Never read or write the operator's real state directory: every path the log
# uses is resolved through common.state_dir() at call time, so this one
# assignment sandboxes the whole suite, the server wiring included.
SANDBOX = Path(tempfile.mkdtemp(prefix="arges-experience-test-"))
common.state_dir = lambda: SANDBOX / "state"

from arges_mcp import experience as xp  # noqa: E402


# ---------------------------------------------------------- signatures ----
# A signature is what lets "this error again" be recognised. It has to ignore
# what varies between occurrences (counts, ids, addresses) and keep what
# identifies the mistake (the exception type, the message, the name involved).
print("Error signatures")

FUSION_TB = """Traceback (most recent call last):
  File "<fusion_execute>", line 12, in <module>
    ext = feats.extrudeFeatures.add(ei)
  File "/Applications/Autodesk Fusion.app/.../fusion.py", line 30211, in add
    return _fusion.ExtrudeFeatures_add(self, input)
RuntimeError: 3 : invalid argument profile

"""
check("the last exception line, numbers generalised",
      xp.error_signature(FUSION_TB), "RuntimeError: N : invalid argument profile")
check("the same mistake with another number is the same signature",
      xp.error_signature("RuntimeError: 5 : invalid argument profile"),
      xp.error_signature(FUSION_TB))
check("a different exception is a different signature",
      xp.error_signature("ValueError: 3 : invalid argument profile")
      == xp.error_signature(FUSION_TB), False)
check("an address does not split one error into many",
      xp.error_signature("TypeError: <adsk.core.Point3D at 0x10f3a2b50> bad"),
      xp.error_signature("TypeError: <adsk.core.Point3D at 0x7ffee1c0> bad"))
check("a name with digits in it survives intact",
      xp.error_signature("NameError: name 'sk2' is not defined"),
      "NameError: name 'sk2' is not defined")
check("a decimal is one placeholder, not two",
      xp.error_signature("ValueError: radius 0.15 too small"),
      "ValueError: radius N too small")
check("an error that never ran keeps its message",
      xp.error_signature("no active Fusion design — open or create one"),
      "no active Fusion design — open or create one")
check("runs of whitespace collapse",
      xp.error_signature("KeyError:   'wall'  "), "KeyError: 'wall'")
check("nothing in, nothing out", xp.error_signature(""), "")
check("None is nothing too", xp.error_signature(None), "")
truthy("a huge message is capped",
       len(xp.error_signature("RuntimeError: " + "x" * 5000)) <= xp.MAX_SIGNATURE_CHARS)


# ------------------------------------------------------- fingerprints ----
# Recognising a helper that keeps being retyped is what eventually turns it
# into a fast command, and nobody retypes a helper identically: the name
# changes, the numbers change, a docstring comes and goes. The shape does not.
print("Helper fingerprints")

SETP = '''
def setp(n, e, u="mm", c=""):
    p = up.itemByName(n)
    if p: p.expression = e
    else: p = up.add(n, VI(e), u, c)
    return (n, p.expression, round(p.value*10, 3))
'''
RENAMED = '''
def set_param(name, expr, unit="in", comment="wall"):
    """Create or update a user parameter."""
    p = up.itemByName(name)
    if p: p.expression = expr
    else: p = up.add(name, VI(expr), unit, comment)
    return (name, p.expression, round(p.value*25.4, 1))
'''
DIFFERENT = '''
def setp(n, e, u="mm", c=""):
    p = up.itemByName(n)
    if p: p.deleteMe()
    return None
'''

prints = xp.function_fingerprints(SETP)
check("a top-level helper is fingerprinted under its name", list(prints), ["setp"])
check("renamed, re-documented and re-numbered, it is the same helper",
      xp.function_fingerprints(RENAMED)["set_param"], prints["setp"])
check("a different body is a different helper",
      xp.function_fingerprints(DIFFERENT)["setp"] == prints["setp"], False)
check("a one-liner is not worth remembering",
      xp.function_fingerprints("def mm(v):\n    return v / 10\n"), {})
check("code that does not parse fingerprints to nothing, without raising",
      xp.function_fingerprints("def broken(:\n"), {})
check("plain statements define no helpers",
      xp.function_fingerprints("x = 1\ny = 2\nresult = x + y\n"), {})
NESTED = ("def outer():\n    x = 1\n    y = 2\n"
          + textwrap.indent(SETP, "    ") + "    return setp\n")
check("only top-level helpers count", list(xp.function_fingerprints(NESTED)), ["outer"])


# ------------------------------------------------------------- records ----
# One line per call. The outcome shapes are the bridge's three envelopes: it
# ran and worked, it ran and raised, or it never ran at all.
print("Call records")

ok = xp.call_record("fusion_execute", SETP + "result = 1",
                    {"ok": True, "result": 1, "stdout": ""}, 0.4321, view="iso")
check("a success is ok", ok["ok"], True)
check("and it ran", ok["ran"], True)
check("with no signature", ok["sig"], "")
check("timed in milliseconds", ok["ms"], 432)
check("with the view it was checked from", ok["view"], "iso")
check("the helpers it defined", list(ok["defs"]), ["setp"])
check("the tool that made it", ok["tool"], "fusion_execute")
truthy("a timestamp", ok["ts"])
truthy("and the session it belongs to", ok["session"])
check("one session per server process",
      xp.call_record("fusion_execute", "x", {"ok": True}, 0)["session"], ok["session"])

raised = xp.call_record("fusion_execute", "boom()",
                        {"ok": False, "traceback": FUSION_TB, "stdout": ""}, 0.1)
check("a raise is not ok", raised["ok"], False)
check("but it ran", raised["ran"], True)
check("and carries its signature", raised["sig"],
      "RuntimeError: N : invalid argument profile")

never = xp.call_record("fusion_execute", "x = 1",
                       {"ok": False, "error": "no active Fusion design"}, 0.01)
check("an error with no traceback never ran", never["ran"], False)
check("and is signed by its message", never["sig"], "no active Fusion design")

long_code = "x = 1\n" * 5000
truthy("the code is kept as an excerpt, not whole",
       len(xp.call_record("fusion_execute", long_code, {"ok": True}, 0)["code"])
       <= xp.CODE_EXCERPT_CHARS)
check("but its real size is recorded",
      xp.call_record("fusion_execute", long_code, {"ok": True}, 0)["chars"], len(long_code))


# ----------------------------------------------------------------- log ----
print("The log file")

log_path = SANDBOX / "logs" / "experience.jsonl"
log = xp.ExperienceLog(log_path)
check("an empty log reads as nothing", log.read(), [])
check("appending reports success", log.append({"n": 1}), True)
log.append({"n": 2})
check("records come back in order", [r["n"] for r in log.read()], [1, 2])
check("the file is private: it holds design code",
      stat.S_IMODE(log_path.stat().st_mode), 0o600)
check("and so is the directory it created",
      stat.S_IMODE(log_path.parent.stat().st_mode), 0o700)

with open(log_path, "a", encoding="utf-8") as fh:
    fh.write("{not json\n")
log.append({"n": 3})
check("a corrupt line is skipped, not fatal", [r["n"] for r in log.read()], [1, 2, 3])

log.append({"n": 4, "thing": object()})
check("a value JSON cannot encode is stored as text, not dropped",
      log.read()[-1]["n"], 4)

small = xp.ExperienceLog(SANDBOX / "small" / "experience.jsonl", max_bytes=200)
for n in range(20):
    small.append({"n": n, "pad": "x" * 30})
truthy("a full log rotates rather than growing forever",
       Path(str(small.path) + ".1").exists())
truthy("the live file stays near its cap", small.path.stat().st_size <= 200 + 100)
seen = [r["n"] for r in small.read()]
check("reading spans the rotation, oldest first", seen, sorted(seen))
check("and ends with the newest", seen[-1], 19)

# Both writers used to see the full file, then the second rotation replaced
# the history with the first writer's fresh line. Events force that ordering;
# with a lock, the second writer instead stops at flock until the first has
# rotated and appended. The timeouts only keep a broken test from hanging.
if fcntl is not None:
    shared = xp.ExperienceLog(SANDBOX / "shared" / "experience.jsonl", max_bytes=100)
    shared.append({"n": 0, "pad": "x" * 100})
    reached = threading.Event()
    results = []
    real_stat, real_flock = Path.stat, fcntl.flock

    def append_other():
        try:
            results.append(shared.append({"n": 1}))
        finally:
            reached.set()

    other = threading.Thread(target=append_other)

    def contended_flock(fd, operation):
        if threading.current_thread() is other and operation == fcntl.LOCK_EX:
            try:
                return real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                reached.set()
        return real_flock(fd, operation)

    def stat_then_append(self, *args, **kwargs):
        snapshot = real_stat(self, *args, **kwargs)
        if self == shared.path and threading.current_thread() is not other:
            other.start()
            truthy("the other writer finishes or reaches the held lock",
                   reached.wait(5))
        return snapshot

    Path.stat, fcntl.flock = stat_then_append, contended_flock
    try:
        check("the rotating writer succeeds", shared.append({"n": 2}), True)
        other.join(5)
        check("the other writer finishes after the lock is released", other.is_alive(), False)
    finally:
        Path.stat, fcntl.flock = real_stat, real_flock
    check("the other writer succeeds too", results, [True])
    check("two writers cannot rotate away the full previous log",
          [r["n"] for r in shared.read()], [0, 2, 1])

blocked = SANDBOX / "blocked"
blocked.mkdir()
(blocked / "experience.jsonl").mkdir()          # a directory where the file should be
check("a log that cannot be written says so instead of raising",
      xp.ExperienceLog(blocked / "experience.jsonl").append({"n": 1}), False)


# ------------------------------------------------------------- switch ----
# Same contract as ARGES_AUTOSAVE: on unless clearly turned off, read per call.
print("Turning it off")

os.environ.pop("ARGES_EXPERIENCE", None)
truthy("on by default", xp.enabled())
for value in ("0", "false", "no", "off", "OFF"):
    os.environ["ARGES_EXPERIENCE"] = value
    check(f"{value!r} turns it off", xp.enabled(), False)
os.environ["ARGES_EXPERIENCE"] = "of"
check("a typo leaves it on", xp.enabled(), True)
os.environ.pop("ARGES_EXPERIENCE", None)
os.environ["FUSION_EXPERIENCE"] = "0"
check("the pre-rename spelling is honoured", xp.enabled(), False)
os.environ.pop("FUSION_EXPERIENCE", None)

check("the default log lives in the state directory",
      xp.default_log().path, SANDBOX / "state" / xp.LOG_NAME)
os.environ["ARGES_EXPERIENCE"] = "0"
check("off means nothing is written", xp.record_call({"n": 1}), False)
check("and nothing is there", xp.default_log().read(), [])
os.environ.pop("ARGES_EXPERIENCE", None)
check("on means it is", xp.record_call({"n": 1}), True)
check("and it reads back", xp.default_log().read(), [{"n": 1}])


# ------------------------------------------------------------- wiring ----
# The log only teaches anything if every call reaches it, whatever the outcome,
# and it is only harmless if a log that cannot be written changes nothing the
# caller sees.
print("Wired into fusion_execute")

from fastmcp.exceptions import ToolError  # noqa: E402

from arges_mcp import server as srv  # noqa: E402

os.environ["ARGES_AUTOSAVE"] = "0"
real_bridge = srv._bridge_request
real_capture = srv._capture


def bridge_answering(answer):
    def request(method, path, payload=None):
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)
    return request


def fresh_log():
    path = xp.default_log().path
    for p in (path, Path(str(path) + ".1")):
        if p.exists():
            p.unlink()


def last_record():
    records = xp.default_log().read()
    return records[-1] if records else None


try:
    fresh_log()
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    out = srv.fusion_execute(code=SETP + "result = 1")
    rec = last_record()
    check("a success is recorded", (rec or {}).get("ok"), True)
    check("as fusion_execute", (rec or {}).get("tool"), "fusion_execute")
    check("with the helpers it defined", list((rec or {}).get("defs", {})), ["setp"])
    check("and the caller's result is untouched", out["result"], 1)

    srv._bridge_request = bridge_answering({"ok": False, "traceback": FUSION_TB, "stdout": ""})
    srv.fusion_execute(code="boom()")
    check("a failure is recorded with its signature", (last_record() or {}).get("sig"),
          "RuntimeError: N : invalid argument profile")

    # The bridge being down is experience too: it is how the report can say
    # "a third of today's calls never reached Fusion".
    srv._bridge_request = bridge_answering(ToolError(srv.MSG_BRIDGE_DOWN))
    try:
        srv.fusion_execute(code="x = 1")
        raised = False
    except ToolError:
        raised = True
    check("a transport failure still raises", raised, True)
    rec = last_record() or {}
    check("and is recorded as one", (rec.get("ok"), rec.get("kind")), (False, "tool_error"))
    truthy("with what went wrong", "Fusion not running" in rec.get("sig", ""))

    # A screenshot view is part of the record: the report measures how often
    # steps were checked by eye.
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    srv._capture = lambda view, w, h: b"\x89PNG" + b"0" * 100
    srv.fusion_execute(code="x = 1", screenshot="top")
    check("the view it was checked from", (last_record() or {}).get("view"), "top")
    srv._capture = real_capture

    # Autosave cost is the open question in FOLLOWUP.md; the log answers it.
    os.environ.pop("ARGES_AUTOSAVE", None)
    real_autosave = srv._autosave
    srv._autosave = lambda result: result.__setitem__("autosave", {"ok": True})
    srv.fusion_execute(code="x = 1")
    truthy("the autosave's cost is measured", "autosave_ms" in (last_record() or {}))
    srv._autosave = real_autosave
    os.environ["ARGES_AUTOSAVE"] = "0"

    count = len(xp.default_log().read())
    os.environ["ARGES_EXPERIENCE"] = "0"
    srv.fusion_execute(code="x = 1")
    check("switched off, nothing is recorded", len(xp.default_log().read()), count)
    os.environ.pop("ARGES_EXPERIENCE", None)

    # The one that matters: a log that cannot be written changes nothing.
    real_state_dir = common.state_dir
    common.state_dir = lambda: blocked    # its experience.jsonl is a directory
    out = srv.fusion_execute(code="x = 1")
    check("an unwritable log does not fail the call", out["ok"], True)
    common.state_dir = real_state_dir
finally:
    srv._bridge_request = real_bridge
    srv._capture = real_capture
    os.environ.pop("ARGES_AUTOSAVE", None)
    os.environ.pop("ARGES_EXPERIENCE", None)


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
