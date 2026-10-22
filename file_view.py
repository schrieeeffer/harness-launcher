"""Read-only file overlay used to plan a whole switch before touching live files."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat

from patch_modes import PatchError, file_hash


class FileView:
    def __init__(self, roots):
        self.roots = [Path(root) for root in roots]
        self.entries = {}
        self.hashes = {}
        for root in self.roots:
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

    def _capture(self, path):
        if path in self.entries or not (path.exists() or path.is_symlink()):
            return
        info = path.lstat()
        if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
            entry = {'type': 'link', 'target': str(path.resolve())}
        elif stat.S_ISDIR(info.st_mode):
            entry = {'type': 'dir'}
            for child in path.iterdir():
                self._capture(child)
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
        info = entry['source'].stat()
        if [info.st_size, info.st_mtime_ns, info.st_ctime_ns] != entry['stamp']:
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

    def tree_hash(self, root):
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
                else:
                    line = ['file', relative, entry['size'], entry['mtime']]
                h.update(json.dumps(line, ensure_ascii=False).encode('utf-8'))
        visit(root)
        return h.hexdigest()

    def change(self, path, before, after, equal, source=None):
        path = Path(path)
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
            self.child_paths.setdefault(path.parent, set()).add(path)
        return True

    def move(self, source, dest):
        source, dest = Path(source), Path(dest)
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

    def snapshot(self, scopes):
        result = {}
        def visit(scope, path, relative):
            result[scope + '|' + relative] = self.entry(path)
            if self.is_dir(path):
                for child in self.children(path):
                    if child.name.startswith('.harness-launcher-'):
                        continue
                    visit(scope, child, child.name if relative == '.' else relative + '/' + child.name)
        for scope, root in scopes.items():
            if self.exists(root):
                visit(scope, root, '.')
        return result
