import json
import os
from datetime import date
from pathlib import Path

import numpy as np

from app.hybrid import Series
from app.schemas import PricePoint


class PriceStore:
    """Riwayat harga nasional harian per komoditas, disimpan sebagai satu file JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._prices: dict[str, dict[date, float]] = {}

    def load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        self._prices = {
            code: {date.fromisoformat(day): float(price) for day, price in prices.items()}
            for code, prices in raw.items()
        }

    def save(self) -> None:
        """Tulis ke file sementara lalu rename, agar file tidak rusak bila proses mati di tengah."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            code: {day.isoformat(): price for day, price in sorted(prices.items())}
            for code, prices in self._prices.items()
        }
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(raw))
        os.replace(temporary, self._path)

    def codes(self) -> list[str]:
        return sorted(self._prices)

    def first_date(self, code: str) -> date | None:
        prices = self._prices.get(code)
        return min(prices) if prices else None

    def last_date(self, code: str) -> date | None:
        prices = self._prices.get(code)
        return max(prices) if prices else None

    def points(self, code: str, start: date, end: date) -> list[PricePoint]:
        prices = self._prices.get(code, {})
        return [PricePoint(date=day, price=prices[day]) for day in sorted(prices) if start <= day <= end]

    def merge(self, code: str, points: list[PricePoint]) -> None:
        self._prices.setdefault(code, {}).update({point.date: point.price for point in points})

    def trim(self, before: date) -> None:
        for prices in self._prices.values():
            for day in [day for day in prices if day < before]:
                del prices[day]

    def series(self, code: str) -> Series | None:
        prices = self._prices.get(code)
        if not prices:
            return None
        days = sorted(prices)
        return days, np.array([prices[day] for day in days], dtype=float)

    def snapshot(self) -> dict[str, Series]:
        return {code: self.series(code) for code in self.codes() if self._prices[code]}
