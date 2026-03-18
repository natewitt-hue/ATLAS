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
    "rushTDs":          StatDef("offensive_stats", "rushTDs",          "SUM", None, "offense"),
    "rushYds":          StatDef("offensive_stats", "rushYds",          "SUM", None, "offense"),
    "rushFum":          StatDef("offensive_stats", "rushFum",          "SUM", None, "offense"),
    "rushYdsPerAtt":    StatDef("offensive_stats", "rushYdsPerAtt",    "AVG", None, "offense"),
    "rushBrokenTackles":StatDef("offensive_stats", "rushBrokenTackles","SUM", None, "offense"),
    # --- Receiving (no position filter) ---
    "recTDs":       StatDef("offensive_stats", "recTDs",       "SUM", None, "offense"),
    "recYds":       StatDef("offensive_stats", "recYds",       "SUM", None, "offense"),
    "recCatches":   StatDef("offensive_stats", "recCatches",   "SUM", None, "offense"),
    "recDrops":         StatDef("offensive_stats", "recDrops",         "SUM", None, "offense"),
    "recYdsPerCatch":   StatDef("offensive_stats", "recYdsPerCatch",   "AVG", None, "offense"),
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


# ---------------------------------------------------------------------------
# Layer 2: Composable QueryBuilder
# ---------------------------------------------------------------------------
class Query:
    """
    Composable SQL builder with domain guards.

    Usage:
        rows, err = (Query("offensive_stats")
            .filter(season=6, stage="regular")
            .stat("passYds")
            .sort("best")
            .limit(10)
            .execute())
    """

    # Stage name → stageIndex mapping
    _STAGE_MAP = {"preseason": "0", "regular": "1", "playoffs": "2"}

    def __init__(self, table: str):
        self._table = table
        self._selects: list[str] = []
        self._wheres: list[str] = []
        self._params: list[Any] = []
        self._group_bys: list[str] = []
        self._having: str | None = None
        self._order_by: str | None = None
        self._limit_val: int | None = None
        self._stat_def: StatDef | None = None
        self._sort_mode: str | None = None  # "best" or "worst"
        self._aggregates: dict[str, str] = {}  # col → agg_func

    def select(self, *columns: str) -> "Query":
        self._selects.extend(columns)
        return self

    def filter(self, **kwargs) -> "Query":
        """Add WHERE filters. Supports: season, stage, pos, user, team."""
        for key, val in kwargs.items():
            if key == "season":
                self._wheres.append("seasonIndex = ?")
                self._params.append(str(val))
            elif key == "stage":
                stage_val = self._STAGE_MAP.get(str(val).lower(), str(val))
                self._wheres.append("stageIndex = ?")
                self._params.append(stage_val)
            elif key == "pos":
                self._wheres.append("pos = ?")
                self._params.append(str(val))
            elif key == "user":
                self._wheres.append("(homeUser = ? OR awayUser = ?)")
                self._params.extend([str(val), str(val)])
            elif key == "team":
                self._wheres.append("(homeTeamName = ? OR awayTeamName = ? OR teamName = ?)")
                self._params.extend([str(val), str(val), str(val)])
            else:
                self._wheres.append(f"{key} = ?")
                self._params.append(str(val))
        return self

    def where(self, clause: str, *params) -> "Query":
        """Add a raw WHERE clause. Values MUST be passed as params."""
        _validate_where_clause(clause)
        self._wheres.append(clause)
        self._params.extend(params)
        return self

    def stat(self, stat_name: str) -> "Query":
        """Set the stat to query. Applies domain rules from STAT_DEFS."""
        sd = STAT_DEFS.get(stat_name)
        if sd is None:
            sd = resolve_stat_keyword(stat_name)
        if sd is None:
            raise ValueError(f"Unknown stat: {stat_name}")
        self._stat_def = sd
        # Auto-apply position filter
        if sd.pos:
            self.filter(pos=sd.pos)
        return self

    def sort(self, mode: str) -> "Query":
        """Set sort mode: 'best' or 'worst'. Domain rules applied at build time."""
        if mode not in ("best", "worst"):
            raise ValueError(f"sort mode must be 'best' or 'worst', got '{mode}'")
        self._sort_mode = mode
        return self

    def aggregate(self, **kwargs) -> "Query":
        """Add aggregations: aggregate(passYds='SUM', passTDs='SUM')."""
        self._aggregates.update(kwargs)
        return self

    def group_by(self, *columns: str) -> "Query":
        self._group_bys.extend(columns)
        return self

    def having(self, clause: str) -> "Query":
        self._having = clause
        return self

    def sort_by(self, column: str, direction: str = "DESC") -> "Query":
        """Explicit sort (bypasses domain guards). Use sort() when possible.
        If column already contains CAST or commas, it's treated as a raw expression."""
        d = direction.upper()
        if d not in ("ASC", "DESC"):
            raise ValueError(f"direction must be ASC or DESC, got '{d}'")
        # If column is already a complex expression (contains CAST, parens, or commas),
        # use it as-is to avoid double-wrapping
        if "CAST(" in column.upper() or "," in column or "(" in column:
            self._order_by = f"{column} {d}"
        else:
            text_cols = ("extendedName", "teamName", "fullName", "player_name",
                         "stat_value", "margin", "total_pts")
            cast = f"CAST({column} AS REAL)" if column not in text_cols else column
            self._order_by = f"{cast} {d}"
        return self

    def limit(self, n: int) -> "Query":
        self._limit_val = n
        return self

    # Valid table names (prevents empty-table queries)
    _VALID_TABLES = {
        "games", "teams", "standings", "offensive_stats", "defensive_stats",
        "team_stats", "trades", "players", "player_abilities", "owner_tenure",
        "player_draft_map",
    }

    def build(self) -> tuple[str, tuple]:
        """Build the SQL query string and parameter tuple."""
        sd = self._stat_def
        selects = list(self._selects)
        wheres = list(self._wheres)
        params = list(self._params)
        group_bys = list(self._group_bys)
        having = self._having
        order_by = self._order_by
        table = self._table

        # If a stat was set, apply domain rules
        if sd:
            actual_col = sd.column
            actual_agg = sd.agg
            cast_type = "REAL" if actual_agg == "AVG" else "INTEGER"

            # Efficiency vs volume: "worst" on a stat with efficiency_alt
            if self._sort_mode == "worst" and sd.efficiency_alt:
                alt = STAT_DEFS.get(sd.efficiency_alt)
                if alt:
                    actual_col = alt.column
                    actual_agg = alt.agg
                    cast_type = "REAL"

            # Build aggregation select
            agg_expr = f"{actual_agg}(CAST({actual_col} AS {cast_type}))"
            if actual_agg == "AVG":
                agg_expr = f"ROUND({agg_expr}, 1)"
            selects.append(f"{agg_expr} AS stat_value")

            # Auto group by player name if it's a player stat table
            if sd.table in ("offensive_stats", "defensive_stats") and not group_bys:
                selects = ["extendedName AS player_name", "teamName"] + selects
                group_bys = ["extendedName", "teamName"]

            # Sort direction: domain-aware
            if self._sort_mode:
                sort_dir = _resolve_sort_direction(sd, self._sort_mode)
                order_by = f"stat_value {sort_dir}"

            # Min games filter for "worst" queries
            if self._sort_mode == "worst":
                having = f"COUNT(*) >= {sd.min_games}"

            table = sd.table

        # Process explicit aggregates (non-stat path)
        agg_selects = []
        for col, agg_func in self._aggregates.items():
            cast_type = "REAL" if agg_func == "AVG" else "INTEGER"
            agg_selects.append(f"{agg_func}(CAST({col} AS {cast_type})) AS {col}")
        if agg_selects:
            selects.extend(agg_selects)

        # Validate table name
        if table not in self._VALID_TABLES:
            raise ValueError(f"Invalid table name: '{table}'. Must be one of: {self._VALID_TABLES}")

        # Build SQL
        select_str = ", ".join(selects) if selects else "*"
        sql = f"SELECT {select_str} FROM {table}"

        # Completed games filter (auto-applied for games table)
        if table == "games" and not any("status" in w for w in wheres):
            wheres.append("status IN ('2', '3')")

        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        if group_bys:
            sql += " GROUP BY " + ", ".join(group_bys)
        if having:
            sql += f" HAVING {having}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if self._limit_val:
            sql += f" LIMIT {self._limit_val}"

        return sql, tuple(params)

    def execute(self) -> tuple[list[dict], str | None]:
        """Build and execute the query, returning (rows, error)."""
        sql, params = self.build()
        return _run_sql(sql, params)


