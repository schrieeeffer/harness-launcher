"""Version and whole-switch regressions; every path belongs to a temp fixture."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from launcher import Manager, MenuError
from file_view import FileView
from patch_modes import PatchError, PatchModes, file_hash, tree_hash
from switch_plan import SwitchPlan
import test_plugin_tracking as helpers


class VersionPlanTests(unittest.TestCase):
    setUp = helpers.GenericTrackingTests.setUp
    wait_job = helpers.GenericTrackingTests.wait_job
    begin = helpers.GenericTrackingTests.begin
    install_fixture = helpers.GenericTrackingTests.install_fixture
    accept = helpers.GenericTrackingTests.accept
    close_runtime = helpers.GenericTrackingTests.close_runtime
    apply = helpers.GenericTrackingTests.apply

    def record(self, name='plugin-a', content=b'A'):
        self.begin(name)
        self.install_fixture(name)
        target = self.home / 'skills' / (name + '.txt')
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(content)
        code = self.profile / 'node_modules' / name / 'index.js'
        code.write_bytes(b'code version 1')
        self.accept()
        return target

    def prepare(self, ids):
        raw, obj, bundles = self.manager.manifest()
        obj['dsh']['profile']['bundles'] = [*helpers.OFFICIAL, *ids]
        after = (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode()
        return SwitchPlan(self.manager.tracking).prepare(ids, self.manager.discover(obj, bundles),
                                                       raw, after, not ids)

    def test_package_revision_captures_nested_dependency_graph_locks_and_code(self):
        self.begin()
        self.install_fixture('plugin-a')
        root = self.profile / 'node_modules' / 'plugin-a'
        meta = json.loads((root / 'package.json').read_bytes())
        meta.update(dependencies={'utility': '^1'}, optionalDependencies={'absent': '*'},
                    peerDependencies={'peer': '*'}, peerDependenciesMeta={'peer': {'optional': True}})
        (root / 'package.json').write_text(json.dumps(meta))
        utility = root / 'node_modules' / 'utility'
        utility.mkdir(parents=True)
        (utility / 'package.json').write_text(json.dumps({'name': 'utility', 'version': '1.3',
                                                         'dependencies': {'leaf': '2'}}))
        (utility / 'index.js').write_bytes(b'utility code')
        leaf = self.profile / 'node_modules' / 'leaf'
        leaf.mkdir()
        (leaf / 'package.json').write_text('{"name":"leaf","version":"2.1"}')
        lock = self.profile / 'package-lock.json'
        lock.write_bytes(b'{"lockfileVersion":3}')
        self.accept()
        tracker = self.manager.tracking
        record = tracker.load()['records']['plugin-a']
        revision = tracker.read_object(record['revision'])
        capsule = tracker.read_object(record['package'])
        self.assertEqual(revision['package'], record['package'])
        self.assertEqual({p['name']: p['version'] for p in capsule['packages'].values()},
                         {'plugin-a': '1.0', 'utility': '1.3', 'leaf': '2.1'})
        self.assertTrue(capsule['packages']['0']['dependencies']['absent']['optional'])
        self.assertEqual(tracker.blob_bytes(capsule['installMetadata']['package-lock.json']), lock.read_bytes())
        self.assertEqual(tracker.read_object(revision['baseline'])['app|main.js']['type'], 'file')
        self.apply([])
        self.apply(['plugin-a'])  # Profile bundle edits do not invalidate the package capsule.
        self.assertIn('plugin-a', self.manager.state()['enabled'])

    def test_package_code_and_shared_dependency_drift_stop_before_any_mutation(self):
        self.begin()
        self.install_fixture('plugin-a')
        root = self.profile / 'node_modules' / 'plugin-a'
        meta = json.loads((root / 'package.json').read_bytes())
        meta['dependencies'] = {'utility': '*'}
        (root / 'package.json').write_text(json.dumps(meta))
        utility = self.profile / 'node_modules' / 'utility'
        utility.mkdir()
        (utility / 'package.json').write_text('{"name":"utility","version":"1"}')
        code = utility / 'index.js'
        code.write_bytes(b'original dependency')
        self.accept()
        code.write_bytes(b'modified dependency, same version')
        before = self.manifest.read_bytes()
        with patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, '依赖内容与版本记录不同'):
                self.apply([])
            replay.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertTrue(self.app.exists())

    def test_nested_node_modules_assets_are_captured_and_verified(self):
        self.install_fixture('plugin-a')
        root = self.profile / 'node_modules' / 'plugin-a'
        bundled = root / 'assets' / 'node_modules' / 'bundled.js'
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b'bundled runtime code')
        versions = self.manager.tracking.versions
        reference = versions.package_snapshot('plugin-a')
        capsule = self.manager.tracking.read_object(reference)
        self.assertIn('assets/node_modules/bundled.js', capsule['packages']['0']['files'])

        bundled.write_bytes(b'changed bundled runtime code')
        with self.assertRaisesRegex(PatchError, '依赖内容与版本记录不同'):
            versions.verify_package('plugin-a', reference)

    def test_optional_peer_does_not_make_required_dependency_optional(self):
        self.install_fixture('plugin-a')
        manifest = self.profile / 'node_modules' / 'plugin-a' / 'package.json'
        meta = json.loads(manifest.read_bytes())
        meta.update(dependencies={'absent-required': '*'},
                    peerDependencies={'absent-required': '*'},
                    peerDependenciesMeta={'absent-required': {'optional': True}})
        manifest.write_text(json.dumps(meta))

        with self.assertRaisesRegex(PatchError, '插件依赖缺失.*absent-required'):
            self.manager.tracking.versions.package_snapshot('plugin-a')

    def test_dependency_directory_without_manifest_cannot_select_a_different_parent_package(self):
        self.install_fixture('plugin-a')
        root = self.profile / 'node_modules' / 'plugin-a'
        manifest = root / 'package.json'
        meta = json.loads(manifest.read_bytes())
        meta['dependencies'] = {'utility': '*'}
        manifest.write_text(json.dumps(meta))
        parent = self.profile / 'node_modules' / 'utility'
        parent.mkdir()
        (parent / 'package.json').write_text('{"name":"utility","version":"1"}')
        local = root / 'node_modules' / 'utility'
        local.mkdir(parents=True)
        (local / 'index.js').write_bytes(b'the code Node actually resolves')
        with self.assertRaisesRegex(PatchError, '依赖缺少 package.json'):
            self.manager.tracking.versions.package_snapshot('plugin-a')

    def test_loose_dependency_module_cannot_shadow_a_recorded_package(self):
        self.install_fixture('plugin-a')
        root = self.profile / 'node_modules' / 'plugin-a'
        manifest = root / 'package.json'
        meta = json.loads(manifest.read_bytes())
        meta['dependencies'] = {'utility': '*'}
        manifest.write_text(json.dumps(meta))
        package = root / 'node_modules' / 'utility'
        package.mkdir(parents=True)
        (package / 'package.json').write_text('{"name":"utility","version":"1"}')
        for suffix in ('.js', '.json', '.node'):
            module = package.with_suffix(suffix)
            module.write_bytes(b'loose module fixture')
            with self.subTest(suffix=suffix), self.assertRaisesRegex(PatchError, '依赖被无清单模块遮蔽'):
                self.manager.tracking.versions.package_snapshot('plugin-a')
            module.unlink()

    def test_upgrade_retains_immutable_old_code_dependencies_and_parent(self):
        file = self.record()
        tracker = self.manager.tracking
        old = copy.deepcopy(tracker.load()['records']['plugin-a'])
        old_bytes = (tracker.root / old['revision']).read_bytes()
        self.begin()
        root = self.profile / 'node_modules' / 'plugin-a'
        meta = json.loads((root / 'package.json').read_bytes())
        meta['version'] = '2.0'
        (root / 'package.json').write_text(json.dumps(meta))
        (root / 'index.js').write_bytes(b'code version 2')
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b'version 2 patch')
        self.accept()
        current = tracker.load()['records']['plugin-a']
        self.assertEqual(current['history'][0]['package'], old['package'])
        self.assertEqual(tracker.read_object(current['revision'])['parent'], old['revision'])
        self.assertEqual((tracker.root / old['revision']).read_bytes(), old_bytes)
        old_package = tracker.read_object(old['package'])
        old_code = old_package['packages']['0']['files']['index.js']['hash']
        self.assertEqual(tracker.blob_bytes(old_code), b'code version 1')
        self.apply([])
        self.assertFalse(file.exists())
        self.apply(['dsh-purge', 'plugin-a'])
        self.assertEqual(file.read_bytes(), b'version 2 patch')

    def test_conflict_is_rejected_before_generic_or_builtin_replay(self):
        self.record()
        self.begin('plugin-b')
        self.install_fixture('plugin-b')
        file = self.home / 'skills' / 'plugin-a.txt'
        file.write_bytes(b'A and B')
        self.accept()
        before = self.manifest.read_bytes()
        with patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, 'plugin-b.*预检失败'):
                self.apply(['dsh-purge', 'plugin-b'])
            replay.assert_not_called()
        self.assertEqual(file.read_bytes(), b'A and B')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse((self.manager.tracking.root / 'switch.json').exists())

    def test_all_independent_combinations_restore_same_base_and_reuse_ids(self):
        a, b = self.record(), self.record('plugin-b', b'B')
        ids = {}
        for selected in ([], ['plugin-a'], ['plugin-b'], ['plugin-a', 'plugin-b'], [], ['plugin-b'], []):
            self.apply(selected)
            self.assertEqual(a.exists(), 'plugin-a' in selected)
            self.assertEqual(b.exists(), 'plugin-b' in selected)
            value = self.manager.tracking.load()
            combo = self.manager.tracking.read_object(value['currentCombination'])
            key = tuple(selected)
            if key in ids:
                self.assertEqual(ids[key], value['currentCombination'])
            ids[key] = value['currentCombination']
            self.assertEqual(combo['plugins'], {name: '1.0' for name in selected})
            self.assertEqual(combo['order'], selected)
            self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(len(value['nativeBaselines']), 1)
        self.assertFalse((self.home / 'skills').exists())
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])

    def test_read_only_preflight_detects_target_conflict_with_parked_app(self):
        self.begin()
        self.install_fixture('plugin-a')
        (self.app / 'main.js').write_bytes(b'patch needs extracted app')
        self.accept()
        self.apply([])
        with patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, '基础插件组合'):
                self.apply(['plugin-a'])
            replay.assert_not_called()
        self.assertFalse(self.app.exists())
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)

    def test_same_timestamp_edit_after_preflight_is_detected(self):
        target = self.record()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        stamp = target.stat().st_mtime_ns
        target.write_bytes(b'X')  # Same length and mtime, different content.
        os.utime(target, ns=(stamp, stamp))
        before = self.manifest.read_bytes()
        with patch.object(planner, 'replay') as replay:
            with self.assertRaisesRegex(PatchError, '预检后文件发生变化'):
                planner.execute(plan)
            replay.assert_not_called()
        self.assertEqual(target.read_bytes(), b'X')
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_untracked_same_timestamp_change_cannot_become_first_native_baseline(self):
        self.record()
        tracker = self.manager.tracking
        previous = tracker.load()
        self.assertFalse(previous.get('currentCombination'))
        self.assertFalse(previous.get('nativeBaselines'))
        target = self.profile / 'cordis.yml'
        before = target.stat()
        original = target.read_bytes()
        self.assertEqual(original.strip(), b'[]')
        modified = original.replace(b'[]', b'{}')
        target.write_bytes(modified)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(tracker.metadata(), tracker.read_object(previous['inventory']))
        manifest = self.manifest.read_bytes()

        with patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, '内容变化.*root_yaml'):
                self.apply([])
            replay.assert_not_called()
        self.assertEqual(target.read_bytes(), modified)
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual(tracker.load(), previous)

    def test_overlay_restore_reads_blob_without_original_file_timestamp(self):
        tracker = self.manager.tracking
        snapshot = tracker.snapshot()
        target = self.profile / 'cordis.yml'
        entry = snapshot['root_yaml|.']
        original = target.read_bytes()
        target.write_bytes(b'# runtime rewrite\n[]\n')
        view = FileView([target])
        blob = tracker.root / 'blobs' / entry['hash']

        self.assertTrue(view.change(target, view.entry(target), entry, tracker.equal, blob))
        self.assertEqual(view.read_bytes(target), original)
        with view.open(target) as stream:
            self.assertEqual(stream.read(), original)
        self.assertEqual(view.entry(target)['mtime'], entry['mtime'])

    def test_corrupt_revision_and_target_blob_stop_before_writes(self):
        file = self.record()
        tracker = self.manager.tracking
        record = tracker.load()['records']['plugin-a']
        path = tracker.root / record['revision']
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        with patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, '校验失败'):
                self.apply([])
            replay.assert_not_called()
        path.write_bytes(original)
        self.apply([])
        delta = tracker.read_object(record['delta'])
        digest = next(item['after']['hash'] for item in delta['changes'] if item['key'].endswith('plugin-a.txt'))
        (tracker.root / 'blobs' / digest).write_bytes(b'corrupt')
        with patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, '校验失败'):
                self.apply(['plugin-a'])
            replay.assert_not_called()
        self.assertFalse(file.exists())

    def test_whole_switch_crash_after_move_recovers_original_combination(self):
        target = self.record()
        tracker = self.manager.tracking
        before = tracker.load()
        manifest = self.manifest.read_bytes()
        original = SwitchPlan.replay
        crashed = False
        def crash(planner, op, undo=False):
            nonlocal crashed
            original(planner, op, undo)
            if not undo and not crashed and op.get('kind') == 'move':
                crashed = True
                raise SystemExit('power loss after move, before cursor')
        with patch.object(SwitchPlan, 'replay', crash):
            with self.assertRaises(SystemExit):
                self.apply([])
        self.assertTrue((tracker.root / 'switch.json').is_file())
        fresh = Manager(self.manager.data)
        with self.assertRaisesRegex(PatchError, '上次中断的组合切换已回滚'):
            fresh.tracking.guard()
        self.assertEqual(target.read_bytes(), b'A')
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual(fresh.tracking.load(), before)
        fresh.tracking.guard(check_files=True)
        self.assertTrue(self.app.exists())

    def test_crash_during_recovery_can_resume_without_double_undo(self):
        target = self.record()
        tracker = self.manager.tracking
        planner = SwitchPlan(tracker)
        plan = self.prepare([])
        for op in plan['operations']:
            planner.replay(op)
        journal = {'format': 3, 'cursor': len(plan['operations']), 'committed': False, 'plan': tracker.versions.object('transactions', plan)}
        planner.journal.write_text(json.dumps(journal))
        original = SwitchPlan.replay
        def crash(planner, op, undo=False):
            original(planner, op, undo)
            if undo:
                raise SystemExit('power loss during rollback')
        with patch.object(SwitchPlan, 'replay', crash):
            with self.assertRaises(SystemExit):
                planner.recover()
        with self.assertRaisesRegex(PatchError, '已回滚'):
            planner.recover()
        self.assertEqual(target.read_bytes(), b'A')
        self.assertEqual(tracker.load(), plan['previous'])
        tracker.guard(check_files=True)

    def test_launch_failure_restores_combination_lock_and_native_baseline_index(self):
        target = self.record()
        self.apply(['dsh-purge', 'plugin-a'])
        old = self.manager.tracking.load()
        with patch('launcher.subprocess.Popen', side_effect=OSError('cannot launch')):
            with self.assertRaises(MenuError):
                self.apply([], launch=True)
        self.assertEqual(self.manager.tracking.load(), old)
        self.assertEqual(target.read_bytes(), b'A')
        self.manager.tracking.guard(check_files=True)

    def test_saved_scheme_and_new_backup_refuse_silent_version_substitution(self):
        target = self.record()
        self.manager.scheme('work', ['dsh-purge', 'plugin-a'])
        self.apply([])
        backup = self.manager.backup_list()[0]['id']
        self.begin()
        root = self.profile / 'node_modules' / 'plugin-a'
        meta = json.loads((root / 'package.json').read_bytes())
        meta['version'] = '2.0'
        (root / 'package.json').write_text(json.dumps(meta))
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b'v2')
        self.accept()
        with self.assertRaisesRegex(MenuError, '版本.*已变化'):
            self.manager.apply(['dsh-purge', 'plugin-a'], self.manager.state()['revision'], scheme='work')
        with self.assertRaisesRegex(MenuError, '版本.*已变化'):
            self.manager.restore(backup, self.manager.state()['revision'])
        self.manager.scheme('work', ['dsh-purge', 'plugin-a'])
        self.manager.apply(['dsh-purge', 'plugin-a'], self.manager.state()['revision'], scheme='work')
        self.assertEqual(target.read_bytes(), b'v2')

    def test_missing_snapshot_and_unknown_shared_directory_files_do_not_get_overwritten(self):
        file = self.record()
        tracker = self.manager.tracking
        capsule = tracker.read_object(tracker.load()['records']['plugin-a']['package'])
        digest = capsule['packages']['0']['files']['index.js']['hash']
        blob = tracker.root / 'blobs' / digest
        content = blob.read_bytes()
        blob.unlink()
        raw = self.manifest.read_bytes()
        with patch('launcher.atomic_write') as write, patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, '缺失或无法读取'):
                self.apply([])
            self.assertFalse(any(call.args[0] == self.manifest for call in write.call_args_list))
            replay.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), raw)
        blob.write_bytes(content)
        unknown = file.parent / 'unrecorded.txt'
        unknown.write_bytes(b'keep this unrelated user file')
        with self.assertRaises(MenuError):
            self.apply([])
        self.assertEqual(unknown.read_bytes(), b'keep this unrelated user file')

    def test_corrupt_combination_object_is_not_silently_replaced(self):
        self.record()
        self.apply([])
        tracker = self.manager.tracking
        reference = tracker.load()['currentCombination']
        (tracker.root / reference).write_bytes(b'corrupted combination')
        with patch.object(SwitchPlan, 'replay') as replay:
            with self.assertRaisesRegex(MenuError, '校验失败'):
                self.apply([])
            replay.assert_not_called()

    def test_unknown_changes_after_interruption_stop_recovery_without_deletion(self):
        self.record()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        count = next(i for i, op in enumerate(plan['operations']) if op.get('kind') == 'move') + 1
        for op in plan['operations'][:count]:
            planner.replay(op)
        moved = Path(plan['operations'][count - 1]['dest'])
        unknown = moved / 'new-user-file.txt'
        unknown.write_bytes(b'keep after crash')
        planner.journal.write_text(json.dumps({'format': 3, 'cursor': count, 'committed': False,
            'plan': self.manager.tracking.versions.object('transactions', plan)}))
        with self.assertRaisesRegex(PatchError, '改变|改动'):
            planner.recover()
        self.assertEqual(unknown.read_bytes(), b'keep after crash')
        self.assertTrue(planner.journal.exists())

    def test_same_timestamp_tree_changes_after_interruption_stop_recovery(self):
        self.record()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        count = next(i for i, op in enumerate(plan['operations'])
                     if op.get('kind') == 'move' and op.get('directory')) + 1
        for op in plan['operations'][:count]:
            planner.replay(op)
        target = Path(plan['operations'][count - 1]['dest']) / 'main.js'
        before = target.stat()
        modified = b'X' * before.st_size
        target.write_bytes(modified)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        planner.journal.write_text(json.dumps({'format': 3, 'cursor': count, 'committed': False,
            'plan': self.manager.tracking.versions.object('transactions', plan)}))

        with self.assertRaisesRegex(PatchError, '内容.*改变'):
            planner.recover()
        self.assertEqual(target.read_bytes(), modified)
        self.assertTrue(planner.journal.exists())

    def test_commit_journal_is_small_and_completed_switch_is_not_undone(self):
        self.record()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        receipt = planner.execute(plan)
        # Simulate a crash after the commit marker, before journal removal.
        planner.journal.write_text(json.dumps({'format': 3, 'cursor': len(plan['operations']), 'committed': True,
            'plan': self.manager.tracking.versions.object('transactions', plan)}))
        self.assertLess(planner.journal.stat().st_size, 512)
        planner.recover()
        self.assertEqual(self.manager.tracking.load()['currentCombination'], receipt['plan']['combination'])
        self.assertFalse(self.app.exists())
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)

    def test_committed_journal_cleanup_failure_still_returns_successful_receipt(self):
        self.record()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        original_unlink = Path.unlink
        def locked_journal(path, *args, **kwargs):
            if path == planner.journal:
                raise PermissionError('simulated antivirus journal lock')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=locked_journal), self.assertLogs(level='WARNING'):
            receipt = planner.execute(plan)
        self.assertEqual(receipt['mode'], 'native')
        self.assertTrue(json.loads(planner.journal.read_bytes())['committed'])
        self.assertEqual(self.manager.tracking.load()['currentCombination'], plan['combination'])
        self.assertFalse(self.app.exists())
        planner.recover()
        self.assertFalse(planner.journal.exists())
        self.assertFalse(self.app.exists())
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)

    def test_dependency_and_registry_changes_after_preflight_are_rejected(self):
        self.record()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        code = self.profile / 'node_modules' / 'plugin-a' / 'index.js'
        old = code.read_bytes()
        code.write_bytes(b'changed after preflight')
        with patch.object(planner, 'replay') as replay:
            with self.assertRaisesRegex(PatchError, '依赖内容与版本记录不同'):
                planner.execute(plan)
            replay.assert_not_called()
        code.write_bytes(old)
        value = self.manager.tracking.load()
        value['externalChange'] = True
        self.manager.tracking.save(value)
        with patch.object(planner, 'replay') as replay:
            with self.assertRaisesRegex(PatchError, '索引发生变化'):
                planner.execute(plan)
            replay.assert_not_called()

    def test_uncached_preflight_shares_reads_but_execution_rehashes_disk(self):
        self.record()
        with patch.object(self.manager, 'verification_cache', None), patch('file_view.file_hash', wraps=file_hash) as read:
            plan = self.prepare([])
            calls = [call for call in read.call_args_list if call.args[0] == self.app / 'main.js']
            self.assertEqual(len(calls), 1)
            SwitchPlan(self.manager.tracking).execute(plan)
            calls = [call for call in read.call_args_list if call.args[0] == self.app / 'main.js']
            self.assertEqual(len(calls), 3)  # Independent input recheck and move validation.

    def test_snapshot_metadata_is_deduplicated_and_detects_corruption(self):
        tracker = self.manager.tracking
        snapshot = tracker.snapshot()
        first = tracker.object('snapshots', snapshot)
        self.assertEqual(first, tracker.object('snapshots', snapshot))
        (tracker.root / first).write_bytes((tracker.root / first).read_bytes() + b' ')
        with self.assertRaisesRegex(PatchError, '校验失败'):
            tracker.read_object(first)

    def test_current_package_mutation_while_saving_does_not_publish_revision(self):
        self.begin()
        self.install_fixture('plugin-a')
        code = self.profile / 'node_modules' / 'plugin-a' / 'index.js'
        code.write_bytes(b'original')
        tracker = self.manager.tracking
        original = tracker.versions.package_snapshot
        def race(name, source=''):
            reference = original(name, source)
            code.write_bytes(b'changed during managed file verification')
            return reference
        with patch.object(tracker.versions, 'package_snapshot', side_effect=race):
            self.close_runtime()
            session = self.wait_job(allow_error=True)['session']
        self.assertIn('依赖内容与版本记录不同', session['error'])
        self.assertNotIn('plugin-a', tracker.load()['records'])
        self.assertTrue(session['baseline'])

    def test_invalid_last_operation_is_rejected_before_first_mutation(self):
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        before = self.manifest.read_bytes()
        for malformed in ({'engine': 'shell', 'kind': 'move'},
                          {'engine': 'core', 'kind': 'erase'},
                          {'engine': 'tracking', 'key': 'app|../outside', 'before': None, 'after': {'type': 'dir'}}):
            modified = copy.deepcopy(plan)
            modified['operations'].append(malformed)
            with patch.object(planner, 'replay') as replay:
                with self.assertRaisesRegex(PatchError, '组合事务记录无效'):
                    planner.execute(modified)
                replay.assert_not_called()
            self.assertFalse(planner.journal.exists())
            self.assertEqual(self.manifest.read_bytes(), before)
            self.assertTrue(self.app.exists())

    def test_invalid_recovery_cursor_and_commit_marker_preserve_journal(self):
        tracker = self.manager.tracking
        planner = SwitchPlan(tracker)
        plan = self.prepare([])
        ref = tracker.versions.object('transactions', plan)
        before = self.manifest.read_bytes()
        for changes in ({'cursor': True}, {'committed': 'true'}, {'committed': True}, {'format': 2}):
            journal = {'format': 3, 'cursor': 0, 'committed': False, 'plan': ref, **changes}
            planner.journal.write_text(json.dumps(journal), encoding='utf-8')
            with patch.object(planner, 'replay') as replay:
                with self.assertRaisesRegex(PatchError, '无效|不一致'):
                    planner.recover()
                replay.assert_not_called()
            self.assertTrue(planner.journal.is_file())
            self.assertEqual(self.manifest.read_bytes(), before)
        planner.journal.unlink()

    def test_recovery_validates_all_commands_before_undoing_anything(self):
        tracker = self.manager.tracking
        planner = SwitchPlan(tracker)
        plan = self.prepare([])
        plan['operations'][0]['engine'] = 'unexpected'
        planner.journal.parent.mkdir(parents=True, exist_ok=True)
        planner.journal.write_text(json.dumps({'format': 3, 'cursor': 0, 'committed': False,
            'plan': tracker.versions.object('transactions', plan)}), encoding='utf-8')
        with patch.object(planner, 'replay') as replay:
            with self.assertRaisesRegex(PatchError, '组合事务记录无效'):
                planner.recover()
            replay.assert_not_called()
        self.assertTrue(self.app.exists())
        self.assertTrue(planner.journal.is_file())

    def test_overlay_rejects_moving_directory_into_itself(self):
        view = FileView([self.app])
        before = set(view.entries)
        with self.assertRaisesRegex(PatchError, '自身'):
            view.move(self.app, self.app / 'nested')
        self.assertEqual(set(view.entries), before)


if __name__ == '__main__':
    unittest.main()
