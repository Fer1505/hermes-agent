"""Unit tests for browser_cdp tool.

Uses a tiny in-process ``websockets`` server to simulate a CDP endpoint —
gives real protocol coverage (connect, send, recv, close) without needing
a real Chrome instance.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

import websockets
from websockets.asyncio.server import serve

from tools import browser_cdp_tool


def _write_raw_cdp_config(value: str) -> Path:
    """Write one raw-CDP config value into the isolated test HERMES_HOME."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.yaml"
    config_path.write_text(
        f"browser:\n  allow_raw_cdp: {value}\n",
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# In-process CDP mock server
# ---------------------------------------------------------------------------


class _CDPServer:
    """A tiny CDP-over-WebSocket mock.

    Each client gets a greeting-free stream.  The server replies to each
    inbound request whose ``id`` is set, using the registered handler for
    that method.  If no handler is registered, returns a generic CDP error.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, Any] = {}
        self._responses: List[Dict[str, Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._host = "127.0.0.1"
        self._port = 0

    # --- handler registration --------------------------------------------

    def on(self, method: str, handler):
        """Register a handler ``handler(params, session_id) -> dict or Exception``."""
        self._handlers[method] = handler

    # --- lifecycle -------------------------------------------------------

    def start(self) -> str:
        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _handler(ws):
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        call_id = msg.get("id")
                        method = msg.get("method", "")
                        params = msg.get("params", {}) or {}
                        session_id = msg.get("sessionId")
                        self._responses.append(msg)

                        fn = self._handlers.get(method)
                        if fn is None:
                            reply = {
                                "id": call_id,
                                "error": {
                                    "code": -32601,
                                    "message": f"No handler for {method}",
                                },
                            }
                        else:
                            try:
                                result = fn(params, session_id)
                                if isinstance(result, Exception):
                                    raise result
                                reply = {"id": call_id, "result": result}
                            except Exception as exc:
                                reply = {
                                    "id": call_id,
                                    "error": {"code": -1, "message": str(exc)},
                                }
                        if session_id:
                            reply["sessionId"] = session_id
                        await ws.send(json.dumps(reply))
                except websockets.exceptions.ConnectionClosed:
                    pass

            async def _serve() -> None:
                self._server = await serve(_handler, self._host, 0)
                sock = next(iter(self._server.sockets))
                self._port = sock.getsockname()[1]
                ready.set()
                await self._server.wait_closed()

            try:
                self._loop.run_until_complete(_serve())
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not ready.wait(timeout=5.0):
            raise RuntimeError("CDP mock server failed to start within 5s")
        return f"ws://{self._host}:{self._port}/devtools/browser/mock"

    def stop(self) -> None:
        if self._loop and self._server:
            def _close() -> None:
                self._server.close()

            self._loop.call_soon_threadsafe(_close)
        if self._thread:
            self._thread.join(timeout=3.0)

    def received(self) -> List[Dict[str, Any]]:
        return list(self._responses)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cdp_server(monkeypatch):
    """Start a CDP mock and route tool resolution to it."""
    server = _CDPServer()
    ws_url = server.start()
    monkeypatch.setattr(
        browser_cdp_tool, "_resolve_cdp_endpoint", lambda: ws_url
    )
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_method_returns_error():
    result = json.loads(browser_cdp_tool.browser_cdp(method=""))
    assert "error" in result
    assert "method" in result["error"].lower()
    assert result.get("cdp_docs") == browser_cdp_tool.CDP_DOCS_URL


def test_non_string_method_returns_error():
    result = json.loads(browser_cdp_tool.browser_cdp(method=123))  # type: ignore[arg-type]
    assert "error" in result
    assert "method" in result["error"].lower()


def test_non_dict_params_returns_error(monkeypatch):
    monkeypatch.setattr(
        browser_cdp_tool, "_resolve_cdp_endpoint", lambda: "ws://localhost:9999"
    )
    result = json.loads(
        browser_cdp_tool.browser_cdp(method="Target.getTargets", params="not-a-dict")  # type: ignore[arg-type]
    )
    assert "error" in result
    assert "object" in result["error"].lower() or "dict" in result["error"].lower()


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


def test_no_endpoint_returns_helpful_error(monkeypatch):
    monkeypatch.setattr(browser_cdp_tool, "_resolve_cdp_endpoint", lambda: "")
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets"))
    assert "error" in result
    assert "/browser connect" in result["error"]
    assert result.get("cdp_docs") == browser_cdp_tool.CDP_DOCS_URL


def test_non_ws_endpoint_returns_error(monkeypatch):
    monkeypatch.setattr(
        browser_cdp_tool, "_resolve_cdp_endpoint", lambda: "http://localhost:9222"
    )
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets"))
    assert "error" in result
    assert "WebSocket" in result["error"]


def test_websockets_missing_returns_error(monkeypatch):
    monkeypatch.setattr(browser_cdp_tool, "_WS_AVAILABLE", False)
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets"))
    assert "error" in result
    assert "websockets" in result["error"].lower()


# ---------------------------------------------------------------------------
# Happy-path: browser-level call
# ---------------------------------------------------------------------------


def test_browser_level_success(cdp_server):
    cdp_server.on(
        "Target.getTargets",
        lambda params, sid: {
            "targetInfos": [
                {"targetId": "A", "type": "page", "title": "Tab 1", "url": "about:blank"},
                {"targetId": "B", "type": "page", "title": "Tab 2", "url": "https://a.test"},
            ]
        },
    )
    result = json.loads(browser_cdp_tool.browser_cdp(method="Target.getTargets"))
    assert result["success"] is True
    assert result["method"] == "Target.getTargets"
    assert "target_id" not in result
    assert len(result["result"]["targetInfos"]) == 2
    # Verify the server actually received exactly one call (no extra traffic)
    calls = cdp_server.received()
    assert len(calls) == 1
    assert calls[0]["method"] == "Target.getTargets"
    assert "sessionId" not in calls[0]


def test_browser_level_redacts_secret_result(cdp_server):
    fake_key = "sk-" + "CDPSECRETRESULT1234567890"
    cdp_server.on(
        "Runtime.evaluate",
        lambda params, sid: {"result": {"type": "string", "value": fake_key}},
    )

    result = json.loads(browser_cdp_tool.browser_cdp(method="Runtime.evaluate"))

    assert result["success"] is True
    serialized = json.dumps(result)
    assert "CDPSECRETRESULT" not in serialized
    assert result["result"]["result"]["value"].startswith("sk-")


def test_stateless_result_redacts_opaque_credentials_and_cookie_values(cdp_server):
    cdp_server.on(
        "Network.getAllCookies",
        lambda params, sid: {
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "opaque-cookie-value-without-known-prefix",
                    "domain": "example.test",
                }
            ],
            "headers": {
                "Authorization": "Bearer opaque-auth-value",
                "Cookie": "sessionid=opaque-cookie-value",
                "X-Public": "visible",
            },
            "accessToken": "opaque-access-value",
        },
    )

    result = json.loads(
        browser_cdp_tool.browser_cdp(method="Network.getAllCookies")
    )["result"]

    assert result["cookies"][0] == {
        "name": "sessionid",
        "value": "«redacted-secret»",
        "domain": "example.test",
    }
    assert result["headers"]["Authorization"] == "«redacted-secret»"
    assert result["headers"]["Cookie"] == "«redacted-secret»"
    assert result["headers"]["X-Public"] == "visible"
    assert result["accessToken"] == "«redacted-secret»"


def test_supervisor_frame_result_uses_same_recursive_redaction(monkeypatch):
    from tools import browser_supervisor
    from agent import async_utils

    class _Snapshot:
        frame_tree = {
            "top": None,
            "children": [
                {
                    "frame_id": "frame-secret",
                    "session_id": "session-secret",
                }
            ],
        }

    class _Loop:
        @staticmethod
        def is_running():
            return True

    class _Supervisor:
        _loop = _Loop()

        @staticmethod
        def snapshot():
            return _Snapshot()

        @staticmethod
        async def _cdp(method, params, *, session_id, timeout):
            return {
                "result": {
                    "cookies": [
                        {
                            "name": "sessionid",
                            "value": "opaque-frame-cookie",
                            "domain": "frame.test",
                        }
                    ],
                    "refresh_token": "opaque-frame-token",
                    "public": "visible",
                }
            }

    class _Completed:
        def __init__(self, coro):
            self._coro = coro

        def result(self, timeout=None):
            return asyncio.run(self._coro)

    monkeypatch.setattr(
        browser_supervisor.SUPERVISOR_REGISTRY,
        "get",
        lambda task_id: _Supervisor(),
    )
    monkeypatch.setattr(
        async_utils,
        "safe_schedule_threadsafe",
        lambda coro, loop: _Completed(coro),
    )

    result = json.loads(
        browser_cdp_tool.browser_cdp(
            method="Network.getAllCookies",
            frame_id="frame-secret",
            task_id="task-secret",
        )
    )["result"]

    assert result["cookies"][0] == {
        "name": "sessionid",
        "value": "«redacted-secret»",
        "domain": "frame.test",
    }
    assert result["refresh_token"] == "«redacted-secret»"
    assert result["public"] == "visible"


def test_empty_params_sends_empty_object(cdp_server):
    cdp_server.on("Browser.getVersion", lambda params, sid: {"product": "Mock/1.0"})
    json.loads(browser_cdp_tool.browser_cdp(method="Browser.getVersion"))
    assert cdp_server.received()[0]["params"] == {}


# ---------------------------------------------------------------------------
# Happy-path: target-attached call
# ---------------------------------------------------------------------------


def test_target_attach_then_call(cdp_server):
    cdp_server.on(
        "Target.attachToTarget",
        lambda params, sid: {"sessionId": f"sess-{params['targetId']}"},
    )
    cdp_server.on(
        "Runtime.evaluate",
        lambda params, sid: {
            "result": {"type": "string", "value": f"evaluated[{sid}]"},
        },
    )
    result = json.loads(
        browser_cdp_tool.browser_cdp(
            method="Runtime.evaluate",
            params={"expression": "document.title", "returnByValue": True},
            target_id="tab-A",
        )
    )
    assert result["success"] is True
    assert result["target_id"] == "tab-A"
    assert result["result"]["result"]["value"] == "evaluated[sess-tab-A]"

    calls = cdp_server.received()
    # First call: attach
    assert calls[0]["method"] == "Target.attachToTarget"
    assert calls[0]["params"] == {"targetId": "tab-A", "flatten": True}
    # Second call: dispatched method on the session
    assert calls[1]["method"] == "Runtime.evaluate"
    assert calls[1]["sessionId"] == "sess-tab-A"


# ---------------------------------------------------------------------------
# CDP error responses
# ---------------------------------------------------------------------------


def test_cdp_method_error_returns_tool_error(cdp_server):
    # No handler registered -> server returns CDP error
    result = json.loads(
        browser_cdp_tool.browser_cdp(method="NonExistent.method")
    )
    assert "error" in result
    assert "CDP error" in result["error"]
    assert result.get("method") == "NonExistent.method"


def test_attach_failure_returns_tool_error(cdp_server):
    # Target.attachToTarget has no handler -> server errors on attach
    result = json.loads(
        browser_cdp_tool.browser_cdp(
            method="Runtime.evaluate",
            params={"expression": "1+1"},
            target_id="missing",
        )
    )
    assert "error" in result
    assert "Target.attachToTarget" in result["error"]


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


def test_timeout_when_server_never_replies(cdp_server):
    # Register a handler that blocks forever
    def slow(params, sid):
        time.sleep(10)
        return {}

    cdp_server.on("Page.slowMethod", slow)
    result = json.loads(
        browser_cdp_tool.browser_cdp(
            method="Page.slowMethod", timeout=0.5
        )
    )
    assert "error" in result
    assert "tim" in result["error"].lower()


# ---------------------------------------------------------------------------
# Timeout clamping
# ---------------------------------------------------------------------------


def test_timeout_clamped_above_max(cdp_server):
    cdp_server.on("Browser.getVersion", lambda p, s: {"product": "ok"})
    # timeout=10_000 should be clamped to 300 but still succeed
    result = json.loads(
        browser_cdp_tool.browser_cdp(method="Browser.getVersion", timeout=10_000)
    )
    assert result["success"] is True


def test_invalid_timeout_falls_back_to_default(cdp_server):
    cdp_server.on("Browser.getVersion", lambda p, s: {"product": "ok"})
    result = json.loads(
        browser_cdp_tool.browser_cdp(method="Browser.getVersion", timeout="nope")  # type: ignore[arg-type]
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registered_in_browser_toolset():
    from tools.registry import registry

    entry = registry.get_entry("browser_cdp")
    assert entry is not None
    # browser_cdp lives in its own toolset so its stricter check_fn
    # (requires reachable CDP endpoint) doesn't gate the whole browser
    # toolset — see commit 96b0f3700.
    assert entry.toolset == "browser-cdp"
    assert entry.schema["name"] == "browser_cdp"
    assert entry.schema["parameters"]["required"] == ["method"]
    assert "Chrome DevTools Protocol" in entry.schema["description"]
    assert browser_cdp_tool.CDP_DOCS_URL in entry.schema["description"]


def test_dispatch_through_registry(cdp_server):
    from tools.registry import registry

    cdp_server.on("Target.getTargets", lambda p, s: {"targetInfos": []})
    raw = registry.dispatch(
        "browser_cdp", {"method": "Target.getTargets"}, task_id="t1"
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["method"] == "Target.getTargets"


# ---------------------------------------------------------------------------
# check_fn gating
# ---------------------------------------------------------------------------


def test_default_config_keeps_raw_cdp_model_tool_disabled():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["browser"]["allow_raw_cdp"] is False


def test_check_fn_false_when_no_cdp_url(monkeypatch):
    """Gate closes when no CDP URL is set — even if the browser toolset is
    otherwise configured."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "check_browser_requirements", lambda: True)
    monkeypatch.setattr(bt, "_get_cdp_override", lambda: "")
    _write_raw_cdp_config("true")
    assert browser_cdp_tool._browser_cdp_check() is False


def test_check_fn_false_without_explicit_raw_cdp_opt_in(monkeypatch):
    """A reachable endpoint alone must not expose unrestricted raw CDP."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "check_browser_requirements", lambda: True)
    monkeypatch.setattr(
        bt, "_get_cdp_override", lambda: "ws://localhost:9222/devtools/browser/x"
    )
    _write_raw_cdp_config("false")
    assert browser_cdp_tool._browser_cdp_check() is False


def test_check_fn_quoted_false_stays_false(monkeypatch):
    """Strict bool normalization must not treat quoted 'false' as truthy."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "check_browser_requirements", lambda: True)
    monkeypatch.setattr(
        bt, "_get_cdp_override", lambda: "ws://localhost:9222/devtools/browser/x"
    )
    _write_raw_cdp_config('"false"')
    assert browser_cdp_tool._browser_cdp_check() is False


def test_check_fn_true_when_opted_in_and_cdp_url_set(monkeypatch):
    """Gate opens only when config opt-in and endpoint requirements both hold."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "check_browser_requirements", lambda: True)
    monkeypatch.setattr(
        bt, "_get_cdp_override", lambda: "ws://localhost:9222/devtools/browser/x"
    )
    _write_raw_cdp_config("true")
    assert browser_cdp_tool._browser_cdp_check() is True


def test_check_fn_false_when_browser_requirements_fail(monkeypatch):
    """Even with a CDP URL, gate closes if the overall browser toolset is
    unavailable (e.g. agent-browser not installed)."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "check_browser_requirements", lambda: False)
    monkeypatch.setattr(
        bt, "_get_cdp_override", lambda: "ws://localhost:9222/devtools/browser/x"
    )
    _write_raw_cdp_config("true")
    assert browser_cdp_tool._browser_cdp_check() is False


def test_registry_exposure_tracks_real_config_opt_in(monkeypatch):
    """Real registered entry is filtered until raw CDP is explicitly enabled."""
    import tools.browser_tool as bt
    from tools import browser_dialog_tool
    from tools.registry import invalidate_check_fn_cache, registry

    monkeypatch.setattr(bt, "check_browser_requirements", lambda: True)
    monkeypatch.setattr(
        bt, "_get_cdp_override", lambda: "ws://localhost:9222/devtools/browser/x"
    )

    _write_raw_cdp_config('"false"')
    invalidate_check_fn_cache()
    definitions = registry.get_definitions({"browser_cdp", "browser_dialog"})
    assert [item["function"]["name"] for item in definitions] == ["browser_dialog"]
    assert browser_dialog_tool._browser_dialog_check() is True
    assert browser_cdp_tool._browser_cdp_check() is False

    _write_raw_cdp_config("true")
    invalidate_check_fn_cache()
    definitions = registry.get_definitions({"browser_cdp", "browser_dialog"})
    assert [item["function"]["name"] for item in definitions] == [
        "browser_cdp",
        "browser_dialog",
    ]
