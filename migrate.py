"""Offline v2 -> v3 conversion; runtime never imports this module.

Verify a separate store, archive the entire source, then swap directories.
Live Harness files are read only. No loader replacement or compatibility facade.
"""
from __future__ import annotations
import argparse
import copy
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from adapters import PURGE
from domain import digest
from errors import PatchError, SchemaError, StorageError
from file_view import FileView
from launcher import DATA, VERSION, Manager, OFFICIAL, instance_lock
from patch_modes import tree_hash
from storage import write_json, hash_file, canonical
from switch_plan import SwitchPlan

REF = re.compile(r'(snapshots|deltas|packages|revisions|combinations|baselines|transactions)/[a-f0-9]{64}\.json')
RUNTIME_FILES = {'instance.json', 'instance.lock', 'launcher.log'}


@dataclass(frozen=True)
class PreparedMigration:
    report: dict
    verify_live: object
    require_stopped: object


@contextmanager
def upgrade_lock(data):
    """A stable sibling lock survives both directory renames.

    The server lock protects the original directory while staging. This lock
    also prevents a second migrator/recovery from removing the cutover marker.
    """
    path = Path(data).absolute().with_name(Path(data).name + '.upgrade.lock')
    for current in (path, *path.parents):
        if current.is_symlink() or current.is_junction():
            raise PatchError('迁移锁不能经过链接。')
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        owned = False
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                owned = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise PatchError('另一进程正在迁移或恢复此目录，请等待完成。') from exc
            yield
        finally:
            if owned:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def marker(data):
    data = Path(data).absolute()
    return data.with_name(data.name + '.upgrade.json')


def inventory(root):
    root = Path(root).absolute()
    for path in (root, *root.parents):
        if path.is_symlink() or path.is_junction():
            raise PatchError('迁移目录不能经过链接。')
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                raise PatchError('迁移数据包含链接：' + str(path))
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative not in RUNTIME_FILES:
                result[relative] = hash_file(path)
    return result


def recover_cutover(data):
    """Recover the directory boundary without overwriting an unexpected path."""
    with upgrade_lock(data):
        _recover_cutover(data)


def _recover_cutover(data):
    data = Path(data).absolute()
    journal = marker(data)
    if not journal.exists():
        return
    try:
        value = read(journal)
        if not isinstance(value, dict) or type(value.get('format')) is not int or value['format'] != 1:
            raise ValueError('Invalid migration envelope')
        archive, stage = Path(value['archive']), Path(value['stage'])
        if (value.get('data') != str(data) or archive.parent != data.parent or stage.parent != data.parent
                or not re.fullmatch(re.escape(data.name) + r'\.archive-[a-zA-Z0-9-]+', archive.name)
                or not re.fullmatch(re.escape(data.name) + r'\.stage-[a-zA-Z0-9-]+', stage.name)):
            raise ValueError('Invalid migration paths')
        for key in ('old', 'new'):
            if not isinstance(value[key], dict) or 'settings.json' not in value[key]:
                raise ValueError('Incomplete directory inventory')
            for relative, content in value[key].items():
                if (not isinstance(relative, str) or Path(relative).is_absolute()
                        or '..' in Path(relative).parts or '\\' in relative or ':' in relative):
                    raise ValueError('Invalid inventory path')
                digest(content)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PatchError('迁移日志路径或内容无效，未执行恢复。') from exc

    present = tuple(path.exists() for path in (data, archive, stage))
    if present not in ((True, False, True), (False, True, True), (True, True, False)):
        raise PatchError('迁移目录组合不符合已记录的切换状态，请保留迁移日志。')
    for path, expected in ((archive, value['old']), (stage, value['new'])):
        if path.exists() and (not path.is_dir() or inventory(path) != expected):
            raise PatchError('迁移中断后目录又发生变化，请保留迁移日志。')
    if data.exists():
        expected = value['new'] if archive.exists() else value['old']
        if not data.is_dir() or inventory(data) != expected:
            raise PatchError('迁移中断后目录又发生变化，请保留迁移日志。')
    else:
        os.replace(archive, data)
    if archive.exists():
        proof = archive.with_name(archive.name + '.manifest.json')
        if proof.exists() and read(proof) != value['old']:
            raise PatchError('旧数据归档清单与已核验内容不同，请保留迁移日志。')
        if not proof.exists():
            write_json(proof, value['old'])
    journal.unlink()


