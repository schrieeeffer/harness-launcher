"""Immutable installation paths passed to infrastructure components."""
from dataclasses import dataclass
import hashlib
from pathlib import Path


@dataclass(frozen=True)
class RuntimeContext:
    data: Path
    home: Path
    profile: Path
    manifest_path: Path
    executable: Path
    resources: Path
    identity: str
    patch_root: Path

    @classmethod
    def from_manager(cls, manager):
        executable = Path(manager.settings['executable']).expanduser().resolve()
        home = manager.home
        resources = executable.parent / 'resources'
        identity = hashlib.sha256((str(resources).lower() + '|' + str(home).lower()).encode()).hexdigest()[:16]
        return cls(manager.data, home, manager.profile, manager.manifest_path, executable,
                   resources, identity, manager.data / 'patches' / identity)
