"""Lessons: a fix recorded once comes back the next time its error does.

Nothing here needs Fusion. A lesson store is only worth having if it is trusted.
That takes three things: a lesson comes back for its own error and only for it,
a wrong one can be removed, and the store can never break the call it is
attached to. The last matters most. A failing call already carries a traceback,
and an unreadable lessons file must not replace that with a different error.

    cd server && uv run --frozen --no-sync python ../tests/test_lessons.py
"""

import json
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


def raises(label, fn, kind=Exception):
    global PASS, FAIL
    try:
        fn()
    except kind:
        PASS += 1
        return
    FAIL += 1
    print(f"  FAIL {label}: did not raise {kind.__name__}")


from arges_mcp import common  # noqa: E402

# Everything resolves through common.state_dir() at call time, so this one
# assignment keeps the suite away from the operator's real lessons.
SANDBOX = Path(tempfile.mkdtemp(prefix="arges-lessons-test-"))
common.state_dir = lambda: SANDBOX / "state"

from arges_mcp import experience as xp  # noqa: E402
from arges_mcp import lessons as ls  # noqa: E402

PROFILE_TB = """Traceback (most recent call last):
  File "<fusion_execute>", line 9, in <module>
    feats.extrudeFeatures.addSimple(prof, dist, op)
RuntimeError: 3 : invalid argument profile
"""
PROFILE_FIX = ("profiles.item(0) is not stable: pick the profile by "
               "areaProperties().area or boundingBox instead of by index.")


# --------------------------------------------------------------- store ----
print("Storing a lesson")

store = ls.Lessons(SANDBOX / "a" / "lessons.json")
check("an empty store has nothing", store.all(), [])
check("and recalls nothing", store.for_error(PROFILE_TB), [])
check("and has no general lessons", store.general(), [])

added = store.add(PROFILE_FIX, error=PROFILE_TB, tags="profiles, Extrude")
check("a new lesson says it is new", added["new"], True)
check("confirmed once", added["confirmed"], 1)
check("signed by its error, not the whole traceback", added["sig"],
      "RuntimeError: N : invalid argument profile")
truthy("and given an id", added["id"])
check("tags are normalised", store.all()[0]["tags"], ["profiles", "extrude"])
check("the file is private", stat.S_IMODE(store.path.stat().st_mode), 0o600)
check("so is its directory", stat.S_IMODE(store.path.parent.stat().st_mode), 0o700)

again = store.add("  Profiles.item(0) is NOT stable: pick the profile by "
                  "areaProperties().area or boundingBox instead of by index. ",
                  error="RuntimeError: 7 : invalid argument profile")
check("the same lesson again confirms it rather than duplicating it",
      (again["new"], again["confirmed"], len(store.all())), (False, 2, 1))
check("and keeps its id", again["id"], added["id"])

other = store.add("If the sketch has no closed loop, profiles is empty.",
                  error=PROFILE_TB)
check("a second lesson for one error is kept alongside", len(store.all()), 2)
recalled = store.for_error("RuntimeError: 12 : invalid argument profile")
check("both come back for the error, numbers notwithstanding", len(recalled), 2)
check("the better-confirmed one first", recalled[0]["id"], added["id"])
check("a different error recalls nothing",
      store.for_error("RuntimeError: 3 : invalid argument body"), [])
check("an empty error recalls nothing", store.for_error(""), [])
check("a signature passed back in is the same signature",
      xp.error_signature(added["sig"]), added["sig"])
check("so a nudge's signature can be handed straight to add",
      store.add(PROFILE_FIX, error=added["sig"])["id"], added["id"])

general = store.add("Querying faces in the call that created a feature reads "
                    "stale topology; split the query into the next call.",
                    tags=["topology"])
check("a lesson with no error is general", general["sig"], "")
check("general lessons are listed", [g["id"] for g in store.general()], [general["id"]])
check("and never recalled for an error", general["id"] in
      [r["id"] for r in store.for_error(PROFILE_TB)], False)

raises("an empty lesson is refused", lambda: store.add("   "), ValueError)
long_one = store.add("x " * 2000)
truthy("a huge lesson is capped",
       len([l for l in store.all() if l["id"] == long_one["id"]][0]["lesson"])
       <= ls.MAX_LESSON_CHARS)

