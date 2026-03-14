"""
guide_cog.py — ATLAS User Guide System
========================================
Dual-delivery guide system for onboarding new users:
  1. Pinned embeds auto-posted to category channels during /setup
  2. Interactive /guide slash command with module picker

All content written in ATLAS persona voice (3rd person, opinionated).
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from setup_cog import get_channel_id

try:
    from permissions import is_commissioner
except ImportError:
    async def is_commissioner(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator

# ── Branding ─────────────────────────────────────────────────────────────────

ATLAS_GOLD = 0xC9962A

try:
    from constants import ATLAS_ICON_URL
except ImportError:
    ATLAS_ICON_URL = None

# ── Guide Embed Builders ────────────────────────────────────────────────────

def _build_overview_guide() -> discord.Embed:
    """General ATLAS overview — pinned in #ask-atlas."""
    embed = discord.Embed(
        title="\U0001f4d6 ATLAS — Welcome to the League",
        description=(
            "ATLAS runs everything in TSL. Stats, trades, rules, betting, casino — "
            "all of it flows through here. This is your cheat sheet."
        ),
        color=ATLAS_GOLD,
    )
    embed.add_field(
        name="\U0001f4ca Oracle — Stats & Analytics",
        value="`/stats hub` — Power rankings, team cards, player stats, hot/cold streaks",
        inline=False,
    )
    embed.add_field(
        name="\U0001f91d Genesis — Trades & Roster",
        value="`/genesis` — Trade evaluations, rosters, dev traits, draft board",
        inline=False,
    )
    embed.add_field(
        name="\U0001f6e1\ufe0f Sentinel — Rules & Compliance",
        value="`/sentinel` — Force requests, 4th down rules, blowout monitor",
        inline=False,
    )
    embed.add_field(
        name="\U0001f4b0 Flow — Economy & Sportsbook",
        value="`/flow` — Wallet, sportsbook, prediction markets, leaderboard",
        inline=False,
    )
    embed.add_field(
        name="\U0001f3b0 Casino — Games",
        value="Blackjack, slots, crash, coinflip — gamble your Flow in the casino channels",
        inline=False,
    )
    embed.add_field(
        name="\U0001f50d Need Help?",
        value="Use `/guide` anywhere to pull up the interactive module picker.",
        inline=False,
    )
    embed.set_footer(text="ATLAS\u2122 \u2014 Autonomous TSL League Administration System")
    if ATLAS_ICON_URL:
        embed.set_thumbnail(url=ATLAS_ICON_URL)
    return embed


def _build_oracle_guide() -> discord.Embed:
    """Oracle module guide — stats, analytics, power rankings."""
    embed = discord.Embed(
        title="\U0001f4ca ATLAS Oracle Guide",
        description=(
            "ATLAS knows every stat, every record, every trend. "
            "The Oracle module is where you go to see who's really about it."
        ),
        color=ATLAS_GOLD,
    )
    fields = [
        ("`/stats hub`", "ATLAS's analytics command center \u2014 everything starts here"),
        ("Team Card", "Full franchise profile \u2014 record, roster, ratings at a glance"),
        ("Owner Card", "Owner legacy \u2014 rings, win rate, career arc across 95+ seasons"),
        ("Hot/Cold", "Who's streaking, who's slumping \u2014 last 5 games tell the truth"),
        ("Clutch", "4th quarter comeback kings and choke artists \u2014 ATLAS keeps receipts"),
        ("Power Rankings", "Composite power ratings \u2014 W%, OVR, offense, defense all weighted"),
        ("Week Scores", "This week's game results and final scores"),
        ("Season Story", "AI-generated narrative recap of the full season arc"),
        ("Draft Grades", "How owners drafted \u2014 hits, misses, and outright steals"),
        ("Player Stats", "Individual stat leaders across every position"),
    ]
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Use /guide to see all modules")
    return embed


def _build_genesis_guide() -> discord.Embed:
    """Genesis module guide — trades, roster, dev traits."""
    embed = discord.Embed(
        title="\U0001f91d ATLAS Genesis Guide",
        description=(
            "Genesis handles the roster moves that shape dynasties. "
            "Trades, dev traits, draft picks \u2014 every move runs through here."
        ),
        color=ATLAS_GOLD,
    )
    fields = [
        ("`/genesis`", "Trade center and roster management hub"),
        ("Trade Eval", "AI-powered trade analysis \u2014 fair deal or highway robbery?"),
        ("Roster", "Current team rosters with OVR and dev traits listed"),
        ("Dev Traits", "Superstar and X-Factor upgrade tracker"),
        ("Draft Board", "Draft pick ownership and trade history"),
        ("Cap Calculator", "Salary cap projections and available space"),
    ]
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Use /guide to see all modules")
    return embed


def _build_sentinel_guide() -> discord.Embed:
    """Sentinel module guide — rules, compliance, enforcement."""
    embed = discord.Embed(
        title="\U0001f6e1\ufe0f ATLAS Sentinel Guide",
        description=(
            "Sentinel enforces the rules so nobody has to argue about it. "
            "Force requests, 4th down calls, blowout monitoring \u2014 all automated."
        ),
        color=ATLAS_GOLD,
    )
    fields = [
        ("`/sentinel`", "Rule enforcement and compliance hub"),
        ("How to Request", "How to submit a force request for ATLAS review"),
        ("4th Down Guide", "4th down rules and when you can go for it"),
        ("Blowout Monitor", "Tracks lopsided games for mercy rule enforcement"),
        ("Compliance Check", "Verify your team meets all league requirements"),
    ]
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Use /guide to see all modules")
    return embed


