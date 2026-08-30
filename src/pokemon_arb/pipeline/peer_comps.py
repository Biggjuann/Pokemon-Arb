"""Valuing a listing against its peers, using eBay alone.

eBay's sold-price data is out of reach: Marketplace Insights is a Limited
Release API closed to new applicants, and Browse returns active listings only.
So with no second data source the only available signal is what everyone else
is currently *asking* for the same card.

That is a genuinely weaker signal than sold comps and the code says so:

  * Asking prices skew high. Sellers list optimistically and the listings that
    actually clear sit toward the bottom of the distribution, so the comp is a
    low percentile of the asks rather than the median.
  * A listing never contributes to the comp it is judged against. Otherwise the
    bargain you are hunting drags down its own reference price, and the
    cheapest listing in a thin group always looks fair.
  * Grades are priced separately. A PSA 10 in the same search must not pull up
    the raw comp, nor a raw card pull the slab comp down.
  * Below a minimum sample the group is not priced at all. A "median" of three
    asks is noise, and noise here means buying something.
"""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from ..matching.normalize import ParsedTitle
from ..sources.ebay import EbayListing

log = logging.getLogger(__name__)


def percentile(values: list[int], fraction: float) -> int:
    """Linear-interpolated percentile of a sorted copy of ``values``."""
    if not values:
        raise ValueError("percentile of an empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight)


def grade_segment(parsed: ParsedTitle) -> str:
    """Which price bracket a listing belongs to.

    Raw and slabbed copies of one card are different markets; pooling them
    produces a comp that describes neither.
    """
    if not parsed.is_graded:
        return "raw"
    if parsed.grading_company == "UNKNOWN" or parsed.grade is None:
        return "slab-unverified"
    return f"{parsed.grading_company} {parsed.grade}"


@dataclass
class PeerGroup:
    """One card, at one grade, as seen across a single search."""

    segment: str
    card_number: str | None
    prices: list[int] = field(default_factory=list)

    @property
    def sample_size(self) -> int:
        return len(self.prices)

    def comp_excluding(self, price_cents: int, fraction: float) -> int | None:
        """The peer comp for one listing, with that listing left out.

        Leaving it in lets a bargain lower the very number it is measured
        against, which is how a naive implementation talks itself into every
        cheap listing being fair.
        """
        others = list(self.prices)
        with contextlib.suppress(ValueError):  # the price always came from this group
            others.remove(price_cents)
        if not others:
            return None
        return percentile(others, fraction)


def group_key(parsed: ParsedTitle) -> tuple[str, str | None]:
    """Peers are grouped within one search, by grade and card number.

    Grouping is deliberately scoped to a single target: the query already names
    the card, so two different cards that happen to share a number cannot be
    pooled the way they could across a whole catalog.
    """
    number = parsed.card_numbers[0] if parsed.card_numbers else None
    return grade_segment(parsed), number


def build_groups(
    items: list[tuple[EbayListing, ParsedTitle]],
) -> dict[tuple[str, str | None], PeerGroup]:
    groups: dict[tuple[str, str | None], PeerGroup] = {}
    by_key: dict[tuple[str, str | None], list[int]] = defaultdict(list)
    for listing, parsed in items:
        key = group_key(parsed)
        by_key[key].append(listing.price_cents + (listing.shipping_cents or 0))
    for key, prices in by_key.items():
        segment, number = key
        groups[key] = PeerGroup(segment=segment, card_number=number, prices=prices)
    return groups
