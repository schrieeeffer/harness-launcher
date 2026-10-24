"""Application-boundary regressions using isolated installations and HTTP."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

import launcher
import test_launcher as fixtures


class ManagerRegressionTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    service = fixtures.ManagerTests.service

    def test_scheme_accepts_ui_order_without_weakening_version_lock(self):
        self.manager.import_schemes({'reverse': ['@demo/plugin-b', 'plugin-a']})
        self.manager.apply(['plugin-a', '@demo/plugin-b'], self.manager.state()['revision'], scheme='reverse')
        self.assertEqual(self.manager.state()['enabled'], ['plugin-a', '@demo/plugin-b'])
        self.manager.settings['schemeLocks']['reverse']['hostHash'] = '0' * 64
        before = self.path.read_bytes()
        with self.assertRaisesRegex(launcher.MenuError, '版本已变化'):
            self.manager.apply(['plugin-a', '@demo/plugin-b'], self.manager.state()['revision'], scheme='reverse')
        self.assertEqual(self.path.read_bytes(), before)

    def test_relative_environment_paths_are_resolved_before_launch_and_persistence(self):
        previous = Path.cwd()
        try:
            os.chdir(self.exe.parent)
            with patch.dict(os.environ, DSH_HOME='home', DSH_EXECUTABLE=self.exe.name):
                manager = launcher.Manager(self.exe.parent / 'relative-data')
            self.assertEqual(manager.settings['executable'], str(self.exe))
            self.assertEqual(manager.settings['home'], str(self.home))
            saved = json.loads(manager.settings_file.read_bytes())
            self.assertEqual(saved['executable'], str(self.exe))
            with patch('launcher.subprocess.Popen') as spawn, patch.object(manager, 'switch_layers', return_value=None):
                manager.apply(['plugin-a'], manager.state()['revision'], launch=True)
            self.assertEqual(spawn.call_args.args[0], [str(self.exe)])
        finally:
            os.chdir(previous)

    def test_non_boolean_delete_cannot_remove_a_saved_scheme(self):
        self.manager.scheme('keep', ['plugin-a'])
        before = self.manager.settings_file.read_bytes()
        _, _, request = self.service()
        for value in ('false', 'true', 1, [], {}):
            with self.subTest(value=value), self.assertRaises(urllib.error.HTTPError) as caught:
                request('scheme', {'name': 'keep', 'selection': [], 'delete': value})
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(json.load(caught.exception)['code'], 'invalid_request')
            self.assertEqual(self.manager.settings_file.read_bytes(), before)

    def test_invalid_operation_path_returns_json_and_releases_mutation_lock(self):
        server, _, request = self.service()
        for path in ('', 'x' * 81):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                request(path, {})
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(json.load(caught.exception)['code'], 'invalid_operation_kind')
            self.assertFalse(server.mutation_lock.locked())
            self.assertIsNone(request('operations')['current'])
        request('scheme', {'name': 'still usable', 'selection': []})

    def test_mutation_is_finished_before_any_response_bytes_are_sent(self):
        server, _, request = self.service()
        observations = []
        send_response = launcher.Handler.send_response

        def observe(handler, *args, **kwargs):
            observations.append((server.mutation_lock.locked(), server.operations.snapshot()['current']))
            return send_response(handler, *args, **kwargs)

        with patch.object(launcher.Handler, 'send_response', new=observe):
            request('scheme', {'name': 'saved', 'selection': []})
            for path, body in (('', {}), ('scheme', {'name': '', 'selection': []}), ('unknown', {})):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                    request(path, body)
                json.load(caught.exception)
            server.stopping.set()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('scheme', {'name': 'stopped', 'selection': []})
            json.load(caught.exception)
        self.assertEqual(observations, [(False, None)] * 5)

    def test_slow_response_delivery_does_not_hold_mutation_lock(self):
        server, _, request = self.service()
        delivering, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        send_reply = launcher.Handler.send_reply
        failures = []

        def hold_first(handler, code, body, mime):
            if not delivering.is_set():
                delivering.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release response delivery')
            return send_reply(handler, code, body, mime)

        def first_request():
            try:
                request('scheme', {'name': 'first', 'selection': []})
            except Exception as exc:
                failures.append(exc)

        with patch.object(launcher.Handler, 'send_reply', new=hold_first):
            worker = threading.Thread(target=first_request)
            worker.start()
            try:
                self.assertTrue(delivering.wait(2))
                self.assertFalse(server.mutation_lock.locked())
                self.assertIsNone(server.operations.snapshot()['current'])
                request('scheme', {'name': 'second', 'selection': []})
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertIn('second', self.manager.settings['schemes'])

    def test_bookkeeping_failure_still_releases_mutation_lock(self):
        server, _, _ = self.service()
        handler = object.__new__(launcher.Handler)
        handler.server = server
        handler.path = '/api/scheme'
        handler.headers = {'Content-Length': '2', 'Content-Type': 'application/json'}
        handler.authorized = Mock(return_value=True)
        handler.read_request_body = Mock(return_value=b'{}')
        handler.post = Mock(side_effect=lambda _: handler.reply(200, {'message': 'committed'}))
        handler.send_reply = Mock()
        with patch.object(server.operations, 'finish', side_effect=RuntimeError('bookkeeping failed')):
            with self.assertRaisesRegex(RuntimeError, 'bookkeeping failed'):
                handler.do_POST()
        self.assertFalse(server.mutation_lock.locked())
        self.assertFalse(handler.defer_response)
        handler.send_reply.assert_not_called()

    def test_malformed_backup_rows_do_not_break_the_backup_browser(self):
        backup = self.manager.backup(self.raw, self.original['dsh']['profile']['bundles'], 'fixture')
        original = json.loads((self.manager.data / 'backups' / backup).read_bytes())
        malformed = [{'plugins': None}, {'plugins': 'plugin-a'}, {'plugins': [None]},
                     {'time': []}, {'reason': {}}, {'format': 3.0}]
        for index, fields in enumerate(malformed):
            name = f'20990101-000000-{index:06}.json'
            (self.manager.data / 'backups' / name).write_text(json.dumps({**original, **fields}), encoding='utf-8')
        (self.manager.data / 'backups' / 'unknown.json').write_text(json.dumps(original), encoding='utf-8')
        self.assertEqual([row['id'] for row in self.manager.state()['backups']], [backup])
        (self.manager.data / 'backups' / backup).write_text('[1]', encoding='utf-8')
        with self.assertRaises(launcher.MenuError):
            self.manager.restore(backup, self.manager.state()['revision'])
        self.assertEqual(self.path.read_bytes(), self.raw)


class RuntimeRegressionTests(unittest.TestCase):
    def test_unsupported_python_fails_before_creating_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'not-created'
            with patch.object(launcher.sys, 'version_info', (3, 11, 4)), \
                    patch.object(launcher.sys, 'argv', ['launcher.py', '--no-open', '--data', str(target)]), \
                    patch.object(launcher.logging, 'basicConfig'), \
                    patch.object(launcher, 'serve') as serve:
                with self.assertRaisesRegex(launcher.MenuError, 'Python 3.12'):
                    launcher.main()
            self.assertFalse(target.exists())
            serve.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows process enumeration')
    def test_incomplete_process_scan_never_means_harness_is_stopped(self):
        for fail_at_first in (True, False):
            with self.subTest(fail_at_first=fail_at_first):
                kernel = Mock()
                kernel.CreateToolhelp32Snapshot.return_value = 123
                kernel.Process32FirstW.return_value = not fail_at_first
                kernel.Process32NextW.return_value = False
                with patch.object(launcher.ctypes, 'WinDLL', return_value=kernel), \
                        patch.object(launcher.ctypes, 'get_last_error', return_value=5):
                    with self.assertRaisesRegex(launcher.MenuError, '无法读取运行状态'):
                        launcher.running_harness()
                kernel.CloseHandle.assert_called_once_with(123)
        with patch.object(launcher.ctypes, 'WinDLL', return_value=kernel), \
                patch.object(launcher.ctypes, 'get_last_error', return_value=18):
            self.assertEqual(launcher.running_harness(), [])


if __name__ == '__main__':
    unittest.main()
