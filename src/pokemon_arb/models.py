"""SQLAlchemy models.

Money is integer cents. Timestamps are naive UTC.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .clock import utcnow

__all__ = [
    "Base",
    "Deal",
    "Listing",
    "PricePoint",
    "Product",
    "ScanRun",
    "Target",
    "utcnow",
]


class Base(DeclarativeBase):
    pass


class Product(Base):
    """A specific card as PriceCharting knows it, plus its comp prices."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    pc_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    set_name: Mapped[str] = mapped_column(String(255), index=True)
    card_number: Mapped[str | None] = mapped_column(String(32), index=True)
    release_date: Mapped[str | None] = mapped_column(String(32))

    # PriceCharting maps its console-era fields onto card grades:
    #   loose -> Ungraded, cib -> Grade 7, new -> Grade 8,
    #   graded -> Grade 9, box-only -> Grade 9.5, manual-only -> PSA 10
    ungraded_cents: Mapped[int | None] = mapped_column(Integer)
    grade7_cents: Mapped[int | None] = mapped_column(Integer)
    grade8_cents: Mapped[int | None] = mapped_column(Integer)
    grade9_cents: Mapped[int | None] = mapped_column(Integer)
    grade95_cents: Mapped[int | None] = mapped_column(Integer)
    psa10_cents: Mapped[int | None] = mapped_column(Integer)

    sales_volume: Mapped[int | None] = mapped_column(Integer, default=0)
    # Precomputed lowercase token bag used by the matcher.
    search_blob: Mapped[str] = mapped_column(Text, default="")
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    price_points: Mapped[list[PricePoint]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_products_set_value", "set_name", "ungraded_cents"),)

    @property
    def display_name(self) -> str:
        return f"{self.name} - {self.set_name}"


class PricePoint(Base):
    """A dated snapshot of a product's comps, so we can see drift/staleness."""

    __tablename__ = "price_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ungraded_cents: Mapped[int | None] = mapped_column(Integer)
    grade9_cents: Mapped[int | None] = mapped_column(Integer)
    psa10_cents: Mapped[int | None] = mapped_column(Integer)
    sales_volume: Mapped[int | None] = mapped_column(Integer)

    product: Mapped[Product] = relationship(back_populates="price_points")


class Listing(Base):
    """A live eBay listing."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    ebay_item_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)

    price_cents: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    buying_format: Mapped[str] = mapped_column(String(32), default="FIXED_PRICE")
    condition: Mapped[str | None] = mapped_column(String(64))

    seller_username: Mapped[str | None] = mapped_column(String(128))
    seller_feedback_pct: Mapped[float | None] = mapped_column(Float)
    seller_feedback_score: Mapped[int | None] = mapped_column(Integer)
    top_rated_seller: Mapped[bool] = mapped_column(Boolean, default=False)
    returns_accepted: Mapped[bool | None] = mapped_column(Boolean)
    item_location: Mapped[str | None] = mapped_column(String(128))

    listed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    @property
    def landed_cents(self) -> int:
        return self.price_cents + (self.shipping_cents or 0)


class Deal(Base):
    """A scored listing<->product pairing. One row per (listing, product)."""

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )

    match_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    match_reason: Mapped[str] = mapped_column(Text, default="")

    market_value_cents: Mapped[int] = mapped_column(Integer, default=0)
    adjusted_value_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    net_proceeds_cents: Mapped[int] = mapped_column(Integer, default=0)
    profit_cents: Mapped[int] = mapped_column(Integer, default=0, index=True)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)

    risk_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_penalty: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    # new | watching | dismissed | bought
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    listing: Mapped[Listing] = relationship(lazy="joined")
    product: Mapped[Product] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("listing_id", "product_id", name="uq_deal_listing_product"),
        Index("ix_deals_status_score", "status", "score"),
    )


class Target(Base):
    """A saved search: 'scan this card' or 'scan this raw query'."""

    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(255))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    set_name: Mapped[str | None] = mapped_column(String(255), index=True)
    # Higher runs first when a scan is capped by the API call budget.
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_scanned_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    product: Mapped[Product | None] = relationship(lazy="joined")

    __table_args__ = (UniqueConstraint("query", "product_id", name="uq_target_query_product"),)


class ScanRun(Base):
    """One execution of the pipeline, for observability."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")
    targets_scanned: Mapped[int] = mapped_column(Integer, default=0)
    listings_seen: Mapped[int] = mapped_column(Integer, default=0)
    listings_new: Mapped[int] = mapped_column(Integer, default=0)
    deals_found: Mapped[int] = mapped_column(Integer, default=0)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()
