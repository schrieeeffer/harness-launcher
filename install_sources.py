"""Read-only source checks. An inspected source is required before an HTTP install.

No clone, download of a tarball, build, or plugin execution during inspection.
Published npm versions and Git commits are pinned; local inputs are rechecked.
This checks install structure, not trustworthiness or runtime compatibility.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import threading
import time
from urllib.parse import quote, urlparse

from catalog import CatalogError, PACKAGE, REPOSITORY, VERSION
from installer_policy import build_notice


def required_files(meta, name):
    if not isinstance(meta, dict) or meta.get('name') != name:
        raise CatalogError('来源中的包名与当前插件不一致。')
    version = meta.get('version', '')
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise CatalogError('来源没有有效的插件版本。')
    dsh = meta.get('dsh')
    bundle = dsh.get('bundle') if isinstance(dsh, dict) else None
    patch = bundle.get('patch') if isinstance(bundle, dict) else None
    if not isinstance(patch, str) or not patch.strip():
        raise CatalogError('没有 dsh.bundle.patch，不能作为独立插件安装。')
    paths = ['package.json', patch]
    if meta.get('main'):
        paths.append(meta['main'])
    # Validate executable export targets too; declarations/wildcards are not entries.
    def exports(value):
        if isinstance(value, str) and value.startswith('./') and '*' not in value and not value.endswith(('.d.ts', '.d.mts', '.d.cts')):
            paths.append(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key != 'types':
                    exports(child)
        elif isinstance(value, list):
            for child in value:
                exports(child)
    exports(meta.get('exports'))
    result = set()
    for path in paths:
        if not isinstance(path, str) or not path or '\\' in path or ':' in path or any(ord(c) < 32 for c in path):
            raise CatalogError('插件声明了无效的安装文件路径。')
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or '..' in parsed.parts or str(parsed) == '.':
            raise CatalogError('插件安装文件必须位于包目录内。')
        result.add(str(parsed))
    return sorted(result)


class InstallSources:
    ttl = 300

    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.RLock()
        self.proofs = {}

    def read(self, url):
        result = self.catalog.cached(('install-source', url), lambda: self.catalog.fetch(url))
        if result.get('stale'):
            raise CatalogError('来源信息已过期且无法重新验证，请稍后重试。')
        return result

    def npm(self, name, value):
        tag = value or 'latest'
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,100}', tag):
            raise CatalogError('请输入一个具体版本或发布标签。')
        meta = self.read('https://registry.npmjs.org/' + quote(name, safe='') + '/' + quote(tag, safe=''))
        required_files(meta, name)
        if re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?', tag) and meta['version'] != tag:
            raise CatalogError('发布元数据版本与指定版本不一致。')
        dist = meta.get('dist', {})
        if not isinstance(dist, dict) or not isinstance(dist.get('integrity'), str) or not re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==', dist['integrity']):
            raise CatalogError('npm 发布包没有有效的 SHA-512 完整性信息。')
        tarball = urlparse(str(dist.get('tarball', '')))
        if tarball.scheme != 'https' or tarball.hostname != 'registry.npmjs.org' or tarball.username or tarball.password:
            raise CatalogError('发布包下载来源不是 npm 官方源。')
        repository = meta.get('repository', {})
        repository = repository.get('url', '') if isinstance(repository, dict) else repository
        return {'source': name + '@' + meta['version'], 'version': meta['version'],
                'repository': repository if isinstance(repository, str) else '',
                'reason': '已确认 npm 发布包，锁定 v' + meta['version'] + '；下载由官方安装器校验。'}

    def git(self, name, value):
        parsed = urlparse(value.removeprefix('git+'))
        repo = parsed.path.strip('/').removesuffix('.git')
        if parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or not REPOSITORY.fullmatch(repo):
            raise CatalogError('目前只能验证 HTTPS GitHub 仓库；其他仓库请构建后验证本地目录。')
        base = 'https://api.github.com/repos/' + repo
        commit = self.read(base + '/commits/' + quote(parsed.fragment or 'HEAD', safe='')).get('sha', '')
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise CatalogError('无法锁定仓库提交。')
        try:
            content = self.read(base + '/contents/package.json?ref=' + commit)
        except CatalogError as exc:
            raise CatalogError('无法确认仓库根目录的 package.json。源码子目录或未构建的仓库不能直接安装；请按作者说明构建后验证本地目录。') from exc
        try:
            if content.get('encoding') != 'base64' or content.get('size', 0) > 256 * 1024:
                raise ValueError()
            raw = base64.b64decode(content['content'])
            if len(raw) > 256 * 1024:
                raise ValueError()
            meta = json.loads(raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise CatalogError('无法读取仓库根目录的包信息。') from exc
        required = required_files(meta, name)
        tree = self.read(base + '/git/trees/' + commit + '?recursive=1')
        if tree.get('truncated') or not isinstance(tree.get('tree'), list):
            raise CatalogError('仓库文件清单不完整，不能确认可安装。')
        files = {x.get('path') for x in tree['tree'] if isinstance(x, dict) and x.get('type') == 'blob' and x.get('mode') in ('100644', '100755')}
        missing = [path for path in required if path not in files]
        if missing:
            raise CatalogError('仓库缺少安装文件：' + '、'.join(missing[:5]) + '。请先构建，再验证本地目录。')
        return {'source': 'git+https://github.com/' + repo + '.git#' + commit,
                'version': meta['version'], 'reason': '根目录与声明的安装文件已确认；锁定提交 ' + commit[:12] + '。'}

    def local(self, name, value):
        root = Path(value)
        if not value or not root.is_absolute() or not root.is_dir():
            raise CatalogError('请提供已构建插件的本地绝对目录。')
        root = root.resolve()
        def read(path, limit):
            file = root / path
            # Reject junction/symlink escapes, directories and oversized entries.
            if not file.resolve().is_relative_to(root) or not file.is_file() or file.stat().st_size > limit:
                raise CatalogError('文件缺失、过大或位于目录外：' + path)
            with file.open('rb') as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise CatalogError('安装入口文件过大：' + path)
            return raw
        try:
            meta = json.loads(read('package.json', 256 * 1024))
            required = required_files(meta, name)
            hashes = {path: hashlib.sha256(read(path, 32 * 1024 * 1024)).hexdigest() for path in required}
        except (OSError, ValueError) as exc:
            raise CatalogError('无法读取本地插件的声明或构建文件。') from exc
        return {'source': str(root), 'version': meta['version'], 'fingerprint': hashes,
                'reason': '已确认本地包名、补丁声明和构建入口；开始前再次核对。'}

    def inspect(self, name, kind, value=''):
        if not isinstance(name, str) or not PACKAGE.fullmatch(name) or len(name) > 160:
            raise CatalogError('请先填写有效的插件包名。')
        if kind not in ('npm', 'git', 'local') or not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise CatalogError('安装来源无效。')
        result = getattr(self, kind)(name, value.strip())
        token = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            self.proofs = {k: v for k, v in self.proofs.items() if v['expiresAt'] > now}
            if len(self.proofs) >= 128:
                self.proofs.pop(next(iter(self.proofs)))
            proof = {**result, 'reason': result.get('reason', '') + build_notice(name, result['source']),
                     'kind': kind, 'name': name, 'expiresAt': now + self.ttl}
            self.proofs[token] = proof
        return {k: v for k, v in {**proof, 'token': token, 'enabled': True}.items() if k != 'fingerprint'}

    def authorize(self, name, source, token):
        with self.lock:
            proof = self.proofs.get(token) if isinstance(token, str) else None
        if not proof or proof['expiresAt'] <= time.time() or (proof['name'], proof['source']) != (name, source):
            raise CatalogError('安装来源未验证、已变化或验证已过期，请重新验证来源。')
        if proof['kind'] == 'local' and self.local(name, source)['fingerprint'] != proof['fingerprint']:
            raise CatalogError('本地构建文件在验证后发生变化，请重新验证。')
        return proof
