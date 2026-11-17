from datetime import date

from app.config import Settings
from app.main import price_store_path
from app.price_store import PriceStore
from app.schemas import PricePoint


def test_saved_prices_survive_reload(tmp_path):
    store = PriceStore(tmp_path / "prices.json")
    store.merge("com_1", [PricePoint(date=date(2026, 1, 2), price=100), PricePoint(date=date(2026, 1, 1), price=90)])
    store.save()

    reloaded = PriceStore(tmp_path / "prices.json")
    reloaded.load()

    dates, prices = reloaded.series("com_1")
    assert dates == [date(2026, 1, 1), date(2026, 1, 2)]
    assert prices.tolist() == [90, 100]
    assert reloaded.last_date("com_1") == date(2026, 1, 2)


def test_merge_overwrites_revised_prices_and_trim_drops_old_days(tmp_path):
    store = PriceStore(tmp_path / "prices.json")
    store.merge("com_1", [PricePoint(date=date(2026, 1, 1), price=90), PricePoint(date=date(2026, 1, 2), price=100)])
    store.merge("com_1", [PricePoint(date=date(2026, 1, 2), price=105)])

    store.trim(before=date(2026, 1, 2))

    dates, prices = store.series("com_1")
    assert dates == [date(2026, 1, 2)]
    assert prices.tolist() == [105]


def test_missing_file_and_unknown_code_are_empty(tmp_path):
    store = PriceStore(tmp_path / "missing.json")
    store.load()

    assert store.series("com_1") is None
    assert store.last_date("com_1") is None
    assert store.snapshot() == {}


def test_each_price_type_has_its_own_file():
    retail = Settings(api_key="x" * 32, data_dir="data", bi_price_type_id=1)
    wholesale = Settings(api_key="x" * 32, data_dir="data", bi_price_type_id=3)

    assert price_store_path(retail).name == "prices_type1.json"
    assert price_store_path(wholesale).name == "prices_type3.json"
