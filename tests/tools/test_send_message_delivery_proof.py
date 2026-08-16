"""Provider-proof contracts for cross-channel delivery receipts."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    extract_provider_delivery_proof,
    provider_delivery_proof_required,
)
from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter
from tools.send_message_tool import _normalize_delivery_envelope


def test_phone_and_direct_message_platforms_require_provider_proof():
    for platform in (
        Platform.SMS,
        Platform.SIGNAL,
        Platform.WHATSAPP,
        "photon",
        Platform.TELEGRAM,
        Platform.BLUEBUBBLES,
    ):
        assert provider_delivery_proof_required(platform) is True


def test_sms_generic_success_remains_attempted_unverified():
    result = _normalize_delivery_envelope({"success": True}, "sms")

    assert result["success"] is False
    assert result["delivery_state"] == "attempted_unverified"
    assert result["delivery_proof_missing"] is True
    assert result["provider_proof"] is None


def test_sms_provider_sid_is_accepted():
    result = _normalize_delivery_envelope(
        {"success": True, "message_id": "SM-provider-proof"},
        "sms",
    )

    assert result["success"] is True
    assert result["delivery_state"] == "accepted"
    assert result["provider_proof"] == {
        "kind": "message_id",
        "value": "SM-provider-proof",
    }


def test_signal_timestamp_is_proof_without_becoming_an_editable_message_id():
    send_result = SendResult(
        success=True,
        message_id=None,
        raw_response={"timestamp": 1723456789000},
    )

    assert extract_provider_delivery_proof(send_result, "signal") == {
        "kind": "provider_timestamp",
        "values": ["1723456789000"],
    }
    assert send_result.message_id is None


def test_signal_success_shape_without_timestamp_is_not_provider_proof():
    send_result = SendResult(
        success=True,
        message_id="not-a-signal-receipt",
        raw_response={"result": {"ok": True}},
    )

    assert extract_provider_delivery_proof(send_result, "signal") is None


def test_non_provider_adapter_ack_is_explicit_not_provider_proof():
    result = _normalize_delivery_envelope({"success": True}, "slack")

    assert result["success"] is True
    assert result["delivery_state"] == "accepted"
    assert result["provider_proof"] == {"kind": "adapter_ack"}


def test_skipped_duplicate_is_not_mislabeled_delivered():
    result = _normalize_delivery_envelope(
        {"success": True, "skipped": True, "reason": "duplicate"},
        "sms",
    )

    assert result["success"] is True
    assert result["delivery_state"] == "skipped"
    assert result["provider_proof"] is None


def test_whatsapp_cloud_contract_requires_exact_wamid_prefix():
    adapter = object.__new__(WhatsAppCloudAdapter)
    assert provider_delivery_proof_required(adapter) is True
    assert extract_provider_delivery_proof(
        SendResult(success=True, message_id="wamid.HBgMNTU1"), adapter
    ) == {"kind": "wamid", "value": "wamid.HBgMNTU1"}
    assert extract_provider_delivery_proof(
        SendResult(success=True, message_id="ordinary-id"), adapter
    ) is None
    assert extract_provider_delivery_proof(
        SendResult(success=True, message_id="ordinary-id"), "whatsapp_cloud"
    ) is None


def test_generic_adapter_ack_capability_does_not_require_provider_id():
    class AckAdapter:
        DELIVERY_PROOF_KIND = "adapter_ack"

    assert provider_delivery_proof_required(AckAdapter()) is False


class _AttemptedUnverifiedAdapter(BasePlatformAdapter):
    def __init__(self):
        self.send_calls = 0

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.send_calls += 1
        return SendResult(
            success=False,
            error="connection lost after accepted prefix",
            raw_response={"delivery_state": "attempted_unverified"},
        )


@pytest.mark.asyncio
async def test_attempted_unverified_never_retries_or_plaintext_falls_back():
    adapter = _AttemptedUnverifiedAdapter()

    result = await adapter._send_with_retry("chat", "response")

    assert result.raw_response == {"delivery_state": "attempted_unverified"}
    assert adapter.send_calls == 1
