"""Определение бумаги с плавающим купоном по BONDTYPE.

14.09.2026, Селигдар9Р (RU000A10DTA2): в портфеле показывался купон 0 ₽, хотя
бумага торгуется и НКД начисляется. MOEX по ней отдаёт COUPONVALUE=0 и
COUPONPERCENT=None — ставка объявляется отдельно, а история купонов лежит в
bondization.

Фолбэк на bondization в коде был, но не запускался: он требовал
BONDTYPE, содержащего «флоатер» или «float», а MOEX пишет РУССКОЙ ФРАЗОЙ
«Облигация с плавающим купоном». Ни одна подстрока не совпадала.

Замер на проде в день инцидента: из 776 уникальных облигаций в портфелях
пользователей 163 показывали нулевой купон. Фактические значения BONDTYPE на
TQCB+TQOB — в _MOEX_BOND_TYPES ниже, они взяты из живого ответа биржи.
"""
import pytest

from app.services.moex_service import MOEXService

# Реальные значения поля BONDTYPE (ответ MOEX 14.09.2026, TQCB+TQOB).
_MOEX_BOND_TYPES = {
    "Облигация с фиксированным (известным) купоном": 1001,
    "Облигация с плавающим купоном": 620,
    "Структурная облигация": 496,
    "Амортизируемая облигация": 470,
    "Облигация с фиксированным (неизвестным) купоном": 267,
    "Валютная облигация": 204,
    "Линкер/облигация с индексируемым номиналом": 13,
    "Конвертируемая облигация": 2,
    "Дисконтная облигация": 2,
}


def _snapshot_fetch(bond_type: str, coupon_value, coupon_percent):
    """Ответ MOEX по одной бумаге с заданным типом и купоном."""
    async def fake_fetch(url, source="moex_price"):
        return {
            "securities": {
                "columns": [
                    "SECID", "BOARDID", "SHORTNAME", "FACEVALUE", "FACEUNIT",
                    "COUPONVALUE", "COUPONPERCENT", "COUPONPERIOD", "NEXTCOUPON",
                    "ACCRUEDINT", "PREVPRICE", "MATDATE", "BONDTYPE", "LISTLEVEL",
                ],
                "data": [[
                    "RU000TEST", "TQCB", "Тест", 1000.0, "SUR",
                    coupon_value, coupon_percent, 30, "2026-10-12",
                    1.52, 101.33, "2027-12-06", bond_type, 2,
                ]],
            },
            "marketdata": {
                "columns": ["SECID", "BOARDID", "LAST", "LCLOSE", "YIELD"],
                "data": [["RU000TEST", "TQCB", 101.33, None, 18.18]],
            },
        }
    return fake_fetch


@pytest.fixture
def svc(monkeypatch):
    s = MOEXService()
    async def none_(*a, **kw):
        return None
    async def meta(secid):
        return (False, True)
    monkeypatch.setattr(s, "_get_smartlab_credit_rating", none_)
    monkeypatch.setattr(s, "_get_credit_rating", none_)
    monkeypatch.setattr(s, "_get_sec_meta", meta)
    return s


# ── Распознавание типа ───────────────────────────────────────────────────────

@pytest.mark.parametrize("bond_type", [
    "Облигация с плавающим купоном",   # реальный тип Селигдар9Р — 620 бумаг
    "облигация с плавающим купоном",   # регистр не должен влиять
    "Флоатер",                          # прежняя форма, ломать её нельзя
    "Floater",
])
async def test_floating_coupon_types_detected(svc, monkeypatch, bond_type):
    """Все формулировки «плавающего купона» должны включать фолбэк."""
    monkeypatch.setattr(svc, "_fetch", _snapshot_fetch(bond_type, 0, None))

    async def last_coupon(secid):
        return {"value": 15.21, "period_days": 30}

    monkeypatch.setattr(svc, "get_last_known_coupon", last_coupon)
    snap = await svc.get_bond_snapshot("RU000TEST")
    assert snap.is_floater is True
    assert snap.coupon == 15.21, "купон обязан подтянуться из bondization"


async def test_fixed_coupon_bond_is_not_floater(svc, monkeypatch):
    """Обычная бумага с известным купоном флоатером не считается."""
    monkeypatch.setattr(
        svc, "_fetch",
        _snapshot_fetch("Облигация с фиксированным (известным) купоном", 35.4, 7.1),
    )
    snap = await svc.get_bond_snapshot("RU000TEST")
    assert snap.is_floater is False
    assert snap.coupon == 35.4, "объявленный купон не должен подменяться"


# ── Фолбэк на bondization ────────────────────────────────────────────────────

async def test_fallback_runs_for_any_type_with_unknown_coupon(svc, monkeypatch):
    """Купон неизвестен — тянем из bondization независимо от типа бумаги.

    Нулевой купон встречается не только у флоатеров: на проде это были ещё
    структурные, амортизируемые и «фиксированные (неизвестные)». Критерий —
    отсутствие купона у торгуемой бумаги, а не её тип.
    """
    monkeypatch.setattr(svc, "_fetch", _snapshot_fetch("Структурная облигация", 0, None))
    called = {"n": 0}

    async def last_coupon(secid):
        called["n"] += 1
        return {"value": 12.5, "period_days": 30}

    monkeypatch.setattr(svc, "get_last_known_coupon", last_coupon)
    snap = await svc.get_bond_snapshot("RU000TEST")
    assert called["n"] == 1, "фолбэк обязан сработать и для не-флоатера"
    assert snap.coupon == 12.5


