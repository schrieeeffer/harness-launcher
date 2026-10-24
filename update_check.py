"""Read-only, source-bound update discovery; never used on the launch path."""
from concurrent.futures import ThreadPoolExecutor
import json
import base64
import re
import time
import os
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse

from catalog import Catalog, CatalogError, PACKAGE


SEMVER = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')
GITHUB = re.compile(r'^(?:git\+)?https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?(?:#([\w./-]+))?$')


def newer(candidate, installed):
    """Compare SemVer precedence, including numeric prerelease components."""
    def key(value):
        match = SEMVER.fullmatch(value)
        if not match:
            raise CatalogError('版本不是可比较的 SemVer，需人工选择。')
        major, minor, patch, pre = match.groups()
        parts = tuple((0, int(x)) if x.isdigit() else (1, x) for x in pre.split('.')) if pre else ()
        return (int(major), int(minor), int(patch), pre is None, parts)
    return key(candidate) > key(installed)


class UpdateCheck:
    def __init__(self, manager, catalog=None):
        self.manager = manager
        self.catalog = catalog or Catalog()

    def inventory(self):
        with self.manager.lock:
            _, manifest, bundles = self.manager.manifest()
            rows = self.manager.discover(manifest, bundles)
            dependencies = {**manifest.get('devDependencies', {}), **manifest.get('optionalDependencies', {}),
                            **manifest.get('dependencies', {})}
            for row in rows:
                row['source'] = dependencies.get(row['id'], '')
        return rows

    def one(self, row):
        result = {'name': row['id'], 'installed': row['version'], 'source': row['source'],
                  'status': 'manual', 'target': None, 'checkedAt': time.time()}
        try:
            source = row['source']
            if not isinstance(source, str) or not PACKAGE.fullmatch(row['id']):
                raise CatalogError('没有可识别的安装来源。')
            git = GITHUB.fullmatch(source)
            if git:
                repo, ref = git.groups()
                if ref and re.fullmatch('[a-fA-F0-9]{40}', ref):
                    result['message'] = '此来源固定到 Git 提交；保持固定版本，不自动跟随分支。'
                    return result
                branch = ref or self.catalog.fetch('https://api.github.com/repos/' + repo)['default_branch']
                commit = self.catalog.cached(('update-commit', repo, branch), lambda: self.catalog.fetch(
                    'https://api.github.com/repos/' + repo + '/commits/' + quote(branch, safe='')))
                sha = commit.get('sha', '')
                if not re.fullmatch('[a-f0-9]{40}', sha):
                    raise CatalogError('GitHub 没有返回有效提交。')
                value = self.catalog.fetch('https://api.github.com/repos/' + repo + '/contents/package.json?ref=' + sha)
                meta = json.loads(base64.b64decode(value['content']))
                if meta.get('name') != row['id'] or not SEMVER.fullmatch(meta.get('version', '')):
                    raise CatalogError('目标提交的包名或版本不匹配。')
                # No installed commit proof: require an explicit source refresh,
                # never label this case as definitely out of date.
                result.update(status='source-change', latest=meta['version'],
                              target='git+https://github.com/' + repo + '.git#' + sha,
                              requirements={'dshTarget': meta.get('dshTarget'), 'engines': meta.get('engines', {}),
                                            'peers': meta.get('peerDependencies', {})},
                              message='远端分支可固定到此提交；无法证明本机提交，预览后可明确选择重新安装。')
                return result
            local = source.startswith(('file:', 'link:'))
            if not source or (not local and (source.startswith(('workspace:', 'npm:')) or '/' in source or ':' in source)):
                result['message'] = '本地、别名或压缩包来源保留原来源，不替换成同名 npm 包。'
                return result
            if self.manager is not None:
                registries = [os.environ.get('npm_config_registry', '')]
                for path in (Path.home() / '.npmrc', self.manager.profile / '.npmrc'):
                    if path.is_file():
                        for line in path.read_text(encoding='utf-8-sig').splitlines():
                            if '=' in line:
                                key, value = line.split('=', 1)
                                if key.strip() == 'registry' or key.strip() == row['id'].split('/')[0] + ':registry':
                                    registries.append(value.strip())
                if any(value and urlparse(value).hostname not in ('registry.npmjs.org', 'registry.npmmirror.com')
                       for value in registries):
                    result['message'] = '检测到自定义或私有注册表；保持原来源，不向公共 npm 查询此包。'
                    return result
            try:
                meta = self.catalog.cached(('update-package', row['id']), lambda: self.catalog.fetch(
                    'https://registry.npmjs.org/' + quote(row['id'], safe='') + '/latest'))
            except CatalogError:
                if not local:
                    raise
                installed_meta = json.loads((Path(row['path']) / 'package.json').read_bytes())
                repository = installed_meta.get('repository', {})
                repository = repository.get('url', '') if isinstance(repository, dict) else repository
                if not isinstance(repository, str) or not GITHUB.fullmatch(repository):
                    raise
                fallback = self.one(dict(row, source=repository))
                fallback['source'] = source
                if fallback['status'] == 'source-change':
                    fallback['message'] = 'npm 查询未成功；可明确改用已安装包声明的 GitHub 仓库，目标固定到提交。外部源目录保留。'
                return fallback
            version = meta.get('version', '')
            if meta.get('name') != row['id'] or not SEMVER.fullmatch(version):
                raise CatalogError('更新源的包名或版本无效。')
            dsh = meta.get('dsh')
            bundle = dsh.get('bundle', {}) if isinstance(dsh, dict) else None
            if not isinstance(bundle, dict) or not isinstance(bundle.get('patch'), str):
                raise CatalogError('新版没有声明可安装的插件组合包。')
            result.update(latest=version, target=row['id'] + '@' + version,
                          status='available' if newer(version, row['version']) else 'current',
                          requirements={'engines': meta.get('engines', {}), 'peers': meta.get('peerDependencies', {}),
                                        'dshTarget': meta.get('dshTarget')},
                          deprecated=meta.get('deprecated', ''), stale=meta.get('stale', False),
                          message='兼容性由官方安装器检查；首次运行完成后保存新补丁。')
            if local:
                installed_meta = json.loads((Path(row['path']) / 'package.json').read_bytes())
                def repository(value):
                    repo = value.get('repository', {})
                    repo = repo.get('url', '') if isinstance(repo, dict) else repo
                    match = GITHUB.fullmatch(repo) if isinstance(repo, str) else None
                    return match.group(1).lower() if match else None
                if not repository(meta) or repository(meta) != repository(installed_meta):
                    result.update(status='manual', target=None, message='本地包与 npm 包的仓库身份无法对应，保持本地来源。')
                elif result['status'] == 'available':
                    result.update(status='source-change', message='本地包存在同仓库 npm 新版；升级将明确改用 npm 来源，外部源目录保留。')
            if meta.get('deprecated') or meta.get('stale'):
                result.update(status='manual', message='版本已弃用或查询使用过期缓存，请核实后重试。')
            if SEMVER.fullmatch(version).group(4):
                result.update(status='manual', message='最新标签指向预发布版本，当前自动升级只选择稳定版。')
        except (CatalogError, KeyError, TypeError, ValueError, OSError) as exc:
            result.update(status='error', message=str(exc))
        return result

    def check(self):
        rows = self.inventory()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.one, rows))
        return {'updates': results, 'checkedAt': time.time(), 'cacheSeconds': 300}

    def target(self, name, requested):
        row = next((r for r in self.inventory() if r['id'] == name), None)
        if row is None:
            raise CatalogError('插件已不在安装列表，请刷新。')
        result = self.one(row)
        if result['status'] not in ('available', 'source-change') or result['target'] != requested:
            raise CatalogError('更新目标已变化或不可自动升级，请重新检查更新。')
        # Re-read the exact version before a write; never execute a floating latest tag.
        git = GITHUB.fullmatch(requested)
        if git:
            repo, ref = git.groups()
            value = self.catalog.fetch('https://api.github.com/repos/' + repo + '/contents/package.json?ref=' + ref)
            meta = json.loads(base64.b64decode(value['content']))
        else:
            meta = self.catalog.fetch('https://registry.npmjs.org/' + quote(name, safe='') + '/' + result['latest'])
        if meta.get('name') != name or meta.get('version') != result['latest'] or meta.get('deprecated'):
            raise CatalogError('目标版本身份无效或已弃用。')
        return result, meta
