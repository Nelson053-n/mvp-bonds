"""Разбор данных T-Bank: деньги, позиции, операции.

Модуль был покрыт на 24%, при том что здесь считаются реальные суммы
пользователя. Ошибка в конвертации units/nano не падает, а тихо искажает
стоимость портфеля и накопленные купоны.

Отдельно проверяем отрицательные значения: в MoneyValue отрицательная сумма
приходит с отрицательными units И nano одновременно — наивное сложение по
модулю дало бы неверный знак.
"""
from app.services.tbank_service import (
    TBankService,
    _money_value,
    _quotation,
)


# ── Деньги ───────────────────────────────────────────────────────────────────

def test_money_value_combines_units_and_nano():
    assert _money_value({"units": "10", "nano": 500_000_000}) == 10.5


def test_money_value_handles_missing_parts():
    assert _money_value({"units": "7"}) == 7.0
    assert _money_value({"nano": 250_000_000}) == 0.25
    assert _money_value({}) == 0.0
    assert _money_value(None) == 0.0


def test_money_value_negative_amount():
    """Убыток приходит с отрицательными units и nano — знак обязан сохраниться."""
    assert _money_value({"units": "-3", "nano": -500_000_000}) == -3.5


def test_money_value_precision():
    """1 nano = 1e-9; копейки не должны теряться."""
    assert _money_value({"units": "0", "nano": 10_000_000}) == 0.01
    assert _money_value({"units": "1234", "nano": 560_000_000}) == 1234.56


def test_quotation_matches_money_semantics():
    assert _quotation({"units": "2", "nano": 500_000_000}) == 2.5
    assert _quotation(None) == 0.0
    assert _quotation({}) == 0.0


# ── Позиции → элементы портфеля ──────────────────────────────────────────────

def _pos(**overrides) -> dict:
    base = {
        "instrumentType": "bond",
        "ticker": "SU26238RMFS4",
        "figi": "BBG00QWERTY1",
        "quantity": {"units": "10", "nano": 0},
        "averagePositionPrice": {"units": "550", "nano": 500_000_000},
    }
    base.update(overrides)
    return base


def test_positions_to_items_basic():
    items = TBankService._positions_to_items([_pos()])
    assert len(items) == 1
    it = items[0]
    assert it["ticker"] == "SU26238RMFS4"
    assert it["instrument_type"] == "bond"
    assert it["quantity"] == 10
    assert it["purchase_price"] == 550.5
    assert it["figi"] == "BBG00QWERTY1"


def test_share_maps_to_stock():
    items = TBankService._positions_to_items([_pos(instrumentType="share", ticker="SBER")])
    assert items[0]["instrument_type"] == "stock"


def test_unsupported_type_skipped():
    """Валюта и фьючерсы в портфель бумаг не импортируются."""
    for bad in ("currency", "futures", "etf", ""):
        assert TBankService._positions_to_items([_pos(instrumentType=bad)]) == []


def test_zero_and_negative_quantity_skipped():
    for q in ({"units": "0", "nano": 0}, {"units": "-5", "nano": 0}):
        assert TBankService._positions_to_items([_pos(quantity=q)]) == []


def test_falls_back_to_current_price():
    """Нет средней цены — берём текущую, позицию не теряем."""
    items = TBankService._positions_to_items([
        _pos(averagePositionPrice={"units": "0", "nano": 0},
             currentPrice={"units": "600", "nano": 0})
    ])
    assert items[0]["purchase_price"] == 600.0


def test_position_without_any_price_skipped():
    items = TBankService._positions_to_items([
        _pos(averagePositionPrice={"units": "0", "nano": 0},
             currentPrice={"units": "0", "nano": 0})
    ])
    assert items == []


def test_position_without_ticker_skipped():
    assert TBankService._positions_to_items([_pos(ticker="")]) == []


