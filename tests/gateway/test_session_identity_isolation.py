"""Direct tool readers must obey the same identity boundary as subprocesses."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context

import pytest

import gateway.session_context as sc


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    tokens = [var.set(sc._UNSET) for var in sc._VAR_MAP.values()]
    async_token = sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)
    bound_token = sc._session_context_bound.set(False)
    monkeypatch.setattr(sc, "_session_context_engaged", False)
    for name in sc._VAR_MAP:
        monkeypatch.setenv(name, "foreign-" + name)
    yield
    for var, token in zip(sc._VAR_MAP.values(), tokens):
        var.reset(token)
    sc._SESSION_ASYNC_DELIVERY.reset(async_token)
    sc._session_context_bound.reset(bound_token)


@pytest.mark.parametrize("name", list(sc._VAR_MAP))
def test_unbound_direct_reader_agrees_with_subprocess_boundary(name):
    from tools.environments.local import _make_run_env

    sc.set_session_vars(session_id="owner")

    def unbound_view():
        assert name not in _make_run_env({})
        assert sc.get_session_env(name, "unavailable") == "unavailable"

    Context().run(unbound_view)


def test_env_only_cli_compatibility_before_host_binding():
    for name in sc._VAR_MAP:
        assert sc.get_session_env(name) == "foreign-" + name


def test_reset_does_not_reauthorize_old_global_identity():
    sc.set_session_vars(session_id="owner", cron_session="1")
    sc.reset_session_vars()
    assert sc.get_session_env("HERMES_SESSION_ID") == ""
    assert sc.get_session_env("HERMES_CRON_SESSION") == ""
    sc.set_session_vars(session_id="new-owner", cron_session="")
    assert sc.get_session_env("HERMES_SESSION_ID") == "new-owner"
    assert sc.get_session_env("HERMES_CRON_SESSION", "fallback") == ""


def test_thread_requires_propagated_context():
    sc.set_session_vars(session_id="owner")
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(sc.get_session_env, "HERMES_SESSION_ID").result() == ""
        assert pool.submit(copy_context().run, sc.get_session_env, "HERMES_SESSION_ID").result() == "owner"


def test_actual_worker_wrapper_preserves_approval_scope(monkeypatch):
    from tools import approval
    from tools.thread_context import propagate_context_to_thread

    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "approve")
    sc.set_session_vars(session_id="owner", cron_session="1")

    def check():
        return approval.check_execute_code_guard("pass", "local")

    with ThreadPoolExecutor(max_workers=1) as pool:
        unbound = pool.submit(check).result()
        assert unbound["approved"] is False
        assert unbound["reason"] == "session_context_missing"
        assert pool.submit(propagate_context_to_thread(check)).result()["approved"] is True
        # Reusing the worker after the wrapper must not retain its permission.
        assert pool.submit(check).result()["reason"] == "session_context_missing"


def test_concurrent_tasks_keep_their_own_identity_and_reset_window_empty():
    async def run():
        ready = asyncio.Event()
        count = 0

        async def turn(identity):
            nonlocal count
            sc.reset_session_vars()
            before = sc.get_session_env("HERMES_SESSION_ID")
            sc.set_session_vars(session_id=identity)
            count += 1
            if count == 2:
                ready.set()
            await ready.wait()
            return before, sc.get_session_env("HERMES_SESSION_ID")

        sc.set_session_vars(session_id="parent")
        return await asyncio.gather(turn("one"), turn("two"))

    assert asyncio.run(run()) == [("", "one"), ("", "two")]


def test_unbound_delegation_does_not_capture_foreign_routing_origin():
    from tools.async_delegation import _capture_routing_origin

    sc.set_session_vars(scope_id="own-scope", user_id="own-user", user_name="own-name")
    assert _capture_routing_origin() == {
        "scope_id": "own-scope", "user_id": "own-user", "user_name": "own-name",
    }
    assert Context().run(_capture_routing_origin) == {}


def test_unbound_send_does_not_claim_another_cron_delivery_target():
    from tools.send_message_tool import _get_cron_auto_delivery_target

    sc.set_session_vars(session_id="owner")
    assert Context().run(_get_cron_auto_delivery_target) is None


@pytest.mark.parametrize("check_availability", [True, False])
def test_unbound_browser_cannot_select_or_dispatch_foreign_controller(monkeypatch, check_availability):
    from gateway import browser_control_broker as control
    from tools.browser_extension_router import (
        extension_controller_available, routed_browser_handler,
    )

    broker = control.BrowserControlBroker(command_timeout=1)
    scope = control.ControllerScope(
        principal_id="owner-principal", profile_id="default",
        session_id="owner-session", controller_id="fixture-controller",
        browser_profile_id="fixture-browser", transport_family="local-api",
        capabilities=frozenset({"controller.noop"}),
    )
    frames = []

    def complete(frame):
        frames.append(frame)
        broker.complete(frame["params"]["command_id"], ok=True, result={"ok": True})

    broker.attach(scope, complete)
    monkeypatch.setattr(control, "browser_control_enabled", lambda: True)
    monkeypatch.setattr(control, "get_browser_control_broker", lambda: broker)
    monkeypatch.setenv("HERMES_SESSION_ID", scope.session_id)
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_PRINCIPAL", scope.principal_id)
    monkeypatch.setenv("HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY", scope.transport_family)
    sc.set_session_vars(
        session_id=scope.session_id, browser_control_principal=scope.principal_id,
        browser_control_transport_family=scope.transport_family,
    )
    assert extension_controller_available("controller.noop") is True
    if check_availability:
        assert not Context().run(extension_controller_available, "controller.noop")
    result = Context().run(
        routed_browser_handler, "controller.noop", {}, fallback=lambda: "legacy",
    )
    assert result == "legacy"
    assert frames == []
    assert json.loads(routed_browser_handler("controller.noop", {}, fallback=lambda: "legacy")) == {"ok": True}
    assert len(frames) == 1


def test_non_session_environment_lookup_is_unchanged(monkeypatch):
    sc.set_session_vars(session_id="owner")
    monkeypatch.setenv("GCR_SYNTHETIC_NON_SESSION", "value")
    assert Context().run(sc.get_session_env, "GCR_SYNTHETIC_NON_SESSION") == "value"


@pytest.mark.parametrize("state", ["fresh", "reset", "cleared"])
@pytest.mark.parametrize("surface", ["dangerous", "combined", "code", "plugin"])
def test_missing_host_context_never_becomes_headless_autoapproval(monkeypatch, state, surface):
    from tools import approval

    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    for name in ("HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE", "HERMES_EXEC_ASK", "HERMES_SINGLE_QUERY_SESSION"):
        monkeypatch.delenv(name, raising=False)
    calls = {
        "dangerous": lambda: approval.check_dangerous_command("rm -rf /tmp/synthetic", "local"),
        "combined": lambda: approval.check_all_command_guards("rm -rf /tmp/synthetic", "local"),
        "code": lambda: approval.check_execute_code_guard("import os", "local"),
        "plugin": lambda: approval.request_tool_approval("fixture", "fixture approval"),
    }
    tokens = sc.set_session_vars(platform="telegram", session_id="owner")

    def check():
        if state == "reset":
            sc.reset_session_vars()
        elif state == "cleared":
            sc.clear_session_vars(tokens)
        result = calls[surface]()
        assert result["approved"] is False
        assert result["outcome"] == "blocked"
        assert result["reason"] == "session_context_missing"
        assert result.get("user_consent") is False

    if state == "fresh":
        Context().run(check)
    else:
        check()
