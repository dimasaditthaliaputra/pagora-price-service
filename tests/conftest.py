import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.bi_client import BankIndonesiaClient
from app.config import Settings, get_settings
from app.main import app, bi_client

API_KEY = "test-key-0123456789abcdef0123456789abcdef"
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def make_bi_client(handler) -> BankIndonesiaClient:
    return BankIndonesiaClient(
        base_url="https://bi.test/hargapangan",
        timeout=5,
        price_type_id=1,
        cache_seconds=60,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(api_key=API_KEY, max_range_days=400, max_horizon_days=90)


@pytest.fixture
def bi_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def client(settings, bi_requests):
    def handler(request: httpx.Request) -> httpx.Response:
        bi_requests.append(request)
        if request.url.path.endswith("GetRefCommodityAndCategory"):
            return httpx.Response(200, json=load_fixture("commodity_tree.json"))
        if request.url.path.endswith("GetGridDataKomoditas"):
            return httpx.Response(200, json=load_fixture("grid_komoditas.json"))
        return httpx.Response(404)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[bi_client] = lambda: make_bi_client(handler)
    yield TestClient(app, headers={"X-API-Key": API_KEY})
    app.dependency_overrides.clear()
