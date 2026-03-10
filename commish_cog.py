"""
commish_cog.py — ATLAS Commissioner Admin Group
=================================================
Consolidates admin commands from multiple cogs into the hidden /commish group.
Uses delegation pattern — calls _impl methods on existing cogs.

Structure:
  /commish sb <cmd>       — Sportsbook admin (15 commands)
  /commish casino <cmd>   — Casino admin (9 commands)
  /commish eco <cmd>      — Economy admin (10 commands)
  /commish markets <cmd>  — Prediction markets admin (3 commands)
  /commish <cmd>          — Flat admin commands (11 commands)

Hidden from non-admins via default_permissions=administrator.
"""

import typing

import discord
from discord import app_commands
from discord.ext import commands

# ── Group Definition ─────────────────────────────────────────────────────────

commish = app_commands.Group(
    name="commish",
    description="Commissioner administration tools.",
    default_permissions=discord.Permissions(administrator=True),
)

sb = app_commands.Group(name="sb", description="Sportsbook admin.", parent=commish)
casino_admin = app_commands.Group(name="casino", description="Casino admin.", parent=commish)
eco_admin = app_commands.Group(name="eco", description="Economy admin.", parent=commish)
markets_admin = app_commands.Group(name="markets", description="Markets admin.", parent=commish)


