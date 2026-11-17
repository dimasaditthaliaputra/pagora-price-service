from datetime import date, timedelta

import numpy as np
import pytest

from app import hybrid
from app.hybrid import ForecastError


def business_series(prices, start: date = date(2025, 1, 6)) -> tuple[list[date], np.ndarray]:
    """Harga pada hari kerja berturut-turut mulai Senin `start`."""
    days, current = [], start
    while len(days) < len(prices):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days, np.array(prices, dtype=float)


def random_walk(n: int, volatility: float, seed: int) -> np.ndarray:
    steps = np.random.default_rng(seed).normal(0, volatility, n)
    return 20_000 * np.exp(np.cumsum(steps))


@pytest.fixture
def model() -> hybrid.TrainedModel:
    series = {f"com_{i}": business_series(random_walk(300, 0.02, seed=i)) for i in range(1, 4)}
    return hybrid.train(series, hybrid.max_steps(90))


def test_feature_matrix_matches_definition():
    log_prices = np.log(random_walk(80, 0.01, seed=1))
    t = len(log_prices) - 1

    last_row = hybrid.feature_matrix(log_prices)[-1]

    expected = [
        log_prices[t] - log_prices[t - 1],
        log_prices[t] - log_prices[t - 5],
        log_prices[t] - log_prices[t - 20],
        log_prices[t] - log_prices[t - 19:t + 1].mean(),
        log_prices[t] - log_prices[t - 59:t + 1].mean(),
    ]
    assert len(hybrid.feature_matrix(log_prices)) == len(log_prices) - hybrid.FEATURE_LOOKBACK + 1
    assert np.allclose(last_row, expected)


def test_ridge_without_penalty_recovers_linear_relation():
    features = np.random.default_rng(0).normal(size=(200, 5))
    targets = 0.01 + features @ np.array([0.5, -0.2, 0.0, 0.1, 0.3])

    ridge = hybrid.fit_ridge(features, targets, penalty=1e-9)

    assert ridge.predict(features[0]) == pytest.approx(targets[0])


def test_stable_trend_uses_theta_with_half_slope(model):
    dates, _ = business_series([0] * 118)  # berakhir Kamis, jadi prediksi pertama = Jumat
    prices = np.array([10_000 + (d - dates[0]).days for d in dates], dtype=float)  # +1 per hari kalender

    result = hybrid.hybrid_forecast(model, dates, prices, horizon_days=3)

    assert result.method == hybrid.THETA
    assert result.volatility < hybrid.BLEND_LOW
    first = result.predictions[0]
    assert first.price == pytest.approx(prices[-1] + 0.5 * (first.date - dates[-1]).days)


def test_volatile_series_uses_autoregression(model):
    dates, prices = business_series(random_walk(120, 0.02, seed=9))

    result = hybrid.hybrid_forecast(model, dates, prices, horizon_days=30)

    assert result.method == hybrid.AUTOREGRESSION
    assert len(result.predictions) == 30
    assert all(p.price > 0 for p in result.predictions)


def test_autoregression_weight_rises_linearly_between_thresholds():
    assert hybrid.autoregression_weight(0.002) == 0
    assert hybrid.autoregression_weight(0.004) == pytest.approx(0.5)
    assert hybrid.autoregression_weight(0.006) == 1


def test_volatility_near_threshold_blends_both_methods(model, monkeypatch):
    dates, prices = business_series(random_walk(120, 0.02, seed=7))

    def forecast_at(volatility):
        monkeypatch.setattr(hybrid, "daily_volatility", lambda _: volatility)
        return hybrid.hybrid_forecast(model, dates, prices, horizon_days=10)

    theta, autoregression, blend = forecast_at(0.001), forecast_at(0.01), forecast_at(0.004)

    assert (theta.method, autoregression.method, blend.method) == (hybrid.THETA, hybrid.AUTOREGRESSION, hybrid.COMBINATION)
    for t, a, b in zip(theta.predictions, autoregression.predictions, blend.predictions):
        assert b.price == pytest.approx((t.price + a.price) / 2, abs=0.01)


def test_weekends_repeat_previous_business_day(model):
    dates, prices = business_series(random_walk(123, 0.02, seed=4))
    assert dates[-1].weekday() == 2  # Rabu

    predictions = hybrid.hybrid_forecast(model, dates, prices, horizon_days=7).predictions

    friday, saturday, sunday = predictions[1], predictions[2], predictions[3]
    assert saturday.date.weekday() == 5
    assert saturday.price == friday.price == sunday.price


def test_weekend_right_after_last_price_keeps_last_price(model):
    dates, prices = business_series(random_walk(125, 0.02, seed=5))
    assert dates[-1].weekday() == 4  # Jumat

    predictions = hybrid.hybrid_forecast(model, dates, prices, horizon_days=2).predictions

    assert [p.price for p in predictions] == [round(prices[-1], 2)] * 2


def test_short_history_is_rejected(model):
    dates, prices = business_series(random_walk(hybrid.MIN_HISTORY - 1, 0.02, seed=1))

    with pytest.raises(ForecastError, match="minimal"):
        hybrid.hybrid_forecast(model, dates, prices, horizon_days=5)


def test_training_requires_long_enough_series():
    with pytest.raises(ForecastError, match="Belum ada komoditas"):
        hybrid.train({"com_1": business_series(random_walk(30, 0.02, seed=1))}, steps=5)
