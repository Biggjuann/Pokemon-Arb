"""Money is stored as integer cents everywhere. These helpers are the only
place allowed to convert to and from floats."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def to_cents(value: str | float | int | Decimal | None) -> int | None:
    """Parse a human/API price ('$12.34', '12.34', 12.34) into integer cents."""
    if value is None:
        return None
    if isinstance(value, int):
        return value * 100
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        if not cleaned:
            return None
        try:
            dec = Decimal(cleaned)
        except InvalidOperation:
            return None
    else:
        dec = Decimal(str(value))
    return int((dec * 100).quantize(Decimal("1")))


def fmt(cents: int | None, *, dash: str = "--") -> str:
    """Render cents as $1,234.56."""
    if cents is None:
        return dash
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def pct(fraction: float | None, digits: int = 0, dash: str = "--") -> str:
    if fraction is None:
        return dash
    return f"{fraction * 100:.{digits}f}%"
