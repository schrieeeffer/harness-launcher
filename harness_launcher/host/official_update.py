"""Official updates as a durable transaction, separate from plugin replay.

Only complete signed-package equality authorizes a generation. Old generation
records and parked patches are retained; immutable content is shared. A package
with file patches is suspended across host versions, never guessed compatible.
"""
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from harness_launcher.domain.errors import LauncherError
from harness_launcher.verification.file_view import FileView
from harness_launcher.app.operations import report_progress
from harness_launcher.app.runtime_context import RuntimeContext
from harness_launcher.storage.content_store import ContentStore, hash_file, write_json
from harness_launcher.plugins.update_check import newer
from harness_launcher.host import runtime_repair as repair


OFFICIAL = ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app')
HOST_SCOPES = {'app', 'archive', 'archive_backup', 'unpacked', 'cli', 'root_yaml'}


def fail(message, code='official_update_conflict'):
    raise LauncherError(message, code=code, recovery='在“设置与说明 → 官方版本更新”查看具体冲突；所有旧基线和回滚记录均保留。')


def compatible(version, requirement):
    """Conservative subset of npm ranges. Unknown grammar never grants access."""
    if not isinstance(requirement, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', version):
        return False
    tokens = requirement.strip().split()
    if not tokens:
        return False
    comparisons = []
    for token in tokens:
        match = re.fullmatch(r'(>=|<=|>|<|=|\^|~)?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)', token)
        if not match:
            return False
        comparisons.append(match.groups())
    # The comparator SET must explicitly include this prerelease tuple; its
    # upper bound may be stable (e.g. >=0.3.0-rc.1 <0.4.0).
    if '-' in version and not any('-' in wanted and version.split('-')[0] == wanted.split('-')[0]
                                  for _, wanted in comparisons):
        return False
    for op, wanted in comparisons:
        above, below = newer(version, wanted), newer(wanted, version)
        if op in ('^', '~'):
            major, minor, patch = map(int, wanted.split('-')[0].split('.'))
            upper = (f'{major}.{minor + 1}.0' if op == '~' else
                     f'{major + 1}.0.0' if major else f'0.{minor + 1}.0' if minor else f'0.0.{patch + 1}')
            if below or not newer(upper, version):
                return False
        elif not {None: not above and not below, '=': not above and not below,
                  '>': above, '<': below, '>=': not below, '<=': not above}[op]:
            return False
    return True


def same_package_code(before, after):
    def normalized(value):
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items()
                    if not (value.get('providedBy') == 'harness' and k in ('hostHash', 'archiveHash'))}
        return [normalized(v) for v in value] if isinstance(value, list) else value
    return normalized({k: v for k, v in before.items() if k != 'installMetadata'}) == normalized(
        {k: v for k, v in after.items() if k != 'installMetadata'})


