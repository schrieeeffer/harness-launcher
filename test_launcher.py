import json
import http.client
import os
from pathlib import Path
import shutil
import subprocess
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from launcher import (Manager, MenuError, OFFICIAL, Server, Handler, UNINSTALL_KEYS, atomic_write,
                      default_executable, registry_executable, file_hash)


def make_runtime(exe):
    from runtime_repair import ANCHORS
    for name in ANCHORS:
        path = exe.parent / 'resources/runtime' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b'official runtime fixture')


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
        make_runtime(self.exe)
        self.manager = Manager(root / 'data', self.home, self.exe)
        # Synthetic, already verified bundle-only records; unknown real plugins
        # now block every file-combination switch, not only native mode.
        tracker = self.manager.tracking
        value = tracker.load()
        for name in ['plugin-a', '@demo/plugin-b']:
            value['records'][name] = {'version': '1.2', 'enabled': name == 'plugin-a',
                                      'delta': tracker.object('deltas', {'changes': []}),
                                      'count': 0, 'baseSelection': [], 'history': []}
            record = value['records'][name]
            record.update(source='fixture', hostHash=file_hash(self.exe), baseVersions={},
                          baseline=tracker.object('snapshots', {}), after=tracker.object('snapshots', {}), manifestBefore=None)
            record['package'] = tracker.versions.package_snapshot(name, 'fixture')
            record['revision'] = tracker.versions.revision(name, record, record['package'], 'fixture')
            value['order'].append(name)
        tracker.save(value)
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

    def test_restore_rejects_duplicate_official_components(self):
        backup = self.manager.backup(self.raw, self.original['dsh']['profile']['bundles'], 'fixture')
        path = self.manager.data / 'backups' / backup
        info = json.loads(path.read_bytes())
        content = json.loads(info['content'])
        content['dsh']['profile']['bundles'].append(OFFICIAL[0])
        info['content'] = json.dumps(content)
        path.write_text(json.dumps(info), encoding='utf-8')
        with self.assertRaisesRegex(MenuError, '备份内容无效'):
            self.manager.restore(backup, self.manager.state()['revision'])
        self.assertEqual(self.path.read_bytes(), self.raw)

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
        with self.assertRaises(MenuError):
            self.manager.apply([], self.manager.state()['revision'])
        self.assertEqual(self.path.read_bytes(), self.raw)

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

    def test_missing_executable_blocks_switching_and_offers_detected_install(self):
        self.manager.settings['executable'] = str(self.exe.parent / 'moved' / 'DeepSeek Harness.exe')
        with patch('launcher.detect_executable', return_value=self.exe):
            state = self.manager.state()
        self.assertFalse(state['executableExists'])
        self.assertEqual(state['detectedExecutable'], str(self.exe))
        with self.assertRaises(MenuError):
            self.manager.apply([], state['revision'])
        self.assertEqual(self.path.read_bytes(), self.raw)
        with self.assertRaises(MenuError):
            self.manager.tracking.begin('plugin-new')
        self.assertIsNone(self.manager.tracking.summary()['session'])

    def test_scheme_persistence_does_not_change_harness(self):
        self.manager.scheme('开发', ['@demo/plugin-b'])
        reread = Manager(self.manager.data)
        self.assertEqual(reread.settings['schemes']['开发'], ['@demo/plugin-b'])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.manager.scheme('开发', delete=True)
        self.assertEqual(self.manager.settings['schemes'], {})

    def test_failed_scheme_version_binding_preserves_previous_scheme(self):
        from patch_modes import PatchError
        self.manager.scheme('existing', ['plugin-a'])
        saved = self.manager.settings_file.read_bytes()
        lock = self.manager.settings['schemeLocks']['existing']
        with patch.object(self.manager.tracking.versions, 'lock', side_effect=PatchError('invalid package')):
            with self.assertRaises(PatchError):
                self.manager.scheme('existing', ['@demo/plugin-b'])
        self.assertEqual(self.manager.settings['schemes']['existing'], ['plugin-a'])
        self.assertEqual(self.manager.settings['schemeLocks']['existing'], lock)
        self.assertEqual(self.manager.settings_file.read_bytes(), saved)

    def test_backup_limit_is_applied_after_filtering_current_profile(self):
        self.manager.backup(self.raw, self.original['dsh']['profile']['bundles'], 'fixture')
        for index in range(100):
            (self.manager.data / 'backups' / f'z-{index:03}.json').write_text(json.dumps({
                'format': 3, 'target': str(self.home / 'other.json'),
                'time': 'fixture', 'reason': 'other profile', 'plugins': []}))
        self.assertEqual(len(self.manager.backup_list()), 1)

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

    def service(self):
        server = Server(('127.0.0.1', 0), Handler)
        server.manager, server.token = self.manager, 'test-token'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(2))
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, body=None, **headers):
            headers = {'Authorization': 'Bearer test-token', 'Content-Type': 'application/json', **headers}
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/' + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
            with opener.open(req, timeout=3) as response:
                return json.load(response)
        return server, thread, request

    def test_disable_all_and_launch_over_http_keeps_menu_available(self):
        server, thread, request = self.service()
        with patch('launcher.subprocess.Popen') as spawn:
            request('launch', {'selection': [], 'revision': request('state')['revision'], 'native': True})
        spawn.assert_called_once()
        self.assertEqual(request('state')['enabled'], [])
        self.assertFalse(request('ping')['running'])
        self.assertFalse(server.stopping.is_set())
        self.assertTrue(thread.is_alive())

    def test_http_operations_share_success_and_failure_identity_with_responses(self):
        server, _, request = self.service()
        self.assertEqual(request('operations'), {'current': None, 'last': None})
        success = request('scheme', {'name': 'fixture', 'selection': ['plugin-a']})
        self.assertRegex(success['operationId'], r'^[a-f0-9]{32}$')
        latest = request('operations')['last']
        self.assertEqual(latest['id'], success['operationId'])
        self.assertEqual(latest['kind'], 'scheme')
        self.assertEqual(latest['status'], 'succeeded')
        self.assertIsNone(latest['error'])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request('scheme', {'name': '', 'selection': []})
        failure = json.load(caught.exception)
        self.assertEqual(set(failure), {'error', 'code', 'retryable', 'details', 'recovery', 'operationId'})
        self.assertRegex(failure['operationId'], r'^[a-f0-9]{32}$')
        latest = request('operations')['last']
        self.assertEqual(latest['status'], 'failed')
        self.assertEqual(latest['id'], failure['operationId'])
        self.assertEqual(latest['error'], failure)

    def test_operation_status_remains_readable_while_mutation_holds_manager_lock(self):
        server, _, request = self.service()
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def slow_apply(*args, **kwargs):
            with self.manager.lock:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fixture release timed out')
                return {'message': 'fixture complete'}

        def submit():
            try:
                outcomes.append(request('apply', {'selection': []}))
            except BaseException as error:
                outcomes.append(error)

        with patch.object(self.manager, 'apply', side_effect=slow_apply):
            worker = threading.Thread(target=submit)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                observed = request('operations')['current']
                self.assertEqual((observed['kind'], observed['status']), ('apply', 'running'))
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    request('scheme', {'name': 'blocked', 'selection': []})
                self.assertEqual(caught.exception.code, 409)
                failure = json.load(caught.exception)
                self.assertEqual(failure['code'], 'operation_busy')
                self.assertTrue(failure['retryable'])
                self.assertEqual(request('operations')['current']['id'], observed['id'])
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        if isinstance(outcomes[0], BaseException):
            raise outcomes[0]
        self.assertEqual(outcomes[0]['operationId'], observed['id'])

    def test_http_security_and_internal_errors_have_one_public_contract(self):
        _, _, request = self.service()
        for headers, expected in (({'Authorization': 'Bearer wrong'}, (403, 'forbidden')),
                                  ({'Content-Type': 'text/plain'}, (415, 'unsupported_media'))):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as caught:
                request('scheme', {}, **headers)
            payload = json.load(caught.exception)
            self.assertEqual((caught.exception.code, payload['code']), expected)
            self.assertEqual(set(payload), {'error', 'code', 'retryable', 'details', 'recovery', 'operationId'})
            self.assertIsNone(payload['operationId'])
        self.assertEqual(request('operations'), {'current': None, 'last': None})
        with (patch.object(self.manager, 'scheme', side_effect=RuntimeError('private_fixture_detail')),
                self.assertLogs(level='ERROR'), self.assertRaises(urllib.error.HTTPError) as caught):
            request('scheme', {'name': 'valid', 'selection': []})
        payload = json.load(caught.exception)
        self.assertEqual((caught.exception.code, payload['code']), (500, 'internal_error'))
        self.assertNotIn('private_fixture_detail', json.dumps(payload))
        self.assertEqual(request('operations')['last']['error'], payload)

    def test_http_frontend_modules_are_served_by_the_current_runtime(self):
        server, _, _ = self.service()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for module, namespace in (('state.js', 'LauncherState'), ('api.js', 'LauncherAPI'), ('runtime.js', 'LauncherRuntime')):
            with self.subTest(module=module), opener.open(f'http://127.0.0.1:{server.server_port}/' + module, timeout=3) as response:
                self.assertIn('text/javascript', response.headers['Content-Type'])
                self.assertIn(namespace, response.read().decode())

    def test_first_start_persists_settings_before_creating_tracking_objects(self):
        fresh = Manager(self.manager.data.parent / 'fresh-data', self.home, self.exe)
        self.assertEqual(json.loads(fresh.settings_file.read_text(encoding='utf-8'))['format'], 3)
        reference = fresh.tracking.object('deltas', {'changes': []})
        reopened = Manager(fresh.data)
        self.assertEqual(reopened.home, self.home)
        self.assertEqual(reopened.settings['executable'], str(self.exe))
        self.assertEqual(reopened.tracking.read_object(reference), {'changes': []})

    def test_unknown_post_is_a_failed_operation_with_a_correlated_error(self):
        _, _, request = self.service()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request('unknown-operation', {})
        payload = json.load(caught.exception)
        self.assertEqual((caught.exception.code, payload['code']), (404, 'not_found'))
        self.assertRegex(payload['operationId'], r'^[a-f0-9]{32}$')
        latest = request('operations')['last']
        self.assertEqual(latest['status'], 'failed')
        self.assertEqual(latest['error'], payload)

    def test_service_quit_requires_auth_and_same_origin_and_preserves_profile(self):
        server, thread, request = self.service()
        for headers in ({'Authorization': 'Bearer wrong'}, {'Origin': 'https://example.com'}):
            with self.subTest(headers=headers):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    request('quit', {}, **headers)
                self.assertEqual(caught.exception.code, 403)
                self.assertFalse(server.stopping.is_set())
        with patch('launcher.running_harness', return_value=[123]):
            request('quit', {})  # Exiting the menu must not kill the application.
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(server.stopping.is_set())
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_rejected_post_consumes_delayed_body_before_closing_connection(self):
        server, _, _ = self.service()
        entered, replied = threading.Event(), threading.Event()
        original_post, original_reply = Handler.do_POST, Handler.reply

        def observe_post(handler):
            entered.set()
            original_post(handler)

        def observe_reply(handler, *args, **kwargs):
            replied.set()
            original_reply(handler, *args, **kwargs)

        with patch.object(Handler, 'do_POST', new=observe_post), patch.object(Handler, 'reply', new=observe_reply), \
                socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as client:
            client.sendall((f'POST /api/quit HTTP/1.0\r\nHost: 127.0.0.1:{server.server_port}\r\n'
                            'Authorization: Bearer wrong\r\nContent-Type: application/json\r\n'
                            'Content-Length: 2\r\n\r\n').encode())
            self.assertTrue(entered.wait(1))
            # Reproduce the headers/body split that urllib can make on Windows.
            # The server must consume the small body before closing its socket.
            self.assertFalse(replied.wait(0.1))
            client.sendall(b'{}')
            response = http.client.HTTPResponse(client)
            response.begin()
            self.assertEqual(response.status, 403)
            payload = json.load(response)
            self.assertEqual(payload['error'], 'Forbidden')
            self.assertEqual(payload['code'], 'forbidden')
            self.assertIsNone(payload['operationId'])
        self.assertFalse(server.stopping.is_set())

    def test_slow_post_body_times_out_without_holding_mutation_lock(self):
        server, _, request = self.service()
        reading = threading.Event()
        original_post = Handler.do_POST

        class ObservedReader:
            def __init__(self, reader):
                self.reader = reader

            def __getattr__(self, name):
                return getattr(self.reader, name)

            def read(self, size):
                reading.set()
                return self.reader.read(size)

            def read1(self, size):
                reading.set()
                return self.reader.read1(size)

        def observe_post(handler):
            handler.rfile = ObservedReader(handler.rfile)
            original_post(handler)

        with patch.object(Handler, 'do_POST', new=observe_post), patch.object(Handler, 'body_timeout', 0.5), \
                socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as client:
            client.sendall((f'POST /api/scheme HTTP/1.0\r\nHost: 127.0.0.1:{server.server_port}\r\n'
                            'Authorization: Bearer test-token\r\nContent-Type: application/json\r\n'
                            'Content-Length: 2\r\n\r\n').encode())
            self.assertTrue(reading.wait(1))
            self.assertFalse(server.mutation_lock.locked())
            request('scheme', {'name': 'responsive', 'selection': []})
            response = http.client.HTTPResponse(client)
            response.begin()
            self.assertEqual(response.status, 408)
            self.assertIn('超时', json.load(response)['error'])
        self.assertIn('responsive', self.manager.settings['schemes'])

    def test_oversized_post_is_rejected_without_waiting_for_body(self):
        server, _, _ = self.service()
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as client:
            client.sendall((f'POST /api/scheme HTTP/1.0\r\nHost: 127.0.0.1:{server.server_port}\r\n'
                            'Authorization: Bearer test-token\r\nContent-Type: application/json\r\n'
                            'Content-Length: 65537\r\n\r\n').encode())
            response = http.client.HTTPResponse(client)
            response.begin()
            self.assertEqual(response.status, 413)
            response.read()
        self.assertFalse(server.mutation_lock.locked())

    def test_service_quit_refuses_active_recording(self):
        server, _, request = self.service()
        with patch.object(self.manager.tracking, 'active', return_value=True):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('quit', {})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn('安装记录仍在进行', json.load(caught.exception)['error'])
        self.assertFalse(server.stopping.is_set())
        self.assertFalse(request('ping')['running'])

    def test_service_quit_refuses_concurrent_mutation_or_supervisor_work(self):
        server, _, request = self.service()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hold_manager():
            with self.manager.lock:
                started.set()
                release.wait(5)
        worker = threading.Thread(target=hold_manager, daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('quit', {})
            self.assertEqual(caught.exception.code, 400)
        finally:
            release.set()
            worker.join(2)
        with server.mutation_lock:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('quit', {})
            self.assertEqual(caught.exception.code, 409)
        self.assertFalse(server.stopping.is_set())
        request('quit', {})

    def test_stopping_service_rejects_new_mutations(self):
        server, _, request = self.service()
        server.stopping.set()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request('scheme', {'name': 'late', 'selection': []})
        self.assertEqual(caught.exception.code, 503)
        self.assertNotIn('late', self.manager.settings['schemes'])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_non_ascii_document_token_is_rejected_without_dropping_connection(self):
        server, _, _ = self.service()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(f'http://127.0.0.1:{server.server_port}/?token=%E4%B8%AD')
        self.assertEqual(caught.exception.code, 403)

    def test_window_launch_failure_cleans_instance_and_listener(self):
        import launcher
        server = Server(('127.0.0.1', 0), Handler)
        with patch.object(launcher, 'Server', return_value=server), \
                patch.object(launcher, 'Manager', return_value=self.manager), \
                patch.object(launcher.logging, 'basicConfig'), \
                patch.object(launcher.sys, 'argv', ['launcher.py', '--data', str(self.manager.data)]), \
                patch.object(launcher, 'open_window', side_effect=OSError('cannot open browser')):
            with self.assertRaisesRegex(OSError, 'cannot open browser'):
                launcher.main()
        self.assertFalse((self.manager.data / 'instance.json').exists())
        self.assertEqual(server.socket.fileno(), -1)
        with launcher.instance_lock(self.manager.data) as owned:
            self.assertTrue(owned)

    def test_default_port_conflict_parallel_start_reuses_one_locked_service(self):
        import launcher
        from contextlib import contextmanager
        server = Server(('127.0.0.1', 0), Handler)
        preparing, release, waiting, ready = (threading.Event() for _ in range(4))
        self.addCleanup(release.set)
        self.addCleanup(server.server_close)
        failures, addresses = [], []
        real_lock, real_serve = launcher.instance_lock, server.serve_forever

        @contextmanager
        def observed_lock(data):
            with real_lock(data) as owned:
                if not owned:
                    waiting.set()
                yield owned

        def make_server(address, handler):
            addresses.append(address)
            if address[1] == 17643:
                raise OSError('default port belongs to another application')
            return server

        def make_manager(data):
            preparing.set()
            if not release.wait(5):
                raise RuntimeError('test did not release startup')
            return self.manager

        def run_server(**kwargs):
            ready.set()
            real_serve(**kwargs)

        def start():
            try:
                launcher.main()
            except Exception as exc:
                failures.append(exc)

        with patch.object(launcher, 'instance_lock', side_effect=observed_lock), \
                patch.object(launcher, 'Server', side_effect=make_server), \
                patch.object(launcher, 'Manager', side_effect=make_manager), \
                patch.object(launcher.logging, 'basicConfig'), \
                patch.object(launcher.sys, 'argv', ['launcher.py', '--no-open', '--data', str(self.manager.data)]), \
                patch.object(server, 'serve_forever', side_effect=run_server):
            first, second = (threading.Thread(target=start, daemon=True) for _ in range(2))
            first.start()
            try:
                self.assertTrue(preparing.wait(3))
                second.start()
                self.assertTrue(waiting.wait(2))
                release.set()
                self.assertTrue(ready.wait(2))
                second.join(3)
                self.assertFalse(second.is_alive())
                self.assertTrue(first.is_alive())
                self.assertEqual(addresses, [('127.0.0.1', 17643), ('127.0.0.1', 0)])
                self.assertEqual(failures, [])
            finally:
                release.set()
                if ready.wait(2):
                    server.shutdown()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        with launcher.instance_lock(self.manager.data) as owned:
            self.assertTrue(owned)
        self.assertFalse((self.manager.data / 'instance.json').exists())

    def test_main_survives_missing_heartbeats_and_cleans_instance_on_explicit_quit(self):
        import launcher
        from types import SimpleNamespace
        from unittest.mock import Mock
        server = Server(('127.0.0.1', 0), Handler)
        # Simulate a sleeping browser across the former five-minute threshold.
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 3600, 3600]),
                                perf_counter=time.perf_counter, sleep=lambda _: None)
        ready = threading.Event()
        serve = server.serve_forever

        def run_server(**kwargs):
            ready.set()
            serve(**kwargs)
        with patch.object(launcher, 'Server', return_value=server), patch.object(launcher, 'Manager', return_value=self.manager), \
                patch.object(launcher, 'time', clock), patch.object(launcher.logging, 'basicConfig'), \
                patch.object(launcher.sys, 'argv', ['launcher.py', '--no-open', '--data', str(self.manager.data)]), \
                patch.object(server, 'serve_forever', side_effect=run_server):
            thread = threading.Thread(target=launcher.main, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(2))
                # A service must survive even with no browser requests at all.
                thread.join(0.1)
                self.assertTrue(thread.is_alive())
                self.assertTrue((self.manager.data / 'instance.json').exists())
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/quit', data=b'{}',
                                             headers={'Authorization': 'Bearer ' + server.token,
                                                      'Content-Type': 'application/json'})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=3) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse((self.manager.data / 'instance.json').exists())

    def test_scheme_import_is_atomic_on_invalid_plugin_or_capacity(self):
        self.manager.scheme('original', ['plugin-a'])
        with self.assertRaises(MenuError):
            self.manager.import_schemes({'valid': [], 'invalid': ['unknown']})
        self.assertEqual(self.manager.settings['schemes'], {'original': ['plugin-a']})
        self.manager.scheme('empty fixture', [])
        empty_lock = self.manager.settings['schemeLocks']['empty fixture']
        self.manager.settings['schemes'] = {str(n): [] for n in range(100)}
        self.manager.settings['schemeLocks'] = {str(n): empty_lock for n in range(100)}
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
        with self.assertRaises(urllib.error.HTTPError) as caught:
            read('catalog/leaderboard', False)
        self.assertEqual(caught.exception.code, 403)
        with patch.object(server.catalog, 'leaderboard', return_value={'items': []}) as board:
            read('catalog/leaderboard?board=rising')
        board.assert_called_once_with('rising')
        self.path.unlink()
        self.assertEqual(read('settings')['home'], str(self.home))
        result = read('catalog/search?source=official')
        self.assertEqual(len(result['items']), 4)
        with patch.object(server.catalog, 'detail', side_effect=CatalogError('network unavailable')):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                read('catalog/detail?name=dsh-test')
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(json.load(caught.exception)['error'], 'network unavailable')


