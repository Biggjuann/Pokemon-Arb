from __future__ import annotations

import pytest

from pokemon_arb.config import Settings
from pokemon_arb.money import fmt, pct, to_cents


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12.34", 1234),
        ("$1,234.50", 123450),
        (12.34, 1234),
        (12, 1200),
        ("", None),
        (None, None),
        ("not a price", None),
        ("0.01", 1),
    ],
)
def test_to_cents(value, expected):
    assert to_cents(value) == expected


def test_to_cents_rounds_half_even_without_float_drift():
    assert to_cents("0.1") == 10
    assert to_cents("19.99") == 1999
    assert to_cents("1000000.00") == 100_000_000


def test_fmt():
    assert fmt(123450) == "$1,234.50"
    assert fmt(-500) == "-$5.00"
    assert fmt(None) == "--"
    assert fmt(0) == "$0.00"


def test_pct():
    assert pct(0.4635) == "46%"
    assert pct(0.4635, 1) == "46.4%"
    assert pct(0.5, 1) == "50.0%"
    assert pct(None) == "--"


def test_postgres_urls_are_normalized():
    assert Settings(database_url="postgres://u:p@h:5432/d").sqlalchemy_url.startswith(
        "postgresql+psycopg://"
    )
    assert Settings(database_url="postgresql://u:p@h:5432/d").sqlalchemy_url.startswith(
        "postgresql+psycopg://"
    )


def test_sqlite_url_is_left_alone():
    assert Settings(database_url="sqlite:///./x.db").sqlalchemy_url == "sqlite:///./x.db"


def test_credential_detection():
    assert not Settings(ebay_client_id=None, ebay_client_secret=None).has_ebay_credentials
    assert not Settings(ebay_client_id="a", ebay_client_secret=None).has_ebay_credentials
    assert Settings(ebay_client_id="a", ebay_client_secret="b").has_ebay_credentials
