"""Read-only file overlay used to plan a whole switch before touching live files."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat

from patch_modes import PatchError, file_hash
from domain import DomainError, SnapshotEntry


class FileView:
    def __init__(self, roots, hashes=None):
        self.roots = [Path(root) for root in roots]
        self.entries = {}
        self.hashes = {} if hashes is None else hashes
        for root in self.roots:
            # The root itself may be represented as a link, but reaching a
            # descendant through a linked ancestor would silently enumerate an
            # unrelated tree. Recheck even paths resolved earlier by settings.
            for parent in root.parents:
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise PatchError('预检根目录的上级路径不能经过链接：' + str(parent))
            self._capture(Path(root))
        self._index_children()

    def _index_children(self):
        self.child_paths = {}
        for path in self.entries:
            self.child_paths.setdefault(path.parent, set()).add(path)

    def __deepcopy__(self, memo):
        clone = object.__new__(type(self))
        clone.roots = list(self.roots)
        clone.entries = copy.deepcopy(self.entries, memo)
        clone.child_paths = {path: set(children) for path, children in self.child_paths.items()}
        # All branches of one read-only plan share content reads. A fresh view
        # before execution gets an independent cache and hashes disk again.
        clone.hashes = self.hashes
        return clone

    def _capture(self, path, info=None):
        if path in self.entries:
            return
        try:
            info = path.lstat() if info is None else info
        except FileNotFoundError:
            return  # A configured root may legitimately be absent.
        if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
            entry = {'type': 'link', 'target': str(path.resolve())}
        elif stat.S_ISDIR(info.st_mode):
            entry = {'type': 'dir'}
            try:
                # Recheck directories before descending: an entry observed by
                # its parent must not have become a junction in the meantime.
                current = path.lstat()
                if (not stat.S_ISDIR(current.st_mode)
                        or getattr(current, 'st_file_attributes', 0) & 0x400):
                    raise PatchError('预检期间目录被替换，请刷新重试：' + str(path))
                with os.scandir(path) as children:
                    for child in children:
                        # On Windows this reuses enumeration metadata instead
                        # of opening every child for exists() and lstat().
                        self._capture(Path(child.path), child.stat(follow_symlinks=False))
            except OSError as exc:
                raise PatchError('预检期间目录发生变化或无法读取：' + str(path)) from exc
        elif stat.S_ISREG(info.st_mode):
            entry = {'type': 'file', 'size': info.st_size, 'mtime': info.st_mtime_ns,
                     'stamp': [info.st_size, info.st_mtime_ns, info.st_ctime_ns], 'source': path}
        else:
            raise PatchError('预检遇到不支持的文件类型：' + str(path))
        self.entries[path] = entry

    def exists(self, path):
        return Path(path) in self.entries

    def is_file(self, path):
        return self.entries.get(Path(path), {}).get('type') == 'file'

    def is_dir(self, path):
        return self.entries.get(Path(path), {}).get('type') == 'dir'

    def _stable(self, entry):
        try:
            info = entry['source'].lstat()
        except OSError as exc:
            raise PatchError('预检期间文件发生变化或无法读取：' + str(entry['source'])) from exc
        if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or [info.st_size, info.st_mtime_ns, info.st_ctime_ns] != entry['stamp']):
            raise PatchError('预检期间文件发生变化，请刷新重试：' + str(entry['source']))

    def hash(self, path):
        entry = self.entries.get(Path(path))
        if not entry or entry['type'] != 'file':
            return None
        if 'hash' not in entry:
            self._stable(entry)
            key = (entry['source'], tuple(entry['stamp']))
            if key not in self.hashes:
                self.hashes[key] = file_hash(entry['source'])
            entry['hash'] = self.hashes[key]
            self._stable(entry)
        return entry['hash']

    def read_bytes(self, path):
        entry = self.entries[Path(path)]
        if entry['type'] != 'file':
            raise PatchError('预检需要普通文件：' + str(path))
        if 'stamp' in entry:
            self._stable(entry)
        content = entry['source'].read_bytes()
        if hashlib.sha256(content).hexdigest() != self.hash(path):
            raise PatchError('预检文件内容已改变：' + str(path))
        if 'stamp' in entry:
            self._stable(entry)
        return content

    def open(self, path):
        # ASAR readers stream from the original or an already verified blob.
        entry = self.entries[Path(path)]
        self.hash(path)
        if 'stamp' in entry:
            self._stable(entry)
        return entry['source'].open('rb')

    def entry(self, path):
        entry = self.entries.get(Path(path))
        if entry is None:
            return None
        if entry['type'] == 'file':
            return {'type': 'file', 'hash': self.hash(path), 'size': entry['size'], 'mtime': entry['mtime']}
        return {k: v for k, v in entry.items() if k in ('type', 'target')}

    def children(self, root):
        return sorted(self.child_paths.get(root, ()), key=lambda p: p.name.lower())

    def tree_hash(self, root, *, content=False):
        root = Path(root)
        if not self.is_dir(root):
            raise PatchError('补丁目录不可用或被替换为链接：' + str(root))
        h = hashlib.sha256()
        def visit(directory):
            for path in self.children(directory):
                entry = self.entries[path]
                relative = path.relative_to(root).as_posix()
                if entry['type'] == 'dir':
                    visit(path)
                    line = ['dir', relative]
                elif entry['type'] == 'link':
                    line = ['link', relative, entry['target']]
                elif content:
                    line = ['file', relative, self.hash(path)]
                else:
                    line = ['file', relative, entry['size'], entry['mtime']]
                h.update(json.dumps(line, ensure_ascii=False).encode('utf-8'))
        visit(root)
        return h.hexdigest()

    def change(self, path, before, after, equal, source=None):
        path = Path(path)
        try:
            for value in (before, after):
                if value is not None:
                    SnapshotEntry.decode(value)
        except DomainError as exc:
            raise PatchError('切换文件记录无效：' + str(exc)) from exc
        actual = self.entry(path)
        if equal(actual, after):
            return False  # Never undo a no-op into a state we did not start with.
        if not equal(actual, before):
            raise PatchError('切换前预检发现文件冲突，未修改应用：' + str(path))
        if after is None:
            if self.children(path):
                raise PatchError('切换前预检发现目录中仍有其他文件：' + str(path))
            self.entries.pop(path, None)
            self.child_paths.get(path.parent, set()).discard(path)
        else:
            parent = self.entries.get(path.parent)
            covered_parent = any(path.parent == root or path.parent.is_relative_to(root) for root in self.roots)
            if (parent is not None and parent['type'] != 'dir') or (parent is None and covered_parent):
                raise PatchError('目标父路径不是目录：' + str(path.parent))
            self.entries[path] = {**after, 'source': source} if after['type'] == 'file' else dict(after)
            # Snapshot timestamps describe the restored destination, while the
            # bytes in this overlay come from a content-addressed blob. Never
            # compare that blob's stat against the original live file's stamp.
            self.entries[path].pop('stamp', None)
            self.child_paths.setdefault(path.parent, set()).add(path)
        return True

    def move(self, source, dest):
        source, dest = Path(source), Path(dest)
        if source == dest or source.is_relative_to(dest) or dest.is_relative_to(source):
            raise PatchError('切换目录不能移动到自身或相互嵌套的路径。')
        if not self.exists(source) or self.exists(dest):
            raise PatchError('切换前预检发现目录冲突：' + str(dest))
        pending, moving = [(source, dest)], []
        while pending:
            path, target = pending.pop()
            moving.append((path, target))
            pending.extend((child, target / child.name) for child in self.children(path))
        for path, target in moving:
            self.entries[target] = self.entries.pop(path)
        self._index_children()

    def snapshot(self, scopes, progress=None):
        result = {}
        def visit(scope, path, relative):
            yield scope + '|' + relative, path
            if self.is_dir(path):
                for child in self.children(path):
                    if child.name.startswith('.harness-launcher-'):
                        continue
                    yield from visit(scope, child, child.name if relative == '.' else relative + '/' + child.name)
        paths = ((key, path) for scope, root in scopes.items() if self.exists(root)
                 for key, path in visit(scope, root, '.'))
        count = 0
        if progress:
            paths = list(paths)
            total = sum(self.is_file(path) for _, path in paths)
            progress(0, total)
        for key, path in paths:
            result[key] = self.entry(path)
            if progress and self.is_file(path):
                count += 1
                progress(count, total)
        return result
