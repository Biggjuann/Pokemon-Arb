"""Upserts and read queries. Keeps SQL out of the pipeline and the views."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Deal, Listing, PricePoint, Product, ScanRun, Target, utcnow
from .sources.ebay import EbayListing


def upsert_product(session: Session, values: dict[str, Any]) -> tuple[Product, bool]:
    product = session.scalar(select(Product).where(Product.pc_id == values["pc_id"]))
    created = product is None
    if product is None:
        product = Product(**values)
        session.add(product)
    else:
        for key, value in values.items():
            setattr(product, key, value)
    return product, created


def record_price_point(session: Session, product: Product) -> None:
    """Snapshot today's comps, at most once per product per day."""
    today = utcnow().date()
    already = session.scalar(
        select(func.count())
        .select_from(PricePoint)
        .where(
            PricePoint.product_id == product.id,
            func.date(PricePoint.captured_at) == str(today),
        )
    )
    if already:
        return
    session.add(
        PricePoint(
            product_id=product.id,
            ungraded_cents=product.ungraded_cents,
            grade9_cents=product.grade9_cents,
            psa10_cents=product.psa10_cents,
            sales_volume=product.sales_volume,
        )
    )


def upsert_listing(session: Session, data: EbayListing) -> tuple[Listing, bool]:
    listing = session.scalar(select(Listing).where(Listing.ebay_item_id == data.ebay_item_id))
    created = listing is None
    if listing is None:
        listing = Listing(
            ebay_item_id=data.ebay_item_id,
            title=data.title,
            url=data.url,
            image_url=data.image_url,
            price_cents=data.price_cents,
            shipping_cents=data.shipping_cents,
            currency=data.currency,
            buying_format=data.buying_format,
            condition=data.condition,
            seller_username=data.seller_username,
            seller_feedback_pct=data.seller_feedback_pct,
            seller_feedback_score=data.seller_feedback_score,
            top_rated_seller=data.top_rated_seller,
            returns_accepted=data.returns_accepted,
            item_location=data.item_location,
            listed_at=data.listed_at,
        )
        session.add(listing)
    else:
        listing.title = data.title
        listing.price_cents = data.price_cents
        listing.shipping_cents = data.shipping_cents
        listing.condition = data.condition
        listing.seller_feedback_pct = data.seller_feedback_pct
        listing.seller_feedback_score = data.seller_feedback_score
        listing.image_url = data.image_url or listing.image_url
        listing.last_seen_at = utcnow()
        listing.is_active = True
    session.flush()
    return listing, created


def upsert_deal(
    session: Session, listing: Listing, product: Product, values: dict[str, Any]
) -> Deal:
    deal = session.scalar(
        select(Deal).where(Deal.listing_id == listing.id, Deal.product_id == product.id)
    )
    if deal is None:
        deal = Deal(listing_id=listing.id, product_id=product.id, **values)
        session.add(deal)
    else:
        # Never resurrect a deal the user has already judged.
        if deal.status in ("dismissed", "bought"):
            return deal
        for key, value in values.items():
            setattr(deal, key, value)
        deal.updated_at = utcnow()
    session.flush()
    return deal


def upsert_target(
    session: Session, *, query: str, product: Product | None, priority: int
) -> Target:
    target = session.scalar(
        select(Target).where(
            Target.query == query,
            Target.product_id == (product.id if product else None),
        )
    )
    if target is None:
        target = Target(
            query=query,
            product_id=product.id if product else None,
            set_name=product.set_name if product else None,
            priority=priority,
        )
        session.add(target)
    else:
        target.priority = priority
        target.enabled = True
    session.flush()
    return target


def top_products_per_set(
    session: Session, per_set: int, sets: list[str] | None = None
) -> list[Product]:
    """The most valuable ungraded cards in each set -- the user's own heuristic."""
    stmt = select(Product).where(Product.ungraded_cents.isnot(None), Product.ungraded_cents > 0)
    if sets:
        stmt = stmt.where(Product.set_name.in_(sets))
    products = list(session.scalars(stmt.order_by(Product.set_name, Product.ungraded_cents.desc())))
    grouped: dict[str, list[Product]] = {}
    for product in products:
        bucket = grouped.setdefault(product.set_name, [])
        if len(bucket) < per_set:
            bucket.append(product)
    return [p for bucket in grouped.values() for p in bucket]


def candidate_products(session: Session, card_numbers: list[str], limit: int = 25) -> list[Product]:
    if not card_numbers:
        return []
    stmt = (
        select(Product)
        .where(Product.card_number.in_(card_numbers))
        .order_by(Product.ungraded_cents.desc().nullslast())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def deactivate_stale_listings(session: Session, older_than: dt.timedelta) -> int:
    cutoff = utcnow() - older_than
    stale = list(
        session.scalars(select(Listing).where(Listing.last_seen_at < cutoff, Listing.is_active))
    )
    for listing in stale:
        listing.is_active = False
    return len(stale)


def latest_scan(session: Session) -> ScanRun | None:
    return session.scalar(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1))
