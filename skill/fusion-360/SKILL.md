---
name: fusion-360
description: Model in Autodesk Fusion 360 through the fusion-mcp bridge — write Fusion API Python, verify with screenshots, export print-ready files. Use for any CAD, 3D modelling, parametric design, 3D-printing, STL/STEP export, enclosure, bracket, or mechanical-part request, and whenever the fusion_execute / fusion_screenshot / fusion_state / fusion_export tools are involved.
---

# Fusion 360 via fusion-mcp

You drive a live Fusion 360 session on this Mac. `fusion_execute` runs Python
inside it, `fusion_screenshot` is your eyes, `fusion_state` says what is open,
`fusion_export` writes files. Read this page fully; load a reference file below
when you need it.

## The two conversions that break everything

**Lengths are CENTIMETRES. Angles are RADIANS.** Regardless of what the document
displays.

| You want | You pass |
|---|---|
| 20 mm | `2.0` |
| 1.5 mm wall | `0.15` |
| 90° | `math.pi / 2` |

A part that comes out 10× too big is always this. Prefer
`adsk.core.ValueInput.createByString("20 mm")` where a `ValueInput` is accepted —
it is unit-aware and self-documenting. Note that some APIs take a plain float
instead (e.g. circle radius), so check before assuming.

## Things that hang Fusion rather than failing

These do not raise — they deadlock the bridge, and the only recovery is
dismissing a dialog in Fusion by hand:

- **Never** call `ui.messageBox(...)`. Autodesk's own doc samples wrap everything
  in `try/except: ui.messageBox(...)` — never copy that part. Let exceptions
  propagate; the bridge returns the traceback to you.
- **Never** call `adsk.doEvents()`, and never create or execute a UI command.
- **Never** write an unbounded loop. Your code runs on Fusion's main thread and
  cannot be cancelled; the timeout only abandons the wait. Bound every loop.

Also: `result` and captured `print()` output are truncated at ~64 KB, so return
summaries, not entity dumps.

## How to work

**1. Look before you touch.** Start every session with `fusion_state()` and a
`fusion_screenshot("iso")`. Never assume the document is empty, is the one from
last session, or survived a crash intact. If what you see disagrees with what
you expected, say so and ask.

**2. Screenshot after every geometry change, and actually read it.** Not every
three changes, not at the end. A wrong extrude direction, a profile that grabbed
the wrong region, a fillet on the wrong edge — none of these raise, and all of
them poison every later step. Ten blind steps means ten to unwind instead of one.
Use `front`/`top`/`right` when you need to know whether something is truly flush
or centred; `iso` shows form.

**3. Predict, then measure.** Before a significant operation, state the expected
bounding box in mm. After it, measure (`references/workflow.md` has a `bbox_mm`
helper). This catches "geometry exists but is mirrored / 10× off / in the wrong
place", which screenshots alone miss.

**4. Ask before destroying anything.** Never boolean against, delete, or roll the
timeline back over geometry you did not create this session, without explicit
approval. Additive mistakes are cheap; a bad join or deletion is the hardest
thing to unwind, and undo does not cover everything a script does.

**5. Small calls, not one big script.** A traceback from 15 lines localises
instantly; from 200 lines it does not, and a half-failed script leaves a
half-built document to diagnose. The namespace persists between calls, so
incrementality is free.

**6. Parameters over hardcoded numbers.** Wall thickness, clearances, hole sizes,
fillet radii — make them user parameters and reference them by expression, so
"make the walls 2 mm" is one edit rather than a rebuild.

**7. Work in a scratch document while iterating**, and keep the design parametric
so the timeline can save you.

## Injected names

`adsk` (with `adsk.core` and `adsk.fusion` imported), `app`, `ui`, `design` —
already resolved for every call. Do not re-import or re-resolve them. Assign to
`result` to return a value. The namespace persists between calls; pass
`reset=true` to clear it.

## Before you guess at geometry placement

Sketch planes do **not** map to world axes the way intuition suggests — only XY
does. On XZ and YZ the sketch's own 2D axes differ in sign and order, which is
the usual cause of a part that comes out mirrored or facing the wrong way.
**Do not guess the mapping.** Create the sketch, then print
`sk.sketchToModelSpace(adsk.core.Point3D.create(1, 0, 0))` and `(0, 1, 0)` and
read the result. If you do not need a specific plane, sketch on XY and move the
body afterwards.

Likewise, **face and edge indices shift after every feature.** Never cache an
index across an operation — re-query and identify geometrically (by centroid,
area, or extreme coordinate).

## Reference files

Load the one you need; do not preload them all.

| File | When |
|---|---|
| `references/patterns.md` | Writing geometry: components, sketches, extrude/revolve/fillet/chamfer/holes/patterns/mirror, profile selection, user parameters |
| `references/gotchas.md` | Something failed oddly: BaseFeature rules for parametric designs, `profiles.count == 0`, stale entities, `ObjectCollection`, boolean operations, occurrence vs component |
| `references/spatial.md` | Placing geometry relative to existing geometry, plane/axis orientation, sketch↔world conversion, bounding boxes |
| `references/printing.md` | 3D-printing dimensions: clearances, wall thickness, M3 hardware, heat-set inserts, overhangs, print orientation |
| `references/workflow.md` | The full session discipline, with the `bbox_mm` helper and the API-changed-after-a-Fusion-update checklist |

## If a pattern here is wrong

Fusion ships breaking API changes mid-cycle. If a documented pattern raises a
`TypeError` about argument counts or an `AttributeError` on a method that should
exist, **suspect a Fusion API change before suspecting your own code** — check
the current online reference for that method. When you find something in these
files is stale, say so explicitly and suggest the correction be written back. A
wrong pattern here is worse than a missing one: it actively misleads.
