"""
oracle_cog.py — ATLAS · Oracle Module v1.0
─────────────────────────────────────────────────────────────────────────────
ATLAS Oracle is the stats intelligence and analytics system.

Consolidated from: analytics_cog, stats_hub_cog

Register in bot.py setup_hook():
    await bot.load_extension("oracle_cog")

Slash commands:
  /stats hub               — Stats Hub navigation panel
  /stats hotcold [player]  — Hot/Cold report (league-wide or single player)
  /stats clutch [margin]   — Clutch rankings
  /stats draft [season]    — Draft class report
  /stats power             — Live power rankings
  /stats recap [week]      — Weekly game recap
  /stats team <name>       — Team stat card
  /stats owner [user]      — Owner profile card
  /stats player <name>     — Player hot/cold breakdown
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

# ── Unified imports ───────────────────────────────────────────────────────────
import asyncio
import datetime
import json
import re
import sqlite3
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import analysis as an
import data_manager as dm
import intelligence as ig

from permissions import ADMIN_USER_IDS

try:
    from echo_loader import get_persona
except ImportError:
    get_persona = lambda _mode="casual": "You are ATLAS."

import atlas_ai
from atlas_ai import Tier


# ══════════════════════════════════════════════════════════════════════════════
#  ORACLE · ANALYTICS HELPERS
# ══════════════════════════════════════════════════════════════════════════════
# NOTE: Color palette (C_HOT, C_COLD, etc.), _season_label(), _rank_emoji(),
# _trend_bar(), _dev_emoji(), _grade_color(), _winpct_bar(), _record_str(),
# _ai_blurb(), and ATLAS_ICON_URL are defined once in the Stats Hub section
# below (lines ~800+). Do NOT duplicate them here.


def _get_user_tier(bot: commands.Bot, user_id: int) -> str:
    """Fetches user tier from SupportCog. Defaults to 'Elite' if missing to prevent breaks."""
    support_cog = bot.get_cog("SupportCog")
    if support_cog and hasattr(support_cog, "get_user_tier"):
        return support_cog.get_user_tier(user_id)
    # SupportCog not loaded — default to Elite so nothing breaks
    return "Elite"


_EMBED_DESC_LIMIT = 4096

def _truncate_for_embed(text: str, limit: int = _EMBED_DESC_LIMIT) -> str:
    """Truncate text to fit within Discord embed description limit."""
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."

#  NAVIGATION VIEW (shared across all report types)
# ─────────────────────────────────────────────────────────────────────────────
# NOTE: First AnalyticsNav class was removed — it was an exact duplicate
# immediately overwritten by the second definition below.

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS — Stale Data Footer + Share Button
# ─────────────────────────────────────────────────────────────────────────────


def _apply_stale_footer(embed: discord.Embed, module: str = "Oracle") -> None:
    """Append a stale-data warning to the embed footer if data is >30min old."""
    stale = dm.get_sync_age_text()
    base = f"ATLAS\u2122 {module}"
    if stale:
        embed.set_footer(text=f"{base} \u00b7 \u26a0\ufe0f {stale}")
    else:
        embed.set_footer(text=base)


class ShareToChannelView(discord.ui.View):
    """Wraps an embed with a 'Share to Channel' button that re-posts it publicly."""

    def __init__(self, embed: discord.Embed):
        super().__init__(timeout=120)
        self._embed = embed

    @discord.ui.button(label="Share to Channel", style=discord.ButtonStyle.secondary, emoji="\U0001f4e4")
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.channel.send(embed=self._embed)
        self.stop()


# ─────────────────────────────────────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────────────────────────────────────

class AnalyticsNav(discord.ui.View):
    """Persistent tab bar allowing quick navigation between Analytics reports."""

    def __init__(self, bot: commands.Bot, origin_user_id: int):
        super().__init__(timeout=300)
        self.bot_ref        = bot
        self.origin_user_id = origin_user_id

    @discord.ui.button(label="🔥 Hot/Cold", style=discord.ButtonStyle.secondary, row=0)
    async def btn_hotcold(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, _ = _build_hotcold_league()
        _apply_stale_footer(embed)
        await interaction.followup.send(embed=embed, view=ShareToChannelView(embed), ephemeral=True)

    @discord.ui.button(label="⚡ Clutch", style=discord.ButtonStyle.secondary, row=0)
    async def btn_clutch(self, interaction: discord.Interaction, button: discord.ui.Button):
        tier = _get_user_tier(self.bot_ref, interaction.user.id)
        if tier not in ["Pro", "Elite"]:
            await interaction.response.send_message("🔒 **Pro Tier Required.** Use `/membership info` to unlock Clutch Rankings.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_clutch_embed()
        _apply_stale_footer(embed)
        await interaction.followup.send(embed=embed, view=ShareToChannelView(embed), ephemeral=True)

    @discord.ui.button(label="📊 Power", style=discord.ButtonStyle.secondary, row=0)
    async def btn_power(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_power_embed()
        _apply_stale_footer(embed)
        await interaction.followup.send(embed=embed, view=ShareToChannelView(embed), ephemeral=True)

    @discord.ui.button(label="📋 Draft History", style=discord.ButtonStyle.secondary, row=1)
    async def btn_draft(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📜 Draft Class Explorer",
            description=(
                "Select a **team** and **season** to view their draft class.\n\n"
                "Each player shows their round, pick, OVR, dev trait, and grade.\n"
                "Traded players are flagged with 🔄 — click their button for the full trade breakdown."
            ),
            color=C_GOLD,
        )
        embed.set_footer(text="ATLAS™ Oracle · Draft Class Explorer", icon_url=ATLAS_ICON_URL)
        view = DraftClassView(self.bot_ref)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="📅 Recap", style=discord.ButtonStyle.secondary, row=1)
    async def btn_recap(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_recap_embed()
        _apply_stale_footer(embed)
        await interaction.followup.send(embed=embed, view=ShareToChannelView(embed), ephemeral=True)

    @discord.ui.button(label="👤 My Profile", style=discord.ButtonStyle.primary, row=1)
    async def btn_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await _build_owner_embed(interaction.user, interaction.guild)
        _apply_stale_footer(embed)
        await interaction.followup.send(embed=embed, view=ShareToChannelView(embed), ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  ORACLE · STATS HUB
# ══════════════════════════════════════════════════════════════════════════════


# ── Optional codex pipeline (SQL + AI-powered NL queries) ─────────────────────
# Module-scope defaults so names are always bound (Pyright safety)
run_sql = None
fuzzy_resolve_user = None
resolve_names_in_question = None
gemini_sql = None
gemini_answer = None
extract_sql = None
validate_sql = None
retry_sql = None
KNOWN_USERS = ""
_build_schema_fn = None
_build_conversation_block = None
_add_conversation_turn = None

try:
    from codex_cog import (
        gemini_sql,
        gemini_answer,
        run_sql,
        extract_sql,
        validate_sql,
        retry_sql,
        fuzzy_resolve_user,
        resolve_names_in_question,
        ai_resolve_names as _ai_resolve_names,
        build_conversation_block as _build_conversation_block,
        add_conversation_turn as _add_conversation_turn,
        _build_schema as _build_schema_fn,
        KNOWN_USERS,
    )
    _HISTORY_OK = True
except ImportError:
    _HISTORY_OK = False
    _ai_resolve_names = None
    print("[oracle_cog] codex_cog not available — /ask history queries disabled")

# Shared H2H SQL from intent system
_get_h2h_sql = None
_detect_intent = None
_resolve_db_username_fn = None
try:
    from codex_intents import get_h2h_sql_and_params as _get_h2h_sql
    from codex_intents import detect_intent as _detect_intent
except ImportError:
    pass
try:
    from build_member_db import resolve_db_username as _resolve_db_username_fn
except ImportError:
    pass

# Optional affinity module
try:
    import affinity as _affinity_mod
except ImportError:
    _affinity_mod = None


# ── ATLAS branding constants ──────────────────────────────────────────────────
from constants import ATLAS_ICON_URL
from atlas_colors import AtlasColors
ATLAS_GOLD = AtlasColors.TSL_GOLD

# ── AI clients managed by atlas_ai module ─────────────────────────────────────

# ── QueryBuilder import (Oracle v3 domain-aware SQL) ──────────────────────────
_QB_OK = False
try:
    import oracle_query_builder as qb
    _QB_OK = True
except ImportError:
    print("[oracle_cog] oracle_query_builder not available — Claude query routing disabled")

# ── Claude-powered Oracle v3 functions ────────────────────────────────────────

# Tool definitions for Claude — maps to QueryBuilder Layer 1 functions
_ORACLE_TOOLS = [
    {
        "name": "stat_leaders",
        "description": "Get player stat leaders/rankings. Use for questions like 'who leads in passing yards', 'top rushers', 'most sacks', 'worst passer rating'. Supports 39 stats including passYds, passTDs, rushYds, rushTDs, recYds, recTDs, defSacks, defInts, defTotalTackles, passerRating, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stat": {"type": "string", "description": "Stat name (e.g. passYds, rushTDs, defSacks, passerRating)"},
                "season": {"type": "integer", "description": "Season number (omit for all-time)"},
                "sort": {"type": "string", "enum": ["best", "worst"], "description": "Sort direction. 'best' = most/highest, 'worst' = least/lowest"},
                "limit": {"type": "integer", "description": "Number of results (default 10)"},
            },
            "required": ["stat"],
        },
    },
    {
        "name": "team_stat_leaders",
        "description": "Get team stat rankings. Use for 'best rushing team', 'worst defense', 'most points scored by team'. Stats: totalYds, passYds, rushYds, ptsFor, ptsAgainst, totalYdsAgainst, sacks, takeaways, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stat": {"type": "string", "description": "Team stat name"},
                "season": {"type": "integer", "description": "Season number (omit for all-time)"},
                "sort": {"type": "string", "enum": ["best", "worst"]},
                "limit": {"type": "integer"},
            },
            "required": ["stat"],
        },
    },
    {
        "name": "h2h",
        "description": "Head-to-head record between two owners. Use for 'record between X and Y', 'how many times has X beaten Y'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user1": {"type": "string", "description": "First owner's DB username"},
                "user2": {"type": "string", "description": "Second owner's DB username"},
                "season": {"type": "integer", "description": "Season number (omit for all-time)"},
            },
            "required": ["user1", "user2"],
        },
    },
    {
        "name": "owner_record",
        "description": "Win/loss record for an owner. Use for 'what is X's record', 'how many wins does X have'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "Owner's DB username"},
                "season": {"type": "integer", "description": "Season number (omit for all-time)"},
            },
            "required": ["user"],
        },
    },
    {
        "name": "standings",
        "description": "Current season standings. Use for 'standings', 'who is in first place', 'AFC East standings'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "division": {"type": "string", "description": "Division name (e.g. 'AFC East', 'NFC West')"},
                "conference": {"type": "string", "description": "Conference (AFC or NFC)"},
            },
            "required": [],
        },
    },
    {
        "name": "recent_games",
        "description": "Recent games for an owner, optionally vs a specific opponent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "Owner's DB username"},
                "limit": {"type": "integer", "description": "Number of games (default 5)"},
                "opponent": {"type": "string", "description": "Opponent's DB username (optional)"},
            },
            "required": ["user"],
        },
    },
    {
        "name": "roster",
        "description": "Current team roster with player ratings. Use for 'show me the Ravens roster', 'best WRs on the Chiefs'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Team name (e.g. 'Ravens', 'Chiefs')"},
                "pos": {"type": "string", "description": "Position filter (e.g. 'QB', 'WR', 'HB')"},
                "sort_by": {"type": "string", "description": "Sort column (default playerBestOvr)"},
            },
            "required": ["team"],
        },
    },
    {
        "name": "trades",
        "description": "Trade history. Use for 'recent trades', 'trades involving the Ravens', 'what has X traded'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Team name"},
                "season": {"type": "integer", "description": "Season number"},
                "user": {"type": "string", "description": "Owner's DB username"},
            },
            "required": [],
        },
    },
    {
        "name": "draft_picks",
        "description": "Draft history. Use for 'who did X draft', 'first round picks season 5'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Team name"},
                "season": {"type": "integer", "description": "Season number"},
                "round": {"type": "integer", "description": "Draft round (1-7)"},
            },
            "required": [],
        },
    },
    {
        "name": "game_extremes",
        "description": "Record-setting games. Use for 'biggest blowout', 'closest game', 'highest scoring game'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["blowout", "closest", "highest", "lowest"], "description": "Type of extreme"},
                "season": {"type": "integer", "description": "Season number (omit for all-time)"},
                "limit": {"type": "integer", "description": "Number of results (default 5)"},
            },
            "required": ["type"],
        },
    },
    {
        "name": "streak",
        "description": "Current win/loss streak for an owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "Owner's DB username"},
            },
            "required": ["user"],
        },
    },
    {
        "name": "owner_history",
        "description": "Ownership tenure history — who owned what team in which season.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "Owner's DB username"},
                "team": {"type": "string", "description": "Team name"},
            },
            "required": [],
        },
    },
    {
        "name": "abilities",
        "description": "Player abilities (X-Factor, Superstar). Use for 'what abilities does X have', 'X-Factor players on Ravens'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Team name"},
                "player": {"type": "string", "description": "Player name"},
            },
            "required": [],
        },
    },
    {
        "name": "career_trajectory",
        "description": "How an owner's stat has changed across seasons. Use for 'show me X's passing yards over the years'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "Owner's DB username"},
                "stat": {"type": "string", "description": "Stat name"},
            },
            "required": ["user", "stat"],
        },
    },
    {
        "name": "free_agents",
        "description": "Available free agents. Use for 'best free agent QBs', 'free agents over 80 OVR'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pos": {"type": "string", "description": "Position filter"},
                "min_ovr": {"type": "integer", "description": "Minimum overall rating"},
            },
            "required": [],
        },
    },
    {
        "name": "team_record_query",
        "description": "Win/loss record for a team (not owner). Use for 'Ravens record this season'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Team name"},
                "season": {"type": "integer", "description": "Season number"},
            },
            "required": ["team"],
        },
    },
]

# Dispatch map: tool name → QueryBuilder function
def _dispatch_tool(name: str, args: dict) -> tuple[list[dict], str | None]:
    """Execute a QueryBuilder Layer 1 function by tool name."""
    fn_map = {
        "stat_leaders": qb.stat_leaders,
        "team_stat_leaders": qb.team_stat_leaders,
        "h2h": qb.h2h,
        "owner_record": qb.owner_record,
        "standings": qb.standings,
        "recent_games": qb.recent_games,
        "roster": qb.roster,
        "trades": qb.trades,
        "draft_picks": qb.draft_picks,
        "game_extremes": qb.game_extremes,
        "streak": qb.streak,
        "owner_history": qb.owner_history,
        "abilities": qb.abilities,
        "career_trajectory": qb.career_trajectory,
        "free_agents": qb.free_agents,
        "team_record_query": qb.team_record_query,
    }
    fn = fn_map.get(name)
    if not fn:
        return [], f"Unknown tool: {name}"
    try:
        return fn(**args)
    except Exception as e:
        return [], str(e)


def _build_oracle_system_prompt(caller_db: str | None = None, conv_context: str = "", affinity: str = "") -> str:
    """Build the system prompt for Claude Oracle queries."""
    persona = get_persona("analytical")

    known_users = ""
    try:
        from build_member_db import get_alias_map
        alias_map = get_alias_map()
        known_users = ", ".join(sorted(set(alias_map.values())))
    except Exception:
        pass

    caller_block = ""
    if caller_db:
        caller_block = (
            f"\nThe person asking is TSL owner with db_username='{caller_db}'. "
            f"When they say 'me', 'my', or 'I', use '{caller_db}' as the user parameter.\n"
        )

    return f"""{persona}

You have access to tools that query the TSL database. Use them to answer questions about stats, records, rosters, trades, and league history.

CURRENT CONTEXT:
- Current season: {dm.CURRENT_SEASON}
- Current week: {dm.CURRENT_WEEK}
{caller_block}
KNOWN TSL OWNERS (use these exact db_usernames in tool calls):
{known_users}

