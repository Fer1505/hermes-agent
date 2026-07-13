"""Adversarial fixtures for the descriptor-anchored import transaction."""

import hashlib
import json
import os
import stat
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest


def _write_backup(path: Path) -> None:
    files = {
        "config.yaml": b"model: restored\n",
        "sessions/one.json": b'{"safe": true}\n',
    }
    members = [
        {
            "path": name,
            "scope": "hermes_home",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": 0o600,
        }
        for name, data in files.items()
    ]
    manifest = {
        "format": "hermes-backup",
        "version": 1,
        "createdAt": "2026-07-13T00:00:00+00:00",
        "members": members,
        "externalRoots": [],
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
        archive.writestr("_hermes_backup_manifest.json", json.dumps(manifest))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configure(monkeypatch, hermes_home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor semantics")
def test_symlinked_restore_ancestor_fails_closed(tmp_path, monkeypatch, capsys):
    import hermes_cli.backup as backup_mod

    selected_parent = tmp_path / "selected-parent"
    selected_parent.mkdir()
    hermes_home = selected_parent / ".hermes"
    hermes_home.mkdir()
    moved_parent = tmp_path / "moved-parent"
    sentinel = selected_parent / "sentinel"
    sentinel.write_text("do not touch\n")
    before = _digest(sentinel)
    archive = tmp_path / "backup.zip"
    _write_backup(archive)
    _configure(monkeypatch, hermes_home)
    real_build_plan = backup_mod._build_restore_plan

    def replace_ancestor_after_prevalidation(*args, **kwargs):
        result = real_build_plan(*args, **kwargs)
        selected_parent.rename(moved_parent)
        selected_parent.symlink_to(moved_parent, target_is_directory=True)
        return result

    monkeypatch.setattr(
        backup_mod, "_build_restore_plan", replace_ancestor_after_prevalidation
    )

    with pytest.raises(SystemExit):
        backup_mod.run_import(Namespace(zipfile=str(archive), force=True))

    assert _digest(sentinel) == before
    assert not (moved_parent / ".hermes" / "config.yaml").exists()
    assert "stable real directory" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor semantics")
def test_ancestor_identity_swap_rolls_back_inside_pinned_parent(
    tmp_path, monkeypatch, capsys
):
    import hermes_cli.backup as backup_mod

    selected_parent = tmp_path / "selected"
    selected_parent.mkdir()
    hermes_home = selected_parent / ".hermes"
    hermes_home.mkdir()
    moved_parent = tmp_path / "selected-moved"
    archive = tmp_path / "backup.zip"
    _write_backup(archive)
    _configure(monkeypatch, hermes_home)
    real_exchange = backup_mod._exchange_directories_at
    swapped = False

    def swap_ancestor_then_exchange(source_fd, source, target_fd, target):
        nonlocal swapped
        if not swapped:
            swapped = True
            selected_parent.rename(moved_parent)
            selected_parent.mkdir()
            (selected_parent / "attacker-sentinel").write_text("unchanged\n")
        return real_exchange(source_fd, source, target_fd, target)

    monkeypatch.setattr(
        backup_mod, "_exchange_directories_at", swap_ancestor_then_exchange
    )
    with pytest.raises(SystemExit):
        backup_mod.run_import(Namespace(zipfile=str(archive), force=True))

    assert (selected_parent / "attacker-sentinel").read_text() == "unchanged\n"
    assert not (selected_parent / ".hermes").exists()
    assert (moved_parent / ".hermes").is_dir()
    assert list((moved_parent / ".hermes").iterdir()) == []
    assert not list(moved_parent.glob(".hermes-import-*"))
    assert "empty-target state was restored" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor semantics")
def test_target_substitution_during_exchange_is_detected_without_touching_attacker(
    tmp_path, monkeypatch, capsys
):
    import hermes_cli.backup as backup_mod

    hermes_home = tmp_path / "target"
    hermes_home.mkdir()
    displaced_original = tmp_path / "target-original"
    archive = tmp_path / "backup.zip"
    _write_backup(archive)
    _configure(monkeypatch, hermes_home)
    real_exchange = backup_mod._exchange_directories_at
    raced = False

    def substitute_then_exchange(source_fd, source, target_fd, target):
        nonlocal raced
        if not raced:
            raced = True
            hermes_home.rename(displaced_original)
            hermes_home.mkdir()
            (hermes_home / "attacker-sentinel").write_text("unchanged\n")
        return real_exchange(source_fd, source, target_fd, target)

    monkeypatch.setattr(
        backup_mod, "_exchange_directories_at", substitute_then_exchange
    )
    with pytest.raises(SystemExit):
        backup_mod.run_import(Namespace(zipfile=str(archive), force=True))

    assert (hermes_home / "attacker-sentinel").read_text() == "unchanged\n"
    assert not (hermes_home / "config.yaml").exists()
    assert list(displaced_original.iterdir()) == []
    assert "rollback could not be confirmed" in capsys.readouterr().out


def test_missing_secure_primitive_fails_before_staging(tmp_path, monkeypatch, capsys):
    import hermes_cli.backup as backup_mod

    hermes_home = tmp_path / "target"
    hermes_home.mkdir()
    archive = tmp_path / "backup.zip"
    _write_backup(archive)
    _configure(monkeypatch, hermes_home)

    def unsupported():
        raise backup_mod.BackupError("atomic directory exchange is unsupported")

    monkeypatch.setattr(backup_mod, "_require_secure_restore_primitives", unsupported)
    with pytest.raises(SystemExit):
        backup_mod.run_import(Namespace(zipfile=str(archive), force=True))

    assert list(hermes_home.iterdir()) == []
    assert not list(tmp_path.glob(".hermes-import-*"))
    assert "unsupported" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor semantics")
def test_post_exchange_failure_rolls_back_and_preserves_live_hashes(
    tmp_path, monkeypatch, capsys
):
    import hermes_cli.backup as backup_mod

    hermes_home = tmp_path / "target"
    hermes_home.mkdir()
    live = tmp_path / "live-state.json"
    live.write_text('{"durable": true}\n')
    live_before = _digest(live)
    archive = tmp_path / "backup.zip"
    _write_backup(archive)
    _configure(monkeypatch, hermes_home)
    real_verify = backup_mod._verify_promoted_target

    def fail_after_verified_exchange(*args, **kwargs):
        real_verify(*args, **kwargs)
        raise backup_mod.BackupError("injected post-exchange failure")

    monkeypatch.setattr(
        backup_mod, "_verify_promoted_target", fail_after_verified_exchange
    )
    with pytest.raises(SystemExit):
        backup_mod.run_import(Namespace(zipfile=str(archive), force=True))

    assert _digest(live) == live_before
    assert hermes_home.is_dir()
    assert list(hermes_home.iterdir()) == []
    assert not list(tmp_path.glob(".hermes-import-*"))
    output = capsys.readouterr().out
    assert "empty-target state was restored" in output
    assert "Import complete" not in output
