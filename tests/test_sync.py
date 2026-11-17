import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx

from app.price_store import PriceStore
from app.schemas import PricePoint
from app.sync import IDLE, ServiceState, date_chunks, maintain, sync_prices
from tests.conftest import load_fixture, make_bi_client

TODAY = date(2026, 10, 8)


def recording_client(grid_requests: list[httpx.Request], failing_code: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetRefCommodityAndCategory"):
            return httpx.Response(200, json=load_fixture("commodity_tree.json"))
        grid_requests.append(request)
        if request.url.params["comcat_id"] == failing_code:
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("grid_komoditas.json"))

    return make_bi_client(handler)


def requested_ranges(requests: list[httpx.Request], code: str) -> list[tuple[str, str]]:
    return [(r.url.params["start_date"], r.url.params["end_date"]) for r in requests if r.url.params["comcat_id"] == code]


def test_date_chunks_cover_range_without_overlap():
    assert date_chunks(date(2026, 1, 1), date(2026, 1, 25), 10) == [
        (date(2026, 1, 1), date(2026, 1, 10)),
        (date(2026, 1, 11), date(2026, 1, 20)),
        (date(2026, 1, 21), date(2026, 1, 25)),
    ]


async def test_new_commodities_are_backfilled_in_yearly_chunks(tmp_path):
    requests: list[httpx.Request] = []
    store = PriceStore(tmp_path / "prices.json")

    failed = await sync_prices(recording_client(requests), store, TODAY, history_days=400)

    assert failed == []
    assert requested_ranges(requests, "com_1") == [("2025-09-03", "2026-09-02"), ("2026-09-03", "2026-10-08")]
    assert store.codes() == ["com_1", "com_14", "com_16"]
    assert (tmp_path / "prices.json").exists()


async def test_known_commodities_sync_incrementally_with_overlap(tmp_path):
    requests: list[httpx.Request] = []
    store = PriceStore(tmp_path / "prices.json")
    store.merge("com_1", [PricePoint(date=date(2026, 10, 6), price=14_000)])

    await sync_prices(recording_client(requests), store, TODAY, history_days=400)

    assert requested_ranges(requests, "com_1") == [("2026-09-29", "2026-10-08")]


async def test_failed_commodity_does_not_stop_the_others(tmp_path):
    requests: list[httpx.Request] = []
    store = PriceStore(tmp_path / "prices.json")

    failed = await sync_prices(recording_client(requests, failing_code="com_14"), store, TODAY, history_days=30)

    assert failed == ["com_14"]
    assert store.codes() == ["com_1", "com_16"]
    assert len(requested_ranges(requests, "com_14")) == 1


async def test_without_model_next_sync_is_retried_soon(tmp_path):
    client = make_bi_client(lambda request: httpx.Response(503))
    state = ServiceState(store=PriceStore(tmp_path / "prices.json"))

    task = asyncio.create_task(maintain(state, client, history_days=30, max_horizon_days=30,
                                        interval_hours=24, sync_enabled=True))
    while state.next_sync_at is None:
        await asyncio.sleep(0.01)
    task.cancel()

    assert state.model is None
    assert state.phase == IDLE
    assert state.next_sync_at - datetime.now(timezone.utc) < timedelta(minutes=16)
