"""Cloud browser fail-closed and explicit local-fallback contracts."""

import logging

import pytest

from agent.browser_provider import REMOTE_PROVIDER_EGRESS
import tools.browser_tool as browser_tool


def _reset_session_state(monkeypatch):
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task: None)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)


def _valid_cloud_session(**overrides):
    session = {
        "session_name": "cloud-session",
        "bb_session_id": "provider-session-1",
        "cdp_url": "wss://browser.example/devtools/browser/abc",
        "features": {"cloud": True},
    }
    session.update(overrides)
    return session


class FakeCloudProvider:
    egress_capability = REMOTE_PROVIDER_EGRESS

    def __init__(self, result=None, error=None):
        self.result = _valid_cloud_session() if result is None else result
        self.error = error
        self.closed = []

    def create_session(self, task_id):
        if self.error is not None:
            raise self.error
        return self.result

    def close_session(self, session_id):
        self.closed.append(session_id)
        return True


class TestCloudProviderRuntimeFallback:
    def test_cloud_failure_fails_closed_by_default(self, monkeypatch):
        _reset_session_state(monkeypatch)
        provider = FakeCloudProvider(error=RuntimeError("401 Unauthorized"))
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        local = lambda task: pytest.fail("local browser must not be selected")
        monkeypatch.setattr(browser_tool, "_create_local_session", local)
        monkeypatch.setattr(
            browser_tool, "_allow_local_fallback_on_cloud_failure", lambda: False
        )

        with pytest.raises(RuntimeError, match="fallback is disabled by default"):
            browser_tool._get_session_info("task-1")

    def test_explicit_opt_in_allows_marked_local_fallback(self, monkeypatch):
        _reset_session_state(monkeypatch)
        provider = FakeCloudProvider(error=RuntimeError("cloud unavailable"))
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(
            browser_tool, "_allow_local_fallback_on_cloud_failure", lambda: True
        )

        session = browser_tool._get_session_info("task-2")

        assert session["features"]["local"] is True
        assert session["fallback_from_cloud"] is True
        assert session["fallback_provider"] == "FakeCloudProvider"
        assert "cloud unavailable" in session["fallback_reason"]

    def test_valid_cloud_session_records_truthful_egress_contract(self, monkeypatch):
        _reset_session_state(monkeypatch)
        provider = FakeCloudProvider()
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(
            browser_tool, "_is_public_network_url", lambda *args, **kwargs: True
        )

        session = browser_tool._get_session_info("task-3")

        assert session["cdp_url"].startswith("wss://")
        assert session["egress"] == REMOTE_PROVIDER_EGRESS.as_session_metadata()
        assert "fallback_from_cloud" not in session

    @pytest.mark.parametrize(
        "cdp_url",
        [
            None,
            "",
            "not-a-url",
            "ws://bad host/devtools/browser/x",
            "ws://browser.example:notaport/devtools/browser/x",
        ],
    )
    def test_absent_or_invalid_cdp_never_becomes_local_session(
        self, monkeypatch, cdp_url
    ):
        _reset_session_state(monkeypatch)
        provider = FakeCloudProvider(result=_valid_cloud_session(cdp_url=cdp_url))
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(
            browser_tool, "_allow_local_fallback_on_cloud_failure", lambda: False
        )
        monkeypatch.setattr(
            browser_tool,
            "_create_local_session",
            lambda task: pytest.fail("invalid cloud metadata selected local backend"),
        )

        with pytest.raises(RuntimeError, match="CDP endpoint|cdp_url"):
            browser_tool._get_session_info("task-invalid")

        assert provider.closed == ["provider-session-1"]

    def test_configuration_error_never_uses_compatibility_fallback(self, monkeypatch):
        _reset_session_state(monkeypatch)
        provider = browser_tool._failed_configured_provider(
            "misspelled-provider", "no provider with that name is registered"
        )
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(
            browser_tool, "_allow_local_fallback_on_cloud_failure", lambda: True
        )
        monkeypatch.setattr(
            browser_tool,
            "_create_local_session",
            lambda task: pytest.fail("configuration errors must never run locally"),
        )

        with pytest.raises(RuntimeError, match="configuration failed closed"):
            browser_tool._get_session_info("task-config-error")

    def test_no_provider_uses_local_directly(self, monkeypatch):
        _reset_session_state(monkeypatch)
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)

        session = browser_tool._get_session_info("task-local")

        assert session["features"]["local"] is True
        assert "fallback_from_cloud" not in session

    def test_cdp_override_bypasses_provider(self, monkeypatch):
        _reset_session_state(monkeypatch)
        provider = FakeCloudProvider(error=AssertionError("provider was consulted"))
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(
            browser_tool,
            "_get_cdp_override",
            lambda: "ws://host:9222/devtools/browser/abc",
        )

        session = browser_tool._get_session_info("task-cdp")

        assert session["cdp_url"] == "ws://host:9222/devtools/browser/abc"

    def test_opt_in_fallback_logs_provider_and_error(self, monkeypatch, caplog):
        _reset_session_state(monkeypatch)
        provider = FakeCloudProvider(error=ConnectionError("timeout"))
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(
            browser_tool, "_allow_local_fallback_on_cloud_failure", lambda: True
        )

        with caplog.at_level(logging.WARNING, logger="tools.browser_tool"):
            browser_tool._get_session_info("task-log")

        assert any(
            "FakeCloudProvider" in record.message and "timeout" in record.message
            for record in caplog.records
        )


def test_local_fallback_policy_uses_config_yaml(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"allow_local_fallback_on_cloud_failure": "true"}},
    )
    assert browser_tool._allow_local_fallback_on_cloud_failure() is True

    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"allow_local_fallback_on_cloud_failure": False}},
    )
    assert browser_tool._allow_local_fallback_on_cloud_failure() is False
