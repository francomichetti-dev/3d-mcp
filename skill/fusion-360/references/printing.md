## 3D-printing defaults

> **⚠️ These are generic FDM community defaults, NOT Franco's recorded preferences.**
> Franco's actual tuned values for his printer, nozzle, and filament have **not** been captured anywhere yet. Everything below is a well-established starting point drawn from common FDM practice — use it when no better number exists, state which value you used, and invite confirmation. **When Franco gives a measured value, it replaces the number here.** Where a value genuinely depends on machine, nozzle, or material, that is called out rather than papered over with a single fake-precise number.
>
> Suggested one-time ask, early in a print-oriented design: *"I'm using 0.4 mm nozzle assumptions (1.6 mm walls, 0.2 mm sliding clearance, M3 heat-set boss 4.0 mm ID). Do you have measured values from a tolerance test I should use instead?"*

**All dimensions in this section are millimetres — the Fusion API's internal length unit is centimetres. 1.6 mm wall → `1.6/10` → `0.16`.** Safer: pass expression strings (`ValueInput.createByString("1.6 mm")`) so the units are explicit in the code. The same rule applies to angles: `ValueInput.createByReal` treats a number as **radians**, so write `createByString("90 deg")` when you mean degrees.

### Fits and clearances

Values are the **nominal gap between the two mating dimensions** (hole minus shaft, or pocket minus part). Per-side gap is half of this — getting that backwards is the most common fit error.

| Fit | Gap | Use |
|---|---|---|
| Interference / press | 0.00 to −0.10 | Pressed pin, permanent boss. Risk of splitting thin walls — add a wall or a slot |
| Snug (hand-press, no play) | +0.10 | Locating dowels, alignment pegs |
| Sliding / close | +0.20 | Lids, drawers, parts that move but shouldn't rattle |
| Free running / loose | +0.40 – 0.50 | Rotating shafts, hinges, anything that must never bind |
| Drop-in cover / captive nut pocket | +0.30 – 0.40 | Assembled by hand, no tools |

Additional facts that matter more than the table:

- **FDM holes print undersize** (inner perimeters pull inward, plus polygonal facet chords). Typically **0.1–0.4 mm** small, machine-dependent. For a hole that must fit a real shaft, either oversize the model or ream/drill after printing.
- **External dimensions print oversize** (elephant foot, over-extrusion) — a printed 20.0 mm cube commonly measures 20.1–20.3 mm at the base.
- These two effects **stack** and are exactly what a tolerance/clearance test print calibrates. Any project where fit matters should start with one; the table above is the guess you use until then.
- A 45° chamfer or 0.5 mm fillet on bottom edges hides elephant foot and keeps the first layer from interfering with a mating part.

### Wall thickness

Driven by **extrusion width**, which is ≈ nozzle diameter (0.4 mm nozzle → ~0.42–0.45 mm typical extrusion width). Design walls as an **integer multiple of extrusion width** so the slicer fills them with whole perimeters and leaves no gap-fill voids.

| | 0.4 mm nozzle | Rule |
|---|---|---|
| Absolute printable minimum | ~0.8–0.9 mm | 2 perimeters — thin, translucent, not structural |
| Practical minimum | **1.2 mm** | 3 perimeters |
| **General default for enclosures/brackets** | **1.6 mm** | 4 perimeters — good stiffness-to-mass |
| Load-bearing / threaded-into | 2.4–3.2 mm | 6–8 perimeters |

With a 0.6 mm nozzle, the same perimeter counts give 1.2 / 1.8 / 2.4 / 3.6 mm — **ask which nozzle is mounted before committing thin walls.**

### M3 hardware

The reference numbers (standards-based, safe to rely on):

