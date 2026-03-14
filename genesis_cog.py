"""
genesis_cog.py — ATLAS · Genesis Module v1.0
─────────────────────────────────────────────────────────────────────────────
ATLAS Genesis is the roster management and transaction system.

Consolidated from: trade_center_cog, parity_cog

Register in bot.py setup_hook():
    await bot.load_extension("genesis_cog")

Slash commands:
  /genesis                 — Open the ATLAS Genesis Hub
  /trade                   — Open the TSL Trade Center (autocomplete team select)
  /tradelist               — [Commissioner] List pending trades
  /runlottery              — [Admin] Run the draft lottery
  /orphanfranchise         — [Admin] Flag/unflag a team as orphaned

Hub-only tools (via /genesis buttons):
  Trade Lookup, Dev Traits, Ability Audit, Ability Check,
  Cornerstone Designation, Contract Check, Lottery Standings
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

# ── Unified imports ───────────────────────────────────────────────────────────
import asyncio
import datetime
import json
import os
import random
import re
import traceback
import uuid
from difflib import SequenceMatcher

import discord
from discord import app_commands
from discord.ext import commands

import data_manager as dm
import trade_engine as te
from player_picker import PlayerPickerView, make_multi_picker

# Image renderer — optional, falls back to embed if unavailable
try:
    import card_renderer as cr
    _IMAGE_RENDER = True
except ImportError:
    cr = None
    _IMAGE_RENDER = False

# Ability engine — required for RosterHub ability audit/check buttons
try:
    import ability_engine as ae
    _AE_AVAILABLE = True
except ImportError:
    ae = None
    _AE_AVAILABLE = False

# ── Shared config ─────────────────────────────────────────────────────────────
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
ATLAS_ICON_URL = "https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"

try:
    from setup_cog import get_channel_id as _get_channel_id
except ImportError:
    def _get_channel_id(key: str, guild_id: int | None = None) -> int | None:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  GENESIS · TRADE CENTER
# ══════════════════════════════════════════════════════════════════════════════


# ── Player dict sanitizer ─────────────────────────────────────────────────────

def _sanitize_player(p: dict) -> dict:
    """
    Defensively clean a player dict before it reaches trade_engine.
    Converts NaN / None numeric fields to safe defaults so int() never explodes.

    Specifically fixes:
      ValueError: cannot convert float NaN to integer   (trade_engine.py line ~125)
      Caused by: int(player.get("age", 25)) when age is NaN from the CSV export.
    """
    import math

    def _safe_int(val, default):
        if val is None:
            return default
        try:
            f = float(val)
            return default if math.isnan(f) or math.isinf(f) else int(f)
        except (ValueError, TypeError):
            return default

    def _safe_float(val, default):
        if val is None:
            return default
        try:
            f = float(val)
            return default if math.isnan(f) or math.isinf(f) else f
        except (ValueError, TypeError):
            return default

    out = dict(p)
    out["age"]           = _safe_int(p.get("age"),           25)
    out["yearsPro"]      = _safe_int(p.get("yearsPro"),      1)
    # overallRating: use playerBestOvr if overallRating is absent/zero
    best_ovr = p.get("playerBestOvr")
    raw_ovr  = p.get("overallRating")
    if not raw_ovr or (isinstance(raw_ovr, float) and math.isnan(float(raw_ovr if raw_ovr else 0))):
        raw_ovr = best_ovr
    out["overallRating"] = _safe_int(raw_ovr, 70)
    out["playerBestOvr"] = out["overallRating"]   # keep in sync
    out["draftRound"]    = _safe_int(p.get("draftRound"),    7)
    out["draftPick"]     = _safe_int(p.get("draftPick"),     32)
    out["rookieYear"]    = _safe_int(p.get("rookieYear"),    dm.CURRENT_SEASON)
    out["capPercent"]    = _safe_float(p.get("capPercent"),  0.0)
    out["capHit"]        = _safe_float(p.get("capHit"),      0.0)
    # Ensure teamId is int-safe
    try:
        out["teamId"] = int(float(p.get("teamId", 0) or 0))
    except (ValueError, TypeError):
        out["teamId"] = 0
    return out


def _serialize_player(p: dict) -> dict:
    """
    Extract only the fields the card renderer needs from a full player dict.
    Stored in trade_state.json so _update_status can re-render without
    re-resolving from raw text (which fails for picker-mode trades).
    """
    return {
        "firstName":     p.get("firstName", ""),
        "lastName":      p.get("lastName", ""),
        "pos":           p.get("pos", p.get("position", "?")),
        "overallRating": p.get("overallRating") or p.get("playerBestOvr") or 0,
        "playerBestOvr": p.get("overallRating") or p.get("playerBestOvr") or 0,
        "age":           p.get("age", "?"),
        "dev":           p.get("dev", "Normal"),
        "teamId":        p.get("teamId", 0),
    }


# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
STATE_PATH     = os.path.join(os.path.dirname(__file__), "trade_state.json")

# ── Channel routing via setup_cog (ID-based, rename-proof) ───────────────────
def _trades_channel_id() -> int | None:
    """Resolve #trades channel ID at call time."""
    return _get_channel_id("trades")

# ── Persistence ───────────────────────────────────────────────────────────────

_trades: dict[str, dict] = {}


def _load_trade_state():
    global _trades
    if os.path.isfile(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                _trades = json.load(f)
        except Exception as e:
            print(f"[trade_center] State load error: {e}")


def _save_trade_state():
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_trades, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f"[trade_center] State save error: {e}")


# ── Team helpers ──────────────────────────────────────────────────────────────

def _get_all_teams() -> list[dict]:
    """Return sorted list of team dicts from dm.df_teams."""
    if dm.df_teams.empty:
        return []
    teams = dm.df_teams.to_dict(orient="records")
    return sorted(teams, key=lambda t: str(t.get("nickName", t.get("displayName", ""))))


def _find_team(name: str) -> dict | None:
    name_l = name.strip().lower()
    for t in _get_all_teams():
        nick    = str(t.get("nickName",    "")).lower()
        display = str(t.get("displayName", "")).lower()
        abbr    = str(t.get("abbrName",    "")).lower()
        if name_l in (nick, display, abbr) or name_l in nick or name_l in display:
            return t
    return None


def _team_label(t: dict) -> str:
    nick  = t.get("nickName",    "")
    owner = t.get("userName",    "")
    return f"{nick} ({owner})" if owner else nick


# ── Player fuzzy search ───────────────────────────────────────────────────────

def _fuzzy_score(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _find_player(name: str, team_id: int | None = None) -> tuple[dict | None, list[dict]]:
    """
    Search _players_cache for best match.
    Returns (best_match, [close_candidates]) where close_candidates are
    other plausible matches for disambiguation.
    team_id: if provided, prefer players on that team.
    """
    query  = name.strip().lower()
    cache  = dm._players_cache
    if not cache:
        # Fallback to df_players
        if not dm.df_players.empty:
            cache = dm.df_players.to_dict(orient="records")
        else:
            return None, []

    scored = []
    for p in cache:
        full = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip().lower()
        last = str(p.get("lastName", "")).lower()
        score = max(_fuzzy_score(query, full), _fuzzy_score(query, last))
        # Boost exact partial match
        if query in full:
            score = max(score, 0.85)
        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] < 0.45:
        return None, []

    best  = scored[0][1]
    close = [p for s, p in scored[1:5] if s >= 0.60]

    # If team_id given, prefer same-team match among top results
    if team_id:
        for score, p in scored[:8]:
            if score >= 0.60 and int(p.get("teamId", 0)) == team_id:
                best = p
                close = [x for _, x in scored[:5] if x is not p][:4]
                break

    return best, close


def _parse_picks(raw: str, team_id: int) -> tuple[list[dict], list[str]]:
    """
    Parse pick strings like "S7R1, S8R2, S6R3".
    Returns (picks_list, errors_list).
    """
    picks, errors = [], []
    if not raw.strip():
        return picks, errors

    current_season = dm.CURRENT_SEASON if hasattr(dm, "CURRENT_SEASON") else 1

    for token in raw.split(","):
        token = token.strip().upper()
        if not token:
            continue
        # Match patterns: S7R1 / S7 R1 / 1ST / 2ND / R1S7 etc.
        m = re.search(r"S(\d+)[^\d]*R(\d+)|R(\d+)[^\d]*S(\d+)", token)
        if m:
            if m.group(1):
                season, rnd = int(m.group(1)), int(m.group(2))
            else:
                season, rnd = int(m.group(4)), int(m.group(3))
        else:
            # Fallback: look for just round number or ordinal
            m2 = re.search(r"(\d)(ST|ND|RD|TH)|R(\d)", token)
            if m2:
                rnd    = int(m2.group(1) or m2.group(3))
                season = current_season + 1
            else:
                errors.append(f"`{token}` — unrecognised format (use **S7R1** for Season 7 Round 1)")
                continue

        if rnd < 1 or rnd > 7:
            errors.append(f"`{token}` — round must be 1–7")
            continue

        picks.append({
            "round":   rnd,
            "year":    season,
            "team_id": team_id,
            "slot":    16,
        })

    return picks, errors


def _resolve_assets(
    players_raw: str,
    picks_raw: str,
    team_id: int,
) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """
    Resolve text input → player dicts + pick dicts.
    Returns: (players, picks, not_found_errors, warnings)
    """
    players, not_found, warnings = [], [], []

    if players_raw.strip():
        for name in players_raw.split(","):
            name = name.strip()
            if not name:
                continue
            match, candidates = _find_player(name, team_id)
            if match is None:
                not_found.append(f"❌ `{name}` — no player found")
            else:
                full = f"{match.get('firstName','')} {match.get('lastName','')}".strip()
                if name.lower() != full.lower():
                    # Fuzzy matched — note what we resolved to
                    ovr_display = match.get('overallRating') or match.get('playerBestOvr') or '?'
                    warnings.append(f"🔍 `{name}` → matched **{full}** ({match.get('pos','?')} OVR {ovr_display})")
                players.append(match)

    picks, pick_errors = _parse_picks(picks_raw, team_id)
    not_found.extend(pick_errors)

    # Sanitize all player dicts — converts NaN numeric fields to safe defaults
    # Prevents ValueError in trade_engine when CSV exports contain NaN age/OVR
    players = [_sanitize_player(p) for p in players]

    return players, picks, not_found, warnings


