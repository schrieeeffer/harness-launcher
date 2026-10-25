"""Authenticated native launch after an official update; never replay old layers.

The new baseline is an independent, content-addressed receipt. Old combinations,
adapter state, snapshots and packages stay byte-for-byte intact. An approval only
permits the already-native manifest to launch, not plugin switching or recording.
"""
from contextlib import ExitStack
import base64
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

from errors import LauncherError
from file_view import FileView
from operations import report_progress
from storage import hash_file, write_json
from domain import relative_path
import runtime_repair as repair


def blocked(message, code='official_host_blocked'):
    raise LauncherError(message, code=code,
        recovery='打开“设置与说明 → 官方更新后原生启动”。保留旧补丁及回滚数据；不要直接替换旧记录的哈希。')


class OfficialHost:
    def __init__(self, manager):
        self.manager = manager
        self.plan = None

    @property
    def tracker(self):
        return self.manager.tracking

    @property
    def pointer(self):
        return self.tracker.root / 'official-host.json'

    def controls(self):
        return {str(path): hash_file(repair.safe_path(path)) if path.is_file() else None
                for path in (self.tracker.index, self.tracker.core.record, self.manager.manifest_path)}

    def read(self):
        if not repair.safe_path(self.pointer).is_file():
            return None
        pointer = json.loads(self.pointer.read_bytes())
        value = self.tracker.store.read_object(pointer['baseline'])
        if value.get('kind') != 'official-native-launch' or value.get('format') != 1:
            blocked('新版原生启动记录格式无效。')
        for name in value['files']:
            relative_path(name)
        return value

    def status(self):
        try:
            self.tracker.check_host_identity()
            return {'status': 'ready', 'message': ''}
        except LauncherError as exc:
            if exc.code != 'host_identity_changed':
                return {'status': 'blocked', 'message': str(exc)}
            components = exc.details.get('components', [])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'status': 'blocked', 'message': '无法读取宿主校验记录：' + str(exc)}
        try:
            approved = self.read()
            if approved and self.matches(approved):
                return {'status': 'native_ready', 'message': '新版官方原生启动已建立独立基线；启动前复核文件。旧版插件记录保留，不能跨版本启用。',
                        'version': approved['version']}
        except (OSError, ValueError, KeyError, TypeError, LauncherError):
            pass  # Corrupt/stale approval never suppresses the host boundary.
        return {'status': 'changed', 'components': components,
                'message': '检测到 Harness 更新或文件变化。旧组合不适用于当前安装；请验证同版本官方安装包后建立原生启动基线。'}

    def matches(self, value):
        exe = Path(self.manager.settings['executable'])
        return (value['executable'] == str(exe) and value['controls'] == self.controls() and
                all(self.tracker.versions.hash(repair.safe_path(exe.parent / name)) == value['files'][name]
                    for name in ('DeepSeek Harness.exe', 'resources/app.asar')))

    def native_inputs(self):
        """Prove the old combination was native; never silently disable plugins."""
        from launcher import OFFICIAL
        self.manager.runtime_repair.idle()
        self.manager.runtime_repair.require_ready()
        raw, obj, bundles = self.manager.manifest()
        if set(bundles) != set(OFFICIAL):
            blocked('当前配置仍启用了额外插件，不能将它直接接纳为新版原生环境。')
        tracker = self.tracker
        registry, core = tracker.load(), tracker.core.load()
        if any(r['enabled'] for r in registry['records'].values()) or core and core['mode'] != 'native':
            blocked('更新前的插件层尚未还原；请保留现场，不能自动将混合安装认定为原生环境。')
        if repair.safe_path(tracker.core.app).exists():
            blocked('存在 resources/app 解包目录，不能确认官方原生入口。')
        reference = registry.get('currentCombination')
        if not reference:
            blocked('缺少更新前已验证的原生组合，不能自动接纳当前用户侧文件。')
        combination = tracker.read_object(reference)
        if combination['selection'] or combination['manifestHash'] != hashlib.sha256(raw).hexdigest():
            blocked('当前配置与更新前的原生组合不一致，不能自动建立新版基线。')
        tracker.core.verify_native_config([])
        # Official host paths will be checked against the signed package. The
        # generated root YAML is checked structurally; user-side patch paths
        # must still match the old native combination, never today's snapshot.
        scopes = {k: p for k, p in tracker.scopes.items()
                  if k not in ('app', 'archive', 'archive_backup', 'unpacked', 'cli', 'root_yaml')}
        expected = {k: v for k, v in combination['state'].items() if k.split('|')[0] in scopes}
        view = FileView(scopes.values(), verification_cache=self.manager.verification_cache)
        actual = tracker.versions.content_state(view.snapshot(scopes))
        if actual != expected:
            blocked('用户侧插件文件与原生记录不一致，已保留修改；不能把未知改动接纳为新版基线。')
        return self.controls()

    def verify_files(self, value):
        exe = Path(self.manager.settings['executable'])
        paths = {name: repair.safe_path(exe.parent / name) for name in value['files']}
        view = FileView(paths.values(), verification_cache=self.manager.verification_cache)
        view.hash_files(progress=lambda n, total: report_progress('files', '复核新版官方启动文件', n, total))
        bad = [name for name, path in paths.items() if view.hash(path) != value['files'][name]]
        if bad:
            blocked('官方文件缺失或存在内容冲突，未覆盖：' + '、'.join(bad[:4]))
        # Unknown unpacked/CLI files could change module resolution. Retain but
        # reject them; other unexecuted user extras are never removed.
        for directory in ('resources/app.asar.unpacked', 'resources/runtime/cli'):
            root = repair.safe_path(exe.parent / directory)
            if not root.exists():
                continue
            extra_view = FileView([root], verification_cache=self.manager.verification_cache)
            for path, entry in extra_view.entries.items():
                if entry['type'] == 'link' or entry['type'] == 'file' and path.relative_to(exe.parent).as_posix() not in value['files']:
                    blocked('官方模块目录含额外文件或链接，已保留：' + str(path))

    def verify(self, value):
        if self.native_inputs() != value['controls'] or not self.matches(value):
            blocked('验证后宿主或原生记录已变化，请重新检查。', 'official_host_stale')
        self.verify_files(value)
        if self.native_inputs() != value['controls'] or not self.matches(value):
            blocked('校验期间宿主或原生记录变化，请重新检查。', 'official_host_stale')

    def prepare(self, installer=None, sha512=None):
        with self.manager.lock, self.manager.verification_cache.session():
            self.plan = None
            controls = self.native_inputs()
            source, expected = self.manager.runtime_repair.candidate(installer, sha512)
            base = repair.safe_path(self.manager.data / 'official-host-staging')
            base.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(base).free < source.stat().st_size * 2 + 64 * 1024 ** 2:
                blocked('暂存磁盘空间不足。')
            stage = base / uuid.uuid4().hex
            stage.mkdir()
            cleanup_pending = False
            try:
                cached = stage / 'installer.exe'
                report_progress('packages', '验证新版官方安装包 SHA-512 与签名')
                with repair.read_lease(source):
                    shutil.copyfile(source, cached)
                with repair.read_lease(cached):
                    with cached.open('rb') as f:
                        checksum = hashlib.file_digest(f, 'sha512').digest()
                    if checksum != expected:
                        blocked('安装包 SHA-512 不匹配。')
                    signed = repair.signature(cached)
                    payload, sizes = repair.SevenZip().extract_host(cached, stage)
                files = {}
                for i, (name, size) in enumerate(sizes.items(), 1):
                    path = repair.safe_path(payload / name)
                    if not repair.regular(path) or path.stat().st_size != size:
                        blocked('官方解压文件与清单不一致：' + name)
                    files[name] = hash_file(path)
                    if i % 50 == 0 or i == len(sizes):
                        report_progress('files', '建立新版官方启动校验清单', i, len(sizes))
                value = {'format': 1, 'kind': 'official-native-launch', 'version': signed['version'],
                         'publisher': signed['publisher'], 'installerSHA512': base64.b64encode(checksum).decode(),
                         'executable': self.manager.settings['executable'], 'controls': controls, 'files': files}
                self.verify(value)
            finally:
                # Only this call's newly created UUID directory; never host or profile.
                try:
                    shutil.rmtree(repair.safe_path(stage))
                except OSError:
                    cleanup_pending = True
            self.plan = {'token': uuid.uuid4().hex, 'created': time.monotonic(), 'value': value}
            return {'token': self.plan['token'], 'version': value['version'], 'files': len(files),
                    'cleanupPending': cleanup_pending,
                    'message': f'官方签名及 {len(files)} 个启动文件校验通过；可保存独立原生基线。旧插件和回滚数据不变。'}

    def accept(self, token):
        with self.manager.lock, self.manager.verification_cache.session():
            if not self.plan or not isinstance(token, str) or token != self.plan['token'] or time.monotonic() - self.plan['created'] > 1800:
                blocked('原生基线预览已失效，请重新验证。')
            value = self.plan['value']
            with ExitStack() as leases:
                self.lease_inputs(leases, value)
                self.verify(value)
                reference = self.tracker.store.object('baselines', value)
                write_json(repair.safe_path(self.pointer), {'baseline': reference})
            self.plan = None
            return {'message': '新版原生启动基线已保存。返回首页可启动；旧插件记录保留且不会跨版本回放。'}

    def lease_inputs(self, leases, value):
        exe = Path(self.manager.settings['executable'])
        paths = [exe, exe.parent / 'resources/app.asar', self.tracker.index,
                 self.tracker.core.record, self.manager.manifest_path]
        for path in paths:
            if path.exists():
                leases.enter_context(repair.read_lease(path))

    def launch(self, spawn):
        """No manifest, registry, adapter or installed-file writes on this lane."""
        value = self.read()
        if value is None:
            blocked('请先验证同版本官方安装包并保存原生启动基线。')
        with ExitStack() as leases:
            self.lease_inputs(leases, value)
            self.verify(value)
            report_progress('launch', '正在启动已验证的新版原生 Harness')
            self.manager.require_stopped()
            spawn()
        return {'message': '新版原生启动请求已发送；旧版插件未加载。', 'changed': False,
                'launched': True, 'patchMode': 'native', 'verifiedCurrent': True, 'officialNative': True}
