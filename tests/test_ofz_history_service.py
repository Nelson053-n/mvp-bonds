"""Загрузчики MOEX для страниц ОФЗ: фикстуры живых ответов 21.10.2026, без сети."""

import collections
import json
import logging
import pathlib
import re

import httpx
import pytest

import app.services.moex_service as m
from app.exceptions import DataFetchError, ValidationError
from app.services import ofz_history_service as svc

FIX = pathlib.Path(__file__).parent / "fixtures" / "moex"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _patch(monkeypatch, router):
    """router(url) -> payload | Exception. Возвращает список запрошенных URL."""
    urls = []

    class _Client:
        async def get(self, url, **kw):
            urls.append(url)
            item = router(url)
            if isinstance(item, Exception):
                raise item
            return _Resp(item)

    monkeypatch.setattr(m.MOEXService, "_get_http", lambda self: _Client())
    monkeypatch.setattr(m, "_RETRY_DELAYS", (0, 0))
    return urls


# --- доска за день ---------------------------------------------------------

async def test_board_day_row_26238(monkeypatch):
    urls = _patch(monkeypatch, lambda u: _load("iss_board_2026-10-20.json"))
    rows = await svc.fetch_board_day("2026-10-20")
    assert len(rows) == 61
    assert "boards/TQOB/securities.json" in urls[0] and "date=2026-10-20" in urls[0]
    r = next(x for x in rows if x["secid"] == "SU26238RMFS4")
    assert r["tradedate"] == "2026-10-20"
    assert r["close"] == 51.524
    assert r["yieldclose"] == 16.23
    assert r["duration"] == 2472
    assert isinstance(r["numtrades"], int)
    assert set(r) == {
        "secid", "tradedate", "close", "yieldclose", "duration", "numtrades",
        "value", "zspread", "facevalue", "iricpiclose", "beiclose",
    }


