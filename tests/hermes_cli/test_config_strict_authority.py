"""Fresh authorization reads cannot reuse stale grants or skip managed policy."""
import os

import pytest
import yaml

from hermes_cli import config, managed_scope


@pytest.fixture
def policy_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: None)
    return home


def test_strict_read_does_not_create_missing_profile(policy_home):
    with pytest.raises(FileNotFoundError):
        config.load_config_readonly(strict=True)
    assert list(policy_home.iterdir()) == []


@pytest.mark.parametrize("broken", ["security: [", "[]", "false", "42"])
def test_strict_read_rejects_invalid_policy_after_success(policy_home, broken):
    path = policy_home / "config.yaml"
    path.write_text("security: {public_contacts: {grants: [{id: old}]}}\n", encoding="utf-8")
    assert config.load_config_readonly()["security"]["public_contacts"]["grants"]
    path.write_text(broken, encoding="utf-8")
    with pytest.raises((ValueError, yaml.YAMLError)):
        config.load_config_readonly(strict=True)


def test_strict_read_observes_change_even_when_file_signature_is_preserved(policy_home):
    path = policy_home / "config.yaml"
    path.write_text("security: {public_contacts: {grants: [{id: old}]}}\n", encoding="utf-8")
    previous = path.stat()
    config.load_config_readonly()
    path.write_text("security: {public_contacts: {grants: [{id: new}]}}\n", encoding="utf-8")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert config.load_config_readonly(strict=True)["security"]["public_contacts"]["grants"] == [{"id": "new"}]


def test_strict_read_honors_managed_revocation_and_rejects_invalid_overlay(policy_home, tmp_path, monkeypatch):
    (policy_home / "config.yaml").write_text(
        "security: {public_contacts: {grants: [{id: old}]}}\n", encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    path = managed / "config.yaml"
    path.write_text("security: {public_contacts: {grants: []}}\n", encoding="utf-8")
    assert config.load_config_readonly(strict=True)["security"]["public_contacts"]["grants"] == []
    path.write_text("security: [", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        config.load_config_readonly(strict=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config_readonly(strict=True)


def test_absent_optional_managed_file_is_allowed(policy_home, tmp_path, monkeypatch):
    (policy_home / "config.yaml").write_text("security: {}\n", encoding="utf-8")
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: tmp_path / "absent-managed")
    assert isinstance(config.load_config_readonly(strict=True), dict)
