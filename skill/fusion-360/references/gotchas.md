## Gotchas that will bite you

Each one below is: **symptom → cause → fix.** Read the first two before writing any geometry code; they cause more broken parts than everything else combined.

---

### 1. Units are CENTIMETERS. Angles are RADIANS. (the #1 error)

**Symptom:** you asked for a 20 mm cube and got a 200 mm cube. Or a "45°" chamfer came out as a 45-radian nonsense value / silent failure. Or a fillet radius of `2` blew out the whole body.

**Cause:** Fusion's internal database unit for length is **cm**, and for angles **radians** — *regardless* of the document's display units. A bare number handed to the API is never mm.

> "Values defined by a real are always interpreted to be in the appropriate internal unit. For example, if the value 2 is used to define the depth of an extrusion (a length value), it will be 2 cm because cm is the internal unit for lengths."
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ValueInput_createByReal.htm -->

**Fix — mm → API conversion table (divide by 10):**

| Real world | API value | Real world | API value |
|---|---|---|---|
| 0.2 mm (print clearance) | `0.02` | 20 mm | `2.0` |
| 0.4 mm (nozzle/wall) | `0.04` | 25.4 mm (1") | `2.54` |
| 1 mm | `0.1` | 30 mm | `3.0` |
| 2 mm (wall) | `0.2` | 40 mm | `4.0` |
| 3 mm (M3 shaft) | `0.3` | 50 mm | `5.0` |
| 3.4 mm (M3 clearance) | `0.34` | 100 mm | `10.0` |
| 5 mm | `0.5` | 200 mm | `20.0` |
| 10 mm | `1.0` | 1 m | `100.0` |

| Angle | API value |
|---|---|
| 15° | `0.2618` (`math.radians(15)`) |
| 30° | `0.5236` |
| 45° | `0.7854` |
| 90° | `1.5708` (`math.pi/2`) |
| 180° | `3.14159` (`math.pi`) |

Areas come back in **cm²** (`BRepFace.area` is documented as "the area in cm ^ 2"), volumes in cm³. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepFace.htm -->

Three safe habits:

```python
import math
MM = 0.1                                   # multiply mm by MM to get API units
depth   = 20 * MM                          # 2.0
angle   = math.radians(45)                 # 0.7853981633974483

# Better: let Fusion parse the units for you.
vi_len  = adsk.core.ValueInput.createByString("20 mm")   # explicit, unambiguous
vi_ang  = adsk.core.ValueInput.createByString("45 deg")
vi_raw  = adsk.core.ValueInput.createByReal(2.0)         # == 2 cm == 20 mm

# Need a plain float from a real-world dimension:
um = design.unitsManager
d = um.evaluateExpression("20 mm")        # -> 2.0, always in internal units (cm)
d = um.convert(20, "mm", "cm")            # -> 2.0  (value, fromUnits, toUnits)
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ValueInput_createByString.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UnitsManager_evaluateExpression.htm (doc example: "1cm + 1in" -> 3.54) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UnitsManager_convert.htm -->

`evaluateExpression` takes an optional second argument, the units to evaluate the expression in; it defaults to `"DefaultDistance"`, so `evaluateExpression("1")` depends on the document's distance-unit preference while `evaluateExpression("20 mm")` does not.

Caution on `createByString`: a bare `"6"` is interpreted in the **document's** current units, not cm — always include the unit in the string. Length units cannot be used for an angle value and vice versa (the docs' own example: `"5 in + 3 cm"` supplied as an angle fails because the expression's units define a length).

**Red-flag heuristic:** any length literal above `50` in API units is half a meter. If you typed a millimeter number straight into the API, this catches it.

---

### 2. Parametric vs direct design — THE BASE FEATURE RECIPE

**Symptom:** `Component.bRepBodies.add(body)` returns `None` or raises; a `TemporaryBRepManager` body you built never appears in the design; boolean'd temp geometry silently vanishes. Everything looked right and nothing showed up.

**Cause:** the design is **parametric** (timeline on) — which is what a new Fusion document almost always is. In a parametric design, raw B-Rep bodies cannot be dropped into a component directly; they must be associated with a `BaseFeature`, and in practice you put that base feature into edit mode around the add.

> `BRepBodies.add(body, targetBaseFeature)` — `targetBaseFeature` is "the BaseFeature object that this BRep body will be associated with. This is an optional requirement. It is required in a parametric modeling design but is ignored in a direct modeling design." It returns "the newly created BRepBody or null if the creation failed."
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepBodies_add.htm -->

`BaseFeature.startEdit()` "set[s] the user-interface so that the base body is in edit mode"; pair it with `finishEdit()`. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BaseFeature_startEdit.htm -->

**Fix — always check the design type, then use the recipe:**

```python
root = design.rootComponent
is_parametric = design.designType == adsk.fusion.DesignTypes.ParametricDesignType
print("parametric" if is_parametric else "direct")
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design_designType.htm -->

```python
# THE BASE FEATURE RECIPE — the only way to land temp B-Rep in a parametric design.
tbm = adsk.fusion.TemporaryBRepManager.get()
box = tbm.createBox(adsk.core.OrientedBoundingBox3D.create(
        adsk.core.Point3D.create(0, 0, 1.0),      # centerPoint, cm
        adsk.core.Vector3D.create(1, 0, 0),       # lengthDirection
        adsk.core.Vector3D.create(0, 1, 0),       # widthDirection (must be perpendicular)
        4.0, 3.0, 2.0))                           # length, width, height = 40x30x20 mm

if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
    base = root.features.baseFeatures.add()
    base.startEdit()
    try:
        root.bRepBodies.add(box, base)   # base feature is REQUIRED here
    finally:
        base.finishEdit()                # ALWAYS finish, even on error
    new_body = base.bodies.item(0)       # the parametric RESULT body, valid after finishEdit
else:
    new_body = root.bRepBodies.add(box)  # direct modeling: no base feature
result = new_body.name
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BaseFeature.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BaseFeatures_add.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/TemporaryBRepManager_createBox.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/OrientedBoundingBox3D_create.htm -->

Notes that bite:
- Wrap `finishEdit()` in `finally`. Leaving a base feature stuck in edit state poisons every later call in the persistent bridge namespace — and the next `fusion_execute` will fail for reasons that look unrelated.
- Add **all** the bodies you need between one `startEdit()`/`finishEdit()` pair; don't open a base feature per body.
- The body you hand to `bRepBodies.add()` becomes a **source body**; Fusion "creates a parametric copy when exiting the base feature, called the 'result body.'" `BaseFeature.sourceBodies` returns the bodies owned by the base feature; `BaseFeature.bodies` returns the bodies created or modified by the feature. Don't assume the object returned by `add()` is the one that shows up in the timeline — re-fetch after `finishEdit()`. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BaseFeature.htm -->
- **Never** flip `design.designType` to `DirectDesignType` to dodge this: "Changing an existing design from ParametricDesignType to DirectDesignType will result in the timeline and all design history being removed and further operations will not be captured in the timeline." It is destructive for Franco's model. Use the recipe instead.
- Prefer real timeline features (`extrudeFeatures`, `revolveFeatures`, `combineFeatures`) over temp B-Rep whenever the shape can be modeled — they stay editable and parametric. Reach for `TemporaryBRepManager` only when there's no feature that does the job.

---

### 3. Collections, arrays, and `ObjectCollection` are three different things

**Symptom (a):** you write clumsy `count`/`item()` index loops because you assume `for f in body.faces` won't work — or you call `.append()` on something the API returned and get `AttributeError`.
**Symptom (b):** a feature-input method rejects the Python list you passed.

**Cause:** the API has *collections* (`count` / `item()`, plus Python sugar), *arrays* returned from methods as a "vector" object, and `ObjectCollection` (a real API type used as an input container). They are not interchangeable.

**Fix:**

```python
bodies = root.bRepBodies
n = bodies.count            # both work
n = len(bodies)             # Fusion's Python wrapper supports len()
b = bodies.item(0)          # both work
b = bodies[0]               # and [-1], and slices like bodies[:2] / bodies[1:4]
for b in bodies:            # standard iteration is supported
    print(b.name)
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/PythonSpecific_UM.htm — the docs explicitly support [], len(), iteration and slicing on collections; do NOT claim .item()/.count are mandatory -->

But **arrays returned from methods are "vector" objects, not lists**: "it's not returned as a standard Python List but is a special API-specific object called 'vector'… you can't use append to add items." They iterate fine; convert if you need list behaviour:

```python
ents = list(design.findEntityByToken(tok))   # findEntityByToken returns an ARRAY (Base[])
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/PythonSpecific_UM.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design_findEntityByToken.htm -->

And where the docs say the parameter type is `ObjectCollection`, build one — do not rely on a Python list being accepted:

```python
coll = adsk.core.ObjectCollection.create()
for f in extrude.endFaces:
    coll.add(f)
# or in one step from a Python list:
coll = adsk.core.ObjectCollection.createWithArray([face_a, face_b])
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ObjectCollection.htm (create, createWithArray, add, asArray, item, contains, removeByIndex, removeByItem, clear, find) -->

Rule of thumb for "one entity or a collection?" — check the doc for that exact method. Example: `ExtrudeFeatures.createInput(profile, operation)` — "the profile argument can be a single Profile, a single planar face, a single SketchText object, or an ObjectCollection consisting of multiple profiles, planar faces, and sketch texts" (which must be co-planar). <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatures_createInput.htm -->

---

### 4. `booleanOperation` takes ONE body, not a collection

**Symptom:** `TemporaryBRepManager.booleanOperation(...)` throws, or you pass an `ObjectCollection` of tools and nothing happens.

**Cause:** two different boolean APIs with different shapes, and it's easy to cross them.

**Fix:**

```python
# TEMP bodies — one tool at a time, target is MUTATED IN PLACE, returns bool.
tbm = adsk.fusion.TemporaryBRepManager.get()
target = tbm.copy(some_temp_body)              # keep an explicit target you own
tool_bodies = [t1, t2]                         # temp bodies you built earlier
for tool in tool_bodies:                       # loop; there is no collection overload
    ok = tbm.booleanOperation(target, tool, adsk.fusion.BooleanTypes.DifferenceBooleanType)
    if not ok:
        raise RuntimeError("boolean failed — bodies probably don't intersect")
# target is now the result; no new body is returned.
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/TemporaryBRepManager_booleanOperation.htm — signature booleanOperation(targetBody, toolBody, booleanType), returns Boolean -->

- `targetBody` **must be a temporary body** (it is the one that gets modified); `toolBody` is left unmodified.
- `BooleanTypes` values are exactly `UnionBooleanType`, `DifferenceBooleanType`, `IntersectionBooleanType`. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BooleanTypes.htm -->

For **real** (persisted, timeline) bodies use a Combine feature instead — *that* one takes a collection:

```python
tools = adsk.core.ObjectCollection.create()
tools.add(tool_body)
ci = root.features.combineFeatures.createInput(target_body, tools)   # (BRepBody, ObjectCollection)
ci.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
root.features.combineFeatures.add(ci)
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/CombineFeatures_createInput.htm -->

---

### 5. `sketch.profiles.count == 0` — nothing to extrude

**Symptom:** the sketch looks fine on screen, but `sketch.profiles.count` is 0, and `createInput` fails with an unhelpful error.

**Cause:** Fusion only computes a profile for a **closed, non-self-intersecting, planar region**. `Sketch.profiles` returns the profiles currently computed for the sketch — if no closed region exists, the collection is legitimately empty. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_profiles.htm -->

**Fix — triage in this order:**

1. **Open contour.** Endpoints that look coincident but aren't (floating-point gaps) leave the loop open. Reuse the *same* `Point3D`/endpoint objects, or add coincident constraints, rather than re-creating points with the same coordinates.
2. **Self-intersecting / doubled curves.** Two identical lines drawn on top of each other, or a shape that crosses itself, produces no profile (or bizarre extra profiles).
3. **Not coplanar with the sketch plane.** Geometry added in 3D, projected geometry from another plane, or curves lying off the sketch plane never form a profile.
4. **Wrong sketch.** In the persistent bridge namespace it's easy to hold a stale `sketch` variable from a previous call. Don't rely on `activeEditObject` or the current selection — keep and use the explicit sketch object you created, or re-fetch `comp.sketches.item(comp.sketches.count - 1)`.

Always assert before you build a feature:

```python
if sketch.profiles.count == 0:
    result = "NO PROFILE: %d curves, sketch '%s' — contour is open, self-intersecting, or off-plane" % (
        sketch.sketchCurves.count, sketch.name)
else:
    prof = sketch.profiles.item(0)   # if count > 1, pick deliberately (largest area, etc.)
```

If you genuinely *want* an open path (a surface extrude, a sweep path), don't fight it — build an open profile explicitly and turn off solid:

```python
open_prof = root.createOpenProfile(sketch.sketchCurves.item(0))   # chainCurves defaults True
ext_in = root.features.extrudeFeatures.createInput(
    open_prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_in.isSolid = False
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Component_createOpenProfile.htm — createOpenProfile(curves, chainCurves=True); curves may be a SketchCurve or an ObjectCollection, and chainCurves is ignored (treated as false) when multiple curves are input ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatures_createInput.htm -->

Related: a **revolve** profile must lie entirely on one side of the axis — touching the axis is fine, crossing it is rejected.

---

### 6. Entity references go stale (the "object is invalid" class of error)

**Symptom:** a variable that worked in the last `fusion_execute` call now raises a `RuntimeError` mentioning an invalid object — or a property read returns garbage. Very common in this bridge, because the namespace persists across calls and it's tempting to keep references around.

**Cause:** deleting a feature, rolling the timeline marker, editing an earlier feature, or the user touching the model in the UI invalidates existing references. Every Fusion object carries `isValid`:

> "Indicates if this object is still valid, i.e. hasn't been deleted or some other action done to invalidate the reference."
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepBody_isValid.htm -->

(The exact exception text varies by object and Fusion version — don't match on the string. `isValid` is the reliable, documented check.)

**Fix — never carry a reference across a modifying operation without re-validating:**

```python
if not (body and body.isValid):
    body = root.bRepBodies.itemByName("Body1")     # re-look-up by NAME
```

Names and `itemByName` are the cheapest stable handle. For faces/edges, use an **entity token**, which is designed exactly for this:

```python
tok = face.entityToken                              # save it
# ... later, after other features ...
matches = list(design.findEntityByToken(tok))       # returns an ARRAY, possibly >1
face = matches[0] if matches else None              # first is "the most logical match"
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepFace_entityToken.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design_findEntityByToken.htm -->

Token caveats straight from the docs: "the token string returned for a specific entity can be different over time" and you should "never compare entity tokens as [a] way to determine what the token represents" — resolve both tokens with `findEntityByToken` and compare the entities instead. A token can also resolve to *several* entities if a later feature split the original face; the docs say "all of the faces that represent the original face will be returned with the first face being the most logical match to the original." Tokens only work on non-temporary entities (`isTemporary == False`).

Also: if you roll the timeline back (`design.timeline.markerPosition = n`), features after the marker do not exist — re-query after `design.timeline.moveToEnd()`. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Timeline.htm — markerPosition (read/write), moveToEnd(), moveToBeginning(), deleteAllAfterMarker() -->

---

### 7. Face and edge INDICES SHIFT after every feature

**Symptom:** "fillet the top edge" worked; you add one more extrude, re-run the same code, and now it fillets an inside corner. Or grip ridges end up floating in space. This is the classic silent wrong-geometry bug — the code doesn't error, it just builds the wrong part.

**Cause:** `body.faces.item(3)` is **not** a stable identifier. Every modifying feature can renumber, split, merge, or delete faces and edges. An index that was correct one line ago may be a different face now.

**Fix — three rules, in order of preference:**

1. **Never cache an index across a feature.** Re-query the collection immediately before you use it.
2. **Get faces from the feature that made them**, not from a body-wide index:
   ```python
   ext = root.features.extrudeFeatures.add(ext_in)
   start = ext.startFaces.item(0)    # faces coincident with the sketch plane
   top   = ext.endFaces.item(0)      # faces capping the far end, opposite the start faces
   sides = ext.sideFaces             # all faces running perpendicular to the extrude direction
   ```
   <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeature.htm — startFaces / endFaces / sideFaces all exist -->
3. **Identify geometrically** — by centroid position, radius, area, or surface normal — never by remembered position in a list:
   ```python
   # "the topmost planar face" — robust across feature additions
   top = None
   for f in body.faces:
       if isinstance(f.geometry, adsk.core.Plane):
           if top is None or f.centroid.z > top.centroid.z:
               top = f

   # "the 8 mm-diameter bore faces" — match the underlying geometry, not an index or an area
   bores = [f for f in body.faces
            if isinstance(f.geometry, adsk.core.Cylinder)
            and abs(f.geometry.radius - 0.4) < 0.01]   # 0.4 cm radius = 8 mm diameter
   ```
   <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepFace_centroid.htm ("a point at the centroid (aka, geometric center) of the face", a Point3D) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Cylinder.htm (radius / origin / axis) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepFace.htm (area "in cm ^ 2") -->

   Prefer `geometry.radius` over `face.area` for round features: the area of a cylindrical face depends on its height too, so an area test silently matches the wrong bore when depths differ.

Same rule for deletion: deleting from a collection shifts every later index. Delete highest-index-first, or address by name.

And verify after the fact — `body.boundingBox.minPoint` / `.maxPoint` (in cm) is the cheapest way to confirm a feature landed where you predicted. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepBody_boundingBox.htm -->

---

### 8. Occurrence vs Component vs proxy

**Symptom:** you moved a component and the part didn't move. Or a body's coordinates are right in its own component but wrong in the assembly. Or you edited one instance and all of them changed.

**Cause:** a **Component** is the definition (geometry, sketches, features — it has no position). An **Occurrence** is one placed instance of that component in a parent, and it carries the transform. Bodies fetched from a component are in *component* space; the same body reached through an occurrence is a **proxy** that reports assembly-context values.

> `Occurrence.bRepBodies`: "Returns the body proxies for the B-Rep bodies in the component referenced by this occurrence… the bodies returned will return information in the context of the root component, not the component they actually exist in."
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrence.htm -->

**Fix:**

```python
occ  = root.occurrences.item(0)
comp = occ.component            # the definition — editing this affects EVERY instance

# Position/orientation lives on the OCCURRENCE, not the component:
occ.transform2 = some_matrix3d  # moving an instance (Matrix3D, read/write)

# Coordinates in assembly space -> go through the occurrence:
body_in_assembly = occ.bRepBodies.item(0)         # already a proxy
# or promote a native object you already hold:
proxy = native_body.createForAssemblyContext(occ)
```
<!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrence_transform2.htm ("Gets and sets the 3d matrix data that defines this occurrences orientation and position in its assembly context. This property replaces the transform property, which has been retired…") ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepBody_createForAssemblyContext.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrence.htm -->

- Use `transform2`, not the retired `transform`.
- Call `createForAssemblyContext` on a **native** object; it returns null on failure and is not meaningful for transient/temporary bodies. If you already hold a proxy, go the other way with `proxy.nativeObject`; `proxy.assemblyContext` gives you the occurrence back.
- Use `occ.fullPathName` (e.g. `"Sub1:1+PartA:1"`) when reporting what you touched — component names alone are ambiguous across instances.
- Build geometry centered at the origin in its own component, then position the **occurrence**. Baking assembly offsets into sketch coordinates makes everything downstream harder.

---

### 9. Bridge rules — things that will hang Fusion, not just fail

These are not Fusion API gotchas; they are hard constraints of *this* bridge. Violating them requires Franco to manually restart the add-in (or Fusion), and can cost unsaved work.

**Never open a modal dialog.**
- **Symptom:** the tool call times out with "code may still be executing", and every later call returns 409. Fusion sits there with an invisible-to-you dialog waiting for a click.
- **Cause:** your code runs on Fusion's main thread via a custom event. A modal dialog blocks that thread and stalls the event queue the bridge depends on — classic deadlock.
- **Fix:** **never** call `ui.messageBox(...)`, never create or execute a UI command (`CommandDefinitions`, `commandDefinition.execute()`, `ui.inputBox`, file dialogs), and never trigger anything that prompts. Use `print()` for messages and assign to `result` to return data — both are captured by the bridge.

**Never call `adsk.doEvents()`.**
- **Symptom:** intermittent hangs or the same job appearing to run twice.
- **Cause:** `doEvents` "temporarily halts the execution of the add-in or script and gives Fusion a chance to handle any queued up messages" — inside the bridge's own event handler that means re-entrancy into the very queue that is dispatching you. <!-- verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/PythonSpecific_UM.htm -->
- **Fix:** don't. `fusion_screenshot` already handles viewport refresh on the bridge's own terms.

**Never write an unbounded loop.**
- **Symptom:** the call times out, the bridge stays busy forever, Fusion's UI is frozen and cannot be interrupted.
- **Cause:** main-thread execution **cannot be cancelled**. The bridge's timeout abandons the *wait*, not the code. There is no Ctrl-C.
- **Fix:** every loop gets a hard, small iteration bound. No `while True`. No unbounded retry loops. No `time.sleep` beyond a few hundred ms. Bound work by a literal count you computed up front:
  ```python
  MAX_HOLES = 64
  for i, pt in enumerate(points[:MAX_HOLES]):
      ...
  ```

**Chunk long work across calls.**
- **Symptom:** a 200-feature generation times out at ~60 s, and you have no idea how far it got.
- **Fix:** the namespace **persists between `fusion_execute` calls** — exploit it. Do 10–20 features per call, `print()` progress, keep your state in module-level variables, and screenshot between chunks to verify before continuing. A timed-out call is far more expensive than three small ones.

**On timeout, do not resend.** The code may still be running. Call `fusion_state` / `fusion_screenshot` to see what actually landed, then continue from there — re-running a partially-applied script duplicates geometry.

---

### 10. `camera.viewOrientation` does not survive assignment — position the camera instead

**Verified against Fusion 2704.1.36, not inferred from docs.** Setting a named
view the obvious way silently does nothing:

```python
cam = vp.camera
cam.viewOrientation = adsk.core.ViewOrientations.FrontViewOrientation
print(cam.viewOrientation)      # FrontViewOrientation — the copy took it
vp.camera = cam
print(vp.camera.viewOrientation)  # ArbitraryViewOrientation — reverted!
```

The assignment reverts it. No exception, no warning — you simply get whatever
view was already on screen, which is worse than an error because a "front"
screenshot that is actually isometric looks plausible.

Position `eye` / `target` / `upVector` explicitly instead:

```python
import math

DIRS = {                       # (direction to look FROM, up vector)
    "front": ((0, -1, 0), (0, 0, 1)),
    "top":   ((0,  0, 1), (0, 1, 0)),
    "right": ((1,  0, 0), (0, 0, 1)),
    "iso":   ((1, -1, 1), (0, 0, 1)),
}

vp = app.activeViewport
bb = design.rootComponent.boundingBox
cx, cy, cz = ((bb.minPoint.x + bb.maxPoint.x) / 2,
              (bb.minPoint.y + bb.maxPoint.y) / 2,
              (bb.minPoint.z + bb.maxPoint.z) / 2)
span = max(bb.maxPoint.x - bb.minPoint.x,
           bb.maxPoint.y - bb.minPoint.y,
           bb.maxPoint.z - bb.minPoint.z) or 1.0

direction, up = DIRS["front"]
n = math.sqrt(sum(c * c for c in direction))
d = span * 5

cam = vp.camera
cam.target = adsk.core.Point3D.create(cx, cy, cz)
cam.eye = adsk.core.Point3D.create(cx + direction[0] / n * d,
                                   cy + direction[1] / n * d,
                                   cz + direction[2] / n * d)
cam.upVector = adsk.core.Vector3D.create(*up)
cam.isSmoothTransition = False   # else the capture lands mid-animation
vp.camera = cam
vp.fit()                          # preserves direction, fixes framing
vp.refresh()
```

`fusion_screenshot` already does this for you — this matters when you drive the
camera yourself. Sanity check: two different named views must produce different
images. If two views come back byte-identical, the camera never moved.

---

### 11. Export filenames: Fusion may append its own extension

**Verified on Fusion 2704.1.36.** `createUSDExportOptions` is present (there is
**no glTF exporter** — do not look for one), and it takes the **filename first**:
`em.createUSDExportOptions(path)`. But exporting to `part.usd` actually writes
**`part.usd.usdz`** — a zip archive containing a `.usdc` crate.

So `os.path.exists(requested_path)` is `False` after a *successful* export. Never
treat that as failure: check the requested path, then the same path with `.usdz`
appended, and report whichever exists.

STL is unaffected — `em.createSTLExportOptions(geometry, filename)` takes geometry
first and writes exactly the filename given, as **binary** STL in **millimetres**
(a 20 mm cube exports as 12 triangles spanning 20.000).

The full exporter list on 2704.1.36: `createC3MFExportOptions`,
`createDXFFlatPatternExportOptions`, `createDXFSketchExportOptions`,
`createFusionArchiveExportOptions`, `createIGESExportOptions`,
`createOBJExportOptions`, `createSATExportOptions`, `createSMTExportOptions`,
`createSTEPExportOptions`, `createSTEPExportOptionsForFlatPattern`,
`createSTLExportOptions`, `createUSDExportOptions`.
