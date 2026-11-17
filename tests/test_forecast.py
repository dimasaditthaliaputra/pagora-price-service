from datetime import date, timedelta

import pytest

from app.forecast import ForecastError, linear_forecast
from app.schemas import PricePoint


def series(prices: list[float], start: date = date(2026, 1, 1), step_days: int = 1) -> list[PricePoint]:
    return [PricePoint(date=start + timedelta(days=i * step_days), price=p) for i, p in enumerate(prices)]


def test_perfect_linear_trend_is_recovered_exactly():
    result = linear_forecast(series([100, 110, 120, 130]), horizon_days=2, min_points=3)

    assert result.slope_per_day == 10
    assert result.intercept == 100
    assert result.r_squared == 1
    assert [p.price for p in result.predictions] == [140, 150]
    assert result.predictions[0].date == date(2026, 1, 5)


def test_calendar_gaps_are_respected_in_slope():
    history = [
        PricePoint(date=date(2026, 1, 2), price=100),  # Jumat
        PricePoint(date=date(2026, 1, 5), price=130),  # Senin
        PricePoint(date=date(2026, 1, 6), price=140),
    ]

    result = linear_forecast(history, horizon_days=1, min_points=3)

    assert result.slope_per_day == 10
    assert result.predictions[0].price == 150


def test_flat_prices_have_zero_slope_and_full_fit():
    result = linear_forecast(series([15000] * 5), horizon_days=3, min_points=3)

    assert result.slope_per_day == 0
    assert result.r_squared == 1
    assert {p.price for p in result.predictions} == {15000}


def test_unsorted_history_is_sorted_before_fitting():
    history = list(reversed(series([100, 110, 120])))

    result = linear_forecast(history, horizon_days=1, min_points=3)

    assert result.history_start == date(2026, 1, 1)
    assert result.predictions[0].price == 130


def test_noisy_series_reports_partial_r_squared():
    result = linear_forecast(series([100, 120, 105, 130, 115]), horizon_days=1, min_points=3)

    assert 0 < result.r_squared < 1


def test_too_few_points_is_rejected():
    with pytest.raises(ForecastError, match="minimal 3"):
        linear_forecast(series([100, 110]), horizon_days=1, min_points=3)


def test_duplicate_dates_are_rejected():
    history = series([100, 110, 120]) + [PricePoint(date=date(2026, 1, 1), price=105)]

    with pytest.raises(ForecastError, match="ganda"):
        linear_forecast(history, horizon_days=1, min_points=3)


def test_trend_reaching_non_positive_price_is_rejected():
    with pytest.raises(ForecastError, match="tidak wajar"):
        linear_forecast(series([30, 20, 10]), horizon_days=5, min_points=3)
