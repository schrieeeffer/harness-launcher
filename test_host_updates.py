"""Host updates must fail early without adopting new bytes or losing rollback."""
import json
import os
import unittest
from unittest.mock import patch

from launcher import MenuError, OFFICIAL
from patch_modes import PatchError
import test_launcher as generic
import test_patch_modes as adapter
import test_plugin_tracking as recording


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
        tracker.core.resources.mkdir(exist_ok=True)
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


class UpdateBoundaryTests(unittest.TestCase):
    setUp = adapter.PatchModeTests.setUp
    switch = adapter.PatchModeTests.switch

    def prepare(self, selection):
        from switch_plan import SwitchPlan
        planner = SwitchPlan(self.manager.tracking)
        raw, obj, bundles = self.manager.manifest()
        plugins = self.manager.discover(obj, bundles)
        obj['dsh']['profile']['bundles'] = [*OFFICIAL, *selection]
        plan = planner.prepare(selection, plugins, raw, json.dumps(obj).encode(), not selection)
        return planner, plan

    def test_downloading_update_outside_install_does_not_invalidate_native_or_plugin_mode(self):
        # The downloaded installer is not an installed executable/ASAR change.
        (self.root / 'new-harness-installer.exe').write_bytes(b'pending update')
        for selection in ([], ['dsh-purge']):
            with self.subTest(selection=selection):
                self.switch(selection)
                with patch('launcher.subprocess.Popen') as spawn:
                    self.manager.apply(selection, self.manager.state()['revision'], launch=True)
                spawn.assert_called_once()

    def test_disabled_adapter_update_does_not_block_native_but_cannot_enable_old_snapshot(self):
        self.switch([])
        meta = self.purge / 'package.json'
        value = json.loads(meta.read_bytes())
        value['version'] = '2.0'
        meta.write_text(json.dumps(value))
        (self.purge / 'index.js').write_bytes(b'updated plugin')
        self.switch([])
        with self.assertRaises(MenuError):
            self.switch(['dsh-purge'])
        self.assertFalse(self.app.exists())
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)

    def test_adapter_code_update_after_commit_prevents_launch_even_without_version_bump(self):
        self.switch([])
        switch = self.manager.switch_layers
        def update_after_commit(*args, **kwargs):
            receipt = switch(*args, **kwargs)
            (self.purge / 'index.js').write_bytes(b'updated with identical package version')
            return receipt
        with patch.object(self.manager, 'switch_layers', side_effect=update_after_commit), \
                patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError) as caught:
            self.manager.apply(['dsh-purge'], self.manager.state()['revision'], launch=True)
        self.assertEqual(caught.exception.code, 'plugin_identity_changed')
        spawn.assert_not_called()

    def test_asar_only_update_stops_rollback_before_any_undo_in_both_modes(self):
        self.switch([])
        for selection in (['dsh-purge'], []):
            with self.subTest(selection=selection):
                planner, plan = self.prepare(selection)
                receipt = planner.execute(plan)
                manifest = self.manifest.read_bytes()
                existed = self.archive.exists()
                before = self.archive.read_bytes() if existed else None
                self.archive.write_bytes(b'new official archive; executable unchanged')
                with patch.object(planner, 'replay') as undo, self.assertRaises(PatchError) as caught:
                    planner.rollback(receipt)
                self.assertEqual(caught.exception.code, 'host_identity_changed')
                undo.assert_not_called()
                self.assertEqual(self.manifest.read_bytes(), manifest)
                self.assertTrue(planner.journal.is_file())
                self.assertEqual(self.archive.read_bytes(), b'new official archive; executable unchanged')
                # Put back exactly the interrupted transaction's host, then recover.
                if existed:
                    self.archive.write_bytes(before)
                else:
                    self.archive.unlink()
                planner.recover(notify=False)
                self.assertFalse(planner.journal.exists())
                self.switch(selection)

    def test_archive_move_crash_before_cursor_write_can_still_recover(self):
        self.switch([])
        planner, plan = self.prepare(['dsh-purge'])
        cursor = next(i for i, op in enumerate(plan['operations'])
                      if op.get('source') == str(self.archive))
        for op in plan['operations'][:cursor + 1]:
            planner.replay(op)
        from patch_modes import put
        put(planner.journal, json.dumps({'format': 3, 'cursor': cursor, 'committed': False,
             'plan': self.manager.tracking.versions.object('transactions', plan)}).encode())
        planner.recover(notify=False)
        self.assertFalse(planner.journal.exists())
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertFalse(self.app.exists())

    def test_update_arriving_during_undo_stops_further_rollback_writes(self):
        self.switch([])
        planner, plan = self.prepare(['dsh-purge'])
        receipt = planner.execute(plan)
        replay = planner.replay
        calls = []
        def update_after_first_undo(op, undo=False):
            calls.append(op)
            replay(op, undo=undo)
            self.archive.write_bytes(b'official updater finished during undo')
        with patch.object(planner, 'replay', side_effect=update_after_first_undo), self.assertRaises(PatchError) as caught:
            planner.rollback(receipt)
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        self.assertEqual(len(calls), 1)
        self.assertTrue(planner.journal.is_file())
        self.assertEqual(self.archive.read_bytes(), b'official updater finished during undo')

    def test_purge_updater_handoff_cannot_be_treated_as_an_unchanged_install(self):
        self.switch(['dsh-purge'])
        moved = self.app.with_name('app.dshpurge-prev')
        self.app.rename(moved)
        # Model the installed purge updater's handoff, not its execution.
        self.archive.write_bytes(self.archive_bytes)
        (self.resources / 'dsh-purge-reapply-asar').write_bytes(b'1')
        manifest = self.manifest.read_bytes()
        with patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError):
            self.manager.apply(['dsh-purge'], self.manager.state()['revision'], launch=True)
        spawn.assert_not_called()
        self.assertTrue(moved.is_dir())
        self.assertFalse(self.app.exists())
        self.assertEqual(self.manifest.read_bytes(), manifest)

    def test_update_at_commit_keeps_structured_host_error_and_journal(self):
        self.switch([])
        planner, plan = self.prepare(['dsh-purge'])
        replay = planner.replay
        registry = self.manager.tracking.index.read_bytes()
        def update(op, undo=False):
            replay(op, undo=undo)
            if op is plan['operations'][-1]:
                self.exe.write_bytes(b'updated during final commit')
        with patch.object(planner, 'replay', side_effect=update), self.assertRaises(PatchError) as caught:
            planner.execute(plan)
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        self.assertEqual(caught.exception.details['components'], ['executable'])
        self.assertTrue(planner.journal.is_file())
        self.assertEqual(self.manager.tracking.index.read_bytes(), registry)


