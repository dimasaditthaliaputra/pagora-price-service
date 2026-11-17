import re
import time
from datetime import date, datetime

import httpx

from app.schemas import Commodity, PricePoint

COMMODITY_CODE_PATTERN = re.compile(r"^com_\d+$")
NATIONAL_ROW_LEVEL = 0


class BankIndonesiaError(RuntimeError):
    """API PIHPS tidak dapat diakses atau formatnya berubah."""


class BankIndonesiaClient:
    """
    Klien endpoint JSON internal PIHPS (Tabel Harga per Komoditas).
    Endpoint ini tidak terdokumentasi resmi, jadi semua parsing dijaga ketat
    dan kegagalan dilempar sebagai BankIndonesiaError agar Laravel fallback.
    """

    def __init__(self, base_url: str, timeout: float, price_type_id: int, cache_seconds: int,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._price_type_id = price_type_id
        self._cache_seconds = cache_seconds
        self._commodity_cache: tuple[float, list[Commodity]] | None = None
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/WebSite/TabelHarga",
            timeout=timeout,
            transport=transport,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PAGORA-PriceService/1.0)",
                "Referer": f"{base_url.rstrip('/')}/TabelHarga/PasarTradisionalKomoditas",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def commodities(self) -> list[Commodity]:
        if self._commodity_cache and time.monotonic() - self._commodity_cache[0] < self._cache_seconds:
            return self._commodity_cache[1]

        rows = await self._get("GetRefCommodityAndCategory", {})
        categories = {row["id"]: row["name"].strip() for row in rows if row.get("cat_id") is None}
        commodities = [
            Commodity(
                code=row["id"],
                name=row["name"].strip(),
                category=categories.get(row["cat_id"]),
                unit=row.get("denomination") or "kg",
            )
            for row in rows
            if COMMODITY_CODE_PATTERN.match(str(row.get("id", "")))
        ]

        self._commodity_cache = (time.monotonic(), commodities)
        return commodities

    async def national_daily_prices(self, code: str, start_date: date, end_date: date) -> list[PricePoint]:
        rows = await self._get("GetGridDataKomoditas", {
            "price_type_id": self._price_type_id,
            "comcat_id": code,
            "province_id": "",
            "regency_id": "",
            "showKota": "false",
            "showPasar": "false",
            "tipe_laporan": 1,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        })

        national = next((row for row in rows if row.get("level") == NATIONAL_ROW_LEVEL), None)
        if national is None:
            return []

        return parse_daily_row(national)

    async def _get(self, endpoint: str, params: dict) -> list[dict]:
        try:
            response = await self._http.get(endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exception:
            raise BankIndonesiaError(f"Gagal mengakses PIHPS {endpoint}: {exception}") from exception

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise BankIndonesiaError(f"Format respons PIHPS {endpoint} tidak dikenali.")

        return payload["data"]


def parse_daily_row(row: dict) -> list[PricePoint]:
    """Kolom `dd/mm/yyyy` → harga `"14,900"`; `"-"` berarti tidak ada data."""
    points = []
    for key, value in row.items():
        try:
            day = datetime.strptime(key, "%d/%m/%Y").date()
        except ValueError:
            continue

        cleaned = str(value).replace(",", "").strip()
        if not cleaned.replace(".", "", 1).isdigit():
            continue

        price = float(cleaned)
        if price > 0:
            points.append(PricePoint(date=day, price=price))

    return sorted(points, key=lambda point: point.date)
