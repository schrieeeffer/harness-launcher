"""Persistence failures must not publish unsaved application settings."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from storage import write_json
import test_launcher as fixtures


class SettingsTransactionTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp

    def test_failed_scheme_create_replace_and_delete_leave_memory_and_disk_unchanged(self):
        self.manager.scheme('existing', ['plugin-a'])
        original = copy.deepcopy(self.manager.settings)
        before = self.manager.settings_file.read_bytes()
        operations = (lambda: self.manager.scheme('new', []),
                      lambda: self.manager.scheme('existing', ['@demo/plugin-b']),
                      lambda: self.manager.scheme('existing', delete=True))
        for operation in operations:
            with patch('launcher.write_json', side_effect=OSError('synthetic disk full')):
                with self.assertRaises(OSError):
                    operation()
            self.assertEqual(self.manager.settings, original)
            self.assertEqual(self.manager.settings_file.read_bytes(), before)
        self.manager.persist()
        self.assertEqual(json.loads(self.manager.settings_file.read_bytes()), original)

    def test_failed_import_does_not_leak_new_or_replaced_schemes_into_later_save(self):
        self.manager.scheme('existing', ['plugin-a'])
        original = copy.deepcopy(self.manager.settings)
        before = self.manager.settings_file.read_bytes()
        with patch('launcher.write_json', side_effect=PermissionError('synthetic access denied')):
            with self.assertRaises(PermissionError):
                self.manager.import_schemes({'existing': ['@demo/plugin-b'], 'new': []})
        self.assertEqual(self.manager.settings, original)
        self.assertEqual(self.manager.settings_file.read_bytes(), before)
        self.manager.persist()
        self.assertEqual(json.loads(self.manager.settings_file.read_bytes()), original)

    def test_preferences_write_failure_preserves_completed_apply_and_single_launch(self):
        original = copy.deepcopy(self.manager.settings)
        before = self.manager.settings_file.read_bytes()
        def fail_preferences_only(path, value):
            if Path(path) == self.manager.settings_file:
                raise OSError('synthetic settings disk full')
            return write_json(path, value)
        for launch, selection in ((False, []), (True, ['@demo/plugin-b'])):
            with self.subTest(launch=launch):
                revision = self.manager.state()['revision']
                with patch('launcher.write_json', side_effect=fail_preferences_only), \
                        patch('launcher.subprocess.Popen') as spawn, \
                        patch.object(self.manager, 'rollback_layers') as rollback, \
                        patch('launcher.logging.warning'):
                    result = self.manager.apply(selection, revision, launch=launch)
                self.assertTrue(result['changed'])
                self.assertEqual(result['launched'], launch)
                self.assertEqual(spawn.call_count, int(launch))
                rollback.assert_not_called()
                self.assertEqual(result['warning']['code'], 'preferences_save_failed')
                self.assertIn('操作已完成，但启动偏好保存失败', result['message'])
                self.assertIn('无需重复启动或应用', result['message'])
                self.assertEqual(self.manager.settings, original)
                self.assertEqual(self.manager.settings_file.read_bytes(), before)
                self.assertEqual(json.loads(self.path.read_bytes())['dsh']['profile']['bundles'],
                                 [*fixtures.OFFICIAL, *selection])
                self.assertFalse((self.manager.tracking.root / 'switch.json').exists())
        self.manager.persist()
        self.assertEqual(json.loads(self.manager.settings_file.read_bytes()), original)


if __name__ == '__main__':
    unittest.main()
