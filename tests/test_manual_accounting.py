from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.exceptions import ValidationError
from app.models import InstrumentMetrics
from app.services.accounting_service import replay, summarize, apply_to_rows
from app.services.storage_service import storage_service
from app.services.portfolio_service import portfolio_service


def row(value=1000):
    return InstrumentMetrics(id=1, type='bond', ticker='TEST', name='Test', quantity=1,
                             purchase_price=1000, current_price=value, current_value=value,
                             profit=value-1000, weight=100, ai_comment='', source='custom')


def op(kind, amount, qty='0', iid=1):
    return {'kind': kind, 'amount_minor': amount * 100, 'quantity': qty, 'item_id': iid,
            'event_date': date.today().isoformat()}


def test_amortization_is_return_of_capital():
    operations = [op('opening', 1000, '1'), op('amortization', 500)]
    result = summarize(operations, [row(500)])
    assert (result['cash'], result['assets'], result['result'], result['principal']) == (500, 1000, 0, 500)
    assert apply_to_rows(operations, [row(500)])[0].full_profit == 0


def test_coupon_withdrawal_and_reinvestment():
    operations = [op('opening', 1000, '10'), op('coupon', 100), op('buy', 100, '1')]
    assert replay(operations)['positions'][1]['quantity'] == Decimal(11)
    assert summarize(operations, [row(1100)])['result'] == 100
    operations = [op('opening', 1000, '1'), op('amortization', 500), op('withdraw', 500, iid=None)]
    assert summarize(operations, [row(500)])['result'] == 0


def test_complete_redemption_keeps_result_with_no_rows():
    operations = [op('opening', 1000, '1'), op('coupon', 100), op('redemption', 1000, '1')]
    assert replay(operations)['positions'][1]['quantity'] == 0
    assert summarize(operations, [])['result'] == 100
    assert summarize(operations, [])['assets'] == 1100


def test_sale_and_fee():
    operations = [op('opening', 1000, '1'), op('sell', 1100, '1'), op('fee', 10, iid=None)]
    assert summarize(operations, [])['result'] == 90


def test_missing_quote_is_not_zero_profit():
    r = row(0)
    r.no_market_data = True
    assert summarize([op('opening', 1000, '1')], [r])['result'] is None


@pytest.fixture
def manual_portfolio(monkeypatch, settings_override):
    monkeypatch.setattr(settings, 'manual_accounting_enabled', True)
    before = {p['id'] for p in storage_service.get_portfolios(1)}
    pid = storage_service.create_portfolio(1, 'Manual accounting test ' + uuid4().hex[:6])
    iid = storage_service.add_item('TEST', 'bond', 1, 1000, pid, source='custom', custom_price=1000)
    yield pid, iid
    from app.services.cache_service import cache_service
    for portfolio in storage_service.get_portfolios(1):
        if portfolio['id'] not in before:
            storage_service.delete_portfolio(portfolio['id'])
            cache_service._caches.pop(portfolio['id'], None)


async def initialize(client, auth_headers, pid):
    r = await client.post(f'/portfolios/{pid}/accounting/initialize', json={'cash': 0}, headers=auth_headers)
    assert r.status_code == 200, r.text


async def post(client, auth_headers, pid, kind, amount, iid=None, **extra):
    return await client.post(f'/portfolios/{pid}/accounting/operations', headers=auth_headers,
                            json={'request_id': str(uuid4()), 'kind': kind, 'amount': amount,
                                  'event_date': date.today().isoformat(), 'item_id': iid, **extra})


