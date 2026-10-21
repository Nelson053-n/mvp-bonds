"""Тесты чистой математики ОФЗ (app/services/ofz_analytics.py) на живых
фикстурах MOEX от 21.10.2026 (tests/fixtures/moex/ofz_*.json, zcyc_2026-10-21.json)."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services import ofz_analytics as oa

FIX = Path(__file__).parent / "fixtures" / "moex"
SEC_26238 = "SU26238RMFS4"

# BONDTYPE MOEX -> kind (тот же маппинг должен делать T2)
KIND_BY_BONDTYPE = {
    "Облигация с фиксированным (известным) купоном": "fixed",
    "Облигация с плавающим купоном": "float",
    "Линкер/облигация с индексируемым номиналом": "linker",
    "Амортизируемая облигация": "amort",
    "Валютная облигация": "fx",
}


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nodes():
    return [tuple(n) for n in _load("zcyc_2026-10-21.json")["nodes"]]


@pytest.fixture(scope="module")
def window_rows():
    d = _load("ofz_days.json")
    cols = [c.lower() for c in d["columns"]]
    return [dict(zip(cols, r)) for rows in d["days"].values() for r in rows]


@pytest.fixture(scope="module")
def tqob():
    d = _load("ofz_tqob.json")
    return [dict(zip(d["columns"], r)) for r in d["data"]]


@pytest.fixture(scope="module")
def securities(tqob):
    return [{"secid": r["SECID"], "kind": KIND_BY_BONDTYPE[r["BONDTYPE"]],
             "faceunit": r["FACEUNIT"], "status": r["STATUS"]} for r in tqob]


# ---------------------------------------------------------------- кривая

def test_interp_on_nodes_and_between():
    n = [(1, 10.0), (3, 14.0), (5, 15.0)]
    assert oa.interp_curve(n, 1) == 10.0
    assert oa.interp_curve(n, 3) == 14.0
    assert oa.interp_curve(n, 5) == 15.0
    assert oa.interp_curve(n, 2) == pytest.approx(12.0)
    assert oa.interp_curve(n, 4) == pytest.approx(14.5)


def test_interp_no_extrapolation_and_unsorted():
    n = [(5, 15.0), (1, 10.0), (3, 14.0)]
    assert oa.interp_curve(n, 0.99) is None
    assert oa.interp_curve(n, 5.01) is None
    assert oa.interp_curve(n, 2) == pytest.approx(12.0)  # порядок узлов не важен
    assert oa.interp_curve([], 2) is None
    assert oa.interp_curve(n, None) is None


def test_spread_26238_on_real_curve(nodes, tqob):
    # 26238 за 2026-10-20: YIELDCLOSE 16.23, DURATION 2472 дн.; кривая от 21.10
    day = next(r for r in _load("ofz_days.json")["days"]["2026-10-20"] if r[0] == SEC_26238)
    s = oa.spread_bp(day[3], day[4], nodes)
    assert s == pytest.approx(-15.74, abs=0.05)
    assert -21 <= s <= -11  # ТЗ: ≈ −16 б.п. ±5


def test_spread_sign_and_order(nodes):
    # бумага выше кривой -> положительный спред; ниже -> отрицательный
    c = oa.interp_curve(nodes, 3.0)
    assert oa.spread_bp(c + 0.5, 3 * 365, nodes) == pytest.approx(50.0)
    assert oa.spread_bp(c - 0.5, 3 * 365, nodes) == pytest.approx(-50.0)


def test_spread_none_cases(nodes):
    assert oa.spread_bp(None, 1000, nodes) is None
    assert oa.spread_bp(0, 1000, nodes) is None       # ПК: YIELD=0
    assert oa.spread_bp(15, None, nodes) is None
    assert oa.spread_bp(15, 40 * 365, nodes) is None  # за пределами кривой
    assert oa.spread_bp(15, 1000, []) is None


def test_spread_series_skips_missing_days():
    nodes_a = [(1, 10.0), (5, 12.0)]
    curves = {"2026-01-12": nodes_a, "2026-01-13": nodes_a, "2026-01-15": nodes_a}
    rows = [
        {"tradedate": "2026-01-13", "yieldclose": 11.0, "duration": 365 * 3},
        {"tradedate": "2026-01-12", "yieldclose": 11.5, "duration": 365 * 3},
        {"tradedate": "2026-01-14", "yieldclose": 11.0, "duration": 365 * 3},  # нет кривой
        {"tradedate": "2026-01-15", "yieldclose": None, "duration": 365 * 3},  # нет доходности
        {"tradedate": "2026-01-16", "yieldclose": 11.0, "duration": 365 * 3},  # нет кривой
    ]
    out = oa.spread_series(rows, curves)
    assert [d for d, _ in out] == ["2026-01-12", "2026-01-13"]
    assert out[0][1] == pytest.approx(50.0)   # 11.5 - 11.0
    assert out[1][1] == pytest.approx(0.0)


def test_median_percentile():
    assert oa.median([]) is None
    assert oa.median([3, 1, 2]) == 2
    assert oa.median([1, 2, 3, 4]) == 2.5
    assert oa.percentile([0, 10], 25) == pytest.approx(2.5)
    assert oa.percentile([5], 90) == 5


# ---------------------------------------------------------------- дюрация

def test_freq_and_modified_duration():
    assert oa.freq_from_period(182) == 2
    assert oa.freq_from_period(91) == 4
    assert oa.freq_from_period(92) == 4
    assert oa.freq_from_period(365) == 1
    assert oa.freq_from_period(None) == 1
    assert oa.modified_duration(2 * 365, 10.0, 2) == pytest.approx(2 / 1.05)
    assert oa.modified_duration(None, 10.0, 2) is None


def _flows(settle, nextcoupon, matdate, coupon, period, face):
    """Потоки (t в годах от расчётов, сумма): купон каждые `period` дней
    начиная с NEXTCOUPON, в дату погашения плюс номинал."""
    d, m, out = date.fromisoformat(nextcoupon), date.fromisoformat(matdate), []
    while d <= m:
        out.append(((d - settle).days / 365.0, coupon + (face if d == m else 0.0)))
        d += timedelta(days=period)
    return out


@pytest.fixture(scope="module")
def flows_26238(tqob):
    r = next(x for x in tqob if x["SECID"] == SEC_26238)
    # постоянный купон, шаг 182 дн. попадает ровно в MATDATE (проверено)
    span = (date.fromisoformat(r["MATDATE"]) - date.fromisoformat(r["NEXTCOUPON"])).days
    assert span % 182 == 0
    # расчёты T+1 после торгов 20.10 = 22.10 (SETTLEDATE в securities)
    return _flows(date(2026, 10, 22), r["NEXTCOUPON"], r["MATDATE"],
                  r["COUPONVALUE"], r["COUPONPERIOD"], r["FACEVALUE"])


def test_macaulay_matches_moex_duration(flows_26238):
    # MOEX DURATION 26238 за 20.10 = 2472 дн.; доходность 16.23 эффективная
    d_days = oa.macaulay_from_cashflows(flows_26238, 16.23, freq=1) * 365
    assert abs(d_days / 2472 - 1) <= 0.03
    # на практике сходится значительно точнее допуска
    assert abs(d_days / 2472 - 1) <= 0.005


def test_cashflow_price_matches_market(flows_26238):
    # грязная цена = 51.524% * 1000 + НКД 27.43 = 542.67 ₽
    assert oa.price_from_cashflows(flows_26238, 16.23, freq=1) == pytest.approx(542.67, abs=0.5)


def test_convexity_positive_and_matches_numeric_second_derivative(flows_26238):
    y = 16.23
    c = oa.convexity_from_cashflows(flows_26238, y, freq=1)
    assert c > 0
    h = 0.01  # п.п.
    p0 = oa.price_from_cashflows(flows_26238, y)
    pu = oa.price_from_cashflows(flows_26238, y + h)
    pd = oa.price_from_cashflows(flows_26238, y - h)
    numeric = (pu - 2 * p0 + pd) / p0 / (h / 100) ** 2
    assert c == pytest.approx(numeric, rel=1e-3)
    # и для f=2 формулировка согласована со своим дисконтированием
    c2 = oa.convexity_from_cashflows(flows_26238, y, freq=2)
    p0 = oa.price_from_cashflows(flows_26238, y, 2)
    num2 = (oa.price_from_cashflows(flows_26238, y + h, 2) - 2 * p0
            + oa.price_from_cashflows(flows_26238, y - h, 2)) / p0 / (h / 100) ** 2
    assert c2 == pytest.approx(num2, rel=1e-3)


# ---------------------------------------------------------------- сценарии

def test_scenarios_linear_symmetry():
    rows = oa.price_scenarios(80.0, 1000.0, md=4.0, conv=None)
    by = {r["shift_bp"]: r for r in rows}
    assert [r["shift_bp"] for r in rows] == [-300, -200, -100, 100, 200, 300]
    assert all(r["linear_only"] for r in rows)
    assert by[100]["dp_pct"] == pytest.approx(-4.0)
    assert by[-100]["dp_pct"] == pytest.approx(4.0)
    for s in (100, 200, 300):
        assert by[s]["dp_pct"] == pytest.approx(-by[-s]["dp_pct"])
    assert by[100]["price_pct_new"] == pytest.approx(80.0 * 0.96)
    assert by[100]["price_rub_new"] == pytest.approx(768.0)
    assert by[100]["dp_rub"] == pytest.approx(-32.0)


def test_scenarios_convexity_asymmetry():
    rows = oa.price_scenarios(80.0, 1000.0, md=4.0, conv=60.0)
    by = {r["shift_bp"]: r for r in rows}
    assert not by[100]["linear_only"]
    for s in (100, 200, 300):
        assert by[-s]["dp_pct"] > -by[s]["dp_pct"] > 0  # рост при падении ставки больше
    # ½·C·Δy² при +100 б.п.: -4% + 0.5*60*0.0001*100 = -3.7%
    assert by[100]["dp_pct"] == pytest.approx(-3.7)
    assert by[-100]["dp_pct"] == pytest.approx(4.3)


def test_scenarios_aci_moves_clean_price_by_dirty_change():
    # НКД не зависит от доходности: котировка меняется на dirty*dp
    r = oa.price_scenarios(50.0, 1000.0, md=4.0, conv=None, shifts_bp=(100,), aci_pct=10.0)[0]
    assert r["dp_pct"] == pytest.approx(-4.0)
    assert r["price_pct_new"] == pytest.approx(50.0 - 60.0 * 0.04)
    assert oa.price_scenarios(50.0, 1000.0, md=None, conv=None) == []


# ---------------------------------------------------------------- ряды

def test_range_stats():
    s = [("2025-01-01", 1.0), ("2025-12-01", 5.0), ("2026-01-10", 3.0), ("2026-02-01", 3.5)]
    r = oa.range_stats(s, window_days=365)
    # 2025-01-01 старше 365 дней от 2026-02-01 -> в окно не входит
    assert r["min"] == 3.0 and r["min_date"] == "2026-01-10"
    assert r["max"] == 5.0 and r["max_date"] == "2025-12-01"
    assert r["last"] == 3.5
    assert r["pos"] == pytest.approx(0.25)  # (3.5-3)/(5-3)
    assert oa.range_stats([]) is None
    flat = oa.range_stats([("2026-01-01", 2.0), ("2026-01-02", 2.0)])
    assert flat["pos"] == 0.5


def test_weekly_downsample():
    s = [("2026-10-05", 1.0), ("2026-10-06", 2.0), ("2026-10-09", 3.0),   # пн-пт одной недели
         ("2026-10-12", 4.0), ("2026-10-14", 5.0)]
    assert oa.weekly_downsample(s) == [("2026-10-09", 3.0), ("2026-10-14", 5.0)]
    assert oa.weekly_downsample([]) == []


# ---------------------------------------------------------------- отбор

def test_select_ofz_real_fixtures(securities, window_rows):
    assert len({r["tradedate"] for r in window_rows}) == 20
    sel = oa.select_ofz(securities, window_rows)
    kind = {s["secid"]: s["kind"] for s in securities}
    assert len(sel) == 50 and len(set(sel)) == 50
    counts = {}
    for sid in sel:
        counts[kind[sid]] = counts.get(kind[sid], 0) + 1
    assert counts == {"fixed": 30, "linker": 4, "amort": 2, "float": 14}
    # конкретные границы спеки §5
    assert any("46012" in s for s in sel)  # проходит порог медианы 27 >= 20
    assert not any(kind[s] == "fx" for s in sel)
    assert not any("26231" in s for s in sel)
    # порядок: группы идут fixed, linker, amort, float
    order = [kind[s] for s in sel]
    assert order == sorted(order, key=lambda k: ("fixed", "linker", "amort", "float").index(k))
    # стабильность
    assert sel == oa.select_ofz(securities, list(reversed(window_rows)))


def _mk(secid, kind, trades, value=1e6, faceunit="SUR", status="A", days=20):
    sec = {"secid": secid, "kind": kind, "faceunit": faceunit, "status": status}
    rows = [{"secid": secid, "tradedate": f"2026-09-{d:02d}", "numtrades": trades,
             "value": value} for d in range(1, days + 1)]
    return sec, rows


def _dates_rows():
    # «якорная» бумага задаёт 20 торговых дат окна
    return _mk("ANCHOR", "fixed", 100)


def test_select_min_days_threshold():
    anchor, arows = _dates_rows()
    ok, okrows = _mk("OK", "fixed", 50)
    # 15 дней из 20 с торгами проходит, 14 — нет
    sec15 = {"secid": "D15", "kind": "fixed", "faceunit": "SUR", "status": "A"}
    sec14 = dict(sec15, secid="D14")
    r15 = [{"secid": "D15", "tradedate": f"2026-09-{d:02d}", "numtrades": 50, "value": 1} for d in range(1, 16)]
    r14 = [{"secid": "D14", "tradedate": f"2026-09-{d:02d}", "numtrades": 50, "value": 1} for d in range(1, 15)]
    # медиана по 20 дням для 15 дней торгов = 50 → если бы не было min_days, оба прошли бы бы по медиане
    # (15 нулей + ... ): 15 из 20 -> медиана 50 (сортировка: 5 нулей, 15 по 50 -> 10-й и 11-й = 50)
    sel = oa.select_ofz([anchor, sec15, sec14], arows + r15 + r14)
    assert "D15" in sel and "D14" not in sel


def test_select_filters_fx_faceunit_status_median():
    anchor, arows = _dates_rows()
    cases = [
        _mk("FX", "fx", 100),                       # kind fx
        _mk("CNY", "fixed", 100, faceunit="CNY"),   # не рублёвая
        _mk("DEAD", "fixed", 100, status="N"),      # не торгуется
        _mk("THIN", "fixed", 19),                   # медиана 19 < 20
        _mk("THICK", "fixed", 20),                  # ровно порог — проходит
    ]
    secs = [anchor] + [c[0] for c in cases]
    rows = arows + [r for c in cases for r in c[1]]
    sel = oa.select_ofz(secs, rows)
    assert set(sel) == {"ANCHOR", "THICK"}
    # fx не проходит ИМЕННО из-за kind: faceunit/status/сделки у него в порядке
    fx_only = oa.select_ofz([anchor, cases[0][0]], arows + cases[0][1], min_median_trades=1)
    assert "FX" not in fx_only


def test_select_floats_fill_remainder_by_turnover_and_mandatory_all():
    anchor, arows = _dates_rows()
    secs, rows = [anchor], list(arows)
    for i, v in enumerate((5e6, 9e6, 1e6)):
        s, r = _mk(f"FL{i}", "float", 50, value=v)
        secs.append(s); rows += r
    s, r = _mk("LIN", "linker", 30, value=1.0)
    secs.append(s); rows += r
    sel = oa.select_ofz(secs, rows, n=4)
    # обязательные (ANCHOR fixed, LIN linker) взяты несмотря на малый оборот,
    # остаток 2 места — два самых оборотистых float
    assert sel == ["ANCHOR", "LIN", "FL1", "FL0"]


def test_liquidity_rank(window_rows, securities):
    place, total, med = oa.liquidity_rank(SEC_26238, window_rows)
    assert 1 <= place <= total
    assert med > 1e8
    rub = [s["secid"] for s in securities if s["faceunit"] == "SUR"]
    p2, t2, m2 = oa.liquidity_rank(SEC_26238, window_rows, universe=rub)
    assert t2 < total and t2 <= len(rub)  # CNY-выпуски из знаменателя ушли
    assert p2 <= place and m2 == med
    # неизвестная бумага: оборот 0, место после всех, у кого оборот > 0
    pl, tot, m = oa.liquidity_rank("NOPE", window_rows)
    assert m == 0.0 and pl > place and pl <= tot


# ---------------------------------------------------------------- соседи

def test_neighbors_real_board(window_rows):
    last = [r for r in window_rows if r["tradedate"] == "2026-10-20"]
    nb = oa.neighbors(SEC_26238, last, k=4)
    assert len(nb) == 4
    assert SEC_26238 not in [r["secid"] for r in nb]
    d = [r["distance_days"] for r in nb]
    assert d == sorted(d)
    me = next(r for r in last if r["secid"] == SEC_26238)["duration"]
    # ближайший действительно ближайший среди всех с доходностью
    best = min(abs(r["duration"] - me) for r in last
               if r["secid"] != SEC_26238 and r["duration"] and r["yieldclose"])
    assert d[0] == best
    assert all(r["yieldclose"] for r in nb)


def test_neighbors_last_day_kind_filter():
    rows = [
        {"secid": "A", "tradedate": "2026-10-19", "duration": 1000, "yieldclose": 10, "kind": "fixed"},
        {"secid": "B", "tradedate": "2026-10-19", "duration": 1001, "yieldclose": 10, "kind": "fixed"},
        {"secid": "A", "tradedate": "2026-10-20", "duration": 1000, "yieldclose": 10, "kind": "fixed"},
        {"secid": "C", "tradedate": "2026-10-20", "duration": 1300, "yieldclose": 10, "kind": "fixed"},
        {"secid": "D", "tradedate": "2026-10-20", "duration": 900, "yieldclose": 10, "kind": "linker"},
        {"secid": "E", "tradedate": "2026-10-20", "duration": 1010, "yieldclose": 0, "kind": "fixed"},
        {"secid": "F", "tradedate": "2026-10-20", "duration": None, "yieldclose": 10, "kind": "fixed"},
    ]
    assert [r["secid"] for r in oa.neighbors("A", rows)] == ["D", "C"]  # B — вчерашний, E/F без данных
    assert [r["secid"] for r in oa.neighbors("A", rows, kind_filter="fixed")] == ["C"]
    assert [r["secid"] for r in oa.neighbors("A", rows, k=1)] == ["D"]
    assert oa.neighbors("Z", rows) == []
