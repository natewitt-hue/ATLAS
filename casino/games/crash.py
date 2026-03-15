"""
crash.py — TSL Casino Crash (Shared Round)
─────────────────────────────────────────────────────────────────────────────
Shared round crash game. One round per crash channel at a time.

Round lifecycle:
  1. First bet triggers a 60-second lobby window (countdown embed)
  2. Round starts — multiplier climbs from 1.00x
  3. Embed edits every 2 seconds showing live multiplier + who's still in
  4. "Cash Out" button available until crash
  5. Crash → all remaining players lose, results posted
  6. 30-second cooldown before next round can start
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from casino.casino_db import (
    deduct_wager, refund_wager,
    create_crash_round, get_crash_round,
    add_crash_bet, cashout_crash_bet, resolve_crash_round,
    is_casino_open, get_channel_id, get_max_bet, get_balance,
    process_wager,
)
from casino.renderer.card_renderer import render_crash_chart

if TYPE_CHECKING:
    pass

GAME_TYPE     = "crash"
LOBBY_SECS    = 60     # seconds to wait for more players after first bet
COOLDOWN_SECS = 30     # seconds between rounds
TICK_SECS     = 2.0    # embed update interval during round

# ── Active round registry: channel_id → CrashRound ───────────────────────────
active_rounds:     dict[int, "CrashRound"] = {}
recent_crashes:    dict[int, list[float]]  = {}   # channel_id → last 10 crash points


# ═════════════════════════════════════════════════════════════════════════════
#  ROUND STATE
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class PlayerBet:
    discord_id:  int
    display_name: str
    wager:       int
    bet_id:      int
    cashed_out:  bool  = False
    cashout_mult: float = 0.0


@dataclass
class CrashRound:
    round_id:    int
    channel_id:  int
    crash_point: float
    seed:        str
    message:     discord.Message | None = None

    players:     dict[int, PlayerBet] = field(default_factory=dict)
    status:      str = "lobby"     # lobby | running | crashed | cooldown
    started_at:  float = 0.0       # time.time() when round starts
    current_mult: float = 1.0

    @property
    def total_wagered(self) -> int:
        return sum(p.wager for p in self.players.values())

    @property
    def players_in(self) -> int:
        return sum(1 for p in self.players.values() if not p.cashed_out)


# ═════════════════════════════════════════════════════════════════════════════
#  MULTIPLIER CURVE
# ═════════════════════════════════════════════════════════════════════════════

def _current_multiplier(elapsed: float) -> float:
    """
    Exponential growth curve.
    1.00x at t=0, doubles roughly every 5 seconds at low values,
    accelerates as it climbs.
    """
    return round(1.0 * (1.06 ** elapsed), 2)


# ═════════════════════════════════════════════════════════════════════════════
#  ROUND RUNNER
# ═════════════════════════════════════════════════════════════════════════════

async def _run_lobby(round_obj: CrashRound, channel: discord.TextChannel) -> None:
    """60-second countdown before round starts."""
    for remaining in range(LOBBY_SECS, 0, -5):
        if round_obj.status != "lobby":
            return
        embed = _build_lobby_embed(round_obj, remaining)
        buf   = render_crash_chart(
            1.00, crashed=False,
            history=recent_crashes.get(round_obj.channel_id, []),
            players_in=len(round_obj.players),
            total_wagered=round_obj.total_wagered,
        )
        file = discord.File(buf, filename="crash.png")
        embed.set_image(url="attachment://crash.png")
        try:
            if round_obj.message is None:
                round_obj.message = await channel.send(embed=embed, file=file)
            else:
                await round_obj.message.edit(embed=embed, attachments=[file])
        except discord.HTTPException:
            pass
        await asyncio.sleep(5)


async def _run_round(round_obj: CrashRound, bot: discord.Client) -> None:
    """Main round loop — live multiplier until crash."""
    channel = bot.get_channel(round_obj.channel_id)
    if channel is None:
        return

    round_obj.status     = "running"
    round_obj.started_at = time.time()

    # Build cash out view
    view = CrashView(round_obj)

    try:
        while True:
            elapsed           = time.time() - round_obj.started_at
            round_obj.current_mult = _current_multiplier(elapsed)

            if round_obj.current_mult >= round_obj.crash_point:
                # CRASH
                round_obj.current_mult = round_obj.crash_point
                break

            buf  = render_crash_chart(
                round_obj.current_mult,
                crashed       = False,
                history       = recent_crashes.get(round_obj.channel_id, []),
                players_in    = round_obj.players_in,
                total_wagered = round_obj.total_wagered,
            )
            file  = discord.File(buf, filename="crash.png")
            embed = _build_running_embed(round_obj)
            embed.set_image(url="attachment://crash.png")
            try:
                if round_obj.message:
                    await round_obj.message.edit(embed=embed, attachments=[file], view=view)
                else:
                    round_obj.message = await channel.send(embed=embed, file=file, view=view)
            except discord.HTTPException:
                pass

            await asyncio.sleep(TICK_SECS)
    finally:
        # Ensure the view is always stopped, even on unexpected errors
        view.stop()
        view.clear_items()

    # ── Crash ──────────────────────────────────────────────────────────────
    round_obj.status = "crashed"

    # Resolve all remaining active bets
    await resolve_crash_round(round_obj.round_id)

    # Log losses for players who didn't cash out
    from casino.casino import post_to_ledger
    now = datetime.now(timezone.utc).isoformat()
    for player in round_obj.players.values():
        if not player.cashed_out:
            # Wager already deducted; just log the session
            result = await process_wager(
                discord_id = player.discord_id,
                wager      = player.wager,
                game_type  = GAME_TYPE,
                outcome    = "loss",
                payout     = 0,
                multiplier = round_obj.crash_point,
                channel_id = round_obj.channel_id,
            )
            # Post to #casino-ledger
            await post_to_ledger(
                bot=bot, guild_id=channel.guild.id,
                discord_id=player.discord_id, game_type=GAME_TYPE,
                wager=player.wager, outcome="loss", payout=0,
                multiplier=round_obj.crash_point,
                new_balance=result["new_balance"],
            )

    # Store crash point in recent history
    ch_history = recent_crashes.setdefault(round_obj.channel_id, [])
    ch_history.append(round_obj.crash_point)
    if len(ch_history) > 10:
        ch_history.pop(0)

    # Final crashed render
    buf   = render_crash_chart(
        round_obj.crash_point,
        crashed       = True,
        history       = recent_crashes.get(round_obj.channel_id, []),
        players_in    = 0,
        total_wagered = round_obj.total_wagered,
    )
    file  = discord.File(buf, filename="crash.png")
    embed = _build_crash_embed(round_obj)
    embed.set_image(url="attachment://crash.png")
    try:
        if round_obj.message:
            await round_obj.message.edit(embed=embed, attachments=[file], view=view)
        else:
            await channel.send(embed=embed, file=file)
    except discord.HTTPException:
        pass

    # ── Cooldown ───────────────────────────────────────────────────────────
    round_obj.status = "cooldown"
    active_rounds.pop(round_obj.channel_id, None)
    await asyncio.sleep(COOLDOWN_SECS)


# ═════════════════════════════════════════════════════════════════════════════
#  CASH OUT VIEW
# ═════════════════════════════════════════════════════════════════════════════

class CrashView(discord.ui.View):
    def __init__(self, round_obj: CrashRound):
        super().__init__(timeout=None)   # no timeout; crash loop controls lifecycle
        self.round_obj = round_obj

    @discord.ui.button(label="💰 Cash Out", style=discord.ButtonStyle.success)
    async def cashout(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid    = interaction.user.id
        player = self.round_obj.players.get(uid)

        if player is None:
            return await interaction.response.send_message(
                "You're not in this round!", ephemeral=True
            )
        if player.cashed_out:
            return await interaction.response.send_message(
                "You already cashed out!", ephemeral=True
            )
        if self.round_obj.status != "running":
            return await interaction.response.send_message(
                "❌ The round isn't running yet or has already crashed!", ephemeral=True
            )

        mult   = self.round_obj.current_mult

        # Set cashed_out BEFORE await to prevent TOCTOU double-cashout
        player.cashed_out   = True
        player.cashout_mult = mult

        payout = await cashout_crash_bet(player.bet_id, uid, mult)

        # Log win session
        result = await process_wager(
            discord_id = uid,
            wager      = player.wager,
            game_type  = GAME_TYPE,
            outcome    = "win",
            payout     = payout,
            multiplier = mult,
            channel_id = self.round_obj.channel_id,
        )

        # Post to #casino-ledger
        from casino.casino import post_to_ledger
        await post_to_ledger(
            bot=interaction.client, guild_id=interaction.guild_id,
            discord_id=uid, game_type=GAME_TYPE,
            wager=player.wager, outcome="win", payout=payout,
            multiplier=mult, new_balance=result["new_balance"],
        )

        profit = payout - player.wager
        await interaction.response.send_message(
            f"✅ **{interaction.user.display_name}** cashed out at **{mult:.2f}x** "
            f"— **+{profit:,} TSL Bucks!**",
            ephemeral=False,
        )


# ═════════════════════════════════════════════════════════════════════════════
#  EMBED BUILDERS
# ═════════════════════════════════════════════════════════════════════════════

def _build_lobby_embed(round_obj: CrashRound, remaining: int) -> discord.Embed:
    embed = discord.Embed(
        title       = "🚀 TSL CRASH — Lobby Open",
        description = (
            f"**Round #{round_obj.round_id}** starts in **{remaining}s**\n"
            f"Use `/crash [amount]` to join!\n\n"
            + _players_list(round_obj)
        ),
        color = discord.Color.from_rgb(212, 175, 55),
    )
    embed.add_field(name="Players", value=str(len(round_obj.players)), inline=True)
    embed.add_field(name="Total Wagered", value=f"{round_obj.total_wagered:,} Bucks", inline=True)
    return embed


def _build_running_embed(round_obj: CrashRound) -> discord.Embed:
    embed = discord.Embed(
        title       = f"🚀 TSL CRASH — {round_obj.current_mult:.2f}x",
        color       = discord.Color.from_rgb(212, 175, 55),
    )
    embed.add_field(name="Multiplier",    value=f"**{round_obj.current_mult:.2f}x**", inline=True)
    embed.add_field(name="Still In",      value=str(round_obj.players_in),            inline=True)
    embed.add_field(name="Total Wagered", value=f"{round_obj.total_wagered:,} Bucks", inline=True)
    embed.add_field(name="Players",       value=_players_list(round_obj),             inline=False)
    return embed


def _build_crash_embed(round_obj: CrashRound) -> discord.Embed:
    embed = discord.Embed(
        title       = f"💥 CRASHED @ {round_obj.crash_point:.2f}x",
        color       = discord.Color.red(),
    )
    embed.add_field(name="Crash Point",   value=f"**{round_obj.crash_point:.2f}x**", inline=True)
    embed.add_field(name="Seed (Proof)",  value=f"`{round_obj.seed}`",               inline=True)

    # Cashouts
    cashed = [(p, p.cashout_mult) for p in round_obj.players.values() if p.cashed_out]
    busted = [p for p in round_obj.players.values() if not p.cashed_out]

    if cashed:
        cashout_lines = "\n".join(
            f"✅ **{p.display_name}** — {m:.2f}x (+{int(p.wager * m) - p.wager:,})"
            for p, m in sorted(cashed, key=lambda x: x[1], reverse=True)
        )
        embed.add_field(name="💰 Cashed Out", value=cashout_lines[:1020], inline=False)

    if busted:
        bust_lines = "\n".join(
            f"❌ **{p.display_name}** — -{p.wager:,} Bucks"
            for p in busted
        )
        embed.add_field(name="💥 Busted", value=bust_lines[:1020], inline=False)

    return embed


def _players_list(round_obj: CrashRound) -> str:
    if not round_obj.players:
        return "*No players yet — be the first!*"
    lines = []
    for p in round_obj.players.values():
        if p.cashed_out:
            lines.append(f"✅ {p.display_name} ({p.cashout_mult:.2f}x)")
        else:
            lines.append(f"🎲 {p.display_name} ({p.wager:,} Bucks)")
    return "\n".join(lines[:15])   # cap at 15 to avoid embed overflow


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT — called from casino.py
# ═════════════════════════════════════════════════════════════════════════════

async def join_crash(interaction: discord.Interaction, wager: int, bot: discord.Client) -> None:
    """Join the current crash round, or start a new one."""
    await interaction.response.defer(ephemeral=True)

    uid     = interaction.user.id
    ch_id   = interaction.channel_id

    # ── Guards ─────────────────────────────────────────────────────────────
    if not await is_casino_open("crash"):
        return await interaction.followup.send(
            "🔴 Crash is currently closed.", ephemeral=True
        )

    crash_channel_id = await get_channel_id("crash")
    if crash_channel_id and ch_id != crash_channel_id:
        return await interaction.followup.send(
            f"🚀 Crash is played in <#{crash_channel_id}>!", ephemeral=True
        )

    max_bet = await get_max_bet()
    if wager < 1 or wager > max_bet:
        return await interaction.followup.send(
            f"❌ Wager must be between **1** and **{max_bet:,} TSL Bucks**.",
            ephemeral=True
        )

    # ── Check if a round is running (can't join mid-flight) ───────────────
    existing = active_rounds.get(ch_id)
    if existing and existing.status == "running":
        return await interaction.followup.send(
            f"🚀 A round is already in progress at **{existing.current_mult:.2f}x**.\n"
            "Wait for it to crash, then join the next round!",
            ephemeral=True
        )

    if existing and existing.status == "cooldown":
        return await interaction.followup.send(
            f"⏳ On cooldown — next round starts in a moment!", ephemeral=True
        )

    # ── Already in this round ──────────────────────────────────────────────
    if existing and uid in existing.players:
        return await interaction.followup.send(
            "You're already in this round!", ephemeral=True
        )

    # ── Deduct wager ───────────────────────────────────────────────────────
    try:
        await deduct_wager(uid, wager)
    except Exception as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)

    await interaction.followup.send(
        f"✅ You're in for **{wager:,} TSL Bucks**. Good luck! 🚀",
        ephemeral=True
    )

    # ── Create new round if none in lobby ─────────────────────────────────
    if existing is None:
        # Set sentinel BEFORE any await to prevent TOCTOU double-round creation
        active_rounds[ch_id] = "PENDING"

        round_id = await create_crash_round(ch_id)
        round_data = await get_crash_round(round_id)

        round_obj = CrashRound(
            round_id    = round_id,
            channel_id  = ch_id,
            crash_point = round_data["crash_point"],
            seed        = round_data["seed"],
        )
        active_rounds[ch_id] = round_obj

        # Add the first player
        bet_id = await add_crash_bet(round_id, uid, wager)
        round_obj.players[uid] = PlayerBet(
            discord_id   = uid,
            display_name = interaction.user.display_name,
            wager        = wager,
            bet_id       = bet_id,
        )

        # Run lobby countdown, then the round — async background task
        asyncio.create_task(_lobby_then_run(round_obj, bot))

    else:
        # Join existing lobby
        bet_id = await add_crash_bet(existing.round_id, uid, wager)
        existing.players[uid] = PlayerBet(
            discord_id   = uid,
            display_name = interaction.user.display_name,
            wager        = wager,
            bet_id       = bet_id,
        )


async def _lobby_then_run(round_obj: CrashRound, bot: discord.Client) -> None:
    """Background task: run lobby countdown, then run the crash round."""
    channel = bot.get_channel(round_obj.channel_id)
    if channel is None:
        return

    await _run_lobby(round_obj, channel)

    if not round_obj.players:
        # Nobody bet — shouldn't happen but guard
        active_rounds.pop(round_obj.channel_id, None)
        return

    await _run_round(round_obj, bot)
