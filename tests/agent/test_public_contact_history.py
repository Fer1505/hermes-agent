"""Historical public-contact evidence is row-bound, never fresh authority."""
import copy
import json
import time

import pytest

from agent import public_contacts as contacts
from agent.compaction_display import project_compaction_message_for_display
from hermes_state import SessionDB
from tests.tools.test_public_contact_delivery import extraction, extract, SID, PHONE, URL


async def save_reply(extraction, *, batch=False):
    ref = (await extract())["public_contacts"][0]["reference"]
    content = f"Call {ref}. Private contact: 202-555-0143."
    replay = [{"type": "message", "content": [{"type": "output_text", "text": content}]}]
    path = extraction.home / "state.db"
    with SessionDB(path) as db:
        db.create_session(SID, source="synthetic-history")
        metadata = {"existing_note": "preserve this"}
        if batch:
            rows = [{"role": "assistant", "content": content, "codex_message_items": replay,
                     "api_content": content, "display_metadata": metadata}]
            db.append_messages_batch(SID, rows)
            assert rows[0]["_session_id"] == SID
            assert contacts.DISPLAY_METADATA_KEY in rows[0]["display_metadata"]
        else:
            db.append_message(SID, "assistant", content=content, codex_message_items=replay,
                              api_content=content, display_metadata=metadata)
        original = db.get_messages(SID)[0]
    return ref, content, replay, path, original


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_saved_contact_survives_expiry_and_current_grant_removal(extraction, monkeypatch, batch):
    ref, content, replay, path, original = await save_reply(extraction, batch=batch)
    metadata = original["display_metadata"]
    assert metadata["existing_note"] == "preserve this"
    assert PHONE not in json.dumps(metadata)
    (extraction.home / "config.yaml").write_text("security: {}\n")
    now = time.time()
    monkeypatch.setattr(contacts.time, "time", lambda: now + 600)
    with SessionDB(path) as db:
        row = db.get_messages(SID)[0]
        history = db.get_messages_as_conversation(SID, include_row_ids=True)
        model_history = db.get_messages_as_conversation(SID)
    for stored in [row, history[0]]:
        before = copy.deepcopy(stored)
        display = project_compaction_message_for_display(stored)
        assert PHONE in display["content"] and URL in display["content"]
        assert "202-555-0143" not in display["content"]
        assert "Verified:" in display["content"]
        assert stored == before
    assert contacts.render_contact_references(ref, session_id=SID) == contacts.UNAVAILABLE
    assert model_history[0]["content"] == content
    assert model_history[0]["api_content"] == content
    assert model_history[0]["codex_message_items"] == replay


