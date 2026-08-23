from __future__ import annotations

from pokemon_arb.matching.matcher import score_pair
from pokemon_arb.matching.normalize import parse_title
from pokemon_arb.pipeline.scoring import (
    RISK_WEIGHTS,
    evaluate,
    liquidity_factor,
    max_actionable_price_cents,
    risk_penalty,
    select_comp,
)


def _evaluate(listing, product, settings):
    parsed = parse_title(listing.title)
    return evaluate(listing, product, score_pair(parsed, product), parsed, settings)


def test_economics_add_up(charizard, listing_factory, settings):
    listing = listing_factory(
        "Charizard 4/102 Base Set Holo NM", price_cents=20000, shipping_cents=500
    )
    econ = _evaluate(listing, charizard, settings)

    assert econ.landed_cost_cents == 20500
    assert econ.total_cost_cents == round(20500 * 1.08)
    assert econ.adjusted_value_cents == round(42000 * settings.condition_haircut)
    expected_fees = (
        round(econ.adjusted_value_cents * settings.sell_fee_rate) + settings.sell_fee_fixed_cents
    )
    assert econ.net_proceeds_cents == (
        econ.adjusted_value_cents - expected_fees - settings.outbound_shipping_cents
    )
    assert econ.profit_cents == econ.net_proceeds_cents - econ.total_cost_cents
    assert econ.roi == round(econ.profit_cents / econ.total_cost_cents, 4)


def test_fees_and_tax_can_flip_a_nominal_bargain(charizard, listing_factory, settings):
    """A listing under comp is not automatically profitable."""
    listing = listing_factory("Charizard 4/102 Base Set Holo NM", price_cents=32000)
    econ = _evaluate(listing, charizard, settings)
    assert econ.discount_pct > 0  # cheaper than the comp
    assert econ.profit_cents < 0  # but a loss after costs
    assert not econ.qualifies


def test_shipping_counts_toward_cost(charizard, listing_factory, settings):
    free = _evaluate(
        listing_factory("Charizard 4/102 Base Set", price_cents=20000), charizard, settings
    )
    paid = _evaluate(
        listing_factory("Charizard 4/102 Base Set NM", price_cents=20000, shipping_cents=2500),
        charizard,
        settings,
    )
    assert paid.total_cost_cents > free.total_cost_cents
    assert paid.profit_cents < free.profit_cents


def test_graded_slab_uses_the_graded_comp(charizard):
    parsed = parse_title("PSA 10 Charizard 4/102 Base Set")
    cents, label, flags = select_comp(parsed, charizard)
    assert cents == charizard.psa10_cents
    assert label == "PSA 10"
    assert not flags


def test_non_psa_ten_is_discounted(charizard):
    parsed = parse_title("CGC 10 Charizard 4/102 Base Set")
    cents, _label, flags = select_comp(parsed, charizard)
    assert cents == int(charizard.psa10_cents * 0.85)
    assert "GRADE_ASSUMED" in flags


def test_unverified_slab_falls_back_to_raw(charizard):
    parsed = parse_title("Graded Slab Charizard 4/102 Base Set")
    cents, _label, flags = select_comp(parsed, charizard)
    assert cents == charizard.ungraded_cents
    assert "UNKNOWN_GRADER" in flags


def test_ungraded_listing_uses_raw_comp(charizard):
    cents, label, flags = select_comp(parse_title("Charizard 4/102 Base Set NM"), charizard)
    assert cents == charizard.ungraded_cents
    assert label == "Ungraded"
    assert not flags


def test_absurd_discount_is_flagged(charizard, listing_factory, settings):
    listing = listing_factory("Charizard 4/102 Base Set Holo", price_cents=1500)
    econ = _evaluate(listing, charizard, settings)
    assert "TOO_GOOD_TO_BE_TRUE" in econ.risk_flags


def test_seller_risk_flags(charizard, listing_factory, settings):
    listing = listing_factory(
        "Charizard 4/102 Base Set Holo NM",
        price_cents=20000,
        seller_feedback_score=2,
        seller_feedback_pct=91.0,
        returns_accepted=False,
        item_location="HK",
    )
    econ = _evaluate(listing, charizard, settings)
    assert {"NEW_SELLER", "LOW_FEEDBACK", "NO_RETURNS", "INTERNATIONAL"} <= set(econ.risk_flags)


def test_risk_lowers_score_but_not_profit(charizard, listing_factory, settings):
    clean = _evaluate(
        listing_factory("Charizard 4/102 Base Set NM", price_cents=20000), charizard, settings
    )
    risky = _evaluate(
        listing_factory("Charizard 4/102 Base Set damaged crease as is", price_cents=20000),
        charizard,
        settings,
    )
    assert risky.profit_cents == clean.profit_cents
    assert risky.score < clean.score


def test_thin_comps_are_flagged(charizard, listing_factory, settings):
    charizard.sales_volume = 1
    econ = _evaluate(
        listing_factory("Charizard 4/102 Base Set NM", price_cents=20000), charizard, settings
    )
    assert "THIN_COMPS" in econ.risk_flags


def test_stale_comps_are_flagged(charizard, listing_factory, settings):
    charizard.last_synced_at = None
    econ = _evaluate(
        listing_factory("Charizard 4/102 Base Set NM", price_cents=20000), charizard, settings
    )
    assert "STALE_COMPS" in econ.risk_flags


def test_every_risk_flag_has_a_weight_and_explanation(charizard, listing_factory, settings):
    listing = listing_factory(
        "Japanese Charizard 4/102 lot proxy damaged as is sealed booster",
        price_cents=500,
        seller_feedback_score=1,
        seller_feedback_pct=80.0,
        returns_accepted=False,
        buying_format="AUCTION",
        item_location="JP",
    )
    econ = _evaluate(listing, charizard, settings)
    for flag in econ.risk_flags:
        assert flag in RISK_WEIGHTS, f"{flag} has no weight/explanation"
    assert econ.risk_penalty <= 0.95


def test_risk_penalty_is_capped():
    assert risk_penalty(list(RISK_WEIGHTS)) == 0.95


def test_liquidity_factor_scales_with_volume():
    assert liquidity_factor(None) == 0.65
    assert liquidity_factor(1) < liquidity_factor(20) < liquidity_factor(200)
    assert liquidity_factor(1000) == 1.0


def test_max_actionable_price_is_a_real_ceiling(charizard, listing_factory, settings):
    ceiling = max_actionable_price_cents(charizard, settings)
    assert ceiling is not None

    at_ceiling = _evaluate(
        listing_factory("Charizard 4/102 Base Set NM", price_cents=ceiling), charizard, settings
    )
    assert at_ceiling.qualifies

    above = _evaluate(
        listing_factory("Charizard 4/102 Base Set Holo", price_cents=ceiling + 2000),
        charizard,
        settings,
    )
    assert not above.qualifies


def test_no_comp_means_no_ceiling(charizard, settings):
    charizard.ungraded_cents = None
    assert max_actionable_price_cents(charizard, settings) is None


def test_rejected_match_never_qualifies(charizard, listing_factory, settings):
    econ = _evaluate(
        listing_factory("Pokemon Lot 50 Cards Charizard 4/102 Base Set", price_cents=5000),
        charizard,
        settings,
    )
    assert not econ.qualifies
