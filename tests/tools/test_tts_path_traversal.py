"""Regression: text_to_speech_tool output_path must reject '..' traversal.

The TTS surface accepts agent/user-supplied absolute paths (writing to a
chosen file is the whole point). What it must reject is paths that use
``..`` components to escape their declared base — those are almost
always either a bug or prompt-injection-controlled
(e.g. ``output_path="audio/../../etc/cron.d/x"``).
"""

import json
import stat
import sys
from pathlib import Path

from tools import tts_tool
from tools.tts_tool import text_to_speech_tool


def _python_copy_command() -> str:
    return (
        f'"{sys.executable}" -c "import shutil, sys; '
        'shutil.copyfile(sys.argv[1], sys.argv[2])" '
        "{input_path} {output_path}"
    )


def test_output_path_rejects_traversal_escape():
    """A path with '..' components must be rejected before any provider work."""
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path="audio/../../etc/cron.d/malicious",
    ))
    assert result["success"] is False
    assert "traversal" in result["error"].lower()


def test_output_path_rejects_bare_dotdot():
    """Bare '..' prefix must be rejected."""
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path="../escape.mp3",
    ))
    assert result["success"] is False
    assert "traversal" in result["error"].lower()


def test_output_path_rejects_hermes_oauth_store(tmp_path, monkeypatch):
    """TTS output_path must not bypass the shared protected-file write guard."""
    import agent.file_safety as file_safety

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: hermes_home)
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: hermes_home)

    target = hermes_home / ".anthropic_oauth.json"
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path=str(target),
    ))

    assert result["success"] is False
    assert "protected credential" in result["error"]
    assert not target.exists()


def test_output_path_rejects_mcp_token_directory(tmp_path, monkeypatch):
    """TTS output_path must not write synthesized audio over MCP token files."""
    import agent.file_safety as file_safety

    hermes_home = tmp_path / "hermes-home"
    token_dir = hermes_home / "mcp-tokens"
    token_dir.mkdir(parents=True)
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: hermes_home)
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: hermes_home)

    target = token_dir / "server.mp3"
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path=str(target),
    ))

    assert result["success"] is False
    assert "protected credential" in result["error"]
    assert not target.exists()


def test_default_audio_cache_remains_writable_with_narrow_workspace_root(
    tmp_path: Path,
    monkeypatch,
):
    """Application-owned default audio is not an arbitrary agent write."""
    hermes_home = tmp_path / "profile"
    audio_cache = hermes_home / "cache" / "audio"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import agent.file_safety as file_safety
    monkeypatch.setattr(
        file_safety,
        "_load_runtime_boundary_config",
        lambda: {"runtime": {"writableSurfaces": [{"path": str(workspace)}]}},
    )
    monkeypatch.setattr(
        tts_tool,
        "_load_tts_config",
        lambda: {
            "provider": "local-test",
            "providers": {
                "local-test": {
                    "type": "command",
                    "command": _python_copy_command(),
                    "output_format": "mp3",
                    "max_text_length": 5,
                }
            },
        },
    )

    result = json.loads(text_to_speech_tool(text="hello world from Hermes"))

    assert result["success"] is True
    assert result["chunk_count"] > 1
    assert Path(result["file_path"]).is_file()
    assert Path(result["file_path"]).resolve().is_relative_to(audio_cache.resolve())
    assert stat.S_IMODE(audio_cache.stat().st_mode) == 0o700


def test_caller_selected_audio_cache_path_still_honors_narrow_workspace_root(
    tmp_path: Path,
    monkeypatch,
):
    """Only the omitted application default receives the cache exception."""
    hermes_home = tmp_path / "profile"
    audio_cache = hermes_home / "cache" / "audio"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import agent.file_safety as file_safety
    monkeypatch.setattr(
        file_safety,
        "_load_runtime_boundary_config",
        lambda: {"runtime": {"writableSurfaces": [{"path": str(workspace)}]}},
    )

    target = audio_cache / "caller-selected.mp3"
    result = json.loads(text_to_speech_tool(text="hello", output_path=str(target)))

    assert result["success"] is False
    assert "protected credential" in result["error"]
    assert not target.exists()


def test_default_audio_cache_rejects_outside_home_symlink(tmp_path: Path, monkeypatch):
    hermes_home = tmp_path / "profile"
    cache = hermes_home / "cache"
    outside = tmp_path / "outside"
    cache.mkdir(parents=True)
    outside.mkdir()
    (cache / "audio").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = json.loads(text_to_speech_tool(text="hello"))

    assert result["success"] is False
    assert "default audio cache" in result["error"].lower()
    assert list(outside.iterdir()) == []


def test_default_audio_cache_rejects_inside_home_protected_symlink(
    tmp_path: Path,
    monkeypatch,
):
    hermes_home = tmp_path / "profile"
    cache = hermes_home / "cache"
    protected = hermes_home / "mcp-tokens"
    cache.mkdir(parents=True)
    protected.mkdir()
    (cache / "audio").symlink_to(protected, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = json.loads(text_to_speech_tool(text="hello"))

    assert result["success"] is False
    assert "default audio cache" in result["error"].lower()
    assert list(protected.iterdir()) == []


def test_internal_output_flag_cannot_bypass_protected_control_path(
    tmp_path: Path,
    monkeypatch,
):
    hermes_home = tmp_path / "profile"
    token_dir = hermes_home / "mcp-tokens"
    token_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = json.loads(
        tts_tool._text_to_speech_single(
            text="hello",
            output_path=str(token_dir / "voice.mp3"),
            _application_owned_output=True,
        )
    )

    assert result["success"] is False
    assert "protected credential" in result["error"]
