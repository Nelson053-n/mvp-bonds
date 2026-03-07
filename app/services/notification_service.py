"""
Telegram notification service.
Sends alerts when instrument ratings change or prices drop significantly.
"""

import logging
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


notification_service = NotificationService()
