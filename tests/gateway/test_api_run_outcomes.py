"""Native result flags through real HTTP/executor and persisted API state."""
import copy
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hermes_state import SessionDB
from tests.gateway.test_api_server import _create_app, _make_adapter
from tests.gateway.test_api_server_runs import _create_runs_app

CASES = [
    ({"completed": True}, "completed", "stop"),
    ({}, "completed", "stop"),  # Legacy agent result without explicit flags.
    ({"completed": False, "failed": True, "error": "Provider rejected request"}, "failed", "error"),
    ({"completed": False, "partial": True}, "incomplete", "error"),
    ({"completed": False, "partial": True, "error": "Output truncated"}, "incomplete", "length"),
    ({"completed": False, "partial": True, "compression_deferred": True, "error": "Compression busy"}, "incomplete", "error"),
    ({"completed": False, "interrupted": True}, "cancelled", "error"),
    ({"completed": False}, "incomplete", "error"),
    ({"completed": True, "failed": True}, "failed", "error"),
    ({"completed": True, "partial": True}, "incomplete", "error"),
    ({"completed": True, "interrupted": True}, "cancelled", "error"),
    ({"completed": False, "failed": True, "partial": True, "error": "Output truncated before failure"}, "failed", "error"),
    ({"error": "OPENAI_API_KEY=sk-synthetic-error-secret-1234567890"}, "failed", "error"),
]
SID = "synthetic-outcome-session"
TEXT = "Retained assistant output."


def sse_events(wire):
    events = []
    for frame in wire.split("\n\n"):
        name = ""
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and line != "data: [DONE]":
                events.append((name, json.loads(line[6:])))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["session", "session_stream", "chat", "chat_stream", "responses", "responses_stream", "run"])
