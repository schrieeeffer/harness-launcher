"""Read-only file overlay used to plan a whole switch before touching live files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import stat

from patch_modes import PatchError
from storage import hash_file as file_hash
from domain import DomainError, SnapshotEntry


class FileView:
    def __init__(self, roots, hashes=None, verification_cache=None):
        self.roots = [Path(root) for root in roots]
        self.entries = {}
        self.hashes = {} if hashes is None else hashes
        self.verification_cache = verification_cache
        self.checkpoints = getattr(verification_cache, 'tree_checkpoints', None)
        self.candidates = {}
        self.tree_hashes = {}
        self.fingerprint_sinks = {}
        cached_children, cached_paths = {}, set()
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
            cached, candidate = self.checkpoints.begin(root) if self.checkpoints else (None, None)
            if cached is not None:
                self.entries.update(cached)
                cached_paths.update(cached)
                for path, children in cached.child_paths.items():
                    cached_children.setdefault(path, set()).update(children)
                self.fingerprint_sinks[root] = cached.fingerprints
                self.tree_hashes.update({(root, content): value for content, value in cached.fingerprints.items()})
            else:
                self._capture(root)
                if candidate:
                    self.candidates[root] = candidate
        self._index_children(cached_children, cached_paths)

    def _index_children(self, cached_children, cached_paths):
        self.child_paths = cached_children
        for path in self.entries:
            if path not in cached_paths:
                self.child_paths.setdefault(path.parent, set()).add(path)

    def __deepcopy__(self, memo):
        clone = object.__new__(type(self))
        clone.roots = list(self.roots)
        # Paths and scalar identities are immutable. Copying Path objects for
        # every overlay branch needlessly reparses tens of thousands of paths.
        clone.entries = {path: dict(entry, stamp=list(entry['stamp'])) if 'stamp' in entry else dict(entry)
                         for path, entry in self.entries.items()}
        clone.child_paths = {path: set(children) for path, children in self.child_paths.items()}
        # Branches share reads only within this plan. Execution gets a fresh
        # view and revalidates disk identities before any persisted cache hit.
        clone.hashes = self.hashes
        clone.verification_cache = self.verification_cache
        clone.checkpoints = self.checkpoints
        clone.candidates = {}  # Virtual overlays must never certify real disk.
        clone.tree_hashes = dict(self.tree_hashes)
        clone.fingerprint_sinks = {}  # Overlay fingerprints cannot certify disk.
        return clone

    def _capture(self, path, info=None):
        if path in self.entries:
            return
        try:
            info = path.lstat() if info is None else info
        except FileNotFoundError:
            return  # A configured root may legitimately be absent.
        if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
            file_id = path.lstat().st_ino
            barrier = self.checkpoints.close_entry(path, file_id) if self.checkpoints else None
            entry = {'type': 'link', 'target': str(path.resolve()), 'file_id': file_id, 'proof_usn': barrier}
        elif stat.S_ISDIR(info.st_mode):
            entry = {'type': 'dir'}
            try:
                # Recheck directories before descending: an entry observed by
                # its parent must not have become a junction in the meantime.
                current = path.lstat()
                if (not stat.S_ISDIR(current.st_mode)
                        or getattr(current, 'st_file_attributes', 0) & 0x400):
                    raise PatchError('预检期间目录被替换，请刷新重试：' + str(path))
                entry['file_id'] = current.st_ino
                entry['proof_usn'] = self.checkpoints.close_entry(path, current.st_ino) if self.checkpoints else None
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
        return path in self.entries if isinstance(path, Path) else Path(path) in self.entries

    def is_file(self, path):
        return self.entries.get(path if isinstance(path, Path) else Path(path), {}).get('type') == 'file'

    def is_dir(self, path):
        return self.entries.get(path if isinstance(path, Path) else Path(path), {}).get('type') == 'dir'

    def _stable(self, entry):
        try:
            info = entry['source'].lstat()
        except OSError as exc:
            raise PatchError('预检期间文件发生变化或无法读取：' + str(entry['source'])) from exc
        if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or [info.st_size, info.st_mtime_ns, info.st_ctime_ns] != entry['stamp']):
            raise PatchError('预检期间文件发生变化，请刷新重试：' + str(entry['source']))

    def hash(self, path):
        entry = self.entries.get(path if isinstance(path, Path) else Path(path))
        if not entry or entry['type'] != 'file':
            return None
        if 'hash' not in entry:
            key = (entry['source'], tuple(entry['stamp']))
            if self.verification_cache and key not in self.hashes:
                self.hashes[key] = self.verification_cache.hash(entry['source'], file_hash,
                    expected_stamp=entry['stamp'], validate=lambda: self._stable(entry),
                    certify=lambda value, usn: entry.update(ntfsIdentity=value, file_id=value[1], proof_usn=usn))
            else:
                self._stable(entry)
                if key not in self.hashes:
                    self.hashes[key] = file_hash(entry['source'])
                self._stable(entry)
            entry['hash'] = self.hashes[key]
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
        entry = self.entries.get(path if isinstance(path, Path) else Path(path))
        if entry is None:
            return None
        if entry['type'] == 'file':
            return {'type': 'file', 'hash': self.hash(path), 'size': entry['size'], 'mtime': entry['mtime']}
        return {k: v for k, v in entry.items() if k in ('type', 'target')}

    def children(self, root):
        return sorted(self.child_paths.get(root, ()), key=lambda p: p.name.lower())

    def hash_files(self, paths=None, progress=None):
        """Read independent files concurrently within this one verification pass.

        Each fresh pass rechecks file identity and stability; an optional NTFS
        cache avoids reading unchanged bytes. Only this thread reports
        progress so request-local observers never leak into worker contexts.
        """
        paths = list(dict.fromkeys(path for path in (self.entries if paths is None else paths)
                                   if self.is_file(path)))
        total = len(paths)
        paths = [path for path in paths if 'hash' not in self.entries[path]]
        completed = total - len(paths)
        if progress:
            progress(completed, total)
        if len(paths) < 32:
            for done, path in enumerate(paths, completed + 1):
                self.hash(path)
                if progress:
                    progress(done, total)
            self._seal_checkpoints()
            return
        workers = 32 if total >= 1024 else 8
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='file-check') as pool:
            pending = [pool.submit(self.hash, path) for path in paths]
            try:
                for done, future in enumerate(as_completed(pending), completed + 1):
                    future.result()
                    if progress:
                        progress(done, total)
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
        self._seal_checkpoints()

    def _seal_checkpoints(self):
        for root, candidate in list(self.candidates.items()):
            # Follow the existing child index. A small CLI/rules directory
            # must not filter every path in an unrelated 13,000-file app.
            entries, pending = {}, [root]
            while pending:
                path = pending.pop()
                if path in self.entries:
                    entries[path] = self.entries[path]
                    pending.extend(self.child_paths.get(path, ()))
            self.checkpoints.seal(root, candidate, entries)
        self.candidates.clear()

    def tree_hash(self, root, *, content=False):
        root = Path(root)
        if not self.is_dir(root):
            raise PatchError('补丁目录不可用或被替换为链接：' + str(root))
        key = (root, content)
        if key in self.tree_hashes:
            return self.tree_hashes[key]
        h = hashlib.sha256()
        def visit(directory, prefix=''):
            for path in self.children(directory):
                entry = self.entries[path]
                relative = prefix + path.name
                if entry['type'] == 'dir':
                    visit(path, relative + '/')
                    line = ['dir', relative]
                elif entry['type'] == 'link':
                    line = ['link', relative, entry['target']]
                elif content:
                    line = ['file', relative, self.hash(path)]
                else:
                    line = ['file', relative, entry['size'], entry['mtime']]
                h.update(json.dumps(line, ensure_ascii=False).encode('utf-8'))
        visit(root)
        self.tree_hashes[key] = h.hexdigest()
        if root in self.fingerprint_sinks:
            self.fingerprint_sinks[root][content] = self.tree_hashes[key]
        return self.tree_hashes[key]

    def change(self, path, before, after, equal, source=None):
        self.candidates.clear()
        self.fingerprint_sinks.clear()
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
        self.tree_hashes = {key: value for key, value in self.tree_hashes.items()
                            if not (path == key[0] or path.is_relative_to(key[0]) or key[0].is_relative_to(path))}
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
        self.candidates.clear()
        self.fingerprint_sinks.clear()
        source, dest = Path(source), Path(dest)
        if source == dest or source.is_relative_to(dest) or dest.is_relative_to(source):
            raise PatchError('切换目录不能移动到自身或相互嵌套的路径。')
        if not self.exists(source) or self.exists(dest):
            raise PatchError('切换前预检发现目录冲突：' + str(dest))
        pending, moving = [(source, dest)], []
        while pending:
            path, target = pending.pop()
            moving.append((path, target))
            prefix = str(target) + os.sep
            pending.extend((child, Path(prefix + child.name)) for child in self.child_paths.get(path, ()))
        for path, target in moving:
            self.entries[target] = self.entries.pop(path)
        # Update only the moved subtree. Rebuilding every parent's index after
        # a one-file rename repeatedly reparsed the unrelated application tree.
        destinations = dict(moving)
        for path, target in moving:
            children = self.child_paths.pop(path, None)
            if children is not None:
                self.child_paths[target] = {destinations[child] for child in children}
        self.child_paths.get(source.parent, set()).discard(source)
        self.child_paths.setdefault(dest.parent, set()).add(dest)
        tree_hashes, self.tree_hashes = self.tree_hashes, {}
        # A whole-tree rename changes locations, not any relative fingerprint.
        for (root, content), value in tree_hashes.items():
            if root == source or root.is_relative_to(source):
                self.tree_hashes[(dest / root.relative_to(source), content)] = value
            elif not source.is_relative_to(root) and not dest.is_relative_to(root):
                self.tree_hashes[(root, content)] = value

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
        if progress:
            paths = list(paths)
            self.hash_files((path for _, path in paths), progress)
        for key, path in paths:
            result[key] = self.entry(path)
        self._seal_checkpoints()
        return result
