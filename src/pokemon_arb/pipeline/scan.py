"""The scan pipeline.

    sync comps  ->  build targets  ->  search eBay  ->  match  ->  price  ->  rank

Each target is one card we care about. For every target we ask eBay only for
listings priced below the point where the trade could still work, cheapest
first, so the API call budget goes almost entirely to plausible bargains.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .. import store
from ..config import Settings, get_settings
from ..db import get_sessionmaker
from ..matching.matcher import best_match
from ..matching.normalize import parse_title
from ..models import Product, ScanRun, Target, utcnow
from ..sources.ebay import EbayClient, EbayError, EbayListing
from ..sources.pricecharting import PriceChartingClient
from .scoring import evaluate, max_actionable_price_cents

log = logging.getLogger(__name__)

# When to conclude a listing is gone from eBay entirely. This is *not* the
# display-freshness rule -- see freshness.py, which governs what may be shown
# and is enforced per request.
STALE_LISTING_AFTER = dt.timedelta(days=3)


@dataclass
class ScanStats:
    targets_scanned: int = 0
    listings_seen: int = 0
    listings_new: int = 0
    deals_found: int = 0
    rejected_matches: int = 0
    below_thresholds: int = 0
    api_calls: int = 0
    skipped_no_comp: int = 0


def build_query(product: Product) -> str:
    """An eBay search string for a card.

    Sellers write "Charizard 4/102 Base Set", not "Charizard #4", so lead with
    the name, then the set (minus the redundant 'Pokemon'), then the number.
    """
    from ..matching.normalize import parse_product_name

    name, number, _ = parse_product_name(product.name)
    set_name = product.set_name
    for prefix in ("Pokemon ", "Pokémon "):
        if set_name.startswith(prefix):
            set_name = set_name[len(prefix) :]
    parts = [name, set_name]
    if number:
        parts.append(number)
    return " ".join(p for p in parts if p).strip()


class ScanService:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: sessionmaker[Session] | None = None,
        ebay_client: EbayClient | None = None,
        pc_client: PriceChartingClient | None = None,
    ):
        self.settings = settings or get_settings()
        self.session_factory = session_factory or get_sessionmaker()
        self._ebay = ebay_client
        self._pc = pc_client

    # --- clients ------------------------------------------------------
    @property
    def ebay(self) -> EbayClient:
        if self._ebay is None:
            self._ebay = EbayClient(
                self.settings.ebay_client_id,
                self.settings.ebay_client_secret,
                api_base=self.settings.ebay_api_base,
                marketplace=self.settings.ebay_marketplace,
            )
        return self._ebay

    @property
    def pricecharting(self) -> PriceChartingClient:
        if self._pc is None:
            self._pc = PriceChartingClient(self.settings.pricecharting_token)
        return self._pc

    # --- comps --------------------------------------------------------
    def sync_products(self, products) -> int:
        """Persist PCProduct records and snapshot their prices."""
        count = 0
        with self.session_factory() as session:
            for pc_product in products:
                if not pc_product.pc_id or not pc_product.prices.get("ungraded_cents"):
                    continue
                product, _created = store.upsert_product(session, pc_product.to_model_kwargs())
                session.flush()
                store.record_price_point(session, product)
                count += 1
                if count % 500 == 0:
                    session.commit()
            session.commit()
        return count

    def sync_from_price_guide(self, category: str = "pokemon-cards") -> int:
        return self.sync_products(self.pricecharting.iter_price_guide(category))

    def sync_from_csv_text(self, text: str) -> int:
        return self.sync_products(PriceChartingClient.parse_price_guide_csv(text))

    def sync_from_queries(self, queries: list[str]) -> int:
        found = []
        for query in queries:
            found.extend(self.pricecharting.search_products(query))
        return self.sync_products(found)

    # --- targets ------------------------------------------------------
    def build_targets(self, per_set: int | None = None, sets: list[str] | None = None) -> int:
        """Target the top-N most valuable cards in each set.

        This is the manual heuristic that worked, automated: the chase cards
        carry the spread, and they are the ones sellers most often mis-price.
        """
        per_set = per_set or self.settings.scan_top_cards_per_set
        created = 0
        with self.session_factory() as session:
            for product in store.top_products_per_set(session, per_set, sets):
                store.upsert_target(
                    session,
                    query=build_query(product),
                    product=product,
                    priority=product.ungraded_cents or 0,
                )
                created += 1
            session.commit()
        return created

    # --- scan ---------------------------------------------------------
    def run(
        self, max_targets: int | None = None, listings_per_target: int | None = None
    ) -> ScanRun:
        settings = self.settings
        per_target = listings_per_target or settings.scan_listings_per_target
        stats = ScanStats()

        with self.session_factory() as session:
            run = ScanRun(status="running")
            session.add(run)
            session.commit()
            run_id = run.id

        try:
            with self.session_factory() as session:
                targets = list(
                    session.scalars(
                        select(Target)
                        .where(Target.enabled.is_(True))
                        .order_by(Target.priority.desc(), Target.last_scanned_at.asc().nullsfirst())
                    )
                )
                if max_targets:
                    targets = targets[:max_targets]

                for target in targets:
                    if stats.api_calls >= settings.ebay_max_calls_per_scan:
                        log.warning(
                            "eBay call budget exhausted after %s targets", stats.targets_scanned
                        )
                        break
                    try:
                        self._scan_target(session, target, per_target, stats)
                    except EbayError as exc:
                        log.error("target %r failed: %s", target.query, exc)
                        session.rollback()
                        if "rate limit" in str(exc).lower():
                            break
                        continue
                    target.last_scanned_at = utcnow()
                    stats.targets_scanned += 1
                    session.commit()

                store.deactivate_stale_listings(session, STALE_LISTING_AFTER)
                session.commit()

            with self.session_factory() as session:
                run = session.get(ScanRun, run_id)
                run.status = "ok"
                run.finished_at = utcnow()
                run.targets_scanned = stats.targets_scanned
                run.listings_seen = stats.listings_seen
                run.listings_new = stats.listings_new
                run.deals_found = stats.deals_found
                run.api_calls = stats.api_calls
                run.stats = {
                    "rejected_matches": stats.rejected_matches,
                    "below_thresholds": stats.below_thresholds,
                    "skipped_no_comp": stats.skipped_no_comp,
                }
                session.commit()
                return run
        except Exception as exc:
            log.exception("scan failed")
            with self.session_factory() as session:
                run = session.get(ScanRun, run_id)
                run.status = "error"
                run.finished_at = utcnow()
                run.error = f"{type(exc).__name__}: {exc}"
                run.targets_scanned = stats.targets_scanned
                run.api_calls = stats.api_calls
                session.commit()
                return run

    def _scan_target(
        self, session: Session, target: Target, per_target: int, stats: ScanStats
    ) -> None:
        settings = self.settings
        product = target.product

        max_price = None
        if product is not None:
            max_price = max_actionable_price_cents(product, settings)
            if max_price is None:
                stats.skipped_no_comp += 1
                return
            max_price = min(max_price, settings.scan_max_price_cents)

        filter_string = EbayClient.build_filter(
            max_price_cents=max_price or settings.scan_max_price_cents,
            min_price_cents=100,
            fixed_price_only=True,
            item_location_country=settings.ebay_delivery_country,
            delivery_country=settings.ebay_delivery_country,
        )

        before_calls = self.ebay.call_count
        listings = list(
            self.ebay.search(
                target.query,
                category_ids=settings.ebay_category_id,
                limit=per_target,
                filter_string=filter_string,
                sort="price",
            )
        )
        stats.api_calls += self.ebay.call_count - before_calls

        for data in listings:
            stats.listings_seen += 1
            self._process_listing(session, data, product, stats)

    def _process_listing(
        self, session: Session, data: EbayListing, target_product: Product | None, stats: ScanStats
    ) -> None:
        listing, created = store.upsert_listing(session, data)
        if created:
            stats.listings_new += 1

        parsed = parse_title(data.title)

        # Consider the card we searched for plus anything else in the catalog
        # sharing a card number in the title -- that is how "Charizard 4"
        # searches that return "Charizard V 4/172" get valued correctly.
        candidates: list[Product] = []
        if target_product is not None:
            candidates.append(target_product)
        for other in store.candidate_products(session, parsed.card_numbers):
            if target_product is None or other.id != target_product.id:
                candidates.append(other)
        if not candidates:
            return

        match = best_match(data.title, candidates, parsed=parsed)
        if match.product is None:
            stats.rejected_matches += 1
            return

        econ = evaluate(listing, match.product, match, parsed, self.settings)
        if not econ.qualifies:
            if match.rejected or match.confidence < self.settings.min_match_confidence:
                stats.rejected_matches += 1
            else:
                stats.below_thresholds += 1
            return

        store.upsert_deal(
            session,
            listing,
            match.product,
            {
                "match_confidence": econ.confidence,
                "match_reason": match.reason_text,
                "market_value_cents": econ.market_value_cents,
                "adjusted_value_cents": econ.adjusted_value_cents,
                "total_cost_cents": econ.total_cost_cents,
                "net_proceeds_cents": econ.net_proceeds_cents,
                "profit_cents": econ.profit_cents,
                "roi": econ.roi,
                "discount_pct": econ.discount_pct,
                "risk_flags": econ.risk_flags,
                "risk_penalty": econ.risk_penalty,
                "score": econ.score,
            },
        )
        stats.deals_found += 1
