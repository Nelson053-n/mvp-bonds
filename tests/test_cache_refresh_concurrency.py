"""Фоновый цикл кэша обновляет портфели пачками, а не все разом.

Регрессия (прод, 23.08): `asyncio.gather` по ВСЕМ портфелям без лимита.
Внутри каждого свой Semaphore(8) на бумаги, поэтому 136 портфелей давали
до 1088 одновременных запросов к MOEX. Пока каждый запрос создавал свой
AsyncClient со своим пулом, это сходило с рук; с общим клиентом (9ac8f44,
max_connections=32) очередь за соединением стала упираться в таймаут —
38302 PoolTimeout за сутки, 36813 трассировок в логе.
"""
import asyncio

import pytest

from app.services.cache_service import CacheService


class TestBackgroundRefreshConcurrency:

    async def test_refresh_runs_in_batches(self, monkeypatch):
        """Одновременно обновляется не больше REFRESH_CONCURRENCY портфелей."""
        svc = CacheService()
        state = {"now": 0, "peak": 0, "done": 0}

        async def fake_refresh(pid: int):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            try:
                await asyncio.sleep(0.01)   # держим слот, чтобы пики пересеклись
            finally:
                state["now"] -= 1
                state["done"] += 1

        monkeypatch.setattr(svc, "refresh", fake_refresh)
        # 40 портфелей, как на проде их 136
        for pid in range(40):
            svc.get_cache(pid)

        # один проход цикла: запускаем и снимаем после первой пачки
        task = asyncio.create_task(svc._background_loop())
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert state["done"] == 40, f"обновились не все: {state['done']}/40"
        assert state["peak"] <= svc.REFRESH_CONCURRENCY, (
            f"пик {state['peak']} превысил лимит {svc.REFRESH_CONCURRENCY} — "
            "gather снова без ограничения"
        )

    async def test_concurrency_fits_connection_pool(self):
        """Суммарный параллелизм не должен превышать пул общего клиента.

        REFRESH_CONCURRENCY × Semaphore(8) внутри портфеля — это и есть
        нагрузка на пул httpx. Если кто-то поднимет лимит, не сверившись
        с max_connections, вернётся PoolTimeout.
        """
        import httpx
        from app.services.moex_service import moex_service

        client = moex_service._get_http()
        pool_limit = client._transport._pool._max_connections
        per_portfolio = 8  # portfolio_service.get_table_fresh
        assert CacheService.REFRESH_CONCURRENCY * per_portfolio <= pool_limit, (
            f"{CacheService.REFRESH_CONCURRENCY}×{per_portfolio} не влезает "
            f"в пул {pool_limit}"
        )
