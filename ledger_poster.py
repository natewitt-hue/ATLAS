"""
ledger_poster.py — ATLAS Universal Ledger Poster
─────────────────────────────────────────────────────────────────────────────
Centralized utility for posting transaction slips to the #ledger channel.
Supports both casino game results and general economy transactions.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Optional

import io

import discord
from discord.ext import commands

from casino.renderer.ledger_renderer import (
    render_ledger_card,
    render_transaction_slip,
)

# ── Outcome → embed color mapping ────────────────────────────────────────────
_OUTCOME_COLOR = {"win": 0x22C55E, "loss": 0xEF4444, "push": 0xF59E0B}
_AMOUNT_COLOR  = {"credit": 0x22C55E, "debit": 0xEF4444, "neutral": 0xD4AF37}


def _resolve_channel(bot: commands.Bot, guild_id: int):
    """Resolve the #ledger channel, returns channel object or None."""
    try:
        from setup_cog import get_channel_id
        ch_id = get_channel_id("ledger", guild_id)
        if not ch_id:
            return None
        return bot.get_channel(ch_id)
    except (ImportError, Exception):
        return None


def _get_display_name(channel, discord_id: int) -> str:
    """Resolve a discord_id to a display name."""
    if channel and channel.guild:
        member = channel.guild.get_member(discord_id)
        if member:
            return member.display_name
    return f"User {discord_id}"


async def post_casino_result(
    bot: commands.Bot,
    guild_id: int,
    discord_id: int,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
    txn_id: Optional[int] = None,
) -> None:
    """Post a casino game result slip to #ledger."""
    try:
        channel = _resolve_channel(bot, guild_id)
        if not channel:
            return

        display_name = _get_display_name(channel, discord_id)

        png_bytes = await render_ledger_card(
            player_name=display_name,
            game_type=game_type,
            wager=wager,
            outcome=outcome,
            payout=payout,
            multiplier=multiplier,
            new_balance=new_balance,
            txn_id=txn_id,
        )

        embed = discord.Embed(color=_OUTCOME_COLOR.get(outcome, 0xD4AF37))
        embed.set_image(url="attachment://ledger.png")
        await channel.send(
            embed=embed,
            file=discord.File(io.BytesIO(png_bytes), filename="ledger.png"),
        )
    except Exception as e:
        print(f"[LEDGER] Failed to post casino result: {e}")


async def post_transaction(
    bot: commands.Bot,
    guild_id: int,
    discord_id: int,
    source: str,
    amount: int,
    balance_after: int,
    description: str = "",
    txn_id: Optional[int] = None,
) -> None:
    """Post a general transaction slip to #ledger."""
    try:
        channel = _resolve_channel(bot, guild_id)
        if not channel:
            return

        display_name = _get_display_name(channel, discord_id)

        png_bytes = await render_transaction_slip(
            source=source,
            player_name=display_name,
            amount=amount,
            balance_after=balance_after,
            description=description,
            txn_id=txn_id,
        )

        color = (
            _AMOUNT_COLOR["credit"] if amount > 0
            else _AMOUNT_COLOR["debit"] if amount < 0
            else _AMOUNT_COLOR["neutral"]
        )
        embed = discord.Embed(color=color)
        embed.set_image(url="attachment://ledger.png")
        await channel.send(
            embed=embed,
            file=discord.File(io.BytesIO(png_bytes), filename="ledger.png"),
        )
    except Exception as e:
        print(f"[LEDGER] Failed to post transaction: {e}")
