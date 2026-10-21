"""Хранение истории ОФЗ и кривой (миграция v10, OfzMixin)."""
import sqlite3

import pytest

from app.config import settings
from app.services.storage_service import StorageService


@pytest.fixture
def svc(tmp_path, monkeypatch) -> StorageService:
    # Своя пустая БД на тест: таблицы ОФЗ не должны делить данные между тестами.
    monkeypatch.setattr(settings, "sqlite_db_path", str(tmp_path / "ofz.db"))
    return StorageService()


def _row(secid="SU26238RMFS4", d="2026-10-20", **kw):
    r = {"secid": secid, "tradedate": d, "close": 70.0, "yieldclose": 16.2,
         "duration": 2472.0, "numtrades": 100, "value": 5e7}
    r.update(kw)
    return r


def test_migration_creates_tables_and_version(svc):
    con = sqlite3.connect(svc.db_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        ver = con.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
    finally:
        con.close()
    assert {"ofz_daily", "ofz_curve_daily"} <= tables
    assert "idx_ofz_daily_tradedate" in idx
    assert ver == StorageService.SCHEMA_VERSION >= 10


def test_upsert_idempotent_and_replaces(svc):
    assert svc.upsert_ofz_daily([_row()]) == 1
    assert svc.upsert_ofz_daily([_row(close=71.5)]) == 1
    rows = svc.get_ofz_board_day("2026-10-20")
    assert len(rows) == 1
    assert rows[0]["close"] == 71.5
    # недостающие колонки -> NULL
    assert rows[0]["zspread"] is None and rows[0]["beiclose"] is None


def test_upsert_skips_rows_without_key(svc):
    assert svc.upsert_ofz_daily([{"secid": "X"}, {"tradedate": "2026-10-20"}]) == 0
    assert svc.upsert_ofz_daily([]) == 0
    assert svc.get_ofz_last_date() is None


def test_history_order_and_window(svc):
    svc.upsert_ofz_daily([_row(d="2026-10-20"), _row(d="2025-01-10"), _row(d="2026-10-01"),
                          _row(d="2025-10-01")])
    svc.upsert_ofz_daily([_row(secid="OTHER", d="2026-12-31")])  # не влияет на окно 26238
    h = svc.get_ofz_history("SU26238RMFS4", days=400)
    # 400 дней от 2026-10-20 -> граница 2025-09-15: 2025-01-10 отсекается
    assert [r["tradedate"] for r in h] == ["2025-10-01", "2026-10-01", "2026-10-20"]
    assert svc.get_ofz_history("NOPE") == []


def test_recent_dates_and_window_on_three_dates(svc):
    for d in ("2026-10-16", "2026-10-19", "2026-10-20"):
        svc.upsert_ofz_daily([_row("A", d), _row("B", d)])
    assert svc.get_ofz_last_date() == "2026-10-20"
    assert svc.get_ofz_recent_dates(2) == ["2026-10-19", "2026-10-20"]
    assert svc.get_ofz_recent_dates(10) == ["2026-10-16", "2026-10-19", "2026-10-20"]
    w = svc.get_ofz_window(2)
    assert len(w) == 4
    assert {r["tradedate"] for r in w} == {"2026-10-19", "2026-10-20"}
    assert [r["tradedate"] for r in w] == sorted(r["tradedate"] for r in w)
    assert len(svc.get_ofz_window(3)) == 6


def test_curve_upsert_order_idempotent(svc):
    nodes = [(2.0, 15.0), (0.25, 12.6), (10.0, 16.1)]
    assert svc.upsert_ofz_curve("2026-10-20", nodes) == 3
    assert svc.upsert_ofz_curve("2026-10-20", [(2.0, 15.5)]) == 1
    assert svc.get_ofz_curve("2026-10-20") == [(0.25, 12.6), (2.0, 15.5), (10.0, 16.1)]
    assert svc.get_ofz_curve("2000-01-01") == []
    assert svc.upsert_ofz_curve("2026-10-20", []) == 0


def test_curve_dates_and_history(svc):
    for d, v in (("2026-10-18", 15.0), ("2026-10-19", 15.2), ("2026-10-20", 15.4), ("2025-01-01", 9.0)):
        svc.upsert_ofz_curve(d, [(2.0, v), (5.0, v + 1)])
    assert svc.get_ofz_curve_last_date() == "2026-10-20"
    assert svc.get_ofz_curve_dates(2) == ["2026-10-19", "2026-10-20"]
    hist = svc.get_ofz_curve_history(2.0, days=400)
    assert hist == [("2026-10-18", 15.0), ("2026-10-19", 15.2), ("2026-10-20", 15.4)]
    assert svc.get_ofz_curve_history(3.0) == []


def test_primary_key_rejects_duplicates_at_db_level(svc):
    """Дубли отсекает схема, а не только INSERT OR REPLACE в коде."""
    svc.upsert_ofz_daily([_row()])
    con = sqlite3.connect(svc.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO ofz_daily (secid, tradedate) VALUES (?, ?)",
                        ("SU26238RMFS4", "2026-10-20"))
        con.rollback()  # иначе открытая транзакция держит блокировку записи
        svc.upsert_ofz_curve("2026-10-20", [(1.0, 14.0)])
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO ofz_curve_daily (tradedate, period, value) VALUES (?,?,?)",
                        ("2026-10-20", 1.0, 1.0))
    finally:
        con.close()
