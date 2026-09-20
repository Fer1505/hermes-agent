"""Synthetic HTTP extraction -> receipt -> SQLite/replay -> gateway/send."""
import json
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from agent import public_contacts as contacts, web_search_registry
from gateway import session_context as sc
from tools import web_result_cache, web_tools, website_policy
from tools.approval import set_current_observability_context, reset_current_observability_context

URL = "https://business.example/contact"
SID = "synthetic-contact-session"
PHONE = "+1 (202) 555-0142"


@pytest.fixture
def extraction(monkeypatch, tmp_path):
    from plugins.web import keyless_mcp
    from plugins.web.firecrawl import provider as module
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    grant = {"id": "example-business", "business": "Example Business", "phone": PHONE,
             "source_url": URL, "session_id": SID, "task": "Find the reviewed business telephone",
             "expires_at": datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()}
    state = SimpleNamespace(home=home, grant=grant, final=URL, content=f"Contact us: {PHONE}.", calls=0)
    def save():
        (home / "config.yaml").write_text(yaml.safe_dump({"security": {"public_contacts": {
            "grants": [grant], "max_age_seconds": 300,
        }}}))
    state.save = save
    save()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["url"] == URL
            state.calls += 1
            payload = json.dumps({"success": True, "data": {
                "markdown": state.content, "metadata": {"sourceURL": state.final},
                "public_contacts": [{"reference": "[public-contact:" + "a" * 64 + "]"}],
            }}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    client = module._KeylessFirecrawlClient(f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(module, "_KeylessFirecrawlClient", lambda: client)
    monkeypatch.setattr(module, "_use_keyless_ring", lambda: True)
    monkeypatch.setattr(keyless_mcp, "extract_with_failover",
                        lambda provider, urls: keyless_mcp.firecrawl_extract_keyless(urls))
    provider = module.FirecrawlWebSearchProvider()
    monkeypatch.setattr(web_search_registry, "get_provider", lambda name: provider)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_backend": provider.name})
    monkeypatch.setattr(web_tools, "async_is_safe_url", AsyncMock(
        side_effect=lambda url: str(url).startswith("https://")))
    monkeypatch.setattr(website_policy, "_cached_policy", None)
    cache = home / "web-cache"
    cache.mkdir()
    monkeypatch.setattr(web_result_cache, "_cache_dir", lambda: cache)
    tokens = [var.set(sc._UNSET) for var in sc._VAR_MAP.values()]
    bound = sc._session_context_bound.set(False)
    monkeypatch.setattr(sc, "_session_context_engaged", False)
    sc.set_session_vars(session_id=SID, platform="telegram")
    correlation = set_current_observability_context(session_id=SID, turn_id="turn-example", tool_call_id="tool-example")
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        reset_current_observability_context(correlation)
        for var, token in zip(sc._VAR_MAP.values(), tokens):
            var.reset(token)
        sc._session_context_bound.reset(bound)


async def extract():
    result = json.loads(await web_tools.web_extract_tool([URL]))
    assert "results" in result, result
    return result["results"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["security: [", "security: {public_contacts: {grants: []}}"])
async def test_current_invalid_or_revoked_policy_cannot_reuse_cached_grant(extraction, policy):
    from hermes_cli.config import load_config_readonly
    reference = (await extract())["public_contacts"][0]["reference"]
    load_config_readonly()  # Populate the ordinary last-known-good settings.
    (extraction.home / "config.yaml").write_text(policy, encoding="utf-8")
    assert contacts.render_contact_references(reference, session_id=SID) == contacts.UNAVAILABLE
    assert not (await extract()).get("public_contacts")


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", [PHONE, "+44 20 7946 0123", "(202) 555-0142 ext. 23"])
async def test_contact_survives_storage_compression_filter_and_delivery(extraction, monkeypatch, phone):
    from agent import redact
    from agent.chat_completion_helpers import build_assistant_message
    from agent.context_compressor import _redact_compaction_text
    from gateway.run import _sanitize_gateway_final_response, _redact_gateway_user_facing_secrets
    from hermes_state import SessionDB
    from tests.gateway.test_send_retry import _StubAdapter, SendResult

    monkeypatch.setattr(redact, "_REDACT_ENABLED", True)
    extraction.grant["phone"] = phone
    extraction.content = f"Business telephone: {phone}."
    extraction.save()
    result = await extract()
    reference = result["public_contacts"][0]["reference"]
    assert contacts.REFERENCE_RE.fullmatch(reference)
    assert phone not in json.dumps(result["public_contacts"])
    secret = "sk-proj-" + "x" * 48
    prose = f"Call {reference}. Private contact: 202-555-0143. Credential: {secret}"
    replay = [{"type": "message", "id": "synthetic", "content": [{"type": "output_text", "text": prose}]}]
    agent = SimpleNamespace(_extract_reasoning=lambda _: None, _strip_think_blocks=lambda v: v,
                            verbose_logging=False, reasoning_callback=None)
    normalized = build_assistant_message(agent, SimpleNamespace(
        content=prose, codex_message_items=replay, tool_calls=None), "stop")
    db_path = extraction.home / "state.db"
    with SessionDB(db_path) as db:
        db.create_session(SID, source="synthetic-public-contact")
        db.append_message(SID, "assistant", content=normalized["content"],
                          codex_message_items=normalized["codex_message_items"])
    with SessionDB(db_path) as db:
        stored = db.get_messages_as_conversation(SID)[0]
    assert stored["codex_message_items"] == replay
    assert reference in stored["content"] and reference in _redact_compaction_text(stored["content"])
    assert phone not in _redact_gateway_user_facing_secrets(stored["content"])
    reply = _sanitize_gateway_final_response("telegram", stored["content"], session_id=SID)
    assert phone in reply and URL in reply and "Verified:" in reply
    assert "202-555-0143" not in reply and secret not in reply
    adapter = _StubAdapter()
    adapter._send_results = [SendResult(success=False, error="ConnectError", retryable=True),
                             SendResult(success=True, message_id="synthetic-contact-receipt")]
    sent = await adapter._send_with_retry("synthetic-chat", reply, base_delay=0)
    assert sent.message_id == "synthetic-contact-receipt"
    assert adapter._send_calls == [("synthetic-chat", reply)] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["redirect", "private_url", "absent", "substring", "expired",
                                  "other_session", "unbound", "uncorrelated", "identifier", "credential", "query"])
async def test_unverified_contacts_cannot_issue(extraction, case):
    if case == "redirect": extraction.final = "https://unreviewed.example/contact"
    elif case == "private_url": extraction.final = "http://127.0.0.1/private"
    elif case == "absent": extraction.content = "No phone here."
    elif case == "substring": extraction.content = "ID" + PHONE + "999"
    elif case == "expired": extraction.grant["expires_at"] = "2020-01-01T00:00:00Z"
    elif case == "other_session": extraction.grant["session_id"] = "someone-else"
    elif case == "unbound": sc.clear_session_vars([])
    elif case == "uncorrelated": set_current_observability_context()
    elif case == "identifier": extraction.grant["phone"] = "2025550142"
    elif case == "credential": extraction.grant["phone"] = "sk-proj-" + "x" * 48
    elif case == "query": extraction.grant["source_url"] += "?api_key=synthetic"
    extraction.save()
    result = await extract()
    assert "public_contacts" not in result


@pytest.mark.asyncio
async def test_receipts_remain_scoped_and_revocable_after_reopen(extraction, monkeypatch, tmp_path):
    result = await extract()
    ref = result["public_contacts"][0]["reference"]
    assert contacts.render_contact_references(ref, session_id=SID).startswith("Example Business")
    for sid in ("", "foreign-session"):
        assert contacts.render_contact_references(ref, session_id=sid) == contacts.UNAVAILABLE
    with monkeypatch.context() as other:
        other.setenv("HERMES_HOME", str(tmp_path / "foreign-profile"))
        assert contacts.render_contact_references(ref, session_id=SID) == contacts.UNAVAILABLE
    extraction.grant["task"] = "A different reviewed task"
    extraction.save()
    assert contacts.render_contact_references(ref, session_id=SID) == contacts.UNAVAILABLE


@pytest.mark.asyncio
async def test_telegram_transport_payload_keeps_verified_contact(extraction):
    from unittest.mock import MagicMock
    from gateway.config import PlatformConfig
    from gateway.run import _sanitize_gateway_final_response
    from plugins.platforms.telegram.adapter import TelegramAdapter, _strip_mdv2
    ref = (await extract())["public_contacts"][0]["reference"]
    reply = _sanitize_gateway_final_response("telegram", ref + "\nPrivate: 202-555-0143", session_id=SID)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="synthetic-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
    adapter._rich_messages_enabled = False
    result = await adapter.send("12345", reply, metadata={"notify": True})
    assert result.success
    wire = adapter._bot.send_message.call_args.kwargs
    assert wire["parse_mode"] == "MarkdownV2"
    visible = _strip_mdv2(wire["text"])
    assert PHONE in visible and URL in visible
    assert "202-555-0143" not in visible and "public-contact:" not in visible



@pytest.mark.asyncio
async def test_cache_does_not_refresh_verification_age(extraction, monkeypatch):
    await extract()
    second = await extract()
    assert extraction.calls == 1 and second["cached"]
    ref = second["public_contacts"][0]["reference"]
    receipt_path = extraction.home / "public-contact-receipts" / (contacts.REFERENCE_RE.fullmatch(ref)[1] + ".json")
    receipt = json.loads(receipt_path.read_text())
    assert receipt["fetched_at"] == second["cache_stored_at"]
    assert receipt["tool_call_id"] == "tool-example" and receipt["turn_id"] == "turn-example"
    now = time.time()
    monkeypatch.setattr(contacts.time, "time", lambda: now + 301)
    assert contacts.render_contact_references(ref, session_id=SID) == contacts.UNAVAILABLE
    third = await extract()
    assert extraction.calls == 1 and "public_contacts" not in third


@pytest.mark.asyncio
async def test_fake_reference_and_storage_failure_do_not_authorize(extraction):
    fake = "[public-contact:" + "a" * 64 + "]"
    assert contacts.render_contact_references(fake, session_id=SID) == contacts.UNAVAILABLE
    (extraction.home / "public-contact-receipts").write_text("not a directory")
    result = await extract()
    assert result["content"] == extraction.content and "public_contacts" not in result


def test_receipt_files_are_not_model_writable(extraction):
    from agent.file_safety import ProtectedFileOperation, decide_protected_control_file
    for operation in (ProtectedFileOperation.WRITE, ProtectedFileOperation.RENAME, ProtectedFileOperation.DELETE):
        decision = decide_protected_control_file(operation, extraction.home / "public-contact-receipts" / "example.json")
        assert decision.protected and not decision.allowed


@pytest.mark.asyncio
async def test_real_dispatcher_supplies_receipt_correlation(extraction):
    import asyncio
    from model_tools import handle_function_call
    token = set_current_observability_context()
    try:
        raw = await asyncio.to_thread(
            handle_function_call, "web_extract", {"urls": [URL]},
            task_id=SID, session_id=SID, turn_id="dispatched-turn",
            tool_call_id="dispatched-tool", enabled_tools=["web_extract"],
        )
    finally:
        reset_current_observability_context(token)
    result = json.loads(raw)["results"][0]
    ref = result["public_contacts"][0]["reference"]
    path = extraction.home / "public-contact-receipts" / (contacts.REFERENCE_RE.fullmatch(ref)[1] + ".json")
    receipt = json.loads(path.read_text())
    assert receipt["turn_id"] == "dispatched-turn"
    assert receipt["tool_call_id"] == "dispatched-tool"
    assert receipt["session_id"] == SID
    assert PHONE in contacts.render_contact_references(ref, session_id=SID)


@pytest.mark.asyncio
async def test_fresh_process_can_render_scoped_receipt(extraction):
    import os
    import subprocess
    import sys
    result = await extract()
    ref = result["public_contacts"][0]["reference"]
    code = (
        "import json,sys; from gateway.run import _sanitize_gateway_final_response; "
        "ref,sid=json.load(sys.stdin); "
        "print(_sanitize_gateway_final_response('telegram', ref, session_id=sid))"
    )
    completed = subprocess.run([sys.executable, "-c", code], input=json.dumps([ref, SID]),
                               text=True, capture_output=True, env=dict(os.environ), timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert PHONE in completed.stdout and URL in completed.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["symlink", "corrupt", "wrong_profile", "wrong_source", "future", "bad_expiry"])
async def test_invalid_receipts_fail_closed(extraction, case, tmp_path):
    ref = (await extract())["public_contacts"][0]["reference"]
    path = extraction.home / "public-contact-receipts" / (contacts.REFERENCE_RE.fullmatch(ref)[1] + ".json")
    original = path.read_text()
    receipt = json.loads(original)
    if case == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text(original)
        path.unlink()
        path.symlink_to(target)
    elif case == "corrupt":
        path.write_text("{broken")
    else:
        if case == "wrong_profile": receipt["profile"] = "foreign-profile"
        elif case == "wrong_source": receipt["source_url"] = "https://unreviewed.example/"
        elif case == "future": receipt["fetched_at"] = time.time() + 100
        elif case == "bad_expiry": receipt["expires_at"] = "not a timestamp"
        path.write_text(json.dumps(receipt))
    assert contacts.render_contact_references(ref, session_id=SID) == contacts.UNAVAILABLE