check("forgetting removes it", store.forget(other["id"]), True)
check("and it is gone", other["id"] in [l["id"] for l in store.all()], False)
check("forgetting something unknown says so", store.forget("nope"), False)


# ---------------------------------------------------------- recipes ----
# Successes are worth as much as fixes. Asked to "delete the one I selected",
# the panel's model first asked which copy, and only after the person said it
# again did it read ui.activeSelections and remove the bodies; that path worked
# first time. A recipe records it with the phrasings that should bring it back,
# so next time it comes with the request instead of after a round of questions.
print("Recipes: successes recalled by what was asked")

store = ls.Lessons(SANDBOX / "recipes" / "lessons.json")
RECIPE = ("Read ui.activeSelections (fusion_run('selection')): a selected face or edge "
          "belongs to entity.body. Remove reversibly with removeFeatures "
          "(fusion_run('remove', {'selection': true})).")
added = store.add(RECIPE, when="the person refers to the one they selected")
check("a recipe is stored with what it answers",
      store.all()[0]["when"], ["the person refers to the one they selected"])
check("it is a general lesson, not tied to an error", added["sig"], "")

found = store.matching("the one that ive selected, we need to delete")
check("the person's own words bring it back", [l["id"] for l in found], [added["id"]])
check("so do other forms of the same words",
      [l["id"] for l in store.matching("delete my selection")], [added["id"]])
check("an unrelated request brings nothing", store.matching("make the walls 2 mm thicker"), [])
check("nor does an empty one", store.matching(""), [])

again = store.add(RECIPE, when="delete what I have highlighted")
check("the same success in other words confirms it", (again["new"], again["confirmed"]), (False, 2))
check("and keeps both phrasings", len(store.all()[0]["when"]), 2)
check("so the new phrasing finds it too",
      [l["id"] for l in store.matching("please delete the highlighted part")], [added["id"]])

store.add(PROFILE_FIX, error=PROFILE_TB)
check("a lesson's own text can match a request as well",
      [l["lesson"] for l in store.matching("which profile do I pick, by area or by index")],
      [PROFILE_FIX])
check("what rides on a result says when it applies",
      ls.brief(store.all()[0]).get("when"), store.all()[0]["when"])
check("an unreadable store matches nothing, without raising",
      ls.Lessons(SANDBOX / "nowhere" / "x" / "lessons.json").matching("delete the selected"), [])


# ------------------------------------------------- shared between processes --
# Claude Code and every design in the panel run their own MCP server, all on
# one file. A store that wrote back a stale copy would erase another's lesson.
print("Shared between processes")

path = SANDBOX / "shared" / "lessons.json"
first, second = ls.Lessons(path), ls.Lessons(path)
first.add("Lesson from the panel.")
second.add("Lesson from Claude Code.")
check("neither writer erases the other",
      sorted(l["lesson"] for l in ls.Lessons(path).all()),
      ["Lesson from Claude Code.", "Lesson from the panel."])

# A reader can hold corrupt bytes after a writer has quarantined them and
# installed a valid store. Renaming by path then moved the writer's new file
# aside, so the next write silently started over. Pause at the read, not on a
# timer, to make that ordering happen on every run.
real_read_text = Path.read_text
for method in ("all", "for_error", "general"):
    path = SANDBOX / method / "lessons.json"
    path.parent.mkdir()
    path.write_text("{ not json", encoding="utf-8")
    reader, writer = ls.Lessons(path), ls.Lessons(path)

    def read_then_write(self, *args, **kwargs):
        raw = real_read_text(self, *args, **kwargs)
        if self == path:
            Path.read_text = real_read_text
            writer.add("Written while the reader held corrupt bytes.")
        return raw

    Path.read_text = read_then_write
    try:
        read = getattr(reader, method)
        check(f"{method} returns empty for the corrupt bytes it read",
              read(PROFILE_TB) if method == "for_error" else read(), [])
    finally:
        Path.read_text = real_read_text
    check(f"{method} leaves the writer's replacement in place",
          [l["lesson"] for l in writer.all()],
          ["Written while the reader held corrupt bytes."])
    writer.add("Written after the reader finished.")
    check(f"{method} cannot make the next writer lose history",
          len(writer.all()), 2)


