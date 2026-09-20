"""Reviewed public telephone/source receipts; never a general redaction exception.

Only web_extract's successful, policy-checked results can issue references.
Operator grants live in protected profile config. Receipts live in a protected
profile directory; ordinary model text, citations and provider metadata confer
no authority. Rendering is explicitly session-bound and performs no network I/O.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from hermes_constants import get_hermes_home

REFERENCE_RE = re.compile(r"\[public-contact:([a-p]{64})\]")
UNAVAILABLE = "[Public contact unavailable; verify the source again.]"
_DIRECTORY = "public-contact-receipts"
_PHONE_RE = re.compile(r"\+?[0-9(][0-9 ().-]{5,35}(?: (?:ext\.?|x) [0-9]{1,6})?", re.I)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _source_url(value) -> bool:
    if not isinstance(value, str) or any(c.isspace() or c in "<>`\\" for c in value):
        return False
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme != "https" or "." not in host or url.username or url.password
                or url.query or url.fragment or url.port not in (None, 443)):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            return not host.endswith((".local", ".localhost", ".internal"))
    except ValueError:
        return False


def _valid_phone(phone) -> bool:
    if not isinstance(phone, str):
        return False
    base = re.split(r" (?:ext\.?|x) ", phone, flags=re.I)[0]
    # Digits alone remain ambiguous identifiers. This validates supported
    # published display forms, not number allocation or dialability.
    return bool(
        _PHONE_RE.fullmatch(phone)
        and 7 <= len(re.sub(r"\D", "", base)) <= 15
        and any(c in phone for c in "+().- ")
    )


def _grants():
    """Read current authority each time, including revocations. Fail closed."""
    import yaml
    from hermes_cli.config import load_config_readonly

    try:
        config = load_config_readonly(strict=True)
        policy = config.get("security", {}).get("public_contacts", {})
        grants = policy.get("grants", [])
        sources = policy.get("source_grants", [])
        max_age = float(policy.get("max_age_seconds", 3600))
        if (not isinstance(grants, list) or not isinstance(sources, list)
                or len(grants) + len(sources) > 100 or not 0 < max_age <= 86400):
            return [], 0
        valid = []
        for grant, source_only in [(g, False) for g in grants] + [(g, True) for g in sources]:
            if not isinstance(grant, dict):
                continue
            fields = ("id", "business", "session_id", "task", "label" if source_only else "phone")
            if not all(isinstance(grant.get(k), str) and 0 < len(grant[k]) <= 200
                       and not any(ord(c) < 32 for c in grant[k])
                       for k in fields):
                continue
            if (not _source_url(grant.get("source_url"))
                    or (not source_only and not _valid_phone(grant["phone"]))
                    or (source_only and ("phone" in grant or not grant["label"].strip()))):
                continue
            expires = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                continue
            valid.append((grant, expires.timestamp(), source_only))
        # Duplicate IDs make reviewed identity ambiguous; none may authorize.
        counts = {}
        for grant, _, _ in valid:
            counts[grant["id"]] = counts.get(grant["id"], 0) + 1
        return [(g, expiry, source_only) for g, expiry, source_only in valid
                if counts[g["id"]] == 1], max_age
    except (OSError, ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError):
        return [], 0


def _published_phones(content: str, grant: dict, *, source_only: bool):
    if not source_only:
        span = re.search(r"(?<![\w+])" + re.escape(grant["phone"]) + r"(?!\w)", content)
        return [(grant["phone"], [span.start(), span.end()])] if span else []
    # The reviewed label must occupy its own contact line. Nearby page prose,
    # identifiers and provider metadata cannot classify another number. Support
    # ordinary extracted Markdown (including bold labels and list items).
    label = re.escape(grant["label"])
    prefixes = [label + r"[ \t]*:"]
    for marker in (r"\*\*", "__"):
        prefixes.extend((marker + label + r"[ \t]*:" + marker,
                         marker + label + marker + r"[ \t]*:"))
    pattern = (r"(?mi)^[ \t]*(?:[-*][ \t]+)?(?:" + "|".join(prefixes)
               + r")[ \t]*(?P<phone>[^\r\n]+?)[ \t]*$")
    found = []
    seen = set()
    for match in re.finditer(pattern, content):
        phone, start, end = match["phone"], *match.span("phone")
        # Preserve exact source offsets through simple Markdown presentation.
        # A tel link is considered only within the reviewed contact label;
        # the URI itself never establishes business ownership or authority.
        if phone.startswith(("**", "__")) and phone.endswith(phone[:2]):
            phone, start, end = phone[2:-2], start + 2, end - 2
        link = re.fullmatch(r"\[([^\[\]\r\n]+)\]\(tel:(\+[0-9().-]+)\)", phone, re.I)
        if link:
            visible, target = link.groups()
            # Global telephone identifiers may contain visual separators
            # (RFC 3966). Do not infer a country code for a conflicting label.
            if re.search(r"[0-9]", visible) and re.sub(r"\D", "", visible) != re.sub(r"\D", "", target):
                continue
            phone = target
            start, end = start + link.start(2), start + link.end(2)
        if phone not in seen and _valid_phone(phone):
            found.append((phone, [start, end]))
            seen.add(phone)
        if len(found) == 8:
            break
    return found


def _receipt_directory(*, create=False):
    home = get_hermes_home().resolve()
    directory = home / _DIRECTORY
    if directory.is_symlink() or directory.resolve().parent != home:
        raise ValueError("Invalid public-contact receipt directory")
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def issue_contact_references(result: dict) -> list[dict]:
    """Called only after extraction association, URL safety and source policy.

    Exact reviewed spans must occur in the retrieved body. Cache age is retained,
    not relabeled as a new fetch. Nothing is issued outside a correlated tool call.
    """
    from gateway.session_context import get_session_env, session_context_missing
    from tools.approval import get_current_tool_context

    context = get_current_tool_context()
    if (result.get("error") or result.get("blocked_by_policy") or session_context_missing()
            or not all(context.values())
            or context["session_id"] != get_session_env("HERMES_SESSION_ID")):
        return []
    content = result.get("raw_content") or result.get("content")
    if not isinstance(content, str) or not content:
        return []
    now = time.time()
    try:
        fetched_at = float(result["cache_stored_at"]) if result.get("cached") else now
    except (KeyError, ValueError, TypeError):
        return []
    grants, max_age = _grants()
    if not math.isfinite(fetched_at) or not 0 <= now - fetched_at <= max_age:
        return []
    references = []
    for grant, expiry, source_only in grants:
        if (expiry <= now or grant["session_id"] != context["session_id"]
                or result.get("requested_url") != grant["source_url"]
                or result.get("url") != grant["source_url"]):
            continue
        for phone, span in _published_phones(content, grant, source_only=source_only):
            receipt = {
                "version": 2 if source_only else 1, "profile": str(get_hermes_home().resolve()),
                "grant_digest": _digest(grant), "grant_id": grant["id"],
                "session_id": context["session_id"], "task": grant["task"],
                "turn_id": context["turn_id"], "tool_call_id": context["tool_call_id"],
                "tool": "web_extract", "requested_url": result["requested_url"],
                "source_url": result["url"], "fetched_at": fetched_at,
                "issued_at": now, "expires_at": min(expiry, fetched_at + max_age),
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "span": span, "phone_sha256": _digest(phone),
            }
            if source_only:
                # Protected receipt stores the newly discovered value; it is
                # never copied to model output or ordinary logs. Current source
                # authorization is still required for every new display.
                receipt["phone"] = phone
                receipt["publication_label"] = grant["label"]
            # Letters only: general phone/token redaction must preserve the reference.
            token = secrets.token_hex(32).translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
            try:
                path = _receipt_directory(create=True) / (token + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(receipt, file, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                references.append({
                    "reference": f"[public-contact:{token}]", "business": grant["business"],
                    "source_url": grant["source_url"],
                    "usage": "Copy this reference verbatim to show the verified public telephone in the final reply.",
                })
            except (OSError, ValueError):
                # Ordinary extraction remains usable when optional receipt storage fails.
                continue
    return references


def render_contact_references(text: str, *, session_id: str) -> str:
    """Resolve references after the surface's existing egress filtering.

    Never call this for logs or provider replay. Explicit session identity is
    mandatory; no process environment fallback is used at the delivery boundary.
    Programmatic APIs retain their existing raw-text policy for ordinary prose.
    """
    if not REFERENCE_RE.search(text):
        return text
    grants, max_age = _grants()
    now = time.time()

    def resolve(match):
        if not session_id:
            return UNAVAILABLE
        try:
            path = _receipt_directory() / (match[1] + ".json")
            if path.is_symlink() or path.stat().st_size > 16384:
                return UNAVAILABLE
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if (receipt["version"] not in (1, 2) or receipt["session_id"] != session_id
                    or receipt["profile"] != str(get_hermes_home().resolve())
                    or not receipt["fetched_at"] <= receipt["issued_at"] <= now
                    or not 0 <= now - receipt["fetched_at"] <= max_age
                    or now >= receipt["expires_at"]):
                return UNAVAILABLE
            for grant, expiry, source_only in grants:
                phone = receipt.get("phone") if source_only else grant["phone"]
                if (receipt["version"] != (2 if source_only else 1)
                        or not _valid_phone(phone)
                        or (source_only and receipt.get("publication_label") != grant["label"])
                        or expiry <= now or grant["session_id"] != session_id
                        or receipt["grant_digest"] != _digest(grant)
                        or receipt["phone_sha256"] != _digest(phone)
                        or receipt["source_url"] != grant["source_url"]
                        or receipt["requested_url"] != grant["source_url"]):
                    continue
                from agent.redact import redact_sensitive_text
                # Only the reviewed telephone span is exempt. All labels and
                # links still pass the ordinary credential/contact filter.
                business = redact_sensitive_text(grant["business"], force=True)
                business = re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", business)
                source = redact_sensitive_text(grant["source_url"], force=True, redact_url_credentials=True)
                if source != grant["source_url"]:
                    return UNAVAILABLE
                date = datetime.fromtimestamp(receipt["fetched_at"], timezone.utc).isoformat()
                return f"{business} — {phone}\nSource: <{source}>\nVerified: {date}"
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            pass
        return UNAVAILABLE

    return REFERENCE_RE.sub(resolve, text)


class ContactReferenceStream:
    """Resolve references split across API deltas without buffering prose.

    This is presentation only, not a streaming secret filter. Callers keep raw
    deltas separately for provider replay and apply their existing egress policy.
    Only a possible reference suffix is held (at most 80 characters). Authority
    is read when the complete reference is emitted, never cached at stream start.
    A reference assembled across different profile/session scopes is denied.
    """

    _prefix = "[public-contact:"

    def __init__(self):
        self._pending = ""
        self._pending_scope = None

    def feed(self, text: str, *, session_id: str) -> str:
        scope = (str(get_hermes_home().resolve()), session_id)
        combined_scope = scope
        if self._pending and self._pending_scope != scope:
            combined_scope = None
        remaining = self._pending + text
        out = []
        self._pending = ""
        while remaining:
            start = remaining.find(self._prefix)
            if start < 0:
                # Retain a split prefix, but send unrelated text immediately.
                hold = next((n for n in range(min(len(remaining), len(self._prefix) - 1), 0, -1)
                             if remaining.endswith(self._prefix[:n])), 0)
                out.append(remaining[:-hold] if hold else remaining)
                self._pending = remaining[-hold:] if hold else ""
                break
            out.append(remaining[:start])
            remaining = remaining[start:]
            match = REFERENCE_RE.match(remaining)
            if match:
                out.append(render_contact_references(
                    match[0], session_id=session_id if combined_scope == scope else "",
                ))
                remaining = remaining[match.end():]
            elif re.fullmatch(r"\[public-contact:[a-p]{0,64}", remaining):
                self._pending = remaining
                break
            else:
                # Malformed references are ordinary text; do not swallow it.
                out.append(remaining[0])
                remaining = remaining[1:]
        self._pending_scope = combined_scope if self._pending else None
        return "".join(out)

    def finish(self) -> str:
        pending, self._pending = self._pending, ""
        self._pending_scope = None
        return UNAVAILABLE if pending.startswith(self._prefix) else pending


DISPLAY_METADATA_KEY = "public_contact_display"


def project_contact_reply(message: dict, *, session_id: str) -> dict:
    """Project a current assistant turn without treating it as a saved DB row.

    This uses current authority, not model-supplied row IDs or display metadata.
    Keep the existing contact-bearing transcript redaction before insertion.
    """
    projected = message.copy()
    content = message.get("content")
    if message.get("role") == "assistant" and isinstance(content, str) and REFERENCE_RE.search(content):
        from agent.redact import redact_sensitive_text
        projected["content"] = render_contact_references(
            redact_sensitive_text(content, force=True), session_id=session_id,
        )
    return projected


def _display_identity(content: str) -> str:
    # Match SessionDB's presentation normalization, without touching replay.
    from agent.memory_manager import sanitize_context
    return _digest(sanitize_context(content).strip())


def capture_contact_display(
    content: str, *, session_id: str, row_id: int, metadata=None,
) -> dict:
    """Capture currently authorized replacements for one newly inserted row.

    Callers cannot supply display authority via metadata. The protected record
    stores only deterministic contact blocks, not unrelated message text.
    """
    clean = dict(metadata) if isinstance(metadata, dict) else {}
    clean.pop(DISPLAY_METADATA_KEY, None)
    if not isinstance(content, str) or not session_id or type(row_id) is not int:
        return clean
    references = list(dict.fromkeys(match[0] for match in REFERENCE_RE.finditer(content)))
    if not references or len(references) > 100:
        return clean
    replacements = {}
    for reference in references:
        rendered = render_contact_references(reference, session_id=session_id)
        if rendered != UNAVAILABLE:
            replacements[reference] = rendered
    if not replacements:
        return clean
    record = {
        "version": 1, "kind": "historical-contact-display",
        "profile": str(get_hermes_home().resolve()),
        "session_id": session_id, "row_id": row_id,
        "content_digest": _display_identity(content),
        "recorded_at": time.time(), "replacements": replacements,
    }
    token = secrets.token_hex(32).translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    try:
        directory = _receipt_directory(create=True) / "display"
        if directory.is_symlink():
            return clean
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (token + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(record, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        clean[DISPLAY_METADATA_KEY] = token
    except (OSError, ValueError):
        pass
    return clean


def project_contact_history(message: dict) -> dict:
    """Display a stored fact without authorizing its reuse in a new reply.

    The caller supplies a durable DB row, not model-provided message identity.
    No fresh grant lookup occurs: this is the authorization captured at insert.
    Content, API sidecars and structured replay in the input remain untouched.
    """
    projected = message.copy()
    content = message.get("content")
    if message.get("role") != "assistant" or not isinstance(content, str) or not REFERENCE_RE.search(content):
        return projected
    from agent.redact import redact_sensitive_text
    safe = redact_sensitive_text(content, force=True)
    replacements = {}
    metadata = message.get("display_metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = None
    token = metadata.get(DISPLAY_METADATA_KEY) if isinstance(metadata, dict) else None
    session_id = message.get("_session_id") or message.get("session_id")
    row_id = message.get("_row_id", message.get("id"))
    if isinstance(token, str) and re.fullmatch(r"[a-p]{64}", token) and session_id and type(row_id) is int:
        try:
            directory = _receipt_directory() / "display"
            path = directory / (token + ".json")
            if directory.is_symlink() or path.is_symlink() or path.stat().st_size > 131072:
                raise ValueError("Invalid display record")
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record["version"] == 1 and record["kind"] == "historical-contact-display"
                    and record["profile"] == str(get_hermes_home().resolve())
                    and record["session_id"] == session_id and record["row_id"] == row_id
                    and record["content_digest"] == _display_identity(content)
                    and isinstance(record["replacements"], dict)):
                replacements = record["replacements"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def replace(match):
        value = replacements.get(match[0])
        return value if isinstance(value, str) else UNAVAILABLE
    projected["content"] = REFERENCE_RE.sub(replace, safe)
    return projected
