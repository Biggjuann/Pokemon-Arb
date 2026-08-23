"""Decide whether an eBay listing really is a given PriceCharting product.

Confidence is the single most important number in this app: valuing a
listing against the wrong comp is how you talk yourself into buying a
proxy for the price of a real Charizard. The matcher is deliberately
strict -- it would rather return "unsure" than a confident wrong answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..models import Product
from .normalize import ParsedTitle, parse_product_name, parse_title

# Weights sum to 1.0 before penalties are applied.
W_CARD_NUMBER = 0.40
W_NAME = 0.40
W_SET = 0.20

# Bracketed variants that materially change a card's value. If the comp says
# one and the title says the other, that is a different card.
_VALUE_VARIANTS = {"first_edition", "shadowless", "reverse_holo", "promo"}


@dataclass
class MatchResult:
    product: Product | None
    confidence: float
    reasons: list[str] = field(default_factory=list)
    rejected: bool = False

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


def _set_similarity(parsed: ParsedTitle, product: Product) -> tuple[float, str]:
    """How strongly does the title corroborate the product's set?"""
    set_tokens = [t for t in parse_product_name(product.set_name)[0].split() if len(t) > 2]
    if not set_tokens:
        return 0.5, "set unknown"
    title_blob = " ".join(parsed.tokens)
    hits = [t for t in set_tokens if t in title_blob]
    if len(hits) == len(set_tokens):
        return 1.0, f"set '{product.set_name}' named in title"
    if hits:
        return 0.55 + 0.35 * (
            len(hits) / len(set_tokens)
        ), f"set partially named ({', '.join(hits)})"
    # Sellers routinely omit the set name; absence is weak evidence, not proof.
    ratio = fuzz.token_set_ratio(" ".join(set_tokens), title_blob) / 100.0
    return min(0.5, ratio), "set not named in title"


def score_pair(parsed: ParsedTitle, product: Product) -> MatchResult:
    """Score one listing/product pairing in [0, 1]."""
    reasons: list[str] = []
    product_name, product_number, product_variants = parse_product_name(product.name)
    if product.card_number and not product_number:
        product_number = product.card_number

    # --- card number -------------------------------------------------
    if product_number and parsed.card_numbers:
        if product_number == parsed.card_numbers[0]:
            number_score, note = 1.0, f"card number {product_number} matches"
        elif product_number in parsed.card_numbers:
            number_score, note = 0.85, f"card number {product_number} present in title"
        else:
            number_score, note = (
                0.0,
                (
                    f"card number mismatch (comp #{product_number}, "
                    f"title #{'/'.join(parsed.card_numbers[:2])})"
                ),
            )
    elif product_number and not parsed.card_numbers:
        number_score, note = 0.45, "no card number in title"
    else:
        number_score, note = 0.5, "comp has no card number"
    reasons.append(note)

    # --- card name ----------------------------------------------------
    title_name = " ".join(parsed.name_tokens)
    name_score = fuzz.token_set_ratio(product_name, title_name) / 100.0
    # token_set_ratio is generous with substrings: "charizard" scores 100
    # against "charizard vmax". Require the comp's distinctive tokens to
    # actually appear before trusting it.
    comp_tokens = [t for t in product_name.split() if len(t) > 2]
    if comp_tokens:
        present = sum(1 for t in comp_tokens if t in title_name)
        coverage = present / len(comp_tokens)
        name_score = min(name_score, 0.55 + 0.45 * coverage)
        reasons.append(f"name coverage {present}/{len(comp_tokens)}")
    else:
        reasons.append("comp name empty")

    # --- set ------------------------------------------------------------
    set_score, set_note = _set_similarity(parsed, product)
    reasons.append(set_note)

    confidence = W_CARD_NUMBER * number_score + W_NAME * name_score + W_SET * set_score

    # --- variant agreement ----------------------------------------------
    comp_value_variants = product_variants & _VALUE_VARIANTS
    title_value_variants = parsed.variants & _VALUE_VARIANTS
    missing = comp_value_variants - title_value_variants
    extra = title_value_variants - comp_value_variants
    if missing:
        confidence *= 0.72
        reasons.append(f"comp is {'/'.join(sorted(missing))} but title is not")
    if extra:
        confidence *= 0.72
        reasons.append(f"title is {'/'.join(sorted(extra))} but comp is not")

    # --- hard rejects -----------------------------------------------------
    rejected = False
    if number_score == 0.0:
        rejected = True
    if parsed.language and parsed.language != "english":
        # A Japanese card is a different market with different comps.
        rejected = True
        reasons.append(f"{parsed.language} card, English comp")
    if parsed.counterfeit_words:
        rejected = True
        reasons.append(f"counterfeit signal: {', '.join(parsed.counterfeit_words)}")
    if parsed.lot_words:
        rejected = True
        reasons.append(f"lot/bundle listing: {', '.join(parsed.lot_words)}")
    if parsed.quantity > 1:
        rejected = True
        reasons.append(f"multi-quantity listing (x{parsed.quantity})")

    if rejected:
        confidence = min(confidence, 0.2)

    return MatchResult(
        product=product,
        confidence=round(max(0.0, min(1.0, confidence)), 4),
        reasons=reasons,
        rejected=rejected,
    )


def best_match(
    title: str, candidates: list[Product], *, parsed: ParsedTitle | None = None
) -> MatchResult:
    """Pick the highest-confidence product for a listing title."""
    parsed = parsed or parse_title(title)
    best = MatchResult(product=None, confidence=0.0, reasons=["no candidates"], rejected=True)
    for product in candidates:
        result = score_pair(parsed, product)
        if result.confidence > best.confidence:
            best = result
    return best
