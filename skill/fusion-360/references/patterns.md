## Core patterns

Copy-adapt library. Every snippet assumes the bridge contract: `adsk`, `app`, `ui`, `design` are already injected — **do not re-import or re-resolve them** — `print()` is captured, and assigning to `result` returns a value. The namespace persists between calls, so a variable from an earlier call is still there. Snippets below reuse names (`root`, `sk`, `prof`, `body`) from the snippet above them; wire those up before running one in isolation.

**Two rules that apply to every snippet below:**
- Raw numbers are **centimeters** and **radians**. `20 mm → 2.0`. `90° → math.pi/2`.
- Autodesk's own doc samples wrap everything in `try/except: ui.messageBox(...)`. **Never copy that part** — a modal dialog deadlocks the bridge. Let the exception propagate; the bridge returns the traceback.

### Root component, and a new component/occurrence

```python
root = design.rootComponent          # adsk.fusion.Component

# A new component = a new occurrence of a new component in root.
transform = adsk.core.Matrix3D.create()               # static; returns an identity matrix
transform.translation = adsk.core.Vector3D.create(5.0, 0, 0)   # 50 mm in +X
occ = root.occurrences.addNewComponent(transform)
comp = occ.component
comp.name = "Housing"

result = {"comp": comp.name, "occ": occ.fullPathName}
```

Build geometry on `comp.sketches` / `comp.features` so it lands inside the component. `Occurrence.transform` is **retired** — use `occ.transform2` to read/move an existing occurrence.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrences_addNewComponent.htm (addNewComponent(transform: Matrix3D) -> Occurrence) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrence.htm (transform RETIRED, transform2 is the replacement; fullPathName) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Matrix3D.htm (create() is static and returns identity; translation is get/set) -->

### Sketch on a base plane

```python
root = design.rootComponent
sk = root.sketches.add(root.xYConstructionPlane)   # or xZConstructionPlane / yZConstructionPlane
```

Construction geometry on any component: `xYConstructionPlane`, `xZConstructionPlane`, `yZConstructionPlane`, `xConstructionAxis`, `yConstructionAxis`, `zConstructionAxis`, `originConstructionPoint`.

Only the XY plane gives you a sketch space that lines up with the model axes the way you would guess. On the XZ and YZ planes the sketch's own 2D axes are **not** simply "model X and model Z" — signs and axis order differ, which is the usual cause of a part that comes out mirrored or pointing the wrong way. Do not guess the mapping: create the sketch, then check it with `sk.sketchToModelSpace(adsk.core.Point3D.create(1, 0, 0))` and `(0, 1, 0)` and print the results before committing to coordinates. If you don't need a specific plane, sketch on XY and move the resulting body.

### Sketch on a planar face

```python
body = root.bRepBodies.item(0)

# Pick the face deliberately — never by remembered index. Here: the highest planar face.
face = max(
    (f for f in body.faces if f.geometry.objectType == adsk.core.Plane.classType()),
    key=lambda f: f.boundingBox.maxPoint.z,
)
sk = root.sketches.add(face)

# Face sketches have their OWN 2D coordinate system. Convert a world point into it:
p_sketch = sk.modelToSketchSpace(adsk.core.Point3D.create(1.0, 2.0, 3.0))
print(p_sketch.x, p_sketch.y, p_sketch.z)   # z is ~0 for a point on the plane
```

`Sketches.add(planarEntity, occurrenceForCreation=None)` — `planarEntity` is a construction plane or planar face; the second argument is required only when the planar entity lives in another component **and** the sketch is not in the root component. Point coordinates you pass to `sketchCurves` are **sketch-space**, not world-space; on a face sketch these differ. `sk.sketchToModelSpace(pt)` is the inverse.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketches_add.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch.htm (modelToSketchSpace / sketchToModelSpace, both "sensitive to the assembly context") -->

### Rectangles, circles, lines

