"""Independent content-addressed store with verified reads and atomic writes.

Object bytes are canonical UTF-8 JSON; their SHA-256 is their permanent identity.
Snapshots keep their stable wire representation. Storage knows no UI or plugins.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from domain import DomainError, OBJECT_KINDS, digest, natural, object_reference, relative_path
from errors import StorageError


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8'))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def hash_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class ContentStore:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def _path(self, relative):
        # Store keys use one canonical, relative POSIX representation. Proving
        # that lexically means no second resolve traversal is needed below.
        try:
            relative_path(relative)
        except DomainError as exc:
            raise StorageError('内容路径超出存储范围或格式无效。') from exc
        path = self.root / relative
        # Check every ancestor on every access; never cache path safety. One
        # lstat supplies both POSIX link type and Windows reparse attributes.
        for ancestor in (path, *path.parents):
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                continue  # Writes may create missing directories after checking existing parents.
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise StorageError('内容存储不能经过链接：' + str(ancestor))
        return path

    def _read(self, relative, expected):
        try:
            data = self._path(relative).read_bytes()
        except OSError as exc:
            raise StorageError('记录文件缺失或无法读取：' + relative) from exc
        if hashlib.sha256(data).hexdigest() != expected:
            raise StorageError('内容校验失败，已停止覆盖：' + relative)
        return data

    def object(self, kind, value):
        if kind not in OBJECT_KINDS:
            raise StorageError('不支持的记录类型。')
        data = canonical(value)
        key = hashlib.sha256(data).hexdigest()
        ref = kind + '/' + key + '.json'
        path = self._path(ref)
        if path.exists():
            self._read(ref, key)
        else:
            atomic_write(path, data)
        return ref

    def read_object(self, ref):
        try:
            object_reference(ref)
        except DomainError as exc:
            raise StorageError('记录文件编号无效。') from exc
        try:
            return json.loads(self._read(ref, Path(ref).stem))
        except ValueError as exc:
            raise StorageError('记录内容损坏：' + ref) from exc

    def put_blob(self, data):
        key = hashlib.sha256(data).hexdigest()
        path = self._path('blobs/' + key)
        if path.exists():
            self.blob_bytes(key)
        else:
            atomic_write(path, data)
        return key

    def blob_bytes(self, key):
        try:
            digest(key)
        except DomainError as exc:
            raise StorageError('文件快照编号无效。') from exc
        return self._read('blobs/' + key, key)

    def store_file(self, path, stamp, cached=None):
        path = Path(path)
        try:
            if not isinstance(stamp, (tuple, list)) or len(stamp) != 3:
                raise DomainError('快照采样标识必须包含大小、修改时间和创建/变化时间。')
            stamp = [natural(value, '快照采样标识') for value in stamp]
        except DomainError as exc:
            raise StorageError('快照采样标识无效。') from exc
        def current():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise StorageError('只能保存普通文件的内容：' + str(path))
            return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        if current() != stamp:
            raise StorageError('快照期间文件仍在变化：' + str(path))
        # Windows ctime can mean creation time, so equal metadata does not prove
        # equal bytes. A cached identity may only avoid a write, never a read.
        if cached and cached.get('stamp') == stamp:
            key = cached.get('hash')
            self.blob_bytes(key)
            actual = hash_file(path)
            if current() != stamp:
                raise StorageError('快照期间文件仍在变化：' + str(path))
            if actual == key:
                return key
        directory = self._path('blobs')
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / (uuid.uuid4().hex + '.tmp')
        try:
            h = hashlib.sha256()
            with path.open('rb') as source, temp.open('xb') as target:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    h.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            if current() != stamp:
                raise StorageError('快照期间文件仍在变化：' + str(path))
            key = h.hexdigest()
            destination = self._path('blobs/' + key)
            if destination.exists():
                self.blob_bytes(key)
            else:
                os.replace(temp, destination)
            return key
        finally:
            temp.unlink(missing_ok=True)

    def statistics(self):
        counts = {}
        for kind in sorted(OBJECT_KINDS | {'blobs'}):
            folder = self._path(kind)
            count, size = 0, 0
            for candidate in folder.glob('*'):
                # A count must not follow a link that regular reads would reject.
                path = self._path(kind + '/' + candidate.name)
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue  # An atomic writer may have just renamed its temp.
                if stat.S_ISREG(info.st_mode):
                    count += 1
                    size += info.st_size
            counts[kind] = {'count': count, 'bytes': size}
        return {'groups': counts, 'bytes': sum(g['bytes'] for g in counts.values()),
                'retention': 'keep-reachable-history'}
