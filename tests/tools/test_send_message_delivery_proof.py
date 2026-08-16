"""Provider-proof contracts for cross-channel delivery receipts."""

from gateway.config import Platform
from gateway.platforms.base import (
    SendResult,
    extract_provider_delivery_proof,
    provider_delivery_proof_required,
)
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
