"""Tests for gateway/channel_directory.py — channel resolution and display."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.platforms.base import Platform
from gateway.channel_directory import (
    build_channel_directory,
    channel_delivery_status,
    lookup_channel_type,
    mark_channel_delivery_failed,
    mark_channel_delivery_success,
    resolve_channel_name,
    format_directory_for_display,
    load_directory,
    _apply_channel_aliases,
    _build_from_sessions,
    _build_slack,
)


import pytest


@pytest.fixture(autouse=True)
def _isolate_channel_aliases(tmp_path_factory):
    """Point the alias overlay at a nonexistent path by default so a real
    ~/.hermes/channel_aliases.json never leaks into directory tests. Tests
    that exercise aliases patch CHANNEL_ALIASES_PATH themselves inside the
    test body, which takes precedence over this outer patch."""
    missing = tmp_path_factory.mktemp("aliases") / "none.json"
    with patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", missing):
        yield


def _write_directory(tmp_path, platforms):
    """Helper to write a fake channel directory."""
    data = {"updated_at": "2026-01-01T00:00:00", "platforms": platforms}
    cache_file = tmp_path / "channel_directory.json"
    cache_file.write_text(json.dumps(data))
    return cache_file


class TestLoadDirectory:
    def test_missing_file(self, tmp_path):
        with patch("gateway.channel_directory.DIRECTORY_PATH", tmp_path / "nope.json"):
            result = load_directory()
        assert result["updated_at"] is None
        assert result["platforms"] == {}

    def test_valid_file(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "telegram": [{"id": "123", "name": "John", "type": "dm"}]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = load_directory()
        assert result["platforms"]["telegram"][0]["name"] == "John"

    def test_corrupt_file(self, tmp_path):
        cache_file = tmp_path / "channel_directory.json"
        cache_file.write_text("{bad json")
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = load_directory()
        assert result["updated_at"] is None


class TestBuildChannelDirectoryWrites:
    def test_failed_write_preserves_previous_cache(self, tmp_path, monkeypatch):
        cache_file = _write_directory(tmp_path, {
            "telegram": [{"id": "123", "name": "Alice", "type": "dm"}]
        })
        previous = json.loads(cache_file.read_text())

        def broken_dump(data, fp, *args, **kwargs):
            fp.write('{"updated_at":')
            fp.flush()
            raise OSError("disk full")

        monkeypatch.setattr(json, "dump", broken_dump)

        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            asyncio.run(build_channel_directory({}))
            result = load_directory()

        assert result == previous

    def test_preserves_seeded_bluebubbles_targets(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "bluebubbles": [
                {
                    "id": "franklin@example.com",
                    "name": "Franklin De La Rosa",
                    "type": "dm",
                    "thread_id": None,
                }
            ]
        })

        with (
            patch("gateway.channel_directory.DIRECTORY_PATH", cache_file),
            patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}),
        ):
            result = asyncio.run(build_channel_directory({}))

        assert result["platforms"]["bluebubbles"] == [
            {
                "id": "franklin@example.com",
                "name": "Franklin De La Rosa",
                "type": "dm",
                "thread_id": None,
            }
        ]

    def test_recovers_stale_delivery_metadata_from_send_failure_transcripts(self, tmp_path):
        cache_file = _write_directory(tmp_path, {})
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    "group": {
                        "origin": {
                            "platform": "telegram",
                            "chat_id": "-1003579233553",
                            "chat_name": "Franklin, Hermes and Athena",
                        },
                        "chat_type": "group",
                    }
                }
            )
        )
        (sessions_dir / "session.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "send_message",
                                        "arguments": json.dumps(
                                            {
                                                "action": "send",
                                                "target": "telegram:Franklin, Hermes and Athena",
                                                "message": "Status",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    ),
                    json.dumps(
                        {
                            "role": "tool",
                            "name": "send_message",
                            "tool_call_id": "call-1",
                            "content": '{"error": "Telegram send failed: Chat not found"}',
                            "timestamp": "2026-06-08T15:23:23.966787",
                        }
                    ),
                ]
            )
        )

        with (
            patch("gateway.channel_directory.DIRECTORY_PATH", cache_file),
            patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}),
        ):
            result = asyncio.run(build_channel_directory({Platform.TELEGRAM: object()}))

        telegram = result["platforms"]["telegram"]
        assert telegram[0]["delivery_status"] == "stale"
        assert telegram[0]["last_delivery_error"] == "Telegram send failed: Chat not found"

    def test_later_success_clears_recovered_stale_delivery_metadata(self, tmp_path):
        cache_file = _write_directory(tmp_path, {})
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    "group": {
                        "origin": {
                            "platform": "telegram",
                            "chat_id": "-1003579233553",
                            "chat_name": "Franklin, Hermes and Athena",
                        },
                        "chat_type": "group",
                    }
                }
            )
        )
        (sessions_dir / "session.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "send_message",
                                        "arguments": json.dumps(
                                            {
                                                "action": "send",
                                                "target": "telegram:-1003579233553",
                                                "message": "Status",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    ),
                    json.dumps(
                        {
                            "role": "tool",
                            "name": "send_message",
                            "tool_call_id": "call-1",
                            "content": '{"error": "Telegram send failed: Chat not found"}',
                            "timestamp": "2026-06-08T15:23:23.966787",
                        }
                    ),
                    json.dumps(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-2",
                                    "function": {
                                        "name": "send_message",
                                        "arguments": json.dumps(
                                            {
                                                "action": "send",
                                                "target": "telegram:-1003579233553",
                                                "message": "Status",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    ),
                    json.dumps(
                        {
                            "role": "tool",
                            "name": "send_message",
                            "tool_call_id": "call-2",
                            "content": '{"success": true, "message_id": "777"}',
                            "timestamp": "2026-06-08T15:24:00.000000",
                        }
                    ),
                ]
            )
        )

        with (
            patch("gateway.channel_directory.DIRECTORY_PATH", cache_file),
            patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}),
        ):
            result = asyncio.run(build_channel_directory({Platform.TELEGRAM: object()}))

        telegram = result["platforms"]["telegram"]
        assert "delivery_status" not in telegram[0]


class TestResolveChannelName:
    def _setup(self, tmp_path, platforms):
        cache_file = _write_directory(tmp_path, platforms)
        return patch("gateway.channel_directory.DIRECTORY_PATH", cache_file)

    def test_exact_match(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "111", "name": "bot-home", "guild": "MyServer", "type": "channel"},
                {"id": "222", "name": "general", "guild": "MyServer", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("discord", "bot-home") == "111"
            assert resolve_channel_name("discord", "#bot-home") == "111"

    def test_case_insensitive(self, tmp_path):
        platforms = {
            "slack": [{"id": "C01", "name": "Engineering", "type": "channel"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("slack", "engineering") == "C01"
            assert resolve_channel_name("slack", "ENGINEERING") == "C01"

    def test_guild_qualified_match(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "111", "name": "general", "guild": "ServerA", "type": "channel"},
                {"id": "222", "name": "general", "guild": "ServerB", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("discord", "ServerA/general") == "111"
            assert resolve_channel_name("discord", "ServerB/general") == "222"

    def test_prefix_match_unambiguous(self, tmp_path):
        platforms = {
            "slack": [
                {"id": "C01", "name": "engineering-backend", "type": "channel"},
                {"id": "C02", "name": "design-team", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            # "engineering" prefix matches only one channel
            assert resolve_channel_name("slack", "engineering") == "C01"

    def test_prefix_match_ambiguous_returns_none(self, tmp_path):
        platforms = {
            "slack": [
                {"id": "C01", "name": "eng-backend", "type": "channel"},
                {"id": "C02", "name": "eng-frontend", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("slack", "eng") is None

    def test_no_channels_returns_none(self, tmp_path):
        with self._setup(tmp_path, {}):
            assert resolve_channel_name("telegram", "someone") is None

    def test_no_match_returns_none(self, tmp_path):
        platforms = {
            "telegram": [{"id": "123", "name": "John", "type": "dm"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "nonexistent") is None

    def test_topic_name_resolves_to_composite_id(self, tmp_path):
        platforms = {
            "telegram": [{"id": "-1001:17585", "name": "Coaching Chat / topic 17585", "type": "group"}]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "Coaching Chat / topic 17585") == "-1001:17585"

    def test_id_match_takes_precedence_over_name(self, tmp_path):
        """A raw channel ID resolves to itself, even when a different
        channel happens to be named the same string. Case-sensitive: Slack
        IDs are uppercase and must not be normalized away."""
        platforms = {
            "slack": [
                {"id": "C0B0QV5434G", "name": "engineering", "type": "channel"},
                {"id": "C99", "name": "c0b0qv5434g", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("slack", "C0B0QV5434G") == "C0B0QV5434G"
            # Lowercase still falls through to name matching (case-insensitive)
            assert resolve_channel_name("slack", "c0b0qv5434g") == "C99"

    def test_display_label_with_type_suffix_resolves(self, tmp_path):
        platforms = {
            "telegram": [
                {"id": "123", "name": "Alice", "type": "dm"},
                {"id": "456", "name": "Dev Group", "type": "group"},
                {"id": "-1001:17585", "name": "Coaching Chat / topic 17585", "type": "group"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "Alice (dm)") == "123"
            assert resolve_channel_name("telegram", "Dev Group (group)") == "456"
            assert resolve_channel_name("telegram", "Coaching Chat / topic 17585 (group)") == "-1001:17585"

    def test_stale_delivery_labels_do_not_resolve_by_name(self, tmp_path):
        platforms = {
            "telegram": [
                {
                    "id": "-100999",
                    "name": "Old Group",
                    "type": "group",
                    "delivery_status": "stale",
                    "last_delivery_error": "Chat not found",
                },
            ]
        }
        with self._setup(tmp_path, platforms):
            assert resolve_channel_name("telegram", "Old Group") is None
            assert resolve_channel_name("telegram", "Old Group (group)") is None
            assert resolve_channel_name("telegram", "-100999") == "-100999"

    def test_channel_delivery_status_reports_stale_metadata(self, tmp_path):
        platforms = {
            "telegram": [
                {
                    "id": "-100999",
                    "name": "Old Group",
                    "type": "group",
                    "delivery_status": "stale",
                    "stale_reason": "delivery_failed",
                    "last_delivery_error": "Chat not found",
                    "last_delivery_failed_at": "2026-01-01T00:00:00",
                },
            ]
        }
        with self._setup(tmp_path, platforms):
            status = channel_delivery_status("telegram", "-100999")

        assert status["delivery_status"] == "stale"
        assert status["last_delivery_error"] == "Chat not found"


class TestDeliveryStaleMetadata:
    def _setup(self, tmp_path, platforms):
        cache_file = _write_directory(tmp_path, platforms)
        return patch("gateway.channel_directory.DIRECTORY_PATH", cache_file)

    def test_permanent_send_failure_marks_entry_stale(self, tmp_path):
        with self._setup(tmp_path, {
            "telegram": [{"id": "-100999", "name": "Old Group", "type": "group"}],
        }):
            assert mark_channel_delivery_failed("telegram", "-100999", "Chat not found") is True
            result = load_directory()

        entry = result["platforms"]["telegram"][0]
        assert entry["delivery_status"] == "stale"
        assert entry["stale_reason"] == "delivery_failed"
        assert "Chat not found" in entry["last_delivery_error"]
        assert entry["last_delivery_failed_at"]

    def test_transient_send_failure_does_not_mark_stale(self, tmp_path):
        with self._setup(tmp_path, {
            "telegram": [{"id": "-100999", "name": "Old Group", "type": "group"}],
        }):
            assert mark_channel_delivery_failed("telegram", "-100999", "Timed out") is False
            result = load_directory()

        assert "delivery_status" not in result["platforms"]["telegram"][0]

    def test_success_clears_stale_delivery_metadata(self, tmp_path):
        with self._setup(tmp_path, {
            "telegram": [
                {
                    "id": "-100999",
                    "name": "Old Group",
                    "type": "group",
                    "delivery_status": "stale",
                    "stale_reason": "delivery_failed",
                    "last_delivery_error": "Chat not found",
                    "last_delivery_failed_at": "2026-01-01T00:00:00",
                },
            ],
        }):
            assert mark_channel_delivery_success("telegram", "-100999") is True
            result = load_directory()

        entry = result["platforms"]["telegram"][0]
        assert "delivery_status" not in entry
        assert "last_delivery_error" not in entry

    def test_build_preserves_stale_metadata_from_previous_directory(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "telegram": [
                {
                    "id": "-100999",
                    "name": "Old Group",
                    "type": "group",
                    "delivery_status": "stale",
                    "stale_reason": "delivery_failed",
                    "last_delivery_error": "Chat not found",
                    "last_delivery_failed_at": "2026-01-01T00:00:00",
                },
            ],
        })
        TestBuildFromSessions()._write_sessions(tmp_path, {
            "session_1": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-100999",
                    "chat_name": "Old Group",
                },
                "chat_type": "group",
            },
        })

        with (
            patch("gateway.channel_directory.DIRECTORY_PATH", cache_file),
            patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}),
        ):
            result = asyncio.run(build_channel_directory({Platform.TELEGRAM: object()}))

        entry = result["platforms"]["telegram"][0]
        assert entry["delivery_status"] == "stale"
        assert entry["last_delivery_error"] == "Chat not found"


class TestBuildFromSessions:
    def _write_sessions(self, tmp_path, sessions_data):
        """Write sessions.json at the path _build_from_sessions expects."""
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps(sessions_data))

    def test_builds_from_sessions_json(self, tmp_path):
        self._write_sessions(tmp_path, {
            "session_1": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "12345",
                    "chat_name": "Alice",
                },
                "chat_type": "dm",
            },
            "session_2": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "67890",
                    "user_name": "Bob",
                },
                "chat_type": "group",
            },
            "session_3": {
                "origin": {
                    "platform": "discord",
                    "chat_id": "99999",
                },
            },
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")

        assert len(entries) == 2
        names = {e["name"] for e in entries}
        assert "Alice" in names
        assert "Bob" in names

    def test_missing_sessions_file(self, tmp_path):
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")
        assert entries == []

    def test_deduplication_by_chat_id(self, tmp_path):
        self._write_sessions(tmp_path, {
            "s1": {"origin": {"platform": "telegram", "chat_id": "123", "chat_name": "X"}},
            "s2": {"origin": {"platform": "telegram", "chat_id": "123", "chat_name": "X"}},
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")

        assert len(entries) == 1

    def test_keeps_distinct_topics_with_same_chat_id(self, tmp_path):
        self._write_sessions(tmp_path, {
            "group_root": {
                "origin": {"platform": "telegram", "chat_id": "-1001", "chat_name": "Coaching Chat"},
                "chat_type": "group",
            },
            "topic_a": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "chat_name": "Coaching Chat",
                    "thread_id": "17585",
                },
                "chat_type": "group",
            },
            "topic_b": {
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "chat_name": "Coaching Chat",
                    "thread_id": "17587",
                },
                "chat_type": "group",
            },
        })

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("telegram")

        ids = {entry["id"] for entry in entries}
        names = {entry["name"] for entry in entries}
        assert ids == {"-1001", "-1001:17585", "-1001:17587"}
        assert "Coaching Chat" in names
        assert "Coaching Chat / topic 17585" in names
        assert "Coaching Chat / topic 17587" in names


class TestFormatDirectoryForDisplay:
    def test_empty_directory(self, tmp_path):
        with patch("gateway.channel_directory.DIRECTORY_PATH", tmp_path / "nope.json"):
            result = format_directory_for_display()
        assert "No messaging platforms" in result

    def test_telegram_display(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "telegram": [
                {"id": "123", "name": "Alice", "type": "dm"},
                {"id": "456", "name": "Dev Group", "type": "group"},
                {"id": "-1001:17585", "name": "Coaching Chat / topic 17585", "type": "group"},
            ]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = format_directory_for_display()

        assert "Telegram:" in result
        assert "telegram:Alice" in result
        assert "telegram:Dev Group" in result
        assert "telegram:Coaching Chat / topic 17585" in result

    def test_stale_targets_are_separated_from_usable_display(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "telegram": [
                {"id": "123", "name": "Alice", "type": "dm"},
                {
                    "id": "-100999",
                    "name": "Old Group",
                    "type": "group",
                    "delivery_status": "stale",
                    "last_delivery_error": "Chat not found",
                },
            ]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = format_directory_for_display()

        active_section = result.split("Unavailable stale targets", 1)[0]
        assert "telegram:Alice" in active_section
        assert "telegram:Old Group" not in active_section
        assert "Unavailable stale targets" in result
        assert "telegram:Old Group (group)" in result
        assert "do not use" in result

    def test_discord_grouped_by_guild(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "discord": [
                {"id": "1", "name": "general", "guild": "Server1", "type": "channel"},
                {"id": "2", "name": "bot-home", "guild": "Server1", "type": "channel"},
                {"id": "3", "name": "chat", "guild": "Server2", "type": "channel"},
            ]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            result = format_directory_for_display()

        assert "Discord (Server1):" in result
        assert "Discord (Server2):" in result
        assert "discord:#general" in result


class TestLookupChannelType:
    def _setup(self, tmp_path, platforms):
        cache_file = _write_directory(tmp_path, platforms)
        return patch("gateway.channel_directory.DIRECTORY_PATH", cache_file)

    def test_forum_channel(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "100", "name": "ideas", "guild": "Server1", "type": "forum"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "100") == "forum"

    def test_regular_channel(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "200", "name": "general", "guild": "Server1", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "200") == "channel"

    def test_unknown_chat_id_returns_none(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "200", "name": "general", "guild": "Server1", "type": "channel"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "999") is None

    def test_unknown_platform_returns_none(self, tmp_path):
        with self._setup(tmp_path, {}):
            assert lookup_channel_type("discord", "100") is None

    def test_channel_without_type_key_returns_none(self, tmp_path):
        platforms = {
            "discord": [
                {"id": "300", "name": "general", "guild": "Server1"},
            ]
        }
        with self._setup(tmp_path, platforms):
            assert lookup_channel_type("discord", "300") is None


def _make_slack_adapter(team_clients):
    """Build a stand-in for SlackAdapter exposing only ``_team_clients``."""
    return SimpleNamespace(_team_clients=team_clients)


def _make_slack_client(pages):
    """Build an AsyncWebClient mock whose ``users_conversations`` returns pages."""
    client = MagicMock()
    client.users_conversations = AsyncMock(side_effect=pages)
    return client


class TestBuildSlack:
    """_build_slack actually calls users.conversations on each workspace client."""

    def test_no_team_clients_falls_back_to_sessions(self, tmp_path):
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps({
            "s1": {"origin": {"platform": "slack", "chat_id": "D123", "chat_name": "Alice"}},
        }))

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({})))

        assert len(entries) == 1
        assert entries[0]["id"] == "D123"

    def test_lists_channels_from_users_conversations(self, tmp_path):
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [
                    {"id": "C0B0QV5434G", "name": "engineering", "is_private": False},
                    {"id": "G123ABCDEF", "name": "secret-chat", "is_private": True},
                ],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        ids = {e["id"] for e in entries}
        assert ids == {"C0B0QV5434G", "G123ABCDEF"}
        types = {e["id"]: e["type"] for e in entries}
        assert types["C0B0QV5434G"] == "channel"
        assert types["G123ABCDEF"] == "private"
        client.users_conversations.assert_awaited_once()

    def test_paginates_via_response_metadata_cursor(self, tmp_path):
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [{"id": "C001", "name": "first", "is_private": False}],
                "response_metadata": {"next_cursor": "cur1"},
            },
            {
                "ok": True,
                "channels": [{"id": "C002", "name": "second", "is_private": False}],
                "response_metadata": {"next_cursor": ""},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        assert {e["id"] for e in entries} == {"C001", "C002"}
        assert client.users_conversations.await_count == 2

    def test_per_workspace_error_does_not_block_others(self, tmp_path):
        bad = MagicMock()
        bad.users_conversations = AsyncMock(side_effect=RuntimeError("boom"))
        good = _make_slack_client([
            {
                "ok": True,
                "channels": [{"id": "C999", "name": "ok-channel", "is_private": False}],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"BAD": bad, "GOOD": good})))

        assert {e["id"] for e in entries} == {"C999"}

    def test_session_dms_merged_when_not_in_api_results(self, tmp_path):
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps({
            "s1": {"origin": {"platform": "slack", "chat_id": "D456", "chat_name": "Bob"}},
            "dup": {"origin": {"platform": "slack", "chat_id": "C001", "chat_name": "first"}},
        }))
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [{"id": "C001", "name": "first", "is_private": False}],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        ids = {e["id"] for e in entries}
        assert "C001" in ids and "D456" in ids
        # Channel ID from API should not be duplicated by the session merge
        assert sum(1 for e in entries if e["id"] == "C001") == 1

    def test_skips_channels_with_no_id_or_name(self, tmp_path):
        client = _make_slack_client([
            {
                "ok": True,
                "channels": [
                    {"id": "C001", "name": "good", "is_private": False},
                    {"id": "", "name": "no-id"},
                    {"id": "C002"},  # no name (e.g. IM)
                ],
                "response_metadata": {},
            },
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        assert {e["id"] for e in entries} == {"C001"}

    def test_response_not_ok_breaks_pagination_for_that_workspace(self, tmp_path):
        client = _make_slack_client([
            {"ok": False, "error": "missing_scope"},
        ])
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = asyncio.run(_build_slack(_make_slack_adapter({"T1": client})))

        assert entries == []


class TestChannelAliases:
    """The user-maintained alias overlay (channel_aliases.json) gives durable
    friendly names that survive the timed directory rebuild."""

    def _setup_aliases(self, tmp_path, aliases):
        alias_file = tmp_path / "channel_aliases.json"
        alias_file.write_text(json.dumps(aliases))
        return patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", alias_file)

    def test_alias_renames_existing_entry_on_load(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "whatsapp": [{"id": "120363@g.us", "name": "120363", "type": "group"}]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             self._setup_aliases(tmp_path, {"whatsapp": {"120363@g.us": "general"}}):
            result = load_directory()
            assert result["platforms"]["whatsapp"][0]["name"] == "general"
            # And the friendly name resolves back to the JID
            assert resolve_channel_name("whatsapp", "general") == "120363@g.us"
            assert resolve_channel_name("whatsapp", "GENERAL") == "120363@g.us"

    def test_alias_injects_undiscovered_group(self, tmp_path):
        """A group named in the alias file but not yet seen in any session is
        still addressable by name (pre-naming before first traffic)."""
        cache_file = _write_directory(tmp_path, {"whatsapp": []})
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             self._setup_aliases(tmp_path, {"whatsapp": {"999@g.us": "marketing"}}):
            assert resolve_channel_name("whatsapp", "marketing") == "999@g.us"
            entries = load_directory()["platforms"]["whatsapp"]
            injected = [e for e in entries if e["id"] == "999@g.us"]
            assert injected and injected[0]["type"] == "group"

    def test_no_alias_file_is_noop(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "whatsapp": [{"id": "120363@g.us", "name": "120363", "type": "group"}]
        })
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", tmp_path / "nope.json"):
            result = load_directory()
            assert result["platforms"]["whatsapp"][0]["name"] == "120363"

    def test_corrupt_alias_file_is_ignored(self, tmp_path):
        cache_file = _write_directory(tmp_path, {
            "whatsapp": [{"id": "120363@g.us", "name": "120363", "type": "group"}]
        })
        bad = tmp_path / "channel_aliases.json"
        bad.write_text("{not json")
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             patch("gateway.channel_directory.CHANNEL_ALIASES_PATH", bad):
            result = load_directory()
            assert result["platforms"]["whatsapp"][0]["name"] == "120363"

    def test_alias_persists_through_rebuild(self, tmp_path, monkeypatch):
        """build_channel_directory must bake aliases into the written file so
        they survive the periodic regeneration, not just live reads."""
        cache_file = tmp_path / "channel_directory.json"
        monkeypatch.setattr("gateway.channel_directory._build_from_sessions",
                            lambda plat: [{"id": "120363@g.us", "name": "120363",
                                           "type": "group", "thread_id": None}]
                            if plat == "whatsapp" else [])
        with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file), \
             self._setup_aliases(tmp_path, {"whatsapp": {"120363@g.us": "general"}}):
            asyncio.run(build_channel_directory({}))
            on_disk = json.loads(cache_file.read_text())
        names = [e["name"] for e in on_disk["platforms"]["whatsapp"]
                 if e["id"] == "120363@g.us"]
        assert names == ["general"]

    def test_apply_aliases_handles_malformed_map(self):
        """Non-dict alias maps and non-string aliases must not raise."""
        platforms = {"whatsapp": [{"id": "1@g.us", "name": "1", "type": "group"}]}
        with patch("gateway.channel_directory._load_channel_aliases",
                   return_value={
                       "whatsapp": "not-a-dict",
                       "telegram": None,
                       "signal": {"+15551234567": 123},
                   }):
            _apply_channel_aliases(platforms)  # should not raise
        assert platforms["whatsapp"][0]["name"] == "1"
