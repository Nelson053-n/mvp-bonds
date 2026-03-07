"""
Portfolio instruments API: add, update, delete, export/import.
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user, get_portfolio_or_403
from app.exceptions import AppError, InstrumentNotFoundError
from app.models import (
    AddInstrumentInput,
    PortfolioTableResponse,
    UpdateCouponInput,
    UpdateCouponRateInput,
    UpdateInstrumentInput,
    ValidationRequest,
    ValidationResponse,
)
from app.services.cache_service import cache_service
from app.services.portfolio_service import portfolio_service
from app.services.storage_service import storage_service
import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["instruments"])


@router.post("/portfolios/{portfolio_id}/validate", response_model=ValidationResponse)
async def validate_input(
    portfolio_id: int,
    payload: ValidationRequest,
    current_user: dict = Depends(get_current_user),
) -> ValidationResponse:
    """Validate instrument data."""
    await get_portfolio_or_403(portfolio_id, current_user)
    return await portfolio_service.validate(payload.user_input)


@router.post("/portfolios/{portfolio_id}/instruments")
async def add_instrument(
    portfolio_id: int,
    payload: AddInstrumentInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Add instrument to portfolio."""
    await get_portfolio_or_403(portfolio_id, current_user)
    logger.info("Add instrument request: portfolio_id=%s payload=%s", portfolio_id, getattr(payload, 'model_dump', lambda: payload)())
    try:
        row = await portfolio_service.add_instrument(portfolio_id, payload)
    except InstrumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AppError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    except Exception as exc:
        logger.exception("Unexpected error while adding instrument to portfolio_id=%s", portfolio_id)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc

    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "company_rating": row.company_rating,
        "current_price": row.current_price,
        "purchase_price": row.purchase_price,
        "quantity": row.quantity,
        "current_value": row.current_value,
        "profit": row.profit,
        "ai_comment": row.ai_comment,
    }


@router.post("/portfolios/{portfolio_id}/instruments/bulk")
async def add_instruments_bulk(
    portfolio_id: int,
    payloads: list[AddInstrumentInput],
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Add multiple instruments at once, with a single cache refresh."""
    await get_portfolio_or_403(portfolio_id, current_user)
    try:
        result = await portfolio_service.add_instruments_bulk(portfolio_id, payloads)
    except AppError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    except Exception:
        logger.exception("Unexpected error in bulk add for portfolio_id=%s", portfolio_id)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")
    return result  # {"added": [...], "failed": [...]}


@router.delete("/portfolios/{portfolio_id}/instruments/{item_id}")
async def delete_instrument(
    portfolio_id: int,
    item_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    """Delete instrument from portfolio."""
    await get_portfolio_or_403(portfolio_id, current_user)

    deleted = portfolio_service.delete_instrument(portfolio_id, item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Строка не найдена")
    return {"deleted": True}


@router.patch("/portfolios/{portfolio_id}/instruments/{item_id}")
async def update_instrument(
    portfolio_id: int,
    item_id: int,
    payload: UpdateInstrumentInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Update instrument in portfolio."""
    await get_portfolio_or_403(portfolio_id, current_user)

    try:
        row = await portfolio_service.update_instrument(portfolio_id, item_id, payload)
    except InstrumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AppError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "company_rating": row.company_rating,
        "current_price": row.current_price,
        "purchase_price": row.purchase_price,
        "quantity": row.quantity,
        "current_value": row.current_value,
        "profit": row.profit,
    }


@router.patch("/portfolios/{portfolio_id}/instruments/{item_id}/coupon")
async def update_coupon(
    portfolio_id: int,
    item_id: int,
    payload: UpdateCouponInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Update coupon for bond instrument."""
    await get_portfolio_or_403(portfolio_id, current_user)

    try:
        row = await portfolio_service.update_coupon(portfolio_id, item_id, payload)
    except InstrumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AppError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "id": row.id,
        "coupon": row.coupon,
        "manual_coupon_set": row.manual_coupon_set,
        "current_value": row.current_value,
        "profit": row.profit,
        "weight": row.weight,
    }


@router.patch("/portfolios/{portfolio_id}/instruments/{item_id}/coupon-rate")
async def update_coupon_rate(
    portfolio_id: int,
    item_id: int,
    payload: UpdateCouponRateInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Update coupon rate for bond instrument."""
    await get_portfolio_or_403(portfolio_id, current_user)

    try:
        row = await portfolio_service.update_coupon_rate(portfolio_id, item_id, payload)
    except InstrumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AppError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "id": row.id,
        "coupon_rate": row.coupon_rate,
        "manual_coupon_rate_set": row.manual_coupon_rate_set,
    }