async def test_board_day_linker_and_unpriced(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_board_2026-10-20.json"))
    rows = {r["secid"]: r for r in await svc.fetch_board_day("2026-10-20")}
    lk = rows["SU52002RMFS1"]
    assert lk["facevalue"] == 1778.41 and lk["iricpiclose"] == 5.09 and lk["beiclose"] == 5.32
    # 26231: размещена, торгов нет — цены None, а не 0
    new = rows["SU26231RMFS9"]
    assert new["close"] is None and new["yieldclose"] is None and new["numtrades"] == 0
    # ПД не имеют индексных полей
    assert rows["SU26238RMFS4"]["iricpiclose"] is None


async def test_board_day_weekend_is_empty_not_error(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_board_2026-10-17_sat.json"))
    assert await svc.fetch_board_day("2026-10-17") == []


async def test_board_day_rejects_bad_date(monkeypatch):
    urls = _patch(monkeypatch, lambda u: {})
    with pytest.raises(ValidationError):
        await svc.fetch_board_day("2026-10-20&x=1")
    assert urls == []


async def test_moex_failure_propagates_typed(monkeypatch):
    _patch(monkeypatch, lambda u: httpx.ReadTimeout(""))
    with pytest.raises(DataFetchError):
        await svc.fetch_board_day("2026-10-20")


async def test_transient_error_retried(monkeypatch):
    seq = [httpx.PoolTimeout(""), _load("iss_board_2026-10-20.json")]
    _patch(monkeypatch, lambda u: seq.pop(0))
    assert len(await svc.fetch_board_day("2026-10-20")) == 61


# --- история по бумаге -----------------------------------------------------

def _paged_router():
    pages = {s: _load(f"iss_hist_26238_start{s}.json") for s in (0, 100, 200)}

    def route(url):
        start = int(re.search(r"[?&]start=(\d+)", url).group(1))
        return pages[start]

    return route


async def test_secid_history_paginates_all_pages(monkeypatch):
    urls = _patch(monkeypatch, _paged_router())
    rows = await svc.fetch_secid_history("SU26238RMFS4", "2025-10-21")
    assert len(rows) == 254  # TOTAL из history.cursor: 100 + 100 + 54
    assert [re.search(r"start=(\d+)", u).group(1) for u in urls] == ["0", "100", "200"]
    assert all("from=2025-10-21" in u for u in urls)
    assert rows[0]["tradedate"] == "2025-10-21" and rows[0]["close"] == 56.712
    assert rows[100]["tradedate"] == "2026-03-17"  # первая строка второго листа
    assert rows[200]["tradedate"] == "2026-08-06"  # первая строка третьего листа
    assert all(r["secid"] == "SU26238RMFS4" for r in rows)
    dates = [r["tradedate"] for r in rows]
    assert dates == sorted(set(dates))  # без дублей и в порядке


async def test_secid_history_single_short_page_stops(monkeypatch):
    urls = _patch(monkeypatch, lambda u: _load("iss_hist_26238_start200.json"))
    rows = await svc.fetch_secid_history("SU26238RMFS4", "2026-08-06")
    assert len(rows) == 54 and len(urls) == 1


async def test_secid_history_empty(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_board_2026-10-17_sat.json"))
    assert await svc.fetch_secid_history("SU26238RMFS4", "2026-10-17") == []


async def test_secid_history_rejects_bad_secid(monkeypatch):
    _patch(monkeypatch, lambda u: {})
    with pytest.raises(ValidationError):
        await svc.fetch_secid_history("A/../x", "2025-10-21")


# --- кривая ----------------------------------------------------------------

async def test_zcyc_day_nodes(monkeypatch):
    urls = _patch(monkeypatch, lambda u: _load("iss_zcyc_2026-10-21.json"))
    nodes = await svc.fetch_zcyc_day("2026-10-21")
    assert len(nodes) == 11
    assert nodes[0] == (0.25, 12.634) and nodes[-1] == (20.0, 16.4327)
    assert (7.0, 16.4066) in nodes
    assert "/engines/stock/zcyc.json" in urls[0] and "/history/" not in urls[0]
    assert "date=2026-10-21" in urls[0]


async def test_zcyc_weekend_empty(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_zcyc_empty.json"))
    assert await svc.fetch_zcyc_day("2026-09-26") == []


# --- справочник TQOB -------------------------------------------------------

async def _securities(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_tqob_securities.json"))
    return await svc.fetch_tqob_securities()


async def test_tqob_kind_counts(monkeypatch):
    secs = await _securities(monkeypatch)
    assert len(secs) == 61
    assert collections.Counter(s["kind"] for s in secs) == {
        "fixed": 31, "float": 21, "linker": 4, "amort": 2, "fx": 3,
    }


async def test_tqob_kind_examples_and_fields(monkeypatch):
    secs = {s["secid"]: s for s in await _securities(monkeypatch)}
    assert secs["SU26238RMFS4"]["kind"] == "fixed"
    assert secs["SU29016RMFS1"]["kind"] == "float"
    assert secs["SU52002RMFS1"]["kind"] == "linker"
    assert {secs["SU46012RMFS9"]["kind"], secs["SU46020RMFS2"]["kind"]} == {"amort"}
    assert {secs[k]["kind"] for k in ("RU000A10DQA8", "RU000A10DQB6", "RU000A10FAK6")} == {"fx"}
    s = secs["SU29016RMFS1"]
    assert s["matdate"] == "2026-12-23" and s["couponvalue"] == 0 and s["couponperiod"] == 91
    assert s["isin"] == "RU000A1025B5" and s["regnumber"] == "29016RMFS"
    assert set(s) == set(svc._SECURITIES_KEYS) | {"kind"}


@pytest.mark.parametrize("bondtype,unit,kind", [
    ("Облигация с плавающим купоном", "SUR", "float"),
    ("Облигация с фиксированным (известным) купоном", "SUR", "fixed"),
    ("Облигация с фиксированным (известным) купоном", "CNY", "fx"),
    ("Что-то новое", "SUR", "other"),
    (None, None, "other"),
])
def test_classify_kind(bondtype, unit, kind):
    assert svc.classify_kind(bondtype, unit) == kind


def test_ofz_number():
    assert svc.ofz_number("SU26238RMFS4") == "26238"
    assert svc.ofz_number("SU52002RMFS1") == "52002"
    assert svc.ofz_number("RU000A10DQA8") is None
    assert svc.ofz_number("SU26238RMFS4X") is None
    assert svc.ofz_number("SU2623RMFS4") is None
    assert svc.ofz_number("") is None and svc.ofz_number(None) is None


async def test_secid_by_number(monkeypatch):
    secs = await _securities(monkeypatch)
    assert svc.secid_by_number("26238", secs) == "SU26238RMFS4"
    assert svc.secid_by_number("46012", secs) == "SU46012RMFS9"
    assert svc.secid_by_number("99999", secs) is None
    # юаневые не получают номера
    assert sum(1 for s in secs if svc.ofz_number(s["secid"])) == 58


# --- description -----------------------------------------------------------

async def test_description_linker(monkeypatch):
    urls = _patch(monkeypatch, lambda u: _load("iss_desc52002.json"))
    d = await svc.fetch_description("SU52002RMFS1")
    assert "/securities/SU52002RMFS1/description.json" in urls[0]
    assert d["bond_type"] == "Линкер/облигация с индексируемым номиналом"
    assert d["facevalue_type"] == "Индексированный"
    assert d["initialfacevalue"] == 1000.0 and d["facevalue"] == 1778.72
    assert d["issuedate"] == "2018-03-21" and d["matdate"] == "2028-02-02"
    assert d["issuesize"] == 250000000 and d["couponfrequency"] == 2
    assert d["coupon_benchmark"] is None and d["amortbond"] is None


async def test_description_floater_benchmark(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_desc29016.json"))
    d = await svc.fetch_description("SU29016RMFS1")
    assert d["coupon_benchmark"] == "RUONIA"
    assert d["coupon_benchmark_spread"] == 0.0  # 0.00 — это маржа ноль, не «нет данных»
    assert d["couponpercent"] is None


async def test_description_amortizing(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_desc46020.json"))
    d = await svc.fetch_description("SU46020RMFS2")
    assert d["amortbond"] is True
    assert d["couponpercent"] == 6.9 and d["bond_type"] == "Амортизируемая облигация"


# --- bondization -----------------------------------------------------------

async def test_bondization_floater_future_coupon_is_none(monkeypatch):
    _patch(monkeypatch, lambda u: _load("iss_bondiz29016.json"))
    b = await svc.fetch_bondization("SU29016RMFS1")
    assert len(b["coupons"]) == 25
    last = b["coupons"][-1]
    assert last["date"] == "2026-12-23"
    assert last["value"] is None and last["valueprc"] is None  # не 0
    prev = b["coupons"][-2]
    assert prev["date"] == "2026-09-23" and prev["value"] == 34.99 and prev["valueprc"] == 14.03
    # погашение ПК — одна «амортизация» 100% номинала
    assert b["amortizations"] == [
        {"date": "2026-12-23", "value": 1000.0, "valueprc": 100.0, "kind": "maturity"}
    ]


async def test_bondization_amortizing(monkeypatch):
    urls = _patch(monkeypatch, lambda u: _load("iss_bondiz46020.json"))
    b = await svc.fetch_bondization("SU46020RMFS2")
    assert "SU46020RMFS2/bondization.json" in urls[0]
    assert [a["date"] for a in b["amortizations"]] == [
        "2034-08-09", "2035-02-07", "2035-08-08", "2036-02-06"]
    assert all(a["valueprc"] == 25 and a["value"] == 250 for a in b["amortizations"])
    assert len(b["coupons"]) == 60
    assert b["coupons"][-1]["value"] == 8.6


async def test_bondization_truncation_warns(monkeypatch, caplog):
    payload = _load("iss_bondiz46020.json")
    payload["coupons.cursor"]["data"] = [[0, 150, 100]]
    _patch(monkeypatch, lambda u: payload)
    with caplog.at_level(logging.WARNING, logger=svc.logger.name):
        await svc.fetch_bondization("SU46020RMFS2")
    assert any("150" in r.getMessage() for r in caplog.records)