def _build_flow_guide() -> discord.Embed:
    """Flow + Casino guide — economy, sportsbook, casino games."""
    embed = discord.Embed(
        title="\U0001f4b0 ATLAS Flow & Casino Guide",
        description=(
            "Flow is the TSL economy. Earn it, bet it, lose it all at the casino. "
            "ATLAS tracks every transaction."
        ),
        color=ATLAS_GOLD,
    )
    fields = [
        ("`/flow`", "Economy hub \u2014 wallet, bets, leaderboard, everything money"),
        ("Sportsbook", "Bet on TSL games with ELO-based odds \u2014 spreads update live"),
        ("Casino", "Blackjack, slots, crash, coinflip \u2014 gamble your Flow"),
        ("Markets", "Prediction markets on league outcomes \u2014 bet on the future"),
        ("Wallet", "Check your Flow balance and transaction history"),
        ("Leaderboard", "Richest owners in the league \u2014 who's stacking"),
        ("My Bets", "Track your open and settled wagers"),
    ]
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Use /guide to see all modules")
    return embed


# ── Guide Picker View ───────────────────────────────────────────────────────

class GuidePickerView(discord.ui.View):
    """Persistent view with 5 module buttons for /guide."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Oracle", emoji="\U0001f4ca",
        style=discord.ButtonStyle.primary,
        custom_id="atlas:guide:oracle", row=0,
    )
    async def btn_oracle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=_build_oracle_guide(), ephemeral=True)

    @discord.ui.button(
        label="Genesis", emoji="\U0001f91d",
        style=discord.ButtonStyle.primary,
        custom_id="atlas:guide:genesis", row=0,
    )
    async def btn_genesis(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=_build_genesis_guide(), ephemeral=True)

    @discord.ui.button(
        label="Sentinel", emoji="\U0001f6e1\ufe0f",
        style=discord.ButtonStyle.primary,
        custom_id="atlas:guide:sentinel", row=0,
    )
    async def btn_sentinel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=_build_sentinel_guide(), ephemeral=True)

    @discord.ui.button(
        label="Flow & Casino", emoji="\U0001f4b0",
        style=discord.ButtonStyle.primary,
        custom_id="atlas:guide:flow", row=0,
    )
    async def btn_flow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=_build_flow_guide(), ephemeral=True)


# ── Pin Targets ──────────────────────────────────────────────────────────────

# Maps config_key → (embed builder, description for logging)
_PIN_TARGETS: list[tuple[str, callable, str]] = [
    ("ask_atlas",    _build_overview_guide,  "Overview"),
    ("announcements", _build_oracle_guide,   "Oracle"),
    ("trades",        _build_genesis_guide,  "Genesis"),
    ("compliance",    _build_sentinel_guide, "Sentinel"),
    ("sportsbook",    _build_flow_guide,     "Flow & Casino"),
]


# ── Cog ──────────────────────────────────────────────────────────────────────

class GuideCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        print("ATLAS: Guide System loaded.")

    # ── /guide ────────────────────────────────────────────────────────────

    guide_group = app_commands.Group(
        name="guide",
        description="ATLAS user guides and module reference",
    )

    @guide_group.command(name="show", description="Browse ATLAS module guides")
    async def guide_show(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="\U0001f4d6 ATLAS Guide — Module Picker",
            description="Pick a module to see its full command reference.",
            color=ATLAS_GOLD,
        )
        if ATLAS_ICON_URL:
            embed.set_thumbnail(url=ATLAS_ICON_URL)
        await interaction.response.send_message(
            embed=embed, view=GuidePickerView(), ephemeral=True,
        )

    @guide_group.command(
        name="refresh",
        description="[Admin] Re-post guide embeds to all category channels",
    )
    @app_commands.default_permissions(administrator=True)
    async def guide_refresh(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            await interaction.response.send_message(
                "Commissioner access required.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        posted, failed = await self.post_all_guides(guild)

        lines = []
        if posted:
            lines.append(f"**Posted:** {', '.join(posted)}")
        if failed:
            lines.append(f"**Failed:** {', '.join(failed)}")
        if not lines:
            lines.append("No channels configured. Run `/setup` first.")

        await interaction.followup.send(
            "\n".join(lines), ephemeral=True,
        )

    # ── Public method for setup_cog integration ──────────────────────────

    async def post_all_guides(
        self, guild: discord.Guild
    ) -> tuple[list[str], list[str]]:
        """
        Post guide embeds to target channels and pin them.
        Returns (posted_labels, failed_labels).
        """
        posted: list[str] = []
        failed: list[str] = []

        for config_key, builder, label in _PIN_TARGETS:
            try:
                ch_id = get_channel_id(config_key, guild.id)
                if not ch_id:
                    failed.append(f"{label} (no channel ID)")
                    continue

                channel = guild.get_channel(ch_id)
                if not channel:
                    failed.append(f"{label} (channel not found)")
                    continue

                embed = builder()
                msg = await channel.send(embed=embed)

                try:
                    await msg.pin(reason="ATLAS Guide")
                except discord.Forbidden:
                    pass  # pin failed but embed still posted
                except discord.HTTPException:
                    pass  # e.g. too many pins

                posted.append(label)
                print(f"[GUIDE] Posted {label} guide to #{channel.name}")

            except Exception as e:
                failed.append(f"{label} ({e})")
                print(f"[GUIDE] Failed to post {label}: {e}")

        return posted, failed


# ── Extension setup ──────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    bot.add_view(GuidePickerView())  # register persistent view for restart
    await bot.add_cog(GuideCog(bot))
