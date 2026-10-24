"""Optimized switches retain content and recovery checks in isolated fixtures."""
import copy
import os
import threading
import unittest
from unittest.mock import patch

from file_view import FileView
from launcher import MenuError
from patch_modes import PatchError, file_hash, tree_hash
from switch_plan import SwitchPlan
import test_file_view as file_tests
import test_version_plan as plan_tests


class ParallelHashTests(unittest.TestCase):
    setUp = file_tests.FileViewTests.setUp

    def test_parallel_reads_report_completed_files_and_match_serial_snapshot(self):
        for index in range(40):
            (self.tree / str(index)).write_bytes(str(index).encode())
        view = FileView([self.tree])
        expected = view.snapshot({'tree': self.tree})
        samples, thread_ids = [], set()
        owner = threading.get_ident()
        def report(done, total):
            samples.append((done, total))
            thread_ids.add(threading.get_ident())
        self.assertEqual(FileView([self.tree]).snapshot({'tree': self.tree}, report), expected)
        self.assertEqual(samples, [(i, 40) for i in range(41)])
        self.assertEqual(thread_ids, {owner})

    def test_fresh_parallel_pass_detects_same_metadata_edit(self):
        for index in range(40):
            (self.tree / str(index)).write_bytes(b'old')
        first = FileView([self.tree])
        first.hash_files()
        target = self.tree / '5'
        before = target.stat()
        target.write_bytes(b'new')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = FileView([self.tree])
        second.hash_files()
        self.assertNotEqual(first.hash(target), second.hash(target))

    def test_parallel_read_propagates_errors_without_returning_a_snapshot(self):
        for index in range(40):
            (self.tree / str(index)).write_bytes(b'content')
        view = FileView([self.tree])
        (self.tree / '5').unlink()
        with self.assertRaises(PatchError):
            view.snapshot({'tree': self.tree}, lambda *_: None)

    def test_overlay_fingerprints_survive_moves_but_invalidate_on_edits(self):
        nested = self.tree / '目录'
        nested.mkdir()
        source = nested / 'file.txt'
        source.write_bytes(b'original')
        view = FileView([self.tree])
        original = view.tree_hash(self.tree, content=True)
        self.assertEqual(original, tree_hash(self.tree, content=True))
        clone = copy.deepcopy(view)
        clone.move(self.tree, self.root / 'moved')
        self.assertEqual(clone.tree_hash(self.root / 'moved', content=True), original)
        target = self.root / 'moved' / '目录' / 'file.txt'
        clone.change(target, clone.entry(target), None, lambda a, b: a == b)
        self.assertNotEqual(clone.tree_hash(self.root / 'moved', content=True), original)
        self.assertEqual(view.tree_hash(self.tree, content=True), original)
        clone.entries[self.root / 'moved']['type'] = 'link'
        with self.assertRaises(PatchError):
            clone.tree_hash(self.root / 'moved', content=True)

    def test_subtree_moves_preserve_unrelated_children_and_match_real_snapshot(self):
        source, target = self.tree / 'old', self.tree / 'new'
        source.mkdir()
        nested = source / 'nested'
        nested.mkdir()
        (nested / 'file').write_bytes(b'content')
        (self.tree / 'untouched').write_bytes(b'keep')
        empty = source / 'empty'
        empty.mkdir()
        view = FileView([self.tree])
        view.hash_files()
        view.move(source, target)
        os.replace(source, target)
        self.assertEqual(view.snapshot({'tree': self.tree}), FileView([self.tree]).snapshot({'tree': self.tree}))
        view.move(target / 'nested' / 'file', self.tree / 'file')
        os.replace(target / 'nested' / 'file', self.tree / 'file')
        self.assertEqual(view.snapshot({'tree': self.tree}), FileView([self.tree]).snapshot({'tree': self.tree}))
        self.assertEqual(view.children(target / 'nested'), [])
        self.assertEqual(view.children(target / 'empty'), [])


