"""Fail-fast validation for the canonical Hermes test environment.

This runs before bytecode compilation or pytest collection. Keep it free of
project imports so an incomplete environment produces one actionable report.
"""

from __future__ import annotations

import importlib
import os
import sys


REQUIRED_IMPORTS = (
    ("pytest", "dev", "pytest"),
    ("pytest_asyncio", "dev", "pytest-asyncio"),
    ("requests", "core", "requests"),
    ("psutil", "core", "psutil"),
    ("mcp", "dev", "mcp"),
    ("telegram", "messaging", "python-telegram-bot[webhooks]"),
    ("aiohttp", "messaging", "aiohttp"),
    ("httpx", "core", "httpx[socks]"),
    ("aiosqlite", "matrix", "aiosqlite"),
    ("defusedxml", "wecom", "defusedxml"),
)

FOCUSED_EXCLUDED_IMPORTS = frozenset({"mcp", "aiosqlite", "defusedxml"})


def selected_requirements(scope: str) -> tuple[tuple[str, str, str], ...]:
    if scope == "canonical":
        return REQUIRED_IMPORTS
    if scope == "focused":
        return tuple(
            requirement
            for requirement in REQUIRED_IMPORTS
            if requirement[0] not in FOCUSED_EXCLUDED_IMPORTS
        )
    raise ValueError(
        "HERMES_TEST_PREFLIGHT_SCOPE must be 'canonical' or explicit 'focused'"
    )


def missing_requirements(scope: str = "canonical") -> list[tuple[str, str, str, str]]:
    missing = []
    for module, extra, distribution in selected_requirements(scope):
        try:
            importlib.import_module(module)
        except Exception as exc:
            missing.append((module, extra, distribution, type(exc).__name__))
    return missing


def main() -> int:
    scope = os.environ.get("HERMES_TEST_PREFLIGHT_SCOPE", "canonical").strip().lower()
    try:
        missing = missing_requirements(scope)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not missing:
        if scope == "focused":
            print(
                "notice: focused Hermes preflight excludes mcp, aiosqlite, "
                "and defusedxml; "
                "this is partial functional evidence, not canonical suite parity.",
                file=sys.stderr,
            )
        return 0
    print(
        "error: Hermes test environment preflight failed before pytest collection.",
        file=sys.stderr,
    )
    print(f"       Python: {sys.executable} (scope={scope})", file=sys.stderr)
    for module, extra, distribution, failure in missing:
        print(
            f"       - import {module}: {failure} "
            f"(declared by [{extra}] as {distribution})",
            file=sys.stderr,
        )
    extras = sorted({extra for _module, extra, _distribution, _failure in missing})
    print(
        "       Use the repository's dependency-complete development environment "
        f"with extras: {','.join(extras)}. Do not continue with partial collection.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