def _resolve_sort_direction(sd: StatDef, mode: str) -> str:
    """
    Resolve sort direction based on stat category and mode.

    For offense: best=DESC (most), worst=ASC (fewest)
    For defense team stats: best=ASC (fewest yards allowed), worst=DESC (most)
    For individual defensive stats (tackles, sacks, ints): best=DESC (most), worst=ASC
    """
    if sd.table == "team_stats" and sd.category == "defense":
        # Team defense: best = fewest yards = ASC
        return "ASC" if mode == "best" else "DESC"
    if sd.table == "standings" and sd.column == "ptsAgainst":
        # Points against: best = fewest = ASC
        return "ASC" if mode == "best" else "DESC"
    # Everything else (offense, individual defense): best = most = DESC
    return "DESC" if mode == "best" else "ASC"


# Known safe column names from all tables (for .where() validation)
_KNOWN_COLUMNS = {
    # games
    "id", "scheduleId", "seasonIndex", "stageIndex", "weekIndex",
    "homeTeamId", "awayTeamId", "homeTeamName", "awayTeamName",
    "homeScore", "awayScore", "status", "homeUser", "awayUser",
    "winner_user", "loser_user", "winner_team", "loser_team",
    # offensive_stats
    "fullName", "extendedName", "gameId", "teamId", "teamName", "rosterId", "pos",
    "passAtt", "passComp", "passCompPct", "passTDs", "passInts", "passYds",
    "passSacks", "passerRating", "rushAtt", "rushYds", "rushTDs", "rushFum",
    "recCatches", "recDrops", "recYds", "recTDs", "recYdsAfterCatch",
    # defensive_stats
    "statId", "defTotalTackles", "defSacks", "defInts", "defForcedFum",
    "defFumRec", "defTDs", "defDeflections",
    # team_stats
    "offTotalYds", "offPassYds", "offRushYds", "defTotalYds", "defPassYds",
    "defRushYds", "ptsFor", "ptsAgainst", "tODiff",
    # players
    "firstName", "lastName", "age", "playerBestOvr", "dev", "isFA", "isOnIR",
    "jerseyNum", "college", "yearsPro", "capHit",
    # standings
    "totalWins", "totalLosses", "totalTies", "divisionName", "conferenceName", "seed", "winPct",
    # player_draft_map
    "drafting_team", "drafting_season", "draftRound", "draftPick", "was_traded",
    # owner_tenure
    "userName", "games_played",
    # player_abilities
    "title", "description",
    # trades
    "team1Name", "team2Name", "team1Sent", "team2Sent",
}


