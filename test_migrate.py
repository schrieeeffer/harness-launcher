import copy
import json
import os
from pathlib import Path
import re
import struct
import unittest
import uuid
from unittest.mock import patch

from file_view import FileView
from launcher import Manager
from migrate import import_data, migrate, recover_metadata
from patch_modes import PatchError, sha
import test_plugin_tracking as helpers
import test_version_plan as version_helpers


class MigrationTests(unittest.TestCase):
    setUp = helpers.GenericTrackingTests.setUp
    wait_job = helpers.GenericTrackingTests.wait_job
    begin = helpers.GenericTrackingTests.begin
    install_fixture = helpers.GenericTrackingTests.install_fixture
    accept = helpers.GenericTrackingTests.accept
    close_runtime = helpers.GenericTrackingTests.close_runtime
    apply = helpers.GenericTrackingTests.apply
    record = version_helpers.VersionPlanTests.record

    def old_data(self, native=False):
        self.record()
        self.apply(['plugin-a'] if native else ['dsh-purge', 'plugin-a'])
        self.manager.scheme('work', ['plugin-a'])
        tracker = self.manager.tracking
        value = tracker.load()
        value.update(format=1, pending=None)
        for record in value['records'].values():
            for key in ('package', 'revision', 'source', 'baseVersions', 'manifestBefore'):
                record.pop(key, None)
        refs = {}
        def old(value):
            if isinstance(value, str) and re.fullmatch(r'(snapshots|deltas)/[a-f0-9]{64}\.json', value):
                if value not in refs:
                    relative = value.split('/')[0] + '/' + uuid.uuid4().hex + '.json'
                    (tracker.root / relative).write_bytes((tracker.root / value).read_bytes())
                    refs[value] = relative
                return refs[value]
            if isinstance(value, dict):
                return {key: old(item) for key, item in value.items()}
            if isinstance(value, list):
                return [old(item) for item in value]
            return value
        value = old(value)
        tracker.index.write_text(json.dumps(value))
        state = json.loads(tracker.core.record.read_bytes())
        state.update(format=1, pending=None)
        state.pop('package'); state.pop('revision')
        if native:
            state['mode'] = 'plugin'  # Real old data can have a stale status label.
        tracker.core.record.write_text(json.dumps(state))
        self.manager.settings.pop('schemeLocks', None)
        self.manager.persist()
        for path in (self.manager.data / 'backups').glob('*.json'):
            backup = json.loads(path.read_bytes())
            backup.pop('format', None); backup.pop('lock', None)
            path.write_text(json.dumps(backup))
        return tracker.index.read_bytes()

    def test_one_time_conversion_preserves_live_files_and_is_idempotent(self):
        self.old_data(native=True)
        manager = Manager(self.manager.data)
        original = self.manifest.read_bytes()
        before = FileView(manager.tracking.scopes.values()).snapshot(manager.tracking.scopes)
        report = migrate(manager, apply=True)
        self.assertEqual(report['status'], 'migrated')
        self.assertEqual(report['adapterMode'], 'native')
        self.assertEqual(len(report['checkedCombinations']), 4)
        fresh = Manager(self.manager.data)
        fresh.tracking.guard(check_files=True)
        record = fresh.tracking.load()['records']['plugin-a']
        self.assertEqual(len(Path(record['delta']).stem), 64)
        self.assertTrue(record['package'] and record['revision'])
        self.assertEqual(fresh.tracking.core.load()['format'], 2)
        self.assertTrue(fresh.tracking.core.load()['package'])
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(before, FileView(fresh.tracking.scopes.values()).snapshot(fresh.tracking.scopes))
        self.assertTrue(Path(report['archive']).is_file())
        self.assertEqual(migrate(fresh, apply=True)['status'], 'already-current')
        fresh.apply([], fresh.state()['revision'], native=True)
        self.assertTrue(fresh.state()['patchMode']['verifiedNative'])
        fresh.apply(['dsh-purge', 'plugin-a'], fresh.state()['revision'])
        self.assertEqual((self.home / 'skills' / 'plugin-a.txt').read_bytes(), b'A')

    def test_runtime_rejects_old_data_until_explicit_conversion(self):
        raw = self.old_data()
        fresh = Manager(self.manager.data)
        with self.assertRaisesRegex(PatchError, 'migrate.py'):
            fresh.tracking.load()
        with self.assertRaisesRegex(PatchError, 'migrate.py'):
            fresh.tracking.core.load()
        self.assertEqual(fresh.tracking.index.read_bytes(), raw)
        report = migrate(fresh)
        self.assertEqual(report['status'], 'prepared')
        self.assertEqual(fresh.tracking.index.read_bytes(), raw)

    def test_import_keeps_source_and_converts_only_the_new_root(self):
        raw = self.old_data(native=True)
        source = self.manager.data
        settings = (source / 'settings.json').read_bytes()
        manifest = self.manifest.read_bytes()
        destination = self.root / 'unified-data'
        import_data(source, destination)
        report = migrate(Manager(destination), apply=True)
        self.assertEqual(report['status'], 'migrated')
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual((source / 'settings.json').read_bytes(), settings)
        self.assertEqual(self.manager.tracking.index.read_bytes(), raw)
        self.assertEqual(migrate(Manager(destination))['status'], 'already-current')
        self.assertFalse((destination / 'import-pending.json').exists())
        self.assertEqual(json.loads((destination / 'import-source.json').read_bytes())['source'], str(source))

    def test_interrupted_import_resumes_without_partial_file_replacements(self):
        self.old_data()
        source, destination = self.manager.data, self.root / 'unified-data'
        import shutil
        real_copy = shutil.copyfileobj
        calls = 0
        def interrupt(stream, target, length):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write(b'incomplete copy')
                raise OSError('synthetic copy interruption')
            return real_copy(stream, target, length)
        with patch('migrate.shutil.copyfileobj', side_effect=interrupt):
            with self.assertRaises(OSError):
                import_data(source, destination)
        self.assertTrue((destination / 'import-pending.json').exists())
        self.assertFalse(list(destination.rglob('*.tmp')))
        import_data(source, destination)
        self.assertEqual(migrate(Manager(destination), apply=True)['status'], 'migrated')

    def test_import_refuses_conflicting_destination_and_active_source(self):
        self.old_data()
        source, destination = self.manager.data, self.root / 'unified-data'
        destination.mkdir()
        settings = destination / 'settings.json'
        settings.write_bytes(b'keep destination')
        with self.assertRaisesRegex(PatchError, '不为空'):
            import_data(source, destination)
        self.assertEqual(settings.read_bytes(), b'keep destination')
        (source / 'instance.json').write_bytes(b'{}')
        with self.assertRaisesRegex(PatchError, '仍在运行'):
            import_data(source, self.root / 'second-data')
        self.assertFalse((self.root / 'second-data').exists())

    def test_import_refuses_directory_links_before_copying(self):
        self.old_data()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'private.txt').write_bytes(b'keep outside')
        link = self.manager.data / 'patches' / 'link'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            os.symlink(outside, link, target_is_directory=True)
        destination = self.root / 'unified-data'
        with self.assertRaisesRegex(PatchError, '包含链接'):
            import_data(self.manager.data, destination)
        self.assertFalse((destination / 'settings.json').exists())
        self.assertEqual((outside / 'private.txt').read_bytes(), b'keep outside')

    def test_runtime_blocks_incomplete_import_and_metadata_migration(self):
        self.record()
        for name in ('import-pending.json', 'migration-pending.json'):
            marker = self.manager.data / name
            marker.write_bytes(b'{}')
            with self.assertRaisesRegex(PatchError, 'migrate.py'):
                self.manager.tracking.load()
            marker.unlink()

    def test_migration_write_failure_rolls_back_only_known_metadata(self):
        raw = self.old_data()
        fresh = Manager(self.manager.data)
        from migrate import put as real_put
        def fail(path, data):
            if path == fresh.tracking.core.record:
                raise OSError('synthetic metadata write failure')
            return real_put(path, data)
        with patch('migrate.put', side_effect=fail):
            with self.assertRaises(OSError):
                migrate(fresh, apply=True)
        self.assertEqual(fresh.tracking.index.read_bytes(), raw)
        self.assertFalse((fresh.data / 'migration-pending.json').exists())

    def test_interrupted_metadata_conversion_can_roll_back(self):
        raw = self.old_data()
        fresh = Manager(self.manager.data)
        from migrate import put as real_put
        def crash(path, data):
            real_put(path, data)
            if path == fresh.tracking.index:
                raise SystemExit('interruption after first metadata replacement')
        with patch('migrate.put', side_effect=crash):
            with self.assertRaises(SystemExit):
                migrate(fresh, apply=True)
        recover_metadata(fresh.data)
        self.assertEqual(fresh.tracking.index.read_bytes(), raw)
        self.assertEqual(migrate(Manager(self.manager.data), apply=True)['status'], 'migrated')

    def test_unfinished_recording_and_unrecorded_files_block_migration(self):
        self.old_data()
        tracker = self.manager.tracking
        raw = tracker.index.read_bytes()
        value = json.loads(raw)
        value['session'] = {'id': 'incomplete'}
        tracker.index.write_text(json.dumps(value))
        with self.assertRaisesRegex(PatchError, '未完成'):
            migrate(Manager(self.manager.data), apply=True)
        tracker.index.write_bytes(raw)
        (self.home / 'skills' / 'unknown.txt').write_bytes(b'preserve this')
        with self.assertRaisesRegex(PatchError, '未归属'):
            migrate(Manager(self.manager.data), apply=True)
        self.assertEqual(tracker.index.read_bytes(), raw)

    def test_shared_host_sdk_is_bound_to_retained_archive(self):
        payload = {
            'package.json': (self.app / 'package.json').read_bytes(), 'main.js': b'official',
            'dsh/node_modules/@deepseek-ai/cordis/package.json': b'{"name":"@deepseek-ai/cordis","version":"4.0.4"}'}
        tree, body = {'files': {}}, b''
        for name, data in payload.items():
            node = tree
            parts = name.split('/')
            for part in parts[:-1]:
                node = node['files'].setdefault(part, {'files': {}})
            node['files'][parts[-1]] = {'size': len(data), 'offset': str(len(body)),
                'integrity': {'algorithm': 'SHA256', 'hash': sha(data)}}
            body += data
        header = json.dumps(tree).encode()
        self.archive.write_bytes(struct.pack('<4I', 4, len(header) + 8, len(header) + 4, len(header)) + header + body)
        meta = json.loads((self.purge / 'package.json').read_bytes())
        meta['peerDependencies'] = {'@deepseek-ai/cordis': '^4.0.1'}
        (self.purge / 'package.json').write_text(json.dumps(meta))
        self.apply([])
        tracker = self.manager.tracking
        capsule = tracker.read_object(tracker.core.load()['package'])
        peer = capsule['packages']['0']['dependencies']['@deepseek-ai/cordis']
        self.assertEqual(peer['providedBy'], 'harness')
        self.assertEqual(peer['version'], '4.0.4')
        self.apply(['dsh-purge'])
        self.apply([])
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])


if __name__ == '__main__':
    unittest.main()
