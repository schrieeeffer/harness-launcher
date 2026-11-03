import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog import Catalog, CatalogError
from install_sources import InstallSources


META = {'name': 'dsh-test', 'version': '1.2.3', 'main': './lib/index.js',
        'exports': {'.': './lib/index.js', './client': {'types': './lib/client.d.ts', 'import': './lib/client.js'}},
        'dsh': {'bundle': {'patch': './cordis.patch.yml'}},
        'dist': {'integrity': 'sha512-' + base64.b64encode(b'x' * 64).decode(),
                 'tarball': 'https://registry.npmjs.org/dsh-test/-/dsh-test-1.2.3.tgz'}}


class InstallSourceTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.service = InstallSources(self.catalog)

    def test_npm_pins_version_reuses_metadata_and_binds_proof(self):
        with patch.object(self.catalog, 'fetch', return_value=META) as fetch:
            proof = self.service.inspect('dsh-test', 'npm')
            self.service.inspect('dsh-test', 'npm')
        fetch.assert_called_once()
        self.assertEqual(proof['source'], 'dsh-test@1.2.3')
        self.service.authorize('dsh-test', proof['source'], proof['token'])
        for name, source, token in [('dsh-test', 'dsh-test@latest', proof['token']),
                                    ('another', proof['source'], proof['token']), ('dsh-test', proof['source'], 'fake')]:
            with self.assertRaises(CatalogError):
                self.service.authorize(name, source, token)
        with patch('install_sources.time.time', return_value=proof['expiresAt'] + 1), self.assertRaises(CatalogError):
            self.service.authorize('dsh-test', proof['source'], proof['token'])

    def test_missing_bundle_checksum_or_mismatched_name_blocks_npm(self):
        for edit in [lambda m: m.pop('dsh'), lambda m: m['dist'].pop('integrity'),
                     lambda m: m.update(name='other'), lambda m: m['dist'].update(tarball='http://evil.test/x')]:
            meta = copy.deepcopy(META); edit(meta)
            with patch.object(self.catalog, 'fetch', return_value=meta), self.assertRaises(CatalogError):
                self.service.inspect('dsh-test', 'npm')
            self.catalog.cache.clear()

    def test_stale_metadata_does_not_enable_install(self):
        with patch.object(self.catalog, 'cached', return_value={**META, 'stale': True}), self.assertRaisesRegex(CatalogError, '过期'):
            self.service.inspect('dsh-test', 'npm')

    def test_exact_version_cannot_be_substituted_by_registry(self):
        with patch.object(self.catalog, 'fetch', return_value=META), self.assertRaisesRegex(CatalogError, '版本不一致'):
            self.service.inspect('dsh-test', 'npm', '1.2.2')

    def git_fetch(self, url):
        if '/commits/' in url:
            return {'sha': 'a' * 40}
        if '/contents/package.json' in url:
            return {'encoding': 'base64', 'content': base64.b64encode(json.dumps(META).encode()).decode()}
        return {'tree': [{'path': path, 'type': 'blob', 'mode': '100644'} for path in
                         ['package.json', 'cordis.patch.yml', 'lib/index.js', 'lib/client.js']]}

    def test_git_requires_actual_build_and_pins_commit(self):
        with patch.object(self.catalog, 'fetch', side_effect=self.git_fetch):
            proof = self.service.inspect('dsh-test', 'git', 'https://github.com/author/project#v1.2.3')
        self.assertEqual(proof['source'], 'git+https://github.com/author/project.git#' + 'a' * 40)
        self.service.authorize('dsh-test', proof['source'], proof['token'])

    def test_git_missing_root_or_build_never_enables(self):
        def fetch(url):
            result = self.git_fetch(url)
            if '/git/trees/' in url:
                result['tree'] = result['tree'][:2]
            return result
        with patch.object(self.catalog, 'fetch', side_effect=fetch), self.assertRaisesRegex(CatalogError, '缺少安装文件'):
            self.service.inspect('dsh-test', 'git', 'https://github.com/author/project')
        self.catalog.cache.clear()
        with patch.object(self.catalog, 'fetch', side_effect=lambda url: {'sha': 'a' * 40} if '/commits/' in url else (_ for _ in ()).throw(CatalogError('404'))), self.assertRaisesRegex(CatalogError, '根目录'):
            self.service.inspect('dsh-test', 'git', 'https://github.com/author/project')

    def test_local_build_and_changes_before_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'package.json').write_text(json.dumps(META))
            (root / 'cordis.patch.yml').write_text('plugins: []')
            with self.assertRaises(CatalogError):
                self.service.inspect('dsh-test', 'local', temp)
            (root / 'lib').mkdir()
            (root / 'lib/index.js').write_text('initial')
            (root / 'lib/client.js').write_text('initial')
            proof = self.service.inspect('dsh-test', 'local', temp)
            self.service.authorize('dsh-test', proof['source'], proof['token'])
            (root / 'lib/client.js').write_text('changed')
            with self.assertRaisesRegex(CatalogError, '发生变化'):
                self.service.authorize('dsh-test', proof['source'], proof['token'])

    def test_untrusted_paths_and_repository_urls_rejected(self):
        for path in ['../escape.js', '/absolute.js', 'C:/other.js', 'lib\\x.js']:
            meta = copy.deepcopy(META); meta['main'] = path
            with patch.object(self.catalog, 'fetch', return_value=meta), self.assertRaises(CatalogError):
                self.service.inspect('dsh-test', 'npm')
            self.catalog.cache.clear()
        for url in ['file:///tmp/x', 'https://a:b@github.com/a/b', 'https://github.com.evil.test/a/b']:
            with self.assertRaises(CatalogError):
                self.service.inspect('dsh-test', 'git', url)


if __name__ == '__main__':
    unittest.main()