async def test_cabinet_cash_rows_and_undo(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    response = await post(client, auth_headers, pid, 'amortization', 500, iid)
    assert response.status_code == 200
    operation_id = response.json()['operation_id']
    assert (await client.post(f'/portfolios/{pid}/accounting/valuation', headers=auth_headers,
                             json={'item_id': iid, 'price': 500})).status_code == 200
    state = (await client.get(f'/portfolios/{pid}/accounting', headers=auth_headers)).json()
    assert state['totals']['assets'] == 1000
    assert state['totals']['result'] == 0
    assert await portfolio_service.get_cash_rub(pid) == 500
    table = (await client.get(f'/portfolios/{pid}/table', headers=auth_headers)).json()
    assert table['items'][0]['full_profit'] == 0
    assert table['items'][0]['realized_principal'] == 500
    assert (await client.delete(f'/portfolios/{pid}/accounting/operations/{operation_id}', headers=auth_headers)).status_code == 200
    state = (await client.get(f'/portfolios/{pid}/accounting', headers=auth_headers)).json()
    assert state['totals']['cash'] == 0


async def test_duplicate_payment_is_not_added_twice(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    key = f'moex:{iid}:coupon:{date.today().isoformat()}'
    one = await post(client, auth_headers, pid, 'coupon', 100, iid, payment_key=key)
    two = await post(client, auth_headers, pid, 'coupon', 100, iid, payment_key=key)
    assert one.status_code == two.status_code == 200
    assert one.json()['operation_id'] == two.json()['operation_id']
    state = (await client.get(f'/portfolios/{pid}/accounting', headers=auth_headers)).json()
    assert state['totals']['cash'] == 100


async def test_redemption_is_atomic_and_reversible(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    response = await post(client, auth_headers, pid, 'redemption', 1000, iid, quantity='1')
    assert response.status_code == 200
    assert storage_service.get_items(pid) == []
    state = (await client.get(f'/portfolios/{pid}/accounting', headers=auth_headers)).json()
    assert state['totals']['assets'] == 1000 and state['totals']['result'] == 0
    assert (await client.delete(f'/portfolios/{pid}/accounting/operations/{response.json()["operation_id"]}', headers=auth_headers)).status_code == 200
    assert storage_service.get_items(pid)[0]['quantity'] == 1


async def test_failed_write_does_not_change_cash_or_journal(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    before = storage_service.get_account_operations(pid)
    assert (await post(client, auth_headers, pid, 'withdraw', 1)).status_code == 400
    assert (await post(client, auth_headers, pid, 'sell', 1000, iid, quantity='2')).status_code == 400
    assert storage_service.get_account_operations(pid) == before


async def test_other_portfolio_and_legacy_mutations_rejected(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    other = storage_service.create_portfolio(1, 'Other')
    other_item = storage_service.add_item('OTHER', 'bond', 1, 1000, other, source='custom', custom_price=1000)
    await initialize(client, auth_headers, pid)
    assert (await post(client, auth_headers, pid, 'coupon', 100, other_item)).status_code == 400
    with pytest.raises(ValidationError):
        storage_service.delete_item(iid, pid)
    with pytest.raises(ValidationError):
        storage_service.add_item('NEW', 'bond', 1, 100, pid)
    with pytest.raises(ValidationError):
        storage_service.move_instrument(other_item, other, pid)


async def test_another_user_cannot_read_account(client, auth_headers, manual_portfolio):
    from app.services.auth_service import auth_service
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    user = auth_service.register('account_' + uuid4().hex[:8], 'secret_test_only')
    response = await client.get(f'/portfolios/{pid}/accounting', headers={'Authorization': 'Bearer '+user['access_token']})
    assert response.status_code == 403


async def test_dates_and_precision_rejected(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    assert (await post(client, auth_headers, pid, 'coupon', '1.001', iid)).status_code == 422
    assert (await post(client, auth_headers, pid, 'coupon', 1, iid, event_date='2000-01-01')).status_code == 400


async def test_feature_disabled_and_initialization_not_repeated(client, auth_headers, manual_portfolio, monkeypatch):
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    assert (await client.post(f'/portfolios/{pid}/accounting/initialize', headers=auth_headers, json={})).status_code == 409
    monkeypatch.setattr(settings, 'manual_accounting_enabled', False)
    assert (await client.get(f'/portfolios/{pid}/accounting', headers=auth_headers)).json()['available'] is False
    assert (await post(client, auth_headers, pid, 'coupon', 1, iid)).status_code == 404


async def test_journal_export(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    await post(client, auth_headers, pid, 'coupon', 100, iid)
    response = await client.get(f'/portfolios/{pid}/accounting/export', headers=auth_headers)
    assert response.status_code == 200
    assert 'coupon' in response.text and '100.00' in response.text


async def test_schedule_pagination_and_unknown_values(monkeypatch):
    import app.services.payment_schedule_service as schedule
    calls = []
    real_client = httpx.AsyncClient
    def handle(request):
        block = request.url.params['iss.only'].split(',')[0]
        start = int(request.url.params['start'])
        calls.append((block, start))
        column = 'coupondate' if block == 'coupons' else 'amortdate'
        rows = [['2026-01-01', 10], ['2026-02-01', None]] if start == 0 else [['2026-03-01', 20]]
        return httpx.Response(200, json={block: {'columns': [column, 'value'], 'data': rows},
                                         block+'.cursor': {'columns': ['TOTAL'], 'data': [[3]]}})
    monkeypatch.setattr(schedule.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    payments = await schedule.load_payments('SU26238RMFS4')
    assert len(payments) == 4
    assert ('coupons', 2) in calls and ('amortizations', 2) in calls


def test_schedule_is_not_cash_until_confirmed():
    from app.services.payment_schedule_service import proposals
    today = date.today().isoformat()
    operations = [op('opening', 1000, '2')]
    payments = [{'date': today, 'record_date': today, 'kind': 'amortization', 'value': 250}]
    proposed = proposals(1, 'TEST', payments, operations, today)
    assert proposed[0]['amount'] == 500
    assert summarize(operations, [row()])['cash'] == 0
    operations.append({**op('amortization', 500), 'external_key': proposed[0]['external_key']})
    assert proposals(1, 'TEST', payments, operations, today) == []


async def test_closed_position_coupons_and_cash_remain_in_analytics(client, auth_headers, manual_portfolio, monkeypatch):
    from app.api.portfolios import _build_analytics_extra
    from app.services.cbr_service import cbr_service
    async def key_rate():
        return 16.0
    monkeypatch.setattr(cbr_service, 'get_key_rate', key_rate)
    pid, iid = manual_portfolio
    await initialize(client, auth_headers, pid)
    await post(client, auth_headers, pid, 'coupon', 100, iid)
    await post(client, auth_headers, pid, 'redemption', 1000, iid, quantity='1')
    extra = await _build_analytics_extra([], [pid])
    assert extra['realized_coupons'] == 100
    assert extra['free_cash_rub'] == 1100
    pdf = await client.get(f'/portfolios/{pid}/report.pdf', headers=auth_headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')


async def test_restore_from_purchase_cost_requires_dates(client, auth_headers, manual_portfolio):
    pid, iid = manual_portfolio
    endpoint = f'/portfolios/{pid}/accounting/initialize'
    response = await client.post(endpoint, headers=auth_headers, json={'basis': 'purchase'})
    assert response.status_code == 400
    assert not storage_service.accounting_enabled(pid)
    with storage_service._connect() as conn:
        conn.execute('UPDATE portfolio_items SET purchase_date=?,custom_price=500 WHERE id=?', (date.today().isoformat(), iid))
        conn.commit()
    from app.services.cache_service import cache_service
    cache_service.invalidate(pid)
    assert (await client.post(endpoint, headers=auth_headers, json={'basis': 'purchase'})).status_code == 200
    await post(client, auth_headers, pid, 'amortization', 500, iid)
    state = (await client.get(f'/portfolios/{pid}/accounting', headers=auth_headers)).json()
    assert state['totals']['result'] == 0 and state['totals']['assets'] == 1000
