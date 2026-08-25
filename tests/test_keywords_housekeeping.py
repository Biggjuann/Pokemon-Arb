"""Negative keywords and scans-page housekeeping."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pokemon_arb import store
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.models import Deal, ExcludedKeyword, Listing, Product, ScanRun, Target
from pokemon_arb.pipeline.scan import ScanService
from pokemon_arb.sources.demo import DemoEbayClient, demo_products
from pokemon_arb.web.app import create_app


@pytest.fixture
def seeded():
    service = ScanService(ebay_client=DemoEbayClient(seed=11))
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    return service


@pytest.fixture
def client(seeded):
    with TestClient(create_app()) as test_client:
        yield test_client


# --- negative keywords -----------------------------------------------------
def test_add_keywords(client):
    client.post("/keywords/add", data={"term": "Fan Art, acrylic"}, follow_redirects=False)
    with get_sessionmaker()() as session:
        terms = {k.term for k in store.list_keywords(session)}
    assert terms == {"fan art", "acrylic"}  # normalised to lowercase


def test_recommended_list_can_be_added(client):
    client.post("/keywords/recommended", follow_redirects=False)
    with get_sessionmaker()() as session:
        terms = {k.term for k in store.list_keywords(session)}
    assert "fan art" in terms
    assert len(terms) == len(store.RECOMMENDED_EXCLUSIONS)


def test_excluded_listings_never_reach_the_database(seeded):
    """An excluded listing should leave no trace beyond the counter."""
    with get_sessionmaker()() as session:
        store.add_keyword(session, "mystery")
        session.commit()

    run = ScanService(ebay_client=DemoEbayClient(seed=11)).run()

    assert run.stats["excluded_by_keyword"] > 0
    with get_sessionmaker()() as session:
        titles = [listing.title.lower() for listing in session.scalars(select(Listing))]
    assert not any("mystery" in title for title in titles)


def test_exclusions_are_counted_per_keyword(seeded):
    with get_sessionmaker()() as session:
        store.add_keyword(session, "mystery")
        session.commit()
    ScanService(ebay_client=DemoEbayClient(seed=11)).run()
    with get_sessionmaker()() as session:
        keyword = session.scalar(select(ExcludedKeyword).where(ExcludedKeyword.term == "mystery"))
        assert keyword.hits > 0


def test_a_disabled_keyword_does_not_filter(seeded):
    with get_sessionmaker()() as session:
        keyword = store.add_keyword(session, "mystery")
        keyword.enabled = False
        session.commit()
    run = ScanService(ebay_client=DemoEbayClient(seed=11)).run()
    assert run.stats["excluded_by_keyword"] == 0


def test_keyword_matching_is_case_insensitive_and_substring(seeded):
    with get_sessionmaker()() as session:
        store.add_keyword(session, "REPACK")
        session.commit()
    run = ScanService(ebay_client=DemoEbayClient(seed=11)).run()
    assert run.stats["excluded_by_keyword"] > 0


def test_toggle_and_delete_keyword(client):
    client.post("/keywords/add", data={"term": "keychain"}, follow_redirects=False)
    with get_sessionmaker()() as session:
        keyword_id = session.scalar(select(ExcludedKeyword)).id

    client.post(f"/keywords/{keyword_id}/toggle", data={"enabled": "false"}, follow_redirects=False)
    with get_sessionmaker()() as session:
        assert session.get(ExcludedKeyword, keyword_id).enabled is False
        assert store.active_exclusions(session) == []

    client.post(f"/keywords/{keyword_id}/delete", follow_redirects=False)
    with get_sessionmaker()() as session:
        assert session.get(ExcludedKeyword, keyword_id) is None


def test_blank_keyword_is_ignored():
    with get_sessionmaker()() as session:
        assert store.add_keyword(session, "   ") is None
        assert store.list_keywords(session) == []


# --- the targets page is gone ---------------------------------------------
def test_targets_page_no_longer_exists(client):
    assert client.get("/targets").status_code == 404


def test_keywords_live_on_the_scans_page(client):
    body = client.get("/scans").text
    assert "Negative keywords" in body
    assert 'action="/keywords/add"' in body


# --- clear scan history ----------------------------------------------------
def test_clear_scan_history_keeps_findings(client, seeded):
    seeded.run()
    with get_sessionmaker()() as session:
        assert session.query(ScanRun).count() > 0
        deals_before = session.query(Deal).count()
        listings_before = session.query(Listing).count()
        assert deals_before > 0

    response = client.post("/scans/clear", data={"confirm": "yes"}, follow_redirects=False)
    assert response.status_code == 303

    with get_sessionmaker()() as session:
        assert session.query(ScanRun).count() == 0
        assert session.query(Deal).count() == deals_before
        assert session.query(Listing).count() == listings_before


def test_clear_requires_confirmation(client, seeded):
    seeded.run()
    assert client.post("/scans/clear", data={}, follow_redirects=False).status_code == 400
    with get_sessionmaker()() as session:
        assert session.query(ScanRun).count() > 0


# --- start from scratch ----------------------------------------------------
def test_reset_clears_findings_but_keeps_the_catalog(client, seeded):
    seeded.run()
    with get_sessionmaker()() as session:
        products = session.query(Product).count()
        targets = session.query(Target).count()
        assert session.query(Deal).count() > 0

    response = client.post("/scans/reset", data={"confirm": "yes"}, follow_redirects=False)
    assert response.status_code == 303

    with get_sessionmaker()() as session:
        assert session.query(Deal).count() == 0
        assert session.query(Listing).count() == 0
        # Rescan is off in this request, so only the reset record remains.
        assert session.query(ScanRun).count() == 0
        # The expensive things survive.
        assert session.query(Product).count() == products
        assert session.query(Target).count() == targets


def test_reset_keeps_keywords(client, seeded):
    client.post("/keywords/add", data={"term": "fan art"}, follow_redirects=False)
    seeded.run()
    client.post("/scans/reset", data={"confirm": "yes"}, follow_redirects=False)
    with get_sessionmaker()() as session:
        assert {k.term for k in store.list_keywords(session)} == {"fan art"}


def test_reset_requires_confirmation(client, seeded):
    seeded.run()
    assert client.post("/scans/reset", data={}, follow_redirects=False).status_code == 400
    with get_sessionmaker()() as session:
        assert session.query(Deal).count() > 0


def test_reset_can_rescan_immediately(client, seeded):
    seeded.run()
    response = client.post(
        "/scans/reset", data={"confirm": "yes", "rescan": "true"}, follow_redirects=False
    )
    assert response.status_code == 303
    # The background rescan runs before the test client returns.
    with get_sessionmaker()() as session:
        assert session.query(ScanRun).count() == 1
        assert session.query(Deal).count() > 0


def test_reset_on_an_empty_database_is_harmless(client):
    assert (
        client.post("/scans/reset", data={"confirm": "yes"}, follow_redirects=False).status_code
        == 303
    )
