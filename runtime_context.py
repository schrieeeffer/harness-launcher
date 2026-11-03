"""Immutable installation paths passed to infrastructure components."""
from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path

from errors import PatchError
from storage import ContentStore


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
    base_patch_root: Path

    @classmethod
    def from_manager(cls, manager):
        executable = Path(manager.settings['executable']).expanduser().resolve()
        home = manager.home
        resources = executable.parent / 'resources'
        identity = hashlib.sha256((str(resources).lower() + '|' + str(home).lower()).encode()).hexdigest()[:16]
        base = manager.data / 'patches' / identity
        pointer = ContentStore(base)._path('active-host.json')
        generation = getattr(manager, '_generation_override', None)
        if generation is None and pointer.exists():
            value = json.loads(pointer.read_bytes())
            if value.get('format') != 1:
                raise PatchError('官方版本指针格式无效，已保留所有版本记录。')
            generation = value.get('generation')
        if generation is not None and not re.fullmatch('[a-f0-9]{32}', generation):
            raise PatchError('官方版本记录编号无效。')
        patch_root = ContentStore(base)._path('generations/' + generation) if generation else base
        # Each generation owns its mutable parked directories; immutable blobs
        # and package revisions remain shared and content-addressed.
        stash_identity = identity + ('-' + generation[:12] if generation else '')
        return cls(manager.data, home, manager.profile, manager.manifest_path, executable,
                   resources, stash_identity, patch_root, base)
