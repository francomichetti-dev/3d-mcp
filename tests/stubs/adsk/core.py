"""adsk.core stand-ins."""


class _Handler:
    """Base for the event handler classes the add-in subclasses at import time."""

    def __init__(self, *args, **kwargs):
        pass


class CustomEventHandler(_Handler):
    pass


class DocumentEventHandler(_Handler):
    pass


class Point3D:
    @staticmethod
    def create(x=0.0, y=0.0, z=0.0):
        point = Point3D()
        point.x, point.y, point.z = x, y, z
        return point


class Vector3D:
    @staticmethod
    def create(x=0.0, y=0.0, z=0.0):
        vector = Vector3D()
        vector.x, vector.y, vector.z = x, y, z
        return vector


class DocumentTypes:
    FusionDesignDocumentType = 0


class PaletteDockingStates:
    PaletteDockStateRight = 4


class _StubApp:
    """The application object, with just enough shape to be poked at.

    Tests that care about document behaviour install their own object instead;
    this is the inert default so an unexpected call fails loudly rather than
    silently doing something plausible.
    """

    def __init__(self):
        self.version = "0.0.0-stub"
        self.activeDocument = None
        self.activeProduct = None
        self.documents = []
        self.userInterface = None

    def registerCustomEvent(self, event_id):
        raise AssertionError(
            "registerCustomEvent reached the stub — the test should install its "
            "own app object before starting the add-in"
        )

    def unregisterCustomEvent(self, event_id):
        return True


class Application:
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = _StubApp()
        return cls._instance

    @classmethod
    def _set(cls, app):
        """Test hook: swap in a purpose-built application object."""
        cls._instance = app
