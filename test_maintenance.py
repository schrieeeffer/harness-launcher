import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cache_gc import CacheGC
from errors import LauncherError
from maintenance_files import CheckpointFiles
from storage import ContentStore
from update_check import UpdateCheck, newer
import test_launcher as generic
import test_patch_modes as adapter


class UpdateTests(unittest.TestCase):
    def test_semver_numeric_and_prerelease_precedence(self):
        self.assertTrue(newer('1.10.0', '1.9.9'))
        self.assertTrue(newer('1.0.0-beta.10', '1.0.0-beta.2'))
        self.assertTrue(newer('1.0.0', '1.0.0-rc.3'))
        self.assertFalse(newer('1.0.0+build2', '1.0.0+build1'))
        self.assertFalse(newer('1.0.0-beta', '1.0.0'))

    def test_foreign_source_does_not_become_same_named_npm_package(self):
        service = UpdateCheck(None)
        for source in ('https://example.org/a.tgz', 'npm:other@1', 'workspace:*'):
            with patch.object(service.catalog, 'fetch') as fetch:
                row = service.one({'id': 'plugin-a', 'version': '1.0.0', 'source': source})
                self.assertEqual(row['status'], 'manual')
                fetch.assert_not_called()

    def test_exact_git_pin_does_not_follow_default_branch(self):
        service = UpdateCheck(None)
        row = service.one({'id': 'plugin-a', 'version': '1.0.0',
                          'source': 'git+https://github.com/owner/repo.git#' + 'a' * 40})
        self.assertIsNone(row['target'])

    def test_registry_name_mismatch_is_error(self):
        service = UpdateCheck(None)
        with patch.object(service.catalog, 'fetch', return_value={'name': 'other', 'version': '2.0.0'}):
            row = service.one({'id': 'plugin-a', 'version': '1.0.0', 'source': '^1.0.0'})
        self.assertEqual(row['status'], 'error')

    def test_latest_target_is_exact_and_not_floating(self):
        service = UpdateCheck(None)
        meta = {'name': 'plugin-a', 'version': '1.2.0', 'dsh': {'bundle': {'patch': './patch.yml'}}}
        with patch.object(service.catalog, 'fetch', return_value=meta):
            row = service.one({'id': 'plugin-a', 'version': '1.0.0', 'source': '^1.0.0'})
        self.assertEqual(row['target'], 'plugin-a@1.2.0')
        self.assertEqual(row['status'], 'available')

    def test_private_registry_never_leaks_name_to_public_registry(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / '.npmrc').write_text('registry=https://private.example.invalid/\n')
            service = UpdateCheck(SimpleNamespace(profile=profile))
            with patch.object(service.catalog, 'fetch') as fetch:
                row = service.one({'id': 'private-package', 'version': '1.0.0', 'source': '^1'})
            self.assertEqual(row['status'], 'manual')
            fetch.assert_not_called()

    def test_local_purge_can_explicitly_change_to_verified_repository_npm_source(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            meta = {'name': 'dsh-purge', 'version': '1.2.0', 'repository': {'url': 'git+https://github.com/owner/repo.git'},
                    'dsh': {'bundle': {'patch': './patch.yml'}}}
            (profile / 'package.json').write_text(json.dumps(meta))
            service = UpdateCheck(SimpleNamespace(profile=profile))
            with patch.object(service.catalog, 'fetch', return_value=meta):
                row = service.one({'id': 'dsh-purge', 'version': '1.0.0', 'source': 'file:local', 'path': directory})
            self.assertEqual(row['status'], 'source-change')
            self.assertEqual(row['target'], 'dsh-purge@1.2.0')


class FileCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.files = self.root / 'files'
        self.files.mkdir()
        self.store = ContentStore(self.root / 'objects')
        self.checkpoint = CheckpointFiles({'files': self.files}, self.store)

    def test_added_deleted_and_modified_files_recover(self):
        (self.files / 'original').write_bytes(b'old')
        (self.files / 'deleted').write_bytes(b'deleted')
        before = self.checkpoint.snapshot()
        (self.files / 'original').write_bytes(b'new')
        (self.files / 'deleted').unlink()
        (self.files / 'extra').mkdir()
        (self.files / 'extra' / 'new').write_bytes(b'new')
        after = self.checkpoint.snapshot()
        self.checkpoint.restore(before, after)
        self.assertFalse(self.checkpoint.changes(before, self.checkpoint.snapshot(False)))

    def test_later_user_edit_blocks_before_any_write(self):
        (self.files / 'a').write_bytes(b'original')
        before = self.checkpoint.snapshot()
        (self.files / 'a').write_bytes(b'plugin')
        after = self.checkpoint.snapshot()
        (self.files / 'a').write_bytes(b'user edit')
        with self.assertRaises(LauncherError):
            self.checkpoint.restore(before, after)
        self.assertEqual((self.files / 'a').read_bytes(), b'user edit')

    def test_unknown_new_file_is_never_deleted_recursively(self):
        before = self.checkpoint.snapshot()
        (self.files / 'new').mkdir()
        after = self.checkpoint.snapshot()
        (self.files / 'new' / 'user-data').write_bytes(b'keep')
        with self.assertRaises(OSError):
            self.checkpoint.restore(before, after)
        self.assertEqual((self.files / 'new' / 'user-data').read_bytes(), b'keep')

    def test_path_traversal_rejected(self):
        for key in ('files|../outside', 'elsewhere|.', 'files|C:/other'):
            with self.subTest(key=key), self.assertRaises((LauncherError, ValueError)):
                self.checkpoint.path(key)

    def test_interrupted_replay_is_idempotent(self):
        (self.files / 'a').write_bytes(b'old')
        before = self.checkpoint.snapshot()
        (self.files / 'a').write_bytes(b'new')
        after = self.checkpoint.snapshot()
        self.checkpoint.restore(before, after)
        self.checkpoint.restore(before, after)
        self.assertEqual((self.files / 'a').read_bytes(), b'old')


class LifecycleTests(unittest.TestCase):
    setUp = generic.ManagerTests.setUp

    def remove(self, action, source, identity):
        self.assertEqual(action, 'remove')
        package = self.profile / 'node_modules' / source
        for item in sorted(package.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            item.rmdir() if item.is_dir() else item.unlink()
        package.rmdir()
        manifest = json.loads(self.path.read_bytes())
        manifest['dependencies'].pop(source, None)
        manifest['dsh']['profile']['bundles'] = [x for x in manifest['dsh']['profile']['bundles'] if x != source]
        self.path.write_text(json.dumps(manifest))

    def uninstall(self, name='plugin-a'):
        service = self.manager.maintenance
        preview = service.preview(name, 'uninstall')
        with patch.object(service, 'package_command', side_effect=self.remove):
            service.start(name, 'uninstall', preview['token'])
        return service

    def test_uninstall_and_recover_preserve_user_data_and_other_packages(self):
        conversation = self.home / 'private-conversation.json'
        conversation.write_bytes(b'never touch')
        other = self.profile / 'node_modules/@demo/plugin-b/package.json'
        other_bytes = other.read_bytes()
        before = self.manager.maintenance.files().snapshot()
        service = self.uninstall()
        self.assertNotIn('plugin-a', self.manager.tracking.load()['records'])
        self.assertEqual(other.read_bytes(), other_bytes)
        history = service.summary()['history'][0]['id']
        _, preview = service.recovery(history)
        service.restore(preview['token'], history)
        self.assertEqual(conversation.read_bytes(), b'never touch')
        self.assertFalse(service.files().changes(before, service.files().snapshot(False)))
        self.assertIn('plugin-a', self.manager.tracking.load()['records'])

    def test_stale_preview_cannot_uninstall(self):
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'uninstall')
        self.manager.scheme('new', [])
        with patch.object(service, 'package_command') as command, self.assertRaises(LauncherError):
            service.start('plugin-a', 'uninstall', preview['token'])
        command.assert_not_called()

    def test_modified_disabled_package_is_not_removed(self):
        self.manager.apply([], self.manager.state()['revision'])
        extra = self.profile / 'node_modules/plugin-a/user-note.txt'
        extra.write_text('keep this user file')
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'uninstall')
        with patch.object(service, 'package_command') as command, self.assertRaises(LauncherError):
            service.start('plugin-a', 'uninstall', preview['token'])
        command.assert_not_called()
        self.assertEqual(extra.read_text(), 'keep this user file')

    def test_shared_direct_dependency_blocks_removal(self):
        meta_path = self.profile / 'node_modules/@demo/plugin-b/package.json'
        meta = json.loads(meta_path.read_bytes())
        meta['dependencies'] = {'plugin-a': '*'}
        meta_path.write_text(json.dumps(meta))
        preview = self.manager.maintenance.preview('plugin-a', 'uninstall')
        self.assertTrue(any('依赖此包' in item for item in preview['blocked']))

    def test_failed_installer_keeps_a_recoverable_checkpoint(self):
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'uninstall')
        def fail(*args):
            self.remove(*args)
            raise LauncherError('simulated installer failure')
        with patch.object(service, 'package_command', side_effect=fail), self.assertRaises(LauncherError):
            service.start('plugin-a', 'uninstall', preview['token'])
        self.assertEqual(service.load()['phase'], 'failed')
        with self.assertRaises(LauncherError):
            self.manager.apply([], self.manager.state()['revision'])
        _, recovery = service.recovery()
        service.restore(recovery['token'])
        self.assertTrue((self.profile / 'node_modules/plugin-a/package.json').exists())

    def test_host_update_after_uninstall_blocks_old_restore(self):
        service = self.uninstall()
        self.exe.write_bytes(b'new official host')
        with self.assertRaises(LauncherError) as caught:
            service.recovery(service.summary()['history'][0]['id'])
        self.assertEqual(caught.exception.code, 'host_identity_changed')

    def test_recovery_does_not_overwrite_later_user_edits(self):
        service = self.uninstall()
        (self.home / 'AGENTS.md').write_bytes(b'user-created instructions')
        with self.assertRaises(LauncherError) as caught:
            service.recovery(service.summary()['history'][0]['id'])
        self.assertEqual(caught.exception.code, 'maintenance_conflict')

    def test_gc_removes_only_unreferenced_old_objects(self):
        tracker = self.manager.tracking
        orphan = tracker.store.put_blob(b'orphan')
        path = tracker.store._path('blobs/' + orphan)
        os.utime(path, (1, 1))
        referenced = tracker.store.put_blob(b'referenced')
        obj = tracker.store.object('transactions', {'hash': referenced})
        history = self.manager.maintenance.folder / 'history/test.json'
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps({'checkpoint': obj}))
        os.utime(tracker.store._path('blobs/' + referenced), (1, 1))
        gc = CacheGC(self.manager)
        preview = gc.preview()
        self.assertTrue(any(x['path'] == 'blobs/' + orphan for x in preview['files']))
        gc.collect(preview['token'])
        self.assertFalse(path.exists())
        self.assertEqual(tracker.store.blob_bytes(referenced), b'referenced')

    def test_gc_rechecks_roots_before_deleting(self):
        gc = CacheGC(self.manager)
        preview = gc.preview()
        self.manager.scheme('new', [])
        with self.assertRaises(LauncherError):
            gc.collect(preview['token'])

    def test_gc_refuses_while_installation_pending(self):
        service = self.manager.maintenance
        service.save({'format': 1, 'id': 'a' * 32, 'phase': 'installing'})
        with self.assertRaises(LauncherError):
            CacheGC(self.manager).preview()

    def upgrade(self, corrupt_other=False):
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'upgrade', 'plugin-a@2.0.0')
        def install(*args):
            path = self.profile / 'node_modules/plugin-a/package.json'
            meta = json.loads(path.read_bytes())
            meta['version'] = '2.0.0'
            path.write_text(json.dumps(meta))
            if corrupt_other:
                (self.profile / 'node_modules/@demo/plugin-b/new-code.js').write_bytes(b'changed shared dependency')
        def launch(pending):
            pending['observed'] = True
            service.save(pending)
        with patch('maintenance.UpdateCheck.target', return_value=({'latest': '2.0.0'}, {})), \
                patch.object(service, 'package_command', side_effect=install), patch.object(service, '_launch', side_effect=launch):
            service.start('plugin-a', 'upgrade', preview['token'], 'plugin-a@2.0.0')
        return service

    def test_upgrade_records_first_run_and_can_restore_old_version(self):
        service = self.upgrade()
        (self.home / 'rules').mkdir()
        (self.home / 'rules/plugin-a.md').write_bytes(b'new plugin instructions')
        service.finish()
        self.assertEqual(self.manager.tracking.load()['records']['plugin-a']['version'], '2.0.0')
        identity = service.summary()['history'][0]['id']
        _, preview = service.recovery(identity)
        service.restore(preview['token'], identity)
        self.assertEqual(json.loads((self.profile / 'node_modules/plugin-a/package.json').read_bytes())['version'], '1.2')
        self.assertFalse((self.home / 'rules/plugin-a.md').exists())

    def test_upgrade_changing_another_package_stops_and_can_recover(self):
        with self.assertRaises(LauncherError):
            self.upgrade(corrupt_other=True)
        service = self.manager.maintenance
        _, preview = service.recovery()
        service.restore(preview['token'])
        self.assertFalse((self.profile / 'node_modules/@demo/plugin-b/new-code.js').exists())

    def test_unsealed_first_run_edit_is_not_silently_adopted_for_rollback(self):
        service = self.upgrade()
        (self.home / 'AGENTS.md').write_bytes(b'user edit during first run')
        with self.assertRaises(LauncherError) as caught:
            service.recovery()
        self.assertEqual(caught.exception.code, 'maintenance_conflict')

    def test_upgrade_requires_observed_first_run(self):
        service = self.upgrade()
        pending = service.load()
        pending['observed'] = False
        service.save(pending)
        with self.assertRaises(LauncherError):
            service.finish()

    def test_interrupted_restore_can_resume(self):
        service = self.uninstall()
        identity = service.summary()['history'][0]['id']
        pending, preview = service.recovery(identity)
        pending['phase'] = 'restoring'
        service.save(pending)
        # Model a crash after one of the checkpoint's exact file writes.
        expected = service.snapshot(pending['expected'])
        before = service.snapshot(pending['before'])
        key = 'metadata0|.'
        self.path.write_bytes(service.tracker.store.blob_bytes(before[key]['hash']))
        _, preview = service.recovery()
        service.restore(preview['token'])
        self.assertTrue((self.profile / 'node_modules/plugin-a/package.json').exists())

    def test_external_local_source_is_never_deleted_and_link_can_be_restored(self):
        package = self.profile / 'node_modules/plugin-a'
        external = Path(self.temp.name).resolve() / 'local-source'
        self.assertTrue(external.is_relative_to(Path(self.temp.name).resolve()))
        package.rename(external)
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(external), str(package))
        else:
            package.symlink_to(external, target_is_directory=True)
        service = self.manager.maintenance
        original = (external / 'package.json').read_bytes()
        def unlink_package(action, name, identity):
            package.rmdir() if package.is_junction() else package.unlink()
            manifest = json.loads(self.path.read_bytes())
            manifest['dependencies'].pop(name)
            self.path.write_text(json.dumps(manifest))
        preview = service.preview('plugin-a', 'uninstall')
        self.assertFalse(preview['blocked'])
        with patch.object(service, 'package_command', side_effect=unlink_package):
            service.start('plugin-a', 'uninstall', preview['token'])
        self.assertEqual((external / 'package.json').read_bytes(), original)
        identity = service.summary()['history'][0]['id']
        _, preview = service.recovery(identity)
        service.restore(preview['token'], identity)
        self.assertEqual(package.resolve(), external)
        self.assertEqual((external / 'package.json').read_bytes(), original)