# ------------------------------------------------------------ failures ----
print("Never in the way")

broken = SANDBOX / "broken" / "lessons.json"
broken.parent.mkdir(parents=True)
broken.write_text("{ not json", encoding="utf-8")
store = ls.Lessons(broken)
check("a corrupt file reads as empty rather than raising", store.all(), [])
check("an unlocked read leaves the corrupt bytes in place",
      broken.exists() and broken.read_text(encoding="utf-8"), "{ not json")
check("and does not quarantine anything",
      list(broken.parent.glob("lessons.json.corrupt-*")), [])
store.add("Fresh start.")
check("the next write starts clean", [l["lesson"] for l in store.all()], ["Fresh start."])
truthy("but the corrupt file is kept aside, not destroyed",
       list(broken.parent.glob("lessons.json.corrupt-*")))

blocked = SANDBOX / "blocked"
(blocked / "lessons.json").mkdir(parents=True)          # a directory, not a file
store = ls.Lessons(blocked / "lessons.json")
check("an unreadable store recalls nothing, without raising",
      store.for_error(PROFILE_TB), [])
check("and lists nothing", store.general(), [])
raises("but writing to it does fail loudly: that is an explicit request",
       lambda: store.add("x"), OSError)

capped = ls.Lessons(SANDBOX / "cap" / "lessons.json", max_lessons=3)
keep = capped.add("Keep me.")
capped.add("Keep me.")                                    # confirmed twice
for n in range(5):
    capped.add(f"Disposable {n}.")
check("a full store stays at its cap", len(capped.all()), 3)
check("dropping the least confirmed first",
      keep["id"] in [l["id"] for l in capped.all()], True)


# -------------------------------------------------------------- wiring ----
print("Wired into the tools")

from fastmcp.exceptions import ToolError  # noqa: E402

from arges_mcp import server as srv  # noqa: E402

os.environ["ARGES_AUTOSAVE"] = "0"
real_bridge = srv._bridge_request


