"""Bounding boxes in millimetres, in world space, for every body or the ones named.

args: {"bodies": ["Plate", "Housing:1/Shell"]}, optional. Omit for every body
in the design. Bodies inside components are reached through their occurrences,
so their boxes are in world coordinates too, keyed "Occurrence:1/Body".
A plain body name matches it wherever it lives.
returns: {"bodies": {name: {"min": [x, y, z], "max": [...], "size": [...]}},
"overall": the box around all of them, "missing": names not found,
"truncated": how many were left out past the cap of 100}.
"""
# Runs inside Fusion: `design` is injected. Internal lengths are centimetres,
# so every coordinate is multiplied by 10 on the way out.
READONLY = True
CAP = 100


def _mm(v):
    return round(v * 10, 3) + 0.0     # folds -0.0 into 0.0


def _box(lo, hi):
    return {"min": [_mm(v) for v in lo], "max": [_mm(v) for v in hi],
            "size": [_mm(h - l) for l, h in zip(lo, hi)]}


def _corners(body):
    bb = body.boundingBox
    lo, hi = bb.minPoint, bb.maxPoint
    return (lo.x, lo.y, lo.z), (hi.x, hi.y, hi.z)


def run(p):
    root = design.rootComponent
    found = [(b.name, b.name, b) for b in root.bRepBodies]
    for occ in root.allOccurrences:
        # occ.bRepBodies are proxies in the root's context: world space. The
        # component's own bodies would report component-local coordinates.
        found += [(occ.name + "/" + b.name, b.name, b) for b in occ.bRepBodies]

    wanted = [str(n) for n in (p.get("bodies") or [])]
    if wanted:
        picked, missing = [], []
        for name in wanted:
            hits = [(key, b) for key, plain, b in found if name in (key, plain)]
            if not hits:
                missing.append(name)
            picked += [hit for hit in hits if hit not in picked]
    else:
        picked, missing = [(key, b) for key, _, b in found], []

    boxes, lows, highs = {}, [], []
    for key, b in picked[:CAP]:
        lo, hi = _corners(b)
        boxes[key] = _box(lo, hi)
        lows.append(lo)
        highs.append(hi)
    out = {"bodies": boxes, "missing": missing, "truncated": max(0, len(picked) - CAP)}
    if lows:
        out["overall"] = _box([min(c) for c in zip(*lows)], [max(c) for c in zip(*highs)])
    return out
