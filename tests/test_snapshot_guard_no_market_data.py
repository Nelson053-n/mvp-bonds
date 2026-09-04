"""Гард «частичный сбой MOEX» не должен вечно блокировать снапшот.

Прод, 03.09.2026: портфель 187 держал 5 строк погашенных бумаг из 32.
MOEX по ним отдаёт пустой securities → no_market_data, цена 0. Покрытие
27/32 = 84% < 90%, и гард пропускал снапшот КАЖДЫЙ цикл — 247 WARNING
за сутки, а причина не сбой, а состав портфеля. Такие строки, как и
custom, из знаменателя исключаются.
"""
import pytest

from app.models import InstrumentMetrics
from app.services.cache_service import CacheService


def _row(i: int, price: float, **kw) -> InstrumentMetrics:
    return InstrumentMetrics(
        id=i, type="bond", name=f"B{i}", ticker=f"T{i}",
        current_price=price, purchase_price=1000, quantity=1,
        current_value=price, profit=0, weight=0, ai_comment="", **kw,
    )


async def _run(monkeypatch, rows):
    import app.services.portfolio_service as ps
    import app.services.storage_service as ss
    import app.services.notification_service as ns

    saved = []

    async def fake_fresh(pid):
        return rows

    async def zero_cash(pid):
        return 0.0

    async def no_notify(*a, **k):
        return None

    monkeypatch.setattr(ps.portfolio_service, "get_table_fresh", fake_fresh)
    monkeypatch.setattr(ps.portfolio_service, "get_cash_rub", zero_cash)
    monkeypatch.setattr(ns.notification_service, "check_and_notify", no_notify)
    monkeypatch.setattr(
        ss.storage_service, "save_portfolio_snapshot",
        lambda *a, **k: saved.append(a),
    )
    await CacheService().refresh(187)
    return saved


async def test_matured_rows_do_not_block_snapshot(monkeypatch):
    rows = [_row(i, 1000.0) for i in range(27)] + [
        _row(100 + i, 0.0, no_market_data=True) for i in range(5)
    ]
    saved = await _run(monkeypatch, rows)
    assert len(saved) == 1, "снапшот пропущен из-за погашенных бумаг"


async def test_real_partial_fetch_still_skips(monkeypatch):
    """Цена 0 БЕЗ флага no_market_data — это сбой MOEX, гард должен держать."""
    rows = [_row(i, 1000.0) for i in range(27)] + [
        _row(100 + i, 0.0) for i in range(5)
    ]
    saved = await _run(monkeypatch, rows)
    assert saved == []
