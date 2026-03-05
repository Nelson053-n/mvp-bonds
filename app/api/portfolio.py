import csv
import io

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

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
from app.services.portfolio_service import portfolio_service
from app.services.storage_service import storage_service
from app.services.cache_service import cache_service


router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.post("/validate", response_model=ValidationResponse)
async def validate_input(payload: ValidationRequest) -> ValidationResponse:
    return await portfolio_service.validate(payload.user_input)


@router.post("/instruments")
async def add_instrument(payload: AddInstrumentInput) -> dict:
    try:
        row = await portfolio_service.add_instrument(payload)
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
        "ai_comment": row.ai_comment,
    }


@router.delete("/instruments/{item_id}")
async def delete_instrument(item_id: int) -> dict[str, bool]:
    deleted = portfolio_service.delete_instrument(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Строка не найдена")
    return {"deleted": True}


@router.patch("/instruments/{item_id}")
async def update_instrument(
    item_id: int,
    payload: UpdateInstrumentInput,
) -> dict:
    try:
        row = await portfolio_service.update_instrument(item_id, payload)
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


@router.patch("/instruments/{item_id}/coupon")
async def update_coupon(
    item_id: int,
    payload: UpdateCouponInput,
) -> dict:
    try:
        row = await portfolio_service.update_coupon(item_id, payload)
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


@router.patch("/instruments/{item_id}/coupon-rate")
async def update_coupon_rate(
    item_id: int,
    payload: UpdateCouponRateInput,
) -> dict:
    try:
        row = await portfolio_service.update_coupon_rate(item_id, payload)
    except InstrumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AppError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "id": row.id,
        "coupon_rate": row.coupon_rate,
        "manual_coupon_rate_set": row.manual_coupon_rate_set,
    }


@router.delete("/instruments/cleanup/not-found")
async def delete_not_found_instruments() -> dict[str, int]:
    deleted_count = await portfolio_service.remove_not_found_instruments()
    return {"deleted_count": deleted_count}


@router.get("/table", response_model=PortfolioTableResponse)
async def get_table() -> PortfolioTableResponse:
    rows = await portfolio_service.get_table()
    return PortfolioTableResponse(items=rows)


# ── CSV Export ───────────────────────────────────────────────────────────────

@router.get("/export")
async def export_csv() -> StreamingResponse:
    """Export portfolio as CSV file."""
    items = storage_service.get_items()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ticker", "instrument_type", "quantity", "purchase_price"])
    for item in items:
        writer.writerow([
            item["ticker"],
            item["instrument_type"],
            item["quantity"],
            item["purchase_price"],
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=portfolio.csv"
        },
    )


# ── CSV Import ───────────────────────────────────────────────────────────────

@router.post("/import")
async def import_csv(file: UploadFile = File(...)) -> dict[str, int]:
    """Import portfolio from CSV file.

    Expected columns: ticker, instrument_type, quantity, purchase_price
    instrument_type must be 'bond' or 'stock'.
    """
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
            storage_service.add_item(
                ticker=ticker,
                instrument_type=instrument_type,
                quantity=quantity,
                purchase_price=purchase_price,
            )
            added += 1
        except Exception as exc:
            errors.append(f"Строка {i}: {exc}")

    if added > 0:
        cache_service.invalidate()

    return {"added": added, "errors": len(errors), "error_details": errors}
