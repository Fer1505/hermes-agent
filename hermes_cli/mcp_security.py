"""Fail-closed authorization for persisted MCP executable software.

HTTP MCP endpoints remain data-only configuration. Persisted stdio MCP entries
are executable software and therefore require one of two attestations:

* a currently shipped catalog manifest, with exact command/args and content
  hashes; or
* an explicit operator CLI authorization receipt for one direct executable.

Hand-edited, unpinned, provenance-free, indirect interpreter/package-runner,
or changed-on-disk entries are rejected before startup. The historical IOC and
shell-payload checks remain as high-signal diagnostics:

1. The exfiltration shape from #45620: a shell interpreter whose inline script
   invokes network egress tooling.
2. The persistence shape from the June 2026 ``hermes-0day`` campaign: a shell
   interpreter whose inline script writes to OS persistence surfaces
   (``~/.ssh/authorized_keys``, ``/etc/ssh``, ``/etc/pam.d``, ``sudoers``,
   crontab, shell rc files). The campaign planted ``command: bash`` MCP entries
   whose payload appended an attacker SSH key to ``authorized_keys``; Hermes
   re-executed them on every cron tick / startup, re-installing the backdoor.

3. A hardcoded indicator-of-compromise (IOC) blocklist for that campaign — the
   attacker's ``hermes-0day`` SSH public key and source IPs. Any entry whose
   command/args/env carry an IOC is refused outright, regardless of shape, so a
   pre-planted ``config.yaml`` cannot spawn it.

These checks run BOTH at save time (``_save_mcp_server`` — dashboard API + CLI)
and at spawn time (``tools.mcp_tool._filter_suspicious_mcp_servers`` — discovery
/ cron / startup), so a hand-edited or pre-planted entry is also caught before
it can execute.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from agent.file_safety import (
    ProtectedFileCapability,
    ProtectedFileOperation,
    require_protected_control_file_capability,
)

_ATTESTATION_KEY = "_hermes_stdio_authorization"
_ATTESTATION_SCHEMA = "hermes-mcp-stdio/v1"
_RECEIPT_SCHEMA = "hermes-mcp-authorizations/v1"

_SHELL_INTERPRETERS = frozenset({
    "bash",
    "sh",
    "zsh",
    "dash",
    "fish",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
})

_INDIRECT_EXECUTABLES = _SHELL_INTERPRETERS | frozenset({
    "python", "python2", "python3", "py",
    "node", "nodejs", "npm", "npx", "pnpm", "yarn", "bun", "deno",
    "uv", "uvx", "pip", "pip3", "env",
})

_DANGEROUS_ENV_NAMES = frozenset({
    "path", "pythonpath", "pythonhome", "node_options", "shell", "comspec",
})

_ENCODED_OR_INDIRECT_ARG = re.compile(
    r"(?:^|[^a-z0-9])(?:base64|frombase64|string\.fromcharcode)(?:$|[^a-z0-9])"
    r"|^data:[^,]+;base64,"
    r"|[A-Za-z0-9+/]{96,}={0,2}",
    re.IGNORECASE,
)

_EGRESS_PATTERN = re.compile(
    r"(?<![\w.-])(?:curl|wget|nc|ncat|socat)(?![\w.-])"
    r"|/dev/tcp/"
    r"|\bInvoke-WebRequest\b"
    r"|\bInvoke-RestMethod\b"
    r"|\bSystem\.Net\.WebClient\b",
    re.IGNORECASE,
)

_EXFIL_HINT_PATTERN = re.compile(
    r"\.env\b|--data-binary|--data-raw|\b-X\s+POST\b|\bPOST\b|<\s*[^\s]+",
    re.IGNORECASE,
)

# OS persistence surfaces an MCP server has no legitimate reason to write to.
# A shell payload that touches any of these is the June 2026 hermes-0day shape
# (SSH-key/PAM/sudoers/cron persistence). Matched anywhere in the inline script.
_PERSISTENCE_PATTERN = re.compile(
    r"authorized_keys"               # SSH key persistence (the campaign's payload)
    r"|\.ssh/"                       # any write under ~/.ssh
    r"|/etc/ssh\b"                   # sshd_config / AuthorizedKeysCommand backdoor
    r"|/etc/pam\.d\b|pam_[\w-]+\.so" # PAM credential logger
    r"|/etc/sudoers"                 # sudoers escalation
    r"|/etc/cron|crontab\b"          # cron persistence
    r"|/etc/rc\.local|/etc/systemd"  # init / unit persistence
    r"|\.bashrc\b|\.bash_profile\b|\.profile\b|\.zshrc\b",  # shell rc backdoor
    re.IGNORECASE,
)

# ── Indicators of compromise: June 2026 hermes-0day campaign ──────────────────
# Hardcoded so a pre-planted config.yaml (written by any vector) is refused at
# both save and spawn time. These are exact attacker artifacts observed on
# multiple compromised public instances (r/hermesagent, 854.media).
_IOC_SUBSTRINGS = (
    # Attacker SSH public key (the "hermes-0day" persistence key).
    "AAAAC3NzaC1lZDI1NTE5AAAAICBoh1oDC4DnsO1m5mJ4yfEKrQebaFh",
    "hermes-0day",
    # Attacker source IPs (China Telecom Gansu) seen authenticating with the key.
    "60.165.167.",
    "118.182.244.156",
    "61.178.123.196",
)


def _command_basename(command: Any) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        parts = text.split()
    first = parts[0] if parts else text
    return os.path.basename(first).lower()


def _inline_script(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, (list, tuple)):
        return " ".join(str(item) for item in args)
    return str(args)


def _entry_text(entry: dict[str, Any]) -> str:
    """Flatten command + args + env values into one string for IOC scanning."""
    parts: list[str] = [str(entry.get("command") or "")]
    parts.append(_inline_script(entry.get("args")))
    env = entry.get("env")
    if isinstance(env, dict):
        parts.extend(str(v) for v in env.values())
    return " ".join(parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _launch_payload(entry: dict[str, Any]) -> dict[str, Any]:
    args = entry.get("args") or []
    env = entry.get("env") or {}
    return {
        "command": str(entry.get("command") or ""),
        "args": [str(arg) for arg in args] if isinstance(args, list) else args,
        "env": {str(k): str(v) for k, v in sorted(env.items())}
        if isinstance(env, dict) else env,
    }


def _portable_launch_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Canonical launch payload for package-owned portable MCP servers."""
    launch = _launch_payload(entry)
    launch["cwd"] = str(entry.get("cwd") or "")
    return launch


