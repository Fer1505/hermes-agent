"""
Channel directory -- cached map of reachable channels/contacts per platform.

Built on gateway startup, refreshed periodically (every 5 min), and saved to
~/.hermes/channel_directory.json.  The send_message tool reads this file for
action="list" and for resolving human-friendly channel names to numeric IDs.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from hermes_cli.config import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

DIRECTORY_PATH = get_hermes_home() / "channel_directory.json"
STATIC_MERGE_PLATFORMS = frozenset({"bluebubbles"})
DELIVERY_METADATA_KEYS = frozenset({
    "delivery_status",
    "stale_reason",
    "last_delivery_error",
    "last_delivery_failed_at",
})
STALE_DELIVERY_ERROR_MARKERS = (
    "chat not found",
    "channel not found",
    "room not found",
    "thread not found",
    "forbidden",
    "bot was blocked",
    "bot blocked",
    "not enough rights",
    "not a member",
    "have no access",
    "missing access",
    "missing permissions",
)
TRANSIENT_DELIVERY_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "try again",
    "too many requests",
    "rate limit",
    "flood",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "connection reset",
    "network",
)


def _normalize_channel_query(value: str) -> str:
    return value.lstrip("#").strip().lower()


def _channel_target_name(platform_name: str, channel: Dict[str, Any]) -> str:
    """Return the human-facing target label shown to users for a channel entry."""
    name = channel["name"]
    if platform_name == "discord" and channel.get("guild"):
        return f"#{name}"
    if platform_name != "discord" and channel.get("type"):
        return f"{name} ({channel['type']})"
    return name


def _session_entry_id(origin: Dict[str, Any]) -> Optional[str]:
    chat_id = origin.get("chat_id")
    if not chat_id:
        return None
    thread_id = origin.get("thread_id")
    if thread_id:
        return f"{chat_id}:{thread_id}"
    return str(chat_id)


def _session_entry_name(origin: Dict[str, Any]) -> str:
    base_name = origin.get("chat_name") or origin.get("user_name") or str(origin.get("chat_id"))
    thread_id = origin.get("thread_id")
    if not thread_id:
        return base_name

    topic_label = origin.get("chat_topic") or f"topic {thread_id}"
    return f"{base_name} / {topic_label}"


def _channel_is_stale(channel: Dict[str, Any]) -> bool:
    return channel.get("delivery_status") == "stale"


def _channel_delivery_id(chat_id: str, thread_id: Optional[str] = None) -> str:
    if thread_id:
        return f"{chat_id}:{thread_id}"
    return str(chat_id)


def _safe_error_text(error: Any) -> str:
    text = str(error or "unknown delivery failure")
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text)
    except Exception:
        pass
    return text[:500]


def is_stale_delivery_error(error: Any) -> bool:
    """Return True when a delivery error indicates a stale target, not a transient outage."""
    text = str(error or "").lower()
    if not text:
        return False
    if any(marker in text for marker in TRANSIENT_DELIVERY_ERROR_MARKERS):
        return False
    return any(marker in text for marker in STALE_DELIVERY_ERROR_MARKERS)


def _preserve_delivery_metadata(
    entries: List[Dict[str, Any]],
    previous_entries: Any,
) -> List[Dict[str, Any]]:
    """Carry stale delivery proof across session-derived directory rebuilds."""
    if not isinstance(previous_entries, list):
        return entries

    previous_by_id = {
        str(entry.get("id")): entry
        for entry in previous_entries
        if isinstance(entry, dict) and entry.get("id") is not None
    }
    for entry in entries:
        previous = previous_by_id.get(str(entry.get("id")))
        if not previous:
            continue
        for key in DELIVERY_METADATA_KEYS:
            if key in previous:
                entry[key] = previous[key]
    return entries


def _merge_static_entries(
    platform_name: str,
    session_entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Preserve installer-seeded targets for platforms with no enumeration API."""
    if platform_name not in STATIC_MERGE_PLATFORMS:
        return session_entries

    existing = load_directory().get("platforms", {}).get(platform_name, [])
    merged: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in [*(existing if isinstance(existing, list) else []), *session_entries]:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id or entry_id in seen_ids:
            continue
        merged.append(entry)
        seen_ids.add(entry_id)
    return merged


