"""_launch_profile_name: serve-lane sessions must be self-describing (2026-08-20)."""

import importlib
import os
from pathlib import Path

import tui_gateway.server as srv


def _with_home(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    else:
        monkeypatch.setenv("HERMES_HOME", value)


def test_profile_home_returns_name(monkeypatch, tmp_path):
    home = tmp_path / "profiles" / "olympus-hermes"
    home.mkdir(parents=True)
    _with_home(monkeypatch, str(home))
    assert srv._launch_profile_name() == "olympus-hermes"


def test_default_home_returns_none(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    _with_home(monkeypatch, str(home))
    assert srv._launch_profile_name() is None


def test_unset_home_returns_none(monkeypatch):
    _with_home(monkeypatch, None)
    assert srv._launch_profile_name() is None


def test_hostile_name_returns_none(monkeypatch, tmp_path):
    home = tmp_path / "profiles" / "Bad Name!"
    home.mkdir(parents=True)
    _with_home(monkeypatch, str(home))
    assert srv._launch_profile_name() is None
