"""Validated value objects at persistence and filesystem boundaries.

Content identity deliberately excludes timestamps. Snapshot metadata belongs to
the original live file, never to the blob used to restore that file.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import PurePosixPath
import re
from typing import Mapping


class DomainError(ValueError):
    pass


PLUGIN_NAME = re.compile(r'^(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*$')
DIGEST = re.compile(r'^[a-f0-9]{64}$')
OBJECT_KINDS = frozenset(('snapshots', 'deltas', 'packages', 'revisions', 'combinations', 'baselines', 'transactions'))


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise DomainError('文件内容标识必须是 SHA-256。')
    return value


def object_reference(value, kind=None):
    if not isinstance(value, str):
        raise DomainError('记录引用必须是字符串。')
    parts = value.split('/')
    if (len(parts) != 2 or parts[0] not in OBJECT_KINDS or not parts[1].endswith('.json')
            or not DIGEST.fullmatch(parts[1][:-5]) or kind and parts[0] != kind):
        raise DomainError('记录引用的类型或内容标识无效。')
    return value


def natural(value, field):
    if type(value) is not int or value < 0:
        raise DomainError(field + ' 必须是非负整数。')
    return value


def relative_path(value, *, allow_root=False):
    """Reject both traversal and Windows filename aliases before joining paths."""
    if not isinstance(value, str) or not value:
        raise DomainError('路径必须是非空字符串。')
    return _relative_path(value, bool(allow_root))


@lru_cache(maxsize=32768)
def _relative_path(value, allow_root):
    # This caches syntax validation of immutable strings only. Filesystem
    # existence, ancestry, identities and contents must still be checked live.
    path = PurePosixPath(value)
    if (path.is_absolute() or '..' in path.parts or str(path) != value
            or '\\' in value or ':' in value or value == '.' and not allow_root):
        raise DomainError('路径包含非规范或越界路径。')
    for part in path.parts:
        # Win32 strips trailing dots/spaces and interprets device names even
        # when they have an extension. Different journal keys must never name
        # the same file or an operating-system device.
        if (part.endswith(('.', ' ')) or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                or re.fullmatch(r'(?:CON|CONIN\$|CONOUT\$|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])',
                                part.split('.', 1)[0].rstrip(' '), flags=re.IGNORECASE)):
            raise DomainError('路径包含 Windows 保留名称或别名。')
    return path


@dataclass(frozen=True)
class ManagedPath:
    scope: str
    relative: str

    @classmethod
    def decode(cls, value):
        if not isinstance(value, str) or value.count('|') != 1:
            raise DomainError('托管路径格式无效。')
        scope, relative = value.split('|', 1)
        if not re.fullmatch(r'[a-z][a-z_]*', scope):
            raise DomainError('托管路径包含非规范或越界路径。')
        relative_path(relative, allow_root=True)
        return cls(scope, relative)


class FileKind(StrEnum):
    FILE = 'file'
    DIRECTORY = 'dir'
    LINK = 'link'


@dataclass(frozen=True)
class ContentIdentity:
    kind: FileKind
    hash: str | None = None
    target: str | None = None

    @classmethod
    def decode(cls, value):
        if not isinstance(value, Mapping):
            raise DomainError('文件内容身份必须是对象。')
        try:
            kind = FileKind(value['type'])
        except (KeyError, ValueError, TypeError) as exc:
            raise DomainError('文件类型无效。') from exc
        if kind is FileKind.FILE:
            if 'target' in value:
                raise DomainError('普通文件不能携带链接目标。')
            return cls(kind, digest(value.get('hash')))
        if kind is FileKind.LINK:
            if 'hash' in value:
                raise DomainError('链接不能携带文件内容哈希。')
            target = value.get('target')
            if not isinstance(target, str) or not target or '\x00' in target:
                raise DomainError('链接目标无效。')
            return cls(kind, target=target)
        if 'hash' in value or 'target' in value:
            raise DomainError('目录不能携带文件内容或链接目标。')
        return cls(kind)

    def to_dict(self):
        result = {'type': self.kind.value}
        if self.kind is FileKind.FILE:
            result['hash'] = self.hash
        elif self.kind is FileKind.LINK:
            result['target'] = self.target
        return result


@dataclass(frozen=True)
class SnapshotMetadata:
    size: int
    mtime_ns: int
    stamp: tuple[int, int, int] | None = None

    @classmethod
    def decode(cls, value):
        if not isinstance(value, Mapping):
            raise DomainError('文件快照元信息必须是对象。')
        size = natural(value.get('size'), '文件大小')
        mtime = natural(value.get('mtime'), '文件修改时间')
        stamp = value.get('stamp')
        if stamp is not None:
            if not isinstance(stamp, (list, tuple)) or len(stamp) != 3:
                raise DomainError('快照采样标识格式无效。')
            stamp = tuple(natural(item, '快照采样标识') for item in stamp)
            if stamp[:2] != (size, mtime):
                raise DomainError('快照元信息与原文件采样标识不一致。')
        return cls(size, mtime, stamp)

    def to_dict(self):
        result = {'size': self.size, 'mtime': self.mtime_ns}
        if self.stamp is not None:
            result['stamp'] = list(self.stamp)
        return result


@dataclass(frozen=True)
class SnapshotEntry:
    identity: ContentIdentity
    metadata: SnapshotMetadata | None = None

    @classmethod
    def decode(cls, value):
        identity = ContentIdentity.decode(value)
        metadata = SnapshotMetadata.decode(value) if identity.kind is FileKind.FILE else None
        if identity.kind is not FileKind.FILE and any(key in value for key in ('size', 'mtime', 'stamp', 'hash')):
            raise DomainError('目录或链接不能携带文件快照元信息。')
        return cls(identity, metadata)

    def to_dict(self):
        return {**self.identity.to_dict(), **(self.metadata.to_dict() if self.metadata else {})}


def validate_snapshot(value):
    """Validate content snapshots and the distinct metadata inventory shape."""
    if not isinstance(value, dict):
        raise DomainError('快照必须是路径映射。')
    shape = None
    for key, entry in value.items():
        ManagedPath.decode(key)
        current = 'inventory' if isinstance(entry, list) else 'content'
        if shape is not None and shape != current:
            raise DomainError('不能混合文件内容快照与元信息清单。')
        shape = current
        if current == 'content':
            SnapshotEntry.decode(entry)
        elif entry and entry[0] == 'file' and len(entry) == 3:
            natural(entry[1], '文件大小')
            natural(entry[2], '文件修改时间')
        elif entry == ['dir']:
            pass
        elif len(entry) == 2 and entry[0] == 'link':
            ContentIdentity.decode({'type': 'link', 'target': entry[1]})
        else:
            raise DomainError('文件元信息清单无效。')
    return value


def validate_changes(value):
    if not isinstance(value, dict) or not isinstance(value.get('changes'), list):
        raise DomainError('文件差异记录必须包含变更列表。')
    seen = set()
    for item in value['changes']:
        if not isinstance(item, dict) or not {'key', 'before', 'after'} <= item.keys():
            raise DomainError('文件差异缺少路径或前后内容。')
        ManagedPath.decode(item['key'])
        if item['key'] in seen:
            raise DomainError('文件差异不能重复包含同一路径。')
        seen.add(item['key'])
        for side in ('before', 'after'):
            if item[side] is not None:
                SnapshotEntry.decode(item[side])
        if item['before'] is None and item['after'] is None:
            raise DomainError('文件差异不能同时缺少前后内容。')
    return value
