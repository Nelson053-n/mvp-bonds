"""Чистая логика подбора бумаг в app/api/bonds.py.

Модуль был покрыт на 14%, хотя именно эти функции решают, какие бумаги попадут
пользователю в подборку и какой у них рейтинг. Ошибка здесь не падает, а тихо
искажает выдачу — поэтому проверяем границы каждого правила, а не только
happy path.
"""
from datetime import date

from app.api.bonds import (
    _MIN_ALLOWED_SCORE,
    _RATING_ORDER,
    _adjust_risk_for_amount,
    _coupon_frequency,
    _parse_date_safe,
    _rating_below_floor,
    _rating_from_listlevel,
    _rating_score,
    _static_rating,
)


# ── Частота купона ───────────────────────────────────────────────────────────

def test_coupon_frequency_by_period():
    assert _coupon_frequency(30) == 12    # месячные
    assert _coupon_frequency(91) == 4     # квартальные
    assert _coupon_frequency(182) == 2    # полугодовые
    assert _coupon_frequency(365) == 1    # годовые


def test_coupon_frequency_boundaries():
    """Границы веток: ровно на пороге и на шаг за ним."""
    assert _coupon_frequency(35) == 12
    assert _coupon_frequency(36) == 4
    assert _coupon_frequency(100) == 4
    assert _coupon_frequency(101) == 2
    assert _coupon_frequency(200) == 2
    assert _coupon_frequency(201) == 1


def test_coupon_frequency_invalid_falls_back_to_semiannual():
    """Нет данных о периоде — берём самое частое допущение, а не падаем."""
    for bad in (None, 0, -10):
        assert _coupon_frequency(bad) == 2


# ── Разбор дат ───────────────────────────────────────────────────────────────

def test_parse_date_safe():
    assert _parse_date_safe("2026-05-15") == date(2026, 5, 15)


def test_parse_date_safe_returns_none_on_garbage():
    """MOEX присылает пустые строки и мусор — это не повод падать."""
    for bad in (None, "", "0000-00-00", "15.05.2026", "не дата"):
        assert _parse_date_safe(bad) is None


# ── Рейтинги ─────────────────────────────────────────────────────────────────

def test_static_rating_by_prefix():
    assert _static_rating("SU26238RMFS4") == "AAA"   # ОФЗ
    assert _static_rating("SBER") == "AAA"
    assert _static_rating("sber") == "AAA"           # регистр не важен


def test_static_rating_unknown_issuer():
    assert _static_rating("RU000A0ZZZZ9") is None


def test_rating_from_listlevel():
    assert _rating_from_listlevel(1) == "AA"
    assert _rating_from_listlevel(2) == "BBB"
    assert _rating_from_listlevel(3) == "BB"


def test_rating_from_listlevel_handles_bad_input():
    for bad in (None, 0, 4, "х", []):
        assert _rating_from_listlevel(bad) is None


def test_rating_from_listlevel_accepts_numeric_string():
    """MOEX отдаёт LISTLEVEL строкой — конверсия должна отработать."""
    assert _rating_from_listlevel("1") == "AA"


def test_rating_score_orders_by_quality():
    """Меньше = лучше; шкала обязана быть строго возрастающей."""
    assert _rating_score("AAA") < _rating_score("AA") < _rating_score("BBB")
    assert _rating_score("AAA") == 0


def test_rating_score_strips_agency_suffix():
    """«AA-(RU)» — обычная форма записи у АКРА."""
    assert _rating_score("AA-(RU)") == _rating_score("AA-")
    assert _rating_score(" aa- ") == _rating_score("AA-")


def test_rating_score_unknown_is_neutral():
    for unknown in (None, "", "ZZZ", "не рейтинг"):
        assert _rating_score(unknown) == len(_RATING_ORDER)


# ── Отсечка по рейтингу ──────────────────────────────────────────────────────

def test_rating_below_floor():
    assert _rating_below_floor("B") is True
    assert _rating_below_floor("AAA") is False
    assert _rating_below_floor(_RATING_ORDER[_MIN_ALLOWED_SCORE]) is False


def test_unknown_rating_is_allowed_not_rejected():
    """Ключевая тонкость: неизвестный рейтинг ПРОПУСКАЕМ, а не режем.

    Иначе бумага без рейтинга (или с формой записи, которой нет в шкале)
    молча исчезала бы из подборки.
    """
    assert _rating_below_floor(None) is False
    assert _rating_below_floor("") is False
    assert _rating_below_floor("ZZZ") is False


def test_floor_boundary_is_exact():
    """На самом пороге бумага проходит, на шаг ниже — уже нет."""
    at_floor = _RATING_ORDER[_MIN_ALLOWED_SCORE]
    below = _RATING_ORDER[_MIN_ALLOWED_SCORE + 1]
    assert _rating_below_floor(at_floor) is False
    assert _rating_below_floor(below) is True


# ── Риск от суммы ────────────────────────────────────────────────────────────

def test_large_amount_lowers_risk():
    assert _adjust_risk_for_amount("high", 10_000_000) == "moderate"
    assert _adjust_risk_for_amount("elevated", 10_000_000) == "moderate"
    assert _adjust_risk_for_amount("high", 5_000_000) == "elevated"


def test_small_amount_keeps_risk():
    for risk in ("conservative", "moderate", "elevated", "high"):
        assert _adjust_risk_for_amount(risk, 100_000) == risk


def test_risk_adjust_boundaries():
    """Ровно на пороге правило уже действует, на рубль ниже — ещё нет."""
    assert _adjust_risk_for_amount("high", 4_999_999) == "high"
    assert _adjust_risk_for_amount("high", 5_000_000) == "elevated"
    assert _adjust_risk_for_amount("high", 9_999_999) == "elevated"
    assert _adjust_risk_for_amount("high", 10_000_000) == "moderate"


def test_conservative_never_upgraded_to_riskier():
    """Правило только снижает риск — повысить его сумма не может."""
    assert _adjust_risk_for_amount("conservative", 50_000_000) == "conservative"
    assert _adjust_risk_for_amount("moderate", 50_000_000) == "moderate"
