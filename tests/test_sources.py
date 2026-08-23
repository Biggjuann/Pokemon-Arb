from __future__ import annotations

import httpx
import pytest
import respx

from pokemon_arb.sources.ebay import EbayAuthError, EbayClient, EbayError, EbayListing
from pokemon_arb.sources.pricecharting import (
    PriceChartingClient,
    PriceChartingError,
)

CSV = """id,console-name,product-name,loose-price,cib-price,new-price,graded-price,box-only-price,manual-only-price,sales-volume,release-date
6910,Pokemon Base Set,Charizard #4,420.00,670.00,1000.00,1800.00,5000.00,12500.00,210,1999-01-09
6911,Pokemon Base Set,Base Set Booster Box,,,,,,,3,1999-01-09
6912,Pokemon Base Set,Blastoise #2,140.00,,,520.00,,3800.00,130,1999-01-09
"""


# --- PriceCharting ---------------------------------------------------------
def test_csv_prices_are_parsed_as_dollars():
    products = PriceChartingClient.parse_price_guide_csv(CSV)
    charizard = next(p for p in products if p.name == "Charizard #4")
    assert charizard.prices["ungraded_cents"] == 42000
    assert charizard.prices["psa10_cents"] == 1250000
    assert charizard.sales_volume == 210


def test_csv_filters_out_sealed_product():
    names = {p.name for p in PriceChartingClient.parse_price_guide_csv(CSV)}
    assert "Base Set Booster Box" not in names
    assert names == {"Charizard #4", "Blastoise #2"}


def test_csv_missing_prices_become_none():
    blastoise = next(
        p for p in PriceChartingClient.parse_price_guide_csv(CSV) if p.name == "Blastoise #2"
    )
    assert blastoise.prices["grade7_cents"] is None
    assert blastoise.prices["ungraded_cents"] == 14000


def test_api_prices_are_parsed_as_pennies():
    with respx.mock(base_url="https://www.pricecharting.com") as mock:
        mock.get("/api/products").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "products": [
                        {
                            "id": "6910",
                            "product-name": "Charizard #4",
                            "console-name": "Pokemon Base Set",
                            "loose-price": 42000,
                            "manual-only-price": 1250000,
                            "sales-volume": "210",
                        }
                    ],
                },
            )
        )
        with PriceChartingClient("token") as client:
            products = client.search_products("charizard")
    assert products[0].prices["ungraded_cents"] == 42000
    assert products[0].prices["psa10_cents"] == 1250000


def test_missing_token_is_a_clear_error():
    with PriceChartingClient(None) as client:
        with pytest.raises(PriceChartingError, match="PRICECHARTING_TOKEN"):
            client.search_products("charizard")


def test_model_kwargs_derive_card_number_and_blob(pc_product):
    kwargs = pc_product.to_model_kwargs()
    assert kwargs["card_number"] == "4"
    assert "charizard" in kwargs["search_blob"]
    assert kwargs["ungraded_cents"] == 42000


# --- eBay ------------------------------------------------------------------
def _token_route(mock):
    return mock.post("/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 7200})
    )


def _item(item_id="v1|1|0", price="200.00", title="Charizard 4/102 Base Set"):
    return {
        "itemId": item_id,
        "title": title,
        "price": {"value": price, "currency": "USD"},
        "shippingOptions": [{"shippingCost": {"value": "4.99"}}],
        "itemWebUrl": f"https://www.ebay.com/itm/{item_id}",
        "condition": "Used",
        "seller": {"username": "cardguy", "feedbackPercentage": "99.4", "feedbackScore": 812},
        "buyingOptions": ["FIXED_PRICE"],
        "itemCreationDate": "2026-08-20T12:00:00.000Z",
        "itemLocation": {"country": "US"},
        "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
    }


def test_search_parses_listings():
    with respx.mock(base_url="https://api.ebay.com") as mock:
        _token_route(mock)
        mock.get("/buy/browse/v1/item_summary/search").mock(
            return_value=httpx.Response(200, json={"total": 1, "itemSummaries": [_item()]})
        )
        with EbayClient("id", "secret") as client:
            listings = list(client.search("charizard base set 4", limit=10))
    assert len(listings) == 1
    listing = listings[0]
    assert listing.price_cents == 20000
    assert listing.shipping_cents == 499
    assert listing.seller_feedback_pct == 99.4
    assert listing.item_location == "US"
    assert listing.listed_at is not None


def test_token_is_reused_across_searches():
    with respx.mock(base_url="https://api.ebay.com") as mock:
        token_route = _token_route(mock)
        mock.get("/buy/browse/v1/item_summary/search").mock(
            return_value=httpx.Response(200, json={"total": 1, "itemSummaries": [_item()]})
        )
        with EbayClient("id", "secret") as client:
            list(client.search("a", limit=1))
            list(client.search("b", limit=1))
            assert client.call_count == 2
    assert token_route.call_count == 1


def test_missing_credentials_raise_a_clear_error():
    with EbayClient(None, None) as client:
        with pytest.raises(EbayAuthError, match="EBAY_CLIENT_ID"):
            list(client.search("charizard"))


def test_rate_limit_is_surfaced():
    with respx.mock(base_url="https://api.ebay.com") as mock:
        _token_route(mock)
        mock.get("/buy/browse/v1/item_summary/search").mock(return_value=httpx.Response(429))
        with EbayClient("id", "secret") as client:
            with pytest.raises(EbayError, match="rate limit"):
                list(client.search("charizard"))


def test_search_stops_at_the_end_of_results():
    with respx.mock(base_url="https://api.ebay.com") as mock:
        _token_route(mock)
        mock.get("/buy/browse/v1/item_summary/search").mock(
            return_value=httpx.Response(
                200, json={"total": 2, "itemSummaries": [_item("a"), _item("b")]}
            )
        )
        with EbayClient("id", "secret") as client:
            listings = list(client.search("charizard", limit=100, max_pages=5))
    assert len(listings) == 2


def test_items_without_a_price_are_skipped():
    assert EbayListing.from_summary({"itemId": "x", "title": "no price"}) is None
    assert EbayListing.from_summary({"title": "no id", "price": {"value": "1.00"}}) is None


def test_auction_plus_bin_is_treated_as_bin():
    item = _item()
    item["buyingOptions"] = ["AUCTION", "FIXED_PRICE"]
    assert EbayListing.from_summary(item).buying_format == "FIXED_PRICE"


def test_filter_string_shape():
    built = EbayClient.build_filter(
        max_price_cents=50000,
        min_price_cents=1000,
        item_location_country="US",
        delivery_country="US",
    )
    assert "buyingOptions:{FIXED_PRICE}" in built
    assert "price:[10.00..500.00]" in built
    assert "priceCurrency:USD" in built
    assert "itemLocationCountry:US" in built


def test_sandbox_base_url(monkeypatch):
    from pokemon_arb.config import Settings

    assert Settings(ebay_env="sandbox").ebay_api_base == "https://api.sandbox.ebay.com"
    assert Settings(ebay_env="production").ebay_api_base == "https://api.ebay.com"
