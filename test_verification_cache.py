"""Cache hits must retain content-change detection across process restarts."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from errors import PatchError
from file_view import FileView
from storage import hash_file
from verification_cache import NtfsFingerprint, VerifiedContentCache


class VerificationCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.file = self.root / 'file'
        self.file.write_bytes(b'old')
        self.cache_path = self.root / 'cache.json'

    @contextmanager
    def fixed(self, path):
        yield lambda: (1, 2, 3, 4, 5, 3)

    def test_unchanged_fingerprint_reuses_hash_across_cache_reload(self):
        reader = Mock(wraps=hash_file)
        cache = VerifiedContentCache(self.cache_path, self.fixed)
        expected = cache.hash(self.file, reader)
        cache.flush()
        cache = VerifiedContentCache(self.cache_path, self.fixed)
        self.assertEqual(cache.hash(self.file, reader), expected)
        self.assertEqual(reader.call_count, 1)

    def test_unsupported_metadata_always_reads_contents(self):
        @contextmanager
        def unsupported(path):
            yield None
        cache = VerifiedContentCache(self.cache_path, unsupported)
        reader = Mock(wraps=hash_file)
        before = cache.hash(self.file, reader)
        self.file.write_bytes(b'new')
        self.assertNotEqual(before, cache.hash(self.file, reader))
        self.assertEqual(reader.call_count, 2)
        self.assertFalse(cache.entries)

    def test_corrupt_cache_cannot_supply_a_hit(self):
        cache = VerifiedContentCache(self.cache_path, self.fixed)
        expected = cache.hash(self.file, hash_file)
        cache.flush()
        saved = json.loads(self.cache_path.read_bytes())
        saved['entries']['1:2:3:4:5:3'] = '0' * 64
        self.cache_path.write_text(json.dumps(saved))
        reader = Mock(wraps=hash_file)
        self.assertEqual(VerifiedContentCache(self.cache_path, self.fixed).hash(self.file, reader), expected)
        reader.assert_called_once()

    def test_changed_or_unavailable_second_stamp_aborts_without_caching(self):
        for last in (None, (1, 2, 3, 4, 6, 3)):
            @contextmanager
            def changing(path):
                yield Mock(side_effect=[(1, 2, 3, 4, 5, 3), last])
            cache = VerifiedContentCache(self.cache_path, changing)
            with self.assertRaises(PatchError):
                cache.hash(self.file, hash_file)
            self.assertFalse(cache.entries)

    def require_ntfs(self):
        with NtfsFingerprint()(self.file) as read:
            if read is None or read() is None:
                self.skipTest('Requires local NTFS file identity and ChangeTime')

    def test_ntfs_same_size_restored_mtime_edit_invalidates_persisted_hit(self):
        self.require_ntfs()
        cache = VerifiedContentCache(self.cache_path)
        reader = Mock(wraps=hash_file)
        before = cache.hash(self.file, reader)
        cache.flush()
        stamp = self.file.stat()
        self.file.write_bytes(b'new')
        os.utime(self.file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertNotEqual(VerifiedContentCache(self.cache_path).hash(self.file, reader), before)
        self.assertEqual(reader.call_count, 2)

    def test_ntfs_replaced_file_invalidates_hit(self):
        self.require_ntfs()
        cache = VerifiedContentCache(self.cache_path)
        before = cache.hash(self.file, hash_file)
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'new')
        stamp = self.file.stat()
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(replacement, self.file)
        self.assertNotEqual(cache.hash(self.file, hash_file), before)

    def test_ntfs_cache_handle_actually_excludes_data_writers(self):
        self.require_ntfs()
        with NtfsFingerprint()(self.file) as read:
            self.assertIsNotNone(read())
            with self.assertRaises(PermissionError):
                self.file.write_bytes(b'new')
        self.file.write_bytes(b'new')  # No lock survives the verification.

    def test_ntfs_existing_writer_forces_full_read_instead_of_cache_hit(self):
        self.require_ntfs()
        cache = VerifiedContentCache(self.cache_path)
        reader = Mock(wraps=hash_file)
        before = cache.hash(self.file, reader)
        with self.file.open('r+b') as writer:
            writer.write(b'new')
            writer.flush()
            with NtfsFingerprint()(self.file) as read:
                self.assertIsNone(read)
            self.assertNotEqual(cache.hash(self.file, reader), before)
        self.assertEqual(reader.call_count, 2)

    def test_legacy_attribute_only_cache_is_ignored(self):
        cache = VerifiedContentCache(self.cache_path, self.fixed)
        cache.hash(self.file, hash_file)
        cache.flush()
        envelope = json.loads(self.cache_path.read_bytes())
        envelope['format'] = 1
        self.cache_path.write_text(json.dumps(envelope))
        reader = Mock(wraps=hash_file)
        VerifiedContentCache(self.cache_path, self.fixed).hash(self.file, reader)
        reader.assert_called_once()

    def test_repeated_edits_keep_only_current_fingerprint_per_identity(self):
        self.require_ntfs()
        cache = VerifiedContentCache(self.cache_path)
        for value in range(20):
            self.file.write_text(str(value))
            self.assertEqual(cache.hash(self.file, hash_file), hash_file(self.file))
        self.assertEqual(len(cache.entries), 1)
        cache.flush()
        self.assertEqual(len(VerifiedContentCache(self.cache_path).entries), 1)

    def test_cache_capacity_is_bounded_without_deleting_source_files(self):
        cache = VerifiedContentCache(self.cache_path, self.fixed)
        cache.LIMIT = 2
        for index in range(5):
            cache.entries[f'1:{index}:3:4:5:3'] = '0' * 64
        cache.dirty = True
        cache.flush()
        self.assertEqual(len(cache.entries), 2)
        self.assertEqual(len(cache.identities), 2)
        self.assertEqual(self.file.read_bytes(), b'old')

    def test_ntfs_view_rejects_file_changed_since_capture(self):
        self.require_ntfs()
        cache = VerifiedContentCache(self.cache_path)
        view = FileView([self.file], verification_cache=cache)
        self.file.write_bytes(b'longer contents')
        with self.assertRaises(PatchError):
            view.hash(self.file)

    def test_ntfs_directory_move_reuses_children_and_fresh_view_checks_edits(self):
        folder = self.root / 'tree'
        folder.mkdir()
        self.file = folder / 'child'
        self.file.write_bytes(b'old')
        self.require_ntfs()
        cache = VerifiedContentCache(self.cache_path)
        reader = Mock(wraps=hash_file)
        before = cache.hash(self.file, reader)
        moved = self.root / 'moved'
        folder.rename(moved)
        self.file = moved / 'child'
        self.assertEqual(cache.hash(self.file, reader), before)
        self.assertEqual(reader.call_count, 1)
        self.file.write_bytes(b'new')
        fresh = FileView([moved], verification_cache=cache)
        self.assertNotEqual(fresh.hash(self.file), before)



if __name__ == '__main__':
    unittest.main()
