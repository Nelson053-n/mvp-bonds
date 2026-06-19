"""Portfolio items mixin: CRUD for portfolio_items + rating history."""
import logging

logger = logging.getLogger(__name__)


class ItemsMixin:
    def add_item(
        self,
        ticker: str,
        instrument_type: str,
        quantity: float,
        purchase_price: float,
        portfolio_id: int,
        source: str = "manual",
        figi: str | None = None,
        purchase_date: str | None = None,
        custom_name: str | None = None,
        custom_price: float | None = None,
        custom_nominal: float | None = None,
        custom_coupon_freq: int | None = None,
        custom_maturity: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO portfolio_items (
                    portfolio_id, ticker, instrument_type, quantity, purchase_price, source, figi,
                    purchase_date, custom_name, custom_price, custom_nominal, custom_coupon_freq,
                    custom_maturity
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (portfolio_id, ticker, instrument_type, quantity, purchase_price, source, figi,
                 purchase_date, custom_name, custom_price, custom_nominal, custom_coupon_freq,
                 custom_maturity),
            )
            conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("Не удалось получить id добавленной записи")
            item_id = int(cursor.lastrowid)
            logger.info(
                "AUDIT add_item: portfolio_id=%d ticker=%s type=%s qty=%.4f price=%.4f source=%s -> item_id=%d",
                portfolio_id, ticker, instrument_type, quantity, purchase_price, source, item_id,
            )
            return item_id

    def get_items(self, portfolio_id: int) -> list[dict[str, int | str | float]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, instrument_type, quantity, purchase_price
                     , manual_coupon, company_rating, manual_coupon_rate, figi, purchase_date
                     , source, custom_name, custom_price
                     , custom_nominal, custom_coupon_freq, custom_maturity, manual_rating
                FROM portfolio_items
                WHERE portfolio_id = ? AND deleted_at IS NULL
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
                "figi": row[8],
                "purchase_date": row[9],
                "source": row[10],
                "custom_name": row[11],
                "custom_price": float(row[12]) if row[12] is not None else None,
                "custom_nominal": float(row[13]) if row[13] is not None else None,
                "custom_coupon_freq": int(row[14]) if row[14] is not None else None,
                "custom_maturity": row[15],
                "manual_rating": row[16],
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
                      AND deleted_at IS NULL
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
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE portfolio_items SET deleted_at = ? WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL",
                (now, item_id, portfolio_id),
            )
            conn.commit()
            deleted = int(cursor.rowcount)
            logger.info(
                "AUDIT delete_item (soft): item_id=%d portfolio_id=%d deleted=%d",
                item_id, portfolio_id, deleted,
            )
            if deleted == 0:
                logger.warning("AUDIT delete_item: item_id=%d NOT FOUND in portfolio_id=%d", item_id, portfolio_id)
            return deleted

    def update_item(
        self,
        item_id: int,
        portfolio_id: int,
        quantity: float,
        purchase_price: float,
        figi: str | None = None,
        purchase_date: str | None = None,
        custom_price: float | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET quantity = ?, purchase_price = ?,
                    figi = COALESCE(?, figi),
                    purchase_date = COALESCE(?, purchase_date),
                    custom_price = COALESCE(?, custom_price)
                WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL
                """,
                (quantity, purchase_price, figi, purchase_date, custom_price, item_id, portfolio_id),
            )
            conn.commit()
            updated = int(cursor.rowcount)
            logger.info(
                "AUDIT update_item: item_id=%d portfolio_id=%d qty=%.4f price=%.4f updated=%d",
                item_id, portfolio_id, quantity, purchase_price, updated,
            )
            return updated

    def update_coupon(self, item_id: int, portfolio_id: int, coupon: float) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE portfolio_items
                SET manual_coupon = ?
                WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL
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
                WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL
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
                "UPDATE portfolio_items SET company_rating = ? WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL",
                (rating, item_id, portfolio_id),
            )
            conn.commit()

    def update_manual_rating(
        self, item_id: int, portfolio_id: int, rating: str | None
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE portfolio_items SET manual_rating = ? WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL",
                (rating, item_id, portfolio_id),
            )
            conn.commit()
            updated = int(cursor.rowcount)
            logger.info(
                "AUDIT update_manual_rating: item_id=%d portfolio_id=%d rating=%s updated=%d",
                item_id, portfolio_id, rating, updated,
            )
            return updated

    def save_rating_history(self, ticker: str, rating: str, source: str) -> None:
        """Save a rating observation to history (deduplicate: skip if same as last entry)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            last = conn.execute(
                """SELECT rating FROM rating_history
                   WHERE ticker = ? AND source = ?
                   ORDER BY recorded_at DESC LIMIT 1""",
                (ticker, source),
            ).fetchone()
            if last and last[0] == rating:
                return  # no change — skip
            conn.execute(
                "INSERT INTO rating_history (ticker, rating, source, recorded_at) VALUES (?, ?, ?, ?)",
                (ticker, rating, source, now),
            )
            conn.commit()

    def get_recent_rating_history(self, ticker: str, source: str, limit: int = 3) -> list[str]:
        """Return last N distinct-consecutive ratings for ticker+source (newest first)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT rating FROM rating_history
                   WHERE ticker = ? AND source = ?
                   ORDER BY recorded_at DESC LIMIT ?""",
                (ticker, source, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def update_rating_all_items_for_ticker(self, ticker: str, rating: str) -> None:
        """Update company_rating for ALL portfolio_items with given ticker."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio_items SET company_rating = ? WHERE ticker = ?",
                (rating, ticker),
            )
            conn.commit()

    def update_snapshot_data(
        self,
        item_id: int,
        portfolio_id: int,
        rating: str | None,
        coupon_rate: float | None,
    ) -> None:
        """Persist market snapshot data (rating + snapshot_coupon_rate) for risk calculation.

        snapshot_coupon_rate is the MOEX coupon rate used only for portfolio risk
        calculation — it never overwrites manual_coupon_rate set by the user.
        """
        with self._connect() as conn:
            if rating is not None:
                conn.execute(
                    """UPDATE portfolio_items
                       SET company_rating = ?,
                           snapshot_coupon_rate = ?
                       WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL""",
                    (rating, coupon_rate, item_id, portfolio_id),
                )
            elif coupon_rate is not None:
                # Update only coupon_rate, keep existing rating
                conn.execute(
                    """UPDATE portfolio_items
                       SET snapshot_coupon_rate = ?
                       WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL""",
                    (coupon_rate, item_id, portfolio_id),
                )
            conn.commit()

    def delete_items(self, item_ids: list[int], portfolio_id: int) -> int:
        if not item_ids:
            return 0

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        placeholders = ",".join(["?"] * len(item_ids))
        query = (
            "UPDATE portfolio_items SET deleted_at = ? "
            f"WHERE id IN ({placeholders}) AND portfolio_id = ? AND deleted_at IS NULL"
        )
        with self._connect() as conn:
            cursor = conn.execute(query, [now] + item_ids + [portfolio_id])
            conn.commit()
            deleted = int(cursor.rowcount)
            logger.info(
                "AUDIT delete_items (soft): item_ids=%s portfolio_id=%d deleted=%d",
                item_ids, portfolio_id, deleted,
            )
            if deleted != len(item_ids):
                logger.warning(
                    "AUDIT delete_items: requested %d items, deleted %d (portfolio_id=%d)",
                    len(item_ids), deleted, portfolio_id,
                )
            return deleted

    def get_deleted_items(self, portfolio_id: int) -> list[dict]:
        """Return soft-deleted items for a portfolio (for recovery)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, ticker, instrument_type, quantity, purchase_price, deleted_at
                   FROM portfolio_items
                   WHERE portfolio_id = ? AND deleted_at IS NOT NULL
                   ORDER BY deleted_at DESC""",
                (portfolio_id,),
            ).fetchall()
        return [
            {
                "id": int(r[0]), "ticker": r[1], "instrument_type": r[2],
                "quantity": float(r[3]), "purchase_price": float(r[4]),
                "deleted_at": r[5],
            }
            for r in rows
        ]

    def restore_item(self, item_id: int, portfolio_id: int) -> int:
        """Restore a soft-deleted item."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE portfolio_items SET deleted_at = NULL WHERE id = ? AND portfolio_id = ? AND deleted_at IS NOT NULL",
                (item_id, portfolio_id),
            )
            conn.commit()
            restored = int(cursor.rowcount)
            logger.info(
                "AUDIT restore_item: item_id=%d portfolio_id=%d restored=%d",
                item_id, portfolio_id, restored,
            )
            return restored

    def get_item(self, item_id: int, portfolio_id: int) -> dict | None:
        """Get a single portfolio item by id and portfolio_id."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM portfolio_items WHERE id = ? AND portfolio_id = ? AND deleted_at IS NULL",
                (item_id, portfolio_id)
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))
