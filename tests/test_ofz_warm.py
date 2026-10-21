"""Фон ОФЗ (app/services/ofz_warm.py): дозагрузка, backfill, отбор 50, прогрев.

Сети нет: функции ofz_history_service подменены, время — через _now/_monotonic.
"""
import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.api as api_pkg
from app.config import settings
from app.services import ofz_history_service as hist
from app.services import ofz_warm as ow
from app.services.storage_service import StorageService

FIX = Path(__file__).parent / "fixtures" / "moex"


def _utc(y, m, d, h=12, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = _utc(2026, 10, 21, 12)   # среда; «вчера по Москве» = 2026-10-20
        self.mono = 1000.0


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ow, "_now", lambda: c.now)
    monkeypatch.setattr(ow, "_monotonic", lambda: c.mono)
    return c


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sqlite_db_path", str(tmp_path / "ofz.db"))
    s = StorageService()
    monkeypatch.setattr(ow, "storage_service", s)
    return s


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ow.reset_caches()
    monkeypatch.setattr(ow, "_backfill_running", False)
    yield
    ow.reset_caches()


@pytest.fixture
def no_sleep(monkeypatch):
    pauses = []

    async def fake(sec):
        pauses.append(sec)
    monkeypatch.setattr(ow, "_sleep", fake)
    return pauses


class Net:
    """Фальшивый MOEX: торговые дни → строки доски; остальные пусты (выходной)."""

    def __init__(self, monkeypatch, trading_days=None, fail_on=()):
        self.trading = trading_days  # None = все дни торговые
        self.fail_on = set(fail_on)
        self.board_calls = []
        self.zcyc_calls = []
        monkeypatch.setattr(hist, "fetch_board_day", self.board)
        monkeypatch.setattr(hist, "fetch_zcyc_day", self.zcyc)

    def _is_trading(self, day):
        if self.trading is not None:
            return day in self.trading
        return datetime.fromisoformat(day).weekday() < 5

    async def board(self, day):
        self.board_calls.append(day)
        if day in self.fail_on:
            raise RuntimeError("moex down")
        if not self._is_trading(day):
            return []
        return [{"secid": "SU26238RMFS4", "tradedate": day, "close": 70.0,
                 "yieldclose": 16.0, "duration": 2400.0, "numtrades": 50, "value": 1e7},
                {"secid": "SU26243RMFS4", "tradedate": day, "close": 80.0,
                 "yieldclose": 15.0, "duration": 1500.0, "numtrades": 40, "value": 1e7}]

    async def zcyc(self, day):
        self.zcyc_calls.append(day)
        return [(1.0, 14.0), (5.0, 16.0)] if self._is_trading(day) else []


def _seed(db, day="2026-10-14"):
    db.upsert_ofz_daily([{"secid": "SU26238RMFS4", "tradedate": day, "numtrades": 5}])


# ------------------------------------------------------------ run_daily_update

async def test_daily_update_loads_exactly_missed_dates_skips_weekends_and_today(db, clock, monkeypatch):
    _seed(db, "2026-10-14")           # ср
    clock.now = _utc(2026, 10, 20, 12)  # вторник, вчера = пн 19-е
    net = Net(monkeypatch)
    stats = await ow.run_daily_update()
    # 15 чт, 16 пт, 17 сб, 18 вс, 19 пн; 20-е (сегодня) не берём
    assert net.board_calls == ["2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"]
    assert stats == {"dates_loaded": 3, "rows": 6, "curve_nodes": 6}
    assert db.get_ofz_last_date() == "2026-10-19"
    # кривая у выходных не запрашивается и не пишется
    assert net.zcyc_calls == ["2026-10-15", "2026-10-16", "2026-10-19"]
    assert db.get_ofz_curve("2026-10-17") == []


async def test_daily_update_uses_moscow_yesterday_not_utc(db, clock, monkeypatch):
    _seed(db, "2026-10-19")
    # 23:00 UTC 20-го = 02:00 МСК 21-го → вчера по Москве = 20-е
    clock.now = _utc(2026, 10, 20, 23, 0)
    net = Net(monkeypatch)
    await ow.run_daily_update()
    assert net.board_calls == ["2026-10-20"]


