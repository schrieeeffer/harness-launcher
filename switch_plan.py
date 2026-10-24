"""Plan a complete combination, then apply it with one recoverable journal."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import uuid

from file_view import FileView
from domain import digest, object_reference
from operation_models import OperationError, decode_operation, validate_operations
from recording_state import validate_registry
from operations import report_progress
from patch_modes import PatchError, archive_info, file_hash, put


class SwitchPlan:
    def __init__(self, tracker, hashes=None):
        self.tracker = tracker
        self.manager = tracker.manager
        self.core = tracker.core
        self.journal = tracker.root / 'switch.json'
        self.hashes = hashes

    def file_view(self, roots, hashes=None):
        return FileView(roots, hashes, self.manager.verification_cache)

    def roots(self, *, include_dormant=True):
        roots = list(self.tracker.scopes.values())
        roots.append(self.manager.manifest_path)
        if include_dormant:
            roots += self.core.extra_roots()
        return roots

    def verify_current(self, selection, plugins, manifest):
        """Read-only launch of a sealed, unchanged combination.

        Dormant adapter trees and rollback blobs are not executed on this path.
        They are still fully checked whenever a switch needs them. Active files,
        packages and the host are verified; NTFS identities invalidate cached
        contents even for same-size edits with a restored modification time.
        """
        self.tracker.verify_idle()
        old = self.tracker.load()
        reference = old.get('currentCombination')
        if not reference:
            return False
        combination = self.tracker.read_object(reference)
        if combination.get('manifestHash') != hashlib.sha256(manifest).hexdigest():
            return False
        if combination.get('selection') != selection:
            return False
        if any(record['enabled'] != (name in selection) for name, record in old['records'].items()):
            return False
        core_state = self.core.load()
        if core_state and (core_state['mode'] != ('plugin' if self.core.adapter.plugin in selection else 'native')
                           or combination.get('adapterRevision') != core_state['revision']):
            return False
        registered = set(old['records'])
        if any(p['id'] not in registered and p['id'] != self.core.adapter.plugin for p in plugins):
            return False
        report_progress('packages', '校验已启用插件及依赖')
        host = self.tracker.versions.hash(Path(self.manager.settings['executable']))
        lock = self.tracker.versions.lock(selection, plugins, old, host)
        if any(combination.get(key) != value for key, value in lock.items()):
            raise PatchError('当前组合的版本记录已变化，请刷新并重新应用选择。')
        self.tracker.check_versions(plugins, selection)
        if self.core.adapter.plugin in selection:
            if not core_state:
                return False
            self.tracker.versions.verify_adapter(core_state, self.core.adapter.plugin)
        report_progress('files', '校验当前组合文件')
        view = self.file_view(self.tracker.scopes.values())
        actual = self.tracker.versions.content_state(view.snapshot(self.tracker.scopes,
            progress=lambda done, total: report_progress('files', '校验当前组合文件', done, total)))
        if actual != combination['state']:
            raise PatchError('当前组合文件内容已变化，未启动 Harness。请检查记录之外的改动。')
        # Recheck control inputs after potentially lengthy reads. This path
        # neither creates a journal nor writes the manifest or plugin layers.
        self.manager.require_stopped()
        if (self.manager.manifest_path.read_bytes() != manifest or self.tracker.load() != old
                or self.core.load() != core_state
                or self.tracker.versions.hash(Path(self.manager.settings['executable'])) != host):
            raise PatchError('校验期间配置或版本记录发生变化，请刷新后重试。')
        return True

    def _generic(self, view, operations, result, plugin, support=None):
        for op in operations:
            path = self.tracker.target(op['key'])
            for entry in (op['before'], op['after']):
                if entry and entry['type'] == 'file':
                    self.tracker.blob_bytes(entry['hash'])
            source = self.tracker.root / 'blobs' / op['after']['hash'] if op['after'] and op['after']['type'] == 'file' else None
            if support is not None and op['after'] is not None:
                self._parents(view, path.parent, result, support)
            try:
                changed = view.change(path, op['before'], op['after'], self.tracker.equal, source)
            except PatchError as exc:
                raise PatchError(f'插件 {plugin} 的组合预检失败：{exc}。请检查记录时的基础组合。') from exc
            if changed:
                result.append({'engine': 'tracking', **op})

    def _parents(self, view, path, operations, support):
        if view.exists(path):
            return
        candidates = [(name, root) for name, root in self.tracker.scopes.items()
                      if path == root or path.is_relative_to(root)]
        if not candidates:
            return  # An existing parent outside the managed scope.
        scope, root = max(candidates, key=lambda pair: len(pair[1].parts))
        if path == self.tracker.scopes['app']:
            raise PatchError('该插件记录需要解包应用目录，请同时选择记录时的基础插件组合。')
        self._parents(view, path.parent, operations, support)
        key = scope + '|' + (path.relative_to(root).as_posix() if path != root else '.')
        op = {'key': key, 'before': None, 'after': {'type': 'dir'}}
        if view.change(path, None, op['after'], self.tracker.equal):
            operations.append({'engine': 'tracking', **op})
            support.append(op)

    def _core(self, view, plan, operations):
        if plan is None:
            return
        for original in plan['operations']:
            op = dict(original, engine='core')
            if op['kind'] == 'move':
                source, dest = Path(op['source']), Path(op['dest'])
                op['fingerprint'] = view.tree_hash(source) if view.is_dir(source) else view.hash(source)
                op['directory'] = view.is_dir(source)
                op['contentFingerprint'] = view.tree_hash(source, content=True) if op['directory'] else view.hash(source)
                view.move(source, dest)
                operations.append(op)
            else:
                path = Path(op['path'])
                # Validate the reverse blob as well, even if this turns out a no-op.
                before_bytes, after_bytes = self.core.bytes(op['before']), self.core.bytes(op['after'])
                before = view.entry(path)
                if (before.get('hash') if before else None) != op['before']:
                    raise PatchError('内置补丁预检基线不匹配：' + str(path))
                op['beforeMtime'] = before.get('mtime') if before else None
                op['afterMtime'] = time.time_ns() if after_bytes is not None else None
                after = {'type': 'file', 'hash': op['after'], 'size': len(after_bytes),
                         'mtime': op['afterMtime']} if after_bytes is not None else None
                source = self.core.root / 'blobs' / op['after'] if after else None
                if view.change(path, before, after, self.tracker.equal, source):
                    operations.append(op)

    def _keep_baseline_blobs(self, view, snapshot):
        # Usually these blobs already came from the installation baseline. Only
        # missing content is copied; no whole-directory staging copy is created.
        for key, entry in snapshot.items():
            if entry['type'] != 'file':
                continue
            destination = self.tracker.root / 'blobs' / entry['hash']
            if not destination.exists():
                source = view.entries[self.tracker.target(key)]['source']
                info = source.stat()
                digest = self.tracker.store_file(source, [info.st_size, info.st_mtime_ns, info.st_ctime_ns])
                if digest != entry['hash']:
                    raise PatchError('保存原生基线时文件改变，未开始切换。')
            self.tracker.blob_bytes(entry['hash'])

    def prepare(self, selection, plugins, before, after, native):
        report_progress('files', '扫描本次切换涉及的文件')
        self.manager.require_stopped()
        self.manager.require_executable()
        old = self.tracker.load()
        previous_core = self.core.load()
        config_plan = self._configuration_plan(selection, plugins, before, after, old, previous_core)
        if config_plan is not None:
            if native:
                self.core.verify_native_config([p for p in plugins if p['id'] not in old['records']])
            return config_plan
        preserve_native = bool(previous_core and previous_core['mode'] == 'native'
                               and self.core.adapter.plugin not in selection)
        view = self.file_view(self.roots(include_dormant=not preserve_native), self.hashes)
        view.hash_files(progress=lambda done, total: report_progress('files', '校验本次切换涉及的文件', done, total))
        with self.tracker.versions.read_view(view):
            return self._prepare(selection, plugins, before, after, native, old, previous_core, view, preserve_native)

    def _configuration_plan(self, selection, plugins, before, after, old, core_state):
        """Plan a bundle-only edit; verify the sealed live state at execution.

        A recorded plugin with an empty file delta needs no layer replay. The
        existing combination is only an expectation here, never proof of today's
        files. execute() freshly verifies it before the manifest can be written.
        """
        reference = old.get('currentCombination')
        if not reference or before == after:
            return None
        current = self.tracker.read_object(reference)
        previous_selection = current['selection']
        changed = set(selection) ^ set(previous_selection)
        if (self.core.adapter.plugin in changed
                or current['manifestHash'] != hashlib.sha256(before).hexdigest()
                or any(name not in old['records'] for name in changed)):
            return None
        if any(self.tracker.read_object(old['records'][name]['delta'])['changes'] for name in changed):
            return None
        # Only bundle membership may differ; the normal planner handles every
        # other edit and all changes to the built-in adapter.
        previous_json, target_json = json.loads(before), json.loads(after)
        previous_json['dsh']['profile']['bundles'] = target_json['dsh']['profile']['bundles']
        if previous_json != target_json:
            return None
        view = self.file_view([self.manager.manifest_path])
        expected = self._signature(view)
        operations = []
        self._core(view, {'operations': [self.core.file_op(self.manager.manifest_path, after, view)]}, operations)
        new = copy.deepcopy(old)
        for name, record in new['records'].items():
            record['enabled'] = name in selection
        lock = self.tracker.versions.lock(selection, plugins, old, current['hostHash'])
        combination = {**lock, 'baseline': current['baseline'], 'state': current['state'],
                       'manifestHash': self.core.blob(after),
                       'bundles': target_json['dsh']['profile']['bundles'],
                       'adapterRevision': current['adapterRevision']}
        reference = self.tracker.versions.object('combinations', combination)
        new['currentCombination'] = reference
        if reference not in new.setdefault('combinations', []):
            new['combinations'].append(reference)
        plan = {'configurationOnly': True, 'operations': operations, 'previous': old, 'new': new,
                'previousCore': core_state, 'inputCore': core_state, 'newCore': core_state,
                'selection': list(selection), 'mode': 'plugin' if selection else 'native',
                'expected': expected, 'target': current['state'], 'host': current['hostHash'],
                'combination': reference}
        self.validate(plan)
        return plan

    def _prepare(self, selection, plugins, before, after, native, old, previous_core, view, preserve_native=False):
        report_progress('files', '校验文件并预演插件组合')
        if old.get('inventory'):
            recorded = self.tracker.recorded_state(old)
            if recorded is not None:
                expected = self.tracker.versions.content_state(recorded)
                actual = self.tracker.versions.content_state(view.snapshot(self.tracker.scopes))
                changed = [key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key)]
                if changed:
                    raise PatchError('记录窗口外发生了文件内容变化，未开始切换：' + '、'.join(sorted(changed)[:4]))
        initial = copy.deepcopy(view)
        operations = []
        new = copy.deepcopy(old)
        for name in reversed(old['order']):
            record = old['records'][name]
            if record['enabled']:
                changes = self.tracker.read_object(record['delta'])['changes']
                self._generic(view, self.tracker.operations(changes, False), operations, name)
            new['records'][name]['enabled'] = name in selection
        self._generic(view, self.tracker.operations(old.get('supportDirectories', []), False), operations, '组合目录')
        registered = set(old['records'])
        core_plugins = [p for p in plugins if p['id'] not in registered]
        report_progress('files', '推演原生基线与插件层')
        # Derive and preserve the native state from the same verified base, even
        # when the user is switching directly between two plugin combinations.
        native_view = copy.deepcopy(view)
        native_plan = self.core.prepare([], core_plugins, before, before, True, native_view,
                                        preserve_native=preserve_native)
        self._core(native_view, native_plan, [])
        baseline_snapshot = native_view.snapshot(self.tracker.scopes)
        # app.asar.bak is a retained extra copy, not the active official base.
        baseline_state = self.tracker.versions.content_state({k: v for k, v in baseline_snapshot.items()
                                                             if not k.startswith('archive_backup|')})
        host = self.tracker.versions.hash(Path(self.manager.settings['executable']))
        baseline = {'format': 1, 'hostHash': host, 'state': baseline_state,
                    'officialArchive': native_plan['state']['archiveHash'] if native_plan else native_view.hash(self.core.archive)}
        previous_baseline = old.get('nativeBaselines', {}).get(host)
        previous_value = self.tracker.read_object(previous_baseline) if previous_baseline else None
        reuse_baseline = bool(isinstance(previous_value, dict)
                              and all(previous_value.get(key) == value for key, value in baseline.items()))
        if native_view.is_file(self.core.archive):
            if reuse_baseline and 'archiveInfo' in previous_value:
                # Archive and unpacked bytes have just been hashed against the
                # sealed baseline. Re-parsing every ASAR member proves nothing
                # additional when the complete content identities are equal.
                baseline['archiveInfo'] = previous_value['archiveInfo']
            else:
                report_progress('files', '验证官方归档内容')
                baseline['archiveInfo'] = archive_info(self.core.archive, self.core.resources / 'app.asar.unpacked',
                                                      verify=True, view=native_view)
        if previous_baseline and previous_value != baseline:
            raise PatchError('原生基线与已有记录不同，未修改文件。请检查应用更新或记录之外的改动。')
        baseline_ref = previous_baseline or self.tracker.versions.object('baselines', baseline)
        if not reuse_baseline:
            report_progress('files', '校验官方归档与还原快照')
            self._keep_baseline_blobs(native_view, baseline_snapshot)
        # Generic replay always verifies every blob it actually uses, including
        # rollback bytes. Unused baseline backups are checked when needed.
        report_progress('files', '生成并检查目标组合')
        new.setdefault('nativeBaselines', {})[host] = baseline_ref
        core_plan = self.core.prepare(selection, core_plugins, before, after, native, view,
                                     preserve_native=preserve_native)
        self._core(view, core_plan, operations)
        if core_plan is None and before != after:
            # Route the plain manifest write through the same journal as layers.
            self._core(view, {'operations': [self.core.file_op(self.manager.manifest_path, after, view)]}, operations)
        support = []
        for name in old['order']:
            if name in selection:
                changes = self.tracker.read_object(old['records'][name]['delta'])['changes']
                self._generic(view, self.tracker.operations(changes), operations, name, support)
        new['supportDirectories'] = support
        target = self.tracker.versions.content_state(view.snapshot(self.tracker.scopes))
        lock = self.tracker.versions.lock(selection, plugins, old, host)
        combination = {**lock, 'baseline': baseline_ref, 'state': target,
                       'manifestHash': self.core.blob(after),
                       'bundles': json.loads(after.decode('utf-8-sig'))['dsh']['profile']['bundles'],
                       'adapterRevision': core_plan['state']['revision'] if core_plan else None}
        reference = self.tracker.versions.object('combinations', combination)
        new['currentCombination'] = reference
        if reference not in new.setdefault('combinations', []):
            new['combinations'].append(reference)
        # Hash every input this plan uses, including dormant trees when the
        # adapter may change. Recheck independently before the first mutation.
        expected = self._signature(initial)
        plan = {'operations': operations, 'previous': old, 'new': new,
                'previousCore': previous_core or (core_plan['previous'] if core_plan else None),
                'newCore': core_plan['state'] if core_plan else None,
                'inputCore': previous_core, 'selection': list(selection),
                'mode': 'plugin' if selection else 'native', 'expected': expected, 'target': target,
                'host': host, 'combination': reference}
        if preserve_native:
            plan['preserveNative'] = True
        self.validate(plan)
        return plan

    def validate(self, plan):
        """Decode the whole command list before the first filesystem write."""
        try:
            if not isinstance(plan, dict):
                raise ValueError('Plan must be an object')
            if plan.get('requiredFeatures', []) != []:
                raise ValueError('Unsupported required transaction features')
            validate_operations(plan['operations'])
            configuration_only = plan.get('configurationOnly', False)
            if type(configuration_only) is not bool:
                raise ValueError('Invalid configuration-only flag')
            if configuration_only:
                old_combo = self.tracker.read_object(plan['previous']['currentCombination'])
                new_combo = self.tracker.read_object(plan['combination'])
                changed = set(plan['selection']) ^ set(old_combo['selection'])
                expected_registry = copy.deepcopy(plan['previous'])
                for name, record in expected_registry['records'].items():
                    record['enabled'] = name in plan['selection']
                expected_registry['currentCombination'] = plan['combination']
                if plan['combination'] not in expected_registry.setdefault('combinations', []):
                    expected_registry['combinations'].append(plan['combination'])
                if (plan['previousCore'] != plan['inputCore'] or plan['newCore'] != plan['inputCore']
                        or plan['new'] != expected_registry
                        or plan['host'] != old_combo['hostHash'] or new_combo['hostHash'] != plan['host']
                        or new_combo['selection'] != plan['selection']
                        or self.core.adapter.plugin in changed or plan['target'] != old_combo['state']
                        or new_combo['state'] != old_combo['state'] or new_combo['baseline'] != old_combo['baseline']
                        or new_combo['adapterRevision'] != old_combo['adapterRevision']
                        or len(plan['operations']) != 1
                        or set(plan['expected']) != {str(self.manager.manifest_path)}
                        or any(name not in plan['previous']['records'] or
                            self.tracker.read_object(plan['previous']['records'][name]['delta'])['changes']
                            for name in changed)):
                    raise ValueError('Configuration-only plan cannot change file layers')
                op = plan['operations'][0]
                if (op['engine'] != 'core' or op['kind'] != 'file' or op['path'] != str(self.manager.manifest_path)
                        or op['before'] != old_combo['manifestHash'] or op['after'] != new_combo['manifestHash']):
                    raise ValueError('Configuration-only plan must change only the manifest')
            preserve_native = plan.get('preserveNative', False)
            if type(preserve_native) is not bool:
                raise ValueError('Invalid preserved adapter flag')
            if preserve_native and (not plan['inputCore'] or plan['inputCore']['mode'] != 'native'
                    or plan['inputCore'] != plan['newCore'] or plan['previousCore'] != plan['inputCore']
                    or self.core.adapter.plugin in plan['selection']
                    or any(op['engine'] == 'core' and (op['kind'] != 'file'
                        or op['path'] != str(self.manager.manifest_path)) for op in plan['operations'])):
                raise ValueError('Preserved adapter cannot execute adapter mutations')
            digest(plan['host'])
            object_reference(plan['combination'], 'combinations')
            if (not isinstance(plan['selection'], list)
                    or any(not isinstance(name, str) for name in plan['selection'])
                    or len(set(plan['selection'])) != len(plan['selection'])
                    or plan['mode'] != ('plugin' if plan['selection'] else 'native')):
                raise ValueError('Invalid plan selection')
            for key in ('previous', 'new'):
                validate_registry(plan[key])
            for key in ('previousCore', 'inputCore', 'newCore'):
                if plan[key] is not None:
                    self.core.validate_state(plan[key])
            if not isinstance(plan['expected'], dict) or not isinstance(plan['target'], dict):
                raise ValueError('Invalid state signature')
            for op in plan['operations']:
                if op['engine'] == 'tracking':
                    self.tracker.target(op['key'])
                elif op['kind'] == 'file':
                    self.core.safe(Path(op['path']))
                else:
                    self.core.safe(Path(op['source']))
                    self.core.safe(Path(op['dest']))
        except (KeyError, TypeError, ValueError) as exc:
            raise PatchError('组合事务记录无效，未修改文件：' + str(exc)) from exc
        return plan

    @staticmethod
    def _signature(view):
        return {str(path): view.entry(path) for path in view.entries}

    def _after(self, op):
        if op['engine'] == 'tracking':
            actual = self.tracker.actual(op['key'])
            return self.tracker.equal(actual, op['after']), self.tracker.equal(actual, op['before'])
        if op['kind'] == 'file':
            actual = file_hash(self.core.safe(Path(op['path'])))
            return actual == op['after'], actual == op['before']
        source, dest = self.core.safe(Path(op['source'])), self.core.safe(Path(op['dest']))
        return dest.exists() and not source.exists(), source.exists() and not dest.exists()

    def replay(self, op, undo=False):
        try:
            decode_operation(op)
        except OperationError as exc:
            raise PatchError('组合操作记录无效：' + str(exc)) from exc
        if op['engine'] == 'tracking':
            self.tracker.replay([op], undo=undo)
            return
        if op['kind'] == 'move':
            at = Path(op['dest'] if undo else op['source'])
            view = self.file_view([at])
            view.hash_files(progress=lambda done, total: report_progress('sync', '校验待移动的补丁文件', done, total))
            signature = view.tree_hash(at) if op['directory'] else view.hash(at)
            if signature != op['fingerprint']:
                raise PatchError('目录或归档在预检后改变，已停止切换：' + str(at))
            content_signature = view.tree_hash(at, content=True) if op['directory'] else view.hash(at)
            if content_signature != op['contentFingerprint']:
                raise PatchError('目录或归档内容在预检后改变，已停止切换：' + str(at))
        self.core.replay([op], undo=undo)
        if op['kind'] == 'file':
            mtime = op.get('beforeMtime' if undo else 'afterMtime')
            if mtime is not None:
                os.utime(Path(op['path']), ns=(mtime, mtime))

    def restore_states(self, plan):
        if plan['previousCore'] is None:
            self.core.record.unlink(missing_ok=True)
        else:
            self.core.save(plan['previousCore'])
        self.tracker.save(plan['previous'])

    def execute(self, plan):
        report_progress('sync', '复核切换条件')
        self.manager.require_stopped()
        self.manager.require_executable()
        self.validate(plan)
        if self.tracker.load() != plan['previous'] or self.core.load() != plan['inputCore']:
            raise PatchError('预检后版本索引发生变化，未开始切换，请刷新重试。')
        _, obj, bundles = self.manager.manifest()
        configuration_only = plan.get('configurationOnly', False)
        if configuration_only:
            previous = self.tracker.read_object(plan['previous']['currentCombination'])
            if not self.verify_current(previous['selection'], self.manager.discover(obj, bundles),
                                       self.core.bytes(previous['manifestHash'])):
                raise PatchError('当前组合已变化，未修改加载配置，请刷新重试。')
        self.tracker.check_versions(self.manager.discover(obj, bundles), plan['selection'])
        if self.tracker.versions.hash(Path(self.manager.settings['executable'])) != plan['host']:
            raise PatchError('Harness 在预检后发生更新，未开始切换。')
        inputs = self.file_view([self.manager.manifest_path] if configuration_only else
                          self.roots(include_dormant=not plan.get('preserveNative', False)))
        inputs.hash_files(progress=lambda done, total: report_progress('sync', '独立复核切换输入', done, total))
        if self._signature(inputs) != plan['expected']:
            raise PatchError('预检后文件发生变化，未开始切换，请刷新重试。')
        journal = {'format': 3, 'id': uuid.uuid4().hex, 'cursor': 0, 'committed': False,
                   'plan': self.tracker.versions.object('transactions', plan)}
        put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
        try:
            for index, op in enumerate(plan['operations']):
                report_progress('sync', '同步插件与补丁', index, len(plan['operations']), 'changes')
                self.replay(op)
                journal['cursor'] = index + 1
                put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
            report_progress('sync', '校验切换结果')
            # A configuration-only transaction never writes managed contents.
            # Those were freshly verified above, just like an unchanged launch.
            # Verify the only changed file; file-layer transactions still get
            # their independent full result scan.
            if configuration_only:
                if file_hash(self.manager.manifest_path) != plan['operations'][0]['after']:
                    raise PatchError('加载配置写入后校验失败。')
                actual = plan['target']
            else:
                verified_snapshot = self.file_view(self.tracker.scopes.values()).snapshot(self.tracker.scopes,
                    progress=lambda done, total: report_progress('sync', '校验切换结果', done, total))
                actual = self.tracker.versions.content_state(verified_snapshot)
            if actual != plan['target']:
                raise PatchError('切换后的文件未通过整套组合校验。')
            self.verify_recovery_host(plan, len(plan['operations']))
            if plan['newCore']:
                self.core.save(plan['newCore'])
            if plan['new']['records'] and not configuration_only:
                # Bind the inventory to the independently verified output.
                # Another directory walk adds latency and could adopt changes
                # arriving after verification into the trusted metadata index.
                inventory = {key: ['file', item['size'], item['mtime']] if item['type'] == 'file'
                             else ['link', item['target']] if item['type'] == 'link' else ['dir']
                             for key, item in verified_snapshot.items()}
                plan['new']['inventory'] = self.tracker.object('snapshots', inventory)
            self.tracker.save(plan['new'])
            journal['committed'] = True
            put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
        except Exception as error:
            try:
                self.recover(notify=False)
            except Exception as rollback_error:
                details = ({'code': rollback_error.code, 'details': rollback_error.details,
                            'recovery': rollback_error.recovery, 'retryable': rollback_error.retryable}
                           if isinstance(rollback_error, PatchError) else {})
                raise PatchError('组合切换和回滚未完成，请保留快照与 switch.json：' + str(rollback_error),
                                 **details) from error
            raise PatchError('组合切换失败，已恢复切换前状态：' + str(error)) from error
        # Once the commit marker is durable, cleanup cannot turn this into a
        # failed switch: recovery deliberately preserves committed file state.
        # Leave the marker for the next recovery if antivirus/permissions hold
        # the journal open, and still return the receipt needed for launch undo.
        try:
            self.journal.unlink()
        except OSError:
            logging.warning('Committed switch journal could not be removed', exc_info=True)
        return {'plan': plan, 'mode': plan['mode']}

    def rollback(self, receipt):
        plan = receipt['plan']
        self.validate(plan)
        # Launch may fail after the switch commit. Make its rollback recoverable
        # as well; repeating the recovery after a crash is idempotent.
        journal = {'format': 3, 'cursor': len(plan['operations']), 'committed': False,
                   'plan': self.tracker.versions.object('transactions', plan)}
        put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
        self.recover(notify=False)

    def verify_recovery_host(self, plan, count):
        """Check all archive locations at the journal cursor before any undo.

        The executable can stay identical in an ASAR-only update. Account for
        this transaction's legitimate archive edits/moves in memory, including
        the mutation-before-cursor-persistence crash boundary. Never accept a
        new active ASAR just because an old parked archive is still present.
        """
        def reject(component):
            raise PatchError('Harness 宿主文件在事务期间发生变化，已停止跨版本回滚并保留恢复记录。',
                code='host_identity_changed', details={'components': [component]},
                recovery='保留 switch.json、事务计划及快照；先恢复该事务对应的 Harness 安装版本，再恢复事务。')
        if self.tracker.versions.hash(Path(self.manager.settings['executable'])) != plan['host']:
            reject('executable')
        paths = {self.core.archive: 'archive|.',
                 self.core.resources / 'app.asar.bak': 'archive_backup|.',
                 self.core.stash / 'official.asar': None}
        if plan.get('configurationOnly'):
            state = self.tracker.read_object(plan['previous']['currentCombination'])['state']
            entries = {path: state.get(key) for path, key in paths.items()}
            core = plan['inputCore']
            if core and core['mode'] == 'plugin':
                entries[self.core.stash / 'official.asar'] = {'type': 'file', 'hash': core['archiveHash']}
        else:
            entries = {path: plan['expected'].get(str(path)) for path in paths}
        expected = {}
        for path, entry in entries.items():
            if entry and entry['type'] != 'file':
                reject('officialArchive')
            expected[path] = entry['hash'] if entry else None
        for op in plan['operations'][:count]:
            if op['engine'] == 'tracking':
                path = self.tracker.target(op['key'])
                if path in expected:
                    entry = op['after']
                    if entry and entry['type'] != 'file':
                        reject('officialArchive')
                    expected[path] = entry['hash'] if entry else None
            elif op['kind'] == 'file':
                path = Path(op['path'])
                if path in expected:
                    expected[path] = op['after']
            else:
                source, dest = Path(op['source']), Path(op['dest'])
                if source in expected or dest in expected:
                    if source not in expected or dest not in expected or op['directory']:
                        reject('officialArchive')
                    expected[dest], expected[source] = expected[source], None
        for path, digest in expected.items():
            if (path.is_symlink() or path.is_junction() or (path.exists() and not path.is_file())
                    or self.tracker.versions.hash(path) != digest):
                reject('officialArchive')

    def recover(self, notify=True):
        if not self.journal.exists():
            return
        self.manager.require_stopped()
        try:
            journal = json.loads(self.journal.read_bytes())
            if (not isinstance(journal, dict) or type(journal.get('format')) is not int or journal['format'] != 3
                    or type(journal.get('committed')) is not bool):
                raise ValueError('Invalid journal envelope')
            object_reference(journal['plan'], 'transactions')
        except (ValueError, KeyError, TypeError) as exc:
            raise PatchError('组合恢复日志格式无效，请保留原始日志。') from exc
        plan = self.validate(self.tracker.read_object(journal['plan']))
        operations = plan['operations']
        count = journal.get('cursor')
        if type(count) is not int or not 0 <= count <= len(operations):
            raise PatchError('组合恢复位置无效，请保留原始日志。')
        if journal['committed'] and count != len(operations):
            raise PatchError('组合提交标记与操作位置不一致，请保留原始日志。')
        if journal.get('committed'):
            self.journal.unlink()
            return
        if count < len(operations):
            after, before = self._after(operations[count])
            if after and not before:
                count += 1  # Crash between file mutation and cursor persistence.
            elif not before:
                raise PatchError('切换中断后文件又被修改，无法自动回滚。')
        # Save a decreasing cursor before each undo. A crash after undo is
        # recognized as a not-applied candidate on the next recovery attempt.
        while count:
            self.verify_recovery_host(plan, count)
            op = operations[count - 1]
            after, before = self._after(op)
            if not after and not before:
                raise PatchError('恢复时发现未知文件改动，已停止覆盖。')
            journal['cursor'] = count - 1
            put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
            if after and not before:
                self.replay(op, undo=True)
            count -= 1
        self.verify_recovery_host(plan, 0)
        self.restore_states(plan)
        self.journal.unlink()
        if notify:
            raise PatchError('上次中断的组合切换已回滚，请刷新后重试。')
