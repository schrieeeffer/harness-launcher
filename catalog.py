"""Read-only package discovery. Fixed public endpoints; never executes remote code."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import copy
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

PACKAGE = re.compile(r'^(?:@[a-z0-9_.-]+/)?[a-z0-9_][a-z0-9_.-]*$')
REPOSITORY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}$)[A-Za-z0-9_.-]+$')
VERSION = re.compile(r'^[0-9][0-9A-Za-z.+-]{0,100}$')
REGISTRIES = {'npm': 'https://registry.npmjs.org', 'mirror': 'https://registry.npmmirror.com'}
ECOSYSTEM_KEYWORDS = 'keywords:dsh-plugin,deepseek-harness'
# npm search cannot sort by downloads, so rank within the first pages of its relevance order.
LEADERBOARD_PAGES, LEADERBOARD_PAGE_SIZE, LEADERBOARD_SIZE = 4, 250, 50
RISING_MIN_WEEKLY = 200
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}')
OFFICIAL_BUNDLES = {
    '@deepseek-ai/dsh-experimental-agent-team-profile': ('智能体团队', '团队协作、共享任务和团队工具。'),
    '@deepseek-ai/dsh-experimental-auto-review': ('自动授权审查', '为工具调用提供模型授权审查。'),
    '@deepseek-ai/dsh-experimental-schedule-bundle': ('自动化任务', '按设定的时间或周期执行任务。'),
    '@deepseek-ai/dsh-experimental-voice-input-bundle': ('语音输入', '本地 SenseVoice 转写语音，首次使用需要下载运行时。'),
}


class CatalogError(Exception):
    pass


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in {'registry.npmjs.org', 'registry.npmmirror.com', 'api.github.com'} or parsed.username or parsed.password:
            raise CatalogError('数据源重定向到了未知站点。')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        parsed = urlparse(value.removeprefix('git+'))
        return parsed.geturl() if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else ''
    except ValueError:
        return ''


class Catalog:
    def __init__(self):
        self.cache = {}
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(6)

    def fetch(self, url):
        # No client-supplied URLs, redirects outside these public hosts, or cookies.
        allowed = {'registry.npmjs.org', 'registry.npmmirror.com', 'api.github.com'}
        if urlparse(url).hostname not in allowed or urlparse(url).scheme != 'https':
            raise CatalogError('不支持的数据源。')
        request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'harness-launcher/0.1'})
        try:
            with self.slots, build_opener(PublicRedirects()).open(request, timeout=12) as response:
                if urlparse(response.url).hostname not in allowed:
                    raise CatalogError('数据源重定向到了未知站点。')
                raw = response.read(6 * 1024 * 1024 + 1)
                if len(raw) > 6 * 1024 * 1024:
                    raise CatalogError('数据源返回内容过大。')
                return json.loads(raw)
        except HTTPError as exc:
            if exc.code in (403, 429):
                raise CatalogError('数据源限流，请稍后重试，或切换 npm / GitHub 来源。') from exc
            if exc.code == 404:
                raise CatalogError('未找到该包或仓库。') from exc
            raise CatalogError(f'数据源暂不可用（HTTP {exc.code}）。') from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise CatalogError('无法连接数据源，请检查网络或尝试中国大陆镜像。') from exc

    def cached(self, key, task, ttl=300):
        with self.lock:
            previous = self.cache.get(key)
            if previous and time.time() - previous[0] < ttl:
                result = copy.deepcopy(previous[1])
                result['cached'] = True
                return result
        try:
            result = task()
        except CatalogError as exc:
            if not previous:
                raise
            result = copy.deepcopy(previous[1])
            result.update(stale=True, warning=str(exc) + ' 当前显示上次成功的结果。')
            return result
        result.update(cached=False, stale=False, fetchedAt=time.time())
        with self.lock:
            # Bound memory even if a client submits many different queries.
            if len(self.cache) >= 100:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.time(), copy.deepcopy(result))
        return result

    def search(self, query='', source='all', page=0, registry='npm'):
        if not isinstance(query, str) or len(query) > 120 or any(ord(x) < 32 for x in query):
            raise CatalogError('搜索词最多 120 个字符。')
        if source not in ('all', 'official', 'community', 'github') or registry not in REGISTRIES:
            raise CatalogError('搜索来源无效。')
        if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page <= 20:
            raise CatalogError('页码无效。')
        query = query.strip()
        key = ('search', query, source, page, registry)

        def task():
            providers = ['official', 'community', 'github'] if source == 'all' else [source]
            results, errors, more = [], [], False
            with ThreadPoolExecutor(max_workers=3) as pool:
                jobs = {pool.submit(self.search_one, query, item, page, registry): item for item in providers}
                for job in as_completed(jobs):
                    try:
                        rows, has_more = job.result()
                        results.extend(rows)
                        more = more or has_more
                    except CatalogError as exc:
                        errors.append(jobs[job] + '：' + str(exc))
            if not results and len(errors) == len(providers):
                raise CatalogError('；'.join(errors))
            results.sort(key=lambda r: (r['kind'] != 'official', r['provider'] == 'github', -r.get('stars', 0), r['name']))
            return {'items': results, 'hasMore': more, 'page': page, 'warning': '；'.join(errors),
                    'query': query, 'source': source}
        return self.cached(key, task)

    def search_one(self, query, source, page, registry):
        size = 18
        if source == 'official':
            matches = [(name, labels) for name, labels in OFFICIAL_BUNDLES.items()
                       if not query or query.lower() in (' '.join((name, *labels))).lower()]
            if matches:
                return [{'name': name, 'title': labels[0], 'description': labels[1], 'version': '',
                         'provider': 'npm', 'kind': 'official', 'stars': 0,
                         'url': 'https://www.npmjs.com/package/' + quote(name, safe='@/')}
                        for name, labels in matches] if page == 0 else [], False
        if source == 'github':
            # Escape qualifiers so searches remain confined to the ecosystem topic.
            terms = ' '.join(re.findall(r'[\w.\-\u4e00-\u9fff]+', query))
            params = {'q': 'topic:dsh-plugin archived:false ' + terms, 'per_page': size,
                      'page': page + 1, 'sort': 'stars', 'order': 'desc'}
            value = self.fetch('https://api.github.com/search/repositories?' + urlencode(params))
            items = []
            for repo in value.get('items', []):
                name = repo.get('full_name', '')
                if not REPOSITORY.fullmatch(name):
                    continue
                items.append({'name': name, 'description': str(repo.get('description') or '')[:1000],
                              'provider': 'github', 'kind': 'community', 'version': '',
                              'url': 'https://github.com/' + name, 'stars': repo.get('stargazers_count', 0)})
            return items, (page + 1) * size < min(value.get('total_count', 0), 378)
        qualifiers = 'scope:deepseek-ai' if source == 'official' else ECOSYSTEM_KEYWORDS
        # A bare package query also finds packages missing ecosystem keywords.
        text = query if PACKAGE.fullmatch(query) and ('dsh' in query or 'harness' in query) and source == 'community' else qualifiers + ' ' + query
        if registry == 'mirror':
            # npmmirror's upstream search does not implement npm keyword/scope qualifiers.
            text = ('@deepseek-ai/dsh ' if source == 'official' else 'dsh ') + query
        value = self.fetch(REGISTRIES[registry] + '/-/v1/search?' + urlencode({'text': text, 'size': size, 'from': page * size}))
        items = []
        for row in value.get('objects', []):
            package = row.get('package', {})
            name = package.get('name', '')
            if not isinstance(name, str) or not PACKAGE.fullmatch(name):
                continue
            official = name.startswith('@deepseek-ai/')
            if (source == 'official') != official:
                continue
            items.append({'name': name, 'version': str(package.get('version', ''))[:100],
                          'description': str(package.get('description', ''))[:1000],
                          'provider': 'npm', 'kind': 'official' if official else 'community',
                          'url': 'https://www.npmjs.com/package/' + quote(name, safe='@/'), 'stars': 0})
        return items, (page + 1) * size < value.get('total', 0)

    def leaderboard(self, board='downloads'):
        """Rank by npm's own 7-day download counts. The mirror reports its own totals, so it is never used."""
        if board not in ('downloads', 'rising'):
            raise CatalogError('排行榜类型无效。')
        result = self.cached(('leaderboard',), self.scan_downloads, ttl=3600)
        rows = result.pop('rows')
        if board == 'rising':
            # Most of the 30-day total happened this week: newly published or suddenly popular.
            rows = [r for r in rows if r['weekly'] >= RISING_MIN_WEEKLY and r['weekly'] * 2 >= r['monthly']]
        rows.sort(key=lambda r: (-r['weekly'], r['name']))
        return {**result, 'board': board, 'items': [{**r, 'rank': i} for i, r in enumerate(rows[:LEADERBOARD_SIZE], 1)]}

    def scan_downloads(self):
        def page(index):
            return self.fetch(REGISTRIES['npm'] + '/-/v1/search?' + urlencode(
                {'text': ECOSYSTEM_KEYWORDS, 'size': LEADERBOARD_PAGE_SIZE, 'from': index * LEADERBOARD_PAGE_SIZE}))
        rows, failed, total, seen = {}, 0, 0, 0
        with ThreadPoolExecutor(max_workers=LEADERBOARD_PAGES) as pool:
            jobs = [pool.submit(page, index) for index in range(LEADERBOARD_PAGES)]
            # Fetch in parallel but merge in page order, so a package listed on two shifting pages keeps its earlier entry.
            for job in jobs:
                try:
                    value = job.result()
                except CatalogError:
                    failed += 1
                    continue
                if isinstance(value.get('total'), int):
                    total = max(total, value['total'])
                for row in value.get('objects', []):
                    seen += 1
                    package, downloads = row.get('package', {}), row.get('downloads')
                    name = package.get('name', '')
                    if not isinstance(name, str) or not PACKAGE.fullmatch(name) or name in rows or not isinstance(downloads, dict):
                        continue
                    weekly, monthly = downloads.get('weekly'), downloads.get('monthly')
                    if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (weekly, monthly)):
                        continue
                    published = package.get('date')
                    rows[name] = {'name': name, 'version': str(package.get('version', ''))[:100],
                                  'description': str(package.get('description', ''))[:1000],
                                  'provider': 'npm', 'kind': 'official' if name.startswith('@deepseek-ai/') else 'community',
                                  'url': 'https://www.npmjs.com/package/' + quote(name, safe='@/'),
                                  'weekly': weekly, 'monthly': max(monthly, weekly),
                                  'published': published[:10] if isinstance(published, str) and DATE.match(published) else ''}
        if not rows:
            if failed:
                raise CatalogError('无法从 npm 官方源获取下载统计，请检查网络后重试。排行榜需要访问 registry.npmjs.org，中国大陆镜像不提供同口径的下载量。')
            raise CatalogError('npm 返回的结果中没有下载量数据，暂时无法生成排行榜。' if seen else 'npm 没有返回插件数据。')
        return {'rows': list(rows.values()), 'scanned': len(rows), 'total': max(total, len(rows)),
                'warning': f'有 {failed} 页数据加载失败，排名可能不完整。' if failed else ''}

    def detail(self, name, provider='npm', registry='npm'):
        if not isinstance(name, str) or registry not in REGISTRIES:
            raise CatalogError('插件标识无效。')
        if provider == 'npm' and PACKAGE.fullmatch(name):
            url = REGISTRIES[registry] + '/' + quote(name, safe='') + '/latest'
        elif provider == 'github' and REPOSITORY.fullmatch(name):
            url = 'https://api.github.com/repos/' + name + '/contents/package.json'
        else:
            raise CatalogError('插件来源或标识无效。')

        def task():
            value = self.fetch(url)
            if provider == 'github':
                try:
                    if value.get('encoding') != 'base64' or value.get('size', 0) > 256 * 1024:
                        raise ValueError()
                    meta = json.loads(base64.b64decode(value['content'], validate=False))
                except (ValueError, KeyError, TypeError) as exc:
                    raise CatalogError('仓库根目录没有可读取的 package.json，请查看仓库说明并手动添加。') from exc
            else:
                meta = value
            if not isinstance(meta, dict):
                raise CatalogError('包元数据格式无效。')
            package = meta.get('name', '')
            version = str(meta.get('version', ''))
            if not isinstance(package, str) or not PACKAGE.fullmatch(package):
                raise CatalogError('包元数据中没有有效的包名。')
            if provider == 'npm' and package != name:
                raise CatalogError('包名与查询不一致。')
            dsh = meta.get('dsh', {})
            bundle = isinstance(dsh, dict) and isinstance(dsh.get('bundle'), dict) and isinstance(dsh['bundle'].get('patch'), str)
            repository = meta.get('repository', {})
            repository = repository.get('url', '') if isinstance(repository, dict) else repository
            peer = meta.get('peerDependencies', {})
            peer = peer if isinstance(peer, dict) else {}
            installable = bundle and (provider == 'github' or bool(VERSION.fullmatch(version)))
            return {'name': package, 'version': version[:100], 'provider': provider,
                    'kind': 'official' if provider == 'npm' and package.startswith('@deepseek-ai/') else 'community',
                    'description': str(meta.get('description', ''))[:3000],
                    'license': str(meta.get('license', '未声明'))[:100],
                    'repository': safe_url(repository), 'homepage': safe_url(meta.get('homepage')),
                    'url': 'https://github.com/' + name if provider == 'github' else 'https://www.npmjs.com/package/' + quote(name, safe='@/'),
                    'compatibility': {k: str(v)[:100] for k, v in peer.items() if k.startswith('@deepseek-ai/')},
                    'installable': installable, 'bundle': bundle,
                    'source': ('git+https://github.com/' + name + '.git') if provider == 'github' else package + '@' + version,
                    'notice': '' if installable else '这是组件库或仓库未在根目录声明 dsh.bundle.patch，不能作为独立插件直接安装。'}
        return self.cached(('detail', provider, name, registry), task)
