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


# ----------------------------------------------------------- repetition ----
print("Repeated helpers")


def helper_record(name="setp", shape=prints["setp"], *, ok=True,
                  tool="fusion_execute", session="earlier"):
    return {"tool": tool, "ok": ok, "session": session, "defs": {name: shape}}


history = [helper_record(), helper_record("set_param", session="another"),
           helper_record("set_param")]
repeat = getattr(xp, "repeated_helpers", None)
truthy("the repetition query is available", callable(repeat))
if callable(repeat):
    check("three earlier successes qualify, including the current call in the total",
          repeat(history, prints), {"setp": {"count": 4, "names": ["set_param"]}})
    check("two earlier successes are too few", repeat(history[:2], prints), {})
    check("the threshold can be changed", repeat(history[:2], prints, threshold=2),
          {"setp": {"count": 3, "names": ["set_param"]}})
    check("no definitions means no repetition", repeat(history, {}), {})
    check("the same name with a different shape is new",
          repeat(history, xp.function_fingerprints(DIFFERENT)), {})
    ignored = [helper_record(ok=False), helper_record(tool="fusion_run"),
               helper_record(tool="fusion_import"), {"defs": prints},
               {"tool": "fusion_execute", "ok": True, "defs": None}]
    check("failed calls and other tools cannot reach the threshold",
          repeat(history[:2] + ignored, prints), {})
    aliases = helper_record()
    aliases["defs"]["set_param"] = prints["setp"]
    check("two aliases in one earlier record count as one call",
          repeat([aliases, aliases], prints), {})
    check("other names are unique, sorted, and exclude the current name",
          repeat(history + [helper_record("z"), helper_record("a")], prints),
          {"setp": {"count": 6, "names": ["a", "set_param", "z"]}})
    before = json.dumps(history)
    repeat(history, prints)
    check("querying does not change the history", json.dumps(history), before)


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


# A fresh cache stands in for a new server process; every seeded record goes
# through the real log, so the first call also tests when it is read.
print("Repetition hints in fusion_execute")


def fresh_helpers(records=()):
    fresh_log()
    srv._helper_counts = None
    srv._hinted_helpers = set()
    for record in records:
        xp.default_log().append(record)


real_read = xp.ExperienceLog.read
reads = []


def counted_read(self):
    reads.append(self.path)
    return real_read(self)


def broken_read(self):
    raise RuntimeError("history unavailable")


