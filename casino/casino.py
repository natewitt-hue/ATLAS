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

import typing

import discord
from discord import app_commands
from discord.ext import commands

import aiosqlite

import casino.casino_db as db
from casino.games.blackjack import start_blackjack, active_sessions as bj_sessions
from casino.games.slots     import play_slots, daily_scratch
from casino.games.crash     import join_crash, active_rounds
from casino.games.coinflip  import play_coinflip, send_challenge
from casino.renderer.card_renderer import warm_cache
from casino.renderer.ledger_renderer import render_ledger_card

ADMIN_ROLE_NAME = "Commissioner"

GAME_CHOICES = typing.Literal["blackjack", "crash", "slots", "coinflip"]

# ── Ledger Feed ───────────────────────────────────────────────────────────────

_OUTCOME_COLOR = {"win": 0x22C55E, "loss": 0xEF4444, "push": 0xF59E0B}


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
) -> None:
    """Render and post a premium ledger card to #casino-ledger."""
    try:
        # Deferred import: setup_cog and casino.py cross-reference each other
        from setup_cog import get_channel_id

        ledger_ch_id = get_channel_id("casino_ledger", guild_id)
        if not ledger_ch_id:
            return
        channel = bot.get_channel(ledger_ch_id)
        if not channel:
            return

        member = channel.guild.get_member(discord_id)
        display_name = member.display_name if member else f"User {discord_id}"

        buf = render_ledger_card(
            player_name=display_name,
            game_type=game_type,
            wager=wager,
            outcome=outcome,
            payout=payout,
            multiplier=multiplier,
            new_balance=new_balance,
        )

        embed = discord.Embed(color=_OUTCOME_COLOR.get(outcome, 0xD4AF37))
        embed.set_image(url="attachment://ledger.png")
        await channel.send(embed=embed, file=discord.File(buf, filename="ledger.png"))
    except Exception as e:
        print(f"[LEDGER] Failed to post ledger entry: {e}")

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

