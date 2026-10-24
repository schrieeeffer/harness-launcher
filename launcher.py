"""Local, dependency-free plugin launch menu for DeepSeek Harness on Windows."""
from __future__ import annotations

import argparse
import copy
import ctypes
from contextlib import contextmanager
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from patch_modes import PatchError, file_hash
from adapters import PURGE, is_adapter
from errors import LauncherError, error_payload
from settings_store import load_settings, validate_settings
from storage import atomic_write, write_json
from operations import OperationLog, report_progress
from plugin_tracking import PluginTracking
from switch_plan import SwitchPlan
from catalog import Catalog, CatalogError
from verification_cache import VerifiedContentCache

ROOT = Path(__file__).resolve().parent
VERSION = "0.3.2"
# AppData can present a per-file MSIX overlay to a packaged caller and a
# different file to Explorer. A home directory has one stable physical root.
DATA = Path.home() / ".harness-launcher"
OFFICIAL = ("@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app")
NAME = re.compile(r"^(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*$")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


UNINSTALL_KEYS = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                  r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")


def registry_executable(winreg=None):
    """Read the installer's uninstall entry, which also covers custom install folders. Read-only."""
    if winreg is None:
        try:
            import winreg
        except ImportError:
            return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key in UNINSTALL_KEYS:
            try:
                root = winreg.OpenKey(hive, key)
            except OSError:
                continue
            with root:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    values = {}
                    try:
                        with winreg.OpenKey(root, name) as entry:
                            for field in ("DisplayName", "DisplayIcon", "InstallLocation", "UninstallString"):
                                try:
                                    values[field] = str(winreg.QueryValueEx(entry, field)[0])
                                except OSError:
                                    pass
                    except OSError:
                        continue
                    if not values.get("DisplayName", "").startswith("DeepSeek Harness"):
                        continue
                    icon = re.sub(r",\s*-?\d+$", "", values.get("DisplayIcon", "")).strip().strip('"')
                    uninstaller = re.match(r'\s*"?([^"]+?\.exe)', values.get("UninstallString", ""), re.I)
                    folders = [values.get("InstallLocation", "").strip().strip('"'),
                               str(Path(uninstaller[1]).parent) if uninstaller else ""]
                    candidates = ([Path(icon)] if icon else []) + [Path(f) / "DeepSeek Harness.exe" for f in folders if f]
                    for candidate in candidates:
                        if candidate.name.lower() == "deepseek harness.exe" and candidate.is_file():
                            return candidate
    return None


