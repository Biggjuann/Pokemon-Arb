"""Credential diagnostics: pinpoint a 401 invalid_client without a shell."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from pokemon_arb.config import Settings, get_settings
from pokemon_arb.diagnostics import check_ebay, check_pricecharting, fingerprint
from pokemon_arb.web.app import create_app

PROD_ID = "JohnDoe-PokeArb-PRD-1a2b3c4d5-6e7f8g9h"
PROD_SECRET = "PRD-1a2b3c4d5e6f-7a8b-9c0d-1e2f"
DEV_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def _named(report, name):
    return next(c for c in report.checks if c.name == name)


# --- fingerprints must never leak the secret -------------------------------
def test_fingerprint_hides_the_value():
    secret = "PRD-supersecretvalue-1234567890"
    printed = fingerprint(secret)
    assert secret not in printed
    assert "31 chars" in printed
    assert "PRD-" in printed  # enough to compare against the console
    assert "supersecretvalue" not in printed


def test_fingerprint_flags_whitespace_and_quotes():
    assert "WHITESPACE" in fingerprint("  abcdef  ")
    assert "QUOTES" in fingerprint('"abcdef"')
    assert fingerprint(None) == "not set"


def test_full_report_never_contains_the_raw_secret(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET)
    get_settings.cache_clear()
    report = check_ebay(get_settings(), live=False)
    blob = " ".join(f"{c.name} {c.detail} {c.fix}" for c in report.checks)
    assert PROD_SECRET not in blob


# --- the shape heuristics --------------------------------------------------
def test_dev_id_used_as_the_secret_is_caught(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", DEV_ID)
    get_settings.cache_clear()
    check = _named(check_ebay(get_settings(), live=False), "Secret is the Cert ID")
    assert check.ok is False
    assert "Dev ID" in check.fix


def test_correct_pair_passes_the_shape_checks(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET)
    get_settings.cache_clear()
    report = check_ebay(get_settings(), live=False)
    assert _named(report, "Secret is the Cert ID").ok
    assert _named(report, "Keyset matches EBAY_ENV").ok
    assert _named(report, "Credentials are clean").ok


def test_sandbox_keyset_against_production_is_caught(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID.replace("-PRD-", "-SBX-"))
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET)
    monkeypatch.setenv("EBAY_ENV", "production")
    get_settings.cache_clear()
    check = _named(check_ebay(get_settings(), live=False), "Keyset matches EBAY_ENV")
    assert check.ok is False
    assert "EBAY_ENV=sandbox" in check.fix


def test_pasted_whitespace_is_caught_and_stripped(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET + "\n")
    get_settings.cache_clear()
    settings = get_settings()
    # Reported...
    assert _named(check_ebay(settings, live=False), "Credentials are clean").ok is False
    # ...and defensively fixed, so it cannot silently break Basic auth.
    assert settings.ebay_client_secret == PROD_SECRET


def test_missing_credentials_are_reported(monkeypatch):
    get_settings.cache_clear()
    check = _named(check_ebay(get_settings(), live=False), "Credentials present")
    assert check.ok is False
    assert "demo mode" in check.fix


def test_unknown_keyset_shape_is_inconclusive_not_a_failure(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "opaque-app-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET)
    get_settings.cache_clear()
    assert _named(check_ebay(get_settings(), live=False), "Keyset matches EBAY_ENV").ok is None


# --- live calls ------------------------------------------------------------
def _settings() -> Settings:
    return Settings(ebay_client_id=PROD_ID, ebay_client_secret=PROD_SECRET, ebay_env="production")


def test_live_401_is_reported_with_the_reason():
    with respx.mock(base_url="https://api.ebay.com") as mock:
        mock.post("/identity/v1/oauth2/token").mock(
            return_value=httpx.Response(
                401,
                json={
                    "error": "invalid_client",
                    "error_description": "client authentication failed",
                },
            )
        )
        check = _named(check_ebay(_settings(), live=True), "OAuth token")
    assert check.ok is False
    assert "invalid_client" in check.detail
    assert "Cert ID" in check.fix


def test_live_success_runs_both_searches():
    with respx.mock(base_url="https://api.ebay.com") as mock:
        mock.post("/identity/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        )
        mock.get("/buy/browse/v1/item_summary/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "total": 1,
                    "itemSummaries": [
                        {
                            "itemId": "v1|1|0",
                            "title": "Charizard 4/102",
                            "price": {"value": "200.00", "currency": "USD"},
                        }
                    ],
                },
            )
        )
        report = check_ebay(_settings(), live=True)
    assert _named(report, "Search (no filters)").ok
    assert _named(report, "Search (scanner's filters)").ok
    assert report.ok


def test_auth_ok_but_search_forbidden_points_at_browse_access():
    """Distinguishes a credential problem from a missing API entitlement."""
    with respx.mock(base_url="https://api.ebay.com") as mock:
        mock.post("/identity/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        )
        mock.get("/buy/browse/v1/item_summary/search").mock(
            return_value=httpx.Response(
                403, json={"errors": [{"message": "Insufficient permissions"}]}
            )
        )
        report = check_ebay(_settings(), live=True)
    assert _named(report, "OAuth token").ok
    check = _named(report, "Search (no filters)")
    assert check.ok is False
    assert "Browse API" in check.fix


def test_pricecharting_missing_token():
    report = check_pricecharting(Settings(pricecharting_token=None), live=False)
    assert report.first_failure is not None


# --- the page --------------------------------------------------------------
@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_diagnostics_page_renders_without_live_calls(client):
    response = client.get("/diagnostics?live=false")
    assert response.status_code == 200
    assert "Diagnostics" in response.text
    assert "Credentials present" in response.text


def test_diagnostics_page_does_not_print_secrets(client, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET)
    get_settings.cache_clear()
    body = client.get("/diagnostics?live=false").text
    assert PROD_SECRET not in body
    assert PROD_ID not in body


def test_diagnostics_is_linked_from_every_page(client):
    assert "/diagnostics" in client.get("/scans").text


# --- cross-keyset pairing --------------------------------------------------
def test_app_id_and_cert_id_from_different_keysets_is_caught(monkeypatch):
    """Each value is individually well formed; only the pairing is wrong."""
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "SBX-1a2b3c4d5e6f-7a8b-9c0d-1e2f")
    monkeypatch.setenv("EBAY_ENV", "production")
    get_settings.cache_clear()
    check = _named(check_ebay(get_settings(), live=False), "App ID and Cert ID are the same keyset")
    assert check.ok is False
    assert "different keysets" in check.fix


def test_matched_keyset_pair_passes(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET)
    monkeypatch.setenv("EBAY_ENV", "production")
    get_settings.cache_clear()
    report = check_ebay(get_settings(), live=False)
    assert _named(report, "App ID and Cert ID are the same keyset").ok


def test_auth_failure_offers_a_way_to_reproduce_outside_the_app():
    with respx.mock(base_url="https://api.ebay.com") as mock:
        mock.post("/identity/v1/oauth2/token").mock(
            return_value=httpx.Response(401, json={"error": "invalid_client"})
        )
        check = _named(check_ebay(_settings(), live=True), "OAuth token")
    assert "curl" in check.fix
    assert "identity/v1/oauth2/token" in check.fix
    assert "not enabled for production" in check.fix
