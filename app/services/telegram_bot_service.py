"""Public Telegram bot for bond lookup by ticker/name.

Long-polling (getUpdates) loop that runs in the leader worker when
MVP_TG_BOT_TOKEN is set. Answers /start and free-text queries: a query is
matched against the cached MOEX bond list; an exact ticker shows a bond card,
several matches show inline-keyboard choices, no match offers the catalog.

No webhook / public route needed — Telegram is polled outbound over HTTPS.
The bot is read-only: it never touches user data or the DB.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from html import escape as h

import httpx

from app.config import settings
from app.exceptions import MOEXError
from app.services.moex_service import moex_service

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_BASE_URL = "https://bondai.ru"
_POLL_TIMEOUT = 30          # long-poll seconds
_HTTP_TIMEOUT = _POLL_TIMEOUT + 15
_MAX_CHOICES = 6            # inline buttons for ambiguous queries

_MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_CCY_SIGN = {"SUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "CNY": "¥", "CHF": "₣"}


def _fmt_date(d: date | None) -> str | None:
    if d is None:
        return None
    return f"{d.day} {_MONTHS_GEN[d.month]} {d.year}"


def _fmt_money(v: float | None, digits: int = 2) -> str | None:
    if v is None:
        return None
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def _freq_text(period_days: int | None) -> str | None:
    if not period_days or period_days <= 0:
        return None
    if period_days <= 35:
        return "ежемесячно"
    if period_days <= 100:
        return "ежеквартально"
    if period_days <= 200:
        return "раз в полгода"
    return "раз в год"


# ── Bond search over the shared cached list ──────────────────────────────────

async def _all_bonds() -> list[dict]:
    from app.api import bonds as bonds_api
    try:
        return await bonds_api._get_bonds_cached()
    except Exception as exc:
        logger.warning("tg-bot: bonds cache fetch failed: %s", exc)
        return []


async def _search(query: str) -> list[dict]:
    """Match query against ticker/name. Exact ticker match floats to the top."""
    q = query.strip().upper()
    if not q:
        return []
    bonds = await _all_bonds()
    exact, partial = [], []
    for b in bonds:
        ticker = (b.get("ticker") or "").upper()
        name = (b.get("name") or "").upper()
        if ticker == q:
            exact.append(b)
        elif q in ticker or q in name:
            partial.append(b)
    # Shorter names first among partials — usually the most relevant issue.
    partial.sort(key=lambda b: len(b.get("name") or ""))
    return exact + partial


# ── Message formatting ───────────────────────────────────────────────────────

_START_TEXT = (
    "👋 <b>Bond AI</b> — поиск облигаций Московской биржи.\n\n"
    "Пришлите <b>тикер</b> или <b>название</b> бумаги — покажу цену, доходность, "
    "купон, рейтинг и даты.\n\n"
    "Примеры:\n"
    "• <code>SU26238RMFS4</code>\n"
    "• <code>Сбер</code>\n"
    "• <code>ОФЗ 26240</code>\n\n"
    "📊 Каталог и калькуляторы: bondai.ru/bond"
)

_HELP_TEXT = (
    "Пришлите тикер или название облигации — например <code>SU26238RMFS4</code> "
    "или <code>Сбер</code>. Я найду бумагу и покажу её параметры.\n\n"
    "Команды: /start — начать, /help — помощь."
)


def _bond_kind(secid: str, board: str | None) -> str:
    if board == "TQOB" or secid.upper().startswith("SU"):
        return "ОФЗ"
    return "Корпоративная облигация"


def _card_text(s, board: str | None) -> str:
    """HTML-formatted bond card for Telegram (parse_mode=HTML)."""
    today = date.today()
    ccy = _CCY_SIGN.get(s.face_unit, s.face_unit)
    price_rub = (s.clean_price_percent / 100.0 * s.nominal) if s.nominal else None

    # Yield-to-offer caveat (same logic as the web pages).
    near_offer = None
    if s.market_yield and s.market_yield > 100:
        for d in (s.offer_date, s.buyback_date):
            if d and d >= today and (near_offer is None or d < near_offer):
                near_offer = d

    lines = [f"<b>{h(s.name)}</b>  <code>{h(s.ticker)}</code>",
             f"<i>{_bond_kind(s.ticker, board)}</i>", ""]

    price_line = f"💰 Цена: <b>{_fmt_money(s.clean_price_percent)}%</b>"
    if price_rub:
        price_line += f" (~{_fmt_money(price_rub)} {ccy})"
    lines.append(price_line)

    if s.market_yield:
        if near_offer:
            lines.append(f"📈 Доходность к оферте: <b>{_fmt_money(s.market_yield)}%</b>")
        else:
            lines.append(f"📈 Доходность (YTM): <b>{_fmt_money(s.market_yield)}%</b>")

    if s.coupon_rate:
        coup = f"🎫 Купон: <b>{_fmt_money(s.coupon_rate)}%</b>"
        if s.coupon:
            coup += f" ({_fmt_money(s.coupon)} ₽"
            freq = _freq_text(s.coupon_period)
            coup += f", {freq})" if freq else ")"
        if s.is_floater:
            coup += " — флоатер"
        lines.append(coup)

    if s.next_coupon_date and s.next_coupon_date >= today:
        lines.append(f"📅 Следующий купон: {_fmt_date(s.next_coupon_date)}")
    if s.aci is not None:
        lines.append(f"💵 НКД: {_fmt_money(s.aci)} ₽")
    if s.maturity_date:
        lines.append(f"🏁 Погашение: {_fmt_date(s.maturity_date)}")
    if s.offer_date and s.offer_date >= today:
        lines.append(f"🔁 Оферта: {_fmt_date(s.offer_date)}")
    if s.company_rating:
        lines.append(f"⭐ Рейтинг: <b>{h(s.company_rating)}</b>")
    if s.is_qual:
        lines.append("🔒 Только для квалифицированных инвесторов")

    if near_offer:
        lines.append("")
        lines.append("⚠️ Доходность показана к оферте, не к погашению.")

    lines.append("")
    lines.append("<i>Данные MOEX. Не индивидуальная инвестиционная рекомендация.</i>")
    return "\n".join(lines)


def _card_keyboard(secid: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "🔗 Открыть на bondai.ru", "url": f"{_BASE_URL}/bond/{secid}"},
    ]]}


def _choices_keyboard(matches: list[dict]) -> dict:
    rows = []
    for b in matches[:_MAX_CHOICES]:
        ticker = b["ticker"]
        label = f"{b.get('name') or ticker} · {ticker}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([{"text": label, "callback_data": f"b:{ticker}"}])
    return {"inline_keyboard": rows}


# ── Telegram API helpers ─────────────────────────────────────────────────────

class TelegramBotService:
    def __init__(self) -> None:
        self._token = ""
        self._running = False

    @property
    def enabled(self) -> bool:
        return bool(settings.tg_bot_token)

    async def _call(self, client: httpx.AsyncClient, method: str, **payload):
        url = _API.format(token=self._token, method=method)
        try:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                logger.warning("tg-bot %s failed: %s", method, data.get("description"))
            return data
        except Exception as exc:
            logger.warning("tg-bot %s error: %s", method, exc)
            return {"ok": False}

    async def _send(self, client, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        await self._call(client, "sendMessage", **payload)

    async def _answer_card(self, client, chat_id, secid: str):
        try:
            snap = await moex_service.get_bond_snapshot(secid)
        except (MOEXError, Exception) as exc:  # noqa: BLE001 - any failure → friendly msg
            logger.info("tg-bot: snapshot failed for %s: %s", secid, exc)
            await self._send(client, chat_id,
                             "Не удалось получить данные по этой бумаге. "
                             "Попробуйте другой тикер или каталог bondai.ru/bond")
            return
        board = None
        for b in await _all_bonds():
            if b["ticker"] == snap.ticker:
                board = b.get("board")
                break
        await self._send(client, chat_id, _card_text(snap, board), _card_keyboard(snap.ticker))

    async def _handle_query(self, client, chat_id, query: str):
        matches = await _search(query)
        if not matches:
            await self._send(
                client, chat_id,
                f"По запросу «{h(query)}» ничего не нашёл.\n\n"
                "Проверьте тикер или название, либо посмотрите каталог: bondai.ru/bond",
            )
            return
        # Exact single ticker, or a clearly dominant first match → card directly.
        if matches[0]["ticker"].upper() == query.strip().upper() or len(matches) == 1:
            await self._answer_card(client, chat_id, matches[0]["ticker"])
            return
        # Several matches → let the user pick.
        await self._send(
            client, chat_id,
            f"Нашёл {len(matches)} бумаг по запросу «{h(query)}». Выберите:",
            _choices_keyboard(matches),
        )

    async def _handle_update(self, client, update: dict):
        # Inline button press → bond card.
        cb = update.get("callback_query")
        if cb:
            await self._call(client, "answerCallbackQuery", callback_query_id=cb["id"])
            data = cb.get("data") or ""
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            if chat_id and data.startswith("b:"):
                await self._answer_card(client, chat_id, data[2:])
            return

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        if text.startswith("/start"):
            await self._send(client, chat_id, _START_TEXT)
        elif text.startswith("/help"):
            await self._send(client, chat_id, _HELP_TEXT)
        elif text.startswith("/"):
            await self._send(client, chat_id, _HELP_TEXT)
        else:
            await self._handle_query(client, chat_id, text)

    async def run_polling(self) -> None:
        """Long-poll Telegram getUpdates until cancelled. Safe to await in a task."""
        if not self.enabled:
            return
        self._token = settings.tg_bot_token
        self._running = True
        offset = 0
        logger.info("tg-bot: starting long-polling")
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            # Drop any webhook so getUpdates works; ignore failure.
            await self._call(client, "deleteWebhook", drop_pending_updates=False)
            while self._running:
                try:
                    data = await self._call(
                        client, "getUpdates",
                        offset=offset, timeout=_POLL_TIMEOUT,
                        allowed_updates=["message", "edited_message", "callback_query"],
                    )
                    if not data.get("ok"):
                        await asyncio.sleep(3)
                        continue
                    for upd in data.get("result", []):
                        offset = max(offset, upd["update_id"] + 1)
                        try:
                            await self._handle_update(client, upd)
                        except Exception:
                            logger.exception("tg-bot: handler error")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning("tg-bot: poll error: %s", exc)
                    await asyncio.sleep(3)
        logger.info("tg-bot: polling stopped")

    def stop(self) -> None:
        self._running = False


telegram_bot_service = TelegramBotService()