def _validate_where_clause(clause: str) -> None:
    """Validate a raw WHERE clause. Blocks dangerous SQL and validates column refs."""
    upper = clause.upper().strip()
    # Block dangerous keywords
    dangerous = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "EXEC",
                 "EXECUTE", "UNION", "--", ";", "/*"}
    for d in dangerous:
        if d in upper:
            raise ValueError(f"Unsafe SQL pattern in WHERE clause: {d}")
    # Strip string literals before identifier validation so quoted values
    # (e.g. 'approved', 'accepted') are not mistaken for column names.
    import re
    stripped = re.sub(r"'[^']*'", "''", clause)
    identifiers = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', stripped)
    safe_keywords = {"IN", "NOT", "LIKE", "BETWEEN", "IS", "NULL", "AND", "OR",
                     "CAST", "AS", "INTEGER", "REAL", "TEXT", "ABS", "COUNT"}
    for ident in identifiers:
        if ident.upper() not in safe_keywords and ident not in _KNOWN_COLUMNS:
            raise ValueError(f"Unknown column in WHERE clause: {ident}")


# ---------------------------------------------------------------------------
# Layer 3: Utility Functions
# ---------------------------------------------------------------------------

# Team aliases (from codex_intents.py:649-682)
_TEAM_ALIASES: dict[str, str] = {
    'cardinals': 'Cardinals', 'cards': 'Cardinals', 'ari': 'Cardinals', 'arizona': 'Cardinals',
    'falcons': 'Falcons', 'atl': 'Falcons', 'atlanta': 'Falcons',
    'ravens': 'Ravens', 'bal': 'Ravens', 'baltimore': 'Ravens',
    'bills': 'Bills', 'buf': 'Bills', 'buffalo': 'Bills',
    'panthers': 'Panthers', 'car': 'Panthers', 'carolina': 'Panthers',
    'bears': 'Bears', 'chi': 'Bears', 'chicago': 'Bears',
    'bengals': 'Bengals', 'cin': 'Bengals', 'cincinnati': 'Bengals',
    'browns': 'Browns', 'cle': 'Browns', 'cleveland': 'Browns',
    'cowboys': 'Cowboys', 'dal': 'Cowboys', 'dallas': 'Cowboys',
    'broncos': 'Broncos', 'den': 'Broncos', 'denver': 'Broncos',
    'lions': 'Lions', 'det': 'Lions', 'detroit': 'Lions',
    'packers': 'Packers', 'gb': 'Packers', 'green bay': 'Packers',
    'texans': 'Texans', 'hou': 'Texans', 'houston': 'Texans',
    'colts': 'Colts', 'ind': 'Colts', 'indianapolis': 'Colts',
    'jaguars': 'Jaguars', 'jags': 'Jaguars', 'jax': 'Jaguars', 'jacksonville': 'Jaguars',
    'chiefs': 'Chiefs', 'kc': 'Chiefs', 'kansas city': 'Chiefs',
    'raiders': 'Raiders', 'lv': 'Raiders', 'las vegas': 'Raiders',
    'chargers': 'Chargers', 'lac': 'Chargers',
    'rams': 'Rams', 'lar': 'Rams', 'la rams': 'Rams',
    'dolphins': 'Dolphins', 'mia': 'Dolphins', 'miami': 'Dolphins',
    'vikings': 'Vikings', 'min': 'Vikings', 'minnesota': 'Vikings',
    'patriots': 'Patriots', 'pats': 'Patriots', 'ne': 'Patriots', 'new england': 'Patriots',
    'saints': 'Saints', 'no': 'Saints', 'new orleans': 'Saints',
    'giants': 'Giants', 'nyg': 'Giants', 'ny giants': 'Giants',
    'jets': 'Jets', 'nyj': 'Jets', 'ny jets': 'Jets',
    'eagles': 'Eagles', 'phi': 'Eagles', 'philadelphia': 'Eagles', 'philly': 'Eagles',
    'steelers': 'Steelers', 'pit': 'Steelers', 'pittsburgh': 'Steelers',
    '49ers': '49ers', 'niners': '49ers', 'sf': '49ers', 'san francisco': '49ers',
    'seahawks': 'Seahawks', 'hawks': 'Seahawks', 'sea': 'Seahawks', 'seattle': 'Seahawks',
    'buccaneers': 'Buccaneers', 'bucs': 'Buccaneers', 'tb': 'Buccaneers',
    'tampa': 'Buccaneers', 'tampa bay': 'Buccaneers',
    'titans': 'Titans', 'ten': 'Titans', 'tennessee': 'Titans',
    'commanders': 'Commanders', 'was': 'Commanders', 'washington': 'Commanders',
}