def rewrite_graph(store, value, replacements, visited=None):
    visited = set() if visited is None else visited
    if isinstance(value, str) and REF.fullmatch(value):
        if value in replacements:
            return replacements[value]
        if value in visited:
            raise PatchError('版本引用包含循环。')
        visited.add(value)
        content = rewrite_graph(store, store.read_object(value), replacements, visited)
        visited.remove(value)
        ref = store.object(value.split('/')[0], content)
        replacements[value] = ref
        return ref
    if isinstance(value, dict):
        return {k: rewrite_graph(store, v, replacements, visited) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_graph(store, v, replacements, visited) for v in value]
    return value


def verify_graph(store, values, source_views=()):
    refs, blobs = set(), set()
    sources = None
    def blob(key):
        nonlocal sources
        if key not in blobs:
            try:
                store.blob_bytes(key)
            except StorageError as exc:
                # Older adapter combinations fingerprinted their parked trees
                # without necessarily storing each byte in the generic store.
                # Reconstruct only missing blobs whose exact expected identity
                # is proved by the already verified read-only filesystem views.
                if not isinstance(exc.__cause__, FileNotFoundError):
                    raise
                if sources is None:
                    sources = {}
                    for view in source_views:
                        for path, entry in view.entries.items():
                            if entry['type'] == 'file':
                                sources.setdefault(view.hash(path), entry['source'])
                source = sources.get(key)
                if source is None:
                    raise PatchError('历史内容快照缺失且没有可核验的原文件：' + key) from exc
                info = source.stat()
                if store.store_file(source, [info.st_size, info.st_mtime_ns, info.st_ctime_ns]) != key:
                    raise PatchError('补全历史内容时文件改变，未迁移。')
                store.blob_bytes(key)
            blobs.add(key)
    def visit(value):
        if isinstance(value, str) and REF.fullmatch(value):
            if value not in refs:
                refs.add(value)
                visit(store.read_object(value))
        elif isinstance(value, dict):
            if value.get('type') == 'file':
                blob(value['hash'])
            if value.get('adapter') and isinstance(value.get('patch'), dict) and value['patch'].get('format') != 3:
                raise PatchError('历史适配器版本缺少内容证据，不能丢弃其旧记录。')
            # manifestBefore is a baselines/ reference; the ordinary recursive
            # walk verifies it just like other immutable object references.
            for key in value.get('installMetadata', {}).values():
                blob(key)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(values)
    return refs, blobs


