"""Stable application error contract, independent of HTTP and persistence."""
from __future__ import annotations


class LauncherError(Exception):
    code = 'operation_rejected'
    recovery = '检查错误详情，修复后重试。'
    retryable = False

    def __init__(self, message, *, code=None, details=None, recovery=None, retryable=None):
        super().__init__(message)
        self.code = code or type(self).code
        self.details = details or {}
        self.recovery = recovery if recovery is not None else type(self).recovery
        self.retryable = retryable if retryable is not None else type(self).retryable


class PatchError(LauncherError):
    code = 'unsafe_file_state'
    recovery = '保留记录，检查文件变化；有未完成操作时先恢复。'


class StorageError(PatchError):
    code = 'storage_integrity'
    recovery = '停止修改数据；从已核验的备份恢复缺失或损坏的对象。'


class SchemaError(LauncherError):
    code = 'unsupported_schema'
    recovery = '保留原数据；选择当前格式的数据目录或从有效备份恢复。'


def error_payload(error, operation_id=None):
    """The API has one shape; messages are not machine-readable error codes."""
    if isinstance(error, LauncherError):
        return dict(error=str(error), code=error.code, details=error.details,
                    recovery=error.recovery, retryable=error.retryable, operationId=operation_id)
    return dict(error=str(error), code='request_failed', details={},
                recovery='检查请求或查看本机日志后重试。', retryable=False, operationId=operation_id)