@pytest.mark.asyncio
async def test_tui_and_api_use_same_history_projection(extraction, monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter
    from tui_gateway.server import _history_to_messages
    _, _, _, path, _ = await save_reply(extraction)
    (extraction.home / "config.yaml").write_text("security: {}\n")
    with SessionDB(path) as db:
        row = db.get_messages(SID)[0]
        history = db.get_messages_as_conversation(SID, include_row_ids=True)
    api = APIServerAdapter._message_response(row)
    tui = _history_to_messages(history)[0]
    assert api["content"] == tui["text"]
    assert PHONE in tui["text"]
    assert contacts.DISPLAY_METADATA_KEY not in api
    assert "202-555-0143" not in api["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["row", "session", "content", "role", "missing_identity"])
async def test_copied_display_handle_is_bound_to_durable_row(extraction, change):
    _, _, _, _, original = await save_reply(extraction)
    forged = copy.deepcopy(original)
    if change == "row": forged["id"] += 1
    elif change == "session": forged["session_id"] = "foreign-session"
    elif change == "content": forged["content"] += " changed"
    elif change == "role": forged["role"] = "user"
    elif change == "missing_identity": forged.pop("id")
    assert PHONE not in project_compaction_message_for_display(forged)["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_incoming_metadata_cannot_reauthorize_expired_reference(extraction, batch):
    _, content, _, path, original = await save_reply(extraction)
    (extraction.home / "config.yaml").write_text("security: {}\n")
    with SessionDB(path) as db:
        if batch:
            rows = [{"role": "assistant", "content": content, "display_metadata": original["display_metadata"],
                     "_row_id": original["id"], "_session_id": SID}]
            # Existing row identity is reconciled, not inserted a second time.
            assert db.append_messages_batch(SID, rows) == 0
            rows[0].pop("_row_id")
            db.append_messages_batch(SID, rows)
        else:
            db.append_message(SID, "assistant", content=content, display_metadata=original["display_metadata"])
        original_row, copied = db.get_messages(SID)
    assert PHONE in project_compaction_message_for_display(original_row)["content"]
    assert PHONE not in project_compaction_message_for_display(copied)["content"]
    assert contacts.DISPLAY_METADATA_KEY not in copied["display_metadata"]
    assert copied["display_metadata"]["existing_note"] == "preserve this"


@pytest.mark.asyncio
async def test_profile_cannot_reuse_copied_display_record(extraction, tmp_path, monkeypatch):
    import shutil
    _, _, _, _, original = await save_reply(extraction)
    other = tmp_path / "other-profile"
    other.mkdir()
    shutil.copytree(extraction.home / "public-contact-receipts", other / "public-contact-receipts")
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert PHONE not in project_compaction_message_for_display(original)["content"]


@pytest.mark.asyncio
async def test_optional_capture_failure_preserves_transcript(extraction):
    ref = (await extract())["public_contacts"][0]["reference"]
    (extraction.home / "public-contact-receipts" / "display").write_text("not a directory")
    with SessionDB(extraction.home / "state.db") as db:
        db.create_session(SID, source="synthetic-history")
        db.append_message(SID, "assistant", content=ref)
        stored = db.get_messages(SID)[0]
    assert stored["content"] == ref
    assert PHONE not in project_compaction_message_for_display(stored)["content"]


@pytest.mark.asyncio
async def test_blank_row_fill_captures_display_and_reflush_keeps_it(extraction):
    ref = (await extract())["public_contacts"][0]["reference"]
    with SessionDB(extraction.home / "state.db") as db:
        db.create_session(SID, source="synthetic-history")
        row_id = db.append_message(SID, "assistant", content="")
        rows = [{"role": "assistant", "content": ref, "_row_id": row_id}]
        assert db.append_messages_batch(SID, rows) == 0
        stored = db.get_messages(SID)[0]
        assert PHONE in project_compaction_message_for_display(stored)["content"]
        assert contacts.DISPLAY_METADATA_KEY in rows[0]["display_metadata"]
        (extraction.home / "config.yaml").write_text("security: {}\n")
        assert db.append_messages_batch(SID, rows) == 0
        assert PHONE in project_compaction_message_for_display(rows[0])["content"]


@pytest.mark.asyncio
async def test_actual_agent_request_excludes_display_authority(extraction, monkeypatch):
    import run_agent
    from unittest.mock import MagicMock
    from tests.run_agent.test_tool_call_guardrail_runtime import _mock_response
    _, content, replay, path, _ = await save_reply(extraction, batch=True)
    monkeypatch.setattr(run_agent, "OpenAI", lambda *a, **k: MagicMock())
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda *a, **k: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda *a, **k: {})
    with SessionDB(path) as db:
        history = db.get_messages_as_conversation(SID, include_row_ids=True)
        agent = run_agent.AIAgent(api_key="synthetic", base_url="https://example.test/v1",
                                  session_id=SID, session_db=db, max_iterations=2,
                                  quiet_mode=True, skip_context_files=True, skip_memory=True)
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent._cached_system_prompt = "Synthetic display/replay check."
        monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
        captured = []
        def provider(request):
            captured.append(copy.deepcopy(request))
            return _mock_response(content="A new unrelated reply.")
        monkeypatch.setattr(agent, "_interruptible_api_call", provider)
        try:
            result = agent.run_conversation("Continue without using the old contact.", conversation_history=history, task_id=SID)
            assert result["final_response"] == "A new unrelated reply."
            assert captured
            for request in captured:
                for message in request["messages"]:
                    assert "_session_id" not in message and "_row_id" not in message
                    assert "display_metadata" not in message
                assert content in [m.get("content") for m in request["messages"]]
            durable = db.get_messages_as_conversation(SID)
            assert durable[0]["codex_message_items"] == replay
        finally:
            agent.close()


@pytest.mark.asyncio
async def test_selected_profile_history_and_dashboard_route(extraction, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.web_routers.sessions import manage_router
    from tui_gateway.server import _history_to_messages
    from hermes_constants import get_hermes_home
    _, content, _, path, _ = await save_reply(extraction)
    with SessionDB(path) as db:
        history = db.get_messages_as_conversation(SID, include_row_ids=True)
    other_home = tmp_path / "launch-profile"
    other_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    assert PHONE not in _history_to_messages(history)[0]["text"]
    projected = _history_to_messages(history, profile_home=str(extraction.home))
    assert PHONE in projected[0]["text"]
    assert get_hermes_home() == other_home

    def open_db(profile, **kwargs):
        assert profile == "reviewed"
        return SessionDB(path, read_only=True)
    def resolve_profile(profile):
        assert profile == "reviewed"
        return extraction.home
    monkeypatch.setattr(web_server, "_open_session_db_for_profile", open_db)
    monkeypatch.setattr(web_server, "_resolve_profile_dir", resolve_profile)
    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{SID}/messages?profile=reviewed")
    assert response.status_code == 200, response.text
    row = response.json()["messages"][0]
    assert row["content"] == content
    assert PHONE in row["display_content"] and "202-555-0143" not in row["display_content"]
    assert get_hermes_home() == other_home