@pytest.mark.parametrize("flags,status,finish", CASES)
async def test_result_flags_control_every_api_outcome(tmp_path, monkeypatch, surface, flags, status, finish):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter(api_key="synthetic-api-key")
    db = SessionDB(tmp_path / "state.db")
    adapter._session_db = db
    db.create_session(SID, source="api_server")
    raw_messages = [{"role": "assistant", "content": TEXT,
                     "codex_message_items": [{"type": "message", "id": "synthetic-item", "content": TEXT}]}]
    expected_messages = copy.deepcopy(raw_messages)

    def create(**kwargs):
        def run_conversation(**run_kwargs):
            callback = kwargs.get("stream_delta_callback")
            if callback:
                callback(TEXT)
            return {"final_response": TEXT, "messages": raw_messages, **flags}
        return SimpleNamespace(session_id=SID, run_conversation=run_conversation,
                               session_prompt_tokens=1, session_completion_tokens=1, session_total_tokens=2)

    monkeypatch.setattr(adapter, "_create_agent", create)
    app = _create_runs_app(adapter) if surface == "run" else _create_app(adapter)
    headers = {"Authorization": "Bearer synthetic-api-key"}
    try:
        async with TestClient(TestServer(app)) as client:
            if surface.startswith("session"):
                suffix = "/stream" if surface.endswith("stream") else ""
                response = await client.post(f"/api/sessions/{SID}/chat{suffix}", headers=headers, json={"message": "Proceed"})
            elif surface.startswith("chat"):
                response = await client.post("/v1/chat/completions", headers=headers, json={
                    "messages": [{"role": "user", "content": "Proceed"}], "stream": surface.endswith("stream"),
                })
            elif surface.startswith("responses"):
                response = await client.post("/v1/responses", headers=headers, json={"input": "Proceed", "stream": surface.endswith("stream")})
            else:
                started = await client.post("/v1/runs", headers=headers, json={"input": "Proceed", "session_id": SID})
                assert started.status == 202
                run_id = (await started.json())["run_id"]
                response = await client.get(f"/v1/runs/{run_id}/events", headers=headers)
            assert response.status == 200, await response.text()
            wire = await response.text()
            assert "sk-synthetic-error-secret-1234567890" not in wire
            if surface.endswith("stream") or surface == "run":
                events = sse_events(wire)
                if surface == "session_stream":
                    terminal = [(name, e) for name, e in events if name in {"run.completed", "run.failed", "run.cancelled", "run.incomplete"}]
                    assert len(terminal) == 1 and terminal[0][0] == "run." + status
                    data = terminal[0][1]
                    message = next(e for name, e in events if name == "assistant.completed")
                    assert message["content"] == TEXT
                    assert message["completed"] is (status == "completed")
                    assert data["completed"] is (status == "completed")
                    assert adapter._run_statuses[data["run_id"]]["status"] == status
                elif surface == "run":
                    terminal = [e for _, e in events if e.get("event", "").startswith("run.")]
                    assert terminal[-1]["event"] == "run." + status
                    polled = await client.get(f"/v1/runs/{run_id}", headers=headers)
                    data = await polled.json()
                    assert data["status"] == status
                    assert data["output"] == TEXT
                elif surface == "chat_stream":
                    data = events[-1][1]
                    assert data["choices"][0]["finish_reason"] == finish
                    assert "".join(e["choices"][0]["delta"].get("content", "") for _, e in events) == TEXT
                else:
                    # Responses has no cancelled stream event in the installed
                    # SDK: native interruptions are incomplete + Hermes detail.
                    response_status = "incomplete" if status == "cancelled" else status
                    terminal = [(name, e) for name, e in events if name in {"response.completed", "response.failed", "response.incomplete"}]
                    assert len(terminal) == 1 and terminal[0][0] == "response." + response_status
                    data = terminal[0][1]["response"]
                    assert data["status"] == response_status
                    items = [e["item"] for name, e in events if name == "response.output_item.done" and e["item"]["type"] == "message"]
                    assert items[-1]["status"] == ("completed" if status == "completed" else "incomplete")
            else:
                data = json.loads(wire)
                if surface == "session":
                    assert data["completed"] is (status == "completed")
                    assert data["status"] == status
                    assert data["message"]["content"] == TEXT
                elif surface == "chat":
                    assert data["choices"][0]["finish_reason"] == finish
                else:
                    assert data["status"] == ("incomplete" if status == "cancelled" else status)
            if surface.startswith("responses"):
                assert data["output"][-1]["content"][0]["text"] == TEXT
                fetched = await client.get("/v1/responses/" + data["id"], headers=headers)
                assert (await fetched.json())["status"] == data["status"]
                assert adapter._response_store.get(data["id"])["conversation_history"][-1] == expected_messages[-1]
                # Check the emitted error/detail subset against the installed
                # SDK rather than inventing native causes as OpenAI enums.
                if data.get("error"):
                    from openai.types.responses.response_error import ResponseError
                    ResponseError.model_validate(data["error"])
                if data.get("incomplete_details"):
                    from openai.types.responses.response import IncompleteDetails
                    IncompleteDetails.model_validate(data["incomplete_details"])
            if status != "completed":
                metadata = data if surface in {"session", "session_stream", "run"} else data["hermes"]
                assert metadata["completed"] is False
                assert metadata["partial"] is bool(flags.get("partial"))
                assert metadata["interrupted"] is bool(flags.get("interrupted"))
            assert raw_messages == expected_messages
    finally:
        db.close()


