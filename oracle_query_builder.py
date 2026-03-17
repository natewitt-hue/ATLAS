# oracle_query_builder.py
"""
Oracle v3 QueryBuilder API — Domain-aware SQL builder for TSL history queries.

Three-layer API:
  Layer 1: High-level domain functions (h2h, stat_leaders, roster, etc.)
  Layer 2: Composable QueryBuilder with domain guards
  Layer 3: Utility functions (compare, summarize, resolve_user, etc.)

Domain rules (sort direction, efficiency vs volume, min games, position filtering)
are enforced mechanically by the API — the LLM cannot violate them.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import data_manager as dm

# ---------------------------------------------------------------------------
# Database helpers (mirrors codex_cog pattern)
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "tsl_history.db"
MAX_ROWS = 50


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _run_sql(sql: str, params: tuple = ()) -> tuple[list[dict], str | None]:
    try:
        conn = _get_db()
        try:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            return rows[:MAX_ROWS], None
        finally:
            conn.close()
    except Exception as e:
        return [], str(e)


# ---------------------------------------------------------------------------
# Domain Knowledge: StatDef + STAT_DEFS registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StatDef:
    """Definition of a single stat metric with domain rules."""
    table: str               # "offensive_stats", "defensive_stats", "team_stats"
    column: str              # DB column name
    agg: str                 # "SUM" (volume) or "AVG" (efficiency)
    pos: str | None          # Position filter (e.g. "QB") or None for all
    category: str            # "offense" or "defense" — determines sort inversion
    efficiency_alt: str | None = None  # Column to use for "worst" queries (e.g. passerRating)
    min_games: int = 4       # Minimum games for "worst" qualification


# All 39 stats from STAT_REGISTRY, now with category + efficiency_alt
STAT_DEFS: dict[str, StatDef] = {
    # --- Passing (QB position filter) ---
    "passTDs":      StatDef("offensive_stats", "passTDs",      "SUM", "QB", "offense"),
    "passYds":      StatDef("offensive_stats", "passYds",      "SUM", "QB", "offense", efficiency_alt="passerRating"),
    "passInts":     StatDef("offensive_stats", "passInts",     "SUM", "QB", "offense"),
    "passerRating": StatDef("offensive_stats", "passerRating", "AVG", "QB", "offense"),
    "passCompPct":  StatDef("offensive_stats", "passCompPct",  "AVG", "QB", "offense"),
    "passComp":     StatDef("offensive_stats", "passComp",     "SUM", "QB", "offense"),
    # --- Rushing (no position filter) ---
    "rushTDs":      StatDef("offensive_stats", "rushTDs",      "SUM", None, "offense"),
    "rushYds":      StatDef("offensive_stats", "rushYds",      "SUM", None, "offense"),
    "rushFum":      StatDef("offensive_stats", "rushFum",      "SUM", None, "offense"),
    # --- Receiving (no position filter) ---
    "recTDs":       StatDef("offensive_stats", "recTDs",       "SUM", None, "offense"),
    "recYds":       StatDef("offensive_stats", "recYds",       "SUM", None, "offense"),
    "recCatches":   StatDef("offensive_stats", "recCatches",   "SUM", None, "offense"),
    "recDrops":     StatDef("offensive_stats", "recDrops",     "SUM", None, "offense"),
    "recYdsAfterCatch": StatDef("offensive_stats", "recYdsAfterCatch", "SUM", None, "offense"),
    # --- Defense (individual player stats, no position filter) ---
    "defForcedFum":     StatDef("defensive_stats", "defForcedFum",     "SUM", None, "defense"),
    "defFumRec":        StatDef("defensive_stats", "defFumRec",        "SUM", None, "defense"),
    "defTDs":           StatDef("defensive_stats", "defTDs",           "SUM", None, "defense"),
    "defDeflections":   StatDef("defensive_stats", "defDeflections",   "SUM", None, "defense"),
    "defTotalTackles":  StatDef("defensive_stats", "defTotalTackles",  "SUM", None, "defense"),
    "defSacks":         StatDef("defensive_stats", "defSacks",         "SUM", None, "defense"),
    "defInts":          StatDef("defensive_stats", "defInts",          "SUM", None, "defense"),
    # --- Team stats (offense) ---
    "offTotalYds":  StatDef("team_stats", "offTotalYds",  "SUM", None, "offense"),
    "offPassYds":   StatDef("team_stats", "offPassYds",   "SUM", None, "offense"),
    "offRushYds":   StatDef("team_stats", "offRushYds",   "SUM", None, "offense"),
    "offPassTDs":   StatDef("team_stats", "offPassTDs",   "SUM", None, "offense"),
    "offRushTDs":   StatDef("team_stats", "offRushTDs",   "SUM", None, "offense"),
    "off1stDowns":  StatDef("team_stats", "off1stDowns",  "SUM", None, "offense"),
    "offSacks":     StatDef("team_stats", "offSacks",     "SUM", None, "offense"),
    "ptsFor":       StatDef("standings",  "ptsFor",       "SUM", None, "offense"),
    # --- Team stats (defense) ---
    "defTotalYds_team":  StatDef("team_stats", "defTotalYds",  "SUM", None, "defense"),
    "defPassYds_team":   StatDef("team_stats", "defPassYds",   "SUM", None, "defense"),
    "defRushYds_team":   StatDef("team_stats", "defRushYds",   "SUM", None, "defense"),
    "defSacks_team":     StatDef("team_stats", "defSacks",     "SUM", None, "defense"),
    "ptsAgainst":        StatDef("standings",  "ptsAgainst",   "SUM", None, "defense"),
    # --- Team turnover ---
    "tODiff":       StatDef("team_stats", "tODiff",       "SUM", None, "offense"),
    "tOGiveAways":  StatDef("team_stats", "tOGiveAways",  "SUM", None, "offense"),
    "tOTakeaways":  StatDef("team_stats", "tOTakeaways",  "SUM", None, "defense"),
    # --- Penalties ---
    "penalties":    StatDef("team_stats", "penalties",    "SUM", None, "offense"),
    "penaltyYds":   StatDef("team_stats", "penaltyYds",  "SUM", None, "offense"),
}

# Keyword → column name mapping (longest-first for matching)
_STAT_KEYWORDS: dict[str, str] = {
    'passing touchdowns': 'passTDs',
    'passing yards': 'passYds',
    'passing tds': 'passTDs',
    'pass tds': 'passTDs',
    'pass yards': 'passYds',
    'interceptions thrown': 'passInts',
    'passer rating': 'passerRating',
    'completion percentage': 'passCompPct',
    'completions': 'passComp',
    'rushing touchdowns': 'rushTDs',
    'rushing yards': 'rushYds',
    'rushing tds': 'rushTDs',
    'rush yards': 'rushYds',
    'rush tds': 'rushTDs',
    'fumbles': 'rushFum',
    'receiving touchdowns': 'recTDs',
    'receiving yards': 'recYds',
    'receiving tds': 'recTDs',
    'receptions': 'recCatches',
    'catches': 'recCatches',
    'drops': 'recDrops',
    'yards after catch': 'recYdsAfterCatch',
    'forced fumbles': 'defForcedFum',
    'fumble recoveries': 'defFumRec',
    'defensive tds': 'defTDs',
    'defensive touchdowns': 'defTDs',
    'pass deflections': 'defDeflections',
    'deflections': 'defDeflections',
    'tackles': 'defTotalTackles',
    'sacks': 'defSacks',
    'interceptions': 'defInts',
}
_STAT_KEYS_SORTED = sorted(_STAT_KEYWORDS.keys(), key=len, reverse=True)


def resolve_stat_keyword(keyword: str) -> StatDef | None:
    """Resolve a natural-language stat keyword to a StatDef."""
    kw = keyword.lower().strip()
    for key in _STAT_KEYS_SORTED:
        if key in kw:
            col = _STAT_KEYWORDS[key]
            return STAT_DEFS.get(col)
    return None