IMPORTANT RULES:
- Always resolve owner names to their db_username before calling tools. Common nicknames: 'Witt'/'TheWitt', 'JT'/'jtcurrent32', 'Killa'/'KillaE94', etc.
- Use resolve_team for team names — e.g. 'Baltimore' → 'Ravens'
- For 'this season', use season={dm.CURRENT_SEASON}
- For 'all time' / 'career', omit the season parameter
- Call the most appropriate tool for each question. If unsure, try stat_leaders or owner_record.
- If a tool returns an error, explain what happened — don't silently fail.
{conv_context}{affinity}"""


async def _claude_query(question: str, caller_db: str | None = None,
                        conv_context: str = "", affinity: str = "") -> tuple[str, list[dict], str]:
    """
    Route a TSL question through Claude → QueryBuilder tools → Claude answer.
    Returns (answer_text, rows, tool_used).
    """
    system = _build_oracle_system_prompt(caller_db, conv_context, affinity)

    # Resolve names in the question
    annotated = question
    alias_map = {}
    if resolve_names_in_question:
        annotated, alias_map = resolve_names_in_question(question)

    # Step 1: Claude picks tools
    result = await atlas_ai.generate_with_tools(
        annotated, tools=_ORACLE_TOOLS,
        system=system, tier=Tier.SONNET, max_tokens=1024,
    )

    # Step 2: Execute tool calls and collect results
    all_rows = []
    tool_used = ""
    tool_results = []

    for tc in result.tool_calls:
        tool_used = tc["name"]
        rows, error = _dispatch_tool(tc["name"], tc["input"])
        all_rows.extend(rows)
        result_text = json.dumps(rows[:30], indent=2) if rows else f"Error: {error}" if error else "No results found."
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": result_text,
        })

    # If Claude didn't use any tools, return its text directly
    if not tool_results:
        return result.text or "ATLAS couldn't figure out how to answer that. Try rephrasing.", [], ""

    # Step 3: Send tool results back for AI to synthesize an answer
    synthesis = await atlas_ai.generate_synthesis(
        messages=[
            {"role": "user", "content": annotated},
            {"role": "assistant", "content": result._raw_content},
        ],
        tool_result_content=tool_results,
        system=system,
        tools=_ORACLE_TOOLS,
        tier=Tier.SONNET,
        max_tokens=800,
    )
    answer = synthesis.text or "ATLAS pulled the data but couldn't formulate a response."

    return answer, all_rows, tool_used


async def _claude_blurb(prompt: str, max_tokens: int = 120) -> str:
    """Short AI-generated text blurb via Claude. Replaces _ai_blurb for non-query uses."""
    try:
        result = await atlas_ai.generate(prompt, tier=Tier.HAIKU, max_tokens=max_tokens)
        return result.text
    except Exception:
        return ""


async def _claude_chat(question: str, system_prompt: str, context: str = "") -> str:
    """General Claude chat for non-TSL modes (Open Intel, Sports Intel, Strategy)."""
    try:
        content = f"{context}\n\n{question}" if context else question
        result = await atlas_ai.generate(content, system=system_prompt, tier=Tier.SONNET, max_tokens=800)
        return result.text or "ATLAS couldn't pull a response on that one."
    except Exception as e:
        return f"ATLAS hit an error: {e}"


# ── Color palette ─────────────────────────────────────────────────────────────
# Domain-specific Oracle colors (not in AtlasColors — semantic to this module)
C_HOT     = discord.Color.from_rgb(255, 87,  51)   # fire orange
C_COLD    = discord.Color.from_rgb(82,  172, 240)   # ice blue
C_NEUTRAL = discord.Color.from_rgb(148, 163, 184)   # slate
C_PURPLE  = discord.Color.from_rgb(139, 92,  246)
C_DARK    = discord.Color.from_rgb(26,  26,  46)
# Mapped from AtlasColors
C_GOLD    = AtlasColors.TSL_GOLD
C_GREEN   = AtlasColors.SUCCESS
C_RED     = AtlasColors.ERROR
C_BLUE    = AtlasColors.INFO

# ─────────────────────────────────────────────────────────────────────────────
#  NFL TEAM IDENTITY  (colors + ESPN logo CDN)
# ─────────────────────────────────────────────────────────────────────────────
_NFL_IDENTITY: dict[str, dict] = {
    "Cardinals":  {"rgb": (151,  35,  63), "abbrev": "ari"},
    "Falcons":    {"rgb": (167,  25,  48), "abbrev": "atl"},
    "Ravens":     {"rgb": ( 26,  25,  95), "abbrev": "bal"},
    "Bills":      {"rgb": (  0,  51, 141), "abbrev": "buf"},
    "Panthers":   {"rgb": (  0, 133, 202), "abbrev": "car"},
    "Bears":      {"rgb": ( 11,  22,  42), "abbrev": "chi"},
    "Bengals":    {"rgb": (251,  79,  20), "abbrev": "cin"},
    "Browns":     {"rgb": ( 49,  29,   0), "abbrev": "cle"},
    "Cowboys":    {"rgb": (  0,  34,  68), "abbrev": "dal"},
    "Broncos":    {"rgb": (251,  79,  20), "abbrev": "den"},
    "Lions":      {"rgb": (  0, 118, 182), "abbrev": "det"},
    "Packers":    {"rgb": ( 24,  48,  40), "abbrev": "gb"},
    "Texans":     {"rgb": (  3,  32,  47), "abbrev": "hou"},
    "Colts":      {"rgb": (  0,  44,  95), "abbrev": "ind"},
    "Jaguars":    {"rgb": (  0, 103, 120), "abbrev": "jax"},
    "Chiefs":     {"rgb": (227,  24,  55), "abbrev": "kc"},
    "Raiders":    {"rgb": (165, 172, 175), "abbrev": "lv"},
    "Chargers":   {"rgb": (  0, 128, 198), "abbrev": "lac"},
    "Rams":       {"rgb": (  0,  53, 148), "abbrev": "lar"},
    "Dolphins":   {"rgb": (  0, 142, 151), "abbrev": "mia"},
    "Vikings":    {"rgb": ( 79,  38, 131), "abbrev": "min"},
    "Patriots":   {"rgb": (  0,  34,  68), "abbrev": "ne"},
    "Saints":     {"rgb": (175, 141,  64), "abbrev": "no"},
    "Giants":     {"rgb": (  1,  35,  82), "abbrev": "nyg"},
    "Jets":       {"rgb": ( 18,  87,  64), "abbrev": "nyj"},
    "Eagles":     {"rgb": (  0,  76,  84), "abbrev": "phi"},
    "Steelers":   {"rgb": ( 16,  24,  32), "abbrev": "pit"},
    "49ers":      {"rgb": (170,   0,   0), "abbrev": "sf"},
    "Seahawks":   {"rgb": (  0,  34,  68), "abbrev": "sea"},
    "Buccaneers": {"rgb": (213,  10,  10), "abbrev": "tb"},
    "Titans":     {"rgb": ( 75, 146, 219), "abbrev": "ten"},
    "Commanders": {"rgb": ( 90,  20,  20), "abbrev": "wsh"},
    "Washington": {"rgb": ( 90,  20,  20), "abbrev": "wsh"},
}

def _team_ident(team_name: str) -> dict:
    for key, val in _NFL_IDENTITY.items():
        if key.lower() in team_name.lower():
            return val
    return {"rgb": (59, 130, 246), "abbrev": None}

def _team_color(team_name: str) -> discord.Color:
    return discord.Color.from_rgb(*_team_ident(team_name)["rgb"])

def _team_logo(team_name: str) -> Optional[str]:
    abbrev = _team_ident(team_name).get("abbrev")
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbrev}.png" if abbrev else None


# ─────────────────────────────────────────────────────────────────────────────
#  TSL ALL-TIME CHAMPIONSHIP DATA
# ─────────────────────────────────────────────────────────────────────────────
_SB_WINNERS: dict[int, str] = {
    1:"PNick",2:"Chok",3:"Hester",4:"Unbeatable",5:"Shelly",6:"Witt",7:"Killa",8:"Witt",
    9:"Epone",10:"Remo",11:"Chok",12:"PNick",13:"Remo",14:"Witt",15:"PNick",16:"Strikernaut",
    17:"Killa",18:"Killa",19:"Bdiddy",20:"Rahj",21:"PNick",22:"Killa",23:"PNick",24:"Pope",
    25:"Killa",26:"Stutts",27:"Jorge",28:"LTH",29:"Killa",30:"Airflight",31:"Jo",32:"Jorge",
    33:"LTH",34:"Killa",35:"RobbyD",36:"JT",37:"Jorge",38:"JT",39:"Ken",40:"JT",
    41:"LTH",42:"Rahj",43:"Rahj",44:"Sharlond",45:"Ruck",46:"Baez",47:"MrCanada",48:"Ken",
    49:"MrCanada",50:"Ken",51:"MrCanada",52:"Ken",53:"Killa",54:"JT",55:"MrCanada",56:"JT",
    57:"Nova",58:"Baez",59:"Baez",60:"John",61:"Ken",62:"John",63:"John",64:"John",65:"JT",
    66:"Jo",67:"JT",68:"JT",69:"Jo",70:"Keem",71:"KG",72:"Jo",73:"Killa",74:"Jo",
    75:"Nova",76:"Nova",77:"Khaled",78:"Eric",79:"JT",80:"Keem",
    81:"Neff",82:"Killa",83:"Nova",84:"Nova",85:"Killa",86:"Jorge",87:"JT",88:"Nova",
    89:"JT",90:"Killa",91:"JT",92:"Chok",93:"JT",94:"JT",95:"Ron",
}
_RINGS_BY_NICK: dict[str, list[int]] = {}
for _sb, _nick in _SB_WINNERS.items():
    _RINGS_BY_NICK.setdefault(_nick, []).append(_sb)

# ── Username → nickname map — pulled live from member registry ────────────────
# Falls back to the hardcoded dict if build_member_db isn't available yet.
try:
    from build_member_db import get_username_to_nick_map
    _USERNAME_TO_NICK: dict[str, str] = get_username_to_nick_map()
except Exception:
    _USERNAME_TO_NICK: dict[str, str] = {
        "TROMBETTATHANYOU":"JT",    "KillaE94":"Killa",       "PLAYERNOVA1":"Nova",
        "PNick12":"PNick",          "KJJ205":"Ken",            "OLIVEIRAYOURFACE":"Jo",
        "MR_C-A-N-A-D-A":"MrCanada","AFFINIZE":"John",        "NUTSONJORGE":"Jorge",
        "TheWitt":"Witt",           "SBAEZ":"Baez",            "Rahjeet":"Rahj",
        "DANGERESQUE_2":"LTH",      "ChokolateThunda":"Chok",  "WithoutRemorse":"Remo",
        "KEEM":"Keem",              "DoceQuatro24":"Pope",     "BDiddy86":"Bdiddy",
        "SHARLOND":"Sharlond",      "RONFK":"Ron",             "Hester2003":"Hester",
        "Unbeatable00":"Unbeatable","ShellyShell":"Shelly",    "Epone":"Epone",
        "MStutts2799":"Stutts",     "AIRFLIGHT_OC":"Airflight","Strikernaut":"Strikernaut",
        "ROBBYD192":"RobbyD",       "RUCKDOESWORK":"Ruck",     "THE_KG_518":"KG",
        "Khaled":"Khaled",          "ERIC":"Eric",             "NEFF":"Neff",
    }

def _to_roman(n: int) -> str:
    vals = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),
            (50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s; n -= v
    return out

def _ring_info(username: str) -> dict:
    """Ring data for a Discord username. Always safe."""
    nick  = _USERNAME_TO_NICK.get(username, "")
    rings = sorted(_RINGS_BY_NICK.get(nick, []))
    count = len(rings)
    last  = rings[-1] if rings else None

    # Consecutive streak detection
    max_streak = streak = 1
    for i in range(1, len(rings)):
        if rings[i] == rings[i - 1] + 1:
            streak += 1; max_streak = max(max_streak, streak)
        else:
            streak = 1

    if   count >= 10: tier = "🐐 GOAT TIER"
    elif count >= 5:  tier = "👑 DYNASTY"
    elif count >= 3:  tier = "💎 ELITE"
    elif count >= 1:  tier = "🏆 CHAMPION"
    else:             tier = "💀 STILL HUNTING"

    return {
        "nick": nick, "count": count, "rings": rings,
        "last": last, "tier": tier, "max_streak": max_streak,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  HISTORICAL SQL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _safe_sql(query: str, params: tuple = ()) -> list[dict]:
    """Execute a SQL query if history DB is available; return [] on any error."""
    if not _HISTORY_OK:
        return []
    rows, err = run_sql(query, params)
    return rows if not err else []

def _franchise_alltime(tn: str) -> dict:
    pat = f"%{tn}%"
    rows = _safe_sql("""
        SELECT
          SUM(CASE WHEN
            (homeTeamName LIKE ? AND CAST(homeScore AS INT)>CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)>CAST(homeScore AS INT))
          THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN
            (homeTeamName LIKE ? AND CAST(homeScore AS INT)<CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)<CAST(homeScore AS INT))
          THEN 1 ELSE 0 END) as losses
        FROM games
        WHERE (homeTeamName LIKE ? OR awayTeamName LIKE ?)
          AND status IN ('2','3') AND stageIndex='1'
    """, (pat, pat, pat, pat, pat, pat))
    r = rows[0] if rows else {}
    return {"wins": int(r.get("wins") or 0), "losses": int(r.get("losses") or 0)}

def _franchise_by_season(tn: str) -> list[dict]:
    pat = f"%{tn}%"
    return _safe_sql("""
        SELECT seasonIndex,
          SUM(CASE WHEN
            (homeTeamName LIKE ? AND CAST(homeScore AS INT)>CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)>CAST(homeScore AS INT))
          THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN
            (homeTeamName LIKE ? AND CAST(homeScore AS INT)<CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)<CAST(homeScore AS INT))
          THEN 1 ELSE 0 END) as losses
        FROM games
        WHERE (homeTeamName LIKE ? OR awayTeamName LIKE ?)
          AND status IN ('2','3') AND stageIndex='1'
        GROUP BY seasonIndex ORDER BY CAST(seasonIndex AS INT)
    """, (pat, pat, pat, pat, pat, pat))

def _franchise_nemesis(tn: str) -> Optional[dict]:
    """Franchise's worst all-time opponent (min 4 games)."""
    pat = f"%{tn}%"
    rows = _safe_sql("""
        SELECT
          CASE WHEN homeTeamName LIKE ? THEN awayTeamName ELSE homeTeamName END as opp,
          SUM(CASE WHEN
            (homeTeamName LIKE ? AND CAST(homeScore AS INT)>CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)>CAST(homeScore AS INT))
          THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN
            (homeTeamName LIKE ? AND CAST(homeScore AS INT)<CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)<CAST(homeScore AS INT))
          THEN 1 ELSE 0 END) as losses,
          COUNT(*) as games
        FROM games
        WHERE (homeTeamName LIKE ? OR awayTeamName LIKE ?)
          AND status IN ('2','3') AND stageIndex='1'
        GROUP BY opp HAVING games >= 4
        ORDER BY CAST(wins AS FLOAT)/CAST(games AS FLOAT) ASC LIMIT 1
    """, (pat, pat, pat, pat, pat, pat, pat))
    return rows[0] if rows else None

def _franchise_punching_bag(tn: str) -> Optional[dict]:
    """Franchise's best all-time record vs any opponent (min 4 games)."""
    pat = f"%{tn}%"
    rows = _safe_sql("""
        SELECT
          CASE WHEN homeTeamName LIKE ? THEN awayTeamName ELSE homeTeamName END as opp,
          SUM(CASE WHEN
            (homeTeamName LIKE ? AND CAST(homeScore AS INT)>CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)>CAST(homeScore AS INT))
          THEN 1 ELSE 0 END) as wins,
          COUNT(*) as games
        FROM games
        WHERE (homeTeamName LIKE ? OR awayTeamName LIKE ?)
          AND status IN ('2','3') AND stageIndex='1'
        GROUP BY opp HAVING games >= 4
        ORDER BY CAST(wins AS FLOAT)/CAST(games AS FLOAT) DESC LIMIT 1
    """, (pat, pat, pat, pat, pat))
    return rows[0] if rows else None

def _franchise_signature_moments(tn: str) -> tuple[Optional[dict], Optional[dict]]:
    """(biggest_win, worst_loss) all-time."""
    pat = f"%{tn}%"
    big_w = _safe_sql("""
        SELECT homeTeamName,awayTeamName,homeScore,awayScore,
               ABS(CAST(homeScore AS INT)-CAST(awayScore AS INT)) as margin,
               seasonIndex,weekIndex
        FROM games
        WHERE (homeTeamName LIKE ? OR awayTeamName LIKE ?)
          AND status IN ('2','3') AND stageIndex='1'
          AND ((homeTeamName LIKE ? AND CAST(homeScore AS INT)>CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)>CAST(homeScore AS INT)))
        ORDER BY margin DESC LIMIT 1
    """, (pat, pat, pat, pat))
    worst_l = _safe_sql("""
        SELECT homeTeamName,awayTeamName,homeScore,awayScore,
               ABS(CAST(homeScore AS INT)-CAST(awayScore AS INT)) as margin,
               seasonIndex,weekIndex
        FROM games
        WHERE (homeTeamName LIKE ? OR awayTeamName LIKE ?)
          AND status IN ('2','3') AND stageIndex='1'
          AND ((homeTeamName LIKE ? AND CAST(homeScore AS INT)<CAST(awayScore AS INT))
            OR (awayTeamName LIKE ? AND CAST(awayScore AS INT)<CAST(homeScore AS INT)))
        ORDER BY margin DESC LIMIT 1
    """, (pat, pat, pat, pat))
    return (big_w[0] if big_w else None), (worst_l[0] if worst_l else None)


# ─────────────────────────────────────────────────────────────────────────────
#  DNA + COMPOSITE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _trend_sparkline(win_log: list[bool]) -> str:
    """Convert list of True/False results into a rising/falling sparkline."""
    blocks = "▁▂▃▄▅▆▇█"
    if not win_log:
        return "▁▁▁▁▁"
    momentum, out = 3, []
    for won in win_log:
        momentum = min(7, momentum + 2) if won else max(0, momentum - 2)
        out.append(blocks[momentum])
    return "".join(out)

def _playstyle_tags(st_row, ts_row, clutch_winpct: float) -> list[str]:
    tags = []
    try:
        p = float(st_row.get("offPassYds", 0))
        r = float(st_row.get("offRushYds", 0))
        ratio = p / (p + r) if (p + r) > 0 else 0.5
        if   ratio > 0.63: tags.append("#AirRaid")
        elif ratio < 0.44: tags.append("#RunFirst")
        else:              tags.append("#Balanced")
    except Exception:
        pass
    if   clutch_winpct >= 0.65: tags.append("#ClutchKing")
    elif clutch_winpct <= 0.33: tags.append("#TiltMachine")
    try:
        tod = int(st_row.get("tODiff", 0))
        if   tod >= 6:  tags.append("#TurnoverKing")
        elif tod <= -4: tags.append("#TurnoverProne")
    except Exception:
        pass
    try:
        dr = int(st_row.get("defTotalYdsRank", 16))
        if   dr <= 5:  tags.append("#IronCurtain")
        elif dr >= 28: tags.append("#Sieve")
    except Exception:
        pass
    if ts_row is not None:
        try:
            pen = int(ts_row.get("penalties", 0))
            if   pen >= 80: tags.append("#PenaltyMachine")
            elif pen <= 35: tags.append("#Disciplined")
        except Exception:
            pass
    try:
        net = int(st_row.get("netPts", 0))
        gms = int(st_row.get("totalWins", 0)) + int(st_row.get("totalLosses", 0))
        if gms > 0:
            npg = net / gms
            if   npg >= 10: tags.append("#Dominant")
            elif npg <= -10: tags.append("#Struggling")
    except Exception:
        pass
    return tags[:4]


# ── Player stat menu definitions ──────────────────────────────────────────────
#   Format: { "POSITION_GROUP": [ (db_column, display_label), ... ] }
PLAYER_STAT_MAP: dict[str, list[tuple[str, str]]] = {
    "QB": [
        ("passYds",      "Pass Yards"),
        ("passTDs",      "Pass TDs"),
        ("passCompPct",  "Comp %"),
        ("passerRating", "Passer Rating"),
        ("passInts",     "INTs"),
        ("rushYds",      "Rush Yards"),
    ],
    "RB": [
        ("rushYds",            "Rush Yards"),
        ("rushTDs",            "Rush TDs"),
        ("rushYdsPerAtt",      "Yards/Carry"),
        ("rushBrokenTackles",  "Broken Tackles"),
        ("recYds",             "Rec Yards"),
        ("recTDs",             "Rec TDs"),
    ],
    "WR": [
        ("recYds",        "Rec Yards"),
        ("recTDs",        "Rec TDs"),
        ("recCatches",    "Receptions"),
        ("recYdsPerCatch","Yards/Catch"),
        ("recDrops",      "Drops"),
    ],
    "TE": [
        ("recYds",        "Rec Yards"),
        ("recTDs",        "Rec TDs"),
        ("recCatches",    "Receptions"),
        ("recYdsPerCatch","Yards/Catch"),
    ],
    "DL": [
        ("defSacks",      "Sacks"),
        ("defTotalTackles","Tackles"),
        ("defForcedFum",  "Forced Fumbles"),
        ("defFumRec",     "Fumble Rec"),
    ],
    "LB": [
        ("defTotalTackles","Tackles"),
        ("defSacks",       "Sacks"),
        ("defInts",        "INTs"),
        ("defForcedFum",   "Forced Fumbles"),
        ("defDeflections", "Pass Deflections"),
    ],
    "DB": [
        ("defInts",        "INTs"),
        ("defTotalTackles","Tackles"),
        ("defDeflections", "Pass Deflections"),
        ("defSacks",       "Sacks"),
        ("defForcedFum",   "Forced Fumbles"),
    ],
}

# DB positions that map to each group
_POS_GROUP_MAP: dict[str, list[str]] = {
    "QB": ["QB"],
    "RB": ["HB", "FB"],
    "WR": ["WR"],
    "TE": ["TE"],
    "DL": ["DT", "LE", "RE"],
    "LB": ["LOLB", "MLB", "ROLB"],
    "DB": ["CB", "FS", "SS"],
}


