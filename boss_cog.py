"""
boss_cog.py — ATLAS Commissioner Control Room (/boss)
─────────────────────────────────────────────────────────────────────────────
Visual hub replacing the 57 /commish slash subcommands with a single /boss
command that opens an ephemeral button-driven control room.

Architecture:
    /boss  →  BossHubView (6 panel buttons)
                ├── Sportsbook  →  sub-panels (Lines & Locks, Bets & Props)
                ├── Casino
                ├── Treasury    →  sub-panels (Balances, Stipends, Bulk Ops)
                ├── Markets     →  polymarket + real sportsbook
                ├── League      →  genesis + awards + codex + roster
                └── Compliance  →  sentinel

Every button/modal delegates to existing _impl methods on target cogs.
No logic is duplicated — boss_cog is a pure UI layer.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("atlas.boss")

COMMISH_COLOR = discord.Color(0x202124)
GOLD = discord.Color(0xC9962A)

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
        import roster
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
        import roster
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
    """Commissioner Control Room — 6 panel navigation buttons."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Sportsbook", emoji="\U0001f4ca", style=discord.ButtonStyle.primary, row=0)
    async def sb(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4ca Sportsbook Admin", "Manage TSL sportsbook lines, bets, and props."),
            view=SBPanelView(self.bot),
        )

    @discord.ui.button(label="Casino", emoji="\U0001f3b0", style=discord.ButtonStyle.primary, row=0)
    async def casino(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f3b0 Casino Admin", "Open/close games, set limits, manage sessions."),
            view=CasinoPanelView(self.bot),
        )

    @discord.ui.button(label="Treasury", emoji="\U0001f4b0", style=discord.ButtonStyle.primary, row=0)
    async def treasury(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4b0 Treasury Admin", "Manage TSL Bucks balances, stipends, and bulk operations."),
            view=TreasuryPanelView(self.bot),
        )

    @discord.ui.button(label="Markets", emoji="\U0001f4c8", style=discord.ButtonStyle.secondary, row=1)
    async def markets(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4c8 Markets Admin", "Prediction markets and real sportsbook management."),
            view=MarketsPanelView(self.bot),
        )

    @discord.ui.button(label="League", emoji="\U0001f3c8", style=discord.ButtonStyle.secondary, row=1)
    async def league(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f3c8 League Admin", "Trades, lottery, polls, roster assignments, and debug tools."),
            view=LeaguePanelView(self.bot),
        )

    @discord.ui.button(label="Compliance", emoji="\u2696\ufe0f", style=discord.ButtonStyle.secondary, row=1)
    async def compliance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\u2696\ufe0f Compliance Admin", "Cases, force requests, and position changes."),
            view=CompliancePanelView(self.bot),
        )


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
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4cf Lines & Locks", "Set spreads, moneylines, O/U, and lock games."),
            view=SBLinesPanelView(self.bot),
        )

    @discord.ui.button(label="Bets & Props", emoji="\U0001f3b2", style=discord.ButtonStyle.primary, row=0)
    async def bets(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f3b2 Bets & Props", "Grade bets, refund, balance adjustments, and prop management."),
            view=SBBetsPanelView(self.bot),
        )

    @discord.ui.button(label="Status", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_status_impl(interaction)

    @discord.ui.button(label="Lock All", emoji="\U0001f512", style=discord.ButtonStyle.danger, row=1)
    async def lockall(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lockall_impl(interaction)

    @discord.ui.button(label="Unlock All", emoji="\U0001f513", style=discord.ButtonStyle.success, row=1)
    async def unlockall(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_unlockall_impl(interaction)

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Sportsbook Sub-Panel: Lines & Locks ──────────────────────────────────────

class SBLinesPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Set Spread", style=discord.ButtonStyle.primary, row=0)
    async def set_spread(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a matchup to set spread:", view=SBMatchupSelectView("spread"), ephemeral=True,
        )

    @discord.ui.button(label="Set ML", style=discord.ButtonStyle.primary, row=0)
    async def set_ml(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a matchup to set moneyline:", view=SBMatchupSelectView("ml"), ephemeral=True,
        )

    @discord.ui.button(label="Set O/U", style=discord.ButtonStyle.primary, row=0)
    async def set_ou(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a matchup to set O/U:", view=SBMatchupSelectView("ou"), ephemeral=True,
        )

    @discord.ui.button(label="Reset All Lines", emoji="\U0001f504", style=discord.ButtonStyle.danger, row=1)
    async def reset_lines(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_resetlines_impl(interaction)

    @discord.ui.button(label="View Lines", emoji="\U0001f4ca", style=discord.ButtonStyle.secondary, row=1)
    async def view_lines(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lines_impl(interaction)

    @discord.ui.button(label="Lock Game", emoji="\U0001f512", style=discord.ButtonStyle.secondary, row=2)
    async def lock_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a matchup to lock/unlock:", view=SBMatchupSelectView("lock"), ephemeral=True,
        )

    @discord.ui.button(label="Cancel Game", emoji="\u274c", style=discord.ButtonStyle.danger, row=2)
    async def cancel_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a matchup to cancel:", view=SBMatchupSelectView("cancel"), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4ca Sportsbook Admin", "Manage TSL sportsbook lines, bets, and props."),
            view=SBPanelView(self.bot),
        )


# ── Sportsbook Sub-Panel: Bets & Props ──────────────────────────────────────

class SBBetsPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Grade Week", emoji="\u2705", style=discord.ButtonStyle.primary, row=0)
    async def grade(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossSBGradeModal())

    @discord.ui.button(label="Refund Bet", emoji="\U0001f4b8", style=discord.ButtonStyle.secondary, row=0)
    async def refund(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossSBRefundModal())

    @discord.ui.button(label="Balance Adjust", emoji="\U0001f4b0", style=discord.ButtonStyle.secondary, row=1)
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to adjust balance:", view=SBBalanceMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Add Prop", emoji="\U0001f4dd", style=discord.ButtonStyle.primary, row=2)
    async def add_prop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossSBAddPropModal())

    @discord.ui.button(label="Settle Prop", emoji="\u2696\ufe0f", style=discord.ButtonStyle.secondary, row=2)
    async def settle_prop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select the prop result:", view=SettlePropSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        cog = interaction.client.get_cog("SportsbookCog")
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
        cog = interaction.client.get_cog("SportsbookCog")
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
        cog = interaction.client.get_cog("SportsbookCog")
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
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lock_impl(interaction, self.matchup, True)

    @discord.ui.button(label="Unlock", emoji="\U0001f513", style=discord.ButtonStyle.success)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_lock_impl(interaction, self.matchup, False)


