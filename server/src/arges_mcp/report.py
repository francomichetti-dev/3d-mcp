"""What accumulated experience says to improve next.

Names, counts, timings, error signatures and the lessons' own text leave this
module; the code, arguments, import paths and command source in the log do
not. Those are design data. A lesson is general text the model wrote, already
sent back to it on every recall, and /fusion-improve cannot propose promoting
a line into the skill without reading it. Error signatures are quoted as
recorded, so one can still name a file or a body.

Standard library only, so the chat service can use the same report later.
"""

import math
from datetime import datetime
from statistics import median

from .experience import HelperCounts, error_signature


def _name(value):
    return value if isinstance(value, str) and value else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if value >= 0 and math.isfinite(value) else None
    except OverflowError:
        return None


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%S")


def _timings(values):
    values = sorted(values)
    if not values:
        return {"median_ms": None, "p90_ms": None}
    # Nearest rank keeps a small group's p90 an observed call, rather than an
    # interpolated duration that never occurred. Missing timings are unknown,
    # not zero: otherwise partial records make a slow path look fast.
    return {"median_ms": median(values),
            "p90_ms": values[math.ceil(len(values) * .9) - 1]}


def summarize(records, lessons=(), commands=(), since=None):
    """Summarize history without changing it or consulting local state.

    Dict records count even if partial. Outcomes must be actual booleans, and
    invalid or negative timings are ignored. Recovery follows log order,
    counting all tools among the next three calls within the same session.
    `since` uses the log's local wall clock; its timestamps carry no timezone.
    """
    if isinstance(since, datetime):
        cutoff = since.replace(tzinfo=None).isoformat()
    elif since is not None:
        cutoff = datetime.strptime(since, "%Y-%m-%d").strftime("%Y-%m-%dT%H:%M:%S")
        if cutoff[:10] != since:
            raise ValueError("use a valid date in YYYY-MM-DD form")
    else:
        cutoff = None

    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        stamp = _stamp(record.get("ts"))
        if cutoff is not None and (stamp is None or stamp < cutoff):
            continue
        # An allowlist is easier to audit than removing today's private fields
        # from a copy; new log fields cannot accidentally become report data.
        shown = record.get("lessons")
        rows.append({"ts": stamp, "session": _name(record.get("session")),
                     "tool": _name(record.get("tool")) or "unknown",
                     "ok": record.get("ok") if type(record.get("ok")) is bool else None,
                     "sig": _name(record.get("sig")), "ms": _number(record.get("ms")),
                     "autosave_ms": _number(record.get("autosave_ms")),
                     "autosave_skipped": record.get("autosave") == "skipped: unchanged",
                     "command": _name(record.get("command")),
                     "lessons": {_name(item) for item in shown if _name(item)}
                                if isinstance(shown, list) else set(),
                     "defs": record.get("defs")})

    stored = {}
    for item in lessons:
        if not isinstance(item, dict) or not _name(item.get("id")):
            continue
        phrasings = item.get("when")
        phrasings = (sum(bool(_name(p)) for p in phrasings)
                     if isinstance(phrasings, list) else int(bool(_name(phrasings))))
        sig = _name(item.get("sig"))
        if sig is None and isinstance(item.get("error"), str):
            sig = error_signature(item["error"]) or None
        when = item.get("when")
        stored[item["id"]] = {"id": item["id"], "confirmed": _count(item.get("confirmed")),
                              "sig": sig, "phrasings": phrasings, "shown": 0, "helped": 0,
                              "lesson": " ".join(str(item.get("lesson") or "").split())[:600],
                              "when": [w for w in when if _name(w)][:5]
                                      if isinstance(when, list) else []}
    covering = {}
    for item in sorted(stored.values(), key=lambda i: (-i["confirmed"], i["id"])):
        if item["sig"]:
            covering.setdefault(item["sig"], item["id"])

    origins = {}
    for command in commands:
        if isinstance(command, dict):
            name, origin = command.get("name"), command.get("origin")
        else:
            name, origin = getattr(command, "name", None), getattr(command, "origin", None)
        if _name(name):
            origins[name] = origin if origin in ("builtin", "learned") else None

    tools, tool_times, failures, command_rows, command_times, sessions = {}, {}, {}, {}, {}, {}
    autosaves = []
    skipped = 0
    for row in rows:
        tool = row["tool"]
        stats = tools.setdefault(tool, {"calls": 0, "ok": 0, "failed": 0})
        stats["calls"] += 1
        stats["ok"] += row["ok"] is True
        stats["failed"] += row["ok"] is False
        if row["ms"] is not None:
            tool_times.setdefault(tool, []).append(row["ms"])
        if row["autosave_ms"] is not None:
            autosaves.append(row["autosave_ms"])
        skipped += row["autosave_skipped"]
        if row["session"]:
            sessions.setdefault(row["session"], []).append(row)
        if row["ok"] is False and row["sig"]:
            error = failures.setdefault(row["sig"], {"sig": row["sig"], "count": 0,
                                        "sessions": set(), "tools": set(), "last": None,
                                        "lesson": covering.get(row["sig"])})
            error["count"] += 1
            if row["session"]:
                error["sessions"].add(row["session"])
            error["tools"].add(tool)
            if row["ts"]:
                error["last"] = max(error["last"] or row["ts"], row["ts"])
        if tool == "fusion_run" and row["command"]:
            name = row["command"]
            stats = command_rows.setdefault(name, {"name": name, "runs": 0, "failed": 0,
                                                   "origin": origins.get(name)})
            stats["runs"] += 1
            stats["failed"] += row["ok"] is False
            if row["ms"] is not None:
                command_times.setdefault(name, []).append(row["ms"])

    # A session id is required to infer recovery. Two partial records with no
    # session must not be treated as evidence that a lesson helped.
    for calls in sessions.values():
        for i, row in enumerate(calls):
            if row["ok"] is not False:
                continue
            helped = row["tool"] != "unknown" and any(
                later["ok"] is True and later["tool"] == row["tool"]
                for later in calls[i + 1:i + 4])
            for lesson_id in row["lessons"]:
                if lesson_id in stored:
                    stored[lesson_id]["shown"] += 1
                    stored[lesson_id]["helped"] += helped
    # Showings without a session are still observed, even though recovery
    # cannot be established for them.
    for row in rows:
        if row["session"] is None and row["ok"] is False:
            for lesson_id in row["lessons"]:
                if lesson_id in stored:
                    stored[lesson_id]["shown"] += 1

    for name in origins:
        command_rows.setdefault(name, {"name": name, "runs": 0, "failed": 0,
                                      "origin": origins[name]})
    slow = []
    for tool, stats in sorted(tools.items()):
        stats.update(_timings(tool_times.get(tool, [])))
        if stats["p90_ms"] is not None and stats["p90_ms"] >= 5000:
            slow.append({"tool": tool, "command": None, "calls": stats["calls"],
                         "p90_ms": stats["p90_ms"]})
    for name, stats in sorted(command_rows.items()):
        timings = _timings(command_times.get(name, []))
        stats["median_ms"] = timings["median_ms"]
        if timings["p90_ms"] is not None and timings["p90_ms"] >= 5000:
            slow.append({"tool": "fusion_run", "command": name, "calls": stats["runs"],
                         "p90_ms": timings["p90_ms"]})

    errors = []
    for error in sorted(failures.values(), key=lambda e: (-e["count"], e["sig"])):
        errors.append({**error, "sessions": len(error["sessions"]),
                       "tools": sorted(error["tools"])})
    fixes, recipes = [], []
    for item in sorted(stored.values(), key=lambda i: (-i["confirmed"], i["id"])):
        entry = {key: item[key] for key in ("id", "confirmed", "shown", "helped", "lesson")}
        if item["phrasings"]:
            recipes.append({**entry, "phrasings": item["phrasings"], "when": item["when"]})
        else:
            fixes.append(entry)

    helpers = HelperCounts(rows)
    candidates = []
    for shape, count in helpers.counts.items():
        names = sorted(helpers.names[shape])
        # Name-only coverage is a heuristic: a renamed command can be missed,
        # and an unrelated command with the same name can hide a candidate.
        # Any known alias covers the shape; source comparison would require
        # retaining code here and would complicate a deliberately safe report.
        if count >= 3 and not set(names) & origins.keys():
            candidates.append({"shape": shape, "count": count, "names": names})

    stamps = [r["ts"] for r in rows if r["ts"]]
    return {"span": {"records": len(rows), "sessions": len(sessions),
                     "first": min(stamps) if stamps else None,
                     "last": max(stamps) if stamps else None},
            "tools": dict(sorted(tools.items())),
            "autosave": {"count": len(autosaves),
                         "median_ms": median(autosaves) if autosaves else None,
                         "total_ms": sum(autosaves), "skipped": skipped},
            "errors": errors,
            "unlearned": [e["sig"] for e in errors if e["count"] >= 2 and e["lesson"] is None],
            "lessons": fixes, "recipes": recipes,
            "commands": [command_rows[name] for name in sorted(command_rows)],
            "never_run": sorted(name for name, c in command_rows.items()
                                if c["origin"] == "learned" and c["runs"] == 0),
            "helpers": sorted(candidates, key=lambda c: (-c["count"], c["names"], c["shape"])),
            "slow": sorted(slow, key=lambda s: (-s["p90_ms"], s["tool"], s["command"] or ""))}


