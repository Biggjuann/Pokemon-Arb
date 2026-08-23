"""Turn a messy eBay title into structured facts.

eBay titles are keyword soup written by sellers to win search, e.g.
    "Pokemon Charizard 4/102 Base Set Holo Rare WOTC 1999 NM PSA READY!!"
Everything downstream (matching, risk, valuation) depends on pulling the
card number, variant and hazard words back out of that.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Alphabetic prefixes that look like a set code but are really grader names,
# condition abbreviations or units. Without this "PSA10" parses as card #10.
_NOT_SET_CODES = {
    "PSA",
    "BGS",
    "CGC",
    "SGC",
    "ACE",
    "TAG",
    "GMA",
    "ISA",
    "MNT",
    "AGS",
    "NM",
    "LP",
    "MP",
    "HP",
    "DMG",
    "GEM",
    "MT",
    "EX",
    "VG",
    "USD",
    "USA",
    "UK",
    "JP",
    "EU",
    "ID",
    "NO",
    "PC",
    "LOT",
    "X",
}

_CARD_NUMBER_RE = re.compile(
    r"""
    (?<![\w/-])
    (?:(?P<hash>\#)\s*)?
    (?P<num>[A-Za-z]{1,4}\d{1,4}[a-z]?|\d{1,4}[a-z]?)
    (?:\s*/\s*(?P<den>[A-Za-z]{0,4}\d{1,4}))?
    (?![\w-])
    """,
    re.VERBOSE,
)

_GRADE_RE = re.compile(
    r"\b(?P<company>PSA|BGS|CGC|SGC|ACE|TAG|GMA|AGS)\s*[-.]?\s*(?P<grade>10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b",
    re.IGNORECASE,
)

_QUANTITY_RE = re.compile(r"\b(?:x\s?\d{1,3}|\d{1,3}\s?x)\b", re.IGNORECASE)

_LANGUAGES = {
    "japanese": "japanese",
    "japan": "japanese",
    "jpn": "japanese",
    "jp": "japanese",
    "korean": "korean",
    "korea": "korean",
    "chinese": "chinese",
    "german": "german",
    "deutsch": "german",
    "french": "french",
    "francais": "french",
    "italian": "italian",
    "spanish": "spanish",
    "espanol": "spanish",
    "portuguese": "portuguese",
    "russian": "russian",
    "thai": "thai",
    "indonesian": "indonesian",
}

# Words that mean "this is not a single authentic English card".
_COUNTERFEIT_WORDS = {
    "proxy",
    "proxies",
    "orica",
    "custom",
    "fanmade",
    "fan-made",
    "fanart",
    "replica",
    "reprint",
    "reproduction",
    "repro",
    "novelty",
    "metal",
    "gold-plated",
    "goldplated",
    "sticker",
    "handmade",
    "digital",
    "printed",
}
_LOT_WORDS = {
    "lot",
    "lots",
    "bundle",
    "bulk",
    "collection",
    "binder",
    "joblot",
    "playset",
    "repack",
    "mystery",
    "random",
    "assorted",
    "wholesale",
}
_PICK_WORDS = {"pick", "choose", "choice", "select", "singles"}
_DAMAGE_WORDS = {
    "damaged",
    "damage",
    "crease",
    "creased",
    "creasing",
    "bent",
    "bend",
    "water",
    "torn",
    "tear",
    "ripped",
    "scratched",
    "scratches",
    "whitening",
    "poor",
    "played",
    "worn",
    "dinged",
    "warped",
    "miscut",
    "stain",
    "stained",
    "writing",
    "marked",
    "trimmed",
}
_NOT_A_CARD_WORDS = {
    "empty",
    "box",
    "booster",
    "pack",
    "packs",
    "tin",
    "etb",
    "sealed",
    "code",
    "codes",
    "sleeve",
    "sleeves",
    "toploader",
    "binder",
    "case",
    "display",
    "playmat",
    "figure",
    "plush",
    "coin",
    "jumbo",
    "oversized",
}
_HEDGE_PHRASES = (
    "read description",
    "read desc",
    "as is",
    "as-is",
    "see photos",
    "see pictures",
    "no returns",
    "sold as",
    "unsearched",
)

_STOPWORDS = {
    "pokemon",
    "pokémon",
    "tcg",
    "card",
    "cards",
    "trading",
    "game",
    "ccg",
    "rare",
    "the",
    "a",
    "of",
    "and",
    "with",
    "for",
    "from",
    "new",
    "authentic",
    "genuine",
    "official",
    "english",
    "eng",
    "mint",
    "nm",
    "near",
    "pack",
    "fresh",
    "invest",
    "investment",
    "vintage",
    "rare!",
    "wotc",
    "free",
    "shipping",
    "ships",
    "fast",
    "lowest",
    "price",
    "deal",
    "hot",
    "look",
}

_VARIANT_TOKENS = {
    "holo": "holo",
    "holographic": "holo",
    "foil": "holo",
    "reverse": "reverse_holo",
    "rev": "reverse_holo",
    "nonholo": "non_holo",
    "shadowless": "shadowless",
    "unlimited": "unlimited",
    "promo": "promo",
    "staff": "staff",
    "prerelease": "prerelease",
    "shadow": None,  # only meaningful as part of "shadowless"
}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_card_number(raw: str | None) -> str | None:
    """Canonical card number: 'TG12/TG30' -> 'TG12', '004/102' -> '4'."""
    if not raw:
        return None
    token = raw.strip().upper().lstrip("#").strip()
    token = token.split("/")[0].strip()
    if not token:
        return None
    m = re.fullmatch(r"([A-Z]*)(\d+)([A-Z]?)", token)
    if not m:
        return token or None
    prefix, digits, suffix = m.groups()
    if prefix in _NOT_SET_CODES and prefix:
        return None
    return f"{prefix}{int(digits)}{suffix}"


def extract_card_numbers(text: str) -> list[str]:
    """All plausible card numbers in a title, most confident first.

    A bare integer is only accepted when it is prefixed with '#' or followed
    by '/total' -- otherwise years, quantities and grades flood the results.
    Grades ("PSA 10") and quantities ("x4") are masked out before scanning.
    """
    text = _GRADE_RE.sub(" ", text)
    text = _QUANTITY_RE.sub(" ", text)
    strong: list[str] = []
    weak: list[str] = []
    for m in _CARD_NUMBER_RE.finditer(text):
        num = m.group("num")
        den = m.group("den")
        hashed = bool(m.group("hash"))
        prefix = re.match(r"[A-Za-z]*", num).group(0).upper()
        if prefix and prefix in _NOT_SET_CODES:
            continue
        canonical = normalize_card_number(num)
        if not canonical:
            continue
        if den or hashed:
            strong.append(canonical)
        elif prefix and len(prefix) >= 2 and re.search(r"\d", num):
            # Set-code style: SWSH284, TG12, GG01, SV49.
            strong.append(canonical)
        elif canonical.isdigit() and 1900 <= int(canonical) <= 2100:
            continue  # a printing year, not a card number
        else:
            weak.append(canonical)
    seen: set[str] = set()
    ordered: list[str] = []
    for value in strong + weak:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def tokenize(text: str) -> list[str]:
    text = strip_accents(text.lower())
    text = re.sub(r"[^a-z0-9#/\.\s-]", " ", text)
    raw = [t.strip("-.") for t in text.split()]
    return [t for t in raw if t and t not in _STOPWORDS]


@dataclass
class ParsedTitle:
    raw: str
    tokens: list[str] = field(default_factory=list)
    card_numbers: list[str] = field(default_factory=list)
    grading_company: str | None = None
    grade: str | None = None
    language: str | None = None
    variants: set[str] = field(default_factory=set)
    quantity: int = 1
    counterfeit_words: list[str] = field(default_factory=list)
    lot_words: list[str] = field(default_factory=list)
    damage_words: list[str] = field(default_factory=list)
    not_a_card_words: list[str] = field(default_factory=list)
    hedge_phrases: list[str] = field(default_factory=list)

    @property
    def is_graded(self) -> bool:
        return self.grading_company is not None

    @property
    def is_english(self) -> bool:
        return self.language is None or self.language == "english"

    @property
    def name_tokens(self) -> list[str]:
        """Tokens likely to be the card's actual name (drop numbers/variants)."""
        out = []
        for t in self.tokens:
            if re.fullmatch(r"[#/\d\.-]+", t):
                continue
            if t in _VARIANT_TOKENS or t in _DAMAGE_WORDS or t in _LOT_WORDS:
                continue
            if t in _LANGUAGES or t in _COUNTERFEIT_WORDS:
                continue
            out.append(t)
        return out


def parse_title(title: str) -> ParsedTitle:
    lowered = strip_accents(title.lower())
    tokens = tokenize(title)
    token_set = set(tokens)

    parsed = ParsedTitle(raw=title, tokens=tokens)
    parsed.card_numbers = extract_card_numbers(title)

    if m := _GRADE_RE.search(title):
        parsed.grading_company = m.group("company").upper()
        parsed.grade = m.group("grade")
    elif re.search(r"\b(graded|slab(bed)?)\b", lowered):
        parsed.grading_company = "UNKNOWN"

    for token in tokens:
        if token in _LANGUAGES:
            parsed.language = _LANGUAGES[token]
            break

    if "reverse" in token_set or "rev" in token_set:
        parsed.variants.add("reverse_holo")
    if "shadowless" in token_set:
        parsed.variants.add("shadowless")
    for token in token_set:
        mapped = _VARIANT_TOKENS.get(token)
        if mapped and mapped != "reverse_holo":
            parsed.variants.add(mapped)
    # "1st" alone is enough; "first" needs "edition"/"ed" to avoid matching
    # card names like "First Partner".
    if "1st" in token_set or ("first" in token_set and token_set & {"edition", "ed"}):
        parsed.variants.add("first_edition")

    if qty := re.search(r"\b(?:x\s?(\d{1,3})|(\d{1,3})\s?x)\b", lowered):
        value = int(qty.group(1) or qty.group(2) or 1)
        if 1 < value <= 500:
            parsed.quantity = value

    parsed.counterfeit_words = sorted(token_set & _COUNTERFEIT_WORDS)
    parsed.lot_words = sorted(token_set & _LOT_WORDS)
    parsed.damage_words = sorted(token_set & _DAMAGE_WORDS)
    parsed.not_a_card_words = sorted(token_set & _NOT_A_CARD_WORDS)
    parsed.hedge_phrases = [p for p in _HEDGE_PHRASES if p in lowered]
    if re.search(r"\b(you )?pick\b|\byour choice\b", lowered) and token_set & _PICK_WORDS:
        parsed.lot_words = sorted(set(parsed.lot_words) | {"pick"})
    return parsed


def parse_product_name(product_name: str) -> tuple[str, str | None, set[str]]:
    """Split a PriceCharting product name.

    'Charizard [1st Edition] #4' -> ('charizard', '4', {'first_edition'})
    """
    variants: set[str] = set()
    for bracket in re.findall(r"\[([^\]]+)\]", product_name):
        key = bracket.strip().lower()
        if "1st" in key or "first" in key:
            variants.add("first_edition")
        if "reverse" in key:
            variants.add("reverse_holo")
        if "shadowless" in key:
            variants.add("shadowless")
        if key in ("holo", "holofoil"):
            variants.add("holo")
        if "promo" in key:
            variants.add("promo")
    cleaned = re.sub(r"\[[^\]]*\]", " ", product_name)

    number = None
    if m := re.search(r"#\s*([A-Za-z]{0,4}\d{1,4}[a-z]?(?:\s*/\s*[A-Za-z]{0,4}\d{1,4})?)", cleaned):
        number = normalize_card_number(m.group(1))
        cleaned = cleaned[: m.start()] + cleaned[m.end() :]

    name = " ".join(tokenize(cleaned))
    return name, number, variants
