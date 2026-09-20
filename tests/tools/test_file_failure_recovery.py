"""Failed reads must not become evidence of observed file contents."""
import json
import subprocess
from pathlib import Path

import pytest

from tools import file_state, file_tools as ft
from tools.file_operations import ReadResult, ShellFileOperations


@pytest.fixture
def files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(workspace))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.delenv("HERMES_WORKSPACE_ROOT", raising=False)
    config = {"runtime": {"workspaceRoots": [str(workspace)]}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    class Env:
        cwd = str(workspace)
        calls = 0
        def execute(self, command, cwd=None, **kwargs):
            self.calls += 1
            proc = subprocess.run(["bash", "-c", command], cwd=cwd or self.cwd,
                                  input=kwargs.get("stdin_data"), text=True,
                                  capture_output=True, timeout=10)
            return {"output": proc.stdout + proc.stderr, "returncode": proc.returncode}
    env = Env()
    ops = ShellFileOperations(env)
    monkeypatch.setattr(ft, "_get_file_ops", lambda _: ops)
    # Same root contract as the terminal registry, backed by this real shell.
    monkeypatch.setattr(ft, "_authoritative_workspace_root", lambda _: env.cwd)
    monkeypatch.setattr(ft, "_uses_container_paths", lambda _: False)
    monkeypatch.setattr(ft, "_file_ops_uses_host_paths", lambda _: True)
    task = "file-recovery-" + tmp_path.name
    yield workspace, ops, env, task, config
    ft._read_tracker.pop(task, None)


def test_created_after_miss_is_read_instead_of_reported_unchanged(files, monkeypatch):
    root, ops, env, task, _ = files
    real_read = ops.read_file
    def read_then_create(*args):
        result = real_read(*args)
        (root / "receipt.md").write_text("new verified fixture content\n")
        return result
    monkeypatch.setattr(ops, "read_file", read_then_create)
    first = json.loads(ft.read_file_tool("receipt.md", task_id=task))
    assert "File not found" in first["error"]
    assert str(root / "receipt.md") not in file_state.known_reads(task)
    monkeypatch.setattr(ops, "read_file", real_read)
    second = json.loads(ft.read_file_tool("receipt.md", task_id=task))
    assert "new verified fixture content" in second["content"]
    assert second.get("status") != "unchanged"


def test_failed_existing_file_never_grants_read_before_write_or_suppresses_retry(files, monkeypatch):
    from tools import skill_manager_tool as sm
    root, ops, env, task, _ = files
    target = root / "SKILL.md"
    target.write_text("# Fixture instruction\n")
    monkeypatch.setattr("tools.skill_provenance.is_background_review", lambda: True)
    sm._reset_background_review_read_marks()
    real_read = ops.read_file
    monkeypatch.setattr(ops, "read_file", lambda *args: ReadResult(error="Backend temporarily unavailable"))
    try:
        for _ in range(4):
            failed = json.loads(ft.read_file_tool("SKILL.md", task_id=task))
            assert failed["error"] == "Backend temporarily unavailable"
        assert str(target) not in file_state.known_reads(task)
        assert not sm._background_review_has_read(target)
        monkeypatch.setattr(ops, "read_file", real_read)
        success = json.loads(ft.read_file_tool("SKILL.md", task_id=task))
        assert "Fixture instruction" in success["content"]
        assert str(target) in file_state.known_reads(task)
        assert sm._background_review_has_read(target)
    finally:
        sm._reset_background_review_read_marks()


def test_missing_relative_path_reports_resolved_location_and_reuses_safe_cache(files):
    root, ops, env, task, _ = files
    (root / "report-notes.md").write_text("synthetic notes\n")
    first = json.loads(ft.read_file_tool("report.md", task_id=task))
    count = env.calls
    second = json.loads(ft.read_file_tool("report.md", task_id=task))
    assert env.calls == count
    assert first == second
    assert first["resolved_path"] == str(root / "report.md")
    assert first["path_status"] == "not_found"
    assert first["similar_files"] == [str(root / "report-notes.md")]
    recovered = json.loads(ft.read_file_tool(first["similar_files"][0], task_id=task))
    assert "synthetic notes" in recovered["content"]


def test_suggestions_do_not_offer_protected_or_outside_symlink_targets(files):
    root, ops, env, task, _ = files
    (root / "auth.json").write_text('{"fixture": "not a real credential"}')
    first = json.loads(ft.read_file_tool("auth.jsn", task_id=task))
    assert str(root / "auth.json") not in first.get("similar_files", [])
    outside = root.parent / "outside.md"
    outside.write_text("outside workspace fixture")
    (root / "report-private.md").symlink_to(outside)
    (root / "report-notes.md").write_text("allowed notes")
    second = json.loads(ft.read_file_tool("report.md", task_id=task))
    assert second["similar_files"] == [str(root / "report-notes.md")]


def test_cached_suggestions_are_rechecked_after_scope_changes(files):
    root, ops, env, task, config = files
    (root / "report-notes.md").write_text("fixture")
    first = json.loads(ft.read_file_tool("report.md", task_id=task))
    assert first.get("similar_files")
    config["runtime"]["workspaceRoots"] = [str(root / "report.md")]
    second = json.loads(ft.read_file_tool("report.md", task_id=task))
    assert not second.get("similar_files")


def test_denied_target_is_still_denied_before_suggestion_scan(files):
    root, ops, env, task, _ = files
    result = json.loads(ft.read_file_tool(str(root.parent / "missing.md"), task_id=task))
    assert "Path boundary denied" in result["error"]
    assert env.calls == 0
    assert "similar_files" not in result


def test_read_uses_guarded_task_path_even_when_shared_backend_cwd_differs(files, monkeypatch):
    root, ops, env, task, _ = files
    other = root.parent / "other-conversation"
    other.mkdir()
    (root / "receipt.md").write_text("intended task fixture")
    (other / "receipt.md").write_text("foreign task fixture")
    env.cwd = str(other)
    monkeypatch.setattr(ft, "_authoritative_workspace_root", lambda _: str(root))
    result = json.loads(ft.read_file_tool("receipt.md", task_id=task))
    assert "intended task fixture" in result["content"]
    assert "foreign task fixture" not in result["content"]
