"""Host updates must fail early without adopting new bytes or losing rollback."""
import json
import os
import unittest
from unittest.mock import patch

from launcher import MenuError, OFFICIAL
from patch_modes import PatchError
import test_launcher as generic
import test_patch_modes as adapter


class AdapterHostUpdateTests(unittest.TestCase):
    setUp = adapter.PatchModeTests.setUp
    switch = adapter.PatchModeTests.switch

    def assert_rejected_without_work(self, selection):
        tracker = self.manager.tracking
        manifest, registry, core = self.manifest.read_bytes(), tracker.index.read_bytes(), tracker.core.record.read_bytes()
        with patch.object(self.manager, 'backup') as backup, \
                patch.object(tracker, 'metadata') as scan, \
                patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError) as caught:
            self.manager.apply(selection, self.manager.state()['revision'], launch=True)
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        self.assertFalse(caught.exception.retryable)
        self.assertIn('不要用旧快照覆盖新版', caught.exception.recovery)
        backup.assert_not_called()
        scan.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual((manifest, registry, core),
                         (self.manifest.read_bytes(), tracker.index.read_bytes(), tracker.core.record.read_bytes()))
        self.assertFalse((tracker.root / 'switch.json').exists())
        return caught.exception

    def test_executable_update_rejects_before_backup_and_tree_scan(self):
        self.switch(['dsh-purge'])
        self.exe.write_bytes(b'new official executable')
        with patch.object(self.manager.tracking.versions, 'hash', wraps=self.manager.tracking.versions.hash) as hashes:
            error = self.assert_rejected_without_work([])
        hashes.assert_called_once_with(self.exe)
        self.assertEqual(error.details['components'], ['executable'])

    def test_asar_only_update_with_same_size_and_mtime_invalidates_anchor(self):
        self.switch([])
        info = self.archive.stat()
        changed = self.archive.read_bytes().replace(b'0.2-test', b'0.3-test')
        self.assertNotEqual(changed, self.archive.read_bytes())
        self.archive.write_bytes(changed)
        os.utime(self.archive, ns=(info.st_atime_ns, info.st_mtime_ns))
        error = self.assert_rejected_without_work(['dsh-purge'])
        self.assertEqual(error.details['components'], ['officialArchive'])
        self.assertEqual(self.archive.read_bytes(), changed)

    def test_new_active_archive_is_not_hidden_by_old_parked_archive(self):
        self.switch(['dsh-purge'])
        parked = self.manager.tracking.core.stash / 'official.asar'
        original = parked.read_bytes()
        self.archive.write_bytes(original.replace(b'0.2-test', b'0.3-test'))
        self.assert_rejected_without_work(['dsh-purge'])
        self.assertEqual(parked.read_bytes(), original)
        self.assertTrue(self.app.is_dir())

    def test_same_bytes_replaced_host_is_not_misclassified_as_update(self):
        self.switch(['dsh-purge'])
        replacement = self.exe.with_suffix('.new')
        replacement.write_bytes(self.exe.read_bytes())
        os.replace(replacement, self.exe)
        self.manager.tracking.check_host_identity(['dsh-purge'])

    def test_idle_file_guard_classifies_update_before_inventory_scan(self):
        self.switch([])
        self.archive.write_bytes(b'changed archive')
        with patch.object(self.manager.tracking, 'metadata') as scan, self.assertRaises(PatchError) as caught:
            self.manager.tracking.verify_idle(check_files=True)
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        scan.assert_not_called()

    def test_anchor_match_does_not_hide_active_patch_conflict(self):
        self.switch(['dsh-purge'])
        (self.app / 'main.js').write_bytes(b'external edit')
        self.manager.tracking.check_host_identity(['dsh-purge'])
        with patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError):
            self.manager.apply(['dsh-purge'], self.manager.state()['revision'], launch=True)
        spawn.assert_not_called()

    def test_unchanged_anchors_do_not_scan_trees_or_write_history(self):
        self.switch(['dsh-purge'])
        tracker = self.manager.tracking
        with patch.object(tracker.versions, 'hash', wraps=tracker.versions.hash) as hashes, \
                patch.object(tracker, 'metadata') as scan, patch.object(tracker.store, 'object') as write:
            tracker.check_host_identity(['dsh-purge'])
        self.assertLessEqual(hashes.call_count, 2)
        scan.assert_not_called()
        write.assert_not_called()


class GenericHostUpdateTests(unittest.TestCase):
    setUp = generic.ManagerTests.setUp

    def test_generic_recorded_archive_is_compared_to_current_not_native_base(self):
        tracker = self.manager.tracking
        tracker.core.resources.mkdir()
        adapter.make_asar(tracker.core.archive)
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        value = tracker.load()
        combination = tracker.read_object(value['currentCombination'])
        tracker.core.archive.write_bytes(b'legitimate captured generic patch')
        from patch_modes import file_hash
        combination['state']['archive|.']['hash'] = file_hash(tracker.core.archive)
        value['currentCombination'] = tracker.versions.object('combinations', combination)
        tracker.save(value)
        # This unit exercises only the early filter. Independent layer and
        # package validation is still required before any real write/launch.
        tracker.check_host_identity(['plugin-a'])
        tracker.core.archive.write_bytes(b'unknown subsequent update')
        with self.assertRaises(PatchError) as caught:
            tracker.check_host_identity(['plugin-a'])
        self.assertEqual(caught.exception.code, 'host_identity_changed')

    def test_unsealed_active_record_already_pins_executable(self):
        self.exe.write_bytes(b'updated host')
        with patch.object(self.manager, 'backup') as backup, self.assertRaises(MenuError) as caught:
            self.manager.apply([], self.manager.state()['revision'])
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        backup.assert_not_called()

    def test_changed_executable_after_preflight_still_fails_commit(self):
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        from switch_plan import SwitchPlan
        original = SwitchPlan.prepare
        def update_after_plan(planner, *args, **kwargs):
            plan = original(planner, *args, **kwargs)
            self.exe.write_bytes(b'updated during preflight')
            return plan
        with patch.object(SwitchPlan, 'prepare', update_after_plan), \
                patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError):
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        spawn.assert_not_called()

    def test_update_after_successful_switch_is_detected_before_process_creation(self):
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        switch = self.manager.switch_layers
        def update_after_commit(*args, **kwargs):
            receipt = switch(*args, **kwargs)
            self.exe.write_bytes(b'new host just before launch')
            return receipt
        with patch.object(self.manager, 'switch_layers', side_effect=update_after_commit), \
                patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError) as caught:
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        spawn.assert_not_called()
        self.assertEqual(self.exe.read_bytes(), b'new host just before launch')

    def test_update_during_failed_launch_preserves_journal_instead_of_cross_version_undo(self):
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        from switch_plan import SwitchPlan
        planner = SwitchPlan(self.manager.tracking)
        raw, obj, bundles = self.manager.manifest()
        obj['dsh']['profile']['bundles'] = list(OFFICIAL)
        after = json.dumps(obj).encode()
        receipt = planner.execute(planner.prepare([], self.manager.discover(obj, bundles), raw, after, True))
        self.exe.write_bytes(b'update after commit, before failed launch rollback')
        manifest = self.path.read_bytes()
        with patch.object(planner, 'replay') as undo, self.assertRaises(PatchError) as caught:
            planner.rollback(receipt)
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        undo.assert_not_called()
        self.assertEqual(self.path.read_bytes(), manifest)
        journal = json.loads(planner.journal.read_bytes())
        self.assertFalse(journal['committed'])
        self.assertTrue((self.manager.tracking.root / journal['plan']).is_file())
