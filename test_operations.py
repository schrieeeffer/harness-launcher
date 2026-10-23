import concurrent.futures
import threading
import unittest

from errors import LauncherError
from operations import OperationLog, report_progress


class OperationLogTests(unittest.TestCase):
    def test_progress_is_real_work_isolated_by_operation_and_thread(self):
        log = OperationLog()
        first = log.begin('launch')
        with log.reporting(first):
            report_progress('files', '校验文件', 10, 20)
            worker = threading.Thread(target=lambda: report_progress('launch', 'unrelated'))
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            progress = log.snapshot()['current']['progress']
            self.assertEqual((progress['step'], progress['completed'], progress['total']), (3, 10, 20))
        report_progress('launch', 'outside')
        self.assertEqual(log.snapshot()['current']['progress'], progress)
        log.finish(first)
        second = log.begin('apply')
        log.progress(first, 'launch', 'late')
        self.assertIsNone(log.snapshot()['current']['progress'])
        with self.assertRaises(ValueError):
            log.progress(second, 'files', 'bad count', 21, 20)
        self.assertEqual(log.snapshot()['last']['progress'], progress)

    def test_lifecycle_preserves_previous_completed_operation(self):
        wall, elapsed = [100.0], [0.0]
        log = OperationLog(clock=lambda: wall[0], monotonic=lambda: elapsed[0])
        self.assertEqual(log.snapshot(), {'current': None, 'last': None})
        first = log.begin('apply')
        elapsed[0] = 2.5
        self.assertEqual(log.snapshot()['current']['durationMs'], 2500)
        # Wall-clock adjustments must not make operation duration negative.
        wall[0] = 99.0
        log.finish(first)
        last = log.snapshot()['last']
        self.assertEqual((last['status'], last['startedAt'], last['finishedAt'], last['durationMs']),
                         ('succeeded', 100.0, 99.0, 2500))
        second = log.begin('launch')
        self.assertNotEqual(first, second)
        self.assertEqual(log.snapshot()['last']['id'], first)
        self.assertEqual(log.snapshot()['current']['id'], second)

    def test_failures_preserve_structured_diagnostics_and_copies_are_isolated(self):
        log = OperationLog()
        identity = log.begin('apply')
        error = LauncherError('文件冲突', code='file_conflict', details={'paths': ['fixture.txt']},
                              recovery='检查文件', retryable=False)
        log.finish(identity, error)
        error.details['paths'].append('later-mutation.txt')
        snapshot = log.snapshot()
        self.assertEqual(snapshot['last']['status'], 'failed')
        self.assertEqual(snapshot['last']['error']['operationId'], identity)
        self.assertEqual(snapshot['last']['error']['code'], 'file_conflict')
        snapshot['last']['error']['details']['paths'].clear()
        self.assertEqual(log.snapshot()['last']['error']['details']['paths'], ['fixture.txt'])

    def test_concurrent_begin_has_exactly_one_owner_and_readers_do_not_block(self):
        log = OperationLog()
        barrier = threading.Barrier(12)

        def attempt(index):
            barrier.wait(timeout=5)
            try:
                return log.begin('apply'), None
            except LauncherError as error:
                return None, error

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(attempt, range(12)))
        owners = [identity for identity, error in outcomes if identity]
        self.assertEqual(len(owners), 1)
        errors = [error for identity, error in outcomes if error]
        self.assertTrue(all(error.code == 'operation_busy' and error.retryable for error in errors))
        self.assertTrue(all(error.details['operationId'] == owners[0] for error in errors))
        self.assertEqual(log.snapshot()['current']['id'], owners[0])
        log.finish(owners[0])

    def test_wrong_or_duplicate_finish_cannot_clear_another_operation(self):
        log = OperationLog()
        identity = log.begin('restore')
        with self.assertRaisesRegex(LauncherError, '不一致'):
            log.finish('wrong')
        self.assertEqual(log.snapshot()['current']['id'], identity)
        log.finish(identity)
        next_id = log.begin('launch')
        with self.assertRaisesRegex(LauncherError, '不一致'):
            log.finish(identity)
        self.assertEqual(log.snapshot()['current']['id'], next_id)

    def test_invalid_kind_never_claims_operation_slot(self):
        log = OperationLog()
        for kind in ('', ' ', None, 1, 'x' * 81):
            with self.subTest(kind=kind), self.assertRaises(LauncherError):
                log.begin(kind)
        self.assertIsNone(log.snapshot()['current'])


if __name__ == '__main__':
    unittest.main()
