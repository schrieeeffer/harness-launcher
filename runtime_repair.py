"""Verified, additive recovery of the official Windows desktop runtime.

Fast launch checks never hash a tree or access the network. Explicit preparation
authenticates a private installer copy and binds extracted bytes to the installed
host. Commit only creates absent files: existing files, profiles and plugin
records are never overwritten or removed, including after interruption.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

from errors import LauncherError
from operations import report_progress
from storage import hash_file, write_json

PUBLISHER = 'Hangzhou DeepSeek Artificial Intelligence Co., Ltd.'
ANCHORS = ('versions.json', 'office-skills/office/SKILL.md', 'cli/bin/dsh.cmd',
           'primary-runtime/dependencies/python/python.exe',
           'primary-runtime/dependencies/python/Lib/encodings/__init__.py',
           'primary-runtime/dependencies/node/bin/node.exe')
MAX_BYTES = 8 * 1024 ** 3
MAX_FILES = 100000


def fail(message, code='runtime_repair_blocked'):
    raise LauncherError(message, code=code, recovery='打开“设置与说明 → 官方运行时修复”，查看检查结果；不要删除用户数据或回滚记录。')


def safe_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            fail('修复路径不能经过链接或重解析点：' + str(item))
    return path


def regular(path):
    path = safe_path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        fail('预期文件的位置不是普通文件：' + str(path))
    return True


@contextmanager
def read_lease(path):
    """On Windows deny file writes/deletion while signatures or hashes are used."""
    path = safe_path(path)
    handle = None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            fail('文件正在被写入或无法锁定，请等待官方更新结束并退出 Harness：' + str(path))
    try:
        if not regular(path):
            fail('文件缺失：' + str(path))
        yield
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


def signature(path):
    if os.name != 'nt':
        fail('官方运行时修复仅支持 Windows。')
    shell = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = '''$ErrorActionPreference='Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$p=$env:HARNESS_REPAIR_SIGNED_FILE
$s=Get-AuthenticodeSignature -LiteralPath $p
$v=[Diagnostics.FileVersionInfo]::GetVersionInfo($p)
$n=if($s.SignerCertificate){$s.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false)}else{''}
@{status=[string]$s.Status;publisher=$n;version=$v.FileVersion;thumbprint=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{''}} | ConvertTo-Json -Compress
'''
    try:
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
            base64.b64encode(script.encode('utf-16le')).decode()], env={**os.environ, 'HARNESS_REPAIR_SIGNED_FILE': str(path),
                'PSModulePath': str(shell.parent / 'Modules')},
            capture_output=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
        value = json.loads(result.stdout.decode('utf-8-sig'))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        fail('无法完成 Windows 发布者签名验证：' + type(exc).__name__)
    if value.get('status') != 'Valid' or value.get('publisher') != PUBLISHER:
        fail('安装包没有有效的 DeepSeek 官方发布者签名。', 'runtime_invalid_signature')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', value.get('version', '')):
        fail('安装包的签名版本信息无法识别。')
    return value


def archive_entries(output):
    """Parse 7-Zip's technical listing; reject Windows aliases and links up front."""
    if '\n----------\n' not in output.replace('\r\n', '\n'):
        fail('无法读取安装包文件清单。')
    text = output.replace('\r\n', '\n').split('\n----------\n', 1)[1]
    result, seen, total = [], set(), 0
    for block in text.strip().split('\n\n'):
        fields = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
        if 'Path' not in fields:
            continue
        path = fields['Path'].replace('\\', '/')
        parts = path.split('/')
        if any(not part or part in ('.', '..') or part.endswith(('.', ' ')) or
               re.search(r'[<>:"|?*\x00-\x1f]', part) or
               re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\..*)?', part, re.I)
               for part in parts) or path.casefold() in seen:
            fail('安装包包含不安全或重复的文件路径。')
        if any(key in fields for key in ('Symbolic Link', 'Hard Link', 'Alternate Stream')) or 'L' in fields.get('Attributes', ''):
            fail('安装包包含链接，不能自动修复。')
        seen.add(path.casefold())
        folder = fields.get('Folder') == '+' or fields.get('Attributes', '').startswith('D')
        try:
            size = int(fields.get('Size') or '0')
        except ValueError:
            fail('安装包文件大小无效。')
        total += size
        if size < 0 or total > MAX_BYTES or len(seen) > MAX_FILES:
            fail('安装包超过修复工具的文件数或空间限制。')
        result.append({'path': path, 'size': size, 'folder': folder})
    if not result:
        fail('安装包清单为空。')
    file_paths = {e['path'].casefold() for e in result if not e['folder']}
    for entry in result:
        for parent in Path(entry['path']).parents:
            if parent.as_posix() == '.':
                break
            if parent.as_posix().casefold() in file_paths:
                fail('安装包存在文件与目录冲突。')
    return result


