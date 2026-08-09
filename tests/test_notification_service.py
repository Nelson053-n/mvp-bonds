"""Tests for NotificationService and the double-downgrade alert condition.

Covered:
- send_telegram: success / API-error / network-error / missing-token short-circuit.
- check_and_notify: rating-change and price-drop message emission with threshold/lang.
- check_and_send_coupon_notifications: dedup of already-sent coupon reminders,
  target-date matching, skipping non-bonds / missing chat_id.
- rating_worsened + the main.py double-downgrade trigger (two consecutive worsenings).

All outbound Telegram traffic is mocked — no real HTTP is performed.
"""

import pytest

from app.models import InstrumentMetrics
from app.services.notification_service import NotificationService
from app.services.rating_utils import rating_rank, rating_worsened


def _row(
    id, *, ticker="T", name="Name", price=100.0, rating=None, type="bond",
    rating_source="smartlab",
):
    return InstrumentMetrics(
        id=id,
        type=type,
        name=name,
        ticker=ticker,
        current_price=price,
        purchase_price=price,
        quantity=1,
        current_value=price,
        profit=0.0,
        weight=0.0,
        company_rating=rating,
        rating_source=rating_source if rating else None,
        ai_comment="",
    )


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Records the last POST and returns a canned response (or raises)."""
    last_url = None
    last_json = None

    def __init__(self, *a, response=None, raise_exc=None, **k):
        self._response = response or _FakeResp()
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        type(self).last_url = url
        type(self).last_json = json
        if self._raise:
            raise self._raise
        return self._response


def _patch_client(monkeypatch, *, response=None, raise_exc=None):
    def factory(*a, **k):
        return _FakeClient(response=response, raise_exc=raise_exc)
    monkeypatch.setattr(
        "app.services.notification_service.httpx.AsyncClient", factory
    )


class TestSendTelegram:
    @pytest.fixture
    def svc(self):
        return NotificationService()

    async def test_returns_false_without_token(self, svc):
        assert await svc.send_telegram("", "chat", "hi") is False

    async def test_returns_false_without_chat_id(self, svc):
        assert await svc.send_telegram("tok", "", "hi") is False

    async def test_success(self, monkeypatch, svc):
        _patch_client(monkeypatch, response=_FakeResp(200))
        ok = await svc.send_telegram("tok", "chat", "hello")
        assert ok is True
        assert "bottok/sendMessage" in _FakeClient.last_url
        assert _FakeClient.last_json["chat_id"] == "chat"
        assert _FakeClient.last_json["parse_mode"] == "HTML"

    async def test_api_error(self, monkeypatch, svc):
        _patch_client(monkeypatch, response=_FakeResp(400, "bad request"))
        assert await svc.send_telegram("tok", "chat", "hi") is False

    async def test_network_error(self, monkeypatch, svc):
        _patch_client(monkeypatch, raise_exc=RuntimeError("dns fail"))
        assert await svc.send_telegram("tok", "chat", "hi") is False


class TestCheckAndNotify:
    """Compares old vs new rows and sends rating/price-drop alerts."""

    @pytest.fixture
    def svc(self):
        return NotificationService()

    def _patch_confirm(self, monkeypatch, svc, *, confirms=True):
        """Stub the independent rating re-check (no real MOEX/SmartLab call)."""
        async def fake_confirm(new_row):
            from app.services.notification_service import _grade
            return _grade(new_row.company_rating) if confirms else None
        monkeypatch.setattr(svc, "_confirm_rating", fake_confirm)

    def _patch_settings(self, monkeypatch, *, owner_chat_id="chat", dedup_free=True, **over):
        settings = {
            "tg_bot_token": "tok",
            "price_drop_threshold": "5.0",
            "tg_lang": "ru",
        }
        settings.update(over)
        from app.services.storage_service import storage_service
        monkeypatch.setattr(storage_service, "get_all_settings", lambda: settings)
        monkeypatch.setattr(storage_service, "get_portfolio",
                            lambda pid: {"id": pid, "user_id": 1})
        monkeypatch.setattr(storage_service, "get_user_by_id",
                            lambda uid: {"id": uid, "tg_chat_id": owner_chat_id})
        monkeypatch.setattr(storage_service, "try_mark_notification_sent",
                            lambda key: dedup_free)

    async def test_no_alert_without_token(self, monkeypatch, svc):
        self._patch_settings(monkeypatch, tg_bot_token="")
        sent = []
        monkeypatch.setattr(svc, "send_telegram",
                            lambda *a, **k: sent.append(a) or True)
        await svc.check_and_notify([_row(1)], [_row(1)], 7)
        assert sent == []

    async def test_rating_change_emits_message(self, monkeypatch, svc):
        self._patch_settings(monkeypatch)
        sent = []
        async def fake_send(token, chat_id, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)

        self._patch_confirm(monkeypatch, svc)

        old = [_row(1, rating="A")]
        new = [_row(1, rating="BBB")]
        await svc.check_and_notify(old, new, 7)
        assert len(sent) == 1
        assert "A" in sent[0] and "BBB" in sent[0]
        assert "рейтинг" in sent[0].lower()

    async def test_no_rating_message_when_unchanged(self, monkeypatch, svc):
        self._patch_settings(monkeypatch)
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        await svc.check_and_notify([_row(1, rating="A")], [_row(1, rating="A")], 7)
        assert sent == []

    async def test_price_drop_above_threshold(self, monkeypatch, svc):
        self._patch_settings(monkeypatch)
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        # 100 → 90 = -10% ≥ 5% threshold
        await svc.check_and_notify([_row(1, price=100.0)], [_row(1, price=90.0)], 7)
        assert len(sent) == 1
        assert "просадка" in sent[0].lower()

    async def test_price_drop_below_threshold_silent(self, monkeypatch, svc):
        self._patch_settings(monkeypatch)
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        # 100 → 98 = -2% < 5%
        await svc.check_and_notify([_row(1, price=100.0)], [_row(1, price=98.0)], 7)
        assert sent == []

    async def test_unknown_row_skipped(self, monkeypatch, svc):
        """A new row absent from old_rows can't be diffed → no message."""
        self._patch_settings(monkeypatch)
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        await svc.check_and_notify([], [_row(2, rating="BBB", price=50.0)], 7)
        assert sent == []

    async def test_english_language(self, monkeypatch, svc):
        self._patch_settings(monkeypatch, tg_lang="en")
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        self._patch_confirm(monkeypatch, svc)
        await svc.check_and_notify([_row(1, rating="A")], [_row(1, rating="BBB")], 7)
        assert "Rating change" in sent[0]

    async def test_no_alert_when_owner_has_no_chat_id(self, monkeypatch, svc):
        """Alerts go to the portfolio owner; no owner chat_id -> nothing sent."""
        self._patch_settings(monkeypatch, owner_chat_id=None)
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        await svc.check_and_notify([_row(1, price=100.0)], [_row(1, price=90.0)], 7)
        assert sent == []

    async def test_sends_to_owner_chat(self, monkeypatch, svc):
        self._patch_settings(monkeypatch, owner_chat_id="owner-42")
        sent = []
        async def fake_send(token, chat_id, text):
            sent.append(chat_id)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        await svc.check_and_notify([_row(1, price=100.0)], [_row(1, price=90.0)], 7)
        assert sent == ["owner-42"]

    async def test_dedup_suppresses_duplicate(self, monkeypatch, svc):
        """A key already claimed (other worker / earlier refresh) is not re-sent."""
        self._patch_settings(monkeypatch, dedup_free=False)
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        await svc.check_and_notify([_row(1, price=100.0)], [_row(1, price=90.0)], 7)
        assert sent == []


