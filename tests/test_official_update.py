"""Synthetic upgrades use real planners/files, but never run an installer."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch

from harness_launcher.domain.errors import LauncherError
from harness_launcher.app.launcher import Manager, OFFICIAL
from harness_launcher.host.official_update import compatible
from harness_launcher.storage.content_store import hash_file
from tests import test_patch_modes as fixtures
from harness_launcher.host import runtime_repair as repair


class OfficialUpdateTests(unittest.TestCase):
    def setUp(self):
        fixtures.PatchModeTests.setUp(self)
        self.manager.apply([], self.manager.state()['revision'], native=True)
        self.old_tracker = self.manager.tracking
        self.old_controls = self.manager.official_host.controls()
        self.old_exe, self.old_archive = self.exe.read_bytes(), self.archive.read_bytes()
        self.payload = self.root / 'official-payload'
        self.exe.write_bytes(b'official host 0.3.0')
        fixtures.make_asar(self.archive, '0.3.0')
        self.sizes = {}
        for path in (self.exe, self.archive, *(self.resources / 'runtime' / n for n in repair.ANCHORS)):
            name = path.relative_to(self.exe.parent).as_posix()
            dest = self.payload / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            self.sizes[name] = path.stat().st_size
        self.installer = self.root / 'official-update.exe'
        self.installer.write_bytes(b'signed installer 0.3.0')
        self.checksum = base64.b64encode(hashlib.sha512(self.installer.read_bytes()).digest()).decode()
        sig = patch('harness_launcher.host.runtime_repair.signature', return_value={'version': '0.3.0', 'publisher': repair.PUBLISHER, 'status': 'Valid'})
        self.signature = sig.start()
        self.addCleanup(sig.stop)
        ziptool = patch('harness_launcher.host.runtime_repair.SevenZip')
        self.extractor = ziptool.start().return_value
        self.addCleanup(ziptool.stop)
        def extract(source, stage):
            shutil.copytree(self.payload, stage / 'payload')
            return stage / 'payload', self.sizes
        self.extractor.extract_all.side_effect = extract
        self.extractor.extract_members.side_effect = lambda source, stage, names: extract(source, stage)[0]
        self.service = self.manager.official_update

    def reconcile(self):
        return self.service.reconcile(str(self.installer), self.checksum)

    def original(self):
        self.exe.write_bytes(self.old_exe)
        self.archive.write_bytes(self.old_archive)

    def updated(self):
        for name in self.sizes:
            shutil.copyfile(self.payload / name, self.exe.parent / name)

    def cache(self):
        pending = self.root / 'local-app-data/@deepseek-aidsh-desktop-updater/pending'
        pending.mkdir(parents=True)
        name = 'deepseek-harness-0.3.0-win-x64.exe'
        shutil.copyfile(self.installer, pending / name)
        (pending / 'update-info.json').write_text(json.dumps({'fileName': name, 'sha512': self.checksum}))

    def record_plugin(self, requirement='>=0.2.0 <1.0.0', changes=None, enabled=True):
        self.original()
        folder = self.profile / 'node_modules/plugin-a'
        folder.mkdir()
        (folder / 'package.json').write_text(json.dumps({'name': 'plugin-a', 'version': '1.0.0',
            'engines': {'dsh': requirement}, 'dsh': {'bundle': {'patch': './patch.yml'}}}))
        (folder / 'patch.yml').write_bytes(b'[]')
        obj = json.loads(self.manifest.read_bytes())
        obj['dependencies']['plugin-a'] = 'file:plugin-a'
        self.manifest.write_text(json.dumps(obj))
        tracker = self.manager.tracking
        state = tracker.load()
        snapshot = tracker.object('snapshots', tracker.snapshot())
        record = {'version': '1.0.0', 'enabled': False, 'delta': tracker.object('deltas', {'changes': changes or []}),
                  'count': len(changes or []), 'baseSelection': [], 'baseVersions': {}, 'history': [],
                  'hostHash': hash_file(self.exe), 'source': 'file:plugin-a', 'baseline': snapshot,
                  'after': snapshot, 'manifestBefore': tracker.store.put_blob(self.manifest.read_bytes())}
        record['package'] = tracker.versions.package_snapshot('plugin-a', record['source'])
        record['revision'] = tracker.versions.revision('plugin-a', record, record['package'], record['source'])
        state['records']['plugin-a'] = record
        state['order'].append('plugin-a')
        tracker.save(state)
        self.manager.apply(['plugin-a'] if enabled else [], self.manager.state()['revision'])
        self.updated()

    def test_external_native_update_creates_generation_and_daily_launch_does_not_extract(self):
        before = self.old_tracker.index.read_bytes()
        result = self.reconcile()
        self.assertIn('dsh-purge', result['suspended'])
        self.assertEqual(self.old_tracker.index.read_bytes(), before)
        self.assertNotEqual(self.old_tracker.root, self.manager.tracking.root)
        self.assertEqual(self.old_tracker.store.root, self.manager.tracking.store.root)
        self.assertEqual(self.manager.state()['hostHealth']['status'], 'ready')
        reopened = Manager(self.manager.data, self.home, self.exe)
        with patch('harness_launcher.plugins.installer_policy.ensure_adapter_isolation'), patch('harness_launcher.app.launcher.subprocess.Popen') as spawn:
            for _ in range(2):
                reopened.apply([], reopened.state()['revision'], launch=True)
        self.assertEqual(spawn.call_count, 2)
        self.extractor.extract_all.assert_called_once()
        self.signature.assert_called_once()

    def test_explicit_launch_auto_adopts_cached_external_update(self):
        self.cache()
        with patch('harness_launcher.plugins.installer_policy.ensure_adapter_isolation'), patch('harness_launcher.app.launcher.subprocess.Popen') as spawn:
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        spawn.assert_called_once()
        self.assertNotEqual(self.old_tracker.root, self.manager.tracking.root)

    def test_polling_does_not_verify_extract_or_change_generation(self):
        self.cache()
        for _ in range(3):
            self.assertEqual(self.manager.state()['hostHealth']['status'], 'changed')
        self.signature.assert_not_called()
        self.assertEqual(self.manager.tracking.root, self.old_tracker.root)

    def test_install_after_native_only_approval_adopts_before_creating_snapshot(self):
        self.cache()
        plan = self.manager.official_host.prepare(str(self.installer), self.checksum)
        self.manager.official_host.accept(plan['token'])
        self.assertEqual(self.manager.state()['hostHealth']['status'], 'native_ready')
        old_registry = self.old_tracker.index.read_bytes()
        with patch('harness_launcher.plugins.plugin_tracking.PluginTracking._job') as job, patch('harness_launcher.plugins.installer_policy.ensure_adapter_isolation'), patch('harness_launcher.app.launcher.subprocess.Popen') as spawn:
            result = self.manager.begin_tracking('@author/plugin-new', '@author/plugin-new@3.1.6')
        job.assert_called_once()
        spawn.assert_not_called()
        self.assertIn('officialUpdate', result)
        self.assertNotEqual(self.old_tracker.root, self.manager.tracking.root)
        self.assertEqual(self.old_tracker.index.read_bytes(), old_registry)
        session = self.manager.tracking.load()['session']
        self.assertEqual(session['plugin'], '@author/plugin-new')
        self.assertEqual(session['source'], '@author/plugin-new@3.1.6')
        self.assertEqual(session['hostHash'], hash_file(self.exe))
        self.extractor.extract_all.assert_called_once()

    def test_install_adoption_conflict_never_begins_recording_or_installer(self):
        self.cache()
        self.exe.write_bytes(b'unknown executable')
        with patch('harness_launcher.plugins.plugin_tracking.PluginTracking._job') as job, self.assertRaises(LauncherError):
            self.manager.begin_tracking('plugin-new', 'plugin-new@1.0.0')
        job.assert_not_called()
        self.assertIsNone(self.old_tracker.load()['session'])
        self.assertEqual(self.manager.tracking.root, self.old_tracker.root)

    def test_install_invalid_input_or_active_job_cannot_trigger_host_adoption(self):
        with patch.object(self.service, 'before_launch') as adopt:
            for name, source in [('bad name', ''), ('plugin-new', '--unsafe'), ('plugin-new', 'https://user:secret@example.com/a')]:
                with self.assertRaises(LauncherError):
                    self.manager.begin_tracking(name, source)
            self.old_tracker.busy = True
            try:
                with self.assertRaises(LauncherError):
                    self.manager.begin_tracking('plugin-new', 'plugin-new@1.0.0')
            finally:
                self.old_tracker.busy = False
        adopt.assert_not_called()

    def test_missing_runtime_is_repaired_from_same_extraction_and_user_data_untouched(self):
        missing = self.resources / 'runtime' / repair.ANCHORS[1]
        missing.unlink()
        user = self.home / 'sessions/test.txt'
        user.parent.mkdir()
        user.write_bytes(b'private user data')
        result = self.reconcile()
        self.assertTrue(missing.is_file())
        self.assertEqual(user.read_bytes(), b'private user data')
        self.extractor.extract_all.assert_called_once()
        self.extractor.extract_members.assert_not_called()
        self.assertEqual(result['version'], '0.3.0')

    def test_unknown_host_conflict_prevents_even_missing_runtime_recovery(self):
        missing = self.resources / 'runtime' / repair.ANCHORS[1]
        missing.unlink()
        self.archive.write_bytes(b'unknown host edit')
        with self.assertRaisesRegex(LauncherError, '冲突'):
            self.reconcile()
        self.assertFalse(missing.exists())
        self.assertEqual(self.archive.read_bytes(), b'unknown host edit')
        self.assertEqual(self.manager.tracking.root, self.old_tracker.root)

    def test_user_edit_and_active_purge_are_not_silently_accepted(self):
        user = self.home / 'AGENTS.md'
        user.write_bytes(b'user modification')
        with self.assertRaisesRegex(LauncherError, '用户侧'):
            self.reconcile()
        self.signature.assert_not_called()
        self.assertEqual(user.read_bytes(), b'user modification')

    def test_compatible_bundle_restored_and_switchable_without_old_file_replay(self):
        self.record_plugin()
        old_revision = self.old_tracker.load()['records']['plugin-a']['revision']
        result = self.reconcile()
        self.assertEqual(result['restored'], ['plugin-a'])
        record = self.manager.tracking.load()['records']['plugin-a']
        self.assertEqual(self.manager.tracking.read_object(record['revision'])['parent'], old_revision)
        self.assertEqual(self.old_tracker.load()['records']['plugin-a']['revision'], old_revision)
        for selection in ([], ['plugin-a'], []):
            self.manager.apply(selection, self.manager.state()['revision'])
        with self.assertRaises(LauncherError):
            self.manager.apply(['dsh-purge'], self.manager.state()['revision'])

    def test_incompatible_or_modified_plugin_is_suspended_without_deletion(self):
        self.record_plugin(requirement='<0.3.0')
        result = self.reconcile()
        self.assertIn('plugin-a', result['suspended'])
        self.assertEqual(result['restored'], [])
        self.assertTrue((self.profile / 'node_modules/plugin-a/package.json').is_file())
        self.manager.apply([], self.manager.state()['revision'])

    def test_changed_package_with_same_version_is_not_rebased(self):
        self.record_plugin()
        (self.profile / 'node_modules/plugin-a/patch.yml').write_bytes(b'changed')
        self.assertIn('plugin-a', self.reconcile()['suspended'])

    def prepare_install(self):
        self.original()
        with patch('harness_launcher.host.patch_modes.archive_info', return_value={'version': '0.2.0'}):
            return self.service.prepare(str(self.installer), self.checksum)

    def test_launcher_update_reuses_preview_cache_and_calls_installer_after_native(self):
        plan = self.prepare_install()
        def install(source):
            self.assertEqual(self.manager.state()['enabled'], [])
            self.assertTrue(self.service.path.is_file())
            self.updated()
            return Mock(pid=1234, wait=Mock(return_value=0), poll=Mock(return_value=0))
        with patch.object(self.service, 'install', side_effect=install) as installer:
            result = self.service.execute(plan['token'])
        self.assertEqual(result['version'], '0.3.0')
        installer.assert_called_once()
        self.signature.assert_called_once()
        self.extractor.extract_all.assert_called_once()
        self.assertFalse(self.service.path.exists())

    def test_installer_failure_keeps_checkpoint_and_allows_same_host_abort(self):
        plan = self.prepare_install()
        with patch.object(self.service, 'install', return_value=Mock(pid=123, wait=Mock(return_value=2), poll=Mock(return_value=2))):
            with self.assertRaisesRegex(LauncherError, '退出码 2'):
                self.service.execute(plan['token'])
        self.assertTrue(self.service.load()['checkpoint'])
        self.service.abort()
        self.assertFalse(self.service.path.exists())
        self.assertEqual(self.exe.read_bytes(), self.old_exe)

    def test_same_version_update_is_noop_without_installer(self):
        self.original()
        with patch('harness_launcher.host.patch_modes.archive_info', return_value={'version': '0.3.0'}), patch.object(self.service, 'install') as installer:
            with self.assertRaisesRegex(LauncherError, '不是更新版本'):
                self.service.prepare(str(self.installer), self.checksum)
        installer.assert_not_called()

    def test_invalid_token_or_changed_preview_does_not_restore_or_install(self):
        plan = self.prepare_install()
        self.manifest.write_bytes(self.manifest.read_bytes() + b' ')
        with patch.object(self.service, 'install') as installer:
            for token in ('invalid', plan['token']):
                with self.assertRaises(LauncherError):
                    self.service.execute(token)
        installer.assert_not_called()
        self.assertFalse(self.service.path.exists())

    def test_activation_failure_can_resume_after_restart_without_extraction(self):
        from harness_launcher.host import official_update
        write = official_update.write_json
        def interrupt(path, value):
            if Path(path).name == 'active-host.json':
                raise OSError('power loss at pointer commit')
            return write(path, value)
        with patch('harness_launcher.host.official_update.write_json', side_effect=interrupt), self.assertRaises(OSError):
            self.reconcile()
        self.assertEqual(self.manager.tracking.root, self.old_tracker.root)
        reopened = Manager(self.manager.data, self.home, self.exe)
        reopened.official_update.reconcile()
        self.assertEqual(reopened.state()['hostHealth']['status'], 'ready')
        self.extractor.extract_all.assert_called_once()

    def test_gc_retains_old_generation_and_transaction_roots(self):
        from harness_launcher.storage.cache_gc import CacheGC
        old = self.old_tracker.load()['currentCombination']
        self.reconcile()
        preview = CacheGC(self.manager).preview(grace=0)
        self.assertNotIn(old, [entry['path'] for entry in preview['files']])

    def test_package_manifest_reused_across_restart_and_manual_repair(self):
        self.reconcile()
        reopened = Manager(self.manager.data, self.home, self.exe)
        preview = reopened.runtime_repair.prepare(str(self.installer), self.checksum)
        self.assertEqual(preview['missing'], [])
        self.assertEqual(preview['conflicts'], [])
        self.signature.assert_called_once()
        self.extractor.extract_all.assert_called_once()
        reopened.runtime_repair.discard()

    def test_reconcile_already_adopted_host_is_noop(self):
        self.reconcile()
        root = self.manager.tracking.root
        self.assertTrue(self.reconcile()['unchanged'])
        self.assertEqual(root, self.manager.tracking.root)
        self.extractor.extract_all.assert_called_once()

    def test_expired_signature_is_rechecked_without_duplicating_manifest(self):
        self.reconcile()
        store = self.manager.official_packages.store
        receipt = next((store.root / 'receipts').glob('*.json'))
        data = json.loads(receipt.read_bytes())
        original_ref = data['manifest']
        data['verifiedAt'] = 0
        receipt.write_text(json.dumps(data))
        with self.manager.official_packages.session():
            package = self.manager.official_packages.inspect(str(self.installer), self.checksum)
        self.assertEqual(package['reference'], original_ref)
        self.assertEqual(self.signature.call_count, 2)
        self.extractor.extract_all.assert_called_once()

    def test_corrupt_cache_is_rebuilt_but_changed_installer_is_never_executed(self):
        self.reconcile()
        store = self.manager.official_packages.store
        receipt = next((store.root / 'receipts').glob('*.json'))
        receipt.write_bytes(b'incomplete JSON')
        with self.manager.official_packages.session():
            self.manager.official_packages.inspect(str(self.installer), self.checksum)
        self.assertEqual(self.extractor.extract_all.call_count, 2)
        self.installer.write_bytes(b'tampered installer')
        with self.assertRaisesRegex(LauncherError, 'SHA-512'), self.manager.official_packages.session():
            self.manager.official_packages.inspect(str(self.installer), self.checksum)
        self.assertEqual(self.signature.call_count, 2)

    def test_pending_update_blocks_other_mutations_and_abort_after_host_update(self):
        plan = self.prepare_install()
        with patch.object(self.service, 'install', return_value=Mock(pid=123, wait=Mock(return_value=2), poll=Mock(return_value=2))):
            with self.assertRaises(LauncherError):
                self.service.execute(plan['token'])
        with self.assertRaisesRegex(LauncherError, '官方更新尚未完成'):
            self.manager.apply([], self.manager.state()['revision'])
        self.updated()
        with self.assertRaises(LauncherError):
            self.service.abort()
        self.assertTrue(self.service.path.exists())

    def test_installer_changed_after_restoration_is_not_executed(self):
        plan = self.prepare_install()
        apply = self.manager.apply
        def change(*args, **kwargs):
            result = apply(*args, **kwargs)
            self.installer.write_bytes(b'changed between preview and execution')
            return result
        with patch.object(self.manager, 'apply', side_effect=change), patch.object(self.service, 'install') as install:
            with self.assertRaisesRegex(LauncherError, 'SHA-512'):
                self.service.execute(plan['token'])
        install.assert_not_called()

    def test_active_purge_after_external_update_blocks_before_extraction(self):
        self.original()
        self.manager.apply(['dsh-purge'], self.manager.state()['revision'])
        self.exe.write_bytes(b'new host over old purge')
        with self.assertRaisesRegex(LauncherError, '旧版活动补丁'):
            self.reconcile()
        self.signature.assert_not_called()

    def test_suspended_plugin_does_not_block_safe_native_launch_after_adoption(self):
        self.record_plugin(requirement='<0.3.0')
        self.cache()
        with patch('harness_launcher.plugins.installer_policy.ensure_adapter_isolation'), patch('harness_launcher.app.launcher.subprocess.Popen') as spawn:
            result = self.manager.apply(['plugin-a'], self.manager.state()['revision'], launch=True)
        spawn.assert_called_once()
        self.assertIn('plugin-a', result['officialUpdate']['suspended'])
        self.assertEqual(self.manager.state()['enabled'], [])

    def add_runtime_member(self):
        name = 'resources/runtime/primary-runtime/library.py'
        path = self.exe.parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'official library')
        dest = self.payload / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        self.sizes[name] = path.stat().st_size
        return path

    def test_missing_non_anchor_runtime_is_repaired_without_rebuilding_generation(self):
        path = self.add_runtime_member()
        self.reconcile()
        root = self.manager.tracking.root
        path.unlink()
        self.cache()
        with patch('harness_launcher.plugins.installer_policy.ensure_adapter_isolation'), patch('harness_launcher.app.launcher.subprocess.Popen') as spawn:
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        self.assertEqual(path.read_bytes(), b'official library')
        self.assertEqual(root, self.manager.tracking.root)
        self.extractor.extract_all.assert_called_once()
        self.extractor.extract_members.assert_called_once()
        spawn.assert_called_once()

    def test_non_anchor_runtime_conflict_and_missing_package_block_launch(self):
        path = self.add_runtime_member()
        self.reconcile()
        for content in (b'user change', None):
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            with patch('harness_launcher.plugins.installer_policy.ensure_adapter_isolation'), patch('harness_launcher.app.launcher.subprocess.Popen') as spawn, self.assertRaises(LauncherError):
                self.manager.apply([], self.manager.state()['revision'], launch=True)
            spawn.assert_not_called()

    def test_native_extension_is_suspended_even_with_compatible_engine_range(self):
        self.record_plugin()
        # Re-record a fixture containing an ABI-dependent binary so its bytes
        # match the prior capsule; suspension is about ABI, not an unknown edit.
        self.original()
        folder = self.profile / 'node_modules/plugin-a'
        (folder / 'native.node').write_bytes(b'old Electron native binary')
        tracker = self.old_tracker
        state = tracker.load()
        record = state['records']['plugin-a']
        record['package'] = tracker.versions.package_snapshot('plugin-a', record['source'])
        record['revision'] = tracker.versions.revision('plugin-a', record, record['package'], record['source'])
        tracker.save(state)
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        self.updated()
        self.assertIn('原生扩展', self.reconcile()['suspended']['plugin-a'])

    def test_installer_preparation_restores_active_purge_before_start(self):
        self.original()
        self.manager.apply(['dsh-purge'], self.manager.state()['revision'])
        with patch('harness_launcher.host.patch_modes.archive_info', return_value={'version': '0.2.0'}):
            plan = self.service.prepare(str(self.installer), self.checksum)
        def install(source):
            self.assertFalse(self.app.exists())
            self.assertEqual(self.manager.state()['enabled'], [])
            self.updated()
            return Mock(pid=12, wait=Mock(return_value=0), poll=Mock(return_value=0))
        with patch.object(self.service, 'install', side_effect=install):
            result = self.service.execute(plan['token'])
        self.assertIn('dsh-purge', result['suspended'])
        self.assertTrue((self.old_tracker.core.stash / 'app').exists())

    def test_suspended_plugin_can_be_previewed_and_uninstalled_without_old_patch_replay(self):
        self.record_plugin(requirement='<0.3.0')
        self.reconcile()
        before = self.old_tracker.index.read_bytes()
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'uninstall')
        self.assertEqual(preview['blocked'], [])
        def uninstall(action, source, operation_id):
            folder = self.profile / 'node_modules/plugin-a'
            self.assertEqual(folder.resolve().parent, (self.profile / 'node_modules').resolve())
            shutil.rmtree(folder)
            obj = json.loads(self.manifest.read_bytes())
            obj['dependencies'].pop('plugin-a')
            obj['dsh']['profile']['bundles'] = list(OFFICIAL)
            self.manifest.write_text(json.dumps(obj))
        with patch.object(service, 'package_command', side_effect=uninstall):
            service.start('plugin-a', 'uninstall', preview['token'])
        self.assertNotIn('plugin-a', self.manager.tracking.load()['suspended'])
        self.assertEqual(self.old_tracker.index.read_bytes(), before)

    def test_suspended_package_changed_after_adoption_blocks_maintenance(self):
        self.record_plugin(requirement='<0.3.0')
        self.reconcile()
        (self.profile / 'node_modules/plugin-a/patch.yml').write_bytes(b'user modification')
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'uninstall')
        with patch.object(service, 'package_command') as installer, self.assertRaises(LauncherError):
            service.start('plugin-a', 'uninstall', preview['token'])
        installer.assert_not_called()

    def test_suspended_plugin_can_revalidate_current_version_and_rollback_without_old_replay(self):
        self.record_plugin(requirement='')
        self.reconcile()
        old_registry = self.old_tracker.index.read_bytes()
        service = self.manager.maintenance
        preview = service.preview('plugin-a', 'upgrade', 'plugin-a@1.0.0')
        self.assertFalse(preview['blocked'])
        def launch(pending):
            self.assertFalse(self.app.exists())
            self.assertEqual(self.old_tracker.index.read_bytes(), old_registry)
            (self.home / 'rules').mkdir(exist_ok=True)
            (self.home / 'rules' / 'new-host-plugin.md').write_bytes(b'fresh current-host plugin file')
            pending['observed'] = True
            service.save(pending)
        with patch('harness_launcher.plugins.maintenance.UpdateCheck.target', return_value=({'latest': '1.0.0'}, {})), \
                patch.object(service, 'package_command'), patch.object(service, '_launch', side_effect=launch):
            service.start('plugin-a', 'upgrade', preview['token'], 'plugin-a@1.0.0')
        self.assertIn('plugin-a', self.manager.tracking.load()['suspended'])
        service.finish()
        self.assertNotIn('plugin-a', self.manager.tracking.load()['suspended'])
        self.assertEqual(self.manager.tracking.load()['records']['plugin-a']['hostHash'], hash_file(self.exe))
        self.assertGreater(self.manager.tracking.load()['records']['plugin-a']['count'], 0)
        self.assertEqual(self.old_tracker.index.read_bytes(), old_registry)
        identity = service.summary()['history'][0]['id']
        _, recovery = service.recovery(identity)
        service.restore(recovery['token'], identity)
        self.assertIn('plugin-a', self.manager.tracking.load()['suspended'])
        self.assertEqual(self.old_tracker.index.read_bytes(), old_registry)
        self.assertFalse((self.home / 'rules' / 'new-host-plugin.md').exists())

    def test_user_edit_during_generation_build_cannot_become_new_baseline(self):
        original = self.service.transferable
        def edit(*args):
            result = original(*args)
            (self.home / 'AGENTS.md').write_bytes(b'concurrent user modification')
            return result
        with patch.object(self.service, 'transferable', side_effect=edit), self.assertRaisesRegex(LauncherError, '用户侧'):
            self.reconcile()
        self.assertEqual(self.manager.tracking.root, self.old_tracker.root)
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'concurrent user modification')

    def test_plugin_edit_after_compatibility_check_is_not_accepted_as_a_new_capsule(self):
        self.record_plugin()
        original = self.service.transferable
        def edit(*args):
            result = original(*args)
            (self.profile / 'node_modules/plugin-a/patch.yml').write_bytes(b'concurrent code change')
            return result
        with patch.object(self.service, 'transferable', side_effect=edit), self.assertRaisesRegex(LauncherError, '插件包发生变化'):
            self.reconcile()
        self.assertEqual(self.manager.tracking.root, self.old_tracker.root)


class CompatibilityTests(unittest.TestCase):
    def test_conservative_ranges(self):
        for version, requirement, expected in [
            ('0.3.0', '>=0.2.0 <1.0.0', True), ('0.3.0', '^0.2.0', False),
            ('0.2.1', '^0.2.0', True), ('1.2.3', '~1.2.0', True), ('1.3.0', '~1.2.0', False),
            ('0.3.0-rc.1', '>=0.2.0', False), ('0.3.0-rc.2', '>=0.3.0-rc.1', True),
            ('0.3.0-rc.2', '>=0.3.0-rc.1 <0.4.0', True),
            ('0.4.0-rc.1', '>=0.3.0-rc.1 <0.5.0', False),
            ('0.3.0', '*', False), ('0.3.0', None, False), ('0.3.0', '>=0.2.0 || *', False),
        ]:
            with self.subTest(version=version, requirement=requirement):
                self.assertEqual(compatible(version, requirement), expected)


if __name__ == '__main__':
    unittest.main()
