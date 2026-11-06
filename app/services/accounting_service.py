"""Confirmed RUB cash flows for opt-in manual portfolios."""
from decimal import Decimal, ROUND_HALF_UP


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
                                        'coupons_minor': 0, 'principal_minor': 0}) if iid else None
        if kind == 'opening':
            pos['quantity'] += qty
            pos['net_cost_minor'] += amount
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
        elif kind == 'sell':
            cash += amount
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
        elif kind == 'coupon':
            cash += amount
            pos['coupons_minor'] += amount
        elif kind == 'fee':
            cash -= amount
            fees += amount
            if pos:
                pos['net_cost_minor'] += amount
        else:
            raise ValueError('Неизвестная операция')
        if cash < 0:
            raise ValueError('Недостаточно свободных денег. Сначала внесите пополнение.')
        if pos and pos['quantity'] < 0:
            raise ValueError('Недостаточно облигаций для продажи')
    return {'cash_minor': cash, 'deposits_minor': deposits, 'withdrawals_minor': withdrawals,
            'opening_value_minor': opening_value, 'fees_minor': fees, 'positions': positions}


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
        row.full_profit = (None if row.no_market_data else
                           (minor(row.current_value) - pos['net_cost_minor'] + pos['coupons_minor']) / 100)
    return rows
