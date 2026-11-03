"""Evidence for peer modules bundled into Harness's browser shell.

Some client peers have no node_modules/package.json: the official frontend
registers them in its staticModules table. Recognize only the literal table
passed to the boot loader, without evaluating JavaScript. Unknown build shapes
remain unproven. Every receipt is bound to the archive and actual entry bytes.
"""
import json
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re

from patch_modes import PatchError, archive_file, sha


def static_modules(script):
    identifier = r'[A-Za-z_$][A-Za-z0-9_$]*'
    calls = re.findall(r'\.create\(\{\s*boot\s*:[^,{}]+,\s*staticModules\s*:(' + identifier + r')\(\)\s*[,}]', script)
    if len(set(calls)) != 1:
        return set()
    bodies = re.findall(r'\bfunction\s+' + re.escape(calls[0]) + r'\(\)\s*\{\s*return\s*\{([^{};]+)\}\s*;?\s*\}', script)
    if len(bodies) != 1:
        return set()
    result = set()
    fields = bodies[0].split(',')
    if len(fields) > 128:
        return set()
    for field in fields:
        match = re.fullmatch(r'\s*("[^"\\\r\n]+"|' + identifier + r')\s*:\s*' + identifier + r'\s*', field)
        if not match:
            return set()
        key = json.loads(match[1]) if match[1].startswith('"') else match[1]
        if key in result:
            return set()
        result.add(key)
    return result


class EntryScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'script' or attrs.get('type') != 'module':
            return
        src = attrs.get('src', '')
        if not src or any(c in src for c in ':\\?#') or src.startswith('//'):
            return
        path = PurePosixPath(src.removeprefix('/'))
        if '..' not in path.parts and path.parts and path.parts[0] == 'assets' and path.suffix == '.js':
            self.paths.append(str(path))


def browser_peer(core, archive, name, view=None):
    prefix = 'dsh/node_modules/@deepseek-ai/dsh-web-frontend/'
    def read(path):
        return archive_file(archive, prefix + path, core.resources / 'app.asar.unpacked', view)
    try:
        manifest = read('package.json')
        meta = json.loads(manifest)
        if meta.get('name') != '@deepseek-ai/dsh-web-frontend' or not isinstance(meta.get('version'), str):
            return None
        html = read('dist/index.html')
        parser = EntryScripts()
        parser.feed(html.decode('utf-8'))
        if not 0 < len(parser.paths) <= 4:
            return None
        for entry in parser.paths:
            content = read('dist/' + entry)
            if name in static_modules(content.decode('utf-8')):
                return {'providedBy': 'harness', 'surface': 'web-static', 'name': name,
                        'version': 'bundled', 'frontendVersion': meta['version'],
                        'manifestHash': sha(manifest), 'indexHash': sha(html),
                        'entry': prefix + 'dist/' + entry, 'entryHash': sha(content)}
    except (PatchError, ValueError, TypeError, KeyError):
        return None
    return None
