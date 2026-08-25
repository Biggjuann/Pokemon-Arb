"""eBay marketplace account deletion endpoint.

Production keysets do not authenticate until eBay has validated this
endpoint, so the challenge-response has to be exactly right.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pokemon_arb.config import get_settings
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.ebay_notifications import (
    compute_challenge_response,
    endpoint_problems,
    extract_username,
    generate_verification_token,
    purge_user_data,
    token_problems,
)
from pokemon_arb.models import Listing
from pokemon_arb.web.app import create_app

TOKEN = "a" * 40
ENDPOINT = "https://pokearb.example.com/ebay/account-deletion"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", TOKEN)
    monkeypatch.setenv("EBAY_DELETION_ENDPOINT_URL", ENDPOINT)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


# --- the hash --------------------------------------------------------------
def test_hash_is_sha256_of_code_token_endpoint_in_that_order():
    expected = hashlib.sha256(f"abc123{TOKEN}{ENDPOINT}".encode()).hexdigest()
    assert compute_challenge_response("abc123", TOKEN, ENDPOINT) == expected


def test_hash_order_matters():
    """Any other arrangement hashes fine and fails validation silently."""
    right = compute_challenge_response("abc", TOKEN, ENDPOINT)
    assert right != hashlib.sha256(f"{TOKEN}abc{ENDPOINT}".encode()).hexdigest()
    assert right != hashlib.sha256(f"abc{ENDPOINT}{TOKEN}".encode()).hexdigest()


def test_hash_is_hex_not_base64():
    value = compute_challenge_response("abc", TOKEN, ENDPOINT)
    assert len(value) == 64
    int(value, 16)  # raises if not hex


def test_endpoint_difference_changes_the_hash():
    """A trailing slash is a different endpoint as far as the hash cares."""
    assert compute_challenge_response("abc", TOKEN, ENDPOINT) != compute_challenge_response(
        "abc", TOKEN, ENDPOINT + "/"
    )


# --- token / endpoint rules ------------------------------------------------
def test_generated_token_is_acceptable():
    for length in (32, 48, 80):
        token = generate_verification_token(length)
        assert len(token) == length
        assert token_problems(token) == []


def test_generated_tokens_differ():
    assert generate_verification_token() != generate_verification_token()


@pytest.mark.parametrize(
    "token,expect",
    [
        (None, "not set"),
        ("short", "32-80"),
        ("a" * 100, "32-80"),
        ("a" * 30 + "!!@@", "underscore"),
    ],
)
def test_token_problems(token, expect):
    assert any(expect in problem for problem in token_problems(token))


def test_endpoint_problems():
    assert endpoint_problems(None) == ["not set"]
    assert any("https" in p for p in endpoint_problems("http://x.example.com/hook"))
    assert any("trailing slash" in p for p in endpoint_problems("https://x.example.com/hook/"))
    assert endpoint_problems(ENDPOINT) == []


# --- the challenge endpoint ------------------------------------------------
def test_challenge_returns_the_expected_response(client):
    response = client.get("/ebay/account-deletion", params={"challenge_code": "xyz789"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "challengeResponse": compute_challenge_response("xyz789", TOKEN, ENDPOINT)
    }


def test_challenge_response_varies_with_the_code(client):
    first = client.get("/ebay/account-deletion", params={"challenge_code": "a"}).json()
    second = client.get("/ebay/account-deletion", params={"challenge_code": "b"}).json()
    assert first != second


def test_challenge_without_a_code_is_rejected(client):
    assert client.get("/ebay/account-deletion").status_code == 422


def test_challenge_says_so_when_unconfigured(monkeypatch):
    monkeypatch.delenv("EBAY_VERIFICATION_TOKEN", raising=False)
    monkeypatch.delenv("EBAY_DELETION_ENDPOINT_URL", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as bare:
        response = bare.get("/ebay/account-deletion", params={"challenge_code": "x"})
    assert response.status_code == 503
    assert "EBAY_VERIFICATION_TOKEN" in response.json()["detail"]


# --- the notification endpoint ---------------------------------------------
def _notice(username: str) -> dict:
    return {
        "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
        "notification": {
            "notificationId": "n-1",
            "data": {"username": username, "userId": "u-1", "eiasToken": "e-1"},
        },
    }


def test_notification_is_acknowledged(client):
    assert client.post("/ebay/account-deletion", json=_notice("someseller")).status_code == 204


def test_notification_scrubs_the_username(client):
    with get_sessionmaker()() as session:
        session.add_all(
            [
                Listing(
                    ebay_item_id="i1",
                    title="Charizard",
                    url="u",
                    price_cents=100,
                    seller_username="deleteme",
                ),
                Listing(
                    ebay_item_id="i2",
                    title="Blastoise",
                    url="u",
                    price_cents=100,
                    seller_username="someone_else",
                ),
            ]
        )
        session.commit()

    assert client.post("/ebay/account-deletion", json=_notice("deleteme")).status_code == 204

    with get_sessionmaker()() as session:
        listings = {listing.ebay_item_id: listing for listing in session.scalars(select(Listing))}
        assert listings["i1"].seller_username is None
        assert listings["i2"].seller_username == "someone_else"
        # The listing itself survives -- a card and its price are not personal data.
        assert listings["i1"].title == "Charizard"


def test_notification_is_acknowledged_even_if_the_payload_is_odd(client):
    """eBay marks the callback down if we do not acknowledge; never 500."""
    for payload in ({}, {"notification": {}}, {"notification": {"data": None}}):
        assert client.post("/ebay/account-deletion", json=payload).status_code == 204


def test_extract_username():
    assert extract_username(_notice("bob")) == "bob"
    assert extract_username({}) is None
    assert extract_username({"notification": {"data": {"username": ""}}}) is None


def test_purge_is_a_noop_for_unknown_users():
    with get_sessionmaker()() as session:
        assert purge_user_data(session, "nobody") == 0
        assert purge_user_data(session, None) == 0
