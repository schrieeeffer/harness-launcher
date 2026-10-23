"""Offline migrations use synthetic installations and never modify user data."""
import copy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from launcher import Manager
from errors import PatchError, SchemaError
from migrate import migrate, inventory, marker, recover_cutover, rewrite_graph, upgrade_lock
from storage import write_json
from file_view import FileView
from switch_plan import SwitchPlan
import test_plugin_tracking as helpers
import test_version_plan as versions


class MigrationTests(unittest.TestCase):
    setUp = helpers.GenericTrackingTests.setUp
    wait_job = helpers.GenericTrackingTests.wait_job
    begin = helpers.GenericTrackingTests.begin
    install_fixture = helpers.GenericTrackingTests.install_fixture
    accept = helpers.GenericTrackingTests.accept
    close_runtime = helpers.GenericTrackingTests.close_runtime
    apply = helpers.GenericTrackingTests.apply
    record = versions.VersionPlanTests.record

    def old_data(self, native=False):
        self.record()
        self.apply([] if native else ['dsh-purge', 'plugin-a'])
        self.manager.scheme('work', ['dsh-purge', 'plugin-a'])
        return self.downgrade()

    def downgrade(self):
        tracker = self.manager.tracking
        state = tracker.core.load()
        new_revision = state['revision']
        state.update(format=2)
        state.pop('treeContentHash')
        for item in state['directories']:
            item.pop('contentHash')
        revision = tracker.read_object(new_revision)
        revision['patch'] = tracker.versions.adapter_patch(state)
        state['revision'] = tracker.store.object('revisions', revision)
        write_json(tracker.core.record, state)
        registry = rewrite_graph(tracker.store, tracker.load(), {new_revision: state['revision']})
        registry.update(format=2)
        write_json(tracker.index, registry)
        settings = copy.deepcopy(self.manager.settings)
        settings.pop('format'); settings.pop('requiredFeatures')
        write_json(self.manager.settings_file, settings)
        for path in (self.manager.data / 'backups').glob('*.json'):
            value = json.loads(path.read_bytes())
            value['format'] = 2
            write_json(path, value)
        return inventory(self.manager.data)

    def test_conversion_preserves_files_history_and_switches_both_directions(self):
        original = self.old_data(native=True)
        raw = self.manifest.read_bytes()
        before = SwitchPlan(self.manager.tracking)._signature(FileView(SwitchPlan(self.manager.tracking).roots()))
        result = migrate(self.manager.data, apply=True)
        self.assertEqual(result['status'], 'migrated')
        self.assertEqual(inventory(Path(result['archive'])), original)
        self.assertTrue(Path(result['archive'] + '.manifest.json').is_file())
        self.assertEqual(self.manifest.read_bytes(), raw)
        self.assertEqual(SwitchPlan(self.manager.tracking)._signature(FileView(SwitchPlan(self.manager.tracking).roots())), before)
        fresh = Manager(self.manager.data)
        self.assertEqual(fresh.tracking.load()['format'], 3)
        self.assertEqual(fresh.tracking.core.load()['format'], 3)
        self.assertEqual(migrate(self.manager.data, apply=True)['status'], 'already-current')
        fresh.apply(['dsh-purge', 'plugin-a'], fresh.state()['revision'])
        self.assertEqual((self.home / 'skills' / 'plugin-a.txt').read_bytes(), b'A')
        fresh.apply([], fresh.state()['revision'], native=True)
        self.assertTrue(fresh.state()['patchMode']['verifiedNative'])

    def test_preview_has_no_source_or_live_writes_and_runtime_rejects_v2(self):
        before = self.old_data()
        raw = self.manifest.read_bytes()
        with self.assertRaises(SchemaError):
            Manager(self.manager.data)
        result = migrate(self.manager.data)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(inventory(self.manager.data), before)
        self.assertEqual(self.manifest.read_bytes(), raw)
        self.assertFalse(list(self.manager.data.parent.glob(self.manager.data.name + '.stage-*')))

    def test_missing_or_active_source_is_not_adopted(self):
        missing = self.root / 'absent'
        with self.assertRaises(PatchError):
            migrate(missing, apply=True)
        self.assertFalse(missing.exists())
        self.old_data()
        (self.manager.data / 'instance.json').write_text('{}')
        with self.assertRaisesRegex(PatchError, '退出'):
            migrate(self.manager.data, apply=True)

    def test_incomplete_session_and_corrupted_blob_abort_before_cutover(self):
        self.old_data()
        index = self.manager.tracking.index
        value = json.loads(index.read_bytes())
        value['session'] = {'phase':'recording'}
        write_json(index, value)
        before = inventory(self.manager.data)
        with self.assertRaisesRegex(PatchError, '未完成'):
            migrate(self.manager.data, apply=True)
        self.assertEqual(inventory(self.manager.data), before)
        value['session'] = None
        write_json(index, value)
        record = value['records']['plugin-a']
        capsule = self.manager.tracking.store.read_object(record['package'])
        digest = capsule['packages']['0']['files']['index.js']['hash']
        (self.manager.tracking.root / 'blobs' / digest).write_bytes(b'broken')
        before = inventory(self.manager.data)
        with self.assertRaises(PatchError):
            migrate(self.manager.data, apply=True)
        self.assertEqual(inventory(self.manager.data), before)

    def test_directory_cutover_failure_restores_exact_original(self):
        before = self.old_data()
        import os
        replace = os.replace
        def fail(source, dest):
            if Path(source).is_dir() and '.stage-' in Path(source).name and Path(dest) == self.manager.data:
                raise OSError('synthetic cutover failure')
            return replace(source, dest)
        with patch('migrate.os.replace', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'synthetic'):
                migrate(self.manager.data, apply=True)
        self.assertEqual(inventory(self.manager.data), before)
        self.assertFalse(marker(self.manager.data).exists())

    def test_recovery_refuses_outside_paths_and_changed_sources(self):
        data = self.manager.data
        write_json(marker(data), {'data':str(data),'archive':str(self.root / 'outside'),
                                 'stage':str(data.with_name(data.name + '.stage-x'))})
        with self.assertRaises(PatchError):
            recover_cutover(data)
        with self.assertRaisesRegex(Exception, '迁移'):
            Manager(data)

    def test_pure_adapter_history_hydrates_verified_parked_file_bytes(self):
        self.apply(['dsh-purge'])
        self.manager.scheme('adapter', ['dsh-purge'])
        tracker = self.manager.tracking
        combo = tracker.read_object(tracker.load()['currentCombination'])
        app_hash = combo['state']['app|main.js']['hash']
        self.assertFalse((tracker.root / 'blobs' / app_hash).exists())
        original = self.downgrade()
        result = migrate(self.manager.data, apply=True)
        self.assertEqual(inventory(Path(result['archive'])), original)
        fresh = Manager(self.manager.data)
        self.assertEqual(fresh.tracking.blob_bytes(app_hash), (self.app / 'main.js').read_bytes())
        fresh.apply([], fresh.state()['revision'], native=True)
        fresh.apply(['dsh-purge'], fresh.state()['revision'])
        self.assertTrue(self.app.is_dir())

    def test_recovery_refuses_modified_archive_after_completed_swap(self):
        data = self.manager.data
        archive = data.with_name(data.name + '.archive-test')
        stage = data.with_name(data.name + '.stage-test')
        write_json(data / 'settings.json', {'format': 3})
        archive.mkdir()
        write_json(archive / 'settings.json', {'format': 2})
        old, new = inventory(archive), inventory(data)
        write_json(marker(data), {'format': 1, 'data': str(data), 'archive': str(archive),
                                 'stage': str(stage), 'old': old, 'new': new})
        (archive / 'settings.json').write_bytes(b'modified after cutover')
        with self.assertRaisesRegex(PatchError, '目录又发生变化'):
            recover_cutover(data)
        self.assertTrue(marker(data).exists())
        self.assertEqual(inventory(data), new)

    def test_completed_recovery_rebuilds_archive_proof_before_releasing_runtime(self):
        data = self.manager.data
        archive = data.with_name(data.name + '.archive-test')
        stage = data.with_name(data.name + '.stage-test')
        write_json(data / 'settings.json', {'format': 3})
        archive.mkdir()
        write_json(archive / 'settings.json', {'format': 2})
        old, new = inventory(archive), inventory(data)
        write_json(marker(data), {'format': 1, 'data': str(data), 'archive': str(archive),
                                 'stage': str(stage), 'old': old, 'new': new})
        recover_cutover(data)
        self.assertFalse(marker(data).exists())
        self.assertEqual(json.loads(Path(str(archive) + '.manifest.json').read_bytes()), old)

    def test_inconsistent_staging_copy_aborts_before_conversion(self):
        original = self.old_data()
        copytree = shutil.copytree
        def inconsistent(source, dest, *args, **kwargs):
            result = copytree(source, dest, *args, **kwargs)
            if Path(source) == self.manager.data / 'patches':
                (Path(dest) / 'unexpected-copy').write_bytes(b'transient source state')
            return result
        with patch('migrate.shutil.copytree', side_effect=inconsistent):
            with self.assertRaisesRegex(PatchError, '复制期间源记录改变'):
                migrate(self.manager.data, apply=True)
        self.assertEqual(inventory(self.manager.data), original)
        self.assertFalse(marker(self.manager.data).exists())
        self.assertFalse(list(self.manager.data.parent.glob(self.manager.data.name + '.stage-*')))

    def test_recovery_cannot_remove_marker_while_migration_owns_boundary(self):
        data = self.manager.data
        write_json(marker(data), {'sentinel': 'must remain untouched'})
        with upgrade_lock(data):
            with self.assertRaisesRegex(PatchError, '另一进程'):
                recover_cutover(data)
        self.assertEqual(json.loads(marker(data).read_bytes()), {'sentinel': 'must remain untouched'})

    def test_live_change_after_staging_inventory_aborts_cutover(self):
        original = self.old_data()
        original_inventory = inventory
        changed = False
        def mutate_after_stage(path):
            nonlocal changed
            result = original_inventory(path)
            if '.stage-' in Path(path).name and (Path(path) / 'migration-report.json').exists() and not changed:
                changed = True
                target = self.app / 'main.js'
                info = target.stat()
                target.write_bytes(b'X' * info.st_size)
                os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            return result
        with patch('migrate.inventory', side_effect=mutate_after_stage):
            with self.assertRaisesRegex(PatchError, '实际文件改变'):
                migrate(self.manager.data, apply=True)
        self.assertTrue(changed)
        self.assertEqual(inventory(self.manager.data), original)
        self.assertFalse(marker(self.manager.data).exists())

    def test_readonly_plans_reuse_hashes_but_cutover_verification_rehashes(self):
        self.apply(['dsh-purge'])
        self.manager.scheme('adapter', ['dsh-purge'])
        self.downgrade()
        from patch_modes import file_hash
        with patch('file_view.file_hash', wraps=file_hash) as read:
            result = migrate(self.manager.data)
        self.assertEqual(result['checkedCombinations'], [['dsh-purge'], []])
        captures = [call for call in read.call_args_list if call.args[0] == self.app / 'main.js']
        # One shared planning read, then independent post-plan and final-boundary
        # reads. Adding a second read-only combination must not reread all files.
        self.assertEqual(len(captures), 3)


if __name__ == '__main__':
    unittest.main()
