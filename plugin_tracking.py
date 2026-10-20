"""Generic before/after file layers for a user-delimited installation window.

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
import re
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

from patch_modes import PatchError, PatchModes, put, sha


class TrackingError(PatchError):
    pass


PLUGIN_NAME = re.compile(r'^(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*$')


class PluginTracking:
    def __init__(self, manager):
        self.manager = manager
        self.core = PatchModes(manager)
        self.root = self.core.root / 'tracking'
        self.index = self.root / 'registry.json'
        self.busy = False
        self.progress = {'count': 0, 'bytes': 0, 'path': ''}
        self.job_lock = threading.RLock()
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
            return {'format': 1, 'records': {}, 'order': [], 'session': None, 'pending': None}
        try:
            value = json.loads(self.index.read_text(encoding='utf-8'))
            if value['format'] != 1 or not isinstance(value['records'], dict):
                raise ValueError()
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TrackingError('自动记录索引损坏，请保留 tracking 目录。') from exc

    def save(self, value):
        put(self.index, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

    def read_object(self, relative):
        if not re.fullmatch(r'(?:snapshots|deltas)/[a-f0-9]{32}\.json', relative):
            raise TrackingError('记录文件编号无效。')
        return json.loads((self.root / relative).read_text(encoding='utf-8'))

    def object(self, kind, value):
        relative = kind + '/' + uuid.uuid4().hex + '.json'
        put(self.root / relative, json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        return relative

    def blob_bytes(self, key):
        if not re.fullmatch('[a-f0-9]{64}', key or ''):
            raise TrackingError('文件快照编号无效。')
        path = self.root / 'blobs' / key
        data = path.read_bytes()
        if sha(data) != key:
            raise TrackingError('文件快照校验失败，已停止覆盖。')
        return data

    def store_file(self, path, stamp, cached=None):
        if cached and cached.get('stamp') == stamp and (self.root / 'blobs' / cached['hash']).is_file():
            return cached['hash']
        blobs = self.root / 'blobs'
        blobs.mkdir(parents=True, exist_ok=True)
        temp = blobs / (uuid.uuid4().hex + '.tmp')
        h = hashlib.sha256()
        try:
            with path.open('rb') as source, temp.open('wb') as target:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    h.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            now = path.stat()
            if [now.st_size, now.st_mtime_ns, now.st_ctime_ns] != stamp:
                raise TrackingError('快照期间文件仍在变化，请退出相关程序后重试：' + str(path))
            key = h.hexdigest()
            destination = blobs / key
            if not destination.exists():
                os.replace(temp, destination)
            return key
        finally:
            temp.unlink(missing_ok=True)

    def target(self, key):
        try:
            scope, relative = key.split('|', 1)
            base = self.scopes[scope]
            parts = PurePosixPath(relative)
            if parts.is_absolute() or '..' in parts.parts or '\\' in relative or ':' in relative:
                raise ValueError()
            path = base if relative == '.' else base.joinpath(*parts.parts)
            # Physical files only: never follow a plugin-created junction to disk outside scope.
            cursor = path
            while cursor != base.parent:
                if cursor.is_symlink() or cursor.is_junction():
                    raise TrackingError('不能自动覆盖链接或其目标：' + str(cursor))
                if cursor == cursor.parent:
                    raise ValueError()
                cursor = cursor.parent
            if not path.resolve().is_relative_to(base.parent.resolve()):
                raise ValueError()
            return path
        except (ValueError, KeyError) as exc:
            raise TrackingError('变更记录包含托管范围以外的路径。') from exc

    @staticmethod
    def equal(left, right):
        if left is None or right is None:
            return left is right
        if left['type'] != right['type']:
            return False
        if left['type'] == 'file':
            return left['hash'] == right['hash']
        if left['type'] == 'link':
            return left['target'] == right['target']
        return True

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
                result[key] = {'type': 'file', 'hash': key_hash, 'size': info.st_size,
                               'mtime': info.st_mtime_ns, 'stamp': stamp}
                self.progress = {'count': self.progress['count'] + 1,
                                 'bytes': self.progress['bytes'] + info.st_size, 'path': key}
            else:
                raise TrackingError('遇到不支持的文件类型：' + str(path))

        for scope, path in self.scopes.items():
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
            if path.exists() or path.is_symlink():
                visit(path, scope + '|.')
        return result

    def seal_inventory(self):
        value = self.load()
        if value['records']:
            value['inventory'] = self.object('snapshots', self.metadata())
        value['inSwitch'] = False
        self.save(value)

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

    def commit_operations(self, operations, new_state):
        old = self.load()
        if old.get('pending'):
            self.recover_pending(old)
            raise TrackingError('上次中断的自动记录切换已回滚，请刷新后重试。')
        # Simulate overlapping layers before changing the first file. A missing
        # base dependency is a conflict, not a failed half-transaction.
        simulated = {}
        for op in operations:
            if op['key'] not in simulated:
                simulated[op['key']] = self.actual(op['key'])
            actual = simulated[op['key']]
            if not self.equal(actual, op['before']) and not self.equal(actual, op['after']):
                raise TrackingError('文件基线不匹配；插件可能依赖记录时的组合或与其他插件冲突：' + op['key'])
            simulated[op['key']] = op['after']
        journal = copy.deepcopy(old)
        transaction = uuid.uuid4().hex
        journal['pending'] = {'id': transaction, 'operations': operations, 'previous': old}
        cursor_file = self.root / 'replay-position.json'
        put(cursor_file, json.dumps({'id': transaction, 'cursor': 0}).encode())
        self.save(journal)
        applied = []
        try:
            for index, op in enumerate(operations):
                self.replay([op])
                applied.append(op)
                put(cursor_file, json.dumps({'id': transaction, 'cursor': index + 1}).encode())
            self.save(new_state)
        except Exception as error:
            try:
                self.replay(applied, undo=True)
                self.save(old)
            except Exception as rollback_error:
                raise TrackingError('自动记录切换未完成，请保留快照和日志：' + str(rollback_error)) from error
            raise TrackingError('自动记录切换失败，已回滚：' + str(error)) from error
        return {'operations': operations, 'previous': old}

    def recover_pending(self, value):
        pending = value['pending']
        cursor = json.loads((self.root / 'replay-position.json').read_text(encoding='utf-8'))
        if cursor.get('id') != pending.get('id'):
            raise TrackingError('回滚位置日志不匹配，请保留快照并人工检查。')
        count = cursor['cursor']
        operations = pending['operations']
        prefix = operations[:count]
        if count < len(operations):
            candidate = operations[count]
            actual = self.actual(candidate['key'])
            if self.equal(actual, candidate['after']) and not self.equal(actual, candidate['before']):
                prefix.append(candidate)
            elif not self.equal(actual, candidate['before']):
                raise TrackingError('中断后文件又发生改动，无法自动回滚：' + candidate['key'])
        self.replay(prefix, undo=True)
        self.save(pending['previous'])

    def rollback(self, receipt):
        if receipt:
            self.replay(receipt['operations'], undo=True)
            self.save(receipt['previous'])

    def guard(self, check_files=False):
        value = self.load()
        if self.busy or value.get('session'):
            raise TrackingError('正在记录插件安装/首次运行，请先完成或放弃记录，再切换插件。')
        if value.get('pending'):
            self.recover_pending(value)
            raise TrackingError('上次中断的文件切换已回滚，请刷新后重试。')
        if check_files and value.get('inventory') and not value.get('inSwitch'):
            expected = self.read_object(value['inventory'])
            actual = self.metadata()
            changed = [key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key)]
            if changed:
                raise TrackingError('记录窗口外发生了文件变化，无法自动归属，已停止覆盖：' + '、'.join(sorted(changed)[:4]))

    def check_versions(self, plugins, selection):
        records = self.load()['records']
        available = {p['id']: p['version'] for p in plugins}
        for plugin in selection:
            if plugin in records and available.get(plugin) != records[plugin]['version']:
                raise TrackingError('插件版本已变化，需在更新前重新记录：' + plugin)

    def suspend(self):
        value = self.load()
        operations = []
        new = copy.deepcopy(value)
        for plugin in reversed(value['order']):
            record = value['records'][plugin]
            if record['enabled']:
                changes = self.read_object(record['delta'])['changes']
                operations.extend(self.operations(changes, enable=False))
                new['records'][plugin]['enabled'] = False
        if not operations and value == new:
            return None
        return self.commit_operations(operations, new)

    def activate(self, selection):
        value = self.load()
        operations = []
        new = copy.deepcopy(value)
        for plugin in value['order']:
            record = value['records'][plugin]
            if plugin in selection:
                changes = self.read_object(record['delta'])['changes']
                operations.extend(self.operations(changes))
                new['records'][plugin]['enabled'] = True
        if not self.scopes['app'].exists() and any(op['key'].startswith('app|') for op in operations):
            if not any(op['key'] == 'app|.' and op['after'] == {'type': 'dir'} for op in operations):
                raise TrackingError('该插件记录依赖已有的解包应用目录。请选择记录时的基础组合，或在原生基线上重新记录。')
        if not operations and value == new:
            return None
        return self.commit_operations(operations, new)

    def summary(self):
        value = self.load()
        session = value['session']
        review = []
        if session and session.get('delta'):
            review = [{'path': x['key'], 'change': '新增' if x['before'] is None else '删除' if x['after'] is None else '修改'}
                      for x in self.read_object(session['delta'])['changes'][:200]]
        return {'busy': self.busy, 'progress': dict(self.progress), 'session': session,
                'review': review, 'pending': bool(value.get('pending') or value.get('inSwitch')),
                'records': {name: {**{k: record[k] for k in ('version', 'enabled', 'count', 'baseSelection')},
                                   'historyCount': len(record.get('history', []))}
                            for name, record in value['records'].items()},
                'scopes': [str(path) for path in self.scopes.values()],
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
                value = self.load()
                if value.get('session'):
                    value['session']['error'] = str(exc)
                    self.save(value)
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def begin(self, name, source=''):
        with self.manager.lock:
            self.manager.require_stopped()
            self.guard(check_files=True)
            if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name) or name == 'dsh-purge':
                raise TrackingError('请输入有效的新插件包名。dsh-purge 使用已有的内置还原机制。')
            if not isinstance(source, str) or len(source) > 2048 or source.startswith('-') or '\n' in source or '\r' in source:
                raise TrackingError('安装来源格式无效。')
            if urlparse(source).username or urlparse(source).password:
                raise TrackingError('安装地址不能包含账号密码，请使用已配置的包管理器认证。')
            value = self.load()
            core_state = self.core.load()
            if value.get('inSwitch') or (core_state and core_state.get('pending')):
                raise TrackingError('上次组合切换尚未完成，请先应用当前组合以完成恢复，再开始记录。')
            raw, obj, bundles = self.manager.manifest()
            if name in value['records']:
                # Remove the old layer before taking an upgrade baseline.
                self.manager.apply([x for x in bundles if x not in ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', name)], sha(raw))
                value = self.load()
                raw, obj, bundles = self.manager.manifest()
            plugins = self.manager.discover(obj, bundles)
            if name not in value['records'] and any(p['id'] == name for p in plugins):
                raise TrackingError('此插件已安装且没有安装前记录，无法补回已经发生的改动。请在安装新插件之前开始记录。')
            value['session'] = {'id': uuid.uuid4().hex, 'plugin': name, 'source': source,
                                'phase': 'preparing', 'started': time.time(), 'error': '',
                                'baseSelection': [p for p in bundles if p not in ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app')],
                                'versions': {p['id']: p['version'] for p in plugins}, 'baseline': None}
            self.manager.persist()
            self.save(value)

            def task():
                cache = {}
                cache_path = self.root / 'cache.json'
                if cache_path.is_file():
                    cache = json.loads(cache_path.read_text(encoding='utf-8'))
                baseline = self.snapshot(cache)
                saved = self.load()
                session = saved['session']
                session['baseline'] = self.object('snapshots', baseline)
                session['phase'] = 'installing' if source else 'recording'
                self.save(saved)
                put(cache_path, json.dumps(baseline, ensure_ascii=False).encode('utf-8'))
                if source:
                    self.install(name, source, session['id'])
                saved = self.load()
                saved['session']['phase'] = 'recording'
                self.save(saved)
            self._job(task)
            return {'message': '正在建立安装前快照，请等待完成后再操作插件。'}

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

    def launch_training(self):
        with self.manager.lock:
            self.manager.require_stopped()
            value = self.load()
            session = value.get('session')
            if self.busy or not session or session['phase'] != 'recording':
                raise TrackingError('安装前快照/安装尚未完成，不能启动测试。')
            raw, obj, bundles = self.manager.manifest()
            name = session['plugin']
            if not any(p['id'] == name and p['available'] for p in self.manager.discover(obj, bundles)):
                raise TrackingError('尚未检测到被记录插件，请先完成安装。')
            if name not in bundles:
                obj['dsh']['profile']['bundles'].append(name)
                self.manager.backup(raw, bundles, '记录窗口内启用测试插件')
                put(self.manager.manifest_path, (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode())
            env = os.environ.copy()
            env['DSH_HOME'] = str(self.manager.home)
            exe = Path(self.manager.settings['executable'])
            subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return {'message': '已启动记录窗口中的测试环境。只测试此新插件，退出托盘后再完成扫描。'}

    def finish(self):
        with self.manager.lock:
            self.manager.require_stopped()
            if self.busy:
                raise TrackingError('快照/安装任务尚未完成。')
            value = self.load()
            session = value.get('session')
            if not session or not session.get('baseline'):
                raise TrackingError('尚无完整的安装前快照。')
            _, obj, bundles = self.manager.manifest()
            plugins = self.manager.discover(obj, bundles)
            versions = {p['id']: p['version'] for p in plugins}
            name = session['plugin']
            if name not in versions:
                raise TrackingError('未找到被记录插件，不能确认安装归属。')
            changed_others = [p for p in set(versions) | set(session['versions'])
                              if p != name and session['versions'].get(p) != versions.get(p)]
            if changed_others:
                raise TrackingError('窗口内还有其他插件安装/升级，无法自动归属：' + '、'.join(changed_others))
            others = [p for p in bundles if p not in ('@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', name)]
            if others != session['baseSelection']:
                raise TrackingError('记录窗口内其他插件的加载组合发生变化，请保持原有基础组合后重试。')
            session['phase'] = 'scanning'
            session['error'] = ''
            self.save(value)

            def task():
                before = self.read_object(session['baseline'])
                after = self.snapshot(before)
                changes = self.differences(before, after)
                # Fail before registering a layer if any link/type transition cannot be replayed.
                self.operations(changes)
                saved = self.load()
                saved['session'].update(phase='review', version=versions[name], count=len(changes),
                                        delta=self.object('deltas', {'changes': changes}))
                self.save(saved)
                put(self.root / 'cache.json', json.dumps(after, ensure_ascii=False).encode('utf-8'))
            self._job(task)
            return {'message': '正在对比安装与首次运行后的文件差异。'}

    def accept(self):
        with self.manager.lock:
            self.manager.require_stopped()
            if self.busy:
                raise TrackingError('扫描任务尚未完成。')
            value = self.load()
            session = value.get('session')
            if not session or session['phase'] != 'review':
                raise TrackingError('请先完成差异扫描并查看变更列表。')
            _, obj, bundles = self.manager.manifest()
            versions = {p['id']: p['version'] for p in self.manager.discover(obj, bundles)}
            expected_versions = dict(session['versions'])
            expected_versions[session['plugin']] = session['version']
            if versions != expected_versions:
                raise TrackingError('复核期间插件版本或安装列表再次改变，请重新扫描后保存。')
            # Review may stay open for hours: revalidate every changed file before claiming it.
            changes = self.read_object(session['delta'])['changes']
            for item in changes:
                if not self.equal(self.actual(item['key']), item['after']):
                    raise TrackingError('复核期间文件再次改变，请重新扫描：' + item['key'])
            name = session['plugin']
            previous = value['records'].get(name)
            history = copy.deepcopy(previous.get('history', [])) if previous else []
            if previous:
                history.append({k: copy.deepcopy(v) for k, v in previous.items()
                                if k not in ('history', 'enabled')})
            value['records'][name] = {'version': session['version'], 'delta': session['delta'],
                                      'count': session['count'], 'enabled': True,
                                      'baseSelection': session['baseSelection'], 'history': history}
            # The new recording sits on top of every layer that stayed enabled.
            value['order'] = [p for p in value['order'] if p != name] + [name]
            value['session'] = None
            self.save(value)
            self.seal_inventory()
            return {'message': f'已保存 {name} 的自动记录，共 {len(changes)} 处变更。'}

    def abandon(self):
        with self.manager.lock:
            if self.busy:
                raise TrackingError('请等待当前快照/安装任务结束，不能丢弃运行中的记录。')
            value = self.load()
            value['session'] = None
            self.save(value)
            return {'message': '已放弃记录，当前文件和安装结果原样保留；未记录插件仍不能保证原生还原。'}
