"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import os
import json
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence


class ProtectedFileOperation(str, Enum):
    READ = "read"
    WRITE = "write"
    RENAME = "rename"
    DELETE = "delete"
    ARCHIVE = "archive"
    IMPORT = "import"


class ProtectedFileCapability(str, Enum):
    """Typed internal authority for Hermes control-file operations.

    These values are deliberately not accepted by model-facing file tools.
    Only narrow operator/config lifecycle call sites may pass them.
    """

    INTERNAL_CONFIG = "internal_config"
    MCP_REGISTRATION = "mcp_registration"
    PROFILE_LIFECYCLE = "profile_lifecycle"
    BACKUP_RESTORE = "backup_restore"


@dataclass(frozen=True)
class ProtectedFileDecision:
    allowed: bool
    operation: ProtectedFileOperation
    protected: bool
    capability: Optional[ProtectedFileCapability]
    matched_path: Optional[str] = None
    reason: Optional[str] = None


_CONTROL_FILE_NAMES = frozenset({
    "auth.json",
    "auth.lock",
    "config.yaml",
    "webhook_subscriptions.json",
    ".env",
    ".anthropic_oauth.json",
    "mcp-authorizations.json",
})
_CONTROL_RELATIVE_FILES = frozenset({
    ("auth", "google_oauth.json"),
    ("cache", "bws_cache.json"),
})
_CONTROL_DIRECTORY_NAMES = frozenset({"mcp-installs", "mcp-tokens", "pairing"})
_MUTATING_CONTROL_OPERATIONS = frozenset({
    ProtectedFileOperation.WRITE,
    ProtectedFileOperation.RENAME,
    ProtectedFileOperation.DELETE,
    ProtectedFileOperation.ARCHIVE,
    ProtectedFileOperation.IMPORT,
})


