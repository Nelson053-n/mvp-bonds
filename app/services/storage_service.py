import sqlite3
from pathlib import Path

from app.config import settings


class StorageService:
    def __init__(self) -> None:
        self.db_path = Path(settings.sqlite_db_path)
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    instrument_type TEXT NOT NULL
                        CHECK(instrument_type IN ('stock', 'bond')),
                    quantity REAL NOT NULL,
                    purchase_price REAL NOT NULL,
                    manual_coupon REAL,
                    company_rating TEXT
                )
                """
            )
            # Migrations for existing databases
            for col, col_def in [
                ("manual_coupon", "REAL"),
                ("company_rating", "TEXT"),
                ("manual_coupon_rate", "REAL"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE portfolio_items ADD COLUMN {col} {col_def}"
                    )
                except sqlite3.OperationalError:
                    pass

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_snapshots (
                    item_id INTEGER PRIMARY KEY,
                    last_price REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # ── Portfolio items ─────────────────────────────────────────────────────

    def add_item(
        self,
        ticker: str,
        instrument_type: str,
        quantity: float,
        purchase_price: float,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO portfolio_items (
                    ticker, instrument_type, quantity, purchase_price
                )
                VALUES (?, ?, ?, ?)
                """,
                (ticker, instrument_type, quantity, purchase_price),
            )
            conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("Не удалось получить id добавленной записи")
            return int(cursor.lastrowid)

    def get_items(self) -> list[dict[str, int | str | float]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, instrument_type, quantity, purchase_price
                     , manual_coupon, company_rating, manual_coupon_rate
                FROM portfolio_items
                ORDER BY id ASC
                """
            ).fetchall()

        return [
            {
                "id": int(row[0]),
                "ticker": row[1],
                "instrument_type": row[2],
                "quantity": float(row[3]),
                "purchase_price": float(row[4]),
                "manual_coupon": (
                    float(row[5]) if row[5] is not None else None
                ),
                "company_rating": row[6],
                "manual_coupon_rate": (
                    float(row[7]) if row[7] is not None else None
                ),
            }
            for row in rows
        ]

    def delete_item(self, item_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM portfolio_items WHERE id = ?",
                (item_id,),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_item(
        self,
        item_id: int,
        quantity: float,
        purchase_price: float,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET quantity = ?, purchase_price = ?
                WHERE id = ?
                """,
                (quantity, purchase_price, item_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_coupon(self, item_id: int, coupon: float) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET manual_coupon = ?
                WHERE id = ?
                """,
                (coupon, item_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_coupon_rate(self, item_id: int, coupon_rate: float) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET manual_coupon_rate = ?
                WHERE id = ?
                """,
                (coupon_rate, item_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_rating(self, item_id: int, rating: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio_items SET company_rating = ? WHERE id = ?",
                (rating, item_id),
            )
            conn.commit()

    def delete_items(self, item_ids: list[int]) -> int:
        if not item_ids:
            return 0

        placeholders = ",".join(["?"] * len(item_ids))
        query = (
            "DELETE FROM portfolio_items "
            f"WHERE id IN ({placeholders})"
        )
        with self._connect() as conn:
            cursor = conn.execute(query, item_ids)
            conn.commit()
            return int(cursor.rowcount)

    # ── Price snapshots ─────────────────────────────────────────────────────

    def get_price_snapshot(self, item_id: int) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_price FROM price_snapshots WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return float(row[0]) if row else None

    def upsert_price_snapshot(self, item_id: int, price: float) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO price_snapshots (item_id, last_price, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    last_price = excluded.last_price,
                    updated_at = excluded.updated_at
                """,
                (item_id, price, now),
            )
            conn.commit()

    def delete_price_snapshot(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM price_snapshots WHERE item_id = ?", (item_id,)
            )
            conn.commit()

    # ── App settings (key-value) ────────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()

    def get_all_settings(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM app_settings"
            ).fetchall()
        return {row[0]: row[1] for row in rows}


storage_service = StorageService()
