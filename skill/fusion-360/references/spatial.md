## Getting geometry where you meant it

This is the part prompting gets wrong most often. The API will happily build your part in the right shape at the wrong place, facing the wrong way — and it will not error. Everything below exists to stop that.

**All numbers in this section are centimetres (API internal unit) and radians.** `20 mm = 2.0`. A dimension over `50` should make you stop and re-read — that's half a metre.

`ValueInput` has two forms and they are not interchangeable: `ValueInput.createByReal(2.0)` is a raw number in **internal units** (cm for length, radians for angle, no unit string parsed), while `ValueInput.createByString('20 mm')` is parsed with units and can carry expressions/parameter names. Prefer `createByString` for anything a human wrote down in mm or degrees.

### The world frame

Fusion's default modeling orientation is **Z up**: `+Z` is up, `XY` is the ground plane, `+X` right, `+Y` "into the screen" from the front view.

| Named view | You are looking at | Sketch plane | API property (on a `Component`) |
|---|---|---|---|
| **Top** | the XY plane, from `+Z` looking down | XY | `root.xYConstructionPlane` |
| **Front** | the XZ plane, from `−Y` looking toward `+Y` | XZ | `root.xZConstructionPlane` |
| **Right** | the YZ plane, from `+X` looking toward `−X` | YZ | `root.yZConstructionPlane` |

<!-- plane property names verified: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Component.htm -->

Consequences worth internalising: on a part sitting on the ground plane, **top = max Z, front = min Y, right = max X**. Origin axes are `root.xConstructionAxis` / `yConstructionAxis` / `zConstructionAxis`.

> **Caveat:** the Z-up default is a *user preference* (Preferences → Design → Default modeling orientation, "Z up" vs "Y up"). The construction planes keep their API names either way, but which one reads as "the floor" flips. If a `fusion_screenshot` of a `top` view looks like a front view, that's the cause — don't compensate blindly, tell the user.

### THE Z-NEGATION RULE

A sketch has its **own 2D coordinate system**. Sketch coordinates are *not* world coordinates, and the mapping on the non-XY origin planes is not the one you'd guess.

| Sketch on | sketch `x` → | sketch `y` → | sketch normal (positive extrude) → | status |
|---|---|---|---|---|
| `xYConstructionPlane` | `+X` | `+Y` | `+Z` | clean, confirmed |
| `xZConstructionPlane` | `+X` | **`−Z`** | `+Y` | confirmed |
| `yZConstructionPlane` | involves `Y`/`Z` with at least one sign flip | — | `+X` | **print it, do not assume** |

The XZ case is the well-documented one: **on the front plane, sketch `y` carries world Z negated.** XY is the only plane where sketch coordinates and world coordinates agree outright. The YZ (right) plane is also reported to flip signs relative to the naive `x→+Y, y→+Z` reading — enough that a "plot the min point" script lands on the max point instead — but the exact axis assignment is not something to recall from memory. Print it.

Worked example — the exact failure people hit:

```python
# Sketch on the XZ (front) plane. Naive intent: "a point 10 mm right, 10 mm up."
sk = design.rootComponent.sketches.add(design.rootComponent.xZConstructionPlane)
p = sk.sketchPoints.add(adsk.core.Point3D.create(1.0, 1.0, 1.0))   # cm
print(p.worldGeometry.asArray())     # -> [1.0, 1.0, -1.0]
# sketch x=1  -> world X = +1
# sketch y=1  -> world Z = -1   <-- 10 mm BELOW the origin, not above
# sketch z=1  -> world Y = +1   (out-of-plane: the API does NOT clamp sketch
#                                geometry to the sketch plane the way the UI does,
#                                so a stray third coordinate silently lifts your
#                                geometry off the plane. Pass 0.)
```
<!-- corroborated: https://forums.autodesk.com/t5/fusion-360-api-and-scripts/point3d-create-makes-z-coordinate-negative-before-placing-point/td-p/8421803 -->

So "10 mm up the front plane" is sketch `(1.0, −1.0, 0.0)`, not `(1.0, 1.0, 0.0)`.

**Never trust the table from memory — confirm it at runtime.** Every sketch reports its own axes in model space, and this is one print:

```python
sk = design.rootComponent.sketches.add(design.rootComponent.xZConstructionPlane)
print('origin', sk.origin.asArray())        # sketch origin, in MODEL space (Point3D)
print('xDir  ', sk.xDirection.asArray())    # -> [1.0, 0.0, 0.0]
print('yDir  ', sk.yDirection.asArray())    # -> [0.0, 0.0, -1.0]  <- the negation, verified
# sketch normal = xDir cross yDir  ->  [0.0, 1.0, 0.0] for XZ
```
<!-- Sketch.xDirection/yDirection are read-only Vector3D in model space; Sketch.origin is a Point3D in model space: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch.htm -->

