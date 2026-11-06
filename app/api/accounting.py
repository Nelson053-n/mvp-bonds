"""Opt-in manual accounting inside the portfolio cabinet."""
import csv
import io
import logging
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.api.deps import get_current_user, get_portfolio_or_403
from app.config import settings
from app.services.accounting_service import latest_event_date, minor, net_contributions, summarize
from app.services.cache_service import cache_service
from app.services.payment_schedule_service import load_payments, proposals
from app.services.portfolio_service import portfolio_service
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/portfolios/{portfolio_id}/accounting', tags=['accounting'])


async def refresh_account_snapshot(portfolio_id: int):
    cache_service.invalidate(portfolio_id)
    rows = await portfolio_service.get_table(portfolio_id)
    if any(r.no_market_data for r in rows):
        return
    securities = sum(r.current_value for r in rows)
    cash = await portfolio_service.get_cash_rub(portfolio_id)
    storage_service.save_portfolio_snapshot(
        portfolio_id, securities + cash,
        net_contributions(storage_service.get_account_operations(portfolio_id)), securities,
        no_open_positions=not rows)


async def access(portfolio_id: int, user: dict = Depends(get_current_user)):
    await get_portfolio_or_403(portfolio_id, user)
    return user


def require_feature():
    if not settings.manual_accounting_enabled:
        raise HTTPException(404, 'Ручной учёт пока недоступен')


class Initialize(BaseModel):
    cash: Decimal = Field(default=Decimal(0), ge=0, le=1000000000, decimal_places=2)
    basis: Literal['current', 'purchase'] = 'current'


class Operation(BaseModel):
    request_id: UUID
    kind: Literal['deposit', 'withdraw', 'buy', 'sell', 'coupon', 'amortization', 'redemption', 'fee']
    event_date: date
    amount: Decimal = Field(gt=0, le=1000000000, decimal_places=2)
    quantity: Decimal = Field(default=Decimal(0), ge=0, le=10000000, decimal_places=6)
    item_id: int | None = Field(default=None, gt=0)
    ticker: str | None = Field(default=None, max_length=32, pattern=r'^[A-Za-z0-9._-]+$')
    payment_key: str | None = Field(default=None, max_length=160, pattern=r'^moex:[0-9]+:(coupon|amortization):[0-9]{4}-[0-9]{2}-[0-9]{2}$')

    @model_validator(mode='after')
    def quantities(self):
        if self.kind in ('buy', 'sell', 'redemption') and self.quantity <= 0:
            raise ValueError('Укажите количество облигаций')
        if self.kind in ('deposit', 'withdraw') and (self.item_id or self.ticker or self.payment_key):
            raise ValueError('Пополнение и вывод относятся ко всему портфелю')
        if self.ticker and (self.kind != 'buy' or self.item_id):
            raise ValueError('Новый тикер можно указать только для покупки новой позиции')
        if self.payment_key and self.payment_key != f'moex:{self.item_id}:{self.kind}:{self.event_date.isoformat()}':
            raise ValueError('Параметры выплаты не совпадают с предложением')
        return self


@router.get('')
async def account_state(portfolio_id: int, user=Depends(access)):
    if not settings.manual_accounting_enabled:
        return {'available': False, 'enabled': False}
    info = storage_service.get_account_info(portfolio_id)
    if not info:
        return {'available': True, 'enabled': False}
    operations = storage_service.get_account_operations(portfolio_id)
    rows = await portfolio_service.get_table(portfolio_id)
    with storage_service._connect() as conn:
        items = [dict(zip(('id', 'ticker', 'quantity', 'source', 'custom_price'), r)) for r in conn.execute(
            'SELECT id,ticker,quantity,source,custom_price FROM portfolio_items WHERE portfolio_id=? ORDER BY id', (portfolio_id,))]
    return {'available': True, 'enabled': True, **info, 'operations': operations,
            'items': items, 'totals': summarize(operations, rows)}


@router.post('/initialize')
async def initialize(portfolio_id: int, payload: Initialize, user=Depends(access)):
    require_feature()
    rows = await portfolio_service.get_table(portfolio_id)
    if any(r.type != 'bond' or r.source == 'tbank' or r.face_unit not in (None, '', 'RUB', 'SUR') for r in rows):
        raise HTTPException(400, 'Первая версия учёта поддерживает только ручные рублёвые облигации')
    if any(r.no_market_data or r.current_value <= 0 for r in rows):
        raise HTTPException(400, 'Сначала загрузите актуальные цены всех позиций')
    if payload.basis == 'purchase' and any(not r.purchase_date or r.purchase_date > latest_event_date().isoformat() for r in rows):
        raise HTTPException(400, 'Для восстановления укажите дату покупки каждой позиции')
    try:
        storage_service.initialize_account(portfolio_id, minor(payload.cash), payload.basis, rows)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await refresh_account_snapshot(portfolio_id)
    return {'ok': True}


