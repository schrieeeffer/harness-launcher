import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from launcher import Manager, MenuError, OFFICIAL, Server, Handler, atomic_write


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.home = root / 'home'
        self.profile = self.home / 'profiles' / 'desktop'
        self.profile.mkdir(parents=True)
        self.original = {'name': 'test', 'private': True, 'dependencies': {'plugin-a': 'link:../a', '@demo/plugin-b': '1.0'},
                         'custom': {'keep': [1, 2]}, 'dsh': {'profile': {'bundles': [*OFFICIAL, 'plugin-a']}, 'other': True}}
        self.path = self.profile / 'package.json'
        self.raw = (json.dumps(self.original, indent=4) + '\r\n').encode()
        self.path.write_bytes(self.raw)
        for name in ['plugin-a', '@demo/plugin-b']:
            directory = self.profile / 'node_modules' / name
            directory.mkdir(parents=True)
            (directory / 'package.json').write_text(json.dumps({'name': name, 'version': '1.2', 'description': '<img onerror=alert(1)>', 'dsh': {'bundle': {'patch': './cordis.patch.yml'}}}))
        nonplugin = self.profile / 'node_modules' / 'utility'
        nonplugin.mkdir()
        (nonplugin / 'package.json').write_text('{"name":"utility"}')
        self.exe = root / 'DeepSeek Harness.exe'
        self.exe.write_bytes(b'not a real executable')
        self.manager = Manager(root / 'data', self.home, self.exe)
        self.process_patch = patch('launcher.running_harness', return_value=[])
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def test_discovery_includes_disabled_scoped_bundle_but_not_utility(self):
        state = self.manager.state()
        self.assertEqual({p['id'] for p in state['plugins']}, {'plugin-a', '@demo/plugin-b'})
        self.assertEqual(state['enabled'], ['plugin-a'])

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows short-path alias')
    def test_windows_short_home_alias_uses_same_profile_and_snapshot_identity(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetShortPathNameW.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        self.assertTrue(kernel.GetShortPathNameW(str(self.home), buffer, len(buffer)))
        alias = Manager(self.manager.data, home=buffer.value, executable=self.exe)
        self.assertEqual(alias.home, self.manager.home)
        self.assertEqual(alias.tracking.root, self.manager.tracking.root)
        self.assertEqual(alias.manifest()[0], self.raw)

    def test_disable_all_preserves_every_other_field_and_can_reenable(self):
        self.manager.apply([], self.manager.state()['revision'])
        after = json.loads(self.path.read_bytes())
        self.assertEqual(after['dsh']['profile']['bundles'], list(OFFICIAL))
        after['dsh']['profile']['bundles'] = self.original['dsh']['profile']['bundles']
        self.assertEqual(after, self.original)
        self.assertEqual(len(self.manager.state()['plugins']), 2)
        self.manager.apply(['plugin-a', '@demo/plugin-b'], self.manager.state()['revision'])
        self.assertEqual(self.manager.state()['enabled'], ['plugin-a', '@demo/plugin-b'])

    def test_restore_exact_original_bytes_and_save_pre_restore(self):
        self.manager.apply([], self.manager.state()['revision'])
        backup = self.manager.state()['backups'][0]['id']
        self.manager.restore(backup, self.manager.state()['revision'])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(len(self.manager.state()['backups']), 2)

    def test_external_edits_are_not_overwritten(self):
        revision = self.manager.state()['revision']
        raw = self.raw + b' '
        self.path.write_bytes(raw)
        with self.assertRaises(MenuError):
            self.manager.apply([], revision)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_running_app_blocks_save_and_launch(self):
        revision = self.manager.state()['revision']
        with patch('launcher.running_harness', return_value=[123]):
            for launch in [False, True]:
                with self.assertRaises(MenuError):
                    self.manager.apply([], revision, launch)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_missing_unknown_duplicate_and_path_traversal_are_rejected(self):
        for selection in [['unknown'], ['../evil'], ['plugin-a', 'plugin-a']]:
            with self.assertRaises(MenuError):
                self.manager.apply(selection, self.manager.state()['revision'])
        with self.assertRaises(MenuError):
            self.manager.restore('../settings.json', self.manager.state()['revision'])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_removed_installed_package_cannot_be_enabled(self):
        (self.profile / 'node_modules' / 'plugin-a' / 'package.json').unlink()
        with self.assertRaises(MenuError):
            self.manager.apply(['plugin-a'], self.manager.state()['revision'])
        self.manager.apply([], self.manager.state()['revision'])
        self.assertEqual(self.manager.state()['enabled'], [])

    def test_failed_spawn_rolls_back_original_bytes(self):
        with patch('launcher.subprocess.Popen', side_effect=OSError('test launch failure')):
            with self.assertRaises(MenuError):
                self.manager.apply([], self.manager.state()['revision'], True)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_launch_passes_home_without_shell(self):
        with patch('launcher.subprocess.Popen') as spawn:
            self.manager.apply(['plugin-a'], self.manager.state()['revision'], True)
            args, kwargs = spawn.call_args
            self.assertEqual(args[0], [str(self.exe)])
            self.assertEqual(kwargs['env']['DSH_HOME'], str(self.home))
            self.assertNotIn('shell', kwargs)

    def test_scheme_persistence_does_not_change_harness(self):
        self.manager.scheme('开发', ['@demo/plugin-b'])
        reread = Manager(self.manager.data)
        self.assertEqual(reread.settings['schemes']['开发'], ['@demo/plugin-b'])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.manager.scheme('开发', delete=True)
        self.assertEqual(self.manager.settings['schemes'], {})

    def test_http_requires_token_and_same_origin(self):
        server = Server(('127.0.0.1', 0), Handler)
        server.manager, server.token, server.last_seen = self.manager, 'test-token', 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}/api/state'
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url)
        self.assertEqual(caught.exception.code, 403)
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer test-token'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)['enabled'], ['plugin-a'])
        request.add_header('Origin', 'https://example.com')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)

    def test_scheme_import_is_atomic_on_invalid_plugin_or_capacity(self):
        self.manager.scheme('original', ['plugin-a'])
        with self.assertRaises(MenuError):
            self.manager.import_schemes({'valid': [], 'invalid': ['unknown']})
        self.assertEqual(self.manager.settings['schemes'], {'original': ['plugin-a']})
        self.manager.settings['schemes'] = {str(n): [] for n in range(100)}
        with self.assertRaises(MenuError):
            self.manager.import_schemes({'one too many': []})
        self.assertEqual(len(self.manager.settings['schemes']), 100)
        self.manager.import_schemes({'0': ['plugin-a']})
        self.assertEqual(self.manager.settings['schemes']['0'], ['plugin-a'])

    def test_live_server_port_cannot_be_bound_twice(self):
        server = Server(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        with self.assertRaises(OSError):
            Server(('127.0.0.1', server.server_port), Handler)

    def test_catalog_http_and_first_run_settings_remain_authenticated(self):
        from catalog import Catalog, CatalogError
        server = Server(('127.0.0.1', 0), Handler)
        server.manager, server.token, server.last_seen = self.manager, 'test-token', 0
        server.catalog = Catalog()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}/api/'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def read(path, authorized=True):
            headers = {'Authorization': 'Bearer test-token'} if authorized else {}
            with opener.open(urllib.request.Request(base + path, headers=headers)) as response:
                return json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            read('catalog/search', False)
        self.assertEqual(caught.exception.code, 403)
        self.path.unlink()
        self.assertEqual(read('settings')['home'], str(self.home))
        result = read('catalog/search?source=official')
        self.assertEqual(len(result['items']), 4)
        with patch.object(server.catalog, 'detail', side_effect=CatalogError('network unavailable')):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                read('catalog/detail?name=dsh-test')
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(json.load(caught.exception)['error'], 'network unavailable')


if __name__ == '__main__':
    unittest.main()
