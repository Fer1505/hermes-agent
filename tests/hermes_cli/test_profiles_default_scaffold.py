"""The dormant pre-profile default home must not present itself as an agent.

On profile-mode deployments (sticky active_profile -> named profile, no
gateway, zero sessions) the default home is scaffolding; listing it painted a
phantom "Hermes" bot beside the real sticky-target profile in every agent
list (desktop BOTS sidebar included).
"""
import json

import pytest

from hermes_cli import profiles as profiles_mod


@pytest.fixture()
def default_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-root"
    (home / "profiles" / "worker").mkdir(parents=True)
    home.joinpath("active_profile").write_text("worker\n")
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(profiles_mod, "_check_gateway_running", lambda _home: False)
    return home


def test_dormant_scaffolding_is_detected(default_home):
    assert profiles_mod._default_home_is_dormant_scaffolding(default_home) is True


def test_sticky_default_keeps_the_entry(default_home):
    default_home.joinpath("active_profile").write_text("")
    assert profiles_mod._default_home_is_dormant_scaffolding(default_home) is False


def test_recorded_sessions_keep_the_entry(default_home):
    sessions = default_home / "sessions"
    sessions.mkdir()
    (sessions / "sessions.json").write_text(json.dumps({"agent:main:x": {"session_id": "s1"}}))
    assert profiles_mod._default_home_is_dormant_scaffolding(default_home) is False


def test_running_gateway_keeps_the_entry(default_home, monkeypatch):
    monkeypatch.setattr(profiles_mod, "_check_gateway_running", lambda _home: True)
    assert profiles_mod._default_home_is_dormant_scaffolding(default_home) is False


def test_list_profiles_skips_dormant_default(default_home, monkeypatch):
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: default_home / "profiles")
    names = [p.name for p in profiles_mod.list_profiles()]
    assert "default" not in names
    assert "worker" in names
    # With a recorded session the default comes back.
    sessions = default_home / "sessions"
    sessions.mkdir()
    (sessions / "sessions.json").write_text(json.dumps({"agent:main:x": {"session_id": "s1"}}))
    names = [p.name for p in profiles_mod.list_profiles()]
    assert "default" in names
