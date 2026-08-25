from __future__ import annotations

from sqlalchemy import select

from pokemon_arb import store
from pokemon_arb.db import get_sessionmaker
from pokemon_arb.models import Deal, Listing, Product, Target
from pokemon_arb.pipeline.scan import ScanService, build_query
from pokemon_arb.sources.demo import DemoEbayClient, demo_products


def _service():
    return ScanService(ebay_client=DemoEbayClient(seed=7))


def test_build_query_is_seller_shaped():
    product = Product(name="Charizard [1st Edition] #4", set_name="Pokemon Base Set")
    query = build_query(product)
    assert "charizard" in query
    assert "Base Set" in query
    assert "Pokemon Base Set" not in query  # the redundant prefix is dropped
    assert query.endswith("4")


def test_sync_products_persists_and_snapshots(pc_product):
    service = _service()
    assert service.sync_products([pc_product]) == 1
    with get_sessionmaker()() as session:
        product = session.scalar(select(Product))
        assert product.ungraded_cents == 42000
        assert product.card_number == "4"
        assert len(product.price_points) == 1


def test_sync_is_idempotent(pc_product):
    service = _service()
    service.sync_products([pc_product])
    pc_product.prices["ungraded_cents"] = 45000
    service.sync_products([pc_product])
    with get_sessionmaker()() as session:
        assert (
            session.scalar(select(Product).where(Product.pc_id == "pc-1")).ungraded_cents == 45000
        )
        assert len(list(session.scalars(select(Product)))) == 1


def test_products_without_a_comp_are_skipped(pc_product):
    pc_product.prices["ungraded_cents"] = None
    assert _service().sync_products([pc_product]) == 0


def test_build_targets_takes_top_n_per_set():
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=2)
    with get_sessionmaker()() as session:
        targets = list(session.scalars(select(Target)))
        by_set: dict[str, int] = {}
        for target in targets:
            by_set[target.set_name] = by_set.get(target.set_name, 0) + 1
        assert all(count <= 2 for count in by_set.values())
        # The most valuable Base Set card must be targeted.
        base_targets = [t for t in targets if t.set_name == "Pokemon Base Set"]
        assert any("charizard" in t.query.lower() for t in base_targets)


def test_full_scan_produces_ranked_deals():
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    run = service.run()

    assert run.status == "ok"
    assert run.listings_seen > 0
    assert run.deals_found > 0

    with get_sessionmaker()() as session:
        deals = list(session.scalars(select(Deal).order_by(Deal.score.desc())))
        assert deals
        scores = [d.score for d in deals]
        assert scores == sorted(scores, reverse=True)
        for deal in deals:
            assert deal.profit_cents > 0
            assert deal.match_confidence >= service.settings.min_match_confidence
            assert deal.roi >= service.settings.min_roi


def test_scan_rejects_traps():
    """Proxies, lots and Japanese cards must never become deals."""
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    run = service.run()
    assert run.stats["rejected_matches"] > 0

    with get_sessionmaker()() as session:
        titles = [d.listing.title.lower() for d in session.scalars(select(Deal))]
        for title in titles:
            assert "proxy" not in title
            assert "lot of" not in title
            assert "japanese" not in title


def test_rescanning_updates_rather_than_duplicates():
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=2)
    service.run()
    with get_sessionmaker()() as session:
        first_listings = session.query(Listing).count()
        first_deals = session.query(Deal).count()

    # Same seed -> same synthetic listings -> upserts, not duplicates.
    ScanService(ebay_client=DemoEbayClient(seed=7)).run()
    with get_sessionmaker()() as session:
        assert session.query(Listing).count() == first_listings
        assert session.query(Deal).count() == first_deals


def test_user_judgement_survives_a_rescan():
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=2)
    service.run()
    with get_sessionmaker()() as session:
        deal = session.scalar(select(Deal).order_by(Deal.score.desc()))
        deal.status = "dismissed"
        deal_id = deal.id
        session.commit()

    ScanService(ebay_client=DemoEbayClient(seed=7)).run()
    with get_sessionmaker()() as session:
        assert session.get(Deal, deal_id).status == "dismissed"


