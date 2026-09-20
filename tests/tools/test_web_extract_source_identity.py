"""The extraction cache must not change a provider-reported source URL."""
import json
from unittest.mock import AsyncMock

import pytest

from agent import web_search_registry
from agent.web_search_provider import WebSearchProvider
from tools import web_result_cache as cache, web_tools


@pytest.fixture
def provider(monkeypatch, tmp_path):
    class Provider(WebSearchProvider):
        name = "synthetic-source-identity"
        display_name = "Synthetic source identity"
        calls = 0
        result_url = "https://example.org/contact"
        def is_available(self): return True
        def supports_extract(self): return True
        async def extract(self, urls, **kwargs):
            self.calls += 1
            return [{"url": self.result_url, "title": "Synthetic business page", "content": "Synthetic contact: +1 202 555 0123."} for _ in urls]
    current = Provider()
    with web_search_registry._lock:
        previous = dict(web_search_registry._providers)
        web_search_registry._providers.clear()
    web_search_registry.register_provider(current)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_backend": current.name, "cache_enabled": True})
    monkeypatch.setattr(web_tools, "async_is_safe_url", AsyncMock(return_value=True))
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(cache, "_cache_dir", lambda: root)
    try:
        yield current
    finally:
        with web_search_registry._lock:
            web_search_registry._providers.clear()
            web_search_registry._providers.update(previous)


@pytest.mark.asyncio
async def test_cached_extract_preserves_provider_reported_source(provider):
    requested = "https://example.com/business"
    first = json.loads(await web_tools.web_extract_tool([requested]))["results"][0]
    second = json.loads(await web_tools.web_extract_tool([requested]))["results"][0]
    assert provider.calls == 1
    assert first["url"] == second["url"] == provider.result_url
    assert first["content"] == second["content"]
    assert second["cached"] is True
    assert isinstance(second["cache_stored_at"], (int, float))


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [None, "", "http://127.0.0.1/contact", "http://internal/contact"])
async def test_unattributed_or_local_report_is_not_relabelled_from_cache(provider, reported):
    provider.result_url = reported
    for _ in range(2):
        result = json.loads(await web_tools.web_extract_tool(["https://example.com/business"]))["results"][0]
        assert result["url"] == reported
        assert "cached" not in result
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_provider_cannot_forge_local_cache_hit_metadata(provider, monkeypatch):
    async def extract(urls, **kwargs):
        provider.calls += 1
        return [{"url": urls[0], "content": "Synthetic text", "cached": True, "cache_stored_at": 123}]
    monkeypatch.setattr(provider, "extract", extract)
    first = json.loads(await web_tools.web_extract_tool(["https://example.com/metadata"]))["results"][0]
    second = json.loads(await web_tools.web_extract_tool(["https://example.com/metadata"]))["results"][0]
    assert "cached" not in first
    assert second["cached"] is True
    assert second["cache_stored_at"] != 123
    assert provider.calls == 1
