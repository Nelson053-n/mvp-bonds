"""OFZ mixin: дневная история торгов ОФЗ и кривая бескупонной доходности."""
from datetime import date, timedelta

_DAILY_COLS = (
    "secid", "tradedate", "close", "yieldclose", "duration", "numtrades",
    "value", "zspread", "facevalue", "iricpiclose", "beiclose",
)
_SELECT_COLS = ", ".join(_DAILY_COLS)


def _row(r) -> dict:
    return dict(zip(_DAILY_COLS, r))


class OfzMixin:
    def upsert_ofz_daily(self, rows: list[dict]) -> int:
        """INSERT OR REPLACE; строки без secid/tradedate пропускаются. Возвращает число записанных."""
        data = [
            tuple(r.get(c) for c in _DAILY_COLS)
            for r in rows
            if r.get("secid") and r.get("tradedate")
        ]
        if not data:
            return 0
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO ofz_daily ({_SELECT_COLS}) "
                f"VALUES ({', '.join('?' * len(_DAILY_COLS))})",
                data,
            )
            conn.commit()
        return len(data)

    def get_ofz_history(self, secid: str, days: int = 400) -> list[dict]:
        """Ряд бумаги по возрастанию даты: `days` календарных дней назад от её последней даты."""
        with self._connect() as conn:
            last = conn.execute(
                "SELECT MAX(tradedate) FROM ofz_daily WHERE secid = ?", (secid,)
            ).fetchone()[0]
            if not last:
                return []
            date_from = (date.fromisoformat(last) - timedelta(days=days)).isoformat()
            cur = conn.execute(
                f"SELECT {_SELECT_COLS} FROM ofz_daily "
                "WHERE secid = ? AND tradedate >= ? ORDER BY tradedate ASC",
                (secid, date_from),
            )
            return [_row(r) for r in cur.fetchall()]

    def get_ofz_board_day(self, tradedate: str) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT {_SELECT_COLS} FROM ofz_daily WHERE tradedate = ? ORDER BY secid ASC",
                (tradedate,),
            )
            return [_row(r) for r in cur.fetchall()]

    def get_ofz_recent_dates(self, n: int) -> list[str]:
        """Последние n различных торговых дат, по возрастанию."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT DISTINCT tradedate FROM ofz_daily ORDER BY tradedate DESC LIMIT ?",
                (n,),
            )
            return sorted(r[0] for r in cur.fetchall())

    def get_ofz_window(self, n_days: int) -> list[dict]:
        """Все строки за последние n_days ТОРГОВЫХ дат таблицы (не календарных)."""
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT {_SELECT_COLS} FROM ofz_daily WHERE tradedate IN "
                "(SELECT DISTINCT tradedate FROM ofz_daily ORDER BY tradedate DESC LIMIT ?) "
                "ORDER BY tradedate ASC, secid ASC",
                (n_days,),
            )
            return [_row(r) for r in cur.fetchall()]

    def get_ofz_last_date(self) -> str | None:
        with self._connect() as conn:
            return conn.execute("SELECT MAX(tradedate) FROM ofz_daily").fetchone()[0]

    def upsert_ofz_curve(self, tradedate: str, nodes: list[tuple[float, float]]) -> int:
        if not tradedate or not nodes:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO ofz_curve_daily (tradedate, period, value) VALUES (?, ?, ?)",
                [(tradedate, float(p), None if v is None else float(v)) for p, v in nodes],
            )
            conn.commit()
        return len(nodes)

    def get_ofz_curve(self, tradedate: str) -> list[tuple[float, float]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT period, value FROM ofz_curve_daily "
                "WHERE tradedate = ? AND value IS NOT NULL ORDER BY period ASC",
                (tradedate,),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def get_ofz_curve_dates(self, n: int) -> list[str]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT DISTINCT tradedate FROM ofz_curve_daily ORDER BY tradedate DESC LIMIT ?",
                (n,),
            )
            return sorted(r[0] for r in cur.fetchall())

    def get_ofz_curve_last_date(self) -> str | None:
        with self._connect() as conn:
            return conn.execute("SELECT MAX(tradedate) FROM ofz_curve_daily").fetchone()[0]

    def get_ofz_curve_history(self, period: float, days: int = 400) -> list[tuple[str, float]]:
        """Ряд одного узла кривой по возрастанию даты, `days` календарных дней от его последней даты."""
        with self._connect() as conn:
            last = conn.execute(
                "SELECT MAX(tradedate) FROM ofz_curve_daily WHERE period = ?", (float(period),)
            ).fetchone()[0]
            if not last:
                return []
            date_from = (date.fromisoformat(last) - timedelta(days=days)).isoformat()
            cur = conn.execute(
                "SELECT tradedate, value FROM ofz_curve_daily "
                "WHERE period = ? AND tradedate >= ? AND value IS NOT NULL ORDER BY tradedate ASC",
                (float(period), date_from),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]
