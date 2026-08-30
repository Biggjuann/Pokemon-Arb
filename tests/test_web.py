from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pokemon_arb.config import get_settings
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.models import Deal
from pokemon_arb.pipeline.scan import ScanService
from pokemon_arb.sources.demo import DemoEbayClient, demo_products
from pokemon_arb.web.app import create_app


@pytest.fixture
def client():
    service = ScanService(ebay_client=DemoEbayClient(seed=11))
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    service.run()
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def deal_id():
    with get_sessionmaker()() as session:
        deal = session.scalar(select(Deal).order_by(Deal.score.desc()))
        return deal.id if deal else None


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_board_renders_deals(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "risk-adjusted" in response.text or "Deals shown" in response.text
    assert "Charizard" in response.text or "Umbreon" in response.text


def test_board_filters_are_applied(client):
    everything = client.get("/?status=all&limit=500").text.count('class="score"')
    filtered = client.get("/?status=all&limit=500&min_roi=5.0").text.count('class="score"')
    assert filtered < everything


def test_low_risk_filter_excludes_flagged_deals(client):
    response = client.get("/?status=all&hide_risky=true&limit=500")
    assert response.status_code == 200
    from pokemon_arb.models import Deal as D

    with get_sessionmaker()() as session:
        shown = client.get("/?status=all&hide_risky=true&limit=500").text.count('class="score"')
        low_risk = len(
            [
                d
                for d in session.scalars(select(D))
                if d.risk_penalty <= 0.25 and d.listing.is_active
            ]
        )
    assert shown == low_risk


def test_deal_detail_shows_the_math(client, deal_id):
    response = client.get(f"/deals/{deal_id}")
    assert response.status_code == 200
    for label in ["Comp used", "Net proceeds", "Sales tax", "Profit", "confidence"]:
        assert label in response.text


def test_deal_detail_404(client):
    assert client.get("/deals/999999").status_code == 404


def test_status_update_round_trip(client, deal_id):
    response = client.post(
        f"/deals/{deal_id}/status",
        data={"status": "bought", "notes": "paid $180", "redirect_to": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with get_sessionmaker()() as session:
        deal = session.get(Deal, deal_id)
        assert deal.status == "bought"
        assert deal.notes == "paid $180"


def test_invalid_status_is_rejected(client, deal_id):
    response = client.post(
        f"/deals/{deal_id}/status", data={"status": "nonsense"}, follow_redirects=False
    )
    assert response.status_code == 400


def test_api_deals_shape(client):
    payload = client.get("/api/deals?status=all&limit=5").json()
    assert payload
    row = payload[0]
    for key in ["id", "score", "card", "url", "profit_cents", "roi", "risk_flags"]:
        assert key in row
    scores = [r["score"] for r in payload]
    assert scores == sorted(scores, reverse=True)


def test_api_min_score_filter(client):
    everything = client.get("/api/deals?status=all&limit=500").json()
    top = client.get("/api/deals?status=all&limit=500&min_score=25").json()
    assert len(top) <= len(everything)
    assert all(row["score"] >= 25 for row in top)


def test_scans_page(client):
    response = client.get("/scans")
    assert response.status_code == 200
    assert "Cards tracked" in response.text


def test_scan_trigger_redirects(client):
    response = client.post("/scan", data={"demo": "true"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/scans"


# --- blank filter fields ---------------------------------------------------
# An HTML number input the user never filled in submits as `field=`, which
# used to 422 the whole board. These lock that in.


def test_the_form_url_with_every_field_blank(client):
    """The exact URL the filter form produces when nothing is filled in."""
    response = client.get(
        "/?status=new&sort=score&set_name=&min_roi=&max_cost=&min_confidence=&hide_risky=true"
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "max_cost=",
        "min_roi=",
        "min_confidence=",
        "limit=",
        "min_roi=&max_cost=&min_confidence=&limit=",
        "max_cost=   ",
    ],
)
def test_blank_numeric_filters_are_ignored(client, query):
    response = client.get(f"/?status=all&{query}")
    assert response.status_code == 200


def test_blank_filters_match_no_filters_at_all(client):
    blank = client.get("/?status=all&min_roi=&max_cost=&min_confidence=").text
    absent = client.get("/?status=all").text
    assert blank.count('class="score"') == absent.count('class="score"')


def test_blank_field_does_not_round_trip_as_none(client):
    """The rendered form must come back empty, not containing the word None."""
    response = client.get("/?status=all&max_cost=&min_roi=&min_confidence=")
    assert 'value="None"' not in response.text


def test_populated_numeric_filters_still_apply(client):
    everything = client.get("/?status=all&limit=500").text.count('class="score"')
    filtered = client.get("/?status=all&limit=500&max_cost=1").text.count('class="score"')
    assert filtered < everything


def test_api_tolerates_blank_numeric_params(client):
    response = client.get("/api/deals?status=all&limit=&min_score=")
    assert response.status_code == 200
    assert len(response.json()) <= 50  # falls back to the default limit


def test_limit_is_clamped_not_rejected(client):
    assert client.get("/?status=all&limit=99999").status_code == 200
    assert client.get("/?status=all&limit=0").status_code == 200
    assert len(client.get("/api/deals?status=all&limit=99999").json()) <= 500


def test_genuinely_invalid_numbers_are_still_rejected(client):
    """Blank is 'no filter'; garbage is still a client error."""
    assert client.get("/?status=all&max_cost=abc").status_code == 422


# --- set-up flow -----------------------------------------------------------
# A deployed instance has no shell, so the catalog has to be loadable from the
# UI. Before this existed, a live deploy could not be brought into a working
# state at all.


@pytest.fixture
def empty_client():
    """A client with no products, targets or deals -- a fresh deploy."""
    with TestClient(create_app()) as test_client:
        yield test_client


def test_fresh_deploy_explains_what_to_do(empty_client):
    body = empty_client.get("/scans").text
    assert "What to scan for" in body
    assert "Nothing is being tracked yet" in body
    assert 'action="/sync"' in body


def test_scope_form_stays_available_once_populated(client):
    """It is the only place to change what is scanned, so it must not hide."""
    body = client.get("/scans").text
    assert "What to scan for" in body
    assert 'action="/sync"' in body
    assert "Nothing is being tracked yet" not in body
    assert "Currently tracking" in body


def test_scan_with_nothing_configured_is_not_reported_as_success(monkeypatch, tmp_path):
    """With eBay credentials set there is no demo fallback, so an unpopulated
    catalog yields a scan that succeeds and does nothing -- say so plainly."""
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    get_settings.cache_clear()

    with TestClient(create_app()) as live_client:
        # No targets exist, so no eBay call is ever attempted.
        live_client.post("/scan", data={}, follow_redirects=False)
        body = live_client.get("/scans").text
    assert "no targets" in body
    assert "nothing to scan" in body
    assert "What to scan for" in body


def test_sync_route_redirects(empty_client):
    response = empty_client.post(
        "/sync", data={"queries": "charizard", "per_set": "5"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/scans"


def test_sync_without_a_pricecharting_token_says_so(empty_client):
    """The most likely live failure should name the missing variable."""
    empty_client.post("/sync", data={"queries": "charizard"}, follow_redirects=False)
    body = empty_client.get("/scans").text
    assert "PRICECHARTING_TOKEN" in body


def test_healthz_reports_the_running_job(empty_client):
    payload = empty_client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert "running_job" in payload
