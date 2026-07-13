#!/usr/bin/env python3
"""Audit gateway session indexes for compression-chain drift.

The gateway keeps a lightweight ``sessions/sessions.json`` map from a lane key
to the active SQLite session id. Context compression ends the old session and
continues in a child session. If the index points at the ended parent, the lane
can reload stale context and appear to "compress without resolving".

This script is intentionally read-only. It reports:

* live session-index entries pointing at a compression parent instead of the tip
* non-seed entries pointing at missing SQLite rows
* active lanes close to their configured compression threshold
* active lanes with deep compression ancestry
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_MINIMUM_CONTEXT_LENGTH = 64_000
_CODEX_OAUTH_CONTEXT_FALLBACK = {
    "gpt-5.6-sol": 372_000,
    "gpt-5.1-codex-max": 272_000,
    "gpt-5.1-codex-mini": 272_000,
    "gpt-5.3-codex-spark": 128_000,
    "gpt-5.3-codex": 272_000,
    "gpt-5.2-codex": 272_000,
    "gpt-5.4-mini": 272_000,
    "gpt-5.5": 272_000,
    "gpt-5.4": 272_000,
    "gpt-5.2": 272_000,
    "gpt-5": 272_000,
}
_DEFAULT_CONTEXT_LENGTHS = {
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4.7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4.6": 1_000_000,
    "claude-sonnet-4.6": 1_000_000,
    "claude": 200_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.4-nano": 400_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.3-codex-spark": 128_000,
    "gpt-5.1-chat": 128_000,
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4": 128_000,
    "gemini": 1_048_576,
    "gemma-4": 256_000,
    "gemma4": 256_000,
    "gemma-4-31b": 256_000,
    "gemma-3": 131_072,
    "gemma": 8_192,
}


@dataclass
class AuditIssue:
    severity: str
    kind: str
    session_key: str
    session_id: str
    detail: str
    tip_session_id: str | None = None
    profile: str | None = None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        # Tiny fallback for environments that run the audit outside the venv.
        data: dict[str, Any] = {"model": {}, "compression": {}}
        active_section: str | None = None
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip() or ":" not in line:
                continue
            indent = len(line) - len(line.lstrip(" "))
            stripped = line.strip()
            if stripped.startswith("-"):
                active_section = None
                continue
            key, value = line.strip().split(":", 1)
            value = value.strip()
            if indent == 0:
                active_section = key if key in {"model", "compression"} and not value else None
                if active_section and not isinstance(data.get(active_section), dict):
                    data[active_section] = {}
                continue
            if indent == 2 and active_section in {"model", "compression"} and value:
                section = data.setdefault(active_section, {})
                if isinstance(section, dict):
                    section[key] = value.strip("'\"")
        return data


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _static_context_length(model_name: str, provider: str) -> int | None:
    model_l = model_name.lower()
    provider_l = provider.lower()
    candidates = (
        _CODEX_OAUTH_CONTEXT_FALLBACK
        if provider_l in {"openai-codex", "codex"}
        else _DEFAULT_CONTEXT_LENGTHS
    )
    for key, context_length in sorted(candidates.items(), key=lambda item: len(item[0]), reverse=True):
        if key in model_l:
            return context_length
    return None


def _compression_threshold_tokens(profile_dir: Path) -> int | None:
    cfg = _load_yaml_mapping(profile_dir / "config.yaml")
    comp_cfg = cfg.get("compression") if isinstance(cfg.get("compression"), dict) else {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}

    threshold_pct = _coerce_float(comp_cfg.get("threshold"), 0.5)
    configured_context = _coerce_int(model_cfg.get("context_length"))
    provider = str(model_cfg.get("provider") or "")

    context_length = configured_context
    model_name = str(model_cfg.get("default") or model_cfg.get("model") or "")
    if model_name:
        try:
            from agent.model_metadata import MINIMUM_CONTEXT_LENGTH, get_model_context_length

            context_length = get_model_context_length(
                model_name,
                base_url=str(model_cfg.get("base_url") or ""),
                api_key="",
                config_context_length=configured_context,
                provider=provider,
            )
            return max(int(context_length * threshold_pct), MINIMUM_CONTEXT_LENGTH)
        except Exception:
            context_length = configured_context or _static_context_length(model_name, provider)

    if context_length:
        return max(int(context_length * threshold_pct), _MINIMUM_CONTEXT_LENGTH)
    return None


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, parent_session_id, started_at, ended_at, end_reason, message_count "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()


def compression_tip(conn: sqlite3.Connection, session_id: str) -> str:
    current = session_id
    seen = {current}
    for _ in range(100):
        row = conn.execute(
            """
            SELECT child.id
            FROM sessions child
            JOIN sessions parent ON parent.id = child.parent_session_id
            WHERE child.parent_session_id = ?
              AND parent.end_reason = 'compression'
              AND child.started_at >= parent.ended_at
            ORDER BY child.started_at DESC, child.id DESC
            LIMIT 1
            """,
            (current,),
        ).fetchone()
        if row is None:
            return current
        next_id = str(row["id"])
        if next_id in seen:
            return current
        seen.add(next_id)
        current = next_id
    return current


def compression_ancestor_depth(conn: sqlite3.Connection, session_id: str) -> int:
    current = session_id
    depth = 0
    seen = {current}
    for _ in range(100):
        row = _session_row(conn, current)
        if row is None or not row["parent_session_id"]:
            return depth
        parent_id = str(row["parent_session_id"])
        if parent_id in seen:
            return depth
        parent = _session_row(conn, parent_id)
        if (
            parent is not None
            and parent["end_reason"] == "compression"
            and row["started_at"] is not None
            and parent["ended_at"] is not None
            and row["started_at"] >= parent["ended_at"]
        ):
            depth += 1
        seen.add(parent_id)
        current = parent_id
    return depth


def audit_profile(
    profile_dir: Path,
    *,
    warn_token_ratio: float = 0.80,
    warn_chain_depth: int = 4,
    ignore_seed_missing: bool = True,
) -> dict[str, Any]:
    profile = profile_dir.name
    db_path = profile_dir / "state.db"
    index_path = profile_dir / "sessions" / "sessions.json"
    issues: list[AuditIssue] = []

    result: dict[str, Any] = {
        "profile": profile,
        "db_path": str(db_path),
        "sessions_index": str(index_path),
        "checked_entries": 0,
        "threshold_tokens": _compression_threshold_tokens(profile_dir),
        "issues": [],
    }
    if not db_path.exists() or not index_path.exists():
        result["missing"] = True
        return result

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(
            AuditIssue(
                severity="critical",
                kind="unreadable_session_index",
                session_key="",
                session_id="",
                detail=str(exc),
                profile=profile,
            )
        )
        result["issues"] = [asdict(issue) for issue in issues]
        return result

    threshold_tokens = result["threshold_tokens"]
    conn = _connect(db_path)
    try:
        for session_key, entry in index.items():
            if not isinstance(entry, dict):
                continue
            session_id = str(entry.get("session_id") or "")
            if not session_id:
                continue
            result["checked_entries"] += 1
            row = _session_row(conn, session_id)
            if row is None:
                if not (ignore_seed_missing and session_id.startswith("seed_")):
                    issues.append(
                        AuditIssue(
                            severity="critical",
                            kind="missing_session_row",
                            session_key=session_key,
                            session_id=session_id,
                            detail="sessions.json points at a session id absent from state.db",
                            profile=profile,
                        )
                    )
                continue

            tip_id = compression_tip(conn, session_id)
            if tip_id != session_id or row["end_reason"] == "compression":
                issues.append(
                    AuditIssue(
                        severity="critical",
                        kind="stale_compression_index",
                        session_key=session_key,
                        session_id=session_id,
                        tip_session_id=tip_id,
                        detail="session index should point at the live compression tip",
                        profile=profile,
                    )
                )

            last_prompt_tokens = _coerce_int(entry.get("last_prompt_tokens")) or 0
            if threshold_tokens and last_prompt_tokens >= int(threshold_tokens * warn_token_ratio):
                issues.append(
                    AuditIssue(
                        severity="warning",
                        kind="active_lane_near_compression_threshold",
                        session_key=session_key,
                        session_id=session_id,
                        detail=(
                            f"last_prompt_tokens={last_prompt_tokens} is >= "
                            f"{warn_token_ratio:.0%} of threshold={threshold_tokens}"
                        ),
                        profile=profile,
                    )
                )

            depth = compression_ancestor_depth(conn, session_id)
            if depth >= warn_chain_depth:
                issues.append(
                    AuditIssue(
                        severity="warning",
                        kind="deep_active_compression_chain",
                        session_key=session_key,
                        session_id=session_id,
                        detail=f"active lane has {depth} compression ancestor(s)",
                        profile=profile,
                    )
                )
    finally:
        conn.close()

    result["issues"] = [asdict(issue) for issue in issues]
    return result


def _iter_profiles(root: Path, selected: list[str] | None) -> list[Path]:
    if selected:
        return [root / name for name in selected]
    return sorted(path for path in root.iterdir() if path.is_dir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles-root",
        type=Path,
        default=Path(os.environ.get("HERMES_PROFILES_ROOT", "~/.hermes/profiles")).expanduser(),
    )
    parser.add_argument("--profile", action="append", help="Profile name to audit; repeatable")
    parser.add_argument("--warn-token-ratio", type=float, default=0.80)
    parser.add_argument("--warn-chain-depth", type=int, default=4)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--fail-on-critical",
        action="store_true",
        help="Exit non-zero when critical issues are present",
    )
    args = parser.parse_args(argv)

    results = [
        audit_profile(
            profile,
            warn_token_ratio=args.warn_token_ratio,
            warn_chain_depth=args.warn_chain_depth,
        )
        for profile in _iter_profiles(args.profiles_root, args.profile)
    ]
    critical_count = sum(
        1
        for result in results
        for issue in result.get("issues", [])
        if issue.get("severity") == "critical"
    )
    warning_count = sum(
        1
        for result in results
        for issue in result.get("issues", [])
        if issue.get("severity") == "warning"
    )
    payload = {
        "profiles_root": str(args.profiles_root),
        "profiles_checked": len(results),
        "critical_count": critical_count,
        "warning_count": warning_count,
        "profiles": results,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"profiles_checked={payload['profiles_checked']} "
            f"critical={critical_count} warning={warning_count}"
        )
        for result in results:
            issues = result.get("issues", [])
            if not issues:
                continue
            print(f"\n{result['profile']}:")
            for issue in issues:
                tip = f" tip={issue['tip_session_id']}" if issue.get("tip_session_id") else ""
                print(
                    f"  [{issue['severity']}] {issue['kind']} "
                    f"session={issue['session_id']}{tip} - {issue['detail']}"
                )

    return 1 if args.fail_on_critical and critical_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
