import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult, is_no_reply_sentinel
from gateway.session import SessionSource, build_session_key


def test_exact_no_reply_sentinel_is_suppressed():
    assert is_no_reply_sentinel("NO_REPLY") is True
    assert is_no_reply_sentinel("  no_reply\n") is True


def test_non_exact_no_reply_text_is_not_suppressed():
    assert is_no_reply_sentinel("NO_REPLY, I cannot help") is False
    assert is_no_reply_sentinel("No reply is needed.") is False
    assert is_no_reply_sentinel("") is False
    assert is_no_reply_sentinel(None) is False


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.sent: list[str] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="sent")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "group"}


@pytest.mark.asyncio
async def test_background_delivery_suppresses_no_reply():
    adapter = _Adapter()

    async def handler(_event):
        return "NO_REPLY"

    adapter.set_message_handler(handler)
    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1003742307678",
            chat_type="group",
            user_id="8748120144",
            user_name="Franklin",
        ),
        message_id="42",
    )
    session_key = build_session_key(event.source)

    await adapter._process_message_background(event, session_key)

    assert adapter.sent == []
