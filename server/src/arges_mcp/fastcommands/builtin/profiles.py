"""Every profile of a sketch, with the invariants to pick one by (area, centroid, size), never by index.

args: {"sketch": "Sketch1"}, required: the sketch's name, searched in every
component.
returns: {"sketch", "component", "plane", "profiles": [{"index", "area_mm2",
"centroid_world_mm", "centroid_sketch_mm", "box_sketch_mm": {"min", "max"},
"loops"}]}.
A profile's centroid and box come from Fusion in SKETCH space (verified on
2704: on an XZ sketch, sketch +Y is world -Z). centroid_world_mm is converted
for you; box_sketch_mm is not, so compare it with other sketch coordinates.
Pick the profile in your next call with the same invariant, for example
max(sk.profiles, key=lambda pr: pr.areaProperties().area).
"""
# Runs inside Fusion. Area comes back in cm^2, lengths in cm.
READONLY = True
CAP = 100


def _mm(point):
    # + 0.0 folds -0.0 into 0.0.
    return [round(v * 10, 3) + 0.0 for v in (point.x, point.y, point.z)]


def run(p):
    name = str(p.get("sketch") or "")
    if not name:
        return {"error": "pass {\"sketch\": \"<name>\"}"}
    matches = [(comp, sk) for comp in design.allComponents for sk in comp.sketches
               if sk.name == name]
    if not matches:
        return {"error": "no sketch named " + repr(name)}
    if len(matches) > 1:
        return {"error": "more than one sketch is named " + repr(name),
                "components": [comp.name for comp, _ in matches]}
    comp, sk = matches[0]
    profiles = []
    for i in range(min(sk.profiles.count, CAP)):
        pr = sk.profiles.item(i)
        ap = pr.areaProperties()
        bb = pr.boundingBox
        profiles.append({
            "index": i,
            "area_mm2": round(ap.area * 100, 3),
            "centroid_world_mm": _mm(sk.sketchToModelSpace(ap.centroid)),
            "centroid_sketch_mm": _mm(ap.centroid),
            "box_sketch_mm": {"min": _mm(bb.minPoint), "max": _mm(bb.maxPoint)},
            "loops": pr.profileLoops.count,
        })
    plane = sk.referencePlane
    return {"sketch": sk.name, "component": comp.name,
            "plane": getattr(plane, "name", None) or plane.objectType.split("::")[-1],
            "profiles": profiles, "truncated": max(0, sk.profiles.count - CAP)}
