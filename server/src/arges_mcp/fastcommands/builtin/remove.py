"""Remove bodies or occurrences reversibly, with a Remove feature for each target.

args: {"selection": true} and/or {"bodies": ["asm:1+bin:1/Box", "RootBody"],
"occurrences": ["asm:1+bin:1"]}; "all_instances": true permits shared removals.
Selected faces, edges and vertices count as their body. Body names match
exactly, with full occurrence paths; bare body names are at root. Occurrences
match full paths, or bare names only when unique across all occurrences.
Names matching multiple entities remove NOTHING: {"removed": [], "ambiguous": [{"name",
"matches"}]}. Ask the person, naming the matches; use their selection or an
unambiguous full path. Body matches identify the body and its occurrence (null
at root); occurrence matches are full paths.
Shared bodies and children of shared parent components are refused, because
native removal affects every instance: "shared": [{"what", "instances"}].
Ask the person, naming every instance, before using all_instances=true.
returns: {"removed": [{"what", "feature", "instances" when shared}],
"missing": [names not found], "skipped": [unsupported selected types]}.
Duplicates make one feature. No arguments or an empty selection removes nothing.
To undo: {"undo": ["RemoveBody-Shell"]} deletes that Remove feature in the
component that holds it, restoring the entity; returns {"restored", "missing",
"ambiguous"}. A name in two components restores nothing until given as
{"feature", "component"}. Never undo through design.timeline: for a removal
inside a subcomponent, the timeline item's entity is the parent occurrence.
"""
# Runs inside Fusion. The native body belongs to the component's features;
# passing its occurrence proxy there is not the operation that worked live.
READONLY = False


def _body_name(body):
    occ = body.assemblyContext
    return occ.fullPathName + "/" + body.name if occ is not None else body.name


def _undo(wanted):
    """Delete the named Remove features where they live. All or nothing."""
    components = list(design.allComponents)
    found, missing, ambiguous = [], [], []
    for item in wanted if isinstance(wanted, list) else [wanted]:
        name, owner = ((item.get("feature"), item.get("component"))
                       if isinstance(item, dict) else (item, None))
        name = str(name)
        hits = [(comp, comp.features.removeFeatures.itemByName(name)) for comp in components
                if owner is None or comp.name == owner]
        hits = [(comp, feature) for comp, feature in hits if feature is not None]
        if not hits:
            missing.append(name)
        elif len(hits) > 1:
            ambiguous.append({"feature": name, "components": [comp.name for comp, _ in hits]})
        else:
            found.append((name, hits[0][1]))
    if ambiguous:
        return {"restored": [], "missing": missing, "ambiguous": ambiguous}
    for _, feature in found:
        feature.deleteMe()
    return {"restored": [name for name, _ in found], "missing": missing, "ambiguous": []}


def run(p):
    if p.get("undo"):
        # Undo only, whatever else came with it: a call that both restored
        # and removed would be one nobody could read back with confidence.
        return _undo(p["undo"])
    root = design.rootComponent
    occurrences = list(root.allOccurrences)
    targets, seen_bodies, seen_occurrences = [], [], []
    missing, skipped, shared, ambiguous = [], [], [], []
    named_bodies, named_occurrences = [], []

    wanted = [str(n) for n in (p.get("bodies") or [])]
    if wanted:
        found = [(b.name, b) for b in root.bRepBodies]
        for occ in occurrences:
            found += [(occ.fullPathName + "/" + b.name, b) for b in occ.bRepBodies]
        for name in wanted:
            hits = [(key, b) for key, b in found if key == name]
            if not hits:
                missing.append(name)
            elif len(hits) > 1:
                ambiguous.append({"name": name, "matches": [
                    {"body": key, "occurrence": b.assemblyContext.fullPathName
                     if b.assemblyContext is not None else None} for key, b in hits]})
            else:
                named_bodies.extend(hits)
    for name in (p.get("occurrences") or []):
        name = str(name)
        hits = [occ for occ in occurrences if occ.fullPathName == name or occ.name == name]
        if not hits:
            missing.append(name)
        elif len(hits) > 1:
            ambiguous.append({"name": name, "matches": [occ.fullPathName for occ in hits]})
        else:
            named_occurrences.extend(hits)
    if ambiguous:
        # A valid target elsewhere in the call must not make a guess destructive.
        return {"removed": [], "ambiguous": ambiguous}

    def pick_target(key, entity, component, instances):
        if len(instances) > 1 and p.get("all_instances") is not True:
            shared.append({"what": key, "instances": instances})
        else:
            targets.append((key, entity, component.features.removeFeatures, instances))

    def pick_body(key, body):
        native = body.nativeObject
        if native is None:
            occ = body.assemblyContext
            native = occ.component.bRepBodies.itemByName(body.name) if occ is not None else body
        # Different occurrences can lead to the same native body. One Remove
        # feature in that component already removes it from every instance.
        if native not in seen_bodies:
            seen_bodies.append(native)
            component = native.parentComponent
            instances = [occ.fullPathName for occ in root.allOccurrencesByComponent(component)]
            pick_target(key, native, component, instances)

    def pick_occurrence(occ):
        context = occ.assemblyContext
        native = occ.nativeObject if context is not None else occ
        if native not in seen_occurrences:
            seen_occurrences.append(native)
            component = context.component if context is not None else root
            # Removing a native child changes every instance of its parent,
            # just as removing a native body changes every instance of its owner.
            instances = [parent.fullPathName + "+" + native.name
                         for parent in root.allOccurrencesByComponent(component)]
            pick_target(occ.fullPathName, native, component, instances)

    if p.get("selection"):
        sel = ui.activeSelections
        for i in range(sel.count):
            entity = sel.item(i).entity
            body = (entity if entity.objectType == adsk.fusion.BRepBody.classType()
                    else getattr(entity, "body", None))
            if body is not None:
                pick_body(_body_name(body), body)
            elif entity.objectType == adsk.fusion.Occurrence.classType():
                pick_occurrence(entity)
            else:
                skipped.append(entity.objectType.split("::")[-1])
    for key, body in named_bodies:
        pick_body(key, body)
    for occ in named_occurrences:
        pick_occurrence(occ)

    # Resolve everything before the first mutation: selection proxies and
    # occurrence collections may change as soon as a Remove feature is added.
    removed = []
    for key, entity, features, instances in targets:
        feature = features.add(entity)
        item = {"what": key, "feature": feature.name}
        if len(instances) > 1:
            item["instances"] = instances
        removed.append(item)
    result = {"removed": removed, "missing": missing, "skipped": skipped}
    if shared:
        result["shared"] = shared
    return result
