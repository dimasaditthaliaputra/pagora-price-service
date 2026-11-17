import secrets
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status

from app.bi_client import COMMODITY_CODE_PATTERN, BankIndonesiaClient, BankIndonesiaError
from app.config import Settings, get_settings
from app.forecast import ForecastError, linear_forecast
from app.schemas import Commodity, ForecastRequest, ForecastResponse, PriceSeries


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.bi_client = BankIndonesiaClient(
        base_url=settings.bi_base_url,
        timeout=settings.bi_timeout_seconds,
        price_type_id=settings.bi_price_type_id,
        cache_seconds=settings.commodity_cache_seconds,
    )
    yield
    await app.state.bi_client.aclose()


app = FastAPI(title="PAGORA Price Service", version="1.0.0", lifespan=lifespan)


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key tidak valid.")


def bi_client(request: Request) -> BankIndonesiaClient:
    return request.app.state.bi_client


Authorized = Depends(require_api_key)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/commodities", dependencies=[Authorized])
async def commodities(client: Annotated[BankIndonesiaClient, Depends(bi_client)]) -> list[Commodity]:
    try:
        return await client.commodities()
    except BankIndonesiaError as exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exception)) from exception


@app.get("/v1/prices", dependencies=[Authorized])
async def prices(
    client: Annotated[BankIndonesiaClient, Depends(bi_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    code: Annotated[str, Query(pattern=COMMODITY_CODE_PATTERN.pattern)],
    start_date: date,
    end_date: date,
) -> PriceSeries:
    if start_date > end_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_date harus ≤ end_date.")

    if (end_date - start_date).days > settings.max_range_days:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Rentang maksimal {settings.max_range_days} hari.",
        )

    try:
        points = await client.national_daily_prices(code, start_date, end_date)
    except BankIndonesiaError as exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exception)) from exception

    return PriceSeries(code=code, start_date=start_date, end_date=end_date, prices=points)


@app.post("/v1/forecast", dependencies=[Authorized])
async def forecast(
    payload: ForecastRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ForecastResponse:
    if payload.horizon_days > settings.max_horizon_days:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"horizon_days maksimal {settings.max_horizon_days}.",
        )

    try:
        return linear_forecast(payload.history, payload.horizon_days, settings.min_forecast_points)
    except ForecastError as exception:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exception)) from exception