class OfficialUpdate:
    def __init__(self, manager):
        self.manager = manager
        self.base = RuntimeContext.from_manager(manager).base_patch_root
        self.store = ContentStore(self.base)
        self.path = self.store._path('official-update/pending.json')
        self.preview = None
        self.process = None
        self.inside = False

    @contextmanager
    def internal(self):
        previous, self.inside = self.inside, True
        try:
            yield
        finally:
            self.inside = previous

    def require_idle(self):
        if not self.inside and self.path.exists():
            fail('官方更新尚未完成，请先继续检查更新结果；不会重放旧补丁。', 'official_update_pending')

    def load(self):
        if not repair.safe_path(self.path).exists():
            return None
        value = json.loads(self.path.read_bytes())
        if value.get('format') != 1 or not re.fullmatch('[a-f0-9]{32}', value.get('id', '')):
            fail('官方更新日志无效，已保留现场。')
        return value

    def status(self):
        value = self.load()
        try:
            self.manager.runtime_repair.candidate()
            available = True
        except (LauncherError, OSError, ValueError):
            available = False
        return {'pending': {k: value.get(k) for k in ('id', 'phase', 'error', 'version', 'suspended')}
                if value else None, 'autoAvailable': available}

    def inputs(self):
        return self.manager.official_host.controls()

    def before_launch(self):
        """Only an explicit launch triggers adoption; state polling is read-only."""
        if self.inside:
            return
        if self.path.exists():
            return self.reconcile()
        changed = False
        try:
            self.manager.tracking.check_host_identity()
        except LauncherError as exc:
            if exc.code != 'host_identity_changed':
                raise
            changed = True
        manifest_missing = self.check_runtime() if not changed else []
        missing = bool(manifest_missing) or self.manager.runtime_repair.status()['status'] == 'missing'
        if not changed and not missing:
            return
        try:
            self.manager.runtime_repair.candidate()
        except LauncherError as exc:
            if exc.code == 'runtime_installer_missing':
                if manifest_missing:
                    fail('官方运行时文件缺失，请提供同版本官方安装包：' + '、'.join(manifest_missing[:6]), 'runtime_missing')
                return
            raise
        if changed:
            return self.reconcile()
        elif missing:
            preview = self.manager.runtime_repair.prepare()
            self.manager.runtime_repair.execute(preview['token'])

    def check_runtime(self):
        """Check a known runtime via directory checkpoints, not just six anchors.

        The CLI is managed by the ordinary patch planner. Other runtime files
        are official-owned; a mismatch is a conflict, never an overwrite.
        """
        reference = self.manager.tracking.load().get('officialPackage')
        if not reference:
            return []
        value = self.manager.official_packages.read(reference)
        runtime = Path(self.manager.settings['executable']).parent / 'resources/runtime'
        entries = {n.removeprefix('resources/runtime/'): e for n, e in value['files'].items()
                   if n.startswith('resources/runtime/')}
        repair.safe_path(runtime)
        try:
            with os.scandir(runtime) as children:
                roots = [Path(child.path) for child in children if child.name != 'cli']
        except FileNotFoundError:
            roots = [runtime]
        # CLI belongs to the switch planner. Its independent close barriers
        # must not invalidate a checkpoint covering every other runtime file.
        # Enumerate top-level entries each time, then certify disjoint trees.
        view = FileView(roots, verification_cache=self.manager.verification_cache)
        # Include generated files when sealing the directory checkpoint. A
        # partial inventory cannot prove the tree unchanged on the next run.
        view.hash_files(progress=lambda n, total: report_progress('files', '复核已知官方运行时（复用目录检查点）', n, total))
        missing, changed = [], []
        for name, entry in entries.items():
            if name.startswith('cli/'):
                continue
            actual = view.hash(runtime / name)
            if actual is None:
                missing.append(name)
            elif actual != entry['sha256']:
                changed.append(name)
        from harness_launcher.host.runtime_integrity import python_extras, verified_python_caches
        files = {n: e['sha256'] for n, e in value['files'].items()}
        changed.extend(n.removeprefix('resources/runtime/') for n in
                       python_extras(self.manager, runtime.parent.parent, files, view=view))
        if changed:
            accepted = verified_python_caches(self.manager, runtime.parent.parent, files,
                                              ['resources/runtime/' + n for n in changed], view=view)
            changed = [n for n in changed if 'resources/runtime/' + n not in accepted]
        if changed:
            fail('运行时存在内容冲突，已保留：' + '、'.join(changed[:6]))
        return missing

    def abort(self):
        with self.manager.lock, self.manager.verification_cache.session(), self.internal():
            pending = self.load()
            if not pending:
                return {'message': '没有待处理的官方更新。'}
            if pending.get('generation') or self.installer_alive(pending):
                fail('安装或版本接续仍在进行，不能撤销。')
            self.manager.require_stopped()
            self.manager.tracking.check_host_identity(pending['selection'])
            self.manager.apply(pending['selection'], hashlib.sha256(self.manager.manifest()[0]).hexdigest())
            pending.update(phase='aborted', finishedAt=time.time())
            write_json(self.store._path('official-update/history/' + pending['id'] + '.json'), pending)
            repair.safe_path(self.path).unlink()
            return {'message': '官方宿主未变化，已恢复更新前插件组合；检查点保留。'}

    def prepare(self, installer=None, sha512=None):
        with self.manager.lock, self.manager.verification_cache.session(), self.manager.official_packages.session():
            self.require_idle()
            self.manager.runtime_repair.idle()
            tracker = self.manager.tracking
            tracker.verify_idle(check_files=True)
            package = self.manager.official_packages.inspect(installer, sha512)
            from harness_launcher.host.patch_modes import archive_info
            current = archive_info(tracker.core.official_archive(), tracker.core.resources / 'app.asar.unpacked')['version']
            target = package['value']['signed']['version']
            if not newer(target, current):
                fail('缓存安装包不是更新版本，未启动安装器，也不会重复下载或重装。', 'official_update_not_newer')
            self.preview = {'token': uuid.uuid4().hex, 'created': time.monotonic(),
                            'inputs': self.inputs(), 'package': package}
            return {'token': self.preview['token'], 'version': target, 'current': current,
                    'message': '安装包已验证。执行将保存检查点、还原受管理补丁，再调用官方安装器；完成后恢复可证明兼容的插件。'}

    def install(self, source):
        """NSIS /D must be last and unquoted. No shell or invented update feed."""
        if os.name != 'nt':
            fail('官方安装仅支持 Windows。')
        destination = str(Path(self.manager.settings['executable']).parent)
        if any(c in source + destination for c in ('"', '\r', '\n', '\x00')):
            fail('安装路径无效。')
        # /S suppresses the finish-page launch; --updated preserves Electron data.
        command = '"' + source + '" /S --updated /D=' + destination
        return subprocess.Popen(command, cwd=str(Path(source).parent),
            env={**os.environ, 'DSH_HOME': str(self.manager.home)},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def execute(self, token):
        with self.manager.lock, self.manager.verification_cache.session(), self.manager.official_packages.session(), self.internal():
            plan = self.preview
            if not plan or token != plan['token'] or time.monotonic() - plan['created'] > 1800:
                fail('官方更新预览已失效，请重新检查。')
            if self.path.exists() or self.inputs() != plan['inputs']:
                fail('预览后配置或更新状态已变化。')
            self.manager.runtime_repair.idle()
            package = self.manager.official_packages.inspect(plan['package']['source'], plan['package']['value']['sha512'])
            if package['reference'] != plan['package']['reference'] and package['value']['files'] != plan['package']['value']['files']:
                fail('预览后安装包变化。')
            raw, _, bundles = self.manager.manifest()
            checkpoint = self.manager.maintenance.capture()
            pending = {'format': 1, 'id': uuid.uuid4().hex, 'phase': 'checkpoint',
                       'checkpoint': checkpoint, 'source': package['source'], 'package': package['reference'],
                       'version': package['value']['signed']['version'], 'selection': [n for n in bundles if n not in OFFICIAL],
                       'oldRoot': str(self.manager.tracking.root), 'startedAt': time.time()}
            write_json(self.path, pending)
            self.preview = None
            try:
                self.manager.apply([], hashlib.sha256(raw).hexdigest(), native=True)
                pending.update(phase='native', nativeInputs=self.inputs())
                write_json(self.path, pending)
                # Keep the authenticated installer stable until its worker exits.
                with repair.read_lease(package['source']):
                    final = self.manager.official_packages.inspect(package['source'], package['value']['sha512'])
                    if final['value']['sha256'] != package['value']['sha256']:
                        fail('安装前官方包已变化，未执行安装器。')
                    pending['phase'] = 'installing'
                    write_json(self.path, pending)
                    self.process = self.install(package['source'])
                    pending['pid'] = self.process.pid
                    write_json(self.path, pending)
                    try:
                        code = self.process.wait(timeout=900)
                    except subprocess.TimeoutExpired:
                        fail('官方安装器仍未结束；保留更新日志，请稍后继续检查。', 'official_update_waiting')
                    pending.update(phase='installed', exitCode=code)
                    write_json(self.path, pending)
                    if code != 0:
                        fail('官方安装器未成功完成（退出码 ' + str(code) + '），已保留检查点。')
                return self.reconcile(package=package)
            except BaseException as exc:
                pending = self.load() or pending
                pending['error'] = str(exc)
                write_json(self.path, pending)
                raise

    def installer_alive(self, pending):
        if pending.get('phase') != 'installing':
            return False
        if self.process is not None:
            return self.process.poll() is None
        if not pending.get('pid'):
            fail('安装器启动结果未知，请确认官方安装已结束后保留日志进行恢复。')
        if os.name != 'nt':
            return True
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pending['pid'])
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            fail('无法确认官方安装器是否退出，已暂停接续。')
        try:
            return api.WaitForSingleObject(handle, 0) != 0
        finally:
            api.CloseHandle(handle)

    def user_evidence(self):
        tracker = self.manager.tracking
        tracker.verify_idle()
        raw, obj, bundles = self.manager.manifest()
        registry, core = tracker.load(), tracker.core.load()
        if core and core['mode'] != 'native' or repair.safe_path(tracker.core.app).exists():
            fail('存在旧版活动补丁或 resources/app 混合安装；已保留文件，不能自动覆盖。')
        ref = registry.get('currentCombination')
        if not ref:
            fail('缺少更新前的已验证组合，不能自动接纳未知文件。')
        combination = tracker.read_object(ref)
        if combination['manifestHash'] != hashlib.sha256(raw).hexdigest():
            fail('加载配置与更新前的记录不一致，已保留修改。')
        try:
            lock = tracker.versions.lock(combination['selection'], self.manager.discover(obj, bundles), registry, combination['hostHash'])
        except (KeyError, ValueError, TypeError):
            fail('更新前的插件组合记录不完整，已保留现场。')
        if any(combination.get(k) != v for k, v in lock.items()):
            fail('更新前的插件版本锁与组合记录不一致，已保留现场。')
        for name in (n for n in bundles if n not in OFFICIAL):
            record = registry['records'].get(name)
            if not record or tracker.read_object(record['delta'])['changes']:
                fail('更新时仍启用了文件补丁：' + name + '；不能自动推断新版文件归属。')
        scopes = {k: p for k, p in tracker.scopes.items() if k not in HOST_SCOPES}
        actual = tracker.versions.content_state(FileView(scopes.values(), verification_cache=self.manager.verification_cache).snapshot(scopes))
        expected = {k: v for k, v in combination['state'].items() if k.split('|')[0] in scopes}
        if actual != expected:
            changed = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
            fail('用户侧文件存在未记录改动，已保留：' + '、'.join(changed[:8]))
        tracker.core.verify_native_config([])
        return raw, registry

    def transferable(self, registry, version):
        tracker, allowed, suspended = self.manager.tracking, {}, {}
        _, obj, bundles = self.manager.manifest()
        for row in self.manager.discover(obj, bundles):
            name = row['id']
            try:
                record = registry['records'].get(name)
                if name == 'dsh-purge' or not record or tracker.read_object(record['delta'])['changes']:
                    fail('含文件补丁或缺少完整记录，需在新版重新适配。')
                tracker.versions.verify_record(name, record)
                meta = json.loads((Path(row['path']) / 'package.json').read_bytes())
                requirement = meta.get('engines', {}).get('dsh')
                if not compatible(version, requirement):
                    fail('未声明可确认兼容的 engines.dsh 范围。')
                before = tracker.store.read_object(record['package'])
                after = tracker.versions.capture(name, before.get('source', ''), store=False, metadata=False)
                if any(filename.lower().endswith('.node') for p in after['packages'].values() for filename in p['files']):
                    fail('包含原生扩展，需针对新版 Electron 重新安装并验证。')
                if not same_package_code(before, after):
                    fail('插件包、依赖或宿主提供的依赖身份已变化。')
                allowed[name] = record
            except (LauncherError, OSError, ValueError, KeyError, TypeError) as exc:
                suspended[name] = str(exc)
        return allowed, suspended

    def reconcile(self, installer=None, sha512=None, *, package=None):
        with self.manager.lock, self.manager.verification_cache.session(), self.manager.official_packages.session(), self.internal():
            self.manager.runtime_repair.idle()
            pending = self.load()
            if pending and self.installer_alive(pending):
                fail('官方安装器仍在运行，请等待完成。', 'official_update_waiting')
            if pending and pending.get('generation'):
                return self.activate(pending)
            if not pending and self.manager.tracking.core.context.patch_root != self.base:
                try:
                    self.manager.tracking.check_host_identity()
                    same_host = True
                except LauncherError as exc:
                    if exc.code != 'host_identity_changed':
                        raise
                    same_host = False
                if same_host:
                    missing_runtime = self.check_runtime()
                    if missing_runtime or self.manager.runtime_repair.status()['status'] != 'ready':
                        preview = self.manager.runtime_repair.prepare(installer, sha512)
                        self.manager.runtime_repair.execute(preview['token'])
                    return {'message': '当前官方版本已经接续，运行时复核完成；无需重建基线。',
                            'suspended': self.manager.tracking.load().get('suspended', {}), 'unchanged': True}
            raw, registry = self.user_evidence()
            controls = self.inputs()
            if package is None:
                if pending:
                    value = self.manager.official_packages.read(pending['package'])
                    installer, sha512 = pending['source'], value['sha512']
                package = self.manager.official_packages.inspect(installer, sha512)
            value = package['value']
            # Validate all existing official files before adding even one missing
            # runtime file; executable/archive/desktop conflicts stop everything.
            missing, conflicts = [], []
            base = Path(self.manager.settings['executable']).parent
            for name, entry in value['files'].items():
                path = repair.safe_path(base / name)
                if not repair.regular(path):
                    (missing if name.startswith('resources/runtime/') else conflicts).append(name)
                elif self.manager.verification_cache.hash(path, hash_file) != entry['sha256']:
                    conflicts.append(name)
            if conflicts:
                from harness_launcher.host.runtime_integrity import verified_python_caches
                accepted = verified_python_caches(self.manager, base,
                    {n: e['sha256'] for n, e in value['files'].items()}, conflicts)
                conflicts = [n for n in conflicts if n not in accepted]
            if conflicts:
                fail('官方安装内容冲突（未覆盖）：' + '、'.join(conflicts[:8]))
            self.manager.official_host.verify_files({'files': {n: e['sha256'] for n, e in value['files'].items() if n not in missing}})
            if missing:
                preview = self.manager.runtime_repair.prepare_package(package)
                self.manager.runtime_repair.execute(preview['token'])
            if self.inputs() != controls:
                fail('校验期间配置发生变化，未接续版本。')
            allowed, suspended = self.transferable(registry, value['signed']['version'])
            if not pending:
                pending = {'format': 1, 'id': uuid.uuid4().hex, 'phase': 'verified',
                           'source': package['source'], 'package': package['reference'],
                           'version': value['signed']['version'], 'oldRoot': str(self.manager.tracking.root),
                           'selection': json.loads(raw)['dsh']['profile']['bundles'], 'startedAt': time.time()}
            pending.update(phase='verified', controls=controls, suspended=suspended)
            write_json(self.path, pending)
            # Build the next generation while the active pointer still refers to
            # the old one. No old state or rollback blob is rewritten.
            staged = copy.copy(self.manager)
            generation = uuid.uuid4().hex
            staged._generation_override = generation
            staged._track_cache = staged._maintenance_cache = staged._official_host_cache = None
            new = staged.tracking
            snapshot = new.snapshot()
            snapshot_ref = new.object('snapshots', snapshot)
            state = new.load()
            state['suspended'] = suspended
            state['suspendedPackages'] = {}
            for name in suspended:
                try:
                    state['suspendedPackages'][name] = new.versions.package_snapshot(name)
                except (LauncherError, OSError, ValueError, KeyError, TypeError):
                    pass  # Unreadable packages require explicit recovery, not deletion.
            state['officialPackage'] = package['reference']
            for name in registry['order']:
                if name not in allowed:
                    continue
                record = dict(allowed[name], enabled=False, hostHash=value['files']['DeepSeek Harness.exe']['sha256'],
                              baseSelection=[], baseVersions={}, baseline=snapshot_ref, after=snapshot_ref,
                              manifestBefore=new.store.put_blob(raw))
                record['package'] = new.versions.package_snapshot(name, record.get('source', ''))
                if not same_package_code(new.store.read_object(allowed[name]['package']), new.store.read_object(record['package'])):
                    fail('兼容性检查后插件包发生变化，未接续：' + name)
                record['revision'] = new.versions.revision(name, record, record['package'], record.get('source', ''),
                                                           allowed[name]['revision'], 'official-update')
                state['records'][name] = record
                state['order'].append(name)
            new.save(state)
            selection = [n for n in pending['selection'] if n in allowed]
            obj = json.loads(raw)
            obj['dsh']['profile']['bundles'] = [*OFFICIAL, *selection]
            after = (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode()
            plugins = [p for p in staged.discover(obj, obj['dsh']['profile']['bundles']) if p['id'] in allowed]
            from harness_launcher.app.switch_plan import SwitchPlan
            planner = SwitchPlan(new)
            plan = planner.prepare(selection, plugins, raw, after, not selection)
            # A fresh snapshot must never turn a concurrent user edit into the
            # next baseline. Compare with the OLD evidence after all long reads.
            if self.inputs() != controls or self.user_evidence()[0] != raw:
                fail('准备新版本期间原始记录变化，未切换活动基线。')
            pending.update(phase='activating', generation=generation, plan=new.store.object('transactions', plan),
                           officialFiles={n: e['sha256'] for n, e in value['files'].items()})
            write_json(self.path, pending)
            return self.activate(pending)

    def activate(self, pending):
        """Idempotent completion after a crash between manifest and pointer writes."""
        staged = copy.copy(self.manager)
        staged._generation_override = pending['generation']
        staged._track_cache = staged._maintenance_cache = staged._official_host_cache = None
        from harness_launcher.app.switch_plan import SwitchPlan
        tracker = staged.tracking
        if str(self.manager.tracking.root) not in (pending['oldRoot'], str(tracker.root)):
            fail('活动版本指针已变化，保留接续日志并停止提交。')
        planner = SwitchPlan(tracker)
        plan = tracker.store.read_object(pending['plan'])
        with ExitStack() as leases:
            for name in ('DeepSeek Harness.exe', 'resources/app.asar'):
                leases.enter_context(repair.read_lease(Path(self.manager.settings['executable']).parent / name))
            self.manager.require_stopped()
            self.manager.official_host.verify_files({'files': pending['officialFiles']})
            if planner.journal.exists():
                planner.recover()
            current = tracker.load()
            if current.get('currentCombination') != plan['combination']:
                planner.execute(plan)
            if not planner.verify_current(plan['selection'],
                [p for p in staged.discover(*staged.manifest()[1:]) if p['id'] not in pending['suspended']],
                staged.manifest()[0]):
                fail('新版组合未通过最终验证。')
            write_json(self.store._path('active-host.json'), {'format': 1, 'generation': pending['generation']})
        pending.update(phase='complete', finishedAt=time.time())
        write_json(self.store._path('official-update/history/' + pending['id'] + '.json'), pending)
        repair.safe_path(self.path).unlink()
        return {'message': '官方版本已接续；兼容插件已恢复，旧基线及回滚材料保留。',
                'version': pending['version'], 'restored': plan['selection'], 'suspended': pending['suspended'],
                'generation': pending['generation']}
