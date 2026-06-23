"""Unit tests for app.services.cbr_service (key-rate scrape + negative cache)."""

import httpx
import pytest

from app.services.cbr_service import (
    CBRService,
    _FALLBACK_KEY_RATE,
    _SUCCESS_TTL_SECONDS,
    _FAILURE_TTL_SECONDS,
)

# Minimal HTML fragment matching the _KEY_RATE_RE pattern.
_HTML_OK = (
    '<div>Ключевая ставка</div>'
    '<div class="value"> 18,00 %</div>'
)
_HTML_NO_RATE = "<html><body>no rate here</body></html>"


def _patch_get(monkeypatch, handler):
    """Patch httpx.AsyncClient.get so cbr_service hits our fake instead of CBR.

    Responses are bound to a dummy request so resp.raise_for_status() works.
    """
    req = httpx.Request("GET", "https://www.cbr.ru/key-indicators/")

    async def fake_get(self, url, **kwargs):
        resp = handler(url, **kwargs)
        if isinstance(resp, httpx.Response):
            resp.request = req
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


# ── happy path ───────────────────────────────────────────────────────────────

async def test_fetch_parses_rate(monkeypatch):
    _patch_get(monkeypatch, lambda url, **kw: httpx.Response(200, text=_HTML_OK))
    svc = CBRService()
    rate = await svc.get_key_rate()
    assert rate == 18.0
    # cached as a real success (not a fallback)
    assert svc._cache[0] == 18.0
    assert svc._cache[2] is False


async def test_success_is_cached(monkeypatch):
    calls = {"n": 0}

    def handler(url, **kw):
        calls["n"] += 1
        return httpx.Response(200, text=_HTML_OK)

    _patch_get(monkeypatch, handler)
    svc = CBRService()
    await svc.get_key_rate()
    await svc.get_key_rate()
    assert calls["n"] == 1  # second call served from cache


# ── edge cases ───────────────────────────────────────────────────────────────

async def test_network_error_uses_fallback(monkeypatch):
    def handler(url, **kw):
        raise httpx.ConnectError("boom")

    _patch_get(monkeypatch, handler)
    svc = CBRService()
    rate = await svc.get_key_rate()
    assert rate == _FALLBACK_KEY_RATE
    # negative-cached as a fallback so we re-fetch sooner
    assert svc._cache[2] is True


async def test_empty_page_uses_fallback(monkeypatch):
    _patch_get(monkeypatch, lambda url, **kw: httpx.Response(200, text=_HTML_NO_RATE))
    svc = CBRService()
    rate = await svc.get_key_rate()
    assert rate == _FALLBACK_KEY_RATE
    assert svc._cache[2] is True


async def test_expired_failure_cache_refetches(monkeypatch):
    """A stale fallback entry past _FAILURE_TTL_SECONDS triggers a fresh fetch."""
    _patch_get(monkeypatch, lambda url, **kw: httpx.Response(200, text=_HTML_OK))
    svc = CBRService()
    # Seed an expired fallback entry (ts far in the past).
    svc._cache = (15.0, 0.0, True)
    rate = await svc.get_key_rate()
    assert rate == 18.0  # re-fetched, not the stale 15.0
    assert svc._cache[2] is False


async def test_fresh_failure_cache_not_refetched(monkeypatch):
    """A recent fallback entry within TTL is returned without hitting the network."""
    import time

    called = {"n": 0}

    def handler(url, **kw):
        called["n"] += 1
        return httpx.Response(200, text=_HTML_OK)

    _patch_get(monkeypatch, handler)
    svc = CBRService()
    svc._cache = (15.0, time.time(), True)  # fresh fallback
    rate = await svc.get_key_rate()
    assert rate == 15.0
    assert called["n"] == 0
