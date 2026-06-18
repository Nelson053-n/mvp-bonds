import logging
import os
import sqlite3
from pathlib import Path

from app.config import settings
from app.services.storage.items import ItemsMixin
from app.services.storage.portfolios import PortfoliosMixin
from app.services.storage.users import UsersMixin

logger = logging.getLogger(__name__)


class StorageService(ItemsMixin, PortfoliosMixin, UsersMixin):
    def __init__(self) -> None:
        self.db_path = Path(settings.sqlite_db_path)
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def checkpoint(self) -> None:
        """No-op: DELETE journal mode has no WAL to checkpoint."""
        pass

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
                    company_rating TEXT,
                    purchase_date TEXT
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
                ("snapshot_coupon_rate", "REAL"),  # MOEX market coupon rate for risk calc
                ("deleted_at", "TEXT"),  # soft-delete timestamp (ISO 8601)
                ("figi", "TEXT"),  # T-Bank instrument id, links position to operations journal
                ("purchase_date", "TEXT"),  # ISO date (YYYY-MM-DD); NULL = unknown (optional)
                ("custom_name", "TEXT"),   # user-set name for off-exchange (source='custom') items
                ("custom_price", "REAL"),  # user-set current price for source='custom' items
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

            try:
                conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
            except sqlite3.OperationalError:
                pass

            # Pro tier (freemium). is_pro flag + optional expiry (ISO date/None=lifetime).
            # On first add, grant Pro to all existing (early-adopter) users.
            try:
                conn.execute("ALTER TABLE users ADD COLUMN is_pro INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE users SET is_pro = 1")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE users ADD COLUMN pro_until TEXT")
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
                    securities_value REAL,
                    UNIQUE(portfolio_id, snapshot_date)
                )
                """
            )
            # total_value = securities + cash; securities_value = securities only
            # (NULL for snapshots taken before this column existed).
            try:
                conn.execute("ALTER TABLE portfolio_snapshots ADD COLUMN securities_value REAL")
            except sqlite3.OperationalError:
                pass

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

            conn.execute("""
                CREATE TABLE IF NOT EXISTS rating_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    rating TEXT NOT NULL,
                    source TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS waitlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
            """)

            # Pro payments (YooKassa) — for NPD bookkeeping ("Мой налог" by hand).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pro_payments (
                    payment_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    username TEXT,
                    plan TEXT,
                    amount REAL,
                    status TEXT,
                    receipt_done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_sync (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id INTEGER NOT NULL UNIQUE REFERENCES portfolios(id) ON DELETE CASCADE,
                    tbank_token_enc TEXT NOT NULL,
                    tbank_token_prefix TEXT NOT NULL,
                    tbank_account_id TEXT NOT NULL,
                    bonds_only INTEGER NOT NULL DEFAULT 0,
                    sync_enabled INTEGER NOT NULL DEFAULT 1,
                    last_sync_at TEXT,
                    last_sync_error TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS tbank_coupons (
                    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
                    figi TEXT NOT NULL,
                    coupons_total REAL NOT NULL DEFAULT 0,
                    first_buy_date TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (portfolio_id, figi)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id INTEGER,
                    details TEXT,
                    ip_address TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    benchmark TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    value REAL NOT NULL,
                    UNIQUE(benchmark, snapshot_date)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS password_reset_codes (
                    code TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_password_reset_codes_expires "
                    "ON password_reset_codes(expires_at)"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_benchmark_snapshots_lookup "
                    "ON benchmark_snapshots(benchmark, snapshot_date)"
                )
            except sqlite3.OperationalError:
                pass

            # Migrations for portfolio_items — add source column
            for col, col_def in [
                ("source", "TEXT NOT NULL DEFAULT 'manual'"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE portfolio_items ADD COLUMN {col} {col_def}"
                    )
                except sqlite3.OperationalError:
                    pass

            # Migrations for portfolio_sync — cash balance JSON
            for col, col_def in [
                ("cash_balance", "TEXT"),
                ("cash_updated_at", "TEXT"),
                ("last_operations_sync_at", "TEXT"),  # last GetOperations fetch (ISO)
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE portfolio_sync ADD COLUMN {col} {col_def}"
                    )
                except sqlite3.OperationalError:
                    pass

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

            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_items_ticker ON portfolio_items(ticker)",
                "CREATE INDEX IF NOT EXISTS idx_coupon_notif_item ON coupon_notifications(item_id)",
                "CREATE INDEX IF NOT EXISTS idx_alerts_item_id ON price_alerts(item_id)",
                "CREATE INDEX IF NOT EXISTS idx_alerts_user_triggered ON price_alerts(user_id, triggered)",
                "CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_rate_limits_window ON rate_limits(window_start)",
                "CREATE INDEX IF NOT EXISTS idx_rating_history_ticker_source ON rating_history(ticker, source, recorded_at)",
                "CREATE INDEX IF NOT EXISTS idx_portfolio_sync_enabled ON portfolio_sync(sync_enabled)",
                "CREATE INDEX IF NOT EXISTS idx_audit_log_admin ON admin_audit_log(admin_user_id, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_audit_log_created ON admin_audit_log(created_at)",
            ]:
                try:
                    conn.execute(idx_sql)
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
                self._write_bootstrap_admin_password(admin_password)
            else:
                conn.commit()


    def _write_bootstrap_admin_password(self, password: str) -> None:
        """Write the bootstrap admin password to a 0600 file next to the DB.

        Avoids leaking the credential into the logger (and any log aggregator).
        Operator should delete the file after the first successful login.
        """
        path = self.db_path.parent / "admin_password.txt"
        try:
            # O_EXCL to avoid silently overwriting an existing operator-written file
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, f"admin\n{password}\n".encode())
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            logger.warning(
                "Admin user created. Credentials written to %s (mode 0600). "
                "Login as 'admin', change the password, then delete this file.",
                path,
            )
        except OSError as e:
            # Fallback: stdout only, never the structured logger
            print(
                f"[BOOTSTRAP] Admin user created. Username: admin  Password: {password}\n"
                f"[BOOTSTRAP] Failed to persist to {path}: {e}. Save this password now.",
                flush=True,
            )


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

    # ── Password reset codes ────────────────────────────────────────────────

    def insert_password_reset_code(self, code: str, user_id: int, expires_at: int) -> None:
        """Store a fresh reset code; overwrites any prior code with the same value."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO password_reset_codes (code, user_id, expires_at, attempts) "
                "VALUES (?, ?, ?, 0)",
                (code, user_id, expires_at),
            )
            conn.commit()

    def consume_password_reset_code(self, code: str, now_ts: int) -> int | None:
        """Atomically consume a valid (non-expired) code. Returns user_id or None.

        Single-use: row is deleted on success so concurrent confirms can't reuse it.
        """
        with self._connect() as conn:
            # Opportunistic GC of expired codes
            conn.execute("DELETE FROM password_reset_codes WHERE expires_at < ?", (now_ts,))
            row = conn.execute(
                "DELETE FROM password_reset_codes WHERE code = ? AND expires_at >= ? "
                "RETURNING user_id",
                (code, now_ts),
            ).fetchone()
            conn.commit()
            return int(row[0]) if row else None

    def burn_password_reset_attempts(self, max_attempts: int, now_ts: int) -> int:
        """Increment attempts on every live code; delete any that hit max_attempts.

        Returns number of codes invalidated by this call. Used after a failed
        /reset-password to make brute-force ineffective: any in-flight code
        dies after enough wrong tries from any source.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM password_reset_codes WHERE expires_at < ?", (now_ts,))
            conn.execute(
                "UPDATE password_reset_codes SET attempts = attempts + 1 WHERE expires_at >= ?",
                (now_ts,),
            )
            cur = conn.execute(
                "DELETE FROM password_reset_codes WHERE attempts >= ?",
                (max_attempts,),
            )
            conn.commit()
            return cur.rowcount or 0

    # ── Admin ───────────────────────────────────────────────────────────────


    def write_audit_log(
        self,
        admin_user_id: int,
        action: str,
        target_type: str | None,
        target_id: int | None,
        details: str | None,
        ip_address: str | None,
    ) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO admin_audit_log
                   (admin_user_id, action, target_type, target_id, details, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (admin_user_id, action, target_type, target_id, details, ip_address, now),
            )
            conn.commit()

    def get_audit_log(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT a.id, a.admin_user_id, u.username AS admin_username,
                          a.action, a.target_type, a.target_id,
                          a.details, a.ip_address, a.created_at
                   FROM admin_audit_log a
                   LEFT JOIN users u ON u.id = a.admin_user_id
                   ORDER BY a.id DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "admin_user_id": int(row[1]),
                "admin_username": row[2],
                "action": row[3],
                "target_type": row[4],
                "target_id": int(row[5]) if row[5] is not None else None,
                "details": row[6],
                "ip_address": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]

    # ── Backups ─────────────────────────────────────────────────────────────

    def _backup_dir(self) -> Path:
        return Path(self.db_path).parent / "backups"

    def create_backup(self, label: str = "manual") -> str:
        import shutil
        from datetime import datetime, timezone
        db_path = Path(self.db_path)
        backup_dir = self._backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"portfolio_{stamp}_{label}.db"
        shutil.copy2(db_path, backup_dir / filename)
        logger.info("Backup created: %s", filename)
        self._prune_backups()
        return filename

    def _prune_backups(self) -> None:
        try:
            keep = int(self.get_setting("backup_keep_count", "30"))
        except ValueError:
            keep = 30
        # Prune only daily/manual backups; startup backups rotate separately
        # (see _backup_db_on_startup) and must not be evicted here.
        backups = sorted(
            b for b in self._backup_dir().glob("portfolio_*.db")
            if not b.name.endswith("_startup.db")
        )
        for old in backups[:-keep]:
            old.unlink()
            logger.info("Old backup removed: %s", old.name)

    def get_backups(self) -> list[dict]:
        backup_dir = self._backup_dir()
        if not backup_dir.exists():
            return []
        backups = sorted(backup_dir.glob("portfolio_*.db"), reverse=True)
        result = []
        for p in backups:
            stat = p.stat()
            result.append({
                "filename": p.name,
                "size": stat.st_size,
                "created_at": p.name.split("_")[1] + "_" + p.name.split("_")[2] if len(p.name.split("_")) >= 3 else "",
            })
        return result

    def delete_backup(self, filename: str) -> bool:
        path = self._backup_dir() / filename
        if not path.exists() or path.parent != self._backup_dir():
            return False
        path.unlink()
        logger.info("Backup deleted: %s", filename)
        return True

    def get_backup_path(self, filename: str) -> Path | None:
        path = self._backup_dir() / filename
        if not path.exists() or path.parent.resolve() != self._backup_dir().resolve():
            return None
        return path





    def get_stats(self) -> dict:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            portfolios = conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0]
            shared = conn.execute(
                "SELECT COUNT(*) FROM portfolios WHERE share_token IS NOT NULL"
            ).fetchone()[0]
            items = conn.execute("SELECT COUNT(*) FROM portfolio_items WHERE deleted_at IS NULL").fetchone()[0]
            active_today = conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_login >= ?", (today,)
            ).fetchone()[0]
            autosync_count = conn.execute(
                "SELECT COUNT(*) FROM portfolio_sync WHERE sync_enabled = 1"
            ).fetchone()[0]
            tg_notif_count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE tg_chat_id IS NOT NULL AND coupon_notif_enabled = 1"
            ).fetchone()[0]
        backup_count = len(self.get_backups())
        return {
            "users": int(users),
            "portfolios": int(portfolios),
            "shared_links": int(shared),
            "total_instruments": int(items),
            "active_today": int(active_today),
            "autosync_count": int(autosync_count),
            "tg_notif_count": int(tg_notif_count),
            "backup_count": backup_count,
        }

    def get_all_portfolios_raw(self) -> list[dict]:
        """Get all portfolios (id, user_id, name) for background jobs."""
        with self._connect() as conn:
            rows = conn.execute("SELECT id, user_id, name FROM portfolios").fetchall()
        return [{"id": r[0], "user_id": r[1], "name": r[2]} for r in rows]

    def get_all_portfolio_items_for_rating(self) -> list[dict]:
        """Return one item per unique ticker (for daily rating refresh)."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT MIN(id) as id, MIN(portfolio_id) as portfolio_id, ticker, instrument_type
                FROM portfolio_items
                WHERE deleted_at IS NULL
                GROUP BY ticker
                ORDER BY ticker
            """).fetchall()
        return [{"id": r[0], "portfolio_id": r[1], "ticker": r[2], "instrument_type": r[3]} for r in rows]


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

    def get_price_alerts_for_item(self, item_id: int, user_id: int) -> list[dict]:
        """Get active price alerts for a specific portfolio item owned by user_id."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM price_alerts WHERE item_id = ? AND user_id = ? AND triggered = 0",
                (item_id, user_id)
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

    # ── Waitlist ────────────────────────────────────────────

    def add_waitlist_email(self, email: str) -> int:
        """Add email to Pro waitlist. Returns id. Raises IntegrityError on duplicate."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO waitlist (email, created_at) VALUES (?, ?)",
                (email, now),
            )
            conn.commit()
            return int(cursor.lastrowid)  # type: ignore[arg-type]

    # ── Pro payments (YooKassa, for NPD bookkeeping) ─────────────────────────

    def record_pro_payment(self, payment_id: str, user_id: int, username: str,
                           plan: str, amount: float, status: str) -> None:
        """Upsert a Pro payment by its YooKassa id (idempotent on webhook retries)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO pro_payments (payment_id, user_id, username, plan, amount, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(payment_id) DO UPDATE SET status=excluded.status""",
                (payment_id, user_id, username, plan, amount, status, now),
            )
            conn.commit()

    def get_pro_payments(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT payment_id, user_id, username, plan, amount, status, receipt_done, created_at
                   FROM pro_payments ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {"payment_id": r[0], "user_id": r[1], "username": r[2], "plan": r[3],
             "amount": r[4], "status": r[5], "receipt_done": bool(r[6]), "created_at": r[7]}
            for r in rows
        ]

    def get_revenue_by_day(self, days: int = 30) -> dict:
        """Daily succeeded-payment revenue for the last `days` days (admin chart).

        Groups by the UTC date in created_at (ISO string). Returns rows for every
        day in the window (zero-filled), oldest first, plus a period total.
        """
        from datetime import date, timedelta
        days = max(1, min(int(days), 365))
        start = (date.today() - timedelta(days=days - 1)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT substr(created_at, 1, 10) AS d,
                          SUM(amount) AS total,
                          COUNT(*) AS cnt
                   FROM pro_payments
                   WHERE status = 'succeeded' AND substr(created_at, 1, 10) >= ?
                   GROUP BY d""",
                (start,),
            ).fetchall()
        by_day = {r[0]: (float(r[1] or 0), int(r[2])) for r in rows}
        series = []
        total_sum = 0.0
        total_cnt = 0
        for i in range(days):
            d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
            amount, cnt = by_day.get(d, (0.0, 0))
            total_sum += amount
            total_cnt += cnt
            series.append({"date": d, "amount": amount, "count": cnt})
        return {"days": days, "series": series, "total": total_sum, "count": total_cnt}

    def set_payment_receipt_done(self, payment_id: str, done: bool) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pro_payments SET receipt_done = ? WHERE payment_id = ?",
                (1 if done else 0, payment_id),
            )
            conn.commit()
            return int(cur.rowcount)

    # ── T-Bank auto-sync ─────────────────────────────────────────────────────

    def get_tbank_items(self, portfolio_id: int) -> list[dict]:
        """Return portfolio_items with source='tbank' that are not soft-deleted."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, instrument_type, quantity, purchase_price, figi
                FROM portfolio_items
                WHERE portfolio_id = ? AND source = 'tbank' AND deleted_at IS NULL
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
                "figi": row[5],
            }
            for row in rows
        ]

    def upsert_sync_config(
        self,
        portfolio_id: int,
        tbank_token_enc: str,
        tbank_token_prefix: str,
        tbank_account_id: str,
        bonds_only: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_sync
                    (portfolio_id, tbank_token_enc, tbank_token_prefix, tbank_account_id, bonds_only, sync_enabled)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(portfolio_id) DO UPDATE SET
                    tbank_token_enc = excluded.tbank_token_enc,
                    tbank_token_prefix = excluded.tbank_token_prefix,
                    tbank_account_id = excluded.tbank_account_id,
                    bonds_only = excluded.bonds_only,
                    sync_enabled = 1
                """,
                (portfolio_id, tbank_token_enc, tbank_token_prefix, tbank_account_id, int(bonds_only)),
            )
            conn.commit()

    def get_sync_config(self, portfolio_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, portfolio_id, tbank_token_enc, tbank_token_prefix,
                       tbank_account_id, bonds_only, sync_enabled, last_sync_at, last_sync_error,
                       cash_balance, cash_updated_at, last_operations_sync_at
                FROM portfolio_sync
                WHERE portfolio_id = ?
                """,
                (portfolio_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "portfolio_id": row[1],
            "tbank_token_enc": row[2],
            "tbank_token_prefix": row[3],
            "tbank_account_id": row[4],
            "bonds_only": bool(row[5]),
            "sync_enabled": bool(row[6]),
            "last_sync_at": row[7],
            "last_sync_error": row[8],
            "cash_balance": row[9],
            "cash_updated_at": row[10],
            "last_operations_sync_at": row[11],
        }

    def update_sync_cash(self, portfolio_id: int, cash_json: str) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio_sync SET cash_balance = ?, cash_updated_at = ? WHERE portfolio_id = ?",
                (cash_json, now, portfolio_id),
            )
            conn.commit()

    def update_operations_sync_at(self, portfolio_id: int, ts: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio_sync SET last_operations_sync_at = ? WHERE portfolio_id = ?",
                (ts, portfolio_id),
            )
            conn.commit()

    def upsert_tbank_coupons(
        self,
        portfolio_id: int,
        figi: str,
        coupons_total: float,
        first_buy_date: str | None,
    ) -> None:
        """Store aggregated realized coupons (RUB) per T-Bank position (by figi)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tbank_coupons
                    (portfolio_id, figi, coupons_total, first_buy_date, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(portfolio_id, figi) DO UPDATE SET
                    coupons_total = excluded.coupons_total,
                    first_buy_date = COALESCE(excluded.first_buy_date, tbank_coupons.first_buy_date),
                    updated_at = excluded.updated_at
                """,
                (portfolio_id, figi, float(coupons_total), first_buy_date, now),
            )
            conn.commit()

    def get_tbank_coupons(self, portfolio_id: int) -> dict[str, dict]:
        """Return {figi: {coupons_total, first_buy_date}} for a portfolio."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT figi, coupons_total, first_buy_date FROM tbank_coupons WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchall()
        return {
            row[0]: {"coupons_total": float(row[1]), "first_buy_date": row[2]}
            for row in rows
        }

    def delete_tbank_coupons(self, portfolio_id: int, figi: str) -> int:
        """Drop the realized-coupon record for a figi (e.g. position fully sold)."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM tbank_coupons WHERE portfolio_id = ? AND figi = ?",
                (portfolio_id, figi),
            )
            conn.commit()
            return int(cur.rowcount)

    def set_sync_enabled(self, portfolio_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio_sync SET sync_enabled = ? WHERE portfolio_id = ?",
                (int(enabled), portfolio_id),
            )
            conn.commit()

    def update_sync_status(
        self,
        portfolio_id: int,
        last_sync_at: str,
        last_sync_error: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE portfolio_sync
                SET last_sync_at = ?, last_sync_error = ?
                WHERE portfolio_id = ?
                """,
                (last_sync_at, last_sync_error, portfolio_id),
            )
            conn.commit()

    def get_all_enabled_syncs(self) -> list[dict]:
        """Return all sync configs where sync_enabled=1, with user_id from portfolios."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ps.id, ps.portfolio_id, ps.tbank_token_enc, ps.tbank_token_prefix,
                       ps.tbank_account_id, ps.bonds_only, ps.sync_enabled,
                       ps.last_sync_at, ps.last_sync_error,
                       p.user_id
                FROM portfolio_sync ps
                JOIN portfolios p ON p.id = ps.portfolio_id
                WHERE ps.sync_enabled = 1
                """,
            ).fetchall()
        return [
            {
                "id": row[0],
                "portfolio_id": row[1],
                "tbank_token_enc": row[2],
                "tbank_token_prefix": row[3],
                "tbank_account_id": row[4],
                "bonds_only": bool(row[5]),
                "sync_enabled": bool(row[6]),
                "last_sync_at": row[7],
                "last_sync_error": row[8],
                "user_id": row[9],
            }
            for row in rows
        ]

    def soft_delete_tbank_items(self, portfolio_id: int, tickers: list[str]) -> int:
        """Soft-delete tbank items by ticker list. Returns count deleted."""
        from datetime import datetime, timezone
        if not tickers:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(tickers))
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE portfolio_items
                SET deleted_at = ?
                WHERE portfolio_id = ? AND ticker IN ({placeholders})
                      AND deleted_at IS NULL
                """,
                (now, portfolio_id, *tickers),
            )
            conn.commit()
            return cursor.rowcount

    # ── Benchmark snapshots ─────────────────────────────────────────
    def upsert_benchmark_snapshot(self, benchmark: str, snapshot_date: str, value: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO benchmark_snapshots (benchmark, snapshot_date, value) "
                "VALUES (?, ?, ?)",
                (benchmark.upper(), snapshot_date, float(value)),
            )
            conn.commit()

    def bulk_upsert_benchmark_snapshots(self, benchmark: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        bm = benchmark.upper()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO benchmark_snapshots (benchmark, snapshot_date, value) "
                "VALUES (?, ?, ?)",
                [(bm, r["date"], float(r["value"])) for r in rows if r.get("date") and r.get("value") is not None],
            )
            conn.commit()
        return len(rows)

    def get_benchmark_snapshots(self, benchmark: str, days: int) -> list[dict]:
        from datetime import date, timedelta
        date_from = (date.today() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT snapshot_date, value FROM benchmark_snapshots "
                "WHERE benchmark = ? AND snapshot_date >= ? ORDER BY snapshot_date ASC",
                (benchmark.upper(), date_from),
            )
            return [{"date": r[0], "value": r[1]} for r in cursor.fetchall()]


storage_service = StorageService()
