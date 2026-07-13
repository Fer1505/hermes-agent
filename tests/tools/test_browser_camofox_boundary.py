"""Camofox network-boundary and landed-URL security contracts."""

import json
from unittest.mock import MagicMock

import pytest

from tools import browser_camofox as camofox
from tools import browser_tool


@pytest.fixture(autouse=True)
def _isolated_camofox(monkeypatch):
    with camofox._sessions_lock:
        camofox._sessions.clear()
    monkeypatch.setattr(camofox, "get_vnc_url", lambda: None)
    monkeypatch.setattr(
        camofox,
        "_get",
        lambda *_args, **_kwargs: {"snapshot": "", "refsCount": 0},
    )
    yield
    with camofox._sessions_lock:
        camofox._sessions.clear()


def _session(monkeypatch, control_url: str, task_id: str = "boundary-task"):
    monkeypatch.setenv("CAMOFOX_URL", control_url)
    session = camofox._get_session(task_id)
    session["tab_id"] = "tab-boundary"
    session["main_frame_url_state"] = "known"
    session["main_frame_url"] = "https://example.com/"
    session["content_quarantined"] = False
    return session


@pytest.mark.parametrize(
    "control_url",
    [
        "http://localhost:9377",
        "http://127.0.0.1:9377",
        "http://127.99.1.2:9377",
        "http://[::1]:9377",
    ],
)
def test_only_loopback_control_authorities_are_co_resident(monkeypatch, control_url):
    monkeypatch.setenv("CAMOFOX_URL", control_url)

    boundary = camofox.get_camofox_boundary_metadata()

    assert camofox.is_camofox_co_resident() is True
    assert boundary == {
        "execution_location": "co-resident",
        "network_boundary": "local-terminal-shared",
        "control_transport": "rest",
        "control_authority": "loopback",
        "page_egress_enforced": False,
        "dns_pinned": False,
    }


@pytest.mark.parametrize(
    "control_url",
    [
        "http://camofox:9377",
        "http://host.docker.internal:9377",
        "http://192.168.1.20:9377",
        "https://browser.example.com",
        "not-a-control-url",
    ],
)
def test_docker_lan_remote_and_invalid_authorities_are_external(monkeypatch, control_url):
    monkeypatch.setenv("CAMOFOX_URL", control_url)

    boundary = camofox.get_camofox_boundary_metadata()

    assert camofox.is_camofox_co_resident() is False
    assert boundary["execution_location"] == "external"
    assert boundary["network_boundary"] == "external-uncontrolled"
    assert boundary["control_authority"] == "non-loopback"
    assert boundary["page_egress_enforced"] is False
    assert boundary["dns_pinned"] is False


