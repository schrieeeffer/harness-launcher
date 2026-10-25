import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from errors import LauncherError
import runtime_repair as repair


class RuntimeRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.exe = self.root / 'install/DeepSeek Harness.exe'
        self.exe.parent.mkdir()
        self.exe.write_bytes(b'official exe')
        self.resources = self.exe.parent / 'resources'
        self.resources.mkdir()
        (self.resources / 'app.asar').write_bytes(b'official asar')
        self.payload = self.root / 'official'
        (self.payload / 'resources/runtime').mkdir(parents=True)
        shutil.copyfile(self.exe, self.payload / self.exe.name)
        shutil.copyfile(self.resources / 'app.asar', self.payload / 'resources/app.asar')
        self.entries = {}
        for name in (*repair.ANCHORS, 'extra/offical.dat'):
            path = self.payload / 'resources/runtime' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = ('official ' + name).encode()
            path.write_bytes(data)
            self.entries[name] = len(data)
        self.installer = self.root / 'official.exe'
        self.installer.write_bytes(b'signed installer fixture')
        self.sha512 = base64.b64encode(hashlib.sha512(self.installer.read_bytes()).digest()).decode()
        self.manager = SimpleNamespace(settings={'executable': str(self.exe)}, data=self.root / 'data',
            lock=threading.RLock(), require_stopped=Mock(),
            tracking=SimpleNamespace(busy=False, summary=lambda: {'session': None}),
            maintenance=SimpleNamespace(load=lambda: None))
        self.service = repair.RuntimeRepair(self.manager)
        self.addCleanup(self.service.discard)
        self.sig = patch('runtime_repair.signature', return_value={'version': '0.2.1-rc.3', 'publisher': repair.PUBLISHER, 'status': 'Valid'})
        self.verify = self.sig.start()
        self.addCleanup(self.sig.stop)
        self.zip = patch('runtime_repair.SevenZip')
        self.extractor = self.zip.start().return_value
        self.addCleanup(self.zip.stop)

        def extract(installer, stage):
            shutil.copytree(self.payload, stage / 'payload')
            return stage / 'payload', self.entries
        self.extractor.extract.side_effect = extract

    def prepare(self):
        return self.service.prepare(str(self.installer), self.sha512)

    def test_fast_check_reports_missing_without_hashing_or_extraction(self):
        with patch('runtime_repair.hash_file', side_effect=AssertionError('must not hash')):
            self.assertEqual(self.service.status()['status'], 'missing')
            with self.assertRaises(LauncherError):
                self.service.require_ready()
        self.verify.assert_not_called()
        self.extractor.extract.assert_not_called()

    def test_repair_preserves_user_files_records_and_existing_runtime(self):
        target = self.service.root / repair.ANCHORS[0]
        target.parent.mkdir()
        target.write_bytes((self.payload / 'resources/runtime' / repair.ANCHORS[0]).read_bytes())
        user = self.service.root / 'my-notes.txt'
        user.write_bytes(b'user-owned')
        sentinel = self.manager.data / 'rollback/user-data'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b'keep')
        before = target.stat().st_mtime_ns
        preview = self.prepare()
        self.assertEqual(len(preview['missing']), len(self.entries) - 1)
        result = self.service.execute(preview['token'])
        self.assertEqual(result['restored'], len(self.entries) - 1)
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(user.read_bytes(), b'user-owned')
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertEqual(self.service.status()['status'], 'ready')
        receipt = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(receipt['status'], 'verified')
        self.assertIsNone(self.service.plan)
        self.assertFalse(any((self.service.folder / 'staging').iterdir()))

    def test_wrong_checksum_rejected_before_extraction(self):
        with self.assertRaisesRegex(LauncherError, 'SHA-512 不匹配'):
            self.service.prepare(str(self.installer), '00' * 64)
        self.extractor.extract.assert_not_called()
        self.assertFalse(self.service.root.exists())

    def test_signature_error_never_extracts_or_writes_host(self):
        self.verify.side_effect = LauncherError('invalid signature')
        with self.assertRaisesRegex(LauncherError, 'invalid signature'):
            self.prepare()
        self.extractor.extract.assert_not_called()
        self.assertFalse(self.service.root.exists())

    def test_same_version_wrong_host_bytes_are_rejected(self):
        for target in (self.exe, self.resources / 'app.asar'):
            original = target.read_bytes()
            target.write_bytes(original + b'changed')
            with self.assertRaisesRegex(LauncherError, '不一致'):
                self.prepare()
            self.assertFalse(self.service.root.exists())
            target.write_bytes(original)

    def test_existing_plugin_change_or_zero_byte_file_blocks_all_repair(self):
        target = self.service.root / repair.ANCHORS[0]
        target.parent.mkdir()
        for content in (b'plugin change', b''):
            target.write_bytes(content)
            preview = self.prepare()
            self.assertEqual(preview['conflicts'], [repair.ANCHORS[0]])
            with self.assertRaises(LauncherError):
                self.service.execute(preview['token'])
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(len(list(self.service.root.rglob('*'))), 1)

    def test_corrupt_extraction_fails_before_host_writes(self):
        self.entries[repair.ANCHORS[0]] += 1
        with self.assertRaisesRegex(LauncherError, '解压文件'):
            self.prepare()
        self.assertFalse(self.service.root.exists())

    def test_staging_tamper_detected_during_execute(self):
        preview = self.prepare()
        (self.service.plan['runtime'] / repair.ANCHORS[0]).write_bytes(b'edited after preview')
        with self.assertRaisesRegex(LauncherError, '临时修复文件校验失败'):
            self.service.execute(preview['token'])
        self.assertFalse(self.service.root.exists())

    def test_host_replacement_after_preview_rejected(self):
        preview = self.prepare()
        self.exe.write_bytes(b'new version')
        with self.assertRaisesRegex(LauncherError, '宿主已变化'):
            self.service.execute(preview['token'])
        self.assertFalse(self.service.root.exists())

    def test_preview_change_requires_new_check(self):
        preview = self.prepare()
        target = self.service.root / repair.ANCHORS[0]
        target.parent.mkdir()
        target.write_bytes((self.service.plan['runtime'] / repair.ANCHORS[0]).read_bytes())
        with self.assertRaisesRegex(LauncherError, '预览后运行时已变化'):
            self.service.execute(preview['token'])

    def test_invalid_expired_token_and_running_harness_are_rejected(self):
        preview = self.prepare()
        with self.assertRaises(LauncherError):
            self.service.execute('wrong')
        self.service.plan['created'] -= 1801
        with self.assertRaises(LauncherError):
            self.service.execute(preview['token'])
        self.manager.require_stopped.side_effect = LauncherError('still running')
        with self.assertRaisesRegex(LauncherError, 'still running'):
            self.prepare()

    def test_interruption_retains_only_verified_additions_and_can_resume(self):
        preview = self.prepare()
        real_link = os.link
        calls = []
        def interrupt(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError('disk write failed')
            return real_link(source, target)
        with patch('runtime_repair.os.link', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.service.execute(preview['token'])
        first = calls[0]
        original = first.read_bytes()
        receipt = json.loads((self.service.folder / 'receipts' / (preview['token'] + '.json')).read_text())
        self.assertEqual(receipt['status'], 'incomplete')
        self.assertFalse(list(self.service.root.rglob('.harness-repair-*')))
        preview = self.prepare()
        self.assertEqual(len(preview['missing']), len(self.entries) - 1)
        self.service.execute(preview['token'])
        self.assertEqual(first.read_bytes(), original)

    def test_concurrent_created_user_file_never_overwritten(self):
        preview = self.prepare()
        real_link = os.link
        def race(source, target):
            target.write_bytes(b'user created concurrently')
            return real_link(source, target)
        with patch('runtime_repair.os.link', side_effect=race):
            with self.assertRaises(FileExistsError):
                self.service.execute(preview['token'])
        self.assertEqual((self.service.root / repair.ANCHORS[0]).read_bytes(), b'user created concurrently')

    def test_cleanup_failure_does_not_relabel_verified_repair_as_failed(self):
        preview = self.prepare()
        with patch('runtime_repair.shutil.rmtree', side_effect=PermissionError('busy temporary file')):
            result = self.service.execute(preview['token'])
        self.assertTrue(result['cleanupPending'])
        self.assertEqual(json.loads(Path(result['receipt']).read_text())['status'], 'verified')
        self.assertEqual(self.service.status()['status'], 'ready')

    def test_official_cache_filename_cannot_escape(self):
        pending = self.root / '@deepseek-aidsh-desktop-updater/pending'
        pending.mkdir(parents=True)
        (pending / 'update-info.json').write_text(json.dumps({'fileName': '../../official.exe', 'sha512': self.sha512}))
        with patch.dict(os.environ, LOCALAPPDATA=str(self.root)):
            with self.assertRaises(LauncherError):
                self.service.candidate()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction')
    def test_junction_target_blocks_repair_without_external_writes(self):
        import _winapi
        outside = self.root / 'outside'
        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(self.service.root))
        self.addCleanup(self.service.root.rmdir)
        with self.assertRaises(LauncherError):
            self.prepare()
        self.assertFalse(list(outside.iterdir()))


class ArchiveSafetyTests(unittest.TestCase):
    def listing(self, name, extra=''):
        return '\n----------\nPath = ' + name + '\nSize = 3\nAttributes = A\n' + extra

    def test_windows_aliases_links_traversals_and_duplicates_rejected(self):
        for name in ('../x', '/absolute', 'C:\\x', 'a:stream', 'folder/../x', 'NUL.txt', 'a.', 'a ', 'a//b', 'COM1/x'):
            with self.subTest(name=name), self.assertRaises(LauncherError):
                repair.archive_entries(self.listing(name))
        for extra in ('Symbolic Link = elsewhere', 'Hard Link = elsewhere'):
            with self.assertRaises(LauncherError):
                repair.archive_entries(self.listing('safe', extra))
        with self.assertRaises(LauncherError):
            repair.archive_entries(self.listing('A') + '\n\nPath = a\nSize = 1')
        with self.assertRaises(LauncherError):
            repair.archive_entries(self.listing('a') + '\n\nPath = a/b\nSize = 1')

    def test_size_limits(self):
        with patch('runtime_repair.MAX_BYTES', 2), self.assertRaises(LauncherError):
            repair.archive_entries(self.listing('safe'))

    @unittest.skipUnless(os.name == 'nt', 'Windows signature boundary')
    def test_signature_status_and_publisher_must_both_match(self):
        for status, publisher in [('Valid', 'Someone else'), ('NotSigned', repair.PUBLISHER)]:
            result = SimpleNamespace(stdout=json.dumps({'status': status, 'publisher': publisher, 'version': '0.2.1-rc.3'}).encode())
            with patch('runtime_repair.subprocess.run', return_value=result), self.assertRaises(LauncherError):
                repair.signature(Path('ignored.exe'))

    @unittest.skipUnless(os.name == 'nt', 'Windows signature boundary')
    def test_signature_uses_only_system_powershell_modules(self):
        value = {'status': 'Valid', 'publisher': repair.PUBLISHER, 'version': '0.2.1-rc.3'}
        with patch.dict(os.environ, PSModulePath='unrelated-pwsh-modules'), patch('runtime_repair.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(value).encode())) as run:
            self.assertEqual(repair.signature(Path('signed.exe')), value)
        self.assertEqual(run.call_args.kwargs['env']['PSModulePath'], str(Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/Modules'))


class LaunchGuardTests(unittest.TestCase):
    def setUp(self):
        from test_launcher import ManagerTests
        ManagerTests.setUp(self)

    def test_missing_runtime_blocks_launch_before_plugin_replay(self):
        (self.exe.parent / 'resources/runtime' / repair.ANCHORS[0]).unlink()
        before = self.path.read_bytes()
        with patch.object(self.manager.tracking, 'guard') as guard:
            with self.assertRaises(LauncherError) as caught:
                self.manager.apply(['plugin-a'], self.manager.state()['revision'], launch=True)
        self.assertEqual(caught.exception.code, 'runtime_missing')
        guard.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list((self.manager.data / 'backups').glob('*')))

    def test_missing_runtime_does_not_prevent_verified_apply_only(self):
        (self.exe.parent / 'resources/runtime' / repair.ANCHORS[0]).unlink()
        self.manager.apply([], self.manager.state()['revision'], launch=False, native=True)
        self.assertEqual(self.manager.state()['enabled'], [])

    def test_authenticated_repair_api_remains_available_when_main_state_fails(self):
        import http.client
        from launcher import Server, Handler
        server = Server(('127.0.0.1', 0), Handler)
        server.token, server.manager = 'repair-test', self.manager
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(path, body=None, authenticated=True):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                headers = {'Content-Type': 'application/json'}
                if authenticated:
                    headers['Authorization'] = 'Bearer repair-test'
                client.request('GET' if body is None else 'POST', '/api/' + path,
                               None if body is None else json.dumps(body), headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        try:
            service = self.manager.runtime_repair
            with patch.object(self.manager, 'state', side_effect=LauncherError('host update')), patch.object(service, 'prepare', return_value={'token': 'preview'}) as prepare:
                self.assertEqual(request('state')[0], 400)
                self.assertEqual(request('runtime/status')[1]['status'], 'ready')
                self.assertEqual(request('runtime/prepare', {}, authenticated=False)[0], 403)
                self.assertEqual(request('runtime/prepare', {'installer':'official.exe','sha512':'expected'})[0], 200)
                prepare.assert_called_once_with('official.exe','expected')
                self.assertEqual(request('runtime/unknown', {})[0], 400)
                self.assertEqual(request('runtime/discard', {})[0], 200)
                self.assertFalse(server.mutation_lock.locked())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)


if __name__ == '__main__':
    unittest.main()
