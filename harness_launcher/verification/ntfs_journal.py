"""Unprivileged NTFS journal and per-file close barriers; never edits file bytes."""
from contextlib import contextmanager
import ctypes
from pathlib import Path
import struct


class JournalUnavailable(Exception):
    pass


def parse_records(data, start):
    """V0 requests return V2 records; never skip an unknown record version."""
    if len(data) < 8:
        raise JournalUnavailable('short journal buffer')
    cursor, = struct.unpack_from('<q', data)
    if cursor < start:
        raise JournalUnavailable('journal moved backwards')
    records, offset, previous = [], 8, start - 1
    while offset < len(data):
        if len(data) - offset < 60:
            raise JournalUnavailable('short journal record')
        length, major, minor, file_id, parent_id, usn, timestamp, reason, source, security, attributes, namesize, nameoff = struct.unpack_from('<IHHQQqqIIIIHH', data, offset)
        if (major != 2 or minor != 0 or length < 60 or length % 8 or offset + length > len(data)
                or usn < start or usn <= previous or usn >= cursor
                or nameoff < 60 or nameoff + namesize > length or namesize % 2):
            raise JournalUnavailable('invalid or unsupported journal record')
        records.append((file_id, parent_id, reason, usn))
        previous, offset = usn, offset + length
    return cursor, records


class NtfsJournal:
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.api = fingerprint.api
        if self.api:
            from ctypes import wintypes as w
            self.api.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD,
                w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
            self.api.DeviceIoControl.restype = w.BOOL

    @contextmanager
    def handle(self, path, access=0):
        if not self.api:
            raise JournalUnavailable('not Windows')
        handle = self.api.CreateFileW(str(path), access, 7, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise JournalUnavailable('cannot open journal root')
        try:
            yield handle
        finally:
            self.api.CloseHandle(handle)

    def root_identity(self, path):
        volume = self.fingerprint.volume(path) if self.api else None
        if volume is None:
            raise JournalUnavailable('not local NTFS')
        with self.handle(path, 0x80) as handle:
            info, basic = self.fingerprint.Info(), self.fingerprint.Basic()
            if (not self.api.GetFileInformationByHandle(handle, ctypes.byref(info))
                    or not self.api.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic))
                    or info.volume != volume or not info.attributes & 0x10 or info.attributes & 0x400):
                raise JournalUnavailable('not a regular directory')
            return (volume, info.id_high << 32 | info.id_low, basic.created)

    def control(self, handle, code, data=None):
        from ctypes import wintypes as w
        buffer, returned = ctypes.create_string_buffer(65536), w.DWORD()
        if not self.api.DeviceIoControl(handle, code, data, len(data) if data else 0,
                buffer, len(buffer), ctypes.byref(returned), None):
            raise JournalUnavailable('journal read unavailable')
        return buffer.raw[:returned.value]

    def query(self, handle):
        data = self.control(handle, 0x900f4)
        if len(data) < 56:
            raise JournalUnavailable('short journal metadata')
        journal, first, next_usn, lowest, maximum, size, delta = struct.unpack_from('<QqqqqQQ', data)
        if not journal or not 0 <= first <= next_usn or not 0 <= lowest <= next_usn:
            raise JournalUnavailable('invalid journal metadata')
        return journal, max(first, lowest), next_usn

    def checkpoint(self, root):
        with self.handle(Path(root).anchor) as handle:
            journal, first, next_usn = self.query(handle)
            return (journal, next_usn)

    def close_entry(self, path, file_id):
        # Attribute-only handles also keep NTFS reason flags accumulated.
        # Explicitly close their journal cycle before observing a directory or
        # link. OPEN_REPARSE_POINT prevents traversing a link to its target.
        with self.handle(path, 0x80) as handle:
            info = self.fingerprint.Info()
            if (not self.api.GetFileInformationByHandle(handle, ctypes.byref(info))
                    or info.id_high << 32 | info.id_low != file_id):
                raise JournalUnavailable('entry identity changed')
            data = self.control(handle, 0x900ef)
            if len(data) != 8:
                raise JournalUnavailable('missing close barrier')
            usn, = struct.unpack('<q', data)
            if usn < 0:
                raise JournalUnavailable('invalid close barrier')
            return usn

    def since(self, root, checkpoint):
        journal, cursor = checkpoint
        with self.handle(Path(root).anchor) as handle:
            current, first, boundary = self.query(handle)
            if current != journal or not first <= cursor <= boundary:
                raise JournalUnavailable('journal gap or reset')
            records = []
            for _ in range(256):  # Bound unrelated volume traffic; fall back.
                if cursor >= boundary:
                    break
                data = struct.pack('<qIIQQQ', cursor, 0xffffffff, 0, 0, 0, journal)
                next_usn, batch = parse_records(self.control(handle, 0x903ab, data), cursor)
                if next_usn <= cursor:
                    raise JournalUnavailable('journal did not reach boundary')
                records.extend(batch)
                cursor = next_usn
            else:
                raise JournalUnavailable('journal scan budget exceeded')
            current, first, latest = self.query(handle)
            if current != journal or not first <= checkpoint[1] <= cursor <= latest:
                raise JournalUnavailable('journal changed during read')
            return (journal, cursor), records