class FakeRegistry:
    """Minimal winreg stand-in: one HKCU uninstall key holding the given entries."""
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = 'HKCU', 'HKLM'

    class Key(dict):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def __init__(self, entries):
        self.entries = entries

    def OpenKey(self, parent, name):
        if parent == 'HKCU' and name == UNINSTALL_KEYS[0]:
            return self.Key(self.entries)
        if isinstance(parent, dict) and name in parent:
            return self.Key(parent[name])
        raise OSError(name)

    def EnumKey(self, key, index):
        try:
            return list(key)[index]
        except IndexError:
            raise OSError(index) from None

    def QueryValueEx(self, key, name):
        if name not in key:
            raise OSError(name)
        return key[name], 1


class ExecutableDetectionTests(unittest.TestCase):
    def test_uninstall_entry_finds_custom_install_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'DeepSeek Harness.exe'
            exe.write_bytes(b'fixture')
            entries = {'other': {'DisplayName': 'Something Else', 'DisplayIcon': str(exe)},
                       'harness': {'DisplayName': 'DeepSeek Harness 0.2.0-rc.2', 'DisplayIcon': f'{exe},0'}}
            self.assertEqual(registry_executable(FakeRegistry(entries)), exe)
            uninstaller = Path(folder) / 'Uninstall DeepSeek Harness.exe'
            entries['harness'] = {'DisplayName': 'DeepSeek Harness 0.2.0', 'UninstallString': f'"{uninstaller}" /currentuser'}
            self.assertEqual(registry_executable(FakeRegistry(entries)), exe)
            entries['harness'] = {'DisplayName': 'DeepSeek Harness', 'DisplayIcon': str(Path(folder) / 'gone' / 'DeepSeek Harness.exe')}
            self.assertIsNone(registry_executable(FakeRegistry(entries)))

    def test_default_executable_prefers_registered_install(self):
        registered = Path('X:/custom/DeepSeek Harness.exe')
        with patch.dict(os.environ), patch('launcher.registry_executable', return_value=registered):
            os.environ.pop('DSH_EXECUTABLE', None)
            self.assertEqual(default_executable(), str(registered))
            os.environ['DSH_EXECUTABLE'] = 'Y:/explicit/DeepSeek Harness.exe'
            self.assertEqual(default_executable(), str(Path('Y:/explicit/DeepSeek Harness.exe')))


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows bootstrap')
class BootstrapTests(unittest.TestCase):
    def run_bootstrap(self, root, registry, runtimes):
        fixture = root / 'bootstrap-fixture.json'
        fixture.write_text(json.dumps({'registry': registry, 'runtimes': runtimes}), encoding='utf-8')
        script = root / 'check-bootstrap.ps1'
        bootstrap = str(Path(__file__).resolve().with_name('bootstrap.ps1')).replace("'", "''")
        script.write_text("""
$fixture = Get-Content -LiteralPath $env:BOOTSTRAP_FIXTURE -Raw | ConvertFrom-Json
function Get-ItemProperty { return $fixture.registry }
function Get-Command { return @($fixture.runtimes | Where-Object { $_.onPath -ne $false } | ForEach-Object { [pscustomobject]@{ Source = $_.path } }) }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru, [switch]$Wait)
    if ($PassThru) {
        $runtime = $fixture.runtimes | Where-Object { $_.path -eq $FilePath } | Select-Object -First 1
        return [pscustomobject]@{ ExitCode = $(if ($runtime) { $runtime.exitCode } else { 1 }) }
    }
    $script:launched = $FilePath
}
function Add-Type { throw ('Bootstrap failed: ' + $Error[0]) }
. '""" + bootstrap + "'\n$script:launched | ConvertTo-Json -Compress\n", encoding='utf-8')
        env = dict(os.environ, USERPROFILE=str(root), LOCALAPPDATA=str(root),
                   ProgramFiles=str(root), DSH_EXECUTABLE='', BOOTSTRAP_FIXTURE=str(fixture))
        env['ProgramFiles(x86)'] = str(root)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(script)],
                                capture_output=True, text=True, env=env, timeout=20,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_first_run_uses_registered_harness_runtime_without_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            exe = root / 'custom install' / 'DeepSeek Harness.exe'
            runtime = exe.parent / 'resources/runtime/primary-runtime/dependencies/python/pythonw.exe'
            runtime.parent.mkdir(parents=True)
            exe.touch()
            runtime.touch()
            selected = self.run_bootstrap(root, [{'DisplayName': 'DeepSeek Harness', 'InstallLocation': str(exe.parent)}],
                                          [{'path': str(runtime), 'exitCode': 0, 'onPath': False}])
            self.assertEqual(Path(selected), runtime)

    def test_path_runtime_selection_skips_python_older_than_312(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root / 'Python311/pythonw.exe', root / 'Python312/pythonw.exe'
            for runtime in (old, new):
                runtime.parent.mkdir()
                runtime.touch()
            selected = self.run_bootstrap(root, [], [{'path': str(old), 'exitCode': 1}, {'path': str(new), 'exitCode': 0}])
            self.assertEqual(Path(selected), new)


if __name__ == '__main__':
    unittest.main()
