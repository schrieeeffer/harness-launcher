"""Package verification keeps dependency and recovery boundaries on warm runs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness_launcher.domain.errors import PatchError, StorageError
from harness_launcher.verification.file_view import FileView
from harness_launcher.verification.ntfs_journal import JournalUnavailable
from harness_launcher.storage.content_store import ContentStore
from harness_launcher.verification.verification_cache import VerifiedContentCache
from harness_launcher.storage.version_store import VersionStore


class PackageCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root / 'profile'
        self.package = self.profile / 'node_modules' / 'example-plugin'
        self.code = self.package / 'dist'
        self.code.mkdir(parents=True)
        (self.package / 'package.json').write_text(json.dumps(
            {'name': 'example-plugin', 'version': '1.0.0', 'dependencies': {'shared': '1.0.0'}}))
        self.dependency = self.package / 'node_modules' / 'shared'
        self.dependency.mkdir(parents=True)
        (self.dependency / 'package.json').write_text(json.dumps({'name': 'shared', 'version': '1.0.0'}))
        (self.dependency / 'index.js').write_bytes(b'old dependency')
        for index in range(130):
            (self.code / f'{index}.js').write_bytes(b'old')
        self.cache_path = self.root / 'cache.json'
        self.cache = VerifiedContentCache(self.cache_path)
        self.store = ContentStore(self.root / 'store')
        self.versions = VersionStore(self.profile, self.root / 'host.exe', self.store, None, self.cache)

    def capture(self):
        return self.versions.capture('example-plugin', store=False, metadata=False)

    def seal(self):
        if not self.cache.tree_checkpoints:
            self.skipTest('Requires NTFS journal')
        try:
            self.cache.tree_checkpoints.journal.checkpoint(self.code)
        except JournalUnavailable:
            self.skipTest('NTFS journal unavailable')
        result = self.capture()
        self.assertTrue(self.cache.tree_checkpoints.trees)
        return result

    def test_warm_package_reuses_verified_subtree_after_cache_reload(self):
        expected = self.seal()
        self.cache.flush()
        self.cache.tree_checkpoints.flush()
        self.cache = VerifiedContentCache(self.cache_path)
        self.versions.verification_cache = self.cache
        original = FileView._capture
        def capture(view, path, *args):
            if path == self.code or path.is_relative_to(self.code):
                raise AssertionError('unchanged code subtree was scanned again')
            return original(view, path, *args)
        with patch.object(FileView, '_capture', capture):
            self.assertEqual(self.capture(), expected)
        self.assertGreater(self.cache.tree_checkpoints.hits, 0)
        # The package root is freshly enumerated, including new loose files.
        added = self.package / 'new.js'
        added.write_bytes(b'new')
        self.assertNotEqual(self.capture(), expected)
        added.unlink()
        self.assertEqual(self.capture(), expected)

    def test_same_size_restored_timestamp_edit_invalidates_code_checkpoint(self):
        expected = self.seal()
        target = self.code / '0.js'
        before = target.stat()
        target.write_bytes(b'new')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(self.capture(), expected)

    def test_dependency_resolution_and_content_are_rechecked_outside_cached_code(self):
        expected = self.seal()
        self.assertNotIn('node_modules', expected['packages']['0']['files'])
        (self.dependency / 'index.js').write_bytes(b'new dependency')
        self.assertNotEqual(self.capture(), expected)
        (self.dependency / 'index.js').write_bytes(b'old dependency')
        parked = self.root / 'shared'
        self.dependency.rename(parked)
        with self.assertRaises(PatchError):
            self.capture()

    def test_excluded_package_inventory_cannot_satisfy_a_complete_tree_check(self):
        expected = self.seal()
        complete = FileView([self.package], verification_cache=self.cache)
        complete.hash_files()
        self.assertIn(self.dependency / 'index.js', complete.entries)
        # Both cache scopes coexist; neither can supply the other's inventory.
        self.assertEqual(self.capture(), expected)
        self.assertIn(self.dependency / 'index.js',
                      FileView([self.package], verification_cache=self.cache).entries)

    def test_only_root_node_modules_is_excluded_from_package_code(self):
        expected = self.seal()
        nested = self.code / 'node_modules'
        nested.mkdir()
        (nested / 'loose.js').write_bytes(b'new code')
        actual = self.capture()
        self.assertNotEqual(actual, expected)
        self.assertIn('dist/node_modules/loose.js', actual['packages']['0']['files'])

    def test_missing_recovery_blob_blocks_before_any_package_change(self):
        reference = self.versions.package_snapshot('example-plugin')
        capsule = self.store.read_object(reference)
        key = capsule['packages']['0']['files']['dist/0.js']['hash']
        (self.store.root / 'blobs' / key).unlink()
        with self.assertRaisesRegex(StorageError, '缺失或无法读取'):
            self.versions.verify_package('example-plugin', reference, recovery=False)
        self.assertEqual((self.code / '0.js').read_bytes(), b'old')

    def test_subtree_add_delete_rename_and_root_replacement_are_detected(self):
        expected = self.seal()
        added = self.code / 'added.js'
        added.write_bytes(b'new')
        self.assertNotEqual(self.capture(), expected)
        added.unlink()
        target = self.code / '0.js'
        target.rename(self.code / 'renamed.js')
        self.assertNotEqual(self.capture(), expected)
        self.code.rename(self.package / 'old-dist')
        self.code.mkdir()
        self.assertNotEqual(self.capture(), expected)

    def test_journal_unavailable_falls_back_to_content_verification(self):
        expected = self.seal()
        with patch.object(self.cache.tree_checkpoints.journal, 'since', side_effect=JournalUnavailable), \
                patch.object(self.cache.tree_checkpoints.journal, 'checkpoint', side_effect=JournalUnavailable):
            (self.code / '0.js').write_bytes(b'new')
            self.assertNotEqual(self.capture(), expected)

    def test_shadow_module_added_during_package_read_is_rejected(self):
        original = self.versions.files
        def read(root, store=True):
            result = original(root, store)
            if root == self.dependency:
                (self.dependency.parent / 'shared.js').write_bytes(b'shadow')
            return result
        with patch.object(self.versions, 'files', side_effect=read), self.assertRaisesRegex(PatchError, '遮蔽'):
            self.capture()

    def test_new_optional_dependency_cannot_reuse_an_earlier_negative_lookup(self):
        manifest = self.package / 'package.json'
        value = json.loads(manifest.read_bytes())
        value['optionalDependencies'] = {'late': '1.0.0'}
        manifest.write_text(json.dumps(value))
        original = self.versions.files
        def read(root, store=True):
            result = original(root, store)
            if root == self.dependency:
                late = self.dependency.parent / 'late'
                late.mkdir()
                (late / 'package.json').write_text(json.dumps({'name': 'late', 'version': '1.0.0'}))
            return result
        with patch.object(self.versions, 'files', side_effect=read), self.assertRaisesRegex(PatchError, '依赖路径发生变化'):
            self.capture()

    def test_launch_verifies_live_code_while_package_restore_verifies_its_blobs(self):
        reference = self.versions.package_snapshot('example-plugin')
        capsule = self.store.read_object(reference)
        key = capsule['packages']['0']['files']['dist/0.js']['hash']
        (self.store.root / 'blobs' / key).write_bytes(b'corrupt recovery copy')
        self.versions.verify_package('example-plugin', reference, recovery=False)
        with self.assertRaises(StorageError):
            self.versions.verify_package('example-plugin', reference)
        (self.code / '0.js').write_bytes(b'new')
        with self.assertRaises(PatchError):
            self.versions.verify_package('example-plugin', reference, recovery=False)


if __name__ == '__main__':
    unittest.main()
