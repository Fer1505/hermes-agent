"""Request/source identity across real providers, dispatch and disk caching."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent import web_search_registry
from agent.web_search_provider import WebSearchProvider
from tools import web_result_cache, web_tools, website_policy
from tools.website_policy import check_website_access as real_check_website_access

A = "https://example.com/first"
B = "https://example.org/second"
C = "https://example.net/third"


@pytest.fixture
def install(monkeypatch, tmp_path):
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "async_is_safe_url", AsyncMock(
        side_effect=lambda url: not str(url).startswith("http://127.")))
    monkeypatch.setattr(website_policy, "check_website_access", lambda url: None)
    root = tmp_path / "web-cache"
    root.mkdir()
    monkeypatch.setattr(web_result_cache, "_cache_dir", lambda: root)

    def bind(provider):
        monkeypatch.setattr(web_search_registry, "get_provider", lambda name: provider)
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {
            "extract_backend": provider.name, "keyless_rescue": False,
        })
        return provider
    return bind


class Provider(WebSearchProvider):
    name = "synthetic-attribution"
    def is_available(self): return True
    def supports_extract(self): return True
    def extract(self, urls, **kwargs): return []


async def extract(urls):
    return json.loads(await web_tools.web_extract_tool(urls))["results"]


@pytest.mark.asyncio
async def test_parallel_successes_then_errors_do_not_poison_cache(install, monkeypatch):
    from plugins.web.parallel import provider as module
    from plugins.web import keyless_mcp
    monkeypatch.setattr(keyless_mcp, "use_keyless", lambda *args: False)
    request = AsyncMock(return_value=SimpleNamespace(
        results=[SimpleNamespace(url=C, title="Third", full_content="THIRD", excerpts=[])],
        errors=[SimpleNamespace(url=A, content="unavailable", error_type="fetch_error"),
                SimpleNamespace(url=B, content="unavailable", error_type="fetch_error")],
    ))
    monkeypatch.setattr(module, "_get_async_client", lambda: SimpleNamespace(
        beta=SimpleNamespace(extract=request)))
    install(module.ParallelWebSearchProvider())
    first = await extract([A, B, C])
    assert [r["url"] for r in first] == [A, B, C]
    assert first[0]["error"] and first[1]["error"]
    assert first[2]["content"] == "THIRD"
    second = await extract([C])
    assert second[0]["content"] == "THIRD" and second[0]["cached"] is True
    assert request.await_count == 1
    assert web_result_cache.extract_cache_get(A, provider="parallel") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [{"url": B, "content": "SECOND"}],
    [{"url": B, "content": "SECOND"}, {"url": A, "content": "FIRST"}],
])
async def test_short_or_reordered_batch_keeps_each_source(install, monkeypatch, rows):
    p = install(Provider())
    monkeypatch.setattr(p, "extract", lambda *args, **kwargs: rows)
    result = await extract([A, B])
    assert result[1]["url"] == B and result[1]["content"] == "SECOND"
    if len(rows) == 1:
        assert result[0]["error"] and not result[0]["content"]
        assert web_result_cache.extract_cache_get(A, provider=p.name) is None
    else:
        assert result[0]["content"] == "FIRST"


@pytest.mark.asyncio
async def test_explicit_request_identity_preserves_redirect_even_when_reordered(install, monkeypatch):
    p = install(Provider())
    monkeypatch.setattr(p, "extract", lambda *args, **kwargs: [
        {"requested_url": B, "url": C, "content": "REDIRECTED SECOND"},
        {"requested_url": A, "url": B, "content": "REDIRECTED FIRST"},
    ])
    result = await extract([A, B])
    assert [r["requested_url"] for r in result] == [A, B]
    assert [r["url"] for r in result] == [B, C]
    assert [r["content"] for r in await extract([A, B])] == [
        "REDIRECTED FIRST", "REDIRECTED SECOND"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [{"url": C, "content": "UNBOUND"}],
    [{"url": A, "content": "CONFLICT ONE"}, {"url": A, "content": "CONFLICT TWO"}],
    [{"requested_url": C, "url": A, "content": "FOREIGN REQUEST"}],
])
async def test_ambiguous_batch_is_not_silently_bound_or_cached(install, monkeypatch, rows):
    p = install(Provider())
    monkeypatch.setattr(p, "extract", lambda *args, **kwargs: rows)
    result = await extract([A, B])
    assert len(result) == 2
    assert all(r["error"] and not r["content"] for r in result)
    assert all(web_result_cache.extract_cache_get(u, provider=p.name) is None for u in [A, B])


@pytest.mark.asyncio
async def test_requested_policy_block_never_reaches_provider_or_rescue(install, monkeypatch):
    p = install(Provider())
    request = Mock(return_value=[{"url": A, "content": "SHOULD NOT FETCH"}])
    monkeypatch.setattr(p, "extract", request)
    monkeypatch.setattr(website_policy, "check_website_access", lambda url: {
        "host": "example.com", "rule": "example.com", "source": "fixture", "message": "Blocked by policy"})
    result = await extract([A])
    request.assert_not_called()
    assert result[0]["error"] and not result[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("final", ["http://127.0.0.1/private", C])
async def test_reported_source_is_checked_on_fresh_and_cached_results(install, monkeypatch, final):
    p = install(Provider())
    monkeypatch.setattr(p, "extract", lambda *args, **kwargs: [
        {"url": final, "content": "BLOCKED CONTENT"}])
    monkeypatch.setattr(website_policy, "check_website_access", lambda url: {
        "host": "example.net", "rule": "example.net", "source": "fixture", "message": "Blocked by policy"
    } if url == C else None)
    result = await extract([A])
    assert result[0]["error"] and not result[0]["content"]
    assert web_result_cache.extract_cache_get(A, provider=p.name) is None
    # Simulate an old entry written while its final source was allowed.
    if final == C:
        web_result_cache.extract_cache_put(A, "CACHED BLOCKED CONTENT", provider=p.name, result_url=C)
        second = await extract([A])
        assert second[0]["error"] and not second[0]["content"]


@pytest.mark.asyncio
async def test_mixed_cache_hit_blocked_input_and_reordered_results(install, monkeypatch):
    p = install(Provider())
    web_result_cache.extract_cache_put(A, "FIRST", provider=p.name, result_url=A)
    monkeypatch.setattr(p, "extract", lambda urls, **kwargs: [
        {"url": C, "content": "THIRD"}, {"url": B, "content": "SECOND"}])
    result = await extract([A, "http://127.0.0.1/private", B, C])
    assert [r["content"] for r in result] == ["FIRST", "", "SECOND", "THIRD"]
    assert result[0]["cached"] is True and result[1]["error"]


def test_keyless_firecrawl_preserves_reported_source_over_real_local_http(monkeypatch):
    from plugins.web import keyless_mcp
    from plugins.web.firecrawl import provider as module
    bodies = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            payload = json.dumps({"success": True, "data": {
                "markdown": "SYNTHETIC REDIRECTED PAGE", "metadata": {"sourceURL": B, "title": "Business"},
            }}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    client = module._KeylessFirecrawlClient(f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(module, "_KeylessFirecrawlClient", lambda: client)
    thread.start()
    try:
        result = keyless_mcp.firecrawl_extract_keyless([A])[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert bodies == [{"url": A, "formats": ["markdown"]}]
    assert result["requested_url"] == A and result["url"] == B
    assert result["metadata"]["sourceURL"] == B


@pytest.mark.asyncio
async def test_previous_cache_schema_cannot_reuse_positional_attribution(install, monkeypatch):
    p = install(Provider())
    web_result_cache.extract_cache_put(A, "WRONG PAGE FROM OLD BATCH", provider=p.name, result_url=B)
    path = web_result_cache._index_path()
    index = json.loads(path.read_text())
    for entry in index.values():
        entry["schema_version"] = 2
    path.write_text(json.dumps(index))
    request = Mock(return_value=[{"url": A, "content": "CORRECT FIRST PAGE"}])
    monkeypatch.setattr(p, "extract", request)
    result = await extract([A])
    assert result[0]["content"] == "CORRECT FIRST PAGE"
    request.assert_called_once()


@pytest.mark.asyncio
async def test_real_profile_blocklist_applies_before_fetch_and_after_redirect(install, monkeypatch, tmp_path):
    p = install(Provider())
    config = tmp_path / "config.yaml"
    config.write_text("security:\n  website_blocklist:\n    enabled: true\n    domains:\n      - example.com\n      - example.net\n")
    monkeypatch.setattr(website_policy, "_cached_policy", None)
    monkeypatch.setattr(website_policy, "check_website_access",
                        lambda url: real_check_website_access(url, config_path=config))
    request = Mock(return_value=[{"url": C, "content": "DISALLOWED REDIRECT"}])
    monkeypatch.setattr(p, "extract", request)
    result = await extract([A, B])
    assert request.call_args.args[0] == [B]
    assert all(r["error"] and not r["content"] for r in result)
    assert [r["blocked_by_policy"]["host"] for r in result] == ["example.com", "example.net"]


@pytest.mark.asyncio
@pytest.mark.parametrize("final, allowed", [(B, True), ("http://127.0.0.1/private", False)])
async def test_keyless_http_provider_dispatch_cache_source_chain(install, monkeypatch, final, allowed):
    from plugins.web import keyless_mcp
    from plugins.web.firecrawl import provider as module
    bodies = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps({"success": True, "data": {
                "markdown": "SYNTHETIC SOURCE CONTENT", "metadata": {"sourceURL": final},
            }}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    client = module._KeylessFirecrawlClient(f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(module, "_KeylessFirecrawlClient", lambda: client)
    monkeypatch.setattr(module, "_use_keyless_ring", lambda: True)
    monkeypatch.setattr(keyless_mcp, "extract_with_failover",
                        lambda provider, urls: keyless_mcp.firecrawl_extract_keyless(urls))
    install(module.FirecrawlWebSearchProvider())
    thread.start()
    try:
        first = (await extract([A]))[0]
        second = (await extract([A]))[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert first["requested_url"] == second["requested_url"] == A
    assert first["url"] == second["url"] == final
    if allowed:
        assert first["content"] == second["content"] == "SYNTHETIC SOURCE CONTENT"
        assert second["cached"] is True and len(bodies) == 1
    else:
        assert all(r["error"] and not r["content"] for r in [first, second])
        assert web_result_cache.extract_cache_get(A, provider="firecrawl") is None
        assert len(bodies) == 2


def test_rescue_respects_reordered_policy_failures_and_short_batches(monkeypatch):
    from plugins.web import keyless_mcp
    blocked = {"url": A, "error": "policy block", "blocked_by_policy": {"rule": "example.com"}}
    failed = {"url": B, "error": "upstream unavailable"}
    rescue = Mock(return_value=[{"url": B, "content": "SECOND"}])
    monkeypatch.setattr(keyless_mcp, "extract_with_failover", rescue)
    result = web_tools._rescue_extract("synthetic", [A, B, C], [failed, blocked])
    assert rescue.call_args.args[1] == [B]
    assert result[0]["blocked_by_policy"] == blocked["blocked_by_policy"]
    assert result[1]["content"] == "SECOND"
    assert result[2]["error"] and not result[2]["content"]


@pytest.mark.asyncio
async def test_policy_denial_during_cache_lookup_does_not_become_a_fetch(install, monkeypatch):
    p = install(Provider())
    request = Mock(return_value=[{"url": A, "content": "MUST NOT FETCH"}])
    monkeypatch.setattr(p, "extract", request)
    monkeypatch.setattr(website_policy, "check_website_access", Mock(side_effect=[None, {
        "host": "example.com", "rule": "example.com", "source": "fixture", "message": "Blocked by policy",
    }]))
    result = (await extract([A]))[0]
    request.assert_not_called()
    assert result["error"] and not result["content"]


@pytest.mark.parametrize("keyless", [False, True])
def test_keenable_metadata_and_result_keep_same_reported_source(monkeypatch, keyless):
    import requests
    from plugins.web import keyless_mcp
    from plugins.web.keenable.provider import KeenableWebSearchProvider
    response = SimpleNamespace(status_code=200, json=lambda: {
        "url": B, "content": "SECOND", "title": "Synthetic"})
    monkeypatch.setattr(requests, "get", Mock(return_value=response))
    monkeypatch.setattr(keyless_mcp, "use_keyless", lambda *args: False)
    monkeypatch.setattr("agent.web_search_provider.get_provider_env", lambda *args: "synthetic-test-key")
    result = (keyless_mcp.keenable_extract_keyless([A]) if keyless else
              KeenableWebSearchProvider().extract([A]))[0]
    assert result["requested_url"] == A
    assert result["url"] == result["metadata"]["sourceURL"] == B
