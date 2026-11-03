"""Offline plugin lifecycle, durable recovery points and explicit previews.

Maintenance is deliberately separate from the fast launch/switch path. Package
operations use the official installer. Only fixed file scopes are checkpointed;
conversations, credentials, unrelated caches and external package links are not
deleted. First-run changes require an observed exit and independent validation.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from adapters import PURGE, is_adapter
from domain import PLUGIN_NAME
from errors import LauncherError
from maintenance_files import CheckpointFiles
from operations import report_progress, progress_workflow
from patch_modes import file_hash, sha
from storage import atomic_write, canonical, write_json
from update_check import UpdateCheck
from installer_policy import build_notice, install_arguments, failure_hint, protect_other_builds


FEATURE = 'maintenance-transaction'
OFFICIAL = ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app')
METADATA = ('package.json', 'pnpm-lock.yaml', 'package-lock.json', 'npm-shrinkwrap.json',
            'yarn.lock', 'pnpm-workspace.yaml', '.npmrc')


class Maintenance:
    def __init__(self, manager):
        self.manager = manager
        self.folder = manager.tracking.root / 'maintenance'
        self.path = self.folder / 'pending.json'
        self.local = threading.local()
        self.process = None

    @property
    def tracker(self):
        return self.manager.tracking

    def load(self):
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.is_junction():
            raise LauncherError('维护日志不能是链接。')
        value = json.loads(self.path.read_bytes())
        if (value.get('format') != 1 or value.get('phase') not in
                ('prepared', 'installing', 'waiting', 'review', 'failed', 'restoring')
                or not isinstance(value.get('id'), str) or len(value['id']) != 32):
            raise LauncherError('维护日志无效，请保留恢复目录。')
        return value

    def save(self, value):
        write_json(self.path, value)

    @contextmanager
    def internal(self):
        previous = getattr(self.local, 'inside', False)
        self.local.inside = True
        try:
            yield
        finally:
            self.local.inside = previous

    def require_idle(self):
        self.manager.official_update.require_idle()
        if not getattr(self.local, 'inside', False) and self.path.exists():
            raise LauncherError('插件维护尚未完成，请在“更新与卸载”中完成验证或恢复检查点。',
                                code='maintenance_pending')

    def roots(self):
        tracker, core = self.tracker, self.tracker.core
        roots = dict(tracker.scopes)
        roots['modules'] = self.manager.profile / 'node_modules'
        for i, path in enumerate(core.extra_roots()):
            if not any(path == root or path.is_relative_to(root) for root in roots.values()):
                roots['extra' + str(i)] = path
        roots['purge_marker'] = PURGE.installed_marker(self.manager.home)
        for i, name in enumerate(METADATA):
            roots['metadata' + str(i)] = self.manager.profile / name
        roots['control_registry'] = tracker.index
        roots['control_adapter'] = core.record
        roots['control_settings'] = self.manager.settings_file
        return roots

    def files(self):
        return CheckpointFiles(self.roots(), self.tracker.store,
                               link_targets=(self.tracker.core.resources / 'runtime',))

    def capture(self):
        report_progress('files', '保存维护检查点（首次检查可能较慢）')
        return self.tracker.store.object('transactions', {'files': self.files().snapshot(progress=lambda count, total:
            report_progress('files', '保存维护检查点', count, total))})

    def snapshot(self, reference):
        return self.tracker.store.read_object(reference)['files']

    def host(self):
        core = self.tracker.core
        archive = core.official_archive()
        return {'exe': self.tracker.versions.hash(Path(self.manager.settings['executable'])),
                'archive': self.tracker.versions.hash(archive) if archive else None}

    def check_host(self, pending):
        if self.host() != pending['host']:
            raise LauncherError('维护期间 Harness 已更新，不能用旧检查点覆盖新宿主。', code='host_identity_changed')

    def summary(self):
        pending = self.load()
        history = []
        for path in sorted((self.folder / 'history').glob('*.json'), reverse=True):
            item = json.loads(path.read_bytes())
            history.append({k: item.get(k) for k in ('id', 'name', 'action', 'at', 'version', 'restored')})
        return {'pending': {k: pending.get(k) for k in ('id', 'name', 'action', 'phase', 'error', 'version', 'observed')}
                if pending else None, 'history': history}

    def preview(self, name, action, target=None):
        with self.manager.lock:
            self.require_idle()
            if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name) or name in OFFICIAL:
                raise LauncherError('请选择额外插件。')
            if action not in ('upgrade', 'uninstall'):
                raise LauncherError('未知维护操作。')
            raw, obj, bundles = self.manager.manifest()
            plugins = self.manager.discover(obj, bundles)
            plugin = next((p for p in plugins if p['id'] == name), None)
            if plugin is None or not plugin['available']:
                raise LauncherError('插件缺失或已卸载，请刷新。')
            records = self.tracker.load()['records']
            suspended = self.tracker.load().get('suspendedPackages', {})
            reasons = []
            if name not in records and not is_adapter(name) and name not in suspended:
                reasons.append('此插件没有安装前记录，无法确认和还原其文件修改。')
            for p in plugins:
                if p['id'] not in records and not is_adapter(p['id']) and p['id'] not in suspended:
                    reasons.append('存在未记录插件：' + p['id'])
                if p['id'] == name:
                    continue
                meta = json.loads((Path(p['path']) / 'package.json').read_bytes())
                deps = {**meta.get('dependencies', {}), **meta.get('optionalDependencies', {}),
                        **meta.get('peerDependencies', {})}
                if name in deps:
                    reasons.append('其他插件依赖此包：' + p['id'])
                if (name in records.get(p['id'], {}).get('baseSelection', [])
                        and records[p['id']].get('count', 0)):
                    reasons.append('其他补丁以此插件为基础，需要先重新记录或卸载：' + p['id'])
            modules = self.manager.profile / 'node_modules'
            external = not Path(plugin['path']).is_relative_to(modules.resolve())
            changes = []
            if name in records:
                changes = [{'path': item['key'], 'action': '还原原始文件' if item['before'] else '移除记录内新增文件'}
                           for item in self.tracker.read_object(records[name]['delta'])['changes']]
            elif is_adapter(name) and self.tracker.core.load():
                changes = [{'path': str(item['path']), 'action': '恢复原生内容；存在外部改动则停止'}
                           for item in self.tracker.core.load()['files']]
            payload = {'name': name, 'action': action, 'target': target, 'revision': sha(raw),
                       'version': plugin['version'], 'registry': self.tracker.load(),
                       'adapter': self.tracker.core.load(), 'settings': self.manager.settings}
            return {'name': name, 'action': action, 'target': target, 'version': plugin['version'],
                    'token': hashlib.sha256(canonical(payload)).hexdigest(), 'blocked': list(dict.fromkeys(reasons)),
                    'files': changes, 'externalSource': external,
                    'preserved': ['账号、会话、附件和工作区', '其他插件仍需的共享依赖', '外部本地插件源目录（只移除或恢复链接）',
                        '归属不明及后来修改的文件（检测到冲突会停止）', '恢复检查点与保存的启动方案'],
                    'message': ('先退出 Harness。确认卸载后会保存恢复检查点、还原此插件已记录的补丁，并移除插件包及其加载配置。'
                                if action == 'uninstall' else
                                '先退出 Harness。维护会暂时关闭插件；升级需完成一次运行并退出，验证成功才登记新版。') +
                        (' 安装来源将保存为：' + str(target) +
                         ' 目标宿主版本未声明或不同，不代表已确认不兼容；将先保留检查点，再安装并验证当前宿主上的新记录。旧宿主补丁不会重放。'
                         + build_notice(name, target) if action == 'upgrade' else '')}

    def _pins(self, name):
        _, obj, bundles = self.manager.manifest()
        names = {p['id'] for p in self.manager.discover(obj, bundles)}
        for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
            names.update(obj.get(section, {}))
        return {other: self.tracker.versions.package_snapshot(other) for other in sorted(names)
                if other != name and other not in OFFICIAL
                and (self.manager.profile / 'node_modules' / other / 'package.json').is_file()}

    def verify_pins(self, pending):
        for name, ref in pending['pins'].items():
            self.tracker.versions.verify_package(name, ref)

    def verify_target(self, name):
        # Maintenance removes code and parked patches even for disabled plugins.
        # Unlike a native launch, it must verify those dormant bytes too.
        registry = self.tracker.load()
        suspended = registry.get('suspendedPackages', {}).get(name)
        if suspended:
            self.tracker.versions.verify_package(name, suspended)
        elif is_adapter(name):
            state = self.tracker.core.load()
            if state:
                self.tracker.versions.verify_adapter(state, name, installed=True)
                self.tracker.core.verify_files(state)
        else:
            record = registry['records'][name]
            self.tracker.versions.verify_package(name, record['package'])

    def package_command(self, action, source, operation_id):
        core = self.tracker.core
        app = core.app if core.app.is_dir() else core.archive
        entry = app / 'dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
        env = os.environ.copy()
        env.update(ELECTRON_RUN_AS_NODE='1', DSH_HOME=str(self.manager.home), DSH_SURFACE='desktop',
                   DSH_DESKTOP_INSTALL=str(core.resources.parent))
        log = self.folder / (operation_id + '.log')
        with protect_other_builds(self.manager, self.load()['name'], env), log.open('wb') as output:
            args = install_arguments(self.load()['name'], source) if action == 'add' else [action, source]
            process = subprocess.Popen([self.manager.settings['executable'], '--expose-internals', str(entry),
                'plugin', '--profile', 'desktop', *args], cwd=self.manager.profile, env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                code = process.wait(timeout=600)
            except subprocess.TimeoutExpired as exc:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
                else:
                    process.kill()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    raise LauncherError('安装进程仍在写入，检查点已保留；不能并发恢复。',
                                        code='installer_timeout', details={'pid': process.pid}) from exc
                raise LauncherError('包管理器超过 10 分钟，已结束本次安装进程并保留恢复点。',
                                    code='installer_failed') from exc
        if code:
            raise LauncherError(f'官方包管理器返回 {code}，检查点已保留。{failure_hint(log)}日志：{log}', code='installer_failed')

    def _clear_adapter(self):
        core = self.tracker.core
        # The old, verified extracted tree is already in the content checkpoint.
        # Delete only its exact recorded entries, never an entire unchecked tree.
        roots = {'retired_app': core.stash / 'app', 'retired_preset': core.home_stash / PURGE.preset}
        files = CheckpointFiles(roots, self.tracker.store)
        snapshot = files.snapshot()
        files.restore({}, snapshot)
        core.record.unlink(missing_ok=True)

    def _detach_external_package_link(self, pending):
        """Allow an explicit source change without pnpm replacing a junction.

        Only the checkpointed leaf link is removed. The external tree remains
        immutable and is verified again before launch and rollback.
        """
        if pending['action'] != 'upgrade' or not pending.get('externalSource'):
            return
        self.tracker.versions.verify_external_source(pending['name'], pending['externalSource']['package'],
                                                    pending['externalSource']['path'])
        key = 'modules|' + pending['name']
        entry = self.snapshot(pending['before']).get(key)
        if not entry or entry.get('type') != 'link' or not entry.get('external'):
            raise LauncherError('外部安装来源不是已记录的目录链接，不能自动替换。')
        # restore rechecks identity and ancestors immediately before unlinking;
        # a changed link or real directory is never followed or removed.
        self.files().restore({}, {key: entry}, lambda: self.check_host(pending))

    def start(self, name, action, token, target=None):
        with self.manager.lock, self.manager.verification_cache.session(), progress_workflow() as phase:
            phase('check', '检查维护条件与目标来源')
            self.manager.require_stopped()
            preview = self.preview(name, action, target)
            if token != preview['token']:
                raise LauncherError('预览已变化，请重新预览。', code='revision_conflict')
            if preview['blocked']:
                raise LauncherError('；'.join(preview['blocked']))
            self.tracker.guard(check_files=True)
            self.verify_target(name)
            update, meta = (UpdateCheck(self.manager).target(name, target) if action == 'upgrade' else (None, None))
            # dshTarget is an author's tested target, not proof of runtime
            # incompatibility. Install against the current native host, then
            # require a fresh, observed run and verified reversible record.
            # In particular, never replay a suspended old-generation patch.
            phase('packages', '保存维护前检查点与共享依赖')
            pins, checkpoint = self._pins(name), self.capture()
            _, obj, bundles = self.manager.manifest()
            pending = {'format': 1, 'id': uuid.uuid4().hex, 'phase': 'prepared', 'name': name, 'action': action,
                       'version': preview['version'], 'target': target, 'at': time.time(), 'host': self.host(),
                       'selection': [x for x in bundles if x not in OFFICIAL], 'pins': pins,
                       'before': checkpoint, 'expected': None, 'error': None}
            original_root = (self.manager.profile / 'node_modules' / name).resolve()
            if not original_root.is_relative_to(self.manager.profile / 'node_modules'):
                pending['externalSource'] = {'path': str(original_root),
                    'package': self.tracker.versions.package_snapshot(name)}
            self.manager.require_stopped()
            self.check_host(pending)
            self.tracker.guard(check_files=True)
            self.verify_target(name)
            if self.preview(name, action, target)['token'] != token:
                raise LauncherError('保存检查点期间配置发生变化，请重新预览。', code='revision_conflict')
            self.save(pending)
            try:
                with self.internal():
                    value = self.tracker.load()
                    value['requiredFeatures'] = [FEATURE]
                    self.tracker.save(value)
                    phase('files', '还原当前补丁并建立安装基线')
                    self.manager.apply([], self.manager.state()['revision'])
                    # All host layers are native before replacing any code.
                    pending['baseline'] = self.tracker.object('snapshots', self.tracker.snapshot())
                    pending['manifestBefore'] = self.tracker.versions.object('baselines',
                        {'manifest': self.manager.manifest_path.read_text(encoding='utf-8-sig'), 'bom': False})
                    if is_adapter(name):
                        self._clear_adapter()
                    value = self.tracker.load()
                    value['requiredFeatures'] = [FEATURE]
                    self.tracker.save(value)
                    pending['phase'] = 'installing'
                    self.save(pending)
                    phase('sync', '正在升级插件' if action == 'upgrade' else '正在卸载插件')
                    self._detach_external_package_link(pending)
                    self.package_command('add' if action == 'upgrade' else 'remove', target or name, pending['id'])
                    phase('launch', '校验安装结果与共享依赖')
                    self.manager.require_stopped()
                    self.check_host(pending)
                    self.verify_pins(pending)
                    raw, manifest, installed = self.manager.manifest()
                    if action == 'uninstall':
                        if name in installed or name in manifest.get('dependencies', {}) or (self.manager.profile / 'node_modules' / name).exists():
                            raise LauncherError('包管理器没有完整移除目标包，保留检查点。')
                        value = self.tracker.load()
                        value['records'].pop(name, None)
                        value.get('suspended', {}).pop(name, None)
                        value.get('suspendedPackages', {}).pop(name, None)
                        value['order'] = [x for x in value['order'] if x != name]
                        value['requiredFeatures'] = []
                        value['currentCombination'] = None
                        value['inventory'] = self.tracker.object('snapshots', self.tracker.metadata())
                        self.tracker.save(value)
                        self.manager.apply([x for x in pending['selection'] if x != name], sha(raw))
                        pending['expected'] = self.capture()
                        self._complete(pending)
                        return {'message': '已卸载并还原记录内补丁，恢复检查点已保留。'}
                    path = self.manager.profile / 'node_modules' / name / 'package.json'
                    actual_meta = json.loads(path.read_bytes())
                    if actual_meta.get('name') != name or actual_meta.get('version') != update['latest']:
                        raise LauncherError('安装结果不是指定目标版本，不能标记升级成功。')
                    pending['newVersion'] = update['latest']
                    pending['package'] = self.tracker.versions.package_snapshot(name, target)
                    manifest['dsh']['profile']['bundles'] = [*OFFICIAL, name]
                    atomic_write(self.manager.manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
                    phase('launch', '保存安装结果，准备首次运行')
                    pending['expected'] = self.capture()
                    pending['phase'] = 'waiting'
                    pending['observed'] = False
                    self.save(pending)
                    self._launch(pending)
                    return {'message': '安装已完成，尚未确认兼容。正常使用并退出 Harness 后，在此完成验证；修改宿主的插件需先应用其新版补丁。失败时保留检查点供恢复。'}
            except Exception as exc:
                pending['phase'], pending['error'] = 'failed', str(exc)
                # Do not snapshot while a timed-out installer is still writing.
                if getattr(exc, 'code', '') != 'installer_timeout':
                    try:
                        pending['expected'] = self.capture()
                    except Exception as capture_error:
                        pending['error'] += '；现场校验未完成：' + str(capture_error)
                self.save(pending)
                raise

    def _launch(self, pending):
        self.manager.require_stopped()
        self.check_host(pending)
        if pending.get('externalSource'):
            self.tracker.versions.verify_external_source(pending['name'], pending['externalSource']['package'],
                                                        pending['externalSource']['path'])
        self.verify_pins(pending)
        self.tracker.versions.verify_package(pending['name'], pending['package'])
        exe = Path(self.manager.settings['executable'])
        env = os.environ.copy()
        env['DSH_HOME'] = str(self.manager.home)
        self.process = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        pending['pid'] = self.process.pid
        self.save(pending)

    def observe(self):
        # The UI polls this inexpensive read; there is no maintenance scan on startup.
        pending = self.load()
        if pending and pending['phase'] == 'waiting' and not pending.get('observed') and self.manager.is_running():
            pending['observed'] = True
            self.save(pending)

    def finish(self):
        with self.manager.lock, self.manager.verification_cache.session(), self.internal(), progress_workflow() as phase:
            phase('check', '检查首次运行是否结束')
            self.manager.require_stopped()
            pending = self.load()
            if not pending or pending['phase'] != 'waiting' or not pending.get('observed'):
                raise LauncherError('尚未观察到新版的完整运行，请正常使用并退出后验证。')
            self.check_host(pending)
            phase('packages', '校验插件版本与共享依赖')
            self.verify_pins(pending)
            name = pending['name']
            self.tracker.versions.verify_package(name, pending['package'])
            try:
                phase('files', '校验当前宿主上的新补丁与可回滚记录')
                raw, obj, bundles = self.manager.manifest()
                if bundles != [*OFFICIAL, name]:
                    raise LauncherError('首次运行时组合被改变，不能把其他改动记到此插件。')
                value = self.tracker.load()
                before = self.tracker.read_object(pending['baseline'])
                if is_adapter(name):
                    from file_view import FileView
                    # The adapter parks its whole extracted tree. Inspect it
                    # once here; only ordinary deltas need new rollback blobs.
                    view = FileView([*self.tracker.scopes.values(), *self.tracker.core.extra_roots()],
                                    verification_cache=self.manager.verification_cache)
                    after = view.snapshot(self.tracker.scopes,
                        progress=lambda done, total: report_progress('files', '校验首次运行文件', done, total))
                    changes = self.tracker.differences(before, after)
                    plugins = self.manager.discover(obj, bundles)
                    changes = self.tracker.capture_adapter(plugins, before, changes, view=view)
                else:
                    after = self.tracker.snapshot()
                    changes = self.tracker.differences(before, after)
                # An archive adapter can also write ordinary managed files
                # (for example profile YAML). Record those with the same delta
                # engine as every other plugin, without duplicating its app tree.
                if changes or not is_adapter(name) or name in value['records']:
                    self.tracker.operations(changes)
                    self.tracker.operations(changes, False)
                    previous = value['records'].get(name)
                    history = copy.deepcopy(previous.get('history', [])) if previous else []
                    if previous:
                        history.append({k: copy.deepcopy(v) for k, v in previous.items() if k not in ('history', 'enabled')})
                    record = {'version': pending['newVersion'], 'delta': self.tracker.object('deltas', {'changes': changes}),
                        'count': len(changes), 'enabled': True, 'baseSelection': [], 'baseVersions': {},
                        'history': history, 'hostHash': pending['host']['exe'], 'source': pending['target'],
                        'baseline': pending['baseline'], 'after': self.tracker.object('snapshots', after),
                        'manifestBefore': pending['manifestBefore'], 'package': pending['package']}
                    record['revision'] = self.tracker.versions.revision(name, record, record['package'],
                        record['source'], previous['revision'] if previous else None)
                    value['records'][name] = record
                    value['order'] = [n for n in value['order'] if n != name] + [name]
                self.check_host(pending)
                self.verify_pins(pending)
                value.get('suspended', {}).pop(name, None)
                value.get('suspendedPackages', {}).pop(name, None)
                value['requiredFeatures'] = []
                value['currentCombination'] = None
                value['inventory'] = self.tracker.object('snapshots', self.tracker.metadata())
                self.tracker.save(value)
                # Restore the previous enabled selection, with the new revision.
                phase('sync', '恢复原启用组合')
                self.manager.apply(pending['selection'], sha(raw))
                phase('launch', '保存最终校验与恢复点')
                pending['expected'] = self.capture()
                self._complete(pending)
                return {'message': '升级验证完成，原启用组合已恢复；可从恢复点回到升级前。'}
            except Exception as exc:
                pending['phase'], pending['error'] = 'failed', str(exc)
                try:
                    pending['expected'] = self.capture()
                except Exception as capture_error:
                    pending['error'] += '；现场校验未完成：' + str(capture_error)
                self.save(pending)
                raise

    def _complete(self, pending, restored=False):
        pending = dict(pending, restored=restored)
        write_json(self.folder / 'history' / (pending['id'] + '.json'), pending)
        self.path.unlink(missing_ok=True)

    def recovery(self, identity=None):
        pending = self.load()
        if identity is not None:
            if pending:
                raise LauncherError('请先处理当前未完成的维护。')
            if not isinstance(identity, str) or not __import__('re').fullmatch('[a-f0-9]{32}', identity):
                raise LauncherError('恢复点编号无效。')
            pending = json.loads((self.folder / 'history' / (identity + '.json')).read_bytes())
        if not pending or pending.get('restored'):
            raise LauncherError('没有可恢复的检查点。')
        if not pending.get('expected'):
            raise LauncherError('维护在文件校验前中断，无法确认现场归属；已保留原始检查点和日志，停止自动覆盖。')
        self.manager.require_stopped()
        self.check_host(pending)
        if pending.get('externalSource'):
            self.tracker.versions.verify_external_source(pending['name'], pending['externalSource']['package'],
                                                        pending['externalSource']['path'])
        files = self.files()
        report_progress('files', '核对恢复检查点与当前文件')
        current = files.snapshot(store=False, progress=lambda done, total:
            report_progress('files', '核对恢复检查点与当前文件', done, total))
        expected, before = self.snapshot(pending['expected']), self.snapshot(pending['before'])
        changed = files.changes(expected, current)
        # During first run new plugin writes are unsealed. Finish validation
        # before rollback instead of absorbing arbitrary external user edits.
        if pending['phase'] == 'restoring':
            changed = [key for key in set(current) | set(expected) | set(before)
                       if files.identity(current.get(key)) not in
                       (files.identity(expected.get(key)), files.identity(before.get(key)))]
        if changed:
            raise LauncherError('恢复点之后存在未验证的新改动，已停止覆盖：' + '、'.join(changed[:5]), code='maintenance_conflict')
        keys = files.changes(current, before)
        token = hashlib.sha256(canonical({'id': pending['id'], 'current': current})).hexdigest()
        return pending, {'token': token, 'id': pending['id'], 'count': len(keys), 'files': keys,
                         'message': '恢复此维护之前的插件代码、配置与已记录补丁；用户会话和工作区保持原样。'}

    def restore(self, token, identity=None):
        with self.manager.lock, self.manager.verification_cache.session(), self.internal():
            pending, preview = self.recovery(identity)
            if token != preview['token']:
                raise LauncherError('恢复预览已变化，请重新预览。')
            pending['phase'] = 'restoring'
            self.save(pending)
            self.check_host(pending)
            before, expected = self.snapshot(pending['before']), self.snapshot(pending['expected'])
            files = self.files()
            archives = [key for key in set(before) | set(expected)
                        if files.path(key, leaf_link=True).name in ('app.asar', 'app.asar.bak', 'official.asar')]
            def guard():
                if self.tracker.versions.hash(Path(self.manager.settings['executable'])) != pending['host']['exe']:
                    raise LauncherError('恢复期间宿主被更新，已停止。', code='host_identity_changed')
                for key in archives:
                    path = files.path(key)
                    actual = self.tracker.versions.hash(path)
                    allowed = {None, (before.get(key) or {}).get('hash'), (expected.get(key) or {}).get('hash')}
                    if actual not in allowed:
                        raise LauncherError('恢复期间官方归档被更新，已停止。', code='host_identity_changed')
            report_progress('sync', '恢复已核验的插件与配置文件')
            files.restore(before, expected, guard)
            report_progress('launch', '核对宿主并保存恢复结果')
            self.check_host(pending)
            self.manager.settings = json.loads(self.manager.settings_file.read_bytes())
            self._complete(pending, restored=True)
            return {'message': '已恢复维护前的插件、补丁与配置。'}
