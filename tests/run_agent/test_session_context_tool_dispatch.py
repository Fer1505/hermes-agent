"""Local HTTP -> real agent loop -> registry -> terminal context acceptance.

Only model responses and provider construction are scripted. Terminal commands
run in temporary test homes and print synthetic conversation identifiers.
"""
import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import gateway.session_context as sc
from tests.gateway.test_api_server_multimodal import _make_adapter, _create_app
from tests.run_agent.test_tool_call_guardrail_runtime import (
    _make_tool_defs, _mock_response, _mock_tool_call,
)


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch, tmp_path):
    tokens = [var.set(sc._UNSET) for var in sc._VAR_MAP.values()]
    bound = sc._session_context_bound.set(False)
    monkeypatch.setattr(sc, "_session_context_engaged", False)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-process-mirror")
    yield
    for var, token in zip(sc._VAR_MAP.values(), tokens):
        var.reset(token)
    sc._session_context_bound.reset(bound)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("request_count,resume", [(1, False), (2, False), (2, True)])
async def test_http_agent_terminal_keeps_request_identity(monkeypatch, tmp_path, endpoint, request_count, resume):
    import run_agent
    from hermes_state import SessionDB
    from tools.registry import registry
    import tools.terminal_tool  # register the actual terminal handler

    monkeypatch.setattr(run_agent, "OpenAI", lambda *a, **k: MagicMock())
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda *a, **k: _make_tool_defs("terminal"))
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda *a, **k: {})
    observations = []
    tool_results = []
    agents = []
    barrier = threading.Barrier(1 if resume else request_count)
    db = SessionDB(db_path=tmp_path / "identity.db")
    entry = registry.get_entry("terminal")
    original_handler = entry.handler

    def observed_handler(args, **kwargs):
        observation = {
            "context_id": sc.get_session_env("HERMES_SESSION_ID"),
            "argument_id": kwargs.get("session_id"),
            "platform": sc.get_session_env("HERMES_SESSION_PLATFORM"),
            "missing": sc.session_context_missing(),
        }
        observations.append(observation)
        result = original_handler(args, **kwargs)
        tool_results.append((observation, json.loads(result)))
        return result

    monkeypatch.setattr(entry, "handler", observed_handler)

    def factory(**kwargs):
        resumed_turn = resume and bool(agents)
        agent = run_agent.AIAgent(
            api_key="synthetic-key", base_url="https://example.test/v1",
            session_id=kwargs["session_id"], max_iterations=4, quiet_mode=True,
            skip_context_files=True, skip_memory=True, session_db=db,
        )
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent._cached_system_prompt = "Execute the supplied synthetic terminal check."
        monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
        calls = 0
        call_id = f"context-check-{len(agents)}"

        def provider(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                if resumed_turn:
                    assert any(m.get("content") == "Synthetic context check passed."
                               for m in request["messages"])
                    assert any(m.get("role") == "tool" and agent.session_id in m.get("content", "")
                               for m in request["messages"])
                barrier.wait(timeout=10)
                return _mock_response(
                    content="", finish_reason="tool_calls",
                    tool_calls=[_mock_tool_call("terminal", json.dumps({
                        "command": "printf '%s' \"$HERMES_SESSION_ID\"",
                        "workdir": str(tmp_path), "timeout": 5,
                    }), call_id)],
                )
            assert calls == 2, "Unexpected extra model call"
            results = [m for m in request["messages"]
                       if m.get("role") == "tool" and m.get("tool_call_id") == call_id]
            assert len(results) == 1
            result = json.loads(results[0]["content"])
            assert result.get("exit_code") == 0, result
            assert result["output"].strip() == agent.session_id
            return _mock_response(content="Synthetic context check passed.")

        monkeypatch.setattr(agent, "_interruptible_api_call", provider)
        agents.append(agent)
        return agent

    adapter = _make_adapter()
    adapter._session_db = db
    if resume:
        adapter._api_key = "synthetic-api-key-for-continuity"
    monkeypatch.setattr(adapter, "_create_agent", factory)
    body = ({"messages": [{"role": "user", "content": "Run the synthetic context check."}], "stream": False}
            if endpoint.endswith("completions") else
            {"input": "Run the synthetic context check.", "stream": False})
    try:
        async with TestClient(TestServer(_create_app(adapter))) as client:
            async def request(headers=None, payload_body=None):
                response = await client.post(endpoint, json=payload_body or body, headers=headers)
                payload = await response.json()
                assert response.status == 200, payload
                assert "Synthetic context check passed." in json.dumps(payload), payload
                return dict(response.headers), payload
            if resume:
                headers = {"Authorization": "Bearer synthetic-api-key-for-continuity"}
                first_headers, first = await request(headers)
                if endpoint.endswith("completions"):
                    headers["X-Hermes-Session-Id"] = first_headers["X-Hermes-Session-Id"]
                    await request(headers)
                else:
                    await request(headers, {**body, "previous_response_id": first["id"]})
            else:
                await asyncio.gather(*(request() for _ in range(request_count)))
        for session_id in {row["context_id"] for row in observations}:
            stored = db.get_messages_as_conversation(session_id)
            assert any(m.get("role") == "tool" and session_id in m.get("content", "") for m in stored)
    finally:
        for agent in agents:
            agent.close()
        db.close()

    assert len(observations) == request_count
    assert len({row["context_id"] for row in observations}) == (1 if resume else request_count)
    for row, result in tool_results:
        assert row["context_id"] == row["argument_id"]
        assert row["context_id"] != "foreign-process-mirror"
        assert row["platform"] == "api_server"
        assert row["missing"] is False
        assert result["output"].strip() == row["context_id"]
