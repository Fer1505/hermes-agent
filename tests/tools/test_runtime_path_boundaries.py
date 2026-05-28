import json

from agent.file_safety import (
    get_path_boundary_error,
    get_writable_surfaces,
    get_workspace_roots,
    is_write_denied,
)


def test_workspace_root_config_gates_reads_and_falls_back_for_writes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("nope", encoding="utf-8")

    config = {"runtime": {"workspaceRoot": str(workspace)}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)

    assert get_workspace_roots() == [str(workspace.resolve())]
    assert get_writable_surfaces() == [str(workspace.resolve())]
    assert get_path_boundary_error(str(workspace / "ok.txt"), purpose="read") is None
    assert get_path_boundary_error(str(outside), purpose="read")
    assert is_write_denied(str(workspace / "ok.txt")) is False
    assert is_write_denied(str(outside)) is True


def test_writable_surfaces_support_plural_env_roots(tmp_path, monkeypatch):
    first = tmp_path / "one"
    second = tmp_path / "two"
    outside = tmp_path / "three" / "file.txt"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOTS", json.dumps([str(first), str(second)]))

    assert is_write_denied(str(first / "a.txt")) is False
    assert is_write_denied(str(second / "b.txt")) is False
    assert is_write_denied(str(outside)) is True


def test_file_tool_blocks_read_outside_workspace_before_environment_creation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"runtime": {"workspaceRoot": str(workspace)}},
    )

    from tools.file_tools import read_file_tool

    result = json.loads(read_file_tool(str(outside)))
    assert "Path boundary denied for read" in result["error"]


def test_file_tool_blocks_write_outside_writable_surface_before_environment_creation(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(workspace))

    from tools.file_tools import write_file_tool

    result = json.loads(write_file_tool(str(outside), "x"))
    assert "Path boundary denied for write" in result["error"]


def test_terminal_blocks_workdir_outside_writable_surface(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(workspace))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))

    from tools import terminal_tool

    result = json.loads(terminal_tool.terminal_tool("pwd", workdir=str(outside)))
    assert result["status"] == "blocked"
    assert "Path boundary denied for workdir" in result["error"]