@pytest.mark.asyncio
async def test_incomplete_run_survives_restart_and_replay_without_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = _make_adapter()
    calls = []

    def create(**kwargs):
        def run_conversation(**run_kwargs):
            calls.append(run_kwargs)
            return {"final_response": TEXT, "completed": False, "partial": True}
        return SimpleNamespace(session_id=SID, run_conversation=run_conversation,
                               session_prompt_tokens=0, session_completion_tokens=0, session_total_tokens=0)

    monkeypatch.setattr(first, "_create_agent", create)
    headers = {"Idempotency-Key": "synthetic-incomplete-replay"}
    async with TestClient(TestServer(_create_runs_app(first))) as client:
        response = await client.post("/v1/runs", json={"input": "Proceed"}, headers=headers)
        run_id = (await response.json())["run_id"]
        events = await client.get(f"/v1/runs/{run_id}/events")
        assert "run.incomplete" in await events.text()
    first._run_idempotency_store.close()
    first._response_store.close()
    restarted = _make_adapter()
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    monkeypatch.setattr(restarted, "_create_agent", lambda **kw: pytest.fail("Must not execute an idempotent replay"))
    try:
        async with TestClient(TestServer(_create_runs_app(restarted))) as client:
            response = await client.get(f"/v1/runs/{run_id}")
            restored = await response.json()
            assert restored["status"] == "incomplete"
            assert restored["output"] == TEXT and restored["completed"] is False
            stopped = await client.post(f"/v1/runs/{run_id}/stop")
            assert stopped.status == 200
            assert (await stopped.json())["status"] == "incomplete"
            replayed = await client.post("/v1/runs", json={"input": "Proceed"}, headers=headers)
            replay = await replayed.json()
            assert replay["replayed"] is True and replay["run_id"] == run_id
            assert replay["status"] == "incomplete" and len(calls) == 1
            restarted._sweep_orphaned_runs_once(restored["updated_at"] + restarted._RUN_STATUS_TTL + 1)
            assert run_id not in restarted._run_statuses
            response = await client.get(f"/v1/runs/{run_id}")
            assert (await response.json())["status"] == "incomplete"
    finally:
        restarted._run_idempotency_store.close()
        restarted._response_store.close()


@pytest.mark.parametrize("status,replay_outcome", [("incomplete", "created"), ("running", "reused")])
def test_retention_expires_incomplete_but_preserves_active_reservation(tmp_path, monkeypatch, status, replay_outcome):
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

    now = [100.0]
    monkeypatch.setattr("gateway.platforms.api_server_run_idempotency.time.time", lambda: now[0])
    store = RunIdempotencyStore(str(tmp_path / "runs.db"))
    try:
        store.reserve("scope", "key", "fingerprint", "run_old", {"status": status})
        now[0] += store.RETENTION_SECONDS + 1
        outcome, record = store.reserve("scope", "key", "fingerprint", "run_new", {"status": "queued"})
        assert outcome == replay_outcome
        assert record["run_id"] == ("run_new" if status == "incomplete" else "run_old")
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["session", "session_stream", "chat", "chat_stream", "responses", "responses_stream", "run"])
@pytest.mark.parametrize("text", [None, ""])
async def test_empty_failed_result_preserves_original_cause(tmp_path, monkeypatch, surface, text):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter()
    db = SessionDB(tmp_path / "state.db")
    adapter._session_db = db
    db.create_session(SID, source="api_server")
    cause = "Original provider failure."

    def create(**kwargs):
        return SimpleNamespace(session_id=SID, session_prompt_tokens=0, session_completion_tokens=0,
                               session_total_tokens=0, run_conversation=lambda **kw: {
                                   "final_response": text, "failed": True, "completed": False, "error": cause,
                               })

    monkeypatch.setattr(adapter, "_create_agent", create)
    app = _create_runs_app(adapter) if surface == "run" else _create_app(adapter)
    try:
        async with TestClient(TestServer(app)) as client:
            if surface.startswith("session"):
                suffix = "/stream" if surface.endswith("stream") else ""
                response = await client.post(f"/api/sessions/{SID}/chat{suffix}", json={"message": "Proceed"})
            elif surface.startswith("chat"):
                response = await client.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "Proceed"}], "stream": surface.endswith("stream"),
                })
            elif surface.startswith("responses"):
                response = await client.post("/v1/responses", json={"input": "Proceed", "stream": surface.endswith("stream")})
            else:
                started = await client.post("/v1/runs", json={"input": "Proceed"})
                response = await client.get(f"/v1/runs/{(await started.json())['run_id']}/events")
            assert response.status == (502 if surface == "chat" else 200)
            wire = await response.text()
            assert cause in wire
            assert "expected string or bytes" not in wire
            if surface == "session":
                assert json.loads(wire)["completed"] is False
            if surface in {"session_stream", "run", "responses_stream"}:
                for name, event in sse_events(wire):
                    assert name not in {"run.completed", "response.completed"}
                    assert event.get("event") != "run.completed"
    finally:
        db.close()