def _portable_tree_digest(root: Path) -> str:
    """Hash every package byte and executable bit, excluding only ``.git``.

    Portable packages must keep runtime state in ``PLUGIN_DATA``.  Symlinks
    are rejected rather than followed so the authorization cannot be made to
    cover bytes outside the installed package root.
    """
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("portable plugin root is not a directory")
    records: list[dict[str, Any]] = []

    def _walk_error(error: OSError) -> None:
        raise ValueError(f"portable plugin package cannot be fully traversed: {error}")

    for current, dirnames, filenames in os.walk(
        resolved_root,
        topdown=True,
        onerror=_walk_error,
    ):
        current_path = Path(current)
        if current_path == resolved_root:
            dirnames[:] = [name for name in dirnames if name != ".git"]
        dirnames.sort()
        filenames.sort()
        for dirname in dirnames:
            path = current_path / dirname
            if path.is_symlink() or (
                hasattr(path, "is_junction") and path.is_junction()
            ):
                raise ValueError(
                    f"portable plugin package contains a symlink or junction: {path}"
                )
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink() or (
                hasattr(path, "is_junction") and path.is_junction()
            ):
                raise ValueError(
                    f"portable plugin package contains a symlink or junction: {path}"
                )
            try:
                mode = path.stat().st_mode
            except OSError as exc:
                raise ValueError(f"portable plugin package cannot be inspected: {exc}") from exc
            if not stat.S_ISREG(mode):
                raise ValueError(f"portable plugin package contains a non-regular file: {path}")
            records.append({
                "path": path.relative_to(resolved_root).as_posix(),
                "executable": bool(mode & 0o111),
                "sha256": _sha256_file(path),
            })
    return _canonical_digest(records)