def test_scan_respects_the_api_call_budget():
    service = _service()
    service.settings.ebay_max_calls_per_scan = 3
    service.sync_products(demo_products())
    service.build_targets(per_set=5)
    run = service.run()
    assert run.targets_scanned <= 3


def test_targets_are_scanned_highest_value_first():
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=5)
    run = service.run(max_targets=1)
    with get_sessionmaker()() as session:
        scanned = [t for t in session.scalars(select(Target)) if t.last_scanned_at]
        assert len(scanned) == 1
        assert "umbreon" in scanned[0].query.lower()  # the most valuable card seeded
    assert run.targets_scanned == 1


def test_stale_listings_are_deactivated():
    import datetime as dt

    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=1)
    service.run()
    with get_sessionmaker()() as session:
        listing = session.scalar(select(Listing))
        listing.last_seen_at = dt.datetime.utcnow() - dt.timedelta(days=30)
        session.commit()
        count = store.deactivate_stale_listings(session, dt.timedelta(days=3))
        session.commit()
        assert count >= 1
        assert session.scalar(select(Listing).where(Listing.id == listing.id)).is_active is False


def test_scan_with_no_targets_reports_it_rather_than_success():
    """An empty run means an unpopulated catalog; "ok" would hide that."""
    run = _service().run()
    assert run.status == "no_targets"
    assert run.targets_scanned == 0
    with get_sessionmaker()() as session:
        assert store.latest_scan(session) is not None


def test_scan_with_targets_still_reports_ok():
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=1)
    assert service.run().status == "ok"


def test_targets_rotate_so_the_tail_is_not_starved():
    """Value-first ordering rescanned the same top slice forever, leaving the
    rest permanently outside the freshness window."""
    service = _service()
    service.sync_products(demo_products())
    service.build_targets(per_set=5)

    service.run(max_targets=3)
    with get_sessionmaker()() as session:
        first = {t.id for t in session.scalars(select(Target)) if t.last_scanned_at}
    assert len(first) == 3

    ScanService(ebay_client=DemoEbayClient(seed=7)).run(max_targets=3)
    with get_sessionmaker()() as session:
        scanned = {t.id for t in session.scalars(select(Target)) if t.last_scanned_at}
    # The second pass must reach targets the first one never touched.
    assert len(scanned - first) == 3


# --- failures must not masquerade as an empty market -----------------------
class _BrokenEbay(DemoEbayClient):
    """Every search fails, the way bad credentials behave."""

    def search(self, *_args, **_kwargs):
        from pokemon_arb.sources.ebay import EbayAuthError

        raise EbayAuthError("eBay token request failed (400): invalid_client")


def test_a_scan_where_every_call_fails_is_reported_as_failed():
    service = ScanService(ebay_client=_BrokenEbay())
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    run = service.run()

    assert run.status == "failed"
    assert run.listings_seen == 0
    assert run.stats["errors"] > 0
    assert "invalid_client" in run.error
    assert "targets failed" in run.error


def test_partial_failure_is_distinguished_from_clean_success():
    class _FlakyEbay(DemoEbayClient):
        def __init__(self):
            super().__init__(seed=3)
            self.n = 0

        def search(self, *args, **kwargs):
            from pokemon_arb.sources.ebay import EbayError

            self.n += 1
            if self.n % 2:
                raise EbayError("transient upstream failure")
            yield from super().search(*args, **kwargs)

    service = ScanService(ebay_client=_FlakyEbay())
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    run = service.run()

    assert run.status == "partial"
    assert run.listings_seen > 0
    assert run.stats["errors"] > 0


def test_clean_scan_records_no_error():
    service = ScanService(ebay_client=DemoEbayClient(seed=7))
    service.sync_products(demo_products())
    service.build_targets(per_set=3)
    run = service.run()
    assert run.status == "ok"
    assert run.error is None
    assert run.stats["errors"] == 0
