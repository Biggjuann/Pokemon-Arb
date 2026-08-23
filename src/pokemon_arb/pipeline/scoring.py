"""Deal economics and risk.

Turning "this listing is cheaper than the comp" into "this is worth buying"
means paying for the three things that quietly eat the spread: sales tax on
the way in, eBay's cut on the way out, and the chance the card is not what
the title says it is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..clock import utcnow
from ..config import Settings
from ..matching.matcher import MatchResult
from ..matching.normalize import ParsedTitle
from ..models import Listing, Product

# flag -> (penalty, human explanation). Penalties are additive, capped at 0.95.
RISK_WEIGHTS: dict[str, tuple[float, str]] = {
    "TOO_GOOD_TO_BE_TRUE": (
        0.55,
        "Priced far below comp -- usually a proxy, reprint or mis-listing",
    ),
    "COUNTERFEIT_WORDS": (0.90, "Title contains proxy/custom/replica wording"),
    "LOT_LISTING": (0.90, "Lot, bundle or pick-your-card listing, not a single card"),
    "NOT_A_CARD": (0.70, "Title suggests sealed product, code card or an accessory"),
    "DAMAGE_WORDS": (0.35, "Title admits damage (crease, water, played)"),
    "HEDGE_LANGUAGE": (0.20, "'Read description' / 'as is' -- undisclosed condition issues"),
    "LOW_CONFIDENCE_MATCH": (0.30, "Weak evidence the listing is this exact card"),
    "GRADE_ASSUMED": (0.25, "Graded slab with no comp for that grade; valued conservatively"),
    "UNKNOWN_GRADER": (0.30, "Slab from an unnamed or low-trust grading company"),
    "THIN_COMPS": (0.30, "Very few recent sales -- the market value is noisy"),
    "STALE_COMPS": (0.15, "Comp prices have not been refreshed recently"),
    "NEW_SELLER": (0.25, "Seller has almost no feedback history"),
    "LOW_FEEDBACK": (0.20, "Seller feedback below 98%"),
    "NO_RETURNS": (0.10, "Seller does not accept returns"),
    "INTERNATIONAL": (0.10, "Ships from outside the target country"),
    "AUCTION": (0.15, "Auction listing -- the price shown is not the price you pay"),
}

STALE_AFTER = dt.timedelta(days=14)


@dataclass
class DealEconomics:
    market_value_cents: int
    adjusted_value_cents: int
    comp_label: str
    landed_cost_cents: int
    total_cost_cents: int
    net_proceeds_cents: int
    profit_cents: int
    roi: float
    discount_pct: float
    risk_flags: list[str] = field(default_factory=list)
    risk_penalty: float = 0.0
    liquidity: float = 1.0
    score: float = 0.0
    confidence: float = 0.0

    @property
    def qualifies(self) -> bool:
        return self._qualifies

    _qualifies: bool = True


def select_comp(parsed: ParsedTitle, product: Product) -> tuple[int | None, str, list[str]]:
    """Pick the comp price that matches how the card is actually being sold.

    A PSA 10 slab should be valued against the PSA 10 comp, not the raw one.
    """
    flags: list[str] = []
    if not parsed.is_graded:
        return product.ungraded_cents, "Ungraded", flags

    company = parsed.grading_company or "UNKNOWN"
    grade = parsed.grade

    if company == "UNKNOWN" or grade is None:
        flags.append("UNKNOWN_GRADER")
        return product.ungraded_cents, "Ungraded (unverified slab)", flags
    if company not in {"PSA", "BGS", "CGC", "SGC"}:
        flags.append("UNKNOWN_GRADER")

    ladder: list[tuple[str, int | None, str]] = [
        ("10", product.psa10_cents, "PSA 10"),
        ("9.5", product.grade95_cents, "Grade 9.5"),
        ("9", product.grade9_cents, "Grade 9"),
        ("8", product.grade8_cents, "Grade 8"),
        ("7", product.grade7_cents, "Grade 7"),
    ]
    exact = {g: (cents, label) for g, cents, label in ladder}
    if grade in exact and exact[grade][0]:
        cents, label = exact[grade]
        # Only PSA reliably clears the PSA 10 comp; discount other slabs.
        if grade == "10" and company != "PSA":
            flags.append("GRADE_ASSUMED")
            return int(cents * 0.85), f"{label} x0.85 ({company} 10)", flags
        return cents, f"{company} {grade}", flags

    # Grade below 7, or no comp at that grade: fall back to the nearest lower
    # rung we do have, then the raw price.
    flags.append("GRADE_ASSUMED")
    for _, cents, label in reversed(ladder):
        if cents:
            return cents, f"{label} (nearest comp)", flags
    return product.ungraded_cents, "Ungraded (fallback)", flags


def liquidity_factor(sales_volume: int | None) -> float:
    """How easily can this card be resold? Scales expected profit."""
    if not sales_volume:
        return 0.65
    return round(min(1.0, 0.5 + 0.5 * min(1.0, sales_volume / 40.0)), 4)


def collect_risk_flags(
    parsed: ParsedTitle,
    listing: Listing,
    product: Product,
    match: MatchResult,
    discount_pct: float,
    settings: Settings,
    extra: list[str] | None = None,
) -> list[str]:
    flags: list[str] = list(extra or [])

    if parsed.counterfeit_words:
        flags.append("COUNTERFEIT_WORDS")
    if parsed.lot_words or parsed.quantity > 1:
        flags.append("LOT_LISTING")
    if parsed.not_a_card_words:
        flags.append("NOT_A_CARD")
    if parsed.damage_words:
        flags.append("DAMAGE_WORDS")
    if parsed.hedge_phrases:
        flags.append("HEDGE_LANGUAGE")

    if discount_pct >= settings.too_good_to_be_true_discount:
        flags.append("TOO_GOOD_TO_BE_TRUE")
    if match.confidence < 0.75:
        flags.append("LOW_CONFIDENCE_MATCH")

    if (product.sales_volume or 0) < settings.min_sales_volume:
        flags.append("THIN_COMPS")
    if product.last_synced_at is None or (utcnow() - product.last_synced_at > STALE_AFTER):
        flags.append("STALE_COMPS")

    if (listing.seller_feedback_score or 0) < 50:
        flags.append("NEW_SELLER")
    if listing.seller_feedback_pct is not None and listing.seller_feedback_pct < 98.0:
        flags.append("LOW_FEEDBACK")
    if listing.returns_accepted is False:
        flags.append("NO_RETURNS")
    if listing.item_location and listing.item_location.upper() not in (
        settings.ebay_delivery_country,
        "",
    ):
        flags.append("INTERNATIONAL")
    if listing.buying_format and listing.buying_format != "FIXED_PRICE":
        flags.append("AUCTION")

    # Preserve order, drop duplicates.
    seen: set[str] = set()
    return [f for f in flags if not (f in seen or seen.add(f))]


def risk_penalty(flags: list[str]) -> float:
    total = sum(RISK_WEIGHTS.get(f, (0.1, ""))[0] for f in flags)
    return round(min(0.95, total), 4)


def evaluate(
    listing: Listing,
    product: Product,
    match: MatchResult,
    parsed: ParsedTitle,
    settings: Settings,
) -> DealEconomics:
    """Full buy-side and sell-side math for one listing/product pairing."""
    comp_cents, comp_label, comp_flags = select_comp(parsed, product)
    market_value = comp_cents or 0
    adjusted_value = round(market_value * settings.condition_haircut)

    landed = listing.price_cents + (listing.shipping_cents or 0)
    total_cost = round(landed * (1 + settings.buyer_tax_rate))

    gross = adjusted_value
    fees = round(gross * settings.sell_fee_rate) + settings.sell_fee_fixed_cents
    net_proceeds = gross - fees - settings.outbound_shipping_cents

    profit = net_proceeds - total_cost
    roi = round(profit / total_cost, 4) if total_cost > 0 else 0.0
    discount = round(1 - (landed / market_value), 4) if market_value > 0 else 0.0

    flags = collect_risk_flags(
        parsed, listing, product, match, discount, settings, extra=comp_flags
    )
    penalty = risk_penalty(flags)
    liquidity = liquidity_factor(product.sales_volume)

    # Rank by risk-adjusted expected profit, in dollars. Every term is
    # something a buyer can point at and argue with.
    score = round((profit / 100.0) * match.confidence * (1 - penalty) * liquidity, 2)

    econ = DealEconomics(
        market_value_cents=market_value,
        adjusted_value_cents=adjusted_value,
        comp_label=comp_label,
        landed_cost_cents=landed,
        total_cost_cents=total_cost,
        net_proceeds_cents=net_proceeds,
        profit_cents=profit,
        roi=roi,
        discount_pct=discount,
        risk_flags=flags,
        risk_penalty=penalty,
        liquidity=liquidity,
        score=score,
        confidence=match.confidence,
    )
    econ._qualifies = (
        market_value > 0
        and not match.rejected
        and match.confidence >= settings.min_match_confidence
        and profit >= settings.min_profit_cents
        and roi >= settings.min_roi
        and score > 0
    )
    return econ


def max_actionable_price_cents(product: Product, settings: Settings) -> int | None:
    """The highest landed price that could still clear the profit/ROI bars.

    Used as the eBay ``price`` filter ceiling so the API only returns
    listings that are already candidates. Without it, most of the call
    budget is spent pulling in fairly-priced cards.
    """
    if not product.ungraded_cents:
        return None
    gross = product.ungraded_cents * settings.condition_haircut
    net = (
        gross * (1 - settings.sell_fee_rate)
        - settings.sell_fee_fixed_cents
        - settings.outbound_shipping_cents
    )
    if net <= 0:
        return None
    by_profit = (net - settings.min_profit_cents) / (1 + settings.buyer_tax_rate)
    by_roi = net / ((1 + settings.buyer_tax_rate) * (1 + settings.min_roi))
    ceiling = int(min(by_profit, by_roi))
    return ceiling if ceiling > 0 else None
