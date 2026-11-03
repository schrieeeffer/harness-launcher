import importlib.util
import json
import marshal
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime_integrity import COMPARE, PYTHON_ROOT, python_extras, source_cache, verified_python_caches
from storage import ContentStore, hash_file
from verification_cache import VerifiedContentCache


class RuntimeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'module.py'
        self.source.write_text('value = 1\n', encoding='utf-8')
        self.cache = self.root / 'module.pyc'

    def compare(self, source=None, flags=0, optimize=0):
        code = compile(source or self.source.read_bytes(), 'different-build-directory/module.py', 'exec', optimize=optimize)
        self.cache.write_bytes(importlib.util.MAGIC_NUMBER + flags.to_bytes(4, 'little') + b'\0' * 8 + marshal.dumps(code))
        row = dict(name='module', source=str(self.source), cache=str(self.cache),
                   tag=sys.implementation.cache_tag, optimize=optimize)
        return self.run_helper(row)

    def run_helper(self, row):
        prefix = self.root / 'new-cache-prefix'
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-X', 'pycache_prefix=' + str(prefix), '-c', COMPARE],
                                input=json.dumps([row]).encode(), capture_output=True, check=True, timeout=20)
        self.assertFalse(prefix.exists())
        return json.loads(result.stdout)

    def test_generated_cache_with_new_filename_or_timestamp_matches_verified_source(self):
        for flags in (0, 1, 3):
            self.assertEqual(self.compare(flags=flags), ['module'])

    def test_changed_executable_code_is_rejected_and_not_executed(self):
        marker = self.root / 'must-not-exist'
        self.assertEqual(self.compare(f'open({str(marker)!r}, "w").write("bad")'), [])
        self.assertFalse(marker.exists())

    def test_nested_code_exception_handlers_and_constants_are_compared(self):
        self.source.write_text('def f():\n    try:\n        return 10\n    except ValueError:\n        return 20\n')
        self.assertEqual(self.compare(), ['module'])
        self.assertEqual(self.compare(self.source.read_text().replace('10', '11')), [])
        self.assertEqual(self.compare(self.source.read_text().replace('ValueError', 'TypeError')), [])

    def test_invalid_flags_and_optimization_mismatch_are_rejected(self):
        self.assertEqual(self.compare(flags=7), [])
        self.source.write_text('assert False\nvalue = 1\n')
        self.assertEqual(self.compare(optimize=1), ['module'])

    def setup_inventory(self):
        base = self.root / 'install'
        source = PYTHON_ROOT + 'Lib/module.py'
        cache = PYTHON_ROOT + 'Lib/__pycache__/module.cpython-313.pyc'
        files = {}
        for name, content in ((source, b'value = 1'), (cache, b'official pyc'), (PYTHON_ROOT + 'python.exe', b'verified interpreter')):
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            files[name] = hash_file(path)
        (base / cache).write_bytes(b'regenerated cache')
        manager = SimpleNamespace(verification_cache=VerifiedContentCache(self.root / 'cache.json'),
                                  official_packages=SimpleNamespace(store=ContentStore(self.root / 'packages')))
        return manager, base, files, source, cache

    def test_comparison_result_reused_by_content_and_shared_across_inventory_scopes(self):
        manager, base, files, source, cache = self.setup_inventory()
        result = SimpleNamespace(stdout=json.dumps([cache]).encode())
        with patch('runtime_integrity.subprocess.run', return_value=result) as run:
            self.assertEqual(verified_python_caches(manager, base, files, [cache]), {cache})
            with patch('runtime_integrity.repair.read_lease', side_effect=AssertionError('duplicate lease on a cache hit')):
                self.assertEqual(verified_python_caches(manager, base, dict(files, **{'DeepSeek Harness.exe': '0' * 64}), [cache]), {cache})
            self.assertEqual(run.call_count, 1)
            (base / cache).write_bytes(b'a different cache')
            verified_python_caches(manager, base, files, [cache])
            self.assertEqual(run.call_count, 2)

    def test_changed_source_or_interpreter_or_unknown_loader_blocks_helper(self):
        for kind in ('source', 'interpreter', 'loader'):
            with self.subTest(kind=kind):
                manager, base, files, source, cache = self.setup_inventory()
                name = source if kind == 'source' else PYTHON_ROOT + ('python.exe' if kind == 'interpreter' else 'python313._pth')
                (base / name).write_bytes(b'changed')
                with patch('runtime_integrity.subprocess.run') as run:
                    self.assertEqual(verified_python_caches(manager, base, files, [cache]), set())
                run.assert_not_called()
                if kind == 'loader':
                    (base / name).unlink()

    def test_sourceless_or_legacy_bytecode_is_not_exempt(self):
        self.assertIsNone(source_cache(PYTHON_ROOT + 'Lib/module.pyc', {}))
        self.assertIsNone(source_cache(PYTHON_ROOT + 'Lib/__pycache__/module.cpython-313.pyc', {}))

    def test_new_python_cache_is_inventoried_and_verified_but_unknown_loader_is_not_allowed(self):
        manager, base, files, source, cache = self.setup_inventory()
        del files[cache]
        self.assertEqual(python_extras(manager, base, files), [cache])
        with patch('runtime_integrity.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps([cache]).encode())):
            self.assertEqual(verified_python_caches(manager, base, files, [cache]), {cache})
        loader = PYTHON_ROOT + 'python313._pth'
        (base / loader).write_bytes(b'unknown loader')
        self.assertEqual(set(python_extras(manager, base, files)), {cache, loader})
        self.assertEqual(verified_python_caches(manager, base, files, [cache, loader]), set())


if __name__ == '__main__':
    unittest.main()
