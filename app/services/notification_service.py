"""
Telegram notification service.
Sends alerts when instrument ratings change, prices drop significantly,
or coupon payments are upcoming.
"""

import asyncio
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.models import InstrumentMetrics

logger = logging.getLogger(__name__)

# Retries for transient network failures. Уведомления одноразовые: купон,
# алерт и понижение рейтинга не повторяются следующим циклом, поэтому
# потерянный ConnectTimeout — это потерянное навсегда сообщение
# (4 таких за 11.08). Паузы короткие: отправка идёт внутри фоновых задач.
_RETRY_ATTEMPTS = 3
_RETRY_DELAYS = (1.0, 3.0)  # паузы между попытками 1→2 и 2→3


# Ratings from SmartLab may carry an assignment date: "A+ (12.05.2026)".
# Alerts compare and display the grade only — a date-only change is not news.
_TRUSTED_RATING_SOURCES = {"smartlab", "moex", "manual"}


def _grade(rating: str | None) -> str | None:
    """Bare rating grade without the assignment date: 'A+ (12.05.2026)' -> 'A+'."""
    if not rating:
        return None
    return rating.split("(")[0].strip() or None


def _is_real_rating_change(
    old_row: "InstrumentMetrics", new_row: "InstrumentMetrics"
) -> bool:
    """True only for a grade move observed within trustworthy sources.

    Silent when either side is missing, comes from the coarse 'listlevel'
    proxy or the stored 'db' value, or when only the source changed — those
    signal a rating-source outage, not an actual issuer downgrade/upgrade.
    """
    old_grade, new_grade = _grade(old_row.company_rating), _grade(new_row.company_rating)
    if not old_grade or not new_grade or old_grade == new_grade:
        return False
    old_src = getattr(old_row, "rating_source", None)
    new_src = getattr(new_row, "rating_source", None)
    if old_src not in _TRUSTED_RATING_SOURCES or new_src not in _TRUSTED_RATING_SOURCES:
        return False
    return True


