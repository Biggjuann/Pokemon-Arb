"""eBay Browse API client -- the supply side of the trade.

Uses the official Browse API with an application (client-credentials) token.
That keeps the app inside eBay's terms and gives structured fields (seller
feedback, shipping cost, buying format) that scraping a search page would
not reliably provide.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..clock import utcnow
from ..money import to_cents

log = logging.getLogger(__name__)

OAUTH_PATH = "/identity/v1/oauth2/token"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"
APP_SCOPE = "https://api.ebay.com/oauth/api_scope"
MAX_LIMIT = 200


class EbayError(RuntimeError):
    pass


class EbayAuthError(EbayError):
    pass


@dataclass
class EbayListing:
    """Normalized view of one Browse API item summary."""

    ebay_item_id: str
    title: str
    url: str
    price_cents: int
    shipping_cents: int
    currency: str = "USD"
    buying_format: str = "FIXED_PRICE"
    condition: str | None = None
    image_url: str | None = None
    seller_username: str | None = None
    seller_feedback_pct: float | None = None
    seller_feedback_score: int | None = None
    top_rated_seller: bool = False
    returns_accepted: bool | None = None
    item_location: str | None = None
    listed_at: dt.datetime | None = None

    @classmethod
    def from_summary(cls, item: dict[str, Any]) -> EbayListing | None:
        item_id = item.get("itemId")
        price = (item.get("price") or {}).get("value")
        if not item_id or price is None:
            return None
        price_cents = to_cents(price)
        if price_cents is None:
            return None

        shipping_cents = 0
        options = item.get("shippingOptions") or []
        if options:
            cost = (options[0].get("shippingCost") or {}).get("value")
            shipping_cents = to_cents(cost) or 0

        buying_options = item.get("buyingOptions") or ["FIXED_PRICE"]
        # A listing that is both auction and BIN is treated as BIN, which is
        # the price we would actually transact at.
        buying_format = "FIXED_PRICE" if "FIXED_PRICE" in buying_options else buying_options[0]

        seller = item.get("seller") or {}
        feedback_pct = seller.get("feedbackPercentage")
        try:
            feedback_pct = float(feedback_pct) if feedback_pct is not None else None
        except (TypeError, ValueError):
            feedback_pct = None

        listed_at = None
        if raw_date := item.get("itemCreationDate"):
            try:
                listed_at = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
            except ValueError:
                listed_at = None

        return cls(
            ebay_item_id=str(item_id),
            title=(item.get("title") or "").strip(),
            url=item.get("itemWebUrl") or "",
            price_cents=price_cents,
            shipping_cents=shipping_cents,
            currency=(item.get("price") or {}).get("currency", "USD"),
            buying_format=buying_format,
            condition=item.get("condition"),
            image_url=(item.get("image") or {}).get("imageUrl"),
            seller_username=seller.get("username"),
            seller_feedback_pct=feedback_pct,
            seller_feedback_score=seller.get("feedbackScore"),
            top_rated_seller=bool(item.get("topRatedBuyingExperience")),
            returns_accepted=None,
            item_location=(item.get("itemLocation") or {}).get("country"),
            listed_at=listed_at,
        )


class EbayClient:
    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        *,
        api_base: str = "https://api.ebay.com",
        marketplace: str = "EBAY_US",
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        self.marketplace = marketplace
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at = dt.datetime(1970, 1, 1)
        self.call_count = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> EbayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- auth ---------------------------------------------------------
    def _fetch_token(self) -> str:
        if not (self.client_id and self.client_secret):
            raise EbayAuthError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set. "
                "Create an app keyset at developer.ebay.com, or run with --demo."
            )
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        response = self._client.post(
            f"{self.api_base}{OAUTH_PATH}",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": APP_SCOPE},
        )
        if response.status_code != 200:
            raise EbayAuthError(
                f"eBay token request failed ({response.status_code}): {response.text[:300]}"
            )
        payload = response.json()
        self._token = payload["access_token"]
        # Refresh a minute early so a long scan never trips over expiry.
        self._token_expires_at = utcnow() + dt.timedelta(
            seconds=int(payload.get("expires_in", 7200)) - 60
        )
        return self._token

    def token(self) -> str:
        if self._token and utcnow() < self._token_expires_at:
            return self._token
        return self._fetch_token()

    # --- search -------------------------------------------------------
    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        reraise=True,
    )
    def _search_page(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._client.get(
            f"{self.api_base}{SEARCH_PATH}",
            params=params,
            headers={
                "Authorization": f"Bearer {self.token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
                "Accept": "application/json",
            },
        )
        self.call_count += 1
        if response.status_code == 401:
            self._token = None
            raise EbayAuthError("eBay rejected the access token (401)")
        if response.status_code == 429:
            raise EbayError("eBay rate limit reached (429) -- back off and retry later")
        if response.status_code >= 400:
            raise EbayError(f"eBay search failed ({response.status_code}): {response.text[:300]}")
        return response.json()

    @staticmethod
    def build_filter(
        *,
        max_price_cents: int | None = None,
        min_price_cents: int | None = None,
        fixed_price_only: bool = True,
        item_location_country: str | None = None,
        delivery_country: str | None = None,
    ) -> str:
        parts: list[str] = []
        if fixed_price_only:
            parts.append("buyingOptions:{FIXED_PRICE}")
        if max_price_cents is not None or min_price_cents is not None:
            low = f"{(min_price_cents or 0) / 100:.2f}"
            high = f"{max_price_cents / 100:.2f}" if max_price_cents else ""
            parts.append(f"price:[{low}..{high}]")
            parts.append("priceCurrency:USD")
        if item_location_country:
            parts.append(f"itemLocationCountry:{item_location_country}")
        if delivery_country:
            parts.append(f"deliveryCountry:{delivery_country}")
        return ",".join(parts)

    def search(
        self,
        query: str,
        *,
        category_ids: str | None = None,
        limit: int = 50,
        filter_string: str | None = None,
        sort: str | None = "price",
        max_pages: int = 1,
    ) -> Iterator[EbayListing]:
        """Yield listings for a query, cheapest first by default."""
        offset = 0
        remaining = limit
        for _ in range(max_pages):
            if remaining <= 0:
                return
            params: dict[str, Any] = {
                "q": query,
                "limit": min(remaining, MAX_LIMIT),
                "offset": offset,
            }
            if category_ids:
                params["category_ids"] = category_ids
            if filter_string:
                params["filter"] = filter_string
            if sort:
                params["sort"] = sort

            payload = self._search_page(params)
            summaries = payload.get("itemSummaries") or []
            if not summaries:
                return
            for item in summaries:
                listing = EbayListing.from_summary(item)
                if listing:
                    yield listing
            count = len(summaries)
            remaining -= count
            offset += count
            if offset >= int(payload.get("total", 0)):
                return