async def test_daily_update_nothing_to_do(db, clock, monkeypatch):
    _seed(db, "2026-10-20")
    net = Net(monkeypatch)
    assert await ow.run_daily_update() == {"dates_loaded": 0, "rows": 0, "curve_nodes": 0}
    assert net.board_calls == []


async def test_daily_update_caps_at_40_days(db, clock, monkeypatch):
    _seed(db, "2026-01-01")
    net = Net(monkeypatch)
    await ow.run_daily_update()
    assert len(net.board_calls) == 40
    assert net.board_calls[0] == "2026-01-02" and net.board_calls[-1] == "2026-02-10"


async def test_daily_update_error_stops_without_skipping_gap(db, clock, monkeypatch):
    _seed(db, "2026-10-14")
    clock.now = _utc(2026, 10, 20, 12)
    net = Net(monkeypatch, fail_on={"2026-10-16"})
    with pytest.raises(RuntimeError):
        await ow.run_daily_update()
    # 15-е сохранено, 16-е упало, 19-е НЕ брали → дыра заполнится в следующий раз
    assert db.get_ofz_last_date() == "2026-10-15"
    assert "2026-10-19" not in net.board_calls


async def test_daily_update_on_empty_db_runs_backfill(db, clock, no_sleep, monkeypatch):
    net = Net(monkeypatch)
    calls = []
    real = ow.backfill

    async def spy(*a, **k):
        calls.append(1)
        return await real(*a, **k)
    monkeypatch.setattr(ow, "backfill", spy)
    stats = await ow.run_daily_update()
    assert calls == [1]
    assert stats["dates_loaded"] > 200 and len(net.board_calls) == 365


# ---------------------------------------------------------------- backfill

async def test_backfill_only_on_empty_table(db, clock, no_sleep, monkeypatch):
    _seed(db)
    net = Net(monkeypatch)
    stats = await ow.backfill()
    assert net.board_calls == [] and net.zcyc_calls == []
    assert stats == {"dates_loaded": 0, "rows": 0, "curve_nodes": 0}


async def test_backfill_sequential_with_pauses_and_survives_errors(db, clock, no_sleep, monkeypatch, caplog):
    net = Net(monkeypatch, fail_on={"2026-10-16"})
    inflight = {"now": 0, "peak": 0}
    orig_board = net.board

    async def watched(day):
        inflight["now"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["now"])
        try:
            await asyncio.sleep(0)
            return await orig_board(day)
        finally:
            inflight["now"] -= 1
    monkeypatch.setattr(hist, "fetch_board_day", watched)
    with caplog.at_level("INFO", logger=ow.logger.name):
        stats = await ow.backfill(days=10)
    assert inflight["peak"] == 1                       # без gather
    assert net.board_calls[0] == "2026-10-20"          # от вчера назад
    assert len(net.board_calls) == 10
    assert no_sleep == [0.15] * 10
    assert any("2026-10-16" in r.message and r.levelname == "WARNING" for r in caplog.records)
    assert any(r.levelname == "INFO" and "backfill" in r.message for r in caplog.records)
    assert db.get_ofz_curve("2026-10-16") == []        # упавший день пропущен, остальные есть
    assert stats["dates_loaded"] == 6                  # 7 будней 11–20 окт минус упавшее 16-е


# ------------------------------------------------------------ get_selection

def _tqob():
    d = json.loads((FIX / "ofz_tqob.json").read_text(encoding="utf-8"))
    out = []
    for r in (dict(zip(d["columns"], row)) for row in d["data"]):
        out.append({"secid": r["SECID"], "shortname": r["SHORTNAME"], "bondtype": r["BONDTYPE"],
                    "faceunit": r["FACEUNIT"], "status": r["STATUS"],
                    "kind": hist.classify_kind(r["BONDTYPE"], r["FACEUNIT"])})
    return out


