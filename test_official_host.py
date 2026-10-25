import base64
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from errors import LauncherError
from launcher import Manager, MenuError
import runtime_repair
from storage import hash_file
import test_patch_modes as adapter


class OfficialHostTests(unittest.TestCase):
    def setUp(self):
        adapter.PatchModeTests.setUp(self)
        self.manager.apply([], self.manager.state()['revision'], native=True)
        self.before_controls = self.manager.official_host.controls()
        self.exe.write_bytes(b'new signed official exe')
        self.archive.write_bytes(b'new signed official archive')
        self.cli.write_bytes(b'updated official CLI')
        self.dll = self.exe.parent / 'electron.dll'
        self.dll.write_bytes(b'official dependency')
        self.payload = self.root / 'payload'
        self.sizes = {}
        for path in (self.exe, self.archive, self.dll, *(self.resources / 'runtime' / name for name in runtime_repair.ANCHORS)):
            name = path.relative_to(self.exe.parent).as_posix()
            dest = self.payload / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            self.sizes[name] = dest.stat().st_size
        self.installer = self.root / 'official.exe'
        self.installer.write_bytes(b'signed official package')
        self.checksum = base64.b64encode(hashlib.sha512(self.installer.read_bytes()).digest()).decode()
        self.sig = patch('runtime_repair.signature', return_value={'version': '0.2.1-rc.3', 'publisher': runtime_repair.PUBLISHER})
        self.signature = self.sig.start()
        self.addCleanup(self.sig.stop)
        self.zip = patch('runtime_repair.SevenZip')
        self.extractor = self.zip.start().return_value
        self.addCleanup(self.zip.stop)
        def extract(installer, stage):
            shutil.copytree(self.payload, stage / 'payload')
            return stage / 'payload', self.sizes
        self.extractor.extract_host.side_effect = extract
        self.service = self.manager.official_host

    def prepare(self):
        return self.service.prepare(str(self.installer), self.checksum)

    def approve(self):
        plan = self.prepare()
        self.service.accept(plan['token'])

    def test_update_is_visible_before_click_and_never_auto_approved(self):
        state = self.manager.state()
        self.assertEqual(state['hostHealth']['status'], 'changed')
        self.assertFalse(state['patchMode']['verifiedNative'])
        with patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError):
            self.manager.apply([], state['revision'], launch=True)
        spawn.assert_not_called()
        self.signature.assert_not_called()
        self.assertFalse(self.service.pointer.exists())

    def test_authentic_update_native_launch_preserves_all_old_records_and_host_bytes(self):
        before = {p: hash_file(p) for p in self.manager.tracking.root.rglob('*') if p.is_file()}
        host = {name: hash_file(self.exe.parent / name) for name in self.sizes}
        self.approve()
        with patch('launcher.subprocess.Popen') as spawn, patch.object(self.manager, 'switch_layers') as replay:
            result = self.manager.apply([], self.manager.state()['revision'], launch=True)
        self.assertTrue(result['officialNative'])
        spawn.assert_called_once()
        replay.assert_not_called()
        self.assertEqual(before, {p: hash_file(p) for p in before})
        self.assertEqual(host, {name: hash_file(self.exe.parent / name) for name in self.sizes})
        reopened = Manager(self.manager.data, self.home, self.exe)
        self.assertEqual(reopened.state()['hostHealth']['status'], 'native_ready')
        self.assertEqual(self.service.controls(), self.before_controls)

    def test_approval_cannot_enable_old_purge_or_apply_or_begin_recording(self):
        self.approve()
        for selection, launch in [(['dsh-purge'], True), ([], False)]:
            with self.subTest(selection=selection, launch=launch), patch('launcher.subprocess.Popen') as spawn, self.assertRaises(MenuError):
                self.manager.apply(selection, self.manager.state()['revision'], launch=launch)
            spawn.assert_not_called()
        with self.assertRaises(LauncherError):
            self.manager.tracking.begin('dsh-purge')

    def test_cached_package_hash_and_signature_fail_closed(self):
        self.installer.write_bytes(b'changed')
        with self.assertRaisesRegex(LauncherError, 'SHA-512'):
            self.prepare()
        self.signature.assert_not_called()
        self.installer.write_bytes(b'signed official package')
        self.signature.side_effect = LauncherError('bad signature')
        with self.assertRaisesRegex(LauncherError, 'bad signature'):
            self.prepare()
        self.extractor.extract_host.assert_not_called()
        self.assertFalse(self.service.pointer.exists())

    def test_installed_host_and_dependency_conflict_are_not_adopted(self):
        for path in (self.exe, self.archive, self.dll, self.cli):
            original = path.read_bytes()
            path.write_bytes(original + b'foreign')
            with self.subTest(path=path), self.assertRaises(LauncherError):
                self.prepare()
            self.assertEqual(path.read_bytes(), original + b'foreign')
            path.write_bytes(original)
        self.assertFalse(self.service.pointer.exists())

    def test_native_user_changes_are_retained_and_block_approval(self):
        user = self.home / 'AGENTS.md'
        user.write_bytes(b'new user modification')
        with self.assertRaisesRegex(LauncherError, '用户侧'):
            self.prepare()
        self.assertEqual(user.read_bytes(), b'new user modification')
        self.signature.assert_not_called()

    def test_active_app_and_added_cli_files_are_retained_and_blocked(self):
        self.app.mkdir()
        with self.assertRaisesRegex(LauncherError, '解包目录'):
            self.prepare()
        self.app.rmdir()
        extra = self.cli.parent / 'unknown.js'
        extra.write_bytes(b'unknown code')
        with self.assertRaisesRegex(LauncherError, '额外文件'):
            self.prepare()
        self.assertTrue(extra.is_file())

    def test_preview_expiry_or_host_change_cannot_commit(self):
        plan = self.prepare()
        with self.assertRaises(LauncherError):
            self.service.accept('wrong')
        self.service.plan['created'] -= 1801
        with self.assertRaises(LauncherError):
            self.service.accept(plan['token'])
        plan = self.prepare()
        self.exe.write_bytes(b'next official update')
        with self.assertRaises(LauncherError):
            self.service.accept(plan['token'])
        self.assertFalse(self.service.pointer.exists())

    def test_post_approval_edits_or_second_update_are_rejected_before_spawn(self):
        self.approve()
        for path in (self.dll, self.cli, self.archive, self.exe, self.home / 'AGENTS.md'):
            original, info = path.read_bytes(), path.stat()
            path.write_bytes(original.replace(original[:1], b'X', 1))
            import os
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            with self.subTest(path=path), patch('launcher.subprocess.Popen') as spawn, self.assertRaises(LauncherError):
                self.manager.apply([], self.manager.state()['revision'], launch=True)
            spawn.assert_not_called()
            path.write_bytes(original)

    def test_changed_manifest_or_registry_invalidates_approval(self):
        self.approve()
        self.manifest.write_bytes(self.manifest.read_bytes() + b' ')
        self.assertEqual(self.service.status()['status'], 'changed')
        with patch('launcher.subprocess.Popen') as spawn, self.assertRaises(LauncherError):
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        spawn.assert_not_called()

    def test_failed_pointer_write_leaves_old_records_and_no_approval(self):
        plan = self.prepare()
        with patch('official_host.write_json', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.service.accept(plan['token'])
        self.assertEqual(self.before_controls, self.service.controls())
        self.assertFalse(self.service.pointer.exists())

    def test_gc_keeps_approval_and_old_recovery_objects(self):
        from cache_gc import CacheGC
        self.approve()
        reference = json.loads(self.service.pointer.read_bytes())['baseline']
        preview = CacheGC(self.manager).preview(grace=0)
        self.assertNotIn(reference, [f['path'] for f in preview['files']])
        self.assertEqual(self.service.read()['version'], '0.2.1-rc.3')

    def test_running_harness_never_approves_or_launches(self):
        self.approve()
        with patch('launcher.running_harness', return_value=[999]):
            with self.assertRaises(LauncherError):
                self.prepare()
            with patch('launcher.subprocess.Popen') as spawn, self.assertRaises(LauncherError):
                self.manager.apply([], self.manager.state()['revision'], launch=True)
            spawn.assert_not_called()

    def test_asar_only_update_keeps_exe_identity_but_needs_independent_proof(self):
        self.exe.write_bytes(b'fixture')
        (self.payload / self.exe.name).write_bytes(b'fixture')
        self.sizes[self.exe.name] = len(b'fixture')
        self.assertEqual(self.service.status()['components'], ['officialArchive'])
        self.approve()
        with patch('launcher.subprocess.Popen') as spawn:
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        spawn.assert_called_once()

    def test_corrupt_approval_cannot_start_or_hide_update(self):
        self.approve()
        reference = json.loads(self.service.pointer.read_bytes())['baseline']
        (self.manager.tracking.root / reference).write_bytes(b'{}')
        self.assertEqual(self.service.status()['status'], 'changed')
        with patch('launcher.subprocess.Popen') as spawn, self.assertRaises(LauncherError):
            self.manager.apply([], self.manager.state()['revision'], launch=True)
        spawn.assert_not_called()

    def test_api_authentication_token_and_mutation_lock(self):
        import http.client
        import threading
        from launcher import Server, Handler
        server = Server(('127.0.0.1', 0), Handler)
        server.token, server.manager = 'host-api-test', self.manager
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(action, body, auth=True):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                headers = {'Content-Type': 'application/json'}
                if auth:
                    headers['Authorization'] = 'Bearer host-api-test'
                client.request('POST', '/api/official-host/' + action, json.dumps(body), headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        try:
            with patch.object(self.service, 'prepare', return_value={'token':'host'}) as prepare, patch.object(self.service, 'accept', return_value={'message':'accepted'}) as accept:
                self.assertEqual(request('prepare', {}, False)[0], 403)
                prepare.assert_not_called()
                self.assertEqual(request('prepare', {'installer':'official.exe','sha512':'sum'})[0], 200)
                prepare.assert_called_once_with('official.exe','sum')
                self.assertEqual(request('accept', {'token':'host'})[0], 200)
                accept.assert_called_once_with('host')
                self.assertEqual(request('unknown', {})[0], 400)
                self.assertFalse(server.mutation_lock.locked())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)


if __name__ == '__main__':
    unittest.main()
