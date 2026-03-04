from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


InstrumentType = Literal["stock", "bond"]


class AddInstrumentInput(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)
    quantity: float = Field(..., gt=0)
    purchase_price: float = Field(..., gt=0)


class UpdateInstrumentInput(BaseModel):
    quantity: float = Field(..., gt=0)
    purchase_price: float = Field(..., gt=0)


class UpdateCouponInput(BaseModel):
    coupon: float = Field(..., ge=0)


class ValidationResponse(BaseModel):
    instrument_type: InstrumentType
    validated: bool
    warnings: list[str] = Field(default_factory=list)


class InstrumentMetrics(BaseModel):
    id: int
    type: InstrumentType
    name: str
    ticker: str
    current_price: float
    purchase_price: float
    quantity: float
    current_value: float
    profit: float
    weight: float
    company_rating: str | None = None
    coupon: float | None = None
    coupon_period: int | None = None
    coupon_rate: float | None = None  # Ставка купона в % от номинала
    manual_coupon_set: bool = False
    maturity_date: date | None = None
    aci: float | None = None
    market_yield: float | None = None
    dividend_yield: float | None = None
    ai_comment: str


class PortfolioTableResponse(BaseModel):
    items: list[InstrumentMetrics]


class ValidationRequest(BaseModel):
    user_input: AddInstrumentInput


class BondSnapshot(BaseModel):
    ticker: str
    name: str
    clean_price_percent: float
    nominal: float | None = None
    coupon: float | None = None
    coupon_period: int | None = None
    coupon_rate: float | None = None  # Ставка купона в % от номинала
    maturity_date: date | None = None
    aci: float | None = None
    market_yield: float | None = None
    company_rating: str | None = None


class StockSnapshot(BaseModel):
    ticker: str
    name: str
    current_price: float
    dividend_yield: float | None = None
    company_rating: str | None = None
