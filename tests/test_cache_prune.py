"""Кэш не должен вечно обновлять удалённые портфели.

06.11.2026 после чистки базы прода фоновый цикл продолжал обходить ~1800
удалённых портфелей (записи в CacheService._caches никогда не удалялись),
а для портфеля с бумагами каждый цикл падал на FOREIGN KEY при сохранении
снимка.
"""
from app.services.cache_service import CacheService
from app.services.storage_service import storage_service


def test_prune_drops_portfolios_missing_in_db(settings_override) -> None:
    cs = CacheService()
    pid = storage_service.create_portfolio(user_id=1, name="cache-prune-test")
    missing = 10_000_000
    cs.get_cache(pid)
    cs.get_cache(missing)

    assert cs._prune_deleted([pid, missing]) == [pid]
    assert pid in cs._caches
    assert missing not in cs._caches
    storage_service.delete_portfolio(pid)


def test_forget_removes_cache_entry(settings_override) -> None:
    cs = CacheService()
    cs.get_cache(42)
    cs.forget(42)
    cs.forget(42)  # повторный вызов безопасен
    assert 42 not in cs._caches


async def test_delete_endpoint_forgets_cache(client, auth_headers) -> None:
    from app.services.cache_service import cache_service

    r = await client.post("/portfolios", headers=auth_headers, json={"name": "cache-forget-test"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    cache_service.get_cache(pid)

    r = await client.delete(f"/portfolios/{pid}", headers=auth_headers)
    assert r.status_code == 204, r.text
    assert pid not in cache_service._caches
