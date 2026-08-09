## Working rules for Claude

These are process rules, not style preferences. Each one exists because skipping it produces work that has to be thrown away.

### 1. Start every session by looking

Before the first modelling call, always:

```
fusion_state()          → doc name, designType, units, timeline count, parameters, bodies
fusion_screenshot("iso")→ actually look at it
```

Never assume the document is empty, or that it's the one from last session, or that it survived a crash intact. Fusion may have recovered a file, the user may have edited by hand, the active document may be a different tab entirely. Reconcile what you see against what you expect *before* touching anything. If they disagree, say so and ask.

Also check `design.designType` early — `adsk.fusion.DesignTypes.ParametricDesignType` (1) vs `adsk.fusion.DesignTypes.DirectDesignType` (0) changes what is legal (see the BaseFeature recipe: temporary BRep bodies can be added directly to a direct-modelling design, but in a parametric design they must go inside a `BaseFeature` — create it with `baseFeatures.add()`, call `startEdit()`, add the bodies through `bRepBodies.add(body, baseFeature)`, then `finishEdit()`).
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design_designType.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DesignTypes.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BaseFeature.htm -->

### 2. Screenshot after EVERY geometry change — and look at it

Not after every three changes. Not "at the end". After **each** operation that creates or modifies geometry.

The cheap way is built in: pass `screenshot="iso"` (or another view) on the same `fusion_execute` call that changes the geometry, and the viewport image arrives together with the result — one round trip, nothing to remember to do afterwards. The capture only happens when the code succeeded; a failed script comes back as a traceback alone. Reach for a standalone `fusion_screenshot` when you want a second angle, a custom size, or a look without running code.

**Never chain modelling steps blind.** A wrong extrude direction, a profile that picked the wrong region, a fillet that consumed the wrong edge — all of these run without raising an exception and all of them silently poison every subsequent step. Ten blind steps means ten steps to unwind instead of one.

The screenshot is not a formality: read it. Is the feature where you predicted? Is the body one piece or two? Did something disappear? If the image doesn't match the mental model, stop and diagnose before adding more.

Profiles deserve their own paranoia: `sketch.profiles` is an ordered collection whose indices are **not** stable or predictable, so `profiles.item(0)` is a guess. Pick by an invariant you can check — `profile.areaProperties().area`, the profile's `boundingBox`, or the number of loops — and remember that `ExtrudeFeatures.addSimple(profile, distance, operation)` accepts either a single `Profile` or an `ObjectCollection` of profiles, while other feature inputs are pickier about which one they want.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_profiles.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatures_addSimple.htm -->

Use more than one view when the shape is ambiguous — `iso` shows form, `front`/`top`/`right` show whether something is actually flush, centred, or floating.

### 3. Predict the bounding box, then verify it

Before a significant operation, state in world coordinates (mm) what you expect the result to be. After it, measure. This catches the whole family of "the geometry exists but is in the wrong place / mirrored / 10× too big" errors that screenshots alone can miss.

```python
def bbox_mm(entity):
    """Bounding box in mm. Internal units are cm, so x10."""
    bb = entity.boundingBox
    lo, hi = bb.minPoint, bb.maxPoint
    return {
        "min":  [round(v * 10, 3) for v in (lo.x, lo.y, lo.z)],
        "max":  [round(v * 10, 3) for v in (hi.x, hi.y, hi.z)],
        "size": [round((h - l) * 10, 3)
                 for h, l in ((hi.x, lo.x), (hi.y, lo.y), (hi.z, lo.z))],
    }

root = design.rootComponent
result = {b.name: bbox_mm(b) for b in root.bRepBodies}
```
<!-- BRepBody.boundingBox → BoundingBox3D: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepBody_boundingBox.htm ; BoundingBox3D has minPoint/maxPoint (Point3D): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BoundingBox3D.htm ; Component.boundingBox "is always in world space of the component", with boundingBox2 for finer control over what is included: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Component_boundingBox.htm -->

For bodies straight off `rootComponent.bRepBodies` this is world space. For a body that lives inside a sub-component, reach it through the occurrence (`occ.bRepBodies`) so you get the proxy — a native body's box is expressed in its own component's coordinates, which is a silent source of "the box is right but in the wrong place". `Component.boundingBox` gives the whole component's extent, and `Component.boundingBox2` lets you control which entity types are included. Because the bridge namespace persists between calls, define `bbox_mm` once and reuse it all session.

If the measured size differs from the prediction by a factor of 10, it's the cm/mm bug. If it differs by a sign on one axis, suspect sketch-plane orientation: a sketch's local X/Y axes do not always line up with the world axes you assumed, so check the plane's actual orientation (`sketch.referencePlane`, `sketch.transform`, or `sketch.sketchToModelSpace`) instead of guessing.

