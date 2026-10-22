"""Immutable package revisions and reproducible combination descriptions.

Package contents are recovery artifacts, never executed by this module.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import re
import stat
import time

from patch_modes import PatchError, archive_file, file_hash, put, sha


NAME = re.compile(r'^(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*$')


class VersionStore:
    def __init__(self, tracker):
        self.tracker = tracker
        self.profile = tracker.manager.profile
        self.view = None

    @contextmanager
    def read_view(self, view):
        previous = self.view
        self.view = view
        try:
            yield
        finally:
            self.view = previous

    def object(self, kind, value):
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        relative = kind + '/' + sha(content) + '.json'
        path = self.tracker.root / relative
        if path.exists() and path.read_bytes() != content:
            raise PatchError('版本记录校验失败，请保留原始记录：' + relative)
        if not path.exists():
            put(path, content)
        return relative

    def files(self, root, store=True):
        """Capture installed code, not user data; reject ambiguous nested links."""
        result = {}
        def visit(directory):
            for child in sorted(directory.iterdir()):
                if child.name == 'node_modules' and child.is_dir():
                    continue  # Dependency edges are captured explicitly below.
                info = child.lstat()
                if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
                    raise PatchError('插件代码含嵌套链接，无法建立完整版本记录：' + str(child))
                key = child.relative_to(root).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    result[key] = {'type': 'dir'}
                    visit(child)
                elif stat.S_ISREG(info.st_mode):
                    stamp = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                    digest = self.tracker.store_file(child, stamp) if store else file_hash(child)
                    now = child.stat()
                    if [now.st_size, now.st_mtime_ns, now.st_ctime_ns] != stamp:
                        raise PatchError('插件代码仍在变化，版本记录未保存：' + str(child))
                    result[key] = {'type': 'file', 'hash': digest, 'size': info.st_size,
                                   'executable': bool(info.st_mode & stat.S_IXUSR)}
                else:
                    raise PatchError('插件代码含不支持的文件类型：' + str(child))
        visit(root)
        return result

    def resolve_dependency(self, root, name):
        if not NAME.fullmatch(name):
            raise PatchError('插件依赖名称无效：' + str(name))
        # Node resolves dependencies from the real package directory upwards.
        for directory in (root, *root.parents):
            if directory.name == 'node_modules':
                continue
            candidate = directory / 'node_modules' / name
            if (candidate / 'package.json').is_file():
                return candidate.resolve()
        return None

    def capture(self, name, source='', store=True, metadata=True):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise PatchError('插件名称无效，无法保存版本。')
        root = (self.profile / 'node_modules' / name).resolve()
        queue, ids, packages = [root], {root: '0'}, {}
        while queue:
            path = queue.pop(0)
            meta = json.loads((path / 'package.json').read_bytes())
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
                        provided = self.host_dependency(dep)
                        if provided:
                            dependencies[dep] = provided
                            continue
                    optional_peer = meta.get('peerDependenciesMeta', {}).get(dep, {}).get('optional')
                    if dep in optional or (dep in peers and optional_peer):
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
                    result['installMetadata'][name] = self.tracker.store_file(path, stamp) if store else file_hash(path)
        return result

    def host_dependency(self, name):
        core = self.tracker.core
        archive = core.official_archive(self.view)
        if archive is None:
            return None
        try:
            content = archive_file(archive, 'dsh/node_modules/' + name + '/package.json',
                                   core.resources / 'app.asar.unpacked', self.view)
        except PatchError:
            return None
        meta = json.loads(content)
        if meta.get('name') != name:
            raise PatchError('Harness 依赖名称不匹配：' + name)
        return {'providedBy': 'harness', 'name': name, 'version': meta['version'], 'manifestHash': sha(content),
                'hostHash': file_hash(Path(self.tracker.manager.settings['executable'])),
                'archiveHash': self.view.hash(archive) if self.view else file_hash(archive)}

    @staticmethod
    def adapter_patch(state):
        return {key: value for key, value in state.items() if key not in ('mode', 'package', 'revision')}

    def bind_adapter(self, state, source='installed:dsh-purge'):
        package = self.package_snapshot('dsh-purge', source)
        revision = self.object('revisions', {'format': 1, 'plugin': 'dsh-purge', 'adapter': 'dsh-purge',
            'version': state['pluginVersion'], 'package': package, 'patch': self.adapter_patch(state)})
        return dict(state, package=package, revision=revision)

    def verify_adapter(self, state):
        revision = self.tracker.read_object(state['revision'])
        if (revision.get('adapter') != 'dsh-purge' or revision['package'] != state['package']
                or revision['patch'] != self.adapter_patch(state)):
            raise PatchError('内置补丁记录与不可变版本不一致。')
        self.verify_package('dsh-purge', state['package'])

    def package_snapshot(self, name, source=''):
        capsule = self.capture(name, source)
        # Compare again without trusting timestamps; source mutation cannot be
        # silently associated with a different dependency graph or package tree.
        if capsule != self.capture(name, source, store=False):
            raise PatchError('插件或依赖仍在写入，完整版本尚未保存。')
        for package in capsule['packages'].values():
            for entry in package['files'].values():
                if entry['type'] == 'file':
                    self.tracker.blob_bytes(entry['hash'])
        return self.object('packages', capsule)

    def verify_package(self, name, reference):
        expected = self.tracker.read_object(reference)
        actual = self.capture(name, expected.get('source', ''), store=False, metadata=False)
        if actual != {k: v for k, v in expected.items() if k != 'installMetadata'}:
            raise PatchError('插件或依赖内容与版本记录不同，请通过启动器重新记录升级：' + name)
        for package in expected['packages'].values():
            for entry in package['files'].values():
                if entry['type'] == 'file':
                    self.tracker.blob_bytes(entry['hash'])
        for digest in expected.get('installMetadata', {}).values():
            self.tracker.blob_bytes(digest)

    def verify_record(self, name, record):
        revision = self.tracker.read_object(record['revision'])
        defaults = {'baseVersions': {}, 'source': ''}
        for key in ('version', 'delta', 'package', 'hostHash', 'baseSelection', 'baseVersions', 'baseline', 'after', 'source', 'manifestBefore'):
            if revision.get(key) != record.get(key, defaults.get(key)):
                raise PatchError('插件索引与不可变版本记录不一致：' + name)
        if revision.get('plugin') != name:
            raise PatchError('插件版本记录名称不匹配：' + name)
        self.tracker.read_object(record['delta'])
        if revision['deltaHash'] != sha((self.tracker.root / record['delta']).read_bytes()):
            raise PatchError('插件文件差异记录已损坏：' + name)

    def revision(self, name, record, package=None, source='', parent=None, origin='installation'):
        self.tracker.read_object(record['delta'])
        value = {'format': 1, 'plugin': name, 'version': record['version'], 'source': source,
                 'package': package, 'delta': record['delta'], 'parent': parent,
                 'deltaHash': sha((self.tracker.root / record['delta']).read_bytes()),
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
        return {key: {k: entry[k] for k in ('type', 'hash', 'target') if k in entry}
                for key, entry in sorted(snapshot.items())}
