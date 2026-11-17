import asyncio
import contextlib
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from app.bi_client import COMMODITY_CODE_PATTERN, BankIndonesiaClient, BankIndonesiaError
from app.config import Settings, get_settings
from app.hybrid import ForecastError, hybrid_forecast
from app.price_store import PriceStore
from app.schemas import (
    Commodity,
    CommodityDataStatus,
    ForecastResponse,
    ForecastUnavailable,
    PriceSeries,
    ServiceStatus,
    SyncProgress,
)
from app.sync import ServiceState, maintain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def price_store_path(settings: Settings) -> Path:
    """Satu file per jenis harga, agar mengganti BI_PRICE_TYPE_ID memicu backfill bersih, bukan seri campuran."""
    return Path(settings.data_dir) / f"prices_type{settings.bi_price_type_id}.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.bi_client = BankIndonesiaClient(
        base_url=settings.bi_base_url,
        timeout=settings.bi_timeout_seconds,
        price_type_id=settings.bi_price_type_id,
        cache_seconds=settings.commodity_cache_seconds,
    )
    store = PriceStore(price_store_path(settings))
    store.load()
    app.state.service = ServiceState(store=store)

    task = asyncio.create_task(maintain(
        app.state.service, app.state.bi_client, settings.history_days,
        settings.max_horizon_days, settings.sync_interval_hours, settings.sync_enabled,
    ))
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await app.state.bi_client.aclose()


app = FastAPI(title="PAGORA Price Service", version="2.0.0", lifespan=lifespan)


# Mendaftarkan skema keamanan agar Swagger menampilkan tombol "Authorize".
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Depends(api_key_header)],
) -> None:
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key tidak valid.")


def bi_client(request: Request) -> BankIndonesiaClient:
    return request.app.state.bi_client


def service_state(request: Request) -> ServiceState:
    return request.app.state.service


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
    code: Annotated[
        str,
        Query(
            pattern=COMMODITY_CODE_PATTERN.pattern,
            description="Kode komoditas dari /v1/commodities, format com_N.",
            examples=["com_1"],
        ),
    ],
    start_date: Annotated[
        date,
        Query(description="Tanggal awal, format YYYY-MM-DD (tahun-bulan-tanggal).", examples=["2026-11-01"]),
    ],
    end_date: Annotated[
        date,
        Query(
            description="Tanggal akhir, format YYYY-MM-DD. Harus >= start_date dan rentang maksimal 400 hari.",
            examples=["2026-11-14"],
        ),
    ],
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


def progress_schema(state: ServiceState) -> SyncProgress | None:
    progress = state.progress
    if progress is None:
        return None
    return SyncProgress(
        started_at=progress.started_at, done=progress.done, total=progress.total,
        current=progress.current, backfill=progress.backfill,
    )


def forecast_unavailable(state: ServiceState) -> JSONResponse:
    """
    503 dengan body terstruktur, agar frontend bisa membedakan "sedang sinkron" (normal,
    tampilkan indikator sinkronisasi) dari kegagalan sungguhan.
    """
    if state.busy:
        progress = state.progress
        message = "Sedang menyinkronkan data harga dari Bank Indonesia. Prediksi tersedia setelah selesai."
        if progress and progress.backfill:
            message = ("Sedang mengunduh riwayat harga pertama dari Bank Indonesia (±35 menit). "
                       "Prediksi tersedia setelah selesai.")
        body = ForecastUnavailable(status="syncing", message=message, phase=state.phase,
                                   progress=progress_schema(state), next_sync_at=None)
        retry_after = 60
    else:
        body = ForecastUnavailable(
            status="unavailable",
            message="Data harga belum berhasil diunduh dari Bank Indonesia. Sinkronisasi akan dicoba lagi.",
            phase=state.phase, progress=None, next_sync_at=state.next_sync_at,
        )
        retry_after = 3600
        if state.next_sync_at:
            retry_after = max(60, int((state.next_sync_at - datetime.now(timezone.utc)).total_seconds()))

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body.model_dump(mode="json"),
        headers={"Retry-After": str(retry_after)},
    )


@app.get(
    "/v1/forecast",
    dependencies=[Authorized],
    responses={503: {"model": ForecastUnavailable, "description": "Model belum siap (lihat field status)."}},
)
async def forecast(
    state: Annotated[ServiceState, Depends(service_state)],
    settings: Annotated[Settings, Depends(get_settings)],
    code: Annotated[
        str,
        Query(
            pattern=COMMODITY_CODE_PATTERN.pattern,
            description="Kode komoditas dari /v1/commodities, format com_N.",
            examples=["com_1"],
        ),
    ],
    horizon_days: Annotated[
        int,
        Query(ge=1, description="Jumlah hari kalender ke depan yang diprediksi, 1 sampai 90.", examples=[30]),
    ] = 30,
) -> ForecastResponse:
    """Prediksi model hibrida dari riwayat harga BI yang disinkronkan service ini sendiri."""
    if horizon_days > settings.max_horizon_days:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"horizon_days maksimal {settings.max_horizon_days}.",
        )

    if state.model is None:
        return forecast_unavailable(state)

    series = state.store.series(code)
    if series is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Belum ada data harga untuk {code}.")

    dates, prices = series
    try:
        result = hybrid_forecast(state.model, dates, prices, horizon_days)
    except ForecastError as exception:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exception)) from exception

    return ForecastResponse(
        code=code,
        model=result.method,
        volatility=result.volatility,
        history_end=dates[-1],
        sample_size=len(prices),
        trained_at=state.model.trained_at,
        syncing=state.busy,
        predictions=result.predictions,
    )


@app.get("/v1/status", dependencies=[Authorized])
async def service_status(state: Annotated[ServiceState, Depends(service_state)]) -> ServiceStatus:
    """Fase & progres sinkronisasi, waktu sinkron/latih terakhir, dan rentang data per komoditas."""
    commodities = []
    for code, (dates, prices) in state.store.snapshot().items():
        commodities.append(CommodityDataStatus(code=code, data_from=dates[0], data_until=dates[-1], points=len(prices)))

    return ServiceStatus(
        phase=state.phase,
        ready=state.model is not None,
        progress=progress_schema(state),
        last_sync=state.last_sync,
        next_sync_at=state.next_sync_at,
        model_trained_at=state.model.trained_at if state.model else None,
        failed_codes=state.failed_codes,
        commodities=commodities,
    )