async def test_no_fallback_when_coupon_known(svc, monkeypatch):
    """Купон объявлен — лишнего запроса в bondization быть не должно."""
    monkeypatch.setattr(
        svc, "_fetch",
        _snapshot_fetch("Облигация с плавающим купоном", 35.4, 7.1),
    )
    called = {"n": 0}

    async def last_coupon(secid):
        called["n"] += 1
        return {"value": 99.0, "period_days": 30}

    monkeypatch.setattr(svc, "get_last_known_coupon", last_coupon)
    snap = await svc.get_bond_snapshot("RU000TEST")
    assert called["n"] == 0
    assert snap.coupon == 35.4


async def test_rate_derived_from_coupon_and_period(svc, monkeypatch):
    """Ставка считается из купона и периода: 15.21/1000 × 365/30 ≈ 18.51%.

    Сверено с продом по трём источникам: последний купон в bondization
    (12.09.2026 = 15.21 ₽), НКД 1.52 ₽ за 3 дня и YIELD биржи 18.18%.
    """
    monkeypatch.setattr(
        svc, "_fetch",
        _snapshot_fetch("Облигация с плавающим купоном", 0, None),
    )

    async def last_coupon(secid):
        return {"value": 15.21, "period_days": 30}

    monkeypatch.setattr(svc, "get_last_known_coupon", last_coupon)
    snap = await svc.get_bond_snapshot("RU000TEST")
    assert snap.coupon_rate == pytest.approx(18.5055, abs=0.01)


async def test_missing_bondization_leaves_zero_without_crash(svc, monkeypatch):
    """Истории купонов нет — отдаём как есть, но не падаем."""
    monkeypatch.setattr(
        svc, "_fetch",
        _snapshot_fetch("Облигация с плавающим купоном", 0, None),
    )

    async def no_coupon(secid):
        return None

    monkeypatch.setattr(svc, "get_last_known_coupon", no_coupon)
    snap = await svc.get_bond_snapshot("RU000TEST")
    assert snap.coupon in (0, 0.0, None)
    assert snap.is_floater is True


# ── Защита от повторения ─────────────────────────────────────────────────────

def test_every_known_moex_bond_type_is_classified():
    """Реальные значения BONDTYPE не должны ломать классификацию.

    Тест фиксирует ФАКТИЧЕСКИЕ строки биржи: если MOEX снова сменит
    формулировку, а мы будем искать старую подстроку, это всплывёт здесь,
    а не в портфеле пользователя.
    """
    floating = {
        t for t in _MOEX_BOND_TYPES
        if "плавающ" in t.lower() or "флоатер" in t.lower() or "float" in t.lower()
    }
    assert "Облигация с плавающим купоном" in floating, (
        "основной тип флоатера на MOEX обязан распознаваться"
    )
    assert "Облигация с фиксированным (известным) купоном" not in floating


# ── Кэш bondization ──────────────────────────────────────────────────────────

async def test_last_coupon_is_cached(monkeypatch):
    """Повторный запрос купона не должен ходить в MOEX.

    Фолбэк теперь запускается для ЛЮБОЙ бумаги с нулевым купоном, а таких в
    одном портфеле на проде оказалось 126 из 232. Без кэша это +126 запросов
    к MOEX на каждое обновление (раз в 15 минут) по источнику, который и так
    периодически отдаёт 502.
    """
    svc = MOEXService()
    calls = {"n": 0}

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"coupons": {
                "columns": ["value", "startdate", "coupondate"],
                "data": [[15.21, "2026-08-13", "2026-09-12"]],
            }}

    class _Client:
        @staticmethod
        async def get(url, **kw):
            calls["n"] += 1
            return _Resp()

    monkeypatch.setattr(svc, "_get_http", lambda: _Client())

    first = await svc.get_last_known_coupon("RU000TEST")
    second = await svc.get_last_known_coupon("RU000TEST")
    assert first == second
    assert first["value"] == 15.21
    assert calls["n"] == 1, "второй вызов обязан читаться из кэша"


async def test_missing_history_cached_too(monkeypatch):
    """Отсутствие истории тоже кэшируется — иначе такие бумаги бьют по MOEX."""
    svc = MOEXService()
    calls = {"n": 0}

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"coupons": {"columns": [], "data": []}}

    class _Client:
        @staticmethod
        async def get(url, **kw):
            calls["n"] += 1
            return _Resp()

    monkeypatch.setattr(svc, "_get_http", lambda: _Client())

    assert await svc.get_last_known_coupon("RU000NONE") is None
    assert await svc.get_last_known_coupon("RU000NONE") is None
    assert calls["n"] == 1


async def test_network_failure_not_cached(monkeypatch):
    """Сетевой сбой НЕ кэшируем: MOEX моргает, бумага не должна залипать без купона."""
    svc = MOEXService()
    calls = {"n": 0}

    class _Client:
        @staticmethod
        async def get(url, **kw):
            calls["n"] += 1
            raise RuntimeError("MOEX 502")

    monkeypatch.setattr(svc, "_get_http", lambda: _Client())

    assert await svc.get_last_known_coupon("RU000FLAP") is None
    assert await svc.get_last_known_coupon("RU000FLAP") is None
    assert calls["n"] == 2, "после сбоя следующий вызов обязан повторить запрос"