class NotificationService:
    def _disable_subscription(self, chat_id: str | None, what: str) -> None:
        """Снять подписку после 403: бот заблокирован, слать туда больше нечего.

        Telegram отдаёт 403 не только на блокировку (бывает «chat not found»
        для удалённого аккаунта), но исход один: адрес недоставляем, и без
        сброса купонные напоминания уходят в стену каждые 6 часов.

        Глобальный app_settings.tg_chat_id не трогаем — это админский канал
        алертов о сбоях фоновых задач, потерять его молча дороже, чем
        продолжать в него стучаться.
        """
        if not chat_id:
            return
        from app.services.storage_service import storage_service

        try:
            rows = storage_service.clear_tg_chat_id(str(chat_id))
        except Exception:
            logger.exception("Failed to clear tg_chat_id after 403 (%s)", what)
            return
        if rows:
            logger.warning(
                "AUDIT tg_unsubscribe: chat_id=%s users=%d reason=403 (%s)",
                chat_id, rows, what,
            )

    async def _post_telegram(self, token: str, payload: dict, what: str) -> bool:
        """POST в Telegram Bot API с ретраем транзиентных сетевых сбоев.

        Ретраятся только ошибки соединения: HTTP-ответ означает, что Telegram
        нас услышал, и повтор ничего не изменит — 400 при кривой разметке или
        403 при блокировке бота повторятся так же, а долбёжка 429 усугубит
        rate-limit. 5xx на стороне Telegram — редкий случай, который закроет
        следующий цикл фоновой задачи.
        """
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.warning(
                        "Telegram API returned %d (%s): %s",
                        resp.status_code, what, resp.text[:200],
                    )
                    if resp.status_code == 403:
                        self._disable_subscription(payload.get("chat_id"), what)
                    return False
                return True
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt + 1 < _RETRY_ATTEMPTS:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(
                        "Telegram %s: %s (попытка %d/%d), повтор через %.1fс",
                        what, type(exc).__name__,
                        attempt + 1, _RETRY_ATTEMPTS, delay,
                    )
                    await asyncio.sleep(delay)
            except Exception:
                logger.exception("Failed to send Telegram message (%s)", what)
                return False
        logger.error(
            "Telegram недоступен после %d попыток (%s): %s",
            _RETRY_ATTEMPTS, what, last_exc,
        )
        return False

    async def send_telegram(
        self, token: str, chat_id: str, text: str
    ) -> bool:
        """Send a message via Telegram Bot API."""
        if not token or not chat_id:
            return False
        return await self._post_telegram(
            token,
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            "message",
        )

    async def check_and_notify(
        self,
        old_rows: "list[InstrumentMetrics]",
        new_rows: "list[InstrumentMetrics]",
        portfolio_id: int,
    ) -> None:
        """Compare old and new rows; alert the portfolio owner via Telegram."""
        from app.services.storage_service import storage_service

        s = storage_service.get_all_settings()
        token = s.get("tg_bot_token", "")
        if not token:
            return

        portfolio = storage_service.get_portfolio(portfolio_id)
        owner = (
            storage_service.get_user_by_id(portfolio["user_id"]) if portfolio else None
        )
        chat_id = (owner or {}).get("tg_chat_id")
        if not chat_id:
            return

        try:
            threshold = float(s.get("price_drop_threshold", "5.0"))
        except ValueError:
            threshold = 5.0

        lang = s.get("tg_lang", "ru")

        old_by_id: dict[int, "InstrumentMetrics"] = {r.id: r for r in old_rows}
        today = date.today().isoformat()
        # (dedup_key, text): the key collapses duplicates across workers,
        # portfolios of the same owner and repeated price flapping within a day
        messages: list[tuple[str, str]] = []
        # Rating changes are not sent straight away: each one is re-checked
        # against a freshly fetched rating before it becomes a message.
        rating_candidates: list[tuple[str, "InstrumentMetrics", "InstrumentMetrics"]] = []

        for new_row in new_rows:
            old_row = old_by_id.get(new_row.id)
            if old_row is None:
                continue

            # Rating change — only a genuine move within one trustworthy source.
            # A source outage (SmartLab down -> coarse 'listlevel' proxy, or the
            # value stored in DB) is NOT a rating change and must stay silent.
            if _is_real_rating_change(old_row, new_row):
                key = (
                    f"rating_change:{chat_id}:{new_row.ticker}:"
                    f"{_grade(old_row.company_rating)}:"
                    f"{_grade(new_row.company_rating)}:{today}"
                )
                rating_candidates.append((key, old_row, new_row))

            # Price drop
            if old_row.current_price > 0 and new_row.current_price > 0:
                drop_pct = (
                    (old_row.current_price - new_row.current_price)
                    / old_row.current_price
                    * 100
                )
                if drop_pct >= threshold:
                    key = f"price_drop:{chat_id}:{new_row.ticker}:{today}"
                    if lang == "en":
                        messages.append((
                            key,
                            f"\U0001f4c9 <b>Price drop</b>\n"
                            f"{new_row.name} ({new_row.ticker})\n"
                            f"Was: {old_row.current_price:.2f} \u2192 "
                            f"Now: {new_row.current_price:.2f} "
                            f"(-{drop_pct:.1f}%)"
                        ))
                    else:
                        messages.append((
                            key,
                            f"\U0001f4c9 <b>Просадка цены</b>\n"
                            f"{new_row.name} ({new_row.ticker})\n"
                            f"Было: {old_row.current_price:.2f} \u2192 "
                            f"Стало: {new_row.current_price:.2f} "
                            f"(-{drop_pct:.1f}%)"
                        ))

        # Verify every rating change against a freshly fetched rating before
        # telling the user anything. A flapping source (SmartLab timing out and
        # recovering minutes later) produced false "A+ -> BBB" alerts that were
        # then undone by an equally false "BBB -> A+" one.
        for key, old_row, new_row in rating_candidates:
            confirmed = await self._confirm_rating(new_row)
            if confirmed is None:
                logger.info(
                    "Rating alert suppressed for %s: re-check could not confirm "
                    "%s -> %s", new_row.ticker,
                    old_row.company_rating, new_row.company_rating,
                )
                continue
            if lang == "en":
                messages.append((
                    key,
                    f"\u26a0\ufe0f <b>Rating change</b>\n"
                    f"{new_row.name} ({new_row.ticker})\n"
                    f"{_grade(old_row.company_rating)} \u2192 {confirmed}"
                ))
            else:
                messages.append((
                    key,
                    f"\u26a0\ufe0f <b>Изменение рейтинга</b>\n"
                    f"{new_row.name} ({new_row.ticker})\n"
                    f"{_grade(old_row.company_rating)} \u2192 {confirmed}"
                ))

        for key, msg in messages:
            if not storage_service.try_mark_notification_sent(key):
                continue
            await self.send_telegram(token, chat_id, msg)

    async def _confirm_rating(self, new_row: "InstrumentMetrics") -> str | None:
        """Re-fetch the rating from its source and confirm the observed value.

        Returns the confirmed grade, or None when the change cannot be
        independently confirmed (source unreachable, or it now reports
        something else) — in which case no alert is sent.
        """
        # A rating the user set by hand needs no external confirmation.
        if getattr(new_row, "rating_source", None) == "manual":
            return _grade(new_row.company_rating)
        from app.services.moex_service import moex_service

        try:
            result = await moex_service.refresh_rating_with_sources(new_row.ticker)
        except Exception:
            logger.exception("Rating re-check failed for %s", new_row.ticker)
            return None
        fresh = _grade(result.get("best"))
        if fresh is None:
            return None
        return fresh if fresh == _grade(new_row.company_rating) else None

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
        return await self._post_telegram(
            token, {"chat_id": chat_id, "text": text}, f"coupon {ticker}"
        )

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

            # Get cached table data for this portfolio. Only warm caches are
            # useful here: rows() on a cold portfolio returns an empty list,
            # and refreshing it from a notification loop would hammer MOEX.
            cached = cache_service.rows(item_row["portfolio_id"])
            if not cached:
                continue

            current_price = None
            for cached_row in cached:
                if cached_row.id == item_id:
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
