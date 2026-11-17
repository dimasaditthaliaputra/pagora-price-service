from datetime import timedelta

import numpy as np

from app.schemas import ForecastResponse, PricePoint


class ForecastError(ValueError):
    """Riwayat tidak memenuhi syarat untuk dihitung (UC-11 alternatif 3a)."""


def linear_forecast(history: list[PricePoint], horizon_days: int, min_points: int) -> ForecastResponse:
    """
    Regresi linier OLS: harga = intercept + slope * x, dengan x = jumlah hari
    sejak tanggal riwayat pertama. Memakai hari kalender sehingga celah akhir
    pekan / libur (BI hanya mencatat hari kerja) tidak mendistorsi kemiringan.
    """
    points = sorted(history, key=lambda point: point.date)

    if len(points) < min_points:
        raise ForecastError(f"Riwayat minimal {min_points} titik, diterima {len(points)}.")

    if len({point.date for point in points}) != len(points):
        raise ForecastError("Riwayat berisi tanggal ganda.")

    origin = points[0].date
    x = np.array([(point.date - origin).days for point in points], dtype=float)
    y = np.array([point.price for point in points], dtype=float)

    x_mean, y_mean = x.mean(), y.mean()
    slope = float(np.sum((x - x_mean) * (y - y_mean)) / np.sum((x - x_mean) ** 2))
    intercept = float(y_mean - slope * x_mean)

    ss_res = float(np.sum((y - (intercept + slope * x)) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    r_squared = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot

    last = points[-1].date
    predictions = []
    for offset in range(1, horizon_days + 1):
        target = last + timedelta(days=offset)
        price = intercept + slope * (target - origin).days
        if price <= 0:
            raise ForecastError("Tren menghasilkan harga tidak wajar (≤ 0).")
        predictions.append(PricePoint(date=target, price=round(price, 2)))

    return ForecastResponse(
        sample_size=len(points),
        history_start=origin,
        history_end=last,
        slope_per_day=round(slope, 4),
        intercept=round(intercept, 4),
        r_squared=round(r_squared, 4),
        predictions=predictions,
    )
