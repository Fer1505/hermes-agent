from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli.subcommands.telegram_inbox import (
    build_telegram_inbox_parser,
    cmd_telegram_inbox,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_telegram_inbox_parser(
        subparsers, cmd_telegram_inbox=cmd_telegram_inbox
    )
    return parser


def test_operator_command_requires_audited_reason():
    with pytest.raises(SystemExit):
        _parser().parse_args(["telegram-inbox", "list"])


def test_show_audits_owner_action_without_exposing_bot_token(
    monkeypatch, tmp_path, capsys
):
    from plugins.platforms.telegram.inbox import TelegramInbox

    inbox = MagicMock()
    inbox.record.return_value = {
        "update_id": 41,
        "state": "dead_letter",
        "payload": {"message": {"text": "sensitive content"}},
        "payload_sha256": "abc123",
    }
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-bot-token")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(TelegramInbox, "for_profile_home", lambda *a, **k: inbox)
    args = SimpleNamespace(
        action="show", update_id=41, reason="incident review", confirm=None, limit=100
    )

    assert cmd_telegram_inbox(args) == 0

    inbox.audit_operator_action.assert_called_once_with(
        "show_dead_letter", reason="incident review", update_id=41
    )
    output = capsys.readouterr().out
    assert "secret-bot-token" not in output
    assert "sensitive content" not in output
    assert '"payload_redacted": true' in output


def test_replay_passes_exact_owner_confirmation(monkeypatch, tmp_path):
    from plugins.platforms.telegram.inbox import TelegramInbox

    inbox = MagicMock()
    inbox.replay_dead_letter.return_value = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-bot-token")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(TelegramInbox, "for_profile_home", lambda *a, **k: inbox)
    args = SimpleNamespace(
        action="replay",
        update_id=41,
        reason="owner accepted duplicate risk",
        confirm="REPLAY:41",
        limit=100,
    )

    assert cmd_telegram_inbox(args) == 0
    inbox.replay_dead_letter.assert_called_once_with(
        41,
        reason="owner accepted duplicate risk",
        confirmation="REPLAY:41",
    )
