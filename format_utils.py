"""Shared formatting utilities for ATLAS modules."""


def fmt_volume(v: float) -> str:
    """Format a volume number with K/M suffixes."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"
