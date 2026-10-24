"""Optional NTFS content cache; unsupported or changed files are read in full.

The cache is a performance hint, not a recovery artifact. File identity and NTFS
ChangeTime and per-file USN supplement timestamps, which can collide on fast writes.
A data-read handle excludes writers/replacement while a hit is checked.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import logging
import os
import struct
from pathlib import Path
import threading

from errors import PatchError
from storage import ContentStore, atomic_write, canonical
from tree_checkpoint import TreeCheckpoints


class NtfsFingerprint:
    def __init__(self):
        self.volumes = {}
        self.lock = threading.Lock()
        self.api = None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [(n, ctypes.c_longlong) for n in ('created', 'accessed', 'written', 'changed')] + [('attributes', w.DWORD)]
        class Info(ctypes.Structure):
            _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME), ('accessed', w.FILETIME),
                        ('written', w.FILETIME), ('volume', w.DWORD), ('size_high', w.DWORD),
                        ('size_low', w.DWORD), ('links', w.DWORD), ('id_high', w.DWORD), ('id_low', w.DWORD)]
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        api.CreateFileW.restype = w.HANDLE
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        api.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        api.GetFileInformationByHandleEx.restype = w.BOOL
        api.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(Info)]
        api.GetFileInformationByHandle.restype = w.BOOL
        api.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD),
                                             ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
        api.GetVolumeInformationW.restype = w.BOOL
        api.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
        api.ReadFile.restype = w.BOOL
        api.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID,
                                       w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
        api.DeviceIoControl.restype = w.BOOL
        self.api, self.Basic, self.Info = api, Basic, Info

    def volume(self, path):
        from ctypes import wintypes as w
        root = (path if isinstance(path, Path) else Path(path)).anchor
        # Network shares and mounted directories without a matching volume
        # serial fall back to hashing. Only local NTFS semantics are supported.
        if not root or root.startswith('\\\\'):
            return None
        if root in self.volumes:
            return self.volumes[root]
        with self.lock:
            if root not in self.volumes:
                name, serial = ctypes.create_unicode_buffer(32), w.DWORD()
                ok = self.api.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None, name, len(name))
                self.volumes[root] = serial.value if ok and name.value == 'NTFS' else None
            return self.volumes[root]

    @contextmanager
    def __call__(self, path, *, exclusive=False):
        volume = self.volume(path) if self.api else None
        if volume is None:
            yield None
            return
        # GENERIC_READ (including data access), SHARE_READ excludes writers.
        # Attribute-only handles do not participate in data sharing checks.
        # OPEN_EXISTING, BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
        handle = self.api.CreateFileW(str(path), 0x80000000, 0 if exclusive else 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            yield None
            return
        journal = None
        try:
            # Query the current journal identity on the same volume for every
            # fingerprint. A journal reset must not reuse an old file USN key.
            journal = self.api.CreateFileW(Path(path).anchor, 0, 7, None, 3, 0x02200000, None)
            if journal == ctypes.c_void_p(-1).value:
                yield None
                return
            from ctypes import wintypes as w
            def control(target, code, data=None):
                buffer, length = ctypes.create_string_buffer(4096), w.DWORD()
                if not self.api.DeviceIoControl(target, code, data, len(data) if data else 0,
                        buffer, len(buffer), ctypes.byref(length), None):
                    return None
                return buffer.raw[:length.value]
            identity = None
            def read():
                nonlocal identity
                basic, info = self.Basic(), self.Info()
                if (not self.api.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic))
                        or basic.attributes & 0x410):
                    return None
                if identity is None:
                    if not self.api.GetFileInformationByHandle(handle, ctypes.byref(info)) or info.volume != volume:
                        return None
                    # File ID and size cannot change through this handle while
                    # data writers and replacement are excluded by SHARE_READ.
                    identity = (info.volume, info.id_high << 32 | info.id_low, info.size_high << 32 | info.size_low)
                metadata = control(journal, 0x900f4)
                record = control(handle, 0x900eb, struct.pack('<HH', 2, 2))
                if metadata is None or len(metadata) < 56 or record is None or len(record) < 60:
                    return None
                journal_id, first, next_usn = struct.unpack_from('<Qqq', metadata)
                length, major, minor, file_id, parent_id, usn = struct.unpack_from('<IHHQQq', record)
                if (not journal_id or not 0 <= first <= next_usn or major != 2 or minor != 0
                        or length != len(record) or length < 60 or file_id != identity[1]
                        or not 0 < usn < next_usn):
                    return None
                values = (identity[0], identity[1], basic.created,
                          basic.written, basic.changed, identity[2], journal_id, usn)
                return values if all(value > 0 for value in values[1:5]) else None
            def close_record():
                data = control(handle, 0x900ef)
                if data is None or len(data) != 8:
                    return None
                usn, = struct.unpack('<q', data)
                return usn if usn > 0 else None
            read.close_record = close_record
            if exclusive:
                # The same handle must read bytes: reopening the pathname is
                # correctly blocked by our exclusive data-read sharing mode.
                def hash_content():
                    from ctypes import wintypes as w
                    value, buffer, length = hashlib.sha256(), ctypes.create_string_buffer(1024 * 1024), w.DWORD()
                    while True:
                        if not self.api.ReadFile(handle, buffer, len(buffer), ctypes.byref(length), None):
                            raise PatchError('独占校验期间读取失败：' + str(path))
                        if not length.value:
                            return value.hexdigest()
                        value.update(buffer.raw[:length.value])
                read.hash_content = hash_content
            yield read
        finally:
            if journal not in (None, ctypes.c_void_p(-1).value):
                self.api.CloseHandle(journal)
            self.api.CloseHandle(handle)


class VerifiedContentCache:
    LIMIT = 100000

    def __init__(self, path, fingerprint=None):
        self.path = Path(path)
        self.fingerprint = fingerprint if fingerprint is not None else NtfsFingerprint()
        self.tree_checkpoints = (TreeCheckpoints(self.path.with_name('verification-trees.json'), self.fingerprint)
                                 if isinstance(self.fingerprint, NtfsFingerprint) else None)
        self.lock = threading.Lock()
        self.entries = {}
        self.identities = {}
        self.dirty = False
        try:
            path = self.safe_path()
            if path.stat().st_size > 32 * 1024 * 1024:
                return
            envelope = json.loads(path.read_bytes())
            entries = envelope['entries']
            if (envelope.get('format') != 3 or not isinstance(entries, dict) or len(entries) > self.LIMIT
                    or hashlib.sha256(canonical(entries)).hexdigest() != envelope['checksum']):
                return
            for key, value in entries.items():
                parts = key.split(':')
                if (len(parts) != 8 or not all(part.isdecimal() for part in parts)
                        or not isinstance(value, str) or len(value) != 64
                        or any(c not in '0123456789abcdef' for c in value)):
                    return
            self.entries = entries
            # One verified version per file identity; repeated edits/toggles
            # must not retain every obsolete ChangeTime in the hint cache.
            for key in list(entries):
                identity = ':'.join(key.split(':')[:3])
                previous = self.identities.get(identity)
                if previous is not None:
                    self.entries.pop(previous, None)
                    self.dirty = True
                self.identities[identity] = key
        except (OSError, ValueError, KeyError, TypeError, PatchError):
            pass  # An absent/damaged optional cache never establishes trust.

    def safe_path(self):
        return ContentStore(self.path.parent)._path(self.path.name)

    def hash(self, path, reader, *, expected_stamp=None, validate=None, certify=None):
        context = (self.fingerprint(path, exclusive=True) if certify and isinstance(self.fingerprint, NtfsFingerprint)
                   else self.fingerprint(path))
        with context as read_stamp:
            before = read_stamp() if read_stamp else None
            if before is None:
                if validate:
                    validate()
                result = reader(path)
                if validate:
                    validate()
                return result
            # The strong, write-excluding handle already checks regular-file
            # type and stability. Do not open the path twice more with lstat.
            if expected_stamp is not None and (before[5], (before[3] - 116444736000000000) * 100) != tuple(expected_stamp[:2]):
                raise PatchError('预检期间文件发生变化，请刷新重试：' + str(path))
            key = ':'.join(map(str, before))
            with self.lock:
                known = self.entries.get(key)
            result = known if known is not None else (read_stamp.hash_content() if hasattr(read_stamp, 'hash_content') else reader(path))
            if read_stamp() != before:
                raise PatchError('校验期间文件发生变化，请刷新重试：' + str(path))
            # End any accumulated reason cycle before caching new content.
            # Otherwise an attribute-only reader can keep a prior write reason
            # alive and hide a later same-timestamp write behind the same USN.
            barrier = None
            if (known is None or certify) and hasattr(read_stamp, 'close_record'):
                barrier = read_stamp.close_record()
                after = read_stamp()
                if after is None or before[:7] != after[:7]:
                    raise PatchError('校验期间文件发生变化，请刷新重试：' + str(path))
                if barrier is None:
                    return result  # Full read is usable, but cannot seed a hit.
                if after[7] != barrier:
                    raise PatchError('校验期间文件变更日志发生变化，请刷新重试：' + str(path))
                before = after
                key = ':'.join(map(str, before))
            if certify and barrier is not None:
                certify(before, barrier)
            if known is None or barrier is not None:
                with self.lock:
                    identity = ':'.join(map(str, before[:3]))
                    previous = self.identities.get(identity)
                    if previous is not None:
                        self.entries.pop(previous, None)
                    self.entries[key] = result
                    self.identities[identity] = key
                    self.dirty = True
            return result

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            if len(self.entries) > self.LIMIT:
                self.entries = dict(list(self.entries.items())[-self.LIMIT:])
                self.identities = {':'.join(key.split(':')[:3]): key for key in self.entries}
            try:
                payload = {'format': 3, 'entries': self.entries,
                           'checksum': hashlib.sha256(canonical(self.entries)).hexdigest()}
                atomic_write(self.safe_path(), canonical(payload))
                self.dirty = False
            except (OSError, PatchError):
                logging.warning('校验缓存未保存；下次启动将重新校验内容。')

    @contextmanager
    def session(self):
        try:
            yield self
        finally:
            self.flush()
            if self.tree_checkpoints:
                self.tree_checkpoints.flush()
