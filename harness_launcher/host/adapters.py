"""Declarative boundaries for built-in host patch adapters.

The application only asks whether an adapter exists. Plugin-owned filenames,
backup conventions and ownership markers belong to this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class HostArchive:
    resources: Path
    stash: Path

    def official_archive(self, view=None):
        candidates = (self.resources / 'app.asar', self.stash / 'official.asar',
                      self.resources / 'app.asar.bak')
        return next((path for path in candidates if
                     (view.is_file(path) if view else path.is_file())), None)


@dataclass(frozen=True)
class AdapterSpec:
    plugin: str
    rule_files: tuple[str, ...]
    injection_file: str
    rule_backup_suffix: str
    cli_backup_suffix: str
    ownership_markers: tuple[bytes, ...]
    preset: str

    def installed_marker(self, home: Path) -> Path:
        return home / self.plugin / 'installed-rev'

    def rule_backup(self, path: Path) -> Path:
        return path.with_name(path.name + self.rule_backup_suffix)

    def cli_path(self, resources: Path) -> Path:
        return resources / 'runtime' / 'cli' / 'bin' / 'dsh.cmd'

    def cli_backup(self, resources: Path) -> Path:
        path = self.cli_path(resources)
        return path.with_name(path.name + self.cli_backup_suffix)

    def owns_rule(self, name: str, content: bytes, *, has_backup: bool,
                  active_rule: bytes | None) -> bool:
        return (name == self.injection_file or has_backup or content == active_rule
                or any(marker in content for marker in self.ownership_markers))

    def extra_roots(self, home: Path, resources: Path, stash: Path,
                    home_stash: Path) -> tuple[Path, ...]:
        return (stash / 'app', stash / 'official.asar', home_stash / self.preset,
                *(self.rule_backup(home / name) for name in self.rule_files),
                self.cli_backup(resources))

    def mutation_targets(self, home: Path, resources: Path, stash: Path,
                         home_stash: Path) -> set[Path]:
        return {resources / 'app', resources / 'app.asar', resources / 'app.asar.bak',
                stash / 'app', stash / 'official.asar',
                *(home / name for name in self.rule_files),
                home / '.agent-presets' / self.preset, home_stash / self.preset,
                self.cli_path(resources)}


PURGE = AdapterSpec(
    plugin='dsh-purge', rule_files=('prompt-inject.md', 'AGENTS.md', 'CLAUDE.md'),
    injection_file='prompt-inject.md', rule_backup_suffix='.dsh-purge-rule.bak',
    cli_backup_suffix='.dsh-purge.bak', preset='redteam',
    ownership_markers=(b'Little Code Sauce', b'UNRESTRICTED SANDBOX'),
)
ADAPTERS = MappingProxyType({PURGE.plugin: PURGE})


def is_adapter(name: str) -> bool:
    return name in ADAPTERS
