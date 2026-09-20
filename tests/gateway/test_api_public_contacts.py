"""Real extraction/receipts and HTTP display, with synthetic agent output."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.public_contacts import UNAVAILABLE
from hermes_state import SessionDB
from tests.gateway.test_api_server import _create_app, _make_adapter
from tests.tools.test_public_contact_delivery import (
    PHONE, SID, URL, extract, extraction, source_authorization,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat/completions", "responses"])
@pytest.mark.parametrize("mode", ["sync", "split_stream", "final_only_stream"])
@pytest.mark.parametrize("authority", ["valid", "revoked", "rotated"])
async def test_contact_http_display_and_raw_replay(
    extraction, source_authorization, monkeypatch, endpoint, mode, authority,
):
    stream = mode != "sync"
    reference = (await extract())["public_contacts"][0]["reference"]
    raw = f"Call {reference}."
    expected_sid = "rotated-session" if authority == "rotated" else SID
    if authority == "revoked":
        (extraction.home / "config.yaml").write_text("security: {}\n")
    adapter = _make_adapter(api_key="test-local-api")
    db = SessionDB(extraction.home / "state.db")
    adapter._session_db = db
    db.create_session(SID, source="api_server")
    adapter._response_store.put("resp_seed", {
        "response": {"id": "resp_seed"}, "conversation_history": [],
        "session_id": SID, "instructions": None,
    })
    calls = []
    results = []

    async def run(**kwargs):
        calls.append(copy.deepcopy(kwargs["conversation_history"]))
        if kwargs.get("agent_ref") is not None:
            kwargs["agent_ref"][0] = SimpleNamespace(session_id=expected_sid)
        callback = kwargs.get("stream_delta_callback")
        if callback and mode == "split_stream":
            # A timer flush in Responses must not expose half a reference.
            for delta in ["Call [pub", "lic-contact:", reference[16:40], reference[40:], "."]:
                callback(delta)
                await asyncio.sleep(0.06)
        result = {"final_response": raw, "session_id": expected_sid}
        results.append(result)
        return result, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(adapter, "_run_agent", run)
    body = {"stream": stream}
    if endpoint == "responses":
        body.update(input="Find the business phone", previous_response_id="resp_seed")
    else:
        body["messages"] = [{"role": "user", "content": "Find the business phone"}]
    headers = {"Authorization": "Bearer test-local-api", "X-Hermes-Session-Id": SID}
    try:
        async with TestClient(TestServer(_create_app(adapter))) as client:
            response = await client.post("/v1/" + endpoint, json=body, headers=headers)
            assert response.status == 200, await response.text()
            if stream:
                wire = await response.text()
                events = [json.loads(line[6:]) for line in wire.splitlines()
                          if line.startswith("data: ") and line != "data: [DONE]"]
                if endpoint == "responses":
                    rendered = "".join(e["delta"] for e in events
                                       if e.get("type") == "response.output_text.delta")
                    data = next(e["response"] for e in events if e.get("type") == "response.completed")
                    assert data["output"][-1]["content"][0]["text"] == rendered
                    assert next(e["text"] for e in events if e.get("type") == "response.output_text.done") == rendered
                else:
                    rendered = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
                assert "[public-contact:" not in rendered
            else:
                data = await response.json()
                rendered = (data["output"][-1]["content"][0]["text"] if endpoint == "responses"
                            else data["choices"][0]["message"]["content"])
            if authority == "valid":
                assert PHONE in rendered
                assert URL in rendered
                assert rendered.startswith("Call Example Business") and rendered.endswith(".")
            else:
                assert rendered == f"Call {UNAVAILABLE}."
                assert PHONE not in rendered
            assert results[0]["final_response"] == raw
            if endpoint == "responses":
                stored = adapter._response_store.get(data["id"])
                assert stored["conversation_history"][-1]["content"] == raw
                fetched = await client.get("/v1/responses/" + data["id"], headers=headers)
                assert (await fetched.json())["output"][-1]["content"][0]["text"] == rendered
                followup = await client.post("/v1/responses", headers=headers, json={
                    "input": "Thanks", "previous_response_id": data["id"],
                })
                assert followup.status == 200
                assert calls[-1][-1]["content"] == raw
    finally:
        db.close()


@pytest.mark.asyncio
async def test_every_reference_split_and_ordinary_text(extraction, source_authorization):
    from agent.public_contacts import ContactReferenceStream, render_contact_references

    reference = (await extract())["public_contacts"][0]["reference"]
    raw = f"First {reference}; second {reference}. Ordinary [public note]."
    expected = render_contact_references(raw, session_id=SID)
    for split in range(len(raw) + 1):
        stream = ContactReferenceStream()
        output = stream.feed(raw[:split], session_id=SID)
        output += stream.feed(raw[split:], session_id=SID) + stream.finish()
        assert output == expected, split
    stream = ContactReferenceStream()
    assert stream.feed("Immediate prose.", session_id=SID) == "Immediate prose."
    assert "".join(stream.feed(c, session_id=SID) for c in raw) + stream.finish() == expected
    # Do not turn arbitrary malformed markers into authorized contacts.
    for ordinary in ["[public", "[public-contact:x]", "[public-contact:" + "a" * 65 + "]"]:
        stream = ContactReferenceStream()
        assert stream.feed(ordinary, session_id=SID) + stream.finish() == ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "session", "profile"])
async def test_stream_rechecks_scope_and_authority(extraction, source_authorization, monkeypatch, tmp_path, change):
    import shutil
    from agent.public_contacts import ContactReferenceStream

    reference = (await extract())["public_contacts"][0]["reference"]
    stream = ContactReferenceStream()
    assert stream.feed("Call " + reference[:40], session_id=SID) == "Call "
    sid = SID
    if change == "revoke":
        (extraction.home / "config.yaml").write_text("security: {}\n")
    elif change == "session":
        sid = "unrelated-session"
    else:
        other = tmp_path / "other"
        shutil.copytree(extraction.home, other)
        monkeypatch.setenv("HERMES_HOME", str(other))
    assert stream.feed(reference[40:], session_id=sid) + stream.finish() == UNAVAILABLE


@pytest.mark.asyncio
async def test_failed_response_stream_retains_raw_partial_replay(extraction, source_authorization, monkeypatch):
    reference = (await extract())["public_contacts"][0]["reference"]
    raw = "Call " + reference + ". Then [public-contact:" + "a" * 10
    adapter = _make_adapter()
    adapter._response_store.put("resp_seed", {
        "response": {"id": "resp_seed"}, "conversation_history": [], "session_id": SID,
    })

    async def run(**kwargs):
        kwargs["stream_delta_callback"](raw)
        await asyncio.sleep(0.1)
        raise RuntimeError("Synthetic provider failure")

    monkeypatch.setattr(adapter, "_run_agent", run)
    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post("/v1/responses", json={
            "input": "Find phone", "previous_response_id": "resp_seed", "stream": True,
        })
        wire = await response.text()
        events = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
        terminal = next(e["response"] for e in events if e.get("type") == "response.failed")
        rendered = terminal["output"][-1]["content"][0]["text"]
        assert PHONE in rendered and rendered.endswith(UNAVAILABLE)
        assert adapter._response_store.get(terminal["id"])["conversation_history"][-1]["content"] == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_concurrent_profile_http_display_uses_own_receipts(
    extraction, source_authorization, monkeypatch, tmp_path, stream,
):
    import shutil
    from aiohttp import web
    from hermes_constants import get_hermes_home

    reference = (await extract())["public_contacts"][0]["reference"]
    other = tmp_path / "other"
    shutil.copytree(extraction.home, other)
    homes = {"owner": extraction.home, "other": other}
    for name, home in homes.items():
        (home / ".env").write_text(f"API_SERVER_KEY=synthetic-{name}-profile-key\n")
    adapter = _make_adapter()
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(
        multiplex_profiles=True, multiplex_profile_allowlist=None,
    ))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **kw: list(homes.items()))
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: homes[name])
    seen = []

    async def run(**kwargs):
        seen.append(get_hermes_home())
        if kwargs.get("agent_ref") is not None:
            kwargs["agent_ref"][0] = SimpleNamespace(session_id=SID)
        if kwargs.get("stream_delta_callback"):
            kwargs["stream_delta_callback"](reference[:40])
            await asyncio.sleep(0.06)
            kwargs["stream_delta_callback"](reference[40:])
        else:
            await asyncio.sleep(0.01)
        return {"final_response": reference, "session_id": SID}, {}

    monkeypatch.setattr(adapter, "_run_agent", run)
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post("/p/{profile}/v1/chat/completions", adapter._handle_chat_completions)
    async with TestClient(TestServer(app)) as client:
        async def request(profile):
            response = await client.post(f"/p/{profile}/v1/chat/completions", headers={
                "Authorization": f"Bearer synthetic-{profile}-profile-key",
            }, json={
                "messages": [{"role": "user", "content": "Find phone"}], "stream": stream,
            })
            assert response.status == 200
            if stream:
                events = [json.loads(line[6:]) for line in (await response.text()).splitlines()
                          if line.startswith("data: ") and line != "data: [DONE]"]
                return "".join(e["choices"][0]["delta"].get("content", "") for e in events)
            return (await response.json())["choices"][0]["message"]["content"]
        owner_text, other_text = await asyncio.gather(request("owner"), request("other"))
    assert PHONE in owner_text and other_text == UNAVAILABLE
    assert set(seen) == set(homes.values())
