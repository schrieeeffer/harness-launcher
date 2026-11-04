"""Enforce dependency directions that keep future schema changes isolated."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'harness_launcher'


def dependencies(name):
    tree = ast.parse((ROOT / (name.replace('.', '/') + '.py')).read_text(encoding='utf-8'))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
            result.update(node.module + '.' + alias.name for alias in node.names)
    return result


class ArchitectureTests(unittest.TestCase):
    def test_runtime_never_depends_on_tests(self):
        for path in PACKAGE.rglob('*.py'):
            module = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
            with self.subTest(module=module):
                imports = dependencies(module)
                self.assertFalse(any(name.split('.')[0] == 'tests' or name.startswith('test_') for name in imports))

    def test_domain_and_storage_do_not_depend_on_application_or_adapter(self):
        forbidden = {'app.launcher', 'plugins.plugin_tracking', 'app.switch_plan',
                     'storage.version_store', 'host.patch_modes', 'plugins.catalog'}
        forbidden = {'harness_launcher.' + name for name in forbidden}
        for module in ('domain.models', 'domain.recording_state', 'domain.operation_models', 'domain.errors',
                       'storage.content_store', 'storage.settings_store', 'app.operations'):
            with self.subTest(module=module):
                self.assertFalse(dependencies('harness_launcher.' + module) & forbidden)

    def test_adapter_and_version_repository_do_not_import_coordinators(self):
        forbidden = {'harness_launcher.app.launcher', 'harness_launcher.plugins.plugin_tracking',
                     'harness_launcher.app.switch_plan'}
        for module in ('host.adapters', 'host.patch_modes', 'storage.version_store', 'verification.file_view'):
            with self.subTest(module=module):
                self.assertFalse(dependencies('harness_launcher.' + module) & forbidden)


if __name__ == '__main__':
    unittest.main()
