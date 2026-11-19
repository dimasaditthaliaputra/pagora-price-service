from datetime import date, datetime, timezone

import httpx

import pytest
from fastapi.testclient import TestClient

from app import hybrid
from app.main import app, bi_client, service_state
from app.price_store import PriceStore
from app.schemas import PricePoint
from app.sync import IDLE, SYNCING, Progress, ServiceState
from tests.conftest import make_bi_client
from tests.test_hybrid import business_series, random_walk


def test_health_needs_no_api_key():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_requests_without_valid_api_key_are_rejected(client):
    assert client.get("/v1/commodities", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/v1/forecast", headers={"X-API-Key": ""}, params={"code": "com_1"}).status_code == 401


def test_commodities_endpoint(client):
    response = client.get("/v1/commodities")

    assert response.status_code == 200
    assert response.json()[0] == {"code": "com_1", "name": "Beras Kualitas Bawah I", "category": "Beras", "unit": "kg"}


def test_prices_endpoint_returns_national_series(client):
    response = client.get("/v1/prices", params={"code": "com_1", "start_date": "2026-10-01", "end_date": "2026-10-08"})

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "bank_indonesia"
    assert body["region"] == "national"
    assert body["prices"][0] == {"date": "2026-10-01", "price": 14900.0}
    assert len(body["prices"]) == 5


def test_prices_endpoint_validates_input_before_calling_bi(client, bi_requests):
    invalid = [
        {"code": "cat_1", "start_date": "2026-10-01", "end_date": "2026-10-08"},
        {"code": "com_1", "start_date": "2026-10-08", "end_date": "2026-10-01"},
        {"code": "com_1", "start_date": "2024-01-01", "end_date": "2026-10-01"},
    ]

    for params in invalid:
        assert client.get("/v1/prices", params=params).status_code == 422

    assert bi_requests == []


def seed_local(store: PriceStore, code: str, prices: dict[str, float]) -> None:
    store.merge(code, [PricePoint(date=date.fromisoformat(day), price=price) for day, price in prices.items()])


def test_prices_come_from_local_history_without_calling_bi(client, bi_requests, local_store):
    seed_local(local_store, "com_1", {"2026-09-30": 14800, "2026-10-01": 15000, "2026-10-08": 15100, "2026-10-09": 15200})

    response = client.get("/v1/prices", params={"code": "com_1", "start_date": "2026-10-01", "end_date": "2026-10-08"})

    assert response.status_code == 200
    assert response.json()["prices"] == [{"date": "2026-10-01", "price": 15000.0}, {"date": "2026-10-08", "price": 15100.0}]
    assert bi_requests == []


def test_only_days_after_local_history_are_requested_from_bi(client, bi_requests, local_store):
    seed_local(local_store, "com_1", {"2026-09-01": 14000, "2026-10-01": 15000, "2026-10-02": 15050})

    response = client.get("/v1/prices", params={"code": "com_1", "start_date": "2026-10-01", "end_date": "2026-10-08"})

    prices = response.json()["prices"]
    assert prices[:2] == [{"date": "2026-10-01", "price": 15000.0}, {"date": "2026-10-02", "price": 15050.0}]
    assert [p["date"] for p in prices[2:]] == ["2026-10-06", "2026-10-07", "2026-10-08"]
    assert len(bi_requests) == 1
    assert bi_requests[0].url.params["start_date"] == "2026-10-03"
    assert bi_requests[0].url.params["end_date"] == "2026-10-08"


def test_local_history_is_returned_when_bi_fails_for_the_newest_days(client, local_store):
    seed_local(local_store, "com_1", {"2026-09-01": 14000, "2026-10-01": 15000})
    app.dependency_overrides[bi_client] = lambda: make_bi_client(lambda request: httpx.Response(503))

    response = client.get("/v1/prices", params={"code": "com_1", "start_date": "2026-10-01", "end_date": "2026-10-08"})

    assert response.status_code == 200
    assert response.json()["prices"] == [{"date": "2026-10-01", "price": 15000.0}]


def test_range_older_than_local_history_is_requested_from_bi_in_full(client, bi_requests, local_store):
    seed_local(local_store, "com_1", {"2026-10-06": 15000})

    response = client.get("/v1/prices", params={"code": "com_1", "start_date": "2026-10-01", "end_date": "2026-10-08"})

    assert response.status_code == 200
    assert bi_requests[0].url.params["start_date"] == "2026-10-01"
    assert response.json()["prices"][0] == {"date": "2026-10-01", "price": 14900.0}


def test_bi_failure_without_local_history_is_a_502(client, local_store):
    app.dependency_overrides[bi_client] = lambda: make_bi_client(lambda request: httpx.Response(503))

    response = client.get("/v1/prices", params={"code": "com_1", "start_date": "2026-10-01", "end_date": "2026-10-08"})

    assert response.status_code == 502


@pytest.fixture
def state(tmp_path) -> ServiceState:
    store = PriceStore(tmp_path / "prices.json")
    for code, seed in (("com_1", 1), ("com_13", 2)):
        dates, prices = business_series(random_walk(150, 0.02, seed=seed))
        store.merge(code, [PricePoint(date=d, price=p) for d, p in zip(dates, prices)])
    return ServiceState(store=store)


@pytest.fixture
def trained(state) -> ServiceState:
    state.model = hybrid.train(state.store.snapshot(), hybrid.max_steps(90))
    state.phase = IDLE
    return state


@pytest.fixture
def forecast_client(client, state):
    app.dependency_overrides[service_state] = lambda: state
    return client


def test_forecast_reports_sync_progress_while_model_is_not_ready(forecast_client, state):
    state.phase = SYNCING
    state.progress = Progress(started_at=datetime.now(timezone.utc), backfill=True, total=21, done=5, current="com_6")

    response = forecast_client.get("/v1/forecast", params={"code": "com_1"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    body = response.json()
    assert body["status"] == "syncing"
    assert "±35 menit" in body["message"]
    assert body["progress"]["done"] == 5
    assert body["progress"]["total"] == 21


def test_forecast_reports_unavailable_when_sync_failed(forecast_client, state):
    state.phase = IDLE

    response = forecast_client.get("/v1/forecast", params={"code": "com_1"})

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_forecast_returns_hybrid_prediction(forecast_client, trained):
    response = forecast_client.get("/v1/forecast", params={"code": "com_13", "horizon_days": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "com_13"
    assert body["model"] == hybrid.AUTOREGRESSION
    assert body["sample_size"] == 150
    assert body["syncing"] is False
    assert len(body["predictions"]) == 10


def test_forecast_keeps_serving_previous_model_during_daily_sync(forecast_client, trained):
    trained.phase = SYNCING

    response = forecast_client.get("/v1/forecast", params={"code": "com_1"})

    assert response.status_code == 200
    assert response.json()["syncing"] is True


def test_forecast_validates_input(forecast_client, trained):
    assert forecast_client.get("/v1/forecast", params={"code": "cat_1"}).status_code == 422
    assert forecast_client.get("/v1/forecast", params={"code": "com_99"}).status_code == 422
    assert forecast_client.get("/v1/forecast", params={"code": "com_1", "horizon_days": 91}).status_code == 422


def test_status_reports_phase_and_local_data(forecast_client, state):
    body = forecast_client.get("/v1/status").json()

    assert body["phase"] == "starting"
    assert body["ready"] is False
    assert [c["code"] for c in body["commodities"]] == ["com_1", "com_13"]
    assert body["commodities"][0]["points"] == 150