### 4. Never destroy existing geometry without explicit approval

**Ask first, every time**, before:

- `Combine` / boolean join, cut, or intersect against geometry you did not create in this session
- deleting a body, component, sketch, or timeline feature
- editing or deleting an existing user parameter that other features depend on
- rolling the timeline marker back over existing work (`timeline.markerPosition`), or `timeline.deleteAllAfterMarker()`
- changing `design.designType` — per the docs, going from `ParametricDesignType` to `DirectDesignType` "will result in the timeline and all design history being removed and further operations will not be captured in the timeline" <!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design_designType.htm -->

Additive mistakes are cheap; a bad join or a deletion is the hardest thing to unwind, and Fusion's undo does not cover everything a script does. Joining is the *final* step of an assembly, not an incidental one. Present what you intend to combine or remove, and wait.

### 5. Iterate in a scratch document

Generated code can mangle a design — a mis-targeted cut or a wrong-face fillet on real work is expensive. Do exploratory modelling in a scratch document, get it right, then rebuild cleanly in the real one. If the user is already in a design that matters, say so and offer to work in a scratch file instead.

The **timeline is the safety net**: keep the design parametric while iterating so bad features can be rolled back and edited rather than reverse-engineered. Suggest the user saves (Cmd-S) before a large or risky sequence.

### 6. Parameters over hardcoded numbers

Every dimension that could plausibly change — wall thickness, clearances, hole sizes, overall envelope, fillet radii — should be a **user parameter**, and features should reference it by expression (`createByString("wall * 2")`), not by a baked float. Then "make the walls 2 mm" is one parameter edit instead of a rebuild, and the design stays a design rather than a one-shot.

Name parameters descriptively (`wall`, `m3_clear`, `body_len`), and derive dependent values from the base ones so intent survives (`boss_od = m3_insert_id + 2 * wall`). Parameter names in expressions are case-sensitive.

### 7. Build incrementally — small calls, not one big script

Prefer a sequence of short `fusion_execute` calls, each doing one comprehensible thing and returning something verifiable, over a single 200-line script.

Reasons, in order of importance:

1. **A traceback from a 200-line script is far harder to localise.** From a 15-line call, the failure is obvious.
2. A long script that fails halfway leaves the document in a **half-built state** you now have to diagnose and clean up.
3. Screenshotting between steps is only possible if there *are* steps.
4. The namespace persists between calls, so incrementality costs nothing — sketches, bodies, and helper functions from earlier calls are still there by name.

Bridge constraints that make this non-optional: the main thread cannot be interrupted, so a long script that hangs cannot be cancelled; **never write unbounded loops** (bound every loop explicitly and cap the iteration count); **never call `ui.messageBox`, `adsk.doEvents()`, or create/execute a UI command** — a modal dialog deadlocks the bridge and the only recovery is dismissing it in Fusion by hand.

Assign to `result` to return data; `print()` output is captured too. Both are truncated at ~64 KB, so return summaries, not whole entity dumps.

### 8. Re-verify documented patterns after a Fusion update

**Fusion ships breaking API changes mid-cycle.** The reference project's screenshot feature broke when its call to `Viewport.saveAsImageFileWithOptions` stopped matching the shipped signature — every user's screenshots failed with an argument-count error. The currently documented form takes a **single** `SaveImageFileOptions` object, built with `adsk.core.SaveImageFileOptions.create(filename)` and then configured via `.width`, `.height`, `.isAntiAliased`, `.isBackgroundTransparent`. The fix was to fall back to the long-stable 3-argument `Viewport.saveAsImageFile(path, width, height)` (0 for width/height means "use the current viewport size").
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Viewport_saveAsImageFile.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Viewport_saveAsImageFileWithOptions.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SaveImageFileOptions_create.htm -->

Therefore:

- If a pattern from this file raises a `TypeError` about argument counts, or an `AttributeError` on a method that should exist, **suspect a Fusion API change before suspecting your own code** — check the current online API reference for that exact method page.
- After Fusion updates (the user will usually mention it, and `fusion_state` reports the version), treat the first failure of a previously-working pattern as a version issue and verify against the docs.
- When you discover a pattern in this file is stale or wrong, **say so explicitly** and suggest the correction be written back into `FUSION-KNOWLEDGE.md`. A wrong pattern in this file is worse than a missing one — it actively misleads.
- Prefer long-stable API surface (`saveAsImageFile`, `ValueInput.createByString`, `extrudeFeatures.addSimple`) over newer variants when both would work.
