import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app import hybrid
from app.bi_client import BankIndonesiaClient, BankIndonesiaError
from app.price_store import PriceStore

logger = logging.getLogger(__name__)

CHUNK_DAYS = 365
OVERLAP_DAYS = 7
# Selama belum ada model (mis. BI down saat backfill pertama), jangan menunggu satu interval penuh.
NO_MODEL_RETRY_HOURS = 0.25

STARTING = "starting"
SYNCING = "syncing"
TRAINING = "training"
IDLE = "idle"


@dataclass
class Progress:
    started_at: datetime
    backfill: bool
    total: int = 0
    done: int = 0
    current: str | None = None


@dataclass
class ServiceState:
    """Riwayat harga lokal, model hibrida terakhir, dan fase sinkronisasi yang sedang berjalan."""

    store: PriceStore
    model: hybrid.TrainedModel | None = None
    phase: str = STARTING
    progress: Progress | None = None
    last_sync: datetime | None = None
    next_sync_at: datetime | None = None
    failed_codes: list[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return self.phase != IDLE


def date_chunks(start: date, end: date, size_days: int) -> list[tuple[date, date]]:
    chunks = []
    while start <= end:
        chunk_end = min(start + timedelta(days=size_days - 1), end)
        chunks.append((start, chunk_end))
        start = chunk_end + timedelta(days=1)
    return chunks


async def sync_prices(client: BankIndonesiaClient, store: PriceStore, today: date, history_days: int,
                      progress: Progress | None = None) -> list[str]:
    """
    Backfill `history_days` untuk komoditas baru, lalu inkremental sejak tanggal terakhir
    (mundur OVERLAP_DAYS untuk menangkap revisi BI). Request dikirim berurutan, satu per
    komoditas per potongan, tanpa retry: komoditas yang gagal dicoba lagi di sinkronisasi berikutnya.
    Mengembalikan kode komoditas yang gagal.
    """
    progress = progress or Progress(started_at=datetime.now(timezone.utc), backfill=False)
    commodities = await client.commodities()
    progress.total = len(commodities)

    failed = []
    for commodity in commodities:
        progress.current = commodity.code
        last = store.last_date(commodity.code)
        start = today - timedelta(days=history_days) if last is None else last - timedelta(days=OVERLAP_DAYS)
        try:
            for chunk_start, chunk_end in date_chunks(start, today, CHUNK_DAYS):
                points = await client.national_daily_prices(commodity.code, chunk_start, chunk_end)
                store.merge(commodity.code, points)
        except BankIndonesiaError as exception:
            logger.warning("Sinkronisasi %s gagal: %s", commodity.code, exception)
            failed.append(commodity.code)
        store.save()
        progress.done += 1

    progress.current = None
    store.trim(today - timedelta(days=history_days))
    store.save()
    return failed


async def retrain(state: ServiceState, max_horizon_days: int) -> None:
    state.phase = TRAINING
    snapshot = state.store.snapshot()
    try:
        state.model = await asyncio.to_thread(hybrid.train, snapshot, hybrid.max_steps(max_horizon_days))
    except hybrid.ForecastError as exception:
        logger.warning("Model belum dapat dilatih: %s", exception)


async def maintain(state: ServiceState, client: BankIndonesiaClient, history_days: int,
                   max_horizon_days: int, interval_hours: float, sync_enabled: bool) -> None:
    """Loop latar: latih dari data yang sudah ada, lalu sinkronkan dan latih ulang tiap interval."""
    await retrain(state, max_horizon_days)
    while sync_enabled:
        now = datetime.now(timezone.utc)
        state.phase = SYNCING
        state.progress = Progress(started_at=now, backfill=not state.store.codes())
        try:
            state.failed_codes = await sync_prices(client, state.store, date.today(), history_days, state.progress)
            state.last_sync = datetime.now(timezone.utc)
        except BankIndonesiaError as exception:
            logger.warning("Sinkronisasi BI gagal: %s", exception)
        except Exception:
            # Loop latar tidak boleh mati diam-diam; coba lagi di interval berikutnya.
            logger.exception("Sinkronisasi gagal karena error tak terduga")
        await retrain(state, max_horizon_days)
        wait_hours = interval_hours if state.model else min(interval_hours, NO_MODEL_RETRY_HOURS)
        state.progress = None
        state.next_sync_at = datetime.now(timezone.utc) + timedelta(hours=wait_hours)
        state.phase = IDLE
        logger.info("Sinkronisasi selesai; berikutnya %s", state.next_sync_at.isoformat())
        await asyncio.sleep(wait_hours * 3600)
    state.phase = IDLE
