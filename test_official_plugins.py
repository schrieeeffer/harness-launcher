"""Official plugin creation coexists with launcher-owned reversible layers."""
import json
from unittest.mock import patch
import unittest

from launcher import MenuError
from patch_modes import PatchError
from errors import LauncherError
import test_plugin_tracking as helpers


class OfficialPluginTests(unittest.TestCase):
    setUp = helpers.GenericTrackingTests.setUp
    wait_job = helpers.GenericTrackingTests.wait_job
    begin = helpers.GenericTrackingTests.begin
    install_fixture = helpers.GenericTrackingTests.install_fixture
    accept = helpers.GenericTrackingTests.accept
    close_runtime = helpers.GenericTrackingTests.close_runtime
    apply = helpers.GenericTrackingTests.apply

    def external(self, enabled=True):
        self.install_fixture('@local/created')
        if not enabled:
            value = json.loads(self.manifest.read_bytes())
            value['dsh']['profile']['bundles'].remove('@local/created')
            self.manifest.write_text(json.dumps(value))

    def test_official_plugin_can_launch_without_fake_record_or_dormant_adapter_reads(self):
        self.apply([])
        self.external()
        config = self.profile / 'cordis.patch.yml'
        config.write_text('# User changed normal Harness settings\n[]\n')
        before = config.read_bytes(), self.manifest.read_bytes(), self.manager.tracking.load()
        with patch.object(self.manager, 'spawn_harness') as spawn, patch.object(self.manager.tracking.core, 'verify_files', side_effect=AssertionError('Dormant adapter')):
            result = self.apply(['@local/created'], launch=True)
        self.assertTrue(result['launched'])
        spawn.assert_called_once()
        self.assertEqual(before, (config.read_bytes(), self.manifest.read_bytes(), self.manager.tracking.load()))
        plugin = next(p for p in self.manager.state()['plugins'] if p['id'] == '@local/created')
        self.assertEqual(plugin['management'], 'official')

    def test_inactive_external_plugin_does_not_block_managed_bundle_switches(self):
        self.apply([])
        self.begin('plugin-a')
        self.install_fixture('plugin-a')
        self.accept()
        self.external(enabled=False)
        self.apply([])
        self.apply(['plugin-a'])
        self.assertEqual(self.manager.state()['enabled'], ['plugin-a'])

    def test_official_loading_is_not_silently_changed_by_launcher(self):
        self.apply([])
        self.external()
        before = self.manifest.read_bytes()
        with self.assertRaises(MenuError) as caught:
            self.apply([])
        self.assertEqual(caught.exception.code, 'official_plugin_managed')
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_active_recorded_patch_tamper_still_blocks_official_config_launch(self):
        self.apply([])
        self.begin('plugin-a')
        self.install_fixture('plugin-a')
        target = self.home / 'rules' / 'owned.txt'
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b'owned')
        self.accept()
        self.external()
        target.write_bytes(b'changed')
        with patch.object(self.manager, 'spawn_harness') as spawn, self.assertRaises((MenuError, PatchError)):
            self.apply(['plugin-a', '@local/created'], launch=True)
        spawn.assert_not_called()

    def test_host_update_still_blocks_stale_patch_state(self):
        self.apply([])
        self.external()
        self.manager.tracking.core.archive.write_bytes(b'unknown host')
        with patch.object(self.manager, 'spawn_harness') as spawn, self.assertRaises((MenuError, PatchError)):
            self.apply(['@local/created'], launch=True)
        spawn.assert_not_called()

    def test_policy_failure_after_switch_restores_the_previous_layers(self):
        self.begin()
        self.install_fixture('plugin-a')
        (self.app / 'main.js').write_bytes(b'A')
        self.accept()
        before = self.manifest.read_bytes()
        with patch('installer_policy.ensure_adapter_isolation', side_effect=LauncherError('policy failed', code='installer_policy_failed')):
            with self.assertRaises(MenuError) as caught:
                self.apply([], launch=True)
        self.assertEqual(caught.exception.code, 'installer_policy_failed')
        self.assertEqual((self.app / 'main.js').read_bytes(), b'A')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertTrue(self.manager.tracking.load()['records']['plugin-a']['enabled'])

    def test_dormant_patch_does_not_freeze_user_yaml_but_cannot_overwrite_new_settings(self):
        self.apply([])
        config = self.profile / 'cordis.patch.yml'
        config.write_bytes(b'# user original\n[]\n')
        self.begin('patch-a')
        self.install_fixture('patch-a')
        config.write_bytes(b'# patch owns this file\n[]\n')
        self.accept()
        self.apply([])
        self.begin('ordinary-b')
        self.install_fixture('ordinary-b')
        self.accept()
        self.apply([])
        config.write_bytes(b'# Harness changed user preferences\n[]\n')
        expected = config.read_bytes()
        self.apply(['ordinary-b'])
        self.apply([])
        self.assertEqual(config.read_bytes(), expected)
        with self.assertRaises(MenuError):
            self.apply(['patch-a'])
        self.assertEqual(config.read_bytes(), expected)

    def test_external_plugin_cannot_hide_an_unrecorded_program_change(self):
        self.apply([])
        self.external()
        target = self.exe.parent / 'resources/runtime/cli/unexpected.js'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'unknown code')
        with patch.object(self.manager, 'spawn_harness') as spawn, self.assertRaises((MenuError, PatchError)):
            self.apply(['@local/created'], launch=True)
        spawn.assert_not_called()

    def test_user_settings_do_not_revalidate_unchanged_archive_members(self):
        self.apply([])
        self.begin('ordinary-b')
        self.install_fixture('ordinary-b')
        rule = self.home / 'rules' / 'ordinary-b.md'
        rule.parent.mkdir(exist_ok=True)
        rule.write_bytes(b'plugin-owned rule')
        self.accept()
        self.apply([])
        tracker = self.manager.tracking
        before = dict(tracker.load()['nativeBaselines'])
        config = self.profile / 'cordis.patch.yml'
        config.write_bytes(b'# changed by official settings UI\n[]\n')
        with patch('switch_plan.archive_info', side_effect=AssertionError('redundant archive validation')):
            self.apply(['ordinary-b'])
        self.assertEqual(config.read_bytes(), b'# changed by official settings UI\n[]\n')
        self.assertNotEqual(tracker.load()['nativeBaselines'], before)
        for reference in before.values():
            self.assertIsInstance(tracker.read_object(reference), dict)
