"""
coinflip.py — TSL Casino Coin Flip
─────────────────────────────────────────────────────────────────────────────
Solo coin flip and PvP challenge system.

Solo:    /coinflip [heads/tails] [amount] — instant result, even money
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
import random
from datetime import datetime, timezone

import discord

from casino.casino_db import (
    deduct_wager, process_wager, refund_wager, get_balance,
    is_casino_open, get_channel_id, get_max_bet,
    create_challenge, get_challenge, resolve_challenge, decline_challenge,
)

GAME_TYPE = "coinflip"

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
    if not interaction.response.is_done():
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

    max_bet = await get_max_bet()
    if wager < 1 or wager > max_bet:
        return await interaction.followup.send(
            f"❌ Wager must be between **1** and **{max_bet:,} TSL Bucks**.",
            ephemeral=True
        )

    try:
        await deduct_wager(uid, wager)
    except Exception as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)

    result  = random.choice(["heads", "tails"])
    won     = result == pick_clean
    payout  = wager * 2 if won else 0
    outcome = "win" if won else "loss"

    db_result = await process_wager(
        discord_id = uid,
        wager      = wager,
        game_type  = GAME_TYPE,
        outcome    = outcome,
        payout     = payout,
        multiplier = 2.0 if won else 0.0,
        channel_id = interaction.channel_id,
    )

    # Post to #casino-ledger
    from casino.casino import post_to_ledger
    await post_to_ledger(
        bot=interaction.client, guild_id=interaction.guild_id,
        discord_id=uid, game_type=GAME_TYPE,
        wager=wager, outcome=outcome, payout=payout,
        multiplier=2.0 if won else 0.0,
        new_balance=db_result["new_balance"],
    )

    profit     = payout - wager
    profit_str = f"+{profit:,}" if profit >= 0 else f"{profit:,}"
    coin_emoji = "🌕" if result == "heads" else "🌑"
    pick_emoji = "✅" if won else "❌"

    embed = discord.Embed(
        title = f"🪙 TSL Coin Flip  |  {interaction.user.display_name}",
        color = discord.Color.green() if won else discord.Color.red(),
    )
    embed.add_field(name="Your Pick",  value=f"{pick_clean.capitalize()} {pick_emoji}", inline=True)
    embed.add_field(name="Result",     value=f"{result.capitalize()} {coin_emoji}",     inline=True)
    embed.add_field(name="Outcome",    value=f"**{'WIN' if won else 'LOSS'}** — {profit_str} Bucks", inline=True)
    embed.add_field(name="Wager",      value=f"{wager:,} Bucks",                        inline=True)
    embed.add_field(name="Payout",     value=f"{payout:,} Bucks",                       inline=True)
    embed.add_field(name="Balance",    value=f"{db_result['new_balance']:,} Bucks",      inline=True)

    await interaction.followup.send(embed=embed)


# ═════════════════════════════════════════════════════════════════════════════
#  PvP CHALLENGE
# ═════════════════════════════════════════════════════════════════════════════

class ChallengeView(discord.ui.View):
    """Accept/Decline buttons for PvP coin flip challenge."""

    def __init__(self, challenge_id: int, challenger_id: int, opponent_id: int, wager: int):
        super().__init__(timeout=300)   # 5-minute window
        self.challenge_id  = challenge_id
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.wager         = wager
        self.resolved      = False

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
        self.resolved = True  # Set immediately to prevent double-accept race

        # Deduct opponent's wager
        try:
            await deduct_wager(self.opponent_id, self.wager)
        except Exception as e:
            self.resolved = False  # Roll back if deduction failed
            return await interaction.response.send_message(
                f"❌ Insufficient funds: {e}", ephemeral=True
            )
        self.stop()
        active_challenges.pop(self.challenge_id, None)

        # Flip the coin
        winner_id = random.choice([self.challenger_id, self.opponent_id])
        loser_id  = self.opponent_id if winner_id == self.challenger_id else self.challenger_id

        payout = await resolve_challenge(
            challenge_id = self.challenge_id,
            winner_id    = winner_id,
            loser_id     = loser_id,
            wager        = self.wager,
        )

        winner_mention = f"<@{winner_id}>"
        loser_mention  = f"<@{loser_id}>"
        result_side    = random.choice(["Heads 🌕", "Tails 🌑"])
        profit         = payout - self.wager

        embed = discord.Embed(
            title = "🪙 TSL Coin Flip — PvP Result",
            color = discord.Color.gold(),
        )
        embed.add_field(
            name  = "Coin landed on",
            value = f"**{result_side}**",
            inline = False
        )
        embed.add_field(
            name  = "🏆 Winner",
            value = f"{winner_mention} — **+{profit:,} Bucks** (1.9x)",
            inline = True
        )
        embed.add_field(
            name  = "❌ Loser",
            value = f"{loser_mention} — **-{self.wager:,} Bucks**",
            inline = True
        )

        self.clear_items()
        await interaction.response.edit_message(embed=embed, view=self)

        # Post to #casino-ledger (winner + loser)
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
            title       = "🪙 TSL Coin Flip — Challenge Declined",
            description = (
                f"<@{self.opponent_id}> declined the challenge.\n"
                f"<@{self.challenger_id}> refunded **{self.wager:,} Bucks**."
            ),
            color = discord.Color.greyple(),
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
                        title       = "🪙 TSL Coin Flip — Challenge Expired",
                        description = (
                            f"<@{self.opponent_id}> didn't respond in time.\n"
                            f"<@{self.challenger_id}> refunded **{self.wager:,} Bucks**."
                        ),
                        color = discord.Color.greyple(),
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

    max_bet = await get_max_bet()
    if wager < 1 or wager > max_bet:
        return await interaction.response.send_message(
            f"❌ Wager must be between **1** and **{max_bet:,} TSL Bucks**.",
            ephemeral=True
        )

    # Deduct challenger's wager now (refunded if declined/timeout)
    try:
        await deduct_wager(uid, wager)
    except Exception as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    # Create challenge record
    challenge_id = await create_challenge(
        challenger_id = uid,
        opponent_id   = opponent.id,
        wager         = wager,
        channel_id    = interaction.channel_id,
    )

    view = ChallengeView(challenge_id, uid, opponent.id, wager)
    active_challenges[challenge_id] = view

    embed = discord.Embed(
        title       = "🪙 TSL Coin Flip — PvP Challenge",
        description = (
            f"{interaction.user.mention} has challenged {opponent.mention} "
            f"to a **{wager:,} TSL Bucks** coin flip!\n\n"
            f"Winner takes **{int(wager * 1.9):,} Bucks** (1.9x)\n\n"
            f"{opponent.mention} — you have **5 minutes** to accept or decline."
        ),
        color = discord.Color.from_rgb(212, 175, 55),
    )
    embed.set_footer(text=f"Challenge #{challenge_id}")

    await interaction.response.send_message(embed=embed, view=view)
    # Store message reference for timeout editing
    view.message = await interaction.original_response()
