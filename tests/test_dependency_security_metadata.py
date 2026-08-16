"""Dependency metadata invariants for reviewed security advisories."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from tools.lazy_deps import LAZY_DEPS


REPO_ROOT = Path(__file__).resolve().parents[1]

# Security floors remain valid when a later patched version is selected. These
# are intentionally not exact-version snapshots.
ADVISORY_FLOORS = {
    "aiohttp": ((3, 14, 3), "GHSA-cq5v-8q36-5273"),
    "cryptography": ((50, 0, 0), "GHSA-g6cj-pr64-35w5"),
    "pynacl": ((1, 6, 2), "GHSA-mrfv-m5wm-5w6w"),
}

REQUIRED_LAZY_PINS = {
    "aiohttp": {
        "platform.discord",
        "platform.slack",
        "platform.matrix",
        "platform.teams",
    },
    "pynacl": {"platform.discord"},
}

_EXACT_PIN = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([0-9]+(?:\.[0-9]+)*)\s*$"
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _exact_pins(specs: list[str] | tuple[str, ...]) -> dict[str, set[str]]:
    pins: dict[str, set[str]] = {}
    for spec in specs:
        requirement = spec.split(";", 1)[0].strip()
        match = _EXACT_PIN.match(requirement)
        if match:
            pins.setdefault(_canonical(match.group(1)), set()).add(match.group(2))
    return pins


def _all_project_specs(data: dict) -> list[str]:
    specs = list(data["project"]["dependencies"])
    for extra_specs in data["project"]["optional-dependencies"].values():
        specs.extend(extra_specs)
    return specs


def _version_tuple(version: str) -> tuple[int, ...]:
    assert re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version), (
        f"security-pinned version must be a stable numeric release: {version!r}"
    )
    return tuple(int(part) for part in version.split("."))


def test_reviewed_advisory_floors_hold_in_project_and_lockfile():
    """Direct declarations and the universal lock must exclude affected ranges."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_pins = _exact_pins(_all_project_specs(project))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {
        _canonical(package["name"]): package["version"]
        for package in lock["package"]
    }

    problems = []
    for package, (floor, advisory) in ADVISORY_FLOORS.items():
        declared = project_pins.get(package)
        if not declared:
            problems.append(f"{package}: missing direct exact pin ({advisory})")
        else:
            for version in sorted(declared):
                if _version_tuple(version) < floor:
                    problems.append(
                        f"{package}: declared {version} below "
                        f"{'.'.join(map(str, floor))} ({advisory})"
                    )

        locked_version = locked.get(package)
        if locked_version is None:
            problems.append(f"{package}: missing from uv.lock ({advisory})")
        elif _version_tuple(locked_version) < floor:
            problems.append(
                f"{package}: locked {locked_version} below "
                f"{'.'.join(map(str, floor))} ({advisory})"
            )

    assert not problems, "known-vulnerable dependency resolution:\n  " + "\n  ".join(problems)


def test_security_pins_cover_every_on_demand_install_path():
    """Lazy installers must preserve the same patched pins as package extras."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_pins = _exact_pins(_all_project_specs(project))

    problems = []
    for package, features in REQUIRED_LAZY_PINS.items():
        expected = project_pins.get(package)
        assert expected and len(expected) == 1, (
            f"{package}: expected one exact project pin, got {expected}"
        )
        for feature in sorted(features):
            assert feature in LAZY_DEPS, f"required lazy feature missing: {feature}"
            actual = _exact_pins(LAZY_DEPS[feature]).get(package)
            if actual != expected:
                problems.append(
                    f"{feature}: {package}={sorted(actual) if actual else 'MISSING'}, "
                    f"expected {sorted(expected)}"
                )

    assert not problems, "security pin drift on lazy install paths:\n  " + "\n  ".join(problems)


def test_discord_voice_stack_is_preserved_without_vulnerable_extra_cap():
    """Voice remains available while released discord.py still caps PyNaCl<1.6."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    eager_specs = project["project"]["optional-dependencies"]["messaging"]
    lazy_specs = list(LAZY_DEPS["platform.discord"])

    for label, specs in (("messaging extra", eager_specs), ("Discord lazy path", lazy_specs)):
        pins = _exact_pins(specs)
        for package in ("discord-py", "pynacl", "davey"):
            assert package in pins, f"{label} must exact-pin {package} for voice support"
        assert not any(
            re.match(r"^\s*discord[._-]py\[voice\]", spec, re.IGNORECASE)
            for spec in specs
        ), f"{label} reintroduced discord.py[voice]'s vulnerable PyNaCl<1.6 cap"
