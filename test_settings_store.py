import copy
from pathlib import Path
import tempfile
import unittest

from errors import SchemaError
from settings_store import load_settings, validate_settings
from storage import write_json


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'
        self.defaults = dict(home='C:/fixture/home', executable='C:/fixture/Harness.exe',
                             schemes={}, schemeLocks={}, order=[], lastSelection=None)

    def test_new_store_has_explicit_format_and_round_trips_extensions(self):
        value = load_settings(self.path, self.defaults)
        self.assertEqual(value['format'], 3)
        value['extensions'] = {'example': {'display': True}}
        write_json(self.path, value)
        self.assertEqual(load_settings(self.path, self.defaults), value)

    def test_existing_unversioned_or_unknown_required_features_fail_closed(self):
        for value in (self.defaults, dict(self.defaults, format=True), dict(self.defaults, format=4),
                      dict(self.defaults, format=3, requiredFeatures=['future-mutation'])):
            write_json(self.path, value)
            before = self.path.read_bytes()
            with self.assertRaises(SchemaError):
                load_settings(self.path, self.defaults)
            self.assertEqual(self.path.read_bytes(), before)

    def test_orphaned_records_are_not_adopted_as_new_store(self):
        (self.path.parent / 'patches').mkdir()
        with self.assertRaises(SchemaError):
            load_settings(self.path, self.defaults)
        self.assertFalse(self.path.exists())

    def test_settings_lists_and_scheme_lock_sets_are_validated(self):
        base = load_settings(self.path, self.defaults)
        for edit in ({'order':['x','x']}, {'lastSelection':'x'}, {'schemes':{'x':[]}},
                     {'schemes':{' x ':[]},'schemeLocks':{' x ':{}}}, {'home':None}):
            with self.subTest(edit=edit), self.assertRaises(SchemaError):
                validate_settings(dict(copy.deepcopy(base), **edit))


if __name__ == '__main__':
    unittest.main()
