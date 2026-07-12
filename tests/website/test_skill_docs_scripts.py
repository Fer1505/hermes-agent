from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(relative_path: str) -> ModuleType:
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docs_generator_excludes_hidden_skill_directories() -> None:
    module = _load_script("website/scripts/generate-skill-docs.py")

    entries = module.discover_skills()

    assert entries
    assert all(
        not any(part.startswith(".") for part in Path(meta["rel_path"]).parts)
        for meta, _parsed in entries
    )


def test_public_skill_index_excludes_hidden_skill_directories() -> None:
    module = _load_script("website/scripts/extract-skills.py")

    entries = module.extract_local_skills()

    assert entries
    assert all("/." not in f"/{entry['docsPath']}" for entry in entries)
