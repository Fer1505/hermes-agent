"""Audited owner-facing Telegram durable-inbox inspection and replay."""

from __future__ import annotations

import json
import os
import hashlib


def build_telegram_inbox_parser(subparsers, *, cmd_telegram_inbox) -> None:
    parser = subparsers.add_parser(
        "telegram-inbox",
        help="Inspect or explicitly replay quarantined Telegram updates",
    )
    parser.add_argument("action", choices=("list", "show", "replay"))
    parser.add_argument("update_id", nargs="?", type=int)
    parser.add_argument("--reason", required=True, help="Reason recorded in the local audit ledger")
    parser.add_argument("--confirm", help="For replay, exactly REPLAY:<update_id>")
    parser.add_argument("--limit", type=int, default=100)
    parser.set_defaults(func=cmd_telegram_inbox)


def cmd_telegram_inbox(args) -> int:
    from hermes_constants import get_hermes_home
    from plugins.platforms.telegram.inbox import TelegramInbox

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is required to select the bot-scoped durable inbox"
        )
    profile_home = get_hermes_home()
    inbox = TelegramInbox.for_profile_home(
        profile_home,
        profile_id=hashlib.sha256(
            str(profile_home.resolve()).encode("utf-8")
        ).hexdigest(),
        bot_token=token,
    )
    if args.action == "list":
        inbox.audit_operator_action("list_dead_letters", reason=args.reason)
        print(json.dumps(inbox.list_dead_letters(limit=args.limit), indent=2))
        return 0
    if args.update_id is None:
        raise ValueError(f"update_id is required for {args.action}")
    if args.action == "show":
        inbox.audit_operator_action(
            "show_dead_letter", reason=args.reason, update_id=args.update_id
        )
        record = inbox.record(args.update_id)
        if record is not None and "payload" in record:
            record = dict(record)
            record.pop("payload", None)
            record["payload_redacted"] = True
        print(json.dumps(record, indent=2))
        return 0 if record is not None else 1
    replayed = inbox.replay_dead_letter(
        args.update_id,
        reason=args.reason,
        confirmation=args.confirm or "",
    )
    print(json.dumps({"update_id": args.update_id, "replayed": replayed}))
    return 0 if replayed else 1
