"""The timeline in order, with each feature's health, so a failed or stale feature shows without a screenshot.

args: {"problems_only": false, "last": 0}, both optional. problems_only keeps
just the features that are not healthy; last=N keeps only the final N.
returns: {"count", "marker", "items": [{"index", "name", "type", "health",
"message", "suppressed", "rolled_back", "group"}], "problems": n}.
Check it after a parameter change: features can fail to recompute with no
exception raised anywhere.
"""
# Runs inside Fusion. Health names come from the enum itself rather than from
# remembered integers (verified on 2704: Healthy 0, Warning 1, Error 2,
# Suppressed 3, RolledBack 4, Unknown 5).
READONLY = True
CAP = 200


def _health_names():
    states = adsk.fusion.FeatureHealthStates
    return {getattr(states, n): n[: -len("FeatureHealthState")]
            for n in dir(states) if n.endswith("FeatureHealthState")}


def run(p):
    try:
        tl = design.timeline
        count = tl.count
    except (AttributeError, RuntimeError):
        return {"error": "this design has no timeline (direct modelling)"}
    names = _health_names()
    items = []
    for i in range(count):
        it = tl.item(i)
        entity = it.entity
        health = names.get(it.healthState, str(it.healthState))
        items.append({
            "index": i, "name": it.name,
            "type": entity.objectType.split("::")[-1] if entity else None,
            "health": health, "message": it.errorOrWarningMessage or "",
            "suppressed": it.isSuppressed, "rolled_back": it.isRolledBack,
            "group": it.isGroup,
        })
    problems = [x for x in items if x["health"] not in ("Healthy", "Suppressed", "RolledBack")]
    if p.get("problems_only"):
        items = problems
    last = int(p.get("last") or 0)
    if last > 0:
        items = items[-last:]
    return {"count": count, "marker": tl.markerPosition, "problems": len(problems),
            "items": items[-CAP:], "truncated": max(0, len(items) - CAP)}