class CommishCog(commands.Cog):
    """ATLAS Commissioner — Unified admin command surface."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.tree.add_command(commish)

    async def cog_unload(self):
        self.bot.tree.remove_command(commish.name)

    # ── Helper ───────────────────────────────────────────────────────────────

    def _get(self, name: str):
        """Get a cog by name, return None if not loaded."""
        return self.bot.get_cog(name)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # /commish sb — Sportsbook Admin (15 commands)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @sb.command(name="grade", description="Settle all pending bets for a week.")
    @app_commands.describe(week="Week number to settle")
    async def sb_grade(self, interaction: discord.Interaction, week: int):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._grade_bets_impl(interaction, week)

    @sb.command(name="status", description="Sportsbook overview — lines, locks, pending bets.")
    async def sb_status(self, interaction: discord.Interaction):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_status_impl(interaction)

    @sb.command(name="lines", description="Debug power ratings driving each game's spread.")
    async def sb_lines(self, interaction: discord.Interaction):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_lines_impl(interaction)

    @sb.command(name="setspread", description="Override the spread for a game.")
    @app_commands.describe(matchup="e.g. 'Cowboys @ Eagles'", home_spread="Home team's spread (e.g. -3.5)")
    async def sb_setspread(self, interaction: discord.Interaction, matchup: str, home_spread: float):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_setspread_impl(interaction, matchup, home_spread)

    @sb.command(name="setml", description="Override moneylines for a game.")
    @app_commands.describe(matchup="e.g. 'Cowboys @ Eagles'", home_ml="Home moneyline", away_ml="Away moneyline")
    async def sb_setml(self, interaction: discord.Interaction, matchup: str, home_ml: int, away_ml: int):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_setml_impl(interaction, matchup, home_ml, away_ml)

    @sb.command(name="setou", description="Override the Over/Under total for a game.")
    @app_commands.describe(matchup="e.g. 'Cowboys @ Eagles'", ou_line="Over/Under total points (e.g. 47.5)")
    async def sb_setou(self, interaction: discord.Interaction, matchup: str, ou_line: float):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_setou_impl(interaction, matchup, ou_line)

    @sb.command(name="resetlines", description="Clear ALL admin line overrides — revert to engine.")
    async def sb_resetlines(self, interaction: discord.Interaction):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_resetlines_impl(interaction)

    @sb.command(name="lock", description="Lock or unlock betting for one game.")
    @app_commands.describe(matchup="Game to lock/unlock", locked="True to lock, False to unlock")
    async def sb_lock(self, interaction: discord.Interaction, matchup: str, locked: bool):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_lock_impl(interaction, matchup, locked)

    @sb.command(name="lockall", description="Lock ALL games for the current week.")
    async def sb_lockall(self, interaction: discord.Interaction):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_lockall_impl(interaction)

    @sb.command(name="unlockall", description="Unlock ALL games for the current week.")
    async def sb_unlockall(self, interaction: discord.Interaction):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_unlockall_impl(interaction)

    @sb.command(name="cancelgame", description="Void and refund all pending bets on a game.")
    @app_commands.describe(matchup="Matchup key, e.g. 'Cowboys @ Eagles'")
    async def sb_cancelgame(self, interaction: discord.Interaction, matchup: str):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_cancelgame_impl(interaction, matchup)

    @sb.command(name="refund", description="Refund a single bet by ID.")
    @app_commands.describe(bet_id="Bet ID number")
    async def sb_refund(self, interaction: discord.Interaction, bet_id: int):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_refund_impl(interaction, bet_id)

    @sb.command(name="balance", description="Manually adjust a member's TSL Bucks.")
    @app_commands.describe(
        member="Discord member to adjust",
        adjustment="Amount to add (positive) or remove (negative)",
        reason="Optional reason for the audit log",
    )
    async def sb_balance(self, interaction: discord.Interaction, member: discord.Member,
                         adjustment: int, reason: str = "Commissioner adjustment"):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_balance_impl(interaction, member, adjustment, reason)

    @sb.command(name="addprop", description="Create a custom prop bet for the current week.")
    @app_commands.describe(
        description="Full prop bet description",
        option_a="First option label",
        option_b="Second option label",
        odds_a="American odds for Option A (default -110)",
        odds_b="American odds for Option B (default -110)",
    )
    async def sb_addprop(self, interaction: discord.Interaction, description: str,
                         option_a: str, option_b: str, odds_a: int = -110, odds_b: int = -110):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_addprop_impl(interaction, description, option_a, option_b, odds_a, odds_b)

    @sb.command(name="settleprop", description="Settle a prop bet and pay out winners.")
    @app_commands.describe(prop_id="Prop bet ID number", result="Winning option: a, b, or push")
    @app_commands.choices(result=[
        app_commands.Choice(name="Option A wins", value="a"),
        app_commands.Choice(name="Option B wins", value="b"),
        app_commands.Choice(name="Push (refund all)", value="push"),
    ])
    async def sb_settleprop(self, interaction: discord.Interaction, prop_id: int, result: str):
        cog = self._get("SportsbookCog")
        if not cog:
            return await interaction.response.send_message("Sportsbook not loaded.", ephemeral=True)
        await cog._sb_settleprop_impl(interaction, prop_id, result)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # /commish casino — Casino Admin (9 commands)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    CASINO_GAMES = typing.Literal["blackjack", "crash", "slots", "coinflip"]

    @casino_admin.command(name="status", description="Casino health check and house P&L.")
    async def casino_status(self, interaction: discord.Interaction):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_status_impl(interaction)

    @casino_admin.command(name="open", description="Open the entire casino.")
    async def casino_open(self, interaction: discord.Interaction):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_open_impl(interaction)

    @casino_admin.command(name="close", description="Close the entire casino.")
    async def casino_close(self, interaction: discord.Interaction):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_close_impl(interaction)

    @casino_admin.command(name="opengame", description="Open a specific game.")
    @app_commands.describe(game="Which game to open")
    async def casino_opengame(self, interaction: discord.Interaction,
                              game: typing.Literal["blackjack", "crash", "slots", "coinflip"]):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_open_game_impl(interaction, game)

    @casino_admin.command(name="closegame", description="Close a specific game.")
    @app_commands.describe(game="Which game to close")
    async def casino_closegame(self, interaction: discord.Interaction,
                               game: typing.Literal["blackjack", "crash", "slots", "coinflip"]):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_close_game_impl(interaction, game)

    @casino_admin.command(name="setlimits", description="Adjust casino bet limits.")
    @app_commands.describe(
        max_bet="Maximum bet per game (default 100)",
        daily_min="Minimum daily scratch reward",
        daily_max="Maximum daily scratch reward",
    )
    async def casino_setlimits(self, interaction: discord.Interaction,
                               max_bet: typing.Optional[int] = None,
                               daily_min: typing.Optional[int] = None,
                               daily_max: typing.Optional[int] = None):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_set_limits_impl(interaction, max_bet, daily_min, daily_max)

    @casino_admin.command(name="housereport", description="House P&L by game type.")
    async def casino_housereport(self, interaction: discord.Interaction):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_house_report_impl(interaction)

    @casino_admin.command(name="clearsession", description="Force-clear a stuck blackjack session.")
    @app_commands.describe(user="The user whose session to clear")
    async def casino_clearsession(self, interaction: discord.Interaction, user: discord.Member):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_clear_session_impl(interaction, user)

    @casino_admin.command(name="givescratch", description="Give a user a bonus scratch card.")
    @app_commands.describe(user="The user to gift a scratch card")
    async def casino_givescratch(self, interaction: discord.Interaction, user: discord.Member):
        cog = self._get("CasinoCog")
        if not cog:
            return await interaction.response.send_message("Casino not loaded.", ephemeral=True)
        await cog._casino_give_scratch_impl(interaction, user)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # /commish eco — Economy Admin (10 commands)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @eco_admin.command(name="give", description="Give TSL Bucks to a member.")
    @app_commands.describe(member="Member to give money to", amount="Amount to give",
                           reason="Reason for grant")
    async def eco_give(self, interaction: discord.Interaction, member: discord.Member,
                       amount: int, reason: str = "Commissioner grant"):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_give_impl(interaction, member, amount, reason)

    @eco_admin.command(name="take", description="Take TSL Bucks from a member.")
    @app_commands.describe(member="Member to take money from", amount="Amount to take",
                           reason="Reason for deduction")
    async def eco_take(self, interaction: discord.Interaction, member: discord.Member,
                       amount: int, reason: str = "Commissioner deduction"):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_take_impl(interaction, member, amount, reason)

    @eco_admin.command(name="set", description="Set a member's exact balance.")
    @app_commands.describe(member="Member", amount="New balance", reason="Reason")
    async def eco_set(self, interaction: discord.Interaction, member: discord.Member,
                      amount: int, reason: str = "Commissioner set"):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_set_impl(interaction, member, amount, reason)

    @eco_admin.command(name="check", description="Check a member's balance.")
    @app_commands.describe(member="Member to check")
    async def eco_check(self, interaction: discord.Interaction, member: discord.Member):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_check_impl(interaction, member)

    @eco_admin.command(name="giverole", description="Give TSL Bucks to all members with a role.")
    @app_commands.describe(role="Role to pay", amount="Amount per member",
                           reason="Reason for grant")
    async def eco_giverole(self, interaction: discord.Interaction, role: discord.Role,
                           amount: int, reason: str = "Role grant"):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_give_role_impl(interaction, role, amount, reason)

    @eco_admin.command(name="takerole", description="Take TSL Bucks from all members with a role.")
    @app_commands.describe(role="Role to deduct from", amount="Amount per member",
                           reason="Reason for deduction")
    async def eco_takerole(self, interaction: discord.Interaction, role: discord.Role,
                           amount: int, reason: str = "Role deduction"):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_take_role_impl(interaction, role, amount, reason)

    @eco_admin.command(name="stipendadd", description="Set up a recurring payment for a role.")
    @app_commands.describe(
        role="Role to pay",
        amount="Amount per interval (positive=income, negative=deduction)",
        interval="Payment frequency",
        reason="Description of the stipend",
    )
    @app_commands.choices(interval=[
        app_commands.Choice(name="Daily", value="daily"),
        app_commands.Choice(name="Weekly", value="weekly"),
        app_commands.Choice(name="Biweekly", value="biweekly"),
        app_commands.Choice(name="Monthly", value="monthly"),
    ])
    async def eco_stipendadd(self, interaction: discord.Interaction, role: discord.Role,
                             amount: int, interval: app_commands.Choice[str],
                             reason: str = "Recurring stipend"):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_stipend_add_impl(interaction, role, amount, interval.value, reason)

    @eco_admin.command(name="stipendremove", description="Remove a recurring stipend for a role.")
    @app_commands.describe(role="Role whose stipend to remove")
    async def eco_stipendremove(self, interaction: discord.Interaction, role: discord.Role):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_stipend_remove_impl(interaction, role)

    @eco_admin.command(name="stipendlist", description="View all active stipends.")
    async def eco_stipendlist(self, interaction: discord.Interaction):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_stipend_list_impl(interaction)

    @eco_admin.command(name="stipendpaynow", description="Manually trigger all due stipend payments.")
    async def eco_stipendpaynow(self, interaction: discord.Interaction):
        cog = self._get("EconomyCog")
        if not cog:
            return await interaction.response.send_message("Economy not loaded.", ephemeral=True)
        await cog._eco_stipend_paynow_impl(interaction)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # /commish markets — Prediction Markets Admin (3 commands)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @markets_admin.command(name="resolve", description="Resolve a prediction market outcome.")
    @app_commands.describe(slug="Market slug to resolve", result="YES, NO, or VOID")
    async def markets_resolve(self, interaction: discord.Interaction, slug: str, result: str):
        cog = self._get("PolymarketCog")
        if not cog:
            return await interaction.response.send_message("Polymarket not loaded.", ephemeral=True)
        await cog._resolve_market_impl(interaction, slug, result)

    @markets_admin.command(name="status", description="Show Polymarket sync status and stats.")
    async def markets_status(self, interaction: discord.Interaction):
        cog = self._get("PolymarketCog")
        if not cog:
            return await interaction.response.send_message("Polymarket not loaded.", ephemeral=True)
        await cog._market_status_impl(interaction)

    @markets_admin.command(name="approve", description="Approve a long-term market for betting.")
    @app_commands.describe(slug="Market slug to approve")
    async def markets_approve(self, interaction: discord.Interaction, slug: str):
        cog = self._get("PolymarketCog")
        if not cog:
            return await interaction.response.send_message("Polymarket not loaded.", ephemeral=True)
        await cog._approve_market_impl(interaction, slug)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # /commish <flat> — Misc Admin Commands (11 commands)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @commish.command(name="tradelist", description="List recent pending trades.")
    async def tradelist(self, interaction: discord.Interaction):
        cog = self._get("GenesisCog")
        if not cog:
            return await interaction.response.send_message("Genesis not loaded.", ephemeral=True)
        await cog._tradelist_impl(interaction)

    @commish.command(name="runlottery", description="Run the weighted draft lottery draw.")
    async def runlottery(self, interaction: discord.Interaction):
        cog = self._get("GenesisCog")
        if not cog:
            return await interaction.response.send_message("Genesis not loaded.", ephemeral=True)
        await cog._runlottery_impl(interaction)

    @commish.command(name="orphan", description="Set or clear orphan franchise flag for a team.")
    @app_commands.describe(team="Team name", flag="True to set orphan, False to clear")
    async def orphan(self, interaction: discord.Interaction, team: str, flag: bool):
        cog = self._get("GenesisCog")
        if not cog:
            return await interaction.response.send_message("Genesis not loaded.", ephemeral=True)
        await cog._orphanfranchise_impl(interaction, team, flag)

    @commish.command(name="caseview", description="View a complaint case by ID.")
    @app_commands.describe(case_id="The complaint case ID")
    async def caseview(self, interaction: discord.Interaction, case_id: str):
        cog = self._get("SentinelCog")
        if not cog:
            return await interaction.response.send_message("Sentinel not loaded.", ephemeral=True)
        await cog.caseview_impl(interaction, case_id)

    @commish.command(name="caselist", description="List all open/pending complaints.")
    async def caselist(self, interaction: discord.Interaction):
        cog = self._get("SentinelCog")
        if not cog:
            return await interaction.response.send_message("Sentinel not loaded.", ephemeral=True)
        await cog.caselist_impl(interaction)

    @commish.command(name="forcehistory", description="View force request stats for this session.")
    async def forcehistory(self, interaction: discord.Interaction):
        cog = self._get("SentinelCog")
        if not cog:
            return await interaction.response.send_message("Sentinel not loaded.", ephemeral=True)
        await cog.forcehistory_impl(interaction)

    @commish.command(name="positionapprove", description="Approve a pending position change.")
    @app_commands.describe(log_id="Log ID from the pending request")
    async def positionapprove(self, interaction: discord.Interaction, log_id: str):
        cog = self._get("SentinelCog")
        if not cog:
            return await interaction.response.send_message("Sentinel not loaded.", ephemeral=True)
        await cog.positionchangeapprove_impl(interaction, log_id)

    @commish.command(name="positiondeny", description="Deny a pending position change.")
    @app_commands.describe(log_id="Log ID from the pending request", reason="Reason for denial")
    async def positiondeny(self, interaction: discord.Interaction, log_id: str,
                           reason: str = "No reason provided."):
        cog = self._get("SentinelCog")
        if not cog:
            return await interaction.response.send_message("Sentinel not loaded.", ephemeral=True)
        await cog.positionchangedeny_impl(interaction, log_id, reason)

    @commish.command(name="createpoll", description="Create an anonymous award poll.")
    @app_commands.describe(title="Poll title", nominees="Comma-separated list of nominees")
    async def createpoll(self, interaction: discord.Interaction, title: str, nominees: str):
        cog = self._get("AwardsCog")
        if not cog:
            return await interaction.response.send_message("Awards not loaded.", ephemeral=True)
        await cog._createpoll_impl(interaction, title, nominees)

    @commish.command(name="closepoll", description="Close a poll and reveal results.")
    @app_commands.describe(poll_id="Poll ID to close")
    async def closepoll(self, interaction: discord.Interaction, poll_id: str):
        cog = self._get("AwardsCog")
        if not cog:
            return await interaction.response.send_message("Awards not loaded.", ephemeral=True)
        await cog._closepoll_impl(interaction, poll_id)

    @commish.command(name="askdebug", description="Show generated SQL + raw rows for any /ask question.")
    @app_commands.describe(question="Your question about TSL history")
    async def askdebug(self, interaction: discord.Interaction, question: str):
        cog = self._get("CodexCog")
        if not cog:
            return await interaction.response.send_message("Codex not loaded.", ephemeral=True)
        await cog._ask_debug_impl(interaction, question)


async def setup(bot: commands.Bot):
    await bot.add_cog(CommishCog(bot))
    print("ATLAS: Commissioner · Admin Command Hub loaded.")
