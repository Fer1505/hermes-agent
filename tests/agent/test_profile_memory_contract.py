from pathlib import Path

from agent.profile_memory_contract import (
    PROFILE_MEMORY_CONTRACT_VERSION,
    resolve_profile_memory_paths,
)


def test_v1_contract_separates_doctrine_learned_and_runtime_memory(tmp_path):
    paths = resolve_profile_memory_paths(tmp_path / "profile")

    assert paths.contract_version == PROFILE_MEMORY_CONTRACT_VERSION
    assert paths.doctrine == tmp_path / "profile" / "SOUL.md"
    assert paths.learned_memory == tmp_path / "profile" / "memories" / "MEMORY.md"
    assert paths.user_profile == tmp_path / "profile" / "memories" / "USER.md"
    assert paths.runtime_directory == tmp_path / "profile" / "memory"
    assert paths.learned_memory.parent != paths.runtime_directory


def test_contract_never_resolves_retired_root_memory_files(tmp_path):
    root = tmp_path / "profile"
    paths = resolve_profile_memory_paths(root)

    assert paths.learned_memory != Path(root, "MEMORY.md")
    assert paths.user_profile != Path(root, "USER.md")