def _portable_install_record(plugin_root: Path) -> dict[str, Any]:
    """Return the exact normal Git-install record for a user plugin."""
    metadata_path = plugin_root.parent / ".install-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "portable stdio authorization requires a normal user-installed Git plugin"
        ) from exc
    record = metadata.get(plugin_root.name) if isinstance(metadata, dict) else None
    if not isinstance(record, dict):
        raise ValueError(
            "portable stdio authorization requires matching Git install metadata"
        )
    normalized = {
        "source": record.get("source"),
        "revision": record.get("revision"),
        "pinned": record.get("pinned"),
    }
    if (
        not isinstance(normalized["source"], str)
        or not normalized["source"].strip()
        or not isinstance(normalized["revision"], str)
        or re.fullmatch(r"[0-9a-f]{40}", normalized["revision"]) is None
        or not isinstance(normalized["pinned"], bool)
    ):
        raise ValueError("portable plugin Git install metadata is incomplete or invalid")
    return normalized


def _portable_receipt_key(plugin_key: str, raw_server_name: str) -> str:
    return "portable:" + _canonical_digest({
        "plugin_key": plugin_key,
        "raw_server_name": raw_server_name,
    })


def _portable_runtime_server_name(plugin_key: str, raw_server_name: str) -> str:
    from hermes_cli.plugins import _portable_skill_namespace

    return f"{_portable_skill_namespace(plugin_key)}__{raw_server_name}"


