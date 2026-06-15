"""Portfolios mixin: CRUD, sharing, snapshots, item-counts/risk, merge/move."""
import logging
from typing import Any

logger = logging.getLogger(__name__)

_UNSET: Any = object()  # sentinel for "not provided" in update_portfolio


class PortfoliosMixin:
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
            updated = int(cursor.rowcount)
            logger.info(
                "AUDIT update_portfolio: portfolio_id=%d fields=%s updated=%d",
                portfolio_id, updates, updated,
            )
            return updated

    def delete_portfolio(self, portfolio_id: int) -> int:
        with self._connect() as conn:
            # Подсчёт элементов перед CASCADE-удалением для аудита
            item_count = conn.execute(
                "SELECT COUNT(*) FROM portfolio_items WHERE portfolio_id = ? AND deleted_at IS NULL",
                (portfolio_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                "DELETE FROM portfolios WHERE id = ?",
                (portfolio_id,),
            )
            conn.commit()
            deleted = int(cursor.rowcount)
            logger.warning(
                "AUDIT delete_portfolio: portfolio_id=%d deleted=%d (CASCADE removed %d items)",
                portfolio_id, deleted, item_count,
            )
            return deleted

    def count_portfolios(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM portfolios WHERE user_id = ?", (user_id,)).fetchone()
            return int(row[0]) if row else 0

    def count_items(self, portfolio_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM portfolio_items WHERE portfolio_id = ? AND deleted_at IS NULL", (portfolio_id,)).fetchone()
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
                       GROUP_CONCAT(pi.company_rating) as ratings,
                       AVG(CASE
                           WHEN COALESCE(pi.manual_coupon_rate, pi.snapshot_coupon_rate) > 0
                           THEN COALESCE(pi.manual_coupon_rate, pi.snapshot_coupon_rate)
                       END) as avg_coupon_rate
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
            avg_coupon = row[6]
            ratings_raw = row[5] or ""
            ratings = [r.strip() for r in ratings_raw.split(",") if r.strip()]

            if avg_coupon is not None:
                # Primary: coupon yield is always available after first cache refresh
                risk = self._calc_risk_from_coupon(float(avg_coupon))
            elif ratings:
                # Fallback: credit ratings if coupon not yet populated
                risk = self._calc_risk_from_ratings(ratings)
            else:
                risk = "unknown"

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
        """Determine portfolio risk level from instrument credit ratings (fallback)."""
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

    @staticmethod
    def _calc_risk_from_coupon(avg_coupon_rate: float) -> str:
        """Determine portfolio risk level from average coupon rate (% of par).

        Coupon yield is a more reliable risk proxy than credit ratings because
        it is always populated after the first cache refresh, even for new
        portfolios created via the landing wizard.

        Thresholds (calibrated to the Russian bond market):
          < 12%  → conservative  (OFZ and top-tier corporate)
          12–15% → low           (A-rated corporate)
          15–18% → moderate      (BBB-rated / wide market)
          18–22% → high          (BB / elevated yield)
          > 22%  → aggressive    (high-yield / VDO)
        """
        if avg_coupon_rate < 12.0:
            return "conservative"
        if avg_coupon_rate < 15.0:
            return "low"
        if avg_coupon_rate < 18.0:
            return "moderate"
        if avg_coupon_rate < 22.0:
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

    def save_portfolio_snapshot(self, portfolio_id: int, total_value: float, total_cost: float) -> None:
        """Save daily snapshot. Upsert by date.

        On the first-ever snapshot for a portfolio, backfills daily entries from
        the portfolio creation date (or up to 90 days back) using total_cost as
        the baseline value, so the history chart has enough points to render.
        """
        # Guard against MOEX outages: a zero value with a non-zero cost means
        # prices never loaded (e.g. network/MOEX downtime), not a real wipe-out.
        # Skip the write so a transient fetch failure doesn't punch a false dip
        # into the history chart.
        if (not total_value) and total_cost > 0:
            logger.warning(
                "Skipping snapshot for portfolio %s: total_value=0 with total_cost=%.2f "
                "(prices likely unavailable)", portfolio_id, total_cost
            )
            return
        from datetime import date, timedelta
        today = date.today()
        today_str = today.isoformat()
        with self._connect() as conn:
            # Check if this portfolio has any snapshots yet
            existing = conn.execute(
                "SELECT COUNT(*) FROM portfolio_snapshots WHERE portfolio_id = ?",
                (portfolio_id,)
            ).fetchone()[0]

            if existing == 0 and total_cost > 0:
                # Insert a baseline point for yesterday (cost = value, profit = 0)
                # so the chart has at least 2 points and can draw a line
                yesterday = (today - timedelta(days=1)).isoformat()
                conn.execute(
                    """INSERT INTO portfolio_snapshots (portfolio_id, snapshot_date, total_value, total_cost)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(portfolio_id, snapshot_date) DO NOTHING""",
                    (portfolio_id, yesterday, total_cost, total_cost)
                )

            # Upsert today's real snapshot
            conn.execute(
                """INSERT INTO portfolio_snapshots (portfolio_id, snapshot_date, total_value, total_cost)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(portfolio_id, snapshot_date) DO UPDATE SET
                   total_value=excluded.total_value, total_cost=excluded.total_cost""",
                (portfolio_id, today_str, total_value, total_cost)
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