class TestRatingSourceOutage:
    """Regression: the 2026-07-25 false-alert storm.

    SmartLab became unreachable, ratings silently fell back to the coarse
    LISTLEVEL proxy ("A+" -> "BBB"), an alert went out, and five minutes later
    the recovered source produced the mirror-image "BBB" -> "A+" alert.
    Neither message described a real rating action.
    """

    @pytest.fixture
    def svc(self):
        return NotificationService()

    def _patch(self, monkeypatch, svc, *, confirms=True):
        from app.services.storage_service import storage_service
        monkeypatch.setattr(storage_service, "get_all_settings", lambda: {
            "tg_bot_token": "tok", "price_drop_threshold": "5.0", "tg_lang": "ru",
        })
        monkeypatch.setattr(storage_service, "get_portfolio",
                            lambda pid: {"id": pid, "user_id": 1})
        monkeypatch.setattr(storage_service, "get_user_by_id",
                            lambda uid: {"id": uid, "tg_chat_id": "chat"})
        monkeypatch.setattr(storage_service, "try_mark_notification_sent",
                            lambda key: True)
        async def fake_confirm(new_row):
            from app.services.notification_service import _grade
            return _grade(new_row.company_rating) if confirms else None
        monkeypatch.setattr(svc, "_confirm_rating", fake_confirm)
        sent = []
        async def fake_send(t, c, text):
            sent.append(text)
            return True
        monkeypatch.setattr(svc, "send_telegram", fake_send)
        return sent

    async def test_smartlab_outage_to_listlevel_is_silent(self, monkeypatch, svc):
        """The exact 11:40 alert: SmartLab A+ -> LISTLEVEL proxy BBB."""
        sent = self._patch(monkeypatch, svc)
        old = [_row(1, ticker="RU000A10DTA2", rating="A+", rating_source="smartlab")]
        new = [_row(1, ticker="RU000A10DTA2", rating="BBB", rating_source="listlevel")]
        await svc.check_and_notify(old, new, 7)
        assert sent == []

    async def test_listlevel_recovery_is_silent(self, monkeypatch, svc):
        """The mirror 11:45 alert: LISTLEVEL proxy BBB -> recovered SmartLab A+."""
        sent = self._patch(monkeypatch, svc)
        old = [_row(1, ticker="RU000A10DTA2", rating="BBB", rating_source="listlevel")]
        new = [_row(1, ticker="RU000A10DTA2", rating="A+", rating_source="smartlab")]
        await svc.check_and_notify(old, new, 7)
        assert sent == []

    async def test_db_fallback_is_silent(self, monkeypatch, svc):
        """A rating served from the DB is a cached value, not a fresh observation."""
        sent = self._patch(monkeypatch, svc)
        old = [_row(1, rating="A+", rating_source="smartlab")]
        new = [_row(1, rating="BBB", rating_source="db")]
        await svc.check_and_notify(old, new, 7)
        assert sent == []

    async def test_unconfirmed_change_is_silent(self, monkeypatch, svc):
        """Trustworthy sources on both sides, but the re-check does not confirm."""
        sent = self._patch(monkeypatch, svc, confirms=False)
        old = [_row(1, rating="A+", rating_source="smartlab")]
        new = [_row(1, rating="BBB", rating_source="smartlab")]
        await svc.check_and_notify(old, new, 7)
        assert sent == []

    async def test_confirmed_real_downgrade_still_alerts(self, monkeypatch, svc):
        """The feature must keep working for a genuine, confirmed downgrade."""
        sent = self._patch(monkeypatch, svc)
        old = [_row(1, rating="A+", rating_source="smartlab")]
        new = [_row(1, rating="BBB", rating_source="smartlab")]
        await svc.check_and_notify(old, new, 7)
        assert len(sent) == 1
        assert "A+" in sent[0] and "BBB" in sent[0]

    async def test_date_only_change_is_silent(self, monkeypatch, svc):
        """'A+ (12.05.2026)' -> 'A+ (20.07.2026)' is a re-affirmation, not news."""
        sent = self._patch(monkeypatch, svc)
        old = [_row(1, rating="A+ (12.05.2026)", rating_source="smartlab")]
        new = [_row(1, rating="A+ (20.07.2026)", rating_source="smartlab")]
        await svc.check_and_notify(old, new, 7)
        assert sent == []

    async def test_alert_shows_bare_grade(self, monkeypatch, svc):
        """The message shows grades, not the assignment dates."""
        sent = self._patch(monkeypatch, svc)
        old = [_row(1, rating="A+ (12.05.2026)", rating_source="smartlab")]
        new = [_row(1, rating="BBB (20.07.2026)", rating_source="smartlab")]
        await svc.check_and_notify(old, new, 7)
        assert len(sent) == 1
        assert "12.05.2026" not in sent[0] and "20.07.2026" not in sent[0]


