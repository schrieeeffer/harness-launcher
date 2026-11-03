"""Content-addressed maintenance checkpoints with conflict-aware file replay.

Only fixed roots supplied by the launcher are writable. No recursive deletion,
no following package links, and no restoration of arbitrary absolute paths.
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import stat
import time

from domain import relative_path
from errors import LauncherError
from storage import atomic_write, hash_file


class CheckpointFiles:
    def __init__(self, roots, store, *, link_targets=()):
        self.roots = {name: Path(path).absolute() for name, path in roots.items()}
        self.store = store
        # Read-only link destinations; their contents are never traversed or
        # restored through a checkpoint link. Supplied by the installation.
        self.link_targets = tuple(Path(path).absolute() for path in link_targets)

    @staticmethod
    def linked(info):
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

    @staticmethod
    def link_target(target):
        # readlink(junction) returns an extended Win32 path. CreateJunction
        # adds this prefix itself; passing it twice creates a dangling link.
        if os.name == 'nt' and target.startswith('\\\\?\\'):
            target = ('\\\\' + target[8:]) if target[4:8].upper() == 'UNC\\' else target[4:]
        return target

    def path(self, key, *, leaf_link=False):
        try:
            root, relative = key.split('|', 1)
            relative_path(relative, allow_root=True)
            base = self.roots[root]
        except (ValueError, KeyError) as exc:
            raise LauncherError('恢复路径不属于本次维护范围。') from exc
        path = base if relative == '.' else base / relative
        for ancestor in (path.parent, *path.parent.parents):
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                continue
            if self.linked(info):
                raise LauncherError('恢复路径经过链接，已停止：' + str(ancestor))
        if not leaf_link:
            try:
                if self.linked(path.lstat()):
                    raise LauncherError('恢复目标被链接替换，已停止：' + str(path))
            except FileNotFoundError:
                pass
        return path

    def checked_link(self, key, target):
        path = self.path(key, leaf_link=True)
        scope, relative = key.split('|', 1)
        if relative == '.':
            raise LauncherError('维护根目录不能是链接。')
        target = self.link_target(target)
        resolved = Path(os.path.abspath(path.parent / target))
        if scope == 'modules':
            resolved = resolved.resolve()
            return resolved, not resolved.is_relative_to(self.roots['modules'])
        if not any(resolved == root or resolved.is_relative_to(root) for root in self.link_targets):
            raise LauncherError('维护链接目标不属于已配置的运行时目录：' + str(path))
        for ancestor in (resolved, *resolved.parents):
            try:
                if self.linked(ancestor.lstat()):
                    raise LauncherError('运行时链接目标经过其他链接，已停止：' + str(ancestor))
            except FileNotFoundError:
                continue
        return resolved, False

    def entry(self, key, store=False):
        path = self.path(key, leaf_link=True)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if self.linked(info):
            target = self.link_target(os.readlink(path))
            _, external = self.checked_link(key, target)
            return {'type': 'link', 'target': target, 'directory': path.is_dir(),
                    'external': external}
        if stat.S_ISDIR(info.st_mode):
            return {'type': 'dir'}
        if not stat.S_ISREG(info.st_mode):
            raise LauncherError('维护范围含特殊文件，已停止：' + str(path))
        stamp = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        digest = self.store.store_file(path, stamp) if store else hash_file(path)
        now = path.stat()
        if stamp != [now.st_size, now.st_mtime_ns, now.st_ctime_ns]:
            raise LauncherError('维护期间文件仍在写入：' + str(path))
        return {'type': 'file', 'hash': digest, 'size': info.st_size,
                'mtime': info.st_mtime_ns, 'mode': stat.S_IMODE(info.st_mode)}

    def snapshot(self, store=True, progress=None):
        result, files = {}, []
        count, last_report = 0, 0
        def visit(key, info=None):
            if info is None:
                path = self.path(key, leaf_link=True)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    return
            if stat.S_ISREG(info.st_mode) and not self.linked(info):
                files.append(key)
                return
            entry = self.entry(key)
            if entry is None:
                return
            result[key] = entry
            if entry['type'] == 'dir':
                # Reuse directory enumeration metadata. Each file's worker
                # independently rechecks every ancestor before reading bytes.
                with os.scandir(self.path(key)) as scanned:
                    children = sorted(scanned, key=lambda child: child.name)
                names = [child.name for child in children]
                if len({n.casefold() for n in names}) != len(names):
                    raise LauncherError('目录内存在大小写冲突，不能建立 Windows 恢复记录。')
                for child in children:
                    name = child.name
                    visit(key.removesuffix('.') + name if key.endswith('|.') else key + '/' + name,
                          child.stat(follow_symlinks=False))
        for root in self.roots:
            visit(root + '|.')
        if progress:
            progress(0, len(files))
        def read(key):
            entry = self.entry(key, store)
            if not entry or entry['type'] != 'file':
                raise LauncherError('维护期间文件被删除或替换：' + key)
            return entry
        def accept(key, entry):
            nonlocal count, last_report
            result[key] = entry
            count += 1
            if progress and time.monotonic() - last_report >= .2:
                progress(count, len(files))
                last_report = time.monotonic()
        if len(files) < 32:
            for key in files:
                accept(key, read(key))
        else:
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix='checkpoint') as pool:
                pending = {pool.submit(read, key): key for key in files}
                try:
                    for future in as_completed(pending):
                        accept(pending[future], future.result())
                except BaseException:
                    for future in pending:
                        future.cancel()
                    raise
        if progress:
            progress(count, count)
        return result

    @staticmethod
    def identity(entry):
        return {k: v for k, v in entry.items() if k not in ('mtime', 'mode')} if entry else None

    @classmethod
    def changes(cls, before, after):
        return [key for key in sorted(set(before) | set(after))
                if cls.identity(before.get(key)) != cls.identity(after.get(key))]

    def restore(self, desired, expected, before_write=lambda: None):
        changes = self.changes(expected, desired)
        # After an interrupted directory-to-link rollback the parent link may
        # already be restored. Its former managed children are now absent from
        # this checkpoint's namespace; never inspect them through that link.
        restored_links = [key for key, entry in desired.items() if entry['type'] == 'link'
                          and self.identity(self.entry(key)) == self.identity(entry)]
        hidden = {key for key in changes if any(key.startswith(parent + '/') for parent in restored_links)}
        if any(key in desired for key in hidden):
            raise LauncherError('恢复记录不能在链接目录内包含子文件。')
        changes = [key for key in changes if key not in hidden]
        for key in changes:
            self.path(key, leaf_link=True)
            actual = self.entry(key)
            if self.identity(actual) not in (self.identity(expected.get(key)), self.identity(desired.get(key))):
                raise LauncherError('文件在检查点之后被修改，已保留现场：' + key, code='maintenance_conflict')
            entry = desired.get(key)
            if entry and entry['type'] == 'file':
                self.store.blob_bytes(entry['hash'])
            elif entry and entry['type'] == 'link':
                _, external = self.checked_link(key, entry['target'])
                if external and not entry.get('external'):
                    raise LauncherError('依赖链接越界。')
        # Remove only leaves and empty directories. A new/unrecognized file
        # makes rmdir fail; it is never recursively erased with its parent.
        def depth(key):
            return len(self.path(key, leaf_link=True).parts)
        for key in sorted(changes, key=depth, reverse=True):
            before_write()
            old, new = self.entry(key), desired.get(key)
            if self.identity(old) == self.identity(new) or old is None:
                continue
            if self.identity(old) != self.identity(expected.get(key)):
                raise LauncherError('恢复期间文件再次改变：' + key, code='maintenance_conflict')
            if new and old['type'] == new['type'] == 'file':
                continue
            path = self.path(key, leaf_link=True)
            if old['type'] == 'dir' or old['type'] == 'link' and path.is_junction():
                path.rmdir()
            else:
                path.unlink()
        for key in sorted(changes, key=depth):
            before_write()
            new = desired.get(key)
            if not new:
                continue
            old = self.entry(key)
            if self.identity(old) == self.identity(new):
                continue
            if old is not None and self.identity(old) != self.identity(expected.get(key)):
                raise LauncherError('恢复期间目标再次改变：' + key, code='maintenance_conflict')
            path = self.path(key)
            if new['type'] == 'dir':
                path.mkdir(parents=True, exist_ok=True)
            elif new['type'] == 'file':
                atomic_write(path, self.store.blob_bytes(new['hash']))
                os.chmod(path, new['mode'])
                os.utime(path, ns=(new['mtime'], new['mtime']))
            elif new['type'] == 'link':
                target, external = self.checked_link(key, new['target'])
                if external and not new.get('external'):
                    raise LauncherError('依赖链接越界。')
                if os.name == 'nt' and new['directory']:
                    import _winapi
                    _winapi.CreateJunction(str(target), str(path))
                else:
                    os.symlink(new['target'], path, target_is_directory=new['directory'])