class GenericPackageUpdateTests(unittest.TestCase):
    setUp = generic.ManagerTests.setUp

    def test_package_update_after_verification_prevents_launch(self):
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        from switch_plan import SwitchPlan
        verify = SwitchPlan.verify_current
        def update(planner, *args, **kwargs):
            result = verify(planner, *args, **kwargs)
            (self.profile / 'node_modules' / 'plugin-a' / 'index.js').write_bytes(b'new code, same version')
            return result
        with patch.object(SwitchPlan, 'verify_current', update), \
                patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError) as caught:
            self.manager.apply(['plugin-a'], self.manager.state()['revision'], launch=True)
        self.assertEqual(caught.exception.code, 'plugin_identity_changed')
        spawn.assert_not_called()

    def test_inactive_generic_update_does_not_block_native_but_is_rejected_on_enable(self):
        self.manager.apply([], self.manager.state()['revision'])
        (self.profile / 'node_modules' / 'plugin-a' / 'index.js').write_bytes(b'new inactive code')
        with patch('launcher.subprocess.Popen') as spawn:
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        spawn.assert_called_once()
        with self.assertRaises(MenuError) as caught:
            self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        self.assertEqual(caught.exception.code, 'plugin_identity_changed')

    def test_configuration_only_rollback_rejects_asar_update(self):
        tracker = self.manager.tracking
        tracker.core.resources.mkdir(exist_ok=True)
        adapter.make_asar(tracker.core.archive)
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        from switch_plan import SwitchPlan
        planner = SwitchPlan(tracker)
        raw, obj, bundles = self.manager.manifest()
        plugins = self.manager.discover(obj, bundles)
        obj['dsh']['profile']['bundles'] = list(OFFICIAL)
        plan = planner.prepare([], plugins, raw, json.dumps(obj).encode(), True)
        self.assertTrue(plan['configurationOnly'])
        receipt = planner.execute(plan)
        tracker.core.archive.write_bytes(b'update during launch failure')
        manifest = self.path.read_bytes()
        with patch.object(planner, 'replay') as undo, self.assertRaises(PatchError) as caught:
            planner.rollback(receipt)
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        undo.assert_not_called()
        self.assertEqual(manifest, self.path.read_bytes())


