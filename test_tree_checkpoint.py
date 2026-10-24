"""Real NTFS integration and malformed journal regressions for fast verification."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from file_view import FileView
from errors import PatchError
from ntfs_journal import JournalUnavailable, parse_records
from storage import canonical
from verification_cache import VerifiedContentCache


class JournalParserTests(unittest.TestCase):
    def record(self, **changes):
        values = dict(length=64, major=2, minor=0, file=10, parent=20, usn=100,
                      timestamp=0, reason=1, source=0, security=0, attrs=32, namesize=0, nameoff=60)
        values.update(changes)
        return struct.pack('<q', 200) + struct.pack('<IHHQQqqIIIIHH', *values.values()) + b'\0' * 4

    def test_redacted_names_still_identify_changes(self):
        self.assertEqual(parse_records(self.record(), 100), (200, [(10, 20, 1, 100)]))

    def test_invalid_buffers_never_supply_a_partial_result(self):
        bad = [b'', struct.pack('<q', 99), self.record()[:-1]]
        bad += [self.record(**value) for value in [dict(major=3), dict(length=0), dict(length=128),
                dict(usn=99), dict(usn=200), dict(namesize=10), dict(nameoff=2), dict(minor=1)]]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(JournalUnavailable):
                parse_records(data, 100)


@unittest.skipUnless(os.name == 'nt', 'Requires Windows NTFS journal')
class TreeCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.tree = self.root / 'tree'
        self.tree.mkdir()
        self.file = self.tree / '0'
        for i in range(130):
            (self.tree / str(i)).write_bytes(b'old')
        self.cache_path = self.root / 'cache.json'
        self.cache = VerifiedContentCache(self.cache_path)
        self.checkpoints = self.cache.tree_checkpoints
        try:
            self.checkpoints.journal.root_identity(self.tree)
            self.checkpoints.journal.checkpoint(self.tree)
        except JournalUnavailable:
            self.skipTest('Unprivileged NTFS journal unavailable')

    def scan(self):
        view = FileView([self.tree], verification_cache=self.cache)
        return view.snapshot({'tree': self.tree}, lambda *_: None)

    def seal(self):
        result = self.scan()
        self.assertEqual(len(self.checkpoints.trees), 1)
        return result

    def assert_hit(self, expected):
        with patch.object(FileView, '_capture', side_effect=AssertionError('whole tree scanned')):
            self.assertEqual(self.scan(), expected)

    def test_reuses_verified_tree_and_survives_reload(self):
        expected = self.seal()
        self.assert_hit(expected)
        self.cache.flush()
        self.checkpoints.flush()
        self.cache = VerifiedContentCache(self.cache_path)
        self.checkpoints = self.cache.tree_checkpoints
        self.assert_hit(expected)

    def test_repeated_directory_moves_reuse_identity_without_growing_cache(self):
        expected = self.seal()
        for i in range(8):
            moved = self.root / ('parked' if i % 2 == 0 else 'tree')
            self.tree.rename(moved)
            self.tree = moved
            self.assert_hit(expected)
        self.assertEqual(len(self.checkpoints.trees), 1)

    def test_projected_entries_cannot_mutate_verified_checkpoint(self):
        expected = self.seal()
        entries, candidate = self.checkpoints.begin(self.tree)
        self.assertIsNone(candidate)
        entries[self.file]['hash'] = '0' * 64
        entries[self.file]['stamp'][0] = 999
        entries[self.file]['ntfsIdentity'][0] = 999
        entries.pop(self.tree)
        self.assert_hit(expected)
        self.file.write_bytes(b'new')
        self.assertNotEqual(self.scan(), expected)

    def test_path_projection_is_bounded_across_many_root_names(self):
        expected = self.seal()
        for index in range(5):
            moved = self.root / ('location-' + str(index))
            self.tree.rename(moved)
            self.tree = moved
            self.assert_hit(expected)
        self.assertEqual(len(self.checkpoints.projections), 1)
        self.assertEqual(len(next(iter(self.checkpoints.projections.values()))[1]), 2)

    def test_fingerprints_reuse_verified_tree_but_never_overlay_changes(self):
        self.seal()
        view = FileView([self.tree], verification_cache=self.cache)
        before = view.tree_hash(self.tree, content=True)
        clone = copy.deepcopy(view)
        clone.change(self.file, clone.entry(self.file), None, lambda a, b: a == b)
        self.assertNotEqual(clone.tree_hash(self.tree, content=True), before)
        fresh = FileView([self.tree], verification_cache=self.cache)
        with patch.object(fresh, 'children', side_effect=AssertionError('repeated tree fingerprint')):
            self.assertEqual(fresh.tree_hash(self.tree, content=True), before)
        fresh.change(self.file, fresh.entry(self.file), None, lambda a, b: a == b)
        fresh.tree_hash(self.tree, content=True)
        self.assertEqual(FileView([self.tree], verification_cache=self.cache).tree_hash(self.tree, content=True), before)
        self.file.write_bytes(b'new')
        actual = FileView([self.tree], verification_cache=self.cache).tree_hash(self.tree, content=True)
        self.assertNotEqual(actual, before)

    def test_same_size_restored_mtime_edit_invalidates_tree(self):
        before = self.seal()
        stamp = self.file.stat()
        self.file.write_bytes(b'new')
        os.utime(self.file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertNotEqual(self.scan(), before)

    def test_add_delete_and_rename_are_detected(self):
        self.seal()
        added = self.tree / 'added'
        added.write_bytes(b'new')
        self.assertIn('tree|added', self.scan())
        added.unlink()
        self.assertNotIn('tree|added', self.scan())
        self.file.rename(self.tree / 'renamed')
        result = self.scan()
        self.assertNotIn('tree|0', result)
        self.assertIn('tree|renamed', result)

    def test_external_hardlink_writes_are_detected_by_file_identity(self):
        before = self.seal()
        alias = self.root / 'alias'
        os.link(self.file, alias)
        self.seal()
        alias.write_bytes(b'new')
        # NTFS may leave the other hardlink's directory timestamp stale. A
        # fail-closed metadata conflict is also valid; an old hash is not.
        try:
            self.assertNotEqual(self.scan()['tree|0']['hash'], before['tree|0']['hash'])
        except PatchError:
            self.assertFalse(self.checkpoints.trees)
        self.assertEqual(self.file.read_bytes(), b'new')

    def test_root_replacement_cannot_reuse_old_index(self):
        before = self.seal()
        self.tree.rename(self.root / 'old-tree')
        self.tree.mkdir()
        self.file.write_bytes(b'new')
        self.assertNotEqual(self.scan(), before)

    def test_lingering_reader_prevents_sealing_coalesced_write_sequence(self):
        # NTFS emits no new same-reason record for later writers while this
        # data reader remains open. SHARE_READ alone is NOT sufficient here.
        with self.file.open('rb'):
            self.file.write_bytes(b'one')
            self.scan()
            self.assertFalse(self.checkpoints.trees)
            self.file.write_bytes(b'two')
            from storage import hash_file
            self.assertEqual(self.scan()['tree|0']['hash'], hash_file(self.file))
            self.assertFalse(self.checkpoints.trees)
        self.seal()

    def test_open_writer_invalidates_hit_and_does_not_reseal(self):
        before = self.seal()
        with self.file.open('r+b') as writer:
            writer.write(b'new'); writer.flush()
            try:
                self.assertNotEqual(self.scan(), before)
            except PatchError:
                pass  # Concurrent metadata instability must also fail closed.
            self.assertFalse(self.checkpoints.trees)
            writer.seek(0); writer.write(b'end'); writer.flush()
            from storage import hash_file
            try:
                self.assertEqual(self.scan()['tree|0']['hash'], hash_file(self.file))
            except PatchError:
                self.assertFalse(self.checkpoints.trees)
        self.seal()

    def test_attribute_only_handle_cannot_hide_repeated_writes_after_sealing(self):
        with self.checkpoints.journal.handle(self.file, 0x80):
            self.file.write_bytes(b'one')
            before = self.seal()
            self.file.write_bytes(b'two')
            self.assertNotEqual(self.scan(), before)
            after = self.seal()
            self.file.write_bytes(b'end')
            self.assertNotEqual(self.scan(), after)

    def test_attribute_handle_cannot_hide_repeated_nested_directory_renames(self):
        nested = self.tree / 'nested'
        nested.mkdir()
        child = nested / 'child'
        child.write_bytes(b'content')
        with self.checkpoints.journal.handle(nested, 0x80):
            parked = self.tree / 'renamed'
            nested.rename(parked); parked.rename(nested)
            before = self.seal()
            nested.rename(parked)
            self.assertNotEqual(self.scan(), before)
            self.assertIn('tree|renamed/child', self.scan())

    def test_legacy_checkpoint_without_close_barriers_is_ignored(self):
        self.seal()
        self.checkpoints.flush()
        envelope = json.loads(self.checkpoints.path.read_bytes())
        envelope['format'] = 1
        self.checkpoints.path.write_bytes(canonical(envelope))
        self.assertFalse(VerifiedContentCache(self.cache_path).tree_checkpoints.trees)

    def test_gap_falls_back_then_can_build_new_checkpoint(self):
        expected = self.seal()
        tree = next(iter(self.checkpoints.trees.values()))
        tree['checkpoint'] = (tree['checkpoint'][0] + 1, tree['checkpoint'][1])
        with patch.object(FileView, '_capture', autospec=True, wraps=FileView._capture) as capture:
            # Just prove it takes the scan; don't let a mocked capture certify.
            FileView([self.tree], verification_cache=self.cache)
            self.assertTrue(capture.called)
        self.assertEqual(self.seal(), expected)
        self.assert_hit(expected)

    def test_event_during_initial_hashing_cannot_establish_checkpoint(self):
        view = FileView([self.tree], verification_cache=self.cache)
        view.hash(self.file)
        self.file.write_bytes(b'new')
        view.hash_files()
        self.assertFalse(self.checkpoints.trees)

    def test_virtual_overlay_never_seals_real_tree(self):
        view = FileView([self.tree], verification_cache=self.cache)
        clone = copy.deepcopy(view)
        clone.hash_files()
        self.assertFalse(self.checkpoints.trees)

    def test_corrupt_persisted_index_and_unsupported_journal_fall_back(self):
        expected = self.seal()
        self.checkpoints.flush()
        envelope = json.loads(self.checkpoints.path.read_bytes())
        next(iter(envelope['trees'].values()))['entries']['0']['hash'] = '0' * 64
        self.checkpoints.path.write_bytes(canonical(envelope))
        self.cache = VerifiedContentCache(self.cache_path)
        self.checkpoints = self.cache.tree_checkpoints
        self.assertFalse(self.checkpoints.trees)
        with patch.object(self.checkpoints.journal, 'checkpoint', side_effect=JournalUnavailable):
            self.assertEqual(self.scan(), expected)
            self.assertFalse(self.checkpoints.trees)


@unittest.skipUnless(os.name == 'nt', 'Requires Windows NTFS journal')
class CheckpointTransactionTests(unittest.TestCase):
    # Exercise the existing transaction/crash assertions with small fixtures
    # eligible for real directory checkpoints, not mocked cached file content.
    import test_version_plan as plans
    wait_job = plans.VersionPlanTests.wait_job
    begin = plans.VersionPlanTests.begin
    install_fixture = plans.VersionPlanTests.install_fixture
    accept = plans.VersionPlanTests.accept
    close_runtime = plans.VersionPlanTests.close_runtime
    apply = plans.VersionPlanTests.apply
    record = plans.VersionPlanTests.record
    prepare = plans.VersionPlanTests.prepare

    def setUp(self):
        self.plans.VersionPlanTests.setUp(self)
        self.checkpoints = self.manager.verification_cache.tree_checkpoints
        self.checkpoints.MIN_FILES = 1
        try:
            self.checkpoints.journal.checkpoint(self.app)
        except JournalUnavailable:
            self.skipTest('Unprivileged NTFS journal unavailable')
        view = FileView([self.app], verification_cache=self.manager.verification_cache)
        view.hash_files()
        self.assertTrue(self.checkpoints.trees)

    def test_cached_tree_edit_after_preflight_stops_transaction(self):
        self.plans.VersionPlanTests.test_same_timestamp_edit_after_preflight_is_detected(self)

    def test_cached_tree_launch_failure_keeps_original_combination(self):
        self.plans.VersionPlanTests.test_launch_failure_restores_combination_lock_and_native_baseline_index(self)
        self.assertGreater(self.checkpoints.hits, 0)

    def test_cached_tree_crash_after_move_recovers_original_combination(self):
        self.plans.VersionPlanTests.test_whole_switch_crash_after_move_recovers_original_combination(self)
        self.assertGreater(self.checkpoints.hits, 0)


if __name__ == '__main__':
    unittest.main()
