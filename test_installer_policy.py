import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from installer_policy import build_key, build_notice, install_arguments, failure_hint, isolated_build_policy
from installer_policy import adapter_policy, adapter_build_window, ensure_adapter_isolation
from errors import LauncherError


class InstallerPolicyTests(unittest.TestCase):
    def test_persistent_isolation_backs_up_policy_and_preserves_unrelated_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = b'packages:\n  - .\n# original policy\n'
            (root / 'pnpm-workspace.yaml').write_bytes(original)
            manager = SimpleNamespace(profile=root, data=root / 'data', require_stopped=Mock(),
                manifest=lambda: (b'', {}, []), discover=lambda *_: [{'id': 'dsh-purge'}])
            state, calls, command = self.policy({'dsh-purge': True, 'ordinary': True})
            with patch('installer_policy.policy_command', return_value=command):
                ensure_adapter_isolation(manager, {})
                writes = sum(call[0] == 'set' for call in calls)
                ensure_adapter_isolation(manager, {})
            self.assertEqual(sum(call[0] == 'set' for call in calls), writes)
            self.assertEqual(state['allowBuilds'], {'dsh-purge': False, 'ordinary': True})
            backups = list((root / 'data' / 'installer-policy').glob('*/*.yaml'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_adapter_timeout_does_not_write_policy_while_child_is_running(self):
        state, calls, command = self.policy({'dsh-purge': False})
        with self.assertRaises(LauncherError):
            with adapter_build_window('dsh-purge', command):
                count = len(calls)
                raise LauncherError('still running', code='installer_timeout')
        self.assertEqual(len(calls), count)

    def test_adapter_denial_covers_pending_git_selector_without_touching_other_packages(self):
        key = 'dsh-purge@https://codeload.github.com/demo/purge/tar.gz/' + 'a'*40
        original = {'dsh-purge': True, key: 'set this to true or false', 'dsh-purge-helper': True, 'custom@1.0.0': True}
        result = adapter_policy(original, ['dsh-purge'])
        self.assertEqual(result, {**original, 'dsh-purge': False, key: False})
        self.assertIs(original['dsh-purge'], True)

    def test_explicit_adapter_build_window_restores_isolation_after_error(self):
        state, _, command = self.policy({'dsh-purge': False, 'dsh-purge@1.0.0': False, 'custom': True})
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with adapter_build_window('dsh-purge', command):
                self.assertNotIn('dsh-purge', state['allowBuilds'])
                self.assertNotIn('dsh-purge@1.0.0', state['allowBuilds'])
                state['allowBuilds']['dsh-purge@2.0.0'] = True
                state['allowBuilds']['new-dependency'] = True
                raise RuntimeError('fixture')
        self.assertEqual(state['allowBuilds'], {'custom': True, 'new-dependency': True,
                                             'dsh-purge': False, 'dsh-purge@2.0.0': False})

    def policy(self, values=None):
        state = {'allowBuilds': dict(values or {})}
        calls = []
        def command(args):
            calls.append(args)
            if args[0] == 'get':
                return json.dumps(state[args[1]]) if args[1] in state else 'undefined'
            self.assertEqual(args[-2:], ['--json', '--location=project'])
            state[args[1]] = json.loads(args[2])
            return ''
        return state, calls, command

    def test_other_plugins_are_denied_only_during_selected_install(self):
        state, calls, command = self.policy({'plugin-a': True, 'shared-library': True})
        with isolated_build_policy(['plugin-a', '@demo/plugin-b'], command):
            self.assertIs(state['allowBuilds']['plugin-a'], False)
            self.assertIs(state['allowBuilds']['@demo/plugin-b'], False)
            self.assertIs(state['allowBuilds']['shared-library'], True)
            state['allowBuilds']['selected-plugin@2.0.0'] = True
        self.assertEqual(state['allowBuilds'], {'plugin-a': True, 'shared-library': True,
                                              'selected-plugin@2.0.0': True})

    def test_failed_install_restores_previous_policy(self):
        state, calls, command = self.policy({'plugin-a': True})
        with self.assertRaisesRegex(LauncherError, 'installer failure'):
            with isolated_build_policy(['plugin-a'], command):
                raise LauncherError('installer failure')
        self.assertEqual(state['allowBuilds'], {'plugin-a': True})

    def test_policy_conflict_is_retained_instead_of_overwritten(self):
        state, calls, command = self.policy({'plugin-a': False})
        with self.assertRaises(LauncherError) as caught:
            with isolated_build_policy(['plugin-a'], command):
                state['allowBuilds']['plugin-a'] = True
        self.assertEqual(caught.exception.code, 'installer_policy_conflict')
        self.assertEqual(state['allowBuilds'], {'plugin-a': True})

    def test_child_still_running_leaves_denials_for_recovery(self):
        state, calls, command = self.policy({'plugin-a': True})
        with self.assertRaises(LauncherError) as caught:
            with isolated_build_policy(['plugin-a'], command):
                raise LauncherError('still running', code='installer_timeout')
        self.assertEqual(caught.exception.code, 'installer_timeout')
        self.assertEqual(state['allowBuilds'], {'plugin-a': False})

    def test_unknown_or_unrestricted_build_policy_never_starts_install(self):
        for key, value in [('dangerouslyAllowAllBuilds', True), ('permissions', {'plugin-a': {'build': True}})]:
            state, calls, command = self.policy({'plugin-a': True})
            state[key] = value
            with self.assertRaises(LauncherError):
                with isolated_build_policy(['plugin-a'], command):
                    self.fail('Unsafe build policy reached installer')
            self.assertFalse(any(call[0] == 'set' for call in calls))

    def test_invalid_non_target_names_never_write_policy(self):
        for name in ('*', '--all', 'plugin-a\nanything', None):
            state, calls, command = self.policy()
            with self.assertRaises(LauncherError):
                with isolated_build_policy([name], command):
                    self.fail('Invalid name reached installer')
            self.assertEqual(calls, [])

    def test_exact_npm_and_scoped_versions_only(self):
        for name in ('plugin-a', '@demo/plugin-b'):
            source = name + '@1.2.3-beta.1'
            self.assertEqual(install_arguments(name, source), ['add', source, '--allow-build=' + source])
            for wrong in (name, name + '@latest', name + '@^1.2.3', 'different@1.2.3'):
                self.assertIsNone(build_key(name, wrong))

    def test_git_source_and_build_selector_are_both_pinned(self):
        source = 'git+https://github.com/demo/plugin.git#' + 'a' * 40
        key = 'plugin-a@https://codeload.github.com/demo/plugin/tar.gz/' + 'a' * 40
        self.assertEqual(build_key('plugin-a', source), key)
        self.assertEqual(install_arguments('plugin-a', source), ['add', source, '--allow-build=' + key])
        self.assertIn(key, build_notice('plugin-a', source))
        self.assertNotIn('后续版本', build_notice('plugin-a', source))
        self.assertIn('a' * 40, build_notice('plugin-a', source))
        self.assertEqual(build_key('@demo/plugin-a', source), '@demo/' + key)
        for wrong in (source.replace('a' * 40, 'main'), source.replace('github.com', 'evil.invalid'), source + '?x=1'):
            self.assertIsNone(build_key('plugin-a', wrong))

    def test_local_unknown_and_option_names_never_get_build_approval(self):
        self.assertEqual(install_arguments('plugin-a', 'D:/plugins/source'), ['add', 'D:/plugins/source'])
        for name in ('*', '--all', 'plugin-a\n--all', '', None):
            self.assertIsNone(build_key(name, 'plugin-a@1.2.3'))

    def test_error_classification_is_bounded_and_not_log_echo(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / 'install.log'
            log.write_bytes(b'private payload\nERR_PNPM_IGNORED_BUILDS\n')
            self.assertIn('安装脚本', failure_hint(log))
            self.assertNotIn('private', failure_hint(log))
            log.write_bytes(b'network unavailable')
            self.assertEqual(failure_hint(log), '')
            log.unlink()
            self.assertEqual(failure_hint(log), '')
