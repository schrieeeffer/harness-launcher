"""Exercise the package entry point and portable source manifest in isolation."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def check_service(python, application_root):
    """Also used when checking an extracted portable build with its own Python."""
    with tempfile.TemporaryDirectory(prefix='launcher-entry-') as folder:
        root = Path(folder)
        data = root / 'launcher-data'
        home = root / 'harness-profile'
        executable = root / 'fake-install/DeepSeek Harness.exe'
        executable.parent.mkdir()
        executable.write_bytes(b'isolated entry point fixture; never executed')
        env = dict(os.environ, DSH_HOME=str(home), DSH_EXECUTABLE=str(executable),
                   USERPROFILE=str(root), HOME=str(root), LOCALAPPDATA=str(root / 'local'),
                   APPDATA=str(root / 'roaming'), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        env.pop('PYTHONPATH', None)
        with (root / 'process.log').open('w+', encoding='utf-8') as output:
            process = subprocess.Popen([str(python), '-B', '-m', 'harness_launcher', '--no-open', '--data', str(data)],
                                       cwd=application_root, env=env, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                deadline = time.monotonic() + 20
                instance = data / 'instance.json'
                while not instance.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        output.seek(0)
                        raise AssertionError('Launcher did not start: ' + output.read())
                    time.sleep(0.05)
                identity = json.loads(instance.read_text(encoding='utf-8'))
                assert identity['pid'] == process.pid, 'Connected to a different launcher process'
                parsed = urllib.parse.urlparse(identity['url'])
                base = f'http://{parsed.netloc}'
                token = urllib.parse.parse_qs(parsed.query)['token'][0]
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for name in ('index.html', 'style.css', 'state.js', 'api.js', 'runtime.js', 'app.js'):
                    url = identity['url'] if name == 'index.html' else base + '/' + name
                    with opener.open(url, timeout=5) as response:
                        assert response.status == 200
                        assert response.read() == (application_root / 'ui' / name).read_bytes(), name
                request = urllib.request.Request(base + '/api/operations', headers={'Authorization': 'Bearer ' + token})
                with opener.open(request, timeout=5) as response:
                    assert json.load(response)['current'] is None
                settings = json.loads((data / 'settings.json').read_text(encoding='utf-8'))
                assert Path(settings['home']) == home
                assert Path(settings['executable']) == executable
                request = urllib.request.Request(base + '/api/quit', data=b'{}',
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                with opener.open(request, timeout=5) as response:
                    assert response.status == 200
                assert process.wait(timeout=10) == 0
                assert not instance.exists(), 'Clean exit left a stale instance record'
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


class EntrypointTests(unittest.TestCase):
    def test_module_entrypoint_serves_ui_and_exits_with_isolated_data(self):
        check_service(Path(sys.executable), ROOT)

    def test_portable_manifest_requires_every_module_and_no_unlisted_module(self):
        builder = runpy.run_path(str(ROOT / 'packaging/build_portable.py'))
        source_files = builder['source_files']
        files = source_files()
        self.assertTrue(all((ROOT / name).is_file() for name in files))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in files:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            with patch.dict(source_files.__globals__, ROOT=root):
                self.assertEqual(source_files(), files)
                extra = root / 'harness_launcher/unlisted.py'
                extra.touch()
                with self.assertRaisesRegex(RuntimeError, 'unlisted.py'):
                    source_files()
                extra.unlink()
                (root / 'harness_launcher/__main__.py').unlink()
                with self.assertRaisesRegex(RuntimeError, '__main__.py'):
                    source_files()


if __name__ == '__main__':
    unittest.main()