def convert(stage, old_settings, tell):
    settings = dict(old_settings, format=3, requiredFeatures=[])
    write_json(stage / 'settings.json', settings)
    manager = Manager(stage)
    manager.require_stopped()
    manager.require_executable()
    tracker, core = manager.tracking, manager.tracking.core
    registries = list((stage / 'patches').glob('*/tracking/registry.json'))
    states = list((stage / 'patches').glob('*/state.json'))
    if any(p != tracker.index for p in registries) or any(p != core.record for p in states):
        raise PatchError('存在其他安装的记录，请分别指定其设置迁移，未丢弃历史。')
    old = read(tracker.index) if tracker.index.exists() else {'format': 2, 'records': {}, 'order': [], 'session': None}
    state = read(core.record) if core.record.exists() else None
    if old.get('format') != 2 or state and state.get('format') != 2:
        raise SchemaError('转换器只接收完整 v2 数据；更早的数据须先恢复并导出为 v2。')
    if (old.get('session') or old.get('pending') or old.get('inSwitch') or (state and state.get('pending'))
            or (tracker.root / 'switch.json').exists()):
        raise PatchError('存在未完成操作，请先在原版本完成恢复。')
    old.update(format=3)
    old.pop('pending', None)
    old.pop('inSwitch', None)
    tracker.save(old)
    # All planning branches are read-only. Share content reads within this
    # conversion, then independently rehash the live tree before cutover.
    planner = SwitchPlan(tracker, hashes={})
    tell('枚举受管文件与补丁暂存区…')
    live_view = FileView(planner.roots(), planner.hashes)
    tell(f'核验 {len(live_view.entries)} 项文件内容…')
    initial = planner._signature(live_view)
    tell('核验插件版本与依赖…')
    generic_base = copy.deepcopy(live_view)
    for name in reversed(old['order']):
        record = old['records'][name]
        tracker.versions.verify_record(name, record)
        tracker.versions.verify_package(name, record['package'])
        if record['enabled']:
            changes = tracker.read_object(record['delta'])['changes']
            planner._generic(generic_base, tracker.operations(changes, False), [], name)
    replacements = {}
    if state:
        tell('核验旧适配器证据并建立内容身份…')
        tracker.versions.verify_adapter(state, PURGE.plugin)
        original_revision = state['revision']
        app_at = core.app if generic_base.exists(core.app) else core.stash / 'app'
        key, content = ('treeContentHash', True) if state.get('treeContentHash') else ('treeHash', False)
        if tree_hash(app_at, generic_base, content=content) != state[key]:
            raise PatchError('旧补丁目录证据不匹配，不能迁移。')
        state['treeContentHash'] = tree_hash(app_at, generic_base, content=True)
        for item in state['directories']:
            at = Path(item['active']) if generic_base.exists(Path(item['active'])) else Path(item['parked'])
            key, content = ('contentHash', True) if item.get('contentHash') else ('hash', False)
            if tree_hash(at, generic_base, content=content) != item[key]:
                raise PatchError('旧补丁目录证据不匹配。')
            item['contentHash'] = tree_hash(at, generic_base, content=True)
        state.update(format=3)
        state.pop('pending', None)
        revision = tracker.read_object(original_revision)
        revision['patch'] = tracker.versions.adapter_patch(state)
        state['revision'] = tracker.store.object('revisions', revision)
        replacements[original_revision] = state['revision']
        core.save(state)
    new = rewrite_graph(tracker.store, old, replacements)
    tracker.save(new)
    settings = rewrite_graph(tracker.store, settings, replacements)
    manager.settings = settings
    manager.persist()
    backups = []
    for path in (stage / 'backups').glob('*.json'):
        value = read(path)
        if value.get('format') != 2 or not value.get('lock'):
            raise PatchError('备份缺少 v2 版本锁，未迁移：' + path.name)
        value = rewrite_graph(tracker.store, dict(value, format=3), replacements)
        write_json(path, value)
        backups.append(value)
    tell('用新版规划器预检当前、原生和所有已保存组合…')
    raw, obj, bundles = manager.manifest()
    plugins = manager.discover(obj, bundles)
    current = [name for name in bundles if name not in OFFICIAL]
    checked = []
    for selected in (current, [], [p['id'] for p in plugins], *settings['schemes'].values()):
        if selected in checked:
            continue
        target = copy.deepcopy(obj)
        target['dsh']['profile']['bundles'] = [*OFFICIAL, *selected]
        tell('预检组合：' + ('、'.join(selected) if selected else '原生模式'))
        planner.prepare(selected, plugins, raw, canonical(target), not selected)
        checked.append(selected)
    tracker.guard(check_files=True)
    if planner._signature(FileView(planner.roots())) != initial:
        raise PatchError('预检期间实际文件改变，未迁移。')
    refs, blobs = verify_graph(tracker.store, [new, settings, backups, state], (live_view, generic_base))
    core_blobs = set()
    for ref in refs:
        value = tracker.read_object(ref)
        if ref.startswith('combinations/') and value.get('manifestHash'):
            core.bytes(value['manifestHash'])
            core_blobs.add(value['manifestHash'])
        patch = value.get('patch') if isinstance(value, dict) else None
        if patch:
            for item in patch.get('files', []):
                for key in ('initial', 'native', 'plugin'):
                    if item.get(key):
                        core.bytes(item[key])
                        core_blobs.add(item[key])
    # Prune only the private stage. The complete old store remains archived.
    for path in tracker.root.glob('*/*.json'):
        if path.relative_to(tracker.root).as_posix() not in refs:
            path.unlink()
    for path in (tracker.root / 'blobs').glob('*'):
        if path.name not in blobs:
            path.unlink()
    for path in (core.root / 'blobs').glob('*'):
        if path.name not in core_blobs:
            path.unlink()
    def verify_live():
        manager.require_stopped()
        manager.require_executable()
        for name, record in new['records'].items():
            tracker.versions.verify_package(name, record['package'])
        if state:
            tracker.versions.verify_adapter(state, PURGE.plugin)
        if planner._signature(FileView(planner.roots())) != initial:
            raise PatchError('目录切换前实际文件改变，未迁移。')
        manager.require_stopped()
    report = {'version': VERSION, 'checkedCombinations': checked, 'plugins': {p['id']: p['version'] for p in plugins},
              'objects': len(refs), 'blobs': len(blobs) + len(core_blobs), 'adapterMode': state['mode'] if state else None}
    return PreparedMigration(report, verify_live, manager.require_stopped)


def migrate(data=DATA, apply=False, progress=None):
    data = Path(data).absolute()
    if not (data / 'settings.json').is_file():
        raise PatchError('缺少 settings.json，未创建或修改源目录。')
    with upgrade_lock(data):
        return _migrate(data, apply, progress)


