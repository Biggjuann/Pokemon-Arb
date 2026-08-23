from __future__ import annotations

from pokemon_arb.matching.matcher import best_match, score_pair
from pokemon_arb.matching.normalize import parse_title
from pokemon_arb.models import Product


def test_exact_match_is_confident(charizard):
    parsed = parse_title("Pokemon Charizard 4/102 Base Set Holo Rare WOTC 1999 NM")
    result = score_pair(parsed, charizard)
    assert result.confidence >= 0.95
    assert not result.rejected


def test_card_number_mismatch_is_rejected(charizard):
    parsed = parse_title("Pokemon Charizard V 154/172 Brilliant Stars Alt Art")
    result = score_pair(parsed, charizard)
    assert result.rejected
    assert result.confidence <= 0.2


def test_missing_set_name_lowers_but_does_not_reject(charizard):
    with_set = score_pair(parse_title("Charizard 4/102 Base Set Holo"), charizard)
    without_set = score_pair(parse_title("Charizard 4/102 Holo Rare"), charizard)
    assert without_set.confidence < with_set.confidence
    assert not without_set.rejected


def test_lot_listing_is_rejected(charizard):
    result = score_pair(parse_title("Pokemon Lot 50 Cards Charizard 4/102 Base Set"), charizard)
    assert result.rejected
    assert any("lot" in reason for reason in result.reasons)


def test_japanese_listing_is_rejected_against_english_comp(charizard):
    result = score_pair(parse_title("Japanese Charizard 4/102 Base Set"), charizard)
    assert result.rejected


def test_proxy_listing_is_rejected(charizard):
    result = score_pair(parse_title("Charizard 4/102 Base Set Custom Proxy"), charizard)
    assert result.rejected


def test_variant_mismatch_is_penalized():
    first_ed = Product(
        pc_id="a", name="Charizard [1st Edition] #4", set_name="Pokemon Base Set", card_number="4"
    )
    unlimited = Product(
        pc_id="b", name="Charizard #4", set_name="Pokemon Base Set", card_number="4"
    )
    parsed = parse_title("Charizard 4/102 Base Set Unlimited Holo")
    assert score_pair(parsed, first_ed).confidence < score_pair(parsed, unlimited).confidence


def test_best_match_picks_the_right_card_among_candidates():
    candidates = [
        Product(pc_id="a", name="Charizard #4", set_name="Pokemon Base Set", card_number="4"),
        Product(pc_id="b", name="Blastoise #2", set_name="Pokemon Base Set", card_number="2"),
        Product(
            pc_id="c",
            name="Umbreon VMAX (Alternate Art) #215",
            set_name="Pokemon Evolving Skies",
            card_number="215",
        ),
    ]
    result = best_match("Umbreon VMAX Alt Art 215/203 Evolving Skies NM", candidates)
    assert result.product.pc_id == "c"
    assert result.confidence >= 0.85


def test_best_match_with_no_candidates():
    result = best_match("Charizard 4/102", [])
    assert result.product is None
    assert result.rejected


def test_name_coverage_guards_against_substring_inflation():
    """'Charizard' must not score as a confident match for 'Charizard VMAX'."""
    vmax = Product(
        pc_id="d",
        name="Charizard VMAX (Rainbow) #74",
        set_name="Pokemon Champions Path",
        card_number="74",
    )
    parsed = parse_title("Charizard 74/73 Champions Path")
    assert score_pair(parsed, vmax).confidence < 0.9