def _load_days(db):
    d = json.loads((FIX / "ofz_days.json").read_text(encoding="utf-8"))
    cols = [c.lower() for c in d["columns"]]
    rows = [dict(zip(cols, r)) for rs in d["days"].values() for r in rs]
    db.upsert_ofz_daily(rows)


class Tqob:
    def __init__(self, monkeypatch, delay=0.0):
        self.calls = 0
        self.fail = False
        self.delay = delay
        monkeypatch.setattr(hist, "fetch_tqob_securities", self)

    async def __call__(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("moex down")
        return _tqob()


async def test_selection_fixture_50_with_quotas(db, clock, monkeypatch):
    _load_days(db)
    Tqob(monkeypatch)
    sel = await ow.get_selection()
    assert len(sel) == 50
    counts = {}
    for s in sel:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
        assert s["number"] == hist.ofz_number(s["secid"]) and s["number"].isdigit()
    assert counts == {"fixed": 30, "linker": 4, "amort": 2, "float": 14}
    assert any(s["number"] == "26238" for s in sel)


async def test_selection_empty_db_returns_empty_without_moex(db, clock, monkeypatch):
    t = Tqob(monkeypatch)
    assert await ow.get_selection() == []
    assert t.calls == 0


async def test_selection_cached_and_reset_after_update(db, clock, monkeypatch):
    _load_days(db)
    t = Tqob(monkeypatch)
    first = await ow.get_selection()
    clock.mono += 3600
    assert await ow.get_selection() is first and t.calls == 1
    # run_daily_update сбрасывает кэш: отбор пересчитывается, справочник — заново
    _seed(db, "2026-10-19")
    Net(monkeypatch)
    await ow.run_daily_update()
    await ow.get_selection()
    assert t.calls == 2


async def test_selection_cache_ttls(db, clock, monkeypatch):
    _load_days(db)
    t = Tqob(monkeypatch)
    await ow.get_selection()
    clock.mono += 6 * 3600 + 1          # отбор устарел, справочник (24 ч) — нет
    await ow.get_selection()
    assert t.calls == 1
    clock.mono += 24 * 3600             # справочник устарел
    await ow.get_selection()
    assert t.calls == 2


async def test_selection_single_flight(db, clock, monkeypatch):
    _load_days(db)
    t = Tqob(monkeypatch, delay=0.05)
    res = await asyncio.gather(*(ow.get_selection() for _ in range(8)))
    assert t.calls == 1
    assert all(len(r) == 50 for r in res)


async def test_selection_moex_failure_serves_old_cache(db, clock, monkeypatch):
    _load_days(db)
    t = Tqob(monkeypatch)
    first = await ow.get_selection()
    clock.mono += 25 * 3600
    t.fail = True
    again = await ow.get_selection()
    assert t.calls == 2 and again == first and len(again) == 50


async def test_selection_moex_failure_without_cache_is_empty_then_recovers(db, clock, monkeypatch):
    _load_days(db)
    t = Tqob(monkeypatch)
    t.fail = True
    assert await ow.get_selection() == []
    assert await ow.get_selection() == [] and t.calls == 1   # пауза повторов
    clock.mono += 61
    t.fail = False
    assert len(await ow.get_selection()) == 50               # пустой результат не залип


# ------------------------------------------------------------------ прогрев

def _fake_pages(monkeypatch, warm):
    mod = types.ModuleType("app.api.ofz_pages")
    if warm is not None:
        mod.warm = warm
    monkeypatch.setitem(sys.modules, "app.api.ofz_pages", mod)
    monkeypatch.setattr(api_pkg, "ofz_pages", mod, raising=False)


async def test_warm_concurrency_at_most_two(monkeypatch):
    state = {"now": 0, "peak": 0, "done": []}

    async def warm(number):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        try:
            await asyncio.sleep(0.01)
        finally:
            state["now"] -= 1
        state["done"].append(number)
        return number != "5"
    _fake_pages(monkeypatch, warm)
    sel = [{"number": str(i)} for i in range(12)]
    ok, fail = await ow.warm_pages(sel)
    assert sorted(state["done"], key=int) == [str(i) for i in range(12)]
    assert state["peak"] == 2, "пик должен быть ровно 2: меньше — семафор лишний, больше — gather без лимита"
    assert (ok, fail) == (11, 1)


async def test_warm_exception_does_not_stop_others(monkeypatch):
    async def warm(number):
        if number == "1":
            raise RuntimeError("boom")
        return True
    _fake_pages(monkeypatch, warm)
    assert await ow.warm_pages([{"number": "1"}, {"number": "2"}, {"number": "3"}]) == (2, 1)


async def test_warm_skipped_without_function(monkeypatch):
    _fake_pages(monkeypatch, None)
    assert await ow.warm_pages([{"number": "1"}]) == (0, 0)


# -------------------------------------------------------------------- цикл

@pytest.mark.parametrize("now,expected", [
    (_utc(2026, 10, 21, 12, 0), 12.5 * 3600),    # 15:00 МСК → до 03:30 следующего дня
    (_utc(2026, 10, 21, 0, 0), 1800),            # 03:00 МСК → через полчаса
    (_utc(2026, 10, 21, 0, 30), 86400),          # ровно 03:30 → завтра
])
def test_secs_to_next_run(clock, now, expected):
    clock.now = now
    assert ow._secs_to_next_run() == pytest.approx(expected)


class _Stop(BaseException):
    pass


def _loop_harness(monkeypatch, db, daily, rounds):
    sleeps = []
    alerts = []
    events = []

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > 1 + rounds:
            raise _Stop

    async def sel():
        events.append("selection")
        return [{"number": "26238"}]

    async def warm(selection):
        events.append(("warm", selection))
        return 1, 0

    async def alert(streak, exc):
        alerts.append(streak)
    monkeypatch.setattr(ow, "_sleep", fake_sleep)
    monkeypatch.setattr(ow, "run_daily_update", daily)
    monkeypatch.setattr(ow, "get_selection", sel)
    monkeypatch.setattr(ow, "warm_pages", warm)
    monkeypatch.setattr(ow, "_alert", alert)
    return sleeps, alerts, events


async def test_loop_runs_update_then_warm(db, clock, monkeypatch):
    _seed(db)
    order = []

    async def daily():
        order.append("update")
        return {}
    sleeps, alerts, events = _loop_harness(monkeypatch, db, daily, rounds=1)
    with pytest.raises(_Stop):
        await ow.ofz_warm_loop()
    assert sleeps[0] == 30
    assert order == ["update"] and events == ["selection", ("warm", [{"number": "26238"}])]
    assert alerts == []


async def test_loop_backfill_at_startup_only_if_empty(db, clock, monkeypatch):
    called = []

    async def bf(*a, **k):
        called.append(1)
        return {}

    async def daily():
        return {}
    monkeypatch.setattr(ow, "backfill", bf)
    _loop_harness(monkeypatch, db, daily, rounds=0)
    with pytest.raises(_Stop):
        await ow.ofz_warm_loop()
    assert called == [1]
    called.clear()
    _seed(db)
    _loop_harness(monkeypatch, db, daily, rounds=0)
    with pytest.raises(_Stop):
        await ow.ofz_warm_loop()
    assert called == []


async def test_loop_fail_streak_alerts_and_survives(db, clock, monkeypatch, caplog):
    _seed(db)

    async def daily():
        raise RuntimeError("moex down")
    sleeps, alerts, events = _loop_harness(monkeypatch, db, daily, rounds=4)
    with caplog.at_level("ERROR", logger=ow.logger.name):
        with pytest.raises(_Stop):
            await ow.ofz_warm_loop()
    assert alerts == [3, 4]
    assert sum(1 for r in caplog.records if r.exc_info) == 4
    assert events == []          # прогрев не идёт после сбоя обновления
