"""
Telegram notification service.
Sends alerts when instrument ratings change, prices drop significantly,
or coupon payments are upcoming.
"""

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.models import InstrumentMetrics

logger = logging.getLogger(__name__)


class NotificationService:
    async def send_telegram(
        self, token: str, chat_id: str, text: str
    ) -> bool:
        """Send a message via Telegram Bot API."""
        if not token or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Telegram API returned %d: %s",
                        resp.status_code,
                        resp.text,
                    )
                    return False
                return True
        except Exception:
            logger.exception("Failed to send Telegram message")
            return False

    async def check_and_notify(
        self,
        old_rows: "list[InstrumentMetrics]",
        new_rows: "list[InstrumentMetrics]",
    ) -> None:
        """Compare old and new rows; send TG alerts for changes."""
        from app.services.storage_service import storage_service

        s = storage_service.get_all_settings()
        token = s.get("tg_bot_token", "")
        chat_id = s.get("tg_chat_id", "")
        if not token or not chat_id:
            return

        try:
            threshold = float(s.get("price_drop_threshold", "5.0"))
        except ValueError:
            threshold = 5.0

        lang = s.get("tg_lang", "ru")

        old_by_id: dict[int, "InstrumentMetrics"] = {r.id: r for r in old_rows}
        messages: list[str] = []

        for new_row in new_rows:
            old_row = old_by_id.get(new_row.id)
            if old_row is None:
                continue

            # Rating change
            if (
                old_row.company_rating
                and new_row.company_rating
                and old_row.company_rating != new_row.company_rating
            ):
                if lang == "en":
                    messages.append(
                        f"\u26a0\ufe0f <b>Rating change</b>\n"
                        f"{new_row.name} ({new_row.ticker})\n"
                        f"{old_row.company_rating} \u2192 {new_row.company_rating}"
                    )
                else:
                    messages.append(
                        f"\u26a0\ufe0f <b>Изменение рейтинга</b>\n"
                        f"{new_row.name} ({new_row.ticker})\n"
                        f"{old_row.company_rating} \u2192 {new_row.company_rating}"
                    )

            # Price drop
            if old_row.current_price > 0 and new_row.current_price > 0:
                drop_pct = (
                    (old_row.current_price - new_row.current_price)
                    / old_row.current_price
                    * 100
                )
                if drop_pct >= threshold:
                    if lang == "en":
                        messages.append(
                            f"\U0001f4c9 <b>Price drop</b>\n"
                            f"{new_row.name} ({new_row.ticker})\n"
                            f"Was: {old_row.current_price:.2f} \u2192 "
                            f"Now: {new_row.current_price:.2f} "
                            f"(-{drop_pct:.1f}%)"
                        )
                    else:
                        messages.append(
                            f"\U0001f4c9 <b>Просадка цены</b>\n"
                            f"{new_row.name} ({new_row.ticker})\n"
                            f"Было: {old_row.current_price:.2f} \u2192 "
                            f"Стало: {new_row.current_price:.2f} "
                            f"(-{drop_pct:.1f}%)"
                        )

        for msg in messages:
            await self.send_telegram(token, chat_id, msg)

    async def check_and_send_coupon_notifications(self) -> None:
        """Check all users with coupon notifications enabled and send Telegram alerts."""
        from app.services.storage_service import storage_service
        from app.services.portfolio_service import portfolio_service

        tg_token = storage_service.get_setting("tg_bot_token", "")
        if not tg_token:
            return

        users = storage_service.get_users_with_coupon_notifications()

        for user in users:
            user_id = user["id"]
            days_before = user["coupon_notif_days"]
            tg_chat_id = user.get("tg_chat_id")
            if not tg_chat_id:
                continue

            target_date = (date.today() + timedelta(days=days_before)).isoformat()

            portfolios = storage_service.get_portfolios(user_id)
            for portfolio in portfolios:
                try:
                    rows = await portfolio_service.get_table(portfolio["id"])
                except Exception as exc:
                    logger.warning(
                        "Failed to get table for portfolio %d: %s",
                        portfolio["id"], exc,
                    )
                    continue

                for row in rows:
                    if row.type != "bond":
                        continue
                    next_coupon = row.next_coupon_date
                    if not next_coupon:
                        continue
                    next_coupon_str = (
                        next_coupon.isoformat()
                        if hasattr(next_coupon, "isoformat")
                        else str(next_coupon)
                    )
                    if next_coupon_str != target_date:
                        continue
                    if storage_service.is_coupon_notification_sent(row.id, next_coupon_str):
                        continue

                    ok = await self._send_coupon_telegram(
                        tg_token,
                        tg_chat_id,
                        portfolio["name"],
                        row.ticker,
                        next_coupon_str,
                        row.coupon or 0,
                        row.quantity or 0,
                    )
                    if ok:
                        storage_service.mark_coupon_notification_sent(row.id, next_coupon_str)

    async def _send_coupon_telegram(
        self,
        token: str,
        chat_id: str,
        portfolio_name: str,
        ticker: str,
        coupon_date: str,
        coupon_amount: float,
        quantity: float,
    ) -> bool:
        """Send a coupon payment reminder via Telegram."""
        total = (coupon_amount or 0) * (quantity or 0)
        text = (
            f"\U0001f514 Купонный платёж\n\n"
            f"Портфель: {portfolio_name}\n"
            f"Бумага: {ticker}\n"
            f"Дата выплаты: {coupon_date}\n"
            f"Купон за 1 бумагу: {coupon_amount:.2f} \u20bd\n"
            f"Количество: {quantity:.0f}\n"
            f"Ожидаемая выплата: {total:.2f} \u20bd"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
                if r.status_code != 200:
                    logger.warning(
                        "Telegram coupon notification returned %d: %s",
                        r.status_code, r.text,
                    )
                    return False
                return True
        except Exception as exc:
            logger.warning("Failed to send coupon notification: %s", exc)
            return False

    async def check_price_alerts(self) -> None:
        """Check all active price alerts and send Telegram notifications."""
        from app.services.storage_service import storage_service

        tg_token = storage_service.get_setting("tg_bot_token", "")
        if not tg_token:
            return

        alerts = storage_service.get_all_active_price_alerts()
        if not alerts:
            return

        from app.services.cache_service import cache_service

        for alert in alerts:
            item_id = alert["item_id"]

            # Get portfolio_id from storage to look up cache
            item_row = None
            with storage_service._connect() as conn:
                row = conn.execute(
                    "SELECT portfolio_id, ticker FROM portfolio_items WHERE id = ?",
                    (item_id,)
                ).fetchone()
                if row:
                    item_row = {"portfolio_id": row[0], "ticker": row[1]}

            if not item_row:
                continue

            # Get cached table data for this portfolio
            cached = cache_service.get(item_row["portfolio_id"])
            if not cached:
                continue

            current_price = None
            for cached_row in cached:
                if hasattr(cached_row, 'id') and cached_row.id == item_id:
                    current_price = cached_row.current_price
                    break

            if current_price is None:
                continue

            target = alert["target_price"]
            triggered = False
            if alert["alert_type"] == "above" and current_price >= target:
                triggered = True
            elif alert["alert_type"] == "below" and current_price <= target:
                triggered = True

            if triggered:
                direction = "выше" if alert["alert_type"] == "above" else "ниже"
                text = (
                    f"\U0001f3af Ценовой алерт сработал!\n\n"
                    f"Бумага: {alert['ticker']}\n"
                    f"Текущая цена: {current_price:.2f}\n"
                    f"Целевая цена ({direction}): {target:.2f}"
                )
                ok = await self.send_telegram(tg_token, alert["tg_chat_id"], text)
                if ok:
                    storage_service.mark_price_alert_triggered(alert["id"])


notification_service = NotificationService()
