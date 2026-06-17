"""Tax helper for Russian bond/stock portfolios (Pro feature).

Estimates personal income tax (НДФЛ) on:
  - coupon income actually received (realized_coupons, T-Bank sync only), and
  - the unrealized financial result (current value − purchase cost) as a
    *forecast* of what tax would be owed if positions were sold now.

This is an ESTIMATE for planning, NOT a substitute for the broker's tax
statement (брокер — налоговый агент и удерживает налог сам). Rates: 13% up to
5 000 000 ₽ of annual base, 15% above (2025+ progressive scale, simplified).
"""
from __future__ import annotations

_THRESHOLD = 5_000_000.0
_RATE_LOW = 0.13
_RATE_HIGH = 0.15


def _tax_on(base: float) -> float:
    """Progressive НДФЛ on a positive base."""
    if base <= 0:
        return 0.0
    if base <= _THRESHOLD:
        return round(base * _RATE_LOW, 2)
    return round(_THRESHOLD * _RATE_LOW + (base - _THRESHOLD) * _RATE_HIGH, 2)


def build_tax_report(rows: list) -> dict:
    """Build a per-position tax estimate from portfolio rows.

    `rows` are PortfolioRow-like objects (attrs: ticker, name, quantity,
    purchase_price, current_value, profit, realized_coupons).
    """
    positions = []
    total_coupon = 0.0
    total_gain = 0.0   # positive financial result only (losses net within a year)
    net_result = 0.0   # gains − losses (annual netting base)

    for r in rows:
        qty = getattr(r, "quantity", 0) or 0
        purchase = (getattr(r, "purchase_price", 0) or 0) * qty
        value = getattr(r, "current_value", 0) or 0
        result = round(value - purchase, 2)          # unrealized fin. result
        coupons = round(getattr(r, "realized_coupons", 0) or 0, 2)

        total_coupon += coupons
        net_result += result
        if result > 0:
            total_gain += result

        positions.append({
            "ticker": getattr(r, "ticker", ""),
            "name": getattr(r, "name", ""),
            "quantity": qty,
            "purchase_cost": round(purchase, 2),
            "current_value": round(value, 2),
            "result": result,
            "coupons_received": coupons,
        })

    # Coupons are always taxable; the financial-result base uses annual netting
    # (gains minus losses across positions, floored at 0).
    coupon_base = round(total_coupon, 2)
    result_base = round(max(net_result, 0.0), 2)
    total_base = round(coupon_base + result_base, 2)

    return {
        "available": True,
        "positions": positions,
        "summary": {
            "coupon_income": coupon_base,
            "coupon_tax": _tax_on(coupon_base),
            "financial_result": round(net_result, 2),
            "taxable_result": result_base,
            "result_tax": _tax_on(result_base),
            "total_base": total_base,
            "total_tax": _tax_on(total_base),
        },
        "disclaimer": (
            "Оценка для планирования. Налог по облигациям удерживает брокер "
            "(налоговый агент) при выплате купона и продаже. Финрезультат "
            "рассчитан по текущей цене как прогноз при продаже сейчас; "
            "реализованные купоны доступны только для портфелей из Т-Банка."
        ),
    }