# ─────────────────────────────────────────────────────────────────────────────
#  UNIVERSAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _season_label() -> str:
    return f"Season {dm.CURRENT_SEASON} | Week {dm.CURRENT_WEEK}"


def _rank_emoji(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"`#{rank}`")


def _winpct_bar(wins: int, losses: int, width: int = 10) -> str:
    total = wins + losses
    if total == 0:
        return "░" * width
    filled = round((wins / total) * width)
    return "█" * filled + "░" * (width - filled)


def _trend_bar(delta_pct: float, width: int = 8) -> str:
    filled = min(abs(int(delta_pct / 10)), width // 2)
    if delta_pct > 0:
        return "░" * (width // 2) + "█" * filled + "░" * (width // 2 - filled)
    return "░" * (width // 2 - filled) + "█" * filled + "░" * (width // 2)


def _record_str(wins: int, losses: int, ties: int = 0) -> str:
    return f"{wins}-{losses}-{ties}" if ties else f"{wins}-{losses}"


def _dev_emoji(dev: str) -> str:
    return {
        "Superstar X-Factor": "⚡",
        "Superstar":          "🌟",
        "Star":               "⭐",
        "Normal":             "◦",
    }.get(dev or "", "◦")


def _grade_color(grade: str) -> discord.Color:
    if grade.startswith("A"): return C_GREEN
    if grade.startswith("B"): return C_BLUE
    if grade.startswith("C"): return C_NEUTRAL
    return C_RED


def _resolve_owner_team(
    interaction: discord.Interaction,
    override: Optional[discord.Member] = None,
) -> tuple[str, str]:
    """
    Universal auto-detect resolver.
    Returns (discord_username, team_name) for the interaction caller or override member.
    Checks roster first (single source of truth), falls back to intelligence profile.
    """
    target = override or interaction.user
    # Primary: roster module (owner assignments from tsl_members)
    try:
        import roster
        team = roster.get_team_name(target.id)
        if team:
            return target.name, team
    except Exception:
        pass
    # Fallback: intelligence profile (API username fuzzy match)
    profile = ig.get_or_create_profile(target.id, target.name)
    return target.name, profile.get("team") or ""


async def _ai_blurb(prompt: str, max_tokens: int = 120) -> str:
    try:
        result = await atlas_ai.generate(prompt, tier=Tier.HAIKU, max_tokens=max_tokens, temperature=0.8)
        return result.text
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  EMBED BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_hub_embed() -> discord.Embed:
    """Live watercooler landing embed — refreshes every time /stats hub is called."""
    embed = discord.Embed(
        title=f"📊 TSL Stats Hub — {_season_label()}",
        description=(
            "Your league. Your data. One place.\n"
            "Drill-downs are **private to you** — no channel flood.\n\u200b"
        ),
        color=C_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    # ── Watercooler snapshots ─────────────────────────────────────────────
    try:
        rankings = an.power_rankings()
        if rankings:
            ldr = rankings[0]
            embed.add_field(
                name="🏆 Power Leader",
                value=f"**{ldr['team']}** ({ldr['record']}) — Score: {ldr['score']}",
                inline=True,
            )
    except Exception:
        pass

    try:
        clutch = ig.get_clutch_records(margin=7)
        most   = clutch.get("most_clutch",  "?")
        least  = clutch.get("least_clutch", "?")
        embed.add_field(
            name="⚡ Clutch King / Choke",
            value=f"👑 **{most}**  ·  💀 {least}",
            inline=True,
        )
    except Exception:
        pass

    try:
        if not dm.df_standings.empty:
            worst_name, worst_val = "", 999
            for _, row in dm.df_standings.iterrows():
                try:
                    v = int(str(row.get("winLossStreak", "0")))
                    if v < worst_val:
                        worst_val  = v
                        worst_name = f"{row.get('teamName','?')} ({v})"
                except Exception:
                    pass
            if worst_name:
                embed.add_field(name="💀 On the Skids", value=f"**{worst_name}**", inline=True)
    except Exception:
        pass

    embed.add_field(
        name="Navigation",
        value=(
            "```\n"
            "🔥 Hot/Cold    ⚡ Clutch    📊 Power      🏆 Standings\n"
            "👤 My Profile  🆚 H-t-H     📅 Recap      📜 Draft\n"
            "🎯 Players     🏈 Team      🏛️ All-Time   🔬 Ask ATLAS\n"
            "```\n"
            "*Ask ATLAS: 📊 TSL League data · 🌐 Open Intel (general AI)*"
        ),
        inline=False,
    )
    embed.set_author(name="ATLAS™ Oracle Module", icon_url=ATLAS_ICON_URL)
    embed.set_footer(text="ATLAS™ Oracle · All drill-downs private to you", icon_url=ATLAS_ICON_URL)
    return embed


# ── Hot / Cold ────────────────────────────────────────────────────────────────

def _build_hotcold_league(top_n: int = 5) -> tuple[discord.Embed, list[str]]:
    """Returns (embed, list_of_player_names) for the drill-down select menu."""
    embed = discord.Embed(
        title="🔥🥶 TSL Hot / Cold Report",
        description=f"League-wide performance trends — {_season_label()}",
        color=C_HOT,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    hot_players, cold_players = [], []
    target_players: list[dict] = []

    for df, col, min_col, min_val in [
        (dm.df_offense, "passYds",  "passAtt",    50),
        (dm.df_offense, "rushYds",  "rushAtt",    20),
        (dm.df_offense, "recYds",   "recCatches", 10),
        (dm.df_defense, "defSacks", None,          0),
        (dm.df_defense, "defInts",  None,          0),
    ]:
        leaders = an.stat_leaders(df, col, top_n=top_n, min_col=min_col, min_val=min_val)
        for r in leaders:
            if r["name"] not in [p["name"] for p in target_players]:
                target_players.append(r)

    for pi in target_players[:20]:
        result = ig.get_hot_cold(pi["name"], last_n=3)
        if "error" in result:
            continue
        score = result.get("trend_score", 0)
        entry = {
            "name":  result.get("name",  pi["name"]),
            "team":  result.get("team",  pi.get("team", "")),
            "pos":   result.get("pos",   pi.get("pos",  "")),
            "score": score,
        }
        if score > 15:
            hot_players.append(entry)
        elif score < -15:
            cold_players.append(entry)

    hot_players.sort(key=lambda x: x["score"], reverse=True)
    cold_players.sort(key=lambda x: x["score"])

    if hot_players:
        lines = [f"🔥 **{p['name']}** ({p['pos']}, {p['team']}) — score: +{p['score']:.0f}" for p in hot_players[:5]]
        embed.add_field(name="🔥 On Fire", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🔥 On Fire", value="_No standout performers this week_", inline=False)

    if cold_players:
        lines = [f"🥶 **{p['name']}** ({p['pos']}, {p['team']}) — score: {p['score']:.0f}" for p in cold_players[:5]]
        embed.add_field(name="🥶 Ice Cold", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🥶 Ice Cold", value="_Everyone holding steady_", inline=False)

    embed.set_footer(text="Select a player below for their Hot/Cold breakdown · ATLAS™ Oracle", icon_url=ATLAS_ICON_URL)

    all_names = [p["name"] for p in (hot_players[:5] + cold_players[:5])]
    return embed, all_names


def _build_hotcold_single(data: dict) -> discord.Embed:
    trend = data.get("trend", "➡️ NEUTRAL")
    name  = data.get("name", "Unknown")
    team  = data.get("team", "")
    pos   = data.get("pos",  "")
    n     = data.get("last_n", 3)

    if "HOT"  in trend: color, icon = C_HOT,  "🔥"
    elif "COLD" in trend: color, icon = C_COLD, "🥶"
    else:                 color, icon = C_NEUTRAL, "➡️"

    embed = discord.Embed(
        title=f"{icon} {name} — {trend}",
        description=f"**{pos}** · {team} · Last {n} vs Season Avg",
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    STAT_LABELS = {
        "passYds": "Pass Yds", "passTDs": "Pass TDs", "passInts": "INTs",
        "passCompPct": "Comp %", "rushYds": "Rush Yds", "rushTDs": "Rush TDs",
        "recYds": "Rec Yds", "recTDs": "Rec TDs", "recCatches": "Catches",
        "defTotalTackles": "Tackles", "defSacks": "Sacks",
        "defInts": "DEF INTs", "defForcedFum": "FF",
    }
    NEG_STATS = {"passInts", "rushFum", "recDrops"}

    season_avg = data.get("season_avg", {})
    last_n_avg = data.get("last_n_avg", {})
    deltas     = data.get("deltas",     {})
    rows       = []

    for col, label in STAT_LABELS.items():
        if col not in season_avg:
            continue
        s_val = season_avg[col]
        l_val = last_n_avg.get(col, s_val)
        d     = deltas.get(col, 0)
        if s_val == 0 and l_val == 0:
            continue
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
        is_neg = col in NEG_STATS
        if   (d > 0 and not is_neg) or (d < 0 and is_neg):
            arrow = f"🟢 {arrow}"
        elif (d < 0 and not is_neg) or (d > 0 and is_neg):
            arrow = f"🔴 {arrow}"
        bar = _trend_bar(d if not is_neg else -d)
        rows.append(
            f"`{label:<12}` Avg: **{s_val}** → L{n}: **{l_val}** {arrow} ({d:+.0f}%)\n`{bar}`"
        )

    if rows:
        embed.add_field(name="📊 Stat Trends", value="\n".join(rows[:5]), inline=False)

    games = data.get("last_n_games", [])
    if games:
        game_lines = []
        for g in games:
            wk    = g.get("weekIndex", "?")
            parts = [f"{lbl}: {g[col]}" for col, lbl in STAT_LABELS.items() if col in g and g[col] != 0]
            game_lines.append(f"**Wk {wk}** — " + " | ".join(parts[:4]))
        embed.add_field(name=f"🗂️ Last {n} Games", value="\n".join(game_lines), inline=False)

    embed.set_footer(text=f"ATLAS™ Oracle · {_season_label()}", icon_url=ATLAS_ICON_URL)
    return embed


# ── Clutch ────────────────────────────────────────────────────────────────────

def _build_clutch_embed(margin: int = 7, highlight_team: str = "") -> discord.Embed:
    data = ig.get_clutch_records(margin=margin)

    if "error" in data:
        return discord.Embed(
            title="⚡ Clutch Rankings",
            description=f"❌ {data['error']}",
            color=C_RED,
        )

    records = data.get("records", [])
    most    = data.get("most_clutch",  "?")
    least   = data.get("least_clutch", "?")

    desc = f"👑 **Most Clutch:** {most}\n💀 **Least Clutch:** {least}\n\n*{_season_label()}*"
    if highlight_team:
        desc += f"\n\n*Your team: **{highlight_team}***"

    embed = discord.Embed(
        title=f"⚡ TSL Clutch Rankings — Games Decided by ≤{margin} Points",
        description=desc,
        color=C_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    ranked = sorted(
        [r for r in records if r.get("clutch_games", 0) > 0],
        key=lambda r: (r["clutch_winpct"], r["clutch_wins"]),
        reverse=True,
    )

    top_lines, bot_lines = [], []
    for i, r in enumerate(ranked, 1):
        cw, cl, pct = r["clutch_wins"], r["clutch_losses"], r["clutch_winpct"]
        ow, ol      = r["overall_wins"],  r["overall_losses"]
        bar  = _winpct_bar(cw, cl, width=6)
        team = r["team"][:12]
        mark = " ◄" if highlight_team and highlight_team.lower() in r["team"].lower() else ""
        line = f"{_rank_emoji(i)} **{team}**{mark}\n`{bar}` {cw}-{cl} ({pct:.0%})\nOverall: {ow}-{ol}"
        if   i <= 5:              top_lines.append(line)
        elif i >= len(ranked) - 4: bot_lines.append(line)

    if top_lines: embed.add_field(name="💎 Most Clutch",   value="\n\n".join(top_lines), inline=True)
    if bot_lines: embed.add_field(name="💸 Choke Artists", value="\n\n".join(bot_lines), inline=True)

    no_close = [r for r in records if r.get("clutch_games", 0) == 0]
    if no_close:
        embed.add_field(
            name="🛡️ Living Comfortably (No Close Games)",
            value=", ".join(r["team"] for r in no_close[:6]),
            inline=False,
        )

    divergent = []
    for r in records:
        if r.get("clutch_games", 0) < 2:
            continue
        ov_total = r["overall_wins"] + r["overall_losses"]
        if ov_total == 0:
            continue
        ov_pct = r["overall_wins"] / ov_total
        divergent.append((r["team"], r["clutch_winpct"] - ov_pct, r["clutch_winpct"], ov_pct))

    divergent.sort(key=lambda x: abs(x[1]), reverse=True)
    if divergent:
        lines = [
            f"**{t}** — Overall: {op:.0%} → Clutch: {cp:.0%} {'▲' if d > 0 else '▼'} {abs(d):.0%}"
            for t, d, cp, op in divergent[:4]
        ]
        embed.add_field(name="📈 Biggest Clutch Swing", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Clutch = games decided by ≤{margin} pts · ◄ = Your team · ATLAS™ Oracle", icon_url=ATLAS_ICON_URL)
    return embed


# ── Power Rankings ────────────────────────────────────────────────────────────

def _build_power_embed() -> discord.Embed:
    rankings = an.power_rankings()
    if not rankings:
        return discord.Embed(title="📊 Power Rankings", description="No data available.", color=C_NEUTRAL)

    embed = discord.Embed(
        title=f"📊 TSL Power Rankings — {_season_label()}",
        color=C_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    top10, rest = rankings[:10], rankings[10:]
    lines = []
    for r in top10:
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r["rank"], f"**#{r['rank']}**")
        lines.append(
            f"{medal} **{r['team']}** ({r['owner']}) — {r['record']}\n"
            f"    Score: **{r['score']}** | Net: {r['net_pts']:+d} | TO: {r['to_diff']:+d}"
        )
    embed.add_field(name="🏆 Top 10", value="\n".join(lines), inline=False)

    if rest:
        lines2 = [f"**#{r['rank']}** {r['team']} — {r['record']} (score: {r['score']})" for r in rest]
        embed.add_field(name="📋 11–32", value="\n".join(lines2), inline=False)

    embed.set_footer(text="Score = Win% × 40 + Net Pts × 30 + TO Diff × 15 + Off/Def Rank × 15", icon_url=ATLAS_ICON_URL)
    return embed


# ── Standings ─────────────────────────────────────────────────────────────────

def _build_standings_embed(caller_team: str = "") -> discord.Embed:
    if dm.df_standings.empty:
        return discord.Embed(
            title="🏆 TSL Standings",
            description="No standings data available.",
            color=C_NEUTRAL,
        )

    embed = discord.Embed(
        title=f"🏆 TSL Standings — {_season_label()}",
        color=C_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    divs: dict[str, list] = {}
    for _, row in dm.df_standings.iterrows():
        div = str(row.get("divisionName", "Unknown"))
        divs.setdefault(div, []).append(row)

    for div_name, teams in sorted(divs.items()):
        sorted_teams = sorted(teams, key=lambda r: int(r.get("totalWins", 0)), reverse=True)
        lines = []
        for r in sorted_teams:
            team   = str(r.get("teamName", "?"))
            w      = int(r.get("totalWins",   0))
            l      = int(r.get("totalLosses", 0))
            t      = int(r.get("totalTies",   0))
            pf     = int(r.get("ptsFor",      0))
            pa     = int(r.get("ptsAgainst",  0))
            streak = str(r.get("winLossStreak", ""))
            arrow  = " ◄" if caller_team and caller_team.lower() in team.lower() else ""
            lines.append(f"**{team}**{arrow}  {_record_str(w, l, t)}  PF:{pf} PA:{pa}  {streak}")
        embed.add_field(name=f"📍 {div_name}", value="\n".join(lines), inline=False)

    footer = "◄ = Your team · ATLAS™ Oracle"
    if not caller_team:
        footer = "ATLAS™ Oracle · Run /stats hub to see your team highlighted"
    embed.set_footer(text=footer, icon_url=ATLAS_ICON_URL)
    return embed


# ── Owner Profile ─────────────────────────────────────────────────────────────

async def _build_owner_embed(target: discord.Member, guild: discord.Guild) -> discord.Embed:
    profile      = ig.get_or_create_profile(target.id, target.name)
    nickname     = profile.get("nickname") or target.display_name
    team         = profile.get("team") or ""
    display_name = target.display_name

    embed = discord.Embed(
        title=f"👤 {nickname}",
        color=C_PURPLE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    id_lines = [f"**Discord:** {target.mention}"]
    if nickname != display_name:
        id_lines.append(f"**TSL Name:** {nickname}")
    embed.add_field(name="🪪 Identity", value="\n".join(id_lines), inline=True)

    if not team:
        embed.add_field(name="🏟️ Team", value="_Not in league / team unknown_", inline=True)
        embed.set_footer(text="ATLAS™ Oracle · Owner Profile", icon_url=ATLAS_ICON_URL)
        return embed

    embed.add_field(name="🏟️ Team", value=f"**{team}**", inline=True)
    record = dm.get_team_record(team)
    embed.add_field(name="📋 Record", value=f"**{record}**", inline=True)

    if not dm.df_standings.empty:
        row = dm.df_standings[dm.df_standings["teamName"].str.lower().str.contains(team.lower(), na=False)]
        if not row.empty:
            r = row.iloc[0]
            w, l = int(r.get("totalWins", 0)), int(r.get("totalLosses", 0))
            embed.add_field(
                name="📊 Team Stats",
                value=(
                    f"Rank: **#{int(r.get('rank', 0))}**\n"
                    f"`{_winpct_bar(w, l, width=10)}` {w}-{l}\n"
                    f"Net Pts: **{int(r.get('netPts', 0)):+d}** | TO Diff: **{int(r.get('tODiff', 0)):+d}**"
                ),
                inline=True,
            )
            embed.add_field(
                name="⚡ Off / Def",
                value=(
                    f"Off Yds: **{r.get('offTotalYds','?')}** (#{int(r.get('offTotalYdsRank', 0))})\n"
                    f"Def Yds: **{r.get('defTotalYds','?')}** (#{int(r.get('defTotalYdsRank', 0))})"
                ),
                inline=True,
            )

    recent = dm.get_last_n_games(team, 5)
    if recent:
        form_parts = []
        for g in recent:
            is_home     = str(g.get("home", "")).lower().find(team.lower()) != -1
            my_score    = g.get("home_score") if is_home else g.get("away_score")
            their_score = g.get("away_score") if is_home else g.get("home_score")
            opp         = g.get("away")       if is_home else g.get("home")
            won         = (my_score or 0) > (their_score or 0)
            form_parts.append(
                f"{'🟢' if won else '🔴'} {'W' if won else 'L'} vs {opp} ({my_score}-{their_score})"
            )
        embed.add_field(name="📅 Last 5 Games", value="\n".join(form_parts), inline=False)

    beefs = profile.get("beefs", [])
    if beefs:
        beef_lines = []
        for b in sorted(beefs, key=lambda x: x["count"], reverse=True)[:4]:
            opp_member = guild.get_member(b["opponent_id"])
            opp_nick   = (
                ig.get_nickname(b["opponent_id"])
                or (opp_member.display_name if opp_member else str(b["opponent_id"]))
            )
            try:
                import roster as _r
                opp_team = _r.get_team_name(b["opponent_id"]) or ""
            except Exception:
                opp_team = ig.KNOWN_MEMBER_TEAMS.get(b["opponent_id"], "")
            h2h_str   = ""
            if team and opp_team:
                h2h = dm.get_h2h_record(team, opp_team)
                h2h_str = f" | H2H: {h2h.get('a_wins', 0)}-{h2h.get('b_wins', 0)}"
            beef_lines.append(f"🥊 **{opp_nick}** ({opp_team or '?'}) — {b['count']} beef(s){h2h_str}")
        embed.add_field(name="🥩 Beef Mode History", value="\n".join(beef_lines), inline=False)

    clutch_data = ig.get_clutch_records(margin=7)
    if "records" in clutch_data:
        tc = next(
            (r for r in clutch_data["records"] if r.get("team", "").lower() == team.lower()),
            None,
        )
        if tc and tc.get("clutch_games", 0) > 0:
            cw, cl, cpct = tc["clutch_wins"], tc["clutch_losses"], tc["clutch_winpct"]
            embed.add_field(
                name="⚡ Clutch Record (≤7pt games)",
                value=f"`{_winpct_bar(cw, cl, width=8)}` **{cw}-{cl}** ({cpct:.0%})",
                inline=True,
            )

    embed.add_field(
        name="🔬 ATLAS Oracle",
        value=(
            f"Total queries: **{profile.get('interactions', 0)}**\n"
            f"Times roasted: **{profile.get('roast_count', 0)}**"
        ),
        inline=True,
    )

    # AI tendency blurb
    if (atlas_ai._get_claude() or atlas_ai._get_gemini()) and not dm.df_team_stats.empty:
        try:
            ts_row = dm.df_team_stats[
                dm.df_team_stats["teamName"].str.lower().str.contains(team.lower(), na=False)
            ]
            if not ts_row.empty:
                r = ts_row.iloc[0]
                pass_ratio = float(r.get("offPassYds", 0)) / max(
                    float(r.get("offPassYds", 0)) + float(r.get("offRushYds", 0)), 1
                )
                stats_snap = (
                    f"passRatio={pass_ratio:.2f}, "
                    f"penalties={r.get('penalties','?')}, "
                    f"tOGiveAways={r.get('tOGiveAways','?')}, "
                    f"off3rdDownConvPct={r.get('off3rdDownConvPct','?')}"
                )
                prompt = (
                    f"{get_persona('analytical')}\n\nIn ONE sharp sentence, "
                    f"describe {nickname}'s ({team}) playstyle. Be brutal and specific. "
                    f"Stats: {stats_snap}"
                )
                blurb = await _ai_blurb(prompt, max_tokens=80)
                if blurb:
                    embed.add_field(
                        name="🔬 ATLAS Oracle · Tendency",
                        value=f"*{blurb}*",
                        inline=False,
                    )
        except Exception:
            pass

    embed.set_footer(text=f"ATLAS™ Oracle · Owner Profile · {_season_label()}", icon_url=ATLAS_ICON_URL)
    return embed


# ── Weekly Recap ──────────────────────────────────────────────────────────────

def _build_recap_embed(week: int | None = None) -> discord.Embed:
    data = an.weekly_recap(week=week)
    if not data.get("games"):
        return discord.Embed(
            title=f"📅 Week {week or dm.CURRENT_WEEK} Recap",
            description="No completed games found for this week.",
            color=C_NEUTRAL,
        )

    actual_week = data["week"]
    games       = data["games"]
    highlights  = data.get("highlights", {})

    embed = discord.Embed(
        title=f"📅 TSL Week {actual_week} Recap",
        description=f"*{len(games)} games played · {_season_label()}*",
        color=C_BLUE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    score_lines = []
    for g in sorted(games, key=lambda x: abs(x.get("home_score", 0) - x.get("away_score", 0)), reverse=True):
        hs, aws        = g.get("home_score", 0), g.get("away_score", 0)
        home, away     = g.get("home", "?"),     g.get("away", "?")
        diff           = abs(hs - aws)
        tag            = "⚡" if diff <= 7 else ("💥" if diff >= 21 else "  ")
        if hs > aws:
            score_lines.append(f"{tag} **{home}** {hs} – {aws} {away}")
        else:
            score_lines.append(f"{tag} {home} {hs} – {aws} **{away}**")
    embed.add_field(name="🏈 Scores", value="\n".join(score_lines), inline=False)

    if highlights.get("biggest_win"):
        embed.add_field(name="💥 Biggest Win",  value=highlights["biggest_win"],  inline=True)
    if highlights.get("closest_game"):
        embed.add_field(name="⚡ Closest Game", value=highlights["closest_game"], inline=True)

    embed.set_footer(
        text="⚡ = Close game (≤7pts)  💥 = Blowout (21+ pts) · ATLAS™ Oracle",
        icon_url=ATLAS_ICON_URL
    )
    return embed


# ── Draft History ─────────────────────────────────────────────────────────────

async def _build_draft_overview_embed() -> discord.Embed:
    data    = await ig.compare_draft_classes()
    classes = data.get("classes", [])

    embed = discord.Embed(
        title="📜 TSL Draft Class History — All Seasons",
        color=C_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    if not classes:
        embed.description = "No draft class data available."
        return embed

    lines = []
    for c in sorted(classes, key=lambda x: x["season"]):
        lines.append(
            f"**S{c['season']}** — **{c['grade']}** | "
            f"Avg OVR {c['avg_ovr']} | {c['total_picks']} picks | "
            f"⚡{c['xfactors']} 🌟{c['superstars']} ⭐{c['stars']}"
        )
    embed.description = "\n".join(lines)

    try:
        best  = max(classes, key=lambda x: x.get("grade_score", 0))
        worst = min(classes, key=lambda x: x.get("grade_score", 999))
        embed.add_field(name="🏆 Best Class",    value=f"Season {best['season']} — **{best['grade']}**",  inline=True)
        embed.add_field(name="💀 Weakest Class", value=f"Season {worst['season']} — **{worst['grade']}**", inline=True)
    except Exception:
        pass

    embed.set_footer(text="Select a season below for a full breakdown · ATLAS™ Oracle", icon_url=ATLAS_ICON_URL)
    return embed


async def _build_draft_season_embeds(season: int) -> list[discord.Embed]:
    data = await ig.get_draft_class(season)

    if "error" in data:
        return [discord.Embed(
            title=f"📜 Draft Class — Season {season}",
            description=f"❌ {data['error']}",
            color=C_RED,
        )]

    grade, score_val = data["letter_grade"], data["grade_score"]
    dev_counts  = data.get("dev_counts", {})
    avg_ovr     = data.get("avg_ovr", 0)
    total_picks = data.get("total_picks", 0)
    color       = _grade_color(grade)

    e1 = discord.Embed(
        title=f"📜 Season {season} Draft Class — Grade: **{grade}**",
        description=(
            f"**{total_picks} picks** | Avg OVR: **{avg_ovr}** | Score: **{score_val:.2f}**\n"
            f"*{_season_label()}*"
        ),
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    xf = dev_counts.get("Superstar X-Factor", 0)
    ss = dev_counts.get("Superstar", 0)
    st = dev_counts.get("Star", 0)
    nm = dev_counts.get("Normal", 0)
    e1.add_field(
        name="⚡ Dev Trait Breakdown",
        value=f"⚡ X-Factor: **{xf}**\n🌟 Superstar: **{ss}**\n⭐ Star: **{st}**\n◦ Normal: **{nm}**",
        inline=True,
    )

    grade_context = {
        "A+": "Historic class. Franchise-altering picks.",
        "A":  "Elite class. Multiple future cornerstones.",
        "A-": "Very strong. Several high-upside picks.",
        "B+": "Solid class with notable hits.",
        "B":  "Average-to-good. Some contributors.",
        "B-": "Below average. Thin on upside.",
        "C+": "Mostly depth. A steal or two buried.",
        "C":  "Forgettable. Few contributors.",
        "C-": "Rough class. Limited long-term impact.",
        "D":  "Disaster. Picks that never panned out.",
    }
    e1.add_field(name="📝 Verdict", value=grade_context.get(grade, "Class grade computed."), inline=True)

    top = data.get("top_picks", [])
    if top:
        lines = [
            f"{_dev_emoji(p.get('dev','Normal'))} **{p.get('extendedName', 'Unknown')}** "
            f"({p.get('pos','')}, OVR {p.get('playerBestOvr',0)}) — "
            f"{p.get('roundLabel','?')} Pick {p.get('draftPick','?')} · {p.get('teamName','')}"
            for p in top[:6]
        ]
        e1.add_field(name="🏆 Top Picks (by Current OVR)", value="\n".join(lines), inline=False)

    steals = data.get("steals", [])
    if steals:
        lines = [
            f"{_dev_emoji(p.get('dev','Normal'))} **{p.get('extendedName', 'Unknown')}** "
            f"({p.get('pos','')}, OVR {p.get('playerBestOvr',0)}) — "
            f"{p.get('roundLabel','?')} Pick {p.get('draftPick','?')}"
            for p in steals[:4]
        ]
        e1.add_field(name="💎 Steals", value="\n".join(lines), inline=True)

    busts = data.get("busts", [])
    if busts:
        lines = [
            f"💀 **{p.get('extendedName', 'Unknown')}** "
            f"({p.get('pos','')}, OVR {p.get('playerBestOvr',0)}) — "
            f"{p.get('roundLabel','?')} Pick {p.get('draftPick','?')}"
            for p in busts[:4]
        ]
        e1.add_field(name="💀 Busts", value="\n".join(lines), inline=True)

    e1.set_footer(text=f"Draft Class Analysis · Season {season} · ATLAS™ Oracle")
    return [e1]


# Aliases referenced elsewhere in the file
_build_draft_comparison_embed = _build_draft_overview_embed
_build_draft_embed = _build_draft_season_embeds


# ── Draft Class Drilldown View ────────────────────────────────────────────────

def _build_team_draft_embed(team_abbr: str, team_nick: str, season: int) -> discord.Embed:
    """Build rich embed showing a team's draft class with per-player fields."""
    data = ig.get_team_draft_class(team_abbr, season)

    if "error" in data:
        return discord.Embed(
            title=f"📜 {team_nick} Draft Class — Season {season}",
            description=f"❌ {data['error']}",
            color=C_RED,
        )

    players = data.get("players", [])
    grade = data.get("team_grade", "N/A")
    grade_score = data.get("team_grade_score", 0)
    avg_ovr = data.get("avg_ovr", 0)
    total = data.get("total_picks", 0)
    color = _grade_color(grade) if grade != "N/A" else C_NEUTRAL

    embed = discord.Embed(
        title=f"📜 {team_nick} Draft Class — Season {season}",
        description=(
            f"**Grade: {grade}** ({grade_score:.2f}) | "
            f"**{total} picks** | Avg OVR: **{avg_ovr}**"
        ) if players else "No draft picks this season.",
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    for p in players:
        dev_e = _dev_emoji(p["dev"])
        traded_tag = ""
        if p["was_traded"]:
            traded_tag = f"\n🔄 Now on **{p['current_team']}**"

        embed.add_field(
            name=f"{dev_e} {p['extendedName']} — {p['pos']} — {p['playerBestOvr']} OVR",
            value=(
                f"{p['roundLabel']} Pick {p['draftPick']} • "
                f"Grade: **{p['grade']}** • {p['dev']}"
                f"{traded_tag}"
            ),
            inline=False,
        )

    embed.set_footer(
        text=f"Draft Class · {team_nick} · Season {season} · ATLAS™ Oracle",
        icon_url=ATLAS_ICON_URL,
    )
    return embed


def _build_trade_card_embed(player_name: str) -> discord.Embed:
    """Build a trade card embed for a traded draft pick."""
    trades = dm.find_trades_by_player(player_name)

    if not trades:
        return discord.Embed(
            title=f"🔄 Trade Details — {player_name}",
            description="Trade details unavailable. The trade may have occurred in a prior season.",
            color=C_NEUTRAL,
        )

    # Show most recent trade
    t = trades[0]
    embed = discord.Embed(
        title=f"🔄 Trade Breakdown — {player_name}",
        color=C_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name=f"📦 {t['team1Name']} sent:",
        value=t["team1Sent"].strip() or "Unknown",
        inline=False,
    )
    embed.add_field(
        name=f"📦 {t['team2Name']} sent:",
        value=t["team2Sent"].strip() or "Unknown",
        inline=False,
    )
    s = t.get("seasonIndex", "?")
    w = t.get("weekIndex", "?")
    try:
        w = int(float(w)) + 1
    except (ValueError, TypeError):
        pass
    embed.set_footer(text=f"Season {s} · Week {w} · ATLAS™ Oracle", icon_url=ATLAS_ICON_URL)
    return embed


class DraftTradeView(discord.ui.View):
    """Dynamically generated buttons for traded players in a draft class."""

    def __init__(self, traded_players: list[dict]):
        super().__init__(timeout=120)
        for p in traded_players[:5]:  # Discord max 5 buttons per row
            btn = discord.ui.Button(
                label=f"🔄 {p['extendedName'][:70]}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"draft_trade_{p['extendedName'][:80]}",
            )
            btn.callback = self._make_callback(p["extendedName"])
            self.add_item(btn)

    @staticmethod
    def _make_callback(player_name: str):
        async def callback(interaction: discord.Interaction):
            embed = _build_trade_card_embed(player_name)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return callback


class DraftClassView(discord.ui.View):
    """Team draft class drilldown with AFC/NFC team selects and season picker."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot_ref = bot
        self._selected_team_abbr: str | None = None
        self._selected_team_nick: str | None = None
        self._selected_season: int = dm.CURRENT_SEASON

        # Build team options
        try:
            import roster
            all_teams = roster.get_all_teams()
        except Exception:
            all_teams = []

        afc = [t for t in all_teams if t["conference"] == "AFC"]
        nfc = [t for t in all_teams if t["conference"] == "NFC"]

        # AFC select
        afc_options = [
            discord.SelectOption(label=f"{t['nickName']} ({t['abbrName']})", value=t["abbrName"])
            for t in afc
        ]
        if afc_options:
            afc_select = discord.ui.Select(
                placeholder="AFC Team...",
                options=afc_options,
                row=0,
            )
            afc_select.callback = self._team_selected
            self.add_item(afc_select)

        # NFC select
        nfc_options = [
            discord.SelectOption(label=f"{t['nickName']} ({t['abbrName']})", value=t["abbrName"])
            for t in nfc
        ]
        if nfc_options:
            nfc_select = discord.ui.Select(
                placeholder="NFC Team...",
                options=nfc_options,
                row=1,
            )
            nfc_select.callback = self._team_selected
            self.add_item(nfc_select)

        # Season select
        season_options = [
            discord.SelectOption(
                label=f"Season {s}",
                value=str(s),
                default=(s == dm.CURRENT_SEASON),
            )
            for s in range(2, dm.CURRENT_SEASON + 1)
        ]
        if season_options:
            season_select = discord.ui.Select(
                placeholder="Season...",
                options=season_options[:25],
                row=2,
            )
            season_select.callback = self._season_selected
            self.add_item(season_select)

        # Store team lookup for nick resolution
        self._team_lookup = {t["abbrName"]: t["nickName"] for t in all_teams}

    async def _team_selected(self, interaction: discord.Interaction):
        self._selected_team_abbr = interaction.data["values"][0]
        self._selected_team_nick = self._team_lookup.get(self._selected_team_abbr, self._selected_team_abbr)
        await self._send_draft(interaction)

    async def _season_selected(self, interaction: discord.Interaction):
        try:
            self._selected_season = int(interaction.data["values"][0])
        except (ValueError, TypeError):
            pass

        if self._selected_team_abbr:
            await self._send_draft(interaction)
        else:
            await interaction.response.defer()

    async def _send_draft(self, interaction: discord.Interaction):
        if not self._selected_team_abbr:
            await interaction.response.defer()
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_team_draft_embed(
            self._selected_team_abbr,
            self._selected_team_nick or self._selected_team_abbr,
            self._selected_season,
        )

        # Check for traded players — add trade buttons
        data = ig.get_team_draft_class(self._selected_team_abbr, self._selected_season)
        traded = [p for p in data.get("players", []) if p.get("was_traded")]

        if traded:
            trade_view = DraftTradeView(traded)
            await interaction.followup.send(embed=embed, view=trade_view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


# ── Player Leaders ────────────────────────────────────────────────────────────

def _build_player_leaders_embed(
    position: str, stat_col: str, stat_label: str
) -> tuple[discord.Embed, list[str]]:
    """
    Returns (embed, player_name_list).
    player_name_list feeds into PlayerDrillView's select menu for Hot/Cold drill-down.

    FIX: df_offense / df_defense only have rosterId + stat columns.
    We join with df_players to get fullName, teamName, pos.
    """
    try:
        import pandas as pd

        # ── Select the right stats table ──────────────────────────────────────
        df_stats = dm.df_defense if position in ("DL", "LB", "DB") else dm.df_offense

        if df_stats is None or df_stats.empty:
            return (
                discord.Embed(title=f"🎯 {position} Leaders", description="No stat data available. Run `/wittsync` first.", color=C_NEUTRAL),
                [],
            )

        if stat_col not in df_stats.columns:
            available = ", ".join(df_stats.columns[:10].tolist())
            return (
                discord.Embed(
                    title=f"🎯 {position} {stat_label} Leaders",
                    description=f"Column `{stat_col}` not found.\nAvailable: `{available}`",
                    color=C_NEUTRAL,
                ),
                [],
            )

        # ── Build player lookup from df_players ───────────────────────────────
        if dm.df_players is None or dm.df_players.empty:
            return (
                discord.Embed(title=f"🎯 {position} Leaders", description="No roster data. Run `/wittsync` first.", color=C_NEUTRAL),
                [],
            )

        # Identify the join key — MaddenStats exports use rosterId
        stats_id_col   = next((c for c in ("rosterId", "playerid", "player_id", "id") if c in df_stats.columns), None)
        players_id_col = next((c for c in ("rosterId", "playerid", "player_id", "id") if c in dm.df_players.columns), None)

        if not stats_id_col or not players_id_col:
            return (
                discord.Embed(title="❌ Schema Error", description=f"Cannot join stats to roster. Stats key: `{stats_id_col}`, Players key: `{players_id_col}`", color=C_RED),
                [],
            )

        # Build a lightweight lookup: rosterId → {firstName, lastName, teamName, pos}
        name_a = next((c for c in ("firstName", "first_name") if c in dm.df_players.columns), None)
        name_b = next((c for c in ("lastName",  "last_name")  if c in dm.df_players.columns), None)
        full_col = next((c for c in ("fullName", "displayName") if c in dm.df_players.columns), None)
        pos_col  = next((c for c in ("pos", "position") if c in dm.df_players.columns), None)
        team_col = next((c for c in ("teamName", "team", "displayName") if c in dm.df_players.columns), None)

        keep_cols = [players_id_col]
        if name_a: keep_cols.append(name_a)
        if name_b: keep_cols.append(name_b)
        if full_col and full_col != players_id_col: keep_cols.append(full_col)
        if pos_col:  keep_cols.append(pos_col)
        if team_col and team_col not in keep_cols: keep_cols.append(team_col)

        roster = dm.df_players[keep_cols].drop_duplicates(subset=[players_id_col]).copy()

        # Build fullName if not present
        if full_col not in roster.columns and name_a and name_b:
            roster["_fullName"] = (
                roster[name_a].fillna("").str.strip() + " " +
                roster[name_b].fillna("").str.strip()
            ).str.strip()
            full_col = "_fullName"
        elif full_col not in roster.columns:
            roster["_fullName"] = roster[players_id_col].astype(str)
            full_col = "_fullName"

        # ── Join stats + roster ───────────────────────────────────────────────
        df_merged = df_stats.merge(roster, left_on=stats_id_col, right_on=players_id_col, how="left")

        # ── Filter by position group ──────────────────────────────────────────
        valid_pos = _POS_GROUP_MAP.get(position, [position])
        if pos_col and pos_col in df_merged.columns:
            df_merged = df_merged[df_merged[pos_col].isin(valid_pos)].copy()

        if df_merged.empty:
            return (
                discord.Embed(
                    title=f"🎯 {position} {stat_label} Leaders",
                    description=f"No players found for position group **{position}** (`{', '.join(valid_pos)}`).\nCheck position names in the export.",
                    color=C_NEUTRAL,
                ),
                [],
            )

        # ── Aggregate by player (sum across games) ────────────────────────────
        df_merged[stat_col] = pd.to_numeric(df_merged[stat_col], errors="coerce").fillna(0)
        group_keys = [full_col] + ([team_col] if team_col and team_col in df_merged.columns else []) + ([pos_col] if pos_col and pos_col in df_merged.columns else [])
        group_keys = list(dict.fromkeys(group_keys))  # dedupe

        agg = (
            df_merged.groupby(group_keys, dropna=False)[stat_col]
            .sum()
            .reset_index()
            .sort_values(stat_col, ascending=(stat_label in ("INTs", "Drops")))
            .head(10)
        )

        embed = discord.Embed(
            title=f"🎯 {position} — {stat_label} Leaders",
            description=f"Top 10 · Season totals · {_season_label()}",
            color=C_BLUE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        lines, player_names = [], []
        for i, (_, row) in enumerate(agg.iterrows(), 1):
            name = str(row.get(full_col, "?")).strip() or "?"
            team = str(row.get(team_col, "?")).strip() if team_col and team_col in row else "?"
            pos  = str(row.get(pos_col,  position)).strip() if pos_col and pos_col in row else position
            val  = row[stat_col]
            fmt  = f"{val:.1f}" if isinstance(val, float) and val != int(val) else f"{int(val)}"
            lines.append(f"{_rank_emoji(i)} **{name}** ({pos}, {team}) — **{fmt}** {stat_label}")
            if name and name != "?":
                player_names.append(name)

        embed.add_field(
            name=f"📊 {stat_label}",
            value="\n".join(lines) if lines else "_No results_",
            inline=False,
        )
        embed.set_footer(text="Select a player below for their Hot/Cold breakdown · ATLAS™ Oracle", icon_url=ATLAS_ICON_URL)
        return embed, player_names

    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        return (
            discord.Embed(title="❌ Error", description=f"`{type(e).__name__}: {e}`", color=C_RED),
            [],
        )


# ── Team Stats ────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  TEAM CARD v2 — TRI-MODE SYSTEM
#  Mode 1: Snapshot  (async — includes AI projection)
#  Mode 2: History   (franchise timeline + rivalries)
#  Mode 3: Scouting  (exploit map + momentum read)
#  Mode 4: Matchup   (H2H comparison + AI prediction)
# ─────────────────────────────────────────────────────────────────────────────

async def _build_team_card_snapshot(
    team_name: str,
    caller_team: str = "",
    owner_username: str = "",
) -> discord.Embed:
    """
    The new team card — data storytelling, not data reporting.
    Dynamic color, NFL logo thumbnail, DNA bars, rings, AI projection.
    """
    is_yours = caller_team and caller_team.lower() in team_name.lower()

    embed = discord.Embed(
        title=f"🏈  {team_name}{'  ◄  Your Team' if is_yours else ''}",
        color=_team_color(team_name),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    logo = _team_logo(team_name)
    if logo:
        embed.set_thumbnail(url=logo)

    # ── Pull standings row ────────────────────────────────────────────────────
    st_row = ts_row = None
    w = l = pf = pa = net = rank = seed = 0
    off_yds = off_rank = def_yds = def_rank = 0
    off_pass = off_rush = def_pass = def_rush = 0
    to_diff = 0
    streak_raw = ""
    sos = "?"

    if not dm.df_standings.empty:
        m = dm.df_standings[
            dm.df_standings["teamName"].str.lower().str.contains(team_name.lower(), na=False)
        ]
        if not m.empty:
            st_row   = m.iloc[0]
            w        = int(st_row.get("totalWins",       0))
            l        = int(st_row.get("totalLosses",     0))
            pf       = int(st_row.get("ptsFor",          0))
            pa       = int(st_row.get("ptsAgainst",      0))
            net      = int(st_row.get("netPts",          0))
            rank     = int(st_row.get("rank",            0))
            seed     = int(st_row.get("seed",            0))
            off_yds  = st_row.get("offTotalYds",   "?")
            off_rank = int(st_row.get("offTotalYdsRank", 0))
            def_yds  = st_row.get("defTotalYds",   "?")
            def_rank = int(st_row.get("defTotalYdsRank", 0))
            off_pass = st_row.get("offPassYds", 0)
            off_rush = st_row.get("offRushYds", 0)
            def_pass = st_row.get("defPassYds", 0)
            def_rush = st_row.get("defRushYds", 0)
            to_diff  = int(st_row.get("tODiff",          0))
            streak_raw = str(st_row.get("winLossStreak", ""))
            sos      = st_row.get("totalSoS", "?")

    if not dm.df_team_stats.empty:
        tsm = dm.df_team_stats[
            dm.df_team_stats["teamName"].str.lower().str.contains(team_name.lower(), na=False)
        ]
        if not tsm.empty:
            ts_row = tsm.iloc[0]

    total_gms  = w + l
    net_per_gm = net / total_gms if total_gms > 0 else 0

    # ── Streak display ────────────────────────────────────────────────────────
    streak_disp = ""
    try:
        sv = int(streak_raw)
        streak_disp = f"{'W' if sv > 0 else 'L'}{abs(sv)}"
    except Exception:
        streak_disp = streak_raw

    # ── Lede description ──────────────────────────────────────────────────────
    lede = [f"**{w}–{l}**"]
    if rank:         lede.append(f"#**{rank}** Power")
    if seed:         lede.append(f"#**{seed}** Seed")
    if streak_disp:  lede.append(f"Streak: **{streak_disp}**")
    if total_gms:    lede.append(f"**{net_per_gm:+.1f}** pts/gm")
    embed.description = f"*{_season_label()}*\n" + "  ·  ".join(lede)

    # ── 🏆 FRANCHISE — rings + owner identity ─────────────────────────────────
    ri = _ring_info(owner_username) if owner_username else None
    if ri and ri["nick"]:
        nick, count, tier, last = ri["nick"], ri["count"], ri["tier"], ri["last"]
        if count > 0:
            ring_dots = "💍" * min(count, 5) + ("+" if count > 5 else "")
            last_roman = _to_roman(last) if last else "?"
            ring_lines = [
                f"**{nick}**  ·  {tier}",
                ring_dots,
                f"**{count}** rings  ·  Last: SB **{last_roman}**",
            ]
            if ri["max_streak"] >= 2:
                ring_lines.append(f"⚡ {ri['max_streak']}× back-to-back")
        else:
            ring_lines = [
                f"**{nick}**  ·  {tier}",
                "💀 Still hunting · 0 rings",
            ]
        embed.add_field(name="🏆 FRANCHISE", value="\n".join(ring_lines), inline=True)

    # ── 📋 THIS SEASON ────────────────────────────────────────────────────────
    if st_row is not None:
        pct_str = f"{w / total_gms * 100:.0f}%" if total_gms else "—"
        embed.add_field(
            name="📋 THIS SEASON",
            value=(
                f"`{_winpct_bar(w, l, width=10)}`  **{pct_str}**\n"
                f"PF **{pf}**  ·  PA **{pa}**  ·  Net **{net:+d}**\n"
                f"Rank **#{rank}**  ·  Seed **#{seed}**  ·  SoS **{sos}**"
            ),
            inline=True,
        )

    # ── ⚡ CLUTCH ─────────────────────────────────────────────────────────────
    clutch_winpct = 0.0
    cd = ig.get_clutch_records(margin=7)
    if "records" in cd:
        tc = next(
            (r for r in cd["records"] if r.get("team", "").lower() in team_name.lower()),
            None,
        )
        if tc and tc.get("clutch_games", 0) > 0:
            cw  = tc["clutch_wins"]
            ccl = tc["clutch_losses"]
            clutch_winpct = float(tc.get("clutch_winpct", 0))
            cs  = round(clutch_winpct * 100)
            if   cs >= 70: c_badge = "🔥 Elite Closer"
            elif cs >= 55: c_badge = "✅ Clutch"
            elif cs >= 45: c_badge = "😐 Average"
            elif cs >= 30: c_badge = "😰 Shaky"
            else:          c_badge = "💀 Choke Artist"
            embed.add_field(
                name="⚡ CLUTCH",
                value=(
                    f"**{cw}–{ccl}** ({clutch_winpct:.0%}) ≤7pt\n"
                    f"`{_winpct_bar(cw, ccl, width=8)}`\n"
                    f"{c_badge}  ·  **{cs}**/100"
                ),
                inline=True,
            )

    # ── 🧬 TEAM DNA ───────────────────────────────────────────────────────────
    if st_row is not None:
        try:
            p_y = float(off_pass or 0)
            r_y = float(off_rush or 0)
            tot = p_y + r_y
            pass_ratio = p_y / tot if tot > 0 else 0.5
            # off_dominance: 1.0 = elite offense (rank 1), 0.0 = elite defense (rank 1)
            off_dom = 1 - (off_rank / 32) if off_rank else 0.5

            filled_pass = round(pass_ratio * 10)
            filled_off  = round(off_dom    * 10)
            run_bar  = "█" * (10 - filled_pass) + "░" * filled_pass
            pass_bar = "░" * (10 - filled_pass) + "█" * filled_pass  # noqa — just for display
            # single bidirectional bar: left = RUN, right = PASS
            dna_rp  = "█" * filled_pass + "░" * (10 - filled_pass)
            dna_od  = "█" * filled_off  + "░" * (10 - filled_off)

            tags = _playstyle_tags(st_row, ts_row, clutch_winpct)
            tag_str = "  ".join(tags) if tags else ""

            dna_block = (
                f"```\n"
                f"RUN  {dna_rp}  PASS\n"
                f"OFF  {dna_od}  DEF\n"
                f"```"
                f"{tag_str}"
            )
            embed.add_field(name="🧬 TEAM DNA", value=dna_block, inline=False)
        except Exception:
            pass

    # ── ⚔️ OFFENSE  +  🛡️ DEFENSE ─────────────────────────────────────────────
    if st_row is not None:
        o_arr = "↑" if off_rank <= 10 else ("↓" if off_rank >= 23 else "→")
        d_arr = "↑" if def_rank <= 10 else ("↓" if def_rank >= 23 else "→")

        third_pct = rz_pct = None
        if ts_row is not None:
            try:
                v = float(ts_row.get("off3rdDownConvPct", 0))
                if v > 0: third_pct = v
            except Exception:
                pass
            try:
                v = float(ts_row.get("offRedZonePct", 0))
                if v > 0: rz_pct = v
            except Exception:
                pass

        off_lines = [
            f"Yds: **{off_yds}** (#{off_rank}) {o_arr}",
            f"Pass **{off_pass}**  ·  Rush **{off_rush}**",
        ]
        if third_pct:
            eff_str = f"3rd: **{third_pct:.0f}%**"
            if rz_pct: eff_str += f"  RZ: **{rz_pct:.0f}%**"
            off_lines.append(eff_str)

        def_lines = [
            f"Allow: **{def_yds}** (#{def_rank}) {d_arr}",
            f"Pass **{def_pass}**  ·  Rush **{def_rush}**",
            f"TO Diff: **{to_diff:+d}**",
        ]
        if ts_row is not None:
            try:
                pen = int(ts_row.get("penalties", 0))
                pyd = ts_row.get("penaltyYds", "?")
                if pen > 0:
                    def_lines.append(f"Penalties: **{pen}** ({pyd} yds)")
            except Exception:
                pass

        embed.add_field(name="⚔️ OFFENSE", value="\n".join(off_lines), inline=True)
        embed.add_field(name="🛡️ DEFENSE", value="\n".join(def_lines), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # 3-col spacer

    # ── 📅 FORM — last 5 with sparkline ──────────────────────────────────────
    recent = dm.get_last_n_games(team_name, 5)
    if recent:
        dots, score_lines, win_log = [], [], []
        for g in recent:
            is_home   = team_name.lower() == str(g.get("home", "")).lower()
            my_score  = int(g.get("home_score", 0) if is_home else g.get("away_score", 0))
            opp_score = int(g.get("away_score", 0) if is_home else g.get("home_score", 0))
            opp_name  = str(g.get("away", "?") if is_home else g.get("home", "?"))
            won       = my_score > opp_score
            win_log.append(won)
            dots.append("🟢" if won else "🔴")
            score_lines.append(
                f"`{'W' if won else 'L'} {my_score:>2}–{opp_score:<2}  vs {opp_name[:12]}`"
            )
        spark = _trend_sparkline(win_log)
        embed.add_field(
            name="📅 FORM — Last 5",
            value=f"{''.join(dots)}  `{spark}`\n" + "\n".join(score_lines),
            inline=False,
        )

    # ── 🔬 ATLAS Oracle · Projection ────────────────────────────────────────────────
    if (atlas_ai._get_claude() or atlas_ai._get_gemini()) and st_row is not None:
        try:
            games_left = 18 - total_gms
            prompt = (
                f"{get_persona('analytical')}\n\nIn exactly 2 sentences max, "
                f"give a savage but accurate season projection for the {team_name}. "
                f"Record: {w}-{l}. Power rank: #{rank}. Seed: #{seed}. "
                f"Net pts/game: {net_per_gm:+.1f}. Streak: {streak_disp or 'none'}. "
                f"Off rank: #{off_rank}. Def rank: #{def_rank}. TO diff: {to_diff:+d}. "
                f"Clutch: {clutch_winpct:.0%}. Games remaining: {games_left}. "
                f"TSL Season {dm.CURRENT_SEASON}, Week {dm.CURRENT_WEEK}. "
                f"Be specific. Be ruthless. No sugarcoating."
            )
            projection = await _ai_blurb(prompt, max_tokens=100)
            if projection:
                embed.add_field(
                    name="🔬 ATLAS Oracle · Projection",
                    value=f"*{projection}*",
                    inline=False,
                )
        except Exception:
            pass

    embed.set_footer(
        text=f"ATLAS™ Oracle · Team Card v2  ·  {_season_label()}  ·  ◄ = Your team", icon_url=ATLAS_ICON_URL
    )
    return embed


def _build_team_card_history(team_name: str) -> discord.Embed:
    """
    Deep Dive: Franchise timeline, all-time record, signature moments, rivalries.
    """
    embed = discord.Embed(
        title=f"📖  {team_name} — Franchise History",
        color=_team_color(team_name),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    logo = _team_logo(team_name)
    if logo:
        embed.set_thumbnail(url=logo)

    # ── Season-by-season timeline ─────────────────────────────────────────────
    seasons = _franchise_by_season(team_name)
    alltime = _franchise_alltime(team_name)
    aw, al  = alltime["wins"], alltime["losses"]

    if seasons:
        max_w  = max(int(r.get("wins", 0)) for r in seasons) or 1
        lines  = []
        for r in seasons:
            s   = int(r.get("seasonIndex", 0)) + 1
            sw  = int(r.get("wins",   0))
            sl  = int(r.get("losses", 0))
            bar = "█" * round((sw / max_w) * 12) + "░" * (12 - round((sw / max_w) * 12))
            peak_tag = "  ← PEAK" if sw == max_w else ""
            lines.append(f"S{s}  `{bar}`  **{sw}–{sl}**{peak_tag}")
        ag   = aw + al
        apct = f"{aw / ag * 100:.1f}%" if ag else "—"
        embed.add_field(
            name=f"📈 Franchise Timeline  |  {aw}–{al} all-time  ({apct})",
            value="\n".join(lines),
            inline=False,
        )

    # ── Signature moments ─────────────────────────────────────────────────────
    big_w, worst_l = _franchise_signature_moments(team_name)
    moments = []
    if big_w:
        ht, at = big_w["homeTeamName"], big_w["awayTeamName"]
        hs, as_ = big_w["homeScore"], big_w["awayScore"]
        m  = big_w["margin"]
        s  = int(big_w.get("seasonIndex", 0)) + 1
        wk = int(big_w.get("weekIndex",   0)) + 1
        moments.append(f"🏆 **Biggest W:** {ht} **{hs}**–{as_} {at}  (+{m} · S{s} Wk{wk})")
    if worst_l:
        ht, at = worst_l["homeTeamName"], worst_l["awayTeamName"]
        hs, as_ = worst_l["homeScore"], worst_l["awayScore"]
        m  = worst_l["margin"]
        s  = int(worst_l.get("seasonIndex", 0)) + 1
        wk = int(worst_l.get("weekIndex",   0)) + 1
        moments.append(f"💀 **Worst L:** {ht} {hs}–**{as_}** {at}  (–{m} · S{s} Wk{wk})")
    if moments:
        embed.add_field(name="🎭 Signature Moments", value="\n".join(moments), inline=False)

    # ── Eternal rivalries ─────────────────────────────────────────────────────
    nemesis     = _franchise_nemesis(team_name)
    punching_bag = _franchise_punching_bag(team_name)
    rival_lines = []
    if nemesis:
        nw = int(nemesis.get("wins",   0))
        nl = int(nemesis.get("losses", 0))
        ng = int(nemesis.get("games",  0))
        opp = str(nemesis.get("opp", "?"))[:22]
        rival_lines.append(
            f"👻 **Nemesis:** {opp}  —  {nw}–{nl} ({nw/ng:.0%} win rate over {ng} games)"
        )
    if punching_bag:
        pw = int(punching_bag.get("wins",  0))
        pg = int(punching_bag.get("games", 0))
        opp = str(punching_bag.get("opp", "?"))[:22]
        rival_lines.append(
            f"💪 **Punching Bag:** {opp}  —  {pw}–{pg-pw} ({pw/pg:.0%} win rate over {pg} games)"
        )
    if rival_lines:
        embed.add_field(name="⚔️ Eternal Rivalries", value="\n".join(rival_lines), inline=False)

    embed.set_footer(text="ATLAS™ Oracle · Franchise History · All 6 Seasons · Regular Season", icon_url=ATLAS_ICON_URL)
    return embed


def _build_team_card_scouting(team_name: str) -> discord.Embed:
    """
    Deep Dive: Scouting report — exploit map, momentum, vulnerability analysis.
    """
    embed = discord.Embed(
        title=f"🔬  {team_name} — Scouting Report",
        description="*How to beat this team. Don't share this with them.*",
        color=_team_color(team_name),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    logo = _team_logo(team_name)
    if logo:
        embed.set_thumbnail(url=logo)

    st_row = None
    if not dm.df_standings.empty:
        m = dm.df_standings[
            dm.df_standings["teamName"].str.lower().str.contains(team_name.lower(), na=False)
        ]
        if not m.empty:
            st_row = m.iloc[0]

    # ── Exploit map ───────────────────────────────────────────────────────────
    if st_row is not None:
        exploits = []
        try:
            tod = int(st_row.get("tODiff", 0))
            if tod >= 4:
                exploits.append(
                    f"✅ **Turnover Machine:** +{tod} TO diff. They live off mistakes — protect the ball."
                )
            elif tod <= -3:
                exploits.append(
                    f"⚠️ **Turnover Prone:** {tod:+d} TO diff. Force chaos — this game ends fast."
                )
        except Exception:
            pass

        try:
            off_r = int(st_row.get("offTotalYdsRank", 16))
            def_r = int(st_row.get("defTotalYdsRank", 16))
            if off_r < def_r - 8:
                exploits.append(
                    f"🎯 **Offense-Dependent:** OFF #{off_r} but DEF #{def_r}. "
                    f"Score fast and force them to play catch-up."
                )
            elif def_r < off_r - 8:
                exploits.append(
                    f"🛡️ **Defense-First:** DEF #{def_r} but OFF #{off_r}. "
                    f"Play ball control. Grind it out. They'll run out of ideas."
                )
        except Exception:
            pass

        try:
            d_pass = float(st_row.get("defPassYds", 0))
            d_rush = float(st_row.get("defRushYds", 0))
            if d_pass > 0 and d_rush > 0:
                ratio = d_pass / (d_pass + d_rush)
                if ratio > 0.62:
                    exploits.append(
                        f"🚀 **Pass to Win:** They surrender {d_pass:.0f} pass yds vs {d_rush:.0f} rush. "
                        f"Air it out early and often."
                    )
                elif ratio < 0.42:
                    exploits.append(
                        f"🏃 **Run to Win:** They surrender {d_rush:.0f} rush yds vs {d_pass:.0f} pass. "
                        f"Run it down their throat and don't stop."
                    )
        except Exception:
            pass

        cd = ig.get_clutch_records(margin=7)
        if "records" in cd:
            tc = next(
                (r for r in cd["records"] if r.get("team","").lower() in team_name.lower()),
                None,
            )
            if tc and tc.get("clutch_games", 0) >= 2:
                cpct = float(tc.get("clutch_winpct", 0.5))
                if cpct <= 0.35:
                    exploits.append(
                        f"⚡ **Clutch Liability:** {cpct:.0%} in ≤7pt games. Keep it tight — they fold."
                    )
                elif cpct >= 0.70:
                    exploits.append(
                        f"⚡ **Clutch Threat:** {cpct:.0%} in ≤7pt games. Do NOT let this game be close."
                    )

        if exploits:
            embed.add_field(
                name="🎯 Exploit Map",
                value="\n".join(exploits),
                inline=False,
            )

    # ── Momentum read ─────────────────────────────────────────────────────────
    recent = dm.get_last_n_games(team_name, 5)
    if recent:
        diffs, wins_l5 = [], []
        for g in recent:
            is_home   = team_name.lower() == str(g.get("home", "")).lower()
            my_s      = int(g.get("home_score", 0) if is_home else g.get("away_score", 0))
            opp_s     = int(g.get("away_score", 0) if is_home else g.get("home_score", 0))
            wins_l5.append(my_s > opp_s)
            diffs.append(my_s - opp_s)
        wc  = sum(wins_l5)
        avg = sum(diffs) / len(diffs)
        if   wc >= 4: mood = f"🔥 **Hot streak:** {wc}–{5 - wc} L5"
        elif wc <= 1: mood = f"❄️ **Ice cold:** {wc}–{5 - wc} L5"
        else:         mood = f"📊 **Inconsistent:** {wc}–{5 - wc} L5"

        embed.add_field(
            name="📈 Momentum Read",
            value=(
                f"{mood}  ·  Avg margin: **{avg:+.0f}**\n"
                f"Best result L5: **{max(diffs):+d}**  ·  Worst: **{min(diffs):+d}**"
            ),
            inline=False,
        )

    # ── Floor / Ceiling ───────────────────────────────────────────────────────
    big_w, worst_l = _franchise_signature_moments(team_name)
    fc_lines = []
    if big_w:
        fc_lines.append(f"🏆 All-time best win: **+{big_w['margin']}** pts")
    if worst_l:
        fc_lines.append(f"💀 All-time worst loss: **–{worst_l['margin']}** pts")
    if fc_lines:
        embed.add_field(name="📚 Franchise Floor / Ceiling", value="\n".join(fc_lines), inline=True)

    embed.set_footer(text="ATLAS™ Oracle · Scouting Report · For your eyes only", icon_url=ATLAS_ICON_URL)
    return embed


async def _build_team_matchup_embed(team_a: str, team_b: str) -> discord.Embed:
    """
    Head-to-head matchup intel: H2H history, side-by-side comparison, AI prediction.
    """
    embed = discord.Embed(
        title=f"⚔️  {team_a}  vs  {team_b}",
        color=_team_color(team_a),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    # ── All-time H2H ──────────────────────────────────────────────────────────
    h2h   = dm.get_h2h_record(team_a, team_b)
    a_w   = h2h.get("a_wins", 0)
    b_w   = h2h.get("b_wins", 0)
    total = a_w + b_w
    if total > 0:
        embed.add_field(
            name="📊 All-Time H2H",
            value=(
                f"**{team_a[:16]}:** {a_w} wins\n"
                f"**{team_b[:16]}:** {b_w} wins\n"
                f"`{_winpct_bar(a_w, b_w, width=10)}`"
            ),
            inline=True,
        )
    else:
        embed.add_field(
            name="📊 All-Time H2H",
            value="No completed games on record.",
            inline=True,
        )

    # ── Side-by-side comparison ────────────────────────────────────────────────
    a_row = b_row = None
    if not dm.df_standings.empty:
        am = dm.df_standings[dm.df_standings["teamName"].str.lower().str.contains(team_a.lower(), na=False)]
        bm = dm.df_standings[dm.df_standings["teamName"].str.lower().str.contains(team_b.lower(), na=False)]
        if not am.empty: a_row = am.iloc[0]
        if not bm.empty: b_row = bm.iloc[0]

    if a_row is not None and b_row is not None:
        def _edge(av, bv, lower_better=False) -> str:
            try:
                a, b = float(av), float(bv)
                better_a = a < b if lower_better else a > b
                return "✅" if better_a else ("❌" if a != b else "➖")
            except Exception:
                return "➖"

        aw = int(a_row.get("totalWins",0)); al = int(a_row.get("totalLosses",0))
        bw = int(b_row.get("totalWins",0)); bl = int(b_row.get("totalLosses",0))
        ao = a_row.get("offTotalYdsRank","?"); bo = b_row.get("offTotalYdsRank","?")
        ad = a_row.get("defTotalYdsRank","?"); bd = b_row.get("defTotalYdsRank","?")
        at_ = int(a_row.get("tODiff",0)); bt = int(b_row.get("tODiff",0))
        an_ = int(a_row.get("netPts",0)); bn = int(b_row.get("netPts",0))

        na = team_a[:9]; nb = team_b[:9]
        lines = [
            f"```",
            f"{'Metric':<14} {na:<11} {nb:<11}",
            f"{'─'*36}",
            f"{'Record':<14} {f'{aw}-{al}':<11} {f'{bw}-{bl}':<11}",
            f"{'Off Rank':<14} {f'#{ao}':<11} {f'#{bo}':<11}",
            f"{'Def Rank':<14} {f'#{ad}':<11} {f'#{bd}':<11}",
            f"{'TO Diff':<14} {f'{at_:+d}':<11} {f'{bt:+d}':<11}",
            f"{'Net Pts':<14} {f'{an_:+d}':<11} {f'{bn:+d}':<11}",
            f"```",
        ]
        embed.add_field(
            name="📈 Season Comparison",
            value="\n".join(lines),
            inline=False,
        )

    # ── AI matchup prediction ─────────────────────────────────────────────────
    if (atlas_ai._get_claude() or atlas_ai._get_gemini()) and a_row is not None and b_row is not None:
        try:
            aw2 = int(a_row.get("totalWins",0)); al2 = int(a_row.get("totalLosses",0))
            bw2 = int(b_row.get("totalWins",0)); bl2 = int(b_row.get("totalLosses",0))
            h2h_ctx = f"{a_w}–{b_w} all-time H2H" if total > 0 else "no prior H2H"
            prompt = (
                f"{get_persona('analytical')}\n\nIn exactly 2 sentences, "
                f"predict {team_a} ({aw2}-{al2}) vs {team_b} ({bw2}-{bl2}). "
                f"H2H: {h2h_ctx}. "
                f"{team_a} OFF #{a_row.get('offTotalYdsRank','?')} DEF #{a_row.get('defTotalYdsRank','?')} "
                f"TO {int(a_row.get('tODiff',0)):+d}. "
                f"{team_b} OFF #{b_row.get('offTotalYdsRank','?')} DEF #{b_row.get('defTotalYdsRank','?')} "
                f"TO {int(b_row.get('tODiff',0)):+d}. "
                f"Name a winner. Give a score prediction. Be decisive."
            )
            pred = await _ai_blurb(prompt, max_tokens=110)
            if pred:
                embed.add_field(
                    name="🔬 ATLAS Oracle · Prediction",
                    value=f"*{pred}*",
                    inline=False,
                )
        except Exception:
            pass

    embed.set_footer(text="ATLAS™ Oracle · Matchup Intel · Ephemeral", icon_url=ATLAS_ICON_URL)
    return embed


# ── All-Time Records ──────────────────────────────────────────────────────────

def _build_alltime_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🏛️ TSL All-Time Records",
        description="6 seasons of history — regular season",
        color=C_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    if not _HISTORY_OK:
        embed.description = "⚠️ History database not available."
        return embed

    try:
        # All-time win leaders
        rows, _ = run_sql("""
            SELECT winner_user, COUNT(*) AS wins
            FROM games
            WHERE status IN ('2','3') AND stageIndex='1'
              AND winner_user IS NOT NULL AND winner_user != ''
            GROUP BY winner_user
            ORDER BY wins DESC
            LIMIT 5
        """)
        if rows:
            lines = [f"{_rank_emoji(i)} **{r['winner_user']}** — {r['wins']} wins" for i, r in enumerate(rows, 1)]
            embed.add_field(name="🏆 All-Time Win Leaders", value="\n".join(lines), inline=True)

        # All-time loss leaders (most losses = most games played usually)
        rows2, _ = run_sql("""
            SELECT loser_user, COUNT(*) AS losses
            FROM games
            WHERE status IN ('2','3') AND stageIndex='1'
              AND loser_user IS NOT NULL AND loser_user != ''
            GROUP BY loser_user
            ORDER BY losses DESC
            LIMIT 5
        """)
        if rows2:
            lines2 = [f"{_rank_emoji(i)} **{r['loser_user']}** — {r['losses']} losses" for i, r in enumerate(rows2, 1)]
            embed.add_field(name="💀 Most Losses", value="\n".join(lines2), inline=True)

        # Highest scoring game ever
        rows3, _ = run_sql("""
            SELECT homeTeamName, awayTeamName, homeScore, awayScore,
                   (CAST(homeScore AS INTEGER) + CAST(awayScore AS INTEGER)) AS total,
                   seasonIndex, weekIndex
            FROM games
            WHERE status IN ('2','3') AND stageIndex='1'
            ORDER BY total DESC
            LIMIT 1
        """)
        if rows3:
            r = rows3[0]
            embed.add_field(
                name="💥 Highest Scoring Game",
                value=(
                    f"**{r['homeTeamName']}** {r['homeScore']} – {r['awayScore']} **{r['awayTeamName']}**\n"
                    f"S{r['seasonIndex']} · Wk {int(r.get('weekIndex', 0))+1} · {r['total']} total pts"
                ),
                inline=False,
            )

        # Biggest blowout
        rows4, _ = run_sql("""
            SELECT homeTeamName, awayTeamName, homeScore, awayScore,
                   ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin,
                   seasonIndex, weekIndex
            FROM games
            WHERE status IN ('2','3') AND stageIndex='1'
            ORDER BY margin DESC
            LIMIT 1
        """)
        if rows4:
            r = rows4[0]
            embed.add_field(
                name="🔨 Biggest Blowout",
                value=(
                    f"**{r['homeTeamName']}** {r['homeScore']} – {r['awayScore']} **{r['awayTeamName']}**\n"
                    f"S{r['seasonIndex']} · Wk {int(r.get('weekIndex', 0))+1} · {r['margin']} pt margin"
                ),
                inline=True,
            )

        # Closest game ever
        rows5, _ = run_sql("""
            SELECT homeTeamName, awayTeamName, homeScore, awayScore,
                   ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin,
                   seasonIndex, weekIndex
            FROM games
            WHERE status IN ('2','3') AND stageIndex='1'
              AND CAST(homeScore AS INTEGER) > 0
            ORDER BY margin ASC
            LIMIT 1
        """)
        if rows5:
            r = rows5[0]
            embed.add_field(
                name="⚡ Closest Game Ever",
                value=(
                    f"**{r['homeTeamName']}** {r['homeScore']} – {r['awayScore']} **{r['awayTeamName']}**\n"
                    f"S{r['seasonIndex']} · Wk {int(r.get('weekIndex', 0))+1} · {r['margin']} pt margin"
                ),
                inline=True,
            )

        # Most active owners (games played)
        rows6, _ = run_sql("""
            SELECT owner, COUNT(*) AS games FROM (
                SELECT homeUser AS owner FROM games WHERE status IN ('2','3') AND stageIndex='1'
                UNION ALL
                SELECT awayUser AS owner FROM games WHERE status IN ('2','3') AND stageIndex='1'
            )
            WHERE owner IS NOT NULL AND owner != ''
            GROUP BY owner
            ORDER BY games DESC
            LIMIT 5
        """)
        if rows6:
            lines6 = [f"**{r['owner']}** — {r['games']} games" for r in rows6]
            embed.add_field(name="🎮 Most Active Owners", value="\n".join(lines6), inline=True)

    except Exception as e:
        embed.add_field(name="⚠️ Error", value=str(e), inline=False)

    embed.set_footer(text="ATLAS™ Oracle · All-Time Records · Seasons 1–6 · Regular Season Only", icon_url=ATLAS_ICON_URL)
    return embed


# ─────────────────────────────────────────────────────────────────────────────
#  TEAM CARD VIEWS + MODALS
# ─────────────────────────────────────────────────────────────────────────────

class TeamCardView(discord.ui.View):
    """
    Persistent button strip under every team card.
    Four modes: History · Scouting · Matchup · Refresh
    """

    def __init__(self, team_name: str, caller_team: str = "", owner_username: str = ""):
        super().__init__(timeout=300)
        self.team_name      = team_name
        self.caller_team    = caller_team
        self.owner_username = owner_username

    @discord.ui.button(label="📖 History", style=discord.ButtonStyle.secondary, row=0)
    async def btn_history(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = _build_team_card_history(self.team_name)
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    @discord.ui.button(label="🔬 Scouting", style=discord.ButtonStyle.secondary, row=0)
    async def btn_scouting(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = _build_team_card_scouting(self.team_name)
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    @discord.ui.button(label="⚔️ Matchup", style=discord.ButtonStyle.primary, row=0)
    async def btn_matchup(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(
            TeamMatchupModal(self.team_name, self.caller_team)
        )

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.success, row=0)
    async def btn_refresh(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = await _build_team_card_snapshot(
            self.team_name, self.caller_team, self.owner_username
        )
        await interaction.followup.send(
            embed=embed,
            view=TeamCardView(self.team_name, self.caller_team, self.owner_username),
            ephemeral=True,
        )


class TeamMatchupModal(discord.ui.Modal, title="⚔️ Matchup Intel"):
    opponent = discord.ui.TextInput(
        label="Opponent Team Name",
        placeholder="e.g. Cowboys, Ravens, 49ers...",
        required=True,
        max_length=50,
    )

    def __init__(self, team_name: str, caller_team: str = ""):
        super().__init__()
        self.team_name   = team_name
        self.caller_team = caller_team

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = await _build_team_matchup_embed(
            self.team_name, self.opponent.value.strip()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class TeamSearchModal(discord.ui.Modal, title="🏈 Team Stats Lookup"):
    """Fallback modal when team can't be auto-detected from profile."""
    team_name_input = discord.ui.TextInput(
        label="Team Name",
        placeholder="e.g. Cowboys, Ravens, 49ers...",
        required=True,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        name = self.team_name_input.value.strip()
        embed = await _build_team_card_snapshot(name, caller_team=name)
        await interaction.followup.send(
            embed=embed,
            view=TeamCardView(name, caller_team=name),
            ephemeral=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  MODALS
# ─────────────────────────────────────────────────────────────────────────────

class H2HModal(discord.ui.Modal, title="⚔️ Head-to-Head Lookup"):
    owner1 = discord.ui.TextInput(
        label="Owner 1",
        placeholder="Your username or nickname",
        required=True,
        max_length=50,
    )
    owner2 = discord.ui.TextInput(
        label="Owner 2",
        placeholder="Opponent username or nickname",
        required=True,
        max_length=50,
    )

    def __init__(self, default_owner: str):
        super().__init__()
        self.owner1.default = default_owner

    async def on_submit(self, interaction: discord.Interaction):
        # Immediate defer — AI call will take > 3 sec
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not _HISTORY_OK:
            await interaction.followup.send("⚠️ Historical database not available.", ephemeral=True)
            return

        u1 = fuzzy_resolve_user(self.owner1.value.strip())
        u2 = fuzzy_resolve_user(self.owner2.value.strip())

        if not u1:
            await interaction.followup.send(f"❓ Couldn't find `{self.owner1.value}`. Check spelling.", ephemeral=True)
            return
        if not u2:
            await interaction.followup.send(f"❓ Couldn't find `{self.owner2.value}`. Check spelling.", ephemeral=True)
            return

        if _get_h2h_sql:
            sql, params = _get_h2h_sql(u1, u2)
        else:
            sql = """
                SELECT
                    seasonIndex,
                    SUM(CASE WHEN winner_user=? THEN 1 ELSE 0 END) AS u1_wins,
                    SUM(CASE WHEN winner_user=? THEN 1 ELSE 0 END) AS u2_wins,
                    COUNT(*) AS games_played
                FROM games
                WHERE status IN ('2','3') AND stageIndex='1'
                  AND ((homeUser=? AND awayUser=?)
                    OR (homeUser=? AND awayUser=?))
                GROUP BY seasonIndex
                ORDER BY CAST(seasonIndex AS INTEGER)
            """
            params = (u1, u2, u1, u2, u2, u1)
        rows, err = run_sql(sql, params)

        embed = discord.Embed(
            title=f"⚔️ Rivalry Report: {u1} vs {u2}",
            color=C_RED,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        if err or not rows or all(int(r.get("games_played", 0)) == 0 for r in rows):
            embed.description = (
                f"📋 No completed regular season games found between **{u1}** and **{u2}**.\n"
                f"The rivalry that hasn't happened yet..."
            )
        else:
            total_u1    = sum(int(r["u1_wins"]    or 0) for r in rows)
            total_u2    = sum(int(r["u2_wins"]    or 0) for r in rows)
            total_games = sum(int(r["games_played"] or 0) for r in rows)

            embed.add_field(
                name="📊 All-Time Record (Regular Season)",
                value=f"**{u1}**: {total_u1}W  |  **{u2}**: {total_u2}W  |  {total_games} games",
                inline=False,
            )

            breakdown = ""
            for r in rows:
                w1 = int(r["u1_wins"] or 0)
                w2 = int(r["u2_wins"] or 0)
                marker = "🏆" if w1 > w2 else ("💀" if w2 > w1 else "🤝")
                breakdown += f"Season {r['seasonIndex']}: **{u1}** {w1}–{w2} **{u2}** {marker}\n"
            embed.add_field(name="📅 Season-by-Season", value=breakdown or "No data", inline=False)

            # ATLAS flair — non-blocking
            try:
                prompt = (
                    f"{get_persona('casual')}\n\nWrite a punchy 2–3 sentence rivalry summary. "
                    f"{u1} all-time wins: {total_u1}. {u2} all-time wins: {total_u2}. "
                    f"{total_games} total games. Make it entertaining and use football slang."
                )
                flair = await atlas_ai.generate(prompt, tier=Tier.HAIKU, max_tokens=300)
                embed.add_field(name="🎙️ ATLAS Echo", value=flair.text[:900], inline=False)
            except Exception:
                pass

        embed.set_footer(text="Regular season only · All 6 seasons · ATLAS™ Oracle", icon_url=ATLAS_ICON_URL)
        await interaction.followup.send(embed=embed, ephemeral=True)



# ─────────────────────────────────────────────────────────────────────────────
#  ASK ATLAS — MODE SELECTOR + TWO MODALS
# ─────────────────────────────────────────────────────────────────────────────

class OracleHubView(discord.ui.View):
    """
    Oracle Hub — 5 Ask ATLAS modes.
    📊 TSL League   → SQL pipeline against tsl_history.db
    🌐 Open Intel   → General AI + web search
    🏈 Sports Intel → NFL / real-world sports + web search
    🎯 Player Scout → Player ratings, dev traits, abilities from roster data
    🧠 Strategy     → Trade advice, roster tips, game strategy
    """

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="📊 TSL League", style=discord.ButtonStyle.primary, row=0)
    async def btn_tsl(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(AskTSLModal())

    @discord.ui.button(label="🌐 Open Intel", style=discord.ButtonStyle.secondary, row=0)
    async def btn_open(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(AskOpenModal())

    @discord.ui.button(label="🏈 Sports Intel", style=discord.ButtonStyle.secondary, row=0)
    async def btn_sports(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(SportsIntelModal())

    @discord.ui.button(label="🎯 Player Scout", style=discord.ButtonStyle.primary, row=1)
    async def btn_scout(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(PlayerScoutModal())

    @discord.ui.button(label="🧠 Strategy", style=discord.ButtonStyle.success, row=1)
    async def btn_strategy(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(StrategyRoomModal())


# ── Oracle Intelligence Modal Base ────────────────────────────────────────────

class _EarlyReturn(Exception):
    """Signal that _generate() already sent a response."""


class _OracleIntelModal(discord.ui.Modal):
    """Base class for AI-powered Oracle intelligence modals.

    Subclasses implement _generate() which returns the answer text
    and embed configuration. The base class handles the lifecycle:
    defer → guard → try/except → embed → send.
    """

    _requires_history: bool = False

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        if self._requires_history and not _HISTORY_OK:
            await interaction.followup.send(
                "⚠️ Historical database not available.", ephemeral=True
            )
            return

        try:
            answer, embed_kwargs = await self._generate(interaction)
            embed = self._build_embed(answer, **embed_kwargs)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except _EarlyReturn:
            pass  # _generate() already sent its own response
        except Exception as e:
            await interaction.followup.send(
                f"❌ Something broke: `{e}`", ephemeral=True
            )

    async def _generate(
        self, interaction: discord.Interaction
    ) -> tuple[str, dict]:
        raise NotImplementedError

    @staticmethod
    def _build_embed(answer: str, *, title: str, color, footer: str) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=_truncate_for_embed(answer),
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text=footer, icon_url=ATLAS_ICON_URL)
        return embed


# ── Oracle Intelligence Modals ────────────────────────────────────────────────

class AskTSLModal(_OracleIntelModal, title="📊 Ask ATLAS — TSL League"):
    """TSL League mode: natural language → SQL → AI-powered answer against tsl_history.db."""

    _requires_history = True

    question = discord.ui.TextInput(
        label="Your TSL Question",
        placeholder="e.g. Who has the most all-time wins? What's Killa's record vs JT?",
        required=True,
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    async def _generate(self, interaction: discord.Interaction) -> tuple[str, dict]:
        q = self.question.value.strip()

        annotated, alias_map = resolve_names_in_question(q)

        # ── AI name resolution fallback ───────────────────────
        if not alias_map and _ai_resolve_names:
            try:
                ai_aliases = await _ai_resolve_names(q)
                if ai_aliases:
                    alias_map = ai_aliases
                    for nickname, username in alias_map.items():
                        annotated = annotated.replace(
                            nickname, f"{nickname} (username: '{username}')"
                        )
            except Exception:
                pass

        # ── Resolve caller identity ─────────────────────────
        caller_db = None
        if _resolve_db_username_fn:
            caller_db = _resolve_db_username_fn(interaction.user.id)
        if not caller_db:
            caller_db = interaction.user.name

        # ── Conversation memory ─────────────────────────────
        conv_block = ""
        if _build_conversation_block:
            conv_block = await _build_conversation_block(interaction.user.id, source="codex")

        # ── Affinity tone (answer only) ─────────────────────
        affinity_block = ""
        if _affinity_mod:
            try:
                score = await _affinity_mod.get_affinity(interaction.user.id)
                affinity_block = _affinity_mod.get_affinity_instruction(score)
            except Exception:
                pass

        # ── Three-tier intent detection ─────────────────────
        intent_result = None
        tier_label = "Tier 3 (NL→SQL)"
        if _detect_intent:
            intent_result = await _detect_intent(q, caller_db, alias_map)

        if intent_result and intent_result.tier < 3 and intent_result.sql:
            rows, error = run_sql(intent_result.sql, intent_result.params)
            sql = intent_result.sql
            tier_label = f"Tier {intent_result.tier} ({'regex' if intent_result.tier == 1 else 'classified'})"
            if error:
                intent_result = None

        if not intent_result or intent_result.tier >= 3 or not intent_result.sql:
            sql = await gemini_sql(annotated, alias_map, conv_context=conv_block)
            if not sql:
                await interaction.followup.send(
                    "📊 Couldn't generate a query for that. Try rephrasing — "
                    "be specific about player names, seasons, or owners.",
                    ephemeral=True,
                )
                raise _EarlyReturn()

            schema = _build_schema_fn() if _build_schema_fn else ""
            rows, sql, error, attempt, _warnings = await retry_sql(sql, schema)
            if error:
                await interaction.followup.send(
                    "⚠️ Couldn't pull that data. Try asking differently!", ephemeral=True
                )
                raise _EarlyReturn()

        answer_context = "\n".join(filter(None, [conv_block, affinity_block]))
        answer = await gemini_answer(q, sql, rows, conv_context=answer_context)

        if _add_conversation_turn:
            await _add_conversation_turn(interaction.user.id, q, answer, sql=sql or "", source="codex")

        footer_parts = [f"🔍 {len(rows)} records analyzed", tier_label]
        if alias_map:
            footer_parts.append(
                f"🔎 Resolved: {', '.join(f'{k}→{v}' for k, v in alias_map.items())}"
            )
        if conv_block:
            footer_parts.append("💬 Conversational")
        if attempt > 1:
            footer_parts.append("⚠️ Self-corrected" if attempt == 2 else "🧠 Opus rescue")

        return answer, {
            "title": "🔬 ATLAS Intelligence — TSL League",
            "color": C_DARK,
            "footer": " | ".join(footer_parts) + " · ATLAS™ Oracle",
        }


class _AskWebModal(_OracleIntelModal):
    """Unified web search modal — parameterized for Open Intel and Sports Intel modes."""

    question = discord.ui.TextInput(
        label="Your Question",
        placeholder="Ask anything...",
        required=True,
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, *, mode: str = "open"):
        self.mode = mode
        if mode == "sports":
            super().__init__(title="🏈 Ask ATLAS — Sports Intel")
            self.question.label = "Your Sports Question"
            self.question.placeholder = "e.g. Who leads the NFL in passing yards? Latest trade rumors?"
        else:
            super().__init__(title="🌐 Ask ATLAS — Open Intel")
            self.question.label = "Your Question"
            self.question.placeholder = "e.g. Who is better, Luka or Jokic? Latest NFL news?"

    async def _generate(self, interaction: discord.Interaction) -> tuple[str, dict]:
        q = self.question.value.strip()

        system_instruction = get_persona("analytical")
        result = await atlas_ai.generate_with_search(q, system=system_instruction)

        if self.mode == "sports":
            embed_title = "🏈 ATLAS Intelligence — Sports Intel"
            embed_color = ATLAS_GOLD
            footer_mode = "Sports Intel mode"
            fallback_msg = "ATLAS couldn't pull intel on that one."
        else:
            embed_title = "🌐 ATLAS Intelligence — Open Intel"
            embed_color = C_BLUE
            footer_mode = "Open Intel mode"
            fallback_msg = "ATLAS couldn't pull a response on that one."

        answer = result.text or fallback_msg

        footer = f"{footer_mode} · Web search enabled · ATLAS™ Oracle"
        if result.fallback_used:
            footer += "  ·  ⚡ via Gemini fallback"

        return answer, {"title": embed_title, "color": embed_color, "footer": footer}


# Backwards-compatible aliases for hub view button callbacks
def AskOpenModal():
    return _AskWebModal(mode="open")

def SportsIntelModal():
    return _AskWebModal(mode="sports")


class PlayerScoutModal(_OracleIntelModal, title="🎯 Ask ATLAS — Player Scout"):
    """Player Scout mode: query Madden player ratings, abilities, dev traits from the roster."""

    _requires_history = True

    question = discord.ui.TextInput(
        label="Your Scouting Question",
        placeholder="e.g. Who is the fastest WR? Best X-Factor QBs? Compare two players?",
        required=True,
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    async def _generate(self, interaction: discord.Interaction) -> tuple[str, dict]:
        q = self.question.value.strip()

        # ── Caller team resolution ────────────────────────────
        caller_db = None
        team_name = None
        if _resolve_db_username_fn:
            caller_db = _resolve_db_username_fn(interaction.user.id)
        if caller_db and not dm.df_teams.empty:
            mask = dm.df_teams["userName"].str.lower() == caller_db.lower()
            if mask.any():
                team_name = dm.df_teams[mask].iloc[0].get("nickName", "")

        # ── Conversation memory fetch ─────────────────────────
        conv_block = ""
        if _build_conversation_block:
            conv_block = await _build_conversation_block(interaction.user.id, source="codex")

        # ── Scout-specific SQL prompt ─────────────────────────
        scout_schema = f"""DATABASE: tsl_history.db — Madden Player Scouting Data

TABLE: players (current roster snapshot — {dm.CURRENT_SEASON})
  Columns: rosterId, firstName, lastName, age, height, weight, pos, jerseyNum,
           college, yearsPro, dev, teamId, teamName, isFA, isOnIR,
           playerBestOvr, capHit, contractSalary, contractYearsLeft,
           speedRating, strengthRating, agilityRating, awareRating, catchRating,
           routeRunShortRating, routeRunMedRating, routeRunDeepRating,
           throwPowerRating, throwAccShortRating, throwAccMedRating, throwAccDeepRating,
           carryRating, jukeMoveRating, spinMoveRating, truckRating, breakTackleRating,
           tackleRating, hitPowerRating, pursuitRating, playRecRating, manCoverRating,
           zoneCoverRating, pressRating, blockSheddingRating, runBlockRating,
           passBlockRating, impactBlockRating, kickPowerRating, kickAccuracyRating
  Notes: dev values: 'Normal', 'Star', 'Superstar', 'Superstar X-Factor'. isFA='1' = free agent.
         devTrait column: '0'=Normal, '1'=Star, '2'=Superstar, '3'=Superstar X-Factor
         WARNING: players has firstName/lastName (NOT fullName). Use firstName || ' ' || lastName.
         WARNING: 800+ players have teamName='Free Agent' but isFA='0'. Exclude with: WHERE teamName != 'Free Agent'

TABLE: player_abilities (X-Factor/Superstar abilities)
  Columns: rosterId, firstName, lastName, teamName, title, description,
           startSeasonIndex, endSeasonIndex
  Notes: Active abilities have no endSeasonIndex or endSeasonIndex >= current season.

RULES:
- ALL columns are TEXT. Use CAST(col AS INTEGER) for math/comparisons.
- To find a player by name: WHERE firstName || ' ' || lastName LIKE '%name%'
- For position groups: QB, HB, WR, TE, LT/LG/C/RG/RT (OL), LE/RE/DT (DL), LOLB/MLB/ROLB (LB), CB/FS/SS (DB)
- Return ONLY the SQL query, no explanation, no markdown fences.
"""

        # Build prompt: schema → team context → conversation → question
        team_block = ""
        if team_name:
            team_block = (
                f"\nUSER CONTEXT: The user owns the {team_name}. "
                f"When they say 'my team', 'my players', or 'my roster', "
                f"filter by teamName='{team_name}'.\n"
            )

        conv_inject = ""
        if conv_block:
            conv_inject = f"\n{conv_block}\n"

        scout_prompt = f"""{scout_schema}{team_block}{conv_inject}
Generate a SQLite SELECT query to answer this scouting question:
"{q}"
"""

        # ── SQL generation (Sonnet for accuracy) ──────────────
        sql_result = await atlas_ai.generate(scout_prompt, tier=Tier.SONNET, max_tokens=500)
        sql = extract_sql(sql_result.text)
        if not sql:
            await interaction.followup.send(
                "🎯 Couldn't generate a scouting query. Try being specific about "
                "position, team, or rating (e.g. 'fastest WR on the Ravens').",
                ephemeral=True,
            )
            raise _EarlyReturn()

        # ── Execute with progressive retry ─────────────────────
        rows, sql, error, attempt, _warnings = await retry_sql(sql, scout_schema)
        if error:
            await interaction.followup.send(
                "⚠️ Scout query failed after retry. Try rephrasing!",
                ephemeral=True,
            )
            raise _EarlyReturn()

        # ── Generate scouting report ──────────────────────────
        results_str = json.dumps(rows[:20], indent=2)
        if len(results_str) > 2500:
            results_str = results_str[:2500] + "\n... (truncated)"

        answer_prompt = f"""{get_persona('analytical')}

You are in Scout mode — analyzing Madden player ratings and abilities.

A user asked: "{q}"

Scouting data ({len(rows)} players):
{results_str}

RESPONSE GUIDELINES:
- Lead with the direct answer to the question.
- Use **bold** for player names, ratings, and dev traits (Discord markdown).
- Compare players when relevant — highlight standout ratings.
- Mention dev trait (Normal/Star/Superstar/XFactor) as it heavily impacts value.
- Keep it under 300 words. Make it feel like a real scouting report.
"""

        answer_result = await atlas_ai.generate(answer_prompt, tier=Tier.HAIKU, max_tokens=400)
        answer = answer_result.text or "No scouting data found."

        # ── Store conversation turn ───────────────────────────
        if _add_conversation_turn:
            await _add_conversation_turn(
                interaction.user.id, q, answer, sql=sql or "", source="codex"
            )

        footer_parts = [f"🔍 {len(rows)} players analyzed"]
        if team_name:
            footer_parts.append(f"🏈 {team_name}")
        if attempt == 2:
            footer_parts.append("⚠️ Self-corrected")
        elif attempt == 3:
            footer_parts.append("🧠 Opus rescue")
        footer_parts.append("ATLAS™ Oracle · Scout Mode")

        return answer, {
            "title": "🎯 ATLAS Intelligence — Player Scout",
            "color": AtlasColors.TSL_BLUE,
            "footer": " · ".join(footer_parts),
        }


class StrategyRoomModal(_OracleIntelModal, title="🧠 Ask ATLAS — Strategy Room"):
    """Strategy mode: trade advice, roster tips, and game strategy using TSL context."""

    question = discord.ui.TextInput(
        label="Your Strategy Question",
        placeholder="e.g. Should I trade my WR1 for picks? How to beat a 3-4 defense?",
        required=True,
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    async def _generate(self, interaction: discord.Interaction) -> tuple[str, dict]:
        q = self.question.value.strip()

        # Build TSL context from live data
        context_parts = []

        if not dm.df_standings.empty:
            top_teams = dm.df_standings.head(10)
            standings_lines = []
            for _, row in top_teams.iterrows():
                name = row.get("teamName", "?")
                wins = row.get("totalWins", "0")
                losses = row.get("totalLosses", "0")
                standings_lines.append(f"  {name}: {wins}-{losses}")
            context_parts.append("CURRENT STANDINGS (Top 10):\n" + "\n".join(standings_lines))

        if not dm.df_teams.empty:
            team_lines = []
            for _, row in dm.df_teams.iterrows():
                name = row.get("nickName", "?")
                ovr = row.get("ovrRating", "?")
                owner = row.get("userName", "?")
                team_lines.append(f"  {name} (OVR {ovr}) — {owner}")
            context_parts.append("TEAM RATINGS:\n" + "\n".join(team_lines[:16]))

        tsl_context = "\n\n".join(context_parts) if context_parts else ""

        system_instruction = get_persona("analytical")
        contents = f"TSL CONTEXT:\n{tsl_context}\n\nUSER QUESTION: {q}" if tsl_context else q
        result = await atlas_ai.generate_with_search(contents, system=system_instruction)

        answer = result.text or "ATLAS couldn't formulate a strategy for that one."

        footer = "Strategy Room · TSL context + web search · ATLAS™ Oracle"
        if result.fallback_used:
            footer += "  ·  ⚡ via Gemini fallback"

        return answer, {
            "title": "🧠 ATLAS Intelligence — Strategy Room",
            "color": AtlasColors.SUCCESS,
            "footer": footer,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  CASCADING SELECT VIEWS
# ─────────────────────────────────────────────────────────────────────────────

class ClutchMarginView(discord.ui.View):
    """Clutch margin selector, shown immediately after clicking ⚡ Clutch."""

    def __init__(self, caller_team: str = ""):
        super().__init__(timeout=120)
        self.caller_team = caller_team

    @discord.ui.select(
        placeholder="Select clutch margin...",
        options=[
            discord.SelectOption(label="≤1 pt  — Nail-biters only",  value="1"),
            discord.SelectOption(label="≤3 pts — Super Clutch",       value="3"),
            discord.SelectOption(label="≤7 pts — Standard",           value="7", default=True),
            discord.SelectOption(label="≤14 pts — Broad",             value="14"),
        ],
    )
    async def margin_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = _build_clutch_embed(margin=int(select.values[0]), highlight_team=self.caller_team)
        await interaction.followup.send(embed=embed, ephemeral=True)


class PlayerPositionView(discord.ui.View):
    """Step 1 of Player Leaders: pick a position group."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Select position group...",
        options=[
            discord.SelectOption(label="QB — Quarterbacks",   value="QB", emoji="🎯"),
            discord.SelectOption(label="RB — Running Backs",  value="RB", emoji="🏃"),
            discord.SelectOption(label="WR — Wide Receivers", value="WR", emoji="🙌"),
            discord.SelectOption(label="TE — Tight Ends",     value="TE", emoji="🔵"),
            discord.SelectOption(label="DL — Defensive Line", value="DL", emoji="💪"),
            discord.SelectOption(label="LB — Linebackers",    value="LB", emoji="⚡"),
            discord.SelectOption(label="DB — Defensive Backs",value="DB", emoji="🛡️"),
        ],
    )
    async def position_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        position = select.values[0]
        stats    = PLAYER_STAT_MAP.get(position, [])
        await interaction.response.send_message(
            f"**🎯 {position} — Choose a stat category:**",
            view=PlayerStatView(position, stats),
            ephemeral=True,
        )


class PlayerStatView(discord.ui.View):
    """Step 2: pick a stat after choosing position group."""

    def __init__(self, position: str, stats: list[tuple[str, str]]):
        super().__init__(timeout=120)
        self.position = position
        # Dynamically set options based on position
        self.stat_select.options = [
            discord.SelectOption(label=label, value=col) for col, label in stats[:25]
        ]

    @discord.ui.select(
        placeholder="Select stat...",
        options=[discord.SelectOption(label="Loading...", value="_placeholder")],
    )
    async def stat_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        stat_col   = select.values[0]
        stat_label = next(
            (label for col, label in PLAYER_STAT_MAP.get(self.position, []) if col == stat_col),
            stat_col,
        )
        await interaction.response.defer(thinking=True, ephemeral=True)

        embed, player_names = _build_player_leaders_embed(self.position, stat_col, stat_label)
        if player_names:
            await interaction.followup.send(embed=embed, view=PlayerDrillView(player_names), ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


class PlayerDrillView(discord.ui.View):
    """Step 3: select a player from the top-10 for a Hot/Cold deep dive."""

    def __init__(self, player_names: list[str]):
        super().__init__(timeout=120)
        self.player_select.options = [
            discord.SelectOption(label=name[:100], value=name[:100])
            for name in player_names[:25]
        ]

    @discord.ui.select(placeholder="Drill into a player's Hot/Cold...", options=[])
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        player_name = select.values[0]
        await interaction.response.defer(thinking=True, ephemeral=True)

        data = ig.get_hot_cold(player_name, last_n=3)
        if "error" in data:
            await interaction.followup.send(f"❌ {data['error']}", ephemeral=True)
            return

        embed = _build_hotcold_single(data)
        await interaction.followup.send(embed=embed, ephemeral=True)


class DraftSeasonView(discord.ui.View):
    """Season selector for Draft History."""

    def __init__(self):
        super().__init__(timeout=120)
        options = [discord.SelectOption(label="All Seasons Overview", value="0")] + [
            discord.SelectOption(label=f"Season {s}", value=str(s))
            for s in range(1, dm.CURRENT_SEASON + 1)
        ]
        self.season_select.options = options[-25:]

    @discord.ui.select(placeholder="Select season...", options=[])
    async def season_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(thinking=True, ephemeral=True)
        season = int(select.values[0])
        if season == 0:
            embed  = await _build_draft_overview_embed()
            embeds = [embed]
        else:
            embeds = await _build_draft_season_embeds(season)
        await interaction.followup.send(embeds=embeds, view=DraftSeasonView(), ephemeral=True)


class WeekRecapView(discord.ui.View):
    """Week selector for Recap."""

    def __init__(self):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(label=f"Week {w}", value=str(w))
            for w in range(1, dm.CURRENT_WEEK + 1)
        ]
        # Only send the 25 most recent weeks (Discord limit)
        self.week_select.options = options[-25:] if options else [
            discord.SelectOption(label="Week 1", value="1")
        ]

    @discord.ui.select(placeholder="Select a week...", options=[])
    async def week_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = _build_recap_embed(week=int(select.values[0]))
        await interaction.followup.send(embed=embed, view=WeekRecapView(), ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
#  SEASON RECAP MODAL
# ─────────────────────────────────────────────────────────────────────────────

class SeasonRecapModal(discord.ui.Modal, title="📅 Season Recap"):
    season = discord.ui.TextInput(
        label="Season Number",
        placeholder=f"1–{dm.CURRENT_SEASON}",
        required=True,
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            season_num = int(self.season.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Please enter a valid season number.", ephemeral=True)
            return

        if season_num < 1 or season_num > dm.CURRENT_SEASON:
            await interaction.followup.send(
                f"❌ Valid seasons are **1** through **{dm.CURRENT_SEASON}**.", ephemeral=True
            )
            return

        if not _HISTORY_OK:
            await interaction.followup.send("⚠️ Historical database not available.", ephemeral=True)
            return

        rows, _ = run_sql("""
            SELECT winner_user, loser_user, winner_team, loser_team,
                   homeScore, awayScore, weekIndex
            FROM games
            WHERE seasonIndex=? AND stageIndex='1' AND status IN ('2','3')
            ORDER BY CAST(weekIndex AS INTEGER)
        """, (str(season_num),))

        wins: Counter = Counter()
        losses: Counter = Counter()
        for r in rows or []:
            if r.get("winner_user"):
                wins[r["winner_user"]] += 1
            if r.get("loser_user"):
                losses[r["loser_user"]] += 1

        leaderboard = sorted(wins.keys(), key=lambda u: wins[u], reverse=True)[:5]
        top_str = "\n".join(f"**{u}**: {wins[u]}W–{losses.get(u, 0)}L" for u in leaderboard)

        embed = discord.Embed(
            title=f"📅 TSL Season {season_num} Recap",
            color=ATLAS_GOLD,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        if not rows:
            embed.description = f"No completed regular-season games found for Season {season_num}."
        else:
            embed.add_field(
                name="🏆 Top 5 Records",
                value=top_str or "No data",
                inline=False,
            )
            embed.add_field(
                name="📊 Games Played",
                value=f"{len(rows)} regular-season games",
                inline=False,
            )

            # ATLAS AI flair — non-blocking
            try:
                prompt = (
                    f"{get_persona('casual')}\n\nWrite a vivid 3–4 sentence recap of TSL Season {season_num}. "
                    f"Total games: {len(rows)}. Top records:\n{top_str}\n"
                    f"Highlight who dominated, any notable storylines, and tease the playoff picture. "
                    f"Keep it punchy and entertaining."
                )
                flair = await atlas_ai.generate(prompt, tier=Tier.HAIKU, max_tokens=300)
                embed.add_field(
                    name="🎙️ ATLAS Echo",
                    value=flair.text[:900],
                    inline=False,
                )
            except Exception:
                pass

        embed.set_footer(
            text=f"ATLAS™ Oracle · Season {season_num} · Regular season only",
            icon_url=ATLAS_ICON_URL,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
#  HUB VIEW — persistent button navigation
#  timeout=None + custom_id on every button = survives bot restarts
# ─────────────────────────────────────────────────────────────────────────────

class HubView(discord.ui.View):
    """
    Persistent 13-button navigation view.
    - timeout=None              buttons never expire on their own
    - custom_id on each button  bot can re-register on restart
    - All drill-downs ephemeral no channel flood
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    # ── Row 0: League-wide ────────────────────────────────────────────────────

    @discord.ui.button(
        label="🔥 Hot/Cold", style=discord.ButtonStyle.secondary,
        row=0, custom_id="hub:hotcold",
    )
    async def btn_hotcold(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed, player_names = _build_hotcold_league()
        if player_names:
            await interaction.followup.send(embed=embed, view=PlayerDrillView(player_names), ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="⚡ Clutch", style=discord.ButtonStyle.secondary,
        row=0, custom_id="hub:clutch",
    )
    async def btn_clutch(self, interaction: discord.Interaction, _b: discord.ui.Button):
        _, caller_team = _resolve_owner_team(interaction)
        await interaction.response.send_message(
            "**⚡ Clutch Rankings — Select a margin:**",
            view=ClutchMarginView(caller_team=caller_team),
            ephemeral=True,
        )

    @discord.ui.button(
        label="📊 Power", style=discord.ButtonStyle.secondary,
        row=0, custom_id="hub:power",
    )
    async def btn_power(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send(embed=_build_power_embed(), ephemeral=True)

    @discord.ui.button(
        label="🏆 Standings", style=discord.ButtonStyle.secondary,
        row=0, custom_id="hub:standings",
    )
    async def btn_standings(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        _, caller_team = _resolve_owner_team(interaction)
        await interaction.followup.send(
            embed=_build_standings_embed(caller_team=caller_team), ephemeral=True
        )

    # ── Row 1: Personal / Matchups ────────────────────────────────────────────

    @discord.ui.button(
        label="👤 My Profile", style=discord.ButtonStyle.primary,
        row=1, custom_id="hub:profile",
    )
    async def btn_profile(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = await _build_owner_embed(interaction.user, interaction.guild)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="🆚 Head-to-Head", style=discord.ButtonStyle.primary,
        row=1, custom_id="oracle:h2h",
    )
    async def btn_h2h(self, interaction: discord.Interaction, _b: discord.ui.Button):
        # Modal handles its own defer — send modal directly, no defer before
        # Pre-fill with resolved db_username so user sees their game identity
        default_name = interaction.user.name
        if _resolve_db_username_fn:
            resolved = _resolve_db_username_fn(interaction.user.id)
            if resolved:
                default_name = resolved
        await interaction.response.send_modal(H2HModal(default_owner=default_name))

    @discord.ui.button(
        label="📅 Recap", style=discord.ButtonStyle.secondary,
        row=1, custom_id="hub:recap",
    )
    async def btn_recap(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = _build_recap_embed()
        await interaction.followup.send(embed=embed, view=WeekRecapView(), ephemeral=True)

    @discord.ui.button(
        label="📜 Draft", style=discord.ButtonStyle.secondary,
        row=1, custom_id="hub:draft",
    )
    async def btn_draft(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = await _build_draft_overview_embed()
        await interaction.followup.send(embed=embed, view=DraftSeasonView(), ephemeral=True)

    # ── Row 2: Deep Dives ─────────────────────────────────────────────────────

    @discord.ui.button(
        label="🎯 Players", style=discord.ButtonStyle.secondary,
        row=2, custom_id="hub:players",
    )
    async def btn_players(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_message(
            "**🎯 Player Leaders — Select a position group:**",
            view=PlayerPositionView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="🏈 Team Stats", style=discord.ButtonStyle.secondary,
        row=2, custom_id="hub:teamstats",
    )
    async def btn_teamstats(self, interaction: discord.Interaction, _b: discord.ui.Button):
        username, caller_team = _resolve_owner_team(interaction)
        if caller_team:
            await interaction.response.defer(thinking=True, ephemeral=True)
            embed = await _build_team_card_snapshot(
                caller_team, caller_team=caller_team, owner_username=username
            )
            await interaction.followup.send(
                embed=embed,
                view=TeamCardView(caller_team, caller_team=caller_team, owner_username=username),
                ephemeral=True,
            )
        else:
            await interaction.response.send_modal(TeamSearchModal())

    @discord.ui.button(
        label="🏛️ All-Time", style=discord.ButtonStyle.secondary,
        row=2, custom_id="hub:alltime",
    )
    async def btn_alltime(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send(embed=_build_alltime_embed(), ephemeral=True)

    @discord.ui.button(
        label="📅 Season Recap", style=discord.ButtonStyle.success,
        row=2, custom_id="oracle:season_recap",
    )
    async def btn_season_recap(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(SeasonRecapModal())

    @discord.ui.button(
        label="🔮 Oracle Hub", style=discord.ButtonStyle.success,
        row=3, custom_id="hub:ask",
    )
    async def btn_ask(self, interaction: discord.Interaction, _b: discord.ui.Button):
        embed = discord.Embed(
            title="🔮 ATLAS Oracle Hub",
            description=(
                "**Choose your intelligence mode:**\n\n"
                "📊 **TSL League** — Query the TSL history database\n"
                "🌐 **Open Intel** — General knowledge + web search\n"
                "🏈 **Sports Intel** — Real-world NFL stats & news\n"
                "🎯 **Player Scout** — Madden ratings, dev traits, abilities\n"
                "🧠 **Strategy** — Trade advice, roster tips, game strategy"
            ),
            color=AtlasColors.TSL_GOLD,
        )
        embed.set_footer(text="ATLAS™ Oracle Module")
        await interaction.response.send_message(
            embed=embed,
            view=OracleHubView(),
            ephemeral=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  COG
# ─────────────────────────────────────────────────────────────────────────────

class StatsHubCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Re-register HubView on startup so persistent buttons survive restarts
        self.bot.add_view(HubView(bot))

    stats = app_commands.Group(
        name="stats",
        description="TSL Stats Hub — stats, profiles, history, and AI queries.",
    )

    # ── /stats hub ─────────────────────────────────────────────────────────────
    @stats.command(name="hub", description="Open the TSL Stats Hub (public).")
    async def hub(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = _build_hub_embed()
        view  = HubView(self.bot)
        await interaction.followup.send(embed=embed, view=view)  # PUBLIC — no ephemeral

    # ── /stats team ────────────────────────────────────────────────────────────
    @stats.command(name="team", description="Look up any team's stat card.")
    @app_commands.describe(team="Team name (partial match OK)")
    async def team(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        username, caller_team = _resolve_owner_team(interaction)
        team_name = team.strip()
        embed = await _build_team_card_snapshot(
            team_name, caller_team=caller_team, owner_username=username
        )
        await interaction.followup.send(
            embed=embed,
            view=TeamCardView(team_name, caller_team=caller_team, owner_username=username),
            ephemeral=True,
        )

    # ── /stats owner ───────────────────────────────────────────────────────────
    @stats.command(name="owner", description="Look up an owner's profile card.")
    @app_commands.describe(user="Discord user (leave blank for yourself)")
    async def owner(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        target = user or interaction.user
        embed  = await _build_owner_embed(target, interaction.guild)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /stats hotcold ────────────────────────────────────────────────────────

    @stats.command(name="hotcold", description="Hot/Cold report — league-wide or single player trend.")
    @app_commands.describe(player="Player name (leave blank for league-wide report)")
    async def hotcold(self, interaction: discord.Interaction, player: Optional[str] = None):
        if player:
            tier = _get_user_tier(self.bot, interaction.user.id)
            if tier not in ["Pro", "Elite"]:
                await interaction.response.send_message("🔒 **Pro Tier Required.** Use `/membership info` to unlock deep-dive player trend analysis.", ephemeral=True)
                return
            
            await interaction.response.defer(ephemeral=True, thinking=True)
            data = ig.get_hot_cold(player.strip(), last_n=3)
            if "error" in data:
                return await interaction.followup.send(
                    f"❌ {data['error']}\n\nTry a full or partial player name.",
                    ephemeral=True
                )
            embed = _build_hotcold_single(data)
            view = AnalyticsNav(self.bot, interaction.user.id)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            embed, player_names = _build_hotcold_league()
            if player_names:
                await interaction.followup.send(embed=embed, view=PlayerDrillView(player_names), ephemeral=True)
            else:
                view = AnalyticsNav(self.bot, interaction.user.id)
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /stats clutch ─────────────────────────────────────────────────────────

    @stats.command(name="clutch", description="Clutch rankings — records in close games (Pro/Elite).")
    @app_commands.describe(margin="Point margin for 'clutch' games (default: 7)")
    async def clutch(self, interaction: discord.Interaction, margin: Optional[int] = 7):
        tier = _get_user_tier(self.bot, interaction.user.id)
        if tier not in ["Pro", "Elite"]:
            await interaction.response.send_message("🔒 **Pro Tier Required.** Use `/membership info` to unlock Clutch Rankings.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_clutch_embed(margin=margin or 7)
        view  = AnalyticsNav(self.bot, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /stats draft ──────────────────────────────────────────────────────────

    @stats.command(name="draft", description="Draft class report — grades, steals, busts.")
    @app_commands.describe(season="Season number (leave blank for all-class comparison)")
    async def draft(self, interaction: discord.Interaction, season: Optional[int] = None):
        if season:
            tier = _get_user_tier(self.bot, interaction.user.id)
            if tier not in ["Pro", "Elite"]:
                await interaction.response.send_message("🔒 **Pro Tier Required.** Use `/membership info` to unlock specific Draft Class deep-dives.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            embeds = await _build_draft_embed(season)
            view   = AnalyticsNav(self.bot, interaction.user.id)
            await interaction.followup.send(embeds=embeds, view=view, ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            embed = await _build_draft_comparison_embed()
            view  = AnalyticsNav(self.bot, interaction.user.id)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /stats power ──────────────────────────────────────────────────────────

    @stats.command(name="power", description="Live composite power rankings for all 32 teams.")
    async def power(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_power_embed()
        view  = AnalyticsNav(self.bot, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /stats recap ──────────────────────────────────────────────────────────

    @stats.command(name="recap", description="Weekly game recap with scores and highlights.")
    @app_commands.describe(week="Week number (leave blank for most recent)")
    async def recap(self, interaction: discord.Interaction, week: Optional[int] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_recap_embed(week=week)
        view  = AnalyticsNav(self.bot, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    # ── /stats player ──────────────────────────────────────────────────────────
    @stats.command(name="player", description="Hot/Cold breakdown for a specific player.")
    @app_commands.describe(player="Player name (partial match OK)")
    async def player(self, interaction: discord.Interaction, player: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        data = ig.get_hot_cold(player.strip(), last_n=3)
        if "error" in data:
            await interaction.followup.send(
                f"❌ {data['error']}\n\nTry a full or partial player name.", ephemeral=True
            )
            return
        embed = _build_hotcold_single(data)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /oracle ────────────────────────────────────────────────────────────────

    @app_commands.command(name="oracle", description="🔮 ATLAS Oracle Hub — Ask questions across 5 intelligence modes.")
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def oracle(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔮 ATLAS Oracle Hub",
            description=(
                "**Choose your intelligence mode:**\n\n"
                "📊 **TSL League** — Query the TSL history database\n"
                "🌐 **Open Intel** — General knowledge + web search\n"
                "🏈 **Sports Intel** — Real-world NFL stats & news\n"
                "🎯 **Player Scout** — Madden ratings, dev traits, abilities\n"
                "🧠 **Strategy** — Trade advice, roster tips, game strategy"
            ),
            color=AtlasColors.TSL_GOLD,
        )
        embed.set_author(
            name="ATLAS · Autonomous TSL League Administration System",
            icon_url=ATLAS_ICON_URL
        )
        embed.set_footer(text="ATLAS™ Oracle Module · Pick a mode to begin")
        await interaction.response.send_message(
            embed=embed,
            view=OracleHubView(),
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  SETUP
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(StatsHubCog(bot))
    print("ATLAS: Oracle Module loaded. 🔬")
