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
                    manual_coupon REAL
                )
                """
            )
            try:
                conn.execute(
                    "ALTER TABLE portfolio_items ADD COLUMN manual_coupon REAL"
                )
            except sqlite3.OperationalError:
                pass
            conn.commit()

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
                     , manual_coupon
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


storage_service = StorageService()
