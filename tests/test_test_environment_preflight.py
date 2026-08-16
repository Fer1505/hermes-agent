from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_PATH = Path(__file__).parents[1] / "scripts" / "test_environment_preflight.py"
_SPEC = importlib.util.spec_from_file_location("test_environment_preflight", _PATH)
assert _SPEC and _SPEC.loader
preflight = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = preflight
_SPEC.loader.exec_module(preflight)


def test_focused_scope_uses_explicit_fixed_dependency_set(monkeypatch):
    imported = []
    monkeypatch.setattr(
        preflight.importlib,
        "import_module",
        lambda name: imported.append(name),
    )

    assert preflight.missing_requirements("focused") == []
    assert set(imported) == {
        module for module, _extra, _distribution in preflight.REQUIRED_IMPORTS
        if module not in preflight.FOCUSED_EXCLUDED_IMPORTS
    }


def test_focused_scope_reports_required_plugin_failure(monkeypatch):
    def fake_import(name):
        if name == "pytest_asyncio":
            raise ModuleNotFoundError(name)

    monkeypatch.setattr(preflight.importlib, "import_module", fake_import)
    missing = preflight.missing_requirements("focused")

    assert missing == [("pytest_asyncio", "dev", "pytest-asyncio", "ModuleNotFoundError")]


def test_canonical_scope_preflights_canonical_dependency_set(monkeypatch):
    imported = []
    monkeypatch.setattr(
        preflight.importlib,
        "import_module",
        lambda name: imported.append(name),
    )

    assert preflight.missing_requirements("canonical") == []
    assert set(imported) == {
        module for module, _extra, _distribution in preflight.REQUIRED_IMPORTS
    }


def test_invalid_scope_fails_closed():
    import pytest

    with pytest.raises(ValueError, match="canonical.*focused"):
        preflight.selected_requirements("auto")