def _normalized_component(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _absolute_path(path: str | os.PathLike[str], cwd: str | None = None) -> Path:
    raw = os.path.expandvars(os.path.expanduser(os.fspath(path)))
    if cwd and not os.path.isabs(raw):
        raw = os.path.join(cwd, raw)
    return Path(os.path.abspath(os.path.normpath(raw)))


def _control_bases() -> list[Path]:
    bases: list[Path] = []
    for base in (_hermes_home_path(), _hermes_root_path()):
        try:
            absolute = _absolute_path(base)
        except Exception:
            continue
        if absolute not in bases:
            bases.append(absolute)
    return bases


def _relative_parts_casefold(path: Path, base: Path) -> Optional[tuple[str, ...]]:
    try:
        relative = path.relative_to(base)
    except ValueError:
        return None
    return tuple(_normalized_component(part) for part in relative.parts)


def _is_control_relative_path(parts: Sequence[str]) -> bool:
    if not parts:
        return False
    if len(parts) == 1 and parts[0] in _CONTROL_FILE_NAMES:
        return True
    if tuple(parts) in _CONTROL_RELATIVE_FILES:
        return True
    if parts[0] in _CONTROL_DIRECTORY_NAMES:
        return True
    return False


def _is_control_ancestor(parts: Sequence[str]) -> bool:
    """Return true for directories whose mutation removes protected children."""
    return tuple(parts) in {("auth",), ("cache",)}


def _classify_control_path(candidate: Path, operation: ProtectedFileOperation) -> bool:
    bases = _control_bases()
    root = _absolute_path(_hermes_root_path())
    for base in bases:
        parts = _relative_parts_casefold(candidate, base)
        if parts is not None and _is_control_relative_path(parts):
            return True
        if (
            parts is not None
            and operation in _MUTATING_CONTROL_OPERATIONS
            and _is_control_ancestor(parts)
        ):
            return True

    # Every named profile is an alternate Hermes root. Protect its control
    # files even when it is not the active HERMES_HOME.
    root_parts = _relative_parts_casefold(candidate, root)
    if root_parts and len(root_parts) >= 3 and root_parts[0] == "profiles":
        if _is_control_relative_path(root_parts[2:]):
            return True

    # Mutating a Hermes/profile root itself can rename/delete/archive every
    # control file below it, so it is a protected target too.
    if operation in _MUTATING_CONTROL_OPERATIONS:
        if any(candidate == base for base in bases):
            return True
        if root_parts and len(root_parts) == 2 and root_parts[0] == "profiles":
            return True
        if root_parts == ("profiles",):
            return True
    return False


def decide_protected_control_file(
    operation: ProtectedFileOperation | str,
    paths: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
    *,
    capability: ProtectedFileCapability | str | None = None,
    cwd: str | None = None,
) -> ProtectedFileDecision:
    """Make one typed decision for aliases, resolved sources, and destinations."""
    try:
        typed_operation = ProtectedFileOperation(operation)
    except ValueError:
        raise ValueError(f"Unsupported protected-file operation: {operation!r}") from None
    try:
        typed_capability = ProtectedFileCapability(capability) if capability is not None else None
    except ValueError:
        raise ValueError(f"Unsupported protected-file capability: {capability!r}") from None

    raw_paths = [paths] if isinstance(paths, (str, os.PathLike)) else list(paths)
    for raw_path in raw_paths:
        try:
            lexical = _absolute_path(raw_path, cwd=cwd)
            resolved = Path(os.path.realpath(lexical))
        except (OSError, TypeError, ValueError) as exc:
            return ProtectedFileDecision(
                allowed=False,
                operation=typed_operation,
                protected=True,
                capability=typed_capability,
                matched_path=os.fspath(raw_path),
                reason=f"protected-file path could not be normalized: {exc}",
            )
        if not (
            _classify_control_path(lexical, typed_operation)
            or _classify_control_path(resolved, typed_operation)
        ):
            continue

        allowed_capabilities = {
            ProtectedFileOperation.READ: {
                ProtectedFileCapability.BACKUP_RESTORE,
                ProtectedFileCapability.INTERNAL_CONFIG,
                ProtectedFileCapability.MCP_REGISTRATION,
                ProtectedFileCapability.PROFILE_LIFECYCLE,
            },
            ProtectedFileOperation.WRITE: {
                ProtectedFileCapability.INTERNAL_CONFIG,
                ProtectedFileCapability.MCP_REGISTRATION,
            },
            ProtectedFileOperation.RENAME: {ProtectedFileCapability.PROFILE_LIFECYCLE},
            ProtectedFileOperation.DELETE: {ProtectedFileCapability.PROFILE_LIFECYCLE},
            ProtectedFileOperation.ARCHIVE: {
                ProtectedFileCapability.BACKUP_RESTORE,
                ProtectedFileCapability.PROFILE_LIFECYCLE,
            },
            ProtectedFileOperation.IMPORT: {
                ProtectedFileCapability.BACKUP_RESTORE,
                ProtectedFileCapability.PROFILE_LIFECYCLE,
            },
        }[typed_operation]
        if typed_capability in allowed_capabilities:
            continue
        return ProtectedFileDecision(
            allowed=False,
            operation=typed_operation,
            protected=True,
            capability=typed_capability,
            matched_path=str(resolved),
            reason=(
                f"{typed_operation.value} denied for protected Hermes control path; "
                "use an operator-authorized typed configuration/profile API"
            ),
        )

    return ProtectedFileDecision(
        allowed=True,
        operation=typed_operation,
        protected=False,
        capability=typed_capability,
    )


def require_protected_control_file_capability(
    operation: ProtectedFileOperation | str,
    paths: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
    *,
    capability: ProtectedFileCapability | str | None = None,
    cwd: str | None = None,
) -> None:
    decision = decide_protected_control_file(
        operation,
        paths,
        capability=capability,
        cwd=cwd,
    )
    if not decision.allowed:
        raise PermissionError(decision.reason or "Protected Hermes control-file operation denied")


def _hermes_home_path() -> Path:
    """Resolve the active HERMES_HOME (profile-aware) without circular imports."""
    try:
        from hermes_constants import get_hermes_home  # local import to avoid cycles
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def _hermes_root_path() -> Path:
    """Resolve the Hermes root dir (always the parent of any profile, never per-profile)."""
    try:
        from hermes_constants import get_default_hermes_root  # local import to avoid cycles
        return get_default_hermes_root()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    hermes_home = _hermes_home_path()
    hermes_root = _hermes_root_path()
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            # Active profile .env (or top-level .env when not in profile mode).
            str(hermes_home / ".env"),
            # Top-level .env, even when running under a profile — overwriting it
            # leaks credentials across every profile that inherits from root (#15981).
            str(hermes_root / ".env"),
            # Active profile Anthropic PKCE credential store.
            str(hermes_home / ".anthropic_oauth.json"),
            # Top-level Anthropic PKCE credential store remains sensitive even
            # when a profile is active; default/non-profile sessions still read it.
            str(hermes_root / ".anthropic_oauth.json"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            os.path.join(home, ".git-credentials"),
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
            os.path.join(home, ".config", "gcloud"),
        ]
    ]


