"""Explicit one-time data conversion. The launcher itself reads only v2 data."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import uuid
import zipfile

from launcher import DATA, VERSION, Manager, MenuError, OFFICIAL
from file_view import FileView
from patch_modes import PatchError, file_hash, put, tree_hash
from switch_plan import SwitchPlan


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def import_data(source, destination, progress=None):
    """Copy a logical old store into one physical root, leaving it intact.

    A packaged Windows caller may see individual source files through MSIX
    virtualization without any symlink. All destination files must resolve
    under the selected new root. Content blobs use hardlinks when possible.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise PatchError('请选择独立的新数据目录。')
    source_manager = Manager(source)
    source_manager.require_stopped()
    if (source / 'instance.json').exists():
        raise PatchError('旧启动器服务仍在运行，请先退出。')
    def linked(path):
        return path.is_symlink() or path.is_junction()
    if linked(source) or linked(destination):
        raise PatchError('数据目录不能是链接。')
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    marker = destination / 'import-pending.json'
    if marker.exists():
        if linked(marker) or json.loads(marker.read_bytes()).get('source') != str(source):
            raise PatchError('目标有另一项未完成的导入，未覆盖。')
    elif any(destination.iterdir()):
        raise PatchError('目标数据目录不为空，请使用新目录或继续原导入。')
    paths = [source / 'settings.json'] if (source / 'settings.json').exists() else []
    if not paths:
        raise PatchError('旧数据目录没有 settings.json，未导入。')
    for folder in ('patches', 'backups', 'recoveries'):
        base = source / folder
        if base.exists():
            if linked(base):
                raise PatchError('旧数据包含链接，未导入：' + str(base))
            for directory, folders, files in os.walk(base, followlinks=False):
                for name in folders:
                    path = Path(directory) / name
                    if linked(path):
                        raise PatchError('旧数据包含链接，未导入：' + str(path))
                paths.extend(Path(directory) / name for name in files)
    # Validate the complete copy list before creating any imported files. MSIX
    # regular files can legitimately resolve outside the logical source root.
    for path in paths:
        if linked(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise PatchError('旧数据包含链接或特殊文件，未导入：' + str(path))
    def checked(relative):
        target = destination / relative
        for path in (target, *target.parents):
            if path == destination:
                break
            if linked(path):
                raise PatchError('导入目标包含链接，未覆盖：' + str(path))
        if not target.resolve().is_relative_to(root):
            raise PatchError('导入目标超出新数据目录。')
        return target
    def copy_file(path, temp):
        with path.open('rb') as source_stream, temp.open('wb') as stream:
            shutil.copyfileobj(source_stream, stream, 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(path, temp)
    put(checked(Path('import-pending.json')), encoded({'source': str(source)}))
    count = 0
    for path in paths:
        target = checked(path.relative_to(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_hash(path) != file_hash(target):
                raise PatchError('目标已有不同数据，未覆盖：' + str(target))
            continue
        temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            if path.parent.name == 'blobs' and re.fullmatch('[a-f0-9]{64}', path.name):
                try:
                    os.link(path.resolve(), temp)
                except OSError:
                    copy_file(path, temp)
            else:
                copy_file(path, temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        count += 1
    if progress:
        progress(f'已导入 {count} 个数据文件；原数据目录保留。')
    put(checked(Path('import-source.json')), encoded({'source': str(source), 'at': time.time()}))
    marker.unlink()


def recover_metadata(data):
    """A migration never changes live files; only known metadata is reverted."""
    journal = data / 'migration-pending.json'
    if not journal.exists():
        return
    value = json.loads(journal.read_bytes())
    if not value.get('committed'):
        for item in reversed(value['files']):
            path = data / item['path']
            if not path.resolve().is_relative_to(data.resolve()):
                raise PatchError('迁移恢复路径超出数据目录。')
            before = base64.b64decode(item['before']) if item['before'] is not None else None
            after = base64.b64decode(item['after'])
            actual = path.read_bytes() if path.exists() else None
            if actual == after:
                if before is None:
                    path.unlink()
                else:
                    put(path, before)
            elif actual != before:
                raise PatchError('迁移中断后元数据又被修改，请保留 migration-pending.json。')
    journal.unlink()


def migrate(manager, apply=False, progress=None):
    tell = progress or (lambda message: None)
    manager.require_stopped()
    manager.require_executable()
    if (manager.data / 'instance.json').exists():
        raise MenuError('请先关闭启动器服务，再运行数据迁移。')
    recover_metadata(manager.data)
    tracker = manager.tracking
    core = tracker.core
    original = {}
    def read(path):
        content = path.read_bytes()
        original[path] = content
        return json.loads(content)
    settings = read(manager.settings_file) if manager.settings_file.exists() else copy.deepcopy(manager.settings)
    old = read(tracker.index) if tracker.index.exists() else {'format': 2, 'records': {}, 'order': [], 'session': None}
    state = read(core.record) if core.record.exists() else None
    if old.get('session') or old.get('pending') or old.get('inSwitch') or (state and state.get('pending')) or (tracker.root / 'switch.json').exists():
        raise PatchError('存在未完成的安装或切换，请先在原版本完成恢复；本次未替换记录。')
    raw, obj, bundles = manager.manifest()
    plugins = manager.discover(obj, bundles)
    available = {p['id']: p for p in plugins}
    missing = set(available) - set(old['records']) - {'dsh-purge'}
    if missing:
        raise PatchError('没有安装前记录，不能通过迁移伪造原始文件：' + '、'.join(sorted(missing)))
    current = [name for name in bundles if name not in OFFICIAL]
    tell('检查记录格式与迁移状态…')
    newest = (old.get('format') == 2 and all(r.get('package') and r.get('revision') for r in old['records'].values())
              and (not state or state.get('format') == 2 and state.get('package') and state.get('revision'))
              and set(settings.get('schemeLocks', {})) == set(settings.get('schemes', {})))
    backups = [(path, read(path)) for path in sorted((manager.data / 'backups').glob('*.json'))]
    newest = newest and all(value.get('format') == 2 for _, value in backups)
    if newest:
        tracker.guard(check_files=True)
        tracker.check_versions(plugins, current)
        if state:
            tracker.versions.verify_adapter(state)
        return {'status': 'already-current', 'version': VERSION, 'plugins': {p['id']: p['version'] for p in plugins}}
    references = {}
    def convert(value):
        if isinstance(value, str) and re.fullmatch(r'(?:snapshots|deltas)/[a-f0-9]{32}\.json', value):
            if value not in references:
                source = json.loads((tracker.root / value).read_bytes())
                references[value] = tracker.object(value.split('/')[0], convert(source))
            return references[value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value
    new = convert(copy.deepcopy(old))
    new['format'] = 2
    new.pop('pending', None)
    new.pop('inSwitch', None)
    # Offline staging: immutable objects may be added, but normal load/save
    # continue to see the original on-disk index until the metadata commit.
    tracker.load = lambda: copy.deepcopy(new)
    staged_core = copy.deepcopy(state)
    core.load = lambda: copy.deepcopy(staged_core)
    if new.get('inventory') and tracker.read_object(new['inventory']) != tracker.metadata():
        raise PatchError('记录范围有未归属改动，不能直接迁移。')
    host = file_hash(Path(manager.settings['executable']))
    verified_blobs = set()
    for name in new['order']:
        tell('校验原始快照并留存插件版本：' + name)
        record = new['records'][name]
        if name not in available or available[name]['version'] != record['version']:
            raise PatchError('已安装插件版本与原始记录不同：' + name)
        if record.get('hostHash') and record['hostHash'] != host:
            raise PatchError('Harness 与原始记录的版本不同。')
        if not record.get('baseline'):
            raise PatchError('缺少安装前快照，不能把当前文件当成原始文件：' + name)
        for reference in ('baseline', 'after'):
            if record.get(reference):
                for entry in tracker.read_object(record[reference]).values():
                    if entry['type'] == 'file' and entry['hash'] not in verified_blobs:
                        tracker.blob_bytes(entry['hash'])
                        verified_blobs.add(entry['hash'])
        record.setdefault('source', 'installed:' + name + '@' + record['version'])
        record.setdefault('after', record['baseline'])
        record.setdefault('manifestBefore', None)
        record.setdefault('baseVersions', {})
        record['history'] = []  # Pre-migration history stays in the checkpoint.
        record['hostHash'] = host
        record['package'] = tracker.versions.package_snapshot(name, record['source'])
        parent = record.get('revision')
        record['revision'] = tracker.versions.revision(name, record, record['package'], record['source'], parent,
                                                       origin='one-time-migration')
    if staged_core:
        tell('核验内置补丁、官方归档与 SDK…')
        original_mode = staged_core['mode']
        at = core.app if core.app.exists() else core.stash / 'app'
        if tree_hash(at) != staged_core['treeHash']:
            raise PatchError('内置补丁目录有未知改动，不能迁移。')
        if core.official_archive() is None or file_hash(core.official_archive()) != staged_core['archiveHash']:
            raise PatchError('保留的官方归档不匹配，不能迁移。')
        native = core.archive.is_file() and not core.app.exists()
        plugin = core.app.is_dir() and (core.stash / 'official.asar').is_file()
        native = native and all(file_hash(Path(item['path'])) == item['native'] for item in staged_core['files'])
        plugin = plugin and all(file_hash(Path(item['path'])) == item['plugin'] for item in staged_core['files'])
        for item in staged_core['directories']:
            native = native and Path(item['parked']).is_dir() and not Path(item['active']).exists()
            plugin = plugin and Path(item['active']).is_dir() and not Path(item['parked']).exists()
        mode = 'native' if native else 'plugin' if plugin else 'captured' if original_mode in ('legacy', 'captured') else None
        if mode is None:
            raise PatchError('内置补丁文件处于混合状态，已停止迁移。')
        staged_core.update(format=2, mode=mode)
        staged_core.pop('pending', None)
        staged_core.pop('package', None)
        staged_core.pop('revision', None)
        staged_core = tracker.versions.bind_adapter(staged_core)
    manager.settings = copy.deepcopy(settings)
    # One read-only command may inspect several combinations of the same disk
    # inputs. Reuse reads there, then independently hash everything before commit.
    planner = SwitchPlan(tracker, hashes={})
    tell('预检当前组合…')
    plan = planner.prepare(current, plugins, raw, raw, not current)
    if staged_core is None:
        staged_core = plan['previousCore']
    # Register the combination only if the actual files already match it.
    # Otherwise the next ordinary launch performs the standard journaled switch.
    if not plan['operations']:
        new = plan['new']
        staged_core = plan['newCore']
    else:
        new['currentCombination'] = None
        new['nativeBaselines'] = plan['new']['nativeBaselines']
    new['inventory'] = tracker.object('snapshots', tracker.metadata())
    manager.settings['schemeLocks'] = {}
    for name, ids in manager.settings.get('schemes', {}).items():
        manager.validate_selection(ids, plugins)
        manager.settings['schemeLocks'][name] = tracker.versions.lock(ids, plugins, new, host)
    writes = {tracker.index: encoded(new), manager.settings_file: encoded(manager.settings)}
    if staged_core:
        writes[core.record] = encoded(staged_core)
    for path, value in backups:
        if value.get('target') != str(manager.manifest_path):
            continue
        ids = [name for name in json.loads(value['content'])['dsh']['profile']['bundles'] if name not in OFFICIAL]
        manager.validate_selection(ids, plugins)
        value.update(format=2, lock=tracker.versions.lock(ids, plugins, new, host),
                     versionBinding='installed-at-migration')
        writes[path] = encoded(value)
    # Exercise supported existing combinations through the same read-only planner.
    checked = []
    for ids in ([], current, [p['id'] for p in plugins], *[[p['id']] for p in plugins]):
        if ids in checked:
            continue
        target = copy.deepcopy(obj)
        tell('预检组合：' + ('、'.join(ids) or '原生模式'))
        target['dsh']['profile']['bundles'] = [*OFFICIAL, *ids]
        planner.prepare(ids, plugins, raw, encoded(target), not ids)
        checked.append(ids)
    report = {'status': 'prepared', 'version': VERSION, 'plugins': {p['id']: p['version'] for p in plugins},
              'enabled': current, 'convertedObjects': len(references), 'backups': len(backups),
              'checkedCombinations': checked, 'needsSwitch': bool(plan['operations']),
              'adapterMode': staged_core['mode'] if staged_core else None}
    if not apply:
        return report
    tell('复核输入文件并提交新版元数据…')
    manager.require_stopped()
    if manager.manifest_path.read_bytes() != raw or planner._signature(FileView(planner.roots())) != plan['expected']:
        raise PatchError('迁移预检之后实际文件又发生改动，未替换元数据。')
    for path, content in original.items():
        if path.read_bytes() != content:
            raise PatchError('迁移预检之后记录又发生改动，未替换元数据。')
    checkpoint = manager.data / 'migrations' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    checkpoint.mkdir(parents=True)
    with zipfile.ZipFile(checkpoint / 'metadata-before.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, content in original.items():
            archive.writestr(path.relative_to(manager.data).as_posix(), content)
        archive.writestr('profile-package.json', raw)
    files = []
    for path, content in writes.items():
        if not path.resolve().is_relative_to(manager.data.resolve()):
            raise PatchError('迁移目标超出启动器数据目录。')
        before = original.get(path)
        files.append({'path': path.relative_to(manager.data).as_posix(),
                      'before': base64.b64encode(before).decode() if before is not None else None,
                      'after': base64.b64encode(content).decode()})
    journal = manager.data / 'migration-pending.json'
    pending = {'format': 1, 'files': files, 'committed': False}
    put(journal, encoded(pending))
    try:
        for path, content in writes.items():
            put(path, content)
        pending['committed'] = True
        put(journal, encoded(pending))
        journal.unlink()
    except Exception:
        recover_metadata(manager.data)
        raise
    report.update(status='migrated', archive=str(checkpoint / 'metadata-before.zip'))
    put(checkpoint / 'report.json', encoded(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=DATA)
    parser.add_argument('--source', type=Path, help='old data directory to copy; the source is retained')
    parser.add_argument('--apply', action='store_true', help='commit the prepared metadata migration')
    args = parser.parse_args()
    try:
        source = args.source
        marker = args.data / 'import-pending.json'
        if source is None and marker.exists():
            source = Path(json.loads(marker.read_bytes())['source'])
        old = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'HarnessLauncher'
        if source is None and not (args.data / 'settings.json').exists() and (old / 'settings.json').exists():
            source = old
        tell = lambda message: print(message, file=sys.stderr, flush=True)
        if source is not None and args.apply:
            import_data(source, args.data, tell)
        manager = Manager(source if source is not None and not args.apply else args.data)
        print(json.dumps(migrate(manager, apply=args.apply,
            progress=lambda message: print(message, file=sys.stderr, flush=True)), ensure_ascii=False, indent=2))
    except (OSError, ValueError, PatchError, MenuError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