class TestConfirmRating:
    """_confirm_rating: the last gate before anything reaches the user."""

    @pytest.fixture
    def svc(self):
        return NotificationService()

    def _patch_refresh(self, monkeypatch, result=None, exc=None):
        from app.services.moex_service import moex_service
        async def fake_refresh(secid):
            if exc:
                raise exc
            return result
        monkeypatch.setattr(moex_service, "refresh_rating_with_sources", fake_refresh)

    async def test_confirms_matching_grade(self, monkeypatch, svc):
        self._patch_refresh(monkeypatch, {"smartlab": "BBB", "moex": None, "best": "BBB"})
        assert await svc._confirm_rating(_row(1, rating="BBB")) == "BBB"

    async def test_rejects_mismatch(self, monkeypatch, svc):
        self._patch_refresh(monkeypatch, {"smartlab": "A+", "moex": None, "best": "A+"})
        assert await svc._confirm_rating(_row(1, rating="BBB")) is None

    async def test_rejects_when_source_unreachable(self, monkeypatch, svc):
        self._patch_refresh(monkeypatch, {"smartlab": None, "moex": None, "best": None})
        assert await svc._confirm_rating(_row(1, rating="BBB")) is None

    async def test_rejects_on_exception(self, monkeypatch, svc):
        self._patch_refresh(monkeypatch, exc=RuntimeError("network down"))
        assert await svc._confirm_rating(_row(1, rating="BBB")) is None

    async def test_manual_rating_needs_no_external_confirmation(self, monkeypatch, svc):
        called = []
        from app.services.moex_service import moex_service
        async def fake_refresh(secid):
            called.append(secid)
            return {"best": None}
        monkeypatch.setattr(moex_service, "refresh_rating_with_sources", fake_refresh)
        row = _row(1, rating="AA", rating_source="manual")
        assert await svc._confirm_rating(row) == "AA"
        assert called == []

    async def test_ignores_assignment_date_when_matching(self, monkeypatch, svc):
        self._patch_refresh(
            monkeypatch, {"smartlab": "BBB (20.07.2026)", "moex": None, "best": "BBB (20.07.2026)"}
        )
        assert await svc._confirm_rating(_row(1, rating="BBB (12.05.2026)")) == "BBB"