def _portable_entry_issues(entry: dict[str, Any], plugin_root: Path) -> list[str]:
    issues = _operator_entry_issues(entry)
    if issues:
        return issues
    command_path, error = _resolve_direct_executable(entry.get("command"))
    if error or command_path is None:
        return [error or "portable stdio executable cannot be resolved"]
    try:
        command_path.relative_to(plugin_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return ["portable stdio executable must be contained in the plugin package"]
    return []


def _portable_attestation(
    plugin_key: str,
    raw_server_name: str,
    plugin_root: Path,
    manifest: dict[str, Any],
    entry: dict[str, Any],
    *,
    receipt_id: str,
) -> dict[str, Any]:
    root = plugin_root.resolve(strict=True)
    issues = _portable_entry_issues(entry, root)
    if issues:
        raise ValueError("; ".join(issues))
    command_path, _ = _resolve_direct_executable(entry.get("command"))
    assert command_path is not None
    configured = dict(entry)
    configured["command"] = str(command_path)
    launch = _portable_launch_payload(configured)
    manifest_path = root / "plugin.json"
    mcp_path = root / "mcp.json"
    install_record = _portable_install_record(root)
    return {
        "schema": _ATTESTATION_SCHEMA,
        "authorization": "portable_plugin",
        "receipt_id": receipt_id,
        "receipt_key": _portable_receipt_key(plugin_key, raw_server_name),
        "plugin_key": plugin_key,
        "plugin_name": manifest.get("name"),
        "plugin_version": manifest.get("version", ""),
        "plugin_root": str(root),
        "install_record": install_record,
        "manifest_sha256": _sha256_file(manifest_path),
        "mcp_sha256": _sha256_file(mcp_path),
        "package_tree_sha256": _portable_tree_digest(root),
        "raw_server_name": raw_server_name,
        "runtime_server_name": _portable_runtime_server_name(
            plugin_key, raw_server_name
        ),
        "launch_sha256": _canonical_digest(launch),
        "content": _content_hashes(command_path, launch["args"]),
    }


def _resolve_direct_executable(command: Any) -> tuple[Path | None, str | None]:
    text = str(command or "").strip()
    if not text:
        return None, "stdio command is missing"
    if any(ch in text for ch in ("\x00", "\r", "\n")):
        return None, "stdio command contains control characters"
    if Path(text).is_absolute():
        # A translated portable ``./...`` command is already one literal path
        # token.  Do not shell-split valid absolute install paths containing
        # spaces (notably Windows user profiles and macOS folder names).
        resolved_text = text
    else:
        try:
            pieces = shlex.split(text, posix=(os.name != "nt"))
        except ValueError:
            return None, "stdio command has invalid quoting"
        if len(pieces) != 1:
            return None, "stdio command must be one direct executable path; move arguments to args"
        resolved_text = shutil.which(pieces[0])
    if not resolved_text:
        return None, f"stdio executable was not found: {text}"
    try:
        path = Path(resolved_text).expanduser().resolve(strict=True)
        mode = path.stat().st_mode
    except (OSError, RuntimeError) as exc:
        return None, f"stdio executable cannot be resolved: {exc}"
    if not stat.S_ISREG(mode) or not os.access(path, os.X_OK):
        return None, f"stdio command is not a regular executable file: {path}"
    return path, None


def _content_hashes(command_path: Path, args: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [{
        "kind": "executable",
        "configured": str(command_path),
        "resolved": str(command_path),
        "sha256": _sha256_file(command_path),
    }]
    if not isinstance(args, list):
        return records
    for index, raw in enumerate(args):
        text = str(raw)
        candidate = Path(os.path.expanduser(os.path.expandvars(text)))
        if not candidate.is_absolute() or not candidate.exists():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file():
                continue
            records.append({
                "kind": "argument_file",
                "index": index,
                "configured": text,
                "resolved": str(resolved),
                "sha256": _sha256_file(resolved),
            })
        except (OSError, RuntimeError):
            continue
    return records


def _validate_content_hashes(
    command_path: Path,
    args: Any,
    expected: Any,
) -> list[str]:
    if not isinstance(expected, list):
        return ["stdio authorization has no executable content hashes"]
    try:
        actual = _content_hashes(command_path, args)
    except OSError as exc:
        return [f"stdio executable content could not be hashed: {exc}"]
    if actual != expected:
        return ["stdio executable or file argument changed since authorization"]
    return []


def _authorization_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "mcp-authorizations.json"


def _load_operator_receipts() -> dict[str, Any]:
    path = _authorization_path()
    require_protected_control_file_capability(
        ProtectedFileOperation.READ,
        path,
        capability=ProtectedFileCapability.MCP_REGISTRATION,
    )
    if not path.exists():
        return {"schema": _RECEIPT_SCHEMA, "servers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": _RECEIPT_SCHEMA, "servers": {}}
    if not isinstance(data, dict) or data.get("schema") != _RECEIPT_SCHEMA:
        return {"schema": _RECEIPT_SCHEMA, "servers": {}}
    if not isinstance(data.get("servers"), dict):
        data["servers"] = {}
    return data


def _save_operator_receipts(data: dict[str, Any]) -> None:
    path = _authorization_path()
    require_protected_control_file_capability(
        ProtectedFileOperation.WRITE,
        path,
        capability=ProtectedFileCapability.MCP_REGISTRATION,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".mcp-authorizations-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def authorize_portable_plugin_stdio_entries(
    plugin_key: str,
    plugin_root: Path,
    data_root: Path,
) -> dict[str, Any]:
    """Issue exact receipts for one explicitly enabled portable package.

    The returned snapshot lets the CLI roll back receipts when persisting the
    enabled-plugin configuration fails.  Discovery never calls this function.
    """
    if not isinstance(plugin_key, str) or not plugin_key:
        raise ValueError("portable plugin registry key is required")
    from hermes_cli.agent_plugins import load_agent_plugin

    root = Path(plugin_root).resolve(strict=True)
    package = load_agent_plugin(root, Path(data_root))
    previous = deepcopy(_load_operator_receipts())
    updated = deepcopy(previous)
    servers = updated.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP authorization receipt store is invalid")

    # An explicit re-enable replaces every prior authority for this canonical
    # key/root, including servers removed from the new package revision.
    for key, receipt in list(servers.items()):
        if not isinstance(receipt, dict) or receipt.get("authorization") != "portable_plugin":
            continue
        if receipt.get("plugin_key") == plugin_key or receipt.get("plugin_root") == str(root):
            del servers[key]

    for raw_server_name, entry in package.mcp_servers.items():
        if "command" not in entry:
            continue
        receipt_id = uuid.uuid4().hex
        attestation = _portable_attestation(
            plugin_key,
            raw_server_name,
            root,
            dict(package.manifest),
            entry,
            receipt_id=receipt_id,
        )
        servers[attestation["receipt_key"]] = dict(attestation)
    _save_operator_receipts(updated)
    return previous


def restore_operator_receipts(snapshot: dict[str, Any]) -> None:
    """Restore a snapshot returned by portable authorization."""
    _save_operator_receipts(deepcopy(snapshot))


def revoke_portable_plugin_stdio_entries(
    *,
    plugin_key: str | None = None,
    plugin_root: Path | None = None,
) -> None:
    """Revoke every portable receipt matching a registry key or package root."""
    if not plugin_key and plugin_root is None:
        raise ValueError("portable receipt revocation requires a key or root")
    resolved_root = str(Path(plugin_root).resolve(strict=False)) if plugin_root else None
    receipts = _load_operator_receipts()
    servers = receipts.get("servers", {})
    changed = False
    for key, receipt in list(servers.items()):
        if not isinstance(receipt, dict) or receipt.get("authorization") != "portable_plugin":
            continue
        if (
            (plugin_key is not None and receipt.get("plugin_key") == plugin_key)
            or (resolved_root is not None and receipt.get("plugin_root") == resolved_root)
        ):
            del servers[key]
            changed = True
    if changed:
        _save_operator_receipts(receipts)


def attach_portable_plugin_stdio_attestation(
    plugin_key: str,
    raw_server_name: str,
    plugin_root: Path,
    manifest: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Attach an exact existing receipt during passive portable discovery.

    Missing or drifted authority deliberately returns the unattested config;
    the unchanged spawn-time validator will quarantine it.
    """
    configured = dict(entry)
    receipt_key = _portable_receipt_key(plugin_key, raw_server_name)
    try:
        receipt = _load_operator_receipts().get("servers", {}).get(receipt_key)
    except (OSError, RuntimeError, ValueError):
        # Passive discovery must not turn a receipt-read/capability failure
        # into a whole-plugin load failure.  Returning unattested keeps stdio
        # quarantined by the unchanged spawn-time validator.
        return configured
    if not isinstance(receipt, dict) or receipt.get("authorization") != "portable_plugin":
        return configured
    try:
        expected = _portable_attestation(
            plugin_key,
            raw_server_name,
            Path(plugin_root),
            manifest,
            configured,
            receipt_id=str(receipt.get("receipt_id") or ""),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return configured
    if receipt != expected:
        return configured
    configured["command"] = expected["content"][0]["resolved"]
    configured[_ATTESTATION_KEY] = expected
    return configured


def _operator_entry_issues(entry: dict[str, Any]) -> list[str]:
    configured_basename = _command_basename(entry.get("command"))
    if configured_basename.removesuffix(".exe") in _INDIRECT_EXECUTABLES:
        return [
            f"indirect stdio launcher '{configured_basename}' is not operator-authorizable; "
            "install and authorize the MCP's direct executable instead"
        ]
    command_path, error = _resolve_direct_executable(entry.get("command"))
    if error or command_path is None:
        return [error or "stdio executable cannot be resolved"]
    basename = command_path.name.casefold()
    basename_without_suffix = basename.removesuffix(".exe")
    if basename in _INDIRECT_EXECUTABLES or basename_without_suffix in _INDIRECT_EXECUTABLES:
        return [
            f"indirect stdio launcher '{basename}' is not operator-authorizable; "
            "install and authorize the MCP's direct executable instead"
        ]
    try:
        magic = command_path.read_bytes()[:4]
    except OSError as exc:
        return [f"stdio executable could not be inspected: {exc}"]
    native_magic = (
        magic.startswith(b"\x7fELF")
        or magic.startswith(b"MZ")
        or magic in {
            b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        }
    )
    if not native_magic:
        return [
            "operator-authorized stdio commands must be direct native executables; "
            "scripts and interpreter/package-runner indirection are refused"
        ]
    args = entry.get("args") or []
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return ["stdio args must be a list of strings"]
    for arg in args:
        if any(ch in arg for ch in ("\x00", "\r", "\n")):
            return ["stdio args contain control characters"]
        if _ENCODED_OR_INDIRECT_ARG.search(arg):
            return ["stdio args contain encoded or indirect executable content"]
    env = entry.get("env") or {}
    if not isinstance(env, dict):
        return ["stdio env must be a mapping"]
    for key in env:
        folded = str(key).casefold()
        if folded in _DANGEROUS_ENV_NAMES or folded.startswith(("ld_", "dyld_")):
            return [f"stdio env may not override executable-loader variable '{key}'"]
    return []


def authorize_operator_stdio_entry(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Return an operator-attested entry and persist its non-secret receipt."""
    issues = _operator_entry_issues(entry)
    if issues:
        raise ValueError("; ".join(issues))
    command_path, _ = _resolve_direct_executable(entry.get("command"))
    assert command_path is not None
    authorized = dict(entry)
    authorized["command"] = str(command_path)
    launch = _launch_payload(authorized)
    receipt_id = uuid.uuid4().hex
    attestation = {
        "schema": _ATTESTATION_SCHEMA,
        "authorization": "operator_cli",
        "receipt_id": receipt_id,
        "launch_sha256": _canonical_digest(launch),
        "content": _content_hashes(command_path, launch["args"]),
    }
    authorized[_ATTESTATION_KEY] = attestation
    receipts = _load_operator_receipts()
    receipts["servers"][name] = {
        "receipt_id": receipt_id,
        "launch_sha256": attestation["launch_sha256"],
        "content": attestation["content"],
    }
    _save_operator_receipts(receipts)
    return authorized


def revoke_operator_stdio_entry(name: str) -> None:
    receipts = _load_operator_receipts()
    if name in receipts["servers"]:
        del receipts["servers"][name]
        _save_operator_receipts(receipts)


def build_catalog_stdio_attestation(
    entry: Any,
    server_config: dict[str, Any],
    install_dir: Path | None,
) -> dict[str, Any]:
    """Bind a catalog stdio config to its shipped manifest and installed bytes."""
    command_path, error = _resolve_direct_executable(server_config.get("command"))
    if error or command_path is None:
        raise ValueError(error or "catalog stdio executable cannot be resolved")
    configured = dict(server_config)
    configured["command"] = str(command_path)
    launch = _launch_payload(configured)
    receipt_id = uuid.uuid4().hex
    attestation = {
        "schema": _ATTESTATION_SCHEMA,
        "authorization": "catalog",
        "receipt_id": receipt_id,
        "catalog_name": entry.name,
        "source": entry.source,
        "manifest_sha256": _sha256_file(Path(entry.manifest_path).resolve(strict=True)),
        "install_dir": str(install_dir.resolve()) if install_dir else None,
        "launch_sha256": _canonical_digest(launch),
        "content": _content_hashes(command_path, launch["args"]),
    }
    configured[_ATTESTATION_KEY] = attestation
    receipts = _load_operator_receipts()
    receipts["servers"][entry.name] = {
        "authorization": "catalog",
        "receipt_id": receipt_id,
        "catalog_name": entry.name,
        "manifest_sha256": attestation["manifest_sha256"],
        "launch_sha256": attestation["launch_sha256"],
        "content": attestation["content"],
    }
    _save_operator_receipts(receipts)
    return configured


def upgrade_matching_catalog_stdio_entry(
    name: str,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    """Attest an exact legacy catalog install; never upgrade custom stdio.

    This is migration-only compatibility for catalog entries installed by an
    older Hermes release. Name, current shipped manifest, default install
    location, command, args, and env must all match before a receipt is issued.
    """
    if not isinstance(entry, dict) or "command" not in entry or _ATTESTATION_KEY in entry:
        return None
    try:
        from hermes_cli.mcp_catalog import _build_server_config, get_entry
        from hermes_constants import get_hermes_home

        catalog_entry = get_entry(name)
        if catalog_entry is None or catalog_entry.transport.type != "stdio":
            return None
        install_dir = (
            get_hermes_home() / "mcp-installs" / catalog_entry.name
            if catalog_entry.install is not None else None
        )
        expected = _build_server_config(
            catalog_entry,
            install_dir,
            include_attestation=False,
        )
        if _launch_payload(entry) != _launch_payload(expected):
            return None
        attested_launch = build_catalog_stdio_attestation(
            catalog_entry,
            expected,
            install_dir,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None

    upgraded = dict(entry)
    upgraded.update(attested_launch)
    return upgraded


def _strict_stdio_issues(name: str, entry: dict[str, Any]) -> list[str]:
    attestation = entry.get(_ATTESTATION_KEY)
    if not isinstance(attestation, dict) or attestation.get("schema") != _ATTESTATION_SCHEMA:
        return [
            f"MCP server '{name}' is unpinned/provenance-free stdio software; "
            "re-add it with explicit operator authorization or install it from the catalog"
        ]
    authorization = attestation.get("authorization")
    launch = (
        _portable_launch_payload(entry)
        if authorization == "portable_plugin"
        else _launch_payload(entry)
    )
    if _canonical_digest(launch) != attestation.get("launch_sha256"):
        return [f"MCP server '{name}' command, args, or env changed after authorization"]
    command_path, error = _resolve_direct_executable(entry.get("command"))
    if error or command_path is None:
        return [f"MCP server '{name}' {error or 'executable cannot be resolved'}"]
    content_issues = _validate_content_hashes(command_path, launch["args"], attestation.get("content"))
    if content_issues:
        return [f"MCP server '{name}' {issue}" for issue in content_issues]

    if authorization == "operator_cli":
        policy_issues = _operator_entry_issues(entry)
        if policy_issues:
            return [f"MCP server '{name}' {issue}" for issue in policy_issues]
        receipt = _load_operator_receipts().get("servers", {}).get(name)
        expected = {
            "receipt_id": attestation.get("receipt_id"),
            "launch_sha256": attestation.get("launch_sha256"),
            "content": attestation.get("content"),
        }
        if receipt != expected:
            return [f"MCP server '{name}' has no matching operator authorization receipt"]
        return []

    if authorization == "catalog":
        try:
            from hermes_cli.mcp_catalog import _build_server_config, get_entry
            catalog_entry = get_entry(str(attestation.get("catalog_name") or ""))
            if catalog_entry is None:
                return [f"MCP server '{name}' references an unknown catalog entry"]
            manifest = Path(catalog_entry.manifest_path).resolve(strict=True)
            if _sha256_file(manifest) != attestation.get("manifest_sha256"):
                return [f"MCP server '{name}' catalog manifest changed after installation"]
            if catalog_entry.source != attestation.get("source"):
                return [f"MCP server '{name}' catalog provenance does not match"]
            receipt = _load_operator_receipts().get("servers", {}).get(name)
            expected_receipt = {
                "authorization": "catalog",
                "receipt_id": attestation.get("receipt_id"),
                "catalog_name": attestation.get("catalog_name"),
                "manifest_sha256": attestation.get("manifest_sha256"),
                "launch_sha256": attestation.get("launch_sha256"),
                "content": attestation.get("content"),
            }
            if receipt != expected_receipt:
                return [f"MCP server '{name}' has no matching catalog installation receipt"]
            raw_expected = _build_server_config(
                catalog_entry,
                Path(attestation["install_dir"]) if attestation.get("install_dir") else None,
                include_attestation=False,
            )
            expected_launch = _launch_payload(raw_expected)
            expected_path, expected_error = _resolve_direct_executable(expected_launch["command"])
            if expected_error or expected_path is None:
                return [f"MCP server '{name}' catalog executable cannot be resolved"]
            expected_launch["command"] = str(expected_path)
            if launch != expected_launch:
                return [f"MCP server '{name}' no longer matches its catalog manifest"]
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            return [f"MCP server '{name}' catalog authorization is invalid: {exc}"]
        return []

    if authorization == "portable_plugin":
        try:
            plugin_key = str(attestation.get("plugin_key") or "")
            raw_server_name = str(attestation.get("raw_server_name") or "")
            plugin_root = Path(str(attestation.get("plugin_root") or "")).resolve(strict=True)
            manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("plugin manifest must be an object")
            expected = _portable_attestation(
                plugin_key,
                raw_server_name,
                plugin_root,
                manifest,
                entry,
                receipt_id=str(attestation.get("receipt_id") or ""),
            )
            if name != expected.get("runtime_server_name"):
                return [f"MCP server '{name}' does not match its portable authorization identity"]
            if attestation != expected:
                return [f"MCP server '{name}' portable plugin identity or package changed"]
            receipt = _load_operator_receipts().get("servers", {}).get(
                attestation.get("receipt_key")
            )
            if receipt != expected:
                return [f"MCP server '{name}' has no matching portable plugin authorization receipt"]
        except (OSError, RuntimeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return [f"MCP server '{name}' portable plugin authorization is invalid: {exc}"]
        return []
    return [f"MCP server '{name}' has an unknown stdio authorization type"]


def validate_mcp_server_entry(
    name: str,
    entry: dict[str, Any],
    *,
    require_attestation: bool = False,
) -> list[str]:
    """Return security warnings for an MCP server entry.

    Empty return means the entry passed the requested policy level. With
    ``require_attestation=True``, stdio is a strict allowlist: a current catalog
    installation receipt, explicit direct-executable operator receipt, or an
    exact explicitly-enabled portable-package receipt is required. The legacy
    diagnostic layer also flags:

    * a known hermes-0day IOC anywhere in command/args/env (hardcoded blocklist);
    * a shell interpreter whose inline script invokes network egress (#45620);
    * a shell interpreter whose inline script writes to an OS persistence
      surface (June 2026 hermes-0day SSH/PAM/sudoers/cron shape).
    """
    if not isinstance(entry, dict):
        return [f"MCP server '{name}' configuration must be a mapping"]

    issues: list[str] = []

    # 1. Hardcoded IOC blocklist — applies regardless of command shape.
    flat = _entry_text(entry)
    for ioc in _IOC_SUBSTRINGS:
        if ioc in flat:
            issues.append(
                f"MCP server '{name}' contains a known hermes-0day "
                f"indicator-of-compromise ('{ioc}')"
            )
            # One IOC is enough to refuse; don't leak the full match list.
            return issues

    command = entry.get("command")
    basename = _command_basename(command)
    if basename in _SHELL_INTERPRETERS:
        script = _inline_script(entry.get("args"))
        if script:
            # 2. Network exfiltration shape.
            if _EGRESS_PATTERN.search(script):
                issue = (
                    f"MCP server '{name}' uses shell interpreter '{command}' with "
                    f"network egress in args"
                )
                if _EXFIL_HINT_PATTERN.search(script):
                    issue += " and exfiltration-shaped arguments"
                issues.append(issue)

            # 3. OS persistence shape (SSH key / PAM / sudoers / cron / rc files).
            if _PERSISTENCE_PATTERN.search(script):
                issues.append(
                    f"MCP server '{name}' uses shell interpreter '{command}' to write "
                    f"to an OS persistence surface (SSH keys / PAM / sudoers / cron / "
                    f"shell rc) — this is the hermes-0day backdoor shape, not a real "
                    f"MCP server"
                )

    if not issues and require_attestation and "command" in entry:
        issues.extend(_strict_stdio_issues(name, entry))

    return issues


def is_mcp_server_entry_suspicious(name: str, entry: dict[str, Any]) -> bool:
    return bool(validate_mcp_server_entry(name, entry, require_attestation=True))
