"""Read-only access to MSI Afterburner's monitoring shared memory (MAHMSharedMemory).

Afterburner only has to be running (the tray icon is enough; its hardware monitor
window can stay closed). Nothing is written to the mapping or to Afterburner.
"""
import ctypes
from ctypes import wintypes
import struct
import time

NAME = 'MAHMSharedMemory'
SIGNATURE = 0x4D41484D  # 'MAHM'
MAX_AGE = 5.0
FILE_MAP_READ = 4

_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.OpenFileMappingW.restype = wintypes.HANDLE
_kernel.OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_kernel.MapViewOfFile.restype = ctypes.c_void_p
_kernel.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t)
_kernel.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
_kernel.CloseHandle.argtypes = (wintypes.HANDLE,)


def parse(raw):
    """Parse a MAHM block into {source name: dict(value, min, max)}; None if invalid."""
    if len(raw) < 24:
        return None
    signature, _, header, count, size, stamp = struct.unpack_from('<6I', raw, 0)
    if signature != SIGNATURE or size < 1312 or header + count * size > len(raw):
        return None
    values = {}
    for i in range(count):
        offset = header + i * size
        name = raw[offset:offset + 260].split(b'\0')[0].decode('latin1')
        value, low, high = struct.unpack_from('<3f', raw, offset + 1300)
        values[name] = dict(value=value, min=low, max=high)
    return dict(timestamp=stamp, values=values)


class Sensors:
    def __init__(self):
        self.handle = None
        self.view = None

    def _open(self):
        handle = _kernel.OpenFileMappingW(FILE_MAP_READ, False, NAME)
        if not handle:
            return False
        view = _kernel.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
        if not view:
            _kernel.CloseHandle(handle)
            return False
        self.handle, self.view = handle, view
        return True

    def close(self):
        if self.view:
            _kernel.UnmapViewOfFile(self.view)
        if self.handle:
            _kernel.CloseHandle(self.handle)
        self.handle = self.view = None

    def read(self):
        """Latest readings, or None when Afterburner is not running / stale."""
        try:
            if not self.view and not self._open():
                return None
            header = ctypes.string_at(self.view, 24)
            _, _, header_size, count, size, _ = struct.unpack('<6I', header)
            if count > 512 or size > 4096:
                self.close()
                return None
            data = parse(ctypes.string_at(self.view, header_size + count * size))
        except (OSError, struct.error):
            self.close()
            return None
        if data is None:
            self.close()
            return None
        if abs(time.time() - data['timestamp']) > MAX_AGE:
            return None
        return data['values']