def current_season() -> int:
    """Return current TSL season number."""
    return dm.CURRENT_SEASON


def current_week() -> int:
    """Return current TSL week number (1-based)."""
    return dm.CURRENT_WEEK


def resolve_team(name: str) -> str | None:
    """Resolve a team name/alias to canonical nickName. Returns None if not found."""
    return _TEAM_ALIASES.get(name.lower().strip())


def resolve_user(name: str) -> str | None:
    """Resolve a user name/alias to canonical DB username. Returns None if not found."""
    try:
        from build_member_db import get_alias_map
        alias_map = get_alias_map()
        lower = name.lower().strip()
        for key, val in alias_map.items():
            if key.lower() == lower:
                return val
        return None
    except (ImportError, Exception):
        return None


# ---------------------------------------------------------------------------
# Layer 1: High-Level Domain Functions
# ---------------------------------------------------------------------------

def stat_leaders(
    stat: str,
    season: int | None = None,
    sort: str = "best",
    limit: int = 10,
    pos_group: list[str] | None = None,
) -> tuple[list[dict], str | None]:
    """Player stat leaders with full domain rules (sort, efficiency, min games, pos).

    Args:
        pos_group: Override position filter with a list of positions (e.g. ["HB", "FB"]).
                   When set, suppresses the auto-applied pos filter from STAT_DEFS.
    """
    sd = STAT_DEFS.get(stat) or resolve_stat_keyword(stat)
    if sd is None:
        return [], f"Unknown stat: {stat}"

    if pos_group:
        # Build query without .stat() auto-pos, then set stat_def manually
        q = Query(sd.table)
        q._stat_def = sd
        q.sort(sort)
        placeholders = ", ".join("?" for _ in pos_group)
        q.where(f"pos IN ({placeholders})", *pos_group)
        q.limit(limit)
    else:
        q = Query(sd.table).stat(stat).sort(sort).limit(limit)

    if season is not None:
        q.filter(season=season)
    else:
        q.filter(stage="regular")
    return q.execute()


