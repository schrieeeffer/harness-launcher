"""Conservative mark/sweep for this installation's content stores only."""
import hashlib
import json
import re
import time

from domain import OBJECT_KINDS
from errors import LauncherError
from operations import report_progress
from storage import canonical


REF = re.compile(r'^(?:' + '|'.join(OBJECT_KINDS) + r')/[a-f0-9]{64}\.json$')
HASH = re.compile(r'^[a-f0-9]{64}$')


class CacheGC:
    def __init__(self, manager):
        self.manager = manager

    def preview(self, grace=3600):
        manager, tracker = self.manager, self.manager.tracking
        manager.maintenance.require_idle()
        tracker.verify_idle()
        stores = {'tracking': tracker.store, 'adapter': tracker.core.store}
        roots = [manager.settings, tracker.load(), tracker.core.load(), manager.maintenance.load()]
        # Backups and every saved recovery point can refer to old combinations.
        for folder in (manager.data / 'backups', manager.maintenance.folder / 'history'):
            for path in folder.glob('*.json'):
                if path.is_symlink() or path.is_junction():
                    raise LauncherError('恢复索引包含链接，已停止清理。')
                roots.append(json.loads(path.read_bytes()))
        reachable = {name: set() for name in stores}
        seen = set()
        seen_values = set()
        report_progress('files', '正在分析恢复对象引用（保留方案、备份与历史）')
        def visit(value):
            # History repeatedly references the same immutable object and hash.
            # Resolve each reference once in this locked walk, rather than doing
            # filesystem probes for every occurrence in old transaction plans.
            if isinstance(value, str) and (REF.fullmatch(value) or HASH.fullmatch(value)):
                if value in seen_values:
                    return
                seen_values.add(value)
            if isinstance(value, dict):
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, str) and REF.fullmatch(value):
                # Revisions are shared through the tracking store; checking
                # both also supports existing adapter/blob references.
                found = False
                for name, store in stores.items():
                    if store._path(value).is_file():
                        found = True
                        reachable[name].add(value)
                        if (name, value) not in seen:
                            seen.add((name, value))
                            report_progress('files', '正在分析恢复对象引用', completed=len(seen), unit='objects')
                            visit(store.read_object(value))
                if not found:
                    raise LauncherError('仍被引用的恢复对象缺失，已停止清理：' + value)
            elif isinstance(value, str) and HASH.fullmatch(value):
                for name, store in stores.items():
                    ref = 'blobs/' + value
                    if store._path(ref).is_file():
                        reachable[name].add(ref)
        for root in roots:
            visit(root)
        cutoff, files = time.time() - grace, []
        report_progress('files', '正在筛选超过宽限期的无引用对象')
        for name, store in stores.items():
            for kind in sorted(OBJECT_KINDS | {'blobs'}):
                for path in store._path(kind).glob('*'):
                    ref = kind + '/' + path.name
                    checked = store._path(ref)
                    if ref in reachable[name] or not checked.is_file():
                        continue
                    valid = HASH.fullmatch(path.name) if kind == 'blobs' else REF.fullmatch(ref)
                    if not valid or checked.stat().st_mtime > cutoff:
                        continue
                    files.append({'store': name, 'path': ref, 'bytes': checked.stat().st_size})
        files.sort(key=lambda item: (item['store'], item['path']))
        token = hashlib.sha256(canonical({'roots': roots, 'files': files})).hexdigest()
        return {'token': token, 'files': files, 'count': len(files), 'bytes': sum(f['bytes'] for f in files),
                'message': '只回收无引用且已超过 1 小时的内容对象；恢复点、方案、共享依赖和用户数据保留。'}

    def collect(self, token):
        with self.manager.lock:
            preview = self.preview()
            if token != preview['token']:
                raise LauncherError('清理预览已变化，请重新预览。', code='revision_conflict')
            stores = {'tracking': self.manager.tracking.store, 'adapter': self.manager.tracking.core.store}
            count = size = 0
            for item in preview['files']:
                store, ref = stores[item['store']], item['path']
                # A corrupt orphan is retained for investigation, never reported as reclaimed.
                if ref.startswith('blobs/'):
                    store.blob_bytes(ref.split('/')[1])
                else:
                    store.read_object(ref)
                store._path(ref).unlink()
                count += 1
                size += item['bytes']
            return {'message': f'已清理 {count} 个无引用对象，共 {size} 字节。', 'count': count, 'bytes': size}
