"""Users mixin: user CRUD, admin flags, Pro tier, login tracking, password reset helpers."""
import logging
import sqlite3

logger = logging.getLogger(__name__)


def _pro_active(is_pro_flag, pro_until) -> bool:
    """Effective Pro status: flag set AND (no expiry OR expiry in the future)."""
    if not is_pro_flag:
        return False
    if not pro_until:
        return True  # lifetime Pro
    from datetime import date
    try:
        return date.fromisoformat(str(pro_until)[:10]) >= date.today()
    except ValueError:
        return True  # malformed expiry → treat as lifetime rather than lock out


class UsersMixin:
    def create_user(self, username: str, password_hash: str) -> int:
        from datetime import datetime, timezone, date, timedelta

        now = datetime.now(timezone.utc).isoformat()
        # New users get a 30-day Pro trial.
        trial_until = (date.today() + timedelta(days=30)).isoformat()
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO users (username, password_hash, created_at, is_pro, pro_until) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (username, password_hash, now, trial_until),
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
                "SELECT id, username, password_hash, created_at, is_admin, email, tg_chat_id, is_pro, pro_until, tg_chat_id_reset FROM users WHERE id = ?",
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
            "is_pro": _pro_active(row[7], row[8]),
            "pro_until": row[8],
            "tg_chat_id_reset": bool(row[9]),
        }

    def get_all_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.id,
                    u.username,
                    u.is_admin,
                    u.created_at,
                    u.last_login,
                    u.is_pro,
                    u.pro_until,
                    COUNT(DISTINCT p.id) AS portfolio_count,
                    COALESCE(MAX(ps.sync_enabled), 0) AS has_autosync,
                    CASE WHEN u.tg_chat_id IS NOT NULL THEN 1 ELSE 0 END AS has_tg,
                    CASE WHEN u.coupon_notif_enabled = 1 THEN 1 ELSE 0 END AS has_coupon_notif,
                    MAX(CASE WHEN p.share_token IS NOT NULL
                              AND (p.share_expires_at IS NULL
                                   OR p.share_expires_at > CAST(strftime('%s','now') AS INTEGER)) THEN 1 ELSE 0 END
                    ) AS has_sharing
                FROM users u
                LEFT JOIN portfolios p ON p.user_id = u.id
                LEFT JOIN portfolio_sync ps ON ps.portfolio_id = p.id
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
                "last_login": row[4],
                "is_pro": _pro_active(row[5], row[6]),
                "pro_until": row[6],
                "portfolio_count": int(row[7]),
                "has_autosync": bool(row[8]),
                "has_tg": bool(row[9]),
                "has_coupon_notif": bool(row[10]),
                "has_sharing": bool(row[11]) if row[11] is not None else False,
            }
            for row in rows
        ]

    def delete_user(self, user_id: int) -> int:
        with self._connect() as conn:
            # Подсчёт портфелей и элементов перед CASCADE-удалением
            portfolio_count = conn.execute(
                "SELECT COUNT(*) FROM portfolios WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            item_count = conn.execute(
                """SELECT COUNT(*) FROM portfolio_items
                   WHERE portfolio_id IN (SELECT id FROM portfolios WHERE user_id = ?)
                   AND deleted_at IS NULL""",
                (user_id,),
            ).fetchone()[0]
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            deleted = int(cursor.rowcount)
            logger.warning(
                "AUDIT delete_user: user_id=%d deleted=%d (CASCADE removed %d portfolios, %d items)",
                user_id, deleted, portfolio_count, item_count,
            )
            return deleted

    def set_user_admin(self, user_id: int, is_admin: bool) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?",
                (1 if is_admin else 0, user_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def set_user_pro(self, user_id: int, is_pro: bool, pro_until: str | None = None) -> int:
        """Grant/revoke Pro. pro_until = ISO date (None = lifetime when granting)."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET is_pro = ?, pro_until = ? WHERE id = ?",
                (1 if is_pro else 0, pro_until if is_pro else None, user_id),
            )
            conn.commit()
            logger.info("AUDIT set_user_pro: user_id=%d is_pro=%s until=%s", user_id, is_pro, pro_until)
            return int(cursor.rowcount)

    def update_user_password(self, user_id: int, password_hash: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_last_login(self, user_id: int) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
            conn.commit()

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
        """Записать chat_id пользователя; ввод нового гасит флаг-баннер.

        Сброс флага только при непустом значении: обнуление приходит и из
        автоматического снятия подписки, где баннер как раз нужен показать.
        """
        with self._connect() as conn:
            if tg_chat_id:
                cursor = conn.execute(
                    "UPDATE users SET tg_chat_id = ?, tg_chat_id_reset = 0 WHERE id = ?",
                    (tg_chat_id, user_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE users SET tg_chat_id = ? WHERE id = ?",
                    (tg_chat_id, user_id),
                )
            conn.commit()
            return int(cursor.rowcount)

    def clear_tg_chat_id(self, tg_chat_id: str) -> int:
        """Снять подписку у всех, у кого этот chat_id: бот заблокирован.

        Гасим по chat_id, а не по user_id: вызывающий (отправка) знает только
        адрес. Возвращает число затронутых строк — 0 означает, что адрес уже
        снят или принадлежит не пользователю, а глобальным настройкам.

        Ставим tg_chat_id_reset: отвязали не по воле пользователя, и он должен
        увидеть в интерфейсе, почему уведомления перестали приходить.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET tg_chat_id = NULL, tg_chat_id_reset = 1 WHERE tg_chat_id = ?",
                (tg_chat_id,),
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
