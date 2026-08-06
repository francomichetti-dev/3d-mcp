"""Model a simple car in Rhino, then capture the viewport.

The real test behind the toy: this is the loop the Fusion bridge runs - create
geometry, then LOOK at the result - and this proves it works in Rhino too. A
model writing CAD code blind gets things subtly wrong with no exception raised,
so the screenshot is the point, not the car.

Everything lands on its own layer so it can be deleted in one click. Every
exception is written to the log file: run through `rhinocode script`, an
uncaught traceback goes to Rhino's own console and the caller sees only silence.
"""

import os
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SHOT = os.path.join(HERE, "rhino-car.png")
LOG = os.path.join(HERE, "rhino-car-output.txt")

_log = open(LOG, "w", encoding="utf-8")


def say(text=""):
    print(text)
    _log.write(str(text) + "\n")
    _log.flush()


LAYER = "3d-mcp-test-car"

try:
    import Rhino
    import rhinoscriptsyntax as rs
    from Rhino.Geometry import Interval, Plane, Point3d, Vector3d

    doc = Rhino.RhinoDoc.ActiveDoc
    say(f"document : {doc.Name or '(unsaved)'}")
    say(f"units    : {doc.ModelUnitSystem}")

    # Rhino's unit system is user-configurable, unlike Fusion's fixed
    # centimetres, so size everything relative to the document. 1.0 == 1 metre
    # whatever the file is set to.
    metre = Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Meters, doc.ModelUnitSystem)
    say(f"1 m      : {metre} document units")

    # --- our own layer, so this is trivial to remove ---------------------
    # rhinoscriptsyntax rather than the RhinoCommon Layer object: the latter
    # needs System.Drawing for the colour, which is not necessarily loaded in
    # Rhino 8's CPython.
    if rs.IsLayer(LAYER):
        rs.PurgeLayer(LAYER)
    rs.AddLayer(LAYER, color=(200, 60, 60))
    rs.CurrentLayer(LAYER)
    say(f"layer    : {LAYER}")
    say()

    made = []

    def add(brep, label):
        guid = doc.Objects.AddBrep(brep)
        if str(guid) == "00000000-0000-0000-0000-000000000000":
            say(f"  FAILED  {label}")
            return None
        made.append((label, guid))
        say(f"  +  {label}")
        return guid

    def box(cx, cy, cz, length, width, height):
        """Box centred in x/y, base sitting at cz. Values in metres."""
        plane = Plane(Point3d(cx * metre, cy * metre, cz * metre), Vector3d.ZAxis)
        return Rhino.Geometry.Box(
            plane,
            Interval(-length / 2 * metre, length / 2 * metre),
            Interval(-width / 2 * metre, width / 2 * metre),
            Interval(0, height * metre),
        ).ToBrep()

    def wheel(x, y, radius, width):
        """Cylinder on its side, axis along y."""
        base = Plane(Point3d(x * metre, (y - width / 2) * metre, radius * metre),
                     Vector3d.YAxis)
        circle = Rhino.Geometry.Circle(base, radius * metre)
        return Rhino.Geometry.Cylinder(circle, width * metre).ToBrep(True, True)

    say("building a small hatchback, 4.0 x 1.8 m")
    add(box(0, 0, 0.30, 4.0, 1.8, 0.70), "body")
    add(box(-0.15, 0, 1.00, 2.1, 1.6, 0.55), "cabin")

    for label, x, y in [
        ("wheel front left",  1.35,  0.90),
        ("wheel front right", 1.35, -0.90),
        ("wheel rear left",  -1.35,  0.90),
        ("wheel rear right", -1.35, -0.90),
    ]:
        add(wheel(x, y, 0.33, 0.25), label)

    # A fillet is where generated CAD code usually goes wrong, so it belongs in
    # the test rather than a pile of clean primitives.
    say()
    try:
        body_obj = doc.Objects.FindId(made[0][1])
        brep = body_obj.Geometry
        edges = list(range(brep.Edges.Count))
        radii = [0.12 * metre] * len(edges)
        filleted = Rhino.Geometry.Brep.CreateFilletEdges(
            brep, edges, radii, radii,
            Rhino.Geometry.BlendType.Fillet,
            Rhino.Geometry.RailType.RollingBall,
            doc.ModelAbsoluteTolerance)
        if filleted and len(filleted) == 1:
            doc.Objects.Replace(made[0][1], filleted[0])
            say(f"  body filleted on {len(edges)} edges")
        else:
            say("  fillet declined by Rhino (left square)")
    except Exception as exc:                               # noqa: BLE001
        say(f"  fillet raised: {exc!r}")

    doc.Views.Redraw()

    # --- look at it ------------------------------------------------------
    say()
    say("capturing the viewport")
    view = doc.Views.ActiveView
    try:
        view.ActiveViewport.SetProjection(
            Rhino.Display.DefinedViewportProjection.Perspective, None, False)
        shaded = Rhino.Display.DisplayModeDescription.FindByName("Shaded")
        if shaded:
            view.ActiveViewport.DisplayMode = shaded
        view.ActiveViewport.ZoomExtents()
        doc.Views.Redraw()
    except Exception as exc:                               # noqa: BLE001
        say(f"  view setup raised: {exc!r}")

    # NOT Rhino.Display.ViewCapture().CaptureToBitmap(): rhinocode runs this
    # script off the UI thread, and touching the display pipeline from there
    # takes Rhino down with it - observed three times, each run dying on
    # exactly that call and taking the application with it.
    #
    # _-ViewCaptureToFile goes through Rhino's normal command pipeline, which
    # marshals to the UI thread itself. This is the same constraint the real
    # adapter will live under, so it is worth learning here rather than later.
    if os.path.exists(SHOT):
        os.remove(SHOT)
    ok = rs.Command(
        '_-ViewCaptureToFile "%s" _Width=1200 _Height=800 _DrawGrid=_Yes '
        '_DrawWorldAxes=_No _DrawCPlaneAxes=_No _TransparentBackground=_No _Enter'
        % SHOT, False)
    if ok and os.path.exists(SHOT):
        say(f"  saved {SHOT} ({os.path.getsize(SHOT)} bytes)")
    else:
        say(f"  capture command returned {ok}, file exists={os.path.exists(SHOT)}")

    say()
    say(f"objects created : {len(made)}")
    say(f"objects in doc  : {doc.Objects.Count}")
    say()
    say(f"to remove all of this, delete the layer '{LAYER}'")
    say("DONE")

except Exception:                                          # noqa: BLE001
    # Without this the traceback goes to Rhino's console and the caller, who is
    # on another machine, sees the log simply stop.
    say()
    say("!!! FAILED")
    say(traceback.format_exc())

finally:
    try:
        _log.close()
    except Exception:
        pass
