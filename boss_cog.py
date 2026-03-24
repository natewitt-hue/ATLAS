"""
boss_cog.py — ATLAS Commissioner Control Room (/boss)
─────────────────────────────────────────────────────────────────────────────
Visual hub replacing the 57 /commish slash subcommands with a single /boss
command that opens an ephemeral button-driven control room.

Architecture:
    /boss  →  BossHubView (7 panel buttons)
                ├── Sportsbook  →  sub-panels (Lines & Locks, Bets & Props)
                ├── Casino
                ├── Treasury    →  sub-panels (Balances, Stipends, Bulk Ops)
                ├── Roster      →  dev traits, ability audit/check/reassign, contracts, assignments
                ├── Markets     →  polymarket + real sportsbook
                ├── League      →  genesis trades + awards + codex
                └── Compliance  →  sentinel

Every button/modal delegates to existing _impl methods on target cogs.
No logic is duplicated — boss_cog is a pure UI layer.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_commissioner
import data_manager as dm
import roster

# Ability engine — optional
try:
    import ability_engine as ae
    _AE_AVAILABLE = True
except ImportError:
    ae = None
    _AE_AVAILABLE = False

log = logging.getLogger("atlas.boss")

from atlas_colors import AtlasColors

COMMISH_COLOR = AtlasColors.COMMISH
GOLD = AtlasColors.TSL_GOLD

VALID_CASINO_GAMES = ("blackjack", "crash", "slots", "coinflip")
VALID_INTERVALS = ("daily", "weekly", "biweekly", "monthly")
VALID_PROP_RESULTS = ("a", "b", "push")
VALID_MARKET_RESULTS = ("YES", "NO", "VOID")
VALID_SPORT_KEYS = (
    ("americanfootball_nfl", "NFL"), ("basketball_nba", "NBA"),
    ("baseball_mlb", "MLB"), ("icehockey_nhl", "NHL"),
    ("americanfootball_ncaaf", "NCAAF"), ("basketball_ncaab", "NCAAB"),
    ("soccer_epl", "EPL"), ("mma_mixed_martial_arts", "MMA"),
)

TIER_EMOJI = {"S": "🔴", "A": "🟠", "B": "🟡", "C": "⚪"}
DEV_EMOJI = {
    "Normal": "⚪",
    "Star": "⭐",
    "Superstar": "🌟",
    "Superstar X-Factor": "⚡",
}

def _dev_badge(dev: str) -> str:
    return f"{DEV_EMOJI.get(dev, '')} {dev}"


# ══════════════════════════════════════════════════════════════════════════════
# Shared Utilities
# ══════════════════════════════════════════════════════════════════════════════

async def _resolve_member(
    interaction: discord.Interaction, text: str,
) -> Optional[discord.Member]:
    """Resolve a text input to a discord.Member.

    Resolution order:
        1. Raw Discord ID or mention format
        2. TSL nickname / db_username / discord_username via roster
        3. Guild display name match
    """
    text = text.strip()
    guild = interaction.guild
    if not guild:
        return None

    # 1. Discord ID / mention
    try:
        did = int(text.replace("<@", "").replace(">", "").replace("!", ""))
        try:
            return guild.get_member(did) or await guild.fetch_member(did)
        except discord.NotFound:
            pass
    except ValueError:
        pass

    # 2. Roster lookup (nickname, db_username, discord_username)
    try:
        text_lower = text.lower()
        for entry in roster.get_all():
            if (
                (entry.nickname and entry.nickname.lower() == text_lower)
                or (entry.discord_username and entry.discord_username.lower() == text_lower)
                or (entry.db_username and entry.db_username.lower() == text_lower)
            ):
                try:
                    return guild.get_member(entry.discord_id) or await guild.fetch_member(entry.discord_id)
                except discord.NotFound:
                    pass
    except Exception:
        pass

    # 3. Display name match
    text_lower = text.lower()
    for m in guild.members:
        if m.display_name.lower() == text_lower or m.name.lower() == text_lower:
            return m

    return None


async def _resolve_role(
    interaction: discord.Interaction, text: str,
) -> Optional[discord.Role]:
    """Resolve a text input to a discord.Role by name match."""
    guild = interaction.guild
    if not guild:
        return None
    text_lower = text.strip().lower()
    for role in guild.roles:
        if role.name.lower() == text_lower:
            return role
    return None


async def _send_not_found(interaction: discord.Interaction, what: str, text: str):
    """Send a standardized 'not found' error."""
    msg = f"❌ Could not find {what} **{text}**. Try their Discord ID or exact name."
    if not interaction.response.is_done():
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.followup.send(msg, ephemeral=True)


async def _send_cog_error(interaction: discord.Interaction, name: str):
    """Send a standardized 'cog not loaded' error."""
    msg = f"❌ {name} module is not loaded."
    if not interaction.response.is_done():
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.followup.send(msg, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# Embed Builders
# ══════════════════════════════════════════════════════════════════════════════

def _home_embed(interaction: discord.Interaction) -> discord.Embed:
    embed = discord.Embed(
        title="\U0001f3db\ufe0f ATLAS Commissioner Control Room",
        description=f"Welcome back, **{interaction.user.display_name}**.",
        color=COMMISH_COLOR,
    )
    try:
        import data_manager as dm
        embed.add_field(name="Season", value=str(dm.CURRENT_SEASON), inline=True)
        embed.add_field(name="Week", value=str(dm.CURRENT_WEEK), inline=True)
    except Exception:
        pass
    try:
        count = len(roster.get_all())
        embed.add_field(name="Owners", value=str(count), inline=True)
    except Exception:
        pass
    embed.set_footer(text="ATLAS\u2122 Boss")
    return embed


def _panel_embed(title: str, desc: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=desc, color=COMMISH_COLOR)
    embed.set_footer(text="ATLAS\u2122 Boss")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# HOME PANEL
# ══════════════════════════════════════════════════════════════════════════════

class BossHubView(discord.ui.View):
    """Commissioner Control Room — 7 panel navigation buttons."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Sportsbook", emoji="\U0001f4ca", style=discord.ButtonStyle.primary, row=0)
    async def sb(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4ca Sportsbook Admin", "Manage TSL sportsbook lines, bets, and props."),
            view=SBPanelView(self.bot),
        )

    @discord.ui.button(label="Casino", emoji="\U0001f3b0", style=discord.ButtonStyle.primary, row=0)
    async def casino(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f3b0 Casino Admin", "Open/close games, set limits, manage sessions."),
            view=CasinoPanelView(self.bot),
        )

    @discord.ui.button(label="Treasury", emoji="\U0001f4b0", style=discord.ButtonStyle.primary, row=0)
    async def treasury(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4b0 Treasury Admin", "Manage TSL Bucks balances, stipends, and bulk operations."),
            view=TreasuryPanelView(self.bot),
        )

    @discord.ui.button(label="Roster", emoji="📋", style=discord.ButtonStyle.primary, row=0)
    async def roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("📋 Roster Admin", "Dev traits, ability audits, contracts, and team assignments."),
            view=RosterPanelView(self.bot),
        )

    @discord.ui.button(label="Markets", emoji="\U0001f4c8", style=discord.ButtonStyle.secondary, row=1)
    async def markets(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4c8 Markets Admin", "Prediction markets and real sportsbook management."),
            view=MarketsPanelView(self.bot),
        )

    @discord.ui.button(label="League", emoji="\U0001f3c8", style=discord.ButtonStyle.secondary, row=1)
    async def league(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f3c8 League Admin", "Trades, lottery, polls, and debug tools."),
            view=LeaguePanelView(self.bot),
        )

    @discord.ui.button(label="Compliance", emoji="\u2696\ufe0f", style=discord.ButtonStyle.secondary, row=1)
    async def compliance(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\u2696\ufe0f Compliance Admin", "Cases, force requests, and position changes."),
            view=CompliancePanelView(self.bot),
        )

    @discord.ui.button(label="FLOW Live", emoji="📡", style=discord.ButtonStyle.secondary, row=1)
    async def flow_live(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("📡 FLOW Live Admin", "Pulse dashboard, highlights, and session management."),
            view=FlowLivePanelView(self.bot),
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if hasattr(self, "message") and self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# SPORTSBOOK PANEL
# ══════════════════════════════════════════════════════════════════════════════

class SBPanelView(discord.ui.View):
    """Sportsbook admin — sub-panel navigation + quick actions."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Lines & Locks", emoji="\U0001f4cf", style=discord.ButtonStyle.primary, row=0)
    async def lines(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4cf Lines & Locks", "Set spreads, moneylines, O/U, and lock games."),
            view=SBLinesPanelView(self.bot),
        )

    @discord.ui.button(label="Bets & Props", emoji="\U0001f3b2", style=discord.ButtonStyle.primary, row=0)
    async def bets(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f3b2 Bets & Props", "Grade bets, refund, balance adjustments, and prop management."),
            view=SBBetsPanelView(self.bot),
        )

    @discord.ui.button(label="Status", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_status_impl(interaction)

    @discord.ui.button(label="Lock All", emoji="\U0001f512", style=discord.ButtonStyle.danger, row=1)
    async def lockall(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lockall_impl(interaction)

    @discord.ui.button(label="Unlock All", emoji="\U0001f513", style=discord.ButtonStyle.success, row=1)
    async def unlockall(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_unlockall_impl(interaction)

    @discord.ui.button(label="Sync All", emoji="\U0001f504", style=discord.ButtonStyle.secondary, row=1)
    async def sync_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await interaction.response.defer(thinking=True, ephemeral=True)
        from datetime import datetime, timezone
        from real_sportsbook_cog import SPORT_SEASONS
        now = datetime.now(timezone.utc)
        synced_sports = []
        for sport_key, cfg in SPORT_SEASONS.items():
            if now.month in cfg["months"]:
                await cog._sync_odds(sport_key)
                synced_sports.append(sport_key)
        await cog._sync_scores()
        await interaction.followup.send(
            f"Synced odds for {len(synced_sports)} in-season sport(s) + scores.",
            ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Sportsbook Sub-Panel: Lines & Locks ──────────────────────────────────────

class SBLinesPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Set Spread", style=discord.ButtonStyle.primary, row=0)
    async def set_spread(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a matchup to set spread:", view=SBMatchupSelectView("spread"), ephemeral=True,
        )

    @discord.ui.button(label="Set ML", style=discord.ButtonStyle.primary, row=0)
    async def set_ml(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a matchup to set moneyline:", view=SBMatchupSelectView("ml"), ephemeral=True,
        )

    @discord.ui.button(label="Set O/U", style=discord.ButtonStyle.primary, row=0)
    async def set_ou(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a matchup to set O/U:", view=SBMatchupSelectView("ou"), ephemeral=True,
        )

    @discord.ui.button(label="Reset All Lines", emoji="\U0001f504", style=discord.ButtonStyle.danger, row=1)
    async def reset_lines(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_resetlines_impl(interaction)

    @discord.ui.button(label="View Lines", emoji="\U0001f4ca", style=discord.ButtonStyle.secondary, row=1)
    async def view_lines(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lines_impl(interaction)

    @discord.ui.button(label="Lock Game", emoji="\U0001f512", style=discord.ButtonStyle.secondary, row=2)
    async def lock_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a matchup to lock/unlock:", view=SBMatchupSelectView("lock"), ephemeral=True,
        )

    @discord.ui.button(label="Cancel Game", emoji="\u274c", style=discord.ButtonStyle.danger, row=2)
    async def cancel_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a matchup to cancel:", view=SBMatchupSelectView("cancel"), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4ca Sportsbook Admin", "Manage TSL sportsbook lines, bets, and props."),
            view=SBPanelView(self.bot),
        )


# ── Sportsbook Sub-Panel: Bets & Props ──────────────────────────────────────

class SBBetsPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Open Bets", emoji="\U0001f4cb", style=discord.ButtonStyle.primary, row=0)
    async def open_bets(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog") or interaction.client.get_cog("SportsbookCog")
        sb_cog = interaction.client.get_cog("SportsbookCog")
        if not sb_cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await sb_cog._open_bets_impl(interaction)

    @discord.ui.button(label="Settled Bets", emoji="\U0001f4c4", style=discord.ButtonStyle.primary, row=0)
    async def settled_bets(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        sb_cog = interaction.client.get_cog("SportsbookCog")
        if not sb_cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await sb_cog._settled_bets_impl(interaction)

    @discord.ui.button(label="Grade Week", emoji="\u2705", style=discord.ButtonStyle.primary, row=1)
    async def grade(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossSBGradeModal())

    @discord.ui.button(label="Force Settle", emoji="\u2696\ufe0f", style=discord.ButtonStyle.danger, row=1)
    async def force_settle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossSBForceSettleModal())

    @discord.ui.button(label="AG Status", emoji="\U0001f4ca", style=discord.ButtonStyle.secondary, row=1)
    async def ag_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        sb_cog = interaction.client.get_cog("SportsbookCog")
        if not sb_cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await sb_cog._autograde_status_impl(interaction)

    @discord.ui.button(label="Refund Bet", emoji="\U0001f4b8", style=discord.ButtonStyle.secondary, row=2)
    async def refund(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossSBRefundModal())

    @discord.ui.button(label="Balance Adjust", emoji="\U0001f4b0", style=discord.ButtonStyle.secondary, row=2)
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to adjust balance:", view=SBBalanceMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Add Prop", emoji="\U0001f4dd", style=discord.ButtonStyle.primary, row=3)
    async def add_prop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossSBAddPropModal())

    @discord.ui.button(label="Settle Prop", emoji="\u2696\ufe0f", style=discord.ButtonStyle.secondary, row=3)
    async def settle_prop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select the prop result:", view=SettlePropSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4ca Sportsbook Admin", "Manage TSL sportsbook lines, bets, and props."),
            view=SBPanelView(self.bot),
        )


# ── Sportsbook Modals ────────────────────────────────────────────────────────

class SBMatchupSelectView(discord.ui.View):
    """Dynamic matchup select populated from current week games."""
    def __init__(self, mode: str):
        super().__init__(timeout=120)
        self.mode = mode
        import data_manager as dm
        games = dm.df_games
        if games is not None and not games.empty and "matchup_key" in games.columns:
            options = [
                discord.SelectOption(label=mk, value=mk)
                for mk in games["matchup_key"].unique()
            ][:25]
        else:
            options = [discord.SelectOption(label="No games loaded", value="none")]
        select = discord.ui.Select(placeholder="Select a matchup...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        matchup = interaction.data["values"][0]
        if matchup == "none":
            return await interaction.response.send_message("❌ No games available.", ephemeral=True)
        if self.mode == "spread":
            await interaction.response.send_modal(SBSpreadFollowUpModal(matchup))
        elif self.mode == "ml":
            await interaction.response.send_modal(SBMLFollowUpModal(matchup))
        elif self.mode == "ou":
            await interaction.response.send_modal(SBOUFollowUpModal(matchup))
        elif self.mode == "lock":
            await interaction.response.send_message(
                f"**{matchup}** \u2014 Lock or Unlock?",
                view=SBLockConfirmView(matchup), ephemeral=True,
            )
        elif self.mode == "cancel":
            await interaction.response.send_message(
                f"\u26a0\ufe0f Cancel **{matchup}** and refund all bets?",
                view=SBCancelConfirmView(matchup), ephemeral=True,
            )


class SBSpreadFollowUpModal(discord.ui.Modal, title="Set Spread Override"):
    home_spread = discord.ui.TextInput(label="Home Spread", placeholder="e.g., -3.5", required=True)

    def __init__(self, matchup: str):
        super().__init__()
        self.matchup = matchup

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            spread = float(self.home_spread.value)
        except ValueError:
            return await interaction.response.send_message("❌ Spread must be a number.", ephemeral=True)
        await cog._sb_setspread_impl(interaction, self.matchup, spread)


class SBMLFollowUpModal(discord.ui.Modal, title="Set Moneyline Override"):
    home_ml = discord.ui.TextInput(label="Home ML", placeholder="e.g., -150", required=True)
    away_ml = discord.ui.TextInput(label="Away ML", placeholder="e.g., +130", required=True)

    def __init__(self, matchup: str):
        super().__init__()
        self.matchup = matchup

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            hml = int(self.home_ml.value)
            aml = int(self.away_ml.value)
        except ValueError:
            return await interaction.response.send_message("❌ Moneylines must be integers.", ephemeral=True)
        await cog._sb_setml_impl(interaction, self.matchup, hml, aml)


class SBOUFollowUpModal(discord.ui.Modal, title="Set Over/Under Override"):
    ou_line = discord.ui.TextInput(label="O/U Total", placeholder="e.g., 45.5", required=True)

    def __init__(self, matchup: str):
        super().__init__()
        self.matchup = matchup

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            ou = float(self.ou_line.value)
        except ValueError:
            return await interaction.response.send_message("❌ O/U total must be a number.", ephemeral=True)
        await cog._sb_setou_impl(interaction, self.matchup, ou)


class SBLockConfirmView(discord.ui.View):
    def __init__(self, matchup: str):
        super().__init__(timeout=60)
        self.matchup = matchup

    @discord.ui.button(label="Lock", emoji="\U0001f512", style=discord.ButtonStyle.danger)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lock_impl(interaction, self.matchup, True)

    @discord.ui.button(label="Unlock", emoji="\U0001f513", style=discord.ButtonStyle.success)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lock_impl(interaction, self.matchup, False)


class SBCancelConfirmView(discord.ui.View):
    def __init__(self, matchup: str):
        super().__init__(timeout=30)
        self.matchup = matchup

    @discord.ui.button(label="Confirm Cancel & Refund", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_cancelgame_impl(interaction, self.matchup)

    @discord.ui.button(label="Nevermind", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BossSBGradeModal(discord.ui.Modal, title="Grade Bets for Week"):
    week = discord.ui.TextInput(label="Week Number", placeholder="e.g., 8", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Manual grading is no longer supported — settlement is automatic via the event bus.",
            ephemeral=True,
        )


class BossSBRefundModal(discord.ui.Modal, title="Refund a Bet"):
    bet_id = discord.ui.TextInput(label="Bet ID", placeholder="e.g., 42", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            bid = int(self.bet_id.value)
        except ValueError:
            return await interaction.response.send_message("❌ Bet ID must be a number.", ephemeral=True)
        await cog._sb_refund_impl(interaction, bid)


class BossSBForceSettleModal(discord.ui.Modal, title="Force-Settle a Bet"):
    bet_id = discord.ui.TextInput(label="Bet ID", placeholder="e.g., 42", required=True)
    result = discord.ui.TextInput(label="Result", placeholder="Won / Lost / Push / Cancelled", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            bid = int(self.bet_id.value)
        except ValueError:
            return await interaction.response.send_message("❌ Bet ID must be a number.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)
        await cog._force_settle_impl(interaction, bid, self.result.value)


class SBBalanceMemberSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member...")
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.send_modal(SBBalanceFollowUpModal(select.values[0]))


class SBBalanceFollowUpModal(discord.ui.Modal, title="Adjust Member Balance"):
    adjustment = discord.ui.TextInput(label="Adjustment (+/-)", placeholder="e.g., 500 or -200", required=True)
    reason = discord.ui.TextInput(
        label="Reason", placeholder="Commissioner adjustment", required=False,
        default="Commissioner adjustment",
    )

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            adj = int(self.adjustment.value)
        except ValueError:
            return await interaction.response.send_message("❌ Adjustment must be an integer.", ephemeral=True)
        reason = self.reason.value or "Commissioner adjustment"
        await cog._sb_balance_impl(interaction, self.member, adj, reason)


class BossSBAddPropModal(discord.ui.Modal, title="Create Prop Bet"):
    description = discord.ui.TextInput(
        label="Prop Description", placeholder="e.g., Will JT throw 5+ TDs?",
        required=True, style=discord.TextStyle.paragraph,
    )
    option_a = discord.ui.TextInput(label="Option A", placeholder="e.g., Yes", required=True)
    option_b = discord.ui.TextInput(label="Option B", placeholder="e.g., No", required=True)
    odds_a = discord.ui.TextInput(label="Odds A (default -110)", placeholder="-110", required=False)
    odds_b = discord.ui.TextInput(label="Odds B (default -110)", placeholder="-110", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            oa = int(self.odds_a.value) if self.odds_a.value.strip() else -110
            ob = int(self.odds_b.value) if self.odds_b.value.strip() else -110
        except ValueError:
            return await interaction.response.send_message("❌ Odds must be integers.", ephemeral=True)
        await cog._sb_addprop_impl(
            interaction, self.description.value, self.option_a.value, self.option_b.value, oa, ob,
        )


class SettlePropSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Select result...",
        options=[
            discord.SelectOption(label="Option A", value="a"),
            discord.SelectOption(label="Option B", value="b"),
            discord.SelectOption(label="Push (refund)", value="push"),
        ],
    )
    async def result_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(SettlePropFollowUpModal(select.values[0]))


class SettlePropFollowUpModal(discord.ui.Modal, title="Settle Prop Bet"):
    prop_id = discord.ui.TextInput(label="Prop ID", placeholder="e.g., 5", required=True)

    def __init__(self, result: str):
        super().__init__()
        self.result = result

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            pid = int(self.prop_id.value)
        except ValueError:
            return await interaction.response.send_message("❌ Prop ID must be a number.", ephemeral=True)
        await cog._sb_settleprop_impl(interaction, pid, self.result)


# ══════════════════════════════════════════════════════════════════════════════
# CASINO PANEL
# ══════════════════════════════════════════════════════════════════════════════

class CasinoPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Open Casino", emoji="\U0001f7e2", style=discord.ButtonStyle.success, row=0)
    async def open_casino(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_open_impl(interaction)

    @discord.ui.button(label="Close Casino", emoji="\U0001f534", style=discord.ButtonStyle.danger, row=0)
    async def close_casino(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_close_impl(interaction)

    @discord.ui.button(label="Status", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_status_impl(interaction)

    @discord.ui.button(label="Open Game", emoji="\u25b6\ufe0f", style=discord.ButtonStyle.success, row=1)
    async def open_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a game to **open**:", view=CasinoGameSelectView("open"), ephemeral=True,
        )

    @discord.ui.button(label="Close Game", emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger, row=1)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a game to **close**:", view=CasinoGameSelectView("close"), ephemeral=True,
        )

    @discord.ui.button(label="Set Limits", emoji="\u2699\ufe0f", style=discord.ButtonStyle.secondary, row=1)
    async def set_limits(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossCasinoLimitsModal())

    @discord.ui.button(label="House Report", emoji="\U0001f4b5", style=discord.ButtonStyle.secondary, row=2)
    async def house_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_house_report_impl(interaction)

    @discord.ui.button(label="Clear Session", emoji="\U0001f9f9", style=discord.ButtonStyle.secondary, row=2)
    async def clear_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to clear session:", view=CasinoMemberSelectView("clearsession"), ephemeral=True,
        )

    @discord.ui.button(label="Give Scratch", emoji="\U0001f3ab", style=discord.ButtonStyle.secondary, row=2)
    async def give_scratch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member:", view=CasinoMemberSelectView("givescratch"), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Casino Modals ─────────────────────────────────────────────────────────────

class CasinoGameSelectView(discord.ui.View):
    def __init__(self, mode: str):
        super().__init__(timeout=120)
        self.mode = mode

    @discord.ui.select(
        placeholder="Select a game...",
        options=[
            discord.SelectOption(label=g.title(), value=g)
            for g in VALID_CASINO_GAMES
        ],
    )
    async def game_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        game = select.values[0]
        if self.mode == "open":
            await cog._casino_open_game_impl(interaction, game)
        else:
            await cog._casino_close_game_impl(interaction, game)


class BossCasinoLimitsModal(discord.ui.Modal, title="Set Casino Limits"):
    max_bet = discord.ui.TextInput(
        label="Max Bet (leave blank to skip)", placeholder="e.g., 1000", required=False,
    )
    daily_min = discord.ui.TextInput(
        label="Daily Min (leave blank to skip)", placeholder="e.g., 100", required=False,
    )
    daily_max = discord.ui.TextInput(
        label="Daily Max (leave blank to skip)", placeholder="e.g., 5000", required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        try:
            mb = int(self.max_bet.value) if self.max_bet.value.strip() else None
            dmin = int(self.daily_min.value) if self.daily_min.value.strip() else None
            dmax = int(self.daily_max.value) if self.daily_max.value.strip() else None
        except ValueError:
            return await interaction.response.send_message("❌ Values must be numbers.", ephemeral=True)
        if mb is None and dmin is None and dmax is None:
            return await interaction.response.send_message("❌ Provide at least one limit.", ephemeral=True)
        await cog._casino_set_limits_impl(interaction, mb, dmin, dmax)


class CasinoMemberSelectView(discord.ui.View):
    def __init__(self, mode: str):
        super().__init__(timeout=120)
        self.mode = mode

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member...")
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        member = select.values[0]
        if self.mode == "clearsession":
            await cog._casino_clear_session_impl(interaction, member)
        elif self.mode == "givescratch":
            await cog._casino_give_scratch_impl(interaction, member)


# ══════════════════════════════════════════════════════════════════════════════
# TREASURY PANEL
# ══════════════════════════════════════════════════════════════════════════════

class TreasuryPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Balances", emoji="\U0001f4b3", style=discord.ButtonStyle.primary, row=0)
    async def balances(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4b3 Balance Management", "Give, take, set, or check member balances."),
            view=BalancesPanelView(self.bot),
        )

    @discord.ui.button(label="Stipends", emoji="\U0001f4c5", style=discord.ButtonStyle.primary, row=0)
    async def stipends(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4c5 Stipend Management", "Manage recurring payments for roles."),
            view=StipendsPanelView(self.bot),
        )

    @discord.ui.button(label="Bulk Ops", emoji="\U0001f465", style=discord.ButtonStyle.primary, row=0)
    async def bulk(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f465 Bulk Operations", "Give or take TSL Bucks for all members with a role."),
            view=BulkPanelView(self.bot),
        )

    @discord.ui.button(label="Economy Health", emoji="\U0001f3e5", style=discord.ButtonStyle.secondary, row=1)
    async def health(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.eco_health_impl(interaction)

    @discord.ui.button(label="Flow Audit", emoji="\U0001f50d", style=discord.ButtonStyle.secondary, row=1)
    async def flow_audit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.flow_audit_impl(interaction)

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Treasury Sub-Panel: Balances ──────────────────────────────────────────────

class BalancesPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Give", emoji="\U0001f4b5", style=discord.ButtonStyle.success, row=0)
    async def give(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to **give** TSL Bucks:", view=EcoMemberSelectView("give"), ephemeral=True,
        )

    @discord.ui.button(label="Take", emoji="\U0001f4b8", style=discord.ButtonStyle.danger, row=0)
    async def take(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to **take** TSL Bucks from:", view=EcoMemberSelectView("take"), ephemeral=True,
        )

    @discord.ui.button(label="Set", emoji="\u270f\ufe0f", style=discord.ButtonStyle.primary, row=0)
    async def set_bal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to **set** balance:", view=EcoMemberSelectView("set"), ephemeral=True,
        )

    @discord.ui.button(label="Check", emoji="\U0001f50d", style=discord.ButtonStyle.secondary, row=0)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to check balance:", view=EcoCheckMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4b0 Treasury Admin", "Manage TSL Bucks balances, stipends, and bulk operations."),
            view=TreasuryPanelView(self.bot),
        )


# ── Treasury Sub-Panel: Stipends ──────────────────────────────────────────────

class StipendsPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Add Stipend", emoji="\u2795", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select the stipend interval:", view=StipendIntervalSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Remove Stipend", emoji="\u2796", style=discord.ButtonStyle.danger, row=0)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select the role to remove stipend from:", view=StipendRemoveRoleSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="List Stipends", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=1)
    async def list_stipends(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await cog._eco_stipend_list_impl(interaction)

    @discord.ui.button(label="Pay Now", emoji="\U0001f4b8", style=discord.ButtonStyle.primary, row=1)
    async def pay_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await cog._eco_stipend_paynow_impl(interaction)

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4b0 Treasury Admin", "Manage TSL Bucks balances, stipends, and bulk operations."),
            view=TreasuryPanelView(self.bot),
        )


# ── Treasury Sub-Panel: Bulk Ops ──────────────────────────────────────────────

class BulkPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Give to Role", emoji="\U0001f4b5", style=discord.ButtonStyle.success, row=0)
    async def give_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a role to **give** TSL Bucks:", view=EcoRoleSelectView("give"), ephemeral=True,
        )

    @discord.ui.button(label="Take from Role", emoji="\U0001f4b8", style=discord.ButtonStyle.danger, row=0)
    async def take_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a role to **take** TSL Bucks from:", view=EcoRoleSelectView("take"), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4b0 Treasury Admin", "Manage TSL Bucks balances, stipends, and bulk operations."),
            view=TreasuryPanelView(self.bot),
        )


# ── Treasury Modals ───────────────────────────────────────────────────────────

class EcoMemberSelectView(discord.ui.View):
    """Step 1: Pick a guild member for economy operations."""
    def __init__(self, mode: str):
        super().__init__(timeout=120)
        self.mode = mode

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member...")
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        titles = {"give": "Give TSL Bucks", "take": "Take TSL Bucks", "set": "Set Balance"}
        await interaction.response.send_modal(
            EcoTransferFollowUpModal(self.mode, member, titles.get(self.mode, "Transfer TSL Bucks")),
        )


class EcoTransferFollowUpModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g., 500", required=True)
    reason = discord.ui.TextInput(label="Reason", placeholder="Commissioner action", required=False)

    def __init__(self, mode: str, member: discord.Member, title: str):
        super().__init__(title=title)
        self.mode = mode
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        try:
            amt = int(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("❌ Amount must be a number.", ephemeral=True)
        reason = self.reason.value or "Commissioner action"
        if self.mode == "give":
            await cog._eco_give_impl(interaction, self.member, amt, reason)
        elif self.mode == "take":
            await cog._eco_take_impl(interaction, self.member, amt, reason)
        elif self.mode == "set":
            await cog._eco_set_impl(interaction, self.member, amt, reason)


class EcoCheckMemberSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member...")
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await cog._eco_check_impl(interaction, select.values[0])


class EcoRoleSelectView(discord.ui.View):
    """Step 1: Pick a role for bulk economy operations."""
    def __init__(self, mode: str):
        super().__init__(timeout=120)
        self.mode = mode

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select a role...")
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        title = "Give Bucks to Role" if self.mode == "give" else "Take Bucks from Role"
        await interaction.response.send_modal(EcoRoleFollowUpModal(self.mode, role, title))


class EcoRoleFollowUpModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g., 500", required=True)
    reason = discord.ui.TextInput(label="Reason", placeholder="Role action", required=False)

    def __init__(self, mode: str, role: discord.Role, title: str):
        super().__init__(title=title)
        self.mode = mode
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        try:
            amt = int(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("❌ Amount must be a number.", ephemeral=True)
        reason = self.reason.value or "Role action"
        if self.mode == "give":
            await cog._eco_give_role_impl(interaction, self.role, amt, reason)
        elif self.mode == "take":
            await cog._eco_take_role_impl(interaction, self.role, amt, reason)


class StipendIntervalSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Select interval...",
        options=[
            discord.SelectOption(label=i.title(), value=i)
            for i in VALID_INTERVALS
        ],
    )
    async def interval_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(StipendAddFollowUpModal(select.values[0]))


class StipendAddFollowUpModal(discord.ui.Modal, title="Add Recurring Stipend"):
    role_name = discord.ui.TextInput(label="Role Name", placeholder="e.g., TSL Owner", required=True)
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g., 100", required=True)
    reason = discord.ui.TextInput(label="Reason", placeholder="Recurring stipend", required=False)

    def __init__(self, interval: str):
        super().__init__()
        self.interval = interval

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        role = await _resolve_role(interaction, self.role_name.value)
        if not role:
            return await _send_not_found(interaction, "role", self.role_name.value)
        try:
            amt = int(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("❌ Amount must be a number.", ephemeral=True)
        reason = self.reason.value or "Recurring stipend"
        await cog._eco_stipend_add_impl(interaction, role, amt, self.interval, reason)


class StipendRemoveRoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select a role...")
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await cog._eco_stipend_remove_impl(interaction, select.values[0])


# ══════════════════════════════════════════════════════════════════════════════
# MARKETS PANEL
# ══════════════════════════════════════════════════════════════════════════════

class MarketsPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    # ── Polymarket ─────────────────────────────────────────────────────────
    @discord.ui.button(label="Resolve Market", emoji="\u2696\ufe0f", style=discord.ButtonStyle.primary, row=0)
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select the market result:", view=MarketResolveSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Approve Market", emoji="\u2705", style=discord.ButtonStyle.success, row=0)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossMarketApproveModal())

    @discord.ui.button(label="Market Status", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def market_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("Polymarket")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        await interaction.response.defer(ephemeral=True)
        await cog._market_status_impl(interaction)

    @discord.ui.button(label="Refund Sports", emoji="\U0001f4b8", style=discord.ButtonStyle.danger, row=1)
    async def refund_sports(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("Polymarket")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.refund_sports_impl(interaction)

    # ── Real Sportsbook ────────────────────────────────────────────────────
    @discord.ui.button(label="Real SB Status", emoji="\U0001f30d", style=discord.ButtonStyle.secondary, row=2)
    async def rsb_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Real Sportsbook")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.status_impl(interaction)

    @discord.ui.button(label="Lock Event", emoji="\U0001f512", style=discord.ButtonStyle.secondary, row=2)
    async def rsb_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossRealSBEventModal("lock"))

    @discord.ui.button(label="Void Event", emoji="\u274c", style=discord.ButtonStyle.danger, row=2)
    async def rsb_void(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossRealSBEventModal("void"))

    @discord.ui.button(label="Grade Real", emoji="\u2705", style=discord.ButtonStyle.secondary, row=3)
    async def rsb_grade(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Real Sportsbook")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.grade_impl(interaction)

    @discord.ui.button(label="Sync Sport", emoji="\U0001f504", style=discord.ButtonStyle.secondary, row=3)
    async def rsb_sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a sport to sync:", view=RealSBSyncSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Markets Modals ────────────────────────────────────────────────────────────

class MarketResolveSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Select result...",
        options=[
            discord.SelectOption(label=r, value=r)
            for r in VALID_MARKET_RESULTS
        ],
    )
    async def result_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(MarketResolveFollowUpModal(select.values[0]))


class MarketResolveFollowUpModal(discord.ui.Modal, title="Resolve Prediction Market"):
    slug = discord.ui.TextInput(label="Market Slug", placeholder="e.g., will-x-happen", required=True)

    def __init__(self, result: str):
        super().__init__()
        self.result = result

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("Polymarket")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        await interaction.response.defer(ephemeral=True)
        await cog._resolve_market_impl(interaction, self.slug.value, self.result)


class BossMarketApproveModal(discord.ui.Modal, title="Approve Market"):
    slug = discord.ui.TextInput(label="Market Slug", placeholder="e.g., will-x-happen", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("Polymarket")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        await interaction.response.defer(ephemeral=True)
        await cog._approve_market_impl(interaction, self.slug.value)


class BossRealSBEventModal(discord.ui.Modal):
    event_id = discord.ui.TextInput(
        label="Event ID (Odds API)", placeholder="e.g., abc123def456", required=True,
    )

    def __init__(self, mode: str):
        title = "Lock Real Event" if mode == "lock" else "Void Real Event"
        super().__init__(title=title)
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Real Sportsbook")
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.mode == "lock":
            await cog.lock_impl(interaction, self.event_id.value)
        elif self.mode == "void":
            await cog.void_impl(interaction, self.event_id.value)


class RealSBSyncSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Select a sport...",
        options=[
            discord.SelectOption(label=label, value=key)
            for key, label in VALID_SPORT_KEYS
        ],
    )
    async def sport_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Real Sportsbook")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.sync_impl(interaction, select.values[0])


# ══════════════════════════════════════════════════════════════════════════════
# ROSTER PANEL
# ══════════════════════════════════════════════════════════════════════════════

class RosterPanelView(discord.ui.View):
    """Roster governance — dev traits, ability audits, contracts, assignments."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Dev Traits", emoji="📊", style=discord.ButtonStyle.primary, row=0)
    async def dev_traits(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossDevAuditModal())

    @discord.ui.button(label="Ability Audit", emoji="🛡️", style=discord.ButtonStyle.primary, row=0)
    async def ability_audit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossAbilityAuditModal())

    @discord.ui.button(label="Ability Check", emoji="👤", style=discord.ButtonStyle.primary, row=0)
    async def ability_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossAbilityCheckModal())

    @discord.ui.button(label="Contract Check", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def contract_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossContractCheckModal())

    @discord.ui.button(label="Ability Reassign", emoji="🔄", style=discord.ButtonStyle.danger, row=1)
    async def ability_reassign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        if not _AE_AVAILABLE:
            return await interaction.response.send_message(
                "❌ ability_engine.py not found.", ephemeral=True
            )
        await interaction.response.send_modal(BossAbilityReassignModal())

    @discord.ui.button(label="Assign", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to assign to a team:", view=AssignMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Unassign", emoji="❌", style=discord.ButtonStyle.danger, row=2)
    async def unassign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a member to unassign:", view=UnassignMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="View Roster", emoji="📋", style=discord.ButtonStyle.secondary, row=2)
    async def view_roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        all_teams = roster.get_all_teams()
        afc_lines, nfc_lines = [], []
        unassigned = []
        for t in all_teams:
            owner = roster.get_owner(t["abbrName"])
            line = f"**{t['nickName']}** ({t['abbrName']})"
            if owner:
                line += f" — <@{owner.discord_id}>"
            else:
                unassigned.append(t["nickName"])
            if t["conference"] == "AFC":
                afc_lines.append(line)
            else:
                nfc_lines.append(line)

        embed = discord.Embed(title="🏈 TSL Roster", color=GOLD)
        embed.add_field(
            name="🏈 AFC", value="\n".join(afc_lines) or "None", inline=True,
        )
        embed.add_field(
            name="🏈 NFC", value="\n".join(nfc_lines) or "None", inline=True,
        )
        if unassigned:
            embed.add_field(
                name="🟨 Unassigned", value=", ".join(unassigned), inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Roster Modals ─────────────────────────────────────────────────────────────

class BossDevAuditModal(discord.ui.Modal, title="📊 Dev Traits"):
    team_name = discord.ui.TextInput(
        label="Team Name (leave blank for all teams)",
        placeholder="e.g. Cowboys, Eagles (partial match OK)",
        required=False,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)
        team_filter = self.team_name.value.strip()
        try:
            players = dm.get_players()
            if not players:
                return await interaction.followup.send("⚠️ No roster data. Run `/wittsync` first.", ephemeral=True)

            dev_order = {"Superstar X-Factor": 0, "Superstar": 1, "Star": 2, "Normal": 3}
            dev_emoji = {"Superstar X-Factor": "⚡", "Superstar": "🌟", "Star": "⭐", "Normal": "◦"}

            if team_filter:
                players = [
                    p for p in players
                    if team_filter.lower() in str(p.get("teamName", "")).lower()
                ]

            if not players:
                return await interaction.followup.send(f"❌ No players found matching `{team_filter}`.", ephemeral=True)

            by_dev: dict[str, list] = {}
            for p in players:
                dev = ae._normalize_dev(p) if _AE_AVAILABLE else (p.get("dev", "Normal") or "Normal")
                if dev == "Normal" and not team_filter:
                    continue
                by_dev.setdefault(dev, []).append(p)

            embed = discord.Embed(
                title=f"📊 Dev Traits — {'League-Wide' if not team_filter else team_filter}",
                color=AtlasColors.TSL_GOLD,
                description=f"Season {dm.CURRENT_SEASON}",
            )
            for dev in sorted(by_dev.keys(), key=lambda d: dev_order.get(d, 9)):
                lines = [
                    f"{dev_emoji.get(dev,'')} **{p.get('firstName','')} {p.get('lastName','')}** "
                    f"({p.get('pos','?')}, {p.get('teamName','?')}) OVR {p.get('playerBestOvr','?')}"
                    for p in sorted(by_dev[dev], key=lambda x: int(x.get("playerBestOvr",0) or 0), reverse=True)
                ]
                chunk = "\n".join(lines[:20])
                if chunk:
                    embed.add_field(name=f"{dev_emoji.get(dev,'')} {dev} ({len(by_dev[dev])})", value=chunk[:1024], inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Dev traits error: `{e}`", ephemeral=True)


class BossContractCheckModal(discord.ui.Modal, title="📋 Contract Check"):
    player_name = discord.ui.TextInput(
        label="Player Name",
        placeholder="Partial name match OK (e.g. Mahomes)",
        min_length=2,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            players = dm.get_players()
            if not players:
                return await interaction.followup.send("⚠️ No roster data.", ephemeral=True)
            query = self.player_name.value.strip().lower()
            matches = [
                p for p in players
                if query in f"{p.get('firstName','')} {p.get('lastName','')}".lower()
            ]
            if not matches:
                return await interaction.followup.send(f"❌ No player found matching `{self.player_name.value}`.", ephemeral=True)

            embed = discord.Embed(
                title=f"📋 Contract Check — {len(matches)} result(s)",
                color=AtlasColors.INFO,
            )
            for p in matches[:5]:
                name   = f"{p.get('firstName','')} {p.get('lastName','')}".strip()
                team   = p.get("teamName", "?")
                pos    = p.get("pos", "?")
                dev    = p.get("dev", "Normal")
                ovr    = p.get("playerBestOvr", "?")
                yr_pro = p.get("yearsPro", "?")
                is_fa  = p.get("isFA", False)
                is_ir  = p.get("isOnIR", False)
                flags  = []
                if is_fa:  flags.append("🟡 Free Agent")
                if is_ir:  flags.append("🚑 IR")
                embed.add_field(
                    name=f"{name} ({pos}, {team})",
                    value=(
                        f"OVR: **{ovr}** | Dev: **{dev}** | Yrs Pro: **{yr_pro}**"
                        + (f"\n{' · '.join(flags)}" if flags else "")
                    ),
                    inline=False,
                )
            if len(matches) > 5:
                embed.set_footer(text=f"Showing 5 of {len(matches)} matches — be more specific.")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)


class BossAbilityAuditModal(discord.ui.Modal, title="🛡️ Ability Audit"):
    team_name = discord.ui.TextInput(
        label="Team Name (leave blank for league-wide)",
        placeholder="e.g. Cowboys, Eagles (partial match OK)",
        required=False,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not _AE_AVAILABLE:
            return await interaction.followup.send(
                "❌ ability_engine.py not found. Place it alongside bot.py.", ephemeral=True
            )

        team_filter = self.team_name.value.strip() or None
        try:
            players   = dm.get_players()
            abilities = dm.get_player_abilities()
            if not players:
                return await interaction.followup.send("⚠️ No roster data. Run `/wittsync` first.", ephemeral=True)

            results = ae.audit_roster(players, abilities, team_filter=team_filter)

            if team_filter:
                if not results:
                    return await interaction.followup.send(
                        f"❌ No Star+ players found for `{team_filter}`.", ephemeral=True
                    )
                embeds = _boss_build_team_ability_embeds(results, results[0].team)
                for i in range(0, len(embeds), 10):
                    await interaction.followup.send(embeds=embeds[i:i+10], ephemeral=True)
            else:
                summary    = ae.summarize_audit(results)
                violations = [r for r in results if not r.is_clean]
                await interaction.followup.send(
                    embed=_boss_build_league_ability_embed(summary, violations), ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"❌ Ability audit error: `{e}`", ephemeral=True)


class BossAbilityCheckModal(discord.ui.Modal, title="👤 Ability Check"):
    player_name = discord.ui.TextInput(
        label="Player Name",
        placeholder="Partial name match OK (e.g. Mahomes, Jefferson)",
        min_length=2,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not _AE_AVAILABLE:
            return await interaction.followup.send(
                "❌ ability_engine.py not found. Place it alongside bot.py.", ephemeral=True
            )

        try:
            players   = dm.get_players()
            abilities = dm.get_player_abilities()
            if not players:
                return await interaction.followup.send("⚠️ No roster data. Run `/wittsync` first.", ephemeral=True)

            query   = self.player_name.value.strip().lower()
            matches = [
                p for p in players
                if query in (p.get("firstName", "") + " " + p.get("lastName", "")).lower()
                and ae._normalize_dev(p) != "Normal"
            ]

            if not matches:
                return await interaction.followup.send(
                    f"❌ No Star+ player found matching `{self.player_name.value}`. "
                    f"Use 🛡️ Ability Audit with a team name for a full roster.",
                    ephemeral=True,
                )

            if len(matches) > 1:
                names = ", ".join(
                    f"{m['firstName']} {m['lastName']} ({m['pos']}, {m.get('teamName','?')})"
                    for m in matches[:8]
                )
                return await interaction.followup.send(
                    f"⚠️ Multiple matches: {names}\nBe more specific.", ephemeral=True
                )

            results = ae.audit_roster([matches[0]], abilities)
            if not results:
                p = matches[0]
                return await interaction.followup.send(
                    f"ℹ️ **{p['firstName']} {p['lastName']}** has no abilities equipped.",
                    ephemeral=True,
                )

            await interaction.followup.send(
                embed=_boss_build_player_ability_embed(results[0]), ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Ability check error: `{e}`", ephemeral=True)


class BossAbilityReassignModal(discord.ui.Modal, title="🔄 Ability Reassignment"):
    confirm = discord.ui.TextInput(
        label="Type REASSIGN to confirm",
        placeholder="REASSIGN",
        min_length=8,
        max_length=8,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        if self.confirm.value.strip().upper() != "REASSIGN":
            return await interaction.response.send_message(
                "❌ Confirmation failed. Type `REASSIGN` exactly to proceed.", ephemeral=True
            )

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            players = dm.get_players()
            abilities = dm.get_player_abilities()
            if not players:
                return await interaction.followup.send(
                    "⚠️ No roster data. Run `/wittsync` first.", ephemeral=True
                )

            results = ae.reassign_roster(players, abilities)

            if not results:
                return await interaction.followup.send(
                    "⚠️ No SS/XF players found in the roster data.", ephemeral=True
                )

            summary = ae.summarize_reassignment(results)

            # 1. Summary embed
            await interaction.followup.send(
                embed=_boss_build_reassignment_summary_embed(summary), ephemeral=True
            )

            # 2. Team-by-team breakdown (only teams with changes)
            changed = [r for r in results if r.has_changes]
            if changed:
                team_embeds = _boss_build_reassignment_team_embeds(changed)
                for i in range(0, len(team_embeds), 10):
                    await interaction.followup.send(
                        embeds=team_embeds[i:i+10], ephemeral=True
                    )

            # 3. JSON file attachment
            import json
            export_data = ae.export_reassignment_json(results)
            if export_data:
                import io as _io
                json_str = json.dumps(export_data, indent=2)
                file = discord.File(
                    _io.BytesIO(json_str.encode("utf-8")),
                    filename=f"reassignment_S{dm.CURRENT_SEASON}.json",
                )
                await interaction.followup.send(
                    content="📎 Full reassignment data attached.",
                    file=file, ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "✅ No ability violations found — all SS/XF players are compliant.",
                    ephemeral=True,
                )

        except Exception as e:
            await interaction.followup.send(
                f"❌ Reassignment error: `{e}`", ephemeral=True
            )


# ── Roster Embed Helpers ──────────────────────────────────────────────────────

def _boss_build_player_ability_embed(result) -> discord.Embed:
    """Rich embed for a single player ability audit result."""
    color = AtlasColors.SUCCESS if result.is_clean else AtlasColors.ERROR
    embed = discord.Embed(
        title=f"{'✅' if result.is_clean else '🚨'} {result.name}",
        color=color,
    )
    embed.add_field(name="Team",      value=result.team,            inline=True)
    embed.add_field(name="Position",  value=result.pos,             inline=True)
    embed.add_field(name="Dev Trait", value=_dev_badge(result.dev), inline=True)
    embed.add_field(name="Archetype", value=result.archetype,       inline=True)

    ability_lines = []
    for ab in result.equipped:
        entry = ae.ABILITY_TABLE.get(ab) if ae else None
        tier  = entry["tier"] if entry else "?"
        emoji = TIER_EMOJI.get(tier, "❓")
        flag  = " ⚠️" if any(i["ability"] == ab for i in result.illegal_abilities) else ""
        ability_lines.append(f"{emoji} **{ab}**{flag}")

    embed.add_field(
        name="Equipped Abilities",
        value="\n".join(ability_lines) if ability_lines else "_None_",
        inline=False,
    )

    if result.illegal_abilities:
        violation_text = []
        for item in result.illegal_abilities:
            reasons = "\n  · ".join(item["reasons"])
            violation_text.append(f"⚠️ **{item['ability']}**\n  · {reasons}")
        embed.add_field(
            name="🚨 Illegal Abilities",
            value="\n\n".join(violation_text),
            inline=False,
        )

    if result.budget_violation:
        embed.add_field(name="💸 Budget Violation", value=result.budget_violation, inline=False)

    if result.is_clean:
        embed.set_footer(text="All abilities earned. No action required.")
    else:
        embed.add_field(
            name="📋 Commissioner Actions",
            value="\n".join(result.action_lines()),
            inline=False,
        )
    return embed


def _boss_build_team_ability_embeds(team_results, team_name: str) -> list[discord.Embed]:
    """One summary embed + one embed per flagged player."""
    violations  = [r for r in team_results if not r.is_clean]
    clean_count = len(team_results) - len(violations)

    summary = discord.Embed(
        title=f"🏈 {team_name} — Ability Audit",
        color=AtlasColors.ERROR if violations else AtlasColors.SUCCESS,
        description=(
            f"**{len(team_results)}** players audited  |  "
            f"**{clean_count}** clean  |  "
            f"**{len(violations)}** violation{'s' if len(violations) != 1 else ''}"
        ),
    )

    if not violations:
        summary.set_footer(text="✅ All players are within ability rules.")
        return [summary]

    action_lines = []
    for r in violations:
        for a in r.action_lines():
            action_lines.append(f"**{r.name}** ({r.pos}): {a}")

    chunk, chunks = [], []
    for line in action_lines:
        if sum(len(l) + 1 for l in chunk) + len(line) > 1000:
            chunks.append(chunk)
            chunk = []
        chunk.append(line)
    if chunk:
        chunks.append(chunk)

    for i, c in enumerate(chunks):
        summary.add_field(
            name=f"📋 Commissioner Actions {'(cont.)' if i else ''}",
            value="\n".join(c),
            inline=False,
        )

    return [summary] + [_boss_build_player_ability_embed(r) for r in violations]


def _boss_build_league_ability_embed(summary: dict, top_violations) -> discord.Embed:
    """High-level league-wide ability audit embed."""
    clean_pct = int(100 * summary["cleanPlayers"] / max(summary["totalPlayersAudited"], 1))
    color = AtlasColors.SUCCESS if summary["violations"] == 0 else AtlasColors.WARNING

    embed = discord.Embed(
        title="🛡️ TSL Full League Ability Audit",
        color=color,
        description=f"**{summary['totalPlayersAudited']}** Star+ players audited across the league",
    )
    embed.add_field(name="✅ Clean",           value=str(summary["cleanPlayers"]),          inline=True)
    embed.add_field(name="🚨 Violations",      value=str(summary["violations"]),            inline=True)
    embed.add_field(name="📈 Compliance",      value=f"{clean_pct}%",                       inline=True)
    embed.add_field(name="📉 Stat Violations", value=str(summary["illegalStatViolations"]), inline=True)
    embed.add_field(name="💸 Budget Only",     value=str(summary["budgetViolationsOnly"]),  inline=True)
    embed.add_field(name="🏟️ Teams Affected", value=str(summary["teamsAffected"]),         inline=True)

    if top_violations:
        lines = [
            f"**{r.name}** ({r.pos}, {r.team}) — "
            f"{len(r.illegal_abilities) + (1 if r.budget_violation else 0)} issue(s)"
            for r in top_violations[:10]
        ]
        embed.add_field(
            name="🔎 Top Violations (use 👤 Ability Check for detail)",
            value="\n".join(lines),
            inline=False,
        )

    if summary["violations"] == 0:
        embed.set_footer(text="✅ League is fully compliant.")
    return embed


def _boss_build_reassignment_summary_embed(summary: dict) -> discord.Embed:
    """High-level league-wide reassignment summary embed."""
    has_changes = summary["playersWithSwaps"] > 0 or summary["playersUnresolved"] > 0
    color = AtlasColors.WARNING if has_changes else AtlasColors.SUCCESS

    embed = discord.Embed(
        title=f"🔄 TSL Ability Reassignment — Season {dm.CURRENT_SEASON}",
        color=color,
        description=f"**{summary['totalProcessed']}** SS/XF players audited across the league",
    )
    embed.add_field(name="✅ Clean",        value=str(summary["playersClean"]),      inline=True)
    embed.add_field(name="🔄 Changed",      value=str(summary["playersWithSwaps"]),  inline=True)
    embed.add_field(name="🏟️ Teams",       value=str(summary["teamsAffected"]),      inline=True)
    embed.add_field(name="🔀 Replacements", value=str(summary["totalSwaps"]),        inline=True)
    embed.add_field(name="⚠️ Unresolved",  value=str(summary["totalUnresolved"]),    inline=True)

    if not has_changes:
        embed.set_footer(text="✅ All SS/XF abilities are earned. No reassignment needed.")
    else:
        embed.set_footer(text="Review team breakdowns below. Apply changes in Madden before next advance.")

    embed.set_author(name="ATLAS™ Boss · Roster Admin")
    return embed


def _boss_build_reassignment_team_embeds(changed_results) -> list[discord.Embed]:
    """Build per-team embeds showing each player's ability changes."""
    FOOTER = "ATLAS™ Boss · Ability Reassignment Engine"
    MAX_EMBED = 5_900  # discord hard cap is 6000; leave 100 chars headroom

    by_team: dict[str, list] = {}
    for r in changed_results:
        by_team.setdefault(r.team, []).append(r)

    embeds: list[discord.Embed] = []

    for team_name in sorted(by_team.keys()):
        team_players = by_team[team_name]

        fields: list[tuple[str, str]] = []
        for r in team_players:
            lines = []

            if r.kept:
                lines.append(f"✅ Kept: {', '.join(r.kept)}")

            for s in r.swaps:
                tier_emoji = TIER_EMOJI.get(s.get("new_tier", "?"), "❓")
                lines.append(
                    f"🔄 Slot {s['slot_index']}: "
                    f"~~{s['old']}~~ → {tier_emoji} **{s['new']}** "
                    f"(fit: {s['fit_score']})"
                )

            for u in r.unresolved:
                lines.append(
                    f"⚠️ Slot {u['slot_index']}: "
                    f"~~{u['old']}~~ → **EMPTY** "
                    f"(no valid replacement)"
                )

            dev_badge = _dev_badge(r.dev)
            fields.append((
                f"{r.name} ({r.pos}, {dev_badge})",
                "\n".join(lines) if lines else "_No changes_",
            ))

        part = 1
        cur_embed = None

        def _make_embed(part_num: int) -> discord.Embed:
            suffix = f" (pt. {part_num})" if part_num > 1 else ""
            e = discord.Embed(
                title=f"🏈 {team_name} — Ability Reassignment{suffix}",
                color=AtlasColors.WARNING,
            )
            return e

        for fname, fval in fields:
            field_chars = len(fname) + len(fval)

            if cur_embed is None:
                cur_embed = _make_embed(part)

            # Use discord.py's own len() — matches what Discord validates exactly
            if len(cur_embed) + field_chars + len(FOOTER) > MAX_EMBED and cur_embed.fields:
                cur_embed.set_footer(text=FOOTER)
                embeds.append(cur_embed)
                part += 1
                cur_embed = _make_embed(part)

            cur_embed.add_field(name=fname, value=fval, inline=False)

        if cur_embed is not None:
            cur_embed.set_footer(text=FOOTER)
            embeds.append(cur_embed)

    return embeds


# ══════════════════════════════════════════════════════════════════════════════
# LEAGUE PANEL
# ══════════════════════════════════════════════════════════════════════════════

class LeaguePanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    # ── Genesis ────────────────────────────────────────────────────────────
    @discord.ui.button(label="Trade List", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def tradelist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("TradeCenterCog")
        if not cog:
            return await _send_cog_error(interaction, "Trade Center")
        await cog._tradelist_impl(interaction)

    @discord.ui.button(label="Run Lottery", emoji="\U0001f3b0", style=discord.ButtonStyle.primary, row=0)
    async def lottery(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("ParityCog")
        if not cog:
            return await _send_cog_error(interaction, "Parity")
        await cog._runlottery_impl(interaction)

    @discord.ui.button(label="Orphan Flag", emoji="\U0001f3e0", style=discord.ButtonStyle.secondary, row=0)
    async def orphan(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_message(
            "Select a team to set orphan status:", view=OrphanTeamSelectView(), ephemeral=True,
        )

    # ── Awards ─────────────────────────────────────────────────────────────
    @discord.ui.button(label="Create Poll", emoji="\U0001f4ca", style=discord.ButtonStyle.primary, row=1)
    async def create_poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossCreatePollModal())

    @discord.ui.button(label="Close Poll", emoji="\U0001f512", style=discord.ButtonStyle.secondary, row=1)
    async def close_poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossClosePollModal())

    # ── Codex ──────────────────────────────────────────────────────────────
    @discord.ui.button(label="Ask Debug", emoji="\U0001f41b", style=discord.ButtonStyle.secondary, row=2)
    async def ask_debug(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossAskDebugModal())

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── League Modals ─────────────────────────────────────────────────────────────

class OrphanTeamSelectView(discord.ui.View):
    """Pick a team (AFC/NFC selects), then set orphan status via buttons."""
    def __init__(self):
        super().__init__(timeout=120)
        teams = roster.get_all_teams()
        afc = [t for t in teams if t["conference"] == "AFC"]
        nfc = [t for t in teams if t["conference"] == "NFC"]
        afc_select = discord.ui.Select(
            placeholder="AFC Team...",
            options=[
                discord.SelectOption(label=f"{t['nickName']} ({t['abbrName']})", value=t["abbrName"])
                for t in afc
            ],
            row=0,
        )
        afc_select.callback = self._on_team
        self.add_item(afc_select)
        nfc_select = discord.ui.Select(
            placeholder="NFC Team...",
            options=[
                discord.SelectOption(label=f"{t['nickName']} ({t['abbrName']})", value=t["abbrName"])
                for t in nfc
            ],
            row=1,
        )
        nfc_select.callback = self._on_team
        self.add_item(nfc_select)

    async def _on_team(self, interaction: discord.Interaction):
        team = interaction.data["values"][0]
        await interaction.response.send_message(
            f"Set orphan status for **{team}**:",
            view=OrphanFlagView(team), ephemeral=True,
        )


class OrphanFlagView(discord.ui.View):
    def __init__(self, team_abbr: str):
        super().__init__(timeout=60)
        self.team_abbr = team_abbr

    @discord.ui.button(label="Mark as Orphan", emoji="\U0001f3e0", style=discord.ButtonStyle.danger)
    async def mark_orphan(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("ParityCog")
        if not cog:
            return await _send_cog_error(interaction, "Parity")
        await cog._orphanfranchise_impl(interaction, self.team_abbr, True)

    @discord.ui.button(label="Remove Orphan Flag", emoji="\u2705", style=discord.ButtonStyle.success)
    async def remove_orphan(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("ParityCog")
        if not cog:
            return await _send_cog_error(interaction, "Parity")
        await cog._orphanfranchise_impl(interaction, self.team_abbr, False)


class BossCreatePollModal(discord.ui.Modal, title="Create Award Poll"):
    poll_title = discord.ui.TextInput(label="Poll Title", placeholder="e.g., MVP Award", required=True)
    nominees = discord.ui.TextInput(
        label="Nominees (comma-separated)",
        placeholder="e.g., JT, Killa, Nova",
        required=True,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("AwardsCog")
        if not cog:
            return await _send_cog_error(interaction, "Awards")
        await cog._createpoll_impl(interaction, self.poll_title.value, self.nominees.value)


class BossClosePollModal(discord.ui.Modal, title="Close Poll"):
    poll_id = discord.ui.TextInput(label="Poll ID", placeholder="e.g., A1B2C3D4", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("AwardsCog")
        if not cog:
            return await _send_cog_error(interaction, "Awards")
        await cog._closepoll_impl(interaction, self.poll_id.value)


class BossAskDebugModal(discord.ui.Modal, title="Ask Debug (SQL + Rows)"):
    question = discord.ui.TextInput(
        label="Question",
        placeholder="e.g., Who has the most all-time wins?",
        required=True,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("CodexCog")
        if not cog:
            return await _send_cog_error(interaction, "Codex")
        await cog._ask_debug_impl(interaction, self.question.value)


class AssignMemberSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member to assign...")
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        embed = discord.Embed(
            title="Team Assignment",
            description=f"Pick a conference to assign **{member.display_name}** to a team.",
            color=GOLD,
        )
        view = roster.AssignConferenceView(member)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class UnassignMemberSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member to unassign...")
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        entry = roster.get_entry_by_id(member.id)
        if not entry:
            return await interaction.response.send_message(
                f"❌ **{member.display_name}** has no team assignment.", ephemeral=True,
            )
        success = await asyncio.get_running_loop().run_in_executor(None, roster.unassign, member.id)
        if success:
            embed = discord.Embed(
                title="Team Assignment Removed",
                description=(
                    f"**{member.display_name}** (<@{member.id}>) "
                    f"removed from **{entry.team_name}** ({entry.team_abbr})."
                ),
                color=AtlasColors.WARNING,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("❌ Failed to unassign.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE PANEL
# ══════════════════════════════════════════════════════════════════════════════

class CompliancePanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="View Case", emoji="\U0001f50d", style=discord.ButtonStyle.primary, row=0)
    async def view_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossCaseViewModal())

    @discord.ui.button(label="List Cases", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def list_cases(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("ComplaintCog")
        if not cog:
            return await _send_cog_error(interaction, "Complaint")
        await cog.caselist_impl(interaction)

    @discord.ui.button(label="Force History", emoji="\U0001f4ca", style=discord.ButtonStyle.secondary, row=0)
    async def force_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("ForceRequestCog")
        if not cog:
            return await _send_cog_error(interaction, "Force Request")
        await cog.forcehistory_impl(interaction)

    @discord.ui.button(label="Approve Position", emoji="\u2705", style=discord.ButtonStyle.success, row=1)
    async def pos_approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossPositionApproveModal())

    @discord.ui.button(label="Deny Position", emoji="\u274c", style=discord.ButtonStyle.danger, row=1)
    async def pos_deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.send_modal(BossPositionDenyModal())

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Compliance Modals ─────────────────────────────────────────────────────────

class BossCaseViewModal(discord.ui.Modal, title="View Complaint Case"):
    case_id = discord.ui.TextInput(label="Case ID", placeholder="e.g., A1B2C3D4", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("ComplaintCog")
        if not cog:
            return await _send_cog_error(interaction, "Complaint")
        await cog.caseview_impl(interaction, self.case_id.value)


class BossPositionApproveModal(discord.ui.Modal, title="Approve Position Change"):
    log_id = discord.ui.TextInput(label="Log ID", placeholder="e.g., PC-001", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("PositionChangeCog")
        if not cog:
            return await _send_cog_error(interaction, "Position Change")
        await cog.positionchangeapprove_impl(interaction, self.log_id.value)


class BossPositionDenyModal(discord.ui.Modal, title="Deny Position Change"):
    log_id = discord.ui.TextInput(label="Log ID", placeholder="e.g., PC-001", required=True)
    reason = discord.ui.TextInput(
        label="Reason", placeholder="No reason provided.", required=False,
        default="No reason provided.",
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        cog = interaction.client.get_cog("PositionChangeCog")
        if not cog:
            return await _send_cog_error(interaction, "Position Change")
        reason = self.reason.value or "No reason provided."
        await cog.positionchangedeny_impl(interaction, self.log_id.value, reason)


# ══════════════════════════════════════════════════════════════════════════════
# FLOW LIVE PANEL
# ══════════════════════════════════════════════════════════════════════════════

class FlowLivePanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Force Pulse Refresh", emoji="🔄", style=discord.ButtonStyle.success, row=0)
    async def force_pulse_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("FlowLiveCog")
        if not cog:
            return await _send_cog_error(interaction, "FLOW Live")
        await cog._update_pulse_impl(interaction.guild)
        await interaction.followup.send("✅ Pulse dashboard refreshed.", ephemeral=True)

    @discord.ui.button(label="Test Highlight", emoji="🧪", style=discord.ButtonStyle.primary, row=0)
    async def test_highlight(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("FlowLiveCog")
        if not cog:
            return await _send_cog_error(interaction, "FLOW Live")
        try:
            from setup_cog import get_channel_id
            ch_id = get_channel_id("flow_live")
            channel = interaction.guild.get_channel(ch_id) if ch_id else None
        except ImportError:
            channel = None
        if not channel:
            return await interaction.followup.send("❌ #flow-live channel not configured.", ephemeral=True)
        await cog._test_highlight_impl(interaction.guild, channel)
        await interaction.followup.send("✅ Test highlight sent.", ephemeral=True)

    @discord.ui.button(label="Session Dump", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def session_dump(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("FlowLiveCog")
        if not cog:
            return await _send_cog_error(interaction, "FLOW Live")
        result = await cog._session_dump_impl(interaction.guild)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ══════════════════════════════════════════════════════════════════════════════
# COG CLASS
# ══════════════════════════════════════════════════════════════════════════════

class BossCog(commands.Cog):
    """ATLAS Commissioner Control Room — visual hub for all admin operations."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="boss", description="Open the ATLAS Commissioner Control Room")
    @app_commands.default_permissions(administrator=True)
    async def boss_cmd(self, interaction: discord.Interaction):
        """Launch the Commissioner Control Room hub."""
        if not await is_commissioner(interaction):
            return await interaction.response.send_message(
                "❌ This command is restricted to commissioners.", ephemeral=True,
            )

        embed = _home_embed(interaction)
        view = BossHubView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BossCog(bot))
    print("ATLAS: Boss \u00b7 Commissioner Control Room loaded. \U0001f3db\ufe0f")
