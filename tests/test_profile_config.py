import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness_launcher.domain.errors import PatchError
from harness_launcher.host.profile_config import parse_documents, verify_entries


class ProfileConfigTests(unittest.TestCase):
    def setUp(self):
        parse_documents.cache_clear()

    def test_model_and_provider_names_are_not_plugin_loaders(self):
        verify_entries([{'name': '@deepseek-ai/dsh-llm-pi-ai', 'config': {
            'providers': [{'name': 'my provider', 'models': [{'name': 'GPT-6 Astra'}]}],
            'example': {'insert': [{'name': 'example text'}]}}}], 'cordis.patch.yml')

    def test_loader_inserts_groups_and_flow_documents_cannot_hide_plugins(self):
        values = [
            [{'insert': [{'name': '@local/custom'}]}],
            [{'group': True, 'config': [{'name': '@local/custom'}]}],
            [{'disabled': True, 'insert': [{'name': '@local/custom'}]}],
            [{'name': {'__jsExpr': 'expression'}}],
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(PatchError):
                verify_entries(value, 'cordis.patch.yml')

    def test_disabled_component_configuration_is_retained(self):
        value = [{'name': '@local/custom', 'disabled': True, 'config': {'name': 'personal setting'}}]
        original = json.dumps(value)
        verify_entries(value, 'cordis.patch.yml')
        self.assertEqual(json.dumps(value), original)

    def test_only_literal_disabled_true_bypasses_active_component_check(self):
        for disabled in [False, 'false', {'__jsExpr': 'false'}, None]:
            with self.subTest(disabled=disabled), self.assertRaises(PatchError):
                verify_entries([{'name': '@local/custom', 'disabled': disabled}], 'cordis.yml')

    def test_yaml_uses_safe_official_parser_and_identical_bytes_are_parsed_once(self):
        document = [{'name': '@deepseek-ai/settings', 'config': {'name': 'personal'}}]
        result = SimpleNamespace(returncode=0, stdout=json.dumps([document]).encode())
        with patch('harness_launcher.host.profile_config.subprocess.run', return_value=result) as run:
            for _ in range(2):
                self.assertEqual(parse_documents('Harness.exe', 'official.asar', ('- name: example',)), (document,))
        self.assertEqual(run.call_count, 1)
        self.assertIn('official.asar/dsh/node_modules/js-yaml', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env']['ELECTRON_RUN_AS_NODE'], '1')

    def test_dynamic_yaml_parser_error_does_not_leak_credentials(self):
        result = SimpleNamespace(returncode=1, stdout=b'', stderr=b'private credential')
        with patch('harness_launcher.host.profile_config.subprocess.run', return_value=result), self.assertRaises(PatchError) as caught:
            parse_documents('Harness.exe', 'official.asar', ('secret: !!js private-credential',))
        self.assertNotIn('private', str(caught.exception))

    def test_parser_timeout_stops_without_accepting_document(self):
        with patch('harness_launcher.host.profile_config.subprocess.run', side_effect=subprocess.TimeoutExpired('parser', 10)), self.assertRaises(PatchError):
            parse_documents('Harness.exe', 'official.asar', ('- name: example',))

    def test_json_profiles_do_not_spawn_parser(self):
        with patch('harness_launcher.host.profile_config.subprocess.run', side_effect=AssertionError('unnecessary child')):
            self.assertEqual(parse_documents('Harness.exe', 'official.asar', ('[]',)), ([],))

    def test_unsupported_structure_does_not_claim_native(self):
        for value in [{}, ['invalid entry']]:
            with self.subTest(value=value), self.assertRaises(PatchError):
                verify_entries(value, 'cordis.yml')