def get_safe_write_roots() -> set[str]:
    """Return resolved HERMES_WRITE_SAFE_ROOT paths. Supports multiple directories
    separated by ``os.pathsep`` (``:`` on Unix, ``;`` on Windows).
    E.g., ``/opt/data:/var/www/html`` on Unix, ``C:\\data;D:\\www`` on Windows."""
    env = os.getenv("HERMES_WRITE_SAFE_ROOT", "")
    if not env:
        return set()
    roots: set[str] = set()
    for path in env.split(os.pathsep):
        if path:
            try:
                resolved = os.path.realpath(os.path.expanduser(path))
                roots.add(resolved)
            except (OSError, ValueError):
                continue
    return roots


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
    """Yield explicitly configured filesystem path values from env/config structures."""
    if isinstance(value, dict):
        for key in _STRUCTURED_PATH_KEYS:
            if key in value:
                yield from _iter_path_values(value.get(key))
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


def _classify_write_denial(path: str) -> Optional[str]:
    """Return ``credential``, ``safe_root``, or ``None`` if writes are allowed."""
    if not decide_protected_control_file(ProtectedFileOperation.WRITE, path).allowed:
        return "credential"
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return "credential"
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return "credential"

    # Olympus boundary: configured writable surfaces stay authoritative — a
    # path outside every configured surface is denied before the upstream
    # control-plane checks run.
    safe_roots = get_writable_surfaces()
    if safe_roots and not any(
        resolved == root or resolved.startswith(root + os.sep)
        for root in safe_roots
    ):
        return "safe_root"
    mcp_tokens_dir_name = "mcp-tokens"

    hermes_dirs = []
    for base in (_hermes_home_path(), _hermes_root_path()):
        try:
            real = os.path.realpath(base)
            if real not in hermes_dirs:
                hermes_dirs.append(real)
        except Exception:
            continue

    for base_real in hermes_dirs:
        # Session transcripts are application-owned state.  Letting the agent's
        # generic file tools rewrite state.db or legacy JSON snapshots can
        # falsify conversation history and invalidate resume/compression state.
        try:
            if resolved == os.path.realpath(os.path.join(base_real, "state.db")):
                return "credential"
            sessions_real = os.path.realpath(os.path.join(base_real, "sessions"))
            if resolved == sessions_real or resolved.startswith(sessions_real + os.sep):
                return "credential"
        except Exception:
            pass
        try:
            mcp_real = os.path.realpath(os.path.join(base_real, mcp_tokens_dir_name))
            if resolved == mcp_real or resolved.startswith(mcp_real + os.sep):
                return "credential"
        except Exception:
            pass
        try:
            pairing_real = os.path.realpath(os.path.join(base_real, "pairing"))
            if resolved == pairing_real or resolved.startswith(pairing_real + os.sep):
                return "credential"
        except Exception:
            pass

    safe_roots = get_safe_write_roots()
    if safe_roots:
        allowed = False
        for safe_root in safe_roots:
            if resolved == safe_root or resolved.startswith(safe_root + os.sep):
                allowed = True
                break
        if not allowed:
            return "safe_root"

    return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    return _classify_write_denial(path) is not None


