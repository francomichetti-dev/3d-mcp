"""Remove bodies or top-level occurrences reversibly, with a Remove feature for each target.

args: {"selection": true} for what is selected, and/or
{"bodies": ["Occurrence:1/Body", "RootBody"], "occurrences": ["motor:1"]}.
Selected faces, edges and vertices count as their body; selected Occurrence
entities count as that occurrence, only at top level. Names match exactly:
a bare body name is at root, an occurrence prefix identifies its component.
Bodies are resolved to native bodies and removed in their own component;
removing a shared component's body affects all instances of that component.
Duplicate targets are removed once, even across selection and named arguments.
returns: {"removed": [{"what", "feature"}], "missing": [names not found],
"skipped": [selected types that cannot be removed]}.
This is reversible: delete the Remove feature from the timeline to restore
the body or occurrence. Remove only what the person asked for.
No arguments or an empty selection removes nothing.
"""
# Runs inside Fusion. The native body belongs to the component's features;
# passing its occurrence proxy there is not the operation that worked live.
READONLY = False


def _body_name(body):
    occ = body.assemblyContext
    return occ.name + "/" + body.name if occ is not None else body.name


def run(p):
    root = design.rootComponent
    targets, seen_bodies, seen_occurrences = [], [], []
    missing, skipped = [], []

    def pick_body(key, body):
        native = body.nativeObject
        if native is None:
            occ = body.assemblyContext
            native = occ.component.bRepBodies.itemByName(body.name) if occ is not None else body
        # Different occurrences can lead to the same native body. One Remove
        # feature in that component already removes it from every instance.
        if native not in seen_bodies:
            seen_bodies.append(native)
            targets.append((key, native, native.parentComponent.features.removeFeatures))

    def pick_occurrence(occ):
        if occ not in seen_occurrences:
            seen_occurrences.append(occ)
            targets.append((occ.name, occ, root.features.removeFeatures))

    if p.get("selection"):
        sel = ui.activeSelections
        for i in range(sel.count):
            entity = sel.item(i).entity
            body = (entity if entity.objectType == adsk.fusion.BRepBody.classType()
                    else getattr(entity, "body", None))
            if body is not None:
                pick_body(_body_name(body), body)
            elif (entity.objectType == adsk.fusion.Occurrence.classType()
                  and entity.assemblyContext is None):
                pick_occurrence(entity)
            else:
                skipped.append(entity.objectType.split("::")[-1])

    wanted = [str(n) for n in (p.get("bodies") or [])]
    if wanted:
        found = [(b.name, b) for b in root.bRepBodies]
        for occ in root.allOccurrences:
            found += [(occ.name + "/" + b.name, b) for b in occ.bRepBodies]
        for name in wanted:
            hits = [(key, b) for key, b in found if key == name]
            if not hits:
                missing.append(name)
            for key, body in hits:
                pick_body(key, body)
    for name in (p.get("occurrences") or []):
        name = str(name)
        occ = root.occurrences.itemByName(name)
        if occ is None:
            missing.append(name)
        else:
            pick_occurrence(occ)

    # Resolve everything before the first mutation: selection proxies and
    # occurrence collections may change as soon as a Remove feature is added.
    removed = []
    for key, entity, features in targets:
        feature = features.add(entity)
        removed.append({"what": key, "feature": feature.name})
    return {"removed": removed, "missing": missing, "skipped": skipped}
