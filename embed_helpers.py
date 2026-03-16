"""
embed_helpers.py — Shared embed builder for consistent ATLAS branding
─────────────────────────────────────────────────────────────────────────────
Enforces ATLAS_GOLD color, standard footer format, and field count limits.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord

from constants import ATLAS_GOLD, ATLAS_ICON_URL


def build_embed(
    title: str,
    module: str = "ATLAS",
    description: str | None = None,
    color: discord.Color | None = None,
    footer_extra: str | None = None,
) -> discord.Embed:
    """Create an embed with consistent ATLAS branding.

    Args:
        title: Embed title.
        module: Module name for footer (e.g. "Oracle", "Casino", "Codex").
        description: Optional embed description.
        color: Override color (defaults to ATLAS_GOLD).
        footer_extra: Extra text appended to footer (e.g. txn_id, stale warning).
    """
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or ATLAS_GOLD,
    )

    footer_parts = [f"ATLAS \u2014 {module}"]
    if footer_extra:
        footer_parts.append(footer_extra)
    embed.set_footer(
        text=" \u00b7 ".join(footer_parts),
        icon_url=ATLAS_ICON_URL,
    )
    return embed


def casino_result_footer(
    new_balance: int,
    txn_id: int | str | None = None,
    streak_info: dict | None = None,
) -> str:
    """Build a standardized casino result footer string.

    Format: "Balance: $X,XXX · txn: abc123 · 🔥 W5"
    """
    parts = [f"Balance: ${new_balance:,}"]
    if txn_id:
        parts.append(f"txn: {txn_id}")
    if streak_info and streak_info.get("len", 0) >= 3:
        stype = "W" if streak_info.get("type") == "win" else "L"
        parts.append(f"{'🔥' if stype == 'W' else '❄️'} {stype}{streak_info['len']}")
    return " · ".join(parts)
