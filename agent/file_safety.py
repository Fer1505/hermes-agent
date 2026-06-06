"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Iterable, Optional


def _hermes_home_path() -> Path:
    """Resolve the active HERMES_HOME (profile-aware) without circular imports."""
    try:
        from hermes_constants import get_hermes_home  # local import to avoid cycles
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    hermes_home = _hermes_home_path()
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            str(hermes_home / ".env"),
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".profile"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".zprofile"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        ]
    }


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    return [
        os.path.realpath(p) + os.sep
        for p in [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            "/etc/sudoers.d",
            "/etc/systemd",
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
        ]
    ]


def get_safe_write_root() -> Optional[str]:
    """Return the resolved HERMES_WRITE_SAFE_ROOT path, or None if unset."""
    root = os.getenv("HERMES_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def _load_runtime_boundary_config() -> dict:
    """Best-effort config load for runtime path boundary settings."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


_STRUCTURED_PATH_KEYS = (
    "path",
    "root",
    "filesystemPath",
    "filesystem_path",
    "workspaceRoot",
    "workspace_root",
    "writeSafeRoot",
    "write_safe_root",
)


def _iter_path_or_route_values(value: object) -> Iterable[str]:
    """Yield concrete filesystem paths from an ambiguous path/route field.

    Olympus runtime manifests historically used ``pathOrRoute`` for both
    filesystem roots and API-ish routes such as ``/tasks``. Treating every
    slash-prefixed route as a writable filesystem root is unsafe, while
    ignoring the field completely breaks profiles that declare real repo
    roots there.  Only accept absolute/``~`` entries that already resolve to
    an existing filesystem location; route labels and globs are ignored.
    """
    for raw in _iter_scalar_path_values(value):
        if any(ch in raw for ch in "*?[]{}"):
            continue
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if not os.path.isabs(expanded):
            continue
        try:
            if os.path.exists(os.path.realpath(expanded)):
                yield raw
        except OSError:
            continue


def _iter_scalar_path_values(value: object) -> Iterable[str]:
    """Yield string path fragments from scalar/list env or config values."""
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    yield from _iter_path_values(item)
                return
        for piece in text.replace(",", os.pathsep).split(os.pathsep):
            piece = piece.strip()
            if piece:
                yield piece
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_scalar_path_values(item)


def _iter_path_values(value: object) -> Iterable[str]:
    """Yield configured filesystem path values from env/config structures."""
    if isinstance(value, dict):
        for key in _STRUCTURED_PATH_KEYS:
            if key in value:
                yield from _iter_path_values(value.get(key))
        for key in ("pathOrRoute", "path_or_route"):
            if key in value:
                yield from _iter_path_or_route_values(value.get(key))
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_path_values(item)
        return
    yield from _iter_scalar_path_values(value)


def _resolve_boundary_path(path: str) -> Optional[str]:
    try:
        expanded = os.path.expandvars(os.path.expanduser(path))
        return os.path.realpath(expanded)
    except Exception:
        return None


def _dedupe_resolved_paths(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        resolved = _resolve_boundary_path(raw)
        if resolved and resolved not in seen:
            out.append(resolved)
            seen.add(resolved)
    return out


def _nested_config_value(cfg: dict, keys: tuple[str, ...]) -> object:
    cur: object = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def get_workspace_roots(config: Optional[dict] = None) -> list[str]:
    """Return configured workspace read/cwd roots.

    Supports both env vars and config spellings used by runtime manifests:
    ``workspaceRoot`` / ``workspace_root`` at top level, under ``runtime``,
    or under ``permissions``. Explicit writable filesystem roots are also
    readable; otherwise a profile can be authorized to edit a repo but unable
    to read the same files before patching them. Empty means no workspace
    boundary is active.
    """
    cfg = config if isinstance(config, dict) else _load_runtime_boundary_config()
    raw_values: list[object] = [
        os.getenv("HERMES_WORKSPACE_ROOTS"),
        os.getenv("HERMES_WORKSPACE_ROOT"),
        os.getenv("HERMES_WRITE_SAFE_ROOTS"),
        os.getenv("HERMES_WRITE_SAFE_ROOT"),
        cfg.get("workspaceRoots"),
        cfg.get("workspace_roots"),
        cfg.get("workspaceRoot"),
        cfg.get("workspace_root"),
        cfg.get("writableSurfaces"),
        cfg.get("writable_surfaces"),
        _nested_config_value(cfg, ("runtime", "workspaceRoots")),
        _nested_config_value(cfg, ("runtime", "workspace_roots")),
        _nested_config_value(cfg, ("runtime", "workspaceRoot")),
        _nested_config_value(cfg, ("runtime", "workspace_root")),
        _nested_config_value(cfg, ("runtime", "writableSurfaces")),
        _nested_config_value(cfg, ("runtime", "writable_surfaces")),
        _nested_config_value(cfg, ("permissions", "workspaceRoots")),
        _nested_config_value(cfg, ("permissions", "workspace_roots")),
        _nested_config_value(cfg, ("permissions", "workspaceRoot")),
        _nested_config_value(cfg, ("permissions", "workspace_root")),
        _nested_config_value(cfg, ("permissions", "writableSurfaces")),
        _nested_config_value(cfg, ("permissions", "writable_surfaces")),
        _nested_config_value(cfg, ("terminal", "writableSurfaces")),
        _nested_config_value(cfg, ("terminal", "writable_surfaces")),
        _nested_config_value(cfg, ("terminal", "write_safe_root")),
    ]
    return _dedupe_resolved_paths(
        path for value in raw_values for path in _iter_path_values(value)
    )


def get_writable_surfaces(config: Optional[dict] = None) -> list[str]:
    """Return configured write/cwd roots.

    ``HERMES_WRITE_SAFE_ROOT`` remains the backward-compatible single-root
    setting. The plural env/config forms support multiple writable surfaces.
    If only ``workspaceRoot`` is configured, it is also used as the writable
    surface so a workspace-only manifest still gets write protection.
    """
    cfg = config if isinstance(config, dict) else _load_runtime_boundary_config()
    raw_values: list[object] = [
        os.getenv("HERMES_WRITE_SAFE_ROOTS"),
        os.getenv("HERMES_WRITE_SAFE_ROOT"),
        cfg.get("writableSurfaces"),
        cfg.get("writable_surfaces"),
        _nested_config_value(cfg, ("runtime", "writableSurfaces")),
        _nested_config_value(cfg, ("runtime", "writable_surfaces")),
        _nested_config_value(cfg, ("permissions", "writableSurfaces")),
        _nested_config_value(cfg, ("permissions", "writable_surfaces")),
        _nested_config_value(cfg, ("terminal", "writableSurfaces")),
        _nested_config_value(cfg, ("terminal", "writable_surfaces")),
        _nested_config_value(cfg, ("terminal", "write_safe_root")),
    ]
    surfaces = _dedupe_resolved_paths(
        path for value in raw_values for path in _iter_path_values(value)
    )
    return surfaces or get_workspace_roots(cfg)


def is_path_within_roots(path: str, roots: Iterable[str], cwd: str | None = None) -> bool:
    """Return True when *path* resolves inside one of *roots*."""
    if not path:
        return False
    candidate = os.path.expandvars(os.path.expanduser(str(path)))
    if cwd and not os.path.isabs(candidate):
        candidate = os.path.join(cwd, candidate)
    resolved = os.path.realpath(candidate)
    for root in roots:
        root_resolved = os.path.realpath(os.path.expanduser(str(root)))
        if resolved == root_resolved or resolved.startswith(root_resolved + os.sep):
            return True
    return False


def get_path_boundary_error(
    path: str,
    *,
    purpose: str,
    cwd: str | None = None,
    config: Optional[dict] = None,
) -> Optional[str]:
    """Return a boundary error for a configured read/write/workdir path.

    No configured roots means fail-open for backward compatibility. This is
    intentionally a path guard, not a shell sandbox: terminal commands can
    still reference absolute paths after launch unless the backend itself is
    sandboxed.
    """
    roots = get_workspace_roots(config) if purpose == "read" else get_writable_surfaces(config)
    if not roots:
        return None
    if is_path_within_roots(path, roots, cwd=cwd):
        return None
    roots_text = ", ".join(roots)
    return (
        f"Path boundary denied for {purpose}: {path!r} resolves outside "
        f"configured root(s): {roots_text}"
    )


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    safe_roots = get_writable_surfaces()
    if safe_roots and not any(
        resolved == root or resolved.startswith(root + os.sep)
        for root in safe_roots
    ):
        return True

    return False


def get_read_block_error(path: str) -> Optional[str]:
    """Return an error message when a read targets internal Hermes cache files."""
    resolved = Path(path).expanduser().resolve()
    hermes_home = _hermes_home_path().resolve()
    blocked_dirs = [
        hermes_home / "skills" / ".hub" / "index-cache",
        hermes_home / "skills" / ".hub",
    ]
    for blocked in blocked_dirs:
        try:
            resolved.relative_to(blocked)
        except ValueError:
            continue
        return (
            f"Access denied: {path} is an internal Hermes cache file "
            "and cannot be read directly to prevent prompt injection. "
            "Use the skills_list or skill_view tools instead."
        )
    return None
