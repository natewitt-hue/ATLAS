"""
casino.py — TSL Casino Main Cog
─────────────────────────────────────────────────────────────────────────────
The central Discord cog for the TSL Casino.

Player commands:
  /casino          — Hub menu with buttons to launch any game, view stats

Commissioner commands:
  /casino_status        — Health check + house P&L
  /casino_open          — Open entire casino
  /casino_close         — Close entire casino
  /casino_open_game     — Open a specific game
  /casino_close_game    — Close a specific game
  /casino_set_limits    — Adjust max bet or daily scratch range
  /casino_house_report  — P&L breakdown by game type
  /casino_clear_session — Force-clear a stuck active blackjack session
  /casino_give_scratch  — Give a bonus scratch card to a user
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import functools
import logging
import typing

import discord
from discord import app_commands
from discord.ext import commands
from atlas_colors import AtlasColors

import aiosqlite

import casino.casino_db as db
from casino.games.blackjack import start_blackjack, active_sessions as bj_sessions
from casino.games.slots     import play_slots, daily_scratch
from casino.games.crash     import join_crash, active_rounds
from casino.games.coinflip  import play_coinflip, send_challenge
log = logging.getLogger(__name__)

ADMIN_ROLE_NAME = "Commissioner"

GAME_CHOICES = typing.Literal["blackjack", "crash", "slots", "coinflip"]

# ── Ledger Feed ───────────────────────────────────────────────────────────────


async def post_to_ledger(
    bot: commands.Bot,
    guild_id: int,
    discord_id: int,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
    txn_id: int | None = None,
    extra: dict | None = None,        # NEW: game-specific metadata
) -> None:
    """Post a casino game result slip to #ledger via ledger_poster."""
    try:
        from ledger_poster import post_casino_result
        await post_casino_result(
            bot, guild_id, discord_id, game_type,
            wager, outcome, payout, multiplier, new_balance, txn_id,
        )
    except Exception:
        log.exception("Failed to post to ledger")

    # Emit FLOW event for live engagement system
    try:
        from flow_events import GameResultEvent, flow_bus
        event = GameResultEvent(
            discord_id=discord_id, guild_id=guild_id, game_type=game_type,
            wager=wager, outcome=outcome, payout=payout, multiplier=multiplier,
            new_balance=new_balance, txn_id=txn_id, extra=extra or {}
        )
        await flow_bus.emit("game_result", event)
    except Exception:
        log.exception("Failed to emit FLOW event")

# ──────────────────────────────────────────────────────────────────────────────


def _is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    return (
        interaction.user.guild_permissions.administrator
        or any(r.name == ADMIN_ROLE_NAME for r in interaction.user.roles)
    )