Do this **every time you sketch on a planar face** rather than an origin plane, and every time you sketch on YZ. A face-derived sketch's axes come from the face and are effectively arbitrary — the table above tells you nothing about them.

### Sketch-local vs world: stop hand-computing

The table is for reading code and reasoning about direction. For *writing* coordinates, convert — it is impossible to get the sign wrong this way:

```python
world = adsk.core.Point3D.create(2.0, 0.0, 1.5)   # where I want it, in world cm
local = sk.modelToSketchSpace(world)              # -> feed to sketch geometry
sk.sketchCurves.sketchCircles.addByCenterRadius(local, 0.4)

back = sk.sketchToModelSpace(local)               # -> read sketch geometry as world
print(back.asArray())
```
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_sketchToModelSpace.htm  ·  https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_modelToSketchSpace.htm -->

Both take and return a `Point3D`, one point at a time, and are sensitive to assembly context. `SketchPoint.worldGeometry` gives the same answer for a point already placed. Rule of thumb: **any literal you type goes through `modelToSketchSpace` unless the sketch is on XY.**

### Place features from existing geometry, never from remembered numbers

Hardcoded coordinates are correct exactly once — until a wall thickness changes or a feature shifts a face. Derive instead. In rough order of preference:

```python
root = design.rootComponent
body = root.bRepBodies.itemByName('Plate')

# 1. Bounding box — the cheapest anchor. Points are cm, in model space.
bb = body.boundingBox
print(bb.minPoint.asArray(), bb.maxPoint.asArray())
cx = (bb.minPoint.x + bb.maxPoint.x) / 2.0        # centre of the body
z_top = bb.maxPoint.z                              # top surface height

# 2. A specific face's own centre and plane — survives dimension changes.
f = body.faces.item(0)
print(f.centroid.asArray())          # Point3D, geometric centre of the face
print(f.pointOnFace.asArray())       # a point GUARANTEED to lie within the face boundary
pl = adsk.core.Plane.cast(f.geometry)
if pl:                               # None if the face isn't planar
    n = pl.normal.copy()
    if f.isParamReversed:            # surface normal != outward face normal
        n.scaleBy(-1)
    print('plane origin', pl.origin.asArray(), 'outward normal', n.asArray())

# 3. A construction plane at an offset from real geometry, not from the origin.
cpi = root.constructionPlanes.createInput()
cpi.setByOffset(f, adsk.core.ValueInput.createByString('5 mm'))
plane = root.constructionPlanes.add(cpi)
# The sign convention for the offset is not documented — check the resulting
# plane.geometry.origin against the face and negate the ValueInput if it went the
# wrong way. Do not assume "positive = along the outward normal".

# 4. Bring existing edges INTO a sketch instead of re-drawing them by coordinate.
sk = root.sketches.add(plane)
ents = sk.project2([f], True)        # isLinked=True -> updates when the source moves
                                     # returns an array of SketchEntity
```
<!-- BRepFace.centroid / pointOnFace / geometry / area / isParamReversed: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepFace.htm -->
<!-- ConstructionPlaneInput.setByOffset(planarEntity, offset: ValueInput): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ConstructionPlaneInput_setByOffset.htm -->
<!-- Sketch.project was retired May 2025 in favour of project2(entities, isLinked): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_project2.htm -->

Also available when you need it: `app.measureManager.measureMinimumDistance(entityOne, entityTwo)` and `app.measureManager.measureAngle(...)` for real measurements between entities (both return a `MeasureResults` object — read `.value`, which is in cm / radians), and `component.orientedMinimumBoundingBox` when an axis-aligned box lies about a rotated part. <!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/MeasureManager.htm · https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Component.htm -->

Do **not** reach for joints/as-built joints to position static geometry. They're for assemblies with intended motion; for "this boss sits on that face", a face-derived sketch or an offset construction plane is simpler and won't add timeline fragility.

### Reading a bounding box to check what you built

```python
bb = body.boundingBox                 # BoundingBox3D: minPoint / maxPoint, both Point3D, cm
print('size mm: %.2f x %.2f x %.2f' % (
    (bb.maxPoint.x - bb.minPoint.x) * 10,
    (bb.maxPoint.y - bb.minPoint.y) * 10,
    (bb.maxPoint.z - bb.minPoint.z) * 10))
```
<!-- https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BoundingBox3D.htm · https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepBody.htm -->

Reading it: `maxPoint.z` = top, `minPoint.z` = bottom, `minPoint.y` = front, `maxPoint.x` = right (Z-up default). For a hollow shell, the interior surfaces sit at the exterior bounds ± wall thickness — the bounding box only ever describes the outside. `component.boundingBox` is fast but not guaranteed tight for curved bodies; `component.preciseBoundingBox` fits tightly around the B-Rep bodies when the exact number matters.

