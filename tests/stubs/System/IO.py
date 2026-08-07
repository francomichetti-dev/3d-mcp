class MemoryStream:
    def __init__(self):
        self._buffer = bytearray()
        self.disposed = False

    def write(self, data):
        self._buffer.extend(data)

    def ToArray(self):
        return bytes(self._buffer)

    def Dispose(self):
        self.disposed = True
