"""Recognize regenerated Python caches without exempting executable bytecode.

Only source-backed __pycache__ files can differ from the official package.
Their code is compared with a fresh compilation of verified official source,
inside the verified bundled interpreter with a new, empty cache prefix. Neither
the candidate bytecode nor the source module is executed. Successful results
are content-bound, so later checks reuse them until bytes change.
"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from file_view import FileView
from storage import canonical, hash_file, write_json
import runtime_repair as repair

PYTHON_ROOT = 'resources/runtime/primary-runtime/dependencies/python/'


def source_cache(name, files):
    path = PurePosixPath(name)
    if not name.startswith(PYTHON_ROOT) or path.parent.name != '__pycache__':
        return None
    match = re.fullmatch(r'(.+)\.(cpython-\d+)(?:\.opt-([12]))?\.pyc', path.name)
    if not match:
        return None
    source = str(path.parent.parent / (match[1] + '.py'))
    return (source, match[2], int(match[3] or 0)) if source in files else None


def python_extras(manager, base, files, *, view=None):
    if PYTHON_ROOT + 'python.exe' not in files:
        return []
    if view is None:
        view = FileView([repair.safe_path(Path(base) / PYTHON_ROOT)], verification_cache=manager.verification_cache)
    return [path.relative_to(base).as_posix() for path, entry in view.entries.items()
            if entry['type'] in ('file', 'link') and path.relative_to(base).as_posix().startswith(PYTHON_ROOT)
            and path.relative_to(base).as_posix() not in files]


# The helper imports only verified stdlib, ignores user/site configuration, and
# cannot read installed __pycache__ because -X supplies a fresh prefix. Bounded
# marshal parsing happens in this child; no untrusted code object is evaluated.
COMPARE = r'''
import sys, json, marshal, types, importlib.util
fields = ('co_code', 'co_names', 'co_varnames', 'co_freevars', 'co_cellvars',
          'co_flags', 'co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount',
          'co_nlocals', 'co_stacksize', 'co_exceptiontable')
def equal(a, b):
    if type(a) is not type(b): return False
    if isinstance(a, types.CodeType):
        return all(getattr(a, n) == getattr(b, n) for n in fields) and equal(a.co_consts, b.co_consts)
    if isinstance(a, tuple): return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, frozenset): return len(a) == len(b) and all(any(equal(x, y) for y in b) for x in a)
    return a == b
accepted = []
for row in json.load(sys.stdin):
    try:
        with open(row['source'], 'rb') as f: source = f.read(16 * 1024 * 1024 + 1)
        with open(row['cache'], 'rb') as f: cache = f.read(16 * 1024 * 1024 + 1)
        if max(len(source), len(cache)) > 16 * 1024 * 1024: continue
        if row['tag'] != sys.implementation.cache_tag or cache[:4] != importlib.util.MAGIC_NUMBER: continue
        if int.from_bytes(cache[4:8], 'little') not in (0, 1, 3): continue
        actual = marshal.loads(cache[16:])
        expected = compile(source, row['source'], 'exec', dont_inherit=True, optimize=row['optimize'])
        if equal(actual, expected): accepted.append(row['name'])
    except (ValueError, TypeError, EOFError, SyntaxError): pass
print(json.dumps(accepted))
'''


def verified_python_caches(manager, base, files, conflicts, *, view=None):
    """Return only mismatches proven to be recompilations of official source.

files maps installer-relative names to official SHA-256 hashes. Unknown or
source-less bytecode stays a conflict. Interpreter files must all match before
starting the comparison process. No installation files are changed.
"""
    candidates = {name: source_cache(name, files) for name in conflicts}
    candidates = {name: value for name, value in candidates.items() if value}
    if not candidates:
        return set()
    base = Path(base)
    root = repair.safe_path(base / PYTHON_ROOT)
    if view is None:
        view = FileView([root], verification_cache=manager.verification_cache)
        view.hash_files()
    for name, expected in files.items():
        if name.startswith(PYTHON_ROOT) and not source_cache(name, files):
            if view.hash(base / name) != expected:
                return set()
    # A ._pth, pyvenv.cfg, shadow module or DLL must not redirect the helper.
    for path, entry in view.entries.items():
        name = path.relative_to(base).as_posix()
        if not name.startswith(PYTHON_ROOT):
            continue
        if entry['type'] == 'link' or entry['type'] == 'file' and name not in files and not source_cache(name, files):
            return set()
    interpreter = repair.safe_path(root / 'python.exe')
    if (PYTHON_ROOT + 'python.exe') not in files:
        return set()
    store = manager.official_packages.store
    identity = hashlib.sha256(canonical({n: h for n, h in files.items() if n.startswith(PYTHON_ROOT)})).hexdigest()
    receipt = store._path('receipts/derived-python-' + identity + '.json')
    allowed = {}
    try:
        value = store.read_object(json.loads(receipt.read_bytes())['manifest'])
        if value['kind'] == 'derived-python-caches' and value['filesHash'] == identity:
            allowed = value['allowed']
    except (OSError, ValueError, KeyError, TypeError, repair.LauncherError):
        pass
    accepted, rows = set(), []
    with ExitStack() as leases:
        for name, (source, tag, optimize) in candidates.items():
            actual = view.hash(base / name)
            if actual is None or view.hash(base / source) != files[source]:
                continue
            if allowed.get(name) == actual:
                accepted.add(name)
            else:
                for filename in (name, source):
                    leases.enter_context(repair.read_lease(base / filename))
                # Only a fresh comparison needs extra leases. A cache hit uses
                # the same already-verified FileView as its caller, avoiding a
                # second per-file NTFS identity pass on daily launches.
                if (manager.verification_cache.hash(base / name, hash_file) != actual or
                    manager.verification_cache.hash(base / source, hash_file) != files[source]):
                    continue
                rows.append({'name': name, 'source': str(base / source), 'cache': str(base / name),
                             'tag': tag, 'optimize': optimize, 'sha256': actual})
        if rows:
            leases.enter_context(repair.read_lease(interpreter))
            # Keep the comparison's loader inputs stable, including verified
            # source modules. Cache files are deliberately never loaded.
            for name in files:
                if name.startswith(PYTHON_ROOT) and not source_cache(name, files):
                    leases.enter_context(repair.read_lease(base / name))
                    if manager.verification_cache.hash(base / name, hash_file) != files[name]:
                        return set()
            stage_root = store._path('staging')
            stage_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='pyc-check-', dir=stage_root) as stage:
                try:
                    result = subprocess.run([str(interpreter), '-I', '-S', '-B', '-X',
                        'pycache_prefix=' + stage, '-c', COMPARE], input=json.dumps(rows).encode(),
                        cwd=stage, capture_output=True, timeout=30, check=True,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    verified = json.loads(result.stdout)
                except (OSError, ValueError, subprocess.SubprocessError):
                    return accepted
            if not isinstance(verified, list):
                return accepted
            for row in rows:
                if row['name'] in verified:
                    accepted.add(row['name'])
                    allowed[row['name']] = row['sha256']
            ref = store.object('baselines', {'kind': 'derived-python-caches', 'filesHash': identity, 'allowed': allowed})
            write_json(receipt, {'manifest': ref})
    return accepted
