"""The scan pipeline.

    sync comps  ->  build targets  ->  search eBay  ->  match  ->  price  ->  rank

Each target is one card we care about. For every target we ask eBay only for
listings priced below the point where the trade could still work, cheapest
first, so the API call budget goes almost entirely to plausible bargains.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .. import store
from ..config import Settings, get_settings
from ..db import get_sessionmaker
from ..matching.matcher import best_match
from ..matching.normalize import ParsedTitle, parse_title
from ..models import Product, ScanRun, Target, utcnow
from ..sources.ebay import EbayClient, EbayError, EbayListing
from ..sources.pricecharting import PriceChartingClient
from .peer_comps import PeerGroup, build_groups, group_key, percentile
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
    errors: int = 0
    excluded_by_keyword: int = 0
    skipped_thin_peers: int = 0
    error_samples: list[str] = field(default_factory=list)

    def record_error(self, message: str) -> None:
        self.errors += 1
        if message not in self.error_samples and len(self.error_samples) < 3:
            self.error_samples.append(message)


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
        self._exclusions: list[str] = []

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
    def sync_products(self, products, should_cancel: Callable[[], bool] | None = None) -> int:
        """Persist PCProduct records and snapshot their prices.

        Cancellable between rows: a full price-guide sync is long enough that
        being unable to stop it is a real problem. Whatever was committed
        before the cancel is kept -- a partial catalog beats none.
        """
        count = 0
        with self.session_factory() as session:
            for pc_product in products:
                # Every row, not every hundredth accepted one: rows that are
                # skipped never advanced `count`, so a guide full of sealed
                # product could go a long way between cancel checks.
                if should_cancel is not None and should_cancel():
                    log.info("sync cancelled after %s cards", count)
                    break
                if not pc_product.external_id or not pc_product.prices.get("ungraded_cents"):
                    continue
                product, _created = store.upsert_product(session, pc_product.to_model_kwargs())
                session.flush()
                store.record_price_point(session, product)
                count += 1
                if count % 500 == 0:
                    session.commit()
            session.commit()
        return count

    def sync_from_price_guide(
        self, category: str = "pokemon-cards", should_cancel: Callable[[], bool] | None = None
    ) -> int:
        return self.sync_products(
            self.pricecharting.iter_price_guide(category, should_cancel=should_cancel),
            should_cancel=should_cancel,
        )

    def sync_from_csv_text(self, text: str) -> int:
        return self.sync_products(PriceChartingClient.parse_price_guide_csv(text))

    def sync_from_queries(
        self, queries: list[str], should_cancel: Callable[[], bool] | None = None
    ) -> int:
        found = []
        for query in queries:
            if should_cancel is not None and should_cancel():
                break
            found.extend(self.pricecharting.search_products(query))
        return self.sync_products(found, should_cancel=should_cancel)

    # --- targets ------------------------------------------------------
    def build_targets(self, per_set: int | None = None, sets: list[str] | None = None) -> int:
        """Target the top-N most valuable cards in each set.

        This is the manual heuristic that worked, automated: the chase cards
        carry the spread, and they are the ones sellers most often mis-price.
        """
        if self.settings.uses_peer_comps:
            # There is no price guide to rank, so targets are whatever the
            # user typed rather than the top cards of each set.
            return 0
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
        self,
        max_targets: int | None = None,
        listings_per_target: int | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ScanRun:
        settings = self.settings
        per_target = listings_per_target or settings.scan_listings_per_target
        stats = ScanStats()
        with self.session_factory() as session:
            # Read once per run rather than per listing.
            self._exclusions = store.active_exclusions(session)

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
                        # Stalest first, value breaking ties. Ordering by value
                        # first meant that with more targets than the call
                        # budget allows, the same top slice was rescanned every
                        # time and the rest never got scanned at all -- so their
                        # listings sat permanently outside the freshness window.
                        .order_by(Target.last_scanned_at.asc().nullsfirst(), Target.priority.desc())
                    )
                )
                if max_targets:
                    targets = targets[:max_targets]

                cancelled = False
                for target in targets:
                    # Checked between targets, so an in-flight eBay request is
                    # allowed to finish and its listings are kept.
                    if should_cancel is not None and should_cancel():
                        log.info("scan cancelled after %s targets", stats.targets_scanned)
                        cancelled = True
                        break
                    if stats.api_calls >= settings.ebay_max_calls_per_scan:
                        log.warning(
                            "eBay call budget exhausted after %s targets", stats.targets_scanned
                        )
                        break
                    try:
                        self._scan_target(session, target, per_target, stats, should_cancel)
                    except EbayError as exc:
                        # Counted and reported, not just logged: a scan where
                        # every eBay call failed used to finish as "ok" with
                        # zero listings, which is indistinguishable from a
                        # market that simply had no bargains that day.
                        log.error("target %r failed: %s", target.query, exc)
                        stats.record_error(str(exc))
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
                # A scan with nothing to scan is not a success -- it is the
                # symptom of an unpopulated catalog, and saying "ok" hides that.
                if cancelled:
                    run.status = "cancelled"
                elif not targets:
                    run.status = "no_targets"
                elif stats.errors and stats.listings_seen == 0:
                    # Nothing worked. Almost always credentials or endpoint.
                    run.status = "failed"
                elif stats.errors:
                    run.status = "partial"
                else:
                    run.status = "ok"
                if stats.errors:
                    run.error = f"{stats.errors} of {len(targets)} targets failed: " + " | ".join(
                        stats.error_samples
                    )
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
                    "errors": stats.errors,
                    "excluded_by_keyword": stats.excluded_by_keyword,
                    "skipped_thin_peers": stats.skipped_thin_peers,
                }
                session.commit()
                return run
        except KeyboardInterrupt:
            # Ctrl-C must not leave the row stuck at "running" either.
            with self.session_factory() as session:
                run = session.get(ScanRun, run_id)
                run.status = "cancelled"
                run.finished_at = utcnow()
                run.targets_scanned = stats.targets_scanned
                run.api_calls = stats.api_calls
                session.commit()
            raise
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
        self,
        session: Session,
        target: Target,
        per_target: int,
        stats: ScanStats,
        should_cancel: Callable[[], bool] | None = None,
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

        if self.settings.uses_peer_comps:
            self._scan_target_peer(session, target, listings, stats, should_cancel)
            return

        for data in listings:
            # A single target can return hundreds of listings; without this a
            # cancel waits for all of them to be matched and priced.
            if should_cancel is not None and should_cancel():
                return
            stats.listings_seen += 1
            self._process_listing(session, data, product, stats)

    def _scan_target_peer(
        self,
        session: Session,
        target: Target,
        listings: list[EbayListing],
        stats: ScanStats,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        """Value this target's listings against each other.

        Needs the whole result set before anything can be priced, so unlike
        the catalog path this is two passes: gather, then judge.
        """
        candidates: list[tuple[EbayListing, ParsedTitle]] = []
        for data in listings:
            if should_cancel is not None and should_cancel():
                return
            stats.listings_seen += 1
            title = data.title.lower()
            hit = next((term for term in self._exclusions if term in title), None)
            if hit:
                stats.excluded_by_keyword += 1
                store.record_keyword_hit(session, hit)
                continue
            parsed = parse_title(data.title)
            # Junk must not be priced *or* counted as a peer: a lot of 50 cards
            # at $30 would drag the comp for a single card down with it.
            if (
                parsed.lot_words
                or parsed.counterfeit_words
                or parsed.quantity > 1
                or (parsed.language and parsed.language != "english")
            ):
                stats.rejected_matches += 1
                continue
            candidates.append((data, parsed))

        groups = build_groups(candidates)
        for (segment, number), group in groups.items():
            if group.sample_size < self.settings.peer_min_sample:
                stats.skipped_thin_peers += 1
                continue
            product = self._peer_product(session, target, segment, number, group)
            for data, parsed in candidates:
                if group_key(parsed) != (segment, number):
                    continue
                if should_cancel is not None and should_cancel():
                    return
                landed = data.price_cents + (data.shipping_cents or 0)
                comp = group.comp_excluding(landed, self.settings.peer_comp_percentile)
                if comp is None:
                    continue
                self._process_listing(
                    session,
                    data,
                    product,
                    stats,
                    parsed=parsed,
                    comp_override=(comp, f"{group.sample_size - 1} peer asks, {segment}"),
                )

    def _peer_product(
        self,
        session: Session,
        target: Target,
        segment: str,
        number: str | None,
        group: PeerGroup,
    ) -> Product:
        """A catalog row standing in for a card the price guide never supplied."""
        label = target.query.strip() or "eBay search"
        display = f"{label}{f' #{number}' if number else ''}"
        if segment != "raw":
            display = f"{display} [{segment}]"
        product, _created = store.upsert_product(
            session,
            {
                "external_id": f"peer:{target.id}:{segment}:{number or '-'}",
                "name": display,
                "set_name": target.set_name or "eBay peer comps",
                "card_number": number,
                "release_date": None,
                # For reference on the deal page; each deal carries its own
                # self-excluded comp in market_value_cents.
                "ungraded_cents": percentile(group.prices, self.settings.peer_comp_percentile),
                "sales_volume": None,
                "search_blob": display.lower(),
                "last_synced_at": utcnow(),
            },
        )
        session.flush()
        return product

    def _process_listing(
        self,
        session: Session,
        data: EbayListing,
        target_product: Product | None,
        stats: ScanStats,
        parsed: ParsedTitle | None = None,
        comp_override: tuple[int, str] | None = None,
    ) -> None:
        if comp_override is None:
            title = data.title.lower()
            hit = next((term for term in self._exclusions if term in title), None)
            if hit:
                # Checked before the listing is stored: an excluded listing
                # should leave no trace beyond the counter.
                stats.excluded_by_keyword += 1
                store.record_keyword_hit(session, hit)
                return

        listing, created = store.upsert_listing(session, data)
        if created:
            stats.listings_new += 1

        if parsed is None:
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

        econ = evaluate(
            listing, match.product, match, parsed, self.settings, comp_override=comp_override
        )
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