```python
sk = root.sketches.add(root.xYConstructionPlane)
lines   = sk.sketchCurves.sketchLines
circles = sk.sketchCurves.sketchCircles

# 40 x 30 mm rectangle by two opposite corners -> returns a SketchLineList of the 4 new lines
rect = lines.addTwoPointRectangle(
    adsk.core.Point3D.create(0, 0, 0),
    adsk.core.Point3D.create(4.0, 3.0, 0),
)

# Rectangle centered on a point: addCenterPointRectangle(centerPoint, cornerPoint) -> SketchLineList
# A 5 mm-radius circle. NOTE: radius is a plain float in cm, NOT a ValueInput.
circles.addByCenterRadius(adsk.core.Point3D.create(2.0, 1.5, 0), 0.5)

# A single line (endpoints may be Point3D or existing SketchPoint objects)
axis = lines.addByTwoPoints(
    adsk.core.Point3D.create(0, -0.5, 0),
    adsk.core.Point3D.create(4.0, -0.5, 0),
)
```

Corner/center points of the rectangle helpers may also be existing `SketchPoint` objects, in which case the lines stay attached to them. Sketch-point z is 0 in a sketch plane's own space. For many curves, wrap creation in `sk.isComputeDeferred = True` … `sk.isComputeDeferred = False` to avoid recomputing profiles after every add.
<!-- addTwoPointRectangle: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchLines_addTwoPointRectangle.htm (pointOne, pointTwo: SketchPoint or Point3D -> SketchLineList) ; addCenterPointRectangle: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchLines_addCenterPointRectangle.htm ; addByCenterRadius (radius is a double in cm): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchCircles_addByCenterRadius.htm ; addByTwoPoints: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchLines_addByTwoPoints.htm -->

### Selecting a profile (and the count==0 / count>1 triage)

`sketch.profiles` returns the profiles currently computed for the sketch — closed regions only, recomputed by Fusion after the sketch changes. Never assume `item(0)` is the one you want.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch.htm (profiles: "Returns the profiles currently computed for the sketch") -->

```python
n = sk.profiles.count
print("profiles:", n)

if n == 0:
    raise RuntimeError("no closed profile — check for open contours / unjoined endpoints")

# Deterministic pick: largest area (database units, so cm^2).
prof = max(sk.profiles, key=lambda p: p.areaProperties().area)

# Or all of them at once (extrude/revolve accept an ObjectCollection of co-planar profiles):
profs = adsk.core.ObjectCollection.create()
for p in sk.profiles:
    profs.add(p)
```

**`profiles.count == 0` triage, in order:** (1) the contour is not closed — endpoints look coincident but are not joined; (2) you drew on the wrong sketch, or the sketch got replaced by a later call; (3) coordinates were scaled wrong (a "20" you meant as mm is 200 mm; a "0.02" collapses below tolerance); (4) you are looking at a stale `Profile` you grabbed before the last edit — re-read `sk.profiles` after every change to the sketch.

**`profiles.count > 1`:** overlapping/self-intersecting curves, or an inner loop split the region. Print each candidate before choosing:
```python
result = [{"i": i, "area": sk.profiles.item(i).areaProperties().area} for i in range(sk.profiles.count)]
```
<!-- Profile.areaProperties(accuracy=CalculationAccuracy.LowCalculationAccuracy, +/-1%): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Profile_areaProperties.htm -->

### Extrude — the simple path

```python
# `prof` from the profile-selection snippet above.
extrudes = root.features.extrudeFeatures
ext_in = extrudes.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)

# Preferred, current API: extent definition + explicit direction.
dist = adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByString("15 mm"))
ext_in.setOneSideExtent(dist, adsk.fusion.ExtentDirections.PositiveExtentDirection)

ext = extrudes.add(ext_in)
body = ext.bodies.item(0)
result = {"body": body.name, "faces": body.faces.count}
```

