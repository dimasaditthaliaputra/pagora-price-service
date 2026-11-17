"""
Model hibrida, dipilih dari backtest walk-forward 2 tahun data PIHPS (21 komoditas):

- Komoditas stabil (volatilitas harian <= 0,3%): metode Theta, yaitu SES ditambah
  setengah slope regresi linier 60 hari kalender terakhir (Hyndman & Billah, 2003).
- Komoditas fluktuatif (>= 0,5%): regresi linier autoregresif (ridge) pada log harga.
  Satu model per horizon hari kerja, dilatih gabungan dari semua komoditas.
- Di antaranya: campuran keduanya, bobot regresi naik linier dari 0 ke 1. Ini mencegah
  komoditas di sekitar ambang (mis. telur) berganti metode secara mendadak.

Prediksi dibuat per hari kerja (Senin-Jumat), lalu Sabtu/Minggu memakai nilai hari
kerja sebelumnya, sehingga respons tetap berisi satu harga per hari kalender.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np

from app.schemas import PricePoint

# Rentang campuran ditetapkan ±0,1% di sekitar ambang 0,4% (dari data tahun pertama), bukan dicari dari data uji.
BLEND_LOW = 0.003
BLEND_HIGH = 0.005
VOLATILITY_WINDOW = 250
FEATURE_LOOKBACK = 60
MIN_HISTORY = FEATURE_LOOKBACK
SLOPE_WINDOW_DAYS = 60
SLOPE_MIN_POINTS = 10
SES_WINDOW = 120
SES_ALPHAS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0)
RIDGE_LAMBDA = 10.0

class ForecastError(ValueError):
    """Riwayat tidak memenuhi syarat untuk dihitung (UC-11 alternatif 3a)."""


THETA = "theta"
AUTOREGRESSION = "linear_autoregression"
COMBINATION = "combination"

Series = tuple[list[date], np.ndarray]


@dataclass(frozen=True)
class RidgeModel:
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray

    def predict(self, features: np.ndarray) -> float:
        return float(self.coef[0] + ((features - self.mean) / self.scale) @ self.coef[1:])


@dataclass(frozen=True)
class TrainedModel:
    horizons: list[RidgeModel]  # indeks 0 = 1 hari kerja ke depan
    trained_at: datetime
    commodity_count: int
    sample_size: int


@dataclass(frozen=True)
class HybridForecast:
    method: str
    volatility: float
    predictions: list[PricePoint]


def max_steps(max_horizon_days: int) -> int:
    """Batas atas jumlah hari kerja dalam `max_horizon_days` hari kalender."""
    return max_horizon_days * 5 // 7 + 2


def daily_volatility(prices: np.ndarray) -> float:
    """Simpangan baku perubahan log harga harian pada jendela terakhir."""
    returns = np.diff(np.log(prices[-(VOLATILITY_WINDOW + 1):]))
    return float(returns.std()) if len(returns) else 0.0


def feature_matrix(log_prices: np.ndarray) -> np.ndarray:
    """
    Baris j = fitur pada indeks t = FEATURE_LOOKBACK - 1 + j:
    perubahan log 1, 5, 20 hari kerja dan jarak dari rata-rata 20 & 60 hari kerja.
    """
    t = np.arange(FEATURE_LOOKBACK - 1, len(log_prices))
    cumulative = np.concatenate([[0.0], np.cumsum(log_prices)])
    ma20 = (cumulative[t + 1] - cumulative[t - 19]) / 20
    ma60 = (cumulative[t + 1] - cumulative[t - 59]) / 60
    current = log_prices[t]
    return np.column_stack([
        current - log_prices[t - 1],
        current - log_prices[t - 5],
        current - log_prices[t - 20],
        current - ma20,
        current - ma60,
    ])


def fit_ridge(features: np.ndarray, targets: np.ndarray, penalty: float) -> RidgeModel:
    """Ridge pada fitur terstandar; intercept tidak dipenalti."""
    mean, scale = features.mean(axis=0), features.std(axis=0) + 1e-12
    design = np.hstack([np.ones((len(features), 1)), (features - mean) / scale])
    regularizer = penalty * np.eye(design.shape[1])
    regularizer[0, 0] = 0
    coef = np.linalg.solve(design.T @ design + regularizer, design.T @ targets)
    return RidgeModel(mean=mean, scale=scale, coef=coef)


def train(series: dict[str, Series], steps: int) -> TrainedModel:
    """Latih satu regresi per horizon 1..steps hari kerja dari semua komoditas yang cukup panjang."""
    usable = [np.log(prices) for _, prices in series.values() if len(prices) > MIN_HISTORY]
    if not usable:
        raise ForecastError(f"Belum ada komoditas dengan riwayat lebih dari {MIN_HISTORY} hari kerja.")

    matrices = [feature_matrix(log_prices) for log_prices in usable]
    horizons = []
    for k in range(1, steps + 1):
        xs, ys = [], []
        for log_prices, features in zip(usable, matrices):
            n = len(log_prices)
            if n - FEATURE_LOOKBACK + 1 - k <= 0:
                continue
            xs.append(features[:n - FEATURE_LOOKBACK + 1 - k])
            ys.append(log_prices[FEATURE_LOOKBACK - 1 + k:] - log_prices[FEATURE_LOOKBACK - 1:n - k])
        if not xs:
            raise ForecastError(f"Riwayat terlalu pendek untuk melatih horizon {k} hari kerja.")
        horizons.append(fit_ridge(np.vstack(xs), np.concatenate(ys), RIDGE_LAMBDA))

    return TrainedModel(
        horizons=horizons,
        trained_at=datetime.now(timezone.utc),
        commodity_count=len(usable),
        sample_size=sum(len(features) for features in matrices),
    )


def ses_level(prices: np.ndarray) -> float:
    """Level akhir simple exponential smoothing, alpha dipilih dari SSE 1-langkah terkecil."""
    best_sse, best_level = None, prices[0]
    for alpha in SES_ALPHAS:
        level, sse = prices[0], 0.0
        for price in prices[1:]:
            sse += (price - level) ** 2
            level = alpha * price + (1 - alpha) * level
        if best_sse is None or sse < best_sse:
            best_sse, best_level = sse, level
    return float(best_level)


def recent_slope(dates: list[date], prices: np.ndarray) -> float:
    """Slope OLS (per hari kalender) pada SLOPE_WINDOW_DAYS hari terakhir, minimal SLOPE_MIN_POINTS titik."""
    days = np.array([(d - dates[0]).days for d in dates], dtype=float)
    start = int(np.searchsorted(days, days[-1] - SLOPE_WINDOW_DAYS + 1))
    start = min(start, len(days) - SLOPE_MIN_POINTS)
    x, y = days[start:], prices[start:]
    spread = np.sum((x - x.mean()) ** 2)
    return 0.0 if spread == 0 else float(np.sum((x - x.mean()) * (y - y.mean())) / spread)


def autoregression_weight(volatility: float) -> float:
    """Bobot regresi autoregresif: 0 di bawah BLEND_LOW, 1 di atas BLEND_HIGH, linier di antaranya."""
    if BLEND_HIGH == BLEND_LOW:
        return 1.0 if volatility >= BLEND_HIGH else 0.0
    return float(np.clip((volatility - BLEND_LOW) / (BLEND_HIGH - BLEND_LOW), 0.0, 1.0))


def theta_path(dates: list[date], prices: np.ndarray, business_days: list[date]) -> tuple[float, np.ndarray]:
    level = ses_level(prices[-SES_WINDOW:])
    slope = recent_slope(dates, prices)
    return level, np.array([level + 0.5 * slope * (d - dates[-1]).days for d in business_days])


def autoregression_path(model: TrainedModel, prices: np.ndarray, steps: int) -> tuple[float, np.ndarray]:
    features = feature_matrix(np.log(prices))[-1]
    last = float(prices[-1])
    return last, np.array([last * np.exp(m.predict(features)) for m in model.horizons[:steps]])


def hybrid_forecast(model: TrainedModel, dates: list[date], prices: np.ndarray, horizon_days: int) -> HybridForecast:
    if len(prices) < MIN_HISTORY:
        raise ForecastError(f"Riwayat minimal {MIN_HISTORY} hari kerja, tersedia {len(prices)}.")

    last = dates[-1]
    calendar = [last + timedelta(days=offset) for offset in range(1, horizon_days + 1)]
    business_days = [d for d in calendar if d.weekday() < 5]
    if len(business_days) > len(model.horizons):
        raise ForecastError(f"Model hanya dilatih sampai {len(model.horizons)} hari kerja ke depan.")

    volatility = daily_volatility(prices)
    weight = autoregression_weight(volatility)
    base, steps = 0.0, np.zeros(len(business_days))
    if weight < 1:
        theta_base, theta_steps = theta_path(dates, prices, business_days)
        base, steps = base + (1 - weight) * theta_base, steps + (1 - weight) * theta_steps
    if weight > 0:
        ar_base, ar_steps = autoregression_path(model, prices, len(business_days))
        base, steps = base + weight * ar_base, steps + weight * ar_steps
    method = THETA if weight == 0 else AUTOREGRESSION if weight == 1 else COMBINATION

    if any(price <= 0 for price in steps):
        raise ForecastError("Tren menghasilkan harga tidak wajar (≤ 0).")

    predictions, current, step = [], base, iter(steps)
    for d in calendar:
        if d.weekday() < 5:
            current = next(step)
        predictions.append(PricePoint(date=d, price=round(float(current), 2)))

    return HybridForecast(method=method, volatility=round(volatility, 6), predictions=predictions)