def render(summary):
    """A bounded terminal report; JSON retains everything the display omits."""
    span = summary["span"]
    if not span["records"] and not summary["lessons"] and not summary["recipes"]:
        if not any(c["origin"] == "learned" for c in summary["commands"]):
            return "No experience recorded yet."

    lines = []

    def short(value):
        # A signature can be long, and hand-edited records can contain newlines.
        # Neither should make one item consume the whole terminal report.
        text = " ".join(str(value).split())
        return text if len(text) <= 160 else text[:157] + "..."

    def section(title, items):
        if not items:
            return
        lines.append(title + ":")
        lines.extend("  " + short(item) for item in items[:4])
        if len(items) > 4:
            lines.append(f"  ... {len(items) - 4} more (see --json)")

    def ms(value):
        return "?" if value is None else f"{value:g} ms"

    section("Span", [f"{span['records']} calls, {span['sessions']} sessions; "
                     f"{span['first'] or '?'} to {span['last'] or '?'}"])
    section("Tools", [f"{name}: {s['calls']} calls, {s['ok']} ok, {s['failed']} failed; "
                      f"median {ms(s['median_ms'])}, p90 {ms(s['p90_ms'])}"
                      for name, s in summary["tools"].items()])
    autosave = summary["autosave"]
    if autosave["count"] or autosave.get("skipped"):
        section("Autosave", [f"{autosave['count']} saves; median {ms(autosave['median_ms'])}, "
                             f"total {ms(autosave['total_ms'])}; "
                             f"{autosave.get('skipped', 0)} skipped (nothing changed)"])
    section("Errors", [f"{e['sig']}: {e['count']}x, {e['sessions']} sessions; "
                       f"lesson {e['lesson'] or 'none'}" for e in summary["errors"]])
    section("Lessons", [f"{s['id']}: confirmed {s['confirmed']}, shown {s['shown']}, "
                        f"helped {s['helped']}" for s in summary["lessons"]])
    section("Recipes", [f"{s['id']}: confirmed {s['confirmed']}, {s['phrasings']} phrasings, "
                        f"shown {s['shown']}, helped {s['helped']}" for s in summary["recipes"]])
    section("Commands", [f"{c['name']} ({c['origin'] or 'unknown'}): {c['runs']} runs, "
                         f"{c['failed']} failed; median {ms(c['median_ms'])}"
                         for c in sorted(summary["commands"], key=lambda c: (-c["runs"], c["name"]))])
    section("Helpers", [f"{', '.join(h['names'])}: defined {h['count']}x" for h in summary["helpers"]])
    section("Slow", [f"{s['command'] or s['tool']}: p90 {ms(s['p90_ms'])} "
                     f"({s['calls']} calls)" for s in summary["slow"]])

    suggestions = []
    for error in summary["errors"]:
        if error["sig"] in summary["unlearned"]:
            suggestions.append(f"record a lesson for {error['sig']} (seen {error['count']}x)")
    for helper in summary["helpers"]:
        suggestions.append(f"save helper {helper['names'][0]} as a fast command "
                           f"(defined {helper['count']}x)")
    for recipe in summary["recipes"]:
        if recipe["confirmed"] >= 2:
            suggestions.append(f"promote recipe {recipe['id']} into the skill "
                               f"(confirmed {recipe['confirmed']}x)")
    for lesson in summary["lessons"]:
        if lesson["confirmed"] >= 3:
            suggestions.append(f"promote lesson {lesson['id']} into the skill "
                               f"(confirmed {lesson['confirmed']}x)")
    for name in summary["never_run"]:
        suggestions.append(f"try learned command {name} (never run)")
    for slow in summary["slow"]:
        suggestions.append(f"measure {slow['command'] or slow['tool']} "
                           f"(p90 {ms(slow['p90_ms'])})")
    if suggestions:
        lines.append("Next:")
        lines.extend("  - " + short(item) for item in suggestions[:5])
    return "\n".join(lines)
