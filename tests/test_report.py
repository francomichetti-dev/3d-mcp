"""A pasteable experience report, without Fusion or the operator's history.

    cd server && uv run --frozen --no-sync python ../tests/test_report.py
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
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
    except Exception as exc:                              # noqa: BLE001
        FAIL += 1
        print(f"  FAIL {label}: raised {type(exc).__name__}, wanted {kind.__name__}")
        return
    FAIL += 1
    print(f"  FAIL {label}: did not raise")


from arges_mcp import report  # noqa: E402
from arges_mcp import fastcommands  # noqa: E402


def record(n, tool="fusion_execute", ok=True, session="a", **extra):
    return {"ts": f"2026-10-08T10:00:{n:02d}", "session": session,
            "tool": tool, "ok": ok, "ran": True, "sig": "", "ms": n * 1000,
            "code": "PRIVATE_CODE_EXCERPT", "args": {"part": "PRIVATE_ARG_VALUE"},
            "defs": {}, **extra}


print("Every report section")
records = [
    record(1, ok=False, sig="ValueError: profile", lessons=["fix", "fix"], autosave_ms=100),
    record(2, "fusion_import", ok=False, sig="Bridge unavailable", kind="tool_error",
           file="PRIVATE_IMPORT_PATH", format="step", from_document="PRIVATE_DOCUMENT_NAME"),
    record(3, defs={"helper": "shape", "alias": "shape", "bbox": "builtin-shape"}),
    record(4, ok=False, session="b", sig="ValueError: profile"),
    record(5, "fusion_run", command="bbox", autosave_ms=300),
    record(6, defs={"helper": "shape", "bbox": "builtin-shape"}),
    record(7, "fusion_run", ok=False, command="bbox", sig="Bridge unavailable"),
    record(8, defs={"alias": "shape", "bbox": "builtin-shape"}),
    record(9, "fusion_run", command="removed"),
]
lessons = [{"id": "fix", "sig": "ValueError: profile", "confirmed": 4,
            "lesson": "PRIVATE_LESSON_TEXT", "when": []},
           {"id": "recipe", "sig": "", "confirmed": 3,
            "lesson": "PRIVATE_RECIPE_TEXT", "when": ["PRIVATE_PHRASING", "remove selection"]}]
commands = [fastcommands.parse('"""PRIVATE_COMMAND_DESCRIPTION"""\ndef run(p): return p',
                              "bbox", "builtin"),
            {"name": "unused", "origin": "learned", "source": "PRIVATE_COMMAND_SOURCE"}]
before = copy.deepcopy((records, lessons, commands))
summary = report.summarize(records, lessons, commands)
check("pure: inputs untouched", (records, lessons, commands), before)
check("span", summary["span"], {"records": 9, "sessions": 2,
      "first": records[0]["ts"], "last": records[-1]["ts"]})
check("execute totals", summary["tools"]["fusion_execute"],
      {"calls": 5, "ok": 3, "failed": 2, "median_ms": 4000, "p90_ms": 8000})
check("autosave", summary["autosave"], {"count": 2, "median_ms": 200, "total_ms": 400,
                                        "skipped": 0})
# A call that changed nothing is not saved at all (step 7). Counting those is
# how the report shows what skipping the save has saved.
skipping = report.summarize([record(1, autosave_ms=200),
                             record(2, autosave="skipped: unchanged"),
                             record(3, autosave="skipped: unchanged")])
check("saves skipped because nothing changed are counted",
      skipping["autosave"], {"count": 1, "median_ms": 200, "total_ms": 200, "skipped": 2})
truthy("and shown", "2 skipped (nothing changed)" in report.render(skipping))
check("errors sorted by frequency then signature", [e["sig"] for e in summary["errors"]],
      ["Bridge unavailable", "ValueError: profile"])
check("bridge failure tools", summary["errors"][0]["tools"], ["fusion_import", "fusion_run"])
check("failure sessions", summary["errors"][1]["sessions"], 2)
check("failure last", summary["errors"][1]["last"], records[3]["ts"])
check("exact signature lesson", summary["errors"][1]["lesson"], "fix")
check("no covering lesson", summary["errors"][0]["lesson"], None)
check("unlearned", summary["unlearned"], ["Bridge unavailable"])
check("shown once per failure, helped", summary["lessons"],
      [{"id": "fix", "confirmed": 4, "shown": 1, "helped": 1, "lesson": "PRIVATE_LESSON_TEXT"}])
check("recipes counted separately", summary["recipes"],
      [{"id": "recipe", "confirmed": 3, "shown": 0, "helped": 0, "phrasings": 2,
        "lesson": "PRIVATE_RECIPE_TEXT", "when": ["PRIVATE_PHRASING", "remove selection"]}])
by_name = {c["name"]: c for c in summary["commands"]}
check("command runs", by_name["bbox"],
      {"name": "bbox", "runs": 2, "failed": 1, "median_ms": 6000, "origin": "builtin"})
check("historical command retained", by_name["removed"]["origin"], None)
check("learned commands never run", summary["never_run"], ["unused"])
check("shapes once per successful call, command names excluded", summary["helpers"],
      [{"shape": "shape", "count": 3, "names": ["alias", "helper"]}])
check("slow groups ordered by p90", [(s["tool"], s["command"], s["p90_ms"])
      for s in summary["slow"]], [("fusion_run", None, 9000),
      ("fusion_run", "removed", 9000), ("fusion_execute", None, 8000),
      ("fusion_run", "bbox", 7000)])

print("Lesson recovery window")
def helped(tail, head=None):
    rows = [head or record(1, ok=False, sig="err", lessons=["fix"])] + tail
    return report.summarize(rows, lessons)["lessons"][0]

check("third call in same session qualifies despite interleaving",
      helped([record(2, session="other"), record(3, "fusion_import"),
              record(4, ok=False), record(5)])["helped"], 1)
check("fourth call is too late", helped([record(2, "fusion_import"),
      record(3, ok=False), record(4, ok=False), record(5)])["helped"], 0)
check("another session does not help", helped([record(2, session="other")])["helped"], 0)
check("another tool does not help", helped([record(2, "fusion_import")])["helped"], 0)
check("one success can follow two showings", helped([
      record(2, ok=False, lessons=["fix"]), record(3)])["helped"], 2)
check("success does not count as lesson shown", helped([], record(1, lessons=["fix"]))["shown"], 0)
check("missing session still counts a showing", helped([], record(
      1, ok=False, session=None, lessons=["fix"]))["shown"], 1)
check("missing session cannot establish recovery", helped([record(2, session=None)], record(
      1, ok=False, session=None, lessons=["fix"]))["helped"], 0)
check("missing tool cannot establish recovery", helped([record(2, tool=None)], record(
      1, tool=None, ok=False, lessons=["fix"]))["helped"], 0)

print("Filtering, timings and malformed history")
rows = [record(1, ts="2026-10-07T23:59:59"), record(2),
        record(3, ts="2026-10-09T00:00:00")]
check("date boundary inclusive", report.summarize(rows, since="2026-10-08")["span"]["records"], 2)
check("datetime boundary", report.summarize(rows, since=datetime(2026, 10, 8, 10, 0, 3))
      ["span"]["records"], 1)
check("datetime keeps subsecond boundary", report.summarize([record(3)],
      since=datetime(2026, 10, 8, 10, 0, 3, 500000))["span"]["records"], 0)
filtered = report.summarize(records, lessons, commands, since="2026-10-09")
check("lessons not date-filtered", len(filtered["lessons"]), 1)
check("commands not date-filtered", len(filtered["commands"]), 2)
check("recipes not date-filtered", len(filtered["recipes"]), 1)
raises("invalid since rejected", lambda: report.summarize([], since="yesterday"), ValueError)
raises("short date rejected", lambda: report.summarize([], since="2026-1-2"), ValueError)
check("nearest-rank p90", report.summarize([record(n, ms=n) for n in range(1, 11)])
      ["tools"]["fusion_execute"]["p90_ms"], 9)
bad = [{}, {"ts": [], "tool": [], "ok": "false", "ms": "100",
       "session": {}, "sig": {}, "lessons": {}, "defs": [], "command": {}},
       record(1, ms=float("nan"), autosave_ms=float("inf")),
       record(2, ms=-1, autosave_ms=True, defs={"bad": [], "other": 4}),
       record(3, ms=True), record(4, ok=False, sig="err", lessons=[{}, [1], "fix"]),
       None, [], "not a record"]
malformed = report.summarize(bad, lessons + [None, {}, {"id": [], "confirmed": {}}],
                            commands + [None, {}, {"name": []}])
check("dict records survive partial fields", malformed["span"]["records"], 6)
check("invalid timings ignored", malformed["tools"]["fusion_execute"]["median_ms"], 4000)
check("invalid autosave ignored", malformed["autosave"]["count"], 0)
check("wrong outcome type is not a failure", malformed["tools"]["unknown"]["failed"], 0)
check("invalid date excluded when filtering", report.summarize(bad, since="2026-10-08")
      ["span"]["records"], 4)
check("raw error can supply a missing lesson sig", report.summarize(
      [record(1, ok=False, sig="RuntimeError: N : bad")],
      [{"id": "raw", "error": "RuntimeError: 3 : bad"}])["errors"][0]["lesson"], "raw")
check("similar signature does not match", report.summarize(
      [record(1, ok=False, sig="ValueError: other")], lessons)["errors"][0]["lesson"], None)
check("errors sorted by frequency", [e["sig"] for e in report.summarize([
      record(1, ok=False, sig="aaa"), record(2, ok=False, sig="zzz"),
      record(3, ok=False, sig="zzz")])["errors"]], ["zzz", "aaa"])
check("best confirmed covering lesson", report.summarize(records,
      lessons + [{"id": "less-confirmed", "sig": "ValueError: profile", "confirmed": 1}])
      ["errors"][1]["lesson"], "fix")
check("span uses extrema, not input order", report.summarize(list(reversed(records)))
      ["span"], summary["span"])
check("unknown timing stays unknown", report.summarize([record(1, ms=None)])
      ["tools"]["fusion_execute"]["p90_ms"], None)
check("slow threshold inclusive", len(report.summarize([record(1, ms=5000)])["slow"]), 1)
check("below slow threshold excluded", report.summarize([record(1, ms=4999)])["slow"], [])
check("failed, non-execute and malformed outcomes do not count helpers",
      report.summarize([record(1, ok=False, defs={"helper": "shape"}),
      record(2, "fusion_run", defs={"helper": "shape"}),
      record(3, ok="true", defs={"helper": "shape"})])["helpers"], [])
check("filter shrinks error and helper history", filtered["unlearned"] + filtered["helpers"], [])

print("Safe and short terminal output")
rendered = report.render(summary)
for secret in ("PRIVATE_CODE_EXCERPT", "PRIVATE_ARG_VALUE", "PRIVATE_COMMAND_SOURCE",
               "PRIVATE_COMMAND_DESCRIPTION", "PRIVATE_IMPORT_PATH", "PRIVATE_DOCUMENT_NAME"):
    check(f"no {secret}", secret in json.dumps(summary) + rendered, False)
# Lessons are the exception, on purpose: /fusion-improve proposes promoting a
# lesson into the skill, and it cannot propose a line it cannot read. They are
# general text the model wrote, already sent back to it on every recall.
dumped = json.dumps(summary)
truthy("a lesson's text is in the JSON, to be promoted", "PRIVATE_LESSON_TEXT" in dumped)
truthy("so are a recipe's text and phrasings",
       "PRIVATE_RECIPE_TEXT" in dumped and "PRIVATE_PHRASING" in dumped)
check("empty report", report.render(report.summarize([])), "No experience recorded yet.")
positions = [rendered.index(title) for title in
             ("Span:", "Tools:", "Errors:", "Lessons:", "Commands:", "Helpers:", "Slow:", "Next:")]
check("section order", positions, sorted(positions))
truthy("next action for error", "record a lesson for Bridge unavailable (seen 2x)" in rendered)
truthy("next action for helper", "save helper alias as a fast command (defined 3x)" in rendered)
truthy("next action for recipe", "promote recipe recipe" in rendered)
large = report.summarize([record(n % 60, tool=f"tool_{n}", ok=False,
      sig=f"error_{n}", ms=9000) for n in range(200)], lessons, commands)
truthy("large log stays under 60 lines", len(report.render(large).splitlines()) < 60)
busy_records = [record(n % 60, defs={f"helper_{n % 20}": f"shape_{n % 20}"},
                       autosave_ms=10000) for n in range(100)]
busy_lessons = [{"id": f"lesson_{n}", "confirmed": 5, "when": ["private"] if n % 2 else []}
                for n in range(100)]
busy_commands = [{"name": f"command_{n}", "origin": "learned"} for n in range(100)]
busy = report.summarize(busy_records + [record(n % 60, tool=f"tool_{n}", ok=False,
      sig=f"error_{n}\nsecond line", ms=9000) for n in range(200)], busy_lessons, busy_commands)
truthy("all large sections still under 60 lines", len(report.render(busy).splitlines()) < 60)
truthy("at most five suggestions", len(rendered.split("Next:\n")[1].splitlines()) <= 5)
truthy("no fastmcp imported by report", "fastmcp" not in sys.modules)

print("CLI in isolated state")
with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(REPO / "server/src"))
    # An import guard proves the command works even when the MCP dependency
    # cannot be loaded, rather than relying on a subprocess's module list.
    probe = """import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'fastmcp' or fullname.startswith('fastmcp.'):
            raise AssertionError('experience imported fastmcp')