| Feature | Value | Note |
|---|---|---|
| Clearance hole, close fit | 3.2 mm | ISO 273 fine |
| **Clearance hole, normal** | **3.4 mm** | ISO 273 medium — the default |
| Clearance hole, loose | 3.6 mm | ISO 273 coarse; use when stacking tolerances |
| Socket head cap screw (ISO 4762) head | Ø5.5 mm × 3.0 mm high | Counterbore Ø6.0–6.5 mm, depth 3.2 mm+ |
| Button head (ISO 7380) head | Ø5.7 mm × 1.65 mm high | |
| Countersunk (ISO 10642 / DIN 7991) head | Ø6.72 mm theoretical, ~1.86 mm high, 90° included | Model the sink ~6.7 mm at the surface so the head seats flush; measured head OD is nearer 6.0 mm |
| Hex nut (ISO 4032) | 5.5 mm across flats, 2.4 mm thick | Across corners 6.01 mm |
| Nut pocket (captive) | 5.6–5.7 mm AF, 2.6 mm deep | Print the pocket as a hexagon, +0.1–0.2 mm on flats |
| Nyloc nut (ISO 10511) | 5.5 mm AF, ~4.0 mm thick | Pocket 4.2 mm deep |
| Flat washer (DIN 125) | Ø7.0 mm, 0.5 mm thick | |

Cutting a countersink through the API: `holeFeatures.createCountersinkInput(holeDia, csDia, csAngle)` — all three are `ValueInput`s, and `csAngle` is the **full included angle**, interpreted as **radians** if you build it with `createByReal`. Use `ValueInput.createByString("90 deg")` and the ambiguity disappears. Feed the resulting input to `holeFeatures.add(input)`.
<!-- HoleFeatures.createCountersinkInput(holeDiameter, countersinkDiameter, countersinkAngle): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatures_createCountersinkInput.htm ; HoleFeatures.add: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatures_add.htm -->

Screwing into printed plastic:

| Method | Hole ID | Boss OD | Note |
|---|---|---|---|
| **Heat-set insert** (best for repeated assembly) | **4.0 mm** typical | ≥ 6.0 mm (ID + ~1 mm wall each side minimum; ~8 mm gives 2 mm each side) | **Brand-dependent** — common M3 inserts (e.g. Ruthex/CNC-Kitchen style) are Ø4.6 mm OD × 5.7 mm long and want a ~4.0 mm hole. Measure the actual inserts. Make the hole 0.5–1 mm deeper than the insert, and add a 0.3 mm lead-in chamfer |
| Thread-forming / self-tapping into plastic | 2.5 mm (range 2.4–2.6) | ≥ 5.5 mm | Good for 1–3 assemblies, strips after that. Tighter hole = more strip risk, looser = pull-out |
| Machine screw + captive nut | 3.4 mm | n/a | Strongest, no plastic threads at all |
| Tapped M3 thread cut into plastic | 2.5 mm pilot | ≥ 6 mm | Weakest option; avoid unless the user asks |

### Bosses and ribs

- **Boss OD ≈ 2–2.5× the hole ID** (M3 clearance boss ≈ Ø7–8 mm; heat-set boss ≈ Ø6.5–8 mm around a 4.0 mm hole). Below ~1.5 mm of wall around an insert, the boss splits when the insert goes in.
- **Boss height ≤ ~3× its OD** unsupported. Taller than that, tie it to the wall with 2–4 gussets/ribs.
- **Stand bosses off the wall** by ≥ one wall thickness and connect with a rib, rather than merging a fat cylinder straight into a thin wall — it prints cleaner and avoids a big solid mass.
- **Rib thickness:** the classic "0.5–0.6× wall" rule is an *injection-moulding* rule that exists to prevent sink marks. **FDM has no sink marks**, so ribs can be a full wall thickness (i.e. the same integer multiple of extrusion width) and are stronger for it. Do not blindly halve rib thickness on a printed part.
- **Rib height ≤ ~3× wall thickness**, spacing ≥ 2× wall thickness. Taller and thinner ribs buckle rather than stiffen.
- Fillet where a rib meets a wall (~0.5× wall radius). Sharp internal corners are the crack-initiation sites in printed parts.
- Draft angles are unnecessary for FDM — do not add them unless the part is destined for moulding.

### Overhangs, bridges, holes

