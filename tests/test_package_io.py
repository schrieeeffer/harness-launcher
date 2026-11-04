"""Real multi-file checkpoints: progress, shared content and corrupt recovery data."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import time
import os

from harness_launcher.domain.errors import StorageError
from harness_launcher.storage.maintenance_files import CheckpointFiles
from harness_launcher.app.operations import OperationLog, progress_workflow
from harness_launcher.storage.content_store import ContentStore
from harness_launcher.storage.content_store import hash_file
from harness_launcher.storage.version_store import VersionStore


class PackageIOTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.profile = self.root / 'profile'
        self.package = self.profile / 'node_modules' / 'example-plugin'
        self.package.mkdir(parents=True)
        (self.package / 'package.json').write_text(json.dumps({'name': 'example-plugin', 'version': '1.0.0'}))
        for index in range(48):
            (self.package / f'file-{index}.js').write_bytes(('shared' if index % 2 else str(index)).encode())
        self.store = ContentStore(self.root / 'store')
        self.versions = VersionStore(self.profile, self.root / 'host.exe', self.store, None)

    def test_parallel_capture_preserves_every_file_and_shared_blob(self):
        reference = self.versions.package_snapshot('example-plugin')
        capsule = self.store.read_object(reference)
        entries = capsule['packages']['0']['files']
        self.assertEqual(len(entries), 49)
        for path in self.package.iterdir():
            data = path.read_bytes()
            self.assertEqual(entries[path.name]['hash'], hashlib.sha256(data).hexdigest())
            self.assertEqual(self.store.blob_bytes(entries[path.name]['hash']), data)
        self.assertEqual(len(list((self.store.root / 'blobs').iterdir())), 26)
        self.versions.verify_package('example-plugin', reference)
        # A damaged shared recovery object is never silently accepted or repaired.
        key = entries['file-1.js']['hash']
        (self.store.root / 'blobs' / key).write_bytes(b'corrupt')
        with self.assertRaises(StorageError):
            self.versions.verify_package('example-plugin', reference)

    def test_parallel_checkpoint_progress_runs_on_request_thread_and_restores(self):
        checkpoint = CheckpointFiles({'modules': self.profile / 'node_modules'}, self.store)
        caller = threading.get_ident()
        reports = []
        before = checkpoint.snapshot(progress=lambda done, total: reports.append((threading.get_ident(), done, total)))
        self.assertTrue(all(thread == caller for thread, _, _ in reports))
        self.assertEqual(reports[-1][1:], (49, 49))
        (self.package / 'file-0.js').write_bytes(b'new version')
        after = checkpoint.snapshot()
        checkpoint.restore(before, after)
        self.assertEqual((self.package / 'file-0.js').read_bytes(), b'0')

    def test_package_progress_is_visible_inside_maintenance_phase(self):
        log = OperationLog()
        identity = log.begin('maintenance/execute')
        with log.reporting(identity), progress_workflow() as phase:
            phase('packages', '保存维护前检查点')
            self.versions.package_snapshot('example-plugin')
        progress = log.snapshot()['current']['progress']
        self.assertEqual(progress['step'], 2)
        self.assertIn('校验恢复副本', progress['label'])
        self.assertEqual((progress['completed'], progress['total']), (26, 26))

    def test_identical_concurrent_files_publish_one_verified_blob(self):
        sources = [self.root / ('same-' + str(index)) for index in range(8)]
        for source in sources:
            source.write_bytes(b'identical package assets')
        barrier = threading.Barrier(8)
        def simultaneous_hash(path):
            value = hash_file(path)
            barrier.wait(timeout=10)
            return value
        replace = os.replace
        def slow_publish(source, target):
            time.sleep(.03)
            replace(source, target)
        def store(path):
            info = path.stat()
            return self.store.store_file(path, [info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        with patch('harness_launcher.storage.content_store.hash_file', side_effect=simultaneous_hash), \
                patch('harness_launcher.storage.content_store.os.replace', side_effect=slow_publish) as publish, \
                ThreadPoolExecutor(max_workers=8) as pool:
            keys = list(pool.map(store, sources))
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(self.store.blob_bytes(keys[0]), b'identical package assets')
        self.assertEqual(len(list((self.store.root / 'blobs').iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