class TestCouponNotifications:
    """check_and_send_coupon_notifications: target-date match + dedup."""

    @pytest.fixture
    def svc(self):
        return NotificationService()

    async def test_no_token_short_circuits(self, monkeypatch, svc):
        from app.services.storage_service import storage_service
        monkeypatch.setattr(storage_service, "get_setting", lambda k, d="": "")
        called = []
        monkeypatch.setattr(storage_service, "get_users_with_coupon_notifications",
                            lambda: called.append(1) or [])
        await svc.check_and_send_coupon_notifications()
        assert called == []

    async def test_dedup_already_sent(self, monkeypatch, svc):
        from datetime import date, timedelta
        from app.services.storage_service import storage_service

        monkeypatch.setattr(storage_service, "get_setting",
                            lambda k, d="": "tok" if k == "tg_bot_token" else d)
        monkeypatch.setattr(storage_service, "get_users_with_coupon_notifications",
                            lambda: [{"id": 1, "coupon_notif_days": 3, "tg_chat_id": "chat"}])
        monkeypatch.setattr(storage_service, "get_portfolios",
                            lambda uid: [{"id": 7, "name": "P"}])

        coupon_row = _row(11, ticker="BND", type="bond")
        coupon_row.next_coupon_date = date.today() + timedelta(days=3)
        coupon_row.coupon = 30.0
        coupon_row.quantity = 5

        async def fake_table(pid):
            return [coupon_row]
        from app.services.portfolio_service import portfolio_service
        monkeypatch.setattr(portfolio_service, "get_table", fake_table)

        # Already sent → must skip.
        monkeypatch.setattr(storage_service, "is_coupon_notification_sent",
                            lambda item_id, d: True)
        sent = []
        async def fake_coupon_tg(*a, **k):
            sent.append(a)
            return True
        monkeypatch.setattr(svc, "_send_coupon_telegram", fake_coupon_tg)

        await svc.check_and_send_coupon_notifications()
        assert sent == []

    async def test_sends_and_marks_when_due(self, monkeypatch, svc):
        from datetime import date, timedelta
        from app.services.storage_service import storage_service
        from app.services.portfolio_service import portfolio_service

        monkeypatch.setattr(storage_service, "get_setting",
                            lambda k, d="": "tok" if k == "tg_bot_token" else d)
        monkeypatch.setattr(storage_service, "get_users_with_coupon_notifications",
                            lambda: [{"id": 1, "coupon_notif_days": 3, "tg_chat_id": "chat"}])
        monkeypatch.setattr(storage_service, "get_portfolios",
                            lambda uid: [{"id": 7, "name": "P"}])
        monkeypatch.setattr(storage_service, "is_coupon_notification_sent",
                            lambda item_id, d: False)

        coupon_row = _row(11, ticker="BND", type="bond")
        coupon_row.next_coupon_date = date.today() + timedelta(days=3)
        coupon_row.coupon = 30.0
        coupon_row.quantity = 5
        # A stock that happens to look due must be ignored (type != bond).
        stock_row = _row(12, ticker="STK", type="stock")
        stock_row.next_coupon_date = date.today() + timedelta(days=3)

        async def fake_table(pid):
            return [coupon_row, stock_row]
        monkeypatch.setattr(portfolio_service, "get_table", fake_table)

        sent = []
        async def fake_coupon_tg(token, chat_id, pname, ticker, *a, **k):
            sent.append(ticker)
            return True
        monkeypatch.setattr(svc, "_send_coupon_telegram", fake_coupon_tg)

        marked = []
        monkeypatch.setattr(storage_service, "mark_coupon_notification_sent",
                            lambda item_id, d: marked.append((item_id, d)))

        await svc.check_and_send_coupon_notifications()
        assert sent == ["BND"]          # only the bond
        assert marked == [(11, (date.today() + timedelta(days=3)).isoformat())]

    async def test_skips_user_without_chat_id(self, monkeypatch, svc):
        from app.services.storage_service import storage_service
        from app.services.portfolio_service import portfolio_service

        monkeypatch.setattr(storage_service, "get_setting",
                            lambda k, d="": "tok" if k == "tg_bot_token" else d)
        monkeypatch.setattr(storage_service, "get_users_with_coupon_notifications",
                            lambda: [{"id": 1, "coupon_notif_days": 3, "tg_chat_id": None}])
        portfolios_called = []
        monkeypatch.setattr(storage_service, "get_portfolios",
                            lambda uid: portfolios_called.append(uid) or [])
        await svc.check_and_send_coupon_notifications()
        assert portfolios_called == []   # bailed before touching portfolios