# ═════════════════════════════════════════════════════════════════════════════
#  HUB VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _compute_presets(max_bet: int) -> list[int]:
    """Generate 4 preset wager amounts scaled to player's tier."""
    raw = [max(1, max_bet // 10), max(1, max_bet // 4), max(1, max_bet // 2), max_bet]
    seen: set[int] = set()
    result: list[int] = []
    for v in raw:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


class CustomWagerModal(discord.ui.Modal):
    """Minimal text modal — only used when player taps 'Custom' amount."""

    def __init__(self, game: str, max_bet: int, callback):
        super().__init__(title=f"Custom Wager — {game.capitalize()}")
        self.game = game
        self.max_bet = max_bet
        self._callback = callback
        self.wager_input = discord.ui.TextInput(
            label="Wager ($)",
            placeholder=f"1 — {max_bet:,}",
            min_length=1,
            max_length=6,
        )
        self.add_item(self.wager_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            wager = int(self.wager_input.value.strip().replace(",", ""))
        except ValueError:
            return await interaction.response.send_message(
                "❌ Enter a whole number (e.g. 50).", ephemeral=True
            )
        if wager < 1:
            return await interaction.response.send_message(
                "❌ Wager must be at least **$1**.", ephemeral=True
            )
        if wager > self.max_bet:
            return await interaction.response.send_message(
                f"❌ Max bet is **${self.max_bet:,}**.", ephemeral=True
            )
        await self._callback(interaction, wager)


class WagerPresetView(discord.ui.View):
    """Preset wager buttons for a specific game. Replaces the old CasinoHubModal."""

    def __init__(self, game: str, max_bet: int, balance: int, user_id: int):
        super().__init__(timeout=60)
        self.game = game
        self.max_bet = max_bet
        self.balance = balance
        self.user_id = user_id
        self._used = False

        presets = _compute_presets(max_bet)
        for amount in presets:
            disabled = balance < amount
            btn = discord.ui.Button(
                label=f"${amount:,}",
                style=discord.ButtonStyle.success if not disabled else discord.ButtonStyle.secondary,
                disabled=disabled,
                row=0,
            )
            btn.callback = functools.partial(self._on_preset, amount=amount)
            self.add_item(btn)

        custom_btn = discord.ui.Button(
            label="Custom",
            style=discord.ButtonStyle.primary,
            emoji="✏️",
            row=0,
        )
        custom_btn.callback = self._on_custom
        self.add_item(custom_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True)
            return False
        return True

    async def _on_preset(self, interaction: discord.Interaction, *, amount: int):
        if self._used:
            return await interaction.response.send_message("Already processing...", ephemeral=True)
        self._used = True
        self.stop()
        await self._launch_game(interaction, amount)

    async def _on_custom(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            CustomWagerModal(self.game, self.max_bet, self._launch_game)
        )

    async def _launch_game(self, interaction: discord.Interaction, wager: int):
        """Route to the appropriate game start function."""
        if self.game == "blackjack":
            await start_blackjack(interaction, wager)
        elif self.game == "slots":
            await play_slots(interaction, wager)
        elif self.game == "crash":
            await join_crash(interaction, wager, interaction.client)
        elif self.game == "coinflip":
            view = CoinPickView(wager)
            await interaction.response.send_message(
                "🪙 Pick a side:", view=view, ephemeral=True
            )


class CoinPickView(discord.ui.View):
    """Heads or Tails picker for hub coinflip."""

    def __init__(self, wager: int):
        super().__init__(timeout=30)
        self.wager = wager

    @discord.ui.button(label="Heads 🌕", style=discord.ButtonStyle.primary)
    async def heads(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await play_coinflip(interaction, "heads", self.wager)

    @discord.ui.button(label="Tails 🌑", style=discord.ButtonStyle.secondary)
    async def tails(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await play_coinflip(interaction, "tails", self.wager)


class CasinoHubView(discord.ui.View):
    """Main casino lobby buttons — persistent (survives bot restarts)."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _send_wager_view(self, interaction: discord.Interaction, game: str):
        """Shared helper — show preset wager buttons for a game."""
        uid = interaction.user.id
        max_bet = await db.get_max_bet(uid)
        balance = await db.get_balance(uid)
        view = WagerPresetView(game, max_bet, balance, uid)
        emoji = {"blackjack": "🃏", "slots": "🎰", "crash": "🚀", "coinflip": "🪙"}.get(game, "🎲")
        await interaction.response.send_message(
            f"{emoji} **{game.capitalize()}** — pick your wager (Balance: ${balance:,})",
            view=view, ephemeral=True,
        )

    @discord.ui.button(label="🃏 Blackjack", style=discord.ButtonStyle.success,
                        custom_id="atlas:casino:blackjack", row=0)
    async def blackjack(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_wager_view(interaction, "blackjack")

    @discord.ui.button(label="🎰 Slots", style=discord.ButtonStyle.primary,
                        custom_id="atlas:casino:slots", row=0)
    async def slots(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_wager_view(interaction, "slots")

    @discord.ui.button(label="🚀 Crash", style=discord.ButtonStyle.danger,
                        custom_id="atlas:casino:crash", row=0)
    async def crash(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_wager_view(interaction, "crash")

    @discord.ui.button(label="🪙 Coin Flip", style=discord.ButtonStyle.secondary,
                        custom_id="atlas:casino:coinflip", row=1)
    async def coinflip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_wager_view(interaction, "coinflip")

    @discord.ui.button(label="🎟️ Daily Scratch", style=discord.ButtonStyle.secondary,
                        custom_id="atlas:casino:scratch", row=1)
    async def scratch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await daily_scratch(interaction)

    @discord.ui.button(label="📊 My Stats", style=discord.ButtonStyle.secondary,
                        custom_id="atlas:casino:stats", row=1)
    async def my_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        uid = interaction.user.id

        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute("""
                SELECT game_type,
                       COUNT(*) as hands,
                       SUM(wager) as wagered,
                       SUM(payout) as returned,
                       SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses
                FROM casino_sessions
                WHERE discord_id=?
                GROUP BY game_type
            """, (uid,)) as cur:
                rows = await cur.fetchall()

        balance = await db.get_balance(uid)

        embed = discord.Embed(
            title = f"🎰 {interaction.user.display_name}'s Casino Stats",
            color = AtlasColors.CASINO,
        )
        embed.add_field(name="Balance", value=f"**${balance:,}**", inline=False)

        total_wagered = total_returned = total_hands = 0
        for row in rows:
            game_type, hands, wagered, returned, wins, losses = row
            wagered  = wagered  or 0
            returned = returned or 0
            roi      = ((returned - wagered) / wagered * 100) if wagered else 0
            total_wagered  += wagered
            total_returned += returned
            total_hands    += hands
            embed.add_field(
                name  = f"{'🃏' if game_type == 'blackjack' else '🎰' if game_type == 'slots' else '🚀' if game_type == 'crash' else '🪙'} {game_type.replace('_',' ').title()}",
                value = (
                    f"{hands} hands | {wins}W-{losses}L\n"
                    f"Wagered: {wagered:,} | ROI: {roi:+.1f}%"
                ),
                inline = True,
            )

        if total_wagered > 0:
            total_roi = (total_returned - total_wagered) / total_wagered * 100
            embed.add_field(
                name  = "📊 Overall",
                value = (
                    f"{total_hands} total hands\n"
                    f"Wagered: {total_wagered:,}\n"
                    f"ROI: {total_roi:+.1f}%"
                ),
                inline = False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
#  THE COG
# ═════════════════════════════════════════════════════════════════════════════

class CasinoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        """Called when the cog is loaded — setup DB and reconcile orphaned wagers."""
        await db.setup_casino_db()
        # Refund any wagers orphaned by a previous crash
        refunded = await db.reconcile_orphaned_wagers()
        if refunded:
            print(f"[Casino] Reconciled {len(refunded)} orphaned wagers")

        # Register persistent view so hub buttons survive bot restarts
        self.bot.add_view(CasinoHubView())

        print("[Casino] DB ready. FLOW Casino online. 🎰")

    # ═══════════════════════════════════════════════════════════════════════
    #  /casino  — Hub
    # ═══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="casino", description="Open the TSL Casino lobby.")
    async def casino_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not await db.is_casino_open():
            return await interaction.followup.send(
                "🔴 The TSL Casino is currently closed. Check back soon!",
                ephemeral=True
            )

        uid = interaction.user.id
        balance = await db.get_balance(uid)
        tier = await db.get_player_tier(uid)
        max_bet = tier["max_bet"]
        streak = await db.get_streak(uid)
        jackpots = await db.get_jackpot_pools()

        # Build jackpot dict for card
        jp_dict = {}
        for t in ("mini", "major", "grand"):
            if t in jackpots:
                jp_dict[t] = jackpots[t]["pool"]

        # Build streak dict for card
        streak_dict = {
            "count": streak.get("len", 0),
            "type": streak.get("type", ""),
        }

        view = CasinoHubView()
        try:
            from sportsbook_cards import build_casino_hub_card, card_to_file
            png = await build_casino_hub_card(
                balance=balance, max_bet=max_bet, tier_name=tier["name"],
                streak=streak_dict, jackpots=jp_dict,
            )
            file = card_to_file(png, "casino_hub.png")
            await interaction.followup.send(file=file, view=view, ephemeral=True)
        except Exception:
            embed = discord.Embed(
                title="ATLAS Casino",
                description=(
                    f"**Balance:** ${balance:,} | **Tier:** {tier['name']}"
                    f" | **Max Bet:** ${max_bet:,}"
                ),
                color=0xD4AF37,
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ═══════════════════════════════════════════════════════════════════════
    #  INDIVIDUAL GAME COMMANDS (channel-restricted)
    # ═══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="blackjack", description="Start a blackjack hand")
    async def blackjack_cmd(self, interaction: discord.Interaction):
        bj_ch = await db.get_channel_id("blackjack")
        if bj_ch and interaction.channel_id != bj_ch:
            return await interaction.response.send_message(
                f"🃏 Play blackjack in <#{bj_ch}>!", ephemeral=True)
        uid = interaction.user.id
        max_bet = await db.get_max_bet(uid)
        balance = await db.get_balance(uid)
        view = WagerPresetView("blackjack", max_bet, balance, uid)
        await interaction.response.send_message(
            f"🃏 **Blackjack** — pick your wager (Balance: ${balance:,})",
            view=view, ephemeral=True,
        )

    @app_commands.command(name="slots", description="Spin the slot machine")
    async def slots_cmd(self, interaction: discord.Interaction):
        sl_ch = await db.get_channel_id("slots")
        if sl_ch and interaction.channel_id != sl_ch:
            return await interaction.response.send_message(
                f"🎰 Play slots in <#{sl_ch}>!", ephemeral=True)
        uid = interaction.user.id
        max_bet = await db.get_max_bet(uid)
        balance = await db.get_balance(uid)
        view = WagerPresetView("slots", max_bet, balance, uid)
        await interaction.response.send_message(
            f"🎰 **Slots** — pick your wager (Balance: ${balance:,})",
            view=view, ephemeral=True,
        )

    @app_commands.command(name="crash", description="Join a crash round")
    @app_commands.describe(wager="Amount to wager")
    async def crash_cmd(self, interaction: discord.Interaction, wager: int):
        cr_ch = await db.get_channel_id("crash")
        if cr_ch and interaction.channel_id != cr_ch:
            return await interaction.response.send_message(
                f"🚀 Play crash in <#{cr_ch}>!", ephemeral=True)
        await join_crash(interaction, wager, self.bot)

    @app_commands.command(name="coinflip", description="Flip a coin — heads or tails")
    @app_commands.describe(side="heads or tails", wager="Amount to wager")
    @app_commands.choices(side=[
        app_commands.Choice(name="Heads 🌕", value="heads"),
        app_commands.Choice(name="Tails 🌑", value="tails"),
    ])
    async def coinflip_cmd(self, interaction: discord.Interaction, side: str, wager: int):
        cf_ch = await db.get_channel_id("coinflip")
        if cf_ch and interaction.channel_id != cf_ch:
            return await interaction.response.send_message(
                f"🪙 Play coinflip in <#{cf_ch}>!", ephemeral=True)
        await play_coinflip(interaction, side, wager)

    # ═══════════════════════════════════════════════════════════════════════
    #  COMMISSIONER COMMANDS
    # ═══════════════════════════════════════════════════════════════════════

    async def _casino_status_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        report  = await db.get_house_report()
        is_open = await db.is_casino_open()
        jackpots = await db.get_jackpot_pools()

        embed = discord.Embed(title="🎰 TSL Casino Status", color=AtlasColors.CASINO)
        embed.add_field(name="Casino Open",    value="✅ Yes" if is_open else "🔴 No", inline=True)
        embed.add_field(name="Total P&L",      value=f"${report['total_pl']:+,}",   inline=True)
        embed.add_field(name="Unique Players", value=str(report["unique_players"]),        inline=True)
        embed.add_field(name="Total Hands",    value=str(report["total_hands"]),           inline=True)
        embed.add_field(name="Total Wagered",  value=f"${report['total_wagered']:,}", inline=True)
        embed.add_field(name="Active BJ Sessions", value=str(len(bj_sessions)),            inline=True)
        embed.add_field(name="Active Crash Rounds", value=str(len(active_rounds)),         inline=True)

        for g in report["by_game"]:
            embed.add_field(
                name  = g["game"].replace("_"," ").title(),
                value = f"P&L: {g['pl']:+,} | Hands: {g['hands']}",
                inline = True,
            )

        # Jackpot pools
        jp_parts = []
        for t in ("mini", "major", "grand"):
            if t in jackpots:
                jp_parts.append(f"{t.capitalize()}: ${jackpots[t]['pool']:,}")
        if jp_parts:
            embed.add_field(name="💎 Jackpots", value=" | ".join(jp_parts), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _casino_open_impl(self, interaction: discord.Interaction):
        await db.set_setting("casino_open", "1")
        await interaction.response.send_message("✅ TSL Casino is now **OPEN**. 🎰")

    async def _casino_close_impl(self, interaction: discord.Interaction):
        await db.set_setting("casino_open", "0")
        await interaction.response.send_message("🔴 TSL Casino is now **CLOSED**.")

    async def _casino_open_game_impl(self, interaction: discord.Interaction, game: GAME_CHOICES):
        await db.set_setting(f"casino_{game}_open", "1")
        await interaction.response.send_message(f"✅ **{game.capitalize()}** is now open.")

    async def _casino_close_game_impl(self, interaction: discord.Interaction, game: GAME_CHOICES):
        await db.set_setting(f"casino_{game}_open", "0")
        await interaction.response.send_message(f"🔴 **{game.capitalize()}** is now closed.")

    async def _casino_set_limits_impl(
        self,
        interaction: discord.Interaction,
        max_bet:   typing.Optional[int] = None,
        daily_min: typing.Optional[int] = None,
        daily_max: typing.Optional[int] = None,
    ):
        changes = []
        if max_bet is not None:
            await db.set_setting("casino_max_bet", str(max_bet))
            changes.append(f"Max bet → **{max_bet:,}**")
        if daily_min is not None:
            await db.set_setting("casino_daily_min", str(daily_min))
            changes.append(f"Daily min → **{daily_min:,}**")
        if daily_max is not None:
            await db.set_setting("casino_daily_max", str(daily_max))
            changes.append(f"Daily max → **{daily_max:,}**")

        if not changes:
            return await interaction.response.send_message(
                "❌ Provide at least one value to change.", ephemeral=True
            )
        await interaction.response.send_message(
            "✅ Limits updated:\n" + "\n".join(changes), ephemeral=True
        )

    async def _casino_house_report_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        report = await db.get_house_report()
        jackpots = await db.get_jackpot_pools()

        embed  = discord.Embed(
            title = "📊 TSL Casino — House P&L Report",
            color = AtlasColors.CASINO,
        )
        embed.add_field(name="Total P&L",      value=f"**${report['total_pl']:+,}**", inline=False)
        embed.add_field(name="Unique Players", value=str(report["unique_players"]),          inline=True)
        embed.add_field(name="Total Hands",    value=str(report["total_hands"]),             inline=True)
        embed.add_field(name="Total Wagered",  value=f"${report['total_wagered']:,}",   inline=True)

        for g in report["by_game"]:
            pl_str = f"{g['pl']:+,}"
            # Include 7-day rolling edge
            rolling = report.get("rolling_7d", {}).get(g["game"])
            edge_str = f" | 7d Edge: {rolling['edge_pct']}%" if rolling else ""
            embed.add_field(
                name  = g["game"].replace("_"," ").title(),
                value = f"P&L: **{pl_str}** | Hands: {g['hands']}{edge_str}",
                inline = True,
            )

        # Jackpot pools
        jp_lines = []
        for t in ("mini", "major", "grand"):
            if t in jackpots:
                jp = jackpots[t]
                jp_lines.append(f"**{t.capitalize()}**: ${jp['pool']:,} (paid: ${jp['total_paid']:,}, hits: {jp['total_hits']})")
        if jp_lines:
            embed.add_field(name="💎 Jackpot Pools", value="\n".join(jp_lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _casino_clear_session_impl(self, interaction: discord.Interaction, user: discord.Member):
        if user.id in bj_sessions:
            session = bj_sessions[user.id]
            if hasattr(session, "view") and session.view:
                session.view.stop()
            session = bj_sessions.pop(user.id)
            # Refund their wager
            await db.refund_wager(user.id, session.wager)
            await interaction.response.send_message(
                f"✅ Cleared {user.mention}'s blackjack session. "
                f"Refunded **${session.wager:,}**.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ {user.mention} has no active blackjack session.",
                ephemeral=True
            )

    async def _casino_give_scratch_impl(self, interaction: discord.Interaction, user: discord.Member):
        # Reset their last_claim so they can claim immediately
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "DELETE FROM daily_scratches WHERE discord_id=?", (user.id,)
            )
            await conn.commit()

        await interaction.response.send_message(
            f"🎟️ Bonus scratch card granted to {user.mention}! "
            f"They can claim it from the `/casino` hub.",
            ephemeral=True
        )

    async def _casino_jackpot_impl(self, interaction: discord.Interaction):
        """View current jackpot pools."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        jackpots = await db.get_jackpot_pools()
        embed = discord.Embed(title="💎 Jackpot Pools", color=AtlasColors.TSL_GOLD)
        for t in ("mini", "major", "grand"):
            if t in jackpots:
                jp = jackpots[t]
                last = f"Last: <@{jp['last_winner']}> won ${jp['last_amount']:,}" if jp['last_winner'] else "No winner yet"
                embed.add_field(
                    name=f"{t.upper()} Jackpot",
                    value=f"Pool: **${jp['pool']:,}**\nSeed: ${jp['seed']:,}\n{last}\nTotal paid: ${jp['total_paid']:,} ({jp['total_hits']} hits)",
                    inline=True,
                )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _casino_jackpot_seed_impl(self, interaction: discord.Interaction, tier: str, amount: int):
        """Add funds to a jackpot pool."""
        tier = tier.lower()
        if tier not in ("mini", "major", "grand"):
            return await interaction.response.send_message("❌ Tier must be mini, major, or grand.", ephemeral=True)
        await db.seed_jackpot(tier, amount)
        await interaction.response.send_message(f"✅ Added **${amount:,}** to **{tier.upper()}** jackpot pool.", ephemeral=True)

    async def _casino_jackpot_boost_impl(self, interaction: discord.Interaction, multiplier: float, minutes: int):
        """Temporarily boost jackpot odds for all players."""
        from datetime import datetime, timezone, timedelta
        expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        value = f"{multiplier},{expires.isoformat()}"
        await db.set_setting("casino_jackpot_boost", value)
        await interaction.response.send_message(
            f"🚀 **JACKPOT BOOST ACTIVE!** {multiplier}x odds for {minutes} minutes!\n"
            f"Expires: <t:{int(expires.timestamp())}:R>",
        )


# ── Cog registration ──────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CasinoCog(bot))
