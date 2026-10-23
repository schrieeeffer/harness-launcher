"""Typed transaction commands and strict decoding at the persistence boundary.

Path ownership is enforced by the selected executor after decoding. No command
is allowed to infer its engine or to fall through to a different command kind.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from domain import DomainError, ManagedPath, SnapshotEntry, digest as content_digest, natural


class OperationError(ValueError):
    pass


def digest(value, *, optional=False):
    if optional and value is None:
        return value
    return content_digest(value)


def absolute_path(value):
    if (not isinstance(value, str) or not value or '\x00' in value
            or not Path(value).is_absolute() or '..' in Path(value).parts):
        raise OperationError('操作路径必须是规范的绝对路径')
    return value


def timestamp(value):
    return None if value is None else natural(value, '文件时间戳')


def entry(value):
    return SnapshotEntry.decode(value) if value is not None else None


@dataclass(frozen=True)
class TrackingOperation:
    key: ManagedPath
    before: SnapshotEntry | None
    after: SnapshotEntry | None


@dataclass(frozen=True)
class FileOperation:
    path: str
    before: str | None
    after: str | None
    before_mtime: int | None
    after_mtime: int | None


@dataclass(frozen=True)
class MoveOperation:
    source: str
    dest: str
    directory: bool
    fingerprint: str
    content_fingerprint: str


def decode_operation(value):
    if not isinstance(value, dict):
        raise OperationError('操作必须是对象')
    try:
        if value.get('engine') == 'tracking':
            if set(value) != {'engine', 'key', 'before', 'after'}:
                raise OperationError('文件差异操作字段无效')
            return TrackingOperation(ManagedPath.decode(value['key']), entry(value['before']), entry(value['after']))
        if value.get('engine') != 'core':
            raise OperationError('未知操作执行器')
        if value.get('kind') == 'file':
            if set(value) != {'engine', 'kind', 'path', 'before', 'after', 'beforeMtime', 'afterMtime'}:
                raise OperationError('文件写入操作字段无效')
            return FileOperation(absolute_path(value['path']), digest(value['before'], optional=True),
                digest(value['after'], optional=True), timestamp(value['beforeMtime']), timestamp(value['afterMtime']))
        if value.get('kind') == 'move':
            if set(value) != {'engine', 'kind', 'source', 'dest', 'directory', 'fingerprint', 'contentFingerprint'}:
                raise OperationError('移动操作字段无效')
            source, dest = absolute_path(value['source']), absolute_path(value['dest'])
            if (Path(source) == Path(dest) or Path(dest).is_relative_to(Path(source))
                    or Path(source).is_relative_to(Path(dest)) or type(value['directory']) is not bool):
                raise OperationError('移动路径或文件类型无效')
            return MoveOperation(source, dest, value['directory'], digest(value['fingerprint']),
                                 digest(value['contentFingerprint']))
    except KeyError as exc:
        raise OperationError('操作缺少必需字段') from exc
    except DomainError as exc:
        raise OperationError(str(exc)) from exc
    raise OperationError('未知操作类型')


def validate_operations(values):
    if not isinstance(values, list):
        raise OperationError('操作列表格式无效')
    return tuple(decode_operation(value) for value in values)
