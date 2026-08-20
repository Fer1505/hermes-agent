"""get_active_profile_name resolves symlink-registered profile homes."""

import pytest

from hermes_cli import profiles as profiles_mod


def test_symlinked_profile_home_returns_profile_name(monkeypatch, tmp_path):
    real_home = tmp_path / "elsewhere" / "olympus-hermes"
    real_home.mkdir(parents=True)
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "olympus-hermes").symlink_to(real_home)

    monkeypatch.setenv("HERMES_HOME", str(real_home))
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: root)

    assert profiles_mod.get_active_profile_name() == "olympus-hermes"


def test_unregistered_home_still_custom(monkeypatch, tmp_path):
    real_home = tmp_path / "elsewhere" / "rogue"
    real_home.mkdir(parents=True)
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(real_home))
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: root)

    assert profiles_mod.get_active_profile_name() == "custom"