### PREDICT-THEN-VERIFY (do this on every geometry-changing call)

1. **State the intent in world coordinates**, in mm, out loud in your reasoning: "boss centred on the plate top, extending from z = 3 mm to z = 8 mm."
2. **Query** the geometry you're anchoring to — don't recall it.
3. **Plan** plane, offset, and direction sign, applying the negation rule.
4. **Print the predicted bounding box before you build.** If you can't state it, you don't know where the feature is going.
5. **Execute**, then print the actual bounding box and compare. Agreement within ~0.001 cm is a pass; anything else is a wrong-place bug, not a rounding issue.
6. **`fusion_screenshot`** — numbers can agree while the thing is visibly absurd. Use `iso` plus whichever ortho view is load-bearing.

A boss that silently extruded *into* the plate instead of out of it has an identical bounding box on one axis and a different one on another — step 5 catches it, step 6 confirms it.

### Worked example: a boss on the top face of an existing plate

Nothing here is hardcoded except the boss's own diameter and height.

```python
root = design.rootComponent
plate = root.bRepBodies.itemByName('Plate')

# --- locate the top face: planar, OUTWARD normal pointing up, highest of those ---
up = adsk.core.Vector3D.create(0, 0, 1)
top = None
for f in plate.faces:
    pl = adsk.core.Plane.cast(f.geometry)          # None if not planar
    if not pl:
        continue
    n = pl.normal.copy()
    if f.isParamReversed:                          # surface normal may be flipped
        n.scaleBy(-1)                              # relative to the face's outward normal
    if n.dotProduct(up) > 0.999:
        if top is None or f.centroid.z > top.centroid.z:
            top = f
if top is None:
    raise RuntimeError('no upward-facing planar face on ' + plate.name)

# --- PREDICT ---
bb = plate.boundingBox
BOSS_H = 0.5                                        # 5 mm, in cm
predicted_max_z = top.centroid.z + BOSS_H
print('before: min', bb.minPoint.asArray(), 'max', bb.maxPoint.asArray())
print('predict after: max z = %.4f  (others unchanged)' % predicted_max_z)

# --- sketch ON the face, and convert the world target into sketch space ---
sk = root.sketches.add(top)
print('sketch xDir', sk.xDirection.asArray(), 'yDir', sk.yDirection.asArray())  # never assume

centre_world = adsk.core.Point3D.create(top.centroid.x, top.centroid.y, top.centroid.z)
centre_sk = sk.modelToSketchSpace(centre_world)
sk.sketchCurves.sketchCircles.addByCenterRadius(centre_sk, 0.4)   # r = 4 mm -> Ø8 mm

# --- extrude along the sketch normal, joined to the plate ---
exts = root.features.extrudeFeatures
inp = exts.createInput(sk.profiles.item(0),
                       adsk.fusion.FeatureOperations.JoinFeatureOperation)
ext_def = adsk.fusion.DistanceExtentDefinition.create(
    adsk.core.ValueInput.createByString('5 mm'))
inp.setOneSideExtent(ext_def, adsk.fusion.ExtentDirections.PositiveExtentDirection)
exts.add(inp)

# --- VERIFY (re-look-up: feature operations can invalidate an old body reference) ---
plate = root.bRepBodies.itemByName('Plate')
bb2 = plate.boundingBox
print('after:  min', bb2.minPoint.asArray(), 'max', bb2.maxPoint.asArray())
ok = abs(bb2.maxPoint.z - predicted_max_z) < 1e-3
result = {'predicted_max_z': predicted_max_z, 'actual_max_z': bb2.maxPoint.z, 'match': ok}
```
<!-- ExtrudeFeatures.createInput(profile, operation): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatures_createInput.htm -->
<!-- setOneSideExtent(extent: ExtentDefinition, direction: ExtentDirections, taperAngle=None): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatureInput_setOneSideExtent.htm -->
<!-- DistanceExtentDefinition.create(distance: ValueInput): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DistanceExtentDefinition_create.htm -->
<!-- The older setDistanceExtent(isSymmetric, distance) was retired Sept 2022 and replaced by setOneSideExtent; it still runs in legacy scripts but prefer the current form. -->
<!-- participantBodies need not be set: it defaults to all bodies the feature intersects. -->

`PositiveExtentDirection` is along the sketch/profile plane normal — which is why step 3 printed the sketch axes. Then call `fusion_screenshot`. If `match` is `False` and `minPoint.z` moved instead, the extrude went the wrong way — swap the direction to `adsk.fusion.ExtentDirections.NegativeExtentDirection` rather than rebuilding the sketch on a different plane.
