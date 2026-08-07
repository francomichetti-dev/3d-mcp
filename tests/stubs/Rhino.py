"""Enough of RhinoCommon to exercise rhino-poller.py off a Rhino machine.

Mirrors tests/stubs/adsk/, which does the same for Fusion. Only the surface the
poller actually touches is here — RhinoDoc.ActiveDoc, the viewport projection
enum, DisplayModeDescription and ViewCapture. Anything the poller starts using
should be added deliberately rather than auto-faked, so that a test failure
means the poller changed, not that a mock guessed.
"""


class _Layer:
    def __init__(self, name):
        self.Name = name


class _Objects:
    def __init__(self, count=0):
        self.Count = count


class _Views:
    def __init__(self, active=None):
        self.ActiveView = active
        self.redraws = 0

    def Redraw(self):
        self.redraws += 1


class _Viewport:
    def __init__(self):
        self.projection = None
        self.DisplayMode = None
        self.zoomed = 0

    def SetProjection(self, projection, _name, _bang):
        self.projection = projection

    def ZoomExtents(self):
        self.zoomed += 1


class _View:
    def __init__(self):
        self.ActiveViewport = _Viewport()


class _Doc:
    def __init__(self, name="", path="", objects=0, layers=("Default",)):
        self.Name = name
        self.Path = path
        self.Modified = False
        self.ModelUnitSystem = "Millimeters"
        self.ModelAbsoluteTolerance = 0.001
        self.Objects = _Objects(objects)
        self.Layers = [_Layer(n) for n in layers]
        self.Views = _Views(_View())


class RhinoDoc:
    ActiveDoc = _Doc()


# A 1x1 PNG, so the base64 the poller returns is a real decodable image.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63fc0f000201010067e19b6b0000000049454e44ae426082"
)


class _Bitmap:
    def Save(self, stream, _image_format):
        stream.write(PNG_BYTES)


class _ViewCapture:
    """Records what it was asked for; the test asserts the size was honoured."""

    last = None

    def __init__(self):
        self.Width = None
        self.Height = None
        self.ScaleScreenItems = None
        self.DrawAxes = None
        self.DrawGrid = None
        self.DrawGridAxes = None
        self.TransparentBackground = None
        self.captured = None
        _ViewCapture.last = self

    def CaptureToBitmap(self, view):
        self.captured = view
        return None if getattr(_ViewCapture, "fail", False) else _Bitmap()


class _DefinedViewportProjection:
    Perspective = "Perspective"
    Top = "Top"
    Front = "Front"
    Right = "Right"


class _DisplayModeDescription:
    @staticmethod
    def FindByName(name):
        return {"name": name}


class Display:
    DefinedViewportProjection = _DefinedViewportProjection
    DisplayModeDescription = _DisplayModeDescription
    ViewCapture = _ViewCapture
