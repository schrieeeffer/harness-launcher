"""Enforce dependency directions that keep future schema changes isolated."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).parent


def dependencies(name):
    tree = ast.parse((ROOT / (name + '.py')).read_text(encoding='utf-8'))
    return {node.module.split('.')[0] for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module} | {
                alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}


class ArchitectureTests(unittest.TestCase):
    def test_runtime_never_depends_on_tests(self):
        for path in ROOT.glob('*.py'):
            if path.stem.startswith('test_'):
                continue
            with self.subTest(module=path.stem):
                imports = dependencies(path.stem)
                self.assertFalse(any(name.startswith('test_') for name in imports))

    def test_domain_and_storage_do_not_depend_on_application_or_adapter(self):
        forbidden = {'launcher', 'plugin_tracking', 'switch_plan', 'version_store', 'patch_modes', 'catalog'}
        for module in ('domain', 'recording_state', 'operation_models', 'errors', 'storage', 'settings_store', 'operations'):
            with self.subTest(module=module):
                self.assertFalse(dependencies(module) & forbidden)

    def test_adapter_and_version_repository_do_not_import_coordinators(self):
        for module in ('adapters', 'patch_modes', 'version_store', 'file_view'):
            with self.subTest(module=module):
                self.assertFalse(dependencies(module) & {'launcher', 'plugin_tracking', 'switch_plan'})


if __name__ == '__main__':
    unittest.main()