class MoveInstrumentInput(BaseModel):
    target_portfolio_id: int


@router.post("/portfolios/{portfolio_id}/instruments/{item_id}/move")
async def move_instrument(
    portfolio_id: int,
    item_id: int,
    payload: MoveInstrumentInput,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    """Move instrument to another portfolio owned by the same user."""
    await get_portfolio_or_403(portfolio_id, current_user)
    await get_portfolio_or_403(payload.target_portfolio_id, current_user)

    moved = storage_service.move_instrument(item_id, portfolio_id, payload.target_portfolio_id)
    if not moved:
        raise HTTPException(status_code=404, detail="Инструмент не найден")
    return {"moved": True}


@router.delete("/portfolios/{portfolio_id}/instruments/cleanup/not-found")
async def delete_not_found_instruments(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict[str, int]:
    """Remove instruments not found on MOEX."""
    await get_portfolio_or_403(portfolio_id, current_user)

    deleted_count = await portfolio_service.remove_not_found_instruments(portfolio_id)
    return {"deleted_count": deleted_count}


@router.get(
    "/portfolios/{portfolio_id}/table", response_model=PortfolioTableResponse
)
async def get_table(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> PortfolioTableResponse:
    """Get portfolio table."""
    await get_portfolio_or_403(portfolio_id, current_user)

    rows = await portfolio_service.get_table(portfolio_id)
    return PortfolioTableResponse(items=rows)


@router.get("/portfolios/{portfolio_id}/export")
async def export_csv(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Export portfolio as CSV file."""
    await get_portfolio_or_403(portfolio_id, current_user)

    items = storage_service.get_items(portfolio_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ticker", "instrument_type", "quantity", "purchase_price"])
    for item in items:
        writer.writerow(
            [
                item["ticker"],
                item["instrument_type"],
                item["quantity"],
                item["purchase_price"],
            ]
        )

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=portfolio.csv"},
    )


@router.post("/portfolios/{portfolio_id}/import")
async def import_csv(
    portfolio_id: int,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Import portfolio from CSV file.

    Expected columns: ticker, instrument_type, quantity, purchase_price
    instrument_type must be 'bond' or 'stock'.
    """
    await get_portfolio_or_403(portfolio_id, current_user)

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1251")

    reader = csv.DictReader(io.StringIO(text))
    required = {"ticker", "instrument_type", "quantity", "purchase_price"}

    added = 0
    errors: list[str] = []

    for i, row in enumerate(reader, start=2):
        missing = required - set(row.keys())
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Отсутствуют колонки: {', '.join(missing)}",
            )

        ticker = str(row["ticker"]).strip().upper()
        instrument_type = str(row["instrument_type"]).strip().lower()

        if instrument_type not in ("bond", "stock"):
            errors.append(f"Строка {i}: неверный тип '{instrument_type}'")
            continue

        try:
            quantity = float(row["quantity"])
            purchase_price = float(row["purchase_price"])
        except ValueError:
            errors.append(f"Строка {i}: неверные числовые значения")
            continue

        if quantity <= 0 or purchase_price <= 0:
            errors.append(f"Строка {i}: quantity и purchase_price должны быть > 0")
            continue

        try:
            # Check if item with same ticker and type already exists
            existing = storage_service.get_item_by_ticker(
                portfolio_id, ticker, instrument_type
            )
            if existing:
                # Update existing item
                storage_service.update_item(
                    existing["id"],
                    portfolio_id,
                    quantity,
                    purchase_price,
                )
            else:
                # Add new item
                storage_service.add_item(
                    ticker=ticker,
                    instrument_type=instrument_type,
                    quantity=quantity,
                    purchase_price=purchase_price,
                    portfolio_id=portfolio_id,
                )
            added += 1
        except Exception as exc:
            errors.append(f"Строка {i}: {exc}")

    if added > 0:
        cache_service.invalidate(portfolio_id)

    return {"added": added, "errors": len(errors), "error_details": errors}