def team_stat_leaders(
    stat: str,
    season: int | None = None,
    sort: str = "best",
    limit: int = 10,
) -> tuple[list[dict], str | None]:
    """Team stat leaders. Domain-aware: defense sort inverted."""
    sd = STAT_DEFS.get(stat)
    if sd is None:
        return [], f"Unknown stat: {stat}"

    if sd.table == "standings":
        # Standings is pre-aggregated — direct query
        q = Query("standings").select("teamName", f"CAST({sd.column} AS INTEGER) AS stat_value")
        sort_dir = _resolve_sort_direction(sd, sort)
        q.sort_by(sd.column, sort_dir)
    else:
        # team_stats needs aggregation per team
        cast_type = "REAL" if sd.agg == "AVG" else "INTEGER"
        q = (Query("team_stats")
             .select("teamName",
                     f"{sd.agg}(CAST({sd.column} AS {cast_type})) AS stat_value")
             .group_by("teamName"))
        if season is not None:
            q.filter(season=season)
        else:
            q.filter(stage="regular")
        sort_dir = _resolve_sort_direction(sd, sort)
        q.sort_by("stat_value", sort_dir)

    q.limit(limit)
    return q.execute()


def h2h(
    user1: str,
    user2: str,
    season: int | None = None,
) -> tuple[list[dict], str | None]:
    """Head-to-head record between two owners."""
    sql = """
        SELECT winner_user, COUNT(*) as wins
        FROM games
        WHERE status IN ('2','3')
          AND ((homeUser = ? AND awayUser = ?) OR (homeUser = ? AND awayUser = ?))
    """
    params = [user1, user2, user2, user1]
    if season is not None:
        sql += " AND seasonIndex = ?"
        params.append(str(season))
    sql += " GROUP BY winner_user"
    return _run_sql(sql, tuple(params))


def owner_record(
    user: str,
    season: int | None = None,
) -> tuple[list[dict], str | None]:
    """Win/loss record for an owner, optionally filtered by season."""
    sql = """
        SELECT
            COUNT(CASE WHEN winner_user = ? THEN 1 END) as total_wins,
            COUNT(CASE WHEN loser_user = ? THEN 1 END) as total_losses
        FROM games
        WHERE status IN ('2','3')
          AND (homeUser = ? OR awayUser = ?)
          AND stageIndex = '1'
    """
    params = [user, user, user, user]
    if season is not None:
        sql += " AND seasonIndex = ?"
        params.append(str(season))
    return _run_sql(sql, tuple(params))


def team_record_query(
    team: str,
    season: int | None = None,
) -> tuple[list[dict], str | None]:
    """Win/loss record for a team (by nickName), optionally by season."""
    canonical = resolve_team(team) or team
    sql = """
        SELECT
            COUNT(CASE WHEN winner_team = ? THEN 1 END) as total_wins,
            COUNT(CASE WHEN loser_team = ? THEN 1 END) as total_losses
        FROM games
        WHERE status IN ('2','3')
          AND (homeTeamName = ? OR awayTeamName = ?)
          AND stageIndex = '1'
    """
    params = [canonical, canonical, canonical, canonical]
    if season is not None:
        sql += " AND seasonIndex = ?"
        params.append(str(season))
    return _run_sql(sql, tuple(params))


def streak(user: str) -> tuple[list[dict], str | None]:
    """Current win/loss streak for an owner."""
    sql = """
        SELECT winner_user, loser_user, homeTeamName, awayTeamName,
               homeScore, awayScore, seasonIndex, weekIndex
        FROM games
        WHERE status IN ('2','3')
          AND (homeUser = ? OR awayUser = ?)
        ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
        LIMIT 20
    """
    return _run_sql(sql, (user, user))