try:
    os.environ["ARGES_AUTOSAVE"] = "0"
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    xp.ExperienceLog.read = counted_read
    fresh_helpers(history)
    out = srv.fusion_execute(code="result = 1")
    check("a call with no helper has no hint", out.get("hint"), None)
    check("and does not load the history", len(reads), 0)
    srv._bridge_request = bridge_answering({"ok": False, "traceback": FUSION_TB})
    out = srv.fusion_execute(code=SETP)
    check("a failed helper has no hint", out.get("hint"), None)
    check("a failure does not load the history either", len(reads), 0)
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    out = srv.fusion_execute(code=SETP)
    check("the fourth successful definition suggests saving it", out.get("hint"),
          "You have now written `setp` 4 times across sessions (also as `set_param`). "
          "Save it with fusion_command_save so it is one fusion_run call from now on.")
    check("history is loaded once", len(reads), 1)
    out = srv.fusion_execute(code=RENAMED)
    check("renaming cannot repeat the same hint", out.get("hint"), None)
    check("another helper call does not reread the log", len(reads), 1)
    xp.ExperienceLog.read = real_read
    check("the successful current call is still logged just once",
          sum(r.get("ok") and r.get("defs") == prints
              for r in xp.default_log().read()), 2)

    fresh_helpers(history[:2])
    check("the third definition does not count the current call twice",
          srv.fusion_execute(code=SETP).get("hint"), None)
    truthy("the next call uses the count updated in memory",
           "`setp` 4 times" in srv.fusion_execute(code=SETP).get("hint", ""))

    fresh_helpers()
    for i in range(3):
        check(f"a fresh process's definition {i + 1} has no hint",
              srv.fusion_execute(code=SETP).get("hint"), None)
    truthy("repetition within this process counts too",
           "`setp` 4 times" in srv.fusion_execute(code=SETP).get("hint", ""))

    fresh_helpers(history)
    concurrent = []
    ready = threading.Barrier(4)

    def repeat_together():
        try:
            ready.wait(5)
            concurrent.append(srv.fusion_execute(code=SETP))
        except Exception as exc:
            concurrent.append({"ok": False, "error": str(exc)})

    callers = [threading.Thread(target=repeat_together) for _ in range(4)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(5)
    check("concurrent successful calls all return",
          sum(bool(out.get("ok")) for out in concurrent), 4)
    check("concurrent calls cannot show the same hint twice",
          sum(bool(out.get("hint")) for out in concurrent), 1)

    fresh_helpers(history[:2])
    srv._bridge_request = bridge_answering({"ok": False, "traceback": FUSION_TB})
    srv.fusion_execute(code=SETP)
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    check("a failure in this process cannot advance the count",
          srv.fusion_execute(code=SETP).get("hint"), None)
    srv._bridge_request = bridge_answering({"ok": False, "traceback": FUSION_TB})
    check("a failure with loaded counts still has no hint",
          srv.fusion_execute(code=SETP).get("hint"), None)
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    srv._run_and_report("fusion_run", SETP, record_code=SETP,
                        screenshot=None, autosave=False)
    out = srv._run_and_report("fusion_import", SETP, record_code=SETP,
                              screenshot=None, autosave=False)
    check("an import never gets a hint", out.get("hint"), None)
    truthy("run and import records cannot advance the cached count",
           "`setp` 4 times" in srv.fusion_execute(code=SETP).get("hint", ""))

    fresh_helpers(history)
    out = srv._run_and_report("fusion_run", SETP, record_code=SETP,
                              screenshot=None, autosave=False)
    check("a run never gets a hint, even with qualifying history", out.get("hint"), None)
    srv._capture = lambda view, w, h: b"\x89PNG" + b"0" * 100
    out = srv.fusion_execute(code=SETP, screenshot="top")
    truthy("the screenshot's JSON block carries the hint", out[0].get("hint"))
    srv._capture = real_capture

    fresh_helpers(history)
    out = srv.fusion_execute(code=SETP + RENAMED)
    check("two names for one shape produce just one suggestion",
          out.get("hint", "").count("fusion_command_save"), 1)
    check("neither alias is nagged on the next call",
          srv.fusion_execute(code=SETP + RENAMED).get("hint"), None)

    other_shape = xp.function_fingerprints(DIFFERENT)["setp"]
    fresh_helpers(history + [helper_record("remove_param", other_shape)] * 3)
    out = srv.fusion_execute(code=SETP + DIFFERENT.replace("def setp(", "def remove_param("))
    truthy("each repeated shape can be suggested", all(
        name in out.get("hint", "") for name in ("`setp`", "`remove_param`")))
    check("the hint stays on one line", len(out.get("hint", "").splitlines()), 1)

    fresh_helpers(history + [helper_record("remove_param", other_shape)] * 3)
    reads.clear()
    xp.ExperienceLog.read = counted_read
    os.environ["ARGES_EXPERIENCE"] = "0"
    out = srv.fusion_execute(code=SETP)
    check("logging off means no hint", out.get("hint"), None)
    check("logging off does not read history", len(reads), 0)
    os.environ.pop("ARGES_EXPERIENCE", None)
    truthy("a call while logging was off did not enter the count",
           "`setp` 4 times" in srv.fusion_execute(code=SETP).get("hint", ""))
    os.environ["ARGES_EXPERIENCE"] = "0"
    out = srv.fusion_execute(code=DIFFERENT)
    check("logging off also applies with an already loaded cache", out.get("hint"), None)
    os.environ.pop("ARGES_EXPERIENCE", None)
    truthy("the disabled call did not advance the loaded count",
           "`setp` 4 times" in srv.fusion_execute(code=DIFFERENT).get("hint", ""))

    fresh_helpers(history)
    xp.ExperienceLog.read = broken_read
    out = srv.fusion_execute(code=SETP)
    check("a history exception cannot fail the successful call", out["ok"], True)
    check("a history exception means no hint", out.get("hint"), None)
    xp.ExperienceLog.read = real_read
    check("the call is still recorded after a hint failure", last_record()["ok"], True)

    fresh_helpers(history)
    real_repeated = xp.HelperCounts.repeated

    def broken_repeated(self, defs, threshold=3):
        raise RuntimeError("count unavailable")

    xp.HelperCounts.repeated = broken_repeated
    try:
        out = srv.fusion_execute(code=SETP)
        check("a counter exception cannot fail the successful call", out["ok"], True)
        check("a counter exception means no hint", out.get("hint"), None)
        check("the call is still logged after a counter failure", last_record()["ok"], True)
    finally:
        xp.HelperCounts.repeated = real_repeated
finally:
    xp.ExperienceLog.read = real_read
    srv._bridge_request = real_bridge
    srv._capture = real_capture
    os.environ.pop("ARGES_AUTOSAVE", None)
    os.environ.pop("ARGES_EXPERIENCE", None)


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
