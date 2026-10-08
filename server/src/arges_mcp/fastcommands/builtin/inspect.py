"""Physical facts about bodies: volume, area, faces, edges, solid or not, where it lives.

args: {"bodies": ["Plate"]}, optional. Omit for every body in the design,
including those inside components (keyed "Occurrence:1/Body").
returns: {"bodies": {name: {"volume_mm3", "area_mm2", "faces", "edges",
"solid", "visible", "component", "size_mm"}}, "missing": [...]}.
Use it to confirm an operation did what you predicted: a shell that should
have opened a face changes "faces"; a cut that missed leaves "volume_mm3" alone.
"""
# Runs inside Fusion. Internal units: cm for length, cm^2 for area, cm^3 for
# volume, so the factors are 10, 100 and 1000.
READONLY = True
CAP = 100


def _facts(body):
    bb = body.boundingBox
    lo, hi = bb.minPoint, bb.maxPoint
    return {
        "volume_mm3": round(body.volume * 1000, 3),
        "area_mm2": round(body.area * 100, 3),
        "faces": body.faces.count,
        "edges": body.edges.count,
        "solid": body.isSolid,
        "visible": body.isVisible,
        "component": body.parentComponent.name,
        "size_mm": [round((h - l) * 10, 3) + 0.0 for l, h in
                    ((lo.x, hi.x), (lo.y, hi.y), (lo.z, hi.z))],
    }


def run(p):
    root = design.rootComponent
    found = [(b.name, b.name, b) for b in root.bRepBodies]
    for occ in root.allOccurrences:
        found += [(occ.name + "/" + b.name, b.name, b) for b in occ.bRepBodies]
    wanted = [str(n) for n in (p.get("bodies") or [])]
    missing = [n for n in wanted if not any(n in (k, plain) for k, plain, _ in found)]
    picked = [(k, b) for k, plain, b in found if not wanted or k in wanted or plain in wanted]
    return {"bodies": {k: _facts(b) for k, b in picked[:CAP]}, "missing": missing,
            "truncated": max(0, len(picked) - CAP)}
