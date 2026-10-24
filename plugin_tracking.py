"""Mandatory, supervised installation and first-run file layers.

Snapshots establish *when* files changed, not which process wrote them. Scope is
explicit; credentials, conversations, the registry and arbitrary disk paths are
outside it. Conflicting overlapping layers fail instead of doing a blind merge.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

from adapters import is_adapter
from domain import (ContentIdentity, DomainError, ManagedPath, PLUGIN_NAME,
                    SnapshotEntry, validate_snapshot, validate_changes)
from recording_state import (RecordingEvent, RecordingPhase, RecordingSession, RunState,
                             UPDATE_GUARDS, validate_registry)
from patch_modes import PatchError, PatchModes, file_hash, put, sha
from runtime_context import RuntimeContext
from storage import ContentStore, write_json
from version_store import VersionStore


class TrackingError(PatchError):
    pass


class PluginTracking:
    def __init__(self, manager):
        self.manager = manager
        self.core = PatchModes(RuntimeContext.from_manager(manager), require_stopped=manager.require_stopped)
        self.root = self.core.root / 'tracking'
        self.index = self.root / 'registry.json'
        self.busy = False
        self.progress = {'count': 0, 'bytes': 0, 'path': ''}
        self.job_lock = threading.RLock()
        self._owned_session = None
        self._process = None
        self._quiet = None
        self.store = ContentStore(self.root)
        self.versions = VersionStore(manager.profile, Path(manager.settings['executable']), self.store, self.core.archive_source,
                                    manager.verification_cache)
        self.core.bind_versions(self.versions)
        resources = self.core.resources
        home = manager.home
        self.scopes = {
            'app': resources / 'app', 'archive': resources / 'app.asar',
            'archive_backup': resources / 'app.asar.bak',
            'unpacked': resources / 'app.asar.unpacked', 'cli': resources / 'runtime' / 'cli',
            'agents': home / 'AGENTS.md', 'claude': home / 'CLAUDE.md',
            'prompt': home / 'prompt-inject.md', 'rules': home / 'rules',
            'skills': home / 'skills', 'presets': home / '.agent-presets',
            'user_yaml': manager.profile / 'cordis.patch.yml', 'root_yaml': manager.profile / 'cordis.yml',
        }

    def load(self):
        if not self.index.is_file():
            return {'format': 3, 'requiredFeatures': [], 'records': {}, 'order': [], 'session': None}
        try:
            value = json.loads(self.index.read_text(encoding='utf-8'))
            return validate_registry(value)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TrackingError('记录格式不是当前版本或已损坏，请保留 tracking 目录并从有效备份恢复。') from exc

    def save(self, value):
        try:
            validate_registry(value)
        except DomainError as exc:
            raise TrackingError('拒绝保存无效记录：' + str(exc)) from exc
        write_json(self.index, value)

    def read_object(self, relative):
        value = self.store.read_object(relative)
        try:
            if relative.startswith('snapshots/'):
                validate_snapshot(value)
            elif relative.startswith('deltas/'):
                validate_changes(value)
        except DomainError as exc:
            raise TrackingError('记录内容结构无效：' + relative + '；' + str(exc)) from exc
        return value

    def object(self, kind, value):
        if kind not in ('snapshots', 'deltas'):
            raise TrackingError('不支持的文件记录类型。')
        try:
            (validate_snapshot if kind == 'snapshots' else validate_changes)(value)
        except DomainError as exc:
            raise TrackingError('不能保存无效文件记录：' + str(exc)) from exc
        return self.store.object(kind, value)

    def blob_bytes(self, key):
        return self.store.blob_bytes(key)

    def store_file(self, path, stamp, cached=None):
        return self.store.store_file(path, stamp, cached)

    def target(self, key):
        try:
            managed = ManagedPath.decode(key)
            relative = managed.relative
            base = self.scopes[managed.scope]
            parts = PurePosixPath(relative)
            if parts.is_absolute() or '..' in parts.parts or '\\' in relative or ':' in relative:
                raise ValueError()
            path = base if relative == '.' else base.joinpath(*parts.parts)
            # Physical files only: never follow a plugin-created junction to disk outside scope.
            for cursor in (path, *path.parents):
                if cursor.is_symlink() or cursor.is_junction():
                    raise TrackingError('不能自动覆盖链接或其目标：' + str(cursor))
            if not path.resolve().is_relative_to(base.parent.resolve()):
                raise ValueError()
            return path
        except (ValueError, KeyError) as exc:
            raise TrackingError('变更记录包含托管范围以外的路径。') from exc

    @staticmethod
    def equal(left, right):
        if left is None or right is None:
            return left is right
        try:
            return ContentIdentity.decode(left) == ContentIdentity.decode(right)
        except DomainError as exc:
            raise TrackingError('无法比较无效文件身份：' + str(exc)) from exc

    @staticmethod
    def _transition(session, event, **data):
        try:
            updated = RecordingSession.decode(session).transition(event, **data).to_dict()
        except DomainError as exc:
            raise TrackingError('安装状态转换被拒绝：' + str(exc)) from exc
        session.clear()
        session.update(updated)

    @staticmethod
    def _check_scope_parents(path):
        # The scope itself may be recorded as a link, but its parents must not
        # redirect enumeration into an unrelated directory.
        for parent in path.parents:
            if parent.is_symlink() or parent.is_junction():
                raise TrackingError('托管目录的上级路径不能是链接：' + str(parent))

    def snapshot(self, cached=None):
        cached = cached or {}
        result = {}
        self.progress = {'count': 0, 'bytes': 0, 'path': ''}

        def capture(path, key, info=None):
            info = info or path.lstat()
            if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
                result[key] = {'type': 'link', 'target': str(path.resolve())}
            elif stat.S_ISDIR(info.st_mode):
                result[key] = {'type': 'dir'}
                for entry in sorted(os.scandir(path), key=lambda item: item.name.lower()):
                    if entry.name.startswith('.harness-launcher-'):
                        continue
                    child = key[:-1] + entry.name if key.endswith('|.') else key + '/' + entry.name
                    capture(Path(entry.path), child, entry.stat(follow_symlinks=False))
            elif stat.S_ISREG(info.st_mode):
                stamp = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                key_hash = self.store_file(path, stamp, cached.get(key))
                result[key] = SnapshotEntry.decode({'type': 'file', 'hash': key_hash, 'size': info.st_size,
                                                    'mtime': info.st_mtime_ns, 'stamp': stamp}).to_dict()
                self.progress = {'count': self.progress['count'] + 1,
                                 'bytes': self.progress['bytes'] + info.st_size, 'path': key}
            else:
                raise TrackingError('遇到不支持的文件类型：' + str(path))

        for scope, path in self.scopes.items():
            self._check_scope_parents(path)
            if path.exists() or path.is_symlink():
                capture(path, scope + '|.')
        return result

    def actual(self, key):
        path = self.target(key)
        if not path.exists():
            return None
        if path.is_dir():
            return {'type': 'dir'}
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        return {'type': 'file', 'hash': h.hexdigest()}

    def metadata(self):
        """Inventory without opening every DLL/JS; used to detect unrecorded writes."""
        result = {}
        def visit(path, key, info=None):
            info = info or path.lstat()
            if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
                result[key] = ['link', str(path.resolve())]
            elif stat.S_ISDIR(info.st_mode):
                result[key] = ['dir']
                for entry in os.scandir(path):
                    if not entry.name.startswith('.harness-launcher-'):
                        child = key[:-1] + entry.name if key.endswith('|.') else key + '/' + entry.name
                        visit(Path(entry.path), child, entry.stat(follow_symlinks=False))
            else:
                result[key] = ['file', info.st_size, info.st_mtime_ns]
        for scope, path in self.scopes.items():
            self._check_scope_parents(path)
            if path.exists() or path.is_symlink():
                visit(path, scope + '|.')
        return result

    def differences(self, before, after):
        result = []
        for key in sorted(set(before) | set(after)):
            a, b = before.get(key), after.get(key)
            if not self.equal(a, b):
                result.append({'key': key, 'before': a, 'after': b})
        return result

    def operations(self, changes, enable=True):
        result = []
        for item in changes:
            a, b = (item['before'], item['after']) if enable else (item['after'], item['before'])
            if (a and a['type'] == 'link') or (b and b['type'] == 'link'):
                raise TrackingError('记录包含新增/修改的链接，无法自动还原：' + item['key'])
            if a and b and a['type'] != b['type']:
                raise TrackingError('文件与目录类型发生互换，无法安全自动还原：' + item['key'])
            result.append({'key': item['key'], 'before': a, 'after': b})

        def order(op):
            depth = len(PurePosixPath(op['key'].split('|', 1)[1]).parts)
            if op['after'] is None:
                return (0 if op['before']['type'] == 'file' else 1, -depth, op['key'])
            return (2 if op['after']['type'] == 'dir' else 3, depth, op['key'])
        return sorted(result, key=order)

    def replay(self, operations, undo=False):
        for op in reversed(operations) if undo else operations:
            self.manager.require_stopped()
            a, b = (op['after'], op['before']) if undo else (op['before'], op['after'])
            path = self.target(op['key'])
            current = self.actual(op['key'])
            if self.equal(current, b):
                # Put back original mtimes too: the existing purge layer fingerprints them.
                if b and b['type'] == 'file':
                    os.utime(path, ns=(b['mtime'], b['mtime']))
                continue
            if not self.equal(current, a):
                raise TrackingError('文件有其他修改或插件之间存在冲突，未覆盖：' + str(path))
            if b is None:
                if a['type'] == 'dir':
                    path.rmdir()  # Never recursively delete unknown files.
                else:
                    path.unlink()
            elif b['type'] == 'dir':
                path.mkdir(parents=False, exist_ok=True)
            else:
                content = self.blob_bytes(b['hash'])
                put(path, content)
                os.utime(path, ns=(b['mtime'], b['mtime']))

    def recorded_state(self, value, inventory=None):
        """Return trusted current contents, never a snapshot of today's files."""
        if value.get('currentCombination'):
            return self.read_object(value['currentCombination'])['state']
        if not value.get('inventory') or not value['order']:
            return None
        if inventory is None:
            inventory = self.read_object(value['inventory'])
        latest = value['records'][value['order'][-1]]
        if latest.get('after'):
            snapshot = self.read_object(latest['after'])
            expected = {key: ['file', item['size'], item['mtime']] if item['type'] == 'file'
                        else ['link', item['target']] if item['type'] == 'link' else ['dir']
                        for key, item in snapshot.items()}
            if expected == inventory:
                return snapshot
        return None

    def guard(self, check_files=False):
        from switch_plan import SwitchPlan
        SwitchPlan(self).recover()
        self.verify_idle(check_files=check_files)

    def check_host_identity(self, selection=(), value=None):
        """Reject a changed host before scanning layers or creating backups.

        This is an early rejection only: matching anchors never approve a
        switch, replace a baseline, or bypass the independent commit checks.
        Hash caches still require fresh NTFS identities on every lookup.
        A new version string is not evidence of an authentic official update.
        """
        value = self.load() if value is None else value
        hosts = {record['hostHash'] for name, record in value['records'].items()
                 if (record['enabled'] or name in selection) and record.get('hostHash')}
        combination = (self.read_object(value['currentCombination'])
                       if value.get('currentCombination') else None)
        if combination:
            hosts.add(combination['hostHash'])
        core_state = self.core.load()
        archive_hash = core_state['archiveHash'] if core_state else None
        archive = self.core.official_archive() if core_state else self.core.archive
        if not core_state and combination:
            # A generic recorded plugin may legitimately patch app.asar. Use
            # the sealed active combination, not its unpatched native base.
            entry = combination['state'].get('archive|.')
            archive_hash = entry['hash'] if entry and entry['type'] == 'file' else None
        def reject(component):
            raise TrackingError('Harness 宿主文件已变化，可能发生了官方更新或手动修改；旧版本补丁记录不能直接用于当前安装。',
                code='host_identity_changed', details={'components': [component]},
                recovery='若刚更新 Harness，请保留旧记录，在新版本的干净安装上重新记录兼容插件；不要用旧快照覆盖新版文件。')
        if hosts and hosts != {self.versions.hash(Path(self.manager.settings['executable']))}:
            reject('executable')  # No reason to hash a large new ASAR as well.
        if archive_hash:
            if archive is None or self.versions.hash(archive) != archive_hash:
                reject('officialArchive')

    def verify_idle(self, check_files=False):
        """Read-only inspection; never recover a transaction as a side effect."""
        self.manager.maintenance.require_idle()
        if (self.root / 'switch.json').exists():
            raise TrackingError('组合切换尚未完成，请通过正常启动器操作恢复；只读检查不会修改文件。')
        value = self.load()
        if self.busy or value.get('session'):
            raise TrackingError('安装记录尚未完成，请等待自动保存；如有异常，请先处理，原始快照会保留。')
        if check_files:
            self.check_host_identity(value=value)
        if check_files and value.get('inventory'):
            expected = self.read_object(value['inventory'])
            actual = self.metadata()
            changed = [key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key)]
            if changed:
                # Harness can rewrite generated cordis.yml without changing its
                # bytes. Metadata is a fast filter, not evidence of a new patch.
                # Only the current combination (or the matching last recording)
                # may supply the expected content; never adopt today's files.
                recorded = self.recorded_state(value, expected) or {}
                from file_view import FileView
                conflicts = []
                for key in changed:
                    before, after, content = expected.get(key), actual.get(key), recorded.get(key)
                    if (before and after and before[0] == after[0] == 'file'
                            and before[1] == after[1] and content and content['type'] == 'file'):
                        path = self.target(key)
                        if self.equal(FileView([path]).entry(path), content):
                            continue
                    conflicts.append(key)
                changed = conflicts
            if changed:
                raise TrackingError('记录窗口外发生了文件变化，无法自动归属，已停止覆盖：' + '、'.join(sorted(changed)[:4]))

    def check_versions(self, plugins, selection):
        records = self.load()['records']
        available = {p['id']: p['version'] for p in plugins}
        for plugin, record in records.items():
            if record['enabled'] or plugin in selection:
                self.versions.verify_record(plugin, record)
            if (record['enabled'] or plugin in selection) and available.get(plugin) != record['version']:
                raise TrackingError('插件版本已变化，旧补丁不能用于更新后的插件：' + plugin,
                    code='plugin_identity_changed', details={'plugin': plugin},
                    recovery='保留现有记录。若已在记录窗口外更新，请先恢复记录对应的插件及依赖，再从“升级 / 重新记录”开始；不能对更新后的文件补记旧基线。')
            if (record['enabled'] or plugin in selection) and record.get('package'):
                self.versions.verify_package(plugin, record['package'])
        host_hash = None
        for name, record in records.items():
            if (record['enabled'] or name in selection) and record.get('hostHash'):
                if host_hash is None:
                    host_hash = self.versions.hash(Path(self.manager.settings['executable']))
                if host_hash != record['hostHash']:
                    raise TrackingError('Harness 程序已更新，不能使用旧版本的文件记录：' + name)

    def check_launch_packages(self, selection, manifest):
        """Recheck executable plugin code after file switching, just before spawn."""
        raw, obj, bundles = self.manager.manifest()
        if raw != manifest:
            raise TrackingError('启动前加载组合发生变化，未打开 Harness。', code='revision_conflict', retryable=True)
        plugins = self.manager.discover(obj, bundles)
        self.check_versions(plugins, selection)
        if self.core.adapter.plugin in selection:
            state = self.core.load()
            if state is None:
                raise TrackingError('启动前缺少内置插件的版本记录。')
            self.versions.verify_adapter(state, self.core.adapter.plugin)
        if self.manager.manifest_path.read_bytes() != manifest:
            raise TrackingError('启动前加载配置再次发生变化，未打开 Harness。', code='revision_conflict', retryable=True)

    def check_session_host(self, session):
        """An install recording must never absorb an official host replacement.

        ASARs are host anchors during recording. Plugins may change extracted
        app files, but replacing the official archive requires a new host base.
        Derive the expectation from existing immutable snapshots, not live bytes.
        """
        if self.versions.hash(Path(self.manager.settings['executable'])) != session['hostHash']:
            raise TrackingError('Harness 程序已更新，已保留原始快照并停止安装记录。',
                code='host_identity_changed', details={'components': ['executable']})
        if session.get('hostArchives') is None:
            raise TrackingError('旧安装会话缺少更新边界记录，不能自动采纳当前文件。请保留会话，在建立它的启动器版本中完成或恢复安装后再升级。')
        if self.session_archives() != session['hostArchives']:
            raise TrackingError('安装记录期间 Harness 官方归档发生变化，不能把宿主更新保存为插件补丁。',
                code='host_identity_changed', details={'components': ['officialArchive']},
                recovery='保留安装会话和快照；先恢复本次记录对应的 Harness 版本。官方更新和插件更新必须分开进行。')

    def session_archives(self):
        paths = {'archive': self.core.archive, 'archive_backup': self.core.resources / 'app.asar.bak',
                 'parked_archive': self.core.stash / 'official.asar'}
        result = {}
        for name, path in paths.items():
            if path.is_symlink() or path.is_junction() or (path.exists() and not path.is_file()):
                raise TrackingError('宿主归档位置含链接或目录，无法建立更新边界记录。')
            result[name] = self.versions.hash(path)
        return result

    def summary(self):
        value = self.load()
        session = value['session']
        review = []
        if session and session.get('delta'):
            review = [{'path': x['key'], 'change': '新增' if x['before'] is None else '删除' if x['after'] is None else '修改'}
                      for x in self.read_object(session['delta'])['changes'][:200]]
        return {'busy': self.busy, 'progress': dict(self.progress), 'session': session,
                'review': review, 'pending': (self.root / 'switch.json').exists(),
                'records': {name: {**{k: record[k] for k in ('version', 'enabled', 'count', 'baseSelection')},
                                   'historyCount': len(record.get('history', [])),
                                   'revision': record.get('revision'), 'packageRecorded': bool(record.get('package')),
                                   'versions': [{'version': item['version'], 'revision': item.get('revision'),
                                                 'packageRecorded': bool(item.get('package'))}
                                                for item in [*record.get('history', []), record]]}
                            for name, record in value['records'].items()},
                'scopes': [str(path) for path in self.scopes.values()],
                'completed': value.get('completed'),
                'currentCombination': value.get('currentCombination'),
                'combinationCount': len(value.get('combinations', [])),
                'limits': '只记录上述目录内的文件差异；不监控注册表、系统服务、进程归属或其他磁盘路径。'}

    def export_changes(self):
        value = self.load()
        session = value.get('session')
        if not session or not session.get('delta'):
            raise TrackingError('当前没有待复核的差异。')
        return {'format': 'harness-plugin-file-changes-v1', 'plugin': session['plugin'],
                'changes': [{'path': x['key'], 'before': x['before'], 'after': x['after']}
                            for x in self.read_object(session['delta'])['changes']]}

    def _job(self, task):
        if self.busy:
            raise TrackingError('快照任务尚未完成。')
        self.busy = True
        def run():
            try:
                task()
            except Exception as exc:
                with self.manager.lock:
                    value = self.load()
                    if value.get('session'):
                        self._transition(value['session'], RecordingEvent.FAIL, error=str(exc) or type(exc).__name__)
                        self.save(value)
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def begin(self, name, source=''):
        with self.manager.lock:
            self.manager.require_stopped()
            self.manager.require_executable()
            self.guard(check_files=True)
            if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name) or is_adapter(name):
                raise TrackingError('请输入有效的新插件包名。内置适配插件使用专用还原机制。')
            if (not isinstance(source, str) or len(source) > 2048 or source.startswith('-')
                    or any(char in source for char in ('\n', '\r', '\x00'))):
                raise TrackingError('安装来源格式无效。')
            if urlparse(source).username or urlparse(source).password:
                raise TrackingError('安装地址不能包含账号密码，请使用已配置的包管理器认证。')
            value = self.load()
            core_state = self.core.load()
            if (self.root / 'switch.json').exists():
                raise TrackingError('上次组合切换尚未完成，请先应用当前组合以完成恢复，再开始记录。')
            raw, obj, bundles = self.manager.manifest()
            existing = self.manager.discover(obj, bundles)
            unknown = [p['id'] for p in existing if p['id'] not in value['records'] and not is_adapter(p['id'])]
            if unknown:
                raise TrackingError('已存在没有安装前记录的插件，不能把未知改动当作原始文件：' + '、'.join(unknown))
            self.check_versions(existing, [p['id'] for p in existing if p['enabled']])
            if core_state and self.core.adapter.plugin in bundles:
                self.versions.verify_adapter(core_state, self.core.adapter.plugin)
            if name in value['records']:
                # Remove the old layer before taking an upgrade baseline.
                self.manager.apply([x for x in bundles if x not in ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', name)], sha(raw))
                value = self.load()
                raw, obj, bundles = self.manager.manifest()
            plugins = self.manager.discover(obj, bundles)
            if name not in value['records'] and any(p['id'] == name for p in plugins):
                raise TrackingError('此插件已安装且没有安装前记录，无法补回已经发生的改动。请在安装新插件之前开始记录。')
            value['session'] = RecordingSession(
                id=uuid.uuid4().hex, plugin=name, source=source, phase=RecordingPhase.PREPARING,
                started=time.time(), host_hash=file_hash(Path(self.manager.settings['executable'])),
                base_selection=tuple(p for p in bundles if p not in ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app')),
                versions={p['id']: p['version'] for p in plugins},
                base_versions={p: value['records'][p]['revision'] for p in value['order'] if p in bundles and p != name},
                host_archives=self.session_archives(),
                package_pins={p['id']: self.versions.package_snapshot(p['id']) for p in plugins if p['id'] != name},
                initial_inventory=self.object('snapshots', self.metadata())).validate().to_dict()
            # Package capture can take time. Do not pin an updater's result as
            # the old host, or publish a session against a changed manifest.
            self.manager.require_stopped()
            self.check_host_identity(value=value)
            self.check_session_host(value['session'])
            if self.manager.manifest_path.read_bytes() != raw:
                raise TrackingError('准备安装期间加载配置发生变化，尚未开始安装，请刷新后重试。',
                    code='revision_conflict', retryable=True)
            value['requiredFeatures'] = [UPDATE_GUARDS]
            self._owned_session = value['session']['id']
            self._quiet = None
            value['completed'] = None
            self.manager.persist()
            self.save(value)
            self._job(self._prepare_install)
            return {'message': '正在备份原始文件，完成安装后会自动打开 Harness；正常使用并退出后自动保存记录。'}

    def _prepare_install(self):
        self.manager.require_stopped()
        value = self.load()
        session = value['session']
        self.check_session_host(session)
        self._verify_base_packages(session)
        if not session.get('baseline'):
            if self.metadata() != self.read_object(session['initialInventory']):
                raise TrackingError('备份完成前文件已经改变，不能把现状当作原始文件。请恢复原状后重试。')
            baseline = self.snapshot()
            recorded = self.recorded_state(value)
            if recorded is not None and self.differences(recorded, baseline):
                raise TrackingError('记录窗口外的文件内容发生变化，不能作为新安装的原始状态。请恢复原状后重试。')
            self.manager.require_stopped()
            if self.metadata() != self.read_object(session['initialInventory']):
                raise TrackingError('备份期间文件发生改动，尚未开始安装，请恢复原状后重试。')
            manifest = self.manager.manifest_path.read_bytes()
            self._transition(session, RecordingEvent.BASELINE_READY, baseline=self.object('snapshots', baseline),
                             manifest_before=self.versions.object('baselines',
                                 {'manifest': manifest.decode('utf-8-sig'), 'bom': manifest.startswith(b'\xef\xbb\xbf')}))
            self.save(value)
        self.check_session_host(session)
        if session['source'] and not session.get('installed'):
            self._transition(session, RecordingEvent.INSTALL)
            self.save(value)
            self.install(session['plugin'], session['source'], session['id'])
            self._transition(session, RecordingEvent.INSTALLED)
        self.save(value)
        self._launch()

    def install(self, name, source, session_id):
        resources = self.core.resources
        app = resources / 'app' if (resources / 'app').is_dir() else resources / 'app.asar'
        entry = app / 'dsh' / 'node_modules' / '@deepseek-ai' / 'dsh-desktop-host' / 'lib' / 'cli.js'
        env = os.environ.copy()
        env.update(ELECTRON_RUN_AS_NODE='1', DSH_HOME=str(self.manager.home), DSH_SURFACE='desktop',
                   DSH_DESKTOP_INSTALL=str(resources.parent))
        log = self.root / 'logs' / (session_id + '.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('wb') as output:
            process = subprocess.Popen([self.manager.settings['executable'], '--expose-internals', str(entry),
                                        'plugin', '--profile', 'desktop', 'add', source],
                                       cwd=str(self.manager.profile), env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            code = process.wait()
        if code:
            raise TrackingError(f'官方安装器退出码 {code}，安装前快照已保留。可修复后继续记录，日志：{log}')
        _, obj, bundles = self.manager.manifest()
        if not any(p['id'] == name and p['available'] for p in self.manager.discover(obj, bundles)):
            raise TrackingError('安装结果中找不到指定插件包名，请检查来源是否与包名一致。')

    def _launch(self):
        with self.manager.lock:
            self.manager.require_stopped()
            self.manager.require_executable()
            value = self.load()
            session = value.get('session')
            if not session or session['phase'] != 'recording' or not session.get('baseline'):
                raise TrackingError('原始文件尚未备份，不能打开安装环境。')
            self.check_session_host(session)
            raw, obj, bundles = self.manager.manifest()
            name = session['plugin']
            plugins = self.manager.discover(obj, bundles)
            versions = {p['id']: p['version'] for p in plugins if p['available']}
            changed = [p for p in set(versions) | set(session['versions'])
                       if p != name and versions.get(p) != session['versions'].get(p)]
            others = [p for p in bundles if p not in ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', name)]
            if changed or others != session['baseSelection']:
                raise TrackingError('安装期间其他插件或加载组合也发生变化，已停止打开 Harness，请先恢复原有组合。')
            self._verify_base_packages(session)
            available = name in versions
            if session['source'] and not available:
                raise TrackingError('尚未检测到插件，已保留安装记录，请先修复安装。')
            if available and name not in bundles:
                obj['dsh']['profile']['bundles'].append(name)
                self.manager.backup(raw, bundles, '首次运行时启用插件并自动记录')
                put(self.manager.manifest_path, (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode())
            # Persist the intent before spawning; a crash can never silently skip first-run recording.
            self._transition(session, RecordingEvent.LAUNCH, launch_at=time.time())
            self.save(value)
            self._owned_session = session['id']
            self._quiet = None
            env = os.environ.copy()
            env['DSH_HOME'] = str(self.manager.home)
            exe = Path(self.manager.settings['executable'])
            self._process = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def active(self):
        session = self.load().get('session')
        return self.busy or bool(session and not session.get('error'))

    def tick(self, now=None):
        """Background supervisor, independent of the browser and its polling timer."""
        with self.manager.lock:
            try:
                self._tick(now)
            except Exception as exc:
                value = self.load()
                if value.get('session'):
                    self._transition(value['session'], RecordingEvent.FAIL, error=str(exc) or type(exc).__name__)
                    self.save(value)

    def _tick(self, now=None):
        with self.manager.lock:
            if self.busy:
                return
            value = self.load()
            session = value.get('session')
            if not session or session.get('error'):
                return
            if session['id'] != self._owned_session:
                self._transition(session, RecordingEvent.FAIL,
                                 error='上次安装记录意外中断，原始快照已保留。检查后点击「继续自动记录」，不会直接采用中断期间的改动。')
                self.save(value)
                return
            if session['phase'] != 'recording':
                return
            now = time.time() if now is None else now
            if self.manager.is_running():
                self._quiet = None
                if session['runState'] != RunState.RUNNING:
                    if self._process is None or self._process.poll() is not None:
                        self._transition(session, RecordingEvent.FAIL,
                                         error='检测到了其他或转交后的 Harness 进程，但无法确认本次启动。原始记录已保留，请检查后继续。')
                        self.save(value)
                        return
                    self._transition(session, RecordingEvent.OBSERVE_RUNNING)
                    self.save(value)
                return
            if session['runState'] in (RunState.IDLE, RunState.LAUNCHING):
                if session.get('launchAt') is not None and now - session['launchAt'] >= 30:
                    self._transition(session, RecordingEvent.FAIL,
                                     error='未检测到 Harness 正常启动，记录尚未保存。请检查应用后继续自动记录。')
                    self.save(value)
                return
            # The Electron bootstrap may exit before its desktop/tray child. Require
            # the full runtime to stop AND its managed files to settle before scanning.
            inventory = self.metadata()
            if self._quiet is None or self._quiet[1] != inventory:
                self._quiet = (now, inventory)
                return
            if now - self._quiet[0] < 2:
                return
            self._transition(session, RecordingEvent.OBSERVE_CLOSED)
            self.save(value)
            try:
                self.finish()
            except Exception as exc:
                value = self.load()
                self._transition(value['session'], RecordingEvent.FAIL, error=str(exc) or type(exc).__name__)
                self.save(value)

    def retry(self):
        with self.manager.lock:
            self.manager.require_stopped()
            value = self.load()
            session = value.get('session')
            if self.busy or not session or not session.get('error'):
                raise TrackingError('当前没有需要继续的异常安装记录。')
            self._owned_session = session['id']
            self._transition(session, RecordingEvent.RETRY)
            self.save(value)
            if session['runState'] == RunState.CLOSED:
                try:
                    return self.finish()
                except Exception as exc:
                    self._transition(session, RecordingEvent.FAIL, error=str(exc) or type(exc).__name__)
                    self.save(value)
                    raise
            self._job(self._prepare_install)
            return {'message': '已继续自动记录，原始快照保持不变。'}

    def _validate_session(self, session):
        self.manager.require_stopped()
        _, obj, bundles = self.manager.manifest()
        plugins = self.manager.discover(obj, bundles)
        versions = {p['id']: p['version'] for p in plugins if p['available']}
        name = session['plugin']
        if name not in versions:
            raise TrackingError('未找到本次安装的插件，请完成安装后继续自动记录。')
        changed = [p for p in set(versions) | set(session['versions'])
                   if p != name and session['versions'].get(p) != versions.get(p)]
        if changed:
            raise TrackingError('期间还有其他插件安装、移除或升级，不能确定改动属于谁：' + '、'.join(sorted(changed)))
        others = [p for p in bundles if p not in ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', name)]
        if others != session['baseSelection']:
            raise TrackingError('其他插件的加载组合发生变化，请恢复安装前的组合后重试。')
        if name not in bundles:
            raise TrackingError('本次插件未启用，无法确认首次运行已被记录。请启用它后继续自动记录。')
        self.check_session_host(session)
        self._verify_base_packages(session)
        return versions

    def _verify_base_packages(self, session):
        pins = session.get('packagePins')
        if pins is None:
            raise TrackingError('旧安装会话缺少其他插件的更新前记录，已停止自动保存并保留恢复材料。')
        for name, reference in pins.items():
            self.versions.verify_package(name, reference)

    def finish(self):
        with self.manager.lock:
            self.manager.require_stopped()
            if self.busy:
                raise TrackingError('快照/安装任务尚未完成。')
            value = self.load()
            session = value.get('session')
            if not session or not session.get('baseline'):
                raise TrackingError('尚无完整的安装前快照。')
            if session['runState'] != RunState.CLOSED:
                raise TrackingError('首次运行尚未完成，正常使用并完全退出 Harness 后会自动保存。')
            versions = self._validate_session(session)
            self._transition(session, RecordingEvent.SCAN)
            self.save(value)

            def task():
                before = self.read_object(session['baseline'])
                self.manager.require_stopped()
                after = self.snapshot()
                changes = self.differences(before, after)
                # Fail before registering a layer if any link/type transition cannot be replayed.
                self.operations(changes)
                saved = self.load()
                self._transition(saved['session'], RecordingEvent.VERIFY,
                                 version=versions[session['plugin']], count=len(changes),
                                 after=self.object('snapshots', after), delta=self.object('deltas', {'changes': changes}))
                self.save(saved)
                self._save_record()
            self._job(task)
            return {'message': '正在自动整理、校验并保存安装记录。'}

    def _save_record(self):
        with self.manager.lock:
            value = self.load()
            session = value.get('session')
            if not session or session['phase'] != 'verifying' or session['runState'] != RunState.CLOSED:
                raise TrackingError('尚未完成自动整理校验，不能跳过安装记录。')
            versions = self._validate_session(session)
            expected_versions = dict(session['versions'])
            expected_versions[session['plugin']] = session['version']
            if versions != expected_versions:
                raise TrackingError('校验期间插件版本或安装列表再次改变，请继续自动记录以重新检查。')
            expected = self.read_object(session['after'])
            actual = self.snapshot()
            if self.differences(expected, actual):
                raise TrackingError('校验期间文件再次改变，记录未保存；请处理后继续自动记录。')
            changes = self.read_object(session['delta'])['changes']
            self.operations(changes, enable=False)
            for item in changes:
                self.target(item['key'])
                for entry in (item['before'], item['after']):
                    if entry and entry['type'] == 'file':
                        self.blob_bytes(entry['hash'])
            inventory = self.metadata()
            expected_inventory = {key: ['file', item['size'], item['mtime']] if item['type'] == 'file'
                                  else ['link', item['target']] if item['type'] == 'link' else ['dir']
                                  for key, item in actual.items()}
            if inventory != expected_inventory:
                raise TrackingError('文件仍在写入，记录未保存，请退出相关程序后继续。')
            self.manager.require_stopped()
            name = session['plugin']
            package = self.versions.package_snapshot(name, session.get('source', ''))
            # Package capture can be lengthy; recheck the managed file set and
            # host/other plugin identities before publishing a complete revision.
            verified = self.snapshot()
            if self.differences(actual, verified):
                raise TrackingError('保存版本期间文件再次改变，请继续自动记录。')
            if self._validate_session(session) != versions:
                raise TrackingError('保存版本期间插件列表再次改变，请继续自动记录。')
            self.versions.verify_package(name, package)
            inventory = self.metadata()
            verified_inventory = {key: ['file', item['size'], item['mtime']] if item['type'] == 'file'
                                  else ['link', item['target']] if item['type'] == 'link' else ['dir']
                                  for key, item in verified.items()}
            if inventory != verified_inventory:
                raise TrackingError('保存版本期间文件仍在变化，已保留原始快照。')
            previous = value['records'].get(name)
            history = copy.deepcopy(previous.get('history', [])) if previous else []
            if previous:
                history.append({k: copy.deepcopy(v) for k, v in previous.items()
                                if k not in ('history', 'enabled')})
            value['records'][name] = {'version': session['version'], 'delta': session['delta'],
                                      'count': session['count'], 'enabled': True,
                                      'baseSelection': session['baseSelection'], 'history': history,
                                      'hostHash': session['hostHash'], 'source': session.get('source', ''),
                                      'baseline': session['baseline'], 'after': self.object('snapshots', verified),
                                      'baseVersions': session.get('baseVersions', {}),
                                      'manifestBefore': session.get('manifestBefore'), 'package': package}
            record = value['records'][name]
            record['revision'] = self.versions.revision(name, record, package, session.get('source', ''),
                                                       previous.get('revision') if previous else None)
            # The new recording sits on top of every layer that stayed enabled.
            value['order'] = [p for p in value['order'] if p != name] + [name]
            value['session'] = None
            value['requiredFeatures'] = [feature for feature in value.get('requiredFeatures', []) if feature != UPDATE_GUARDS]
            value['inventory'] = self.object('snapshots', inventory)
            value['completed'] = {'plugin': name, 'count': len(changes), 'at': time.time()}
            value['currentCombination'] = None  # Next switch verifies and seals this new revision.
            self.save(value)
            return {'message': f'已保存 {name} 的自动记录，共 {len(changes)} 处变更。'}

    def abandon(self):
        with self.manager.lock:
            self.manager.require_stopped()
            if self.busy:
                raise TrackingError('请等待当前快照/安装任务结束，不能丢弃运行中的记录。')
            value = self.load()
            session = value.get('session')
            if not session:
                raise TrackingError('没有进行中的安装。')
            self.check_session_host(session)
            self._verify_base_packages(session)
            raw, obj, bundles = self.manager.manifest()
            versions = {p['id']: p['version'] for p in self.manager.discover(obj, bundles)}
            if versions != session['versions']:
                raise TrackingError('插件安装已经发生，必须保留记录。请处理异常后继续自动记录。')
            # A reinstall may replace code without changing package.json's
            # version. Keep its recovery session until the package is restored.
            for name, record in value['records'].items():
                if name == session['plugin'] or name in session['baseSelection']:
                    self.versions.verify_package(name, record['package'])
            restore_manifest = None
            if session.get('baseline'):
                changed = self.differences(self.read_object(session['baseline']), self.snapshot())
                baseline = self.versions.store.read_object(session['manifestBefore'])
                original = json.loads(baseline['manifest'])
                expected = copy.deepcopy(original)
                selected = expected['dsh']['profile']['bundles']
                if session['plugin'] in versions and session['plugin'] not in selected:
                    selected.append(session['plugin'])
                if obj != original and obj != expected:
                    raise TrackingError('安装期间配置已经改变，不能丢弃记录。请恢复安装前的配置后重试。')
                if obj != original:
                    restore_manifest = (b'\xef\xbb\xbf' if baseline.get('bom') else b'') + baseline['manifest'].encode('utf-8')
            else:
                changed = self.metadata() != self.read_object(session['initialInventory'])
            if changed:
                raise TrackingError('文件已经改动，不能丢弃还原记录。请处理异常后继续自动记录。')
            if restore_manifest is not None:
                if self.manager.manifest_path.read_bytes() != raw:
                    raise TrackingError('取消期间配置再次改变，已保留安装记录。')
                # Undo only the launcher's own first-run enablement. The old
                # revision was disabled before taking the upgrade baseline.
                put(self.manager.manifest_path, restore_manifest)
            value['session'] = None
            value['requiredFeatures'] = [feature for feature in value.get('requiredFeatures', []) if feature != UPDATE_GUARDS]
            self.save(value)
            return {'message': '已取消尚未发生文件改动的安装。'}
