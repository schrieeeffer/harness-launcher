import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from launcher import Manager, MenuError, OFFICIAL
from patch_modes import PatchError, PatchModes, archive_file, archive_info, file_hash, tree_hash


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


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'app.asar'
        self.unpacked = self.root / 'app.asar.unpacked'

    def write_archive(self, files, payload=b''):
        header = json.dumps({'files': files}).encode()
        self.archive.write_bytes(struct.pack('<4I', 4, len(header) + 8,
                                             len(header) + 4, len(header)) + header + payload)

    def test_unpacking_rejects_windows_drive_paths_before_reading(self):
        self.write_archive({'C:': {'files': {'package.json': {'unpacked': True, 'size': 0}}}})
        with self.assertRaises(PatchError):
            archive_info(self.archive, self.unpacked, verify=True)
        with self.assertRaises(PatchError):
            archive_file(self.archive, 'C:/package.json', self.unpacked)

    def test_archive_entry_reads_reject_traversal_and_backslashes(self):
        make_asar(self.archive)
        for name in (None, '../package.json', r'..\package.json', '/package.json', 'package.json\x00'):
            with self.subTest(name=name), self.assertRaises(PatchError):
                archive_file(self.archive, name, self.unpacked)

    def test_unpacked_entries_cannot_follow_a_junction(self):
        content = b'{"name":"@deepseek-ai/dsh-desktop","version":"1"}'
        self.unpacked.mkdir()
        (self.unpacked / 'package.json').write_bytes(content)
        self.write_archive({'package.json': {'unpacked': True, 'size': len(content)}})
        is_junction = Path.is_junction
        with patch.object(Path, 'is_junction', lambda path: path == self.unpacked or is_junction(path)):
            with self.assertRaises(PatchError):
                archive_info(self.archive, self.unpacked)
            with self.assertRaises(PatchError):
                archive_file(self.archive, 'package.json', self.unpacked)

    def test_invalid_offsets_sizes_and_shapes_raise_controlled_errors(self):
        entries = [[], {'size': -1, 'offset': '0'}, {'size': True, 'offset': '0'},
                   {'size': 1, 'offset': '-1'}, {'size': 2 ** 64, 'offset': '0'},
                   {'size': 0, 'offset': '0', 'integrity': []}]
        for entry in entries:
            self.write_archive({'package.json': entry})
            with self.subTest(entry=entry):
                with self.assertRaises(PatchError):
                    archive_info(self.archive, self.unpacked)
                with self.assertRaises(PatchError):
                    archive_file(self.archive, 'package.json', self.unpacked)


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
        from test_launcher import make_runtime
        make_runtime(self.exe)
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

    def test_missing_record_launch_uses_retained_archive_and_preserves_user_rules(self):
        self.switch([])
        self.switch(['dsh-purge'])
        controller = self.manager.tracking.core
        controller.record.unlink()
        self.assertFalse(self.archive.exists())
        self.assertTrue(self.manager.state()['patchMode']['archiveAvailable'])

        with patch('launcher.subprocess.Popen') as spawn:
            self.manager.apply(['dsh-purge'], self.manager.state()['revision'], launch=True)
        spawn.assert_called_once()
        self.switch([])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'original user instruction')
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])
        self.switch(['dsh-purge'])
        self.assertEqual(tree_hash(self.app), self.initial_tree)
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'Little Code Sauce plugin-owned rule')

    def test_recapture_prefers_retained_official_archive_over_plugin_backup(self):
        self.switch([])
        self.switch(['dsh-purge'])
        controller = self.manager.tracking.core
        controller.record.unlink()
        backup = self.resources / 'app.asar.bak'
        backup.write_bytes(b'not an official archive')

        self.switch([])
        self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        self.assertEqual(backup.read_bytes(), b'not an official archive')

    def test_recapture_rejects_corrupt_retained_archive_without_changing_app(self):
        self.switch([])
        self.switch(['dsh-purge'])
        controller = self.manager.tracking.core
        controller.record.unlink()
        official = controller.stash / 'official.asar'
        original = official.read_bytes()
        official.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        before = self.manifest.read_bytes()

        with self.assertRaisesRegex(MenuError, '完整性校验失败'):
            self.switch([])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(tree_hash(self.app), self.initial_tree)
        self.assertFalse(controller.record.exists())
        self.assertFalse(self.archive.exists())

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
        ctl = self.manager.tracking.core
        changed = ctl.stash / 'app' / 'main.js'
        changed.write_bytes(b'other program changed this')
        with self.assertRaises(MenuError):
            self.switch(['dsh-purge'])
        self.assertEqual(changed.read_bytes(), b'other program changed this')
        self.assertFalse(self.app.exists())

    def test_timestamp_only_changes_to_captured_trees_do_not_block_switching(self):
        self.switch([])
        controller = self.manager.tracking.core
        for target in (controller.stash / 'app' / 'main.js',
                       controller.home_stash / 'redteam' / 'preset.yml'):
            stamp = target.stat().st_mtime_ns + 2_000_000_000
            os.utime(target, ns=(stamp, stamp))

        self.switch(['dsh-purge'])
        self.assertEqual((self.app / 'main.js').read_bytes(), b'// existing dsh-purge patch\n')
        self.switch([])
        self.assertTrue(self.manager.state()['patchMode']['verifiedNative'])

    def test_same_timestamp_content_change_to_captured_tree_is_rejected(self):
        self.switch([])
        controller = self.manager.tracking.core
        target = controller.stash / 'app' / 'main.js'
        before = target.stat()
        modified = b'X' * before.st_size
        target.write_bytes(modified)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

        with self.assertRaisesRegex(MenuError, '补丁目录有新改动'):
            self.switch(['dsh-purge'])
        self.assertEqual(target.read_bytes(), modified)
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
        self.assertEqual(self.manager.tracking.core.load()['mode'], 'captured')

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

    def test_adapter_context_and_archive_source_do_not_depend_on_manager_or_tracker(self):
        core = self.manager.tracking.core
        versions = self.manager.tracking.versions
        self.assertFalse(hasattr(core, 'manager'))
        self.assertFalse(hasattr(versions, 'tracker'))
        self.assertIs(versions.host_archive, core.archive_source)
        self.assertEqual(versions.host_archive.official_archive(), self.archive)
        self.switch([])
        self.assertEqual(core.load()['format'], 3)

    def test_adapter_rejects_legacy_or_incomplete_content_identity(self):
        self.switch([])
        core = self.manager.tracking.core
        state = core.load()
        for alter in (lambda value: value.update(format=2),
                      lambda value: value.pop('treeContentHash'),
                      lambda value: value['directories'][0].pop('contentHash')):
            value = json.loads(json.dumps(state))
            alter(value)
            core.record.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaisesRegex(PatchError, '补丁记录格式或内容无效'):
                core.load()
            self.assertEqual(self.archive.read_bytes(), self.archive_bytes)
        core.save(state)

    def test_adapter_rejects_noncanonical_target_before_mutating_files(self):
        core = self.manager.tracking.core
        target = self.home / 'rules' / '..' / 'AGENTS.md'
        with self.assertRaisesRegex(PatchError, '非规范'):
            core.safe(target)
        self.assertEqual((self.home / 'AGENTS.md').read_bytes(), b'Little Code Sauce plugin-owned rule')



if __name__ == '__main__':
    unittest.main()
