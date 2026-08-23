from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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
