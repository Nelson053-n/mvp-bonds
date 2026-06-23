"""Background T-Bank portfolio sync service.

Handles decryption, sync execution, status updates, and concurrency guard.
Designed to be broker-agnostic in the future: other brokers can follow
the same pattern (do_sync_one + _XXXX_INTERVAL background loop).
"""

import logging
from datetime import datetime, timezone

from app.services.crypto_utils import decrypt_and_maybe_migrate
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
        storage_service.update_sync_status(portfolio_id, now_iso, error_msg)
        # Disable sync on auth failure to avoid repeated 401s
        if "токен" in exc.message.lower() or "401" in exc.message:
            storage_service.set_sync_enabled(portfolio_id, False)
            logger.warning(
                "sync_portfolio %d: auth error, sync disabled: %s", portfolio_id, exc.message
            )
        raise
    except Exception as exc:
        now_iso = datetime.now(timezone.utc).isoformat()
        storage_service.update_sync_status(portfolio_id, now_iso, str(exc)[:500])
        raise
    finally:
        storage_service.release_sync_lock(portfolio_id)