def test_external_camofox_does_not_inherit_browser_local_exemption(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://camofox:9377")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(browser_tool, "_is_camofox_co_resident", lambda: False)

    assert browser_tool._is_local_backend() is False


def test_external_camofox_blocks_private_pre_navigation(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://camofox:9377")
    monkeypatch.setattr(camofox, "is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(camofox, "is_safe_url", lambda _url: False)

    def fail_post(*_args, **_kwargs):
        raise AssertionError("blocked URL must not reach Camofox")

    monkeypatch.setattr(camofox.requests, "post", fail_post)

    result = json.loads(camofox.camofox_navigate("http://10.0.0.5/admin"))

    assert result["success"] is False
    assert "private or internal" in result["error"]
    assert result["browser_boundary"]["network_boundary"] == "external-uncontrolled"


def test_external_adopted_tab_content_waits_for_verified_url(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://camofox:9377")
    session = camofox._get_session("adopted-unknown")
    session["tab_id"] = "adopted-tab"

    def fail_get(*_args, **_kwargs):
        raise AssertionError("unknown external tab content must not be read")

    monkeypatch.setattr(camofox, "_get", fail_get)

    result = json.loads(camofox.camofox_snapshot(task_id="adopted-unknown"))

    assert result["success"] is False
    assert result["final_url_state"] == "unknown"
    assert "quarantined" in result["error"]


def test_loopback_camofox_keeps_ordinary_private_navigation(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9377")
    monkeypatch.setattr(camofox, "is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(camofox, "is_safe_url", lambda _url: False)

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "tabId": "private-tab",
        "url": "http://10.0.0.5/admin",
    }
    monkeypatch.setattr(camofox.requests, "post", lambda *_args, **_kwargs: response)

    result = json.loads(camofox.camofox_navigate("http://10.0.0.5/admin"))

    assert result["success"] is True
    assert result["url"] == "http://10.0.0.5/admin"
    assert result["browser_boundary"]["execution_location"] == "co-resident"


def test_always_blocked_floor_applies_to_loopback_camofox(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setattr(camofox, "is_always_blocked_url", lambda _url: True)

    def fail_post(*_args, **_kwargs):
        raise AssertionError("metadata URL must not reach Camofox")

    monkeypatch.setattr(camofox.requests, "post", fail_post)

    result = json.loads(
        camofox.camofox_navigate("http://169.254.169.254/latest/meta-data/")
    )

    assert result["success"] is False
    assert "cloud metadata" in result["error"]


def test_explicit_private_override_is_respected_by_external_camofox(monkeypatch):
    _session(monkeypatch, "http://camofox:9377", task_id="allow-private")
    monkeypatch.setattr(camofox, "is_always_blocked_url", lambda _url: False)
    # is_safe_url returns True when the existing security/browser private-URL
    # override is explicitly enabled.
    monkeypatch.setattr(camofox, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(
        camofox,
        "_post",
        lambda *_args, **_kwargs: {"ok": True, "url": "http://10.0.0.5/admin"},
    )

    result = json.loads(
        camofox.camofox_navigate(
            "http://10.0.0.5/admin",
            task_id="allow-private",
        )
    )

    assert result["success"] is True
    assert result["url"] == "http://10.0.0.5/admin"


@pytest.mark.parametrize(
    ("action", "invoke"),
    [
        ("navigate", lambda: camofox.camofox_navigate("https://example.com", "unsafe-navigate")),
        ("click", lambda: camofox.camofox_click("@e1", "unsafe-click")),
        ("press", lambda: camofox.camofox_press("Enter", "unsafe-press")),
        ("back", lambda: camofox.camofox_back("unsafe-back")),
    ],
)
def test_unsafe_landed_url_is_quarantined_before_content_can_be_read(
    monkeypatch,
    action,
    invoke,
):
    task_id = f"unsafe-{action}"
    _session(monkeypatch, "http://camofox:9377", task_id=task_id)
    monkeypatch.setattr(camofox, "is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(
        camofox,
        "is_safe_url",
        lambda url: "10.0.0.5" not in url,
    )
    posts = []

    def fake_post(path, body, timeout=None):
        posts.append((path, body, timeout))
        if len(posts) == 1:
            return {"ok": True, "url": "http://10.0.0.5/internal"}
        return {"ok": True, "url": "about:blank"}

    monkeypatch.setattr(camofox, "_post", fake_post)

    result = json.loads(invoke())

    assert result["success"] is False
    assert "private or internal" in result["error"]
    assert result["content_quarantined"] is True
    assert result["browser_boundary"]["network_boundary"] == "external-uncontrolled"
    assert posts[-1][1]["url"] == "about:blank"

    def fail_get(*_args, **_kwargs):
        raise AssertionError("quarantined content must not be snapshotted")

    monkeypatch.setattr(camofox, "_get", fail_get)
    snapshot = json.loads(camofox.camofox_snapshot(task_id=task_id))
    assert snapshot["success"] is False
    assert "quarantined" in snapshot["error"]


@pytest.mark.parametrize(
    ("action", "invoke"),
    [
        ("navigate", lambda: camofox.camofox_navigate("https://example.com", "unknown-navigate")),
        ("click", lambda: camofox.camofox_click("@e1", "unknown-click")),
        ("press", lambda: camofox.camofox_press("Enter", "unknown-press")),
        ("back", lambda: camofox.camofox_back("unknown-back")),
    ],
)
def test_missing_final_url_is_reported_as_unknown_without_invention(
    monkeypatch,
    action,
    invoke,
):
    task_id = f"unknown-{action}"
    _session(monkeypatch, "http://localhost:9377", task_id=task_id)
    monkeypatch.setattr(camofox, "is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(camofox, "_post", lambda *_args, **_kwargs: {})

    result = json.loads(invoke())

    assert result["success"] is False
    assert result["final_url_state"] == "unknown"
    assert "final main-frame URL" in result["error"]
    assert "url" not in result
    assert result["browser_boundary"]["control_authority"] == "loopback"


def test_post_action_metadata_floor_applies_even_when_camofox_is_co_resident(monkeypatch):
    _session(monkeypatch, "http://localhost:9377", task_id="metadata-click")
    monkeypatch.setattr(
        camofox,
        "is_always_blocked_url",
        lambda url: "169.254.169.254" in url,
    )
    responses = iter([
        {"ok": True, "url": "http://169.254.169.254/latest/meta-data/"},
        {"ok": True, "url": "about:blank"},
    ])
    monkeypatch.setattr(camofox, "_post", lambda *_args, **_kwargs: next(responses))

    result = json.loads(camofox.camofox_click("@e1", "metadata-click"))

    assert result["success"] is False
    assert "cloud metadata" in result["error"]
    assert result["content_quarantined"] is True


def test_content_remains_blocked_when_blank_quarantine_fails(monkeypatch):
    _session(monkeypatch, "http://camofox:9377", task_id="failed-quarantine")
    monkeypatch.setattr(camofox, "is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(camofox, "is_safe_url", lambda _url: False)
    calls = 0

    def fake_post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": True, "url": "http://10.0.0.5/internal"}
        raise RuntimeError("blank navigation failed")

    monkeypatch.setattr(camofox, "_post", fake_post)

    result = json.loads(camofox.camofox_click("@e1", "failed-quarantine"))

    assert result["success"] is False
    assert result["content_quarantined"] is True
    assert result["quarantine_navigation_succeeded"] is False

    snapshot = json.loads(camofox.camofox_snapshot(task_id="failed-quarantine"))
    assert snapshot["success"] is False
    assert "quarantined" in snapshot["error"]


def test_quarantined_content_cannot_be_read_through_camofox_evaluate(monkeypatch):
    session = _session(monkeypatch, "http://localhost:9377", task_id="eval-quarantine")
    session["content_quarantined"] = True
    session["main_frame_url_state"] = "quarantined"

    def fail_post(*_args, **_kwargs):
        raise AssertionError("quarantined content must not reach evaluate")

    monkeypatch.setattr(camofox, "_post", fail_post)

    result = json.loads(browser_tool._camofox_eval("document.body.innerText", "eval-quarantine"))

    assert result["success"] is False
    assert "quarantined" in result["error"]


def test_external_camofox_evaluate_fails_closed_without_final_url_proof(monkeypatch):
    _session(monkeypatch, "http://camofox:9377", task_id="external-eval")

    def fail_post(*_args, **_kwargs):
        raise AssertionError("external Camofox must not receive evaluate")

    monkeypatch.setattr(camofox, "_post", fail_post)

    result = json.loads(browser_tool._camofox_eval("location.href = 'http://10.0.0.5'", "external-eval"))

    assert result["success"] is False
    assert "external Camofox boundary" in result["error"]
    assert result["browser_boundary"]["network_boundary"] == "external-uncontrolled"


def test_co_resident_camofox_evaluate_remains_available(monkeypatch):
    _session(monkeypatch, "http://localhost:9377", task_id="local-eval")
    monkeypatch.setattr(camofox, "_post", lambda *_args, **_kwargs: {"result": "42"})

    result = json.loads(browser_tool._camofox_eval("6 * 7", "local-eval"))

    assert result == {"success": True, "result": 42, "result_type": "int"}
