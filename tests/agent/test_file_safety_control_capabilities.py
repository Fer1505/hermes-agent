from __future__ import annotations

from pathlib import Path

import pytest

from agent import file_safety
from agent.file_safety import (
    ProtectedFileCapability,
    ProtectedFileOperation,
    decide_protected_control_file,
)


@pytest.fixture
def control_roots(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    active = root / "profiles" / "active"
    active.mkdir(parents=True)
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: root)
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: active)
    return root, active


@pytest.mark.parametrize(
    "relative",
    [
        "auth.json",
        "config.yaml",
        "webhook_subscriptions.json",
        "mcp-authorizations.json",
        "auth/google_oauth.json",
        "mcp-tokens/server.json",
        "mcp-installs/server/package.js",
        "pairing/device.json",
    ],
)
def test_model_facing_control_file_mutation_defaults_to_denied(
    control_roots, relative
):
    _root, active = control_roots
    decision = decide_protected_control_file(
        ProtectedFileOperation.WRITE,
        active / relative,
    )
    assert decision.protected is True
    assert decision.allowed is False


def test_nfkc_alias_and_case_are_protected(control_roots):
    _root, active = control_roots
    fullwidth = active / "ＣＯＮＦＩＧ．ＹＡＭＬ"
    assert decide_protected_control_file("write", fullwidth).allowed is False


def test_symlink_alias_to_control_file_is_protected(control_roots, tmp_path):
    _root, active = control_roots
    target = active / "config.yaml"
    target.write_text("model: test\n", encoding="utf-8")
    alias = tmp_path / "innocent.yaml"
    alias.symlink_to(target)

    decision = decide_protected_control_file("read", alias)
    assert decision.protected is True
    assert decision.allowed is False


def test_rename_checks_both_source_and_destination(control_roots, tmp_path):
    _root, active = control_roots
    ordinary = tmp_path / "ordinary.txt"
    destination = active / "auth.json"
    decision = decide_protected_control_file(
        ProtectedFileOperation.RENAME,
        (ordinary, destination),
    )
    assert decision.allowed is False
    assert decision.matched_path == str(destination.resolve())


def test_relative_task_cwd_and_symlink_destination_are_protected(
    control_roots, tmp_path
):
    _root, active = control_roots
    target = active / "config.yaml"
    target.write_text("model: test\n", encoding="utf-8")
    alias = tmp_path / "destination.yaml"
    alias.symlink_to(target)

    relative = decide_protected_control_file(
        "write",
        "config.yaml",
        cwd=str(active),
    )
    renamed = decide_protected_control_file(
        "rename",
        (tmp_path / "ordinary", alias),
    )
    assert relative.allowed is False
    assert renamed.allowed is False


@pytest.mark.parametrize("relative", ["auth", "cache"])
def test_mutating_control_file_ancestor_directory_is_protected(
    control_roots, relative
):
    _root, active = control_roots
    assert decide_protected_control_file("delete", active / relative).allowed is False


def test_mutating_profiles_container_is_protected(control_roots):
    root, _active = control_roots
    assert decide_protected_control_file("rename", root / "profiles").allowed is False


@pytest.mark.parametrize(
    "operation",
    [
        ProtectedFileOperation.RENAME,
        ProtectedFileOperation.DELETE,
        ProtectedFileOperation.ARCHIVE,
        ProtectedFileOperation.IMPORT,
    ],
)
def test_profile_lifecycle_capability_is_narrowly_authorized(
    control_roots, operation
):
    _root, active = control_roots
    assert decide_protected_control_file(
        operation,
        active,
        capability=ProtectedFileCapability.PROFILE_LIFECYCLE,
    ).allowed is True
    assert decide_protected_control_file(
        ProtectedFileOperation.WRITE,
        active / "config.yaml",
        capability=ProtectedFileCapability.PROFILE_LIFECYCLE,
    ).allowed is False


def test_mcp_registration_capability_only_allows_internal_read_write(control_roots):
    _root, active = control_roots
    path = active / "mcp-authorizations.json"
    assert decide_protected_control_file(
        "write",
        path,
        capability=ProtectedFileCapability.MCP_REGISTRATION,
    ).allowed is True
    assert decide_protected_control_file(
        "delete",
        path,
        capability=ProtectedFileCapability.MCP_REGISTRATION,
    ).allowed is False


def test_backup_restore_capability_only_allows_archive_and_import(control_roots):
    root, _active = control_roots
    for operation in ("archive", "import"):
        assert decide_protected_control_file(
            operation,
            root,
            capability=ProtectedFileCapability.BACKUP_RESTORE,
        ).allowed is True
    assert decide_protected_control_file(
        "write",
        root / "config.yaml",
        capability=ProtectedFileCapability.BACKUP_RESTORE,
    ).allowed is False
