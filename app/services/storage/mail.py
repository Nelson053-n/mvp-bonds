"""Mail mixin: письма ящика поддержки (входящие по IMAP и отправленные из админки)."""

_COLS = ("id, direction, msg_id, from_addr, to_addr, cc, subject, body, in_reply_to, date, is_read, "
         "answered, mailbox")
_LIST_COLS = ("id, direction, from_addr, to_addr, subject, substr(body, 1, 160), date, is_read, answered, "
              "mailbox")
_LIST_KEYS = ("id", "direction", "from_addr", "to_addr", "subject", "snippet", "date", "is_read",
              "answered", "mailbox")


class MailMixin:
    def add_mail(self, direction: str, msg_id: str | None, from_addr: str, to_addr: str,
                 subject: str, body: str, date: str, cc: str = "",
                 in_reply_to: str | None = None, is_read: bool = False,
                 mailbox: str = "support") -> int | None:
        """Новое письмо; None — такое (direction, msg_id) уже есть.

        mailbox — ящик, на который письмо пришло (support / alert).
        """
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO mail_messages "
                "(direction, msg_id, from_addr, to_addr, cc, subject, body, in_reply_to, date, is_read, "
                "mailbox) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (direction, msg_id, from_addr, to_addr, cc, subject, body, in_reply_to, date,
                 int(is_read), mailbox),
            )
            conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def list_mail(self, direction: str, limit: int = 50, offset: int = 0,
                  mailbox: str | None = None) -> list[dict]:
        sql = f"SELECT {_LIST_COLS} FROM mail_messages WHERE direction = ?"
        args: list = [direction]
        if mailbox:
            sql += " AND mailbox = ?"
            args.append(mailbox)
        sql += " ORDER BY date DESC, id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(zip(_LIST_KEYS, r)) for r in rows]

    def get_mail(self, mail_id: int) -> dict | None:
        with self._connect() as conn:
            r = conn.execute(f"SELECT {_COLS} FROM mail_messages WHERE id = ?", (mail_id,)).fetchone()
        return dict(zip(_COLS.replace(" ", "").split(","), r)) if r else None

    def set_mail_read(self, mail_id: int, read: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE mail_messages SET is_read = ? WHERE id = ?", (int(read), mail_id))
            conn.commit()
            return cur.rowcount > 0

    def count_unread_mail(self, mailbox: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM mail_messages WHERE direction = 'in' AND is_read = 0"
        args: list = []
        if mailbox:
            sql += " AND mailbox = ?"
            args.append(mailbox)
        with self._connect() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def set_mail_answered(self, mail_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE mail_messages SET answered = 1 WHERE id = ?", (mail_id,))
            conn.commit()

    def delete_mail(self, mail_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM mail_messages WHERE id = ?", (mail_id,))
            conn.commit()
            return cur.rowcount > 0
