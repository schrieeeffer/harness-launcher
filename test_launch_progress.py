"""Launch optimizations must never skip validation of active contents."""
import os
import unittest
from unittest.mock import patch

from launcher import MenuError
from operations import OperationLog
from switch_plan import SwitchPlan
import test_launcher as launcher_tests
import test_patch_modes as patch_tests


class CurrentLaunchTests(unittest.TestCase):
    setUp = launcher_tests.ManagerTests.setUp

    def seal(self):
        self.manager.apply(['plugin-a'], self.manager.state()['revision'])

    def launch(self):
        return self.manager.apply(['plugin-a'], self.manager.state()['revision'], launch=True)

    def test_same_combination_skips_backup_replay_and_configuration_writes(self):
        self.seal()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        log = OperationLog()
        identity = log.begin('launch')
        with log.reporting(identity), patch.object(self.manager, 'backup') as backup, \
                patch.object(self.manager, 'switch_layers') as switch, patch('launcher.subprocess.Popen') as spawn:
            result = self.launch()
        self.assertTrue(result['verifiedCurrent'])
        backup.assert_not_called()
        switch.assert_not_called()
        spawn.assert_called_once()
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))
        self.assertEqual(log.snapshot()['current']['progress']['stage'], 'launch')
        self.assertFalse((self.manager.tracking.root / 'switch.json').exists())

    def test_same_metadata_live_content_edit_blocks_launch(self):
        live = self.home / 'rules' / 'custom.txt'
        live.parent.mkdir()
        live.write_bytes(b'original')
        self.seal()
        info = live.stat()
        live.write_bytes(b'modified')
        os.utime(live, ns=(info.st_atime_ns, info.st_mtime_ns))
        with patch('launcher.subprocess.Popen') as spawn, self.assertRaisesRegex(MenuError, '文件内容已变化'):
            self.launch()
        spawn.assert_not_called()

    def test_active_package_or_host_edit_blocks_launch(self):
        self.seal()
        for path in (self.profile / 'node_modules/plugin-a/package.json', self.exe):
            original = path.read_bytes()
            path.write_bytes(original.replace(b'1.2', b'1.3') if path != self.exe else b'changed host')
            with self.subTest(path=path.name), patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError):
                self.launch()
            spawn.assert_not_called()
            path.write_bytes(original)

    def test_failed_fast_launch_does_not_rollback_unchanged_files(self):
        self.seal()
        raw = self.path.read_bytes()
        with patch.object(self.manager, 'rollback_layers') as undo, \
                patch('launcher.subprocess.Popen', side_effect=OSError('fixture spawn failed')), self.assertRaises(MenuError):
            self.launch()
        undo.assert_not_called()
        self.assertEqual(raw, self.path.read_bytes())

    def test_change_during_validation_is_not_launched(self):
        self.seal()
        original = self.manager.tracking.check_versions
        def change(*args):
            original(*args)
            self.path.write_bytes(self.path.read_bytes() + b' ')
        with patch.object(self.manager.tracking, 'check_versions', side_effect=change), \
                patch('launcher.subprocess.Popen') as spawn, self.assertRaisesRegex(MenuError, '校验期间配置'):
            self.launch()
        spawn.assert_not_called()

    def test_different_combination_commits_a_transaction(self):
        self.seal()
        before = self.manager.tracking.load()['currentCombination']
        with patch.object(self.manager, 'switch_layers', wraps=self.manager.switch_layers) as switch, \
                patch('launcher.subprocess.Popen'):
            result = self.manager.apply([], self.manager.state()['revision'], launch=True)
        switch.assert_called_once()
        self.assertFalse(result['verifiedCurrent'])
        self.assertEqual(self.manager.state()['enabled'], [])
        after = self.manager.tracking.load()['currentCombination']
        self.assertNotEqual(before, after)
        self.assertEqual(self.manager.tracking.read_object(after)['selection'], [])
        self.assertFalse((self.manager.tracking.root / 'switch.json').exists())


class DormantAdapterLaunchTests(unittest.TestCase):
    setUp = patch_tests.PatchModeTests.setUp
    switch = patch_tests.PatchModeTests.switch

    def test_dormant_tree_is_not_read_or_replayed_until_switch(self):
        self.switch([])
        parked = self.manager.tracking.core.stash / 'app' / 'main.js'
        parked.write_bytes(b'dormant edit')
        with patch('launcher.subprocess.Popen') as spawn:
            result = self.manager.apply([], self.manager.state()['revision'], launch=True, native=True)
        self.assertTrue(result['verifiedCurrent'])
        spawn.assert_called_once()
        self.assertEqual(parked.read_bytes(), b'dormant edit')
        with self.assertRaises(MenuError):
            self.switch(['dsh-purge'])

    def test_active_adapter_tree_is_still_content_checked(self):
        self.switch(['dsh-purge'])
        (self.app / 'main.js').write_bytes(b'active edit')
        with patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError):
            self.manager.apply(['dsh-purge'], self.manager.state()['revision'], launch=True)
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
