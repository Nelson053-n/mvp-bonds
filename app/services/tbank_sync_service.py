"""Background T-Bank portfolio sync service.

Handles decryption, sync execution, status updates, and concurrency guard.
Designed to be broker-agnostic in the future: other brokers can follow
the same pattern (do_sync_one + _XXXX_INTERVAL background loop).
"""

import logging
from datetime import datetime, timezone
from html import escape

from app.services.crypto_utils import decrypt_and_maybe_migrate
from app.services.notification_service import notification_service
from app.services.storage_service import storage_service
from app.services.tbank_service import TBankError, TBankService

logger = logging.getLogger(__name__)

_PENDING_REMOVAL_PREFIX = "PENDING_REMOVAL:"

# TTL for the cross-process sync lock. A sync (GetOperations + writes) should
# finish well within this; if a worker crashes mid-sync, the lock is reclaimed
# after this many seconds so the portfolio isn't stuck.
_SYNC_LOCK_TTL_SECONDS = 600


def parse_pending_removal(last_sync_error: str | None) -> list[str]:
    """Extract tickers from a PENDING_REMOVAL: prefixed error string."""
    if not last_sync_error or not last_sync_error.startswith(_PENDING_REMOVAL_PREFIX):
        return []
    payload = last_sync_error[len(_PENDING_REMOVAL_PREFIX):]
    return [t.strip() for t in payload.split(",") if t.strip()]


def _is_auth_error(message: str) -> bool:
    return "токен" in message.lower() or "401" in message


def _prev_error_is_real(portfolio_id: int) -> bool:
    """True, если синк УЖЕ был в ошибке. PENDING_REMOVAL — не сбой, а успешный синк."""
    cfg = storage_service.get_sync_config(portfolio_id) or {}
    prev = cfg.get("last_sync_error")
    return bool(prev) and not prev.startswith(_PENDING_REMOVAL_PREFIX)


async def _notify_sync_failed(portfolio_id: int, auth_error: bool) -> None:
    """Одно Telegram-сообщение владельцу при переходе «ок → ошибка».

    Вызывающий отвечает за проверку перехода. Любой сбой здесь только логируется:
    уведомление не должно ломать сам синк. Ретраи и отписку по 403/400 делает
    notification_service.send_telegram.
    """
    try:
        token = storage_service.get_setting("tg_bot_token", "")
        portfolio = storage_service.get_portfolio(portfolio_id)
        owner = storage_service.get_user_by_id(portfolio["user_id"]) if portfolio else None
        chat_id = (owner or {}).get("tg_chat_id")
        if not token or not chat_id:
            return
        name = escape(str(portfolio.get("name") or ""))
        if auth_error:
            text = (
                f"⚠️ Синхронизация портфеля «{name}» с Т-Банком не удалась: "
                "токен недействителен. Автосинхронизация отключена — "
                "обновите токен в настройках портфеля."
            )
        else:
            text = (
                f"⚠️ Синхронизация портфеля «{name}» с Т-Банком не удалась: "
                "Т-Банк временно недоступен. Приложение повторит попытку само."
            )
        await notification_service.send_telegram(token, str(chat_id), text)
    except Exception:
        logger.exception("sync_portfolio %d: не удалось отправить уведомление об ошибке", portfolio_id)


async def do_sync_one(portfolio_id: int, cfg: dict) -> dict:
    """Decrypt token, run sync, update status in DB.

    Returns result dict with keys: added, updated, removed_candidates, errors.
    Also sets {"skipped": True} if another sync for this portfolio is already running.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if not storage_service.acquire_sync_lock(portfolio_id, now_ts, _SYNC_LOCK_TTL_SECONDS):
        logger.debug("sync_portfolio %d: already in progress, skipping", portfolio_id)
        return {"skipped": True, "added": 0, "updated": 0, "removed_candidates": [], "errors": []}

    try:
        token, migrated_enc = decrypt_and_maybe_migrate(cfg["tbank_token_enc"])
        if migrated_enc is not None:
            # Token was stored under the legacy jwt_secret-derived key; persist
            # it re-encrypted under the dedicated key (lazy migration).
            storage_service.update_sync_token(portfolio_id, migrated_enc)
            logger.info("AUDIT tbank_token re-encrypted portfolio=%d", portfolio_id)
        svc = TBankService(token)
        result = await svc.sync_portfolio(
            portfolio_id=portfolio_id,
            account_id=cfg["tbank_account_id"],
            bonds_only=cfg["bonds_only"],
            storage=storage_service,
        )

        now_iso = datetime.now(timezone.utc).isoformat()

        # If there are removed candidates, store them in last_sync_error with special prefix
        if result["removed_candidates"]:
            error_str = _PENDING_REMOVAL_PREFIX + ",".join(result["removed_candidates"])
            storage_service.update_sync_status(portfolio_id, now_iso, error_str)
        else:
            storage_service.update_sync_status(portfolio_id, now_iso, None)

        return result

    except TBankError as exc:
        now_iso = datetime.now(timezone.utc).isoformat()
        error_msg = exc.message[:500]
        was_failing = _prev_error_is_real(portfolio_id)
        storage_service.update_sync_status(portfolio_id, now_iso, error_msg)
        # Disable sync on auth failure to avoid repeated 401s
        auth_error = _is_auth_error(exc.message)
        if auth_error:
            storage_service.set_sync_enabled(portfolio_id, False)
            logger.warning(
                "sync_portfolio %d: auth error, sync disabled: %s", portfolio_id, exc.message
            )
        if not was_failing:
            await _notify_sync_failed(portfolio_id, auth_error)
        raise
    except Exception as exc:
        now_iso = datetime.now(timezone.utc).isoformat()
        was_failing = _prev_error_is_real(portfolio_id)
        storage_service.update_sync_status(portfolio_id, now_iso, str(exc)[:500])
        if not was_failing:
            await _notify_sync_failed(portfolio_id, False)
        raise
    finally:
        storage_service.release_sync_lock(portfolio_id)
