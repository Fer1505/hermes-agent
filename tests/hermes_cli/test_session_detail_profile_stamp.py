"""GET /api/sessions/{id} must stamp the row's OWN profile.

Clients echo the detail stamp back as ``?profile=`` on follow-up reads
(messages, resume). Stamping from the request scope instead of the row's
``profile_name`` routed those follow-ups to the wrong store — the 2026-08-20
Desktop "Couldn't load this session" strand (detail 200 → messages 404).
"""

import pytest

import hermes_cli.web_server as web_server
from hermes_cli.web_routers.sessions import get_session_detail


class _FakeDB:
    def __init__(self, row):
        self._row = row

    def resolve_session_id(self, sid):
        return self._row["id"] if sid == self._row["id"] else None

    def get_session(self, sid):
        return dict(self._row) if sid == self._row["id"] else None

    def close(self):
        pass


def _wire(monkeypatch, row):
    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda profile, *, read_only: _FakeDB(row),
    )
    monkeypatch.setattr(web_server, "_cron_default_profile", lambda: "default")
    monkeypatch.setattr(
        web_server, "_cron_profile_home", lambda profile: (profile, "/unused")
    )


@pytest.mark.asyncio
async def test_detail_prefers_row_profile_name(monkeypatch):
    _wire(monkeypatch, {"id": "s1", "profile_name": "olympus-hermes"})
    out = await get_session_detail("s1", profile=None)
    assert out["profile"] == "olympus-hermes"
    assert out["is_default_profile"] is False


@pytest.mark.asyncio
async def test_detail_row_stamp_wins_over_request_scope(monkeypatch):
    # A scoped request still reports the row's owner, not the scope it was
    # reached through — the stamp is an ownership claim, not an echo.
    _wire(monkeypatch, {"id": "s1", "profile_name": "athena"})
    out = await get_session_detail("s1", profile="default")
    assert out["profile"] == "athena"


@pytest.mark.asyncio
async def test_detail_unstamped_row_falls_back_to_serving_profile(monkeypatch):
    _wire(monkeypatch, {"id": "s1", "profile_name": None})
    out = await get_session_detail("s1", profile=None)
    assert out["profile"] == "default"
    assert out["is_default_profile"] is True