class RecordingUpdateTests(unittest.TestCase):
    setUp = recording.GenericTrackingTests.setUp
    wait_job = recording.GenericTrackingTests.wait_job
    begin = recording.GenericTrackingTests.begin
    install_fixture = recording.GenericTrackingTests.install_fixture
    close_runtime = recording.GenericTrackingTests.close_runtime
    accept = recording.GenericTrackingTests.accept

    def test_asar_only_update_during_first_run_is_not_saved_as_plugin_patch(self):
        self.begin()
        self.install_fixture('plugin-a')
        tracker = self.manager.tracking
        baseline = tracker.load()['session']['baseline']
        self.archive.write_bytes(b'new official archive, unchanged executable')
        self.close_runtime()
        session = self.wait_job(allow_error=True)['session']
        self.assertIn('官方归档发生变化', session['error'])
        self.assertEqual(session['baseline'], baseline)
        self.assertNotIn('plugin-a', tracker.load()['records'])
        self.assertEqual(self.archive.read_bytes(), b'new official archive, unchanged executable')

    def test_host_update_cannot_abandon_recovery_session(self):
        self.begin()
        tracker = self.manager.tracking
        session = tracker.load()['session']
        self.exe.write_bytes(b'new executable')
        with self.assertRaises(PatchError) as caught:
            tracker.abandon()
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        self.assertEqual(tracker.load()['session'], session)

    def test_host_update_while_preparing_does_not_execute_installer(self):
        tracker = self.manager.tracking
        with patch.object(tracker, '_job'):
            tracker.begin('plugin-a', 'plugin-a@2.0')
        self.exe.write_bytes(b'updated before installer')
        with patch.object(tracker, 'install') as install, self.assertRaises(PatchError):
            tracker._prepare_install()
        install.assert_not_called()
        self.assertIsNotNone(tracker.load()['session'])

    def test_archive_update_before_baseline_is_not_adopted_even_if_mtime_matches(self):
        tracker = self.manager.tracking
        with patch.object(tracker, '_job'):
            tracker.begin('plugin-a', 'plugin-a@2.0')
        before = self.archive.stat()
        self.archive.write_bytes(self.archive.read_bytes().replace(b'0.2-test', b'0.3-test'))
        os.utime(self.archive, ns=(before.st_atime_ns, before.st_mtime_ns))
        with patch.object(tracker, 'install') as install, self.assertRaises(PatchError) as caught:
            tracker._prepare_install()
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        install.assert_not_called()
        self.assertIsNone(tracker.load()['session']['baseline'])

    def test_other_adapter_same_version_update_during_recording_is_rejected(self):
        self.begin()
        self.install_fixture('plugin-a')
        tracker = self.manager.tracking
        before = tracker.load()['session']
        (self.purge / 'index.js').write_bytes(b'updated adapter without version bump')
        self.close_runtime()
        session = self.wait_job(allow_error=True)['session']
        self.assertIn('插件或依赖内容与版本记录不同', session['error'])
        self.assertEqual(session['packagePins'], before['packagePins'])
        self.assertNotIn('plugin-a', tracker.load()['records'])

    def test_same_bytes_host_replacement_during_recording_can_complete(self):
        self.begin()
        self.install_fixture('plugin-a')
        for path in (self.exe, self.archive):
            replacement = path.with_suffix('.new')
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        self.accept()
        self.assertIn('plugin-a', self.manager.tracking.load()['records'])
        self.assertEqual(self.manager.tracking.load()['requiredFeatures'], [])

    def test_preparing_dependency_update_prevents_installer_and_keeps_pins(self):
        tracker = self.manager.tracking
        with patch.object(tracker, '_job'):
            tracker.begin('plugin-a', 'plugin-a@2.0')
        pins = tracker.load()['session']['packagePins']
        (self.purge / 'index.js').write_bytes(b'update between begin and preparation')
        with patch.object(tracker, 'install') as install, self.assertRaises(PatchError) as caught:
            tracker._prepare_install()
        self.assertEqual(caught.exception.code, 'plugin_identity_changed')
        install.assert_not_called()
        self.assertEqual(tracker.load()['session']['packagePins'], pins)

    def test_update_during_initial_package_capture_does_not_publish_session(self):
        tracker = self.manager.tracking
        capture = tracker.versions.package_snapshot
        def update(name, *args, **kwargs):
            reference = capture(name, *args, **kwargs)
            self.archive.write_bytes(b'official update during package capture')
            return reference
        with patch.object(tracker.versions, 'package_snapshot', side_effect=update), \
                patch.object(tracker, '_job') as job, self.assertRaises(PatchError) as caught:
            tracker.begin('plugin-a')
        self.assertEqual(caught.exception.code, 'host_identity_changed')
        job.assert_not_called()
        self.assertIsNone(tracker.load()['session'])
