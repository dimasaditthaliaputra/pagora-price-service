from datetime import date

import httpx
import pytest

from app.bi_client import BankIndonesiaError, parse_daily_row
from tests.conftest import load_fixture, make_bi_client


def test_parse_daily_row_skips_missing_values_and_metadata():
    row = load_fixture("grid_komoditas.json")["data"][0]

    points = parse_daily_row(row)

    assert [p.date for p in points] == [
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8),
    ]
    assert points[0].price == 14900


async def test_national_prices_use_level_zero_row_and_send_expected_params():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("grid_komoditas.json"))

    client = make_bi_client(handler)
    points = await client.national_daily_prices("com_1", date(2026, 10, 1), date(2026, 10, 8))

    assert len(points) == 5
    assert all(p.price == 14900 for p in points)
    params = seen[0].url.params
    assert params["comcat_id"] == "com_1"
    assert params["tipe_laporan"] == "1"
    assert params["start_date"] == "2026-10-01"
    assert seen[0].headers["X-Requested-With"] == "XMLHttpRequest"


async def test_commodities_keep_only_items_and_resolve_category():
    client = make_bi_client(lambda request: httpx.Response(200, json=load_fixture("commodity_tree.json")))

    commodities = await client.commodities()

    assert [c.code for c in commodities] == ["com_1", "com_14", "com_16"]
    assert commodities[0].category == "Beras"
    assert commodities[1].name == "Cabai Merah Keriting"
    assert commodities[1].category is None


async def test_commodities_are_cached():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=load_fixture("commodity_tree.json"))

    client = make_bi_client(handler)
    await client.commodities()
    await client.commodities()

    assert len(calls) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(503),
    httpx.Response(200, text="<html>maintenance</html>"),
    httpx.Response(200, json={"unexpected": True}),
])
async def test_unavailable_or_changed_api_raises_domain_error(response):
    client = make_bi_client(lambda request: response)

    with pytest.raises(BankIndonesiaError):
        await client.national_daily_prices("com_1", date(2026, 10, 1), date(2026, 10, 8))


async def test_missing_national_row_returns_empty_series():
    client = make_bi_client(lambda request: httpx.Response(200, json={"data": [{"level": 1, "01/10/2026": "1"}]}))

    assert await client.national_daily_prices("com_1", date(2026, 10, 1), date(2026, 10, 1)) == []
