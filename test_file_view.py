"""Directory enumeration and concurrent mutation boundaries."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from file_view import FileView
from errors import PatchError


class FileViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tree = self.root / 'tree'
        self.tree.mkdir()

    def raced_scan(self, target, mutate):
        original = os.scandir
        @contextmanager
        def scan(directory):
            with original(directory) as children:
                def entries():
                    for child in children:
                        if Path(child.path) != target:
                            yield child
                            continue
                        class Entry:
                            path = child.path
                            def stat(self, *, follow_symlinks):
                                self_result = child.stat(follow_symlinks=follow_symlinks)
                                mutate()
                                return self_result
                        yield Entry()
                yield entries()
        return patch('file_view.os.scandir', side_effect=scan)

    def test_enumeration_preserves_snapshot_metadata(self):
        child = self.tree / 'nested'
        child.mkdir()
        file = child / 'file'
        file.write_bytes(b'exact content')
        before = file.stat()
        view = FileView([self.tree])
        entry = view.entry(file)
        self.assertEqual(entry['size'], before.st_size)
        self.assertEqual(entry['mtime'], before.st_mtime_ns)
        self.assertEqual(view.entries[file]['stamp'], [before.st_size, before.st_mtime_ns, before.st_ctime_ns])
        self.assertEqual(view.read_bytes(file), b'exact content')

    def test_file_removed_after_enumeration_fails_before_content_use(self):
        target = self.tree / 'vanished'
        target.write_bytes(b'initial')
        with self.raced_scan(target, target.unlink):
            view = FileView([self.tree])
        with self.assertRaisesRegex(PatchError, '文件发生变化'):
            view.entry(target)

    def test_directory_replaced_after_enumeration_is_not_descended(self):
        target = self.tree / 'directory'
        target.mkdir()
        (target / 'child').write_bytes(b'preserve')
        saved = self.root / 'saved'
        def mutate():
            os.replace(target, saved)
            target.write_bytes(b'changed kind')
        with self.raced_scan(target, mutate):
            with self.assertRaisesRegex(PatchError, '目录被替换'):
                FileView([self.tree])
        self.assertEqual((saved / 'child').read_bytes(), b'preserve')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_explicit_root_cannot_bypass_a_junction_in_its_parent_path(self):
        outside = self.root / 'outside'
        outside.mkdir()
        child = outside / 'subtree'
        child.mkdir()
        (child / 'not-managed').write_bytes(b'outside fixture')
        link = self.tree / 'junction'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.addCleanup(lambda: link.rmdir() if link.is_junction() else None)
        with self.assertRaisesRegex(PatchError, '上级路径不能经过链接'):
            FileView([link / 'subtree'])
        self.assertEqual((child / 'not-managed').read_bytes(), b'outside fixture')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_junction_is_recorded_as_link_without_reading_target_tree(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'not-managed').write_bytes(b'outside fixture')
        link = self.tree / 'junction'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.addCleanup(lambda: link.rmdir() if link.is_junction() else None)
        view = FileView([self.tree])
        self.assertEqual(view.entry(link), {'type': 'link', 'target': str(outside)})
        self.assertEqual(view.children(link), [])
        self.assertNotIn(link / 'not-managed', view.entries)


if __name__ == '__main__':
    unittest.main()
