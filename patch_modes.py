"""Reversible, journaled desktop layers. No plugin scripts or shell hooks run here.

The dsh-purge adapter parks the extracted app and known injected rules, so native
mode uses the retained official ASAR. Plugin mode restores the exact captured
layer, rather than running a broad patch script against multiple installations.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import uuid


class PatchError(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def tree_hash(root, view=None):
    """Fast change fingerprint: metadata for files, targets for junctions.

    The official archive is verified by content separately. Reading every DLL/JS
    in the extracted tree triggers very slow per-file antivirus scans on Windows;
    size/mtime/identity detect normal edits without opening thousands of files.
    """
    if view is not None:
        return view.tree_hash(root)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise PatchError(f'补丁目录不可用或被替换为链接：{root}')
    h = hashlib.sha256()
    def visit(directory):
        for entry in sorted(os.scandir(directory), key=lambda p: p.name.lower()):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            if getattr(info, 'st_file_attributes', 0) & 0x400 or entry.is_symlink():
                line = ['link', relative, str(path.resolve())]
            elif stat.S_ISDIR(info.st_mode):
                line = ['dir', relative]
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                line = ['file', relative, info.st_size, info.st_mtime_ns]
            else:
                raise PatchError(f'无法核验补丁文件：{path}')
            h.update(json.dumps(line, ensure_ascii=False).encode('utf-8'))
    visit(root)
    return h.hexdigest()


def archive_info(archive, unpacked, verify=False, view=None):
    """Read ASAR without executing its code. Validate shipped integrity records."""
    try:
        with (view.open(archive) if view else archive.open('rb')) as stream:
            header = struct.unpack('<4I', stream.read(16))
            if header[0] != 4 or not (0 < header[3] < 32 * 1024 * 1024):
                raise ValueError('header')
            tree = json.loads(stream.read(header[3]))
            base = 8 + header[1]
            def entries(node, prefix=''):
                for name, value in node.get('files', {}).items():
                    if name in ('.', '..') or '/' in name or '\\' in name:
                        raise ValueError('path')
                    path = prefix + name
                    if 'files' in value:
                        yield from entries(value, path + '/')
                    elif 'link' not in value:
                        yield path, value
            package = None
            for name, value in entries(tree):
                if not verify and name != 'package.json':
                    continue
                if value.get('unpacked'):
                    path = unpacked.joinpath(*name.split('/'))
                    content = view.read_bytes(path) if view else path.read_bytes()
                else:
                    stream.seek(base + int(value['offset']))
                    content = stream.read(value['size'])
                if len(content) != value['size']:
                    raise ValueError('size')
                integrity = value.get('integrity')
                if integrity and (integrity.get('algorithm') != 'SHA256' or sha(content) != integrity['hash']):
                    raise PatchError('官方归档或其解包依赖的完整性校验失败：' + name)
                if name == 'package.json':
                    package = json.loads(content)
            if not package or package.get('name') != '@deepseek-ai/dsh-desktop':
                raise ValueError('not official desktop archive')
            return {'version': package['version'], 'commit': package.get('dshBuildCommit', '')}
    except PatchError:
        raise
    except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
        raise PatchError('无法验证保留的官方 app.asar，未修改应用。') from exc


def archive_file(archive, name, unpacked, view=None):
    """Read one host-provided package manifest without executing Electron."""
    try:
        with (view.open(archive) if view else archive.open('rb')) as stream:
            header = struct.unpack('<4I', stream.read(16))
            if header[0] != 4 or not 0 < header[3] < 32 * 1024 * 1024:
                raise ValueError('header')
            node = json.loads(stream.read(header[3]))
            for part in name.split('/'):
                if part in ('', '.', '..'):
                    raise ValueError('path')
                node = node['files'][part]
            if node.get('unpacked'):
                path = unpacked.joinpath(*name.split('/'))
                content = view.read_bytes(path) if view else path.read_bytes()
            else:
                stream.seek(8 + header[1] + int(node['offset']))
                content = stream.read(node['size'])
            integrity = node.get('integrity')
            if len(content) != node['size'] or (integrity and
                    (integrity.get('algorithm') != 'SHA256' or sha(content) != integrity['hash'])):
                raise ValueError('integrity')
            return content
    except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
        raise PatchError('无法验证 Harness 提供的依赖：' + name) from exc


class PatchModes:
    def __init__(self, manager):
        self.manager = manager
        self.home = manager.home
        self.resources = Path(manager.settings['executable']).resolve().parent / 'resources'
        key = sha((str(self.resources).lower() + '|' + str(self.home).lower()).encode())[:16]
        self.root = manager.data / 'patches' / key
        self.record = self.root / 'state.json'
        self.app = self.resources / 'app'
        self.archive = self.resources / 'app.asar'
        self.stash = self.resources / ('.harness-launcher-' + key)
        self.home_stash = self.home / ('.harness-launcher-' + key)

    def official_archive(self, view=None):
        # Capture and switching must agree when plugin mode has parked the ASAR.
        candidates = (self.archive, self.stash / 'official.asar', self.resources / 'app.asar.bak')
        return next((path for path in candidates if (view.is_file(path) if view else path.is_file())), None)

    def load(self):
        if not self.record.is_file():
            return None
        try:
            state = json.loads(self.record.read_text(encoding='utf-8'))
            if state.get('format') != 2 or not state.get('package') or not state.get('revision') or state.get('pending'):
                raise ValueError()
            if state['resources'] != str(self.resources) or state['home'] != str(self.home):
                raise ValueError()
            return state
        except (OSError, ValueError, KeyError):
            raise PatchError('补丁记录需要迁移或已损坏，请先运行 migrate.py 并保留 patches 目录。')

    def save(self, state):
        put(self.record, (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

    def blob(self, content):
        if content is None:
            return None
        key = sha(content)
        path = self.root / 'blobs' / key
        if not path.exists():
            put(path, content)
        return key

    def bytes(self, key):
        if key is None:
            return None
        if not re.fullmatch('[a-f0-9]{64}', key):
            raise PatchError('补丁快照编号无效。')
        try:
            content = (self.root / 'blobs' / key).read_bytes()
        except OSError as exc:
            raise PatchError('内置补丁快照缺失或无法读取，未覆盖应用。') from exc
        if sha(content) != key:
            raise PatchError('补丁快照校验失败，未覆盖应用。')
        return content

    def summary(self, plugins):
        try:
            state = self.load()
            known = any(p['id'] == 'dsh-purge' for p in plugins)
            unknown = [p['id'] for p in plugins if p['id'] != 'dsh-purge']
            return {'supported': known or state is not None, 'mode': state['mode'] if state else 'unmanaged',
                    'verifiedNative': bool(state and state['mode'] == 'native'
                                           and self.archive.is_file() and not self.app.exists() and not unknown),
                    'pending': False, 'unknown': unknown,
                    'managed': ['dsh-purge'] if known or state else [],
                    'archiveAvailable': self.official_archive() is not None, 'error': ''}
        except PatchError as exc:
            return {'supported': True, 'mode': 'error', 'verifiedNative': False, 'pending': False,
                    'unknown': [], 'managed': [], 'archiveAvailable': False, 'error': str(exc)}

    def safe(self, path):
        """Every mutation stays inside this exact installation/profile."""
        path = Path(path)
        allowed = (self.resources.resolve(), self.home.resolve())
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root) and resolved != root for root in allowed):
            raise PatchError('拒绝操作安装/数据目录以外的文件：' + str(path))
        if path.is_symlink() or path.is_junction():
            raise PatchError('拒绝覆盖链接：' + str(path))
        targets = {self.app, self.archive, self.resources / 'app.asar.bak',
                   self.stash / 'app', self.stash / 'official.asar',
                   self.home / 'prompt-inject.md', self.home / 'AGENTS.md', self.home / 'CLAUDE.md',
                   self.home / '.agent-presets' / 'redteam', self.home_stash / 'redteam',
                   self.resources / 'runtime' / 'cli' / 'bin' / 'dsh.cmd', self.manager.manifest_path}
        if path not in targets:
            raise PatchError('文件不属于已适配的补丁目标：' + str(path))
        return path

    def file_op(self, path, desired, view=None):
        self.safe(path)
        before = view.read_bytes(path) if view and view.is_file(path) else path.read_bytes() if not view and path.is_file() else None
        return {'kind': 'file', 'path': str(path), 'before': self.blob(before), 'after': self.blob(desired)}

    def move_op(self, source, dest, view=None):
        self.safe(source)
        self.safe(dest)
        if not (view.exists(source) if view else source.exists()) or (view.exists(dest) if view else dest.exists()):
            raise PatchError('切换目录存在冲突，未覆盖任何目录：' + str(dest))
        return {'kind': 'move', 'source': str(source), 'dest': str(dest)}

    def replay(self, operations, undo=False):
        for op in reversed(operations) if undo else operations:
            self.manager.require_stopped()
            if op['kind'] == 'file':
                path = self.safe(Path(op['path']))
                before, after = (op['after'], op['before']) if undo else (op['before'], op['after'])
                actual = file_hash(path)
                if actual == after:
                    continue
                if actual != before:
                    raise PatchError('文件在切换期间被其他程序修改，已停止：' + str(path))
                content = self.bytes(after)
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    put(path, content)
            elif op['kind'] == 'move':
                source, dest = Path(op['source']), Path(op['dest'])
                if undo:
                    source, dest = dest, source
                self.safe(source)
                self.safe(dest)
                if dest.exists() and not source.exists():
                    continue
                if not source.exists() or dest.exists():
                    raise PatchError('目录在切换期间发生冲突，请保留两侧文件：' + str(source))
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, dest)
            else:
                raise PatchError('未知的补丁日志操作。')

    def capture(self, plugins, view=None, persist=True):
        purge = next((p for p in plugins if p['id'] == 'dsh-purge' and p['available']), None)
        if not purge:
            raise PatchError('找不到 dsh-purge，无法建立可逆补丁快照。')
        if any(p['enabled'] and p['id'] != 'dsh-purge' for p in plugins):
            raise PatchError('首次建立补丁快照时存在其他已启用插件，无法确认目录归属。请先完成这些插件的还原适配。')
        archive = self.official_archive(view)
        if archive is None:
            raise PatchError('找不到保留的官方 app.asar。请确认 Harness 安装路径，并保留现有补丁记录与暂存目录。')
        info = archive_info(archive, self.resources / 'app.asar.unpacked', verify=True, view=view)
        if not (view.is_dir(self.app) if view else self.app.is_dir()):
            raise PatchError('尚无已应用的插件补丁目录。请先在 dsh-purge 中完成一次应用，再使用自动切换。')
        app_package = json.loads(view.read_bytes(self.app / 'package.json') if view else (self.app / 'package.json').read_bytes())
        if app_package.get('version') != info['version'] or app_package.get('dshBuildCommit', '') != info['commit']:
            raise PatchError('补丁目录与官方归档的版本不同，不能自动切换。')
        files = []
        # Only known plugin-owned injection files; never read credentials/sessions.
        active = None
        rules_state = self.home / 'rules' / 'state.json'
        if view.is_file(rules_state) if view else rules_state.is_file():
            active = json.loads(view.read_bytes(rules_state) if view else rules_state.read_bytes()).get('active')
        active_rule = None
        if isinstance(active, str) and re.fullmatch('[a-zA-Z0-9_-]+', active):
            rule_path = self.home / 'rules' / (active + '.md')
            if view.is_file(rule_path) if view else rule_path.is_file():
                active_rule = view.read_bytes(rule_path) if view else rule_path.read_bytes()
        for name in ['prompt-inject.md', 'AGENTS.md', 'CLAUDE.md']:
            path = self.home / name
            if not (view.is_file(path) if view else path.is_file()):
                continue
            content = view.read_bytes(path) if view else path.read_bytes()
            backup = path.with_name(path.name + '.dsh-purge-rule.bak')
            owned = (name == 'prompt-inject.md' or (view.is_file(backup) if view else backup.is_file()) or content == active_rule
                     or b'Little Code Sauce' in content or b'UNRESTRICTED SANDBOX' in content)
            if owned:
                original = (view.read_bytes(backup) if view else backup.read_bytes()) if (view.is_file(backup) if view else backup.is_file()) else None
                files.append({'path': str(path), 'initial': self.blob(content),
                              'plugin': self.blob(content), 'native': self.blob(original)})
        # Desktop CLI repair is outside resources/app; preserve both versions.
        cli = self.resources / 'runtime' / 'cli' / 'bin' / 'dsh.cmd'
        backup = cli.with_name(cli.name + '.dsh-purge.bak')
        if view.is_file(cli) if view else cli.is_file():
            initial_cli = view.read_bytes(cli) if view else cli.read_bytes()
            original_cli = (view.read_bytes(backup) if view else backup.read_bytes()) if (view.is_file(backup) if view else backup.is_file()) else initial_cli
            if b'ELECTRON_RUN_AS_NODE' not in original_cli or b'dsh-desktop-host' not in original_cli:
                raise PatchError('桌面 CLI 包装脚本不是受支持的官方格式，未修改脚本。')
            original_cli = original_cli.replace(b'\\app\\', b'\\app.asar\\')
            patched_cli = initial_cli.replace(b'\\app.asar\\', b'\\app\\')
            files.append({'path': str(cli), 'initial': self.blob(initial_cli),
                          'plugin': self.blob(patched_cli), 'native': self.blob(original_cli)})
        preset = self.home / '.agent-presets' / 'redteam'
        directories = []
        if view.is_dir(preset) if view else preset.is_dir():
            directories.append({'active': str(preset), 'parked': str(self.home_stash / 'redteam'),
                                'hash': tree_hash(preset, view)})
        state = {'format': 2, 'resources': str(self.resources), 'home': str(self.home),
                 'mode': 'captured', 'pluginVersion': purge['version'], 'archiveHash': (view.hash(archive) if view else file_hash(archive)),
                 'archiveInfo': info, 'initialArchive': str(archive), 'treeHash': tree_hash(self.app, view),
                 'files': files, 'directories': directories}
        state = self.manager.tracking.versions.bind_adapter(state)
        if persist:
            self.save(state)
        return state

    def prepare(self, selection, plugins, before, after, strict_native=False, view=None):
        state = self.load()
        purge = next((p for p in plugins if p['id'] == 'dsh-purge'), None)
        unknown = [p['id'] for p in plugins if p['id'] != 'dsh-purge']
        if strict_native and unknown:
            raise PatchError('这些插件尚未接入补丁还原，无法保证原生模式：' + '、'.join(unknown))
        if strict_native:
            for filename in ['cordis.yml', 'cordis.patch.yml']:
                path = self.manager.profile / filename
                if not (view.is_file(path) if view else path.is_file()):
                    continue
                source = '\n'.join(line for line in (view.read_bytes(path).decode('utf-8-sig') if view else path.read_text(encoding='utf-8-sig')).splitlines()
                                   if not line.lstrip().startswith('#'))
                names = re.findall(r'''(?m)^\s*(?:-\s*)?name:\s*["']?([^\s"'#]+)''', source)
                if '!!js' in source or any(not name.startswith('@deepseek-ai/') for name in names):
                    raise PatchError('用户 YAML 配置包含额外插件或动态表达式，无法验证原生模式：' + filename)
        if not state and not purge:
            if strict_native and (view.exists(self.app) if view else self.app.exists()):
                raise PatchError('存在未受管理的 resources/app，无法验证原生状态。')
            return None
        if not state:
            state = self.capture(plugins, view=view, persist=False)
        self.manager.tracking.versions.verify_adapter(state)
        desired = 'plugin' if 'dsh-purge' in selection else 'native'
        if purge and purge['version'] != state['pluginVersion']:
            raise PatchError('dsh-purge 已升级，旧补丁快照已停用。需在新版本上重新建立快照。')
        official = self.official_archive(view)
        if official is None or (view.hash(official) if view else file_hash(official)) != state['archiveHash']:
            raise PatchError('Harness 官方归档已更新或被修改，已停止使用旧补丁快照。')
        operations = []
        app_at = self.app if (view.exists(self.app) if view else self.app.exists()) else self.stash / 'app'
        if tree_hash(app_at, view) != state['treeHash']:
            raise PatchError('补丁目录有新改动或其他插件写入，已停止切换以保护这些修改。')
        for item in state['files']:
            actual = (view.hash(Path(item['path'])) if view else file_hash(Path(item['path'])))
            expected = item['native'] if state['mode'] == 'native' else item.get('initial', item['plugin']) if state['mode'] == 'captured' else item['plugin']
            if actual != expected:
                raise PatchError('插件注入文件已被修改，需重新建立快照：' + item['path'])
        for item in state['directories']:
            at = Path(item['parked'] if state['mode'] == 'native' else item['active'])
            if tree_hash(at, view) != item['hash']:
                raise PatchError('插件预设已被修改，未覆盖现有预设。')
        if desired == 'native':
            if (view.exists(self.app) if view else self.app.exists()):
                operations.append(self.move_op(self.app, self.stash / 'app', view))
            if official != self.archive:
                operations.append(self.move_op(official, self.archive, view))
        else:
            if official != self.stash / 'official.asar':
                operations.append(self.move_op(official, self.stash / 'official.asar', view))
            if not (view.exists(self.app) if view else self.app.exists()):
                operations.append(self.move_op(self.stash / 'app', self.app, view))
        for item in state['files']:
            operations.append(self.file_op(Path(item['path']), self.bytes(item[desired]), view))
        for item in state['directories']:
            source = Path(item['active'] if desired == 'native' else item['parked'])
            dest = Path(item['parked'] if desired == 'native' else item['active'])
            if view.exists(source) if view else source.exists():
                operations.append(self.move_op(source, dest, view))
            elif not (view.exists(dest) if view else dest.exists()):
                raise PatchError('插件预设目录缺失。')
        manifest = self.manager.manifest_path
        if (view.read_bytes(manifest) if view else manifest.read_bytes()) != before:
            raise PatchError('配置在校验期间被更改，请刷新后重试。')
        if before != after:
            operations.append(self.file_op(manifest, after, view))
        target = dict(state, mode=desired)
        return {'previous': state, 'state': target, 'operations': operations, 'mode': desired}
