"""
Channel directory -- cached map of reachable channels/contacts per platform.

Built on gateway startup, refreshed periodically (every 5 min), and saved to
~/.hermes/channel_directory.json.  The send_message tool reads this file for
action="list" and for resolving human-friendly channel names to numeric IDs.
"""

import json
import logging
import re
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
_EXPLICIT_TOPIC_TARGET_RE = re.compile(r"^\s*(-?\d+)(?::(\d+))?\s*$")
# User-maintained friendly-name overlay. The directory is fully regenerated
# from live adapters + session data on a timer, so hand-edits to
# channel_directory.json don't survive. Aliases declared here are re-applied
# on every build AND every load, giving durable human-friendly names (and
# letting you pre-name a chat before it has produced any traffic).
# Format: {"<platform>": {"<chat_id>": "<friendly name>", ...}, ...}
CHANNEL_ALIASES_PATH = get_hermes_home() / "channel_aliases.json"


def _load_channel_aliases() -> Dict[str, Dict[str, str]]:
    if not CHANNEL_ALIASES_PATH.exists():
        return {}
    try:
        with open(CHANNEL_ALIASES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_channel_aliases(platforms: Dict[str, Any]) -> None:
    """Overlay friendly names onto directory entries by chat_id.

    Renames matching entries in place; injects a placeholder entry for an
    aliased id that hasn't been discovered yet (so a freshly-created group is
    addressable by name before its first message). Mutates *platforms*.
    """
    aliases = _load_channel_aliases()
    for plat_name, id_map in aliases.items():
        if not isinstance(id_map, dict):
            continue
        entries = platforms.setdefault(plat_name, [])
        if not isinstance(entries, list):
            continue
        for chat_id, friendly in id_map.items():
            if not isinstance(friendly, str) or not friendly.strip():
                continue
            chat_id = str(chat_id)
            friendly = friendly.strip()
            matched = False
            for e in entries:
                if isinstance(e, dict) and e.get("id") == chat_id:
                    e["name"] = friendly
                    matched = True
            if not matched:
                entries.append({
                    "id": chat_id,
                    "name": friendly,
                    "type": "group" if str(chat_id).endswith("@g.us") else "dm",
                    "thread_id": None,
                })


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


def channel_delivery_status(
    platform_name: str,
    chat_id: str,
    *,
    thread_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return cached delivery metadata for a platform target, when known."""
    directory = load_directory()
    channels = directory.get("platforms", {}).get(platform_name, [])
    if not isinstance(channels, list):
        return None

    target_id = _channel_delivery_id(str(chat_id), str(thread_id) if thread_id else None)
    for channel in channels:
        if not isinstance(channel, dict) or str(channel.get("id")) != target_id:
            continue
        return {
            "delivery_status": channel.get("delivery_status"),
            "stale_reason": channel.get("stale_reason"),
            "last_delivery_error": channel.get("last_delivery_error"),
            "last_delivery_failed_at": channel.get("last_delivery_failed_at"),
        }
    return None


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
    if not isinstance(entries, list) or not isinstance(previous_entries, list):
        return entries

    previous_by_id = {
        str(entry.get("id")): entry
        for entry in previous_entries
        if isinstance(entry, dict) and entry.get("id") is not None
    }
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        previous = previous_by_id.get(str(entry.get("id")))
        if not previous:
            continue
        for key in DELIVERY_METADATA_KEYS:
            if key in previous:
                entry[key] = previous[key]
    return entries


def _parse_json_prefix(value: Any) -> Optional[Dict[str, Any]]:
    """Parse the leading JSON object from a tool payload.

    Tool loop warnings can be appended after the JSON result, so a strict
    json.loads() would miss the provider error that matters for stale recovery.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed, _idx = json.JSONDecoder().raw_decode(value.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _resolve_recovery_target(
    platforms: Dict[str, List[Dict[str, Any]]],
    target: Any,
) -> Optional[tuple[str, str, Optional[str]]]:
    if not isinstance(target, str) or ":" not in target:
        return None

    platform_name, target_ref = target.split(":", 1)
    platform_name = platform_name.strip().lower()
    target_ref = target_ref.strip()
    if not platform_name or not target_ref:
        return None

    explicit_match = _EXPLICIT_TOPIC_TARGET_RE.fullmatch(target_ref)
    if explicit_match:
        return platform_name, explicit_match.group(1), explicit_match.group(2)

    query = _normalize_channel_query(target_ref)
    for channel in platforms.get(platform_name, []):
        if not isinstance(channel, dict):
            continue
        entry_id = channel.get("id")
        if entry_id is None:
            continue
        if str(entry_id) == target_ref:
            return platform_name, str(entry_id), None
        if _normalize_channel_query(str(channel.get("name") or "")) == query:
            return platform_name, str(entry_id), channel.get("thread_id")
        if _normalize_channel_query(_channel_target_name(platform_name, channel)) == query:
            return platform_name, str(entry_id), channel.get("thread_id")

    return None


def _recent_session_jsonl_paths(limit: int = 200) -> List[Any]:
    sessions_dir = get_hermes_home() / "sessions"
    if not sessions_dir.exists():
        return []
    try:
        paths = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return []
    return list(reversed(paths[:limit]))


def _recover_session_delivery_metadata(
    platforms: Dict[str, List[Dict[str, Any]]],
) -> Dict[tuple[str, str, Optional[str]], Dict[str, Any]]:
    """Recover stale delivery marks from persisted send_message transcripts."""
    states: Dict[tuple[str, str, Optional[str]], Dict[str, Any]] = {}

    for path in _recent_session_jsonl_paths():
        pending_calls: Dict[str, Dict[str, Any]] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        for line in lines:
            try:
                event = json.loads(line)
            except Exception:
                continue

            if event.get("role") == "assistant":
                for tool_call in event.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    if function.get("name") != "send_message":
                        continue
                    call_id = tool_call.get("id") or tool_call.get("call_id")
                    if not call_id:
                        continue
                    arguments = _parse_json_prefix(function.get("arguments"))
                    if isinstance(arguments, dict):
                        pending_calls[str(call_id)] = arguments
                continue

            if event.get("role") != "tool" or event.get("name") != "send_message":
                continue
            call_id = event.get("tool_call_id")
            arguments = pending_calls.get(str(call_id))
            if not arguments:
                continue
            resolved = _resolve_recovery_target(platforms, arguments.get("target"))
            if not resolved:
                continue

            platform_name, chat_id, thread_id = resolved
            key = (platform_name, chat_id, str(thread_id) if thread_id else None)
            result = _parse_json_prefix(event.get("content")) or {}
            error = result.get("error")
            if error and is_stale_delivery_error(error):
                states[key] = {
                    "delivery_status": "stale",
                    "stale_reason": "delivery_failed",
                    "last_delivery_error": _safe_error_text(error),
                    "last_delivery_failed_at": event.get("timestamp") or datetime.now().isoformat(),
                }
            elif result.get("success"):
                states.pop(key, None)

    return states


def _apply_recovered_delivery_metadata(
    platforms: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    recovered = _recover_session_delivery_metadata(platforms)
    if not recovered:
        return platforms

    for platform_name, channels in platforms.items():
        if not isinstance(channels, list):
            continue
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            entry_id = channel.get("id")
            if entry_id is None:
                continue
            key = (
                platform_name,
                str(entry_id),
                str(channel.get("thread_id")) if channel.get("thread_id") else None,
            )
            metadata = recovered.get(key)
            if metadata:
                channel.update(metadata)
    return platforms


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
    # discovery automatically, but only for platforms connected in THIS gateway
    # process. Historical session origins for disabled/decommissioned platforms
    # must not be resurrected into the active send-target directory (stale
    # targets make send_message route to platforms that can no longer deliver).
    _SKIP_SESSION_DISCOVERY = frozenset({"local", "api_server", "webhook"})
    adapter_platform_names = {getattr(p, "value", str(p)) for p in adapters}
    for plat in Platform:
        plat_name = plat.value
        if plat_name in _SKIP_SESSION_DISCOVERY or plat_name in platforms:
            continue
        # Connected-only rule applies to SESSION discovery only; installer-seeded
        # static targets (STATIC_MERGE_PLATFORMS, e.g. BlueBubbles) must survive
        # even when the platform is not connected in this gateway process.
        session_entries = (
            _build_from_sessions(plat_name)
            if plat_name in adapter_platform_names
            else []
        )
        merged_entries = _merge_static_entries(plat_name, session_entries)
        if merged_entries:
            platforms[plat_name] = merged_entries

    # Include plugin-registered platforms (dynamic enum members aren't in
    # Platform.__members__, so the loop above misses them). Same
    # connected-only rule: don't expose stale session targets for plugins
    # that are not loaded.
    try:
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            if entry.name not in _SKIP_SESSION_DISCOVERY and entry.name not in platforms:
                # Connected-only rule applies to SESSION discovery (don't expose
                # stale session targets for plugins that are not loaded), but
                # installer-seeded static targets must survive regardless — they
                # are the only reachable directory for platforms with no
                # enumeration API (e.g. BlueBubbles seeds).
                session_entries = (
                    _build_from_sessions(entry.name)
                    if entry.name in adapter_platform_names
                    else []
                )
                merged_entries = _merge_static_entries(entry.name, session_entries)
                if merged_entries:
                    platforms[entry.name] = merged_entries
    except Exception:
        pass

    # Overlay user-maintained friendly names before preserving/recovering
    # delivery metadata, so stale status survives alias display-name changes.
    _apply_channel_aliases(platforms)

    for platform_name, entries in list(platforms.items()):
        platforms[platform_name] = _preserve_delivery_metadata(
            entries,
            previous_platforms.get(platform_name, []),
        )
    platforms = _apply_recovered_delivery_metadata(platforms)

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
    """Pull known channels/contacts from gateway session origin data.

    state.db is the primary source (#9006): gateway session rows persist
    origin_json.  Falls back to sessions.json for pre-migration databases.
    """
    entries = _build_from_sessions_db(platform_name)
    if entries:
        return entries
    return _build_from_sessions_json(platform_name)


def _build_from_sessions_db(platform_name: str) -> List[Dict[str, str]]:
    """Pull channels/contacts from state.db gateway session rows."""
    entries: List[Dict[str, str]] = []
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            lister = getattr(db, "list_gateway_sessions", None)
            if not callable(lister):
                return []
            rows = lister(platform=platform_name, active_only=False)
        finally:
            db.close()

        seen_ids = set()
        for row in rows:
            origin: Dict[str, Any] = {}
            if row.get("origin_json"):
                try:
                    parsed = json.loads(row["origin_json"])
                    if isinstance(parsed, dict):
                        origin = parsed
                except (TypeError, ValueError):
                    pass
            if not origin:
                origin = {
                    "chat_id": row.get("chat_id"),
                    "thread_id": row.get("thread_id"),
                    "chat_name": row.get("display_name"),
                }
            entry_id = _session_entry_id(origin)
            if not entry_id or entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entries.append({
                "id": entry_id,
                "name": _session_entry_name(origin),
                "type": row.get("chat_type") or "dm",
                "thread_id": origin.get("thread_id"),
            })
    except Exception as e:
        logger.debug(
            "Channel directory: state.db session read failed for %s: %s",
            platform_name, e,
        )
    return entries


def _build_from_sessions_json(platform_name: str) -> List[Dict[str, str]]:
    """Legacy fallback: pull channels/contacts from sessions.json origin data."""
    sessions_path = get_hermes_home() / "sessions" / "sessions.json"
    if not sessions_path.exists():
        return []

    entries = []
    try:
        with open(sessions_path, encoding="utf-8") as f:
            data = json.load(f)

        seen_ids = set()
        for _key, session in data.items():
            # Skip documentation/metadata sentinels (keys starting with "_",
            # e.g. the gateway's "_README" note) — not session entries.
            if str(_key).startswith("_") or not isinstance(session, dict):
                continue
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
        base = {"updated_at": None, "platforms": {}}
        _apply_channel_aliases(base["platforms"])
        return base
    try:
        with open(DIRECTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # Re-apply aliases on read so friendly names take effect immediately,
        # even between timed rebuilds and for brand-new alias entries.
        _apply_channel_aliases(data.setdefault("platforms", {}))
        return data
    except Exception:
        base = {"updated_at": None, "platforms": {}}
        _apply_channel_aliases(base["platforms"])
        return base


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
    stale_lines: List[str] = []
    active_count = 0

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
                    if _channel_is_stale(ch):
                        stale_lines.append(_format_stale_target_line(plat_name, ch))
                        continue
                    lines.append(f"  discord:{_channel_target_name(plat_name, ch)}")
                    active_count += 1
            if dms:
                active_dms = [ch for ch in dms if not _channel_is_stale(ch)]
                for ch in dms:
                    if _channel_is_stale(ch):
                        stale_lines.append(_format_stale_target_line(plat_name, ch))
                if active_dms:
                    lines.append("Discord (DMs):")
                    for ch in active_dms:
                        lines.append(f"  discord:{_channel_target_name(plat_name, ch)}")
                        active_count += 1
            lines.append("")
        else:
            active_channels = []
            for ch in channels:
                if _channel_is_stale(ch):
                    stale_lines.append(_format_stale_target_line(plat_name, ch))
                else:
                    active_channels.append(ch)
            if active_channels:
                lines.append(f"{plat_name.title()}:")
                for ch in active_channels:
                    label = _channel_target_name(plat_name, ch)
                    lines.append(f"  {plat_name}:{label}")
                    active_count += 1
            lines.append("")

    if active_count == 0:
        lines.append("No currently usable messaging targets are available.")
        lines.append("")

    if stale_lines:
        lines.append("Unavailable stale targets (do not use until refreshed):")
        lines.extend(stale_lines)
        lines.append("")

    lines.append('Use only the available targets above as the "target" parameter when sending.')
    lines.append('Bare platform name (e.g. "telegram") sends to home channel.')

    return "\n".join(lines)


def _format_stale_target_line(platform_name: str, channel: Dict[str, Any]) -> str:
    label = _channel_target_name(platform_name, channel)
    error = channel.get("last_delivery_error") or channel.get("stale_reason") or "delivery failed"
    return f"  {platform_name}:{label} — stale, not currently usable ({error})"
