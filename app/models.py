from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


InstrumentType = Literal["stock", "bond"]


class AddInstrumentInput(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)
    quantity: float = Field(..., gt=0)
    purchase_price: float | None = Field(None, gt=0)  # None = auto-fetch from MOEX
    purchase_date: date | None = None  # optional; enables realized-coupon profit
    # Off-exchange ("custom") item: no MOEX lookup. When is_custom=True the bond is
    # built from these fields and ticker is just a label.
    is_custom: bool = False
    instrument_type: InstrumentType | None = None  # required when is_custom
    custom_name: str | None = Field(None, max_length=128)
    current_price: float | None = Field(None, gt=0)  # user-set live price (custom only)
    coupon_rate: float | None = Field(None, ge=0)    # annual coupon %, custom bonds
    custom_nominal: float | None = Field(None, gt=0)        # face value, custom bonds
    custom_coupon_freq: int | None = Field(None, gt=0)      # coupon payments per year (2/4/12)
    custom_maturity: date | None = None                     # maturity date, custom bonds


class UpdateInstrumentInput(BaseModel):
    quantity: float = Field(..., gt=0)
    purchase_price: float = Field(..., gt=0)
    purchase_date: date | None = None  # optional
    current_price: float | None = Field(None, gt=0)  # custom items: update live price
    manual_rating: str | None = Field(None, max_length=16)  # user-set credit rating override
    custom_nominal: float | None = Field(None, gt=0)        # face value, custom bonds
    custom_coupon_freq: int | None = Field(None, gt=0)      # coupon payments per year (2/4/12)
    custom_maturity: date | None = None                     # maturity date, custom bonds
    coupon_rate: float | None = Field(None, ge=0)           # annual coupon %, custom bonds


class UpdateCouponInput(BaseModel):
    coupon: float = Field(..., ge=0)


class UpdateCouponRateInput(BaseModel):
    coupon_rate: float = Field(..., ge=0)


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
    day_profit: float | None = None  # P&L vs. previous trading session close (RUB)
    prev_close_value: float | None = None  # Position value at prev close (RUB)
    weight: float
    company_rating: str | None = None
    # Origin of company_rating: 'manual' | 'smartlab' | 'moex' | 'listlevel' | 'db' | None.
    # 'listlevel' is a coarse listing-level proxy, NOT a real issuer rating — it must
    # never trigger a rating-change alert nor overwrite a real rating in the DB.
    rating_source: str | None = None
    is_qual: bool = False
    is_traded: bool = True
    # Котировку получить не удалось (бумаги нет на MOEX — например,
    # иностранная, приехавшая синком брокера). Отличает «данных нет» от
    # честного нуля: без флага такая позиция выглядит как обнулившаяся,
    # с убытком на всю сумму покупки.
    no_market_data: bool = False
    coupon: float | None = None
    coupon_period: int | None = None
    coupon_rate: float | None = None  # Ставка купона в % от номинала
    manual_coupon_set: bool = False
    manual_coupon_rate_set: bool = False
    is_floater: bool = False  # True if coupon is based on last known (floater)
    maturity_date: date | None = None
    buyback_date: date | None = None
    offer_date: date | None = None
    next_coupon_date: date | None = None
    nominal: float | None = None
    aci: float | None = None
    realized_coupons: float | None = None  # Σ coupons paid since purchase (RUB)
    full_profit: float | None = None  # revaluation + aci×qty + realized_coupons (RUB)
    market_yield: float | None = None
    dividend_yield: float | None = None
    face_unit: str | None = None  # Валюта номинала (SUR, CNY, USD, EUR, CHF)
    fx_rate: float | None = None  # Курс валюты номинала к рублю (None/1.0 для рублёвых)
    purchase_date: str | None = None  # ISO date or None (optional)
    source: str | None = None  # 'manual' | 'tbank' | 'custom'
    ai_comment: str


class PortfolioTableResponse(BaseModel):
    items: list[InstrumentMetrics]


class ValidationRequest(BaseModel):
    user_input: AddInstrumentInput


class BondSnapshot(BaseModel):
    ticker: str
    name: str
    clean_price_percent: float
    prev_close_percent: float | None = None  # Closing price of previous trading session, % of face
    nominal: float | None = None
    coupon: float | None = None
    coupon_period: int | None = None
    coupon_rate: float | None = None  # Ставка купона в % от номинала
    maturity_date: date | None = None
    buyback_date: date | None = None
    offer_date: date | None = None
    next_coupon_date: date | None = None
    aci: float | None = None
    market_yield: float | None = None
    company_rating: str | None = None
    rating_source: str | None = None  # 'smartlab' | 'moex' | 'listlevel' | None
    is_qual: bool = False
    is_traded: bool = True
    face_unit: str = "SUR"  # Валюта номинала (SUR=RUB, CNY, USD, EUR, CHF)
    fx_rate: float = 1.0    # Курс валюты к рублю (1.0 для рублёвых)
    is_floater: bool = False  # True if coupon is derived from bondization (not announced yet)


class StockSnapshot(BaseModel):
    ticker: str
    name: str
    current_price: float
    prev_close_price: float | None = None  # Previous trading session close (RUB)
    dividend_yield: float | None = None
    company_rating: str | None = None
    rating_source: str | None = None  # 'smartlab' | 'moex' | 'listlevel' | None
