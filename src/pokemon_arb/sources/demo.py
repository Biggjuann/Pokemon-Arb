"""Offline fixtures.

Demo mode exists so the ranking, matching and risk logic can be exercised --
and reviewed -- without eBay or PriceCharting credentials. The listing
generator deliberately mixes genuine bargains with the traps that make naive
"price < comp" scanners lose money: proxies, lots, Japanese prints, damaged
cards and wrong-variant matches.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Iterator

from ..clock import utcnow
from .ebay import EbayListing
from .pricecharting import PCProduct

# (set, card name, ungraded, grade9, psa10, annual sales volume)
DEMO_CATALOG: list[tuple[str, str, int, int, int, int]] = [
    ("Pokemon Base Set", "Charizard #4", 42000, 180000, 1250000, 210),
    ("Pokemon Base Set", "Blastoise #2", 14000, 52000, 380000, 130),
    ("Pokemon Base Set", "Venusaur #15", 12000, 46000, 340000, 120),
    ("Pokemon Base Set", "Chansey #3", 4500, 16000, 90000, 60),
    ("Pokemon Base Set", "Mewtwo #10", 4000, 14000, 78000, 88),
    ("Pokemon Evolving Skies", "Umbreon VMAX (Alternate Art) #215", 52000, 78000, 145000, 340),
    ("Pokemon Evolving Skies", "Rayquaza VMAX (Alternate Art) #218", 24000, 36000, 68000, 190),
    ("Pokemon Evolving Skies", "Sylveon VMAX (Alternate Art) #212", 13000, 19000, 41000, 150),
    ("Pokemon Evolving Skies", "Glaceon VMAX (Alternate Art) #209", 9000, 14000, 30000, 110),
    ("Pokemon Hidden Fates", "Charizard GX #SV49", 11000, 19000, 42000, 175),
    ("Pokemon Hidden Fates", "Gyarados GX #SV30", 3500, 6000, 15000, 70),
    ("Pokemon Lost Origin", "Giratina V (Alternate Art) #TG12", 18000, 27000, 55000, 160),
    ("Pokemon Lost Origin", "Aerodactyl V (Alternate Art) #TG16", 5500, 9000, 21000, 80),
    ("Pokemon Crown Zenith", "Mewtwo VSTAR (Gold) #GG69", 7000, 11000, 26000, 95),
    ("Pokemon Crown Zenith", "Giratina VSTAR (Gold) #GG68", 6200, 9500, 22000, 85),
    ("Pokemon Neo Genesis", "Lugia #9", 38000, 140000, 900000, 65),
    ("Pokemon Neo Genesis", "Typhlosion #17", 3000, 9000, 44000, 40),
    ("Pokemon Team Rocket", "Dark Charizard #4", 9000, 30000, 190000, 72),
    ("Pokemon Team Rocket", "Dark Blastoise #3", 2200, 7000, 38000, 35),
    ("Pokemon Jungle", "Snorlax #11", 3200, 11000, 60000, 55),
]

_CONDITION_WORDS = ["NM", "Near Mint", "LP", "Excellent", "Pack Fresh", "Mint"]
_FILLER = ["WOTC", "Rare Holo", "TCG", "Authentic", "Fast Ship", "Investment", "Vintage"]


def demo_products() -> list[PCProduct]:
    products: list[PCProduct] = []
    for index, (set_name, name, ungraded, g9, psa10, volume) in enumerate(DEMO_CATALOG):
        products.append(
            PCProduct(
                pc_id=f"demo-{index:04d}",
                name=name,
                set_name=set_name,
                prices={
                    "ungraded_cents": ungraded,
                    "grade7_cents": int(ungraded * 1.6),
                    "grade8_cents": int(ungraded * 2.4),
                    "grade9_cents": g9,
                    "grade95_cents": int((g9 + psa10) / 2),
                    "psa10_cents": psa10,
                },
                sales_volume=volume,
                release_date="1999-01-09",
            )
        )
    return products


def _title(
    name: str, set_name: str, number: str | None, rng: random.Random, extra: str = ""
) -> str:
    display_set = set_name.replace("Pokemon ", "")
    bits = [
        "Pokemon",
        name,
        f"{number}" if number else "",
        display_set,
        rng.choice(_CONDITION_WORDS),
        rng.choice(_FILLER),
        extra,
    ]
    return " ".join(b for b in bits if b).strip()


class DemoEbayClient:
    """Drop-in stand-in for :class:`EbayClient` used by ``--demo`` scans."""

    def __init__(self, seed: int = 20240823):
        self.rng = random.Random(seed)
        self.call_count = 0

    def close(self) -> None:  # pragma: no cover - parity with EbayClient
        pass

    @staticmethod
    def build_filter(**kwargs) -> str:
        from .ebay import EbayClient

        return EbayClient.build_filter(**kwargs)

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
        self.call_count += 1
        rng = self.rng
        tokens = query.split()
        number = tokens[-1] if tokens and any(c.isdigit() for c in tokens[-1]) else None
        name = " ".join(tokens[:-1]) if number else query

        # Find the comp this query came from so prices look plausible.
        comp = 5000
        for _set_name, card, ungraded, *_ in DEMO_CATALOG:
            if card.split(" #")[0].lower().split(" (")[0] in query.lower():
                comp = ungraded
                break

        recipes = [
            # (price multiplier vs comp, extra title text, weight)
            (rng.uniform(0.40, 0.60), "", 3),  # the real find
            (rng.uniform(0.62, 0.80), "", 3),  # marginal
            (rng.uniform(0.85, 1.10), "", 4),  # fairly priced
            (rng.uniform(0.05, 0.12), "Custom Proxy Metal Card Novelty", 1),
            (rng.uniform(0.15, 0.30), "Lot of 12 Cards Mystery Repack", 1),
            (rng.uniform(0.30, 0.45), "Japanese", 1),
            (rng.uniform(0.35, 0.55), "damaged crease read description as is", 1),
        ]
        pool = [r for r in recipes for _ in range(r[2])]
        rng.shuffle(pool)

        for index, (multiplier, extra, _weight) in enumerate(pool[:limit]):
            price = max(199, int(comp * multiplier))
            shipping = rng.choice([0, 0, 399, 499, 599])
            feedback_score = rng.choice([3, 22, 140, 900, 5400, 18000])
            yield EbayListing(
                ebay_item_id=f"demo|{abs(hash((query, index))) % 10**12}",
                title=_title(name.title(), "", number, rng, extra),
                url=f"https://www.ebay.com/itm/{abs(hash((query, index))) % 10**12}",
                price_cents=price,
                shipping_cents=shipping,
                currency="USD",
                buying_format="FIXED_PRICE",
                condition="Ungraded",
                image_url=None,
                seller_username=f"seller{feedback_score}",
                seller_feedback_pct=rng.choice([97.1, 98.6, 99.3, 99.8, 100.0]),
                seller_feedback_score=feedback_score,
                top_rated_seller=feedback_score > 1000,
                returns_accepted=rng.choice([True, True, False]),
                item_location="US",
                listed_at=utcnow() - dt.timedelta(hours=rng.randint(1, 72)),
            )