# ── AI Commentary ─────────────────────────────────────────────────────────────

async def _get_ai_commentary(result: te.TradeEvalResult, team_a_name: str, team_b_name: str) -> str:
    """Get ATLAS Echo trade commentary via Gemini."""
    if not GEMINI_API_KEY:
        return "_AI commentary unavailable._"
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)

        notes_text = "\n".join(result.notes) if result.notes else "No flags."
        prompt = (
            f"You are ATLAS Echo, the TSL league intelligence and trade analysis system. "
            f"Give a 2-sentence ruthless, sharp trade analysis. No fluff.\n\n"
            f"Trade: {team_a_name} ({result.side_a_value:,} pts) vs {team_b_name} ({result.side_b_value:,} pts)\n"
            f"Delta: {result.delta_pct:.1f}% | Band: {result.band}\n"
            f"Notes: {notes_text}\n\n"
            f"Side A assets:\n{''.join(result.breakdown_a[:15])}\n"
            f"Side B assets:\n{''.join(result.breakdown_b[:15])}"
        )

        response = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.0-flash",
                config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=120),
                contents=prompt,
            )
        )
        return response.text.strip()
    except Exception as e:
        print(f"[trade_center] AI commentary error: {e}")
        return "_AI commentary unavailable._"


# ── Trade Card Builder ────────────────────────────────────────────────────────

def _player_line(p: dict) -> str:
    name = f"{p.get('firstName','')} {p.get('lastName','')}".strip()
    pos  = p.get("pos", p.get("position", "?"))
    ovr  = p.get("overallRating") or p.get("playerBestOvr") or "?"
    age  = p.get("age", "?")
    return f"**{name}** | {pos} · OVR {ovr} · Age {age}"


def _pick_line(pk: dict) -> str:
    rnd  = pk.get("round", "?")
    year = pk.get("year", "?")
    ordinals = {1: "1st", 2: "2nd", 3: "3rd"}
    rnd_label = ordinals.get(rnd, f"{rnd}th")
    return f"**S{year} {rnd_label}-Round Pick**"


BAND_CONFIG = {
    "GREEN":  (discord.Color.from_rgb(34, 197, 94),   "🟢", "FAIR"),
    "YELLOW": (discord.Color.from_rgb(234, 179, 8),   "🟡", "REVIEW"),
    "RED":    (discord.Color.from_rgb(239, 68, 68),   "🔴", "LOPSIDED"),
}