@router.post('/operations')
async def add_operation(portfolio_id: int, payload: Operation, user=Depends(access)):
    require_feature()
    if not storage_service.accounting_enabled(portfolio_id):
        raise HTTPException(409, 'Сначала включите учёт')
    new_item = None
    if payload.ticker:
        from app.services.moex_service import moex_service
        snapshot = await moex_service.get_bond_snapshot(payload.ticker.upper())
        if snapshot.face_unit not in ('SUR', 'RUB', ''):
            raise HTTPException(400, 'Поддерживаются только рублёвые облигации')
        new_item = {'ticker': snapshot.ticker, 'price': float(payload.amount / payload.quantity)}
    data = {'kind': payload.kind, 'event_date': payload.event_date.isoformat(),
            'amount_minor': minor(payload.amount), 'quantity': str(payload.quantity), 'item_id': payload.item_id,
            'external_key': payload.payment_key or f'manual:{payload.request_id}',
            'source': 'moex_confirmed' if payload.payment_key else 'manual'}
    try:
        op_id = storage_service.add_account_operation(portfolio_id, data, new_item)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await refresh_account_snapshot(portfolio_id)
    return {'ok': True, 'operation_id': op_id}


@router.delete('/operations/{operation_id}')
async def undo_operation(portfolio_id: int, operation_id: int, user=Depends(access)):
    require_feature()
    try:
        storage_service.undo_account_operation(portfolio_id, operation_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await refresh_account_snapshot(portfolio_id)
    return {'ok': True}


class Valuation(BaseModel):
    item_id: int = Field(gt=0)
    price: Decimal = Field(ge=0, le=100000000, decimal_places=4)


@router.post('/valuation')
async def valuation(portfolio_id: int, payload: Valuation, user=Depends(access)):
    require_feature()
    if not storage_service.accounting_enabled(portfolio_id):
        raise HTTPException(409, 'Сначала включите учёт')
    with storage_service._connect() as conn:
        cur = conn.execute("UPDATE portfolio_items SET custom_price=? WHERE id=? AND portfolio_id=? AND source='custom' AND deleted_at IS NULL",
                           (float(payload.price), payload.item_id, portfolio_id))
        if not cur.rowcount:
            raise HTTPException(400, 'Ручная оценка доступна только для внебиржевой позиции')
        conn.commit()
    logger.info('AUDIT manual_valuation portfolio_id=%d item_id=%d price=%s', portfolio_id, payload.item_id, payload.price)
    await refresh_account_snapshot(portfolio_id)
    return {'ok': True}


@router.get('/payments')
async def pending_payments(portfolio_id: int, user=Depends(access)):
    require_feature()
    info = storage_service.get_account_info(portfolio_id)
    if not info:
        raise HTTPException(409, 'Сначала включите учёт')
    if not storage_service.check_rate_limit(f'manual_payments:{portfolio_id}', 60, 3):
        raise HTTPException(429, 'График недавно загружался. Повторите через минуту.')
    operations = storage_service.get_account_operations(portfolio_id)
    with storage_service._connect() as conn:
        items = conn.execute("SELECT id,ticker FROM portfolio_items WHERE portfolio_id=? AND source!='custom'", (portfolio_id,)).fetchall()
    out, errors = [], []
    for iid, ticker in items:
        try:
            out.extend(proposals(iid, ticker, await load_payments(ticker), operations, info['started_on']))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning('Payment schedule failed ticker=%s: %s', ticker, type(exc).__name__)
            errors.append(ticker)
    return {'payments': out, 'unavailable_tickers': errors,
            'notice': 'Это график, а не подтверждение зачисления. Проверьте сумму и право на выплату у брокера.'}


@router.get('/export')
async def export_journal(portfolio_id: int, user=Depends(access)):
    require_feature()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['date', 'kind', 'item_id', 'amount_rub', 'quantity', 'source'])
    for op in storage_service.get_account_operations(portfolio_id):
        writer.writerow([op['event_date'], op['kind'], op['item_id'], f"{op['amount_minor']/100:.2f}", op['quantity'], op['source']])
    return StreamingResponse(iter([output.getvalue()]), media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename=operations.csv'})
