"""Один сбойный инструмент не должен ронять всю таблицу /table,
но обязан быть наблюдаемым в логах (а не молча маскироваться под
«нет рыночных данных»). Регрессия: AttributeError/KeyError в расчёте
метрик одной бумаги раньше проглатывался без следа в логах.
"""
import logging

import pytest

from app.exceptions import PriceNotFoundError
from app.models import StockSnapshot
from app.services.portfolio_service import portfolio_service


def _item(item_id: int, ticker: str) -> dict:
    """Минимальная строка из storage для StockSnapshot-инструмента."""
    return {
        "id": item_id,
        "ticker": ticker,
        "instrument_type": "stock",
        "quantity": 10.0,
        "purchase_price": 100.0,
        "manual_coupon": None,
        "manual_coupon_rate": None,
    }


async def test_one_failing_row_is_logged_and_does_not_break_others(
    monkeypatch, caplog
):
    items = [_item(1, "GOOD"), _item(2, "BOOM")]
    monkeypatch.setattr(
        "app.services.storage_service.storage_service.get_items",
        lambda portfolio_id: items,
    )
    monkeypatch.setattr(
        "app.services.storage_service.storage_service.get_tbank_coupons",
        lambda portfolio_id: {},
    )

    async def fake_stock_snapshot(ticker: str) -> StockSnapshot:
        if ticker == "BOOM":
            # имитируем реальный дефект в расчёте (как AttributeError выше)
            raise KeyError("synthetic calc failure")
        return StockSnapshot(ticker=ticker, name="Good Co", current_price=120.0)

    monkeypatch.setattr(
        "app.services.moex_service.moex_service.get_stock_snapshot",
        fake_stock_snapshot,
    )

    # Без аргумента logger=: setup_logging() (вызывается импортом app.main в
    # других тестах) переустанавливает root-логгер и чистит хэндлеры — при
    # передаче logger="..." caplog не перехватывает запись стабильно.
    with caplog.at_level(logging.WARNING):
        rows = await portfolio_service.get_table_fresh(portfolio_id=777)

    # обе строки на месте — упавшая бумага не уронила остальные
    by_ticker = {r.ticker: r for r in rows}
    assert set(by_ticker) == {"GOOD", "BOOM"}

    # успешная строка посчитана нормально
    assert by_ticker["GOOD"].current_price == 120.0
    assert by_ticker["GOOD"].current_value == pytest.approx(1200.0)

    # сбойная строка получила fallback (нулевая цена), но не исчезла
    assert by_ticker["BOOM"].current_price == 0.0

    # и главное — сбой наблюдаем: есть warning с тикером и контекстом
    boom_logs = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING and "BOOM" in rec.getMessage()
    ]
    assert boom_logs, "падение расчёта метрик BOOM должно логироваться"
    msg = boom_logs[0].getMessage()
    assert "777" in msg, "в логе должен быть portfolio_id"
    assert "KeyError" in msg or "synthetic calc failure" in msg, (
        "в логе должен быть тип/текст исходной ошибки"
    )


async def test_missing_quote_is_not_logged_as_warning(monkeypatch, caplog):
    """Бумага без котировки — штатная ситуация, не сбой.

    Снятая с торгов / погашенная / иностранная бумага поднимает
    PriceNotFoundError на КАЖДОМ пересчёте таблицы. Раньше общий
    `except Exception` писал на неё WARNING с полной трассировкой:
    одна такая бумага давала 241 запись в сутки, а всего их набегало
    1010 из 1010 — то есть весь WARNING-поток был штатным шумом,
    в котором утонул бы настоящий сбой.
    """
    items = [_item(1, "GOOD"), _item(2, "DELISTED")]
    monkeypatch.setattr(
        "app.services.storage_service.storage_service.get_items",
        lambda portfolio_id: items,
    )
    monkeypatch.setattr(
        "app.services.storage_service.storage_service.get_tbank_coupons",
        lambda portfolio_id: {},
    )

    async def fake_stock_snapshot(ticker: str) -> StockSnapshot:
        if ticker == "DELISTED":
            raise PriceNotFoundError(ticker, "акция")
        return StockSnapshot(ticker=ticker, name="Good Co", current_price=120.0)

    monkeypatch.setattr(
        "app.services.moex_service.moex_service.get_stock_snapshot",
        fake_stock_snapshot,
    )

    with caplog.at_level(logging.DEBUG):
        rows = await portfolio_service.get_table_fresh(portfolio_id=777)

    by_ticker = {r.ticker: r for r in rows}
    assert set(by_ticker) == {"GOOD", "DELISTED"}

    # строка на месте и честно помечена «нет данных», а не обнулена в убыток
    assert by_ticker["DELISTED"].no_market_data is True
    assert by_ticker["DELISTED"].profit == 0.0

    # ключевое: никакого WARNING и никакой трассировки
    noisy = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING and "DELISTED" in rec.getMessage()
    ]
    assert not noisy, (
        "отсутствие котировки не должно логироваться как WARNING: "
        f"получено {[r.getMessage()[:80] for r in noisy]}"
    )
    assert not any(
        rec.exc_info for rec in caplog.records
        if "DELISTED" in rec.getMessage()
    ), "трассировка для штатного случая не нужна"

    # но событие всё же наблюдаемо — INFO с тикером и портфелем
    info = [
        rec for rec in caplog.records
        if rec.levelno == logging.INFO and "DELISTED" in rec.getMessage()
    ]
    assert info, "событие должно остаться видимым на уровне INFO"
    assert "777" in info[0].getMessage(), "в логе должен быть portfolio_id"
