"""Storage invariants exercised exclusively against disposable directories."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness_launcher.domain.errors import StorageError
from harness_launcher.storage.content_store import ContentStore, atomic_write, hash_file


class ContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = ContentStore(self.root / 'store')
        self.source = self.root / 'source.txt'
        self.source.write_bytes(b'original data')

    def stamp(self):
        info = self.source.stat()
        return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest('This environment cannot create symlinks: ' + str(exc))

    def test_canonical_objects_deduplicate_and_reject_corrupt_existing_content(self):
        ref = self.store.object('revisions', {'b': 2, 'a': '内容'})
        self.assertEqual(ref, self.store.object('revisions', {'a': '内容', 'b': 2}))
        self.assertEqual(self.store.read_object(ref), {'a': '内容', 'b': 2})
        path = self.store.root / ref
        path.write_bytes(b'corrupted')
        for action in (lambda: self.store.read_object(ref),
                       lambda: self.store.object('revisions', {'b': 2, 'a': '内容'})):
            with self.assertRaises(StorageError) as error:
                action()
            self.assertEqual(error.exception.code, 'storage_integrity')
        self.assertEqual(path.read_bytes(), b'corrupted')

    def test_invalid_object_references_and_blob_ids_never_escape_store(self):
        for ref in ('../source.txt', 'snapshots/../source.txt', '/absolute.json',
                    'snapshots/' + 'a' * 64 + '.json/extra', 42, None):
            with self.assertRaises(StorageError):
                self.store.read_object(ref)
        for key in ('../source.txt', 'A' * 64, None, ''):
            with self.assertRaises(StorageError):
                self.store.blob_bytes(key)
        self.assertEqual(self.source.read_bytes(), b'original data')
        self.assertFalse(self.store.root.exists())

    def test_store_paths_reject_lexical_escapes_and_aliases(self):
        for value in ('../source.txt', '/source.txt', 'C:source.txt', 'blobs/../source.txt',
                      'blobs\\source.txt', 'blobs/file:stream', 'blobs//hash', './blobs',
                      'blobs/', '.', '', 'blobs/\x00hash', 'blobs/hash.', 'blobs/hash ',
                      'blobs/NUL', 'blobs/COM1.json', 'blobs/*'):
            with self.assertRaises(StorageError, msg=repr(value)):
                self.store._path(value)
        self.assertFalse(self.store.root.exists())

    def test_cached_identity_is_rechecked_against_source_bytes(self):
        old = self.store.store_file(self.source, self.stamp())
        before = self.source.stat()
        self.source.write_bytes(b'changed! data')
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        stamp = self.stamp()
        # Windows creation time can remain unchanged. Explicitly supply that
        # stale cache scenario on all platforms instead of assuming OS behavior.
        current = self.store.store_file(self.source, stamp, {'stamp': stamp, 'hash': old})
        self.assertNotEqual(current, old)
        self.assertEqual(self.store.blob_bytes(current), b'changed! data')
        self.assertEqual(self.store.blob_bytes(old), b'original data')

    def test_valid_cached_blob_avoids_rewrite_but_still_reads_source(self):
        stamp = self.stamp()
        digest = self.store.store_file(self.source, stamp)
        with patch('harness_launcher.storage.content_store.hash_file', wraps=hash_file) as read, patch('harness_launcher.storage.content_store.os.replace') as replace:
            self.assertEqual(self.store.store_file(self.source, stamp, {'stamp': stamp, 'hash': digest}), digest)
            read.assert_called_once_with(self.source)
            replace.assert_not_called()

    def test_corrupt_blob_is_never_silently_healed_or_reused(self):
        stamp = self.stamp()
        key = self.store.store_file(self.source, stamp)
        blob = self.store.root / 'blobs' / key
        blob.write_bytes(b'corrupt')
        for action in (lambda: self.store.store_file(self.source, stamp, {'stamp': stamp, 'hash': key}),
                       lambda: self.store.store_file(self.source, stamp),
                       lambda: self.store.put_blob(self.source.read_bytes())):
            with self.assertRaises(StorageError):
                action()
            self.assertEqual(blob.read_bytes(), b'corrupt')
        self.assertEqual(list((self.store.root / 'blobs').glob('*.tmp')), [])

    def test_uncached_duplicate_checkpoint_reads_but_never_writes_a_temp_file(self):
        stamp = self.stamp()
        key = self.store.store_file(self.source, stamp)
        with patch('harness_launcher.storage.content_store.hash_file', wraps=hash_file) as read, patch('harness_launcher.storage.content_store.os.fsync') as sync, \
                patch('harness_launcher.storage.content_store.os.replace') as replace:
            self.assertEqual(self.store.store_file(self.source, stamp), key)
        read.assert_called_once_with(self.source)
        sync.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(list((self.store.root / 'blobs').glob('*.tmp')), [])

    def test_content_change_between_hash_and_copy_does_not_publish_wrong_identity(self):
        original = hash_file(self.source)
        def wrong_hash(_):
            return '0' * 64
        with patch('harness_launcher.storage.content_store.hash_file', side_effect=wrong_hash), self.assertRaises(StorageError):
            self.store.store_file(self.source, self.stamp())
        self.assertFalse((self.store.root / 'blobs' / original).exists())
        self.assertEqual(list((self.store.root / 'blobs').glob('*.tmp')), [])

    def test_source_change_during_write_does_not_publish_blob(self):
        stamp = self.stamp()
        fsync = os.fsync
        def mutate(descriptor):
            self.source.write_bytes(b'changed while being captured')
            fsync(descriptor)
        with patch('harness_launcher.storage.content_store.os.fsync', side_effect=mutate):
            with self.assertRaisesRegex(StorageError, '仍在变化'):
                self.store.store_file(self.source, stamp)
        self.assertEqual(list((self.store.root / 'blobs').iterdir()), [])
        self.assertEqual(self.source.read_bytes(), b'changed while being captured')

    def test_source_change_during_cached_hash_is_detected(self):
        stamp = self.stamp()
        key = self.store.store_file(self.source, stamp)
        def mutate(path):
            result = hash_file(path)
            self.source.write_bytes(b'changed after hash')
            return result
        with patch('harness_launcher.storage.content_store.hash_file', side_effect=mutate):
            with self.assertRaisesRegex(StorageError, '仍在变化'):
                self.store.store_file(self.source, stamp, {'stamp': stamp, 'hash': key})

    def test_stale_or_invalid_source_stamp_is_rejected(self):
        old = self.stamp()
        self.source.write_bytes(b'new content with a different size')
        for stamp in (old, [True, 1, 2], [1, 2], '123', [1, -1, 2]):
            with self.assertRaises(StorageError):
                self.store.store_file(self.source, stamp)
        self.assertFalse(self.store.root.exists())

    def test_store_root_or_blob_links_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.symlink(self.store.root, outside, directory=True)
        with self.assertRaises(StorageError):
            self.store.put_blob(b'test')
        self.assertEqual(list(outside.iterdir()), [])
        self.store.root.unlink()
        key = self.store.put_blob(b'test')
        blob = self.store.root / 'blobs' / key
        blob.unlink()
        self.symlink(blob, self.source)
        with self.assertRaises(StorageError):
            self.store.blob_bytes(key)
        with self.assertRaises(StorageError):
            self.store.statistics()

    def test_source_link_is_not_stored_as_a_regular_file(self):
        link = self.root / 'source-link'
        self.symlink(link, self.source)
        with self.assertRaisesRegex(StorageError, '普通文件'):
            self.store.store_file(link, self.stamp())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_store_junction_is_rejected_without_requiring_symlink_privilege(self):
        outside = self.root / 'outside'
        outside.mkdir()
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(self.store.root), str(outside)],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.addCleanup(lambda: self.store.root.rmdir() if self.store.root.is_junction() else None)
        self.assertTrue(self.store.root.is_junction())
        with self.assertRaises(StorageError):
            self.store.put_blob(b'refuse linked storage')
        with self.assertRaises(StorageError):
            self.store.statistics()
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_path_safety_is_rechecked_after_regular_directory_becomes_junction(self):
        key = self.store.put_blob(b'test')
        self.assertEqual(self.store.blob_bytes(key), b'test')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / key).write_bytes(b'test')
        directory = self.store.root / 'blobs'
        saved = self.store.root / 'saved-blobs'
        os.replace(directory, saved)
        try:
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(directory), str(outside)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            with self.assertRaisesRegex(StorageError, '链接'):
                self.store.blob_bytes(key)
            with self.assertRaisesRegex(StorageError, '链接'):
                self.store.put_blob(b'test')
        finally:
            if directory.is_junction():
                directory.rmdir()
            os.replace(saved, directory)

    def test_failed_atomic_write_preserves_destination_and_cleans_temp(self):
        with patch('harness_launcher.storage.content_store.os.replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                atomic_write(self.source, b'new')
        self.assertEqual(self.source.read_bytes(), b'original data')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing semantics')
    def test_atomic_write_retries_transient_windows_reader_without_rewriting_temp(self):
        blocked = PermissionError('reader briefly holds destination')
        blocked.winerror = 32
        replace = os.replace
        calls = []
        def race(source, destination):
            calls.append(source)
            if len(calls) < 3:
                self.assertEqual(self.source.read_bytes(), b'original data')
                raise blocked
            replace(source, destination)
        with patch('harness_launcher.storage.content_store.os.replace', side_effect=race), patch('harness_launcher.storage.content_store.time.sleep'):
            atomic_write(self.source, b'complete new record')
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(self.source.read_bytes(), b'complete new record')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing semantics')
    def test_atomic_write_permanent_access_denial_is_bounded_and_preserves_original(self):
        blocked = PermissionError('permanent access denial')
        blocked.winerror = 5
        with patch('harness_launcher.storage.content_store.os.replace', side_effect=blocked) as replace, patch('harness_launcher.storage.content_store.time.sleep') as wait:
            with self.assertRaises(PermissionError):
                atomic_write(self.source, b'cannot publish')
        self.assertEqual(replace.call_count, 8)
        self.assertEqual(wait.call_count, 7)
        self.assertEqual(self.source.read_bytes(), b'original data')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_nonfinite_json_is_rejected_before_writing(self):
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(ValueError):
                self.store.object('snapshots', {'invalid': value})
        self.assertFalse(self.store.root.exists())

    def test_statistics_counts_only_files_and_reports_real_bytes(self):
        key = self.store.put_blob(b'test')
        ref = self.store.object('revisions', {'version': '1'})
        (self.store.root / 'blobs' / 'empty-directory').mkdir()
        stats = self.store.statistics()
        self.assertEqual(stats['groups']['blobs'], {'count': 1, 'bytes': 4})
        self.assertEqual(stats['groups']['revisions']['count'], 1)
        self.assertEqual(stats['bytes'], 4 + (self.store.root / ref).stat().st_size)


if __name__ == '__main__':
    unittest.main()
