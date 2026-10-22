"""Local, dependency-free plugin launch menu for DeepSeek Harness on Windows."""
from __future__ import annotations

import argparse
import copy
import ctypes
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
from patch_modes import PatchModes, PatchError, file_hash
from plugin_tracking import PluginTracking
from switch_plan import SwitchPlan
from catalog import Catalog, CatalogError

ROOT = Path(__file__).resolve().parent
VERSION = "0.2.0"
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "HarnessLauncher"
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


class MenuError(Exception):
    pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


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
    finally:
        kernel.CloseHandle(handle)
    return found


class Manager:
    def __init__(self, data=DATA, home=None, executable=None):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.settings_file = self.data / "settings.json"
        self.settings = read_json(self.settings_file, {})
        self.settings.setdefault("home", str(home or os.environ.get("DSH_HOME") or Path.home() / ".dsh"))
        self.settings.setdefault("executable", str(executable or default_executable()))
        self.settings.setdefault("schemes", {})
        self.settings.setdefault("schemeLocks", {})
        self.settings.setdefault("order", [])
        self.settings.setdefault("lastSelection", None)
        if home:
            self.settings["home"] = str(home)
        if executable:
            self.settings["executable"] = str(executable)
        self._track_cache = None

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

    def persist(self):
        write_json(self.settings_file, self.settings)

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
        for path in sorted(folder.glob("*.json"), reverse=True)[:100]:
            try:
                info = read_json(path)
                if info.get('format') == 2 and info.get("target") == str(self.manifest_path):
                    records.append({"id": path.name, "time": info["time"],
                                    "reason": info["reason"], "plugins": info["plugins"]})
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return records

    def state(self):
        with self.lock:
            raw, obj, bundles = self.manifest()
            plugins = self.discover(obj, bundles)
            marker = self.home / "dsh-purge" / "installed-rev"
            applied = marker.is_file() and marker.read_text(encoding="utf-8").strip().startswith("apply:")
            tracking = self.tracking.summary()
            tracked = set(tracking['records'])
            patch_mode = PatchModes(self).summary([p for p in plugins if p['id'] not in tracked])
            patch_mode['managed'] = list(dict.fromkeys(patch_mode['managed'] + list(tracked)))
            patch_mode['supported'] = patch_mode['supported'] or bool(tracked)
            patch_mode['verifiedNative'] = (patch_mode['verifiedNative'] and not tracking['pending']
                                            and not tracking['session'] and not any(r['enabled'] for r in tracking['records'].values()))
            patch_mode['pending'] = patch_mode['pending'] or tracking['pending']
            for plugin in plugins:
                plugin['tracking'] = ('builtin' if plugin['id'] == 'dsh-purge' else 'auto' if plugin['id'] in tracked else 'untracked')
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
                   "format": 2,
                   "time": stamp.isoformat(timespec="seconds"), "reason": reason,
                   "plugins": selected, "lock": lock,
                   "content": raw.decode("utf-8-sig"), "bom": raw.startswith(b"\xef\xbb\xbf")})
        return key

    def require_stopped(self):
        if running_harness():
            raise MenuError("Harness 正在运行。请先在托盘中完全退出，再应用或启动；不会强制结束你的会话。")

    def is_running(self):
        return bool(running_harness())

    def require_executable(self):
        # Patch switching and recording locate the install folder from this path.
        if not Path(self.settings["executable"]).is_file():
            raise MenuError("找不到 Harness 程序。补丁切换和安装记录需要准确的安装目录，请先在设置中修正应用路径。")

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
        tracker.guard(check_files=True)
        registered = set(tracker.load()['records'])
        unknown = [p['id'] for p in plugins if p['id'] != 'dsh-purge' and p['id'] not in registered]
        if unknown:
            raise PatchError('这些插件缺少安装前记录，无法可靠切换文件组合：' + '、'.join(unknown) +
                             '。请先恢复干净的安装，再通过启动器安装并自动记录；不能用安装后的快照补记。')
        tracker.check_versions(plugins, selection)
        planner = SwitchPlan(tracker)
        plan = planner.prepare(selection, plugins, before, after, native)
        return planner.execute(plan)

    def rollback_layers(self, receipt):
        SwitchPlan(self.tracking).rollback(receipt)

    def apply(self, selection, revision, launch=False, native=False, scheme=None):
        with self.lock:
            self.require_stopped()
            try:
                self.tracking.guard()
            except PatchError as exc:
                raise MenuError(str(exc)) from exc
            raw, obj, bundles = self.manifest()
            if revision != digest(raw):
                raise MenuError("配置已被 Harness 或其他程序修改，请刷新后重试。")
            plugins = self.discover(obj, bundles)
            self.validate_selection(selection, plugins)
            self.require_executable()
            if scheme is not None:
                if not isinstance(scheme, str) or scheme not in self.settings['schemes']:
                    raise MenuError('启动方案不存在，请刷新后重试。')
                if set(self.settings['schemes'][scheme]) != set(selection):
                    raise MenuError('所选插件与保存的方案不同，请刷新后重选。')
                self.validate_lock(self.settings.get('schemeLocks', {}).get(scheme), selection, plugins)
            exe = Path(self.settings["executable"])
            # Always keep official components; preserve all other JSON fields.
            chosen = set(selection)
            ordered = [x for x in bundles if x in OFFICIAL or x in chosen]
            ordered.extend(x for x in selection if x not in ordered)
            obj["dsh"]["profile"]["bundles"] = ordered
            changed = ordered != bundles
            controller = PatchModes(self)
            has_layer = controller.record.is_file() or any(p['id'] == 'dsh-purge' for p in plugins)
            if changed or has_layer:
                self.backup(raw, bundles, "启动前切换" if launch else "保存插件选择")
                # Recheck after backup, before the single atomic config replacement.
                self.require_stopped()
                if digest(self.manifest_path.read_bytes()) != revision:
                    raise MenuError("备份期间配置发生变化，请刷新后重试。")
            payload = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode('utf-8') if changed else raw
            receipt = None
            try:
                receipt = self.switch_layers(selection, plugins, raw, payload, native)
                if launch:
                    env = os.environ.copy()
                    env["DSH_HOME"] = str(self.home)
                    subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                                     creationflags=CREATE_NO_WINDOW,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except PatchError as exc:
                raise MenuError(str(exc)) from exc
            except OSError as exc:
                if receipt:
                    try:
                        self.rollback_layers(receipt)
                    except Exception as rollback_error:
                        raise MenuError('启动失败且补丁回滚未完成：' + str(rollback_error)) from exc
                raise MenuError(("启动失败，已恢复切换前配置：" if receipt else "无法完成切换，已停止操作：") + str(exc)) from exc
            self.settings["order"] = [p["id"] for p in plugins]
            self.settings["lastSelection"] = list(selection)
            self.persist()
            return {"message": ("启动请求已发送" if launch else "插件配置已保存") +
                    (f" · 已选择 {len(selection)} 个额外插件" if selection else " · 不加载额外插件"),
                    "changed": changed, "launched": launch,
                    "patchMode": receipt['mode'] if receipt else None}

    def restore(self, backup_id, revision, native=False):
        with self.lock:
            self.require_stopped()
            if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9-]+[a-f0-9]{6}\.json", backup_id):
                raise MenuError("无效的备份编号。")
            raw, _, bundles = self.manifest()
            if revision != digest(raw):
                raise MenuError("配置已改变，请刷新后再恢复。")
            info = read_json(self.data / "backups" / backup_id)
            if not info or info.get("target") != str(self.manifest_path):
                raise MenuError("此备份不属于当前桌面配置。")
            if info.get('format') != 2:
                raise MenuError('备份需要先通过 migrate.py 转为新版格式。')
            try:
                restored = json.loads(info["content"])
                restored_bundles = restored["dsh"]["profile"]["bundles"]
                if not isinstance(restored_bundles, list) or not all(isinstance(x, str) and NAME.fullmatch(x) for x in restored_bundles):
                    raise ValueError()
                if not all(x in restored_bundles for x in OFFICIAL):
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
                raise MenuError(str(exc)) from exc
            if receipt is None:
                atomic_write(self.manifest_path, payload)
            return {"message": "桌面插件配置已恢复。"}

    def scheme(self, name, selection=None, delete=False):
        with self.lock:
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
                raise MenuError("方案名称需为 1–40 个字符。")
            name = name.strip()
            if delete:
                self.settings["schemes"].pop(name, None)
                self.settings.setdefault('schemeLocks', {}).pop(name, None)
            else:
                self.require_executable()
                if len(self.settings["schemes"]) >= 100 and name not in self.settings["schemes"]:
                    raise MenuError("最多保存 100 个方案。")
                _, obj, bundles = self.manifest()
                plugins = self.discover(obj, bundles)
                self.validate_selection(selection, plugins)
                self.settings["schemes"][name] = list(selection)
                self.settings['schemeLocks'][name] = self.tracking.versions.lock(
                    selection, plugins, self.tracking.load(), file_hash(Path(self.settings['executable'])))
            self.persist()
            return {"message": "方案已删除" if delete else "方案已保存"}

    def configure(self, home, executable):
        with self.lock:
            try:
                self.tracking.guard()
            except PatchError as exc:
                raise MenuError(str(exc)) from exc
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
            self.settings["schemes"] = merged
            for name in normalized:
                self.settings['schemeLocks'][name] = locks[name]
            self.persist()
            return {"message": f"已导入 {len(normalized)} 个方案"}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    catalog = Catalog()

    def server_bind(self):
        # SO_REUSEADDR permits two live listeners on the same Windows port.
        # Exclusive ownership also prevents two menus from changing one profile.
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Tokens and URLs never enter logs.
        pass

    def reply(self, code, body, mime="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, document=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            return False
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if document:
            supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        if not secrets.compare_digest(supplied, self.server.token):
            return False
        origin = self.headers.get("Origin")
        return origin in (None, "http://" + expected)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/style.css", "/app.js"):
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                return self.reply(403, {"error": "Forbidden"})
            return self.reply(200, (ROOT / "ui" / path[1:]).read_bytes(),
                              "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8")
        if not self.authorized(document=path == "/"):
            return self.reply(403, {"error": "请从桌面快捷方式打开启动菜单。"})
        self.server.last_seen = time.monotonic()
        try:
            if path == "/":
                return self.reply(200, (ROOT / "ui" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self.reply(200, self.server.manager.state())
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
        if not isinstance(exc, (MenuError, CatalogError, PatchError)):
            logging.exception("Launcher operation failed")
        self.reply(400 if isinstance(exc, (MenuError, CatalogError, PatchError)) else 500,
                   {"error": str(exc) if isinstance(exc, (MenuError, CatalogError, PatchError)) else "操作失败，详情已记录在 launcher.log。"})

    def do_POST(self):
        if not self.authorized():
            return self.reply(403, {"error": "Forbidden"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.reply(415, {"error": "JSON required"})
        self.server.last_seen = time.monotonic()
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 65536:
                return self.reply(413, {"error": "请求过大"})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise MenuError("请求格式错误。")
            path = urlparse(self.path).path
            manager = self.server.manager
            if path in ("/api/apply", "/api/launch"):
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
                    raise MenuError(str(exc)) from exc
            elif path == "/api/open":
                folders = {"profile": manager.profile, "backups": manager.data / "backups", "source": ROOT}
                target = folders.get(body.get("target"))
                if target is None:
                    raise MenuError("未知目录。")
                target.mkdir(parents=True, exist_ok=True)
                os.startfile(str(target))
                result = {"message": "已打开目录。"}
            else:
                return self.reply(404, {"error": "Not found"})
            self.reply(200, result)
        except (ValueError, UnicodeDecodeError) as exc:
            self.reply(400, {"error": "请求格式错误。"})
        except Exception as exc:
            self.error(exc)


def open_window(url):
    candidates = [Path(os.environ.get(key, "C:\\")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
                  for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    edge = next((path for path in candidates if path.is_file()), None)
    if edge:
        subprocess.Popen([str(edge), "--app=" + url, "--window-size=1200,860"],
                         creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--data", type=Path, default=DATA)
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.data / "launcher.log", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
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
    server.token = secrets.token_urlsafe(32)
    server.manager = Manager(args.data)
    server.last_seen = time.monotonic()
    url = f"http://127.0.0.1:{server.server_port}/?token={server.token}"
    write_json(instance_path, {"url": url, "pid": os.getpid()})

    monitor_stop = threading.Event()

    def supervise():
        while not monitor_stop.wait(1):
            try:
                server.manager.tracking.tick()
            except Exception:
                logging.exception('Automatic installation supervisor failed; records retained')

    def expire():
        while time.monotonic() - server.last_seen < 300 or server.manager.tracking.active():
            time.sleep(10)
        server.shutdown()

    threading.Thread(target=expire, daemon=True).start()
    threading.Thread(target=supervise, daemon=True).start()
    if not args.no_open:
        open_window(url)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        monitor_stop.set()
        server.server_close()
        try:
            if read_json(instance_path, {}).get("pid") == os.getpid():
                instance_path.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Launcher startup failed")
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, "启动菜单未能打开，请检查 HarnessLauncher\\launcher.log。", "Harness 启动菜单", 0x10)
        else:
            raise
