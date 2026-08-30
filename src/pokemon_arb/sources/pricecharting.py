"""PriceCharting client -- the market-value side of the trade.

Two ways in:
  * the JSON API (``/api/products``), for looking up single cards, and
  * the subscriber CSV price guide, for pulling a whole category at once.

Beware the units: the JSON API returns prices as integer pennies, the CSV
guide returns dollar strings. Both funnel through ``_price`` below.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..clock import utcnow
from ..money import to_cents

log = logging.getLogger(__name__)

BASE_URL = "https://www.pricecharting.com"
DEFAULT_CATEGORY = "pokemon-cards"

# PriceCharting reuses its video-game price fields for trading cards.
# This mapping is the whole reason the numbers make sense.
GRADE_FIELDS = {
    "ungraded_cents": "loose-price",
    "grade7_cents": "cib-price",
    "grade8_cents": "new-price",
    "grade9_cents": "graded-price",
    "grade95_cents": "box-only-price",
    "psa10_cents": "manual-only-price",
}


class PriceChartingError(RuntimeError):
    pass


def _price(value: Any, *, pennies: bool) -> int | None:
    """Normalize a PriceCharting price field to integer cents."""
    if value in (None, "", "-"):
        return None
    if pennies:
        try:
            return int(value)
        except (TypeError, ValueError):
            return to_cents(str(value))
    return to_cents(str(value))


@dataclass
class PCProduct:
    external_id: str
    name: str
    set_name: str
    prices: dict[str, int | None] = field(default_factory=dict)
    sales_volume: int | None = None
    release_date: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, pennies: bool) -> PCProduct:
        prices = {
            field_name: _price(payload.get(source), pennies=pennies)
            for field_name, source in GRADE_FIELDS.items()
        }
        volume = payload.get("sales-volume")
        try:
            volume = int(float(volume)) if volume not in (None, "") else None
        except (TypeError, ValueError):
            volume = None
        return cls(
            external_id=str(payload.get("id", "")).strip(),
            name=str(payload.get("product-name", "")).strip(),
            set_name=str(payload.get("console-name", "")).strip(),
            prices=prices,
            sales_volume=volume,
            release_date=(payload.get("release-date") or None),
        )

    def to_model_kwargs(self) -> dict[str, Any]:
        from ..matching.normalize import parse_product_name

        name_tokens, number, _variants = parse_product_name(self.name)
        return {
            "external_id": self.external_id,
            "name": self.name,
            "set_name": self.set_name,
            "card_number": number,
            "release_date": self.release_date,
            "sales_volume": self.sales_volume,
            "search_blob": f"{name_tokens} {self.set_name.lower()}".strip(),
            "last_synced_at": utcnow(),
            **self.prices,
        }

    @property
    def is_card(self) -> bool:
        """Filter out sealed product rows that share the cards category."""
        lowered = self.name.lower()
        junk = ("booster box", "booster pack", "elite trainer", "sealed", "tin ", "bundle")
        return self.external_id != "" and not any(j in lowered for j in junk)


class PriceChartingClient:
    def __init__(
        self, token: str | None, *, client: httpx.Client | None = None, timeout: float = 30.0
    ):
        self.token = token
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PriceChartingClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_token(self) -> str:
        if not self.token:
            raise PriceChartingError(
                "PRICECHARTING_TOKEN is not set. Add it to .env, or run with --demo."
            )
        return self.token

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        response = self._client.get(f"{BASE_URL}{path}", params=params)
        response.raise_for_status()
        return response

    def search_products(self, query: str) -> list[PCProduct]:
        payload = self._get("/api/products", {"t": self._require_token(), "q": query}).json()
        if payload.get("status") == "error":
            raise PriceChartingError(payload.get("error-message", "unknown PriceCharting error"))
        return [PCProduct.from_payload(p, pennies=True) for p in payload.get("products", [])]

    def get_product(self, external_id: str) -> PCProduct | None:
        payload = self._get("/api/product", {"t": self._require_token(), "id": external_id}).json()
        if payload.get("status") == "error":
            return None
        return PCProduct.from_payload(payload, pennies=True)

    def iter_price_guide(
        self,
        category: str = DEFAULT_CATEGORY,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[PCProduct]:
        """Stream the full CSV price guide for a category (subscriber feature).

        Genuinely streamed, row by row. An earlier version buffered the whole
        response before yielding anything, which meant a cancel could not take
        effect for the entire download -- the longest part of the job, and the
        one most worth being able to stop.
        """
        params = {"t": self._require_token(), "category": category}
        with self._client.stream(
            "GET", f"{BASE_URL}/price-guide/download-custom", params=params
        ) as response:
            response.raise_for_status()
            for row in csv.DictReader(response.iter_lines()):
                if should_cancel is not None and should_cancel():
                    log.info("price guide download cancelled")
                    return
                product = PCProduct.from_payload(row, pennies=False)
                if product.is_card:
                    yield product

    @staticmethod
    def parse_price_guide_csv(text: str) -> list[PCProduct]:
        """Parse an already-downloaded price-guide CSV."""
        return [
            p
            for p in (
                PCProduct.from_payload(row, pennies=False)
                for row in csv.DictReader(io.StringIO(text))
            )
            if p.is_card
        ]
