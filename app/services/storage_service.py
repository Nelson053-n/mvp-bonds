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
        import logging
        import uuid
        import secrets
        from datetime import datetime, timezone

        logger = logging.getLogger(__name__)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # Create users table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            # Create portfolios table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    share_token TEXT UNIQUE,
                    share_password_hash TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id INTEGER,
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
                ("portfolio_id", "INTEGER"),
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

            # Create indices
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_portfolio_items_portfolio_id "
                    "ON portfolio_items(portfolio_id)"
                )
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_portfolios_user_id "
                    "ON portfolios(user_id)"
                )
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_portfolios_share_token "
                    "ON portfolios(share_token)"
                )
            except sqlite3.OperationalError:
                pass

            # Bootstrap user #1 if no users exist
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if user_count == 0:
                now = datetime.now(timezone.utc).isoformat()
                import bcrypt

                # Generate admin password
                admin_password = secrets.token_urlsafe(12)
                password_hash = bcrypt.hashpw(
                    admin_password.encode(), bcrypt.gensalt()
                ).decode()

                # Create admin user
                cursor = conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    ("admin", password_hash, now),
                )
                admin_user_id = cursor.lastrowid

                # Create default portfolio
                conn.execute(
                    "INSERT INTO portfolios (user_id, name, created_at) VALUES (?, ?, ?)",
                    (admin_user_id, "Основной", now),
                )

                # Migrate existing portfolio_items to default portfolio
                conn.execute(
                    "UPDATE portfolio_items SET portfolio_id = ? WHERE portfolio_id IS NULL",
                    (1,),
                )

                conn.commit()
                logger.warning(
                    "Admin user created. Username: admin  Password: %s",
                    admin_password,
                )
            else:
                conn.commit()

    # ── Portfolio items ─────────────────────────────────────────────────────

    def add_item(
        self,
        ticker: str,
        instrument_type: str,
        quantity: float,
        purchase_price: float,
        portfolio_id: int,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO portfolio_items (
                    portfolio_id, ticker, instrument_type, quantity, purchase_price
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (portfolio_id, ticker, instrument_type, quantity, purchase_price),
            )
            conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("Не удалось получить id добавленной записи")
            return int(cursor.lastrowid)

    def get_items(self, portfolio_id: int) -> list[dict[str, int | str | float]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, instrument_type, quantity, purchase_price
                     , manual_coupon, company_rating, manual_coupon_rate
                FROM portfolio_items
                WHERE portfolio_id = ?
                ORDER BY id ASC
                """,
                (portfolio_id,),
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

    def delete_item(self, item_id: int, portfolio_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM portfolio_items WHERE id = ? AND portfolio_id = ?",
                (item_id, portfolio_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_item(
        self,
        item_id: int,
        portfolio_id: int,
        quantity: float,
        purchase_price: float,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET quantity = ?, purchase_price = ?
                WHERE id = ? AND portfolio_id = ?
                """,
                (quantity, purchase_price, item_id, portfolio_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_coupon(self, item_id: int, portfolio_id: int, coupon: float) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET manual_coupon = ?
                WHERE id = ? AND portfolio_id = ?
                """,
                (coupon, item_id, portfolio_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_coupon_rate(
        self, item_id: int, portfolio_id: int, coupon_rate: float
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET manual_coupon_rate = ?
                WHERE id = ? AND portfolio_id = ?
                """,
                (coupon_rate, item_id, portfolio_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_rating(
        self, item_id: int, portfolio_id: int, rating: str | None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio_items SET company_rating = ? WHERE id = ? AND portfolio_id = ?",
                (rating, item_id, portfolio_id),
            )
            conn.commit()

    def delete_items(self, item_ids: list[int], portfolio_id: int) -> int:
        if not item_ids:
            return 0

        placeholders = ",".join(["?"] * len(item_ids))
        query = (
            "DELETE FROM portfolio_items "
            f"WHERE id IN ({placeholders}) AND portfolio_id = ?"
        )
        with self._connect() as conn:
            cursor = conn.execute(query, item_ids + [portfolio_id])
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

    # ── Users ───────────────────────────────────────────────────────────────

    def create_user(self, username: str, password_hash: str) -> int:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, password_hash, now),
                )
                conn.commit()
                if cursor.lastrowid is None:
                    raise RuntimeError("Не удалось получить id добавленного пользователя")
                return int(cursor.lastrowid)
            except sqlite3.IntegrityError as e:
                raise ValueError(f"Пользователь {username} уже существует") from e

    def get_user_by_username(self, username: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if not row:
            return None
        return {
            "id": int(row[0]),
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
        }

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

        if not row:
            return None
        return {
            "id": int(row[0]),
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
        }

    # ── Portfolios ──────────────────────────────────────────────────────────

    def create_portfolio(self, user_id: int, name: str) -> int:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO portfolios (user_id, name, created_at) VALUES (?, ?, ?)",
                (user_id, name, now),
            )
            conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("Не удалось получить id добавленного портфеля")
            return int(cursor.lastrowid)

    def get_portfolios(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, name, share_token, share_password_hash, created_at
                FROM portfolios
                WHERE user_id = ?
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()

        return [
            {
                "id": int(row[0]),
                "user_id": int(row[1]),
                "name": row[2],
                "share_token": row[3],
                "share_password_hash": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]

    def get_portfolio(self, portfolio_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, name, share_token, share_password_hash, created_at
                FROM portfolios
                WHERE id = ?
                """,
                (portfolio_id,),
            ).fetchone()

        if not row:
            return None
        return {
            "id": int(row[0]),
            "user_id": int(row[1]),
            "name": row[2],
            "share_token": row[3],
            "share_password_hash": row[4],
            "created_at": row[5],
        }

    def get_portfolio_by_share_token(self, share_token: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, name, share_token, share_password_hash, created_at
                FROM portfolios
                WHERE share_token = ?
                """,
                (share_token,),
            ).fetchone()

        if not row:
            return None
        return {
            "id": int(row[0]),
            "user_id": int(row[1]),
            "name": row[2],
            "share_token": row[3],
            "share_password_hash": row[4],
            "created_at": row[5],
        }

    def update_portfolio(
        self,
        portfolio_id: int,
        name: str | None = None,
        share_token: str | None = None,
        share_password_hash: str | None = None,
    ) -> int:
        updates = []
        values = []

        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if share_token is not None:
            updates.append("share_token = ?")
            values.append(share_token)
        if share_password_hash is not None:
            updates.append("share_password_hash = ?")
            values.append(share_password_hash)

        if not updates:
            return 0

        values.append(portfolio_id)
        query = f"UPDATE portfolios SET {', '.join(updates)} WHERE id = ?"

        with self._connect() as conn:
            cursor = conn.execute(query, values)
            conn.commit()
            return int(cursor.rowcount)

    def delete_portfolio(self, portfolio_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM portfolios WHERE id = ?",
                (portfolio_id,),
            )
            conn.commit()
            return int(cursor.rowcount)


storage_service = StorageService()