class SevenZip:
    def __init__(self):
        candidates = [Path(os.environ.get(key, 'C:/Program Files')) / '7-Zip/7z.exe'
                      for key in ('ProgramFiles', 'ProgramFiles(x86)')]
        self.exe = next((p for p in candidates if p.is_file()), None)
        if self.exe is None:
            fail('需要安装 7-Zip 才能读取官方安装包。请从 https://www.7-zip.org/ 安装后重试。', 'runtime_extractor_missing')
        safe_path(self.exe)

    def run(self, *args):
        try:
            result = subprocess.run([str(self.exe), *map(str, args)], capture_output=True,
                timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
            return result.stdout.decode('utf-8-sig', errors='strict')
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            fail('安装包读取失败（不执行安装程序）：' + type(exc).__name__)

    def listing(self, path):
        return archive_entries(self.run('l', '-slt', '-sccUTF-8', path))

    def unpack(self, installer, stage):
        outer = self.listing(installer)
        if not any(e['path'] == '$PLUGINSDIR/app-64.7z' and not e['folder'] for e in outer):
            fail('安装包不是支持的 Windows x64 官方包。')
        self.run('e', '-y', '-spd', '-sccUTF-8', installer, '$PLUGINSDIR\\app-64.7z', '-o' + str(stage))
        payload = stage / 'app-64.7z'
        regular(payload)
        entries = self.listing(payload)
        return payload, entries

    def extract_host(self, installer, stage):
        """Read official desktop files, CLI and launch anchors without copying all Python packages."""
        payload, entries = self.unpack(installer, stage)
        anchors = {'resources/runtime/' + name for name in ANCHORS}
        expected = {e['path']: e['size'] for e in entries if not e['folder'] and
                    (not e['path'].startswith('resources/runtime/') or
                     e['path'].startswith('resources/runtime/cli/') or e['path'] in anchors)}
        if not (anchors | {'DeepSeek Harness.exe', 'resources/app.asar'}) <= expected.keys():
            fail('官方包缺少宿主或运行时启动文件，不能建立新版原生基线。')
        if shutil.disk_usage(stage).free < sum(expected.values()) + 64 * 1024 ** 2:
            fail('暂存磁盘空间不足，不能验证新版官方安装。')
        listing = stage / 'members.txt'
        listing.write_text('\n'.join(expected) + '\n', encoding='utf-8')
        self.run('x', '-y', '-spd', '-sccUTF-8', '-scsUTF-8', payload,
                 '@' + str(listing), '-o' + str(stage / 'payload'))
        return stage / 'payload', expected

    def extract(self, installer, stage):
        payload, entries = self.unpack(installer, stage)
        expected = {e['path'].removeprefix('resources/runtime/'): e['size'] for e in entries
                    if e['path'].startswith('resources/runtime/') and not e['folder']}
        if not all(name in expected for name in ANCHORS):
            fail('安装包缺少必需运行时文件，或采用了尚不支持的目录布局。')
        required = ('DeepSeek Harness.exe', 'resources/app.asar')
        if not all(any(e['path'] == name and not e['folder'] for e in entries) for name in required):
            fail('安装包缺少宿主身份文件。')
        needed = sum(expected.values()) + sum(e['size'] for e in entries if e['path'] in required)
        if shutil.disk_usage(stage).free < needed + 64 * 1024 ** 2:
            fail('暂存目录所在磁盘空间不足，请释放空间后重新检查。')
        self.run('x', '-y', '-sccUTF-8', payload, 'resources\\runtime\\*', *required, '-o' + str(stage / 'payload'))
        return stage / 'payload', expected


class RuntimeRepair:
    def __init__(self, manager):
        self.manager = manager
        self.exe = Path(manager.settings['executable']).absolute()
        self.resources = self.exe.parent / 'resources'
        self.root = self.resources / 'runtime'
        self.folder = manager.data / 'runtime-repair'
        self.plan = None

    def status(self):
        missing = []
        try:
            for name in ANCHORS:
                path = self.root / name
                if not regular(path) or path.stat().st_size == 0:
                    missing.append(name)
        except (OSError, LauncherError) as exc:
            return {'status': 'blocked', 'message': str(exc), 'missing': missing}
        return {'status': 'missing' if missing else 'ready', 'missing': missing,
                'message': f'发现 {len(missing)} 个必需文件缺失或为空，请完整检查后修复。' if missing else
                '必需文件存在；此快速检查不代表全部运行时文件完整，可按需进行完整检查。'}

    def require_ready(self):
        value = self.status()
        if value['status'] != 'ready':
            fail(value['message'], 'runtime_missing')

    def idle(self):
        self.manager.require_stopped()
        # Do not call tracking.guard(): an official update may have invalidated
        # old plugin anchors. Runtime recovery must not adopt or replay them.
        tracker = self.manager.tracking
        summary = tracker.summary()
        if tracker.busy or summary.get('session') or summary.get('pending') or self.manager.maintenance.load():
            fail('插件安装、记录或维护尚未结束，请先处理该操作。')
        for path in (self.exe, self.resources, self.folder):
            safe_path(path)

    def candidate(self, installer=None, expected_sha512=None):
        if installer is not None and (not isinstance(installer, str) or not installer.strip()):
            fail('官方安装包路径无效。')
        if installer:
            path = Path(installer).expanduser().absolute()
        else:
            pending = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / '@deepseek-aidsh-desktop-updater/pending'
            info_path = safe_path(pending / 'update-info.json')
            try:
                info = json.loads(info_path.read_text(encoding='utf-8-sig'))
                name = info['fileName']
                if not isinstance(name, str) or not re.fullmatch(r'deepseek-harness-[A-Za-z0-9.+-]+-win-x64\.exe', name):
                    raise ValueError()
                path, expected_sha512 = pending / name, info['sha512']
            except (OSError, ValueError, KeyError, TypeError):
                fail('没有可用的官方更新缓存，请填写同版本官方安装包路径及 SHA-512。', 'runtime_installer_missing')
        if not isinstance(expected_sha512, str):
            fail('请提供官方公布的安装包 SHA-512（Base64 或十六进制）。')
        try:
            expected = bytes.fromhex(expected_sha512) if re.fullmatch('[0-9a-fA-F]{128}', expected_sha512) else base64.b64decode(expected_sha512, validate=True)
            if len(expected) != 64:
                raise ValueError()
        except ValueError:
            fail('安装包 SHA-512 格式无效。')
        if not regular(path) or path.stat().st_size > 2 * 1024 ** 3:
            fail('安装包不存在或大小不受支持。')
        return path, expected

    def inventory(self, expected):
        missing, conflicts = [], []
        for i, (name, entry) in enumerate(expected.items(), 1):
            path = safe_path(self.root / name)
            if not regular(path):
                missing.append(name)
            else:
                with read_lease(path):
                    if path.stat().st_size != entry['size'] or hash_file(path) != entry['sha256']:
                        conflicts.append(name)
            if i % 50 == 0 or i == len(expected):
                report_progress('files', '核验现有运行时；保留非官方文件', i, len(expected))
        return missing, conflicts

    def discard(self):
        if not self.plan:
            return {'message': '没有当前修复预览；异常退出留下的旧暂存目录不会自动删除。'}
        if self.plan:
            stage = safe_path(self.plan['stage'])
            base = safe_path(self.folder / 'staging')
            if stage.parent != base or not re.fullmatch('[a-f0-9]{32}', stage.name):
                fail('临时目录身份无效，已保留现场。')
            # This directory is created by us and is never a host/profile root.
            shutil.rmtree(stage)
            self.plan = None
        return {'message': '已关闭预览并清理本次临时解压文件。'}

    def prepare(self, installer=None, expected_sha512=None):
        with self.manager.lock:
            self.idle()
            self.discard()
            source, expected_sha = self.candidate(installer, expected_sha512)
            extractor = SevenZip()
            staging = safe_path(self.folder / 'staging')
            staging.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(staging).free < source.stat().st_size * 2 + 64 * 1024 ** 2:
                fail('暂存目录所在磁盘空间不足，无法安全解包官方安装包。')
            stage = staging / uuid.uuid4().hex
            stage.mkdir()
            self.plan = {'stage': stage}
            try:
                report_progress('packages', '核验官方安装包 SHA-512 和发布者签名')
                cached = stage / 'installer.exe'
                with read_lease(source):
                    shutil.copyfile(source, cached)
                with read_lease(cached):
                    with cached.open('rb') as stream:
                        sha512 = hashlib.file_digest(stream, 'sha512').digest()
                    if sha512 != expected_sha:
                        fail('安装包 SHA-512 不匹配，未修改安装目录。', 'runtime_checksum_mismatch')
                    signed = signature(cached)
                    report_progress('packages', '解出同版本官方运行时（可能需要数分钟）')
                    payload, sizes = extractor.extract(cached, stage)
                archive = self.resources / 'app.asar'
                # Active/retained plugin archives are deliberately not guessed.
                if not regular(archive):
                    fail('缺少当前 app.asar，无法证明同版本官方基线；请先处理宿主安装或插件状态。')
                with read_lease(self.exe), read_lease(archive):
                    host = {str(self.exe): hash_file(self.exe), str(archive): hash_file(archive)}
                    if host[str(self.exe)] != hash_file(payload / 'DeepSeek Harness.exe') or host[str(archive)] != hash_file(payload / 'resources/app.asar'):
                        fail('安装包与当前主程序或 app.asar 不一致，不能跨版本修复或覆盖插件补丁。', 'runtime_host_mismatch')
                runtime = payload / 'resources/runtime'
                entries = {}
                for i, (name, size) in enumerate(sizes.items(), 1):
                    path = safe_path(runtime / name)
                    if not regular(path) or path.stat().st_size != size:
                        fail('解压文件与官方清单不一致：' + name)
                    entries[name] = {'size': size, 'sha256': hash_file(path)}
                    if i % 50 == 0 or i == len(sizes):
                        report_progress('files', '建立官方运行时文件校验清单', i, len(sizes))
                # Authentication and host equality are now bound to in-memory
                # hashes. Retain only runtime payload until execute/discard.
                for temporary in (cached, stage / 'app-64.7z', payload / 'DeepSeek Harness.exe', payload / 'resources/app.asar'):
                    try:
                        safe_path(temporary).unlink(missing_ok=True)
                    except OSError:
                        pass  # Final cleanup will retry; never mask verified data.
                missing, conflicts = self.inventory(entries)
                self.plan.update(token=uuid.uuid4().hex, created=time.monotonic(), signed=signed,
                    sha512=base64.b64encode(sha512).decode(), host=host, runtime=runtime,
                    entries=entries, missing=missing, conflicts=conflicts)
                return {'token': self.plan['token'], 'version': signed['version'], 'publisher': signed['publisher'],
                    'sha512': self.plan['sha512'], 'total': len(entries), 'missing': missing, 'conflicts': conflicts,
                    'bytes': sum(entries[n]['size'] for n in missing),
                    'message': f'已验证官方签名及宿主一致性；共 {len(entries)} 个文件，缺失 {len(missing)} 个，内容冲突 {len(conflicts)} 个。',
                    'preserved': ['账号、会话和工作区', '插件、共享依赖及已有文件', '补丁记录和回滚数据'],
                    'blocked': ['已有文件与官方内容不同；先处理插件改动或损坏文件，修复不会覆盖。'] if conflicts else []}
            except BaseException:
                self.discard()
                raise

    def execute(self, token):
        with self.manager.lock:
            self.idle()
            plan = self.plan
            if not plan or not isinstance(token, str) or token != plan.get('token') or time.monotonic() - plan.get('created', 0) > 1800:
                fail('修复预览已失效，请重新完整检查。', 'runtime_preview_expired')
            if plan['conflicts']:
                fail('预览存在内容冲突，不能执行修复。')
            from contextlib import ExitStack
            with ExitStack() as leases:
                for path, expected in plan['host'].items():
                    leases.enter_context(read_lease(path))
                    if hash_file(path) != expected:
                        fail('预览后官方宿主已变化，请重新检查。', 'runtime_host_mismatch')
                missing, conflicts = self.inventory(plan['entries'])
                if conflicts or missing != plan['missing']:
                    fail('预览后运行时已变化，请重新检查。', 'runtime_preview_changed')
                if shutil.disk_usage(safe_path(self.resources)).free < sum(plan['entries'][n]['size'] for n in missing) + 64 * 1024 ** 2:
                    fail('Harness 所在磁盘空间不足，尚未补齐任何文件。')
                record = {'format': 1, 'version': plan['signed']['version'], 'host': plan['host'],
                    'installerSHA512': plan['sha512'], 'publisher': plan['signed']['publisher'],
                    'files': {name: plan['entries'][name] for name in missing}, 'status': 'repairing',
                    'target': str(self.root), 'startedAt': time.time()}
                receipt = safe_path(self.folder / 'receipts' / (token + '.json'))
                write_json(receipt, record)
                try:
                    for i, name in enumerate(missing, 1):
                        self.manager.require_stopped()
                        source = safe_path(plan['runtime'] / name)
                        target = safe_path(self.root / name)
                        with read_lease(source):
                            if hash_file(source) != plan['entries'][name]['sha256']:
                                fail('临时修复文件校验失败：' + name)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            safe_path(target)
                            # Write elsewhere on the target volume, then link with
                            # no-replace semantics. A concurrent creator wins.
                            fd, temp = tempfile.mkstemp(prefix='.harness-repair-', dir=target.parent)
                            temp = Path(temp)
                            try:
                                with os.fdopen(fd, 'wb') as out, source.open('rb') as stream:
                                    shutil.copyfileobj(stream, out, 1024 * 1024)
                                    out.flush()
                                    os.fsync(out.fileno())
                                if hash_file(temp) != plan['entries'][name]['sha256']:
                                    fail('写入后校验失败：' + name)
                                safe_path(target)
                                os.link(temp, target)  # Never overwrite an existing file.
                            finally:
                                safe_path(temp).unlink(missing_ok=True)
                        report_progress('sync', '补齐已验证的缺失文件', i, len(missing))
                    self.manager.require_stopped()
                    absent, changed = self.inventory(plan['entries'])
                    if absent or changed:
                        fail('修复期间文件发生变化，已保留现场，请重新检查。')
                    record['status'] = 'verified'
                    record['finishedAt'] = time.time()
                    write_json(receipt, record)
                except BaseException:
                    record['status'] = 'incomplete'
                    write_json(receipt, record)
                    # Existing bytes were never touched. Keep verified additions
                    # so a fresh preview safely resumes rather than deletes data.
                    raise
            cleanup = ''
            try:
                self.discard()
            except OSError:
                cleanup = ' 临时文件暂未清理，请稍后点击取消并清理临时文件。'
            return {'message': f'已补齐 {len(missing)} 个运行时文件并完整验证。可返回启动 Harness；插件兼容性仍单独检查。' + cleanup,
                    'cleanupPending': bool(cleanup),
                    'restored': len(missing), 'receipt': str(receipt)}
