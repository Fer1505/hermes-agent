"""Security-boundary tests for the Discord voice diagnostic."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "discord-voice-doctor.py"


def _load_doctor():
    spec = importlib.util.spec_from_file_location("discord_voice_doctor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pynacl_security_floor_rejects_affected_and_unknown_versions():
    doctor = _load_doctor()

    for version in (None, "unknown", "1.5.0", "1.6.0", "1.6.1"):
        assert doctor._pynacl_is_security_current(version) is False


def test_pynacl_security_floor_accepts_patched_and_later_versions():
    doctor = _load_doctor()

    for version in ("1.6.2", "1.6.3", "1.7.0", "2.0.0"):
        assert doctor._pynacl_is_security_current(version) is True
