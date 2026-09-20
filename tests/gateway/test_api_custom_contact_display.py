"""Custom HTTP/executor/SQLite paths, synthetic model, real contact receipts."""
import copy
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.public_contacts import UNAVAILABLE
from hermes_state import SessionDB
from tests.gateway.test_api_server import _create_app, _make_adapter
from tests.gateway.test_api_server_runs import _create_runs_app
from tests.tools.test_public_contact_delivery import (
    PHONE, SID, URL, extract, extraction, source_authorization,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["session", "session_stream", "run"])
@pytest.mark.parametrize("authority", ["valid", "revoked", "rotated", "midstream_revoked"])
async def test_custom_reply_stream_and_transcript(
    extraction, source_authorization, monkeypatch, surface, authority,
):
    reference = (await extract())["public_contacts"][0]["reference"]
    raw = f"Call {reference}."
    effective_sid = "rotated-session" if authority == "rotated" else SID
    if authority == "revoked":
        (extraction.home / "config.yaml").write_text("security: {}\n")
    adapter = _make_adapter()
    db = SessionDB(extraction.home / "state.db")
    adapter._session_db = db
    db.create_session(SID, source="api_server")
    if effective_sid != SID:
        db.create_session(effective_sid, source="api_server")
    message = {"role": "assistant", "content": raw,
               "codex_message_items": [{"type": "message", "id": "synthetic-provider-item", "content": raw}]}
    original = copy.deepcopy(message)

    def create(**kwargs):
        agent = SimpleNamespace(session_id=SID, session_prompt_tokens=1,
                                session_completion_tokens=1, session_total_tokens=2)

        def run_conversation(**run_kwargs):
            assert run_kwargs["task_id"] == SID
            agent.session_id = effective_sid
            callback = kwargs.get("stream_delta_callback")
            if callback:
                for index, chunk in enumerate([raw[:10], raw[10:40], raw[40:]]):
                    callback(chunk)
                    time.sleep(0.02)
                    if index == 0 and authority == "midstream_revoked":
                        (extraction.home / "config.yaml").write_text("security: {}\n")
            elif authority == "midstream_revoked":
                (extraction.home / "config.yaml").write_text("security: {}\n")
            db.append_message(effective_sid, "assistant", content=raw)
            return {"final_response": raw, "messages": [message], "completed": True}

        agent.run_conversation = run_conversation
        return agent

    monkeypatch.setattr(adapter, "_create_agent", create)
    app = _create_runs_app(adapter) if surface == "run" else _create_app(adapter)
    try:
        async with TestClient(TestServer(app)) as client:
            if surface == "run":
                response = await client.post("/v1/runs", json={"input": "Find phone", "session_id": SID})
                assert response.status == 202, await response.text()
                run_id = (await response.json())["run_id"]
                response = await client.get(f"/v1/runs/{run_id}/events")
            else:
                suffix = "/stream" if surface == "session_stream" else ""
                response = await client.post(f"/api/sessions/{SID}/chat{suffix}", json={"message": "Find phone"})
            assert response.status == 200, await response.text()
            if surface == "session":
                data = await response.json()
                displayed = data["message"]["content"]
                assert data["session_id"] == effective_sid
            else:
                wire = await response.text()
                events = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
                if surface == "run":
                    displayed = next(e["output"] for e in events if e.get("event") == "run.completed")
                    delta_text = "".join(e["delta"] for e in events if e.get("event") == "message.delta")
                    status_response = await client.get(f"/v1/runs/{run_id}")
                    status = await status_response.json()
                    assert status["output"] == displayed
                    assert status["session_id"] == effective_sid
                else:
                    displayed = next(e["content"] for e in events if "content" in e)
                    delta_text = "".join(e["delta"] for e in events if "delta" in e)
                    transcript = next(e["messages"] for e in events if "messages" in e)
                    assert transcript[-1]["content"] == displayed
                assert delta_text == displayed
            if authority == "valid":
                assert PHONE in displayed and URL in displayed
            else:
                assert displayed == f"Call {UNAVAILABLE}."
            assert message == original
            assert db.get_messages_as_conversation(effective_sid)[-1]["content"] == raw
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["session_stream", "run"])
async def test_custom_background_execution_keeps_profile_scope(
    extraction, source_authorization, monkeypatch, tmp_path, surface,
):
    import asyncio
    import shutil
    from aiohttp import web
    from hermes_constants import get_hermes_home

    reference = (await extract())["public_contacts"][0]["reference"]
    raw = f"Call {reference}."
    other = tmp_path / "other"
    shutil.copytree(extraction.home, other)
    homes = {"owner": extraction.home, "other": other}
    for name, home in homes.items():
        (home / ".env").write_text(f"API_SERVER_KEY=synthetic-{name}-profile-key\n")
        with SessionDB(home / "state.db") as db:
            db.create_session(SID, source="api_server")
    adapter = _make_adapter()
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(
        multiplex_profiles=True, multiplex_profile_allowlist=None,
    ))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **kw: list(homes.items()))
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: homes[name])

    def create(**kwargs):
        expected_home = get_hermes_home()
        agent = SimpleNamespace(session_id=SID, session_prompt_tokens=1,
                                session_completion_tokens=1, session_total_tokens=2)

        def run_conversation(**run_kwargs):
            assert get_hermes_home() == expected_home
            for chunk in [raw[:40], raw[40:]]:
                kwargs["stream_delta_callback"](chunk)
                time.sleep(0.04)
            assert get_hermes_home() == expected_home
            with SessionDB(expected_home / "state.db") as db:
                db.append_message(SID, "assistant", content=raw)
            return {"final_response": raw, "messages": [{"role": "assistant", "content": raw}]}

        agent.run_conversation = run_conversation
        return agent

    monkeypatch.setattr(adapter, "_create_agent", create)
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post("/p/{profile}/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_post("/p/{profile}/v1/runs", adapter._handle_runs)
    app.router.add_get("/p/{profile}/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_get("/p/{profile}/v1/runs/{run_id}", adapter._handle_get_run)
    try:
        async with TestClient(TestServer(app)) as client:
            async def request(profile):
                prefix = f"/p/{profile}"
                headers = {"Authorization": f"Bearer synthetic-{profile}-profile-key"}
                if surface == "run":
                    started = await client.post(prefix + "/v1/runs", headers=headers,
                                                json={"input": "Find phone", "session_id": SID})
                    assert started.status == 202, await started.text()
                    run_id = (await started.json())["run_id"]
                    response = await client.get(prefix + f"/v1/runs/{run_id}/events", headers=headers)
                else:
                    response = await client.post(prefix + f"/api/sessions/{SID}/chat/stream",
                                                 headers=headers, json={"message": "Find phone"})
                assert response.status == 200, await response.text()
                events = [json.loads(line[6:]) for line in (await response.text()).splitlines()
                          if line.startswith("data: ")]
                displayed = (next(e["output"] for e in events if e.get("event") == "run.completed")
                             if surface == "run" else next(e["content"] for e in events if "content" in e))
                assert "".join(e["delta"] for e in events if "delta" in e) == displayed
                if surface == "run":
                    response = await client.get(prefix + f"/v1/runs/{run_id}", headers=headers)
                    assert (await response.json())["output"] == displayed
                return displayed
            owner_text, other_text = await asyncio.gather(request("owner"), request("other"))
        assert PHONE in owner_text and other_text == f"Call {UNAVAILABLE}."
        for home in homes.values():
            with SessionDB(home / "state.db") as db:
                assert db.get_messages_as_conversation(SID)[-1]["content"] == raw
    finally:
        adapter._close_cached_session_dbs()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["session_stream", "run"])
async def test_custom_failed_stream_finishes_partial_contact(extraction, monkeypatch, surface):
    adapter = _make_adapter()
    db = SessionDB(extraction.home / "state.db")
    adapter._session_db = db
    db.create_session(SID, source="api_server")

    def create(**kwargs):
        def run_conversation(**run_kwargs):
            kwargs["stream_delta_callback"]("Call [public-contact:" + "a" * 20)
            raise RuntimeError("Synthetic provider exception")
        return SimpleNamespace(session_id=SID, run_conversation=run_conversation)

    monkeypatch.setattr(adapter, "_create_agent", create)
    app = _create_runs_app(adapter) if surface == "run" else _create_app(adapter)
    try:
        async with TestClient(TestServer(app)) as client:
            if surface == "run":
                response = await client.post("/v1/runs", json={"input": "Find phone", "session_id": SID})
                run_id = (await response.json())["run_id"]
                response = await client.get(f"/v1/runs/{run_id}/events")
            else:
                response = await client.post(f"/api/sessions/{SID}/chat/stream", json={"message": "Find phone"})
            wire = await response.text()
            events = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
            assert "".join(e["delta"] for e in events if "delta" in e) == "Call " + UNAVAILABLE
            assert "run.completed" not in wire and "assistant.completed" not in wire
            assert "Synthetic provider exception" in wire
    finally:
        db.close()