# ---------------------------------------------------------------------------
# Build / refresh
# ---------------------------------------------------------------------------

async def build_channel_directory(adapters: Dict[Any, Any]) -> Dict[str, Any]:
    """
    Build a channel directory from connected platform adapters and session data.

    Returns the directory dict and writes it to DIRECTORY_PATH.
    """
    from gateway.config import Platform

    previous_platforms = load_directory().get("platforms", {})
    platforms: Dict[str, List[Dict[str, Any]]] = {}

    for platform, adapter in adapters.items():
        try:
            if platform == Platform.DISCORD:
                platforms["discord"] = _build_discord(adapter)
            elif platform == Platform.SLACK:
                platforms["slack"] = await _build_slack(adapter)
        except Exception as e:
            logger.warning("Channel directory: failed to build %s: %s", platform.value, e)

    # Platforms that don't support direct channel enumeration get session-based
    # discovery automatically.  Skip infrastructure entries that aren't messaging
    # platforms — everything else falls through to _build_from_sessions().
    _SKIP_SESSION_DISCOVERY = frozenset({"local", "api_server", "webhook"})
    for plat in Platform:
        plat_name = plat.value
        if plat_name in _SKIP_SESSION_DISCOVERY or plat_name in platforms:
            continue
        platforms[plat_name] = _merge_static_entries(
            plat_name,
            _build_from_sessions(plat_name),
        )

    # Include plugin-registered platforms (dynamic enum members aren't in
    # Platform.__members__, so the loop above misses them).
    try:
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            if entry.name not in _SKIP_SESSION_DISCOVERY and entry.name not in platforms:
                platforms[entry.name] = _merge_static_entries(
                    entry.name,
                    _build_from_sessions(entry.name),
                )
    except Exception:
        pass

    for platform_name, entries in list(platforms.items()):
        platforms[platform_name] = _preserve_delivery_metadata(
            entries,
            previous_platforms.get(platform_name, []),
        )

    directory = {
        "updated_at": datetime.now().isoformat(),
        "platforms": platforms,
    }

    try:
        atomic_json_write(DIRECTORY_PATH, directory)
    except Exception as e:
        logger.warning("Channel directory: failed to write: %s", e)

    return directory


def mark_channel_delivery_failed(
    platform_name: str,
    chat_id: str,
    error: Any,
    *,
    thread_id: Optional[str] = None,
) -> bool:
    """Mark a cached directory entry stale after a permanent delivery failure."""
    if not is_stale_delivery_error(error):
        return False

    directory = load_directory()
    platforms = directory.get("platforms", {})
    channels = platforms.get(platform_name, [])
    if not isinstance(channels, list):
        return False

    target_id = _channel_delivery_id(str(chat_id), str(thread_id) if thread_id else None)
    changed = False
    for channel in channels:
        if not isinstance(channel, dict) or str(channel.get("id")) != target_id:
            continue
        channel["delivery_status"] = "stale"
        channel["stale_reason"] = "delivery_failed"
        channel["last_delivery_error"] = _safe_error_text(error)
        channel["last_delivery_failed_at"] = datetime.now().isoformat()
        changed = True

    if not changed:
        return False

    try:
        atomic_json_write(DIRECTORY_PATH, directory)
    except Exception as exc:
        logger.warning("Channel directory: failed to mark %s:%s stale: %s", platform_name, target_id, exc)
        return False
    return True


