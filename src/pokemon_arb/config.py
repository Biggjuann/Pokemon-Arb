"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _clean_secret(value: object) -> object:
    """Trim whitespace and quote marks off a pasted credential.

    A trailing newline from a copy-paste changes the Basic auth header and
    eBay answers 401 invalid_client -- identical to a genuinely wrong key,
    and far harder to spot.
    """
    if isinstance(value, str):
        return value.strip().strip("\"'") or None
    return value


Secret = Annotated[str | None, BeforeValidator(_clean_secret)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix=""
    )

    # --- storage -------------------------------------------------------
    # Relative to the working directory, not the source tree: once the package
    # is pip-installed the source lives in site-packages, which is the wrong
    # place (and often not writable) for a database.
    database_url: str = "sqlite:///./data/pokearb.db"

    # --- eBay Browse API ----------------------------------------------
    ebay_client_id: Secret = None
    ebay_client_secret: Secret = None
    # "production" or "sandbox"
    ebay_env: str = "production"
    ebay_marketplace: str = "EBAY_US"
    # Pokemon "Individual Cards" leaf category.
    ebay_category_id: str = "183454"
    ebay_delivery_country: str = "US"
    # Browse API allows 5k calls/day on the default keyset; stay well under.
    ebay_max_calls_per_scan: int = 400

    # --- PriceCharting -------------------------------------------------
    pricecharting_token: Secret = None

    # --- deal economics (all rates are fractions of 1.0) ---------------
    # eBay final value fee for trading cards (incl. payment processing).
    sell_fee_rate: float = 0.1355
    # Fixed per-order fee eBay charges the seller, in cents.
    sell_fee_fixed_cents: int = 40
    # What it costs you to ship the card out when you resell it.
    outbound_shipping_cents: int = 500
    # Sales tax you pay as the buyer, applied to item + inbound shipping.
    buyer_tax_rate: float = 0.08
    # Haircut on PriceCharting "ungraded" comps: raw cards in unknown
    # condition realistically clear below the running average.
    condition_haircut: float = 0.90

    # --- ranking thresholds --------------------------------------------
    min_profit_cents: int = 1000
    min_roi: float = 0.25
    # Discounts beyond this are usually proxies, reprints or mis-listings.
    too_good_to_be_true_discount: float = 0.90
    min_match_confidence: float = 0.62
    # Ignore comps with almost no recent sales -- the "market value" is noise.
    min_sales_volume: int = 3

    # --- data freshness --------------------------------------------------
    # eBay's API License Agreement 8.1(c) caps displayed listing data at six
    # hours old. This may be tightened but is clamped to 360 on the way out --
    # see freshness.MAX_DISPLAY_AGE.
    listing_freshness_minutes: int = 360

    # --- scanning ------------------------------------------------------
    scan_top_cards_per_set: int = 25
    scan_listings_per_target: int = 50
    scan_max_price_cents: int = 100_000
    demo_mode: bool = False

    # --- deployment -----------------------------------------------------
    port: int = 8000
    host: str = "0.0.0.0"
    # Set on Railway to run the scan loop inside the web process. 0 disables.
    scan_interval_minutes: int = 0
    seed_demo_on_startup: bool = False

    @property
    def sqlalchemy_url(self) -> str:
        """Normalize provider-supplied URLs to a driver SQLAlchemy accepts.

        Railway (and Heroku) hand out ``postgres://`` / ``postgresql://``;
        SQLAlchemy 2 needs an explicit driver, and psycopg3 is what we ship.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
        return url

    @property
    def ebay_api_base(self) -> str:
        return (
            "https://api.sandbox.ebay.com" if self.ebay_env == "sandbox" else "https://api.ebay.com"
        )

    @property
    def has_ebay_credentials(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
