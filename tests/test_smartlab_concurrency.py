"""Лимит параллельных запросов к SmartLab — в одной точке, а не у вызывающих.

Регрессия (прод, 21.08 04:00): ограничители стояли у ВЫЗЫВАЮЩИХ и каждый
знал только про себя — ночной обход (2), пересчёт рейтингов портфеля (3)
и синк T-Bank через notification_service (без лимита вовсе). В сумме они
давали 5+ одновременных запросов, SmartLab отвечал 429: 260 запросов по
132 бумагам за час, 52 из них исчерпали все ретраи.
"""
import asyncio

import pytest

from app.services.moex_service import MOEXService


class TestSmartLabConcurrencyLimit:

    def _patch_client(self, svc, monkeypatch, hold: float = 0.02):
        """Фейковый клиент, считающий ОДНОВРЕМЕННЫЕ запросы."""
        state = {"now": 0, "peak": 0}

        class _Resp:
            status_code = 200
            text = '<div class="linear-progress-bar__text">AA-</div>'

            def raise_for_status(self):
                return None

        class _Client:
            async def get(self, url, headers=None, timeout=None):
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
                try:
                    await asyncio.sleep(hold)   # держим слот, чтобы пики пересеклись
                finally:
                    state["now"] -= 1
                return _Resp()

        monkeypatch.setattr(MOEXService, "_get_http", lambda self: _Client())
        return state

    async def test_peak_never_exceeds_limit(self, monkeypatch):
        """20 бумаг разом → одновременных запросов не больше SMARTLAB_CONCURRENCY."""
        svc = MOEXService()
        state = self._patch_client(svc, monkeypatch)

        await asyncio.gather(*(
            svc._get_smartlab_credit_rating(f"BOND{i:04d}") for i in range(20)
        ))

        assert state["peak"] <= svc.SMARTLAB_CONCURRENCY, (
            f"пик {state['peak']} превысил лимит {svc.SMARTLAB_CONCURRENCY}"
        )

    async def test_limit_holds_across_independent_callers(self, monkeypatch):
        """Ключевое: лимит общий, даже когда вызывающие ничего друг о друге не знают.

        Имитируем прод-ситуацию — ночной обход и синк T-Bank стартуют
        одновременно. Раньше каждый имел свой семафор и они складывались.
        """
        svc = MOEXService()
        state = self._patch_client(svc, monkeypatch)

        async def caller_a():   # «ночной обход»
            await asyncio.gather(*(
                svc._get_smartlab_credit_rating(f"A{i:04d}") for i in range(10)
            ))

        async def caller_b():   # «синк T-Bank», отдельный код, свой gather
            await asyncio.gather(*(
                svc._get_smartlab_credit_rating(f"B{i:04d}") for i in range(10)
            ))

        await asyncio.gather(caller_a(), caller_b())

        assert state["peak"] <= svc.SMARTLAB_CONCURRENCY, (
            f"два независимых вызывающих дали пик {state['peak']} "
            f"при лимите {svc.SMARTLAB_CONCURRENCY}"
        )

    async def test_semaphore_is_lazy_and_shared(self):
        """Семафор создаётся лениво (event loop) и один на сервис."""
        svc = MOEXService()
        assert svc._smartlab_sem is None, "до первого вызова семафора быть не должно"
        sem = svc._get_smartlab_sem()
        assert sem is svc._get_smartlab_sem(), "должен переиспользоваться"
