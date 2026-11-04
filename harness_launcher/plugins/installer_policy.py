"""Allow lifecycle scripts only for the selected fixed install source.

Registry selectors retain the exact version. GitHub sources use pnpm's
resolved tarball path, including the commit; a bare name does not approve
Git-hosted artifacts in the bundled pnpm 11.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import subprocess
import hashlib

from harness_launcher.host.adapters import ADAPTERS, is_adapter
from harness_launcher.storage.content_store import atomic_write

from harness_launcher.domain.errors import LauncherError


_NAME = r'(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*'
_VERSION = r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?'
_GIT = re.compile(r'(?:(?:git\+)?https://github\.com/|github:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?#([a-f0-9]{40})')


def build_key(name, source):
    if not isinstance(name, str) or not re.fullmatch(_NAME, name) or not isinstance(source, str):
        return None
    if re.fullmatch(re.escape(name) + '@' + _VERSION, source):
        return source
    git = _GIT.fullmatch(source)
    if git and all(part not in ('.', '..') for part in git[1].split('/')):
        return name + '@https://codeload.github.com/' + git[1] + '/tar.gz/' + git[2]
    return None


def install_arguments(name, source):
    key = build_key(name, source)
    return ['add', source, *(['--allow-build=' + key] if key else [])]


@contextmanager
def isolated_build_policy(names, command):
    """Temporarily deny non-target builds; retain the installer's other edits.

    command calls the official pnpm config CLI, never parses YAML with regex.
    A crash leaves denials in place and the transaction checkpoint can restore
    the original file. A later conflicting edit is retained, not overwritten.
    """
    names = list(names)
    if any(not isinstance(name, str) or not re.fullmatch(_NAME, name) for name in names):
        raise LauncherError('非目标插件名称无效，已停止安装。')
    names = sorted(set(names))
    if not names:
        yield
        return
    require_supported_policy(command)

    original = read_policy(command)
    changed = [name for name in names if original.get(name) is not False]
    protected = {**original, **{name: False for name in changed}}
    if changed:
        write_policy(command, protected)
    may_restore = True
    try:
        if read_policy(command) != protected:
            raise LauncherError('非目标插件构建隔离未生效，已停止安装。')
        yield
    except LauncherError as exc:
        # Do not edit policy while a timed-out child may still be writing.
        if exc.code == 'installer_timeout':
            may_restore = False
        raise
    finally:
        if may_restore:
            current = read_policy(command)
            if any(current.get(name) is not False for name in names):
                raise LauncherError('维护期间非目标插件构建策略被修改，已保留现场和恢复点。',
                                    code='installer_policy_conflict')
            restored = dict(current)
            for name in changed:
                if name in original:
                    restored[name] = original[name]
                else:
                    restored.pop(name, None)
            if changed:
                write_policy(command, restored)


def require_supported_policy(command):
    if command(['get', 'dangerouslyAllowAllBuilds', '--json']).strip() == 'true':
        raise LauncherError('当前包管理器配置会放行全部构建脚本，无法保证非目标插件隔离。',
                            code='installer_policy_unsupported')
    permissions = command(['get', 'permissions', '--json']).strip()
    if permissions not in ('', 'undefined', 'null', '{}'):
        raise LauncherError('当前构建策略使用另一套 permissions 配置，需先确认隔离能力。',
                            code='installer_policy_unsupported')

def policy_command(manager, env):
    """Use Harness's own package manager to read and update its project policy."""
    if any(key.lower() in ('pnpm_config_dangerously_allow_all_builds', 'npm_config_dangerously_allow_all_builds')
           and str(value).lower() not in ('', 'false', '0') for key, value in env.items()):
        raise LauncherError('环境配置会放行全部构建脚本，无法保证非目标插件隔离。',
                            code='installer_policy_unsupported')
    pnpm = Path(manager.settings['executable']).parent / 'resources/runtime/primary-runtime/dependencies/pnpm/bin/pnpm.mjs'

    def command(arguments):
        try:
            result = subprocess.run([manager.settings['executable'], '--expose-internals', str(pnpm),
                                     'config', *arguments], cwd=manager.profile, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LauncherError('官方构建策略操作未完成，检查点已保留。', code='installer_policy_failed') from exc
        if result.returncode or len(result.stdout) > 65536:
            raise LauncherError('官方包管理器无法读取或保存本次构建隔离策略，检查点已保留。',
                                code='installer_policy_failed')
        return result.stdout.decode('utf-8-sig')

    return command


def adapter_policy(value, names):
    """Deny only adapter-owned matchers, including fixed Git/tarball sources."""
    result = dict(value)
    for name in names:
        if name not in ADAPTERS:
            raise LauncherError('未知补丁适配器，未修改安装策略。')
        for key in list(result):
            if key == name or key.startswith(name + '@'):
                result[key] = False
        result[name] = False
    return result


def read_policy(command):
    raw = command(['get', 'allowBuilds', '--json']).strip()
    value = {} if raw in ('', 'undefined', 'null') else json.loads(raw)
    if not isinstance(value, dict) or any(not isinstance(key, str) or
            not isinstance(item, (bool, str)) for key, item in value.items()):
        raise LauncherError('无法确认官方构建策略，未修改配置。')
    return value


def write_policy(command, value):
    command(['set', 'allowBuilds', json.dumps(value, ensure_ascii=False), '--json', '--location=project'])
    if read_policy(command) != value:
        raise LauncherError('补丁脚本隔离未生效，请检查官方包管理器配置。', code='installer_policy_conflict')


def ensure_adapter_isolation(manager, env=None):
    """Keep adapter scripts out of ordinary official installs, even after exit.

    This changes build permission, not package bytes or plugin loading. An
    explicit adapter maintenance operation opens a temporary build window below.
    """
    _, manifest, bundles = manager.manifest()
    names = [p['id'] for p in manager.discover(manifest, bundles) if is_adapter(p['id'])]
    if not names:
        return
    manager.require_stopped()
    if env is None:
        import os
        env = dict(os.environ, ELECTRON_RUN_AS_NODE='1', DSH_HOME=str(manager.home))
    command = policy_command(manager, env)
    require_supported_policy(command)
    original = read_policy(command)
    desired = adapter_policy(original, names)
    if original == desired:
        return
    # Preserve the exact workspace policy before the first persistent edit.
    path = manager.profile / 'pnpm-workspace.yaml'
    content = path.read_bytes() if path.is_file() else b''
    profile_key = hashlib.sha256(str(manager.profile).encode()).hexdigest()[:16]
    backup = manager.data / 'installer-policy' / profile_key / (hashlib.sha256(content).hexdigest()+'.yaml')
    if not backup.exists():
        atomic_write(backup, content)
    if read_policy(command) != original or (path.read_bytes() if path.is_file() else b'') != content:
        raise LauncherError('安装策略已被其他操作修改，请重试。', code='installer_policy_conflict')
    write_policy(command, desired)


@contextmanager
def adapter_build_window(selected, command):
    """Only explicit adapter maintenance can allow its selected source script."""
    if not is_adapter(selected):
        yield
        return
    original = read_policy(command)
    opened = {key: value for key, value in original.items()
              if key != selected and not key.startswith(selected + '@')}
    write_policy(command, opened)
    may_restore = True
    try:
        yield
    except LauncherError as exc:
        if exc.code == 'installer_timeout':
            may_restore = False
        raise
    finally:
        # Keep unrelated installer's edits; never leave an adapter allowed for
        # the next ordinary install, whether this operation succeeded or failed.
        if may_restore:
            write_policy(command, adapter_policy(read_policy(command), [selected]))


@contextmanager
def protect_other_builds(manager, selected, env):
    ensure_adapter_isolation(manager, env)
    command = policy_command(manager, env)
    _, manifest, bundles = manager.manifest()
    names = [plugin['id'] for plugin in manager.discover(manifest, bundles) if plugin['id'] != selected]
    with adapter_build_window(selected, command), isolated_build_policy(names, command):
        yield


def build_notice(name, source):
    key = build_key(name, source)
    return (' 确认安装将允许此固定来源自身的安装脚本：' + key +
            '；依赖的脚本仍遵循官方包管理器策略。维护期间禁止非目标插件运行构建脚本。'
            '普通插件随后恢复原策略，特殊补丁插件继续隔离，仅在专门维护时运行所选来源的脚本。') if key else ''


def failure_hint(log):
    # Read bounded diagnostics only to classify an error, never execute text.
    try:
        with log.open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read().decode('utf-8', errors='replace')
    except OSError:
        return ''
    if 'ERR_PNPM_IGNORED_BUILDS' in tail:
        return '安装脚本被官方构建策略阻止；请核对日志中的精确依赖并授权后重试，不要放行全部脚本。'
    return ''
