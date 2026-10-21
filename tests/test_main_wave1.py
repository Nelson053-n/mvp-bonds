"""Цикл синка Т-Банка (пропуск упавших портфелей), og-теги /share, llms.txt."""
import pytest

import app.main as main_mod
from app.services import tbank_sync_service


def _cfg(pid, err=None):
    return {"portfolio_id": pid, "user_id": 1, "last_sync_error": err}


@pytest.fixture
def cycle_env(monkeypatch):
    calls = []

    async def fake_sync(pid, cfg):
        calls.append(pid)

    state = {"cfgs": []}
    monkeypatch.setattr(tbank_sync_service, "do_sync_one", fake_sync)
    monkeypatch.setattr(main_mod.storage_service, "get_all_enabled_syncs", lambda: state["cfgs"])
    monkeypatch.setattr("app.api.deps.user_can_autosync", lambda uid: True)
    return calls, state


async def test_failed_portfolio_skipped_two_cycles_then_retried(cycle_env):
    calls, state = cycle_env
    state["cfgs"] = [_cfg(1, "504 gateway timeout")]
    skipped = {}
    for _ in range(3):
        await main_mod._tbank_sync_cycle(skipped)
    assert calls == [1]  # 1-й и 2-й циклы пропущены, на 3-м попытка
    for _ in range(3):
        await main_mod._tbank_sync_cycle(skipped)
    assert calls == [1, 1]  # снова 2 пропуска и попытка


async def test_success_and_pending_removal_never_skipped(cycle_env):
    calls, state = cycle_env
    state["cfgs"] = [_cfg(1, None), _cfg(2, "PENDING_REMOVAL:SU26238")]
    skipped = {}
    for _ in range(3):
        await main_mod._tbank_sync_cycle(skipped)
    assert calls == [1, 2] * 3


async def test_counter_reset_after_success(cycle_env):
    calls, state = cycle_env
    skipped = {}
    state["cfgs"] = [_cfg(1, "boom")]
    await main_mod._tbank_sync_cycle(skipped)
    assert calls == [] and skipped[1] == 1
    state["cfgs"] = [_cfg(1, None)]  # синк оправился
    await main_mod._tbank_sync_cycle(skipped)
    assert calls == [1] and 1 not in skipped


def _share_client(monkeypatch, name, pwd_hash=None):
    from fastapi.testclient import TestClient
    p = {"id": 1, "user_id": 1, "name": name, "share_token": "tok",
         "share_password_hash": pwd_hash, "created_at": "", "share_expires_at": None}
    monkeypatch.setattr(main_mod.storage_service, "get_portfolio_by_share_token", lambda t: p)
    return TestClient(main_mod.app)


def test_share_page_og_tags_and_escaping(monkeypatch):
    evil = '<script>alert(1)</script>"x\' onload=1'
    r = _share_client(monkeypatch, evil).get("/share/tok")
    body = r.text
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&quot;x&#x27; onload=1" in body
    assert "<title>Bond AI</title>" not in body
    assert '<meta property="og:title" content="&lt;script' in body
    assert '<meta property="og:image" content="https://bondai.ru/og-image.png">' in body
    assert '<meta property="og:type"' in body and '<meta property="og:description"' in body
    assert "window.isSharedView=true" in body


def test_share_page_password_protected_hides_name(monkeypatch):
    r = _share_client(monkeypatch, "Секретное имя", pwd_hash="h").get("/share/tok")
    assert "Секретное имя" not in r.text
    assert 'property="og:title"' in r.text


def test_llms_txt_facts():
    from fastapi.testclient import TestClient
    from app.services.cache_service import cache_service
    text = TestClient(main_mod.app).get("/llms.txt").text
    assert "1500+" in text and "1200+" not in text
    assert "каждые 5 минут" in text and "15 минут" not in text
    assert cache_service.refresh_interval == 300
