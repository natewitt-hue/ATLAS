"""
oracle_helpers.py — ATLAS Oracle helper functions, constants, and embed builders
─────────────────────────────────────────────────────────────────────────────
Extracted from oracle_cog.py for maintainability.

Contains:
  - Import block (analysis, data_manager, intelligence, permissions, codex pipeline)
  - ATLAS branding constants, color palette, NFL identity dict
  - Gemini client cache
  - Universal helper functions (_season_label, _rank_emoji, etc.)
  - All _build_* embed builder functions
  - Super Bowl history data and ring info
  - SQL helper functions for franchise history
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

# ── Unified imports ───────────────────────────────────────────────────────────
import asyncio
import datetime
import json
import os
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


# ══════════════════════════════════════════════════════════════════════════════
#  ORACLE · ANALYTICS HELPERS
# ══════════════════════════════════════════════════════════════════════════════
# NOTE: Color palette (C_HOT, C_COLD, etc.), _season_label(), _rank_emoji(),
# _trend_bar(), _dev_emoji(), _grade_color(), _winpct_bar(), _record_str(),
# _ai_blurb(), and ATLAS_ICON_URL are defined once in the Stats Hub section
# below (lines ~800+). Do NOT duplicate them here.



_EMBED_DESC_LIMIT = 4096

def _truncate_for_embed(text: str, limit: int = _EMBED_DESC_LIMIT) -> str:
    """Truncate text to fit within Discord embed description limit."""
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."

#  NAVIGATION VIEW (shared across all report types)
# ─────────────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
#  ORACLE · STATS HUB
# ══════════════════════════════════════════════════════════════════════════════


# ── Optional codex pipeline (SQL + Gemini NL queries) ────────────────────────
# Module-scope defaults so names are always bound (Pyright safety)
run_sql = None
fuzzy_resolve_user = None
resolve_names_in_question = None
gemini_sql = None
gemini_answer = None
extract_sql = None
DB_SCHEMA = ""
KNOWN_USERS = ""
_build_conversation_block = None
_add_conversation_turn = None

try:
    from codex_cog import (
        gemini_sql,
        gemini_answer,
        run_sql,
        extract_sql,
        fuzzy_resolve_user,
        resolve_names_in_question,
        _build_conversation_block,
        _add_conversation_turn,
        DB_SCHEMA,
        KNOWN_USERS,
    )
    _HISTORY_OK = True
except ImportError:
    _HISTORY_OK = False
    print("[oracle_helpers] codex_cog not available — /ask history queries disabled")



# ── ATLAS branding constants ──────────────────────────────────────────────────
from constants import ATLAS_ICON_URL
ATLAS_GOLD = discord.Color.from_rgb(201, 150, 42)

# ── Module-level cached Gemini client (avoids spinning up a new client per call)
_GEMINI_CLIENT = None

def _get_gemini_client():
    """Return the cached Gemini client, creating it once if needed."""
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        _GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
        return _GEMINI_CLIENT
    except Exception:
        return None

# ── Color palette ─────────────────────────────────────────────────────────────
C_HOT     = discord.Color.from_rgb(255, 87,  51)   # fire orange
C_COLD    = discord.Color.from_rgb(82,  172, 240)   # ice blue
C_NEUTRAL = discord.Color.from_rgb(148, 163, 184)   # slate
C_GOLD    = discord.Color.from_rgb(250, 189, 47)    # championship gold
C_GREEN   = discord.Color.from_rgb(34,  197, 94)
C_RED     = discord.Color.from_rgb(239, 68,  68)
C_PURPLE  = discord.Color.from_rgb(139, 92,  246)
C_BLUE    = discord.Color.from_rgb(59,  130, 246)
C_DARK    = discord.Color.from_rgb(26,  26,  46)

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
    client = _get_gemini_client()
    if not client:
        return ""
    try:
        from google.genai import types
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.0-flash",
                config=types.GenerateContentConfig(temperature=0.8, max_output_tokens=max_tokens),
                contents=prompt,
            ),
        )
        return response.text.strip()
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
    if GEMINI_API_KEY and not dm.df_team_stats.empty:
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
                    f"You are ATLAS Oracle, TSL analytics intelligence. In ONE sharp sentence, "
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

def _build_draft_overview_embed() -> discord.Embed:
    data    = ig.compare_draft_classes()
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


def _build_draft_season_embeds(season: int) -> list[discord.Embed]:
    data = ig.get_draft_class(season)

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