def test_ticker_uppercased():
    items = TBankService._positions_to_items([_pos(ticker="sber")])
    assert items[0]["ticker"] == "SBER"


def test_mixed_batch_keeps_only_valid():
    positions = [
        _pos(),                                   # ок
        _pos(instrumentType="currency"),          # не тот тип
        _pos(ticker="", figi="BBG2"),             # нет тикера
        _pos(ticker="SBER", instrumentType="share"),  # ок
    ]
    items = TBankService._positions_to_items(positions)
    assert [i["ticker"] for i in items] == ["SU26238RMFS4", "SBER"]


# ── Операции → купоны и покупки ──────────────────────────────────────────────

def test_coupons_summed_per_figi():
    ops = [
        {"figi": "F1", "operation_type": "OPERATION_TYPE_COUPON", "payment": 35.4},
        {"figi": "F1", "operation_type": "OPERATION_TYPE_COUPON", "payment": 35.4},
        {"figi": "F2", "operation_type": "OPERATION_TYPE_COUPON", "payment": 10.0},
    ]
    agg = TBankService._operations_to_coupons_and_buys(ops)
    assert agg["F1"]["coupons"] == 70.8
    assert agg["F2"]["coupons"] == 10.0


def test_first_buy_is_earliest_date():
    ops = [
        {"figi": "F1", "operation_type": "OPERATION_TYPE_BUY", "date": "2026-05-10"},
        {"figi": "F1", "operation_type": "OPERATION_TYPE_BUY", "date": "2026-01-15"},
        {"figi": "F1", "operation_type": "OPERATION_TYPE_BUY_CARD", "date": "2026-03-01"},
    ]
    agg = TBankService._operations_to_coupons_and_buys(ops)
    assert agg["F1"]["first_buy"] == "2026-01-15"


def test_sell_tracked_with_latest_date():
    """has_sell отличает реальную продажу от позиции, пропавшей из сбойного ответа."""
    ops = [
        {"figi": "F1", "operation_type": "OPERATION_TYPE_SELL", "date": "2026-02-01"},
        {"figi": "F1", "operation_type": "OPERATION_TYPE_SELL_CARD", "date": "2026-06-01"},
    ]
    agg = TBankService._operations_to_coupons_and_buys(ops)
    assert agg["F1"]["has_sell"] is True
    assert agg["F1"]["last_sell"] == "2026-06-01"


def test_no_sell_means_flag_stays_false():
    ops = [{"figi": "F1", "operation_type": "OPERATION_TYPE_BUY", "date": "2026-01-01"}]
    agg = TBankService._operations_to_coupons_and_buys(ops)
    assert agg["F1"]["has_sell"] is False
    assert agg["F1"]["last_sell"] is None


def test_operations_without_figi_ignored():
    agg = TBankService._operations_to_coupons_and_buys(
        [{"operation_type": "OPERATION_TYPE_COUPON", "payment": 100.0}]
    )
    assert agg == {}


def test_unknown_operation_types_do_not_break_aggregation():
    ops = [
        {"figi": "F1", "operation_type": "OPERATION_TYPE_BROKER_FEE", "payment": -5.0},
        {"figi": "F1", "operation_type": "OPERATION_TYPE_COUPON", "payment": 20.0},
    ]
    agg = TBankService._operations_to_coupons_and_buys(ops)
    assert agg["F1"]["coupons"] == 20.0   # комиссия в купоны не попала


def test_coupons_rounded_to_kopecks():
    ops = [
        {"figi": "F1", "operation_type": "OPERATION_TYPE_COUPON", "payment": 0.111},
        {"figi": "F1", "operation_type": "OPERATION_TYPE_COUPON", "payment": 0.222},
    ]
    agg = TBankService._operations_to_coupons_and_buys(ops)
    assert agg["F1"]["coupons"] == 0.33


def test_empty_operations_list():
    assert TBankService._operations_to_coupons_and_buys([]) == {}
