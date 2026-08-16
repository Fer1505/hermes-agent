from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tools.environments import base


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_sandbox_root_is_created_owner_private_even_under_permissive_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "profile"
    monkeypatch.setattr(base, "get_hermes_home", lambda: hermes_home)
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
    previous_umask = os.umask(0)
    try:
        sandbox = base.get_sandbox_dir()
    finally:
        os.umask(previous_umask)

    assert sandbox == hermes_home / "sandboxes"
    assert _mode(sandbox) == 0o700


def test_existing_permissive_directory_and_children_are_corrected(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir(mode=0o755)
    os.chmod(sandbox, 0o755)
    child = sandbox / "docker" / "task" / "workspace"

    assert base.ensure_private_directory(sandbox) == sandbox
    assert base.ensure_private_directory(sandbox / "docker") == sandbox / "docker"
    assert base.ensure_private_directory(sandbox / "docker" / "task") == sandbox / "docker" / "task"
    assert base.ensure_private_directory(child) == child
    assert _mode(sandbox) == 0o700
    assert _mode(sandbox / "docker") == 0o700
    assert _mode(sandbox / "docker" / "task") == 0o700
    assert _mode(child) == 0o700


def test_symlinked_sandbox_leaf_is_rejected_without_changing_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    link = tmp_path / "sandboxes"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlinked sandbox directory"):
        base.ensure_private_directory(link)

    assert _mode(target) == 0o755


def test_foreign_owned_directory_fails_before_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir(mode=0o755)
    real_fstat = os.fstat
    fchmod_calls: list[int] = []

    def foreign_fstat(descriptor: int):
        observed = real_fstat(descriptor)
        return type(
            "ForeignStat",
            (),
            {"st_mode": observed.st_mode, "st_uid": os.geteuid() + 1},
        )()

    monkeypatch.setattr(base.os, "fstat", foreign_fstat)
    monkeypatch.setattr(base.os, "fchmod", lambda _descriptor, mode: fchmod_calls.append(mode))

    with pytest.raises(RuntimeError, match="not owned by the current user"):
        base.ensure_private_directory(sandbox)

    assert fchmod_calls == []