def mark_channel_delivery_success(
    platform_name: str,
    chat_id: str,
    *,
    thread_id: Optional[str] = None,
) -> bool:
    """Clear stale delivery metadata after a later successful send."""
    directory = load_directory()
    platforms = directory.get("platforms", {})
    channels = platforms.get(platform_name, [])
    if not isinstance(channels, list):
        return False

    target_id = _channel_delivery_id(str(chat_id), str(thread_id) if thread_id else None)
    changed = False
    for channel in channels:
        if not isinstance(channel, dict) or str(channel.get("id")) != target_id:
            continue
        for key in DELIVERY_METADATA_KEYS:
            if key in channel:
                channel.pop(key, None)
                changed = True

    if not changed:
        return False

    try:
        atomic_json_write(DIRECTORY_PATH, directory)
    except Exception as exc:
        logger.warning("Channel directory: failed to clear stale mark for %s:%s: %s", platform_name, target_id, exc)
        return False
    return True


def _build_discord(adapter) -> List[Dict[str, str]]:
    """Enumerate all text channels and forum channels the Discord bot can see."""
    channels = []
    client = getattr(adapter, "_client", None)
    if not client:
        return channels

    try:
        import discord as _discord  # noqa: F401 — SDK presence check
    except ImportError:
        return channels

    for guild in client.guilds:
        for ch in guild.text_channels:
            channels.append({
                "id": str(ch.id),
                "name": ch.name,
                "guild": guild.name,
                "type": "channel",
            })
        # Forum channels (type 15) — creating a message auto-spawns a thread post.
        forums = getattr(guild, "forum_channels", None) or []
        for ch in forums:
            channels.append({
                "id": str(ch.id),
                "name": ch.name,
                "guild": guild.name,
                "type": "forum",
            })
        # Also include DM-capable users we've interacted with is not
        # feasible via guild enumeration; those come from sessions.

    # Merge any DMs from session history
    channels.extend(_build_from_sessions("discord"))
    return channels


