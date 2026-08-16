"""Explicit portable MCP authorization lifecycle at plugin CLI seams."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

import pytest
import yaml


def _installed_portable(home: Path) -> Path:
    from hermes_cli.agent_plugins import MCP_SCHEMA_V1, PLUGIN_SCHEMA_V1

    root = home / "plugins" / "portable.test"
    root.mkdir(parents=True)
    # Root-of-repository installs retain .git and are discovered as source
    # kind "git" rather than "user".
    (root / ".git").mkdir()
    executable = root / "bin" / "worker"
    executable.parent.mkdir()
    shutil.copy2(sys.executable, executable)
    executable.chmod(0o700)
    manifest = {
        "$schema": PLUGIN_SCHEMA_V1,
        "name": "portable.test",
        "version": "1.0.0",
    }
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "mcp.json").write_text(
        json.dumps({
            "$schema": MCP_SCHEMA_V1,
            "mcpServers": {
                "worker": {"type": "stdio", "command": "./bin/worker"},
            },
        }),
        encoding="utf-8",
    )
    (root.parent / ".install-metadata.json").write_text(
        json.dumps({
            root.name: {
                "source": "https://example.test/portable.git",
                "revision": "a" * 40,
                "pinned": False,
            }
        }),
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [], "disabled": []}}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def portable_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = _installed_portable(home)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    import hermes_cli.config as config_mod

    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    return home, root


def _portable_receipts(home: Path) -> list[dict]:
    path = home / "mcp-authorizations.json"
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    return [
        receipt for receipt in value.get("servers", {}).values()
        if receipt.get("authorization") == "portable_plugin"
    ]


def test_cmd_enable_issues_and_reenable_refreshes_receipt(portable_home):
    from hermes_cli.plugins_cmd import cmd_enable

    home, root = portable_home
    cmd_enable("portable.test", allow_tool_override=False)
    [first] = _portable_receipts(home)
    assert first["plugin_key"] == "portable.test"
    assert first["plugin_root"] == str(root.resolve())

    cmd_enable("portable.test", allow_tool_override=False)
    [second] = _portable_receipts(home)
    assert second["receipt_id"] != first["receipt_id"]


def test_cmd_enable_rolls_back_receipt_when_config_save_fails(
    portable_home, monkeypatch
):
    import hermes_cli.plugins_cmd as plugins_cmd

    home, _ = portable_home
    monkeypatch.setattr(
        plugins_cmd,
        "_save_enabled_set",
        lambda _enabled: (_ for _ in ()).throw(RuntimeError("save failed")),
    )
    with pytest.raises(RuntimeError, match="save failed"):
        plugins_cmd.cmd_enable("portable.test", allow_tool_override=False)
    assert _portable_receipts(home) == []


def test_install_with_enable_issues_and_force_reinstall_revokes(
    portable_home, monkeypatch
):
    import hermes_cli.plugins_cmd as plugins_cmd

    home, root = portable_home
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(plugins_cmd, "_looks_like_bare_index_name", lambda _value: False)
    monkeypatch.setattr(
        plugins_cmd, "_resolve_git_url", lambda _value: ("https://example.test/p.git", None)
    )
    monkeypatch.setattr(
        plugins_cmd,
        "_install_plugin_core",
        lambda *_args, **_kwargs: (root, manifest, "portable.test"),
    )
    monkeypatch.setattr(plugins_cmd, "_display_after_install", lambda *_args: None)

    plugins_cmd.cmd_install("owner/repo", enable=True)
    assert len(_portable_receipts(home)) == 1
    plugins_cmd.cmd_install("owner/repo", force=True, enable=False)
    assert _portable_receipts(home) == []


def test_disable_remove_and_update_revoke_portable_receipts(
    portable_home, monkeypatch
):
    import hermes_cli.plugins_cmd as plugins_cmd

    home, root = portable_home

    plugins_cmd.cmd_enable("portable.test", allow_tool_override=False)
    plugins_cmd.cmd_disable("portable.test")
    assert _portable_receipts(home) == []

    plugins_cmd.cmd_enable("portable.test", allow_tool_override=False)
    monkeypatch.setattr(plugins_cmd, "_git_pull_plugin_dir", lambda _root: (True, "updated"))
    monkeypatch.setattr(plugins_cmd, "_resolve_git_executable", lambda: "/usr/bin/git")
    monkeypatch.setattr(plugins_cmd, "_git_head_revision", lambda *_args: "b" * 40)
    plugins_cmd.cmd_update("portable.test")
    assert _portable_receipts(home) == []

    from hermes_cli.mcp_security import authorize_portable_plugin_stdio_entries
    from hermes_cli.plugins import _portable_skill_namespace

    authorize_portable_plugin_stdio_entries(
        "portable.test",
        root,
        home / "plugin-data" / _portable_skill_namespace("portable.test"),
    )
    plugins_cmd.cmd_remove("portable.test")
    assert _portable_receipts(home) == []
    assert not root.exists()


def test_native_manifest_precedence_does_not_require_portable_receipt(
    tmp_path, monkeypatch
):
    from hermes_cli.agent_plugins import PLUGIN_SCHEMA_V1
    from hermes_cli.plugins_cmd import cmd_enable

    home = tmp_path / "home"
    root = home / "plugins" / "mixed"
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "mixed", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (root / "plugin.json").write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_V1, "name": "ignored.portable"}),
        encoding="utf-8",
    )
    (root / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [], "disabled": []}}),
        encoding="utf-8",
    )
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    import hermes_cli.config as config_mod

    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()

    cmd_enable("mixed", allow_tool_override=False)

    assert _portable_receipts(home) == []
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert "mixed" in config["plugins"]["enabled"]
