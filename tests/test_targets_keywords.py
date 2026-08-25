"""Controlling what gets scanned: targets and negative keywords."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pokemon_arb import store
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.models import ExcludedKeyword, Listing, Target
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


# --- targets ---------------------------------------------------------------
def test_targets_page_lists_them(client):
    body = client.get("/targets").text
    assert "Charizard" in body or "charizard" in body
    assert "Negative keywords" in body


def test_disable_a_target_and_it_is_not_scanned(client, seeded):
    with get_sessionmaker()() as session:
        target = session.scalars(select(Target).order_by(Target.priority.desc())).first()
        target_id, query = target.id, target.query

    client.post(f"/targets/{target_id}/toggle", data={"enabled": "false"}, follow_redirects=False)
    ScanService(ebay_client=DemoEbayClient(seed=11)).run()

    with get_sessionmaker()() as session:
        disabled = session.get(Target, target_id)
        assert disabled.enabled is False
        assert disabled.last_scanned_at is None, f"{query} was scanned while disabled"


def test_rebuilding_targets_does_not_resurrect_disabled_ones(client, seeded):
    """Turning a target off has to survive a rebuild."""
    with get_sessionmaker()() as session:
        target_id = session.scalars(select(Target)).first().id
    client.post(f"/targets/{target_id}/toggle", data={"enabled": "false"}, follow_redirects=False)

    seeded.build_targets(per_set=5)

    with get_sessionmaker()() as session:
        assert session.get(Target, target_id).enabled is False


def test_add_custom_search_targets(client):
    response = client.post(
        "/targets/add",
        data={"query": "charizard vmax alt art, umbreon gold star"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with get_sessionmaker()() as session:
        customs = list(session.scalars(select(Target).where(Target.product_id.is_(None))))
        assert {t.query for t in customs} == {"charizard vmax alt art", "umbreon gold star"}
        assert all(t.enabled for t in customs)


def test_adding_the_same_custom_target_twice_is_idempotent(client):
    for _ in range(2):
        client.post("/targets/add", data={"query": "moonbreon"}, follow_redirects=False)
    with get_sessionmaker()() as session:
        assert len(list(session.scalars(select(Target).where(Target.query == "moonbreon")))) == 1


def test_delete_a_target(client):
    with get_sessionmaker()() as session:
        target_id = session.scalars(select(Target)).first().id
    client.post(f"/targets/{target_id}/delete", follow_redirects=False)
    with get_sessionmaker()() as session:
        assert session.get(Target, target_id) is None


def test_bulk_disable_a_whole_set(client):
    client.post(
        "/targets/bulk",
        data={"set_name": "Pokemon Base Set", "enabled": "false"},
        follow_redirects=False,
    )
    with get_sessionmaker()() as session:
        base = session.scalars(select(Target).where(Target.set_name == "Pokemon Base Set"))
        assert all(not t.enabled for t in base)
        others = session.scalars(select(Target).where(Target.set_name != "Pokemon Base Set"))
        assert any(t.enabled for t in others)


def test_target_filters(client):
    assert client.get("/targets?search=charizard").status_code == 200
    assert client.get("/targets?show=disabled").status_code == 200
    assert client.get("/targets?set_name=Pokemon+Base+Set").status_code == 200


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
