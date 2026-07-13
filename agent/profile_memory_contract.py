"""Versioned filesystem contract for profile-scoped Hermes memory.

Doctrine and learned memory intentionally live in different namespaces:

* ``SOUL.md`` is profile doctrine/persona and lives at the profile root.
* ``memories/MEMORY.md`` and ``memories/USER.md`` are learned, curated state.
* ``memory/`` is reserved for runtime/swarm state owned by integrations.

Keeping these paths in one dependency-light module prevents integrations from
silently reviving the retired root-level ``MEMORY.md`` / ``USER.md`` layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


PROFILE_MEMORY_CONTRACT_VERSION = "olympus.profile-memory/v1"
DOCTRINE_RELATIVE_PATH = Path("SOUL.md")
LEARNED_MEMORY_DIRECTORY = Path("memories")
LEARNED_MEMORY_RELATIVE_PATH = LEARNED_MEMORY_DIRECTORY / "MEMORY.md"
USER_PROFILE_RELATIVE_PATH = LEARNED_MEMORY_DIRECTORY / "USER.md"
RUNTIME_MEMORY_DIRECTORY = Path("memory")


@dataclass(frozen=True)
class ProfileMemoryPaths:
    """Canonical paths for one already-resolved Hermes profile root."""

    contract_version: str
    profile_root: Path
    doctrine: Path
    learned_directory: Path
    learned_memory: Path
    user_profile: Path
    runtime_directory: Path


def resolve_profile_memory_paths(
    profile_root: Optional[Path] = None,
) -> ProfileMemoryPaths:
    """Resolve the v1 memory contract without consulting legacy root decoys."""

    root = Path(profile_root) if profile_root is not None else get_hermes_home()
    return ProfileMemoryPaths(
        contract_version=PROFILE_MEMORY_CONTRACT_VERSION,
        profile_root=root,
        doctrine=root / DOCTRINE_RELATIVE_PATH,
        learned_directory=root / LEARNED_MEMORY_DIRECTORY,
        learned_memory=root / LEARNED_MEMORY_RELATIVE_PATH,
        user_profile=root / USER_PROFILE_RELATIVE_PATH,
        runtime_directory=root / RUNTIME_MEMORY_DIRECTORY,
    )