def bridge_answering(answer):
    def request(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1"}
        return dict(answer)
    return request


try:
    learned = srv.fusion_learn(lesson=PROFILE_FIX, error=PROFILE_TB)
    check("fusion_learn records a lesson", learned["ok"], True)
    check("and says it is new", learned["new"], True)
    check("under the error's signature", learned["error_signature"],
          "RuntimeError: N : invalid argument profile")
    raises("a lesson with no text is refused",
           lambda: srv.fusion_learn(lesson=" "), ToolError)

    srv._bridge_request = bridge_answering(
        {"ok": False, "traceback": PROFILE_TB.replace("3 :", "5 :"), "stdout": ""})
    out = srv.fusion_execute(code="feats.extrudeFeatures.addSimple(prof, dist, op)")
    check("a failure comes back with the lesson recorded for it",
          [l["lesson"] for l in out.get("lessons", [])], [PROFILE_FIX])
    check("with the id needed to correct it", out["lessons"][0]["id"], learned["id"])
    check("and no nudge, since there is a lesson", "learn" in out, False)
    check("the traceback is still there, untouched",
          out["traceback"], PROFILE_TB.replace("3 :", "5 :"))
    check("the log notes which lessons the call was shown",
          xp.default_log().read()[-1].get("lessons"), [learned["id"]])

    srv._bridge_request = bridge_answering(
        {"ok": False, "traceback": "NameError: name 'sk9' is not defined", "stdout": ""})
    out = srv.fusion_execute(code="sk9.profiles")
    check("an unknown error carries no lessons", "lessons" in out, False)
    truthy("but a nudge to record the fix once it is found",
           "fusion_learn" in out.get("learn", ""))
    truthy("quoting the signature to pass back",
           "NameError: name 'sk9' is not defined" in out.get("learn", ""))

    srv._bridge_request = bridge_answering({"ok": False, "error": "no active Fusion design"})
    out = srv.fusion_execute(code="x = 1")
    check("code that never ran gets no nudge: its message already says what to do",
          "learn" in out, False)

    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    srv._streak_reset()                 # a first success, not one after failures
    out = srv.fusion_execute(code="x = 1")
    check("a success carries neither", ("lessons" in out, "learn" in out), (False, False))

    # The one that matters: the store breaking must not change the failure.
    real_state_dir = common.state_dir
    common.state_dir = lambda: blocked
    srv._bridge_request = bridge_answering({"ok": False, "traceback": PROFILE_TB, "stdout": ""})
    out = srv.fusion_execute(code="boom()")
    check("an unreadable store leaves the failure exactly as it was",
          (out["ok"], out["traceback"]), (False, PROFILE_TB))
    common.state_dir = real_state_dir

    # Correcting a lesson is part of trusting them: a wrong one misleads every
    # session that recalls it.
    fixed = srv.fusion_learn(lesson="Pick profiles by area, never by index.",
                             error=PROFILE_TB, forget=learned["id"])
    check("forget plus a lesson replaces it", fixed["forgot"], learned["id"])
    check("the old one is gone from recall",
          [l["lesson"] for l in ls.default_store().for_error(PROFILE_TB)],
          ["Pick profiles by area, never by index."])
    gone = srv.fusion_learn(lesson="", forget=fixed["id"])
    check("forget alone just removes it", (gone["ok"], gone["forgot"]), (True, fixed["id"]))
    missing = srv.fusion_learn(lesson="", forget="nope")
    check("forgetting an unknown id says so instead of pretending",
          missing["ok"], False)

    # Recipes through the tools: recorded with `when`, recalled by fusion_state
    # with the person's request as the query.
    recipe = srv.fusion_learn(lesson=RECIPE, when="the person refers to the one they selected")
    check("fusion_learn records a recipe", (recipe["ok"], recipe["when"]),
          (True, ["the person refers to the one they selected"]))

    def state_bridge_q(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1", "app_version": "2704"}
        return {"ok": True, "result": {"ok": True, "document": "tower"}, "stdout": ""}
    srv._bridge_request = state_bridge_q
    state = srv.fusion_state(query="the one I selected, delete it")
    check("fusion_state(query=...) returns what worked before for a request like it",
          [l["id"] for l in state["experience"]["relevant"]], [recipe["id"]])
    check("and nothing relevant without a query", srv.fusion_state()["experience"].get("relevant"), [])
    srv.fusion_learn(lesson="", forget=recipe["id"])

    # A success that took several failures is the moment a quick path is
    # known and about to be forgotten.
    srv._streak_reset()
    srv._bridge_request = bridge_answering({"ok": False, "traceback": PROFILE_TB, "stdout": ""})
    srv.fusion_execute(code="a()")
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    out = srv.fusion_execute(code="b()")
    check("one failure then a success is ordinary: no nudge", "learn" in out, False)
    srv._bridge_request = bridge_answering({"ok": False, "traceback": PROFILE_TB, "stdout": ""})
    srv.fusion_execute(code="a()")
    srv._bridge_request = bridge_answering({"ok": False, "traceback": "NameError: x", "stdout": ""})
    srv.fusion_execute(code="a()")
    srv._bridge_request = bridge_answering({"ok": True, "result": 1, "stdout": ""})
    out = srv.fusion_execute(code="c()")
    truthy("a success after two failures nudges to record the quick path",
           "2 failed attempts" in out.get("learn", "") and "when=" in out.get("learn", ""))
    out = srv.fusion_execute(code="d()")
    check("once: the next success is ordinary again", "learn" in out, False)

    # fusion_state is what a session reads first, so it is where the lessons
    # that have no error to trigger them get seen at all.
    srv.fusion_learn(lesson="Read the screenshot before the next step.", tags="verify")

    def state_bridge(method, path, payload=None):
        if path == "/health":
            return {"ok": True, "document": "tower", "bridge_version": "1",
                    "app_version": "2704"}
        return {"ok": True, "result": {"ok": True, "document": "tower"}, "stdout": ""}

    srv._bridge_request = state_bridge
    state = srv.fusion_state()
    check("fusion_state carries the general lessons",
          [l["lesson"] for l in state["experience"]["lessons"]],
          ["Read the screenshot before the next step."])
    truthy("and how many lessons exist in all", state["experience"]["lessons_total"] >= 1)
finally:
    srv._bridge_request = real_bridge
    os.environ.pop("ARGES_AUTOSAVE", None)


import shutil  # noqa: E402
shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
