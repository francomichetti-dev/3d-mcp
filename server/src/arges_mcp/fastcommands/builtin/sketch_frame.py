"""Where a sketch's own X and Y axes point in the world: read this instead of guessing the plane mapping.

args: {"sketch": "Sketch1"}, optional. Omit for every sketch (up to 30).
returns: {"sketches": [{"name", "component", "plane", "origin_mm", "x_axis",
"y_axis", "normal"}]}. Axes are unit vectors in the component's model space,
which is world space for the root component.
Only XY maps sketch to world unchanged. On XZ sketch +Y is world -Z
(verified on 2704). For anything else, read it here.
"""
# Runs inside Fusion. One sketch unit is 1 cm, so mapping (1, 0, 0) and
# (0, 1, 0) and subtracting the mapped origin gives the unit axes directly.
READONLY = True
CAP = 30


def _vec(a, b):
    # + 0.0 folds -0.0 into 0.0.
    return [round(q - r, 4) + 0.0 for q, r in ((b.x, a.x), (b.y, a.y), (b.z, a.z))]


def _frame(comp, sk):
    P = adsk.core.Point3D.create
    o = sk.sketchToModelSpace(P(0, 0, 0))
    x = _vec(o, sk.sketchToModelSpace(P(1, 0, 0)))
    y = _vec(o, sk.sketchToModelSpace(P(0, 1, 0)))
    n = [round(v, 4) + 0.0 for v in (x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2],
                                      x[0] * y[1] - x[1] * y[0])]
    plane = sk.referencePlane
    return {"name": sk.name, "component": comp.name,
            "plane": getattr(plane, "name", None) or plane.objectType.split("::")[-1],
            "origin_mm": [round(v * 10, 3) + 0.0 for v in (o.x, o.y, o.z)],
            "x_axis": x, "y_axis": y, "normal": n}


def run(p):
    name = str(p.get("sketch") or "")
    frames = []
    for comp in design.allComponents:
        for sk in comp.sketches:
            if name and sk.name != name:
                continue
            frames.append(_frame(comp, sk))
            if len(frames) >= CAP:
                return {"sketches": frames, "truncated": True}
    if name and not frames:
        return {"error": "no sketch named " + repr(name)}
    return {"sketches": frames}