def get_write_denied_error(path: str, *, verb: str = "Write") -> Optional[str]:
    """Return a user/model-facing error when writes to ``path`` are blocked."""
    denial = _classify_write_denial(path)
    if denial is None:
        return None
    if denial == "safe_root":
        roots_display = os.pathsep.join(sorted(get_safe_write_roots()))
        return (
            f"{verb} denied: '{path}' is outside HERMES_WRITE_SAFE_ROOT "
            f"({roots_display}). Unset the variable or add this path's directory prefix."
        )
    return f"{verb} denied: '{path}' is a protected system/credential file."


# Common secret-bearing project-local environment file basenames.
# These are blocked because .env files routinely contain API keys,
# database passwords, and other credentials.
_BLOCKED_PROJECT_ENV_BASENAMES: set[str] = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".env.staging",
    ".envrc",
}


def get_read_block_error(path: str) -> Optional[str]:
    """Return an error message when a read targets a denied Hermes path.

    Three categories are blocked:

      * Internal Hermes cache files under ``HERMES_HOME/skills/.hub`` —
        readable metadata that an attacker could use as a prompt-injection
        carrier.
      * Credential / secret stores under HERMES_HOME and the global Hermes
        root: ``auth.json``, ``auth.lock``, ``.anthropic_oauth.json``,
        ``.env``, ``webhook_subscriptions.json``, ``auth/google_oauth.json``,
        and anything under ``mcp-tokens/``. These hold plaintext provider keys,
        OAuth tokens, and HMAC secrets that the agent never needs to read
        directly — provider tools / gateway adapters consume them through
        internal channels.
      * Project-local environment files anywhere on disk: ``.env``,
        ``.env.local``, ``.env.development``, ``.env.production``,
        ``.env.test``, ``.env.staging``, ``.envrc``. These routinely hold
        API keys, database passwords, and other credentials for the user's
        own projects. The agent helping debug a project shouldn't normally
        need to read these — ``.env.example`` is the documented-shape
        substitute.

    **This is NOT a security boundary.** The terminal tool runs as the
    same OS user with shell access; the agent can still ``cat auth.json``
    or ``cat ~/.hermes/.env`` and exfiltrate the file. The read-deny exists
    as defense-in-depth that:

      * Returns a clear error to models that respect tool denials, which
        empirically prompts most modern models to stop rather than reach
        for the shell.
      * Surfaces a visible audit trail when something tries to read
        credentials — easier to spot in logs than a generic ``cat``.

    Treat any user-visible framing around this as "may help" rather than
    "stops attackers." A determined model or malicious instruction can
    always shell out.

    Callers that resolve relative paths against a non-process cwd
    (e.g. ``TERMINAL_CWD`` in ``tools/file_tools.py``) MUST pre-resolve
    and pass the absolute path string.  This function's own ``resolve()``
    is anchored at the Python process cwd, so a relative input like
    ``"auth.json"`` would otherwise miss the denylist when the task's
    terminal cwd differs from the process cwd.
    """
    control_decision = decide_protected_control_file(ProtectedFileOperation.READ, path)
    if not control_decision.allowed:
        kind = (
            "MCP token store"
            if "mcp-tokens" in _normalized_component(str(control_decision.matched_path or path))
            else "credential store or protected Hermes control path"
        )
        return (
            f"Access denied: {path} is a Hermes {kind} and "
            "cannot be read by model-facing file tools."
        )

    resolved = Path(path).expanduser().resolve()

    # Resolve BOTH the active HERMES_HOME (profile-aware) AND the global
    # Hermes root so credential stores at <root>/auth.json etc. are also
    # blocked when running under a profile (HERMES_HOME points at
    # <root>/profiles/<name> in profile mode). Same shape as the write
    # deny widening (#15981, #14157).
    hermes_dirs: list[Path] = []
    for base in (_hermes_home_path(), _hermes_root_path()):
        try:
            real = base.resolve()
            if real not in hermes_dirs:
                hermes_dirs.append(real)
        except Exception:
            continue

    # Skills .hub: prompt-injection carriers.
    for hd in hermes_dirs:
        blocked_dirs = [
            hd / "skills" / ".hub" / "index-cache",
            hd / "skills" / ".hub",
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

    # Credential / secret stores. Exact-file matches under either
    # HERMES_HOME or <root>.
    credential_file_names = (
        "auth.json",
        "auth.lock",
        ".anthropic_oauth.json",
        ".env",
        "webhook_subscriptions.json",
        os.path.join("auth", "google_oauth.json"),
        # Bitwarden Secrets Manager disk cache: stores plaintext secret values
        # to avoid re-fetching across back-to-back CLI invocations. The file
        # was introduced by #31968 but not added to this guard.
        os.path.join("cache", "bws_cache.json"),
    )
    for hd in hermes_dirs:
        for name in credential_file_names:
            try:
                blocked = (hd / name).resolve()
            except Exception:
                continue
            if resolved == blocked:
                return (
                    f"Access denied: {path} is a Hermes credential store "
                    "and cannot be read directly. Provider tools consume "
                    "these credentials through internal channels. "
                    "(Defense-in-depth — not a security boundary; the "
                    "terminal tool can still bypass.)"
                )

    # mcp-tokens/: directory prefix match — anything inside is OAuth
    # token material.
    for hd in hermes_dirs:
        try:
            mcp_tokens = (hd / "mcp-tokens").resolve()
        except Exception:
            continue
        if resolved == mcp_tokens:
            return (
                f"Access denied: {path} is the Hermes MCP token directory "
                "and cannot be read directly. (Defense-in-depth — not a "
                "security boundary; the terminal tool can still bypass.)"
            )
        try:
            resolved.relative_to(mcp_tokens)
        except ValueError:
            continue
        return (
            f"Access denied: {path} is a Hermes MCP token file "
            "and cannot be read directly. (Defense-in-depth — not a "
            "security boundary; the terminal tool can still bypass.)"
        )

    # Block common secret-bearing project-local .env files anywhere on disk.
    # The agent helping a user with their project rarely needs to read raw
    # .env contents — .env.example is the documented-shape substitute. The
    # terminal tool can still ``cat .env``; this is defense-in-depth, not a
    # boundary (see module docstring).
    if resolved.name.lower() in _BLOCKED_PROJECT_ENV_BASENAMES:
        return (
            f"Access denied: {path} is a secret-bearing environment file "
            "and cannot be read to prevent credential leakage. "
            "If you need to check the file structure, read .env.example instead. "
            "(Defense-in-depth — not a security boundary; the terminal tool can still bypass.)"
        )

    return None


def raise_if_read_blocked(path: str) -> None:
    """Raise ``ValueError`` if ``path`` is a denied Hermes read (see
    :func:`get_read_block_error`), else return.

    Shared chokepoint for provider input-loading sites that read a local
    file the model/tool supplied (e.g. image-gen ``image_url`` /
    ``reference_image_urls`` paths). Centralizes the guard so every provider
    enforces the same read boundary with identical semantics instead of each
    open-coding the try/except block (#57698).

    Best-effort by design: if ``agent.file_safety`` machinery is somehow
    unavailable at the call site the guard no-ops rather than breaking local
    image loading — consistent with the defense-in-depth (not security
    boundary) framing of the denylist itself. The blocking ``ValueError`` from
    a real hit still propagates; only unexpected internal errors are swallowed.
    """
    try:
        blocked = get_read_block_error(path)
    except Exception:  # noqa: BLE001 - guard must never break local-file loading
        return
    if blocked:
        raise ValueError(blocked)


# ---------------------------------------------------------------------------
# Cross-profile write guard (#TBD)
#
# Hermes profiles are separate HERMES_HOME dirs under
# ``<root>/profiles/<name>/``. Each profile has its own skills/, plugins/,
# cron/, memories/. When an agent runs under one profile, writing into
# ANOTHER profile's directories is almost always wrong — those skills /
# plugins / cron jobs / memories affect a different session the user runs
# from a different shell.
#
# Soft guard, NOT a security boundary: the agent runs as the same OS user
# and has unrestricted terminal access, so this returns a warning the model
# can choose to honor or override with ``cross_profile=True``. Same shape
# as the dangerous-command approval flow — the agent is told the boundary
# exists, and explicit user direction is required to cross it.
#
# Reference: May 2026 incident where a hermes-security profile session
# edited skills under both ``~/.hermes/profiles/hermes-security/skills/``
# AND ``~/.hermes/skills/`` (the default profile's skills) without realizing
# the second path belonged to a different profile.
# ---------------------------------------------------------------------------

# Profile-scoped directories under HERMES_HOME / <root> / <root>/profiles/<X>/
# that should be guarded. Adding a new area here extends the guard with no
# other code change.
PROFILE_SCOPED_AREAS = ("skills", "plugins", "cron", "memories")


def _resolve_active_profile_name() -> str:
    """Return the active profile name derived from HERMES_HOME.

    ``~/.hermes``              -> ``"default"``
    ``~/.hermes/profiles/X``  -> ``"X"``

    Falls back to ``"default"`` on any resolution failure so the guard
    never raises into the tool path.
    """
    try:
        home_real = _hermes_home_path().resolve()
        root_real = _hermes_root_path().resolve()
    except (OSError, RuntimeError):
        return "default"
    profiles_dir = root_real / "profiles"
    try:
        rel = home_real.relative_to(profiles_dir)
        parts = rel.parts
        if len(parts) >= 1:
            return parts[0]
    except ValueError:
        pass
    return "default"


def classify_cross_profile_target(path: str) -> Optional[dict]:
    """Classify a write target as cross-profile if it lands in another
    profile's scoped area (skills/plugins/cron/memories).

    Returns ``None`` when the target is outside Hermes scope, or is inside
    the ACTIVE profile, or doesn't hit a profile-scoped area. Otherwise
    returns a dict with:

      * ``active_profile``: name of the profile the agent is running as
      * ``target_profile``: name of the profile the path belongs to
      * ``area``: which scoped area (``"skills"``, ``"plugins"``, etc.)
      * ``target_path``: the resolved path string

    The caller decides what to do with the result — surface a warning to
    the model, prompt the user, or (with explicit consent /
    ``cross_profile=True``) proceed anyway.
    """
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
        root_real = _hermes_root_path().resolve()
    except (OSError, RuntimeError):
        return None

    target_profile: Optional[str] = None
    area: Optional[str] = None

    try:
        rel = target.relative_to(root_real)
    except ValueError:
        return None

    parts = rel.parts
    if not parts:
        return None

    if parts[0] in PROFILE_SCOPED_AREAS:
        # ``<root>/<area>/...`` → default profile.
        target_profile = "default"
        area = parts[0]
    elif (
        parts[0] == "profiles"
        and len(parts) >= 3
        and parts[2] in PROFILE_SCOPED_AREAS
    ):
        # ``<root>/profiles/<name>/<area>/...`` → named profile.
        target_profile = parts[1]
        area = parts[2]
    else:
        return None

    active_profile = _resolve_active_profile_name()
    if target_profile == active_profile:
        # In-profile write — not a cross-profile event.
        return None

    return {
        "active_profile": active_profile,
        "target_profile": target_profile,
        "area": area,
        "target_path": str(target),
    }


def get_cross_profile_warning(path: str) -> Optional[str]:
    """Return a model-facing warning string when ``path`` is cross-profile.

    Returns ``None`` when the write is in-scope (same profile) or outside
    Hermes entirely. Caller is expected to surface the warning to the
    agent as a tool-result error, NOT to silently allow the write — the
    agent must either get explicit user direction to proceed, or pass
    ``cross_profile=True`` to its write tool.

    This is defense-in-depth: the terminal tool runs as the same OS user
    and can write any of these paths without going through this guard.
    Treat the guard as a confusion-reducer, not a security boundary.
    """
    info = classify_cross_profile_target(path)
    if info is None:
        return None
    return (
        f"Cross-profile write blocked by soft guard: {info['target_path']} "
        f"belongs to Hermes profile {info['target_profile']!r}, but the "
        f"agent is running under profile {info['active_profile']!r}. "
        f"Editing another profile's {info['area']}/ will affect that "
        f"profile's future sessions, not the one you are currently in. "
        f"Confirm with the user before proceeding. To bypass this guard "
        f"after explicit user direction, retry the call with "
        f"``cross_profile=True``. (Defense-in-depth — not a security "
        f"boundary; the terminal tool can still bypass.)"
    )


# ---------------------------------------------------------------------------
# Sandbox-mirror write guard (#32049)
#
# Non-local terminal backends (Docker, Daytona, etc.) bind a sandbox-local
# directory to the container's ``$HOME``. The on-disk layout looks like
#
#   <HERMES_HOME>/profiles/<name>/sandboxes/<backend>/<task>/home/.hermes/...
#
# When the agent (running host-side) speculates that authoritative profile
# state lives at one of those sandbox-mirror paths, the write lands on the
# mirror — never read by the host process — while the host file is left
# untouched. The agent reports success, the user sees no change, and on
# disk two divergent copies accumulate. See #32049 for evidence.
#
# This guard is path-shape-only: it detects the
# ``…/sandboxes/<backend>/<task>/home/.hermes/…`` segment and warns
# regardless of which Hermes profile is active. It does NOT cover the
# inner-container case where the bind mount strips the ``sandboxes/`` prefix
# (the agent's view inside the container is plain ``/root/.hermes/...``);
# that case needs a separate dispatch-layer or host-side ``profile_state``
# tool.
# ---------------------------------------------------------------------------


def _find_sandbox_mirror_segments(parts: tuple) -> Optional[int]:
    """Return the index of the inner ``.hermes`` part in a sandbox-mirror path.

    Matches ``…/sandboxes/<backend>/<task>/home/.hermes/…`` and returns the
    index where the inner Hermes-state portion starts. Returns ``None`` for
    paths that do not contain the sandbox-mirror shape.
    """
    for i, part in enumerate(parts):
        if part != "sandboxes":
            continue
        # Need at least: sandboxes / <backend> / <task> / home / .hermes / <thing>
        if i + 5 >= len(parts):
            continue
        if parts[i + 3] == "home" and parts[i + 4] == ".hermes":
            return i + 4
    return None


def classify_sandbox_mirror_target(path: str) -> Optional[dict]:
    """Classify a write target as a sandbox-mirror of authoritative Hermes state.

    Returns ``None`` when the path does not match the sandbox-mirror shape.
    Otherwise returns a dict with:

      * ``target_path``: the resolved path string
      * ``mirror_root``: the ``…/sandboxes/<backend>/<task>/home/.hermes``
        prefix (so callers can show users which sandbox owns the mirror)
      * ``inner_path``: the portion under the mirror's ``.hermes`` (what the
        agent likely meant to address on the host)

    Detection is path-shape-only — does not require any Hermes resolver to
    succeed, so it works correctly even when called from contexts where
    HERMES_HOME resolution would be ambiguous.
    """
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
    except (OSError, RuntimeError):
        return None

    parts = target.parts
    inner_idx = _find_sandbox_mirror_segments(parts)
    if inner_idx is None:
        return None

    mirror_root = str(Path(*parts[: inner_idx + 1]))
    inner_path = str(Path(*parts[inner_idx + 1 :])) if inner_idx + 1 < len(parts) else ""

    return {
        "target_path": str(target),
        "mirror_root": mirror_root,
        "inner_path": inner_path,
    }


def get_sandbox_mirror_warning(path: str) -> Optional[str]:
    """Return a model-facing warning when ``path`` lands in a sandbox mirror.

    Returns ``None`` when the path is not a sandbox-mirror target. Caller
    is expected to surface the warning to the agent as a tool-result
    error. The bypass kwarg (``cross_profile=True``) is shared with the
    cross-profile guard: both are soft "I know what I'm doing" overrides
    a user can authorise.

    Defense-in-depth, NOT a security boundary: the terminal tool runs as
    the same OS user and can write the mirror path directly. The guard
    exists to surface the misclassification before the silent-success +
    divergent-copy footgun in #32049 fires.
    """
    info = classify_sandbox_mirror_target(path)
    if info is None:
        return None
    return (
        f"Sandbox-mirror write blocked by soft guard: {info['target_path']} "
        f"sits under {info['mirror_root']!r}, which is a per-task mirror "
        f"created by a non-local terminal backend (docker/daytona/etc.). "
        f"Writes here land on a copy that the host Hermes process never "
        f"reads — the authoritative file is likely {info['inner_path']!r} "
        f"under the real HERMES_HOME. Use the host-side tool for "
        f"authoritative state (e.g. ``memory`` for memories), or address "
        f"the host path directly. To bypass this guard after explicit "
        f"user direction, retry the call with ``cross_profile=True``. "
        f"(Defense-in-depth — not a security boundary; the terminal tool "
        f"can still bypass.)"
    )


# ---------------------------------------------------------------------------
# Container-context mirror guard (inner-container case — #32049 follow-up)
#
# Brian's shape-based detector (#32213) catches paths that still carry the
# full ``…/sandboxes/<backend>/<task>/home/.hermes/…`` prefix on the host.
# But when file tools execute *inside* the container the bind-mount strips
# that prefix: the agent sees plain ``/root/.hermes/…``.  The root:root
# ownership on the divergent SOUL.md in #32049 confirms this is the primary
# failure mode.
#
# Fix: file_tools passes the active Docker mirror prefix when the terminal
# backend is docker + persistent. This catches the very first file-tool call,
# before a DockerEnvironment object necessarily exists.
# ---------------------------------------------------------------------------


def classify_container_mirror_target(
    path: str,
    mirror_prefix: str | None = None,
) -> Optional[dict]:
    """Classify a write target as a container-side sandbox mirror.

    ``mirror_prefix`` must be supplied by the caller after it has established
    that file tools are executing in a container whose home is a sandbox
    mirror. Returns ``None`` when no such context is active or the path is not
    under the mirror prefix. Otherwise returns:

      * ``target_path``: resolved path string
      * ``mirror_root``: the declared container mirror prefix
      * ``inner_path``: portion under the mirror root (what the agent
        likely meant to address in the host HERMES_HOME)
    """
    if not mirror_prefix:
        return None
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
        mirror = Path(os.path.expanduser(mirror_prefix)).resolve()
        inner = target.relative_to(mirror)
    except (OSError, RuntimeError, ValueError):
        return None
    return {
        "target_path": str(target),
        "mirror_root": str(mirror),
        "inner_path": inner.as_posix(),
    }


def get_container_mirror_warning(
    path: str,
    mirror_prefix: str | None = None,
) -> Optional[str]:
    """Return a model-facing warning when *path* lands in the container's
    sandbox mirror of authoritative Hermes state.

    The caller supplies ``mirror_prefix`` only when the current file-tool
    backend is known to execute inside a Docker sandbox. Same contract as
    ``get_cross_profile_warning``: soft guard, returns ``None`` for
    non-mirror paths, caller surfaces as a tool-result error. Bypass via
    ``cross_profile=True`` after explicit user direction.
    """
    info = classify_container_mirror_target(path, mirror_prefix)
    if info is None:
        return None
    return (
        f"Sandbox-mirror write blocked by soft guard: {info['target_path']} "
        f"sits under {info['mirror_root']!r}, which is the container's "
        f"bind-mounted home — a per-task mirror that the host Hermes "
        f"process never reads. The authoritative file is "
        f"{info['inner_path']!r} under the real HERMES_HOME. Use the "
        f"host-side tool for authoritative state (e.g. ``memory`` for "
        f"memories), or address the host path directly. To bypass after "
        f"explicit user direction, retry with ``cross_profile=True``. "
        f"(Defense-in-depth — not a security boundary; the terminal tool "
        f"can still bypass.)"
    )
