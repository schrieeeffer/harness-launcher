import json
import os
from pathlib import Path
import tempfile
import time
import threading
import urllib.request
import unittest
from unittest.mock import patch

from launcher import Manager, MenuError, OFFICIAL, Server, Handler
from patch_modes import PatchModes, tree_hash
from plugin_tracking import TrackingError
import test_patch_modes as fixtures


class GenericTrackingTests(unittest.TestCase):
    setUp = fixtures.PatchModeTests.setUp

    def wait_job(self):
        limit = time.monotonic() + 20
        while self.manager.tracking.busy and time.monotonic() < limit:
            time.sleep(.01)
        self.assertFalse(self.manager.tracking.busy, 'worker did not finish')
        summary = self.manager.tracking.summary()
        if summary['session']:
            self.assertFalse(summary['session'].get('error'), summary['session'].get('error'))
        return summary

    def begin(self, name='plugin-a'):
        self.manager.tracking.begin(name)
        return self.wait_job()

    def install_fixture(self, name):
        target = self.profile / 'node_modules' / name
        target.mkdir(parents=True, exist_ok=True)
        (target / 'package.json').write_text(json.dumps({'name': name, 'version': '1.0', 'dsh': {'bundle': {}}}))
        obj = json.loads(self.manifest.read_bytes())
        obj['dependencies'][name] = 'link:fixture'
        if name not in obj['dsh']['profile']['bundles']:
            obj['dsh']['profile']['bundles'].append(name)
        self.manifest.write_text(json.dumps(obj))

    def accept(self):
        self.manager.tracking.finish()
        result = self.wait_job()
        self.assertEqual(result['session']['phase'], 'review')
        self.manager.tracking.accept()

    def apply(self, ids, launch=False):
        return self.manager.apply(ids, self.manager.state()['revision'], launch=launch, native=not ids)

    def test_modify_add_delete_are_restored_and_reapplied_without_adapter(self):
        old = self.app / 'remove.txt'
        old.write_bytes(b'deleted by new plugin')
        self.begin()
        self.install_fixture('plugin-a')
        (self.app / 'main.js').write_bytes(b'new plugin patch')
        old.unlink()
        added = self.home / 'skills' / 'new-skill' / 'SKILL.md'
        added.parent.mkdir(parents=True)
        added.write_bytes(b'new plugin skill')
        self.accept()
        self.assertEqual(self.manager.state()['plugins'][1]['tracking'], 'auto')
        self.apply([])
        self.assertFalse(self.app.exists())
        self.assertFalse(added.exists())
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])
        self.apply(['dsh-purge', 'plugin-a'])
        self.assertEqual((self.app / 'main.js').read_bytes(), b'new plugin patch')
        self.assertFalse(old.exists())
        self.assertEqual(added.read_bytes(), b'new plugin skill')
        self.assertIn('plugin-a', json.loads(self.manifest.read_bytes())['dependencies'])

    def test_empty_delta_registers_bundle_only_plugin(self):
        self.begin()
        self.install_fixture('plugin-a')
        self.accept()
        self.assertEqual(self.manager.tracking.load()['records']['plugin-a']['count'], 0)
        self.apply([])
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])
        self.apply(['plugin-a'])
        self.assertIn('plugin-a', self.manager.state()['enabled'])

    def test_file_injections_restore_mtimes_needed_by_existing_purge_layer(self):
        before = (self.app / 'main.js').stat().st_mtime_ns
        self.begin()
        self.install_fixture('plugin-a')
        (self.app / 'main.js').write_bytes(b'generic layer')
        self.accept()
        self.apply(['dsh-purge'])
        self.assertEqual((self.app / 'main.js').stat().st_mtime_ns, before)
        ctl = PatchModes(self.manager)
        self.assertEqual(tree_hash(ctl.app), ctl.load()['treeHash'])

    def test_conflicting_shared_file_selection_rolls_back_entire_combination(self):
        self.begin('plugin-a')
        self.install_fixture('plugin-a')
        shared = self.home / 'skills' / 'shared.txt'
        shared.parent.mkdir(parents=True)
        shared.write_bytes(b'A')
        self.accept()
        self.begin('plugin-b')
        self.install_fixture('plugin-b')
        shared.write_bytes(b'A plus B')
        self.accept()
        before = self.manifest.read_bytes()
        with self.assertRaises(MenuError):
            self.apply(['dsh-purge', 'plugin-b'])
        self.assertEqual(shared.read_bytes(), b'A plus B')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertTrue(self.manager.tracking.load()['records']['plugin-a']['enabled'])
        self.assertTrue(self.manager.tracking.load()['records']['plugin-b']['enabled'])
        self.apply([])
        self.assertFalse(shared.exists())
        self.apply(['dsh-purge', 'plugin-a', 'plugin-b'])
        self.assertEqual(shared.read_bytes(), b'A plus B')

    def test_independent_plugins_can_be_toggled_separately(self):
        for name in ['plugin-a', 'plugin-b']:
            self.begin(name)
            self.install_fixture(name)
            file = self.home / 'skills' / (name + '.txt')
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(name.encode())
            self.accept()
        self.apply(['dsh-purge', 'plugin-b'])
        self.assertFalse((self.home / 'skills' / 'plugin-a.txt').exists())
        self.assertTrue((self.home / 'skills' / 'plugin-b.txt').exists())

    def test_unrecorded_changes_stop_native_switch_without_overwriting(self):
        self.begin()
        self.install_fixture('plugin-a')
        self.accept()
        unknown = self.app / 'unrecorded.js'
        unknown.write_bytes(b'new change after recording')
        before = self.manifest.read_bytes()
        with self.assertRaises(MenuError):
            self.apply([])
        self.assertEqual(unknown.read_bytes(), b'new change after recording')
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_credentials_sessions_dependencies_and_outside_paths_are_not_snapshotted(self):
        (self.home / '.credentials.yaml').write_bytes(b'not to be read')
        (self.home / 'sessions').mkdir()
        (self.home / 'sessions' / 'conversation.json').write_bytes(b'not to be read')
        self.begin()
        snap = self.manager.tracking.read_object(self.manager.tracking.load()['session']['baseline'])
        keys = list(snap)
        self.assertFalse(any('credential' in key or 'conversation' in key for key in keys))
        self.assertFalse(any('profiles/desktop/node_modules' in key for key in keys))
        with self.assertRaises(TrackingError):
            self.manager.tracking.target('app|../../outside.txt')

    def test_recording_window_blocks_unrelated_apply_and_duplicate_begin(self):
        self.begin()
        with self.assertRaises(MenuError):
            self.apply([])
        with self.assertRaises(TrackingError):
            self.manager.tracking.begin('plugin-b')
        self.manager.tracking.abandon()
        self.assertIsNone(self.manager.tracking.load()['session'])

    def test_existing_unrecorded_plugin_cannot_fabricate_install_baseline(self):
        self.install_fixture('plugin-a')
        with self.assertRaises(TrackingError):
            self.manager.tracking.begin('plugin-a')

    def test_other_plugin_install_in_window_blocks_attribution(self):
        self.begin()
        self.install_fixture('plugin-a')
        self.install_fixture('plugin-b')
        with self.assertRaises(TrackingError):
            self.manager.tracking.finish()

    def test_review_rejects_files_changed_after_scan(self):
        self.begin()
        self.install_fixture('plugin-a')
        file = self.home / 'rules' / 'new.md'
        file.parent.mkdir()
        file.write_bytes(b'first')
        self.manager.tracking.finish()
        self.wait_job()
        file.write_bytes(b'user changed during review')
        with self.assertRaises(TrackingError):
            self.manager.tracking.accept()

    def test_launch_failure_restores_generic_and_builtin_layers(self):
        self.begin()
        self.install_fixture('plugin-a')
        (self.app / 'main.js').write_bytes(b'A')
        self.accept()
        before = self.manifest.read_bytes()
        with patch('launcher.subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(MenuError):
                self.apply([], launch=True)
        self.assertEqual((self.app / 'main.js').read_bytes(), b'A')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertTrue(self.manager.tracking.load()['records']['plugin-a']['enabled'])

    def test_source_is_passed_as_one_argument_to_official_cli(self):
        tracker = self.manager.tracking
        self.install_fixture('plugin-a')
        with patch('plugin_tracking.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 0
            tracker.install('plugin-a', 'D:/plugins/a & b', '0' * 32)
            args, kwargs = popen.call_args
            self.assertEqual(args[0][-1], 'D:/plugins/a & b')
            self.assertIn('--profile', args[0])
            self.assertEqual(kwargs['env']['DSH_HOME'], str(self.home))
            self.assertNotIn('shell', kwargs)

    def test_generic_records_survive_restart(self):
        self.begin()
        self.install_fixture('plugin-a')
        self.accept()
        fresh = Manager(self.manager.data)
        self.assertIn('plugin-a', fresh.tracking.load()['records'])

    def test_upgrade_records_new_delta_before_replacing_old_version(self):
        self.begin()
        self.install_fixture('plugin-a')
        file = self.app / 'main.js'
        original = file.read_bytes()
        retired = self.home / 'rules' / 'version-one-only.md'
        retired.parent.mkdir()
        retired.write_bytes(b'old plugin injection')
        file.write_bytes(b'A version 1')
        self.accept()
        old_delta = self.manager.tracking.load()['records']['plugin-a']['delta']
        self.begin('plugin-a')
        self.assertEqual(file.read_bytes(), original)
        self.assertFalse(retired.exists())
        self.assertEqual(self.manager.tracking.load()['records']['plugin-a']['delta'], old_delta)
        meta = self.profile / 'node_modules' / 'plugin-a' / 'package.json'
        pkg = json.loads(meta.read_bytes())
        pkg['version'] = '2.0'
        meta.write_text(json.dumps(pkg))
        file.write_bytes(b'A version 2')
        self.accept()
        self.apply([])
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])
        self.assertFalse(retired.exists())
        self.apply(['dsh-purge', 'plugin-a'])
        self.assertEqual(file.read_bytes(), b'A version 2')
        self.assertFalse(retired.exists())
        record = self.manager.tracking.load()['records']['plugin-a']
        self.assertEqual(record['version'], '2.0')
        self.assertEqual(record['history'][0]['version'], '1.0')
        self.assertEqual(record['history'][0]['delta'], old_delta)
        self.assertEqual(self.manager.tracking.summary()['records']['plugin-a']['historyCount'], 1)

    def test_version_changed_during_review_requires_rescan(self):
        self.begin()
        self.install_fixture('plugin-a')
        self.manager.tracking.finish()
        self.wait_job()
        meta = self.profile / 'node_modules' / 'plugin-a' / 'package.json'
        pkg = json.loads(meta.read_bytes())
        pkg['version'] = '2.0'
        meta.write_text(json.dumps(pkg))
        with self.assertRaisesRegex(TrackingError, '复核期间插件版本'):
            self.manager.tracking.accept()
        self.assertNotIn('plugin-a', self.manager.tracking.load()['records'])
        self.accept()
        self.assertEqual(self.manager.tracking.load()['records']['plugin-a']['version'], '2.0')

    def test_recovery_handles_crash_between_file_write_and_cursor_update(self):
        self.begin()
        self.install_fixture('plugin-a')
        file = self.home / 'rules' / 'new.md'
        file.parent.mkdir()
        file.write_bytes(b'A')
        self.accept()
        tracker = self.manager.tracking
        state = tracker.load()
        operations = tracker.operations(tracker.read_object(state['records']['plugin-a']['delta'])['changes'], enable=False)
        journal = json.loads(json.dumps(state))
        journal['pending'] = {'id': 'test', 'operations': operations, 'previous': state}
        tracker.save(journal)
        (tracker.root / 'replay-position.json').write_text(json.dumps({'id': 'test', 'cursor': 0}))
        tracker.replay([operations[0]])
        with self.assertRaises(TrackingError):
            tracker.guard()
        self.assertEqual(file.read_bytes(), b'A')
        self.assertIsNone(tracker.load()['pending'])

    def test_new_links_are_not_silently_registered(self):
        tracker = self.manager.tracking
        changes = [{'key': 'skills|new-link', 'before': None,
                    'after': {'type': 'link', 'target': 'C:/outside'}}]
        with self.assertRaises(TrackingError):
            tracker.operations(changes)

    def test_http_recording_flow_and_native_switch(self):
        server = Server(('127.0.0.1', 0), Handler)
        server.manager, server.token, server.last_seen = self.manager, 'tracking-test', 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def api(route, body=None):
            headers = {'Authorization': 'Bearer tracking-test'}
            data = None
            if body is not None:
                headers['Content-Type'] = 'application/json'
                data = json.dumps(body).encode()
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/' + route,
                                             data=data, headers=headers)
            with opener.open(request, timeout=20) as response:
                return json.load(response)
        api('tracking/begin', {'name': 'plugin-a', 'source': ''})
        self.wait_job()
        self.install_fixture('plugin-a')
        (self.app / 'main.js').write_bytes(b'generic HTTP-recorded patch')
        api('tracking/finish', {})
        self.wait_job()
        self.assertTrue(api('tracking/diff')['changes'])
        api('tracking/accept', {})
        state = api('state')
        self.assertIn('plugin-a', state['tracking']['records'])
        api('apply', {'selection': [], 'revision': state['revision'], 'native': True})
        self.assertTrue(api('state')['patchMode']['verifiedNative'])


if __name__ == '__main__':
    unittest.main()
