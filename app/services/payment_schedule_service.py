"""Payment proposals from MOEX; a schedule is not a receipt confirmation."""
from datetime import date
from decimal import Decimal

import httpx

from app.config import settings
from app.services.accounting_service import minor, replay
from app.services.moex_service import valid_secid


async def load_payments(ticker: str) -> list[dict]:
    if not valid_secid(ticker):
        raise ValueError('Некорректный тикер')
    payments = []
    async with httpx.AsyncClient(timeout=15) as client:
        for block, date_field, kind in [('coupons', 'coupondate', 'coupon'),
                                       ('amortizations', 'amortdate', 'amortization')]:
            start = 0
            while True:
                response = await client.get(
                    f'{settings.moex_base_url}/securities/{ticker}/bondization.json',
                    params={'iss.meta': 'off', 'iss.only': f'{block},{block}.cursor',
                            'limit': 100, 'start': start})
                response.raise_for_status()
                data = response.json()
                section = data.get(block, {})
                rows = [dict(zip(section.get('columns', []), r)) for r in section.get('data', [])]
                for row in rows:
                    when = row.get(date_field)
                    value = row.get('value')
                    if when and value is not None:
                        payments.append({'date': str(when)[:10], 'kind': kind, 'value': value,
                                         'record_date': str(row.get('recorddate') or when)[:10]})
                cursor = data.get(f'{block}.cursor', {})
                cursor_rows = cursor.get('data', [])
                total = dict(zip(cursor.get('columns', []), cursor_rows[0])).get('TOTAL') if cursor_rows else None
                if not rows or (total is not None and start + len(rows) >= int(total)) or (total is None and len(rows) < 100):
                    break
                start += len(rows)
                if start > 10000:
                    raise ValueError('Слишком длинный график выплат')
    return payments


def proposals(item_id: int, ticker: str, payments: list[dict], operations: list[dict], started_on: str) -> list[dict]:
    known = {o.get('external_key') for o in operations}
    today = date.today().isoformat()
    out = []
    for p in payments:
        if not started_on <= p['date'] <= today or Decimal(str(p['value'])) <= 0:
            continue
        key = f"moex:{item_id}:{p['kind']}:{p['date']}"
        if key in known:
            continue
        # The user must confirm entitlement and the amount before this enters cash.
        history = [o for o in operations if o['event_date'] <= p['record_date']]
        pos = replay(history)['positions'].get(item_id)
        qty = pos['quantity'] if pos else Decimal(0)
        if qty <= 0:
            continue
        out.append({'item_id': item_id, 'ticker': ticker, 'kind': p['kind'],
                    'event_date': p['date'], 'record_date': p['record_date'],
                    'amount': minor(Decimal(str(p['value'])) * qty) / 100,
                    'quantity': str(qty), 'external_key': key,
                    'status': 'scheduled'})
    return out
