import copy
from dataclasses import replace
import unittest

from domain import (ContentIdentity, DomainError, ManagedPath, SnapshotEntry,
                    SnapshotMetadata, relative_path, validate_changes, validate_snapshot)
from recording_state import (RecordingEvent as Event, RecordingPhase as Phase,
                             RecordingSession, RunState, validate_registry)


HASH = 'a' * 64


def ref(kind):
    return kind + '/' + HASH + '.json'


def preparing():
    return RecordingSession(id='b' * 32, plugin='plugin-a', source='', phase=Phase.PREPARING,
                            started=1.0, host_hash=HASH, initial_inventory=ref('snapshots')).validate()


def ready(source=''):
    return replace(preparing(), source=source).transition(Event.BASELINE_READY,
                                                         baseline=ref('snapshots'), manifest_before=ref('baselines'))


def closed():
    return (ready().transition(Event.LAUNCH, launch_at=2.0)
            .transition(Event.OBSERVE_RUNNING).transition(Event.OBSERVE_CLOSED))


class DomainTests(unittest.TestCase):
    def test_content_identity_excludes_original_file_metadata(self):
        a = {'type': 'file', 'hash': HASH, 'size': 20, 'mtime': 100, 'stamp': [20, 100, 101]}
        b = {**a, 'mtime': 999, 'stamp': [20, 999, 1000]}
        self.assertEqual(ContentIdentity.decode(a), ContentIdentity.decode(b))
        self.assertNotEqual(SnapshotEntry.decode(a), SnapshotEntry.decode(b))

    def test_source_stamp_cannot_be_reused_as_original_snapshot_metadata(self):
        with self.assertRaisesRegex(DomainError, '不一致'):
            SnapshotMetadata.decode({'size': 10, 'mtime': 100, 'stamp': [10, 900, 900]})

    def test_snapshot_roundtrip_retains_original_times_and_omits_source_objects(self):
        value = {'type': 'file', 'hash': HASH, 'size': 20, 'mtime': 100, 'stamp': [20, 100, 101]}
        self.assertEqual(SnapshotEntry.decode(value).to_dict(), value)
        value.pop('stamp')
        self.assertEqual(SnapshotEntry.decode(value).to_dict(), value)

    def test_malformed_content_and_metadata_are_rejected(self):
        for value in ({'type': 'file', 'hash': '../outside'}, {'type': 'link', 'target': ''},
                      {'type': 'dir', 'hash': HASH}, {'type': 'file', 'hash': HASH, 'target': 'elsewhere'},
                      {'type': 'file', 'hash': HASH, 'size': True, 'mtime': 0},
                      {'type': 'file', 'hash': HASH, 'size': 10, 'mtime': -1}):
            with self.subTest(value=value), self.assertRaises(DomainError):
                SnapshotEntry.decode(value)

    def test_managed_paths_cannot_escape_or_use_aliases(self):
        for key in ('app|../outside', 'app|/absolute', 'app|a/./b', 'app|a//b',
                    'app|C:/target', 'app|a\\b', 'app|', 'app|x|y',
                    'app|file.', 'app|file ', 'app|CON', 'app|CONOUT$', 'app|nul.txt',
                    'app|nested/LPT1.log', 'app|COM¹', 'app|nested /file',
                    'app|*.js', 'app|bad\x00name', 'app|bad\nname'):
            with self.subTest(key=key), self.assertRaises(DomainError):
                ManagedPath.decode(key)
        self.assertEqual(ManagedPath.decode('app|.').relative, '.')
        self.assertEqual(ManagedPath.decode('skills|test/SKILL.md').scope, 'skills')
        self.assertEqual(ManagedPath.decode('app|.config/COM10.txt').relative, '.config/COM10.txt')

    def test_inventory_links_reject_the_same_invalid_targets_as_content_snapshots(self):
        for target in ('bad\x00target', '', None, 123):
            with self.subTest(target=target), self.assertRaises(DomainError):
                validate_snapshot({'app|link': ['link', target]})

    def test_cached_path_syntax_keeps_root_permission_and_type_validation(self):
        self.assertEqual(str(relative_path('.', allow_root=True)), '.')
        for value in ('.', [], {}, None, 123, 'ok/../outside', 'ok/COM1.txt'):
            with self.subTest(value=value), self.assertRaises(DomainError):
                relative_path(value)
        self.assertEqual(str(relative_path('ok/file.txt')), 'ok/file.txt')

    def test_snapshot_metadata_rejects_non_objects_at_decode_boundary(self):
        for value in (None, [], 'metadata', 1):
            with self.subTest(value=value), self.assertRaises(DomainError):
                SnapshotMetadata.decode(value)

    def test_snapshot_cannot_mix_inventory_and_content(self):
        with self.assertRaisesRegex(DomainError, '混合'):
            validate_snapshot({'app|.': ['dir'], 'app|child': {'type': 'dir'}})
        validate_snapshot({'app|.': ['dir'], 'app|child': ['file', 0, 1]})

    def test_duplicate_delta_paths_cannot_override_an_earlier_operation(self):
        change = {'key': 'app|child', 'before': None, 'after': {'type': 'dir'}}
        with self.assertRaisesRegex(DomainError, '重复'):
            validate_changes({'changes': [change, copy.deepcopy(change)]})


