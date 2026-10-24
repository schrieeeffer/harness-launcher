"""Versioned launcher settings; old formats never enter the runtime."""
import json
from pathlib import Path
from errors import SchemaError
from domain import PLUGIN_NAME

FORMAT = 3


def validate_settings(value):
    if not isinstance(value, dict) or type(value.get('format')) is not int or value['format'] != FORMAT:
        raise SchemaError('启动器数据格式不受支持；请保留原数据并选择当前格式的数据目录。')
    features = value.get('requiredFeatures', [])
    if not isinstance(features, list) or features:
        raise SchemaError('此数据需要当前程序不支持的功能。')
    for name in ('home', 'executable'):
        if not isinstance(value.get(name), str) or not value[name] or '\x00' in value[name]:
            raise SchemaError('设置路径无效：' + name)
    def selection(items, nullable=False):
        if nullable and items is None:
            return
        if (not isinstance(items, list) or any(not isinstance(x, str) or not PLUGIN_NAME.fullmatch(x) for x in items)
                or len(set(items)) != len(items)):
            raise SchemaError('设置中的插件列表无效。')
    selection(value.get('order', []))
    selection(value.get('lastSelection'), True)
    schemes, locks = value.get('schemes'), value.get('schemeLocks')
    if not isinstance(schemes, dict) or not isinstance(locks, dict) or set(schemes) != set(locks) or len(schemes) > 100:
        raise SchemaError('方案与版本锁不一致。')
    for name, selected in schemes.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 40:
            raise SchemaError('方案名称无效。')
        selection(selected)
        if not isinstance(locks[name], dict):
            raise SchemaError('方案版本锁无效。')
    return value


def load_settings(path, defaults):
    path = Path(path)
    if path.exists():
        try:
            return validate_settings(json.loads(path.read_text(encoding='utf-8-sig')))
        except (ValueError, OSError) as exc:
            raise SchemaError('设置文件损坏或不可读取，请从备份恢复。') from exc
    # An orphaned store must never be silently adopted under new settings.
    if any((path.parent / name).exists() for name in ('patches', 'backups')):
        raise SchemaError('已有记录但缺少 settings.json，请从有效备份恢复设置。')
    return validate_settings(dict(defaults, format=FORMAT, requiredFeatures=[]))
