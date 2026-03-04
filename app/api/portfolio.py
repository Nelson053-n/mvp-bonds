from fastapi import APIRouter, HTTPException

from app.models import (
    AddInstrumentInput,
    PortfolioTableResponse,
    UpdateCouponInput,
    UpdateInstrumentInput,
    ValidationRequest,
    ValidationResponse,
)
from app.services.portfolio_service import portfolio_service


router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.post("/validate", response_model=ValidationResponse)
async def validate_input(payload: ValidationRequest) -> ValidationResponse:
    return await portfolio_service.validate(payload.user_input)


@router.post("/instruments")
async def add_instrument(payload: AddInstrumentInput) -> dict:
    try:
        row = await portfolio_service.add_instrument(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    except ValueError as exc:
        message = str(exc)
        if "не найдена" in message.lower():
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc

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
    except ValueError as exc:
        message = str(exc)
        if "не найдена" in message.lower():
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc

    return {
        "id": row.id,
        "coupon": row.coupon,
        "manual_coupon_set": row.manual_coupon_set,
        "current_value": row.current_value,
        "profit": row.profit,
        "weight": row.weight,
    }


@router.delete("/instruments/cleanup/not-found")
async def delete_not_found_instruments() -> dict[str, int]:
    deleted_count = await portfolio_service.remove_not_found_instruments()
    return {"deleted_count": deleted_count}


@router.get("/table", response_model=PortfolioTableResponse)
async def get_table() -> PortfolioTableResponse:
    rows = await portfolio_service.get_table()
    return PortfolioTableResponse(items=rows)
