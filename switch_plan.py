"""Plan a complete combination, then apply it with one recoverable journal."""
from __future__ import annotations

import copy
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
from patch_modes import PatchError, archive_info, file_hash, put, tree_hash


class SwitchPlan:
    def __init__(self, tracker, hashes=None):
        self.tracker = tracker
        self.manager = tracker.manager
        self.core = tracker.core
        self.journal = tracker.root / 'switch.json'
        self.hashes = hashes

    def roots(self):
        roots = list(self.tracker.scopes.values())
        roots += [self.manager.manifest_path, *self.core.extra_roots()]
        return roots

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
        self.manager.require_stopped()
        self.manager.require_executable()
        old = self.tracker.load()
        previous_core = self.core.load()
        view = FileView(self.roots(), self.hashes)
        with self.tracker.versions.read_view(view):
            return self._prepare(selection, plugins, before, after, native, old, previous_core, view)

    def _prepare(self, selection, plugins, before, after, native, old, previous_core, view):
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
        # Derive and preserve the native state from the same verified base, even
        # when the user is switching directly between two plugin combinations.
        native_view = copy.deepcopy(view)
        native_plan = self.core.prepare([], core_plugins, before, before, True, native_view)
        self._core(native_view, native_plan, [])
        baseline_snapshot = native_view.snapshot(self.tracker.scopes)
        # app.asar.bak is a retained extra copy, not the active official base.
        baseline_state = self.tracker.versions.content_state({k: v for k, v in baseline_snapshot.items()
                                                             if not k.startswith('archive_backup|')})
        host = file_hash(Path(self.manager.settings['executable']))
        baseline = {'format': 1, 'hostHash': host, 'state': baseline_state,
                    'officialArchive': native_plan['state']['archiveHash'] if native_plan else native_view.hash(self.core.archive)}
        if native_view.is_file(self.core.archive):
            baseline['archiveInfo'] = archive_info(self.core.archive, self.core.resources / 'app.asar.unpacked',
                                                  verify=True, view=native_view)
        baseline_ref = self.tracker.versions.object('baselines', baseline)
        previous_baseline = old.get('nativeBaselines', {}).get(host)
        if previous_baseline and self.tracker.read_object(previous_baseline) != baseline:
            raise PatchError('原生基线与已有记录不同，未修改文件。请检查应用更新或记录之外的改动。')
        self._keep_baseline_blobs(native_view, baseline_snapshot)
        new.setdefault('nativeBaselines', {})[host] = baseline_ref
        core_plan = self.core.prepare(selection, core_plugins, before, after, native, view)
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
        # Hash the entire input, including parked adapter files. Rechecked before
        # the first mutation; no timestamps are trusted as content validation.
        expected = self._signature(initial)
        plan = {'operations': operations, 'previous': old, 'new': new,
                'previousCore': previous_core or (core_plan['previous'] if core_plan else None),
                'newCore': core_plan['state'] if core_plan else None,
                'inputCore': previous_core, 'selection': list(selection),
                'mode': 'plugin' if selection else 'native', 'expected': expected, 'target': target,
                'host': host, 'combination': reference}
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
            signature = tree_hash(at) if op['directory'] else file_hash(at)
            if signature != op['fingerprint']:
                raise PatchError('目录或归档在预检后改变，已停止切换：' + str(at))
            content_signature = tree_hash(at, content=True) if op['directory'] else file_hash(at)
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
        self.manager.require_stopped()
        self.manager.require_executable()
        self.validate(plan)
        if self.tracker.load() != plan['previous'] or self.core.load() != plan['inputCore']:
            raise PatchError('预检后版本索引发生变化，未开始切换，请刷新重试。')
        _, obj, bundles = self.manager.manifest()
        self.tracker.check_versions(self.manager.discover(obj, bundles), plan['selection'])
        if file_hash(Path(self.manager.settings['executable'])) != plan['host']:
            raise PatchError('Harness 在预检后发生更新，未开始切换。')
        if self._signature(FileView(self.roots())) != plan['expected']:
            raise PatchError('预检后文件发生变化，未开始切换，请刷新重试。')
        journal = {'format': 3, 'id': uuid.uuid4().hex, 'cursor': 0, 'committed': False,
                   'plan': self.tracker.versions.object('transactions', plan)}
        put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
        try:
            for index, op in enumerate(plan['operations']):
                self.replay(op)
                journal['cursor'] = index + 1
                put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
            actual = self.tracker.versions.content_state(FileView(self.tracker.scopes.values()).snapshot(self.tracker.scopes))
            if actual != plan['target']:
                raise PatchError('切换后的文件未通过整套组合校验。')
            if plan['newCore']:
                self.core.save(plan['newCore'])
            if plan['new']['records']:
                plan['new']['inventory'] = self.tracker.object('snapshots', self.tracker.metadata())
            self.tracker.save(plan['new'])
            journal['committed'] = True
            put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
        except Exception as error:
            try:
                self.recover(notify=False)
            except Exception as rollback_error:
                raise PatchError('组合切换和回滚未完成，请保留快照与 switch.json：' + str(rollback_error)) from error
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
            op = operations[count - 1]
            after, before = self._after(op)
            if not after and not before:
                raise PatchError('恢复时发现未知文件改动，已停止覆盖。')
            journal['cursor'] = count - 1
            put(self.journal, json.dumps(journal, ensure_ascii=False).encode())
            if after and not before:
                self.replay(op, undo=True)
            count -= 1
        self.restore_states(plan)
        self.journal.unlink()
        if notify:
            raise PatchError('上次中断的组合切换已回滚，请刷新后重试。')
