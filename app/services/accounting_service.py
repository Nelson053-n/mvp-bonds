"""Confirmed RUB cash flows for opt-in manual portfolios."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP


def latest_event_date() -> date:
    """Latest date a journal entry may carry.

    The server runs in UTC, but the cabinet suggests the user's local date: in
    Vladivostok (UTC+10) from midnight to 10:00 it is already "tomorrow" for the
    user, and today's coupon was rejected as a future date. Allow the furthest
    time zone on Earth (UTC+14).
    """
    return (datetime.now(timezone.utc) + timedelta(hours=14)).date()


def minor(value) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def replay(operations: list[dict]) -> dict:
    cash = deposits = withdrawals = opening_value = fees = 0
    positions: dict[int, dict] = {}
    for op in operations:
        kind, amount = op['kind'], op['amount_minor']
        qty = Decimal(str(op.get('quantity') or 0))
        iid = op.get('item_id')
        pos = positions.setdefault(iid, {'quantity': Decimal(0), 'net_cost_minor': 0,
                                        'coupons_minor': 0, 'principal_minor': 0,
                                        # Remaining cost basis (average cost) and the result
                                        # already fixed by sales / principal returns.
                                        'cost_minor': 0, 'realized_minor': 0,
                                        'nominal_reduction': Decimal(0)}) if iid else None
        if kind == 'opening':
            pos['quantity'] += qty
            pos['net_cost_minor'] += amount
            pos['cost_minor'] += amount
            opening_value += amount
        elif kind == 'deposit':
            cash += amount
            deposits += amount
        elif kind == 'withdraw':
            cash -= amount
            withdrawals += amount
        elif kind == 'buy':
            cash -= amount
            pos['quantity'] += qty
            pos['net_cost_minor'] += amount
            pos['cost_minor'] += amount
        elif kind == 'sell':
            cash += amount
            if 0 < qty <= pos['quantity']:
                removed = int((Decimal(pos['cost_minor']) * qty / pos['quantity'])
                              .quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                pos['cost_minor'] -= removed
                pos['realized_minor'] += amount - removed
            pos['quantity'] -= qty
            pos['net_cost_minor'] -= amount
        elif kind in ('amortization', 'redemption'):
            if pos['quantity'] <= 0:
                raise ValueError('Нет облигаций для возврата номинала')
            cash += amount
            pos['principal_minor'] += amount
            pos['net_cost_minor'] -= amount
            if kind == 'redemption':
                if qty != pos['quantity']:
                    raise ValueError('При полном погашении укажите всё оставшееся количество')
                pos['quantity'] = Decimal(0)
                pos['realized_minor'] += amount - pos['cost_minor']
                pos['cost_minor'] = 0
            else:
                # Partial return of face value: first reduces the cost basis,
                # anything above it (bond bought below par) is already a result.
                pos['nominal_reduction'] += Decimal(amount) / 100 / pos['quantity']
                absorbed = min(amount, max(pos['cost_minor'], 0))
                pos['cost_minor'] -= absorbed
                pos['realized_minor'] += amount - absorbed
        elif kind == 'coupon':
            cash += amount
            pos['coupons_minor'] += amount
        elif kind == 'fee':
            cash -= amount
            fees += amount
            if pos:
                pos['net_cost_minor'] += amount
                pos['cost_minor'] += amount
        else:
            raise ValueError('Неизвестная операция')
        if cash < 0:
            raise ValueError('Недостаточно свободных денег. Сначала внесите пополнение.')
        if pos and pos['quantity'] < 0:
            raise ValueError('Недостаточно облигаций для продажи')
    return {'cash_minor': cash, 'deposits_minor': deposits, 'withdrawals_minor': withdrawals,
            'opening_value_minor': opening_value, 'fees_minor': fees, 'positions': positions}


def net_contributions(operations: list[dict]) -> float:
    """Money put into a journal portfolio: opening base + deposits - withdrawals.

    Used as the cost line of value-history snapshots. The sum of purchase prices of
    the bonds still held is wrong there: after an amortization or redemption the
    held cost shrinks while the money stays in the portfolio as cash, and the chart
    showed that as growth (+100% for a plain 50% amortization).
    """
    s = replay(operations)
    return (s['opening_value_minor'] + s['deposits_minor'] - s['withdrawals_minor']) / 100


def summarize(operations: list[dict], rows: list) -> dict:
    state = replay(operations)
    values = {r.id: minor(r.current_value) for r in rows}
    missing = any(r.no_market_data for r in rows)
    securities = sum(values.values())
    cash = state['cash_minor']
    result = securities + cash + state['withdrawals_minor'] - state['deposits_minor'] - state['opening_value_minor']
    return {
        'cash': cash / 100, 'securities_value': securities / 100, 'assets': (securities + cash) / 100,
        'result': None if missing else result / 100,
        'deposits': state['deposits_minor'] / 100, 'withdrawals': state['withdrawals_minor'] / 100,
        'opening_value': state['opening_value_minor'] / 100,
        'coupons': sum(p['coupons_minor'] for p in state['positions'].values()) / 100,
        'principal': sum(p['principal_minor'] for p in state['positions'].values()) / 100,
        'fees': state['fees_minor'] / 100, 'valuation_complete': not missing,
    }


def apply_to_rows(operations: list[dict], rows: list) -> list:
    positions = replay(operations)['positions']
    for row in rows:
        pos = positions.get(row.id)
        if pos is None:
            continue
        row.realized_coupons = pos['coupons_minor'] / 100
        row.realized_principal = pos['principal_minor'] / 100
        row.accounting_managed = True
        # Price column and "profit from purchase" follow the journal's cost basis:
        # after a 50% amortization the remaining bonds cost half, so a price that
        # fell with the face value is not a loss.
        if pos['quantity'] > 0:
            row.purchase_price = round(pos['cost_minor'] / 100 / float(pos['quantity']), 4)
        if row.no_market_data:
            row.full_profit = None
        else:
            value = minor(row.current_value)
            row.profit = (value - pos['cost_minor']) / 100
            row.full_profit = (value - pos['cost_minor'] + pos['realized_minor'] + pos['coupons_minor']) / 100
    return rows


def amortization_per_bond(operations: list[dict], operation: dict) -> Decimal:
    """Face value returned per bond by one amortization (quantity held right before it)."""
    before = [o for o in operations
              if (o['event_date'], o['id']) < (operation['event_date'], operation['id'])]
    pos = replay(before)['positions'].get(operation['item_id'])
    if not pos or pos['quantity'] <= 0:
        return Decimal(0)
    return Decimal(operation['amount_minor']) / 100 / pos['quantity']
