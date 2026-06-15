"""Search-engine verification endpoints + IndexNow submitter."""

import pytest

import app.main as main_mod
from app.services import indexnow_service


# ── Verification & IndexNow key endpoints ────────────────────────────────────

async def test_yandex_verification_served_when_configured(client, monkeypatch):
    monkeypatch.setattr(main_mod.settings, "yandex_verification", "tok123", raising=False)
    r = await client.get("/yandex_tok123.html")
    assert r.status_code == 200
    assert "Verification: tok123" in r.text


async def test_yandex_verification_wrong_token_404(client, monkeypatch):
    monkeypatch.setattr(main_mod.settings, "yandex_verification", "tok123", raising=False)
    r = await client.get("/yandex_other.html")
    assert r.status_code == 404


async def test_yandex_verification_disabled_404(client, monkeypatch):
    monkeypatch.setattr(main_mod.settings, "yandex_verification", "", raising=False)
    r = await client.get("/yandex_anything.html")
    assert r.status_code == 404


async def test_google_verification_served(client, monkeypatch):
    monkeypatch.setattr(main_mod.settings, "google_verification", "Abc9", raising=False)
    r = await client.get("/googleAbc9.html")
    assert r.status_code == 200
    assert r.text == "google-site-verification: googleAbc9.html"


async def test_indexnow_key_file_served(client, monkeypatch):
    monkeypatch.setattr(main_mod.settings, "indexnow_key", "deadbeef00", raising=False)
    r = await client.get("/deadbeef00.txt")
    assert r.status_code == 200
    assert r.text == "deadbeef00"


async def test_indexnow_key_file_wrong_404(client, monkeypatch):
    monkeypatch.setattr(main_mod.settings, "indexnow_key", "deadbeef00", raising=False)
    r = await client.get("/somethingelse.txt")
    assert r.status_code == 404


# ── Existing tech files must still work (catch-all .txt must not shadow them) ──

async def test_robots_txt_still_served(client):
    r = await client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent: GPTBot" in r.text
    assert "Sitemap: https://bondai.ru/sitemap.xml" in r.text


async def test_llms_txt_still_served(client):
    r = await client.get("/llms.txt")
    assert r.status_code == 200
    assert r.text.startswith("# Bond AI")


# ── IndexNow submitter ───────────────────────────────────────────────────────

def test_indexnow_disabled_when_no_key(monkeypatch):
    monkeypatch.setattr(indexnow_service.settings, "indexnow_key", "", raising=False)
    assert indexnow_service.enabled() is False


async def test_indexnow_submit_noop_without_key(monkeypatch):
    monkeypatch.setattr(indexnow_service.settings, "indexnow_key", "", raising=False)
    assert await indexnow_service.submit(["https://bondai.ru/"]) is False


async def test_indexnow_submit_posts_payload(monkeypatch):
    monkeypatch.setattr(indexnow_service.settings, "indexnow_key", "key12345", raising=False)
    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(indexnow_service.httpx, "AsyncClient", _Client)
    ok = await indexnow_service.submit(["https://bondai.ru/", "https://bondai.ru/bond/X"])
    assert ok is True
    assert captured["json"]["key"] == "key12345"
    assert captured["json"]["host"] == "bondai.ru"
    assert captured["json"]["urlList"] == ["https://bondai.ru/", "https://bondai.ru/bond/X"]
    assert captured["json"]["keyLocation"] == "https://bondai.ru/key12345.txt"