class SBCancelConfirmView(discord.ui.View):
    def __init__(self, matchup: str):
        super().__init__(timeout=30)
        self.matchup = matchup

    @discord.ui.button(label="Confirm Cancel & Refund", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        await cog._sb_cancelgame_impl(interaction, self.matchup)

    @discord.ui.button(label="Nevermind", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BossSBGradeModal(discord.ui.Modal, title="Grade Bets for Week"):
    week = discord.ui.TextInput(label="Week Number", placeholder="e.g., 8", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            week_num = int(self.week.value)
        except ValueError:
            return await interaction.response.send_message("❌ Week must be a number.", ephemeral=True)
        await cog._grade_bets_impl(interaction, week_num)


class BossSBRefundModal(discord.ui.Modal, title="Refund a Bet"):
    bet_id = discord.ui.TextInput(label="Bet ID", placeholder="e.g., 42", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("SportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Sportsbook")
        try:
            bid = int(self.bet_id.value)
        except ValueError:
            return await interaction.response.send_message("❌ Bet ID must be a number.", ephemeral=True)
        await cog._sb_refund_impl(interaction, bid)


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
        cog = interaction.client.get_cog("SportsbookCog")
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
        cog = interaction.client.get_cog("SportsbookCog")
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
        cog = interaction.client.get_cog("SportsbookCog")
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
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_open_impl(interaction)

    @discord.ui.button(label="Close Casino", emoji="\U0001f534", style=discord.ButtonStyle.danger, row=0)
    async def close_casino(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_close_impl(interaction)

    @discord.ui.button(label="Status", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_status_impl(interaction)

    @discord.ui.button(label="Open Game", emoji="\u25b6\ufe0f", style=discord.ButtonStyle.success, row=1)
    async def open_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a game to **open**:", view=CasinoGameSelectView("open"), ephemeral=True,
        )

    @discord.ui.button(label="Close Game", emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger, row=1)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a game to **close**:", view=CasinoGameSelectView("close"), ephemeral=True,
        )

    @discord.ui.button(label="Set Limits", emoji="\u2699\ufe0f", style=discord.ButtonStyle.secondary, row=1)
    async def set_limits(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossCasinoLimitsModal())

    @discord.ui.button(label="House Report", emoji="\U0001f4b5", style=discord.ButtonStyle.secondary, row=2)
    async def house_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("CasinoCog")
        if not cog:
            return await _send_cog_error(interaction, "Casino")
        await cog._casino_house_report_impl(interaction)

    @discord.ui.button(label="Clear Session", emoji="\U0001f9f9", style=discord.ButtonStyle.secondary, row=2)
    async def clear_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to clear session:", view=CasinoMemberSelectView("clearsession"), ephemeral=True,
        )

    @discord.ui.button(label="Give Scratch", emoji="\U0001f3ab", style=discord.ButtonStyle.secondary, row=2)
    async def give_scratch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member:", view=CasinoMemberSelectView("givescratch"), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4b3 Balance Management", "Give, take, set, or check member balances."),
            view=BalancesPanelView(self.bot),
        )

    @discord.ui.button(label="Stipends", emoji="\U0001f4c5", style=discord.ButtonStyle.primary, row=0)
    async def stipends(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f4c5 Stipend Management", "Manage recurring payments for roles."),
            view=StipendsPanelView(self.bot),
        )

    @discord.ui.button(label="Bulk Ops", emoji="\U0001f465", style=discord.ButtonStyle.primary, row=0)
    async def bulk(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_panel_embed("\U0001f465 Bulk Operations", "Give or take TSL Bucks for all members with a role."),
            view=BulkPanelView(self.bot),
        )

    @discord.ui.button(label="Economy Health", emoji="\U0001f3e5", style=discord.ButtonStyle.secondary, row=1)
    async def health(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.eco_health_impl(interaction)

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Treasury Sub-Panel: Balances ──────────────────────────────────────────────

class BalancesPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Give", emoji="\U0001f4b5", style=discord.ButtonStyle.success, row=0)
    async def give(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to **give** TSL Bucks:", view=EcoMemberSelectView("give"), ephemeral=True,
        )

    @discord.ui.button(label="Take", emoji="\U0001f4b8", style=discord.ButtonStyle.danger, row=0)
    async def take(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to **take** TSL Bucks from:", view=EcoMemberSelectView("take"), ephemeral=True,
        )

    @discord.ui.button(label="Set", emoji="\u270f\ufe0f", style=discord.ButtonStyle.primary, row=0)
    async def set_bal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to **set** balance:", view=EcoMemberSelectView("set"), ephemeral=True,
        )

    @discord.ui.button(label="Check", emoji="\U0001f50d", style=discord.ButtonStyle.secondary, row=0)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to check balance:", view=EcoCheckMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await interaction.response.send_message(
            "Select the stipend interval:", view=StipendIntervalSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Remove Stipend", emoji="\u2796", style=discord.ButtonStyle.danger, row=0)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select the role to remove stipend from:", view=StipendRemoveRoleSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="List Stipends", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=1)
    async def list_stipends(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await cog._eco_stipend_list_impl(interaction)

    @discord.ui.button(label="Pay Now", emoji="\U0001f4b8", style=discord.ButtonStyle.primary, row=1)
    async def pay_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("EconomyCog")
        if not cog:
            return await _send_cog_error(interaction, "Economy")
        await cog._eco_stipend_paynow_impl(interaction)

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await interaction.response.send_message(
            "Select a role to **give** TSL Bucks:", view=EcoRoleSelectView("give"), ephemeral=True,
        )

    @discord.ui.button(label="Take from Role", emoji="\U0001f4b8", style=discord.ButtonStyle.danger, row=0)
    async def take_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a role to **take** TSL Bucks from:", view=EcoRoleSelectView("take"), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await interaction.response.send_message(
            "Select the market result:", view=MarketResolveSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Approve Market", emoji="\u2705", style=discord.ButtonStyle.success, row=0)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossMarketApproveModal())

    @discord.ui.button(label="Market Status", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def market_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("PolymarketCog")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        if not hasattr(cog, "_market_status_impl"):
            return await interaction.response.send_message(
                "\u23f3 Market status is not yet implemented.", ephemeral=True,
            )
        await cog._market_status_impl(interaction)

    @discord.ui.button(label="Refund Sports", emoji="\U0001f4b8", style=discord.ButtonStyle.danger, row=1)
    async def refund_sports(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("PolymarketCog")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.refund_sports_impl(interaction)

    # ── Real Sportsbook ────────────────────────────────────────────────────
    @discord.ui.button(label="Real SB Status", emoji="\U0001f30d", style=discord.ButtonStyle.secondary, row=2)
    async def rsb_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Real Sportsbook")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.status_impl(interaction)

    @discord.ui.button(label="Lock Event", emoji="\U0001f512", style=discord.ButtonStyle.secondary, row=2)
    async def rsb_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossRealSBEventModal("lock"))

    @discord.ui.button(label="Void Event", emoji="\u274c", style=discord.ButtonStyle.danger, row=2)
    async def rsb_void(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossRealSBEventModal("void"))

    @discord.ui.button(label="Grade Real", emoji="\u2705", style=discord.ButtonStyle.secondary, row=3)
    async def rsb_grade(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("RealSportsbookCog")
        if not cog:
            return await _send_cog_error(interaction, "Real Sportsbook")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.grade_impl(interaction)

    @discord.ui.button(label="Sync Sport", emoji="\U0001f504", style=discord.ButtonStyle.secondary, row=3)
    async def rsb_sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a sport to sync:", view=RealSBSyncSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        cog = interaction.client.get_cog("PolymarketCog")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        if not hasattr(cog, "_resolve_market_impl"):
            return await interaction.response.send_message(
                "\u23f3 Market resolution is not yet implemented.", ephemeral=True,
            )
        await cog._resolve_market_impl(interaction, self.slug.value, self.result)


class BossMarketApproveModal(discord.ui.Modal, title="Approve Market"):
    slug = discord.ui.TextInput(label="Market Slug", placeholder="e.g., will-x-happen", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("PolymarketCog")
        if not cog:
            return await _send_cog_error(interaction, "Polymarket")
        if not hasattr(cog, "_approve_market_impl"):
            return await interaction.response.send_message(
                "\u23f3 Market approval is not yet implemented.", ephemeral=True,
            )
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
# LEAGUE PANEL
# ══════════════════════════════════════════════════════════════════════════════

class LeaguePanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    # ── Genesis ────────────────────────────────────────────────────────────
    @discord.ui.button(label="Trade List", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def tradelist(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GenesisHubCog") or interaction.client.get_cog("TradeCenterCog")
        if not cog:
            return await _send_cog_error(interaction, "Genesis")
        await cog._tradelist_impl(interaction)

    @discord.ui.button(label="Run Lottery", emoji="\U0001f3b0", style=discord.ButtonStyle.primary, row=0)
    async def lottery(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GenesisHubCog") or interaction.client.get_cog("TradeCenterCog")
        if not cog:
            return await _send_cog_error(interaction, "Genesis")
        await cog._runlottery_impl(interaction)

    @discord.ui.button(label="Orphan Flag", emoji="\U0001f3e0", style=discord.ButtonStyle.secondary, row=0)
    async def orphan(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a team to set orphan status:", view=OrphanTeamSelectView(), ephemeral=True,
        )

    # ── Awards ─────────────────────────────────────────────────────────────
    @discord.ui.button(label="Create Poll", emoji="\U0001f4ca", style=discord.ButtonStyle.primary, row=1)
    async def create_poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossCreatePollModal())

    @discord.ui.button(label="Close Poll", emoji="\U0001f512", style=discord.ButtonStyle.secondary, row=1)
    async def close_poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossClosePollModal())

    # ── Codex ──────────────────────────────────────────────────────────────
    @discord.ui.button(label="Ask Debug", emoji="\U0001f41b", style=discord.ButtonStyle.secondary, row=2)
    async def ask_debug(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossAskDebugModal())

    # ── Roster ─────────────────────────────────────────────────────────────
    @discord.ui.button(label="Assign", emoji="\u2705", style=discord.ButtonStyle.success, row=2)
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to assign to a team:", view=AssignMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="Unassign", emoji="\u274c", style=discord.ButtonStyle.danger, row=2)
    async def unassign(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a member to unassign:", view=UnassignMemberSelectView(), ephemeral=True,
        )

    @discord.ui.button(label="View Roster", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=3)
    async def view_roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        import roster
        all_teams = roster.get_all_teams()
        afc_lines, nfc_lines = [], []
        unassigned = []
        for t in all_teams:
            owner = roster.get_owner(t["abbrName"])
            line = f"**{t['nickName']}** ({t['abbrName']})"
            if owner:
                line += f" \u2014 <@{owner.discord_id}>"
            else:
                unassigned.append(t["nickName"])
            if t["conference"] == "AFC":
                afc_lines.append(line)
            else:
                nfc_lines.append(line)

        embed = discord.Embed(title="\U0001f3c8 TSL Roster", color=GOLD)
        embed.add_field(
            name="\U0001f3c8 AFC", value="\n".join(afc_lines) or "None", inline=True,
        )
        embed.add_field(
            name="\U0001f3c8 NFC", value="\n".join(nfc_lines) or "None", inline=True,
        )
        if unassigned:
            embed.add_field(
                name="\U0001f7e8 Unassigned", value=", ".join(unassigned), inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── League Modals ─────────────────────────────────────────────────────────────

class OrphanTeamSelectView(discord.ui.View):
    """Pick a team (AFC/NFC selects), then set orphan status via buttons."""
    def __init__(self):
        super().__init__(timeout=120)
        import roster
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
        cog = interaction.client.get_cog("GenesisHubCog") or interaction.client.get_cog("TradeCenterCog")
        if not cog:
            return await _send_cog_error(interaction, "Genesis")
        await cog._orphanfranchise_impl(interaction, self.team_abbr, True)

    @discord.ui.button(label="Remove Orphan Flag", emoji="\u2705", style=discord.ButtonStyle.success)
    async def remove_orphan(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("GenesisHubCog") or interaction.client.get_cog("TradeCenterCog")
        if not cog:
            return await _send_cog_error(interaction, "Genesis")
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
        cog = interaction.client.get_cog("AwardsCog")
        if not cog:
            return await _send_cog_error(interaction, "Awards")
        await cog._createpoll_impl(interaction, self.poll_title.value, self.nominees.value)


class BossClosePollModal(discord.ui.Modal, title="Close Poll"):
    poll_id = discord.ui.TextInput(label="Poll ID", placeholder="e.g., A1B2C3D4", required=True)

    async def on_submit(self, interaction: discord.Interaction):
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
        cog = interaction.client.get_cog("CodexCog")
        if not cog:
            return await _send_cog_error(interaction, "Codex")
        await cog._ask_debug_impl(interaction, self.question.value)


class AssignMemberSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a member to assign...")
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        import roster
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
        import roster
        member = select.values[0]
        entry = roster.get_entry_by_id(member.id)
        if not entry:
            return await interaction.response.send_message(
                f"❌ **{member.display_name}** has no team assignment.", ephemeral=True,
            )
        success = roster.unassign(member.id)
        if success:
            embed = discord.Embed(
                title="Team Assignment Removed",
                description=(
                    f"**{member.display_name}** (<@{member.id}>) "
                    f"removed from **{entry.team_name}** ({entry.team_abbr})."
                ),
                color=discord.Color.orange(),
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
        await interaction.response.send_modal(BossCaseViewModal())

    @discord.ui.button(label="List Cases", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary, row=0)
    async def list_cases(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SentinelHubCog") or interaction.client.get_cog("ComplaintCog")
        if not cog:
            return await _send_cog_error(interaction, "Sentinel")
        await cog.caselist_impl(interaction)

    @discord.ui.button(label="Force History", emoji="\U0001f4ca", style=discord.ButtonStyle.secondary, row=0)
    async def force_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("ForceRequestCog") or interaction.client.get_cog("SentinelHubCog")
        if not cog:
            return await _send_cog_error(interaction, "Sentinel")
        await cog.forcehistory_impl(interaction)

    @discord.ui.button(label="Approve Position", emoji="\u2705", style=discord.ButtonStyle.success, row=1)
    async def pos_approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossPositionApproveModal())

    @discord.ui.button(label="Deny Position", emoji="\u274c", style=discord.ButtonStyle.danger, row=1)
    async def pos_deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossPositionDenyModal())

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_home_embed(interaction), view=BossHubView(self.bot))


# ── Compliance Modals ─────────────────────────────────────────────────────────

class BossCaseViewModal(discord.ui.Modal, title="View Complaint Case"):
    case_id = discord.ui.TextInput(label="Case ID", placeholder="e.g., A1B2C3D4", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("SentinelHubCog") or interaction.client.get_cog("ComplaintCog")
        if not cog:
            return await _send_cog_error(interaction, "Sentinel")
        await cog.caseview_impl(interaction, self.case_id.value)


class BossPositionApproveModal(discord.ui.Modal, title="Approve Position Change"):
    log_id = discord.ui.TextInput(label="Log ID", placeholder="e.g., PC-001", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("SentinelHubCog") or interaction.client.get_cog("PositionChangeCog")
        if not cog:
            return await _send_cog_error(interaction, "Sentinel")
        await cog.positionchangeapprove_impl(interaction, self.log_id.value)


class BossPositionDenyModal(discord.ui.Modal, title="Deny Position Change"):
    log_id = discord.ui.TextInput(label="Log ID", placeholder="e.g., PC-001", required=True)
    reason = discord.ui.TextInput(
        label="Reason", placeholder="No reason provided.", required=False,
        default="No reason provided.",
    )

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("SentinelHubCog") or interaction.client.get_cog("PositionChangeCog")
        if not cog:
            return await _send_cog_error(interaction, "Sentinel")
        reason = self.reason.value or "No reason provided."
        await cog.positionchangedeny_impl(interaction, self.log_id.value, reason)


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
        # Permission check
        from permissions import is_commissioner
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
