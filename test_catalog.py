import base64
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from catalog import Catalog, CatalogError, safe_url


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()

    def metadata(self, **overrides):
        return {'name': 'dsh-example', 'version': '1.2.3', 'description': '<script>untrusted</script>',
                'dsh': {'bundle': {'patch': './cordis.patch.yml'}},
                'repository': {'url': 'git+https://github.com/example/plugin.git'},
                'peerDependencies': {'@deepseek-ai/dsh': '~0.2.0'}, **overrides}

    def test_detail_pins_version_and_checks_bundle(self):
        with patch.object(self.catalog, 'fetch', return_value=self.metadata()) as fetch:
            result = self.catalog.detail('dsh-example')
        self.assertTrue(result['installable'])
        self.assertEqual(result['source'], 'dsh-example@1.2.3')
        self.assertEqual(result['repository'], 'https://github.com/example/plugin.git')
        self.assertEqual(result['compatibility'], {'@deepseek-ai/dsh': '~0.2.0'})
        self.assertEqual(fetch.call_count, 1)

    def test_component_library_is_not_installable(self):
        with patch.object(self.catalog, 'fetch', return_value=self.metadata(dsh={'client': {}})):
            result = self.catalog.detail('dsh-example')
        self.assertFalse(result['installable'])
        self.assertTrue(result['notice'])

    def test_unsafe_metadata_links_are_removed(self):
        with patch.object(self.catalog, 'fetch', return_value=self.metadata(homepage='javascript:alert(1)', repository={'url': 'https://user:secret@example.org'})):
            result = self.catalog.detail('dsh-example')
        self.assertEqual(result['homepage'], '')
        self.assertEqual(result['repository'], '')

    def test_invalid_package_repo_page_and_source_are_rejected_before_network(self):
        with patch.object(self.catalog, 'fetch') as fetch:
            for name in ['../credentials', 'https://localhost', 'foo\nbar', '@demo/x?token=x']:
                with self.assertRaises(CatalogError):
                    self.catalog.detail(name)
            for repository in ['example/../../secret', '../foo', 'example/..']:
                with self.assertRaises(CatalogError):
                    self.catalog.detail(repository, 'github')
            for values in [('', 'evil', 0), ('x' * 121, 'all', 0), ('', 'all', -1), ('', 'all', True)]:
                with self.assertRaises(CatalogError):
                    self.catalog.search(*values)
            fetch.assert_not_called()

    def test_official_starter_list_has_chinese_function_search(self):
        with patch.object(self.catalog, 'fetch') as fetch:
            result = self.catalog.search('语音', 'official')
        self.assertEqual(len(result['items']), 1)
        self.assertIn('voice', result['items'][0]['name'])
        fetch.assert_not_called()

    def test_all_source_pagination_does_not_repeat_official_starter_list(self):
        with patch.object(self.catalog, 'fetch') as fetch:
            rows, more = self.catalog.search_one('', 'official', 1, 'npm')
        self.assertEqual(rows, [])
        self.assertFalse(more)
        fetch.assert_not_called()

    def test_partial_provider_failure_keeps_other_results_and_reports_source(self):
        def provider(query, source, page, registry):
            if source == 'github':
                raise CatalogError('限流')
            return [{'name': 'dsh-example', 'kind': 'community', 'provider': 'npm', 'stars': 0}], False
        with patch.object(self.catalog, 'search_one', side_effect=provider):
            result = self.catalog.search('tools')
        self.assertTrue(result['items'])
        self.assertIn('github', result['warning'])

    def test_complete_outage_is_not_reported_as_no_matches(self):
        with patch.object(self.catalog, 'search_one', side_effect=CatalogError('offline')):
            with self.assertRaises(CatalogError):
                self.catalog.search('tools')

    def test_cache_reuses_reads_and_marks_offline_fallback(self):
        with patch.object(self.catalog, 'fetch', return_value=self.metadata()) as fetch:
            self.catalog.detail('dsh-example')
            second = self.catalog.detail('dsh-example')
        self.assertTrue(second['cached'])
        self.assertEqual(fetch.call_count, 1)
        key = ('detail', 'npm', 'dsh-example', 'npm')
        self.catalog.cache[key] = (0, self.catalog.cache[key][1])
        with patch.object(self.catalog, 'fetch', side_effect=CatalogError('offline')):
            fallback = self.catalog.detail('dsh-example')
        self.assertTrue(fallback['stale'])
        self.assertIn('offline', fallback['warning'])

    def test_github_detail_reads_manifest_without_executing_it(self):
        raw = json.dumps(self.metadata(scripts={'postinstall': 'arbitrary code'})).encode()
        with patch.object(self.catalog, 'fetch', return_value={'encoding': 'base64', 'size': len(raw), 'content': base64.b64encode(raw).decode()}):
            result = self.catalog.detail('example/plugin', 'github')
        self.assertEqual(result['source'], 'git+https://github.com/example/plugin.git')
        self.assertEqual(result['kind'], 'community')
        self.assertTrue(result['installable'])

    def test_github_query_cannot_escape_topic_with_search_qualifiers(self):
        with patch.object(self.catalog, 'fetch', return_value={'items': [], 'total_count': 0}) as fetch:
            self.catalog.search('x repo:secret/passwords', 'github')
        query = parse_qs(urlparse(fetch.call_args[0][0]).query)['q'][0]
        self.assertIn('topic:dsh-plugin', query)
        self.assertNotIn('repo:', query)

    def test_npm_search_preserves_pagination_and_filters_official_scope(self):
        objects = [{'package': {'name': name, 'version': '1.0'}} for name in ['dsh-example', '@deepseek-ai/dsh-base', '../bad']]
        with patch.object(self.catalog, 'fetch', return_value={'objects': objects, 'total': 90}) as fetch:
            rows = self.catalog.search('tools', 'community', 1)
        query = parse_qs(urlparse(fetch.call_args[0][0]).query)
        self.assertEqual(query['from'], ['18'])
        self.assertEqual([x['name'] for x in rows['items']], ['dsh-example'])
        self.assertTrue(rows['hasMore'])

    def test_mirror_search_uses_plain_ecosystem_query(self):
        with patch.object(self.catalog, 'fetch', return_value={'objects': [], 'total': 0}) as fetch:
            self.catalog.search('memory', 'community', registry='mirror')
        query = parse_qs(urlparse(fetch.call_args[0][0]).query)['text'][0]
        self.assertEqual(query, 'dsh memory')
        self.assertNotIn('keywords:', query)

    def test_mismatched_package_name_cannot_supply_install_source(self):
        with patch.object(self.catalog, 'fetch', return_value=self.metadata(name='other')):
            with self.assertRaises(CatalogError):
                self.catalog.detail('dsh-example')

    def test_network_targets_are_fixed(self):
        with self.assertRaises(CatalogError):
            self.catalog.fetch('http://127.0.0.1/private')
        self.assertEqual(safe_url('javascript:alert(1)'), '')
        self.assertEqual(safe_url('https://user:secret@example.com'), '')


if __name__ == '__main__':
    unittest.main()