class PurgeLifecycleTests(unittest.TestCase):
    setUp = adapter.PatchModeTests.setUp
    switch = adapter.PatchModeTests.switch
    remove = LifecycleTests.remove

    def test_disabled_purge_parked_user_file_is_preserved(self):
        self.switch(['dsh-purge'])
        self.switch([])
        extra = self.manager.tracking.core.stash / 'app/user-note.txt'
        extra.write_text('keep parked user file')
        service = self.manager.maintenance
        preview = service.preview('dsh-purge', 'uninstall')
        with patch.object(service, 'package_command') as command, self.assertRaises(LauncherError):
            service.start('dsh-purge', 'uninstall', preview['token'])
        command.assert_not_called()
        self.assertEqual(extra.read_text(), 'keep parked user file')

    def test_purge_uninstall_restores_user_rules_and_is_reversible(self):
        self.path = self.manifest
        self.switch(['dsh-purge'])
        service = self.manager.maintenance
        before = service.files().snapshot()
        preview = service.preview('dsh-purge', 'uninstall')
        with patch.object(service, 'package_command', side_effect=self.remove):
            service.start('dsh-purge', 'uninstall', preview['token'])
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'original user instruction')
        self.assertIsNone(self.manager.tracking.core.load())
        identity = service.summary()['history'][0]['id']
        _, preview = service.recovery(identity)
        service.restore(preview['token'], identity)
        self.assertFalse(service.files().changes(before, service.files().snapshot(False)))

    def test_purge_edited_user_rule_blocks_uninstall(self):
        self.switch(['dsh-purge'])
        (self.home / 'AGENTS.md').write_bytes(b'edited by user')
        service = self.manager.maintenance
        preview = service.preview('dsh-purge', 'uninstall')
        with patch.object(service, 'package_command') as command, self.assertRaises(LauncherError):
            service.start('dsh-purge', 'uninstall', preview['token'])
        command.assert_not_called()

    def test_purge_upgrade_recaptures_adapter_and_rolls_back(self):
        self.switch(['dsh-purge'])
        service = self.manager.maintenance
        before = service.files().snapshot()
        preview = service.preview('dsh-purge', 'upgrade', 'dsh-purge@1.2.0')
        def install(*args):
            path = self.purge / 'package.json'
            meta = json.loads(path.read_bytes())
            meta['version'] = '1.2.0'
            path.write_text(json.dumps(meta))
        def launch(pending):
            self.app.mkdir()
            (self.app / 'package.json').write_bytes(adapter.archive_file(self.archive, 'package.json', self.resources / 'app.asar.unpacked'))
            (self.app / 'main.js').write_bytes(b'new purge patch')
            (self.home / 'AGENTS.md').write_bytes(b'Little Code Sauce new rule')
            pending['observed'] = True
            service.save(pending)
        with patch('maintenance.UpdateCheck.target', return_value=({'latest': '1.2.0'}, {'dshTarget': '0.2-test'})), \
                patch.object(service, 'package_command', side_effect=install), patch.object(service, '_launch', side_effect=launch):
            service.start('dsh-purge', 'upgrade', preview['token'], 'dsh-purge@1.2.0')
        service.finish()
        self.assertEqual(self.manager.tracking.core.load()['pluginVersion'], '1.2.0')
        identity = service.summary()['history'][0]['id']
        _, preview = service.recovery(identity)
        service.restore(preview['token'], identity)
        self.assertFalse(service.files().changes(before, service.files().snapshot(False)))

    def test_incompatible_purge_is_rejected_before_checkpoint(self):
        self.switch(['dsh-purge'])
        service = self.manager.maintenance
        preview = service.preview('dsh-purge', 'upgrade', 'dsh-purge@2.0.0')
        with patch('maintenance.UpdateCheck.target', return_value=({'latest': '2.0.0'}, {'dshTarget': 'new-host'})), \
                patch.object(service, 'capture') as capture, self.assertRaises(LauncherError):
            service.start('dsh-purge', 'upgrade', preview['token'], 'dsh-purge@2.0.0')
        capture.assert_not_called()
