from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
from tests.gateway.test_whatsapp_formatting import _AsyncCM, _make_adapter


class TestWhatsAppNativeFormatting:

    def test_invisible_unicode_prefixes_are_sanitized(self):
        adapter = _make_adapter()

        assert adapter.format_message("\u2060\u202ftext") == " text"


@pytest.mark.asyncio
async def test_send_location_posts_to_bridge_location_endpoint():
    adapter = _make_adapter()
    resp = MagicMock(status=200)
    resp.json = AsyncMock(return_value={"success": True, "messageId": "loc-msg"})
    adapter._http_session.post = MagicMock(return_value=_AsyncCM(resp))

    result = await adapter.send_location(
        "15551234567",
        41.015,
        28.979,
        name="HQ",
        address="Example Street",
    )

    assert result.success
    assert result.message_id == "loc-msg"
    call = adapter._http_session.post.call_args
    assert call.args[0] == "http://127.0.0.1:3000/send-location"
    assert call.kwargs["json"] == {
        "chatId": "15551234567@s.whatsapp.net",
        "latitude": 41.015,
        "longitude": 28.979,
        "name": "HQ",
        "address": "Example Street",
    }


@pytest.mark.asyncio
async def test_send_missing_bridge_message_id_is_attempted_unverified():
    adapter = _make_adapter()
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"success": True})
    adapter._http_session.post = MagicMock(return_value=_AsyncCM(response))

    result = await adapter.send("15551234567", "hello")

    assert result.success is False
    assert result.raw_response == {"delivery_state": "attempted_unverified"}


@pytest.mark.asyncio
async def test_send_partial_bridge_chunk_failure_is_attempted_unverified():
    adapter = _make_adapter()
    first = MagicMock(status=200)
    first.json = AsyncMock(return_value={"success": True, "messageId": "msg-1"})
    second = MagicMock(status=500)
    second.text = AsyncMock(return_value="bridge rejected second chunk")
    adapter._http_session.post = MagicMock(
        side_effect=[_AsyncCM(first), _AsyncCM(second)]
    )

    result = await adapter.send(
        "15551234567", "x" * (adapter.MAX_MESSAGE_LENGTH + 100)
    )

    assert result.success is False
    assert result.raw_response == {"delivery_state": "attempted_unverified"}

