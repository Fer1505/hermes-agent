"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest
import yaml

_mcp_server_mod = pytest.importorskip("mcp.server")

if not hasattr(_mcp_server_mod, "MCPServer"):
    # `mcp.server.MCPServer` replaced `mcp.server.fastmcp.FastMCP` in mcp 2.0.
    # Skip rather than fail on a FastMCP-era SDK: the probe below is written
    # against the 2.x API, and the pinned version provides it.
    pytest.skip(
        "profile-local MCP discovery probe requires mcp >= 2.0 (MCPServer)",
        allow_module_level=True,
    )


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    server = tmp_path / "mcp_probe.py"
    # Ephemeral-port HTTP probe (NOT stdio like upstream): the Olympus fork
    # gates stdio MCP servers behind operator authorization, so this test
    # exercises discovery through the ungated streamable-http path.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server import MCPServer

            mcp = MCPServer("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                mcp.run(transport="streamable-http", host="127.0.0.1", port={port})
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "profileprobe": {
                        "enabled": True,
                        "url": f"http://127.0.0.1:{port}/mcp",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    mcp_proc = subprocess.Popen(
        [sys.executable, str(server)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if mcp_proc.poll() is not None:
                pytest.fail("profile-local MCP probe server exited during startup")
            if time.monotonic() >= deadline:
                pytest.fail("profile-local MCP probe server did not start")
            time.sleep(0.05)

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "0"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "0.05"
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "tui_gateway.slash_worker",
                "--session-key",
                "agent:main:tui:dm:mcp-profile-test",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=tmp_path,
        )
        output: queue.Queue[str] = queue.Queue()
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None
            stdout = proc.stdout
            threading.Thread(
                target=lambda: output.put(stdout.readline()),
                daemon=True,
            ).start()
            proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
            proc.stdin.flush()
            try:
                line = output.get(timeout=10)
            except queue.Empty:
                pytest.fail("slash worker produced no /tools response within 10 seconds")
            response = json.loads(line)
            assert response["ok"] is True
            assert "mcp__profileprobe__hermes_61922_profile_probe" in response["output"]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        mcp_proc.terminate()
        try:
            mcp_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mcp_proc.kill()
            mcp_proc.wait(timeout=5)