class RecordingStateTests(unittest.TestCase):
    def test_complete_workflow_roundtrips_without_boolean_flags(self):
        session = ready('plugin-a@1').transition(Event.INSTALL).transition(Event.INSTALLED)
        session = session.transition(Event.LAUNCH, launch_at=2).transition(Event.OBSERVE_RUNNING)
        session = session.transition(Event.OBSERVE_CLOSED).transition(Event.SCAN)
        session = session.transition(Event.VERIFY, after=ref('snapshots'), delta=ref('deltas'), version='1', count=3)
        value = session.to_dict()
        self.assertEqual(session, RecordingSession.decode(value))
        self.assertEqual(value['runState'], 'closed')
        self.assertFalse({'automatic', 'observedRunning', 'runClosed'} & value.keys())

    def test_scan_cannot_skip_observed_runtime_and_close(self):
        for session in (preparing(), ready(), ready().transition(Event.LAUNCH, launch_at=2),
                        ready().transition(Event.LAUNCH, launch_at=2).transition(Event.OBSERVE_RUNNING)):
            with self.subTest(phase=session.phase, run=session.run_state), self.assertRaises(DomainError):
                session.transition(Event.SCAN)

    def test_launch_intent_is_not_replaced_without_an_explicit_retry(self):
        session = ready().transition(Event.LAUNCH, launch_at=2)
        with self.assertRaises(DomainError):
            session.transition(Event.LAUNCH, launch_at=3)
        retried = session.transition(Event.FAIL, error='failed start').transition(Event.RETRY)
        self.assertEqual(retried.run_state, RunState.IDLE)
        self.assertEqual(retried.baseline, session.baseline)
        self.assertIsNone(retried.launch_at)
        self.assertEqual(retried.transition(Event.LAUNCH, launch_at=3).launch_at, 3)

    def test_failed_install_retry_retains_baseline_without_faking_install_success(self):
        session = ready('plugin-a@1').transition(Event.FAIL, error='installer exited')
        retried = session.transition(Event.RETRY)
        self.assertEqual(retried.phase, Phase.INSTALLING)
        self.assertFalse(retried.installed)
        self.assertEqual(retried.baseline, session.baseline)

    def test_failed_scan_retry_never_relaunches_and_discards_stale_scan_results(self):
        session = closed().transition(Event.SCAN).transition(Event.VERIFY,
                  after=ref('snapshots'), delta=ref('deltas'), version='1', count=3)
        session = session.transition(Event.FAIL, error='files changed')
        resumed = session.transition(Event.RETRY).transition(Event.SCAN)
        self.assertEqual(resumed.run_state, RunState.CLOSED)
        self.assertEqual(resumed.baseline, session.baseline)
        self.assertIsNone(resumed.after)
        self.assertIsNone(resumed.delta)

    def test_error_pauses_state_until_retry(self):
        session = ready().transition(Event.FAIL, error='locked')
        with self.assertRaises(DomainError):
            session.transition(Event.LAUNCH, launch_at=2)
        with self.assertRaises(DomainError):
            ready().transition(Event.RETRY)

    def test_corrupt_phase_or_run_combinations_are_rejected_at_decode(self):
        good = ready().to_dict()
        for change in ({'phase': 'scanning'}, {'runState': 'closed'}, {'phase': 'verifying'},
                       {'baseline': None}, {'phase': 'unknown'}, {'runClosed': True}, {'automatic': True}):
            with self.subTest(change=change), self.assertRaises(DomainError):
                RecordingSession.decode({**good, **change})

    def test_registry_rejects_old_formats_and_session_corruption(self):
        value = {'format': 3, 'records': {}, 'order': [], 'session': None}
        validate_registry(value)
        for changed in ({'format': 2}, {'format': 3.0}, {'pending': False}, {'order': ['missing']},
                        {'requiredFeatures': ['unimplemented-file-merge']},
                        {'session': {**ready().to_dict(), 'runClosed': True}}):
            with self.subTest(changed=changed), self.assertRaises(DomainError):
                validate_registry({**value, **changed})


if __name__ == '__main__':
    unittest.main()
