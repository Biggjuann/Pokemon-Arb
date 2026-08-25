"""How old eBay listing data is allowed to be before we stop showing it.

eBay's API License Agreement, section 8.1(c):

    "Displayed item listing information may not be more than six (6) hours
    older than information displayed on the eBay Site, and other eBay Content
    must be no more than twenty-four (24) hours older"

So this is a *display* rule, evaluated when a page is rendered -- not a flag
written during a scan. If scans stop for any reason (rate limit, expired
credentials, a dead scheduler), cached listings have to age out on their own
rather than sit on the board looking current.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.sql.elements import ColumnElement

from .clock import utcnow
from .config import Settings
from .models import Listing

# The ceiling the licence sets. Configuration may tighten this, never loosen it.
MAX_DISPLAY_AGE = dt.timedelta(hours=6)


def display_window(settings: Settings) -> dt.timedelta:
    """The freshness window actually in force, clamped to the licence limit."""
    configured = dt.timedelta(minutes=max(1, settings.listing_freshness_minutes))
    return min(configured, MAX_DISPLAY_AGE)


def cutoff(settings: Settings, *, now: dt.datetime | None = None) -> dt.datetime:
    """Listings last seen before this instant must not be displayed."""
    return (now or utcnow()) - display_window(settings)


def fresh_clause(settings: Settings, *, now: dt.datetime | None = None) -> ColumnElement[bool]:
    """SQL predicate for 'this listing is fresh enough to show'."""
    return Listing.last_seen_at >= cutoff(settings, now=now)


def is_fresh(listing: Listing, settings: Settings, *, now: dt.datetime | None = None) -> bool:
    return listing.last_seen_at >= cutoff(settings, now=now)


def age(moment: dt.datetime, *, now: dt.datetime | None = None) -> dt.timedelta:
    return (now or utcnow()) - moment


def age_label(moment: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    """Compact human age of a timestamp, e.g. '4m', '2h 10m'."""
    if moment is None:
        return "never"
    seconds = max(0, int(age(moment, now=now).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
