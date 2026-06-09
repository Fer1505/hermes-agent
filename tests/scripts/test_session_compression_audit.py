import importlib.util
import builtins
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_state import SessionDB


def _load_module():
    script = Path(__file__).parents[2] / "scripts" / "session_compression_audit.py"
    spec = importlib.util.spec_from_file_location("session_compression_audit", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_profile_config(profile_dir: Path, *, threshold: float = 0.5, context_length: int = 200_000):
    (profile_dir / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  default: test-model",
                f"  context_length: {context_length}",
                "compression:",
                f"  threshold: {threshold}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_sessions_index(profile_dir: Path, entries: dict):
    sessions_dir = profile_dir / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sessions.json").write_text(json.dumps(entries), encoding="utf-8")


def test_audit_profile_flags_stale_compression_parent_index(tmp_path):
    audit = _load_module()
    profile = tmp_path / "olympus-hermes"
    profile.mkdir()
    _write_profile_config(profile)

    db = SessionDB(db_path=profile / "state.db")
    db.create_session("compressed_parent", "telegram")
    db.end_session("compressed_parent", "compression")
    db.create_session("compressed_child", "telegram", parent_session_id="compressed_parent")
    db.append_message("compressed_child", "user", "live continuation")
    db.close()

    _write_sessions_index(
        profile,
        {
            "agent:main:telegram:dm:fernando": {
                "session_id": "compressed_parent",
                "last_prompt_tokens": 0,
            }
        },
    )

    result = audit.audit_profile(profile)

    assert result["checked_entries"] == 1
    assert result["issues"][0]["severity"] == "critical"
    assert result["issues"][0]["kind"] == "stale_compression_index"
    assert result["issues"][0]["tip_session_id"] == "compressed_child"


def test_audit_profile_warns_when_lane_nears_threshold(tmp_path):
    audit = _load_module()
    profile = tmp_path / "themis"
    profile.mkdir()
    _write_profile_config(profile, threshold=0.5, context_length=200_000)

    db = SessionDB(db_path=profile / "state.db")
    db.create_session("active_session", "telegram")
    db.append_message("active_session", "user", "hello")
    db.close()

    _write_sessions_index(
        profile,
        {
            "agent:main:telegram:dm:fernando": {
                "session_id": "active_session",
                "last_prompt_tokens": 85_000,
            }
        },
    )

    result = audit.audit_profile(profile, warn_token_ratio=0.80)

    assert [issue["kind"] for issue in result["issues"]] == [
        "active_lane_near_compression_threshold"
    ]
    assert result["issues"][0]["severity"] == "warning"


def test_audit_profile_ignores_missing_seed_rows_by_default(tmp_path):
    audit = _load_module()
    profile = tmp_path / "athena"
    profile.mkdir()
    _write_profile_config(profile)
    db = SessionDB(db_path=profile / "state.db")
    db.close()
    _write_sessions_index(
        profile,
        {"agent:main:telegram:dm:seed": {"session_id": "seed_dm_123"}},
    )

    result = audit.audit_profile(profile)

    assert result["issues"] == []


def test_cli_resolves_threshold_when_run_from_outside_repo(tmp_path):
    repo_root = Path(__file__).parents[2]
    script = repo_root / "scripts" / "session_compression_audit.py"
    profile = tmp_path / "profiles" / "olympus-hermes"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: openai-codex",
                "  default: gpt-5.5",
                "  base_url: https://chatgpt.com/backend-api/codex",
                "compression:",
                "  threshold: 0.72",
                "",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--profiles-root",
            str(tmp_path / "profiles"),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(proc.stdout)

    assert payload["profiles"][0]["threshold_tokens"] == 195_840


def test_threshold_uses_static_fallback_when_model_metadata_import_fails(tmp_path, monkeypatch):
    audit = _load_module()
    profile = tmp_path / "olympus-hermes"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: openai-codex",
                "  default: gpt-5.5",
                "compression:",
                "  threshold: 0.72",
                "",
            ]
        ),
        encoding="utf-8",
    )
    real_import = builtins.__import__

    def blocked_model_metadata_import(name, *args, **kwargs):
        if name == "agent.model_metadata":
            raise ModuleNotFoundError("No module named 'requests'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_model_metadata_import)

    assert audit._compression_threshold_tokens(profile) == 195_840


def test_yaml_fallback_keeps_top_level_model_when_lists_have_model_keys(tmp_path, monkeypatch):
    audit = _load_module()
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "model:",
                "  provider: openai-codex",
                "  default: gpt-5.5",
                "fallback_providers:",
                "- provider: custom",
                "  model: gemma4:e4b",
                "compression:",
                "  threshold: 0.72",
                "",
            ]
        ),
        encoding="utf-8",
    )
    real_import = builtins.__import__

    def blocked_yaml_import(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_yaml_import)

    parsed = audit._load_yaml_mapping(config)

    assert parsed["model"]["default"] == "gpt-5.5"
    assert parsed["compression"]["threshold"] == "0.72"
