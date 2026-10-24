"""Build a Windows x64 portable archive from an explicit public file allowlist.

Downloads CPython from python.org and checks the release page's SHA-256 before
extracting or executing it. No user's Harness files or launcher data are bundled.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.4.0'
PYTHON = '3.13.17'
RUNTIME_SHA = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
MODULES = '''adapters bootstrap.ps1 cache_gc catalog domain errors file_view launch.vbs launcher maintenance
maintenance_files ntfs_journal operation_models operations patch_modes plugin_tracking
recording_state runtime_context settings_store storage switch_plan tree_checkpoint update_check
verification_cache version_store'''.split()


def build():
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    target = dist / ('Harness-Launcher-' + VERSION + '-Windows-x64')
    # Never recursively remove a computed directory. A fresh temporary build
    # directory becomes the versioned artifact after every verification passes.
    import tempfile
    stage = Path(tempfile.mkdtemp(prefix='portable-', dir=dist))
    runtime_zip = dist / ('python-' + PYTHON + '-embed-amd64.zip')
    if not runtime_zip.exists():
        with urllib.request.urlopen('https://www.python.org/ftp/python/' + PYTHON + '/' + runtime_zip.name,
                                    timeout=60) as response:
            runtime_zip.write_bytes(response.read(30 * 1024 * 1024))
    if hashlib.sha256(runtime_zip.read_bytes()).hexdigest() != RUNTIME_SHA:
        raise RuntimeError('CPython download does not match the published release SHA-256')
    runtime = stage / 'runtime'
    runtime.mkdir()
    with zipfile.ZipFile(runtime_zip) as archive:
        for name in archive.namelist():
            if Path(name).name != name:
                raise RuntimeError('Unexpected runtime archive path')
        archive.extractall(runtime)
    (runtime / 'python313._pth').write_text('python313.zip\n.\n..\n', encoding='utf-8')
    for module in MODULES:
        filename = module if '.' in module else module + '.py'
        shutil.copyfile(ROOT / filename, stage / filename)
    shutil.copytree(ROOT / 'ui', stage / 'ui')
    shutil.copytree(ROOT / 'docs', stage / 'docs')
    for name in ('README.md', 'README.en.md', 'LICENSE', 'CHANGELOG.md'):
        shutil.copyfile(ROOT / name, stage / name)
    quickstart = '''Harness Launcher 0.4.0 — Windows 10/11 64 位便携版

1. 先安装并至少打开一次 DeepSeek Harness 官方桌面版。
2. 将本 ZIP 完整解压到你自己的文件夹，例如 D:\\Apps\\HarnessLauncher。
3. 双击 Harness Launcher.exe。不需要安装 Python，也不需要输入命令。
4. 若未找到 Harness，在“设置与说明”中点击“自动检测”，或填写应用路径。
5. 日常选择插件后点击“启动 Harness”。更新、卸载、恢复和清理在“更新与卸载”中。

更新启动器：先退出旧启动器后台，解压新版到新文件夹后打开。
配置与恢复数据保存在用户目录 .harness-launcher，不在程序文件夹里。
不要删除这个数据目录来解决插件冲突；里面存有回滚材料。
数据目录只接受当前格式；格式不符时停止操作并保留原数据。

本软件为社区预览版，主程序未做商业代码签名。请从 schrieeeffer/harness-launcher
的 GitHub Releases 下载，并可用同页 SHA256SUMS.txt 核对文件。
只针对 Windows x64；不包含 DeepSeek Harness、第三方插件或个人数据。
升级下载和首次完整检查可能超过 30 秒；日常启动约 30 秒，受机器与插件影响。
首次运行记录只覆盖文档列出的范围；外部系统服务、注册表和工作区不属于回滚范围。
'''
    (stage / '先读我.txt').write_text(quickstart, encoding='utf-8-sig')
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                    '/reference:System.Windows.Forms.dll', '/out:' + str(stage / 'Harness Launcher.exe'),
                    str(ROOT / 'packaging/Launcher.cs')], check=True)
    subprocess.run([str(runtime / 'python.exe'), '-B', '-c',
        "import launcher,maintenance,cache_gc,ssl; assert launcher.VERSION == '0.4.0'; print('Portable runtime imports OK')"],
        cwd=stage, check=True)
    (stage / 'BUILD.json').write_text(json.dumps({'version': VERSION, 'python': PYTHON,
        'pythonDownloadSHA256': RUNTIME_SHA, 'sourceCommit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'files': {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in stage.rglob('*') if p.is_file()}}, indent=2), encoding='utf-8')
    archive_path = dist / (target.name + '.zip')
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                archive.write(path, target.name + '/' + path.relative_to(stage).as_posix())
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (dist / 'SHA256SUMS.txt').write_text(digest + '  ' + archive_path.name + '\n', encoding='ascii')
    print(json.dumps({'stage': str(stage), 'archive': str(archive_path), 'bytes': archive_path.stat().st_size,
                      'sha256': digest}))


if __name__ == '__main__':
    build()