async def _build_slack(adapter) -> List[Dict[str, Any]]:
    """List Slack channels the bot has joined across all workspaces.

    Uses ``users.conversations`` against each workspace's web client. Pulls
    public + private channels the bot is a member of, then merges in DMs
    discovered from session history (IMs aren't useful to enumerate
    proactively).
    """
    team_clients = getattr(adapter, "_team_clients", None) or {}
    if not team_clients:
        return _build_from_sessions("slack")

    channels: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for team_id, client in team_clients.items():
        try:
            cursor: Optional[str] = None
            for _page in range(20):  # safety cap on pagination
                response = await client.users_conversations(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
                if not response.get("ok"):
                    logger.warning(
                        "Channel directory: users.conversations not ok for team %s: %s",
                        team_id,
                        response.get("error", "unknown"),
                    )
                    break
                for ch in response.get("channels", []):
                    cid = ch.get("id")
                    name = ch.get("name")
                    if not cid or not name or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    channels.append({
                        "id": cid,
                        "name": name,
                        "type": "private" if ch.get("is_private") else "channel",
                    })
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            logger.warning(
                "Channel directory: failed to list Slack channels for team %s: %s",
                team_id, e,
            )
            continue

    # Merge in DM/group entries discovered from session history.
    for entry in _build_from_sessions("slack"):
        if entry.get("id") not in seen_ids:
            channels.append(entry)
            seen_ids.add(entry.get("id"))

    return channels


def _build_from_sessions(platform_name: str) -> List[Dict[str, str]]:
    """Pull known channels/contacts from sessions.json origin data."""
    sessions_path = get_hermes_home() / "sessions" / "sessions.json"
    if not sessions_path.exists():
        return []

    entries = []
    try:
        with open(sessions_path, encoding="utf-8") as f:
            data = json.load(f)

        seen_ids = set()
        for _key, session in data.items():
            origin = session.get("origin") or {}
            if origin.get("platform") != platform_name:
                continue
            entry_id = _session_entry_id(origin)
            if not entry_id or entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entries.append({
                "id": entry_id,
                "name": _session_entry_name(origin),
                "type": session.get("chat_type", "dm"),
                "thread_id": origin.get("thread_id"),
            })
    except Exception as e:
        logger.debug("Channel directory: failed to read sessions for %s: %s", platform_name, e)

    return entries


# ---------------------------------------------------------------------------
# Read / resolve
# ---------------------------------------------------------------------------

def load_directory() -> Dict[str, Any]:
    """Load the cached channel directory from disk."""
    if not DIRECTORY_PATH.exists():
        return {"updated_at": None, "platforms": {}}
    try:
        with open(DIRECTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated_at": None, "platforms": {}}


def lookup_channel_type(platform_name: str, chat_id: str) -> Optional[str]:
    """Return the channel ``type`` string (e.g. ``"channel"``, ``"forum"``) for *chat_id*, or *None* if unknown."""
    directory = load_directory()
    for ch in directory.get("platforms", {}).get(platform_name, []):
        if ch.get("id") == chat_id:
            return ch.get("type")
    return None


def resolve_channel_name(platform_name: str, name: str) -> Optional[str]:
    """
    Resolve a human-friendly channel name to a numeric ID.

    Matching strategy (case-insensitive, first match wins):
    - Discord: "bot-home", "#bot-home", "GuildName/bot-home"
    - Telegram: display name or group name
    - Slack: "engineering", "#engineering"
    """
    directory = load_directory()
    channels = directory.get("platforms", {}).get(platform_name, [])
    if not channels:
        return None

    # 0. Exact ID match — case-sensitive, no normalization. Lets callers pass
    # raw platform IDs (e.g. Slack "C0B0QV5434G") even when the format guard
    # in _parse_target_ref hasn't recognized them as explicit.
    raw = name.strip()
    for ch in channels:
        if ch.get("id") == raw:
            return ch["id"]

    query = _normalize_channel_query(name)

    # 1. Exact name match, including the display labels shown by send_message(action="list")
    for ch in channels:
        if _channel_is_stale(ch):
            continue
        if _normalize_channel_query(ch["name"]) == query:
            return ch["id"]
        if _normalize_channel_query(_channel_target_name(platform_name, ch)) == query:
            return ch["id"]

    # 2. Guild-qualified match for Discord ("GuildName/channel")
    if "/" in query:
        guild_part, ch_part = query.rsplit("/", 1)
        for ch in channels:
            if _channel_is_stale(ch):
                continue
            guild = ch.get("guild", "").strip().lower()
            if guild == guild_part and _normalize_channel_query(ch["name"]) == ch_part:
                return ch["id"]

    # 3. Partial prefix match (only if unambiguous)
    matches = [
        ch for ch in channels
        if not _channel_is_stale(ch) and _normalize_channel_query(ch["name"]).startswith(query)
    ]
    if len(matches) == 1:
        return matches[0]["id"]

    return None


def format_directory_for_display() -> str:
    """Format the channel directory as a human-readable list for the model."""
    directory = load_directory()
    platforms = directory.get("platforms", {})

    if not any(platforms.values()):
        return "No messaging platforms connected or no channels discovered yet."

    lines = ["Available messaging targets:\n"]

    for plat_name, channels in sorted(platforms.items()):
        if not channels:
            continue

        # Group Discord channels by guild
        if plat_name == "discord":
            guilds: Dict[str, List] = {}
            dms: List = []
            for ch in channels:
                guild = ch.get("guild")
                if guild:
                    guilds.setdefault(guild, []).append(ch)
                else:
                    dms.append(ch)

            for guild_name, guild_channels in sorted(guilds.items()):
                lines.append(f"Discord ({guild_name}):")
                for ch in sorted(guild_channels, key=lambda c: c["name"]):
                    lines.append(f"  discord:{_channel_target_name(plat_name, ch)}")
            if dms:
                lines.append("Discord (DMs):")
                for ch in dms:
                    lines.append(f"  discord:{_channel_target_name(plat_name, ch)}")
            lines.append("")
        else:
            lines.append(f"{plat_name.title()}:")
            for ch in channels:
                label = _channel_target_name(plat_name, ch)
                if _channel_is_stale(ch):
                    label = f"{label} [stale: delivery failed]"
                lines.append(f"  {plat_name}:{label}")
            lines.append("")

    lines.append('Use these as the "target" parameter when sending.')
    lines.append('Bare platform name (e.g. "telegram") sends to home channel.')

    return "\n".join(lines)