def _build_trade_card(
    trade_id: str,
    team_a: dict, team_b: dict,
    players_a: list, picks_a: list,
    players_b: list, picks_b: list,
    result: te.TradeEvalResult,
    ai_commentary: str,
    warnings: list[str],
    proposer_id: int,
    status: str = "pending",
) -> discord.Embed:

    color, band_emoji, band_label = BAND_CONFIG.get(result.band, (discord.Color.greyple(), "⚪", "UNKNOWN"))

    a_name = _team_label(team_a)
    b_name = _team_label(team_b)

    # Status header
    status_map = {
        "pending":  ("⏳", "Pending Review"),
        "approved": ("✅", "APPROVED"),
        "rejected": ("❌", "REJECTED"),
        "countered": ("🔄", "Counter Offered"),
    }
    s_emoji, s_label = status_map.get(status, ("⏳", "Pending"))

    embed = discord.Embed(
        title=f"💱 Trade Proposal — {s_emoji} {s_label}",
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    # ── Side A ────────────────────────────────────────────────────────────────
    a_lines = []
    for p in players_a:
        a_lines.append(_player_line(p))
    for pk in picks_a:
        a_lines.append(_pick_line(pk))
    if not a_lines:
        a_lines = ["_Nothing_"]

    # ── Side B ────────────────────────────────────────────────────────────────
    b_lines = []
    for p in players_b:
        b_lines.append(_player_line(p))
    for pk in picks_b:
        b_lines.append(_pick_line(pk))
    if not b_lines:
        b_lines = ["_Nothing_"]

    embed.add_field(
        name=f"📤 {a_name} sends",
        value="\n".join(a_lines),
        inline=True,
    )
    embed.add_field(
        name=f"📥 {b_name} sends",
        value="\n".join(b_lines),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

    # ── Valuation summary ─────────────────────────────────────────────────────
    delta_arrow = "▲" if result.side_a_value > result.side_b_value else "▼"
    favored     = a_name if result.side_a_value > result.side_b_value else b_name

    # OVR delta
    a_ovr = sum(int(p.get("overallRating") or p.get("playerBestOvr") or 0) for p in players_a)
    b_ovr = sum(int(p.get("overallRating") or p.get("playerBestOvr") or 0) for p in players_b)
    ovr_delta = a_ovr - b_ovr

    embed.add_field(
        name="📊 Valuation",
        value=(
            f"`{a_name[:20]}` **{result.side_a_value:,} pts**\n"
            f"`{b_name[:20]}` **{result.side_b_value:,} pts**\n"
            f"Delta: **{result.delta_pct:.1f}%** {delta_arrow} favors {favored.split('(')[0].strip()}"
        ),
        inline=True,
    )
    embed.add_field(
        name="⚡ OVR Delta",
        value=(
            f"{'➕' if ovr_delta >= 0 else '➖'} **{abs(ovr_delta)} OVR**\n"
            f"{a_name.split('(')[0].strip()} side: {a_ovr} total\n"
            f"{b_name.split('(')[0].strip()} side: {b_ovr} total"
        ),
        inline=True,
    )
    embed.add_field(
        name=f"{band_emoji} Fairness Band",
        value=(
            f"**{band_label}**\n"
            f"{'✅ Within legal range' if result.band != 'RED' else '🚨 Outside legal range'}\n"
            f"{'Commissioner approval required' if result.band in ('YELLOW','RED') else 'Auto-eligible'}"
        ),
        inline=True,
    )

    # ── Pick values ───────────────────────────────────────────────────────────
    all_picks = picks_a + picks_b
    if all_picks:
        pick_lines = []
        for pk in all_picks:
            ev = te.pick_ev(pk["round"], pk["year"], pk.get("team_id", 0), pk.get("slot", 16))
            ordinals = {1: "1st", 2: "2nd", 3: "3rd"}
            rnd_label = ordinals.get(pk["round"], f"{pk['round']}th")
            pick_lines.append(f"S{pk['year']} {rnd_label}: **{ev['final_ev']:,} pts** (×{ev['temporal_factor']} temporal)")
        embed.add_field(
            name="🎯 Pick Values",
            value="\n".join(pick_lines),
            inline=False,
        )

    # ── Rule violations / flags ───────────────────────────────────────────────
    if result.notes:
        embed.add_field(
            name="⚠️ Flags",
            value="\n".join(result.notes[:8]),
            inline=False,
        )

    # ── Fuzzy match warnings ──────────────────────────────────────────────────
    if warnings:
        embed.add_field(
            name="🔍 Name Resolutions",
            value="\n".join(warnings[:6]),
            inline=False,
        )

    # ── AI commentary ─────────────────────────────────────────────────────────
    commentary_text = f"*{ai_commentary}*"
    if len(commentary_text) > 1024:
        commentary_text = commentary_text[:1021] + "…*"
    embed.add_field(
        name="🔬 ATLAS Echo · Analysis",
        value=commentary_text,
        inline=False,
    )

    embed.set_footer(text=f"Trade ID: {trade_id} • Proposer ID: {proposer_id}")
    return embed


# ── Trade Detail Modal (Step 3) ───────────────────────────────────────────────

class TradeDetailModal(discord.ui.Modal):
    players_a_input = discord.ui.TextInput(
        label="Players FROM Team A (comma-separated)",
        placeholder='e.g. "Patrick Mahomes, Travis Kelce" — or leave blank',
        required=False,
        max_length=400,
    )
    picks_a_input = discord.ui.TextInput(
        label="Picks FROM Team A  (format: S7R1, S8R2)",
        placeholder='e.g. "S7R1, S8R2" — Season 7 Round 1, Season 8 Round 2',
        required=False,
        max_length=200,
    )
    players_b_input = discord.ui.TextInput(
        label="Players FROM Team B (comma-separated)",
        placeholder='e.g. "Justin Jefferson, Jordan Love" — or leave blank',
        required=False,
        max_length=400,
    )
    picks_b_input = discord.ui.TextInput(
        label="Picks FROM Team B  (format: S7R1, S8R2)",
        placeholder='e.g. "S7R1" — Season 7 Round 1',
        required=False,
        max_length=200,
    )

    def __init__(self, team_a: dict, team_b: dict, proposer_id: int, bot: commands.Bot,
                 prefill: dict | None = None):
        super().__init__(title="💱 Trade Details")
        self.team_a      = team_a
        self.team_b      = team_b
        self.proposer_id = proposer_id
        self.bot_ref     = bot

        # Pre-fill for counter offers
        if prefill:
            if prefill.get("players_a"):
                self.players_a_input.default = prefill["players_a"]
            if prefill.get("picks_a"):
                self.picks_a_input.default = prefill["picks_a"]
            if prefill.get("players_b"):
                self.players_b_input.default = prefill["players_b"]
            if prefill.get("picks_b"):
                self.picks_b_input.default = prefill["picks_b"]

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _evaluate_and_post(
            interaction=interaction,
            team_a=self.team_a,
            team_b=self.team_b,
            players_a_raw=self.players_a_input.value,
            picks_a_raw=self.picks_a_input.value,
            players_b_raw=self.players_b_input.value,
            picks_b_raw=self.picks_b_input.value,
            proposer_id=self.proposer_id,
            bot=self.bot_ref,
        )


async def _evaluate_and_post(
    interaction: discord.Interaction,
    team_a: dict, team_b: dict,
    players_a_raw: str, picks_a_raw: str,
    players_b_raw: str, picks_b_raw: str,
    proposer_id: int,
    bot: commands.Bot,
    original_trade_id: str | None = None,
    resolved_players_a: list[dict] | None = None,
    resolved_players_b: list[dict] | None = None,
):
    """
    Core evaluation logic — resolves assets, runs engine, posts Trade Card.

    resolved_players_a / resolved_players_b: pre-resolved player lists from
    the picker UI. When provided, skip text resolution for that side.
    Players are still sanitized (NaN-safe) before hitting the engine.
    """

    team_a_id = int(team_a.get("id", 0))
    team_b_id = int(team_b.get("id", 0))

    # Resolve assets — use pre-resolved lists from picker when available
    if resolved_players_a is not None:
        # Picker path: players already sanitized, just parse picks
        players_a = resolved_players_a
        picks_a, pick_errors_a = _parse_picks(picks_a_raw, team_a_id)
        errors_a   = pick_errors_a
        warnings_a = []
    else:
        players_a, picks_a, errors_a, warnings_a = _resolve_assets(players_a_raw, picks_a_raw, team_a_id)

    if resolved_players_b is not None:
        players_b = resolved_players_b
        picks_b, pick_errors_b = _parse_picks(picks_b_raw, team_b_id)
        errors_b   = pick_errors_b
        warnings_b = []
    else:
        players_b, picks_b, errors_b, warnings_b = _resolve_assets(players_b_raw, picks_b_raw, team_b_id)

    all_errors = errors_a + errors_b
    if all_errors:
        err_embed = discord.Embed(
            title="❌ Could not resolve some assets",
            description=(
                "Please fix the following and resubmit:\n\n" +
                "\n".join(all_errors) +
                "\n\n**Tip:** Use full player names. For picks use `S7R1` format (Season 7 Round 1)."
            ),
            color=discord.Color.red(),
        )
        return await interaction.followup.send(embed=err_embed, ephemeral=True)

    if not players_a and not picks_a and not players_b and not picks_b:
        return await interaction.followup.send(
            "❌ Trade has no assets on either side.", ephemeral=True
        )

    # Run engine
    side_a  = te.TradeSide(players=players_a, picks=picks_a, team_id=team_a_id)
    side_b  = te.TradeSide(players=players_b, picks=picks_b, team_id=team_b_id)
    result  = te.evaluate_trade(side_a, side_b)

    # AI commentary (non-blocking)
    ai_text = await _get_ai_commentary(result, _team_label(team_a), _team_label(team_b))

    warnings = warnings_a + warnings_b

    # Build trade record
    trade_id = str(uuid.uuid4())[:8].upper()
    trade = {
        "id":            trade_id,
        "original_id":   original_trade_id,
        "proposer_id":   proposer_id,
        "team_a_id":     team_a_id,
        "team_b_id":     team_b_id,
        "team_a_name":   team_a.get("nickName", ""),
        "team_a_owner":  team_a.get("userName", ""),
        "team_b_name":   team_b.get("nickName", ""),
        "team_b_owner":  team_b.get("userName", ""),
        "players_a_raw": players_a_raw,
        "picks_a_raw":   picks_a_raw,
        "players_b_raw": players_b_raw,
        "picks_b_raw":   picks_b_raw,
        # Serialized asset data — survives approve/reject re-render even when
        # raw strings are empty (picker-mode trades). Fixes blank card bug.
        "players_a_data": [_serialize_player(p) for p in players_a],
        "picks_a_data":   [dict(pk) for pk in picks_a],
        "players_b_data": [_serialize_player(p) for p in players_b],
        "picks_b_data":   [dict(pk) for pk in picks_b],
        "side_a_value":  result.side_a_value,
        "side_b_value":  result.side_b_value,
        "delta_pct":     result.delta_pct,
        "band":          result.band,
        "status":        "pending",
        "submitted_at":  datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "resolved_at":   None,
        "resolved_by":   None,
    }
    _trades[trade_id] = trade
    _save_trade_state()

    # ── Build OVR delta for card data ────────────────────────────────────────
    a_ovr = sum(int(p.get("overallRating") or p.get("playerBestOvr") or 0) for p in players_a)
    b_ovr = sum(int(p.get("overallRating") or p.get("playerBestOvr") or 0) for p in players_b)
    ovr_delta = a_ovr - b_ovr

    # Pick value lines for card
    pick_lines = []
    for pk in picks_a + picks_b:
        ev = te.pick_ev(pk["round"], pk["year"], pk.get("team_id", 0), pk.get("slot", 16))
        ordinals = {1: "1st", 2: "2nd", 3: "3rd"}
        rnd_label = ordinals.get(pk["round"], f"{pk['round']}th")
        pick_lines.append(f"S{pk['year']} {rnd_label}: {ev['final_ev']:,} pts")

    view = TradeActionView(
        trade_id=trade_id,
        team_a=team_a,
        team_b=team_b,
        proposer_id=proposer_id,
        bot=bot,
    )

    # ── Try image render first, fall back to embed ────────────────────────────
    card_data = {
        "trade_id":      trade_id,
        "status":        "pending",
        "team_a_name":   team_a.get("nickName", _team_label(team_a)),
        "team_a_owner":  team_a.get("userName", ""),
        "team_b_name":   team_b.get("nickName", _team_label(team_b)),
        "team_b_owner":  team_b.get("userName", ""),
        "players_a":     players_a,
        "picks_a":       picks_a,
        "players_b":     players_b,
        "picks_b":       picks_b,
        "side_a_value":  result.side_a_value,
        "side_b_value":  result.side_b_value,
        "delta_pct":     result.delta_pct,
        "band":          result.band,
        "ovr_delta":     ovr_delta,
        "pick_lines":    pick_lines,
        "notes":         result.notes or [],
        "ai_commentary": ai_text,
        "proposer_id":   proposer_id,
        "warnings":      warnings,
    }

    png_bytes = None
    if _IMAGE_RENDER and cr:
        png_bytes = await cr.render_trade_card(card_data)

    if png_bytes:
        import io as _io
        file = discord.File(_io.BytesIO(png_bytes), filename=f"trade_{trade_id}.png")
        await interaction.followup.send(file=file, view=view)
    else:
        # Embed fallback
        embed = _build_trade_card(
            trade_id=trade_id,
            team_a=team_a, team_b=team_b,
            players_a=players_a, picks_a=picks_a,
            players_b=players_b, picks_b=picks_b,
            result=result,
            ai_commentary=ai_text,
            warnings=warnings,
            proposer_id=proposer_id,
        )
        await interaction.followup.send(embed=embed, view=view)

    # Mirror to trade log channel if configured
    if _trades_channel_id() and interaction.guild:
        log_ch = interaction.guild.get_channel(_trades_channel_id())
        if log_ch:
            try:
                if png_bytes:
                    import io as _io
                    log_file = discord.File(_io.BytesIO(png_bytes), filename=f"trade_{trade_id}.png")
                    await log_ch.send(
                        content=f"📋 **New Trade Proposal** — ID `{trade_id}`",
                        file=log_file,
                        view=TradeActionView(trade_id=trade_id, team_a=team_a,
                                            team_b=team_b, proposer_id=proposer_id, bot=bot)
                    )
                else:
                    embed = _build_trade_card(
                        trade_id=trade_id, team_a=team_a, team_b=team_b,
                        players_a=players_a, picks_a=picks_a,
                        players_b=players_b, picks_b=picks_b,
                        result=result, ai_commentary=ai_text,
                        warnings=warnings, proposer_id=proposer_id,
                    )
                    await log_ch.send(
                        content=f"📋 **New Trade Proposal** — ID `{trade_id}`",
                        embed=embed,
                        view=TradeActionView(trade_id=trade_id, team_a=team_a,
                                            team_b=team_b, proposer_id=proposer_id, bot=bot)
                    )
            except Exception:
                pass


# ── Team Select Views (Steps 1 & 2) ──────────────────────────────────────────

def _build_conference_team_options(
    conference: str, exclude_id: int | None = None,
) -> list[discord.SelectOption]:
    """Return team select options filtered by conference (AFC or NFC).

    Uses divName field from dm.df_teams (e.g. 'AFC North', 'NFC West').
    Each conference has 16 teams -- well under Discord's 25-item limit.
    """
    conf_upper = conference.upper()
    options = []
    for t in _get_all_teams():
        tid = int(t.get("id", 0))
        if exclude_id and tid == exclude_id:
            continue
        div_name = str(t.get("divName", ""))
        if not div_name.upper().startswith(conf_upper):
            continue
        nick  = t.get("nickName", t.get("displayName", "Unknown"))
        owner = t.get("userName", "")
        label = f"{nick} — {owner}" if owner else nick
        options.append(discord.SelectOption(label=label[:100], value=str(tid)))
    return options


def _step_info(step: str) -> tuple[str, str]:
    """Return (step_number, step_label) for the given step letter."""
    if step == "A":
        return "1", "Team A (sending)"
    return "2", "Team B (receiving)"


def _conference_select_embed(step: str, description: str, team_a: dict | None = None) -> discord.Embed:
    """Build the standard conference-selection embed for a given trade step."""
    step_num, _ = _step_info(step)
    if team_a and step == "B":
        description = f"**Team A:** {_team_label(team_a)}\n\n{description}"
    return discord.Embed(
        title=f"💱 Trade Center — Step {step_num}",
        description=description,
        color=discord.Color.blurple(),
    )


class ConferenceSelectView(discord.ui.View):
    """AFC / NFC buttons -- used for both Team A and Team B selection steps."""

    def __init__(
        self, bot: commands.Bot, proposer_id: int,
        step: str = "A",
        team_a: dict | None = None,
    ):
        super().__init__(timeout=180)
        self.bot_ref     = bot
        self.proposer_id = proposer_id
        self.step        = step
        self.team_a      = team_a

    @discord.ui.button(label="AFC", style=discord.ButtonStyle.primary, emoji="🏈")
    async def afc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_teams(interaction, "AFC")

    @discord.ui.button(label="NFC", style=discord.ButtonStyle.secondary, emoji="🏈")
    async def nfc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_teams(interaction, "NFC")

    async def _show_teams(self, interaction: discord.Interaction, conference: str):
        exclude_id = int(self.team_a.get("id", 0)) if self.team_a else None
        options = _build_conference_team_options(conference, exclude_id=exclude_id)
        if not options:
            return await interaction.response.send_message(
                f"❌ No {conference} teams found.", ephemeral=True,
            )
        _, step_label = _step_info(self.step)
        embed = _conference_select_embed(
            self.step,
            f"Select **{step_label}** from the **{conference}**.",
            team_a=self.team_a,
        )
        view = ConferenceTeamSelectView(
            bot=self.bot_ref, proposer_id=self.proposer_id,
            step=self.step, team_a=self.team_a, options=options,
        )
        await interaction.response.edit_message(embed=embed, view=view)


class ConferenceTeamSelect(discord.ui.Select):
    """Select menu showing only teams from one conference."""

    def __init__(
        self, bot: commands.Bot, proposer_id: int,
        step: str, team_a: dict | None,
        options: list[discord.SelectOption],
    ):
        self.bot_ref     = bot
        self.proposer_id = proposer_id
        self.step        = step
        self.team_a      = team_a
        placeholder = "Select Team A..." if step == "A" else "Select Team B..."
        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        team_id = int(self.values[0])
        team = next(
            (t for t in _get_all_teams() if int(t.get("id", 0)) == team_id), None,
        )
        if not team:
            return await interaction.response.send_message("❌ Team not found.", ephemeral=True)

        if self.step == "A":
            embed = _conference_select_embed(
                "B",
                "Pick a conference to select **Team B** (the team receiving).",
                team_a=team,
            )
            view = ConferenceSelectView(
                bot=self.bot_ref, proposer_id=self.proposer_id,
                step="B", team_a=team,
            )
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            view = PickerTradeView(
                team_a=self.team_a, team_b=team,
                proposer_id=self.proposer_id, bot=self.bot_ref,
            )
            await interaction.response.edit_message(embed=view.step_embed(), view=view)


class ConferenceTeamSelectView(discord.ui.View):
    """Wraps the conference-filtered team select + a Back button."""

    def __init__(
        self, bot: commands.Bot, proposer_id: int,
        step: str, team_a: dict | None,
        options: list[discord.SelectOption],
    ):
        super().__init__(timeout=180)
        self.bot_ref     = bot
        self.proposer_id = proposer_id
        self.step        = step
        self.team_a      = team_a
        self.add_item(ConferenceTeamSelect(
            bot=bot, proposer_id=proposer_id,
            step=step, team_a=team_a, options=options,
        ))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _, step_label = _step_info(self.step)
        embed = _conference_select_embed(
            self.step,
            f"Pick a conference to select **{step_label}**.",
            team_a=self.team_a,
        )
        view = ConferenceSelectView(
            bot=self.bot_ref, proposer_id=self.proposer_id,
            step=self.step, team_a=self.team_a,
        )
        await interaction.response.edit_message(embed=embed, view=view)


# ── Picker-based Trade Flow ───────────────────────────────────────────────────

class PickerTradeView(discord.ui.View):
    """
    Replaces the free-text modal with a filtered player picker.

    Flow:
      Step A — Pick players from Team A (multi picker, pre-filtered to team A)
      Step B — Pick players from Team B (multi picker, pre-filtered to team B)
      Step C — Enter picks via modal (short modal, picks only — no player names)
      Step D — Evaluate + post trade card

    Players are resolved from the live roster via rosterId — no fuzzy matching
    needed, no NaN crashes possible (picker only surfaces valid roster entries).
    """

    def __init__(self, team_a: dict, team_b: dict, proposer_id: int, bot: commands.Bot):
        super().__init__(timeout=600)
        self.team_a      = team_a
        self.team_b      = team_b
        self.proposer_id = proposer_id
        self.bot_ref     = bot
        self.players_a:  list[dict] = []
        self.players_b:  list[dict] = []
        self._step       = "A"   # "A", "B", or "picks"
        self._rebuild()

    def step_embed(self) -> discord.Embed:
        a_nick = self.team_a.get("nickName", "Team A")
        b_nick = self.team_b.get("nickName", "Team B")
        steps  = {
            "A":     (f"Step 3a — Select players from **{a_nick}**",
                      f"Use the dropdowns below to filter and pick players {a_nick} is trading away.\n"
                      f"Hit **✅ Done with Team A** when finished (or pick 0 players for picks-only)."),
            "B":     (f"Step 3b — Select players from **{b_nick}**",
                      f"Now pick players **{b_nick}** is trading away.\n"
                      f"Hit **✅ Done with Team B** when finished."),
            "picks": ("Step 3c — Add picks (optional)",
                      "Click **📋 Add Picks** to enter draft picks, or **🚀 Submit Trade** to evaluate now."),
        }
        title, desc = steps.get(self._step, ("Trade Builder", ""))
        embed = discord.Embed(title=f"💱 TSL Trade Center — {title}", color=discord.Color.blurple())
        embed.description = desc

        a_names = [f"{p.get('firstName','')} {p.get('lastName','')}".strip() for p in self.players_a]
        b_names = [f"{p.get('firstName','')} {p.get('lastName','')}".strip() for p in self.players_b]

        if a_names:
            a_val = "\n".join(f"• {n}" for n in a_names)
            embed.add_field(name=f"📤 {a_nick} gives", value=a_val[:1024], inline=True)
        if b_names:
            b_val = "\n".join(f"• {n}" for n in b_names)
            embed.add_field(name=f"📥 {b_nick} gives", value=b_val[:1024], inline=True)

        embed.set_footer(text="TSL Trade Engine v2.7 — picker mode")
        return embed

    def _rebuild(self):
        self.clear_items()
        a_nick = self.team_a.get("nickName", "Team A")
        b_nick = self.team_b.get("nickName", "Team B")

        if self._step == "A":
            # Picker dropdown pre-filtered to Team A
            self._add_picker_row(
                team=a_nick,
                bucket=self.players_a,
                done_label=f"✅ Done with {a_nick}",
                done_value="done_a",
            )

        elif self._step == "B":
            # Picker dropdown pre-filtered to Team B
            self._add_picker_row(
                team=b_nick,
                bucket=self.players_b,
                done_label=f"✅ Done with {b_nick}",
                done_value="done_b",
            )

        elif self._step == "picks":
            picks_btn = discord.ui.Button(
                label="📋 Add Picks",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            picks_btn.callback = self._on_add_picks
            self.add_item(picks_btn)

            submit_btn = discord.ui.Button(
                label="🚀 Submit Trade",
                style=discord.ButtonStyle.success,
                row=0,
            )
            submit_btn.callback = self._on_submit
            self.add_item(submit_btn)

    def _add_picker_row(self, team: str, bucket: list, done_label: str, done_value: str):
        """Add position filter + team-filtered player select + Done button."""
        from player_picker import _get_pos_options, _filter_players, _build_player_options, POS_GROUPS
        import data_manager as dm

        # Position filter stored per-step
        pos_attr = f"_pos_{done_value}"
        pos_group = getattr(self, pos_attr, "ALL")

        # Row 0: Position filter
        pos_sel = discord.ui.Select(
            placeholder=f"Filter by Position: {pos_group}",
            options=_get_pos_options(),
            row=0, min_values=1, max_values=1,
        )
        for opt in pos_sel.options:
            opt.default = (opt.value == pos_group)

        async def on_pos(interaction: discord.Interaction):
            setattr(self, pos_attr, interaction.data["values"][0])
            self._rebuild()
            await interaction.response.edit_message(embed=self.step_embed(), view=self)

        pos_sel.callback = on_pos
        self.add_item(pos_sel)

        # Row 1: Player list (filtered by pos + team)
        players = _filter_players(pos_group, team)
        player_opts = _build_player_options(players)
        player_sel  = discord.ui.Select(
            placeholder="Select a player to add...",
            options=player_opts,
            row=1, min_values=1, max_values=1,
        )

        async def on_player(interaction: discord.Interaction):
            selected_rid = interaction.data["values"][0]
            if selected_rid == "NONE":
                return await interaction.response.defer()
            # Resolve from full player cache by rosterId
            from player_picker import _all_players, _display_name
            player = next(
                (p for p in _all_players()
                 if str(p.get("rosterId") or p.get("id") or id(p)) == selected_rid),
                None
            )
            if not player:
                return await interaction.response.send_message("❌ Player not found.", ephemeral=True)
            already = any(
                str(p.get("rosterId") or p.get("id") or id(p)) == selected_rid
                for p in bucket
            )
            if already:
                name = _display_name(player)
                return await interaction.response.send_message(
                    f"⚠️ **{name}** already added.", ephemeral=True
                )
            bucket.append(_sanitize_player(player))
            self._rebuild()
            await interaction.response.edit_message(embed=self.step_embed(), view=self)

        player_sel.callback = on_player
        self.add_item(player_sel)

        # Row 2: Clear last + Done
        if bucket:
            remove_btn = discord.ui.Button(
                label=f"✖ Remove last ({len(bucket)} added)",
                style=discord.ButtonStyle.danger,
                row=2,
            )
            async def on_remove(interaction: discord.Interaction):
                if bucket:
                    bucket.pop()
                self._rebuild()
                await interaction.response.edit_message(embed=self.step_embed(), view=self)
            remove_btn.callback = on_remove
            self.add_item(remove_btn)

        done_btn = discord.ui.Button(
            label=done_label,
            style=discord.ButtonStyle.success,
            custom_id=done_value,
            row=2,
        )
        async def on_done(interaction: discord.Interaction):
            next_step = "B" if done_value == "done_a" else "picks"
            self._step = next_step
            self._rebuild()
            await interaction.response.edit_message(embed=self.step_embed(), view=self)
        done_btn.callback = on_done
        self.add_item(done_btn)

    async def _on_add_picks(self, interaction: discord.Interaction):
        modal = PicksOnlyModal(
            team_a=self.team_a,
            team_b=self.team_b,
            players_a=self.players_a,
            players_b=self.players_b,
            proposer_id=self.proposer_id,
            bot=self.bot_ref,
        )
        await interaction.response.send_modal(modal)

    async def _on_submit(self, interaction: discord.Interaction):
        # No picks — submit with empty pick strings
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _evaluate_and_post(
            interaction=interaction,
            team_a=self.team_a,
            team_b=self.team_b,
            players_a_raw="",  # already resolved
            picks_a_raw="",
            players_b_raw="",
            picks_b_raw="",
            proposer_id=self.proposer_id,
            bot=self.bot_ref,
            # Pass pre-resolved player lists directly
            resolved_players_a=self.players_a,
            resolved_players_b=self.players_b,
        )


class PicksOnlyModal(discord.ui.Modal):
    """Lightweight modal — only pick inputs. Player lists come from picker."""

    picks_a_input = discord.ui.TextInput(
        label="Picks FROM Team A  (format: S7R1, S8R2)",
        placeholder='e.g. "S7R1, S8R2" — leave blank if none',
        required=False, max_length=200,
    )
    picks_b_input = discord.ui.TextInput(
        label="Picks FROM Team B  (format: S7R1, S8R2)",
        placeholder='e.g. "S7R1" — leave blank if none',
        required=False, max_length=200,
    )

    def __init__(self, team_a, team_b, players_a, players_b, proposer_id, bot):
        super().__init__(title="📋 Add Draft Picks")
        self.team_a      = team_a
        self.team_b      = team_b
        self.players_a   = players_a
        self.players_b   = players_b
        self.proposer_id = proposer_id
        self.bot_ref     = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _evaluate_and_post(
            interaction=interaction,
            team_a=self.team_a,
            team_b=self.team_b,
            players_a_raw="",
            picks_a_raw=self.picks_a_input.value,
            players_b_raw="",
            picks_b_raw=self.picks_b_input.value,
            proposer_id=self.proposer_id,
            bot=self.bot_ref,
            resolved_players_a=self.players_a,
            resolved_players_b=self.players_b,
        )


# ── Trade Action Buttons ──────────────────────────────────────────────────────

class CounterModal(discord.ui.Modal, title="🔄 Counter Offer"):
    """Lets the proposer revise the trade terms."""

    players_a_input = discord.ui.TextInput(
        label="Players FROM Team A (revised)",
        required=False, max_length=400,
    )
    picks_a_input = discord.ui.TextInput(
        label="Picks FROM Team A (revised, e.g. S7R1)",
        required=False, max_length=200,
    )
    players_b_input = discord.ui.TextInput(
        label="Players FROM Team B (revised)",
        required=False, max_length=400,
    )
    picks_b_input = discord.ui.TextInput(
        label="Picks FROM Team B (revised, e.g. S7R1)",
        required=False, max_length=200,
    )

    def __init__(self, trade: dict, team_a: dict, team_b: dict, bot: commands.Bot):
        super().__init__()
        self.trade   = trade
        self.team_a  = team_a
        self.team_b  = team_b
        self.bot_ref = bot
        # Pre-fill with existing values
        self.players_a_input.default = trade.get("players_a_raw", "")
        self.picks_a_input.default   = trade.get("picks_a_raw", "")
        self.players_b_input.default = trade.get("players_b_raw", "")
        self.picks_b_input.default   = trade.get("picks_b_raw", "")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        # Mark original as countered
        self.trade["status"] = "countered"
        _save_trade_state()
        await _evaluate_and_post(
            interaction=interaction,
            team_a=self.team_a,
            team_b=self.team_b,
            players_a_raw=self.players_a_input.value,
            picks_a_raw=self.picks_a_input.value,
            players_b_raw=self.players_b_input.value,
            picks_b_raw=self.picks_b_input.value,
            proposer_id=interaction.user.id,
            bot=self.bot_ref,
            original_trade_id=self.trade["id"],
        )


class TradeActionView(discord.ui.View):
    def __init__(self, trade_id: str, team_a: dict, team_b: dict,
                 proposer_id: int, bot: commands.Bot):
        super().__init__(timeout=None)
        self.trade_id    = trade_id
        self.team_a      = team_a
        self.team_b      = team_b
        self.proposer_id = proposer_id
        self.bot_ref     = bot

    def _is_commissioner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in ADMIN_USER_IDS:
            return True
        if interaction.guild:
            return any(r.name == "Commissioner" for r in interaction.user.roles)
        return False

    async def _update_status(
        self,
        interaction: discord.Interaction,
        new_status: str,
        label: str,
        color: discord.Color,
    ):
        trade = _trades.get(self.trade_id)
        if not trade:
            return await interaction.response.send_message("❌ Trade not found.", ephemeral=True)

        # ── FIX: Defer immediately — buys 15 min instead of 3-second timeout.
        # Without this, the trade eval + image render below exceeds Discord's
        # 3-second interaction deadline → 404 Unknown Interaction on every
        # approve/reject click.
        await interaction.response.defer()

        trade["status"]      = new_status
        trade["resolved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        trade["resolved_by"] = interaction.user.id
        _save_trade_state()

        # Disable all buttons after resolution
        disabled_view = discord.ui.View()
        for child in self.children:
            child.disabled = True
            disabled_view.add_item(child)

        # ── Re-render image card with updated status ──────────────────────
        # Uses stored serialized data instead of re-resolving from raw strings.
        # This fixes picker-mode trades where players_a_raw/players_b_raw are "".
        if _IMAGE_RENDER and cr and interaction.message and interaction.message.attachments:
            try:
                team_a_id = trade["team_a_id"]
                team_b_id = trade["team_b_id"]

                # Pull stored asset data (saved at trade creation time)
                players_a = trade.get("players_a_data") or []
                picks_a   = trade.get("picks_a_data") or []
                players_b = trade.get("players_b_data") or []
                picks_b   = trade.get("picks_b_data") or []

                # Fallback: if no stored data, try re-resolving from raw
                # (handles trades created before this patch was applied)
                if not players_a and not picks_a and (trade.get("players_a_raw") or trade.get("picks_a_raw")):
                    players_a, picks_a, _, _ = _resolve_assets(
                        trade.get("players_a_raw", ""),
                        trade.get("picks_a_raw", ""),
                        team_a_id,
                    )
                if not players_b and not picks_b and (trade.get("players_b_raw") or trade.get("picks_b_raw")):
                    players_b, picks_b, _, _ = _resolve_assets(
                        trade.get("players_b_raw", ""),
                        trade.get("picks_b_raw", ""),
                        team_b_id,
                    )

                side_a = te.TradeSide(players=players_a, picks=picks_a, team_id=team_a_id)
                side_b = te.TradeSide(players=players_b, picks=picks_b, team_id=team_b_id)
                result = te.evaluate_trade(side_a, side_b)
                a_ovr = sum(int(p.get("overallRating") or p.get("playerBestOvr") or 0) for p in players_a)
                b_ovr = sum(int(p.get("overallRating") or p.get("playerBestOvr") or 0) for p in players_b)
                card_data = {
                    "trade_id":     self.trade_id,
                    "status":       new_status,
                    "team_a_name":  self.team_a.get("nickName", trade.get("team_a_name", "")),
                    "team_a_owner": self.team_a.get("userName", trade.get("team_a_owner", "")),
                    "team_b_name":  self.team_b.get("nickName", trade.get("team_b_name", "")),
                    "team_b_owner": self.team_b.get("userName", trade.get("team_b_owner", "")),
                    "players_a": players_a, "picks_a": picks_a,
                    "players_b": players_b, "picks_b": picks_b,
                    "side_a_value": result.side_a_value,
                    "side_b_value": result.side_b_value,
                    "delta_pct":    result.delta_pct,
                    "band":         result.band,
                    "ovr_delta":    a_ovr - b_ovr,
                    "notes":        result.notes or [],
                    "ai_commentary": trade.get("ai_commentary", ""),
                    "proposer_id":  trade.get("proposer_id", 0),
                    "warnings":     [],
                }
                png_bytes = await cr.render_trade_card(card_data)
                if png_bytes:
                    import io as _io
                    file = discord.File(_io.BytesIO(png_bytes), filename=f"trade_{self.trade_id}_{new_status}.png")
                    await interaction.message.edit(attachments=[file], view=disabled_view)
                    if _trades_channel_id() and interaction.guild:
                        log_ch = interaction.guild.get_channel(_trades_channel_id())
                        if log_ch:
                            a_name = trade.get("team_a_name", "Team A")
                            b_name = trade.get("team_b_name", "Team B")
                            await log_ch.send(
                                f"📋 Trade `{self.trade_id}` **{new_status.upper()}** by {interaction.user.mention} "
                                f"— {a_name} ↔ {b_name}"
                            )
                    return
            except Exception as e:
                print(f"[trade_center] Status re-render error: {e}")

        # Fallback: edit existing embed title
        if interaction.message and interaction.message.embeds:
            old_embed = interaction.message.embeds[0]
            status_map = {
                "approved":  ("✅", "APPROVED"),
                "rejected":  ("❌", "REJECTED"),
                "countered": ("🔄", "Counter Offered"),
            }
            s_emoji, s_label = status_map.get(new_status, ("⏳", new_status.title()))
            new_embed = old_embed.copy()
            new_embed.title = f"💱 Trade Proposal — {s_emoji} {s_label}"
            new_embed.color = color
            await interaction.message.edit(embed=new_embed, view=disabled_view)
        else:
            await interaction.followup.send(
                f"{label} by {interaction.user.mention}", ephemeral=True
            )

        # Announce in trade log channel
        if _trades_channel_id() and interaction.guild:
            log_ch = interaction.guild.get_channel(_trades_channel_id())
            if log_ch:
                try:
                    a_name = trade.get("team_a_name", "Team A")
                    b_name = trade.get("team_b_name", "Team B")
                    await log_ch.send(
                        f"📋 Trade `{self.trade_id}` **{new_status.upper()}** by {interaction.user.mention} "
                        f"— {a_name} ↔ {b_name}"
                    )
                except Exception:
                    pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_commissioner(interaction):
            return await interaction.response.send_message(
                "❌ Only commissioners can approve trades.", ephemeral=True
            )
        await self._update_status(
            interaction, "approved", "✅ Approved",
            discord.Color.from_rgb(34, 197, 94)
        )

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌", row=0)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_commissioner(interaction):
            return await interaction.response.send_message(
                "❌ Only commissioners can reject trades.", ephemeral=True
            )
        await self._update_status(
            interaction, "rejected", "❌ Rejected",
            discord.Color.from_rgb(239, 68, 68)
        )

    @discord.ui.button(label="Counter", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
    async def counter_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Any involved owner can counter, or commissioner
        trade = _trades.get(self.trade_id)
        if not trade:
            return await interaction.response.send_message("❌ Trade not found.", ephemeral=True)

        is_involved = (
            interaction.user.id == self.proposer_id or
            self._is_commissioner(interaction)
        )
        if not is_involved:
            # Also allow if the user owns team_a or team_b
            # (check via KNOWN_MEMBER_TEAMS from intelligence)
            try:
                from intelligence import KNOWN_MEMBER_TEAMS
                user_team_nick = KNOWN_MEMBER_TEAMS.get(interaction.user.id, "")
                team_a_nick = trade.get("team_a_name", "")
                team_b_nick = trade.get("team_b_name", "")
                if user_team_nick.lower() not in (team_a_nick.lower(), team_b_nick.lower()):
                    return await interaction.response.send_message(
                        "❌ Only the involved owners or commissioners can counter.", ephemeral=True
                    )
            except ImportError:
                pass  # Can't verify — allow it

        modal = CounterModal(
            trade=trade,
            team_a=self.team_a,
            team_b=self.team_b,
            bot=self.bot_ref,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Full Breakdown", style=discord.ButtonStyle.secondary, emoji="📊", row=1)
    async def breakdown_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Shows the full trade_engine point breakdown ephemerally."""
        trade = _trades.get(self.trade_id)
        if not trade:
            return await interaction.response.send_message("❌ Trade not found.", ephemeral=True)

        # ── FIX: Defer before heavy work — trade eval can exceed 3-second deadline
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Re-run to get breakdown lines
        team_a_id = trade["team_a_id"]
        team_b_id = trade["team_b_id"]

        players_a, picks_a, _, _ = _resolve_assets(trade["players_a_raw"], trade["picks_a_raw"], team_a_id)
        players_b, picks_b, _, _ = _resolve_assets(trade["players_b_raw"], trade["picks_b_raw"], team_b_id)

        side_a = te.TradeSide(players=players_a, picks=picks_a, team_id=team_a_id)
        side_b = te.TradeSide(players=players_b, picks=picks_b, team_id=team_b_id)
        result = te.evaluate_trade(side_a, side_b)

        a_name = trade.get("team_a_name", "Team A")
        b_name = trade.get("team_b_name", "Team B")

        embed = discord.Embed(
            title=f"📊 Full Breakdown — Trade `{self.trade_id}`",
            color=discord.Color.blurple(),
        )

        # Chunk breakdown (Discord field limit = 1024 chars)
        def _chunk(lines: list[str], limit: int = 900) -> list[str]:
            chunks, current = [], ""
            for line in lines:
                if len(current) + len(line) + 1 > limit:
                    chunks.append(current)
                    current = line + "\n"
                else:
                    current += line + "\n"
            if current:
                chunks.append(current)
            return chunks or ["_None_"]

        a_chunks = _chunk(result.breakdown_a)
        b_chunks = _chunk(result.breakdown_b)

        for i, chunk in enumerate(a_chunks[:3]):
            embed.add_field(name=f"📤 {a_name} {'(cont.)' if i else ''}", value=chunk, inline=False)
        for i, chunk in enumerate(b_chunks[:3]):
            embed.add_field(name=f"📥 {b_name} {'(cont.)' if i else ''}", value=chunk, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class TradeCenterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _load_trade_state()

    @app_commands.command(
        name="trade",
        description="Open the TSL Trade Center — pick a conference, then select teams.",
    )
    async def trade(self, interaction: discord.Interaction):
        """Conference-button trade flow.  AFC/NFC → 16-team dropdown → picker."""
        if dm.df_teams.empty or not dm.get_players():
            return await interaction.response.send_message(
                "⚠️ Roster data not loaded yet. Run `/wittsync` first.", ephemeral=True,
            )
        embed = discord.Embed(
            title="💱 Trade Center — Step 1",
            description="Pick a conference to select **Team A** (the team sending).",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="TSL Trade Engine v2.7 • Picker mode • All valuations are advisory")
        view = ConferenceSelectView(
            bot=self.bot, proposer_id=interaction.user.id, step="A",
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _tradelist_impl(self, interaction: discord.Interaction):
        pending = [t for t in _trades.values() if t.get("status") == "pending"]
        if not pending:
            return await interaction.response.send_message("✅ No pending trades.", ephemeral=True)

        embed = discord.Embed(
            title="💱 Pending Trade Proposals",
            color=discord.Color.blurple(),
            description=f"**{len(pending)}** pending"
        )
        for t in sorted(pending, key=lambda x: x["submitted_at"], reverse=True)[:10]:
            band_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(t.get("band",""), "⚪")
            embed.add_field(
                name=f"`{t['id']}` {band_emoji} {t.get('team_a_name','?')} ↔ {t.get('team_b_name','?')}",
                value=(
                    f"Delta: **{t.get('delta_pct',0):.1f}%** | "
                    f"By: <@{t['proposer_id']}> | "
                    f"<t:{int(datetime.datetime.fromisoformat(t['submitted_at']).timestamp())}:R>"
                ),
                inline=False,
            )
        embed.set_footer(text="Use 🔍 Trade Lookup in /genesis for full details.")
        await interaction.response.send_message(embed=embed, ephemeral=True)



# ══════════════════════════════════════════════════════════════════════════════
#  GENESIS · PARITY, DEV TRAITS & ABILITY AUDIT
# ══════════════════════════════════════════════════════════════════════════════

# ── Constants ─────────────────────────────────────────────────────────────────

OFFENSE_POSITIONS = {"QB", "HB", "FB", "WR", "TE", "LT", "LG", "C", "RG", "RT"}
DEFENSE_POSITIONS = {"LEDGE", "REDGE", "DT", "MIKE", "WILL", "SAM", "FS", "SS", "CB"}

LOTTERY_BASELINE   = 100         # ping-pong balls at elimination
LOTTERY_PER_WIN    = 25          # balls added per post-elimination win

# Ability audit display helpers (mirrors ability_cog.py, inlined for consolidation)
TIER_EMOJI = {"S": "🔴", "A": "🟠", "B": "🟡", "C": "⚪"}
DEV_EMOJI  = {
    "Normal":             "⚪",
    "Star":               "⭐",
    "Superstar":          "🌟",
    "Superstar X-Factor": "⚡",
}

# ── In-memory state (persisted to JSON between restarts) ─────────────────────
_STATE_PATH = os.path.join(os.path.dirname(__file__), "parity_state.json")

_state: dict = {
    "cornerstones":    {},          # rosterId → {team, name, designated_week}
    "orphan_teams":    set(),       # team names with orphan flag set
    "cap_clear_log":   [],          # [{timestamp, admin_id, team, action}]
    "rings":           {},          # team_id (str) → ring count
    "lottery_winners": [],          # list of {season, team, pick}
}


def _load_state():
    global _state
    if os.path.isfile(_STATE_PATH):
        try:
            with open(_STATE_PATH, "r") as f:
                loaded = json.load(f)
                loaded["orphan_teams"] = set(loaded.get("orphan_teams", []))
                _state.update(loaded)
        except Exception as e:
            print(f"[parity_cog] State load error: {e}")


def _save_state():
    try:
        to_save = dict(_state)
        to_save["orphan_teams"] = list(_state["orphan_teams"])
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(to_save, f, indent=2)
        os.replace(tmp, _STATE_PATH)
    except Exception as e:
        print(f"[parity_cog] State save error: {e}")


# ── Ability audit embed helpers (ported from ability_cog.py) ─────────────────

def _dev_badge(dev: str) -> str:
    return f"{DEV_EMOJI.get(dev, '')} {dev}"


def _build_player_ability_embed(result: "ae.PlayerAuditResult") -> discord.Embed:
    """Rich embed for a single player ability audit result."""
    color = discord.Color.green() if result.is_clean else discord.Color.red()
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


def _build_team_ability_embeds(
    team_results: "list[ae.PlayerAuditResult]", team_name: str
) -> list[discord.Embed]:
    """One summary embed + one embed per flagged player."""
    violations  = [r for r in team_results if not r.is_clean]
    clean_count = len(team_results) - len(violations)

    summary = discord.Embed(
        title=f"🏈 {team_name} — Ability Audit",
        color=discord.Color.red() if violations else discord.Color.green(),
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

    # Chunk into fields (Discord 1024 char limit per field)
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

    return [summary] + [_build_player_ability_embed(r) for r in violations]


def _build_league_ability_embed(
    summary: dict, top_violations: "list[ae.PlayerAuditResult]"
) -> discord.Embed:
    """High-level league-wide ability audit embed."""
    clean_pct = int(100 * summary["cleanPlayers"] / max(summary["totalPlayersAudited"], 1))
    color = discord.Color.green() if summary["violations"] == 0 else discord.Color.orange()

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


# ── Reassignment embed helpers ────────────────────────────────────────────────

def _build_reassignment_summary_embed(summary: dict) -> discord.Embed:
    """High-level league-wide reassignment summary embed."""
    has_changes = summary["playersWithSwaps"] > 0 or summary["playersUnresolved"] > 0
    color = discord.Color.orange() if has_changes else discord.Color.green()

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

    embed.set_author(name="ATLAS™ Genesis Module", icon_url=ATLAS_ICON_URL)
    return embed


def _build_reassignment_team_embeds(
    changed_results: "list[ae.ReassignmentResult]",
) -> list[discord.Embed]:
    """Build per-team embeds showing each player's ability changes."""
    by_team: dict[str, list] = {}
    for r in changed_results:
        by_team.setdefault(r.team, []).append(r)

    embeds: list[discord.Embed] = []

    for team_name in sorted(by_team.keys()):
        team_players = by_team[team_name]

        embed = discord.Embed(
            title=f"🏈 {team_name} — Ability Reassignment",
            color=discord.Color.orange(),
        )

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
            embed.add_field(
                name=f"{r.name} ({r.pos}, {dev_badge})",
                value="\n".join(lines) if lines else "_No changes_",
                inline=False,
            )

        embed.set_footer(text="ATLAS™ Genesis · Ability Reassignment Engine")
        embeds.append(embed)

    return embeds


# ── Lottery helpers ────────────────────────────────────────────────────────────

def _build_lottery_pool() -> list[tuple[str, int]]:
    """
    Build the weighted pool of (team_name, balls) for eliminated teams.
    Returns sorted list. Requires dm.get_team_record_dict() (Phase 1).
    """
    pool: list[tuple[str, int]] = []

    if dm.df_standings.empty:
        return pool

    for _, row in dm.df_standings.iterrows():
        team = row.get("teamName", "")
        wins = int(row.get("totalWins", 0))
        losses = int(row.get("totalLosses", 0))
        eliminated = bool(row.get("playoffEliminated", False))

        if not eliminated:
            continue

        # Try to get post-elimination wins from extended data
        try:
            elim_data = dm.get_team_record_dict(int(row.get("teamId", 0)))
            post_elim_wins = elim_data.get("post_elim_wins", 0)
        except AttributeError:
            post_elim_wins = 0

        balls = LOTTERY_BASELINE + (post_elim_wins * LOTTERY_PER_WIN)
        pool.append((team, balls))

    pool.sort(key=lambda x: x[1], reverse=True)
    return pool


# ═══════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════

class ParityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _load_state()

    # ── /runlottery ───────────────────────────────────────────────────────────
    async def _runlottery_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        pool = _build_lottery_pool()
        if not pool:
            await interaction.followup.send("❌ No eliminated teams available for lottery.")
            return

        # Build weighted list
        weighted: list[str] = []
        for team, balls in pool:
            weighted.extend([team] * balls)

        # Draw picks 1 through N (one per eliminated team, no repeats)
        random.shuffle(weighted)
        seen = set()
        ordered = []
        for t in weighted:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
            if len(ordered) == len(pool):
                break

        # Log result
        _state["lottery_winners"].append({
            "season": dm.CURRENT_SEASON,
            "results": ordered,
        })
        _save_state()

        lines = [f"**Pick {i+1}**: {team}" for i, team in enumerate(ordered)]
        embed = discord.Embed(
            title=f"🎰 Lottery Results — Season {dm.CURRENT_SEASON}",
            color=discord.Color.gold(),
            description="\n".join(lines)
        )
        embed.set_footer(text=f"Drawn by {interaction.user} | Results logged.")
        await interaction.followup.send(embed=embed)

    # ── /orphanfranchise ──────────────────────────────────────────────────────
    async def _orphanfranchise_impl(self, interaction: discord.Interaction, team: str, flag: bool):
        if flag:
            _state["orphan_teams"].add(team.strip())
            msg = f"✅ **{team}** marked as an Orphan Franchise. Cap-clear is now permitted by bot."
        else:
            _state["orphan_teams"].discard(team.strip())
            msg = f"✅ **{team}** orphan flag cleared. Cap-clear is now blocked again."

        _save_state()
        await interaction.response.send_message(msg)

    # ── Cap integrity gate (called by trade_cog / admin commands) ─────────────

    def can_clear_cap(self, team: str) -> bool:
        """Return True only if orphan flag is set for this team."""
        return team.strip() in _state.get("orphan_teams", set())

    def log_cap_clear_attempt(self, team: str, admin_id: int, action: str):
        """Log a cap-clear attempt to the audit trail."""
        import datetime
        _state["cap_clear_log"].append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "admin_id":  admin_id,
            "team":      team,
            "action":    action,
        })
        _save_state()



# ══════════════════════════════════════════════════════════════════════════════
#  GENESIS HUB — /genesis
# ══════════════════════════════════════════════════════════════════════════════

# ── Genesis Hub embed ─────────────────────────────────────────────────────────

def _build_genesis_hub_embed() -> discord.Embed:
    """Landing embed for /genesis — mirrors _build_hub_embed() style."""
    embed = discord.Embed(
        title="🧬 ATLAS Genesis — Roster Hub",
        description=(
            f"Season {dm.CURRENT_SEASON} | Week {dm.CURRENT_WEEK}\n"
            "Trades, dev traits, and franchise tools — **private to you**.\u200b"
        ),
        color=discord.Color.from_rgb(201, 150, 42),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="Navigation",
        value=(
            "```\n"
            "💱 Trade       📜 Trades      🔍 Lookup\n"
            "📊 Dev Traits  🛡️ Ability Audit  👤 Ability Check\n"
            "🔒 Cornerstone  📋 Contract  🔄 Reassign\n"
            "🎰 Lottery  🏈 Rules\n"
            "```"
        ),
        inline=False,
    )
    embed.set_author(name="ATLAS™ Genesis Module", icon_url=ATLAS_ICON_URL)
    embed.set_footer(text="ATLAS™ Genesis · All drill-downs private to you", icon_url=ATLAS_ICON_URL)
    return embed


# ── Genesis Hub View ───────────────────────────────────────────────────────────

class GenesisHubView(discord.ui.View):
    """
    Persistent button panel for /genesis.
    Mirrors HubView pattern from oracle_cog.
    timeout=None + custom_id = survives bot restarts.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    # ── Row 0: Trade Tools ────────────────────────────────────────────────────

    @discord.ui.button(
        label="💱 Trade", style=discord.ButtonStyle.primary,
        row=0, custom_id="genesis:trade",
    )
    async def btn_trade(self, interaction: discord.Interaction, _b: discord.ui.Button):
        """Open the Trade Center directly — launches conference select flow."""
        if dm.df_teams.empty or not dm.get_players():
            return await interaction.response.send_message(
                "Roster data not loaded yet. Run `/wittsync` first.", ephemeral=True,
            )
        embed = discord.Embed(
            title="💱 Trade Center — Step 1",
            description="Pick a conference to select **Team A** (the team sending).",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="TSL Trade Engine v2.7 · Picker mode · All valuations are advisory")
        view = ConferenceSelectView(
            bot=self.bot, proposer_id=interaction.user.id, step="A",
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="📜 Pending Trades", style=discord.ButtonStyle.secondary,
        row=0, custom_id="genesis:tradelist",
    )
    async def btn_tradelist(self, interaction: discord.Interaction, _b: discord.ui.Button):
        is_admin = (
            interaction.user.id in ADMIN_USER_IDS or
            (interaction.guild and any(r.name == "Commissioner" for r in interaction.user.roles))
        )
        if not is_admin:
            return await interaction.response.send_message("❌ Commissioners only.", ephemeral=True)

        pending = [t for t in _trades.values() if t.get("status") == "pending"]
        if not pending:
            return await interaction.response.send_message("✅ No pending trades.", ephemeral=True)

        embed = discord.Embed(
            title="💱 Pending Trades",
            color=discord.Color.blurple(),
            description=f"**{len(pending)}** pending",
        )
        for t in sorted(pending, key=lambda x: x["submitted_at"], reverse=True)[:10]:
            band_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(t.get("band",""), "⚪")
            embed.add_field(
                name=f"`{t['id']}` {band_emoji} {t.get('team_a_name','?')} ↔ {t.get('team_b_name','?')}",
                value=(
                    f"Delta: **{t.get('delta_pct',0):.1f}%** | "
                    f"By: <@{t['proposer_id']}> | "
                    f"<t:{int(datetime.datetime.fromisoformat(t['submitted_at']).timestamp())}:R>"
                ),
                inline=False,
            )
        embed.set_footer(text="Use 🔍 Trade Lookup in /genesis for full details.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="🔍 Trade Lookup", style=discord.ButtonStyle.secondary,
        row=0, custom_id="genesis:tradelookup",
    )
    async def btn_tradelookup(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(_TradeLookupModal())

    # ── Row 1: Dev / Ability ─────────────────────────────────────────────────

    @discord.ui.button(
        label="📊 Dev Traits", style=discord.ButtonStyle.secondary,
        row=1, custom_id="genesis:devaudit",
    )
    async def btn_devaudit(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(_DevAuditModal())

    @discord.ui.button(
        label="🛡️ Ability Audit", style=discord.ButtonStyle.secondary,
        row=1, custom_id="genesis:abilityaudit",
    )
    async def btn_abilityaudit(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(_AbilityAuditModal())

    @discord.ui.button(
        label="👤 Ability Check", style=discord.ButtonStyle.secondary,
        row=1, custom_id="genesis:abilitycheck",
    )
    async def btn_abilitycheck(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(_AbilityCheckModal())

    @discord.ui.button(
        label="🔒 Cornerstone", style=discord.ButtonStyle.secondary,
        row=2, custom_id="genesis:cornerstone",
    )
    async def btn_cornerstone(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(_CornerstoneModal())

    @discord.ui.button(
        label="📋 Contract Check", style=discord.ButtonStyle.secondary,
        row=2, custom_id="genesis:contract",
    )
    async def btn_contract(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(_ContractCheckModal())

    @discord.ui.button(
        label="🔄 Ability Reassign", style=discord.ButtonStyle.danger,
        row=2, custom_id="genesis:abilityreassign",
    )
    async def btn_abilityreassign(self, interaction: discord.Interaction, _b: discord.ui.Button):
        is_admin = (
            interaction.user.id in ADMIN_USER_IDS or
            (interaction.guild and any(r.name == "Commissioner" for r in interaction.user.roles))
        )
        if not is_admin:
            return await interaction.response.send_message(
                "❌ Ability Reassignment is a commissioner-only tool.", ephemeral=True
            )
        if not _AE_AVAILABLE:
            return await interaction.response.send_message(
                "❌ ability_engine.py not found.", ephemeral=True
            )
        await interaction.response.send_modal(_AbilityReassignModal())

    # ── Row 3: Franchise Tools ───────────────────────────────────────────────

    @discord.ui.button(
        label="🎰 Lottery", style=discord.ButtonStyle.secondary,
        row=3, custom_id="genesis:lottery",
    )
    async def btn_lottery(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        cornerstones = _state.get("cornerstones", {})

        try:
            if dm.df_standings.empty:
                return await interaction.followup.send("⚠️ No standings data. Run `/wittsync` first.", ephemeral=True)

            # Build set of team names that have a cornerstone designated
            cs_teams = {v.get("team") for v in cornerstones.values() if v.get("team")}

            rows = []
            for _, row in dm.df_standings.iterrows():
                wins   = int(row.get("totalWins",   0) or 0)
                losses = int(row.get("totalLosses", 0) or 0)
                tname  = str(row.get("teamName", "?"))
                tickets = max(losses - wins, 1)
                cs_flag = "🔒" if tname in cs_teams else ""
                rows.append(f"{cs_flag} **{tname}** — {wins}W-{losses}L · {tickets} ticket(s)")

            embed = discord.Embed(
                title=f"🎰 Lottery Standings — S{dm.CURRENT_SEASON} W{dm.CURRENT_WEEK}",
                description="\n".join(rows[:32]) if rows else "_No data_",
                color=discord.Color.purple(),
            )
            embed.set_footer(text="Run /runlottery to execute · ATLAS™ Genesis")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Lottery data error: `{e}`", ephemeral=True)

    @discord.ui.button(
        label="🏈 Rules Hub", style=discord.ButtonStyle.secondary,
        row=3, custom_id="genesis:rulehub",
    )
    async def btn_rulehub(self, interaction: discord.Interaction, _b: discord.ui.Button):
        """Cross-link to Sentinel rules hub — opens inline."""
        try:
            from sentinel_cog import SentinelHubView, _build_sentinel_hub_embed
            embed = _build_sentinel_hub_embed()
            view = SentinelHubView(self.bot)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception:
            await interaction.response.send_message(
                "Use `/sentinel` to open the ATLAS Sentinel Rules Hub.",
                ephemeral=True,
            )


# ── Helper modals for hub buttons ─────────────────────────────────────────────

class _TradeLookupModal(discord.ui.Modal, title="🔍 Trade Lookup"):
    trade_id = discord.ui.TextInput(
        label="Trade ID",
        placeholder="e.g. A1B2C3D4",
        min_length=6,
        max_length=12,
    )

    async def on_submit(self, interaction: discord.Interaction):
        trade = _trades.get(self.trade_id.value.upper())
        if not trade:
            return await interaction.response.send_message(
                f"❌ No trade found with ID `{self.trade_id.value.upper()}`.", ephemeral=True
            )
        status_map = {"pending": "⏳ Pending", "approved": "✅ Approved", "rejected": "❌ Rejected", "countered": "🔄 Countered"}
        embed = discord.Embed(title=f"💱 Trade `{trade['id']}`", color=discord.Color.blurple())
        embed.add_field(name="Status", value=status_map.get(trade["status"], trade["status"]), inline=True)
        embed.add_field(name="Band",   value=trade.get("band", "?"), inline=True)
        embed.add_field(name="Delta",  value=f"{trade.get('delta_pct', 0):.1f}%", inline=True)
        embed.add_field(name="Team A", value=trade.get("team_a_name", "?"), inline=True)
        embed.add_field(name="Team B", value=trade.get("team_b_name", "?"), inline=True)
        embed.add_field(name="Proposer", value=f"<@{trade['proposer_id']}>", inline=True)
        embed.add_field(name="📤 A sends", value=f"Players: `{trade.get('players_a_raw','—')}`\nPicks: `{trade.get('picks_a_raw','—')}`", inline=False)
        embed.add_field(name="📥 B sends", value=f"Players: `{trade.get('players_b_raw','—')}`\nPicks: `{trade.get('picks_b_raw','—')}`", inline=False)
        embed.set_footer(text=f"Submitted: {trade.get('submitted_at','?')[:10]}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class _DevAuditModal(discord.ui.Modal, title="📊 Dev Traits"):
    team_name = discord.ui.TextInput(
        label="Team Name (leave blank for all teams)",
        placeholder="e.g. Cowboys, Eagles (partial match OK)",
        required=False,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
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
                # Use ae._normalize_dev() to correctly handle both int devTrait
                # and string dev fields from different API export formats
                dev = ae._normalize_dev(p) if _AE_AVAILABLE else (p.get("dev", "Normal") or "Normal")
                if dev == "Normal" and not team_filter:
                    continue
                by_dev.setdefault(dev, []).append(p)

            embed = discord.Embed(
                title=f"📊 Dev Traits — {'League-Wide' if not team_filter else team_filter}",
                color=discord.Color.gold(),
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


class _ContractCheckModal(discord.ui.Modal, title="📋 Contract Check"):
    player_name = discord.ui.TextInput(
        label="Player Name",
        placeholder="Partial name match OK (e.g. Mahomes)",
        min_length=2,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
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

            from discord import Embed
            embed = Embed(
                title=f"📋 Contract Check — {len(matches)} result(s)",
                color=discord.Color.blurple(),
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


class _AbilityAuditModal(discord.ui.Modal, title="🛡️ Ability Audit"):
    team_name = discord.ui.TextInput(
        label="Team Name (leave blank for league-wide)",
        placeholder="e.g. Cowboys, Eagles (partial match OK)",
        required=False,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
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
                embeds = _build_team_ability_embeds(results, results[0].team)
                for i in range(0, len(embeds), 10):
                    await interaction.followup.send(embeds=embeds[i:i+10], ephemeral=True)
            else:
                summary    = ae.summarize_audit(results)
                violations = [r for r in results if not r.is_clean]
                await interaction.followup.send(
                    embed=_build_league_ability_embed(summary, violations), ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"❌ Ability audit error: `{e}`", ephemeral=True)


class _AbilityCheckModal(discord.ui.Modal, title="👤 Ability Check"):
    player_name = discord.ui.TextInput(
        label="Player Name",
        placeholder="Partial name match OK (e.g. Mahomes, Jefferson)",
        min_length=2,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
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
                embed=_build_player_ability_embed(results[0]), ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Ability check error: `{e}`", ephemeral=True)


class _CornerstoneModal(discord.ui.Modal, title="🔒 Cornerstone Designation"):
    player_name = discord.ui.TextInput(
        label="Player Name",
        placeholder="Partial name match OK (e.g. Mahomes)",
        min_length=2,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            players = dm.get_players()
            if not players:
                return await interaction.followup.send("⚠️ No roster data. Run `/wittsync` first.", ephemeral=True)

            query = self.player_name.value.strip().lower()
            matches = [
                p for p in players
                if query in f"{p.get('firstName', '')} {p.get('lastName', '')}".lower()
            ]

            if not matches:
                return await interaction.followup.send(
                    f"❌ No player found matching `{self.player_name.value}`.", ephemeral=True
                )
            if len(matches) > 1:
                names = ", ".join(
                    f"{m['firstName']} {m['lastName']} ({m['pos']})" for m in matches[:5]
                )
                return await interaction.followup.send(
                    f"⚠️ Multiple matches: {names}\nBe more specific.", ephemeral=True
                )

            p = matches[0]
            rid = p.get("rosterId")
            name = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()

            if rid in _state["cornerstones"]:
                return await interaction.followup.send(
                    f"⚠️ **{name}** is already designated as a Cornerstone this season.",
                    ephemeral=True,
                )

            _state["cornerstones"][rid] = {
                "name":            name,
                "team":            p.get("teamName", "?"),
                "pos":             p.get("pos", "?"),
                "designated_week": dm.CURRENT_WEEK,
                "designated_by":   str(interaction.user),
            }
            _save_state()

            embed = discord.Embed(
                title=f"🔒 Cornerstone Designation — {name}",
                color=discord.Color.gold(),
                description=(
                    f"**{name}** ({p.get('pos')}, {p.get('teamName', '?')}) has been designated as a Cornerstone.\n\n"
                    f"• One-tier dev trait bump applied (commissioner must execute in Madden)\n"
                    f"• Player is **trade-locked** for the remainder of Season {dm.CURRENT_SEASON}\n"
                    f"• Designation recorded at Week {dm.CURRENT_WEEK}"
                ),
            )
            embed.set_footer(text="Cornerstone lock enforced by bot in /tradepropose.")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Cornerstone error: `{e}`", ephemeral=True)


class _AbilityReassignModal(discord.ui.Modal, title="🔄 Ability Reassignment"):
    confirm = discord.ui.TextInput(
        label="Type REASSIGN to confirm",
        placeholder="REASSIGN",
        min_length=8,
        max_length=8,
    )

    async def on_submit(self, interaction: discord.Interaction):
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
                embed=_build_reassignment_summary_embed(summary), ephemeral=True
            )

            # 2. Team-by-team breakdown (only teams with changes)
            changed = [r for r in results if r.has_changes]
            if changed:
                team_embeds = _build_reassignment_team_embeds(changed)
                for i in range(0, len(team_embeds), 10):
                    await interaction.followup.send(
                        embeds=team_embeds[i:i+10], ephemeral=True
                    )

            # 3. JSON file attachment
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


# ── Genesis Hub Cog ────────────────────────────────────────────────────────────

class GenesisHubCog(commands.Cog):
    """ATLAS Genesis — roster hub navigation command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Re-register GenesisHubView so persistent buttons survive restarts
        self.bot.add_view(GenesisHubView(bot))

    @app_commands.command(
        name="genesis",
        description="Open the ATLAS Genesis Hub — trades, dev traits, abilities, and franchise tools.",
    )
    async def genesis(self, interaction: discord.Interaction):
        embed = _build_genesis_hub_embed()
        view  = GenesisHubView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SETUP
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(TradeCenterCog(bot))
    await bot.add_cog(ParityCog(bot))
    await bot.add_cog(GenesisHubCog(bot))
    print("ATLAS: Genesis Module loaded. 🧬")
