"""Independent runtime/CLI checks must not invalidate each other's evidence."""
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness_launcher.domain.errors import LauncherError
from harness_launcher.verification.file_view import FileView
from harness_launcher.verification.ntfs_journal import JournalUnavailable
from harness_launcher.host.official_update import OfficialUpdate
from harness_launcher.verification.verification_cache import VerifiedContentCache


class RuntimeCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.runtime = self.base / 'resources/runtime'
        self.code = self.runtime / 'engine'
        self.cli = self.runtime / 'cli'
        self.code.mkdir(parents=True)
        self.cli.mkdir()
        files = {}
        for index in range(130):
            path = self.code / str(index)
            path.write_bytes(b'old')
            files[path.relative_to(self.base).as_posix()] = {'sha256': hashlib.sha256(b'old').hexdigest()}
        (self.cli / 'command.js').write_bytes(b'cli')
        self.cache = VerifiedContentCache(self.base / 'cache.json')
        self.manager = SimpleNamespace(settings={'executable': str(self.base / 'host.exe')},
            tracking=SimpleNamespace(load=lambda: {'officialPackage': 'official'}),
            official_packages=SimpleNamespace(read=lambda _: {'files': files}), verification_cache=self.cache)

    def check(self):
        return OfficialUpdate.check_runtime(SimpleNamespace(manager=self.manager))

    def test_cli_close_barriers_do_not_rescan_unrelated_runtime(self):
        if not self.cache.tree_checkpoints:
            self.skipTest('Requires NTFS journal')
        try:
            self.cache.tree_checkpoints.journal.checkpoint(self.code)
        except JournalUnavailable:
            self.skipTest('NTFS journal unavailable')
        self.assertEqual(self.check(), [])
        self.assertTrue(self.cache.tree_checkpoints.trees)
        FileView([self.cli], verification_cache=self.cache).hash_files()
        with patch.object(FileView, '_capture', side_effect=AssertionError('runtime unnecessarily rescanned')):
            self.assertEqual(self.check(), [])
        self.assertGreater(self.cache.tree_checkpoints.hits, 0)

    def test_runtime_content_change_missing_file_and_missing_tree_still_detected(self):
        self.assertEqual(self.check(), [])
        target = self.code / '0'
        stamp = target.stat()
        target.write_bytes(b'new')
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaisesRegex(LauncherError, '内容冲突'):
            self.check()
        target.unlink()
        self.assertEqual(self.check(), ['engine/0'])
        self.runtime.rename(self.base / 'parked-runtime')
        self.assertEqual(len(self.check()), 130)


if __name__ == '__main__':
    unittest.main()