def standings(
    division: str | None = None,
    conference: str | None = None,
) -> tuple[list[dict], str | None]:
    """Current standings, optionally filtered by division or conference."""
    q = (Query("standings")
         .select("teamName", "totalWins", "totalLosses", "totalTies",
                 "divisionName", "conferenceName", "seed", "winPct")
         .sort_by("CAST(seed AS INTEGER)", "ASC"))
    if division:
        q.where("divisionName = ?", division)
    if conference:
        q.where("conferenceName = ?", conference)
    return q.execute()


def recent_games(
    user: str,
    limit: int = 5,
    opponent: str | None = None,
) -> tuple[list[dict], str | None]:
    """Recent games for an owner, optionally filtered by opponent."""
    q = (Query("games")
         .select("seasonIndex", "weekIndex", "homeTeamName", "awayTeamName",
                 "homeScore", "awayScore", "homeUser", "awayUser",
                 "winner_user", "loser_user")
         .where("(homeUser = ? OR awayUser = ?)", user, user)
         .sort_by("CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER)", "DESC")
         .limit(limit))
    if opponent:
        q.where("(homeUser = ? OR awayUser = ?)", opponent, opponent)
    return q.execute()


def roster(
    team: str,
    pos: str | None = None,
    sort_by: str = "playerBestOvr",
) -> tuple[list[dict], str | None]:
    """Team roster from players table."""
    canonical = resolve_team(team) or team
    q = (Query("players")
         .select("firstName", "lastName", "pos", "playerBestOvr", "age",
                 "dev", "teamName")
         .where("teamName = ?", canonical)
         .sort_by(sort_by, "DESC"))
    if pos:
        q.where("pos = ?", pos)
    return q.execute()


def free_agents(
    pos: str | None = None,
    min_ovr: int | None = None,
) -> tuple[list[dict], str | None]:
    """Free agents, optionally filtered by position."""
    q = (Query("players")
         .select("firstName", "lastName", "pos", "playerBestOvr", "age", "dev")
         .where("isFA = '1'")
         .sort_by("playerBestOvr", "DESC")
         .limit(25))
    if pos:
        q.where("pos = ?", pos)
    if min_ovr:
        q.where("CAST(playerBestOvr AS INTEGER) >= ?", str(min_ovr))
    return q.execute()


def draft_picks(
    team: str | None = None,
    season: int | None = None,
    round: int | None = None,
) -> tuple[list[dict], str | None]:
    """Draft history from player_draft_map."""
    q = (Query("player_draft_map")
         .select("extendedName", "drafting_team", "drafting_season",
                 "draftRound", "draftPick", "pos", "playerBestOvr", "dev")
         .sort_by("draftRound", "ASC"))
    if team:
        canonical = resolve_team(team) or team
        q.where("drafting_team = ?", canonical)
    if season:
        q.where("drafting_season = ?", str(season))
    if round:
        # draftRound mapping: 2=R1, 3=R2, ..., 8=R7
        q.where("draftRound = ?", str(round + 1))
    return q.execute()


def owner_history(
    user: str | None = None,
    team: str | None = None,
) -> tuple[list[dict], str | None]:
    """Owner tenure history."""
    q = (Query("owner_tenure")
         .select("teamName", "userName", "seasonIndex", "games_played")
         .sort_by("CAST(seasonIndex AS INTEGER)", "ASC"))
    if user:
        q.where("userName = ?", user)
    if team:
        canonical = resolve_team(team) or team
        q.where("teamName = ?", canonical)
    return q.execute()


def trades(
    team: str | None = None,
    season: int | None = None,
    user: str | None = None,
) -> tuple[list[dict], str | None]:
    """Trade history."""
    q = (Query("trades")
         .select("team1Name", "team2Name", "team1Sent", "team2Sent",
                 "seasonIndex", "weekIndex", "status")
         .where("status IN ('approved', 'accepted')"))
    if team:
        canonical = resolve_team(team) or team
        q.where("(team1Name = ? OR team2Name = ?)", canonical, canonical)
    if season:
        q.filter(season=season)
    if user:
        # Look up teams this user has owned and filter by ANY of those team names
        tenure_rows, _ = owner_history(user=user)
        if tenure_rows:
            teams = list({r["teamName"] for r in tenure_rows})
            placeholders = " OR ".join(
                "(team1Name = ? OR team2Name = ?)" for _ in teams
            )
            team_params = []
            for t in teams:
                team_params.extend([t, t])
            q.where(f"({placeholders})", *team_params)
    return q.execute()


