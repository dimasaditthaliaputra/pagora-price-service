from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class Commodity(BaseModel):
    code: str
    name: str
    category: Optional[str]
    unit: str


class PricePoint(BaseModel):
    date: date
    price: float = Field(gt=0)


class PriceSeries(BaseModel):
    code: str
    source: str = "bank_indonesia"
    region: str = "national"
    start_date: date
    end_date: date
    prices: list[PricePoint]


class ForecastRequest(BaseModel):
    history: list[PricePoint]
    horizon_days: int = Field(default=30, ge=1)


class ForecastResponse(BaseModel):
    model: str = "linear_regression"
    sample_size: int
    history_start: date
    history_end: date
    slope_per_day: float
    intercept: float
    r_squared: float
    predictions: list[PricePoint]