sys.meta_path.insert(0, Guard())
from arges_mcp.cli import main
sys.exit(main(sys.argv[1:]))
"""

    def run(*args):
        return subprocess.run([sys.executable, "-c", probe, "experience", *args],
                              env=env, capture_output=True, text=True)

    empty = run()
    check("missing log exits zero without MCP", empty.returncode, 0)
    truthy("missing log message", "No experience recorded yet." in empty.stdout)
    check("empty message appears once", empty.stdout.count("No experience recorded yet."), 1)
    truthy("shows searched path", str(home / ".arges/experience.jsonl") in empty.stdout)
    check("empty JSON is still JSON", json.loads(run("--json").stdout)["span"]["records"], 0)
    check("read-only CLI creates no state", (home / ".arges").exists(), False)
    state = home / ".arges"
    state.mkdir()
    (state / "experience.jsonl").write_text("\n".join(json.dumps(r) for r in records)
                                           + "\nbroken\n[]\n", encoding="utf-8")
    (state / "lessons.json").write_text(json.dumps({"lessons": lessons}), encoding="utf-8")
    learned = state / "commands"
    learned.mkdir()
    (learned / "unused.py").write_text('"""Unused"""\ndef run(p): return p\n', encoding="utf-8")
    populated = run("--json")
    check("populated CLI exits zero without MCP", populated.returncode, 0)
    data = json.loads(populated.stdout)
    check("CLI reads log", data["span"]["records"], 9)
    check("CLI reads lessons", data["lessons"][0]["shown"], 1)
    check("CLI reads library", data["never_run"], ["unused"])
    truthy("populated plain text", "Tools:" in run().stdout)
    check("CLI since", json.loads(run("--json", "--since", "2026-10-09").stdout)
          ["span"]["records"], 0)
    check("CLI invalid date is usage error", run("--since", "2026-13-99").returncode, 2)
    (state / "experience.jsonl").write_text("", encoding="utf-8")
    check("empty existing log exits zero", run().returncode, 0)
    truthy("empty existing log message", "No experience recorded yet." in run().stdout)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
