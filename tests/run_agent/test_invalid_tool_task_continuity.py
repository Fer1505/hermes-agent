"""Exercise actual provider request assembly after a nonexistent tool.

Provider responses are scripted: these tests prove transcript continuity and
bounded recovery, not that a live model will choose a useful final answer.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.run_agent.test_tool_call_guardrail_runtime import (
    _make_agent, _mock_response, _mock_tool_call,
)
from tests.run_agent.test_run_agent_codex_responses import (
    _build_agent, _codex_message_response,
)

TASK = "Finish the synthetic outage report using the evidence already collected."
EVIDENCE = "Synthetic receipt: job synthetic-42 failed at 08:00 UTC; recovery is unverified."
HISTORY = [
    {"role": "user", "content": "Inspect the synthetic job."},
    {"role": "assistant", "content": EVIDENCE},
]


def response(mode, calls=None, text=None):
    if mode == "chat":
        return _mock_response(content=text or "", finish_reason="tool_calls" if calls else "stop",
                              tool_calls=[_mock_tool_call(name, "{}", cid) for name, cid in calls] if calls else None)
    if not calls:
        return _codex_message_response(text)
    return SimpleNamespace(
        output=[SimpleNamespace(type="function_call", id=f"fc_{cid}", call_id=cid,
                                name=name, arguments="{}") for name, cid in calls],
        usage=SimpleNamespace(input_tokens=12, output_tokens=4, total_tokens=16),
        status="completed", model="gpt-5-codex",
    )


def prepare(monkeypatch, mode, responses):
    agent = _make_agent("terminal") if mode == "chat" else _build_agent(monkeypatch)
    agent.compression_enabled = False
    agent._cached_system_prompt = "Complete the user's active task using verified evidence."
    agent.save_trajectories = False
    agent.max_iterations = 10
    requests = []
    def provider(kwargs):
        requests.append(deepcopy(kwargs))
        assert responses, "Unexpected extra inference request"
        return responses.pop(0)
    monkeypatch.setattr(agent, "_interruptible_api_call", provider)
    monkeypatch.setattr(agent, "_persist_session", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **k: None)
    return agent, requests


def assert_task_retained(requests, mode):
    key = "messages" if mode == "chat" else "input"
    for request in requests:
        serialized = json.dumps(request[key])
        assert TASK in serialized
        assert EVIDENCE in serialized
    # The original prefix must not be rebuilt to inject a recovery user turn.
    first = requests[0][key]
    for request in requests[1:]:
        assert request[key][:len(first)] == first
    if mode == "codex":
        assert all(req["instructions"] == requests[0]["instructions"] for req in requests)


@pytest.mark.parametrize("mode", ["chat", "codex"])
def test_invalid_tool_preserves_original_task_and_prior_evidence(monkeypatch, mode):
    final = "The synthetic report is blocked: no messaging tool is available; no delivery occurred."
    agent, requests = prepare(monkeypatch, mode, [
        response(mode, [("message", "missing_1")]), response(mode, text=final),
    ])
    with patch("run_agent.handle_function_call") as dispatch:
        result = agent.run_conversation(TASK, conversation_history=deepcopy(HISTORY))
    dispatch.assert_not_called()
    assert len(requests) == 2
    assert_task_retained(requests, mode)
    assert result["final_response"] == final
    error = next(m for m in result["messages"] if m.get("tool_call_id") == "missing_1")
    assert "does not exist" in error["content"]
    assert "Continue the current user request" in error["content"]
    assert "No action was executed for this rejected call" in error["content"]
    if mode == "codex":
        items = requests[1]["input"]
        assert any(x.get("type") == "function_call" and x.get("call_id") == "missing_1" for x in items)
        assert any(x.get("type") == "function_call_output" and x.get("call_id") == "missing_1" for x in items)


@pytest.mark.parametrize("mode", ["chat", "codex"])
def test_repeated_invalid_calls_stop_partial_without_losing_task(monkeypatch, mode):
    agent, requests = prepare(monkeypatch, mode, [
        response(mode, [("message", f"missing_{i}")]) for i in range(3)
    ])
    with patch("run_agent.handle_function_call") as dispatch:
        result = agent.run_conversation(TASK, conversation_history=deepcopy(HISTORY))
    dispatch.assert_not_called()
    assert len(requests) == 3
    assert_task_retained(requests, mode)
    assert result["partial"] is True
    assert result["completed"] is False
    assert "message" in result["error"]
    assert TASK in json.dumps(result["messages"])
    assert EVIDENCE in json.dumps(result["messages"])


@pytest.mark.parametrize("mode", ["chat", "codex"])
def test_mixed_invalid_call_does_not_discard_valid_work(monkeypatch, mode):
    final = "Synthetic report saved; delivery is blocked because messaging is unavailable."
    agent, requests = prepare(monkeypatch, mode, [
        response(mode, [("message", "missing_1"), ("terminal", "valid_1")]),
        response(mode, text=final),
    ])
    with patch("run_agent.handle_function_call", return_value='{"saved": "synthetic report"}') as dispatch:
        result = agent.run_conversation(TASK, conversation_history=deepcopy(HISTORY))
    assert dispatch.call_count == 1
    assert dispatch.call_args.args[0] == "terminal"
    assert_task_retained(requests, mode)
    assert result["final_response"] == final
    results = {m["tool_call_id"]: m["content"] for m in result["messages"] if m.get("role") == "tool"}
    assert "does not exist" in results["missing_1"]
    assert "synthetic report" in results["valid_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat", "codex"])
@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/responses"])
async def test_http_api_retains_task_through_real_agent_recovery(monkeypatch, mode, endpoint):
    from aiohttp.test_utils import TestClient, TestServer
    from tests.gateway.test_api_server_multimodal import _make_adapter, _create_app
    final = "The synthetic report remains blocked because the messaging capability is unavailable."
    agent, requests = prepare(monkeypatch, mode, [
        response(mode, [("message", "missing_http")]), response(mode, text=final),
    ])
    adapter = _make_adapter()
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    body = ({"messages": deepcopy(HISTORY) + [{"role": "user", "content": TASK}], "stream": False}
            if endpoint.endswith("completions") else
            {"input": TASK, "conversation_history": deepcopy(HISTORY), "stream": False})
    with patch("run_agent.handle_function_call") as dispatch:
        async with TestClient(TestServer(_create_app(adapter))) as client:
            http_response = await client.post(endpoint, json=body)
            payload = await http_response.json()
    assert http_response.status == 200, payload
    dispatch.assert_not_called()
    assert len(requests) == 2
    assert_task_retained(requests, mode)
    assert final in json.dumps(payload)


@pytest.mark.parametrize("mode", ["chat", "codex"])
def test_partial_failure_survives_database_reopen_and_agent_recreation(monkeypatch, tmp_path, mode):
    from hermes_state import SessionDB
    from run_agent import AIAgent
    path = tmp_path / "continuity.db"
    session_id = "synthetic-task-recovery"
    db = SessionDB(db_path=path)
    db.create_session(session_id, source="api")
    for item in HISTORY:
        db.append_message(session_id, item["role"], item["content"])
    agent, requests = prepare(monkeypatch, mode, [
        response(mode, [("message", f"missing_{i}")]) for i in range(3)
    ])
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    monkeypatch.setattr(agent, "_persist_session", AIAgent._persist_session.__get__(agent, AIAgent))
    try:
        with patch("run_agent.handle_function_call") as dispatch:
            result = agent.run_conversation(TASK, conversation_history=deepcopy(HISTORY))
        dispatch.assert_not_called()
        assert result["partial"] is True
    finally:
        db.close()
    db = SessionDB(db_path=path)
    try:
        retained = db.get_messages_as_conversation(session_id, repair_alternation=True)
        assert TASK in json.dumps(retained)
        assert EVIDENCE in json.dumps(retained)
        assert any(m.get("role") == "tool" and "does not exist" in m.get("content", "") for m in retained)
        final = "The original report remains blocked on unavailable messaging; recovery is unverified."
        resumed, replay_requests = prepare(monkeypatch, mode, [response(mode, text=final)])
        resumed._session_db = db
        resumed._session_db_created = True
        resumed.session_id = session_id
        monkeypatch.setattr(resumed, "_persist_session", AIAgent._persist_session.__get__(resumed, AIAgent))
        with patch("run_agent.handle_function_call") as dispatch:
            result = resumed.run_conversation("Continue the existing request.", conversation_history=retained)
        dispatch.assert_not_called()
        assert_task_retained(replay_requests, mode)
        assert result["final_response"] == final
    finally:
        db.close()
