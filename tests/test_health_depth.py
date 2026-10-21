"""/health: db_ok/cache_age_s и fail_streak-алерты фоновых циклов."""
import asyncio

import pytest

from app import main
from app.services.cache_service import cache_service


async def test_health_db_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["cache_age_s"] is None or body["cache_age_s"] >= 0


async def test_health_db_broken_still_200(client, monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(main.storage_service, "_connect", boom)
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["db_ok"] is False


async def test_health_cache_age(client, monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: 1000.0)
    monkeypatch.setattr(cache_service, "_caches", {})
    c = cache_service.get_cache(1)
    c.last_refresh = 990.0
    r = await client.get("/health")
    assert r.json()["cache_age_s"] == 10.0


def _stop_after(n):
    """Фейковый asyncio.sleep: пропускает n вызовов, затем отменяет цикл."""
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > n:
            raise asyncio.CancelledError
    return fake_sleep


async def test_tbank_loop_alerts_at_threshold(monkeypatch):
    alerts = []

    async def fake_alert(name, streak, exc):
        alerts.append((name, streak))

    async def failing_cycle(skipped):
        raise RuntimeError("whole pass failed")

    monkeypatch.setattr(main, "_tbank_sync_cycle", failing_cycle)
    monkeypatch.setattr(main, "_alert_task_failure", fake_alert)
    # sleep #1 — стартовая задержка, затем по одному после каждого прохода: n проходов = n разрешённых sleep
    monkeypatch.setattr(main.asyncio, "sleep", _stop_after(main._FAILURE_ALERT_THRESHOLD))
    with pytest.raises(asyncio.CancelledError):
        await main._tbank_sync_loop()
    assert alerts == [("Синк Т-Банка", main._FAILURE_ALERT_THRESHOLD)]


async def test_tbank_loop_success_resets_streak(monkeypatch):
    alerts = []
    results = iter([True, True, False, True, True])  # True = падение

    async def fake_alert(name, streak, exc):
        alerts.append(streak)

    async def cycle(skipped):
        if next(results):
            raise RuntimeError("x")

    monkeypatch.setattr(main, "_tbank_sync_cycle", cycle)
    monkeypatch.setattr(main, "_alert_task_failure", fake_alert)
    monkeypatch.setattr(main.asyncio, "sleep", _stop_after(5))
    with pytest.raises(asyncio.CancelledError):
        await main._tbank_sync_loop()
    assert alerts == []  # серия никогда не дотянула до порога


async def test_cache_watchdog_alerts_when_stale(monkeypatch):
    alerts = []

    async def fake_alert(name, streak, exc):
        alerts.append((name, streak))

    monkeypatch.setattr(cache_service, "_caches", {})
    cache_service.get_cache(1).last_refresh = 1.0  # давным-давно
    monkeypatch.setattr(main, "_alert_task_failure", fake_alert)
    monkeypatch.setattr(main.asyncio, "sleep", _stop_after(main._FAILURE_ALERT_THRESHOLD))
    with pytest.raises(asyncio.CancelledError):
        await main._cache_watchdog_loop()
    assert alerts == [("Обновление кэша котировок", main._FAILURE_ALERT_THRESHOLD)]


async def test_cache_watchdog_quiet_when_fresh(monkeypatch):
    alerts = []

    async def fake_alert(*a):
        alerts.append(a)

    monkeypatch.setattr(cache_service, "_caches", {})
    cache_service.get_cache(1).last_refresh = main.time.time()
    monkeypatch.setattr(main, "_alert_task_failure", fake_alert)
    monkeypatch.setattr(main.asyncio, "sleep", _stop_after(5))
    with pytest.raises(asyncio.CancelledError):
        await main._cache_watchdog_loop()
    assert alerts == []


async def test_health_cache_age_uses_freshest(client, monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: 1000.0)
    monkeypatch.setattr(cache_service, "_caches", {})
    cache_service.get_cache(1).last_refresh = 100.0
    cache_service.get_cache(2).last_refresh = 990.0
    cache_service.get_cache(3).last_refresh = 0.0  # не прогрет — не учитывается
    r = await client.get("/health")
    assert r.json()["cache_age_s"] == 10.0
