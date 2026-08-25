"""eBay API License Agreement 8.1(c): displayed listing data must be under six
hours old. These lock down the *display* rule, which is enforced per request --
a scan flag written hours ago is not evidence of freshness."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pokemon_arb.clock import utcnow
from pokemon_arb.config import Settings, get_settings
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.freshness import (
    MAX_DISPLAY_AGE,
    age_label,
    cutoff,
    display_window,
    is_fresh,
)
from pokemon_arb.models import Deal, Listing
from pokemon_arb.pipeline.scan import ScanService
from pokemon_arb.sources.demo import DemoEbayClient, demo_products
from pokemon_arb.web.app import create_app


@pytest.fixture
def seeded():
    service = ScanService(ebay_client=DemoEbayClient(seed=11))
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    service.run()


@pytest.fixture
def client(seeded):
    with TestClient(create_app()) as test_client:
        yield test_client


def _age_all_listings(delta: dt.timedelta) -> None:
    with get_sessionmaker()() as session:
        for listing in session.scalars(select(Listing)):
            listing.last_seen_at = utcnow() - delta
        session.commit()


# --- the window itself -----------------------------------------------------
def test_default_window_is_the_licence_limit():
    assert display_window(Settings()) == MAX_DISPLAY_AGE == dt.timedelta(hours=6)


def test_window_can_be_tightened():
    assert display_window(Settings(listing_freshness_minutes=30)) == dt.timedelta(minutes=30)


@pytest.mark.parametrize("minutes", [361, 1440, 4320, 100000])
def test_window_cannot_be_loosened_past_the_licence(minutes):
    """Config must never be able to put the app out of compliance."""
    assert display_window(Settings(listing_freshness_minutes=minutes)) == MAX_DISPLAY_AGE


def test_nonsense_window_falls_back_to_something_sane():
    assert display_window(Settings(listing_freshness_minutes=0)) == dt.timedelta(minutes=1)
    assert display_window(Settings(listing_freshness_minutes=-99)) == dt.timedelta(minutes=1)


def test_is_fresh_boundary(settings):
    now = utcnow()
    just_inside = Listing(last_seen_at=now - dt.timedelta(hours=5, minutes=59))
    just_outside = Listing(last_seen_at=now - dt.timedelta(hours=6, minutes=1))
    assert is_fresh(just_inside, settings, now=now)
    assert not is_fresh(just_outside, settings, now=now)


def test_cutoff_tracks_the_clock(settings):
    now = utcnow()
    assert cutoff(settings, now=now) == now - dt.timedelta(hours=6)


def test_age_label():
    now = utcnow()
    assert age_label(now - dt.timedelta(minutes=4), now=now) == "4m"
    assert age_label(now - dt.timedelta(hours=2, minutes=10), now=now) == "2h 10m"
    # A clock skew must not render as a negative age.
    assert age_label(now + dt.timedelta(minutes=5), now=now) == "0m"
    # Targets that were never scanned have no timestamp at all.
    assert age_label(None) == "never"


# --- board -----------------------------------------------------------------
def test_fresh_deals_are_shown(client):
    assert client.get("/?status=all&limit=500").text.count('class="score"') > 0


def test_stale_deals_are_withheld_from_the_board(client):
    _age_all_listings(dt.timedelta(hours=7))
    body = client.get("/?status=all&limit=500").text
    assert body.count('class="score"') == 0
    assert "8.1(c)" in body


def test_staleness_is_evaluated_per_request_not_at_scan_time(client):
    """The old flag-based approach kept showing listings when scans stopped."""
    with get_sessionmaker()() as session:
        # Explicitly still "active" -- only the clock has moved on.
        for listing in session.scalars(select(Listing)):
            listing.is_active = True
            listing.last_seen_at = utcnow() - dt.timedelta(hours=8)
        session.commit()
    assert client.get("/?status=all&limit=500").text.count('class="score"') == 0


def test_board_reports_how_many_were_hidden(client):
    with get_sessionmaker()() as session:
        total = session.query(Deal).count()
    _age_all_listings(dt.timedelta(hours=7))
    body = client.get("/?status=all&limit=500").text
    assert f"{total} deal" in body


def test_deals_just_inside_the_window_still_show(client):
    _age_all_listings(dt.timedelta(hours=5, minutes=45))
    assert client.get("/?status=all&limit=500").text.count('class="score"') > 0


def test_a_tighter_window_hides_more(client, monkeypatch):
    _age_all_listings(dt.timedelta(hours=2))
    assert client.get("/?status=all&limit=500").text.count('class="score"') > 0

    monkeypatch.setenv("LISTING_FRESHNESS_MINUTES", "60")
    get_settings.cache_clear()
    assert client.get("/?status=all&limit=500").text.count('class="score"') == 0


# --- detail page -----------------------------------------------------------
def test_detail_page_refuses_to_render_stale_ebay_content(client):
    with get_sessionmaker()() as session:
        deal = session.scalar(select(Deal).order_by(Deal.score.desc()))
        deal_id, price = deal.id, deal.listing.price_cents
    _age_all_listings(dt.timedelta(hours=9))

    response = client.get(f"/deals/{deal_id}")
    assert response.status_code == 409
    assert "8.1(c)" in response.text
    # None of the cached eBay content may appear.
    assert f"{price / 100:,.2f}" not in response.text
    assert "Buy it now" not in response.text


def test_detail_page_shows_data_age_when_fresh(client):
    with get_sessionmaker()() as session:
        deal_id = session.scalar(select(Deal).order_by(Deal.score.desc())).id
    response = client.get(f"/deals/{deal_id}")
    assert response.status_code == 200
    assert "Data age" in response.text


# --- json api --------------------------------------------------------------
def test_api_withholds_stale_deals(client):
    assert client.get("/api/deals?status=all&limit=500").json()
    _age_all_listings(dt.timedelta(hours=7))
    assert client.get("/api/deals?status=all&limit=500").json() == []


def test_api_exposes_listing_age(client):
    row = client.get("/api/deals?status=all&limit=1").json()[0]
    assert "listing_seen_at" in row
    assert "listing_age" in row


# --- cli -------------------------------------------------------------------
def test_cli_top_withholds_stale_deals(seeded):
    from typer.testing import CliRunner

    from pokemon_arb.cli import app as cli_app

    runner = CliRunner()
    assert "SCORE" in runner.invoke(cli_app, ["top", "--status", "all"]).stdout

    _age_all_listings(dt.timedelta(hours=7))
    result = runner.invoke(cli_app, ["top", "--status", "all"])
    assert "No deals with listing data under 6h old" in result.stdout