class TestRatingComparison:
    """rating_rank / rating_worsened — the building blocks of the alert."""

    def test_lower_rank_is_better(self):
        assert rating_rank("AAA") < rating_rank("BBB") < rating_rank("D")

    def test_unknown_rating_is_worst_but_below_none(self):
        assert rating_rank("ZZZ") == 998
        assert rating_rank(None) == 999

    def test_worsened_true_when_downgraded(self):
        assert rating_worsened("A", "BBB") is True

    def test_worsened_false_when_upgraded(self):
        assert rating_worsened("BBB", "A") is False

    def test_worsened_false_when_unchanged(self):
        assert rating_worsened("A", "A") is False


class TestDoubleDowngradeCondition:
    """The main.py guard fires only on TWO consecutive worsenings.

    history is newest-first: history[0]=latest, history[1]=prev, history[2]=oldest.
    Alert ⇔ worsened(h2→h1) AND worsened(h1→h0) AND ≥3 records AND tg configured.
    """

    @staticmethod
    def _double_downgrade(history, tg_token="tok", tg_chat_id="chat"):
        """Mirror of the boolean condition in app/main.py:232-237."""
        return (
            len(history) >= 3
            and rating_worsened(history[2], history[1])
            and rating_worsened(history[1], history[0])
            and bool(tg_token)
            and bool(tg_chat_id)
        )

    def test_fires_on_two_consecutive_downgrades(self):
        # oldest → ... → newest : AA → A → BBB  (worsens twice)
        assert self._double_downgrade(["BBB", "A", "AA"]) is True

    def test_no_fire_on_single_downgrade(self):
        # Only the latest step worsened; the earlier one held flat.
        assert self._double_downgrade(["BBB", "A", "A"]) is False

    def test_no_fire_on_recovery(self):
        # A → BBB → A : second step is an upgrade.
        assert self._double_downgrade(["A", "BBB", "A"]) is False

    def test_no_fire_with_fewer_than_three_records(self):
        assert self._double_downgrade(["BBB", "A"]) is False

    def test_no_fire_without_telegram_config(self):
        assert self._double_downgrade(["BBB", "A", "AA"], tg_token="") is False
        assert self._double_downgrade(["BBB", "A", "AA"], tg_chat_id="") is False


