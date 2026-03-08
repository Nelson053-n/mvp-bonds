import sqlite3
from pathlib import Path
from typing import Any

from app.config import settings

_UNSET: Any = object()  # sentinel for "not provided" in update_portfolio


class StorageService:
    def __init__(self) -> None:
        self.db_path = Path(settings.sqlite_db_path)
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

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
                    portfolio_id INTEGER REFERENCES portfolios(id) ON DELETE CASCADE,
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
            # Migrations for portfolios table
            for col, col_def in [
                ("share_expires_at", "INTEGER"),  # UNIX timestamp, NULL = never expires
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE portfolios ADD COLUMN {col} {col_def}"
                    )
                except sqlite3.OperationalError:
                    pass

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

            # Migrate users table
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute("UPDATE users SET is_admin = 1 WHERE username = 'admin'")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE users ADD COLUMN tg_chat_id TEXT")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE users ADD COLUMN coupon_notif_enabled INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE users ADD COLUMN coupon_notif_days INTEGER NOT NULL DEFAULT 3")
            except sqlite3.OperationalError:
                pass

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coupon_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES portfolio_items(id) ON DELETE CASCADE,
                    coupon_date TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(item_id, coupon_date)
                )
                """
            )

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

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limits (
                    key TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (key, window_start)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES portfolio_items(id) ON DELETE CASCADE,
                    ticker TEXT NOT NULL,
                    alert_type TEXT NOT NULL CHECK(alert_type IN ('above', 'below')),
                    target_price REAL NOT NULL,
                    triggered INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    triggered_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
                    snapshot_date TEXT NOT NULL,
                    total_value REAL NOT NULL DEFAULT 0,
                    total_cost REAL NOT NULL DEFAULT 0,
                    UNIQUE(portfolio_id, snapshot_date)
                )
                """
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    ticker TEXT NOT NULL,
                    instrument_type TEXT NOT NULL DEFAULT 'bond',
                    note TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, ticker, instrument_type)
                )
            """)

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

            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_snapshots_portfolio_date "
                    "ON portfolio_snapshots(portfolio_id, snapshot_date)"
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

    def get_item_by_ticker(
        self, portfolio_id: int, ticker: str, instrument_type: str
    ) -> dict | None:
        """Get item by ticker and type in a specific portfolio."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, ticker, instrument_type, quantity, purchase_price
                     , manual_coupon, company_rating, manual_coupon_rate
                FROM portfolio_items
                WHERE portfolio_id = ? AND ticker = ? AND instrument_type = ?
                LIMIT 1
                """,
                (portfolio_id, ticker, instrument_type),
            ).fetchone()

        if not row:
            return None

        return {
            "id": int(row[0]),
            "ticker": row[1],
            "instrument_type": row[2],
            "quantity": float(row[3]),
            "purchase_price": float(row[4]),
            "manual_coupon": float(row[5]) if row[5] is not None else None,
            "company_rating": row[6],
            "manual_coupon_rate": float(row[7]) if row[7] is not None else None,
        }

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
                "SELECT id, username, password_hash, created_at, is_admin FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if not row:
            return None
        return {
            "id": int(row[0]),
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
            "is_admin": bool(row[4]),
        }

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, created_at, is_admin, email, tg_chat_id FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

        if not row:
            return None
        return {
            "id": int(row[0]),
            "username": row[1],
            "password_hash": row[2],
            "created_at": row[3],
            "is_admin": bool(row[4]),
            "email": row[5],
            "tg_chat_id": row[6],
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
                SELECT id, user_id, name, share_token, share_password_hash, created_at, share_expires_at
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
                "share_expires_at": row[6],
            }
            for row in rows
        ]

    def get_portfolio(self, portfolio_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, name, share_token, share_password_hash, created_at, share_expires_at
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
            "share_expires_at": row[6],
        }

    def get_portfolio_by_share_token(self, share_token: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, name, share_token, share_password_hash, created_at, share_expires_at
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
            "share_expires_at": row[6],
        }

    def update_portfolio(
        self,
        portfolio_id: int,
        name: str | None = None,
        share_token: str | None = _UNSET,
        share_password_hash: str | None = _UNSET,
        share_expires_at: int | None = _UNSET,
    ) -> int:
        updates = []
        values = []

        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if share_token is not _UNSET:
            updates.append("share_token = ?")
            values.append(share_token)
        if share_password_hash is not _UNSET:
            updates.append("share_password_hash = ?")
            values.append(share_password_hash)
        if share_expires_at is not _UNSET:
            updates.append("share_expires_at = ?")
            values.append(share_expires_at)

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

    def count_portfolios(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM portfolios WHERE user_id = ?", (user_id,)).fetchone()
            return int(row[0]) if row else 0

    def count_items(self, portfolio_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM portfolio_items WHERE portfolio_id = ?", (portfolio_id,)).fetchone()
            return int(row[0]) if row else 0

    def cleanup_expired_shares(self) -> int:
        """Remove expired share tokens. Returns count of cleaned up records."""
        import time
        now = int(time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE portfolios SET share_token = NULL, share_password_hash = NULL, share_expires_at = NULL "
                "WHERE share_expires_at IS NOT NULL AND share_expires_at < ?",
                (now,)
            )
            conn.commit()
            return cursor.rowcount

    def check_rate_limit(self, key: str, window_seconds: int, max_count: int) -> bool:
        """Returns True if request is allowed, False if rate-limited. Atomically increments counter."""
        import time
        now = int(time.time())
        window_start = now - (now % window_seconds)
        with self._connect() as conn:
            # Clean old windows
            conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (window_start - window_seconds,))
            row = conn.execute(
                "SELECT count FROM rate_limits WHERE key = ? AND window_start = ?",
                (key, window_start)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1)",
                    (key, window_start)
                )
                conn.commit()
                return True
            if row[0] >= max_count:
                conn.commit()
                return False
            conn.execute(
                "UPDATE rate_limits SET count = count + 1 WHERE key = ? AND window_start = ?",
                (key, window_start)
            )
            conn.commit()
            return True

    # ── Admin ───────────────────────────────────────────────────────────────

    def get_all_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT u.id, u.username, u.is_admin, u.created_at,
                       COUNT(p.id) as portfolio_count
                FROM users u
                LEFT JOIN portfolios p ON p.user_id = u.id
                GROUP BY u.id
                ORDER BY u.id ASC
                """
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "username": row[1],
                "is_admin": bool(row[2]),
                "created_at": row[3],
                "portfolio_count": int(row[4]),
            }
            for row in rows
        ]

    def delete_user(self, user_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            return int(cursor.rowcount)

    def set_user_admin(self, user_id: int, is_admin: bool) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?",
                (1 if is_admin else 0, user_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_user_password(self, user_id: int, password_hash: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_user_username(self, user_id: int, new_username: str) -> bool:
        """Returns False if username already taken."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?",
                (new_username, user_id),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                "UPDATE users SET username = ? WHERE id = ?",
                (new_username, user_id),
            )
            conn.commit()
            return True

    def update_user_tg_chat_id(self, user_id: int, tg_chat_id: str | None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET tg_chat_id = ? WHERE id = ?",
                (tg_chat_id, user_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def get_user_by_username_for_reset(self, username: str) -> dict | None:
        """Returns minimal user info for password reset (email, tg_chat_id)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, tg_chat_id FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if not row:
            return None
        return {"id": int(row[0]), "email": row[1], "tg_chat_id": row[2]}

    def update_user_email(self, user_id: int, email: str | None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET email = ? WHERE id = ?",
                (email, user_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def move_instrument(self, item_id: int, from_portfolio_id: int, to_portfolio_id: int) -> bool:
        """Move an instrument from one portfolio to another. Returns True if moved."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE portfolio_items SET portfolio_id = ? WHERE id = ? AND portfolio_id = ?",
                (to_portfolio_id, item_id, from_portfolio_id),
            )
            conn.commit()
            return int(cursor.rowcount) > 0

    def get_portfolios_with_item_counts(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.name, p.created_at,
                       COUNT(pi.id) as item_count,
                       COALESCE(SUM(pi.quantity * pi.purchase_price), 0) as total_cost,
                       GROUP_CONCAT(pi.company_rating) as ratings
                FROM portfolios p
                LEFT JOIN portfolio_items pi ON pi.portfolio_id = p.id
                WHERE p.user_id = ?
                GROUP BY p.id
                ORDER BY p.id ASC
                """,
                (user_id,),
            ).fetchall()
        result = []
        for row in rows:
            ratings_raw = row[5] or ""
            ratings = [r.strip() for r in ratings_raw.split(",") if r.strip()]
            risk = self._calc_risk_from_ratings(ratings)
            result.append({
                "id": int(row[0]),
                "name": row[1],
                "created_at": row[2],
                "item_count": int(row[3]),
                "total_cost": round(float(row[4]), 2),
                "risk": risk,
            })
        return result

    @staticmethod
    def _calc_risk_from_ratings(ratings: list[str]) -> str:
        """Determine portfolio risk level from instrument ratings."""
        if not ratings:
            return "unknown"
        _map = {
            "AAA": 0, "AA+": 1, "AA": 1, "AA-": 1,
            "A+": 2, "A": 2, "A-": 2,
            "BBB+": 3, "BBB": 3, "BBB-": 3,
            "BB+": 4, "BB": 4, "BB-": 4,
            "B+": 5, "B": 5, "B-": 5,
        }
        scores = [_map.get(r.upper(), 3) for r in ratings]
        avg = sum(scores) / len(scores)
        if avg <= 1.5:
            return "conservative"
        if avg <= 2.5:
            return "low"
        if avg <= 3.5:
            return "moderate"
        if avg <= 4.5:
            return "high"
        return "aggressive"

    def merge_portfolios(self, source_id: int, target_id: int) -> int:
        """Move all items from source portfolio into target. Returns count moved."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE portfolio_items SET portfolio_id = ? WHERE portfolio_id = ?",
                (target_id, source_id),
            )
            moved = int(cursor.rowcount)
            conn.commit()
        return moved

    def get_all_portfolios_with_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.name, p.user_id, u.username,
                       p.share_token, p.created_at,
                       COUNT(pi.id) as item_count
                FROM portfolios p
                JOIN users u ON u.id = p.user_id
                LEFT JOIN portfolio_items pi ON pi.portfolio_id = p.id
                GROUP BY p.id
                ORDER BY p.user_id ASC, p.id ASC
                """
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "name": row[1],
                "user_id": int(row[2]),
                "username": row[3],
                "share_token": row[4],
                "created_at": row[5],
                "item_count": int(row[6]),
            }
            for row in rows
        ]

    def get_stats(self) -> dict:
        with self._connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            portfolios = conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0]
            shared = conn.execute(
                "SELECT COUNT(*) FROM portfolios WHERE share_token IS NOT NULL"
            ).fetchone()[0]
            items = conn.execute("SELECT COUNT(*) FROM portfolio_items").fetchone()[0]
        return {
            "users": int(users),
            "portfolios": int(portfolios),
            "shared_links": int(shared),
            "total_instruments": int(items),
        }

    def get_all_portfolios_raw(self) -> list[dict]:
        """Get all portfolios (id, user_id, name) for background jobs."""
        with self._connect() as conn:
            rows = conn.execute("SELECT id, user_id, name FROM portfolios").fetchall()
        return [{"id": r[0], "user_id": r[1], "name": r[2]} for r in rows]

    def save_portfolio_snapshot(self, portfolio_id: int, total_value: float, total_cost: float) -> None:
        """Save daily snapshot. Upsert by date."""
        from datetime import date
        today = date.today().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO portfolio_snapshots (portfolio_id, snapshot_date, total_value, total_cost)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(portfolio_id, snapshot_date) DO UPDATE SET
                   total_value=excluded.total_value, total_cost=excluded.total_cost""",
                (portfolio_id, today, total_value, total_cost)
            )
            conn.commit()

    def get_portfolio_snapshots(self, portfolio_id: int, days: int = 90) -> list[dict]:
        """Get historical snapshots for last N days."""
        from datetime import date, timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT snapshot_date, total_value, total_cost
                   FROM portfolio_snapshots
                   WHERE portfolio_id = ? AND snapshot_date >= ?
                   ORDER BY snapshot_date ASC""",
                (portfolio_id, since)
            ).fetchall()
        return [{"date": r[0], "total_value": r[1], "total_cost": r[2]} for r in rows]

    # ── User notification settings ─────────────────────────────────────────

    def get_user_notification_settings(self, user_id: int) -> dict:
        """Return coupon notification settings for a user."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT coupon_notif_enabled, coupon_notif_days FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return {"coupon_notif_enabled": False, "coupon_notif_days": 3}
        return {
            "coupon_notif_enabled": bool(row[0]),
            "coupon_notif_days": int(row[1]),
        }

    def update_user_notification_settings(
        self, user_id: int, enabled: bool, days_before: int
    ) -> None:
        """Update coupon notification settings for a user."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET coupon_notif_enabled = ?, coupon_notif_days = ? WHERE id = ?",
                (1 if enabled else 0, days_before, user_id),
            )
            conn.commit()

    def get_users_with_coupon_notifications(self) -> list[dict]:
        """Return users who have coupon notifications enabled and a tg_chat_id set."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, username, tg_chat_id, coupon_notif_days
                FROM users
                WHERE coupon_notif_enabled = 1 AND tg_chat_id IS NOT NULL
                """
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "username": row[1],
                "tg_chat_id": row[2],
                "coupon_notif_days": int(row[3]),
            }
            for row in rows
        ]

    # ── Coupon notifications log ───────────────────────────────────────────

    def mark_coupon_notification_sent(self, item_id: int, coupon_date: str) -> None:
        """Record that a coupon notification was sent for item_id + coupon_date."""
        from datetime import datetime, timezone

        sent_at = datetime.now(timezone.utc).isoformat()
        # Resolve portfolio_id for the item
        with self._connect() as conn:
            row = conn.execute(
                "SELECT portfolio_id FROM portfolio_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                return
            portfolio_id = row[0]
            conn.execute(
                """
                INSERT OR IGNORE INTO coupon_notifications (portfolio_id, item_id, coupon_date, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (portfolio_id, item_id, coupon_date, sent_at),
            )
            conn.commit()

    def is_coupon_notification_sent(self, item_id: int, coupon_date: str) -> bool:
        """Return True if a notification for this item+coupon_date was already sent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM coupon_notifications WHERE item_id = ? AND coupon_date = ?",
                (item_id, coupon_date),
            ).fetchone()
        return row is not None

    # ── Price alerts ───────────────────────────────────────────────────────

    def get_price_alerts(self, user_id: int) -> list[dict]:
        """Get all price alerts for a user."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT pa.id, pa.user_id, pa.portfolio_id, pa.item_id, pa.ticker,
                          pa.alert_type, pa.target_price, pa.triggered, pa.created_at, pa.triggered_at
                   FROM price_alerts pa WHERE pa.user_id = ? ORDER BY pa.created_at DESC""",
                (user_id,)
            ).fetchall()
        cols = ["id", "user_id", "portfolio_id", "item_id", "ticker", "alert_type", "target_price", "triggered", "created_at", "triggered_at"]
        return [dict(zip(cols, r)) for r in rows]

    def get_price_alerts_for_item(self, item_id: int) -> list[dict]:
        """Get active price alerts for a specific portfolio item."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM price_alerts WHERE item_id = ? AND triggered = 0",
                (item_id,)
            ).fetchall()
        cols = ["id", "user_id", "portfolio_id", "item_id", "ticker", "alert_type", "target_price", "triggered", "created_at", "triggered_at"]
        return [dict(zip(cols, r)) for r in rows]

    def create_price_alert(self, user_id: int, portfolio_id: int, item_id: int, ticker: str, alert_type: str, target_price: float) -> int:
        """Create a new price alert. Returns the new alert id."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO price_alerts (user_id, portfolio_id, item_id, ticker, alert_type, target_price, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, portfolio_id, item_id, ticker, alert_type, target_price, now)
            )
            conn.commit()
            return int(cursor.lastrowid)

    def delete_price_alert(self, alert_id: int, user_id: int) -> bool:
        """Delete a price alert owned by the user. Returns True if deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM price_alerts WHERE id = ? AND user_id = ?",
                (alert_id, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_all_active_price_alerts(self) -> list[dict]:
        """Get all non-triggered price alerts with user tg_chat_id."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT pa.id, pa.user_id, pa.item_id, pa.ticker, pa.alert_type, pa.target_price,
                          u.tg_chat_id
                   FROM price_alerts pa
                   JOIN users u ON u.id = pa.user_id
                   WHERE pa.triggered = 0 AND u.tg_chat_id IS NOT NULL"""
            ).fetchall()
        cols = ["id", "user_id", "item_id", "ticker", "alert_type", "target_price", "tg_chat_id"]
        return [dict(zip(cols, r)) for r in rows]

    def mark_price_alert_triggered(self, alert_id: int) -> None:
        """Mark a price alert as triggered with current timestamp."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE price_alerts SET triggered = 1, triggered_at = ? WHERE id = ?",
                (now, alert_id)
            )
            conn.commit()

    def get_item(self, item_id: int, portfolio_id: int) -> dict | None:
        """Get a single portfolio item by id and portfolio_id."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM portfolio_items WHERE id = ? AND portfolio_id = ?",
                (item_id, portfolio_id)
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    # ── Watchlist ───────────────────────────────────────────────────────────

    def get_watchlist(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, user_id, ticker, instrument_type, note, created_at FROM watchlist WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()
        cols = ["id", "user_id", "ticker", "instrument_type", "note", "created_at"]
        return [dict(zip(cols, r)) for r in rows]

    def add_to_watchlist(self, user_id: int, ticker: str, instrument_type: str, note: str = None) -> int:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO watchlist (user_id, ticker, instrument_type, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, ticker, instrument_type, note, now)
                )
                conn.commit()
                return cursor.lastrowid
            except Exception:
                # Already exists — return existing id
                row = conn.execute(
                    "SELECT id FROM watchlist WHERE user_id = ? AND ticker = ? AND instrument_type = ?",
                    (user_id, ticker, instrument_type)
                ).fetchone()
                return row[0] if row else -1

    def remove_from_watchlist(self, user_id: int, watchlist_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM watchlist WHERE id = ? AND user_id = ?",
                (watchlist_id, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0


storage_service = StorageService()
