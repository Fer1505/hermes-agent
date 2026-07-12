import json

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter(tmp_path, *, profile="atlas", peers=None):
    return TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "peer_relay": {
                    "enabled": True,
                    "profile": profile,
                    "display_name": profile.title(),
                    "home": str(tmp_path / profile),
                    "groups": ["-100"],
                    "peers": peers or [],
                    "poll_interval_seconds": 0.1,
                    "max_depth": 4,
                }
            },
        )
    )


@pytest.mark.asyncio
async def test_peer_relay_writes_final_group_response_to_peer_inbox(tmp_path):
    peer_home = tmp_path / "prometheus"
    adapter = _adapter(
        tmp_path,
        profile="atlas",
        peers=[{"profile": "prometheus", "home": str(peer_home)}],
    )
    source = adapter.build_source(
        chat_id="-100",
        chat_name="Franklin, Atlas and Prometheus",
        chat_type="group",
        user_id="8081078155",
        user_name="Franklin",
        message_id="11",
    )
    event = MessageEvent(
        text="atlas, ask prometheus a question",
        message_type=MessageType.TEXT,
        source=source,
        message_id="11",
        channel_prompt="group prompt",
    )

    await adapter._after_final_text_response_sent(
        event,
        "Prometheus, what proof should Atlas verify?",
        SendResult(success=True, message_id="12"),
    )

    inbox = peer_home / "telegram_peer_relay" / "inbox"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["origin_profile"] == "atlas"
    assert payload["origin_display_name"] == "Atlas"
    assert payload["chat_id"] == "-100"
    assert payload["trigger_user_id"] == "8081078155"
    assert payload["message_id"] == "12"
    assert payload["relay_depth"] == 1


@pytest.mark.asyncio
async def test_peer_relay_inbox_processes_synthetic_bot_event(tmp_path):
    adapter = _adapter(tmp_path, profile="prometheus")
    seen = []

    async def capture(event):
        seen.append(event)

    adapter.handle_message = capture
    inbox = tmp_path / "prometheus" / "telegram_peer_relay" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "relay.json").write_text(
        json.dumps(
            {
                "schema": "hermes-agent.telegram-peer-relay.v1",
                "origin_profile": "atlas",
                "origin_display_name": "Atlas",
                "chat_id": "-100",
                "chat_name": "Franklin, Atlas and Prometheus",
                "chat_type": "group",
                "message_id": "12",
                "text": "Prometheus, what proof should Atlas verify?",
                "trigger_user_id": "8081078155",
                "trigger_user_name": "Franklin",
                "channel_prompt": "group prompt",
                "relay_depth": 1,
            }
        ),
        encoding="utf-8",
    )

    assert await adapter._process_peer_relay_inbox_once() == 1
    assert len(seen) == 1
    event = seen[0]
    assert event.text == "Atlas: Prometheus, what proof should Atlas verify?"
    assert event.internal is True
    assert event.channel_prompt == "group prompt"
    assert event.message_id == "12"
    assert event.source.chat_id == "-100"
    assert event.source.user_id == "8081078155"
    assert event.source.user_name == "Atlas"
    assert event.source.is_bot is True
