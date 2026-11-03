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
from operations import OperationLog, progress_workflow, report_progress
from catalog import CatalogError
from storage import ContentStore
from update_check import UpdateCheck, newer
import test_launcher as generic
import test_patch_modes as adapter


class UpdateTests(unittest.TestCase):
    def test_pnpm_github_shorthand_preserves_fixed_commit_for_revalidation(self):
        import base64
        from installer_policy import build_key
        service = UpdateCheck(None)
        source = 'github:owner/repo#' + 'a' * 40
        row = {'id': 'plugin-a', 'version': '1.2.0', 'source': source, 'suspended': 'new host'}
        meta = {'name': 'plugin-a', 'version': '1.2.0'}
        payload = {'content': base64.b64encode(json.dumps(meta).encode()).decode()}
        with patch.object(service.catalog, 'fetch', return_value=payload), patch.object(service, 'inventory', return_value=[row]):
            result, actual = service.target('plugin-a', source)
        self.assertEqual(result['status'], 'revalidate')
        self.assertEqual(result['target'], source)
        self.assertEqual(actual, meta)
        self.assertEqual(build_key('plugin-a', source),
                         'plugin-a@https://codeload.github.com/owner/repo/tar.gz/' + 'a' * 40)

    def test_git_retry_reuses_successful_metadata_but_execution_rechecks_target(self):
        import base64
        service = UpdateCheck(None)
        row = {'id': 'plugin-a', 'version': '1.0.0', 'source': 'git+https://github.com/owner/repo.git'}
        target = 'git+https://github.com/owner/repo.git#' + 'a' * 40
        payload = {'content': base64.b64encode(json.dumps({'name': 'plugin-a', 'version': '2.0.0'}).encode()).decode()}
        replies = [{'default_branch': 'main'}, {'sha': 'a' * 40}, CatalogError('temporary reset'), payload, payload]
        with patch.object(service.catalog, 'fetch', side_effect=replies) as fetch:
            self.assertEqual(service.one(row)['status'], 'error')
            self.assertEqual(service.one(row)['target'], target)
            with patch.object(service, 'inventory', return_value=[row]):
                service.target('plugin-a', target)
        urls = [call.args[0] for call in fetch.call_args_list]
        self.assertEqual(len(urls), 5)
        self.assertEqual(sum('/commits/' in url for url in urls), 1)
        self.assertEqual(sum('/contents/package.json' in url for url in urls), 3)

    def test_execution_reuses_discovery_cache_but_rechecks_exact_target(self):
        from types import SimpleNamespace
        from catalog import Catalog
        manager = SimpleNamespace(catalog=Catalog())
        service = UpdateCheck(manager)
        self.assertIs(service.catalog, manager.catalog)
        row = {'id': 'plugin-a', 'version': '1.0.0', 'source': '1.0.0'}
        meta = {'name': 'plugin-a', 'version': '2.0.0', 'dsh': {'bundle': {'patch': './patch.yml'}}}
        with patch.object(service.catalog, 'fetch', return_value=meta) as fetch:
            # Use manager=None here only to avoid requiring registry files in this fixture.
            service.manager = None
            service.one(row)
            with patch.object(service, 'inventory', return_value=[row]):
                service.target('plugin-a', 'plugin-a@2.0.0')
        self.assertEqual(len(fetch.call_args_list), 2)
        self.assertTrue(fetch.call_args_list[-1].args[0].endswith('/2.0.0'))

    def test_source_network_error_is_not_misreported_as_version_change(self):
        service = UpdateCheck(None)
        row = {'id': 'plugin-a', 'version': '1.0.0', 'source': '1.0.0'}
        with patch.object(service, 'inventory', return_value=[row]), \
                patch.object(service.catalog, 'fetch', side_effect=CatalogError('fixture network failure')):
            with self.assertRaisesRegex(CatalogError, 'fixture network failure'):
                service.target('plugin-a', 'plugin-a@2.0.0')
    def test_same_npm_version_can_be_revalidated_only_when_suspended(self):
        service = UpdateCheck(None)
        meta = {'name': 'plugin-a', 'version': '1.2.0', 'dsh': {'bundle': {'patch': './patch.yml'}}}
        row = {'id': 'plugin-a', 'version': '1.2.0', 'source': '^1.0.0', 'suspended': 'unverified host'}
        with patch.object(service.catalog, 'fetch', return_value=meta), patch.object(service, 'inventory', return_value=[row]):
            self.assertEqual(service.one(row)['status'], 'revalidate')
            self.assertEqual(service.target('plugin-a', 'plugin-a@1.2.0')[0]['latest'], '1.2.0')
            row['suspended'] = ''
            with self.assertRaises(CatalogError):
                service.target('plugin-a', 'plugin-a@1.2.0')

    def test_suspended_package_cannot_revalidate_from_stale_or_older_release(self):
        row = {'id': 'plugin-a', 'version': '1.2.0', 'source': '^1.0.0', 'suspended': 'unverified host'}
        for version, stale in [('1.2.0', True), ('1.1.0', False)]:
            service = UpdateCheck(None)
            meta = {'name': 'plugin-a', 'version': version, 'stale': stale, 'dsh': {'bundle': {'patch': './patch.yml'}}}
            with patch.object(service.catalog, 'cached', return_value=meta):
                self.assertNotEqual(service.one(row)['status'], 'revalidate')

    def test_pinned_suspended_git_revalidates_only_original_commit(self):
        import base64
        service = UpdateCheck(None)
        source = 'git+https://github.com/owner/repo.git#' + 'a' * 40
        row = {'id': 'plugin-a', 'version': '1.2.0', 'source': source, 'suspended': 'unverified host'}
        payload = {'content': base64.b64encode(json.dumps({'name': 'plugin-a', 'version': '1.2.0'}).encode()).decode()}
        with patch.object(service.catalog, 'fetch', return_value=payload) as fetch:
            result = service.one(row)
        self.assertEqual(result['status'], 'revalidate')
        self.assertEqual(result['target'], source)
        self.assertTrue(all(call.args[0].endswith('ref=' + 'a' * 40) for call in fetch.call_args_list))

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

    def test_runtime_link_is_a_leaf_and_rollback_never_changes_target(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        content = runtime / 'keep.txt'
        content.write_bytes(b'official runtime')
        checkpoint = CheckpointFiles({'files': self.files}, self.store, link_targets=(runtime,))
        before = checkpoint.snapshot()
        link = self.files / 'runtime'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(runtime), str(link))
        else:
            link.symlink_to(runtime, target_is_directory=True)
        after = checkpoint.snapshot()
        self.assertEqual(after['files|runtime']['type'], 'link')
        self.assertNotIn('files|runtime/keep.txt', after)
        with self.assertRaises(LauncherError):
            checkpoint.path('files|runtime/keep.txt')
        checkpoint.restore(before, after)
        self.assertFalse(link.exists())
        self.assertEqual(content.read_bytes(), b'official runtime')
        checkpoint.restore(after, before)
        self.assertEqual(checkpoint.changes(after, checkpoint.snapshot(False)), [])
        forged = copy.deepcopy(after)
        forged['files|runtime']['target'] = str(self.root / 'private')
        forged['files|runtime']['external'] = True
        with self.assertRaises(LauncherError):
            checkpoint.restore(forged, after)
        self.assertEqual(content.read_bytes(), b'official runtime')

    def test_runtime_link_with_unapproved_or_linked_target_is_rejected(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        link = self.files / 'runtime'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(runtime), str(link))
        else:
            link.symlink_to(runtime, target_is_directory=True)
        with self.assertRaises(LauncherError):
            self.checkpoint.snapshot()
        checkpoint = CheckpointFiles({'files': self.files}, self.store, link_targets=(link,))
        with self.assertRaises(LauncherError):
            checkpoint.checked_link('files|other', str(link / 'child'))

    def test_added_deleted_and_modified_files_recover(self):
        (self.files / 'original').write_bytes(b'old')
        (self.files / 'deleted').write_bytes(b'deleted')
        before = self.checkpoint.snapshot()
        (self.files / 'original').write_bytes(b'new')
        (self.files / 'deleted').unlink()
        (self.files / 'extra').mkdir()
        (self.files / 'extra' / 'new').write_bytes(b'new')
        after = self.checkpoint.snapshot()
        progress = []
        self.checkpoint.restore(before, after, progress=lambda *event: progress.append(event))
        self.assertFalse(self.checkpoint.changes(before, self.checkpoint.snapshot(False)))
        self.assertEqual(progress[-1], ('写回检查点文件', 4, 4))
        self.assertEqual({label for label, done, total in progress},
                         {'校验待恢复内容', '移除已记录的替换条目', '写回检查点文件'})

    def test_checkpoint_reports_actual_files_and_keeps_enclosing_phase(self):
        (self.files / 'one').write_bytes(b'one')
        (self.files / 'two').write_bytes(b'two')
        log = OperationLog()
        identity = log.begin('maintenance/execute')
        with log.reporting(identity), progress_workflow() as phase:
            phase('packages', '保存检查点')
            self.checkpoint.snapshot(progress=lambda count, total: report_progress('files', '保存文件', count, total))
            progress = log.snapshot()['current']['progress']
            self.assertEqual((progress['step'], progress['completed'], progress['total']), (2, 2, 2))
        with log.reporting(identity):
            report_progress('sync', '安装包')
        self.assertEqual(log.snapshot()['current']['progress']['step'], 4)

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

    def test_suspended_plugin_preview_does_not_include_another_adapters_files(self):
        tracker = self.manager.tracking
        value = tracker.load()
        record = value['records'].pop('plugin-a')
        value['order'].remove('plugin-a')
        value['suspended'] = {'plugin-a': 'host validation required'}
        value['suspendedPackages'] = {'plugin-a': record['package']}
        tracker.save(value)
        with patch('patch_modes.PatchModes.load', return_value={'files': [{'path': 'unrelated-adapter-file'}]}):
            preview = self.manager.maintenance.preview('plugin-a', 'uninstall')
        self.assertEqual(preview['files'], [])
        self.assertEqual(preview['blocked'], [])

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

    def test_gc_does_not_probe_blobs_for_unstored_official_hashes(self):
        hashes = {format(index, '064x') for index in range(1000, 2000)}
        self.manager.settings['officialFileHashes'] = sorted(hashes)
        original = ContentStore._path
        def checked(store, relative):
            if relative.startswith('blobs/') and relative[6:] in hashes:
                self.fail('An unstored inventory hash caused a filesystem probe')
            return original(store, relative)
        with patch.object(ContentStore, '_path', checked):
            CacheGC(self.manager).preview()

    def test_gc_does_not_sweep_objects_created_after_inventory(self):
        store = self.manager.tracking.store
        reference = store.object('transactions', {'referenced': True})
        self.manager.settings['extraReference'] = reference
        read = ContentStore.read_object
        created = []
        def create_during_walk(current, ref):
            if ref == reference and not created:
                key = store.put_blob(b'created after inventory')
                os.utime(store._path('blobs/' + key), (1, 1))
                created.append('blobs/' + key)
            return read(current, ref)
        with patch.object(ContentStore, 'read_object', create_during_walk):
            preview = CacheGC(self.manager).preview()
        self.assertTrue(created)
        self.assertNotIn(created[0], [item['path'] for item in preview['files']])
        self.assertIn(created[0], [item['path'] for item in CacheGC(self.manager).preview()['files']])

    def test_gc_missing_reference_still_blocks_cleanup(self):
        self.manager.settings['missingReference'] = 'transactions/' + 'f' * 64 + '.json'
        with self.assertRaisesRegex(LauncherError, '恢复对象缺失'):
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
        log = OperationLog()
        identity = log.begin('maintenance/execute')
        with log.reporting(identity), patch.object(log, 'progress', wraps=log.progress) as report:
            service = self.upgrade()
        stages = [call.args[1] for call in report.call_args_list]
        order = ['check', 'packages', 'files', 'sync', 'launch']
        self.assertEqual(list(dict.fromkeys(stages)), order)
        self.assertEqual([order.index(stage) for stage in stages], sorted(order.index(stage) for stage in stages))
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

    def _upgrade_external_link(self, fail=False):
        package = self.profile / 'node_modules/plugin-a'
        external = Path(self.temp.name).resolve() / 'upgrade-source'
        package.rename(external)
        # A real package manager may recursively clean a junction before its
        # final rename. The link must be absent before handing off to it.
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(external), str(package))
        else:
            package.symlink_to(external, target_is_directory=True)
        original = (external / 'package.json').read_bytes()
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'upgrade', 'plugin-a@2.0.0')
        def install(*args):
            self.assertFalse(package.exists())
            self.assertFalse(package.is_symlink() or package.is_junction())
            self.assertEqual((external / 'package.json').read_bytes(), original)
            package.mkdir()
            meta = json.loads(original)
            meta['version'] = '2.0.0'
            (package / 'package.json').write_text(json.dumps(meta))
            if fail:
                raise LauncherError('installer failed after publishing real directory')
        def launch(pending):
            pending['observed'] = True
            service.save(pending)
        with patch('maintenance.UpdateCheck.target', return_value=({'latest': '2.0.0'}, {})), \
                patch.object(service, 'package_command', side_effect=install), patch.object(service, '_launch', side_effect=launch):
            if fail:
                with self.assertRaisesRegex(LauncherError, 'installer failed'):
                    service.start('plugin-a', 'upgrade', preview['token'], 'plugin-a@2.0.0')
            else:
                service.start('plugin-a', 'upgrade', preview['token'], 'plugin-a@2.0.0')
        self.assertEqual((external / 'package.json').read_bytes(), original)
        if not fail:
            service.finish()
            identity = service.summary()['history'][0]['id']
        else:
            self.assertEqual(service.load()['phase'], 'failed')
            identity = None
        pending, recovery = service.recovery(identity)
        service.restore(recovery['token'], identity)
        self.assertEqual(package.resolve(), external)
        self.assertEqual((external / 'package.json').read_bytes(), original)
        # Model resuming after publishing the parent link but before marking
        # the transaction complete. Do not inspect deleted children via it.
        service.files().restore(service.snapshot(pending['before']), service.snapshot(pending['expected']))
        self.assertEqual((external / 'package.json').read_bytes(), original)

    def test_external_link_upgrade_detaches_before_installer_and_restores_link(self):
        self._upgrade_external_link()

    def test_failed_external_link_upgrade_restores_original_without_touching_source(self):
        self._upgrade_external_link(fail=True)


