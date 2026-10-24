"""Installation recording state machine, independent of IO and UI.

Every transition produces a validated immutable session. The persisted runState
is the single source of truth; contradictory observed/closed booleans cannot be
represented. Errors pause a state without destroying its recovery evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import math
from typing import Mapping

from domain import DomainError, PLUGIN_NAME, digest, natural, object_reference


UPDATE_GUARDS = 'recording-update-guards'
HOST_ARCHIVES = frozenset(('archive', 'archive_backup', 'parked_archive'))


class RecordingPhase(StrEnum):
    PREPARING = 'preparing'
    INSTALLING = 'installing'
    RECORDING = 'recording'
    SCANNING = 'scanning'
    VERIFYING = 'verifying'


class RunState(StrEnum):
    IDLE = 'idle'
    LAUNCHING = 'launching'
    RUNNING = 'running'
    CLOSED = 'closed'


class RecordingEvent(StrEnum):
    BASELINE_READY = 'baseline_ready'
    INSTALL = 'install'
    INSTALLED = 'installed'
    LAUNCH = 'launch'
    OBSERVE_RUNNING = 'observe_running'
    OBSERVE_CLOSED = 'observe_closed'
    SCAN = 'scan'
    VERIFY = 'verify'
    FAIL = 'fail'
    RETRY = 'retry'


def _name(value):
    if not isinstance(value, str) or not PLUGIN_NAME.fullmatch(value):
        raise DomainError('插件名称无效。')
    return value


def _names(value):
    if not isinstance(value, (list, tuple)):
        raise DomainError('插件组合必须是列表。')
    result = tuple(_name(item) for item in value)
    if len(result) != len(set(result)):
        raise DomainError('插件组合不能有重复项。')
    return result


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise DomainError('记录时间无效。')
    return value


@dataclass(frozen=True)
class RecordingSession:
    id: str
    plugin: str
    source: str
    phase: RecordingPhase
    started: float
    host_hash: str
    initial_inventory: str
    base_selection: tuple[str, ...] = ()
    versions: dict[str, str] = field(default_factory=dict)
    base_versions: dict[str, str] = field(default_factory=dict)
    baseline: str | None = None
    manifest_before: str | None = None
    installed: bool = False
    run_state: RunState = RunState.IDLE
    launch_at: float | None = None
    error: str = ''
    after: str | None = None
    delta: str | None = None
    version: str | None = None
    count: int | None = None
    host_archives: dict[str, str | None] | None = None
    package_pins: dict[str, str] | None = None

    FIELDS = {'id': 'id', 'plugin': 'plugin', 'source': 'source', 'phase': 'phase',
              'started': 'started', 'hostHash': 'host_hash', 'initialInventory': 'initial_inventory',
              'baseSelection': 'base_selection', 'versions': 'versions', 'baseVersions': 'base_versions',
              'baseline': 'baseline', 'manifestBefore': 'manifest_before', 'installed': 'installed',
              'runState': 'run_state', 'launchAt': 'launch_at', 'error': 'error', 'after': 'after',
              'delta': 'delta', 'version': 'version', 'count': 'count',
              'hostArchives': 'host_archives', 'packagePins': 'package_pins'}

    @classmethod
    def decode(cls, value):
        if not isinstance(value, Mapping) or set(value) - cls.FIELDS.keys():
            raise DomainError('安装会话含未知或旧版字段。')
        try:
            kwargs = {field_name: value[key] for key, field_name in cls.FIELDS.items() if key in value}
            kwargs['phase'] = RecordingPhase(value['phase'])
            kwargs['run_state'] = RunState(value['runState'])
            kwargs['base_selection'] = _names(value['baseSelection'])
            result = cls(**kwargs)
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError('安装会话结构无效。') from exc
        return result.validate()

    def validate(self):
        if not isinstance(self.phase, RecordingPhase) or not isinstance(self.run_state, RunState):
            raise DomainError('安装阶段和运行状态必须使用已定义的枚举。')
        if not isinstance(self.id, str) or len(self.id) != 32 or any(c not in '0123456789abcdef' for c in self.id):
            raise DomainError('安装会话编号无效。')
        _name(self.plugin)
        _names(self.base_selection)
        _time(self.started)
        digest(self.host_hash)
        if (self.host_archives is None) != (self.package_pins is None):
            raise DomainError('安装会话的宿主和依赖更新保护必须成套记录。')
        if self.host_archives is not None:
            if not isinstance(self.host_archives, dict) or set(self.host_archives) != HOST_ARCHIVES:
                raise DomainError('安装会话的宿主归档记录无效。')
            for signature in self.host_archives.values():
                if signature is not None:
                    digest(signature)
            if not isinstance(self.package_pins, dict) or self.plugin in self.package_pins:
                raise DomainError('安装会话的基础插件记录无效。')
            for name, reference in self.package_pins.items():
                _name(name)
                object_reference(reference, 'packages')
            if not isinstance(self.versions, dict) or set(self.package_pins) != set(self.versions) - {self.plugin}:
                raise DomainError('安装会话缺少其他插件的安装前内容记录。')
        object_reference(self.initial_inventory, 'snapshots')
        if not isinstance(self.source, str) or not isinstance(self.error, str) or type(self.installed) is not bool:
            raise DomainError('安装会话来源、错误或安装标识无效。')
        if len(self.source) > 2048 or self.source.startswith('-') or any(c in self.source for c in ('\r', '\n', '\x00')):
            raise DomainError('安装会话来源格式无效。')
        if self.installed and not self.source:
            raise DomainError('手动安装不能标记为已完成自动安装器。')
        for values, revisions in ((self.versions, False), (self.base_versions, True)):
            if not isinstance(values, dict):
                raise DomainError('插件版本映射无效。')
            for name, version in values.items():
                _name(name)
                if revisions:
                    object_reference(version, 'revisions')
                elif not isinstance(version, str):
                    raise DomainError('插件版本必须是字符串。')
        for ref, kind in ((self.baseline, 'snapshots'), (self.manifest_before, 'baselines'),
                          (self.after, 'snapshots'), (self.delta, 'deltas')):
            if ref is not None:
                object_reference(ref, kind)
        if self.phase is RecordingPhase.PREPARING:
            if (self.baseline is not None or self.manifest_before is not None or self.installed
                    or self.run_state is not RunState.IDLE):
                raise DomainError('准备中的会话不能包含完整快照或运行记录。')
        elif self.baseline is None or self.manifest_before is None:
            raise DomainError('安装后续阶段必须有完整原始快照及清单。')
        if self.phase is RecordingPhase.INSTALLING:
            if not self.source or self.installed or self.run_state is not RunState.IDLE:
                raise DomainError('安装器阶段状态不一致。')
        if self.run_state is not RunState.IDLE:
            if self.phase in (RecordingPhase.PREPARING, RecordingPhase.INSTALLING) or self.launch_at is None:
                raise DomainError('运行记录缺少启动意图或阶段不允许运行。')
        elif self.launch_at is not None:
            raise DomainError('尚未启动的会话不能携带启动时间。')
        if self.source and self.phase in (RecordingPhase.RECORDING, RecordingPhase.SCANNING, RecordingPhase.VERIFYING) and not self.installed:
            raise DomainError('自动安装器成功后才能进入首次运行记录。')
        if self.launch_at is not None:
            _time(self.launch_at)
        if self.phase in (RecordingPhase.SCANNING, RecordingPhase.VERIFYING) and self.run_state is not RunState.CLOSED:
            raise DomainError('扫描和校验必须在首次运行结束后执行。')
        if self.phase is RecordingPhase.VERIFYING:
            if self.after is None or self.delta is None or not isinstance(self.version, str) or self.count is None:
                raise DomainError('校验阶段缺少完整差异证据。')
        elif any(item is not None for item in (self.after, self.delta, self.version, self.count)):
            raise DomainError('非校验阶段不能携带已完成扫描的结果。')
        if self.count is not None:
            natural(self.count, '变更数量')
        return self

    def to_dict(self):
        result = {}
        for key, name in self.FIELDS.items():
            value = getattr(self, name)
            if isinstance(value, StrEnum):
                value = value.value
            elif isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            result[key] = value
        return result

    def transition(self, event, **data):
        """Reject invalid transitions before any persisted state is changed."""
        self.validate()
        event = RecordingEvent(event)
        if self.error and event not in (RecordingEvent.FAIL, RecordingEvent.RETRY):
            raise DomainError('异常安装会话必须先明确继续。')
        next_state = self
        if event is RecordingEvent.FAIL:
            if not isinstance(data.get('error'), str) or not data['error']:
                raise DomainError('暂停会话必须说明错误。')
            next_state = replace(self, error=data['error'])
        elif event is RecordingEvent.RETRY:
            if not self.error:
                raise DomainError('正常会话不能执行异常重试。')
            if self.run_state is RunState.CLOSED:
                next_state = replace(self, error='')
            else:
                phase = (RecordingPhase.PREPARING if self.baseline is None else
                         RecordingPhase.INSTALLING if self.source and not self.installed else RecordingPhase.RECORDING)
                next_state = replace(self, phase=phase, run_state=RunState.IDLE, launch_at=None, error='')
        elif event is RecordingEvent.BASELINE_READY:
            self._require(self.phase is RecordingPhase.PREPARING)
            next_state = replace(self, baseline=data['baseline'], manifest_before=data['manifest_before'],
                                 phase=RecordingPhase.INSTALLING if self.source else RecordingPhase.RECORDING)
        elif event is RecordingEvent.INSTALL:
            self._require(self.phase is RecordingPhase.INSTALLING and not self.installed)
        elif event is RecordingEvent.INSTALLED:
            self._require(self.phase is RecordingPhase.INSTALLING)
            next_state = replace(self, installed=True, phase=RecordingPhase.RECORDING)
        elif event is RecordingEvent.LAUNCH:
            self._require(self.phase is RecordingPhase.RECORDING and self.run_state is RunState.IDLE)
            next_state = replace(self, run_state=RunState.LAUNCHING, launch_at=data['launch_at'])
        elif event is RecordingEvent.OBSERVE_RUNNING:
            self._require(self.phase is RecordingPhase.RECORDING and self.run_state is RunState.LAUNCHING)
            next_state = replace(self, run_state=RunState.RUNNING)
        elif event is RecordingEvent.OBSERVE_CLOSED:
            self._require(self.phase is RecordingPhase.RECORDING and self.run_state is RunState.RUNNING)
            next_state = replace(self, run_state=RunState.CLOSED)
        elif event is RecordingEvent.SCAN:
            self._require(self.run_state is RunState.CLOSED and self.phase in
                          (RecordingPhase.RECORDING, RecordingPhase.SCANNING, RecordingPhase.VERIFYING))
            next_state = replace(self, phase=RecordingPhase.SCANNING, after=None, delta=None, version=None, count=None)
        elif event is RecordingEvent.VERIFY:
            self._require(self.phase is RecordingPhase.SCANNING)
            next_state = replace(self, phase=RecordingPhase.VERIFYING, after=data['after'], delta=data['delta'],
                                 version=data['version'], count=data['count'])
        return next_state.validate()

    @staticmethod
    def _require(condition):
        if not condition:
            raise DomainError('安装会话状态不允许此次转换。')


def validate_registry(value):
    if not isinstance(value, dict) or type(value.get('format')) is not int or value['format'] != 3:
        raise DomainError('仅支持当前格式 3；旧数据需要离线导入。')
    features = value.get('requiredFeatures', [])
    if (not isinstance(features, list) or any(item not in (UPDATE_GUARDS, 'maintenance-transaction') for item in features)
            or len(features) != len(set(features))):
        raise DomainError('登记表需要当前程序不支持的功能。')
    records, order = value.get('records'), value.get('order')
    if not isinstance(records, dict) or not isinstance(order, list):
        raise DomainError('插件登记表结构无效。')
    names = _names(order)
    if set(names) != set(records):
        raise DomainError('插件安装顺序与登记表不一致。')
    if 'pending' in value or 'inSwitch' in value:
        raise DomainError('登记表中包含旧版未完成操作。')
    for name, record in records.items():
        _name(name)
        if not isinstance(record, dict):
            raise DomainError('插件记录必须是对象。')
        object_reference(record.get('revision'), 'revisions')
        object_reference(record.get('package'), 'packages')
        if type(record.get('enabled')) is not bool or not isinstance(record.get('version'), str):
            raise DomainError('插件启用状态或版本无效。')
        natural(record.get('count'), '变更数量')
        _names(record.get('baseSelection'))
        histories = record.get('history', [])
        if not isinstance(histories, list):
            raise DomainError('插件历史版本必须是列表。')
        for history in histories:
            if not isinstance(history, dict):
                raise DomainError('插件历史版本无效。')
            object_reference(history.get('revision'), 'revisions')
            object_reference(history.get('package'), 'packages')
    if 'session' not in value:
        raise DomainError('登记表缺少会话状态。')
    if value['session'] is not None:
        session = RecordingSession.decode(value['session'])
        if (UPDATE_GUARDS in features) != (session.host_archives is not None):
            raise DomainError('安装会话的更新保护能力声明不匹配。')
    elif UPDATE_GUARDS in features:
        raise DomainError('没有安装会话时不能保留临时更新保护能力声明。')
    for key, kind in (('inventory', 'snapshots'), ('currentCombination', 'combinations')):
        if value.get(key) is not None:
            object_reference(value[key], kind)
    if 'combinations' in value:
        if not isinstance(value['combinations'], list):
            raise DomainError('组合列表无效。')
        for reference in value['combinations']:
            object_reference(reference, 'combinations')
    return value