def install_candidates():
    candidates = []
    for folder in (os.environ.get('LOCALAPPDATA'), os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')):
        if folder:
            base = Path(folder)
            candidates.extend([base / 'Programs' / 'DeepSeek Harness' / 'DeepSeek Harness.exe',
                               base / 'DeepSeek Harness' / 'DeepSeek Harness.exe'])
    candidates.append(ROOT.parent / 'DeepSeek Harness.exe')
    return candidates


def detect_executable():
    """Return an existing DeepSeek Harness.exe: DSH_EXECUTABLE, the installer registry entry, then common folders."""
    configured = os.environ.get('DSH_EXECUTABLE')
    if configured and Path(configured).expanduser().is_file():
        return Path(configured).expanduser()
    return registry_executable() or next((p for p in install_candidates() if p.is_file()), None)


def default_executable():
    """Use explicit configuration or detected install locations, never a developer path."""
    configured = os.environ.get('DSH_EXECUTABLE')
    if configured:
        return str(Path(configured).expanduser())
    return str(detect_executable() or install_candidates()[0])


class MenuError(LauncherError):
    pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def digest(data):
    return hashlib.sha256(data).hexdigest()


def running_harness():
    """Read the process table. Never terminate a process or touch a session lock."""
    if os.name != "nt":
        return []
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise MenuError("无法读取运行状态，请稍后重试。")
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "deepseek harness.exe":
                found.append(entry.th32ProcessID)
            ok = kernel.Process32NextW(handle, ctypes.byref(entry))
        # Only ERROR_NO_MORE_FILES proves enumeration completed. An access or
        # snapshot error must not permit patch writes to a running application.
        if ctypes.get_last_error() != 18:
            raise MenuError("无法读取运行状态，请稍后重试。")
    finally:
        kernel.CloseHandle(handle)
    return found


class Manager:
    def __init__(self, data=DATA, home=None, executable=None):
        self.data = Path(data).absolute()
        if self.data.with_name(self.data.name + '.upgrade.json').exists():
            raise MenuError('目录迁移尚未完成，请退出后运行 migrate.py --recover。', code='migration_pending')
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.verification_cache = VerifiedContentCache(self.data / 'verification-cache.json')
        self.settings_file = self.data / "settings.json"
        self.settings = load_settings(self.settings_file, {
            'home': str(home or os.environ.get('DSH_HOME') or Path.home() / '.dsh'),
            'executable': str(executable or default_executable()),
            'schemes': {}, 'schemeLocks': {}, 'order': [], 'lastSelection': None,
        })
        if home:
            self.settings["home"] = str(home)
        if executable:
            self.settings["executable"] = str(executable)
        # Environment variables and saved paths may be relative or use ~.
        # Resolve them once, before Popen changes cwd to the application folder.
        for key in ('home', 'executable'):
            self.settings[key] = str(Path(self.settings[key]).expanduser().resolve())
        self._track_cache = None
        # Establish the root schema before any tracking object can be created.
        # A first install must remain reopenable even if settings were never edited.
        if not self.settings_file.exists():
            self.persist()

    @property
    def tracking(self):
        with self.lock:
            identity = (str(self.home), self.settings['executable'])
            if self._track_cache is None or self._track_cache[0] != identity:
                self._track_cache = (identity, PluginTracking(self))
            return self._track_cache[1]

    @property
    def home(self):
        return Path(self.settings["home"]).expanduser().resolve()

    @property
    def profile(self):
        return self.home / "profiles" / "desktop"

    @property
    def manifest_path(self):
        return self.profile / "package.json"

    def persist(self, settings=None):
        candidate = copy.deepcopy(self.settings if settings is None else settings)
        validate_settings(candidate)
        write_json(self.settings_file, candidate)
        self.settings = candidate

    def manifest(self):
        try:
            raw = self.manifest_path.read_bytes()
            obj = json.loads(raw.decode("utf-8-sig"))
            bundles = obj["dsh"]["profile"]["bundles"]
            if not isinstance(bundles, list) or not all(isinstance(x, str) and NAME.fullmatch(x) for x in bundles):
                raise ValueError("unsupported bundles")
            if not all(x in bundles for x in OFFICIAL):
                raise MenuError("此配置缺少桌面版必需的官方 bundle，暂不支持修改。")
            if len(bundles) != len(set(bundles)):
                raise MenuError("插件配置有重复项，请先修复 package.json。")
            return raw, obj, bundles
        except MenuError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MenuError(f"无法读取桌面配置：{self.manifest_path}。请在设置中确认数据目录。") from exc

    def discover(self, obj, bundles):
        modules = self.profile / "node_modules"
        candidates = list(bundles)
        for key in ("dependencies", "optionalDependencies", "devDependencies"):
            deps = obj.get(key, {})
            if isinstance(deps, dict):
                candidates.extend(deps)
        # Directly installed bundle packages remain discoverable when disabled.
        if modules.is_dir():
            for child in modules.iterdir():
                if child.name.startswith("."):
                    continue
                if child.name.startswith("@") and child.is_dir():
                    candidates.extend(f"{child.name}/{sub.name}" for sub in child.iterdir() if sub.is_dir())
                elif child.is_dir():
                    candidates.append(child.name)
        result = {}
        for name in dict.fromkeys(candidates):
            if not NAME.fullmatch(name) or name in OFFICIAL:
                continue
            path = modules.joinpath(*name.split("/"))
            try:
                meta = read_json(path / "package.json", {})
                if not isinstance(meta, dict):
                    raise ValueError("invalid manifest")
                dsh = meta.get("dsh", {})
                is_bundle = isinstance(dsh, dict) and "bundle" in dsh
                if name not in bundles and not is_bundle:
                    continue
                if meta.get("name") not in (None, name):
                    raise ValueError("package name mismatch")
                result[name] = {"id": name, "version": str(meta.get("version", "未知")),
                                "description": str(meta.get("description", "此插件未提供描述。")),
                                "path": str(path.resolve()), "available": bool(meta),
                                "enabled": name in bundles, "error": "" if meta else "插件文件缺失"}
            except (OSError, ValueError, TypeError) as exc:
                if name in bundles:
                    result[name] = {"id": name, "version": "未知", "description": "无法读取插件信息",
                                    "path": str(path), "available": False, "enabled": True,
                                    "error": str(exc)}
        # Keep the established bundle order, including plugins temporarily turned off.
        order = list(dict.fromkeys(self.settings.get("order", []) + bundles + sorted(result)))
        order = [x for x in order if x in result]
        return [result[x] for x in order]

    def backup_list(self):
        folder = self.data / "backups"
        records = []
        for path in sorted(folder.glob("*.json"), reverse=True):
            try:
                info = read_json(path)
                if (not isinstance(info, dict) or type(info.get('format')) is not int or info['format'] != 3
                        or info.get("target") != str(self.manifest_path)
                        or not re.fullmatch(r"[0-9-]+[a-f0-9]{6}\.json", path.name)
                        or not isinstance(info.get('time'), str) or not isinstance(info.get('reason'), str)
                        or not isinstance(info.get('plugins'), list)
                        or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in info['plugins'])):
                    continue
                records.append({"id": path.name, "time": info["time"],
                                "reason": info["reason"], "plugins": info["plugins"]})
                if len(records) == 100:
                    break
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return records

    def state(self):
        with self.lock:
            raw, obj, bundles = self.manifest()
            plugins = self.discover(obj, bundles)
            marker = PURGE.installed_marker(self.home)
            applied = marker.is_file() and marker.read_text(encoding="utf-8").strip().startswith("apply:")
            tracking = self.tracking.summary()
            tracked = set(tracking['records'])
            patch_mode = self.tracking.core.summary([p for p in plugins if p['id'] not in tracked])
            patch_mode['managed'] = list(dict.fromkeys(patch_mode['managed'] + list(tracked)))
            patch_mode['supported'] = patch_mode['supported'] or bool(tracked)
            patch_mode['verifiedNative'] = (patch_mode['verifiedNative'] and not tracking['pending']
                                            and not tracking['session'] and not any(r['enabled'] for r in tracking['records'].values()))
            patch_mode['pending'] = patch_mode['pending'] or tracking['pending']
            for plugin in plugins:
                plugin['tracking'] = ('builtin' if is_adapter(plugin['id']) else 'auto' if plugin['id'] in tracked else 'untracked')
            exists = Path(self.settings["executable"]).is_file()
            detected = None if exists else detect_executable()
            return {"version": VERSION, "plugins": plugins, "enabled": [x for x in bundles if x not in OFFICIAL],
                    "official": list(OFFICIAL), "revision": digest(raw),
                    "running": bool(running_harness()), "home": str(self.home),
                    "profile": str(self.profile), "executable": self.settings["executable"],
                    "executableExists": exists,
                    "detectedExecutable": str(detected) if detected else None,
                    "schemes": self.settings["schemes"], "lastSelection": self.settings["lastSelection"],
                    "schemeLocks": self.settings.get("schemeLocks", {}),
                    "backups": self.backup_list(), "patchApplied": applied and patch_mode['mode'] != 'native',
                    "patchMode": patch_mode,
                    "tracking": tracking,
                    "data": str(self.data)}

    def validate_selection(self, selection, plugins):
        if not isinstance(selection, list) or any(not isinstance(x, str) for x in selection):
            raise MenuError("插件选择格式错误。")
        if len(selection) != len(set(selection)):
            raise MenuError("插件选择包含重复项。")
        valid = {p["id"] for p in plugins if p["available"]}
        unknown = set(selection) - valid
        if unknown:
            raise MenuError("以下插件缺失或不可用，请刷新后重选：" + "、".join(sorted(unknown)))

    def backup(self, raw, bundles, reason):
        stamp = datetime.now().astimezone()
        key = stamp.strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:6] + ".json"
        selected = [x for x in bundles if x not in OFFICIAL]
        plugins = self.discover(json.loads(raw.decode('utf-8-sig')), bundles)
        lock = self.tracking.versions.lock(selected, plugins, self.tracking.load(),
                                          file_hash(Path(self.settings['executable'])))
        write_json(self.data / "backups" / key, {"target": str(self.manifest_path),
                   "format": 3,
                   "time": stamp.isoformat(timespec="seconds"), "reason": reason,
                   "plugins": selected, "lock": lock,
                   "content": raw.decode("utf-8-sig"), "bom": raw.startswith(b"\xef\xbb\xbf")})
        return key

    def require_stopped(self):
        if running_harness():
            raise MenuError("Harness 正在运行。请先在托盘中完全退出，再应用或启动；不会强制结束你的会话。",
                            code='harness_running', recovery='在托盘中退出 Harness 后重新操作。', retryable=True)

    def is_running(self):
        return bool(running_harness())

    def require_executable(self):
        # Patch switching and recording locate the install folder from this path.
        if not Path(self.settings["executable"]).is_file():
            raise MenuError("找不到 Harness 程序。补丁切换和安装记录需要准确的安装目录，请先在设置中修正应用路径。",
                            code='missing_executable', recovery='打开设置并选择当前的 DeepSeek Harness.exe。')

    def validate_lock(self, lock, selection, plugins):
        if not isinstance(lock, dict):
            raise MenuError('方案或备份缺少新版版本记录，请先运行 migrate.py。')
        expected = self.tracking.versions.lock(selection, plugins, self.tracking.load(),
                                             file_hash(Path(self.settings['executable'])))
        if lock != expected:
            raise MenuError('此方案或备份记录的插件版本、应用顺序或 Harness 版本已变化。'
                            '请检查后重新保存方案；历史配置不能套用到不同的插件版本。')

    def switch_layers(self, selection, plugins, before, after, native):
        tracker = self.tracking
        report_progress('check', '检查安装记录与配置')
        tracker.guard(check_files=True)
        registered = set(tracker.load()['records'])
        unknown = [p['id'] for p in plugins if not is_adapter(p['id']) and p['id'] not in registered]
        if unknown:
            raise PatchError('这些插件缺少安装前记录，无法可靠切换文件组合：' + '、'.join(unknown) +
                             '。请先恢复干净的安装，再通过启动器安装并自动记录；不能用安装后的快照补记。')
        report_progress('packages', '校验插件版本与依赖文件')
        tracker.check_versions(plugins, selection)
        planner = SwitchPlan(tracker)
        plan = planner.prepare(selection, plugins, before, after, native)
        return planner.execute(plan)

    def rollback_layers(self, receipt):
        SwitchPlan(self.tracking).rollback(receipt)

    def apply(self, selection, revision, launch=False, native=False, scheme=None):
        with self.lock, self.verification_cache.session():
            report_progress('check', '检查运行状态与插件选择')
            self.require_stopped()
            try:
                self.tracking.guard()
            except PatchError as exc:
                raise MenuError(str(exc), code=exc.code, details=exc.details, recovery=exc.recovery, retryable=exc.retryable) from exc
            raw, obj, bundles = self.manifest()
            if revision != digest(raw):
                raise MenuError("配置已被 Harness 或其他程序修改，请刷新后重试。", code='revision_conflict',
                                recovery='刷新实际配置，重新确认插件选择后提交。', retryable=True)
            plugins = self.discover(obj, bundles)
            self.validate_selection(selection, plugins)
            self.require_executable()
            try:
                self.tracking.check_host_identity(selection)
            except PatchError as exc:
                raise MenuError(str(exc), code=exc.code, details=exc.details,
                                recovery=exc.recovery, retryable=exc.retryable) from exc
            if scheme is not None:
                if not isinstance(scheme, str) or scheme not in self.settings['schemes']:
                    raise MenuError('启动方案不存在，请刷新后重试。')
                if set(self.settings['schemes'][scheme]) != set(selection):
                    raise MenuError('所选插件与保存的方案不同，请刷新后重选。')
                self.validate_lock(self.settings.get('schemeLocks', {}).get(scheme),
                                   self.settings['schemes'][scheme], plugins)
            exe = Path(self.settings["executable"])
            # Always keep official components; preserve all other JSON fields.
            chosen = set(selection)
            ordered = [x for x in bundles if x in OFFICIAL or x in chosen]
            ordered.extend(x for x in selection if x not in ordered)
            obj["dsh"]["profile"]["bundles"] = ordered
            changed = ordered != bundles
            controller = self.tracking.core
            has_layer = controller.record.is_file() or any(is_adapter(p['id']) for p in plugins)
            # An already sealed combination can be launched without undoing and
            # replaying every layer. Revalidate live file identities each time.
            try:
                current_verified = (launch and not changed and
                                    SwitchPlan(self.tracking).verify_current(selection, plugins, raw))
            except PatchError as exc:
                raise MenuError(str(exc), code=exc.code, details=exc.details,
                                recovery=exc.recovery, retryable=exc.retryable) from exc
            if not current_verified and (changed or has_layer):
                self.backup(raw, bundles, "启动前切换" if launch else "保存插件选择")
                # Recheck after backup, before the single atomic config replacement.
                self.require_stopped()
                if digest(self.manifest_path.read_bytes()) != revision:
                    raise MenuError("备份期间配置发生变化，请刷新后重试。")
            payload = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode('utf-8') if changed else raw
            receipt = None
            try:
                if not current_verified:
                    receipt = self.switch_layers(selection, plugins, raw, payload, native)
                if launch:
                    report_progress('launch', '正在发送 Harness 启动请求')
                    self.require_stopped()
                    # An updater can finish after the preflight/commit. Do not
                    # launch a new host with a combination sealed for the old one.
                    self.tracking.check_host_identity(selection)
                    env = os.environ.copy()
                    env["DSH_HOME"] = str(self.home)
                    subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                                     creationflags=CREATE_NO_WINDOW,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except PatchError as exc:
                raise MenuError(str(exc), code=exc.code, details=exc.details, recovery=exc.recovery, retryable=exc.retryable) from exc
            except OSError as exc:
                if receipt:
                    try:
                        self.rollback_layers(receipt)
                    except Exception as rollback_error:
                        raise MenuError('启动失败且补丁回滚未完成：' + str(rollback_error)) from exc
                raise MenuError(("启动失败，已恢复切换前配置：" if receipt else "无法完成切换，已停止操作：") + str(exc)) from exc
            result = {"message": ("启动请求已发送" if launch else "插件配置已保存") +
                      (f" · 已选择 {len(selection)} 个额外插件" if selection else " · 不加载额外插件"),
                      "changed": changed, "launched": launch,
                      "patchMode": receipt['mode'] if receipt else ('plugin' if selection else 'native'),
                      "verifiedCurrent": bool(current_verified)}
            candidate = copy.deepcopy(self.settings)
            candidate["order"] = [p["id"] for p in plugins]
            candidate["lastSelection"] = list(selection)
            try:
                self.persist(candidate)
            except OSError:
                # The file transaction is committed and a launch may already
                # have been sent. An ancillary preference failure must not
                # report the completed mutation as failed or trigger rollback.
                warning = {'code': 'preferences_save_failed',
                           'message': '操作已完成，但启动偏好保存失败。',
                           'recovery': '本次配置已生效，无需重复启动或应用。请检查磁盘空间和数据目录权限。'}
                result['warning'] = warning
                result['message'] += ' · ' + warning['message'] + warning['recovery']
                logging.warning('Preferences could not be saved after a completed operation', exc_info=True)
            return result

    def restore(self, backup_id, revision, native=False):
        with self.lock:
            self.require_stopped()
            if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9-]+[a-f0-9]{6}\.json", backup_id):
                raise MenuError("无效的备份编号。")
            raw, _, bundles = self.manifest()
            if revision != digest(raw):
                raise MenuError("配置已改变，请刷新后再恢复。")
            info = read_json(self.data / "backups" / backup_id)
            if not isinstance(info, dict) or info.get("target") != str(self.manifest_path):
                raise MenuError("此备份不属于当前桌面配置。")
            if info.get('format') != 3:
                raise MenuError('备份需要先通过 migrate.py 转为新版格式。')
            try:
                restored = json.loads(info["content"])
                restored_bundles = restored["dsh"]["profile"]["bundles"]
                if not isinstance(restored_bundles, list) or not all(isinstance(x, str) and NAME.fullmatch(x) for x in restored_bundles):
                    raise ValueError()
                if not all(x in restored_bundles for x in OFFICIAL):
                    raise ValueError()
                if len(restored_bundles) != len(set(restored_bundles)):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise MenuError("备份内容无效，未作修改。")
            self.require_executable()
            self.backup(raw, bundles, "恢复备份前")
            self.require_stopped()
            if digest(self.manifest_path.read_bytes()) != revision:
                raise MenuError("配置已改变，请刷新后再恢复。")
            payload = info["content"].encode("utf-8")
            payload = (b"\xef\xbb\xbf" if info.get("bom") else b"") + payload
            selected = [x for x in restored_bundles if x not in OFFICIAL]
            _, current_obj, current_bundles = self.manifest()
            plugins = self.discover(current_obj, current_bundles)
            self.validate_selection(selected, plugins)
            self.validate_lock(info.get('lock'), selected, plugins)
            try:
                receipt = self.switch_layers(selected, plugins, raw, payload, native and not selected)
            except PatchError as exc:
                raise MenuError(str(exc), code=exc.code, details=exc.details, recovery=exc.recovery, retryable=exc.retryable) from exc
            if receipt is None:
                atomic_write(self.manifest_path, payload)
            return {"message": "桌面插件配置已恢复。"}

    def scheme(self, name, selection=None, delete=False):
        with self.lock:
            if type(delete) is not bool:
                raise MenuError("删除标志必须是布尔值。", code='invalid_request')
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
                raise MenuError("方案名称需为 1–40 个字符。")
            name = name.strip()
            candidate = copy.deepcopy(self.settings)
            if delete:
                candidate["schemes"].pop(name, None)
                candidate['schemeLocks'].pop(name, None)
            else:
                self.require_executable()
                if len(self.settings["schemes"]) >= 100 and name not in self.settings["schemes"]:
                    raise MenuError("最多保存 100 个方案。")
                _, obj, bundles = self.manifest()
                plugins = self.discover(obj, bundles)
                self.validate_selection(selection, plugins)
                lock = self.tracking.versions.lock(
                    selection, plugins, self.tracking.load(), file_hash(Path(self.settings['executable'])))
                candidate["schemes"][name] = list(selection)
                candidate['schemeLocks'][name] = lock
            self.persist(candidate)
            return {"message": "方案已删除" if delete else "方案已保存"}

    def configure(self, home, executable):
        with self.lock:
            try:
                self.tracking.guard()
            except PatchError as exc:
                raise MenuError(str(exc), code=exc.code, details=exc.details, recovery=exc.recovery, retryable=exc.retryable) from exc
            if not isinstance(home, str) or not isinstance(executable, str):
                raise MenuError("路径格式错误。")
            candidate = Path(home).expanduser().resolve()
            exe = Path(executable).expanduser().resolve()
            if not (candidate / "profiles" / "desktop" / "package.json").is_file():
                raise MenuError("数据目录应包含 profiles\\desktop\\package.json。")
            if not exe.is_file() or exe.name.lower() != "deepseek harness.exe":
                raise MenuError("请选择 DeepSeek Harness.exe。")
            old = dict(self.settings)
            try:
                self.settings.update(home=str(candidate), executable=str(exe))
                self.manifest()
                if old["home"] != str(candidate):
                    self.settings.update(schemes={}, schemeLocks={}, order=[], lastSelection=None)
                self.persist()
            except Exception:
                self.settings = old
                raise
            return {"message": "路径设置已保存。"}

    def import_schemes(self, schemes):
        """Validate an entire import before writing anything."""
        with self.lock:
            self.require_executable()
            if not isinstance(schemes, dict) or len(schemes) > 100:
                raise MenuError("方案文件格式错误或超过 100 个方案。")
            _, obj, bundles = self.manifest()
            plugins = self.discover(obj, bundles)
            normalized = {}
            for name, selection in schemes.items():
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
                    raise MenuError("方案名称需为 1–40 个字符。")
                if name.strip() in normalized:
                    raise MenuError("方案文件包含重复名称。")
                self.validate_selection(selection, plugins)
                normalized[name.strip()] = list(selection)
            merged = {**self.settings["schemes"], **normalized}
            if len(merged) > 100:
                raise MenuError("导入后将超过 100 个方案，请先移除不用的方案。")
            locks = {name: self.tracking.versions.lock(ids, plugins, self.tracking.load(),
                     file_hash(Path(self.settings['executable']))) for name, ids in normalized.items()}
            candidate = copy.deepcopy(self.settings)
            candidate["schemes"] = merged
            for name in normalized:
                candidate['schemeLocks'][name] = locks[name]
            self.persist(candidate)
            return {"message": f"已导入 {len(normalized)} 个方案"}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    catalog = Catalog()

    def __init__(self, *args, **kwargs):
        self.mutation_lock = threading.Lock()
        self.operations = OperationLog()
        self.stopping = threading.Event()
        super().__init__(*args, **kwargs)

    def request_exit(self):
        # Called with mutation_lock held: no new HTTP mutation can race shutdown.
        # The supervisor also uses manager.lock and may have a worker in progress.
        if not self.manager.lock.acquire(blocking=False):
            raise MenuError('正在处理文件，请等待完成后再退出启动器后台。')
        try:
            if self.manager.tracking.active():
                raise MenuError('安装记录仍在进行，请等待自动保存或处理异常后再退出启动器后台。')
            self.stopping.set()
        finally:
            self.manager.lock.release()

    def server_bind(self):
        # SO_REUSEADDR permits two live listeners on the same Windows port.
        # Exclusive ownership also prevents two menus from changing one profile.
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    body_limit = 65536
    body_timeout = 2.0

    def log_message(self, fmt, *args):
        # Tokens and URLs never enter logs.
        pass

    def reply(self, code, body, mime="application/json; charset=utf-8"):
        if isinstance(body, dict) and 'error' in body and 'code' not in body:
            codes = {400:'invalid_request', 403:'forbidden', 404:'not_found', 408:'request_timeout',
                     409:'operation_busy', 413:'request_too_large', 415:'unsupported_media', 503:'service_stopping'}
            body = error_payload(MenuError(body['error'], code=codes.get(code, 'request_failed'), retryable=code in (408,409,503)))
        if isinstance(body, dict) and 'error' in body and getattr(self, 'operation_id', None):
            body['operationId'] = self.operation_id
            if getattr(self, 'operation_error', None) is None:
                self.operation_error = MenuError(body['error'], code=body['code'],
                                                details=body['details'], recovery=body['recovery'], retryable=body['retryable'])
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if getattr(self, 'defer_response', False):
            # Publish the result only after operation bookkeeping and lock
            # release. Clients may issue their next mutation immediately.
            self.pending_response = (code, body, mime)
            return True
        return self.send_reply(code, body, mime)

    def send_reply(self, code, body, mime):
        # The application outcome is already determined. A tab closing during
        # response delivery must not relabel a committed mutation as failed or
        # trigger a second HTTP response on a partially written connection.
        try:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:
            self.close_connection = True
            logging.info('HTTP response delivery ended: operation=%s status=%s cause=%s',
                         getattr(self, 'operation_id', None), code, type(exc).__name__)
            return False
        return True

    def authorized(self, document=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            return False
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if document:
            supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        if not secrets.compare_digest(supplied.encode('utf-8'), self.server.token.encode('utf-8')):
            return False
        origin = self.headers.get("Origin")
        return origin in (None, "http://" + expected)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/style.css", "/state.js", "/api.js", "/runtime.js", "/app.js"):
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                return self.reply(403, {"error": "Forbidden"})
            return self.reply(200, (ROOT / "ui" / path[1:]).read_bytes(),
                              "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8")
        if not self.authorized(document=path == "/"):
            return self.reply(403, {"error": "请从桌面快捷方式打开启动菜单。"})
        if self.server.stopping.is_set():
            return self.reply(503, {"error": "启动器后台正在退出，请重新打开启动菜单。"})
        try:
            if path == "/":
                return self.reply(200, (ROOT / "ui" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self.reply(200, self.server.manager.state())
            if path == '/api/operations':
                return self.reply(200, self.server.operations.snapshot())
            if path == '/api/storage':
                return self.reply(200, self.server.manager.tracking.store.statistics())
            if path == '/api/settings':
                manager = self.server.manager
                return self.reply(200, {'home': str(manager.home), 'executable': manager.settings['executable'],
                                        'detected': str(detect_executable() or '')})
            if path == "/api/ping":
                return self.reply(200, {"running": bool(running_harness())})
            if path == '/api/tracking':
                return self.reply(200, self.server.manager.tracking.summary())
            if path == '/api/tracking/diff':
                return self.reply(200, self.server.manager.tracking.export_changes())
            if path == '/api/catalog/search':
                query = parse_qs(urlparse(self.path).query)
                try:
                    page = int(query.get('page', ['0'])[0])
                except ValueError as exc:
                    raise CatalogError('页码无效。') from exc
                return self.reply(200, self.server.catalog.search(query.get('q', [''])[0],
                    query.get('source', ['all'])[0], page, query.get('registry', ['npm'])[0]))
            if path == '/api/catalog/detail':
                query = parse_qs(urlparse(self.path).query)
                return self.reply(200, self.server.catalog.detail(query.get('name', [''])[0],
                    query.get('provider', ['npm'])[0], query.get('registry', ['npm'])[0]))
            if path == '/api/catalog/leaderboard':
                query = parse_qs(urlparse(self.path).query)
                return self.reply(200, self.server.catalog.leaderboard(query.get('board', ['downloads'])[0]))
            self.reply(404, {"error": "Not found"})
        except Exception as exc:
            self.error(exc)

    def error(self, exc):
        expected = isinstance(exc, (LauncherError, CatalogError))
        if not expected:
            logging.exception("Launcher operation failed")
        public = exc if expected else MenuError('操作失败，详情已记录在 launcher.log。', code='internal_error')
        self.operation_error = public
        self.reply(400 if expected else 500, error_payload(public, getattr(self, 'operation_id', None)))

    def do_POST(self):
        self.operation_id = None
        self.operation_error = None
        # Consume ordinary request bodies before responding or taking the global
        # mutation lock. Closing a Windows socket with unread request bytes can
        # reset the connection before the client receives the error response.
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.reply(400, {'error': '请求格式错误。'})
        if size < 0 or size > self.body_limit:
            return self.reply(413, {'error': '请求过大'})
        try:
            body = self.read_request_body(size)
        except TimeoutError:
            return self.reply(408, {'error': '读取请求超时，请重试。'})
        except MenuError as exc:
            return self.reply(400, {'error': str(exc)})
        if not self.authorized():
            return self.reply(403, {"error": "Forbidden"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.reply(415, {"error": "JSON required"})
        if not self.server.mutation_lock.acquire(blocking=False):
            return self.reply(409, {"error": "另一项操作正在进行，请等待完成后重试。"})
        self.defer_response = True
        self.pending_response = None
        try:
            if self.server.stopping.is_set():
                return self.reply(503, {"error": "启动器后台正在退出，请重新打开启动菜单。"})
            self.operation_error = None
            try:
                self.operation_id = self.server.operations.begin(urlparse(self.path).path.removeprefix('/api/'))
            except LauncherError as exc:
                return self.error(exc)
            with self.server.operations.reporting(self.operation_id):
                report_progress('check', '等待文件操作完成并检查配置')
                self.post(body)
        finally:
            try:
                try:
                    if getattr(self, 'operation_id', None):
                        self.server.operations.finish(self.operation_id, self.operation_error)
                finally:
                    # Bookkeeping failure must never strand the request lock.
                    self.server.mutation_lock.release()
                if self.pending_response is not None:
                    self.send_reply(*self.pending_response)
            finally:
                self.defer_response = False
                if self.server.stopping.is_set():
                    # Stop even if the requesting tab closes before receiving the reply.
                    threading.Thread(target=self.server.shutdown, daemon=True).start()

    def read_request_body(self, size):
        deadline = time.perf_counter() + self.body_timeout
        previous_timeout = self.connection.gettimeout()
        chunks = []
        remaining = size
        try:
            while remaining:
                available = deadline - time.perf_counter()
                if available <= 0:
                    raise TimeoutError()
                self.connection.settimeout(available)
                # read1 performs at most one socket read, letting the deadline
                # cap the entire body even when a client trickles in bytes.
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    raise MenuError('请求体不完整，请重试。')
                chunks.append(chunk)
                remaining -= len(chunk)
            return b''.join(chunks)
        finally:
            self.connection.settimeout(previous_timeout)

    def post(self, raw):
        response_code = 200
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise MenuError("请求格式错误。")
            path = urlparse(self.path).path
            manager = self.server.manager
            if path == '/api/quit':
                self.server.request_exit()
                result = {'message': '启动器后台已退出。下次使用请重新打开启动菜单。'}
            elif path in ("/api/apply", "/api/launch"):
                result = manager.apply(body.get("selection"), body.get("revision"), launch=path.endswith("launch"),
                                       native=body.get('native', False) is True, scheme=body.get('scheme'))
            elif path == "/api/scheme":
                result = manager.scheme(body.get("name"), body.get("selection"), body.get("delete", False))
            elif path == "/api/restore":
                result = manager.restore(body.get("id"), body.get("revision"), native=True)
            elif path == "/api/settings":
                result = manager.configure(body.get("home"), body.get("executable"))
            elif path == "/api/import":
                result = manager.import_schemes(body.get("schemes"))
            elif path.startswith('/api/tracking/'):
                action = path.rsplit('/', 1)[-1]
                tracker = manager.tracking
                try:
                    if action == 'begin':
                        result = tracker.begin(body.get('name'), body.get('source', ''))
                    elif action == 'abandon':
                        result = tracker.abandon()
                    elif action == 'retry':
                        result = tracker.retry()
                    else:
                        raise MenuError('未知记录操作。')
                except PatchError as exc:
                    raise MenuError(str(exc), code=exc.code, details=exc.details, recovery=exc.recovery, retryable=exc.retryable) from exc
            elif path == "/api/open":
                folders = {"profile": manager.profile, "backups": manager.data / "backups", "source": ROOT}
                target = folders.get(body.get("target"))
                if target is None:
                    raise MenuError("未知目录。")
                target.mkdir(parents=True, exist_ok=True)
                os.startfile(str(target))
                result = {"message": "已打开目录。"}
            else:
                response_code, result = 404, {"error": "Not found"}
        except (ValueError, UnicodeDecodeError) as exc:
            return self.error(MenuError('请求格式错误。', code='invalid_request'))
        except Exception as exc:
            return self.error(exc)
        # Keep response serialization/delivery outside the business exception
        # boundary. None of its failures can undo an already returned result.
        return self.reply(response_code, dict(result, operationId=getattr(self, 'operation_id', None)))


def open_window(url):
    candidates = [Path(os.environ.get(key, "C:\\")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
                  for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    edge = next((path for path in candidates if path.is_file()), None)
    if edge:
        subprocess.Popen([str(edge), "--app=" + url, "--window-size=1200,860"],
                         creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


@contextmanager
def instance_lock(data):
    """Hold one OS lock per data directory for the server's complete lifetime."""
    # Keep the file after unlock: unlinking it could let another process lock a
    # new file while the previous inode is still owned by a running service.
    with (Path(data) / 'instance.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        owned = False
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                owned = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
            yield owned
        finally:
            if owned:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main():
    if sys.version_info < (3, 12):
        raise MenuError('需要 Python 3.12 或更高版本，请使用 Harness 自带的 Python 或 launch.vbs 启动。',
                        code='unsupported_python')
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--data", type=Path, default=DATA)
    args = parser.parse_args()
    if args.data.with_name(args.data.name + '.upgrade.json').exists():
        raise MenuError('目录迁移尚未完成，请先运行 migrate.py --recover。')
    args.data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.data / "launcher.log", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    if any((args.data / name).exists() for name in ('import-pending.json', 'migration-pending.json')):
        raise MenuError('数据迁移尚未结束，请先运行 migrate.py --apply。')
    with instance_lock(args.data) as owned:
        serve(args, owned)


def serve(args, owned):
    if args.data.with_name(args.data.name + '.upgrade.json').exists():
        raise MenuError('目录迁移尚未完成，请先运行 migrate.py --recover。')
    # Fixed loopback port makes repeated shortcuts reopen the same local menu.
    port = 17643
    instance_path = args.data / "instance.json"
    def existing_instance():
        import urllib.request
        try:
            instance = read_json(instance_path, {})
            url = instance.get('url', '')
            parsed = urlparse(url)
            if parsed.hostname != '127.0.0.1' or not parsed.port:
                return None
            token = parse_qs(parsed.query)['token'][0]
            request = urllib.request.Request(f'http://127.0.0.1:{parsed.port}/api/ping',
                                             headers={'Authorization': 'Bearer ' + token})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1.5) as response:
                result = json.load(response)
                if response.status == 200 and isinstance(result.get('running'), bool):
                    return url
        except Exception:
            return None
        return None

    existing = existing_instance()
    if not owned:
        # Another launcher can own the data before its instance.json is ready,
        # including when the usual port belongs to an unrelated application.
        for _ in range(10):
            if existing:
                break
            time.sleep(0.15)
            existing = existing_instance()
        if not existing:
            raise MenuError('启动器后台正在启动或退出，请稍后重新打开启动菜单。')
    if existing:
        if not args.no_open:
            open_window(existing)
        return
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError:
        # The owner may still be writing instance.json during a simultaneous start.
        for _ in range(5):
            time.sleep(0.15)
            existing = existing_instance()
            if existing:
                break
        if existing:
            if not args.no_open:
                open_window(existing)
            return
        # Use an ephemeral port if another application occupies the default.
        server = Server(("127.0.0.1", 0), Handler)
    monitor_stop = threading.Event()
    url = None
    try:
        server.token = secrets.token_urlsafe(32)
        server.manager = Manager(args.data)
        url = f"http://127.0.0.1:{server.server_port}/?token={server.token}"
        write_json(instance_path, {"url": url, "pid": os.getpid()})

        def supervise():
            while not monitor_stop.wait(1):
                try:
                    server.manager.tracking.tick()
                except Exception:
                    logging.exception('Automatic installation supervisor failed; records retained')

        # Browser timers may stop in sleeping/background windows. Missing heartbeats
        # are not proof that the menu was closed; only an explicit quit stops us.
        threading.Thread(target=supervise, daemon=True).start()
        if not args.no_open:
            open_window(url)
        server.serve_forever(poll_interval=0.5)
    finally:
        monitor_stop.set()
        server.server_close()
        try:
            instance = read_json(instance_path, {})
            if url and isinstance(instance, dict) and instance.get('url') == url:
                instance_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Launcher startup failed")
        message = str(exc) if isinstance(exc, LauncherError) else '启动失败，请查看数据目录中的 launcher.log。'
        if os.name == 'nt' and '--no-open' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, message, 'Harness Launcher', 0x10)
        elif sys.stderr is not None:
            print(message, file=sys.stderr)
        raise SystemExit(1)
