"""Valuing listings against their peers, using eBay alone.

eBay sold data is unavailable (Marketplace Insights is a closed Limited
Release; Browse is active listings only), so peer comps use asking prices.
These tests pin the properties that keep that honest.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from pokemon_arb.config import get_settings
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.matching.normalize import parse_title
from pokemon_arb.models import Deal, Product, Target
from pokemon_arb.pipeline.peer_comps import (
    PeerGroup,
    build_groups,
    grade_segment,
    group_key,
    percentile,
)
from pokemon_arb.pipeline.scan import ScanService
from pokemon_arb.sources.ebay import EbayListing


# --- percentile ------------------------------------------------------------
def test_percentile_interpolates():
    values = [100, 200, 300, 400, 500]
    assert percentile(values, 0.0) == 100
    assert percentile(values, 1.0) == 500
    assert percentile(values, 0.5) == 300
    assert 200 < percentile(values, 0.35) < 300


def test_percentile_single_value():
    assert percentile([420], 0.35) == 420


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_percentile_is_clamped():
    assert percentile([10, 20], -5) == 10
    assert percentile([10, 20], 9) == 20


# --- grade segmentation ----------------------------------------------------
def test_raw_and_slabs_are_priced_separately():
    assert grade_segment(parse_title("Charizard 4/102 NM")) == "raw"
    assert grade_segment(parse_title("PSA 10 Charizard 4/102")) == "PSA 10"
    assert grade_segment(parse_title("BGS 9.5 Charizard 4/102")) == "BGS 9.5"
    assert grade_segment(parse_title("Graded slab Charizard 4/102")) == "slab-unverified"


def test_a_slab_does_not_join_the_raw_group():
    raw = group_key(parse_title("Charizard 4/102 Base Set"))
    slab = group_key(parse_title("PSA 10 Charizard 4/102 Base Set"))
    assert raw != slab
    assert raw[1] == slab[1] == "4"


# --- self-exclusion --------------------------------------------------------
def test_a_listing_is_not_part_of_its_own_comp():
    """Otherwise the bargain drags down the number it is judged against."""
    group = PeerGroup(segment="raw", card_number="4", prices=[9000, 18000, 21000, 24000, 30000])
    with_self = percentile(group.prices, 0.35)
    without_self = group.comp_excluding(9000, 0.35)
    assert without_self > with_self


def test_comp_excluding_the_only_price_is_none():
    assert PeerGroup(segment="raw", card_number="4", prices=[100]).comp_excluding(100, 0.5) is None


def test_grouping_counts_landed_price():
    items = [
        (
            EbayListing(
                ebay_item_id="a",
                title="Charizard 4/102",
                url="",
                price_cents=1000,
                shipping_cents=500,
            ),
            parse_title("Charizard 4/102"),
        )
    ]
    assert build_groups(items)[("raw", "4")].prices == [1500]


# --- end to end ------------------------------------------------------------
class _PeerEbay:
    """A card with a settled market and one genuine bargain in it."""

    def __init__(self, prices=(18000, 20000, 21000, 23000, 26000, 9000), noise=True):
        self.prices = prices
        self.noise = noise
        self.call_count = 0

    @staticmethod
    def build_filter(**kwargs):
        from pokemon_arb.sources.ebay import EbayClient

        return EbayClient.build_filter(**kwargs)

    def close(self):
        pass

    def search(self, query, **kwargs):
        self.call_count += 1
        for index, price in enumerate(self.prices):
            yield EbayListing(
                ebay_item_id=f"peer-{index}",
                title="Pokemon Charizard 4/102 Base Set Holo NM",
                url=f"https://www.ebay.com/itm/{index}",
                price_cents=price,
                shipping_cents=0,
                seller_username=f"seller{index}",
                seller_feedback_pct=100.0,
                seller_feedback_score=2000,
                returns_accepted=True,
                item_location="US",
                listed_at=dt.datetime(2026, 8, 1),
            )
        if self.noise:
            # Must not be pooled into the single-card comp.
            yield EbayListing(
                ebay_item_id="lot",
                title="Pokemon Lot 50 Cards Charizard 4/102 bulk",
                url="",
                price_cents=3000,
                shipping_cents=0,
                item_location="US",
            )
            yield EbayListing(
                ebay_item_id="slab",
                title="PSA 10 Charizard 4/102 Base Set",
                url="",
                price_cents=900000,
                shipping_cents=0,
                item_location="US",
            )


@pytest.fixture
def peer_mode(monkeypatch):
    monkeypatch.setenv("COMP_SOURCE", "peer")
    monkeypatch.setenv("MIN_ROI", "0.05")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _target(query: str = "charizard base set 4") -> None:
    from pokemon_arb import store

    with get_sessionmaker()() as session:
        store.add_custom_target(session, query)
        session.commit()


def test_peer_scan_finds_the_bargain(peer_mode):
    _target()
    run = ScanService(ebay_client=_PeerEbay()).run()

    assert run.status == "ok"
    with get_sessionmaker()() as session:
        deals = list(session.scalars(select(Deal)))
        assert len(deals) == 1, [d.listing.title for d in deals]
        deal = deals[0]
        assert deal.listing.price_cents == 9000
        assert "PEER_COMP" in deal.risk_flags
        # Valued against the others, not against itself.
        assert deal.market_value_cents > 9000


def test_lots_and_slabs_do_not_pollute_the_comp(peer_mode):
    _target()
    ScanService(ebay_client=_PeerEbay()).run()
    with get_sessionmaker()() as session:
        deal = session.scalar(select(Deal))
        # A $30 lot of 50 in the pool would drag this well down.
        assert deal.market_value_cents > 15000


def test_thin_groups_are_not_priced(peer_mode):
    """Three asks is noise, and noise here means buying something."""
    _target()
    run = ScanService(ebay_client=_PeerEbay(prices=(20000, 21000, 9000), noise=False)).run()
    assert run.stats["skipped_thin_peers"] > 0
    with get_sessionmaker()() as session:
        assert session.query(Deal).count() == 0


def test_peer_comp_is_flagged_as_weaker_evidence(peer_mode):
    from pokemon_arb.pipeline.scoring import RISK_WEIGHTS

    _target()
    ScanService(ebay_client=_PeerEbay()).run()
    with get_sessionmaker()() as session:
        deal = session.scalar(select(Deal))
        assert deal.risk_penalty >= RISK_WEIGHTS["PEER_COMP"][0]


def test_peer_mode_creates_a_stand_in_product(peer_mode):
    _target()
    ScanService(ebay_client=_PeerEbay()).run()
    with get_sessionmaker()() as session:
        product = session.scalar(select(Product))
        assert product.external_id.startswith("peer:")
        assert product.sales_volume is None


def test_unknown_sales_volume_is_not_a_thin_comp(peer_mode):
    """None means the source publishes no count, not that it never sells."""
    _target()
    ScanService(ebay_client=_PeerEbay()).run()
    with get_sessionmaker()() as session:
        assert "THIN_COMPS" not in session.scalar(select(Deal)).risk_flags


def test_peer_mode_does_not_build_catalog_targets(peer_mode):
    service = ScanService(ebay_client=_PeerEbay())
    assert service.build_targets(per_set=25) == 0
    with get_sessionmaker()() as session:
        assert session.query(Target).count() == 0


def test_a_settled_market_yields_no_deals(peer_mode):
    """Every listing near the same price means there is nothing to find."""
    _target()
    ScanService(
        ebay_client=_PeerEbay(prices=(20000, 20500, 21000, 20800, 20200), noise=False)
    ).run()
    with get_sessionmaker()() as session:
        assert session.query(Deal).count() == 0
