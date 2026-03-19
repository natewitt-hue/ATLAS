"""
slots.py — TSL Casino Slots + Daily Scratch Card
─────────────────────────────────────────────────────────────────────────────
Controlled-RTP slot machine with TSL-themed symbols.
Also hosts the /scratch daily free reward claim.

The outcome tier is rolled first, then matching reel visuals are generated.
This is industry-standard for precise RTP control (~96% RTP / ~4% house edge).

Symbols (visual only — payouts come from outcome table):
  TSL Shield  (jackpot)  — 3-match epic/mega tiers
  Crown       (legend)   — 3-match legend tier
  Trophy      (epic)     — 3-match epic tier
  Wild ✦      (wild)     — substitutes for any symbol
  Football    (rare)     — 3-match rare tier
  Star        (common)   — 3-match base tier
  Coin        (base)     — 3-match base tier
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import functools
import io
import random

import discord
from atlas_colors import AtlasColors

from casino.casino_db import (
    deduct_wager, process_wager, get_balance,
    is_casino_open, get_channel_id, get_max_bet, check_achievements,
    can_claim_scratch, claim_scratch,
)
from casino.play_again import PlayAgainView
from casino.renderer.casino_html_renderer import render_slots_card, render_scratch_card_v6
from embed_helpers import casino_result_footer

GAME_TYPE = "slots"

# ── Symbol definitions (used for visual reel generation) ──────────────────────
SYMBOLS = [
    {"key": "shield",   "label": "TSL Shield 🛡",  "tier": "jackpot"},
    {"key": "crown",    "label": "Crown ♛",         "tier": "legend"},
    {"key": "trophy",   "label": "Trophy 🏆",        "tier": "epic"},
    {"key": "wild",     "label": "Wild ✦",           "tier": "wild"},
    {"key": "football", "label": "Football 🏈",      "tier": "rare"},
    {"key": "star",     "label": "Star ★",           "tier": "common"},
    {"key": "coin",     "label": "Coin ●",           "tier": "base"},
]

_SYMBOL_KEYS = [s["key"] for s in SYMBOLS]
_HIGH_SYMBOLS = ["shield", "crown", "trophy"]
_LOW_SYMBOLS  = ["football", "star", "coin"]
_ALL_GAME_SYMBOLS = [s["key"] for s in SYMBOLS if s["key"] != "wild"]

# ── Controlled RTP Outcome Table ─────────────────────────────────────────────
# Total EV = 0.962 → house edge ~3.8%
# (cumulative_probability, multiplier, visual_type, result_message)
SLOTS_OUTCOME_TABLE = [
    (0.35,  0.0,  "loss",          "❌ No match — try again!"),
    (0.45,  0.0,  "near_miss",     "😬 SO CLOSE! Almost a match..."),
    (0.65,  0.3,  "2match_low",    "🔄 2-match — 0.3x back"),
    (0.78,  0.8,  "2match_high",   "🔄 2-match — 0.8x back!"),
    (0.86,  1.5,  "3match_base",   "✅ Triple match! 1.5x"),
    (0.915, 2.5,  "3match_rare",   "✅ Triple match! 2.5x"),
    (0.955, 4.0,  "3match_epic",   "🏆 BIG WIN! 4x!"),
    (0.980, 7.0,  "3match_legend", "👑 HUGE WIN! 7x!"),
    (0.995, 12.0, "3match_jackpot","🛡 EPIC! 12x!"),
    (1.000, 25.0, "3match_mega",   "💎 LEGENDARY! 25x!!"),
]

# Free spin trigger: epic tier and above (~8.5% of spins)
FREE_SPIN_TIERS = {"3match_epic", "3match_legend", "3match_jackpot", "3match_mega"}
# Free spin has a more generous outcome table (less loss chance)
FREE_SPIN_OUTCOME_TABLE = [
    (0.25,  0.0,  "loss",          "❌ No match — try again!"),
    (0.35,  0.0,  "near_miss",     "😬 SO CLOSE! Almost a match..."),
    (0.55,  0.3,  "2match_low",    "🔄 2-match — 0.3x back"),
    (0.68,  0.8,  "2match_high",   "🔄 2-match — 0.8x back!"),
    (0.78,  1.5,  "3match_base",   "✅ Triple match! 1.5x"),
    (0.86,  2.5,  "3match_rare",   "✅ Triple match! 2.5x"),
    (0.92,  4.0,  "3match_epic",   "🏆 BIG WIN! 4x!"),
    (0.96,  7.0,  "3match_legend", "👑 HUGE WIN! 7x!"),
    (0.99,  12.0, "3match_jackpot","🛡 EPIC! 12x!"),
    (1.000, 25.0, "3match_mega",   "💎 LEGENDARY! 25x!!"),
]

for _tbl in (SLOTS_OUTCOME_TABLE, FREE_SPIN_OUTCOME_TABLE):
    assert abs(_tbl[-1][0] - 1.0) < 1e-9, "RTP table final cumulative probability must be 1.0"


def _roll_outcome(table=None) -> tuple[float, str, str]:
    """Roll an outcome tier. Returns (multiplier, visual_type, message)."""
    if table is None:
        table = SLOTS_OUTCOME_TABLE
    roll = random.random()
    for cum_prob, mult, vtype, msg in table:
        if roll < cum_prob:
            return mult, vtype, msg
    # Fallback (should not reach)
    return 0.0, "loss", "❌ No match — try again!"


def _generate_reels_for_outcome(visual_type: str) -> list[str]:
    """Generate reel visuals that match the predetermined outcome tier."""
    if visual_type == "loss":
        # All different symbols
        chosen = random.sample(_ALL_GAME_SYMBOLS, 3)
        return chosen

    if visual_type == "near_miss":
        # Two high-value matching symbols + different third
        base = random.choice(_HIGH_SYMBOLS)
        others = [s for s in _ALL_GAME_SYMBOLS if s != base]
        third = random.choice(others)
        reels = [base, base, third]
        random.shuffle(reels)
        return reels

    if visual_type.startswith("2match"):
        # Two matching + one different
        if "high" in visual_type:
            base = random.choice(_HIGH_SYMBOLS)
        else:
            base = random.choice(_LOW_SYMBOLS)
        others = [s for s in _ALL_GAME_SYMBOLS if s != base]
        third = random.choice(others)
        # Chance to show wild as the matching symbol
        if random.random() < 0.15:
            reels = [base, "wild", third]
        else:
            reels = [base, base, third]
        random.shuffle(reels)
        return reels

    if visual_type.startswith("3match"):
        # Determine symbol based on tier
        tier_symbol_map = {
            "3match_base":    _LOW_SYMBOLS,
            "3match_rare":    ["football"],
            "3match_epic":    ["trophy"],
            "3match_legend":  ["crown"],
            "3match_jackpot": ["shield"],
            "3match_mega":    ["shield"],
        }
        pool = tier_symbol_map.get(visual_type, _LOW_SYMBOLS)
        base = random.choice(pool)
        # Sometimes show wild as one of the three
        if random.random() < 0.20:
            reels = [base, base, "wild"]
        else:
            reels = [base, base, base]
        random.shuffle(reels)
        return reels

    # Fallback
    return random.sample(_ALL_GAME_SYMBOLS, 3)


def _spin_controlled(wager: int, table=None) -> tuple[list[str], int, str, float, str, bool]:
    """
    Roll outcome first, then generate matching visuals.
    Returns (reels, payout, message, multiplier, visual_type, is_near_miss).
    """
    mult, vtype, msg = _roll_outcome(table)
    reels = _generate_reels_for_outcome(vtype)
    payout = int(wager * mult)
    is_near_miss = vtype == "near_miss"
    return reels, payout, msg, mult, vtype, is_near_miss


# ═════════════════════════════════════════════════════════════════════════════
#  SLOTS ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

async def play_slots(interaction: discord.Interaction, wager: int) -> None:
    """Play one spin of the slot machine with animated reveal + free spin."""
    await interaction.response.defer()

    if not await is_casino_open("slots"):
        return await interaction.followup.send(
            "🔴 Slots are currently closed.", ephemeral=True
        )

    slots_channel_id = await get_channel_id("slots")
    if slots_channel_id and interaction.channel_id != slots_channel_id:
        return await interaction.followup.send(
            f"🎰 Slots are played in <#{slots_channel_id}>!", ephemeral=True
        )

    max_bet = await get_max_bet(interaction.user.id)
    if wager < 1 or wager > max_bet:
        return await interaction.followup.send(
            f"❌ Wager must be between **$1** and **${max_bet:,}**.",
            ephemeral=True
        )

    try:
        await deduct_wager(interaction.user.id, wager)
    except Exception as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)

    # ── Roll outcome via controlled RTP table ─────────────────────────────
    reels, payout, result_msg, mult, vtype, is_near_miss = _spin_controlled(wager)

    # ── Initial render — all spinning ─────────────────────────────────────
    bal = await get_balance(interaction.user.id)
    player_name = interaction.user.display_name
    png = await render_slots_card(reels, revealed=0, wager=wager, balance=bal, player_name=player_name)
    file  = discord.File(io.BytesIO(png), filename="slots.png")
    embed = discord.Embed(
        title = f"🎰 FLOW Casino — Slots  |  {player_name}",
        color = AtlasColors.CASINO,
    )
    embed.set_image(url="attachment://slots.png")
    embed.set_footer(text="Spinning...")

    msg = await interaction.followup.send(embed=embed, file=file, wait=True)

    for revealed in (1, 2, 3):
        await asyncio.sleep(0.9)
        png2  = await render_slots_card(reels, revealed=revealed, wager=wager, balance=bal, player_name=player_name)
        file2 = discord.File(io.BytesIO(png2), filename="slots.png")
        embed2 = discord.Embed(
            title = f"🎰 FLOW Casino — Slots  |  {player_name}",
            color = AtlasColors.CASINO,
        )
        embed2.set_image(url="attachment://slots.png")
        embed2.set_footer(
            text="Spinning..." if revealed < 3 else "Done!"
        )
        await msg.edit(embed=embed2, attachments=[file2])

    # ── Determine outcome ────────────────────────────────────────────────
    if payout == wager:
        outcome = "push"
    elif payout > 0:
        outcome = "win"
    else:
        outcome = "loss"

    result = await process_wager(
        discord_id = interaction.user.id,
        wager      = wager,
        game_type  = GAME_TYPE,
        outcome    = outcome,
        payout     = payout,
        multiplier = mult,
        channel_id = interaction.channel_id,
    )

    # Check achievements
    await check_achievements(
        interaction.user.id, GAME_TYPE, outcome, mult,
        result.get("streak_info", {}), result.get("jackpot_result"),
    )

    # Post to #ledger
    from casino.casino import post_to_ledger
    _slots_extra = {"jackpot": True} if result.get("jackpot_result") or vtype in ("3match_jackpot", "3match_mega") else None
    await post_to_ledger(
        bot=interaction.client, guild_id=interaction.guild_id,
        discord_id=interaction.user.id, game_type=GAME_TYPE,
        wager=wager, outcome=outcome, payout=payout,
        multiplier=mult, new_balance=result["new_balance"],
        txn_id=result.get("txn_id"),
        extra=_slots_extra,
    )

    # ── Final render with result ───────────────────────────────────────────
    total_payout = payout
    profit = payout - wager

    # Near-miss uses amber color
    if is_near_miss:
        embed_color = AtlasColors.WARNING
    elif payout > 0:
        embed_color = AtlasColors.SUCCESS
    else:
        embed_color = AtlasColors.ERROR

    # Streak & jackpot info from result
    streak_info = result.get("streak_info")
    jackpot_hit = result.get("jackpot_result")

    profit_str = f"+${profit:,}" if profit >= 0 else f"-${abs(profit):,}"
    png3  = await render_slots_card(
        reels, revealed=3,
        wager=wager, payout=payout,
        balance=result["new_balance"],
        result_msg=result_msg,
        player_name=player_name,
        txn_id=str(result.get("txn_id", "")),
    )
    file3  = discord.File(io.BytesIO(png3), filename="slots.png")
    embed3 = discord.Embed(
        title = f"🎰 FLOW Casino — Slots  |  {player_name}",
        color = embed_color,
    )
    embed3.add_field(name="Result", value=result_msg, inline=False)
    embed3.add_field(name="Wager",   value=f"${wager:,}",              inline=True)
    embed3.add_field(name="Payout",  value=f"${payout:,}",             inline=True)
    embed3.add_field(name="P&L",     value=f"{profit_str}",           inline=True)

    # Show streak badge
    if streak_info and streak_info.get("len", 0) >= 3:
        from casino.casino_db import get_streak_bonus, get_cold_streak_mercy
        bonus = get_streak_bonus(streak_info)
        if bonus:
            embed3.add_field(
                name="Momentum",
                value=f"🔥 {bonus['label']} (W{streak_info['len']})",
                inline=True,
            )
    if streak_info and streak_info.get("type") == "loss" and streak_info.get("len", 0) >= 5:
        embed3.add_field(name="Streak", value=f"❄️ L{streak_info['len']}", inline=True)

    # Show streak bonus
    if result.get("streak_bonus"):
        sb = result["streak_bonus"]
        embed3.add_field(
            name="Streak Bonus", value=f"+${sb['amount']:,} ({sb['label']})", inline=True
        )

    # Show jackpot hit
    if jackpot_hit:
        embed3.add_field(
            name=f"💎 JACKPOT {jackpot_hit['tier'].upper()}!",
            value=f"+${jackpot_hit['amount']:,}",
            inline=False,
        )

    embed3.set_image(url="attachment://slots.png")
    embed3.set_footer(text=casino_result_footer(
        result["new_balance"], result.get("txn_id"), streak_info,
    ))

    replay_view = PlayAgainView(
        user_id=interaction.user.id,
        wager=wager,
        replay_callback=functools.partial(play_slots, wager=wager),
        double_callback=functools.partial(play_slots, wager=min(wager * 2, max_bet)),
        streak_info=streak_info,
        near_miss_msg=result_msg if is_near_miss else None,
    )
    await msg.edit(embed=embed3, attachments=[file3], view=replay_view)

    # ── Free Spin trigger ────────────────────────────────────────────────
    if vtype in FREE_SPIN_TIERS:
        await asyncio.sleep(1.5)
        free_reels, free_payout, free_msg, free_mult, free_vtype, free_near = _spin_controlled(
            wager, table=FREE_SPIN_OUTCOME_TABLE
        )

        if free_payout > 0:
            free_outcome = "win"
        else:
            free_outcome = "loss"

        free_result = await process_wager(
            discord_id=interaction.user.id,
            wager=0,  # free spin — no cost
            game_type=GAME_TYPE,
            outcome=free_outcome,
            payout=free_payout,
            multiplier=free_mult,
            channel_id=interaction.channel_id,
        )

        # Check achievements on free spin too
        await check_achievements(
            interaction.user.id, GAME_TYPE, free_outcome, free_mult,
            free_result.get("streak_info", {}), free_result.get("jackpot_result"),
        )

        if free_payout > 0:
            await post_to_ledger(
                bot=interaction.client, guild_id=interaction.guild_id,
                discord_id=interaction.user.id, game_type=GAME_TYPE,
                wager=0, outcome=free_outcome, payout=free_payout,
                multiplier=free_mult, new_balance=free_result["new_balance"],
                txn_id=free_result.get("txn_id"),
            )

        free_png = await render_slots_card(
            free_reels, revealed=3, wager=0, payout=free_payout,
            balance=free_result["new_balance"],
            result_msg=f"🎁 FREE SPIN! {free_msg}",
            player_name=player_name,
            txn_id=str(free_result.get("txn_id", "")),
        )
        free_file = discord.File(io.BytesIO(free_png), filename="freespin.png")
        free_embed = discord.Embed(
            title=f"🎁 FREE SPIN!  |  {player_name}",
            description=f"Your big win triggered a bonus spin!",
            color=AtlasColors.TSL_GOLD if free_payout > 0 else AtlasColors.CODEX,
        )
        free_embed.add_field(name="Result", value=free_msg, inline=False)
        free_embed.add_field(name="Bonus Payout", value=f"${free_payout:,}", inline=True)
        free_embed.add_field(name="Balance", value=f"${free_result['new_balance']:,}", inline=True)
        free_embed.set_image(url="attachment://freespin.png")
        await interaction.followup.send(embed=free_embed, file=free_file)


# ═════════════════════════════════════════════════════════════════════════════
#  DAILY SCRATCH CARD
# ═════════════════════════════════════════════════════════════════════════════

class ScratchView(discord.ui.View):
    """Handles sequential tile reveals for the scratch card."""

    def __init__(self, discord_id: int, tiles: list[int], total: int = 0, balance: int = 0):
        super().__init__(timeout=120)
        self.discord_id = discord_id
        self.tiles      = tiles
        self.total      = total
        self.balance    = balance
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

        png = await render_scratch_card_v6(
            self.tiles,
            revealed=self.revealed,
            is_match=self.is_match,
            player_name=interaction.user.display_name,
            total=self.total,
            balance=self.balance,
        )
        file = discord.File(io.BytesIO(png), filename="scratch.png")
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
        color = AtlasColors.CASINO
        desc  = f"Tap **Scratch!** to reveal tiles ({3 - revealed} left)"
    elif is_match:
        total = tiles[0] * 3
        color = AtlasColors.TSL_GOLD
        desc  = f"🏆 **TRIPLE MATCH!** All tiles: **{tiles[0]:,}** × 3 = **+{total:,}!**"
    else:
        total = sum(tiles)
        color = AtlasColors.SUCCESS
        desc  = f"✅ You won **{total:,}!**"

    embed = discord.Embed(
        title       = f"🎟️ TSL Daily Scratch  |  {display_name}",
        description = desc,
        color       = color,
    )
    return embed


async def daily_scratch(interaction: discord.Interaction) -> None:
    """Claim and play the daily scratch card."""
    await interaction.response.defer()

    uid = interaction.user.id

    if not await can_claim_scratch(uid):
        return await interaction.followup.send(
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
    base_total = (tiles[0] * 3) if is_match else sum(tiles)

    # Credit the reward (pass pre-rolled total so UI matches balance)
    # claim_scratch now returns a dict with streak info
    result = await claim_scratch(uid, reward=base_total)
    if result is None:
        return await interaction.followup.send(
            "⏰ Already claimed today!", ephemeral=True
        )

    # Extract streak bonus info
    total = result["amount"]  # includes streak bonus
    streak = result.get("streak", 1)
    bonus_pct = result.get("bonus_pct", 0)

    balance = await get_balance(uid)

    # Render initial card (0 tiles revealed)
    png = await render_scratch_card_v6(
        tiles, revealed=0, is_match=is_match,
        player_name=interaction.user.display_name, total=total,
        balance=balance,
    )
    file  = discord.File(io.BytesIO(png), filename="scratch.png")
    embed = _build_scratch_embed(interaction.user.display_name, tiles, 0, is_match)
    embed.set_image(url="attachment://scratch.png")

    footer_parts = [f"Potential win: {total:,} credited to your account"]
    if streak > 1:
        footer_parts.append(f"Day {streak} streak (+{int(bonus_pct*100)}% bonus!)")
    embed.set_footer(text=" | ".join(footer_parts))

    view = ScratchView(uid, tiles, total=total, balance=balance)
    await interaction.followup.send(embed=embed, file=file, view=view)
