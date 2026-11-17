from datetime import date, datetime
from datetime import date as Date
from typing import Optional

from pydantic import BaseModel, Field


class Commodity(BaseModel):
    code: str
    name: str
    category: Optional[str]
    unit: str


class PricePoint(BaseModel):
    date: Date = Field(description="Tanggal, format YYYY-MM-DD (tahun-bulan-tanggal).", examples=["2026-11-01"])
    price: float = Field(
        gt=0,
        description="Harga dalam Rupiah, berupa angka tanpa 'Rp' dan tanpa pemisah ribuan.",
        examples=[15000],
    )


class PriceSeries(BaseModel):
    code: str
    source: str = "bank_indonesia"
    region: str = "national"
    start_date: date
    end_date: date
    prices: list[PricePoint]


class ForecastResponse(BaseModel):
    code: str
    model: str = Field(description="Metode yang dipakai: 'theta' (komoditas stabil) atau 'linear_autoregression'.")
    volatility: float = Field(description="Simpangan baku perubahan log harga harian (0.004 = 0,4%).")
    history_end: date = Field(description="Tanggal harga BI terakhir yang dipakai.")
    sample_size: int = Field(description="Jumlah hari kerja dalam riwayat komoditas ini.")
    trained_at: datetime = Field(description="Waktu model terakhir dilatih (UTC).")
    syncing: bool = Field(
        description="true bila sinkronisasi harian sedang berjalan; prediksi ini memakai model sebelumnya.",
    )
    predictions: list[PricePoint]


class SyncProgress(BaseModel):
    started_at: datetime
    done: int = Field(description="Jumlah komoditas yang sudah selesai disinkronkan.")
    total: int = Field(description="Jumlah komoditas yang disinkronkan.")
    current: Optional[str] = Field(description="Kode komoditas yang sedang diunduh.")
    backfill: bool = Field(description="true bila ini unduhan riwayat pertama (lama, ±35 menit).")


class ForecastUnavailable(BaseModel):
    """Body respons 503 /v1/forecast."""

    status: str = Field(
        description="'syncing' = data sedang diunduh/model sedang dilatih (normal, tunggu); "
                    "'unavailable' = sinkronisasi gagal, dicoba lagi pada next_sync_at.",
    )
    message: str
    phase: str
    progress: Optional[SyncProgress]
    next_sync_at: Optional[datetime]


class CommodityDataStatus(BaseModel):
    code: str
    data_from: date
    data_until: date
    points: int


class ServiceStatus(BaseModel):
    phase: str = Field(description="'starting', 'syncing', 'training', atau 'idle'.")
    ready: bool = Field(description="true bila /v1/forecast sudah bisa menjawab.")
    progress: Optional[SyncProgress]
    last_sync: Optional[datetime]
    next_sync_at: Optional[datetime]
    model_trained_at: Optional[datetime]
    failed_codes: list[str]
    commodities: list[CommodityDataStatus]
