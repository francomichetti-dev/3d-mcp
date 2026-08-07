"""A UITimer that records instead of firing.

Importing the poller starts a timer. Under test nothing should actually tick —
the tests call _tick directly, so timing is deterministic and a test never
races the thing it is asserting about.
"""


class UITimer:
    instances = []

    def __init__(self):
        self.Interval = None
        self.Elapsed = _Event()
        self.started = False
        self.stopped = False
        UITimer.instances.append(self)

    def Start(self):
        self.started = True

    def Stop(self):
        self.stopped = True


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self