`createInput(profile, operation)` — `profile` may be a single `Profile`, a planar `BRepFace`, a `SketchText`, or an `ObjectCollection` of those, and when a collection is used everything in it must be co-planar. `setOneSideExtent(extent, direction, taperAngle=None)` takes an optional third `ValueInput` taper angle. `ExtentDirections` is `PositiveExtentDirection` (same direction as the profile's parent sketch-plane normal) / `NegativeExtentDirection` / `SymmetricExtentDirection`.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatures_createInput.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatureInput_setOneSideExtent.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DistanceExtentDefinition_create.htm (static, takes one ValueInput) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtentDirections.htm -->

For a surface rather than a solid, build the profile with `comp.createOpenProfile(...)` / `comp.createBRepEdgeProfile(...)` and set `ext_in.isSolid = False`.

Symmetric about the sketch plane:
```python
ext_in.setSymmetricExtent(adsk.core.ValueInput.createByString("15 mm"), True)  # True = distance IS the full length
```
`setSymmetricExtent(distance, isFullLength, taperAngle=None)` — with `isFullLength=False` the distance is measured per side, i.e. half the final length.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatureInput_setSymmetricExtent.htm -->

**`setDistanceExtent(isSymmetric, distance)` still works but was retired in Sept 2022** ("replaced with the new SetOneSideExtent and passing in either a DistanceExtentDefinition or SymmetricExtentDefinition"). You will see it in every Autodesk sample and most forum code. Reading it is fine; prefer `setOneSideExtent` in new code.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatureInput_setDistanceExtent.htm -->

### Extrude — join / cut / intersect

The operation is chosen at `createInput` time. The five values of `adsk.fusion.FeatureOperations` are exactly:

`NewBodyFeatureOperation` · `JoinFeatureOperation` · `CutFeatureOperation` · `IntersectFeatureOperation` · `NewComponentFeatureOperation`
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/FeatureOperations.htm -->

```python
# `pocket_prof` is a profile you selected; `body` is the existing body to cut into.
cut_in = extrudes.createInput(pocket_prof, adsk.fusion.FeatureOperations.CutFeatureOperation)
cut_in.setOneSideExtent(
    adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByString("5 mm")),
    adsk.fusion.ExtentDirections.NegativeExtentDirection,   # into the material
)
# Cut/intersect: if not set, EVERY body intersected by the feature participates.
cut_in.participantBodies = [body]
extrudes.add(cut_in)
```

If a cut "does nothing", the usual cause is the wrong `ExtentDirections` — it extruded away from the material. Screenshot and check before adding more features.
<!-- participantBodies (applies to cut/intersect; "If this property has not been set, the default behavior is that all bodies that are intersected by the feature will participate"): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatureInput_participantBodies.htm -->

### Revolve

```python
sk = root.sketches.add(root.xYConstructionPlane)
lines = sk.sketchCurves.sketchLines
# Profile entirely on one side of the axis line (touching the axis is OK; crossing is NOT).
lines.addTwoPointRectangle(adsk.core.Point3D.create(1.0, 0, 0), adsk.core.Point3D.create(2.0, 4.0, 0))
axis_line = lines.addByTwoPoints(adsk.core.Point3D.create(0, 0, 0), adsk.core.Point3D.create(0, 4.0, 0))

prof = max(sk.profiles, key=lambda p: p.areaProperties().area)

revolves = root.features.revolveFeatures
rev_in = revolves.createInput(prof, axis_line, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
rev_in.setAngleExtent(False, adsk.core.ValueInput.createByString("360 deg"))   # or createByReal(2*math.pi)
rev = revolves.add(rev_in)
```

- `createInput(profile, axis, operation)`. `profile` may be a `Profile`, a planar face, or an `ObjectCollection` of them. `axis` may be a sketch line, construction axis, linear edge, or an axis-defining face (cylinder/cone/torus); if it is not in the same plane as the profile it is projected onto the profile plane.
- `setAngleExtent(isSymmetric, angle)` — `angle` is a `ValueInput`. With `isSymmetric=True` the revolve is symmetric about the profile plane and the angle applies to each side; with `False` a signed angle sets a one-directional revolve. `createByReal` here means **radians** (`math.pi` = half turn); `createByString("360 deg")` is safer to read.
- **The profile must not cross the axis** — Fusion rejects the feature. A full circle centered on the axis fails; a half-circle with its flat side on the axis works. This is the #1 revolve failure.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/RevolveFeatures_createInput.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/RevolveFeatureInput_setAngleExtent.htm ; sample using createByReal(math.pi): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SimpleRevolveFeatureSample_Sample.htm -->

### Fillet

Edges always arrive as an `adsk.core.ObjectCollection`.

```python
edges = adsk.core.ObjectCollection.create()
for e in body.edges:
    if e.length > 0.9:          # cm — filter deliberately, don't trust edge indices
        edges.add(e)

fillets = root.features.filletFeatures
f_in = fillets.createInput()                        # takes no arguments
f_in.edgeSetInputs.addConstantRadiusEdgeSet(
    edges,                                           # ObjectCollection of BRepEdge / BRepFace / Feature
    adsk.core.ValueInput.createByString("2 mm"),
    True,                                            # isTangentChain
)
fillets.add(f_in)
```

`addConstantRadiusEdgeSet(entities, radius, isTangentChain)` returns a `ConstantRadiusFilletEdgeSetInput` you can tune further. On `FilletFeatureInput` itself, `addConstantRadiusEdgeSet`, `addVariableRadiusEdgeSet`, `addChordLengthEdgeSet`, `isTangentChain` and `isG2` are all **retired** — the current path is via `f_in.edgeSetInputs`. `isRollingBallCorner` and `targetBaseFeature` remain on the input. Older samples use the retired form.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/FilletFeatures_createInput.htm (no arguments) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/FilletFeatureInput.htm (add*EdgeSet / isTangentChain / isG2 marked RETIRED) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/FilletEdgeSetInputs_addConstantRadiusEdgeSet.htm -->

### Chamfer

```python
edges = adsk.core.ObjectCollection.create()
edges.add(body.edges.item(0))

chamfers = root.features.chamferFeatures
c_in = chamfers.createInput2()                       # createInput2, not createInput — takes no arguments
c_in.chamferEdgeSets.addEqualDistanceChamferEdgeSet(
    edges,
    adsk.core.ValueInput.createByString("1 mm"),
    True,                                            # isTangentChain
)
chamfers.add(c_in)
```

On `ChamferFeatureInput`, the `edges` / `isTangentChain` properties and the `setToEqualDistance` / `setToTwoDistances` / `setToDistanceAndAngle` methods are all **retired** — go through `chamferEdgeSets`. `addEqualDistanceChamferEdgeSet(edges, distance, isTangentChain)` returns a boolean, not an edge-set object; `edges` may hold `BRepEdge`, `BRepFace` or `Feature` objects.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ChamferFeatures_createInput2.htm (no arguments) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ChamferFeatureInput.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ChamferEdgeSets_addEqualDistanceChamferEdgeSet.htm ; sample: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/EqualDistanceChamferFeature_Sample.htm -->

### Holes

A hole needs **three** things set: diameter (at `createXInput`), a position, and an extent.

```python
holes = root.features.holeFeatures

# --- Simple hole, positioned by a point projected onto a planar face ---
# top_face: a planar BRepFace you selected deliberately (see "Sketch on a planar face").
h_in = holes.createSimpleInput(adsk.core.ValueInput.createByString("3.4 mm"))   # M3 clearance
h_in.setPositionByPoint(top_face, adsk.core.Point3D.create(1.0, 1.0, 1.5))      # model-space Point3D
h_in.setAllExtent(adsk.fusion.ExtentDirections.NegativeExtentDirection)          # through-all
holes.add(h_in)

# --- Counterbore, positioned by sketch points (best for a repeatable pattern) ---
# `sk` is a sketch on the face you are drilling into.
pts = adsk.core.ObjectCollection.create()
for x, y in [(1.0, 1.0), (3.0, 1.0), (1.0, 2.0), (3.0, 2.0)]:
    pts.add(sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)))

cb_in = holes.createCounterboreInput(
    adsk.core.ValueInput.createByString("3.4 mm"),   # holeDiameter
    adsk.core.ValueInput.createByString("6.0 mm"),   # counterboreDiameter
    adsk.core.ValueInput.createByString("3.0 mm"),   # counterboreDepth
)
cb_in.setPositionBySketchPoints(pts)
cb_in.setDistanceExtent(adsk.core.ValueInput.createByString("10 mm"))
holes.add(cb_in)
```

Diameters are fixed when you build the input (`createSimpleInput(holeDiameter)`, `createCounterboreInput(holeDiameter, counterboreDiameter, counterboreDepth)`, and the countersink/clearance variants) — `HoleFeatureInput` has no `holeDiameter` property to set afterwards; to change a diameter later, edit the parameter on the resulting `HoleFeature`. Every `ValueInput` here is centimeters when created from a real.

Positioning options on `HoleFeatureInput`: `setPositionByPoint(planarEntity, point)` (a planar face or construction plane plus a `Point3D` or vertex; the point is projected onto the plane along its normal — a vertex is associative, a `Point3D` is not), `setPositionBySketchPoint(pt)`, `setPositionBySketchPoints(objColl)`, `setPositionAtCenter(...)`, `setPositionOnEdge(...)`, `setPositionByPlaneAndOffsets(...)`. Extents: `setDistanceExtent(distance)`, `setAllExtent(direction)` where `direction` is an `ExtentDirections` value, `setOneSideToExtent(...)`. If the hole ends up on the wrong side, flip `isDefaultDirection`.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatures_createSimpleInput.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatures_createCounterboreInput.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatureInput.htm (no holeDiameter property) ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatureInput_setPositionByPoint.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatureInput_setAllExtent.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatureInput_setDistanceExtent.htm ; sample: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatureSample_Sample.htm -->

### Rectangular pattern

```python
inputs = adsk.core.ObjectCollection.create()
inputs.add(body)                       # all entities must be the same type

pats = root.features.rectangularPatternFeatures
p_in = pats.createInput(
    inputs,
    root.xConstructionAxis,                              # directionOneEntity
    adsk.core.ValueInput.createByString("3"),            # quantityOne (unitless)
    adsk.core.ValueInput.createByString("20 mm"),        # distanceOne
    adsk.fusion.PatternDistanceType.SpacingPatternDistanceType,
)
p_in.setDirectionTwo(
    root.yConstructionAxis,                              # directionTwoEntity (may be None for 90°)
    adsk.core.ValueInput.createByString("2"),            # quantityTwo
    adsk.core.ValueInput.createByString("15 mm"),        # distanceTwo
)
pats.add(p_in)
```

`inputEntities` may hold `BRepFace`, `PartFeature`, `BRepBody` or `Occurrence` objects, all of the same type; direction entities may be a linear edge, construction axis, sketch line, or an existing rectangular pattern feature. Patterning construction geometry (`ConstructionPoint` / `ConstructionAxis` / `ConstructionPlane`) allows only one entity in the collection.

`PatternDistanceType.SpacingPatternDistanceType` = distance **between** each element; `ExtentPatternDistanceType` = total distance of the pattern, with the instances evenly spaced within it. Getting these two confused is a silent 2–3× sizing error.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/RectangularPatternFeatures_createInput.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/RectangularPatternFeatureInput_setDirectionTwo.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/PatternDistanceType.htm -->

### Circular pattern

```python
inputs = adsk.core.ObjectCollection.create()
inputs.add(hole_feature)               # a feature, body, face, or occurrence — all the same type

cpats = root.features.circularPatternFeatures
c_in = cpats.createInput(inputs, root.zConstructionAxis)
c_in.quantity   = adsk.core.ValueInput.createByString("6")
c_in.totalAngle = adsk.core.ValueInput.createByString("360 deg")
c_in.isSymmetric = False
cpats.add(c_in)
```

`createInput(inputEntities, axis)` — axis may be a sketch line, linear edge, construction axis, an edge/sketch curve that defines an axis (a circle, etc.), or an axis-defining face. `quantity` / `totalAngle` are `ValueInput` properties set afterwards, not constructor arguments; a negative `totalAngle` reverses direction. Patterning construction geometry allows only one entity in the collection.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/CircularPatternFeatures_createInput.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/CircularPatternFeatureInput.htm -->

### Mirror

```python
inputs = adsk.core.ObjectCollection.create()
inputs.add(body)

mirrors = root.features.mirrorFeatures
m_in = mirrors.createInput(inputs, root.yZConstructionPlane)   # planar face or construction plane
mirrors.add(m_in)
```

All entities in the collection must be the same type (`BRepFace`, `PartFeature`, `BRepBody`, or `Occurrence`); construction geometry mirrors one entity at a time.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/MirrorFeatures_createInput.htm ; sample: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/MirrorFeatureSample_Sample.htm -->

### User parameters, and driving dimensions by expression

```python
params = design.userParameters

def upsert(name, expr, units, comment=""):
    p = params.itemByName(name)
    if p:
        p.expression = expr
        return p
    return params.add(name, adsk.core.ValueInput.createByString(expr), units, comment)

upsert("wall",     "2 mm",  "mm", "shell thickness")
upsert("boss_od",  "6 mm",  "mm")
upsert("boss_id",  "2.5 mm","mm", "M3 self-tapping")
upsert("draft",    "1 deg", "deg")
upsert("hole_qty", "4",     "")        # unitless: empty units string

result = {p.name: p.expression for p in params}
```

`add(name, value, units, comment)` — all four are required. `units` is a string like `"mm"`/`"cm"`/`"deg"`, `""` for a unitless numeric parameter, or `"Text"` for a text parameter (whose expression must be a quoted string literal, e.g. `"'Hello'"`). Units in the `ValueInput` string must be the same *type* as the `units` argument (both lengths, or both angles) or the call fails. If `value` is a `createByReal`, it is read in the **internal** unit for that type (5 → 5 cm for a length, 5 → 5 radians for an angle), so prefer `createByString`.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UserParameters_add.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UserParameters.htm (itemByName) -->

Once the parameter exists, **drive features by name** — any `ValueInput.createByString` is evaluated as an expression:

```python
ext_in.setOneSideExtent(
    adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByString("wall * 3")),
    adsk.fusion.ExtentDirections.PositiveExtentDirection,
)
```

Re-drive an existing feature after the fact through its parameter (`ModelParameter.expression` and `.name` are settable; `.value` is in database units):

```python
ext.extentOne.distance.expression = "wall * 4"     # DistanceExtentDefinition.distance is a parameter
print(ext.extentOne.distance.value)                # cm
```

`ext.extentOne` only exposes `.distance` when the extent actually is a `DistanceExtentDefinition` — check `ext.extentType` (or the object's `objectType`) before assuming.

Sketch dimensions work the same way:

```python
dim = sk.sketchDimensions.addDistanceDimension(
    line.startSketchPoint, line.endSketchPoint,      # both must be SketchPoint objects
    adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,   # or Vertical / Aligned
    adsk.core.Point3D.create(2.0, -0.5, 0),                             # text position
)
dim.parameter.expression = "width"
```

`addDistanceDimension(pointOne, pointTwo, orientation, textPoint, isDriving=True)`; the enum values are `AlignedDimensionOrientation`, `HorizontalDimensionOrientation`, `VerticalDimensionOrientation`.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ModelParameter.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DistanceExtentDefinition.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchDimensions_addDistanceDimension.htm ; https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DimensionOrientations.htm -->

### `createByString` vs `createByReal` — which one, when

Fusion's design database units are **centimeters**, **radians**, and kilograms.
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Units_UM.htm — "If you create a ValueInput using a real value then the value is assumed to be in database units"; Design length = cm, angles = rad -->

| | interpreted as | use it when |
|---|---|---|
| `ValueInput.createByReal(2.0)` | 2 cm (length) / 2 radians (angle) — **always** database units, ignores document display units | the number came from a measurement/computation you already did in cm or radians (bounding boxes, `face.area`, `edge.length`, geometry math) |
| `ValueInput.createByString("20 mm")` | parsed like user input; explicit units win | anything you are writing by hand, and **required** for expressions: `"wall * 2"`, `"20 mm + wall"`, `"360 deg"` |

```python
adsk.core.ValueInput.createByReal(2.0)          # 20 mm — unambiguous
adsk.core.ValueInput.createByString("20 mm")    # 20 mm — unambiguous, self-documenting
adsk.core.ValueInput.createByString("20")       # DANGER: 20 of the DOCUMENT's default unit.
                                                # 20 mm in a mm doc, 20 cm in a cm doc, 20 in in an inch doc.
```

**Rule: always write units into the string, or use `createByReal` with an explicit cm value.** A bare numeric string is the one form whose meaning depends on a setting you did not check.

Two places that are **not** `ValueInput` and take a raw float in cm regardless:
```python
circles.addByCenterRadius(pt, 0.5)       # 5 mm radius — plain double, cm
adsk.core.Point3D.create(4.0, 3.0, 0)    # 40 mm, 30 mm — plain doubles, cm
```

Sanity check before running anything: any hand-written length above ~50 is half a meter. If you meant millimeters, divide by 10.