class NativeSwitchTests(unittest.TestCase):
    setUp = plan_tests.VersionPlanTests.setUp
    wait_job = plan_tests.VersionPlanTests.wait_job
    begin = plan_tests.VersionPlanTests.begin
    install_fixture = plan_tests.VersionPlanTests.install_fixture
    accept = plan_tests.VersionPlanTests.accept
    close_runtime = plan_tests.VersionPlanTests.close_runtime
    apply = plan_tests.VersionPlanTests.apply
    prepare = plan_tests.VersionPlanTests.prepare

    def record_native_plugin(self):
        self.apply([])
        self.begin('plugin-a')
        self.install_fixture('plugin-a')
        target = self.home / 'rules' / 'feature.txt'
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b'plugin content')
        self.accept()
        return target

    def test_generic_round_trip_never_reads_or_modifies_dormant_adapter(self):
        target = self.record_native_plugin()
        parked = self.manager.tracking.core.stash / 'app'
        dormant = parked / 'main.js'
        dormant.write_bytes(b'keep dormant edit')
        capture = FileView._capture
        def forbid(view, path, info=None):
            self.assertFalse(path == parked or path.is_relative_to(parked))
            return capture(view, path, info)
        with patch.object(FileView, '_capture', forbid):
            self.apply([])
            self.assertFalse(target.exists())
            self.apply(['plugin-a'])
            self.assertEqual(target.read_bytes(), b'plugin content')
        self.assertEqual(dormant.read_bytes(), b'keep dormant edit')
        with self.assertRaises(MenuError):
            self.apply(['dsh-purge', 'plugin-a'])

    def test_inventory_uses_verified_result_and_rejects_later_additions(self):
        self.record_native_plugin()
        plan = self.prepare([])
        tracker = self.manager.tracking
        save = tracker.core.save
        added = self.home / 'rules' / 'outside-record.txt'
        def after_verification(value):
            save(value)
            added.parent.mkdir(exist_ok=True)
            added.write_bytes(b'not part of the verified result')
        with patch.object(tracker.core, 'save', side_effect=after_verification):
            SwitchPlan(tracker).execute(plan)
        self.assertTrue(added.exists())
        self.assertNotIn('rules|outside-record.txt', tracker.read_object(tracker.load()['inventory']))
        with self.assertRaises(PatchError):
            tracker.guard(check_files=True)

    def test_preserved_adapter_cannot_hide_a_move_in_a_transaction(self):
        self.record_native_plugin()
        plan = self.prepare([])
        self.assertTrue(plan['preserveNative'])
        broken = copy.deepcopy(plan)
        broken['selection'] = ['dsh-purge']
        with self.assertRaises(PatchError):
            SwitchPlan(self.manager.tracking).validate(broken)
        broken = copy.deepcopy(plan)
        broken['preserveNative'] = 1
        with self.assertRaises(PatchError):
            SwitchPlan(self.manager.tracking).validate(broken)
        broken = copy.deepcopy(plan)
        core = self.manager.tracking.core
        signature = file_hash(core.archive)
        broken['operations'].append({'engine': 'core', 'kind': 'move', 'source': str(core.archive),
                                     'dest': str(core.stash / 'official.asar'), 'directory': False,
                                     'fingerprint': signature, 'contentFingerprint': signature})
        with self.assertRaisesRegex(PatchError, 'Preserved adapter'):
            SwitchPlan(self.manager.tracking).validate(broken)

    def test_active_edit_after_native_preflight_still_blocks_commit(self):
        target = self.record_native_plugin()
        plan = self.prepare([])
        before = target.stat()
        target.write_bytes(b'X' * before.st_size)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        manifest = self.manifest.read_bytes()
        planner = SwitchPlan(self.manager.tracking)
        with patch.object(planner, 'replay') as replay, self.assertRaises(PatchError):
            planner.execute(plan)
        replay.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), manifest)

    def test_verified_native_base_is_reused_but_active_corruption_is_rejected(self):
        self.record_native_plugin()
        tracker = self.manager.tracking
        with patch('switch_plan.archive_info', side_effect=AssertionError('redundant ASAR parse')), \
                patch.object(SwitchPlan, '_keep_baseline_blobs', side_effect=AssertionError('unused baseline copies')):
            plan = self.prepare([])
        self.assertTrue(plan['preserveNative'])
        before = self.archive.stat()
        raw = self.archive.read_bytes()
        self.archive.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        os.utime(self.archive, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(PatchError):
            self.prepare([])

    def test_used_rollback_blob_is_still_verified_when_native_base_is_reused(self):
        self.record_native_plugin()
        tracker = self.manager.tracking
        record = tracker.load()['records']['plugin-a']
        changes = tracker.read_object(record['delta'])['changes']
        content = next(change['after']['hash'] for change in changes
                       if change['after'] and change['after']['type'] == 'file')
        (tracker.root / 'blobs' / content).write_bytes(b'corrupt rollback bytes')
        manifest = self.manifest.read_bytes()
        with self.assertRaises(MenuError):
            self.apply([])
        self.assertEqual(self.manifest.read_bytes(), manifest)

    def test_native_switch_launch_failure_restores_configuration_and_files(self):
        target = self.record_native_plugin()
        before = self.manifest.read_bytes()
        with patch('launcher.subprocess.Popen', side_effect=OSError('fixture failure')), self.assertRaises(MenuError):
            self.apply([], launch=True)
        self.assertEqual(target.read_bytes(), b'plugin content')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(self.manager.tracking.core.load()['mode'], 'native')

    def record_bundle_only(self):
        self.apply([])
        self.begin('plugin-a')
        self.install_fixture('plugin-a')
        self.accept()
        self.apply(['plugin-a'])

    def test_bundle_only_switch_checks_active_contents_once_and_round_trips(self):
        self.record_bundle_only()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        self.assertTrue(plan['configurationOnly'])
        with patch.object(planner, 'verify_current', wraps=planner.verify_current) as verify, \
                patch.object(planner, '_prepare', side_effect=AssertionError('unnecessary file replay')):
            receipt = planner.execute(plan)
        verify.assert_called_once()
        self.assertEqual(self.manager.state()['enabled'], [])
        planner.rollback(receipt)
        self.assertEqual(self.manager.state()['enabled'], ['plugin-a'])
        self.apply([])
        self.apply(['plugin-a'])

    def test_bundle_only_commit_rejects_same_metadata_active_changes(self):
        self.record_bundle_only()
        plan = self.prepare([])
        raw = self.manifest.read_bytes()
        info, content = self.archive.stat(), self.archive.read_bytes()
        self.archive.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        os.utime(self.archive, ns=(info.st_atime_ns, info.st_mtime_ns))
        planner = SwitchPlan(self.manager.tracking)
        with patch.object(planner, 'replay') as replay, self.assertRaises(PatchError):
            planner.execute(plan)
        replay.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), raw)

    def test_bundle_only_flag_rejects_file_mutations_and_nonboolean_values(self):
        self.record_bundle_only()
        plan = self.prepare([])
        broken = copy.deepcopy(plan)
        broken['configurationOnly'] = 1
        with self.assertRaises(PatchError):
            SwitchPlan(self.manager.tracking).validate(broken)
        broken = copy.deepcopy(plan)
        broken['operations'][0]['path'] = str(self.archive)
        with self.assertRaises(PatchError):
            SwitchPlan(self.manager.tracking).validate(broken)
        broken = copy.deepcopy(plan)
        broken['new']['records']['plugin-a']['enabled'] = True
        with self.assertRaises(PatchError):
            SwitchPlan(self.manager.tracking).validate(broken)

    def test_bundle_only_failed_launch_restores_manifest_and_combination(self):
        self.record_bundle_only()
        before, registry = self.manifest.read_bytes(), self.manager.tracking.load()
        with patch('launcher.subprocess.Popen', side_effect=OSError('fixture failure')), self.assertRaises(MenuError):
            self.apply([], launch=True)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(self.manager.tracking.load(), registry)

    def test_adapter_round_trip_reuses_content_verified_native_baseline(self):
        self.apply([])
        with patch('switch_plan.archive_info', side_effect=AssertionError('redundant ASAR parse')), \
                patch.object(SwitchPlan, '_keep_baseline_blobs', side_effect=AssertionError('unused baseline copies')):
            self.apply(['dsh-purge'])
            self.apply([])


if __name__ == '__main__':
    unittest.main()
