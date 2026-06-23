"""Tests for do_sync_one status-recording + PENDING_REMOVAL round-trip.

do_sync_one carries money-critical side effects that aren't covered by the
lock tests: it records last_sync_error (None on success, PENDING_REMOVAL:
prefix for removal candidates, str(exc)[:500] on failure), auto-disables sync
on auth errors, and re-raises so the background loop sees the failure.

The advisory lock methods use real portfolios (FK on sync_locks.portfolio_id),
so we create real portfolios but spy on update_sync_status / set_sync_enabled
to assert exactly what gets recorded.
"""

import pytest

from app.services import tbank_sync_service
from app.services.storage_service import storage_service
from app.services.tbank_service import TBankError

CFG = {
    "tbank_token_enc": "enc-token",
    "tbank_account_id": "acc-1",
    "bonds_only": False,
}


@pytest.fixture
def portfolio_id(settings_override, request) -> int:
    """A real portfolio (sync_locks.portfolio_id has a FK to portfolios)."""
    uid = storage_service.create_user(f"svc-{request.node.name}", "x")
    return storage_service.create_portfolio(uid, "P")


@pytest.fixture
def status_spy(monkeypatch):
    """Capture update_sync_status / set_sync_enabled calls without touching DB."""
    calls = {"status": [], "enabled": []}
    monkeypatch.setattr(
        storage_service,
        "update_sync_status",
        lambda pid, at, err: calls["status"].append((pid, at, err)),
    )
    monkeypatch.setattr(
        storage_service,
        "set_sync_enabled",
        lambda pid, enabled: calls["enabled"].append((pid, enabled)),
    )
    monkeypatch.setattr(
        tbank_sync_service, "decrypt_and_maybe_migrate", lambda *a, **k: ("tok", None)
    )
    return calls


def _fake_tbank(result=None, exc=None):
    """Build a fake TBankService class whose sync_portfolio returns/raises."""

    class _Fake:
        def __init__(self, token: str) -> None:
            pass

        async def sync_portfolio(self, **kwargs) -> dict:
            if exc is not None:
                raise exc
            return result

    return _Fake


# --- PENDING_REMOVAL parsing (pure function) --------------------------------

class TestParsePendingRemoval:
    def test_none_returns_empty(self) -> None:
        assert tbank_sync_service.parse_pending_removal(None) == []

    def test_plain_error_returns_empty(self) -> None:
        assert tbank_sync_service.parse_pending_removal("боярышник 401") == []

    def test_parses_prefixed_tickers(self) -> None:
        assert tbank_sync_service.parse_pending_removal(
            "PENDING_REMOVAL:SU26240,RU000A105"
        ) == ["SU26240", "RU000A105"]

    def test_strips_whitespace_and_skips_blanks(self) -> None:
        assert tbank_sync_service.parse_pending_removal(
            "PENDING_REMOVAL: SU26240 , , RU000A105 "
        ) == ["SU26240", "RU000A105"]


# --- do_sync_one status recording -------------------------------------------

@pytest.mark.asyncio
async def test_success_records_no_error(portfolio_id, status_spy, monkeypatch) -> None:
    result = {"added": 2, "updated": 1, "removed_candidates": [], "errors": []}
    monkeypatch.setattr(tbank_sync_service, "TBankService", _fake_tbank(result=result))

    out = await tbank_sync_service.do_sync_one(portfolio_id, CFG)

    assert out == result
    assert len(status_spy["status"]) == 1
    pid, _at, err = status_spy["status"][0]
    assert pid == portfolio_id
    assert err is None
    assert status_spy["enabled"] == []


@pytest.mark.asyncio
async def test_removed_candidates_written_and_parsed_back(
    portfolio_id, status_spy, monkeypatch
) -> None:
    """removed_candidates are encoded into last_sync_error and decode back."""
    candidates = ["SU26240", "RU000A105"]
    result = {"added": 0, "updated": 0, "removed_candidates": candidates, "errors": []}
    monkeypatch.setattr(tbank_sync_service, "TBankService", _fake_tbank(result=result))

    out = await tbank_sync_service.do_sync_one(portfolio_id, CFG)

    assert out["removed_candidates"] == candidates
    _pid, _at, err = status_spy["status"][0]
    assert err == "PENDING_REMOVAL:SU26240,RU000A105"
    # Round-trip: the recorded string parses back to the original tickers.
    assert tbank_sync_service.parse_pending_removal(err) == candidates


@pytest.mark.asyncio
async def test_auth_error_disables_sync_and_reraises(
    portfolio_id, status_spy, monkeypatch
) -> None:
    exc = TBankError("Неверный токен (401 Unauthorized)")
    monkeypatch.setattr(tbank_sync_service, "TBankService", _fake_tbank(exc=exc))

    with pytest.raises(TBankError):
        await tbank_sync_service.do_sync_one(portfolio_id, CFG)

    _pid, _at, err = status_spy["status"][0]
    assert err == "Неверный токен (401 Unauthorized)"
    assert status_spy["enabled"] == [(portfolio_id, False)]


@pytest.mark.asyncio
async def test_non_auth_tbank_error_keeps_sync_enabled(
    portfolio_id, status_spy, monkeypatch
) -> None:
    exc = TBankError("MOEX недоступен")
    monkeypatch.setattr(tbank_sync_service, "TBankService", _fake_tbank(exc=exc))

    with pytest.raises(TBankError):
        await tbank_sync_service.do_sync_one(portfolio_id, CFG)

    _pid, _at, err = status_spy["status"][0]
    assert err == "MOEX недоступен"
    assert status_spy["enabled"] == []  # not an auth error → stays enabled


@pytest.mark.asyncio
async def test_generic_exception_truncates_and_reraises(
    portfolio_id, status_spy, monkeypatch
) -> None:
    """A non-TBankError is recorded as str(exc)[:500] and re-raised."""
    long_msg = "x" * 800
    monkeypatch.setattr(
        tbank_sync_service, "TBankService", _fake_tbank(exc=RuntimeError(long_msg))
    )

    with pytest.raises(RuntimeError):
        await tbank_sync_service.do_sync_one(portfolio_id, CFG)

    _pid, _at, err = status_spy["status"][0]
    assert err == "x" * 500
    assert status_spy["enabled"] == []


@pytest.mark.asyncio
async def test_lock_held_returns_skipped_without_syncing(
    portfolio_id, status_spy, monkeypatch
) -> None:
    """When the lock is already held, do_sync_one short-circuits to skipped."""
    monkeypatch.setattr(
        storage_service, "acquire_sync_lock", lambda *a, **k: False
    )

    def _boom(token):  # TBankService must never be constructed
        raise AssertionError("sync ran despite the lock being held")

    monkeypatch.setattr(tbank_sync_service, "TBankService", _boom)

    out = await tbank_sync_service.do_sync_one(portfolio_id, CFG)

    assert out == {
        "skipped": True,
        "added": 0,
        "updated": 0,
        "removed_candidates": [],
        "errors": [],
    }
    assert status_spy["status"] == []  # no status recorded when skipped
