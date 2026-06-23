"""Cross-process advisory lock for T-Bank portfolio sync.

Guards against two uvicorn workers syncing the same portfolio concurrently:
the old in-memory `_sync_in_progress` set was process-local and couldn't.
do_sync_one now takes a SQLite advisory lock (sync_locks table) instead.
"""

import asyncio

import pytest

from app.services import tbank_sync_service
from app.services.storage_service import storage_service

CFG = {
    "tbank_token_enc": "enc-token",
    "tbank_account_id": "acc-1",
    "bonds_only": False,
}


@pytest.fixture
def portfolio_ids(settings_override, request) -> list[int]:
    """Two real portfolios (sync_locks.portfolio_id has a FK to portfolios).

    Session-scoped DB is shared across tests, so usernames must be unique.
    """
    uid = storage_service.create_user(f"lock-{request.node.name}", "x")
    return [storage_service.create_portfolio(uid, f"P{i}") for i in range(2)]


class TestSyncLockStorage:
    """Direct unit tests for the advisory lock primitives."""

    def test_acquire_then_second_blocked(self, portfolio_ids: list[int]) -> None:
        pid = portfolio_ids[0]
        assert storage_service.acquire_sync_lock(pid, now_ts=1000, ttl_seconds=600) is True
        # Second acquire while the first is still held (within TTL) is refused.
        assert storage_service.acquire_sync_lock(pid, now_ts=1100, ttl_seconds=600) is False

    def test_release_allows_reacquire(self, portfolio_ids: list[int]) -> None:
        pid = portfolio_ids[0]
        assert storage_service.acquire_sync_lock(pid, now_ts=1000, ttl_seconds=600) is True
        storage_service.release_sync_lock(pid)
        assert storage_service.acquire_sync_lock(pid, now_ts=1001, ttl_seconds=600) is True

    def test_stale_lock_reclaimed_after_ttl(self, portfolio_ids: list[int]) -> None:
        pid = portfolio_ids[0]
        assert storage_service.acquire_sync_lock(pid, now_ts=1000, ttl_seconds=600) is True
        # A crashed worker never released; after TTL the lock is reclaimable.
        assert storage_service.acquire_sync_lock(pid, now_ts=1700, ttl_seconds=600) is True

    def test_different_portfolios_independent(self, portfolio_ids: list[int]) -> None:
        assert storage_service.acquire_sync_lock(portfolio_ids[0], 1000, 600) is True
        assert storage_service.acquire_sync_lock(portfolio_ids[1], 1000, 600) is True


class _GatedTBankService:
    """Fake TBankService whose sync_portfolio holds the lock until released."""

    started: asyncio.Event
    release: asyncio.Event

    def __init__(self, token: str) -> None:
        pass

    async def sync_portfolio(self, **kwargs) -> dict:
        _GatedTBankService.started.set()
        await _GatedTBankService.release.wait()
        return {"added": 1, "updated": 0, "removed_candidates": [], "errors": []}


@pytest.mark.asyncio
async def test_concurrent_do_sync_one_second_is_skipped(
    portfolio_ids: list[int], monkeypatch
) -> None:
    """Two concurrent do_sync_one for the same portfolio: the second is skipped."""
    pid = portfolio_ids[0]
    storage_service.upsert_sync_config(pid, "enc", "abcd", "acc-1", bonds_only=False)

    monkeypatch.setattr(tbank_sync_service, "decrypt_and_maybe_migrate", lambda *a, **k: ("tok", None))
    monkeypatch.setattr(tbank_sync_service, "TBankService", _GatedTBankService)
    _GatedTBankService.started = asyncio.Event()
    _GatedTBankService.release = asyncio.Event()

    # Sync #1 acquires the lock and blocks inside sync_portfolio.
    first = asyncio.create_task(tbank_sync_service.do_sync_one(pid, CFG))
    await asyncio.wait_for(_GatedTBankService.started.wait(), timeout=5)

    # Sync #2 runs while #1 holds the lock → must be skipped, never reaching sync_portfolio.
    second = await tbank_sync_service.do_sync_one(pid, CFG)
    assert second["skipped"] is True
    assert second["added"] == 0

    # Let #1 finish; it completes normally and releases the lock.
    _GatedTBankService.release.set()
    first_result = await asyncio.wait_for(first, timeout=5)
    assert first_result["added"] == 1
    assert "skipped" not in first_result

    # Lock released → a fresh sync can acquire again.
    assert storage_service.acquire_sync_lock(pid, 9999, 600) is True
    storage_service.release_sync_lock(pid)


@pytest.mark.asyncio
async def test_lock_released_after_sync_error(
    portfolio_ids: list[int], monkeypatch
) -> None:
    """A failing sync still releases the lock (finally), so retries aren't stuck."""
    pid = portfolio_ids[0]
    monkeypatch.setattr(tbank_sync_service, "decrypt_and_maybe_migrate", lambda *a, **k: ("tok", None))

    class _Boom:
        def __init__(self, token: str) -> None:
            pass

        async def sync_portfolio(self, **kwargs) -> dict:
            raise RuntimeError("network down")

    monkeypatch.setattr(tbank_sync_service, "TBankService", _Boom)

    with pytest.raises(RuntimeError):
        await tbank_sync_service.do_sync_one(pid, CFG)

    assert storage_service.acquire_sync_lock(pid, 9999, 600) is True
    storage_service.release_sync_lock(pid)