- **45° rule:** surfaces up to **45° from vertical** print unsupported and reliably. 45–60° gets rougher and is machine/cooling dependent. Past ~60° from vertical, plan on support — or redesign.
- **Bridges** (flat spans between two supported points) are fine to roughly **20–50 mm** with decent part cooling; under ~10 mm always works. A bridge sags slightly at midspan — never bridge a surface that has to be flat or dimensionally accurate.
- **Horizontal holes** (axis parallel to the bed) sag at the top and come out oval above ~8–10 mm diameter. Fixes: **teardrop** profile, or a 45° chamfer across the top of the hole, or reorient so the hole axis is vertical.
- **Chamfer, don't fillet, overhanging edges** — a 45° chamfer under an overhang is self-supporting; a fillet's tangent approach is not.
- **Countersinks print fine** (a countersink is a ≤45° overhang) if the screw enters from the top.

### Orientation for printing

Orientation decides strength, accuracy, and support, so decide it while designing, not at the slicer:

- **Layer adhesion (Z) is significantly weaker than in-plane (XY)** — commonly cited as roughly 30–70% of XY strength depending on material, temperature, and cooling. Treat it as "Z is the weak axis", not as a number you can calculate with. **Orient the part so the main tensile/bending load runs within the layers, not across them.**
- A **cantilever, hook, or snap-fit** printed standing up will snap at the layer plane. Lay it down.
- A **boss with a screw pulling axially in Z** peels layers apart — this is precisely why heat-set inserts (which fuse into the surrounding plastic) outperform threads tapped into printed plastic.
- **Largest flat face on the bed** — best adhesion, least warping, fewest supports.
- **Accurate round holes want their axis vertical** (Z). Vertical holes are round but undersize; horizontal holes are oval and sag.
- **Design to avoid supports**: keep overhangs ≤45°, chamfer instead of overhang, split a part into two printable halves and join with dowels/screws rather than supporting a big overhang. Supports on a functional mating surface ruin the fit.
- The **cosmetic face should not be the support-scarred face** — and the top face gets the nicest finish.
- Tall thin parts printed vertically are prone to layer shifts and wobble; if aspect ratio > ~5:1, consider reorienting or adding a brim/base.

### Encoding these as parameters

Put the numbers into user parameters so they can be retuned without regenerating the model:

```python
import adsk.core, adsk.fusion

app    = adsk.core.Application.get()
design = adsk.fusion.Design.cast(app.activeProduct)   # already in the namespace after fusion_state

def P(name, expr, units="mm", comment=""):
    """Create-or-update a user parameter. Expressions may reference other parameters by name."""
    existing = design.userParameters.itemByName(name)
    if existing:
        existing.expression = expr          # Parameter.expression is read/write
        return existing
    return design.userParameters.add(       # add(name, ValueInput, units, comment)
        name, adsk.core.ValueInput.createByString(expr), units, comment)

P("wall",         "1.6 mm", "mm", "4 perimeters @ 0.4 nozzle")
P("clr_sliding",  "0.2 mm", "mm", "nominal gap, sliding fit")
P("m3_clear",     "3.4 mm", "mm", "ISO 273 medium")
P("m3_insert_id", "4.0 mm", "mm", "heat-set insert hole - CONFIRM vs actual inserts")
P("boss_od",      "m3_insert_id + 2 * wall", "mm", "derived")
result = [{"name": p.name, "expr": p.expression, "value_cm": p.value}
          for p in design.userParameters]
```
<!-- UserParameters.add(name, value, units, comment) → UserParameter (null on failure): https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UserParameters_add.htm ; itemByName / item / count: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UserParameters.htm ; ValueInput.createByString takes an expression string: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ValueInput_createByString.htm ; Parameter.expression is read/write and may reference other parameters by name ("Length / 2"), case-sensitive: https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Parameter_expression.htm -->

`userParameters.add` returns `None` if it fails (including a name that already exists), which is why the create-or-update path checks `itemByName` first. Note `p.value` comes back in **internal units** — cm for lengths, radians for angles — while `p.expression` shows what you wrote. That is a useful sanity check that a "1.6 mm" wall really is 0.16 internally.
