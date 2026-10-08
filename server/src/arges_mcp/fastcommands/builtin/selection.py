"""What is selected right now, including the bodies behind selected faces, edges and vertices.

args: none.
returns: {"items": [{"type", "name" where present, "body", "occurrence",
"size_mm" for bodies}], "bodies": ["Occurrence:1/Body", "RootBody"],
"occurrences": ["Occurrence:1"], "count", "truncated"}.
Body and occurrence lists are de-duplicated; occurrences includes only selected
Occurrence entities. A root body's occurrence is null. Sizes are bounding-box
lengths in millimetres. Items and their lists are capped at 100; count is the
full selection count and truncated is how many items were left out.
Use this when the person says "this", "the one I selected", or "these".
An empty selection means ask what they mean rather than guess.
"""
# Runs inside Fusion. Internal lengths are centimetres, so sizes go out x10.
READONLY = True
CAP = 100


def run(p):
    sel = ui.activeSelections
    items, bodies, occurrences = [], [], []
    for i in range(min(sel.count, CAP)):
        entity = sel.item(i).entity
        item = {"type": entity.objectType.split("::")[-1]}
        name = getattr(entity, "name", None)
        if name is not None:
            item["name"] = name
        body = (entity if entity.objectType == adsk.fusion.BRepBody.classType()
                else getattr(entity, "body", None))
        if body is not None:
            occ = body.assemblyContext
            key = occ.name + "/" + body.name if occ is not None else body.name
            bb = body.boundingBox
            lo, hi = bb.minPoint, bb.maxPoint
            item.update({
                "body": key, "occurrence": occ.name if occ is not None else None,
                "size_mm": [round((h - l) * 10, 3) + 0.0 for l, h in
                            ((lo.x, hi.x), (lo.y, hi.y), (lo.z, hi.z))],
            })
            if key not in bodies:
                bodies.append(key)
        if entity.objectType == adsk.fusion.Occurrence.classType():
            if entity.name not in occurrences:
                occurrences.append(entity.name)
        items.append(item)
    return {"items": items, "bodies": bodies, "occurrences": occurrences,
            "count": sel.count, "truncated": max(0, sel.count - CAP)}
