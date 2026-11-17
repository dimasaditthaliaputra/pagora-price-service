from fastapi.testclient import TestClient

from app.main import app


def test_health_needs_no_api_key():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_requests_without_valid_api_key_are_rejected(client):
    assert client.get("/v1/commodities", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/v1/forecast", headers={"X-API-Key": ""}, json={"history": []}).status_code == 401


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


def test_forecast_endpoint(client):
    history = [{"date": f"2026-01-0{d}", "price": 100 + 10 * d} for d in range(1, 5)]

    response = client.post("/v1/forecast", json={"history": history, "horizon_days": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "linear_regression"
    assert body["sample_size"] == 4
    assert body["predictions"] == [{"date": "2026-01-05", "price": 150.0}, {"date": "2026-01-06", "price": 160.0}]


def test_forecast_rejects_insufficient_history_and_excessive_horizon(client):
    short = [{"date": "2026-01-01", "price": 100}]
    enough = [{"date": f"2026-01-0{d}", "price": 100} for d in range(1, 5)]

    assert client.post("/v1/forecast", json={"history": short}).status_code == 422
    assert client.post("/v1/forecast", json={"history": enough, "horizon_days": 91}).status_code == 422