class TestCheckPriceAlerts:
    """check_price_alerts() shipped but was never scheduled AND called a
    non-existent cache_service.get(), so it would have raised AttributeError
    on the first run. Users could create alerts that never fired."""

    @staticmethod
    def _setup(monkeypatch, alerts, cached_rows, *, token="tok"):
        """Wire storage/cache/telegram fakes; returns (svc, sent, triggered)."""
        from app.services import notification_service as ns

        svc = NotificationService()
        sent = []
        triggered = []

        class FakeStorage:
            def get_setting(self, key, default=""):
                return token if key == "tg_bot_token" else default

            def get_all_active_price_alerts(self):
                return alerts

            def mark_price_alert_triggered(self, alert_id):
                triggered.append(alert_id)

            def _connect(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                return self

            def fetchone(self):
                return (1, "SBER")

        class FakeCache:
            def rows(self, portfolio_id):
                return cached_rows

        monkeypatch.setattr(ns, "storage_service", FakeStorage(), raising=False)
        import app.services.storage_service as ss
        import app.services.cache_service as cs
        monkeypatch.setattr(ss, "storage_service", FakeStorage())
        monkeypatch.setattr(cs, "cache_service", FakeCache())

        async def fake_send(token, chat_id, text):
            sent.append(text)
            return True

        monkeypatch.setattr(svc, "send_telegram", fake_send)
        return svc, sent, triggered

    @staticmethod
    def _alert(alert_type, target, item_id=1):
        return {
            "id": 10, "user_id": 1, "item_id": item_id, "ticker": "SBER",
            "alert_type": alert_type, "target_price": target, "tg_chat_id": "42",
        }

    async def test_above_alert_fires(self, monkeypatch):
        """Price rose past the target → notification sent and alert marked."""
        svc, sent, triggered = self._setup(
            monkeypatch, [self._alert("above", 250.0)], [_row(1, ticker="SBER", price=283.0)]
        )

        await svc.check_price_alerts()

        assert len(sent) == 1
        assert "SBER" in sent[0]
        assert triggered == [10]

    async def test_below_alert_fires(self, monkeypatch):
        svc, sent, triggered = self._setup(
            monkeypatch, [self._alert("below", 300.0)], [_row(1, ticker="SBER", price=283.0)]
        )

        await svc.check_price_alerts()

        assert len(sent) == 1
        assert triggered == [10]

    async def test_not_reached_stays_silent(self, monkeypatch):
        """The important negative: a target that hasn't been hit must not fire."""
        svc, sent, triggered = self._setup(
            monkeypatch, [self._alert("above", 400.0)], [_row(1, ticker="SBER", price=283.0)]
        )

        await svc.check_price_alerts()

        assert sent == []
        assert triggered == []

    async def test_cold_cache_is_skipped(self, monkeypatch):
        """An empty cache must not crash or invent a price."""
        svc, sent, triggered = self._setup(
            monkeypatch, [self._alert("above", 1.0)], []
        )

        await svc.check_price_alerts()

        assert sent == []

    async def test_no_token_returns_early(self, monkeypatch):
        svc, sent, _ = self._setup(
            monkeypatch, [self._alert("above", 1.0)], [_row(1, price=283.0)], token=""
        )

        await svc.check_price_alerts()

        assert sent == []
