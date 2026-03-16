"""
ledger_poster.py — ATLAS Universal Ledger Poster
─────────────────────────────────────────────────────────────────────────────
Centralized utility for posting transaction audit lines to the #ledger channel.
#ledger is a text-only audit trail; visual highlights go to #flow-live.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands


# ── Outcome → emoji mapping ───────────────────────────────────────────────
_OUTCOME_EMOJI = {"win": "✅", "loss": "❌", "push": "➖"}

# ── Game type → display label mapping ────────────────────────────────────
_GAME_LABEL = {
    "blackjack": "Blackjack",
    "slots":     "Slots",
    "crash":     "Crash",
    "coinflip":  "Coinflip",
    "scratch":   "Scratch",
    "roulette":  "Roulette",
}

# ── Transaction source → display label mapping ───────────────────────────
_SOURCE_LABEL = {
    "admin":      "Admin Adjustment",
    "stipend":    "Weekly Stipend",
    "bet_win":    "Sportsbook Win",
    "bet_loss":   "Sportsbook Loss",
    "bet_refund": "Sportsbook Refund",
    "transfer":   "Transfer",
    "reward":     "Reward",
    "penalty":    "Penalty",
}


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


def _timestamp() -> str:
    """Return a short datetime string, e.g. 'Mar 16 14:32'."""
    return datetime.utcnow().strftime("%b %d %H:%M")


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
    """Post a casino game result audit line to #ledger."""
    try:
        channel = _resolve_channel(bot, guild_id)
        if not channel:
            return

        display_name = _get_display_name(channel, discord_id)
        ts = _timestamp()
        game_label = _GAME_LABEL.get(game_type.lower(), game_type.title())
        outcome_emoji = _OUTCOME_EMOJI.get(outcome.lower(), "❓")
        outcome_label = outcome.title()

        line = (
            f"`{ts}` | **{display_name}** | {game_label} | "
            f"{outcome_emoji} {outcome_label} | "
            f"Wager: ${wager:,} | Payout: ${payout:,} | Balance: ${new_balance:,}"
        )
        if txn_id is not None:
            line += f" | `#{txn_id}`"

        await channel.send(line)
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
    """Post a general transaction audit line to #ledger."""
    try:
        channel = _resolve_channel(bot, guild_id)
        if not channel:
            return

        display_name = _get_display_name(channel, discord_id)
        ts = _timestamp()
        source_label = _SOURCE_LABEL.get(source.lower(), source.replace("_", " ").title())
        amount_sign = f"+${amount:,}" if amount >= 0 else f"-${abs(amount):,}"

        line = (
            f"`{ts}` | **{display_name}** | {source_label} | "
            f"{amount_sign} | Balance: ${balance_after:,}"
        )
        if description:
            line += f" | {description}"
        if txn_id is not None:
            line += f" | `#{txn_id}`"

        await channel.send(line)
    except Exception as e:
        print(f"[LEDGER] Failed to post transaction: {e}")
