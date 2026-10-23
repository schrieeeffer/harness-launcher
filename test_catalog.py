import base64
import json
import time
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

    def test_empty_patch_path_and_invalid_version_are_not_installable(self):
        for path in ['', '   ']:
            with self.subTest(path=path), patch.object(self.catalog, 'fetch', return_value=self.metadata(dsh={'bundle': {'patch': path}})):
                self.catalog.cache.clear()
                result = self.catalog.detail('dsh-example')
                self.assertFalse(result['bundle'])
                self.assertFalse(result['installable'])
        self.catalog.cache.clear()
        with patch.object(self.catalog, 'fetch', return_value=self.metadata(version='latest')):
            result = self.catalog.detail('dsh-example')
        self.assertTrue(result['bundle'])
        self.assertFalse(result['installable'])
        self.assertIn('版本', result['notice'])

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

    def test_final_supported_page_does_not_offer_an_invalid_next_page(self):
        result = {'objects': [{'package': {'name': 'dsh-example', 'version': '1.2.3'}}], 'total': 10000}
        with patch.object(self.catalog, 'fetch', return_value=result):
            self.assertTrue(self.catalog.search('example', 'community', 19)['hasMore'])
            last = self.catalog.search('example', 'community', 20)
        self.assertTrue(last['items'])
        self.assertFalse(last['hasMore'])

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

    def test_github_malformed_manifest_response_reports_catalog_error(self):
        for value in [None, [], {'encoding': 'base64', 'size': None, 'content': ''},
                      {'encoding': 'base64', 'size': 0, 'content': base64.b64encode(b' ' * (256 * 1024 + 1)).decode()}]:
            with self.subTest(value_type=type(value).__name__), patch.object(self.catalog, 'fetch', return_value=value):
                with self.assertRaises(CatalogError):
                    self.catalog.detail('example/plugin', 'github')

    def test_search_rejects_malformed_envelopes_as_catalog_errors(self):
        for source, items, total in [('community', 'objects', 'total'), ('github', 'items', 'total_count')]:
            for value in [None, [], {}, {items: None}, {items: [], total: 'many'}, {items: [], total: True}]:
                with self.subTest(source=source, value=value), patch.object(self.catalog, 'fetch', return_value=value):
                    with self.assertRaises(CatalogError):
                        self.catalog.search('tools', source)

    def test_malformed_search_response_uses_last_successful_cache(self):
        with patch.object(self.catalog, 'fetch', return_value={'objects': [{'package': {'name': 'dsh-example'}}], 'total': 1}):
            self.catalog.search('tools', 'community')
        key = ('search', 'tools', 'community', 0, 'npm')
        self.catalog.cache[key] = (0, self.catalog.cache[key][1])
        with patch.object(self.catalog, 'fetch', return_value={'objects': None}):
            result = self.catalog.search('tools', 'community')
        self.assertTrue(result['stale'])
        self.assertEqual(result['items'][0]['name'], 'dsh-example')
        self.assertIn('格式无效', result['warning'])

    def test_search_skips_malformed_rows_and_normalizes_invalid_star_counts(self):
        with patch.object(self.catalog, 'fetch', return_value={'objects': [None, 1, {'package': None}, {'package': {'name': 'dsh-good'}}], 'total': 4}):
            result = self.catalog.search('tools', 'community')
        self.assertEqual([item['name'] for item in result['items']], ['dsh-good'])
        with patch.object(self.catalog, 'fetch', return_value={'items': [None, {'full_name': None}, {'full_name': 'example/plugin', 'stargazers_count': None}], 'total_count': 3}):
            result = self.catalog.search('tools', 'github')
        self.assertEqual([(item['name'], item['stars']) for item in result['items']], [('example/plugin', 0)])

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

    def search_pages(self, pages, fail=(), slow=()):
        """Fake npm search keyed by the `from` offset; offsets in `fail` raise, offsets in `slow` answer last."""
        def fetch(url):
            offset = int(parse_qs(urlparse(url).query)['from'][0])
            if offset in slow:
                time.sleep(0.2)
            if offset in fail:
                raise CatalogError('offline')
            objects = [{'package': {'name': name, 'version': '1.0', 'date': '2026-10-01T08:00:00.000Z'},
                        'downloads': {'weekly': weekly, 'monthly': monthly}} for name, weekly, monthly in pages.get(offset, [])]
            return {'objects': objects, 'total': 8246}
        return patch.object(self.catalog, 'fetch', side_effect=fetch)

    def test_leaderboard_ranks_by_weekly_downloads_across_fixed_npm_pages(self):
        pages = {0: [('dsh-steady', 900, 4000), ('dsh-spiked', 64, 41764), ('../bad', 9999, 9999)],
                 250: [('dsh-steady', 1, 1), ('@demo/dsh-big', 5000, 20000)],
                 500: [('dsh-no-downloads', True, 3)], 750: []}
        # Page 0 answers last, so merging in completion order would keep the later page's duplicate.
        with self.search_pages(pages, slow={0}) as fetch:
            result = self.catalog.leaderboard('downloads')
        urls = [urlparse(call.args[0]) for call in fetch.call_args_list]
        self.assertEqual({u.hostname for u in urls}, {'registry.npmjs.org'})
        self.assertEqual(sorted(int(parse_qs(u.query)['from'][0]) for u in urls), [0, 250, 500, 750])
        self.assertEqual({parse_qs(u.query)['text'][0] for u in urls}, {'keywords:dsh-plugin,deepseek-harness'})
        self.assertEqual([(x['rank'], x['name'], x['weekly']) for x in result['items']],
                         [(1, '@demo/dsh-big', 5000), (2, 'dsh-steady', 900), (3, 'dsh-spiked', 64)])
        self.assertEqual(result['items'][0]['published'], '2026-10-01')
        self.assertEqual((result['scanned'], result['total'], result['warning']), (3, 8246, ''))

    def test_rising_board_keeps_plugins_downloaded_mostly_this_week(self):
        pages = {0: [('dsh-new', 3000, 3000), ('dsh-surging', 1000, 1900), ('dsh-steady', 5000, 21000),
                     ('dsh-tiny-new', 150, 150)]}
        with self.search_pages(pages) as fetch:
            rising = self.catalog.leaderboard('rising')
            downloads = self.catalog.leaderboard('downloads')
        self.assertEqual([x['name'] for x in rising['items']], ['dsh-new', 'dsh-surging'])
        self.assertEqual(downloads['items'][0]['name'], 'dsh-steady')
        self.assertEqual(fetch.call_count, 4, 'both boards should share one cached scan')
        self.assertTrue(downloads['cached'])

    def test_leaderboard_reports_partial_failure_and_complete_outage(self):
        with self.search_pages({0: [('dsh-example', 10, 40)]}, fail={250}):
            partial = self.catalog.leaderboard()
        self.assertEqual(len(partial['items']), 1)
        self.assertIn('1 页', partial['warning'])
        self.catalog.cache.clear()
        with self.search_pages({}, fail={0, 250, 500, 750}):
            with self.assertRaises(CatalogError) as caught:
                self.catalog.leaderboard()
        self.assertIn('registry.npmjs.org', str(caught.exception))

    def test_leaderboard_keeps_valid_pages_when_another_page_is_malformed(self):
        def fetch(url):
            offset = int(parse_qs(urlparse(url).query)['from'][0])
            if offset == 250:
                return None
            return {'objects': [None, {'package': None}, {'package': {'name': 'dsh-good'}, 'downloads': {'weekly': 20, 'monthly': 40}}], 'total': 1}
        with patch.object(self.catalog, 'fetch', side_effect=fetch):
            result = self.catalog.leaderboard()
        self.assertEqual([item['name'] for item in result['items']], ['dsh-good'])
        self.assertIn('1 页', result['warning'])

    def test_unknown_leaderboard_is_rejected_before_network(self):
        with patch.object(self.catalog, 'fetch') as fetch:
            with self.assertRaises(CatalogError):
                self.catalog.leaderboard('stars')
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
