"""Immutable package revisions and reproducible combination descriptions.

Package contents are recovery artifacts, never executed by this module.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import stat
import time

from patch_modes import PatchError, archive_file, file_hash, sha
from domain import ContentIdentity, DomainError
from operations import report_progress


NAME = re.compile(r'^(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*$')


class VersionStore:
    def __init__(self, profile, executable, content_store, host_archive, verification_cache=None):
        self.profile = Path(profile)
        self.executable = Path(executable)
        self.store = content_store
        self.host_archive = host_archive
        self.view = None
        self.verification_cache = verification_cache

    def hash(self, path):
        return self.verification_cache.hash(path, file_hash) if self.verification_cache else file_hash(path)

    @contextmanager
    def read_view(self, view):
        previous = self.view
        self.view = view
        try:
            yield
        finally:
            self.view = previous

    def object(self, kind, value):
        return self.store.object(kind, value)

    @staticmethod
    def checked_items(items, action, label):
        """Bound independent reads; publish progress only on the request thread."""
        items = list(items)
        total, last_report = len(items), 0
        report_progress(None, label, 0, total)
        def update(done):
            nonlocal last_report
            now = time.monotonic()
            if done == total or now - last_report >= .2:
                report_progress(None, label, done, total)
                last_report = now
        if total < 32:
            for done, item in enumerate(items, 1):
                result = action(item)
                update(done)
                yield item, result
            return
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix='package-check') as pool:
            pending = {pool.submit(action, item): item for item in items}
            try:
                for done, future in enumerate(as_completed(pending), 1):
                    result = future.result()
                    update(done)
                    yield pending[future], result
            except BaseException:
                for future in pending:
                    future.cancel()
                raise

    def files(self, root, store=True):
        """Capture installed code, not user data; reject ambiguous nested links."""
        result, files = {}, {}
        def visit(directory):
            with os.scandir(directory) as scanned:
                children = sorted(scanned, key=lambda entry: entry.name)
            for scanned in children:
                child = Path(scanned.path)
                if directory == root and child.name == 'node_modules' and child.is_dir():
                    continue  # Dependency edges are captured explicitly below.
                info = scanned.stat(follow_symlinks=False)
                if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
                    raise PatchError('插件代码含嵌套链接，无法建立完整版本记录：' + str(child))
                key = child.relative_to(root).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    result[key] = {'type': 'dir'}
                    visit(child)
                elif stat.S_ISREG(info.st_mode):
                    files[child] = (key, info)
                else:
                    raise PatchError('插件代码含不支持的文件类型：' + str(child))
        visit(root)
        def read(child):
            key, info = files[child]
            stamp = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            digest = self.store.store_file(child, stamp) if store else self.hash(child)
            now = child.lstat()
            if (not stat.S_ISREG(now.st_mode) or getattr(now, 'st_file_attributes', 0) & 0x400
                    or [now.st_size, now.st_mtime_ns, now.st_ctime_ns] != stamp):
                raise PatchError('插件代码仍在变化，版本记录未保存：' + str(child))
            return {'type': 'file', 'hash': digest, 'size': info.st_size,
                    'executable': bool(info.st_mode & stat.S_IXUSR)}
        for child, entry in self.checked_items(files, read, ('保存' if store else '校验') + '插件文件：' + root.name):
            result[files[child][0]] = entry
        return result

    def resolve_dependency(self, root, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise PatchError('插件依赖名称无效：' + str(name))
        # Node resolves dependencies from the real package directory upwards.
        for directory in (root, *root.parents):
            if directory.name == 'node_modules':
                continue
            candidate = directory / 'node_modules' / name
            # CommonJS resolves files before package directories. A loose
            # module can shadow a valid package at this or a higher level;
            # recording that other package would verify the wrong runtime code.
            if any(Path(str(candidate) + suffix).is_file() for suffix in ('', '.js', '.json', '.node')):
                raise PatchError('插件依赖被无清单模块遮蔽，无法保存完整版本：' + str(candidate))
            if (candidate / 'package.json').is_file():
                return candidate.resolve()
            if any((candidate / ('index' + suffix)).is_file() for suffix in ('.js', '.json', '.node')):
                raise PatchError('插件依赖缺少 package.json，无法保存完整版本：' + str(candidate))
        return None

    def capture(self, name, source='', store=True, metadata=True, *, root=None):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise PatchError('插件名称无效，无法保存版本。')
        root = Path(root).resolve() if root is not None else (self.profile / 'node_modules' / name).resolve()
        queue, ids, packages = [root], {root: '0'}, {}
        while queue:
            path = queue.pop(0)
            meta = json.loads((path / 'package.json').read_bytes())
            if not isinstance(meta, dict):
                raise PatchError('插件依赖清单必须是对象：' + str(path))
            package_id = ids[path]
            dependencies = {}
            required = meta.get('dependencies', {})
            optional = meta.get('optionalDependencies', {})
            peers = meta.get('peerDependencies', {})
            for section in (required, optional, peers):
                if not isinstance(section, dict):
                    raise PatchError('插件依赖清单无效：' + str(path))
            for dep in sorted(set(required) | set(optional) | set(peers)):
                resolved = self.resolve_dependency(path, dep)
                if resolved is None:
                    if dep in peers and dep not in required and dep not in optional:
                        dsh = meta.get('dsh', {})
                        client = dsh.get('client', {}) if isinstance(dsh, dict) else {}
                        provided = self.host_dependency(dep, browser=isinstance(client, dict) and client.get('platform') == 'web')
                        if provided:
                            dependencies[dep] = provided
                            continue
                    optional_peer = meta.get('peerDependenciesMeta', {}).get(dep, {}).get('optional')
                    if dep in optional or (dep in peers and dep not in required and optional_peer):
                        dependencies[dep] = {'missing': True, 'optional': True}
                        continue
                    raise PatchError('插件依赖缺失，不能保存完整版本：' + dep)
                if resolved not in ids:
                    ids[resolved] = str(len(ids))
                    queue.append(resolved)
                dependencies[dep] = {'package': ids[resolved],
                                     'requested': optional.get(dep, required.get(dep, peers.get(dep)))}
            files = self.files(path, store)
            package_bytes = (path / 'package.json').read_bytes()
            if sha(package_bytes) != files['package.json']['hash'] or json.loads(package_bytes) != meta:
                raise PatchError('读取期间插件依赖清单发生变化：' + str(path))
            packages[package_id] = {'name': meta.get('name'), 'version': str(meta.get('version', '未知')),
                                    'files': files, 'dependencies': dependencies}
        if packages['0']['name'] != name:
            raise PatchError('版本记录中的插件包名不匹配：' + name)
        result = {'format': 1, 'plugin': name, 'source': source, 'packages': packages}
        if metadata:
            result['installMetadata'] = {}
            for name in ('package.json', 'package-lock.json', 'npm-shrinkwrap.json',
                         'pnpm-lock.yaml', 'yarn.lock', 'node_modules/.package-lock.json'):
                path = self.profile / name
                if path.is_symlink() or path.is_junction():
                    raise PatchError('安装元数据不能是链接：' + str(path))
                if path.is_file():
                    info = path.stat()
                    stamp = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                    result['installMetadata'][name] = self.store.store_file(path, stamp) if store else self.hash(path)
        return result

    def host_dependency(self, name, *, browser=False):
        core = self.host_archive
        archive = core.official_archive(self.view)
        if archive is None:
            return None
        try:
            content = archive_file(archive, 'dsh/node_modules/' + name + '/package.json',
                                   core.resources / 'app.asar.unpacked', self.view)
        except PatchError:
            if browser:
                from host_modules import browser_peer
                provided = browser_peer(core, archive, name, self.view)
                if provided:
                    return dict(provided, hostHash=self.hash(self.executable),
                                archiveHash=self.view.hash(archive) if self.view else self.hash(archive))
            return None
        meta = json.loads(content)
        if meta.get('name') != name:
            raise PatchError('Harness 依赖名称不匹配：' + name)
        return {'providedBy': 'harness', 'name': name, 'version': meta['version'], 'manifestHash': sha(content),
                'hostHash': self.hash(self.executable),
                'archiveHash': self.view.hash(archive) if self.view else self.hash(archive)}

    @staticmethod
    def adapter_patch(state):
        return {key: value for key, value in state.items() if key not in ('mode', 'package', 'revision')}

    def bind_adapter(self, state, adapter, source=None, *, package=None):
        # A supervised capture may revise native-side bytes using its original
        # checkpoint. Reuse its just-captured package instead of scanning twice.
        package = package or self.package_snapshot(adapter, source or 'installed:' + adapter)
        revision = self.object('revisions', {'format': 1, 'plugin': adapter, 'adapter': adapter,
            'version': state['pluginVersion'], 'package': package, 'patch': self.adapter_patch(state)})
        return dict(state, package=package, revision=revision)

    def verify_adapter(self, state, adapter, *, installed=True):
        revision = self.store.read_object(state['revision'])
        if (revision.get('adapter') != adapter or revision['package'] != state['package']
                or revision['patch'] != self.adapter_patch(state)):
            raise PatchError('内置补丁记录与不可变版本不一致。')
        if installed:
            self.verify_package(adapter, state['package'])

    def package_snapshot(self, name, source=''):
        capsule = self.capture(name, source)
        # Compare again without trusting timestamps; source mutation cannot be
        # silently associated with a different dependency graph or package tree.
        if capsule != self.capture(name, source, store=False):
            raise PatchError('插件或依赖仍在写入，完整版本尚未保存。')
        self.verify_blobs(capsule)
        return self.object('packages', capsule)

    def verify_blobs(self, capsule):
        keys = {entry['hash'] for package in capsule['packages'].values()
                for entry in package['files'].values() if entry['type'] == 'file'}
        keys.update(capsule.get('installMetadata', {}).values())
        # Same content may occur in several packages; verify each blob once in
        # this pass, with no persistent trust based on timestamps.
        for _ in self.checked_items(keys, lambda key: bool(self.store.blob_bytes(key)), '校验恢复副本'):
            pass

    def verify_package(self, name, reference):
        expected = self.store.read_object(reference)
        actual = self.capture(name, expected.get('source', ''), store=False, metadata=False)
        if actual != {k: v for k, v in expected.items() if k != 'installMetadata'}:
            raise PatchError('插件或依赖内容与版本记录不同，旧补丁不能用于更新后的插件：' + name,
                code='plugin_identity_changed', details={'plugin': name},
                recovery='保留记录；先恢复记录对应的插件包及依赖，再从“升级 / 重新记录”开始。仅修改版本号或清空缓存不能恢复旧基线。')
        self.verify_blobs(expected)

    def verify_external_source(self, name, reference, root):
        """Read-only check before restoring a local link; never overwrite its project."""
        expected = self.store.read_object(reference)
        actual = self.capture(name, expected.get('source', ''), store=False, metadata=False, root=root)
        if actual != {k: v for k, v in expected.items() if k != 'installMetadata'}:
            raise PatchError('原本地插件源项目已修改，已保留其内容并停止恢复链接：' + name,
                             code='maintenance_conflict')

    def verify_record(self, name, record):
        revision = self.store.read_object(record['revision'])
        defaults = {'baseVersions': {}, 'source': ''}
        for key in ('version', 'delta', 'package', 'hostHash', 'baseSelection', 'baseVersions', 'baseline', 'after', 'source', 'manifestBefore'):
            if revision.get(key) != record.get(key, defaults.get(key)):
                raise PatchError('插件索引与不可变版本记录不一致：' + name)
        if revision.get('plugin') != name:
            raise PatchError('插件版本记录名称不匹配：' + name)
        self.store.read_object(record['delta'])
        if revision['deltaHash'] != Path(record['delta']).stem:
            raise PatchError('插件文件差异记录已损坏：' + name)

    def revision(self, name, record, package=None, source='', parent=None, origin='installation'):
        self.store.read_object(record['delta'])
        value = {'format': 1, 'plugin': name, 'version': record['version'], 'source': source,
                 'package': package, 'delta': record['delta'], 'parent': parent,
                 'deltaHash': Path(record['delta']).stem,
                 'baseline': record.get('baseline'), 'after': record.get('after'),
                 'manifestBefore': record.get('manifestBefore'),
                 'baseSelection': record['baseSelection'], 'baseVersions': record.get('baseVersions', {}),
                 'hostHash': record.get('hostHash'), 'origin': origin, 'recordedAt': time.time()}
        return self.object('revisions', value)

    def lock(self, selection, plugins, value, host_hash):
        available = {p['id']: p for p in plugins}
        ordered = [name for name in value['order'] if name in selection]
        layers = []
        for name in ordered:
            record = value['records'][name]
            layers.append({'plugin': name, 'version': record['version'], 'delta': record['delta'],
                           'revision': record['revision'], 'package': record['package']})
        return {'format': 1, 'hostHash': host_hash, 'selection': list(selection), 'order': ordered,
                'plugins': {name: available[name]['version'] for name in selection}, 'layers': layers}

    @staticmethod
    def content_state(snapshot):
        try:
            return {key: ContentIdentity.decode(entry).to_dict() for key, entry in sorted(snapshot.items())}
        except DomainError as exc:
            raise PatchError('组合文件内容身份无效：' + str(exc)) from exc
