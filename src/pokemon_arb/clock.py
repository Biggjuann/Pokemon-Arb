"""Single source of truth for "now".

The whole app stores naive UTC datetimes: SQLite has no timezone type, and
mixing aware and naive values is a reliable way to get a TypeError deep in a
comparison. Everything goes through :func:`utcnow`.
"""

from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    """Current UTC time, naive (no tzinfo), for storage and comparison."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)