def game_extremes(
    type: str,
    season: int | None = None,
    limit: int = 5,
) -> tuple[list[dict], str | None]:
    """Game records: blowout, closest, highest scoring, lowest scoring."""
    base_cols = ("seasonIndex", "weekIndex", "homeTeamName", "awayTeamName",
                 "homeScore", "awayScore", "homeUser", "awayUser")
    q = Query("games").select(*base_cols)

    if type == "blowout":
        q.select("ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin")
        q.sort_by("margin", "DESC")
    elif type == "closest":
        q.select("ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin")
        q.sort_by("margin", "ASC")
    elif type == "highest":
        q.select("(CAST(homeScore AS INTEGER) + CAST(awayScore AS INTEGER)) AS total_pts")
        q.sort_by("total_pts", "DESC")
    elif type == "lowest":
        q.select("(CAST(homeScore AS INTEGER) + CAST(awayScore AS INTEGER)) AS total_pts")
        q.sort_by("total_pts", "ASC")
    else:
        return [], f"Unknown extreme type: {type}"

    if season:
        q.filter(season=season)

    q.limit(limit)
    return q.execute()


def abilities(
    team: str | None = None,
    player: str | None = None,
) -> tuple[list[dict], str | None]:
    """Player abilities (X-Factor, Superstar, etc.)."""
    q = Query("player_abilities").select("firstName", "lastName", "teamName",
                                          "title", "description")
    if team:
        canonical = resolve_team(team) or team
        q.where("teamName = ?", canonical)
    if player:
        q.where("(firstName || ' ' || lastName) LIKE ?", f"%{player}%")
    return q.execute()


# Cross-season analysis functions
def compare_seasons(
    stat: str,
    user_or_team: str,
    season1: int,
    season2: int,
) -> tuple[list[dict], str | None]:
    """Compare a stat between two seasons for a user or team."""
    rows1, err1 = stat_leaders(stat, season=season1, sort="best", limit=50)
    rows2, err2 = stat_leaders(stat, season=season2, sort="best", limit=50)
    if err1 or err2:
        return [], err1 or err2
    return rows1 + rows2, None


def improvement_leaders(
    stat: str,
    season1: int,
    season2: int,
    limit: int = 10,
) -> tuple[list[dict], str | None]:
    """Players/teams who improved most in a stat between two seasons."""
    rows1, err1 = stat_leaders(stat, season=season1, sort="best", limit=50)
    rows2, err2 = stat_leaders(stat, season=season2, sort="best", limit=50)
    if err1 or err2:
        return [], err1 or err2
    s1_map = {r.get("player_name", ""): r.get("stat_value", 0) for r in rows1}
    deltas = []
    for r in rows2:
        name = r.get("player_name", "")
        if name in s1_map:
            try:
                delta = float(r.get("stat_value", 0)) - float(s1_map[name])
                deltas.append({**r, "prev_value": s1_map[name], "delta": delta})
            except (ValueError, TypeError):
                pass
    deltas.sort(key=lambda x: x.get("delta", 0), reverse=True)
    return deltas[:limit], None


def career_trajectory(
    user: str,
    stat: str,
) -> tuple[list[dict], str | None]:
    """Stat per season over an owner's career."""
    sd = STAT_DEFS.get(stat) or resolve_stat_keyword(stat)
    if sd is None:
        return [], f"Unknown stat: {stat}"

    cast_type = "REAL" if sd.agg == "AVG" else "INTEGER"
    pos_clause = ""
    params = [user, user]
    if sd.pos:
        pos_clause = "AND pos = ?"
        params.append(sd.pos)

    sql = f"""
        SELECT seasonIndex,
               {sd.agg}(CAST({sd.column} AS {cast_type})) AS stat_value,
               COUNT(*) AS games_played
        FROM {sd.table}
        WHERE (teamName IN (SELECT teamName FROM owner_tenure WHERE userName = ?))
          AND seasonIndex IN (SELECT CAST(seasonIndex AS TEXT) FROM owner_tenure WHERE userName = ?)
          AND stageIndex = '1'
          {pos_clause}
        GROUP BY seasonIndex
        ORDER BY CAST(seasonIndex AS INTEGER) ASC
    """
    return _run_sql(sql, tuple(params))