class CasinoHubModal(discord.ui.Modal):
    """Wager input modal — used by hub buttons to collect bet amount."""

    def __init__(self, game: str):
        super().__init__(title=f"TSL Casino — {game.capitalize()}")
        self.game = game
        self.wager_input = discord.ui.TextInput(
            label       = "Wager (TSL Bucks)",
            placeholder = "Enter amount (e.g. 50)",
            min_length  = 1,
            max_length  = 6,
        )
        self.add_item(self.wager_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            wager = int(self.wager_input.value.strip().replace(",", ""))
        except ValueError:
            return await interaction.response.send_message(
                "❌ Invalid amount. Please enter a whole number.", ephemeral=True
            )

        if self.game == "blackjack":
            await start_blackjack(interaction, wager)
        elif self.game == "slots":
            await play_slots(interaction, wager)
        elif self.game == "crash":
            await join_crash(interaction, wager, interaction.client)
        elif self.game == "coinflip":
            # Solo coinflip — pick heads or tails via followup
            await interaction.response.send_message(
                "Pick your side:", view=CoinPickView(wager), ephemeral=True
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
    """Main casino lobby buttons."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="🃏 Blackjack", style=discord.ButtonStyle.success, row=0)
    async def blackjack(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CasinoHubModal("blackjack"))

    @discord.ui.button(label="🎰 Slots", style=discord.ButtonStyle.primary, row=0)
    async def slots(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CasinoHubModal("slots"))

    @discord.ui.button(label="🚀 Crash", style=discord.ButtonStyle.danger, row=0)
    async def crash(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CasinoHubModal("crash"))

    @discord.ui.button(label="🪙 Coin Flip", style=discord.ButtonStyle.secondary, row=1)
    async def coinflip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CasinoHubModal("coinflip"))

    @discord.ui.button(label="🎟️ Daily Scratch", style=discord.ButtonStyle.secondary, row=1)
    async def scratch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await daily_scratch(interaction)

    @discord.ui.button(label="📊 My Stats", style=discord.ButtonStyle.secondary, custom_id="casino:stats", row=1)
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
            color = discord.Color.from_rgb(212, 175, 55),
        )
        embed.add_field(name="Balance", value=f"**{balance:,} TSL Bucks**", inline=False)

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

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if hasattr(self, "message") and self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════════
#  THE COG
# ═════════════════════════════════════════════════════════════════════════════

class CasinoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        """Called when the cog is loaded — setup DB and warm card cache."""
        await db.setup_casino_db()
        warm_cache()   # pre-render all 52 cards + back on startup
        print("[Casino] DB ready. Card cache warmed. TSL Casino online. 🎰")

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

        balance = await db.get_balance(interaction.user.id)
        max_bet = await db.get_max_bet()

        embed = discord.Embed(
            title       = "🎰 Welcome to the TSL Casino",
            description = (
                "**The Sim League — Gold Standard Gaming**\n\n"
                "Pick a game below to get started.\n\n"
                "🃏 **Blackjack** — Beat the dealer (3:2 BJ payout)\n"
                "🎰 **Slots** — 3-reel TSL-themed machine (up to 50x)\n"
                "🚀 **Crash** — Shared multiplier — cash out before it crashes\n"
                "🪙 **Coin Flip** — 50/50, even money\n"
                "🎟️ **Daily Scratch** — Free daily card (25–150 Bucks)\n"
            ),
            color = discord.Color.from_rgb(212, 175, 55),
        )
        embed.add_field(name="Your Balance", value=f"**{balance:,} TSL Bucks**", inline=True)
        embed.add_field(name="Max Bet",      value=f"{max_bet:,} Bucks",          inline=True)
        embed.set_footer(text="TSL Casino • The Sim League • Madden Gold Standard")

        await interaction.followup.send(embed=embed, view=CasinoHubView(), ephemeral=True)

    # ═══════════════════════════════════════════════════════════════════════
    #  COMMISSIONER COMMANDS
    # ═══════════════════════════════════════════════════════════════════════

    async def _casino_status_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        report  = await db.get_house_report()
        is_open = await db.is_casino_open()

        embed = discord.Embed(title="🎰 TSL Casino Status", color=discord.Color.teal())
        embed.add_field(name="Casino Open",    value="✅ Yes" if is_open else "🔴 No", inline=True)
        embed.add_field(name="Total P&L",      value=f"{report['total_pl']:+,} Bucks",   inline=True)
        embed.add_field(name="Unique Players", value=str(report["unique_players"]),        inline=True)
        embed.add_field(name="Total Hands",    value=str(report["total_hands"]),           inline=True)
        embed.add_field(name="Total Wagered",  value=f"{report['total_wagered']:,} Bucks", inline=True)
        embed.add_field(name="Active BJ Sessions", value=str(len(bj_sessions)),            inline=True)
        embed.add_field(name="Active Crash Rounds", value=str(len(active_rounds)),         inline=True)

        for g in report["by_game"]:
            embed.add_field(
                name  = g["game"].replace("_"," ").title(),
                value = f"P&L: {g['pl']:+,} | Hands: {g['hands']}",
                inline = True,
            )

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
        embed  = discord.Embed(
            title = "📊 TSL Casino — House P&L Report",
            color = discord.Color.teal(),
        )
        embed.add_field(name="Total P&L",      value=f"**{report['total_pl']:+,} Bucks**", inline=False)
        embed.add_field(name="Unique Players", value=str(report["unique_players"]),          inline=True)
        embed.add_field(name="Total Hands",    value=str(report["total_hands"]),             inline=True)
        embed.add_field(name="Total Wagered",  value=f"{report['total_wagered']:,} Bucks",   inline=True)

        for g in report["by_game"]:
            pl_str = f"{g['pl']:+,}"
            embed.add_field(
                name  = g["game"].replace("_"," ").title(),
                value = f"P&L: **{pl_str}** | Hands: {g['hands']}",
                inline = True,
            )

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
                f"Refunded **{session.wager:,} Bucks**.",
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


# ── Cog registration ──────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CasinoCog(bot))
