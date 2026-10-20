import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from launcher import Manager, MenuError, OFFICIAL
from patch_modes import PatchModes, file_hash, tree_hash


def make_asar(path):
    contents = {'package.json': json.dumps({'name': '@deepseek-ai/dsh-desktop', 'version': '0.2-test', 'dshBuildCommit': 'test'}).encode(),
                'main.js': b'// official original code\n'}
    files, payload, offset = {}, b'', 0
    for name, data in contents.items():
        files[name] = {'size': len(data), 'offset': str(offset), 'integrity': {'algorithm': 'SHA256', 'hash': hashlib.sha256(data).hexdigest()}}
        offset += len(data)
        payload += data
    encoded = json.dumps({'files': files}).encode()
    header = struct.pack('<4I', 4, len(encoded) + 8, len(encoded) + 4, len(encoded))
    path.write_bytes(header + encoded + payload)
    return contents


class PatchModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.profile = self.home / 'profiles' / 'desktop'
        self.profile.mkdir(parents=True)
        self.exe = self.root / 'install' / 'DeepSeek Harness.exe'
        self.exe.parent.mkdir()
        self.exe.write_bytes(b'fixture')
        self.resources = self.exe.parent / 'resources'
        self.resources.mkdir()
        self.archive = self.resources / 'app.asar'
        original = make_asar(self.archive)
        self.archive_bytes = self.archive.read_bytes()
        self.app = self.resources / 'app'
        self.app.mkdir()
        (self.app / 'package.json').write_bytes(original['package.json'])
        (self.app / 'main.js').write_bytes(b'// existing dsh-purge patch\n')
        self.purge = self.profile / 'node_modules' / 'dsh-purge'
        self.purge.mkdir(parents=True)
        (self.purge / 'package.json').write_text(json.dumps({'name': 'dsh-purge', 'version': '1.1-test', 'dsh': {'bundle': {'patch': './cordis.patch.yml'}}}))
        self.manifest = self.profile / 'package.json'
        self.manifest.write_text(json.dumps({'dependencies': {'dsh-purge': 'link:local'}, 'dsh': {'profile': {'bundles': [*OFFICIAL, 'dsh-purge']}}}))
        (self.profile / 'cordis.yml').write_text('[]\n')
        (self.profile / 'cordis.patch.yml').write_text('[]\n')
        (self.home / 'AGENTS.md').write_bytes(b'Little Code Sauce plugin-owned rule')
        (self.home / 'AGENTS.md.dsh-purge-rule.bak').write_bytes(b'original user instruction')
        (self.home / 'prompt-inject.md').write_bytes(b'plugin injection')
        (self.home / '.agent-presets' / 'redteam').mkdir(parents=True)
        (self.home / '.agent-presets' / 'redteam' / 'preset.yml').write_text('plugin preset')
        self.cli = self.resources / 'runtime' / 'cli' / 'bin' / 'dsh.cmd'
        self.cli.parent.mkdir(parents=True)
        self.cli.write_bytes(b'ELECTRON_RUN_AS_NODE dsh-desktop-host \\app.asar\\file.js')
        self.manager = Manager(self.root / 'data', self.home, self.exe)
        self.process_patch = patch('launcher.running_harness', return_value=[])
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)
        self.initial_manifest = self.manifest.read_bytes()
        self.initial_tree = tree_hash(self.app)

    def switch(self, ids):
        return self.manager.apply(ids, self.manager.state()['revision'], native=not ids)

    def test_off_uses_exact_official_archive_and_restores_original_user_rules(self):
        self.switch([])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertFalse(self.app.exists())
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'original user instruction')
        self.assertFalse((self.home / 'prompt-inject.md').exists())
        self.assertFalse((self.home / '.agent-presets' / 'redteam').exists())
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])

    def test_reenable_restores_exact_patch_and_injections_and_repairs_cli_path(self):
        self.switch([])
        self.switch(['dsh-purge'])
        self.assertEqual(tree_hash(self.app), self.initial_tree)
        self.assertFalse(self.archive.exists())
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'Little Code Sauce plugin-owned rule')
        self.assertTrue((self.home / 'prompt-inject.md').is_file())
        self.assertTrue((self.home / '.agent-presets' / 'redteam').is_dir())
        self.assertIn(b'\\app\\file.js', self.cli.read_bytes())
        self.switch([])
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])

    def test_archive_update_does_not_restore_old_patches(self):
        self.switch([])
        self.archive.write_bytes(self.archive_bytes + b'updated')
        before = self.manifest.read_bytes()
        with self.assertRaises(MenuError):
            self.switch(['dsh-purge'])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse(self.app.exists())

    def test_other_program_changes_to_patch_tree_are_preserved(self):
        self.switch([])
        ctl = PatchModes(self.manager)
        changed = ctl.stash / 'app' / 'main.js'
        changed.write_bytes(b'other program changed this')
        with self.assertRaises(MenuError):
            self.switch(['dsh-purge'])
        self.assertEqual(changed.read_bytes(), b'other program changed this')
        self.assertFalse(self.app.exists())

    def test_unadapted_plugin_blocks_native_claim_before_mutating_files(self):
        other = self.profile / 'node_modules' / 'other'
        other.mkdir()
        (other / 'package.json').write_text(json.dumps({'name': 'other', 'dsh': {'bundle': {}}}))
        with self.assertRaises(MenuError):
            self.switch([])
        self.assertTrue(self.app.exists())
        self.assertEqual(self.manifest.read_bytes(), self.initial_manifest)

    def test_dynamic_or_plugin_yaml_blocks_native_claim(self):
        (self.profile / 'cordis.patch.yml').write_text('- insert:\n  - name: other-plugin\n')
        with self.assertRaises(MenuError):
            self.switch([])
        self.assertTrue(self.app.exists())

    def test_mid_switch_failure_rolls_back_directory_files_and_manifest(self):
        real_replace = os.replace
        failed = False
        def fail_once(source, dest):
            nonlocal failed
            if not failed and Path(source) == self.home / '.agent-presets' / 'redteam':
                failed = True
                raise OSError('simulated occupied directory')
            return real_replace(source, dest)
        with patch('patch_modes.os.replace', side_effect=fail_once):
            with self.assertRaises(MenuError):
                self.switch([])
        self.assertEqual(tree_hash(self.app), self.initial_tree)
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(self.manifest.read_bytes(), self.initial_manifest)
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'Little Code Sauce plugin-owned rule')
        self.assertEqual(PatchModes(self.manager).load()['mode'], 'legacy')

    def test_launch_failure_rolls_back_patch_switch(self):
        with patch('launcher.subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(MenuError):
                self.manager.apply([], self.manager.state()['revision'], launch=True, native=True)
        self.assertTrue(self.app.exists())
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(self.manifest.read_bytes(), self.initial_manifest)

    def test_snapshot_version_mismatch_is_rejected(self):
        self.switch([])
        (self.purge / 'package.json').write_text(json.dumps({'name': 'dsh-purge', 'version': 'new', 'dsh': {'bundle': {}}}))
        with self.assertRaises(MenuError):
            self.switch(['dsh-purge'])
        self.assertFalse(self.app.exists())

    def test_changed_native_user_rule_is_not_overwritten(self):
        self.switch([])
        (self.home / 'AGENTS.md').write_bytes(b'new native rule written by user')
        with self.assertRaises(MenuError):
            self.switch(['dsh-purge'])
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'new native rule written by user')

    def test_incomplete_journal_recovers_on_next_attempt(self):
        ctl = PatchModes(self.manager)
        raw, obj, bundles = self.manager.manifest()
        state = ctl.capture(self.manager.discover(obj, bundles))
        op = ctl.move_op(ctl.app, ctl.stash / 'app')
        state['pending'] = {'target': 'native', 'operations': [op]}
        ctl.save(state)
        ctl.replay([op])
        with self.assertRaises(MenuError):
            self.switch([])
        self.assertEqual(tree_hash(self.app), self.initial_tree)
        self.assertIsNone(ctl.load()['pending'])


if __name__ == '__main__':
    unittest.main()
