from __future__ import annotations

import pytest

from pokemon_arb import db as db_module
from pokemon_arb.config import get_settings
from pokemon_arb.models import utcnow
from pokemon_arb.sources.pricecharting import PCProduct


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own SQLite file and a fresh settings cache."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PRICECHARTING_TOKEN", raising=False)
    get_settings.cache_clear()
    db_module.reset_engine()
    db_module.init_db()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def charizard():
    from pokemon_arb.models import Product

    return Product(
        external_id="pc-1",
        name="Charizard #4",
        set_name="Pokemon Base Set",
        card_number="4",
        ungraded_cents=42000,
        grade7_cents=67000,
        grade8_cents=100000,
        grade9_cents=180000,
        grade95_cents=500000,
        psa10_cents=1250000,
        sales_volume=210,
        last_synced_at=utcnow(),
    )


@pytest.fixture
def listing_factory():
    from pokemon_arb.models import Listing

    def make(title: str, price_cents: int = 15000, **kwargs):
        defaults = {
            "ebay_item_id": f"item-{abs(hash(title)) % 10**8}",
            "title": title,
            "url": "https://www.ebay.com/itm/1",
            "price_cents": price_cents,
            "shipping_cents": 0,
            "currency": "USD",
            "buying_format": "FIXED_PRICE",
            "seller_username": "seller",
            "seller_feedback_pct": 100.0,
            "seller_feedback_score": 5000,
            "returns_accepted": True,
            "item_location": "US",
        }
        defaults.update(kwargs)
        return Listing(**defaults)

    return make


@pytest.fixture
def pc_product():
    return PCProduct(
        external_id="pc-1",
        name="Charizard #4",
        set_name="Pokemon Base Set",
        prices={
            "ungraded_cents": 42000,
            "grade7_cents": 67000,
            "grade8_cents": 100000,
            "grade9_cents": 180000,
            "grade95_cents": 500000,
            "psa10_cents": 1250000,
        },
        sales_volume=210,
    )
