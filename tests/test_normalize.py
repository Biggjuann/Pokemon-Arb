from __future__ import annotations

import pytest

from pokemon_arb.matching.normalize import (
    extract_card_numbers,
    normalize_card_number,
    parse_product_name,
    parse_title,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("004/102", "4"),
        ("#4", "4"),
        ("TG12/TG30", "TG12"),
        ("SV49", "SV49"),
        ("SWSH284", "SWSH284"),
        ("149a", "149A"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_card_number(raw, expected):
    assert normalize_card_number(raw) == expected


def test_extract_ignores_years_grades_and_quantities():
    title = "Pokemon Charizard 4/102 Base Set WOTC 1999 PSA 10 ready x3"
    assert extract_card_numbers(title) == ["4"]


def test_extract_prefers_slash_and_hash_forms():
    assert extract_card_numbers("Umbreon VMAX 215/203 Evolving Skies")[0] == "215"
    assert extract_card_numbers("Pikachu Promo #025 holo")[0] == "25"


def test_extract_handles_set_codes():
    assert extract_card_numbers("Giratina V Alt Art TG12/TG30 Lost Origin") == ["TG12"]


def test_grader_prefix_is_not_a_card_number():
    assert "PSA10" not in extract_card_numbers("PSA10 Charizard")
    assert normalize_card_number("PSA10") is None


def test_parse_title_flags_hazards():
    parsed = parse_title("Pokemon Lot of 12 Cards Mystery Repack damaged crease read description")
    assert parsed.lot_words
    assert parsed.damage_words
    assert "read description" in parsed.hedge_phrases


def test_parse_title_detects_language_and_grade():
    parsed = parse_title("Japanese Charizard 4/102 BGS 9.5")
    assert parsed.language == "japanese"
    assert not parsed.is_english
    assert parsed.grading_company == "BGS"
    assert parsed.grade == "9.5"
    assert parsed.is_graded


def test_parse_title_detects_counterfeit_words():
    parsed = parse_title("Charizard Custom Proxy Metal Card Replica")
    assert set(parsed.counterfeit_words) >= {"custom", "proxy", "replica"}


def test_parse_title_quantity():
    assert parse_title("Charizard 4/102 x3 bundle").quantity == 3
    assert parse_title("Charizard 4/102").quantity == 1


def test_parse_title_variants():
    parsed = parse_title("Pikachu #58 Reverse Holo 1st Edition Shadowless")
    assert {"reverse_holo", "first_edition", "shadowless"} <= parsed.variants


def test_parse_product_name():
    name, number, variants = parse_product_name("Charizard [1st Edition] #4")
    assert name == "charizard"
    assert number == "4"
    assert variants == {"first_edition"}


def test_parse_product_name_without_number():
    name, number, _ = parse_product_name("Ancient Mew")
    assert name == "ancient mew"
    assert number is None
