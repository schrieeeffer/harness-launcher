"""Bounded, thread-safe status of the current and most recent UI operation.

This is transient observability, not a transaction journal or a task queue.
File recovery belongs to the persisted transaction. Reading this log never
acquires the manager's potentially long-held file-operation lock.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import uuid

from errors import LauncherError, error_payload


_reporter = ContextVar('operation_reporter', default=None)


def report_progress(stage, label, completed=None, total=None, unit='files'):
    """Publish actual work, never a time-based estimate; absent outside HTTP work."""
    reporter = _reporter.get()
    if reporter is not None:
        reporter(stage, label, completed, total, unit)


class OperationLog:
    def __init__(self, *, clock=time.time, monotonic=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._monotonic = monotonic
        self._started = None
        self._current = None
        self._last = None

    def begin(self, kind):
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 80:
            raise LauncherError('操作类型无效。', code='invalid_operation_kind')
        with self._lock:
            if self._current is not None:
                raise LauncherError('另一个操作正在执行，请等待完成。', code='operation_busy',
                                    retryable=True,
                                    recovery='查看当前操作状态；结束后重新确认是否仍需执行。',
                                    details={'operationId': self._current['id'], 'kind': self._current['kind']})
            operation_id = uuid.uuid4().hex
            self._started = self._monotonic()
            self._current = {'id': operation_id, 'kind': kind, 'status': 'running',
                             'startedAt': self._clock(), 'finishedAt': None,
                             'durationMs': 0, 'error': None, 'progress': None}
            return operation_id

    @contextmanager
    def reporting(self, operation_id):
        token = _reporter.set(lambda *args: self.progress(operation_id, *args))
        try:
            yield
        finally:
            _reporter.reset(token)

    def progress(self, operation_id, stage, label, completed=None, total=None, unit='files'):
        stages = ('check', 'packages', 'files', 'sync', 'launch')
        if stage not in stages or not isinstance(label, str):
            raise ValueError('Invalid progress stage')
        for value in (completed, total):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Invalid progress count')
        if total is not None and (completed is None or completed > total):
            raise ValueError('Invalid progress total')
        with self._lock:
            # Late callbacks can never modify the next operation.
            if self._current is None or self._current['id'] != operation_id:
                return
            self._current['progress'] = {'stage': stage, 'label': label,
                'step': stages.index(stage) + 1, 'steps': len(stages),
                'completed': completed, 'total': total, 'unit': unit}

    def finish(self, operation_id, error=None):
        with self._lock:
            if self._current is None or self._current['id'] != operation_id:
                raise LauncherError('操作状态与完成通知不一致。', code='invalid_operation_state',
                                    details={'operationId': operation_id})
            self._last = {**self._current,
                          'status': 'failed' if error is not None else 'succeeded',
                          'finishedAt': self._clock(),
                          'durationMs': max(0, round((self._monotonic() - self._started) * 1000)),
                          'error': deepcopy(error_payload(error, operation_id)) if error is not None else None}
            self._current = None
            self._started = None

    def snapshot(self):
        with self._lock:
            current = None if self._current is None else {
                **self._current,
                'durationMs': max(0, round((self._monotonic() - self._started) * 1000))}
            return deepcopy({'current': current, 'last': self._last})
