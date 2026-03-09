"""
slots.py — TSL Casino Slots + Daily Scratch Card
─────────────────────────────────────────────────────────────────────────────
3-reel weighted slot machine with TSL-themed symbols.
Also hosts the /scratch daily free reward claim.

Symbols and payouts (3-match):
  TSL Shield  (jackpot)  50x  — weight 2
  Crown       (legend)   20x  — weight 5
  Trophy      (epic)     10x  — weight 8
  Football    (rare)      5x  — weight 15
  Star        (common)    3x  — weight 20
  Coin        (base)      2x  — weight 30

2-match: returns 0.5x wager for top 3 symbols only (shield/crown/trophy)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import random

import discord

from casino.casino_db import (
    deduct_wager, process_wager, get_balance,
    is_casino_open, get_channel_id, get_max_bet,
    can_claim_scratch, claim_scratch,
)
from casino.renderer.card_renderer import (
    render_slot_result, render_scratch_card,
)

GAME_TYPE = "slots"

# ── Symbol definitions ─────────────────────────────────────────────────────────
SYMBOLS = [
    {"key": "shield",   "label": "TSL Shield 🛡",  "weight": 2,  "payout_3x": 50},
    {"key": "crown",    "label": "Crown ♛",         "weight": 5,  "payout_3x": 20},
    {"key": "trophy",   "label": "Trophy 🏆",        "weight": 8,  "payout_3x": 10},
    {"key": "football", "label": "Football 🏈",      "weight": 15, "payout_3x": 5},
    {"key": "star",     "label": "Star ★",           "weight": 20, "payout_3x": 3},
    {"key": "coin",     "label": "Coin ●",           "weight": 30, "payout_3x": 2},
]

# Symbols that pay on 2-match (0.5x wager)
TWO_MATCH_SYMBOLS = {"shield", "crown", "trophy"}

_SYMBOL_KEYS    = [s["key"]    for s in SYMBOLS]
_SYMBOL_WEIGHTS = [s["weight"] for s in SYMBOLS]
_PAYOUT_MAP     = {s["key"]: s["payout_3x"] for s in SYMBOLS}


def _spin() -> list[str]:
    """Spin 3 reels, return list of 3 symbol keys."""
    return random.choices(_SYMBOL_KEYS, weights=_SYMBOL_WEIGHTS, k=3)


def _calculate_payout(reels: list[str], wager: int) -> tuple[int, str, float]:
    """
    Returns (payout, result_message, multiplier).
    payout = 0 for a loss.
    """
    a, b, c = reels

    # ── 3-match ────────────────────────────────────────────────────────────
    if a == b == c:
        mult    = _PAYOUT_MAP[a]
        payout  = wager * mult
        if a == "shield":
            msg = f"🏆 JACKPOT! TSL SHIELD x3 — {mult}x!"
        elif a == "crown":
            msg = f"👑 LEGENDARY! Crown x3 — {mult}x!"
        else:
            label = next(s["label"] for s in SYMBOLS if s["key"] == a)
            msg   = f"✅ {label} x3 — {mult}x!"
        return payout, msg, float(mult)

    # ── 2-match (top symbols only) ─────────────────────────────────────────
    counts = {k: reels.count(k) for k in set(reels)}
    for sym, cnt in counts.items():
        if cnt == 2 and sym in TWO_MATCH_SYMBOLS:
            payout = wager // 2
            label  = next(s["label"] for s in SYMBOLS if s["key"] == sym)
            return payout, f"🔄 {label} x2 — 0.5x (half back)", 0.5

    return 0, "❌ No match — try again!", 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  SLOTS ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

async def play_slots(interaction: discord.Interaction, wager: int) -> None:
    """Play one spin of the slot machine with animated reveal."""

    if not await is_casino_open("slots"):
        return await interaction.response.send_message(
            "🔴 Slots are currently closed.", ephemeral=True
        )

    slots_channel_id = await get_channel_id("slots")
    if slots_channel_id and interaction.channel_id != slots_channel_id:
        return await interaction.response.send_message(
            f"🎰 Slots are played in <#{slots_channel_id}>!", ephemeral=True
        )

    max_bet = await get_max_bet()
    if wager < 1 or wager > max_bet:
        return await interaction.response.send_message(
            f"❌ Wager must be between **1** and **{max_bet:,} TSL Bucks**.",
            ephemeral=True
        )

    try:
        await deduct_wager(interaction.user.id, wager)
    except Exception as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    reels = _spin()

    # ── Initial render — all spinning ─────────────────────────────────────
    bal = await get_balance(interaction.user.id)
    buf = render_slot_result(reels, revealed=0, wager=wager, balance=bal)
    file  = discord.File(buf, filename="slots.png")
    embed = discord.Embed(
        title = f"🎰 TSL Slots  |  {interaction.user.display_name}",
        color = discord.Color.from_rgb(212, 175, 55),
    )
    embed.set_image(url="attachment://slots.png")
    embed.set_footer(text="Spinning...")

    await interaction.response.send_message(embed=embed, file=file)

    # ── Animated reveal: reel 1 → 2 → 3 ──────────────────────────────────
    msg = await interaction.original_response()

    for revealed in (1, 2, 3):
        await asyncio.sleep(0.9)
        buf2  = render_slot_result(reels, revealed=revealed, wager=wager, balance=bal)
        file2 = discord.File(buf2, filename="slots.png")
        embed2 = discord.Embed(
            title = f"🎰 TSL Slots  |  {interaction.user.display_name}",
            color = discord.Color.from_rgb(212, 175, 55),
        )
        embed2.set_image(url="attachment://slots.png")
        embed2.set_footer(
            text="Spinning..." if revealed < 3 else "Done!"
        )
        await msg.edit(embed=embed2, attachments=[file2])

    # ── Calculate result ───────────────────────────────────────────────────
    payout, result_msg, mult = _calculate_payout(reels, wager)
    outcome = "win" if payout > 0 else "loss"

    result = await process_wager(
        discord_id = interaction.user.id,
        wager      = wager,
        game_type  = GAME_TYPE,
        outcome    = outcome,
        payout     = payout,
        multiplier = mult,
        channel_id = interaction.channel_id,
    )

    # ── Final render with result ───────────────────────────────────────────
    profit = payout - wager
    profit_str = f"+{profit:,}" if profit >= 0 else f"{profit:,}"
    buf3  = render_slot_result(
        reels, revealed=3,
        wager=wager, payout=payout,
        balance=result["new_balance"],
        result_msg=result_msg,
    )
    file3  = discord.File(buf3, filename="slots.png")
    embed3 = discord.Embed(
        title = f"🎰 TSL Slots  |  {interaction.user.display_name}",
        color = discord.Color.green() if payout > 0 else discord.Color.red(),
    )
    embed3.add_field(name="Result", value=result_msg, inline=False)
    embed3.add_field(name="Wager",   value=f"{wager:,} Bucks",              inline=True)
    embed3.add_field(name="Payout",  value=f"{payout:,} Bucks",             inline=True)
    embed3.add_field(name="P&L",     value=f"{profit_str} Bucks",           inline=True)
    embed3.set_image(url="attachment://slots.png")
    embed3.set_footer(text=f"New Balance: {result['new_balance']:,} TSL Bucks")

    await msg.edit(embed=embed3, attachments=[file3])


# ═════════════════════════════════════════════════════════════════════════════
#  DAILY SCRATCH CARD
# ═════════════════════════════════════════════════════════════════════════════

class ScratchView(discord.ui.View):
    """Handles sequential tile reveals for the scratch card."""

    def __init__(self, discord_id: int, tiles: list[int]):
        super().__init__(timeout=120)
        self.discord_id = discord_id
        self.tiles      = tiles
        self.revealed   = 0
        self.is_match   = len(set(tiles)) == 1

    @discord.ui.button(label="Scratch!", style=discord.ButtonStyle.success, emoji="🪙")
    async def scratch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            return await interaction.response.send_message(
                "This isn't your scratch card!", ephemeral=True
            )

        self.revealed += 1

        if self.revealed >= 3:
            button.disabled = True
            button.label    = "Done!"

        buf   = render_scratch_card(self.tiles, revealed=self.revealed, is_match=self.is_match)
        file  = discord.File(buf, filename="scratch.png")
        embed = _build_scratch_embed(
            interaction.user.display_name,
            self.tiles,
            self.revealed,
            self.is_match,
        )
        embed.set_image(url="attachment://scratch.png")
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self)


def _build_scratch_embed(
    display_name: str,
    tiles:        list[int],
    revealed:     int,
    is_match:     bool,
) -> discord.Embed:
    if revealed < 3:
        color = discord.Color.from_rgb(212, 175, 55)
        desc  = f"Tap **Scratch!** to reveal tiles ({3 - revealed} left)"
    elif is_match:
        total = tiles[0] * 3
        color = discord.Color.gold()
        desc  = f"🏆 **TRIPLE MATCH!** All tiles: **{tiles[0]:,}** × 3 = **+{total:,} TSL Bucks!**"
    else:
        total = sum(tiles)
        color = discord.Color.green()
        desc  = f"✅ You won **{total:,} TSL Bucks!**"

    embed = discord.Embed(
        title       = f"🎟️ TSL Daily Scratch  |  {display_name}",
        description = desc,
        color       = color,
    )
    return embed


async def daily_scratch(interaction: discord.Interaction) -> None:
    """Claim and play the daily scratch card."""
    uid = interaction.user.id

    if not await can_claim_scratch(uid):
        return await interaction.response.send_message(
            "⏰ You've already claimed your daily scratch card today.\n"
            "Come back tomorrow for another free card! (Resets at midnight UTC)",
            ephemeral=True
        )

    # Generate 3 tile values (same pool as casino_db but here for the reveal UX)
    reward_pool = [
        (25,  40),
        (50,  30),
        (75,  15),
        (100, 10),
        (150,  5),
    ]
    amounts = [r[0] for r in reward_pool]
    weights = [r[1] for r in reward_pool]
    tiles   = random.choices(amounts, weights=weights, k=3)

    # Triple match bonus
    is_match = len(set(tiles)) == 1
    total    = (tiles[0] * 3) if is_match else sum(tiles)

    # Credit the reward
    result = await claim_scratch(uid)
    if result is None:
        return await interaction.response.send_message(
            "⏰ Already claimed today!", ephemeral=True
        )

    # Render initial card (0 tiles revealed)
    buf   = render_scratch_card(tiles, revealed=0, is_match=False)
    file  = discord.File(buf, filename="scratch.png")
    embed = _build_scratch_embed(interaction.user.display_name, tiles, 0, is_match)
    embed.set_image(url="attachment://scratch.png")
    embed.set_footer(text=f"Potential win: {total:,} TSL Bucks credited to your account")

    view = ScratchView(uid, tiles)
    await interaction.response.send_message(embed=embed, file=file, view=view)
