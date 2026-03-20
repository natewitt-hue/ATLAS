"""
coinflip.py — TSL Casino Coin Flip
─────────────────────────────────────────────────────────────────────────────
Solo coin flip and PvP challenge system.

Solo:    /coinflip [heads/tails] [amount] — 1.95x payout (2.5% house edge)
PvP:     /challenge @user [amount]
           • Tagged user has 5 minutes to Accept/Decline via buttons
           • Winner gets 1.9x (slight house edge)
           • Challenger's wager deducted at challenge creation
           • Opponent's wager deducted on accept
           • Full refund on decline or timeout
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import functools
import io
import random
import uuid
from datetime import datetime, timezone

import discord
from atlas_colors import AtlasColors

from casino.casino_db import (
    deduct_wager, process_wager, refund_wager, get_balance,
    is_casino_open, get_channel_id, get_max_bet, check_achievements,
    create_challenge, get_challenge, resolve_challenge, decline_challenge,
)
from casino.play_again import PlayAgainView
from casino.renderer.casino_html_renderer import render_coinflip_card
from embed_helpers import casino_result_footer

GAME_TYPE          = "coinflip"
SOLO_PAYOUT_MULT   = 1.95     # 2.5% house edge (was 2.0 = 0% edge)

# ── Active PvP challenge registry: challenge_id → ChallengeView ───────────────
active_challenges: dict[int, "ChallengeView"] = {}


# ═════════════════════════════════════════════════════════════════════════════
#  SOLO COIN FLIP
# ═════════════════════════════════════════════════════════════════════════════

async def play_coinflip(
    interaction: discord.Interaction,
    pick:        str,    # "heads" or "tails"
    wager:       int,
) -> None:
    """Instant solo coin flip. Even money (1x profit)."""
    await interaction.response.defer()

    uid = interaction.user.id

    if not await is_casino_open("coinflip"):
        return await interaction.followup.send(
            "🔴 Coin flip is currently closed.", ephemeral=True
        )

    cf_channel_id = await get_channel_id("coinflip")
    if cf_channel_id and interaction.channel_id != cf_channel_id:
        return await interaction.followup.send(
            f"🪙 Coin flip is played in <#{cf_channel_id}>!", ephemeral=True
        )

    pick_clean = pick.strip().lower()
    if pick_clean not in ("heads", "tails"):
        return await interaction.followup.send(
            "❌ Pick must be **heads** or **tails**.", ephemeral=True
        )

    max_bet = await get_max_bet(uid)
    if wager < 1 or wager > max_bet:
        return await interaction.followup.send(
            f"❌ Wager must be between **$1** and **${max_bet:,}**.",
            ephemeral=True
        )

    correlation_id = uuid.uuid4().hex[:8]
    try:
        await deduct_wager(uid, wager, correlation_id=correlation_id)
    except Exception as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)

    result  = random.choice(["heads", "tails"])
    won     = result == pick_clean
    payout  = int(wager * SOLO_PAYOUT_MULT) if won else 0
    outcome = "win" if won else "loss"
    mult    = SOLO_PAYOUT_MULT if won else 0.0

    # Near-miss: ~30% of losses get a "wobble" flavor
    edge_tease = (not won) and random.random() < 0.30

    db_result = await process_wager(
        discord_id = uid,
        wager      = wager,
        game_type  = GAME_TYPE,
        outcome    = outcome,
        payout     = payout,
        multiplier = mult,
        channel_id = interaction.channel_id,
        correlation_id = correlation_id,
    )

    # Check achievements
    await check_achievements(
        uid, GAME_TYPE, outcome, mult,
        db_result.get("streak_info", {}), db_result.get("jackpot_result"),
    )

    # Post to #ledger
    from casino.casino import post_to_ledger
    await post_to_ledger(
        bot=interaction.client, guild_id=interaction.guild_id,
        discord_id=uid, game_type=GAME_TYPE,
        wager=wager, outcome=outcome, payout=payout,
        multiplier=mult,
        new_balance=db_result["new_balance"],
        txn_id=db_result.get("txn_id"),
    )

    streak_info = db_result.get("streak_info")
    profit     = payout - wager
    profit_str = f"+${profit:,}" if profit >= 0 else f"-${abs(profit):,}"

    # Edge tease flavor text for near-misses
    near_miss_text = None
    if edge_tease:
        near_miss_text = "The coin teetered on edge before falling..."

    # Render coin flip card
    png = await render_coinflip_card(
        result=result,
        player_pick=pick_clean,
        wager=wager,
        payout=payout,
        balance=db_result["new_balance"],
        player_name=interaction.user.display_name,
        txn_id=str(db_result.get("txn_id", "")),
    )
    file = discord.File(io.BytesIO(png), filename="coinflip.png")

    # Amber color for near-miss, green for win, red for loss
    if edge_tease:
        embed_color = AtlasColors.WARNING
    elif won:
        embed_color = AtlasColors.SUCCESS
    else:
        embed_color = AtlasColors.ERROR

    embed = discord.Embed(
        title = f"🪙 FLOW Casino — Coin Flip  |  {interaction.user.display_name}",
        color = embed_color,
    )
    embed.add_field(name="Your Pick",  value=f"{pick_clean.capitalize()} {'✅' if won else '❌'}", inline=True)
    embed.add_field(name="Result",     value=f"{result.capitalize()} {'🌕' if result == 'heads' else '🌑'}",     inline=True)
    embed.add_field(name="Outcome",    value=f"**{'WIN' if won else 'LOSS'}** — {profit_str}", inline=True)
    if near_miss_text:
        embed.add_field(name="😬 Close Call", value=near_miss_text, inline=False)

    # Streak info
    if streak_info and streak_info.get("len", 0) >= 3:
        from casino.casino_db import get_streak_bonus
        bonus = get_streak_bonus(streak_info)
        if bonus:
            embed.add_field(name="Momentum", value=f"🔥 {bonus['label']} (W{streak_info['len']})", inline=True)
    if streak_info and streak_info.get("type") == "loss" and streak_info.get("len", 0) >= 5:
        embed.add_field(name="Streak", value=f"❄️ L{streak_info['len']}", inline=True)

    # Streak bonus display
    if db_result.get("streak_bonus"):
        sb = db_result["streak_bonus"]
        embed.add_field(name="Streak Bonus", value=f"+${sb['amount']:,} ({sb['label']})", inline=True)

    # Jackpot hit
    if db_result.get("jackpot_result"):
        jp = db_result["jackpot_result"]
        embed.add_field(name=f"💎 JACKPOT {jp['tier'].upper()}!", value=f"+${jp['amount']:,}", inline=False)

    embed.set_image(url="attachment://coinflip.png")
    embed.set_footer(text=casino_result_footer(
        db_result["new_balance"], db_result.get("txn_id"), streak_info,
    ))

    max_bet = await get_max_bet(uid)
    replay_view = PlayAgainView(
        user_id=uid,
        wager=wager,
        replay_callback=functools.partial(play_coinflip, pick=pick_clean, wager=wager),
        double_callback=functools.partial(play_coinflip, pick=pick_clean, wager=min(wager * 2, max_bet)),
        streak_info=streak_info,
        near_miss_msg=near_miss_text,
    )
    await interaction.followup.send(embed=embed, file=file, view=replay_view)


# ═════════════════════════════════════════════════════════════════════════════
#  PvP CHALLENGE
# ═════════════════════════════════════════════════════════════════════════════

class ChallengeView(discord.ui.View):
    """Accept/Decline buttons for PvP coin flip challenge."""

    def __init__(self, challenge_id: int, challenger_id: int, opponent_id: int, wager: int, challenger_correlation_id: str = ""):
        super().__init__(timeout=300)   # 5-minute window
        self.challenge_id  = challenge_id
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.wager         = wager
        self.resolved      = False
        self.challenger_correlation_id = challenger_correlation_id
        self.opponent_correlation_id   = ""

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message(
                "❌ This challenge isn't for you!", ephemeral=True
            )
        if self.resolved:
            return await interaction.response.send_message(
                "❌ This challenge has already been resolved.", ephemeral=True
            )

        # Set resolved BEFORE any await to prevent TOCTOU double-accept
        self.resolved = True

        # Deduct opponent's wager
        self.opponent_correlation_id = uuid.uuid4().hex[:8]
        try:
            await deduct_wager(self.opponent_id, self.wager, correlation_id=self.opponent_correlation_id)
        except Exception as e:
            self.resolved = False
            return await interaction.response.send_message(
                f"❌ Insufficient funds: {e}", ephemeral=True
            )
        # Only stop view after confirmed deduct — keeps buttons alive for retry on failure
        self.stop()
        active_challenges.pop(self.challenge_id, None)

        # Flip the coin — derive winner from the flip result
        result_side = random.choice(["Heads 🌕", "Tails 🌑"])
        # Challenger is always "Heads", opponent is always "Tails"
        winner_id = self.challenger_id if "Heads" in result_side else self.opponent_id
        loser_id  = self.opponent_id if winner_id == self.challenger_id else self.challenger_id

        payout = await resolve_challenge(
            challenge_id = self.challenge_id,
            winner_id    = winner_id,
            loser_id     = loser_id,
            wager        = self.wager,
        )

        winner_mention = f"<@{winner_id}>"
        loser_mention  = f"<@{loser_id}>"
        profit         = payout - self.wager

        # Determine names
        challenger_name = interaction.guild.get_member(self.challenger_id)
        opponent_member = interaction.guild.get_member(self.opponent_id)
        challenger_display = challenger_name.display_name if challenger_name else f"User {self.challenger_id}"
        opponent_display = opponent_member.display_name if opponent_member else f"User {self.opponent_id}"
        winner_name = challenger_display if winner_id == self.challenger_id else opponent_display
        loser_name = opponent_display if winner_id == self.challenger_id else challenger_display

        result_raw = "heads" if "Heads" in result_side else "tails"
        png = await render_coinflip_card(
            result=result_raw,
            player_pick="heads",  # Challenger always picks heads
            wager=self.wager,
            payout=payout,
            balance=0,  # PvP doesn't show individual balance
            player_name=challenger_display,
            is_pvp=True,
            opponent_name=opponent_display,
            opponent_pick="tails",
        )
        file = discord.File(io.BytesIO(png), filename="coinflip.png")

        embed = discord.Embed(
            title = "🪙 FLOW Casino — PvP Coin Flip",
            color = AtlasColors.TSL_GOLD,
        )
        embed.add_field(
            name  = "Coin landed on",
            value = f"**{result_side}**",
            inline = False
        )
        embed.add_field(
            name  = "🏆 Winner",
            value = f"{winner_mention} — **+${profit:,}** (1.9x)",
            inline = True
        )
        embed.add_field(
            name  = "❌ Loser",
            value = f"{loser_mention} — **-${self.wager:,}**",
            inline = True
        )
        embed.set_image(url="attachment://coinflip.png")

        self.clear_items()
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self)

        # Post to #ledger (winner + loser)
        from casino.casino import post_to_ledger
        from casino.casino_db import get_balance
        winner_bal = await get_balance(winner_id)
        loser_bal  = await get_balance(loser_id)
        await post_to_ledger(
            bot=interaction.client, guild_id=interaction.guild_id,
            discord_id=winner_id, game_type="coinflip_pvp",
            wager=self.wager, outcome="win", payout=payout,
            multiplier=1.9, new_balance=winner_bal,
        )
        await post_to_ledger(
            bot=interaction.client, guild_id=interaction.guild_id,
            discord_id=loser_id, game_type="coinflip_pvp",
            wager=self.wager, outcome="loss", payout=0,
            multiplier=0.0, new_balance=loser_bal,
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message(
                "❌ Only the challenged player can decline.", ephemeral=True
            )
        if self.resolved:
            return await interaction.response.send_message(
                "This challenge is already resolved.", ephemeral=True
            )

        self.resolved = True
        self.stop()
        active_challenges.pop(self.challenge_id, None)

        await decline_challenge(self.challenge_id)
        await refund_wager(self.challenger_id, self.wager)

        embed = discord.Embed(
            title       = "🪙 FLOW Casino — Coin Flip — Challenge Declined",
            description = (
                f"<@{self.opponent_id}> declined the challenge.\n"
                f"<@{self.challenger_id}> refunded **${self.wager:,}**."
            ),
            color = AtlasColors.INFO,
        )
        self.clear_items()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        """Auto-decline on 5-minute timeout."""
        if not self.resolved:
            self.resolved = True
            active_challenges.pop(self.challenge_id, None)
            await decline_challenge(self.challenge_id)
            await refund_wager(self.challenger_id, self.wager)
            self.clear_items()
            if self.message:
                try:
                    embed = discord.Embed(
                        title       = "🪙 FLOW Casino — Coin Flip — Challenge Expired",
                        description = (
                            f"<@{self.opponent_id}> didn't respond in time.\n"
                            f"<@{self.challenger_id}> refunded **${self.wager:,}**."
                        ),
                        color = AtlasColors.INFO,
                    )
                    await self.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass


# ═════════════════════════════════════════════════════════════════════════════
#  PvP ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

async def send_challenge(
    interaction: discord.Interaction,
    opponent:    discord.Member,
    wager:       int,
) -> None:
    """Send a PvP coin flip challenge to another user."""
    uid = interaction.user.id

    if not await is_casino_open("coinflip"):
        return await interaction.response.send_message(
            "🔴 Coin flip is currently closed.", ephemeral=True
        )

    cf_channel_id = await get_channel_id("coinflip")
    if cf_channel_id and interaction.channel_id != cf_channel_id:
        return await interaction.response.send_message(
            f"🪙 Challenges are sent in <#{cf_channel_id}>!", ephemeral=True
        )

    if opponent.id == uid:
        return await interaction.response.send_message(
            "❌ You can't challenge yourself!", ephemeral=True
        )

    if opponent.bot:
        return await interaction.response.send_message(
            "❌ You can't challenge a bot!", ephemeral=True
        )

    max_bet = await get_max_bet(uid)
    if wager < 1 or wager > max_bet:
        return await interaction.response.send_message(
            f"❌ Wager must be between **$1** and **${max_bet:,}**.",
            ephemeral=True
        )

    # Deduct challenger's wager now (refunded if declined/timeout)
    challenger_correlation_id = uuid.uuid4().hex[:8]
    try:
        await deduct_wager(uid, wager, correlation_id=challenger_correlation_id)
    except Exception as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    # Create challenge record
    challenge_id = await create_challenge(
        challenger_id = uid,
        opponent_id   = opponent.id,
        wager         = wager,
        channel_id    = interaction.channel_id,
    )

    view = ChallengeView(challenge_id, uid, opponent.id, wager, challenger_correlation_id=challenger_correlation_id)
    active_challenges[challenge_id] = view

    embed = discord.Embed(
        title       = "🪙 FLOW Casino — Coin Flip — PvP Challenge",
        description = (
            f"{interaction.user.mention} has challenged {opponent.mention} "
            f"to a **${wager:,}** coin flip!\n\n"
            f"Winner takes **${int(wager * 1.9):,}** (1.9x)\n\n"
            f"{opponent.mention} — you have **5 minutes** to accept or decline."
        ),
        color = AtlasColors.CASINO,
    )
    embed.set_footer(text=f"Challenge #{challenge_id}")

    await interaction.response.send_message(embed=embed, view=view)
    # Store message reference for timeout editing
    view.message = await interaction.original_response()
