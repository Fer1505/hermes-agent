"""Tests for ``_get_cloud_provider()`` caching policy.

Regression coverage for issue #22324: a transient ``None`` from the resolver
must not be cached for the lifetime of the process. Cache only when:

* The user explicitly opts in to ``cloud_provider: local``, OR
* A provider is successfully resolved.

Transient auto-detect ``None`` outcomes leave the cache unset so the next call
retries. Explicit invalid providers return a non-local fail-closed marker and
also remain uncached so repaired plugin/config state can recover.
"""
import logging
from unittest.mock import Mock

import pytest

import tools.browser_tool as browser_tool


@pytest.fixture(autouse=True)
def _reset_resolver_state(monkeypatch):
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    yield


class TestCloudProviderCachePolicy:
    def test_explicit_local_caches_permanently(self, monkeypatch):
        """`cloud_provider: local` is a positive choice and must stick."""
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"cloud_provider": "local"}},
        )

        assert browser_tool._get_cloud_provider() is None
        assert browser_tool._cloud_provider_resolved is True

        # Even if config later changes, the cache stays.
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"cloud_provider": "browser-use"}},
        )
        assert browser_tool._get_cloud_provider() is None

    def test_successful_cloud_resolution_caches_permanently(self, monkeypatch):
        """A real provider instance must be cached and reused."""
        fake_provider = Mock(name="BrowserUseProvider-instance")
        factory = Mock(return_value=fake_provider)
        monkeypatch.setattr(
            browser_tool, "_PROVIDER_REGISTRY", {"browser-use": factory}
        )
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"cloud_provider": "browser-use"}},
        )

        assert browser_tool._get_cloud_provider() is fake_provider
        assert browser_tool._cloud_provider_resolved is True

        # Subsequent calls hit the cache; factory not called again.
        assert browser_tool._get_cloud_provider() is fake_provider
        assert factory.call_count == 1

    def test_no_credentials_yet_does_not_cache_none(self, monkeypatch):
        """Auto-detect path with no creds: must NOT poison the cache."""
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {}},
        )

        bu_unconfigured = Mock()
        bu_unconfigured.is_configured.return_value = False
        bb_unconfigured = Mock()
        bb_unconfigured.is_configured.return_value = False
        monkeypatch.setattr(
            browser_tool, "BrowserUseProvider", lambda: bu_unconfigured
        )
        monkeypatch.setattr(
            browser_tool, "BrowserbaseProvider", lambda: bb_unconfigured
        )

        assert browser_tool._get_cloud_provider() is None
        assert browser_tool._cloud_provider_resolved is False

        # Credentials self-heal — next call must retry and pick up the provider.
        healed = Mock(name="healed-provider")
        healed.is_configured.return_value = True
        monkeypatch.setattr(browser_tool, "BrowserUseProvider", lambda: healed)

        assert browser_tool._get_cloud_provider() is healed
        assert browser_tool._cloud_provider_resolved is True

    def test_config_read_failure_does_not_cache_none(self, monkeypatch):
        """A raised config read must not pin the resolver to local mode."""
        def boom():
            raise OSError("config file locked")

        monkeypatch.setattr("hermes_cli.config.read_raw_config", boom)

        assert browser_tool._get_cloud_provider() is None
        assert browser_tool._cloud_provider_resolved is False

    def test_explicit_provider_instantiation_failure_does_not_cache(
        self, monkeypatch, caplog
    ):
        """A broken explicit provider fails closed without poisoning retry."""
        def exploding_factory():
            raise RuntimeError("missing dependency")

        monkeypatch.setattr(
            browser_tool, "_PROVIDER_REGISTRY", {"browser-use": exploding_factory}
        )
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"cloud_provider": "browser-use"}},
        )

        with caplog.at_level(logging.WARNING, logger="tools.browser_tool"):
            failed = browser_tool._get_cloud_provider()

        assert failed is not None
        assert failed.configuration_error is True
        with pytest.raises(RuntimeError, match="provider initialization failed"):
            failed.create_session("task")
        assert browser_tool._cloud_provider_resolved is False
        assert any(
            "browser-use" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_explicit_unknown_provider_is_not_auto_detected_or_local(
        self, monkeypatch
    ):
        """A typo is represented as a failing cloud backend, never as None."""
        monkeypatch.setattr(
            browser_tool,
            "_PROVIDER_REGISTRY",
            {"browser-use": browser_tool.BrowserUseProvider},
        )
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"cloud_provider": "typo-provider"}},
        )

        failed = browser_tool._get_cloud_provider()

        assert failed is not None
        assert failed.configuration_error is True
        assert browser_tool._is_local_mode() is False
