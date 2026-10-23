"""Previewing a current store must never perform transaction recovery."""
import json
import unittest
from unittest.mock import patch

from errors import PatchError
from file_view import FileView
from migrate import inventory, migrate
from switch_plan import SwitchPlan
import test_version_plan as fixtures


class CurrentMigrationTests(unittest.TestCase):
    setUp = fixtures.VersionPlanTests.setUp
    wait_job = fixtures.VersionPlanTests.wait_job
    begin = fixtures.VersionPlanTests.begin
    install_fixture = fixtures.VersionPlanTests.install_fixture
    accept = fixtures.VersionPlanTests.accept
    close_runtime = fixtures.VersionPlanTests.close_runtime
    apply = fixtures.VersionPlanTests.apply
    record = fixtures.VersionPlanTests.record
    prepare = fixtures.VersionPlanTests.prepare

    def test_current_format_preview_preserves_interrupted_switch_and_live_files(self):
        self.record()
        planner = SwitchPlan(self.manager.tracking)
        plan = self.prepare([])
        count = next(i for i, op in enumerate(plan['operations'])
                     if op.get('kind') == 'move' and op.get('directory')) + 1
        for op in plan['operations'][:count]:
            planner.replay(op)
        planner.journal.write_text(json.dumps({'format': 3, 'cursor': count, 'committed': False,
            'plan': self.manager.tracking.versions.object('transactions', plan)}))
        before = inventory(self.manager.data)
        actual = planner._signature(FileView(planner.roots()))
        with patch.object(SwitchPlan, 'recover', side_effect=AssertionError('read-only checks must not recover')):
            for apply in (False, True):
                with self.subTest(apply=apply), self.assertRaisesRegex(PatchError, '切换尚未完成'):
                    migrate(self.manager.data, apply=apply)
        self.assertEqual(inventory(self.manager.data), before)
        self.assertEqual(planner._signature(FileView(planner.roots())), actual)
        self.assertTrue(planner.journal.is_file())

    def test_current_format_pending_session_preview_preserves_registry(self):
        self.begin()
        before = inventory(self.manager.data)
        with patch.object(SwitchPlan, 'recover', side_effect=AssertionError('read-only checks must not recover')):
            with self.assertRaisesRegex(PatchError, '安装记录尚未完成'):
                migrate(self.manager.data)
        self.assertEqual(inventory(self.manager.data), before)

    def test_current_format_preview_checks_contents_without_writing_objects(self):
        self.record()
        before = inventory(self.manager.data)
        with patch.object(SwitchPlan, 'recover', side_effect=AssertionError('read-only checks must not recover')):
            self.assertEqual(migrate(self.manager.data)['status'], 'already-current')
        self.assertEqual(inventory(self.manager.data), before)

    def test_current_format_preview_rejects_modified_parked_adapter_files(self):
        self.record()
        self.apply([])
        tracker = self.manager.tracking
        targets = (tracker.core.stash / 'app' / 'main.js',
                   tracker.core.home_stash / 'redteam' / 'preset.yml')
        for target in targets:
            original = target.read_bytes()
            target.write_bytes(b'external change in a parked patch')
            before = inventory(self.manager.data)
            with self.subTest(target=target), self.assertRaises(PatchError):
                migrate(self.manager.data)
            self.assertEqual(inventory(self.manager.data), before)
            self.assertEqual(target.read_bytes(), b'external change in a parked patch')
            target.write_bytes(original)

    def test_current_format_preview_checks_adapter_below_enabled_generic_layers(self):
        self.begin()
        self.install_fixture('plugin-a')
        (self.app / 'main.js').write_bytes(b'generic patch over the adapter')
        self.accept()
        self.apply(['dsh-purge', 'plugin-a'])
        before = inventory(self.manager.data)
        self.assertEqual(migrate(self.manager.data)['status'], 'already-current')
        self.assertEqual(inventory(self.manager.data), before)


if __name__ == '__main__':
    unittest.main()