class PurgeLifecycleTests(unittest.TestCase):
    setUp = adapter.PatchModeTests.setUp
    switch = adapter.PatchModeTests.switch
    remove = LifecycleTests.remove

    def test_adapter_upgrade_records_profile_changes_and_restores_native(self):
        # Official updates move journals into a new generation while immutable
        # blobs remain shared; preview must read through the owning store.
        self.manager._generation_override = 'a' * 32
        self.switch([])
        service = self.manager.maintenance
        patch_path = self.profile / 'cordis.patch.yml'
        original = patch_path.read_bytes()
        native_archive = self.archive.read_bytes()
        native_cli = self.cli.read_bytes()
        preview = service.preview('dsh-purge', 'upgrade', 'dsh-purge@1.2.0')
        def install(*args):
            path = self.purge / 'package.json'
            meta = json.loads(path.read_bytes())
            meta['version'] = '1.2.0'
            path.write_text(json.dumps(meta))
            patch_path.write_bytes(b'- name: dsh-purge/profile-insert\n')
        def launch(pending):
            self.app.mkdir()
            (self.app / 'package.json').write_bytes(adapter.archive_file(self.archive, 'package.json', self.resources / 'app.asar.unpacked'))
            (self.app / 'main.js').write_bytes(b'new purge patch')
            runtime = self.resources / 'runtime'
            if os.name == 'nt':
                import _winapi
                _winapi.CreateJunction(str(runtime), str(self.app / 'runtime'))
            else:
                (self.app / 'runtime').symlink_to(runtime, target_is_directory=True)
            self.cli.with_name(self.cli.name + '.dsh-purge.bak').write_bytes(
                b'ELECTRON_RUN_AS_NODE dsh-desktop-host \\app.asar\\old-host.js')
            self.cli.write_bytes(native_cli.replace(b'\\app.asar\\', b'\\app\\'))
            pending['observed'] = True
            service.save(pending)
        with patch('maintenance.UpdateCheck.target', return_value=({'latest': '1.2.0'}, {})), \
                patch.object(service, 'package_command', side_effect=install), patch.object(service, '_launch', side_effect=launch):
            service.start('dsh-purge', 'upgrade', preview['token'], 'dsh-purge@1.2.0')
        service.finish()
        self.assertEqual(patch_path.read_bytes(), original)
        self.assertEqual(self.archive.read_bytes(), native_archive)
        self.assertFalse(self.app.exists())
        self.assertEqual(self.cli.read_bytes(), native_cli)
        self.switch(['dsh-purge'])
        self.assertIn(b'dsh-purge/profile-insert', patch_path.read_bytes())
        patched = patch_path.read_bytes()
        patch_path.write_bytes(patched + b'# later user edit\n')
        with self.assertRaises(LauncherError):
            self.switch([])
        self.assertEqual(patch_path.read_bytes(), patched + b'# later user edit\n')
        patch_path.write_bytes(patched)
        self.switch([])
        self.assertEqual(patch_path.read_bytes(), original)
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])

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
        with patch('maintenance.UpdateCheck.target', return_value=({'latest': '1.2.0'}, {'dshTarget': 'older-tested-host'})), \
                patch.object(service, 'package_command', side_effect=install), patch.object(service, '_launch', side_effect=launch):
            service.start('dsh-purge', 'upgrade', preview['token'], 'dsh-purge@1.2.0')
        service.finish()
        self.assertEqual(self.manager.tracking.core.load()['pluginVersion'], '1.2.0')
        identity = service.summary()['history'][0]['id']
        _, preview = service.recovery(identity)
        service.restore(preview['token'], identity)
        self.assertFalse(service.files().changes(before, service.files().snapshot(False)))

    def test_unverified_purge_can_install_but_missing_patch_cannot_finish_and_is_reversible(self):
        self.switch(['dsh-purge'])
        service = self.manager.maintenance
        before = service.files().snapshot()
        preview = service.preview('dsh-purge', 'upgrade', 'dsh-purge@2.0.0')
        def install(*args):
            path = self.purge / 'package.json'
            meta = json.loads(path.read_bytes())
            meta['version'] = '2.0.0'
            path.write_text(json.dumps(meta))
        def launch(pending):
            pending['observed'] = True
            service.save(pending)
        with patch('maintenance.UpdateCheck.target', return_value=({'latest': '2.0.0'}, {'dshTarget': 'new-host'})), \
                patch.object(service, 'package_command', side_effect=install), patch.object(service, '_launch', side_effect=launch):
            service.start('dsh-purge', 'upgrade', preview['token'], 'dsh-purge@2.0.0')
        self.assertEqual(service.load()['phase'], 'waiting')
        self.assertIsNone(self.manager.tracking.core.load())
        with self.assertRaisesRegex(LauncherError, '尚无已应用'):
            service.finish()
        self.assertEqual(service.load()['phase'], 'failed')
        _, recovery = service.recovery()
        service.restore(recovery['token'])
        self.assertFalse(service.files().changes(before, service.files().snapshot(False)))