def _migrate(data, apply, progress):
    data = Path(data).absolute()
    tell = progress or (lambda message: None)
    if not (data / 'settings.json').is_file():
        raise PatchError('缺少 settings.json，未创建或修改源目录。')
    if marker(data).exists():
        raise PatchError('有未完成的目录切换，请先运行 --recover。')
    for path in (data, *data.parents):
        if path.is_symlink() or path.is_junction():
            raise PatchError('迁移目录不能经过链接。')
    with instance_lock(data) as owned:
        if not owned or (data / 'instance.json').exists():
            raise PatchError('请先退出启动器后台再执行离线迁移。')
        if any((data / n).exists() for n in ('import-pending.json', 'migration-pending.json')):
            raise PatchError('旧导入尚未完成，请先使用原版本恢复。')
        before = inventory(data)
        settings_bytes = (data / 'settings.json').read_bytes()
        if hashlib.sha256(settings_bytes).hexdigest() != before.get('settings.json'):
            raise PatchError('读取期间源设置改变，未开始转换。')
        settings = json.loads(settings_bytes.decode('utf-8-sig'))
        if settings.get('format') == 3:
            manager = Manager(data)
            manager.require_stopped()
            tracker = manager.tracking
            tracker.verify_idle(check_files=True)
            registry = tracker.load()
            state = tracker.core.load()
            for name, record in registry['records'].items():
                tracker.versions.verify_record(name, record)
                tracker.versions.verify_package(name, record['package'])
            if state:
                tracker.versions.verify_adapter(state, PURGE.plugin)
                # Current combinations omit the parked adapter trees. Verify
                # those too, after removing generic layers in a read-only view.
                planner = SwitchPlan(tracker)
                base = FileView(planner.roots())
                for name in reversed(registry['order']):
                    record = registry['records'][name]
                    if record['enabled']:
                        changes = tracker.read_object(record['delta'])['changes']
                        planner._generic(base, tracker.operations(changes, False), [], name)
                planner._generic(base, tracker.operations(registry.get('supportDirectories', []), False), [], '组合目录')
                tracker.core.verify_files(state, base)
            recorded = tracker.recorded_state(registry)
            if recorded is not None:
                actual = FileView(tracker.scopes.values()).snapshot(tracker.scopes)
                if tracker.versions.content_state(recorded) != tracker.versions.content_state(actual):
                    raise PatchError('当前文件与已记录内容不同，只读检查未修改文件。')
            if inventory(data) != before:
                raise PatchError('检查期间源记录改变，未修改数据。')
            return {'status': 'already-current', 'version': VERSION}
        if 'format' in settings:
            raise SchemaError('不支持的设置版本，未修改源数据。')
        suffix = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        stage = data.with_name(data.name + '.stage-' + suffix)
        archive = data.with_name(data.name + '.archive-' + suffix)
        stage.mkdir()
        try:
            for folder in ('patches', 'backups'):
                if (data / folder).exists():
                    shutil.copytree(data / folder, stage / folder, symlinks=True)
            expected_copy = {key: value for key, value in before.items() if key.startswith(('patches/', 'backups/'))}
            if inventory(stage) != expected_copy:
                raise PatchError('复制期间源记录改变，未开始转换。')
            prepared = convert(stage, settings, tell)
            report = prepared.report
            if inventory(data) != before:
                raise PatchError('迁移期间源记录改变，未切换目录。')
            report.update(status='prepared', archive=str(archive), sourceFiles=len(before))
            if not apply:
                prepared.verify_live()
                return report
            write_json(stage / 'migration-report.json', dict(report, status='migrated'))
            after = inventory(stage)
            prepared.verify_live()
            write_json(marker(data), {'format': 1, 'data': str(data), 'stage': str(stage),
                       'archive': str(archive), 'old': before, 'new': after})
        except BaseException:
            if not marker(data).exists():
                shutil.rmtree(stage)
            raise
        finally:
            if not apply and stage.exists():
                shutil.rmtree(stage)
    # Windows cannot rename the directory while its instance lock is open.
    # The sibling journal blocks startup throughout this rename boundary.
    try:
        prepared.require_stopped()
        os.replace(data, archive)
        os.replace(stage, data)
        if inventory(data) != after or inventory(archive) != before:
            raise PatchError('切换后内容校验失败，请保留迁移日志。')
        write_json(archive.with_name(archive.name + '.manifest.json'), before)
        marker(data).unlink()
    except BaseException:
        _recover_cutover(data)
        raise
    report['status'] = 'migrated'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=DATA)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    if args.recover:
        recover_cutover(args.data)
    else:
        print(json.dumps(migrate(args.data, args.apply, lambda message: print(message, flush=True)),
                         ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
