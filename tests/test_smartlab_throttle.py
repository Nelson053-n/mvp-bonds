"""SmartLab режет по ЧАСТОТЕ, а не по одновременности — нужен интервал.

Регрессия (прод, 24.08): семафор параллелизма (5578d37) не помог. Обход
прошёл 732 тикера за 51с — ~14 запросов/с при лимите 2 одновременных,
первая 429 прилетела уже на седьмом запросе. За сутки 676 попыток,
148 исчерпали все ретраи. Два потока подряд шлют столько же запросов в
минуту, сколько и пять, просто ровнее.
"""
import asyncio

import pytest

from app.services.moex_service import MOEXService


class TestSmartLabThrottle:

    async def test_requests_are_spaced_by_min_interval(self):
        """Подряд идущие запросы разносятся на SMARTLAB_MIN_INTERVAL."""
        svc = MOEXService()
        loop = asyncio.get_running_loop()

        t0 = loop.time()
        n = 5
        for _ in range(n):
            await svc._smartlab_throttle()
        elapsed = loop.time() - t0

        expected = (n - 1) * svc.SMARTLAB_MIN_INTERVAL
        assert elapsed >= expected * 0.9, (
            f"{n} запросов заняли {elapsed:.2f}с, ожидалось ≥{expected:.2f}с — "
            "троттлинг не работает"
        )

    async def test_rate_stays_under_limit_across_concurrent_callers(self):
        """Ключевое: частота держится, даже когда шлют независимые задачи.

        Именно этого не давал семафор — он ограничивал одновременность,
        а суммарный темп оставался прежним.
        """
        svc = MOEXService()
        loop = asyncio.get_running_loop()
        stamps: list[float] = []

        async def caller(count: int):
            for _ in range(count):
                await svc._smartlab_throttle()
                stamps.append(loop.time())

        t0 = loop.time()
        await asyncio.gather(caller(6), caller(6))   # две независимые задачи
        elapsed = loop.time() - t0

        assert len(stamps) == 12
        max_rate = 1.0 / svc.SMARTLAB_MIN_INTERVAL
        actual_rate = len(stamps) / max(elapsed, 1e-6)
        assert actual_rate <= max_rate * 1.35, (
            f"темп {actual_rate:.1f} зап/с превысил лимит {max_rate:.1f} — "
            "два вызывающих обошли троттлинг"
        )

    async def test_first_request_is_not_delayed(self):
        """Одиночный запрос после паузы не ждёт — слот в прошлом."""
        svc = MOEXService()
        loop = asyncio.get_running_loop()

        t0 = loop.time()
        await svc._smartlab_throttle()
        elapsed = loop.time() - t0

        assert elapsed < svc.SMARTLAB_MIN_INTERVAL / 2, (
            f"первый запрос ждал {elapsed:.3f}с — не должен"
        )

    async def test_429_backoff_shifts_shared_slot(self):
        """После 429 общий слот сдвигается: притормаживают ВСЕ вызывающие.

        Иначе соседние задачи продолжают долбить закрытый лимит, пока
        одна отдыхает.
        """
        svc = MOEXService()
        loop = asyncio.get_running_loop()
        await svc._smartlab_throttle()          # инициализируем слот

        # имитируем ветку 429 из _get_smartlab_credit_rating
        svc._smartlab_next_at = max(
            svc._smartlab_next_at, loop.time() + svc.SMARTLAB_RATE_LIMIT_BACKOFF
        )

        t0 = loop.time()
        await svc._smartlab_throttle()          # другой вызывающий
        waited = loop.time() - t0

        assert waited >= svc.SMARTLAB_RATE_LIMIT_BACKOFF * 0.9, (
            f"сосед подождал всего {waited:.2f}с при backoff "
            f"{svc.SMARTLAB_RATE_LIMIT_BACKOFF}с"
        )
