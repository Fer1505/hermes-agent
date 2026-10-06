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


def test_missing_user_opt_in_still_reads_fresh_managed_policy(policy_home, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    path = managed / "config.yaml"
    path.write_text("agent: {native_oauth_task_limits: {enabled: true, max_attempts: 2, max_seconds: 60}}\n", encoding="utf-8")
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    result = config.load_config_readonly(strict=True, allow_missing_user=True)
    assert result["agent"]["native_oauth_task_limits"]["enabled"] is True
    assert list(policy_home.iterdir()) == []
    with pytest.raises(FileNotFoundError):
        config.load_config_readonly(strict=True)
    path.write_text("agent: [", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        config.load_config_readonly(strict=True, allow_missing_user=True)
    with pytest.raises(ValueError, match="strict"):
        config.load_config_readonly(allow_missing_user=True)


@pytest.mark.parametrize("user", [None, "{}\n", "agent: [", "[]\n"])
def test_cold_config_import_is_readonly_and_cache_clean(tmp_path, user):
    import json
    import subprocess
    import sys
    from pathlib import Path
    home = tmp_path / "uninitialized"
    if user is not None:
        home.mkdir()
        (home / "config.yaml").write_text(user, encoding="utf-8")
    before = {str(p.relative_to(tmp_path)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*")}
    code = r'''
import json, os, sys, socket
from pathlib import Path
import httpx
import yaml

network_attempts = []
def no_network(*args, **kwargs):
    network_attempts.append("network-or-auth-refresh")
    raise AssertionError("Read-only config import attempted network/auth refresh")
socket.socket.connect = no_network
httpx.Client.send = no_network
home = Path(sys.argv[1])
attempted_writes = []
auth_reads = []
def audit(event, args):
    path = args[0] if args else None
    if not isinstance(path, (str, bytes)):
        return
    try:
        inside = Path(os.fsdecode(path)).absolute().is_relative_to(home.absolute())
    except (ValueError, TypeError):
        return
    if not inside:
        return
    if event == 'open' and Path(os.fsdecode(path)).name == 'auth.json':
        auth_reads.append(event)
        raise AssertionError('Cold config import attempted auth access')
    writing = event in {'os.mkdir', 'os.chmod', 'os.rename', 'os.remove', 'os.rmdir'}
    if event == 'open':
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(x in mode for x in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
    if writing:
        attempted_writes.append(event)
        raise AssertionError('Cold config import attempted a profile write')
sys.addaudithook(audit)
from hermes_cli import config
assert not attempted_writes and not auth_reads and not network_attempts
assert not config._LOAD_CONFIG_CACHE and not config._LAST_EXPANDED_CONFIG_BY_PATH and not config._RAW_CONFIG_CACHE
assert not config._CONFIG_IMPORT_READONLY
if not (home / 'config.yaml').exists():
    assert config.load_config_readonly(strict=True, allow_missing_user=True)['agent']['native_oauth_task_limits']['enabled'] is False
    assert not attempted_writes and not home.exists()
elif sys.argv[2] == "valid":
    assert config.load_config_readonly(strict=True, allow_missing_user=True)["agent"]["native_oauth_task_limits"]["enabled"] is False
else:
    try:
        config.load_config_readonly(strict=True, allow_missing_user=True)
    except (ValueError, yaml.YAMLError):
        pass
    else:
        raise AssertionError("Malformed config was not refused")
    assert not attempted_writes and not auth_reads
assert not network_attempts
print(json.dumps({'profileWrites': len(attempted_writes), 'configCachesEmpty': not config._LOAD_CONFIG_CACHE and not config._LAST_EXPANDED_CONFIG_BY_PATH and not config._RAW_CONFIG_CACHE}))
'''
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    env.update(HERMES_HOME=str(home), HERMES_MANAGED_DIR=str(tmp_path / "absent-managed"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", code, str(home), "valid" if user == "{}\n" else "invalid"], cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"profileWrites": 0, "configCachesEmpty": True}
    after = {str(p.relative_to(tmp_path)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*")}
    assert after == before


def test_import_guard_does_not_relax_existing_explicit_strict_authority(policy_home, monkeypatch):
    monkeypatch.setattr(config, "_CONFIG_IMPORT_READONLY", True)
    with pytest.raises(FileNotFoundError):
        config.load_config_readonly(strict=True)
    # The ordinary recursive provider query is converted to fresh/read-only;
    # explicitly strict callers still retain their original absence refusal.
    assert config.load_config()["agent"]["native_oauth_task_limits"]["enabled"] is False
    assert list(policy_home.iterdir()) == []
    key = str(policy_home / "config.yaml")
    assert key not in config._LOAD_CONFIG_CACHE
    assert key not in config._LAST_EXPANDED_CONFIG_BY_PATH


@pytest.mark.parametrize("broken", ["agent: null", "agent: []", "agent: false", "agent: {native_oauth_task_limits: null}", "agent: {native_oauth_task_limits: []}"])
def test_native_container_validation_is_opt_in_before_null_normalization(policy_home, broken):
    path = policy_home / "config.yaml"
    path.write_text(broken, encoding="utf-8")
    before = path.read_bytes(), path.stat().st_mode
    # The ordinary UI still retains its documented empty-section behavior.
    if "null" in broken:
        assert config.load_config_readonly(strict=True)["agent"]["native_oauth_task_limits"]["enabled"] is False
    with pytest.raises(ValueError, match="mapping"):
        config.load_config_readonly(strict=True, validate_native_containers=True)
    assert (path.read_bytes(), path.stat().st_mode) == before


@pytest.mark.parametrize("broken", ["agent: null", "agent: []", "agent: {native_oauth_task_limits: null}", "agent: {native_oauth_task_limits: false}"])
def test_native_invalid_managed_containers_cannot_disappear_in_merge(policy_home, tmp_path, monkeypatch, broken):
    (policy_home / "config.yaml").write_text("agent: {native_oauth_task_limits: {enabled: false}}\n", encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    (managed / "config.yaml").write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        config.load_config_readonly(strict=True, validate_native_containers=True)
