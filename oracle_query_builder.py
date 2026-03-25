"""
oracle_query_builder.py — QueryBuilder API + DomainKnowledge Registry for Oracle v3
═══════════════════════════════════════════════════════════════════════════════════════
Phase 1 Foundation: Typed query API that encodes TSL domain rules mechanically.
The future Code-Gen Agent (Phase 3) will generate Python against this API.

Three layers:
  Layer 1: High-level domain functions (h2h, stat_leaders, roster, etc.)
  Layer 2: Composable Query builder (fluent SQL construction with domain guards)
  Layer 3: Utility functions (resolve_user, current_season, compare, etc.)

Domain guards enforced by code, not LLM suggestions:
  - Defense stat sort inversion (lower = better for team yards allowed)
  - Efficiency alternatives for "worst" queries (passYds worst → passerRating)
  - Automatic CAST wrapping for TEXT columns
  - Parameterized queries (no string interpolation)
  - Read-only enforcement (SELECT only)
  - Position filtering per stat definition
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger("oracle_query_builder")

# ── Lazy imports to avoid circular dependencies ──────────────────────────────
_dm = None
_codex_utils = None
_codex_intents = None


def _get_dm():
    global _dm
    if _dm is None:
        try:
            import data_manager as dm
            _dm = dm
        except ImportError:
            pass
    return _dm


def _get_codex_utils():
    global _codex_utils
    if _codex_utils is None:
        try:
            import codex_utils
            _codex_utils = codex_utils
        except ImportError:
            pass
    return _codex_utils


def _get_codex_intents():
    global _codex_intents
    if _codex_intents is None:
        try:
            import codex_intents
            _codex_intents = codex_intents
        except ImportError:
            pass
    return _codex_intents


# ══════════════════════════════════════════════════════════════════════════════
#  DOMAIN KNOWLEDGE REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class StatDef:
    """Typed definition for a single league stat.

    Encodes table, column, aggregation, position filter, category,
    and domain-specific sort behavior so the QueryBuilder can enforce
    correct SQL mechanically.
    """
    table: str                                       # "offensive_stats" | "defensive_stats" | "team_stats"
    column: str                                      # DB column name
    agg: Literal["SUM", "AVG", "MAX", "MIN"]         # Aggregation type
    pos_filter: str | None                           # "QB" or None (all positions)
    category: Literal["offense", "defense", "team"]  # Stat category
    efficiency_alt: str | None = None                # Column for "worst" queries
    invert_sort: bool = False                        # True = lower is better
    cast_type: Literal["INTEGER", "REAL"] = "INTEGER"


class DomainKnowledge:
    """Centralized stat definitions — single source of truth.

    Migrated from codex_intents.STAT_REGISTRY with added metadata:
    category, efficiency_alt, invert_sort, cast_type.
    """

    STATS: dict[str, StatDef] = {
        # ── Passing ──────────────────────────────────────────
        "passing touchdowns":   StatDef("offensive_stats", "passTDs",     "SUM", "QB", "offense"),
        "passing yards":        StatDef("offensive_stats", "passYds",     "SUM", "QB", "offense",
                                        efficiency_alt="passerRating"),
        "passing tds":          StatDef("offensive_stats", "passTDs",     "SUM", "QB", "offense"),
        "pass tds":             StatDef("offensive_stats", "passTDs",     "SUM", "QB", "offense"),
        "pass yards":           StatDef("offensive_stats", "passYds",     "SUM", "QB", "offense",
                                        efficiency_alt="passerRating"),
        "interceptions thrown": StatDef("offensive_stats", "passInts",    "SUM", "QB", "offense"),
        "passer rating":        StatDef("offensive_stats", "passerRating","AVG", "QB", "offense",
                                        cast_type="REAL"),
        "completion percentage":StatDef("offensive_stats", "passCompPct", "AVG", "QB", "offense",
                                        cast_type="REAL"),
        "completions":          StatDef("offensive_stats", "passComp",    "SUM", "QB", "offense"),

        # ── Rushing ──────────────────────────────────────────
        "rushing touchdowns":   StatDef("offensive_stats", "rushTDs",     "SUM", None, "offense"),
        "rushing yards":        StatDef("offensive_stats", "rushYds",     "SUM", None, "offense"),
        "rushing tds":          StatDef("offensive_stats", "rushTDs",     "SUM", None, "offense"),
        "rush yards":           StatDef("offensive_stats", "rushYds",     "SUM", None, "offense"),
        "rush tds":             StatDef("offensive_stats", "rushTDs",     "SUM", None, "offense"),
        "fumbles":              StatDef("offensive_stats", "rushFum",     "SUM", None, "offense"),

        # ── Receiving ────────────────────────────────────────
        "receiving touchdowns": StatDef("offensive_stats", "recTDs",           "SUM", None, "offense"),
        "receiving yards":      StatDef("offensive_stats", "recYds",           "SUM", None, "offense"),
        "receiving tds":        StatDef("offensive_stats", "recTDs",           "SUM", None, "offense"),
        "receptions":           StatDef("offensive_stats", "recCatches",       "SUM", None, "offense"),
        "catches":              StatDef("offensive_stats", "recCatches",       "SUM", None, "offense"),
        "drops":                StatDef("offensive_stats", "recDrops",         "SUM", None, "offense"),
        "yards after catch":    StatDef("offensive_stats", "recYdsAfterCatch", "SUM", None, "offense"),

        # ── Passing (extended) ───────────────────────────────
        "yards per attempt":    StatDef("offensive_stats", "passYdsPerAtt",      "AVG", "QB",   "offense", cast_type="REAL"),
        "pass attempts":        StatDef("offensive_stats", "passAtt",            "SUM", "QB",   "offense"),
        "sacks taken":          StatDef("offensive_stats", "passSacks",          "SUM", "QB",   "offense"),
        "longest pass":         StatDef("offensive_stats", "passLongest",        "MAX", "QB",   "offense"),

        # ── Rushing (extended) ───────────────────────────────
        "rush attempts":        StatDef("offensive_stats", "rushAtt",            "SUM", None,   "offense"),
        "yards per carry":      StatDef("offensive_stats", "rushYdsPerAtt",      "AVG", None,   "offense", cast_type="REAL"),
        "broken tackles":       StatDef("offensive_stats", "rushBrokenTackles",  "SUM", None,   "offense"),
        "yards after contact":  StatDef("offensive_stats", "rushYdsAfterContact","SUM", None,   "offense"),
        "longest rush":         StatDef("offensive_stats", "rushLongest",        "MAX", None,   "offense"),
        "20 yard runs":         StatDef("offensive_stats", "rush20PlusYds",      "SUM", None,   "offense"),

        # ── Receiving (extended) ─────────────────────────────
        "catch percentage":     StatDef("offensive_stats", "recCatchPct",        "AVG", None,   "offense", cast_type="REAL"),
        "yards per catch":      StatDef("offensive_stats", "recYdsPerCatch",     "AVG", None,   "offense", cast_type="REAL"),
        "yac per catch":        StatDef("offensive_stats", "recYacPerCatch",     "AVG", None,   "offense", cast_type="REAL"),
        "longest reception":    StatDef("offensive_stats", "recLongest",         "MAX", None,   "offense"),

        # ── Individual Defense (more = better for the defender) ──
        "forced fumbles":       StatDef("defensive_stats", "defForcedFum",     "SUM", None, "defense"),
        "fumble recoveries":    StatDef("defensive_stats", "defFumRec",        "SUM", None, "defense"),
        "defensive tds":        StatDef("defensive_stats", "defTDs",           "SUM", None, "defense"),
        "defensive touchdowns": StatDef("defensive_stats", "defTDs",           "SUM", None, "defense"),
        "pass deflections":     StatDef("defensive_stats", "defDeflections",   "SUM", None, "defense"),
        "deflections":          StatDef("defensive_stats", "defDeflections",   "SUM", None, "defense"),
        "tackles":              StatDef("defensive_stats", "defTotalTackles",  "SUM", None, "defense"),
        "sacks":                StatDef("defensive_stats", "defSacks",         "SUM", None, "defense"),
        "interceptions":        StatDef("defensive_stats", "defInts",          "SUM", None, "defense"),

        # ── Individual Defense (extended) ────────────────────
        "catches allowed":      StatDef("defensive_stats", "defCatchAllowed",   "SUM", None,   "defense", invert_sort=True),
        "int return yards":     StatDef("defensive_stats", "defIntReturnYds",   "SUM", None,   "defense"),
        "safeties":             StatDef("defensive_stats", "defSafeties",       "SUM", None,   "defense"),

        # ── Team Defense (fewer yards allowed = better → invert_sort) ──
        "team total yards allowed": StatDef("team_stats", "defTotalYds",  "SUM", None, "team",
                                            invert_sort=True),
        "team pass yards allowed":  StatDef("team_stats", "defPassYds",   "SUM", None, "team",
                                            invert_sort=True),
        "team rush yards allowed":  StatDef("team_stats", "defRushYds",   "SUM", None, "team",
                                            invert_sort=True),
        "team sacks":               StatDef("team_stats", "defSacks",     "SUM", None, "team"),
        "team takeaways":           StatDef("team_stats", "tOTakeaways",  "SUM", None, "team"),
        "team turnover diff":       StatDef("team_stats", "tODiff",       "SUM", None, "team"),

        # ── Team Offense ─────────────────────────────────────
        "team total yards":     StatDef("team_stats", "offTotalYds", "SUM", None, "team"),
        "team pass yards":      StatDef("team_stats", "offPassYds",  "SUM", None, "team"),
        "team rush yards":      StatDef("team_stats", "offRushYds",  "SUM", None, "team"),
        "team pass tds":        StatDef("team_stats", "offPassTDs",  "SUM", None, "team"),
        "team rush tds":        StatDef("team_stats", "offRushTDs",  "SUM", None, "team"),
        "penalties":            StatDef("team_stats", "penalties",    "SUM", None, "team"),
        "penalty yards":        StatDef("team_stats", "penaltyYds",  "SUM", None, "team"),

        # ── Aliases (shorthand for NL matching) ──────────────
        "ypa":                  StatDef("offensive_stats", "passYdsPerAtt",      "AVG", "QB",   "offense", cast_type="REAL"),
        "ypc":                  StatDef("offensive_stats", "rushYdsPerAtt",      "AVG", None,   "offense", cast_type="REAL"),
        "catch pct":            StatDef("offensive_stats", "recCatchPct",        "AVG", None,   "offense", cast_type="REAL"),
        "yac":                  StatDef("offensive_stats", "recYacPerCatch",     "AVG", None,   "offense", cast_type="REAL"),
        "broken tackle rate":   StatDef("offensive_stats", "rushBrokenTackles",  "SUM", None,   "offense"),  # alias: "rate" = frequency/count, not a ratio
    }

    # Pre-sorted keys (longest first) for correct substring matching
    STAT_KEYS_SORTED: list[str] = sorted(STATS.keys(), key=len, reverse=True)

    @classmethod
    def lookup(cls, text: str) -> tuple[str, StatDef] | None:
        """Find the best matching stat definition for a text query.

        Returns (matched_key, StatDef) or None.
        Uses longest-first matching to prefer "passing touchdowns" over "pass tds".
        """
        text_lower = text.lower()
        for key in cls.STAT_KEYS_SORTED:
            if key in text_lower:
                return key, cls.STATS[key]
        return None

    @classmethod
    def get(cls, name: str) -> StatDef | None:
        """Get a stat definition by exact key."""
        return cls.STATS.get(name.lower())

    @classmethod
    def by_category(cls, category: str) -> dict[str, StatDef]:
        """Get all stats in a category."""
        return {k: v for k, v in cls.STATS.items() if v.category == category}

    @classmethod
    def columns(cls) -> set[str]:
        """All unique column names across all stat definitions."""
        return {sd.column for sd in cls.STATS.values()}


# ══════════════════════════════════════════════════════════════════════════════
#  QUERY BUILDER (Layer 2 — Composable SQL Construction)
# ══════════════════════════════════════════════════════════════════════════════

_VALID_TABLES = frozenset({
    "games", "teams", "standings", "offensive_stats", "defensive_stats",
    "team_stats", "trades", "players", "player_abilities",
    "owner_tenure", "player_draft_map",
})


class Query:
    """Fluent SQL builder with domain-aware guards.

    Usage::

        sql, params = (
            Query("offensive_stats")
            .select("fullName", "teamName")
            .filter(season=6, stage="regular")
            .aggregate(passYds="SUM", passTDs="SUM")
            .group_by("fullName", "teamName")
            .sort_by("passYds", direction="best")
            .limit(10)
            .build()
        )
    """

    def __init__(self, table: str):
        if table not in _VALID_TABLES:
            raise ValueError(f"Invalid table: {table!r}. Valid: {', '.join(sorted(_VALID_TABLES))}")
        self._table = table
        self._selects: list[str] = []
        self._aggregates: dict[str, str] = {}   # col -> agg_type
        self._filters: dict[str, Any] = {}
        self._wheres: list[str] = []
        self._where_params: list[Any] = []
        self._group_bys: list[str] = []
        self._havings: list[str] = []
        self._having_params: list[Any] = []
        self._order_col: str | None = None
        self._order_dir: str = "DESC"
        self._limit_n: int | None = None
        self._pos_filter: str | None = None

    # ── Fluent builder methods ────────────────────────────────────────────

    def select(self, *cols: str) -> Query:
        """Add columns to SELECT clause."""
        self._selects.extend(cols)
        return self

    def filter(self, **kwargs: Any) -> Query:
        """Add equality filters mapped to standard column names.

        Supported keys:
          season  → seasonIndex = ?
          stage   → stageIndex = ? ("regular" → "1", "playoffs" → "2")
          team    → teamName = ?
          user    → (homeUser = ? OR awayUser = ?) for games, else subquery
          pos     → pos = ?
          status  → status IN ('2','3')
        """
        for key, val in kwargs.items():
            self._filters[key] = val
        return self

    def where(self, clause: str, *params: Any) -> Query:
        """Add a raw WHERE clause with parameterized values."""
        self._wheres.append(clause)
        self._where_params.extend(params)
        return self

    def aggregate(self, **kwargs: str) -> Query:
        """Add aggregate functions.

        Keys are column names, values are agg types (SUM, AVG, COUNT, MIN, MAX).
        Automatically wraps with CAST based on DomainKnowledge.
        """
        for col, agg in kwargs.items():
            agg_upper = agg.upper()
            if agg_upper not in ("SUM", "AVG", "COUNT", "MIN", "MAX"):
                raise ValueError(f"Invalid aggregation: {agg!r}")
            self._aggregates[col] = agg_upper
        return self

    def group_by(self, *cols: str) -> Query:
        """Add GROUP BY columns."""
        self._group_bys.extend(cols)
        return self

    def having(self, clause: str, *params: Any) -> Query:
        """Add HAVING clause with parameterized values."""
        self._havings.append(clause)
        self._having_params.extend(params)
        return self

    def sort_by(self, col: str, direction: str = "DESC") -> Query:
        """Set ORDER BY with domain-aware direction handling.

        direction can be "DESC", "ASC", "best", or "worst".
        "best"/"worst" auto-determine direction based on DomainKnowledge.
        """
        self._order_col = col
        dir_upper = direction.upper()

        if dir_upper in ("ASC", "DESC"):
            self._order_dir = dir_upper
        elif dir_upper == "BEST":
            self._order_dir = self._resolve_best_direction(col)
        elif dir_upper == "WORST":
            best = self._resolve_best_direction(col)
            self._order_dir = "ASC" if best == "DESC" else "DESC"
            self._apply_worst_guard(col)
        else:
            self._order_dir = "DESC"

        return self

    def limit(self, n: int) -> Query:
        """Set LIMIT."""
        if n < 1:
            raise ValueError("Limit must be >= 1")
        self._limit_n = n
        return self

    def pos(self, position: str) -> Query:
        """Filter by player position."""
        self._pos_filter = position
        return self

    # ── Build ─────────────────────────────────────────────────────────────

    def build(self) -> tuple[str, tuple]:
        """Generate parameterized SQL and params tuple.

        Only SELECT statements are generated. All aggregations are CAST-wrapped.
        """
        params: list[Any] = []

        # SELECT
        select_parts = list(self._selects)
        for col, agg in self._aggregates.items():
            cast_type = self._get_cast_type(col)
            select_parts.append(f"{agg}(CAST({col} AS {cast_type})) AS {col}")
        if not select_parts:
            select_parts = ["*"]

        sql = f"SELECT {', '.join(select_parts)}\nFROM {self._table}"

        # WHERE
        where_clauses: list[str] = []
        where_params: list[Any] = []

        for key, val in self._filters.items():
            clause, p = self._expand_filter(key, val)
            if clause:
                where_clauses.append(clause)
                where_params.extend(p)

        if self._pos_filter:
            where_clauses.append("pos = ?")
            where_params.append(self._pos_filter)

        where_clauses.extend(self._wheres)
        where_params.extend(self._where_params)

        if where_clauses:
            sql += "\nWHERE " + "\n  AND ".join(where_clauses)
            params.extend(where_params)

        # GROUP BY
        if self._group_bys:
            sql += f"\nGROUP BY {', '.join(self._group_bys)}"

        # HAVING
        if self._havings:
            sql += "\nHAVING " + " AND ".join(self._havings)
            params.extend(self._having_params)

        # ORDER BY
        if self._order_col:
            if self._order_col in self._aggregates:
                sql += f"\nORDER BY {self._order_col} {self._order_dir}"
            else:
                cast_type = self._get_cast_type(self._order_col)
                sql += f"\nORDER BY CAST({self._order_col} AS {cast_type}) {self._order_dir}"

        # LIMIT
        if self._limit_n is not None:
            sql += f"\nLIMIT {self._limit_n}"

        return sql, tuple(params)

    def execute(self) -> list[dict]:
        """Build and execute synchronously via codex_utils.run_sql()."""
        utils = _get_codex_utils()
        if utils is None:
            raise RuntimeError("codex_utils not available")
        sql, params = self.build()
        rows, error = utils.run_sql(sql, params)
        if error:
            raise RuntimeError(f"Query execution failed: {error}")
        return rows

    async def execute_async(self) -> list[dict]:
        """Build and execute asynchronously via codex_utils.run_sql_async()."""
        utils = _get_codex_utils()
        if utils is None:
            raise RuntimeError("codex_utils not available")
        sql, params = self.build()
        rows, error = await utils.run_sql_async(sql, params)
        if error:
            raise RuntimeError(f"Query execution failed: {error}")
        return rows

    # ── Internal helpers ──────────────────────────────────────────────────

    def _resolve_best_direction(self, col: str) -> str:
        """Determine "best" sort direction for a column via DomainKnowledge."""
        for sd in DomainKnowledge.STATS.values():
            if sd.column == col and sd.invert_sort:
                return "ASC"
        return "DESC"

    def _apply_worst_guard(self, col: str) -> None:
        """For "worst" on volume stats, switch to efficiency alt + min games."""
        for sd in DomainKnowledge.STATS.values():
            if sd.column == col and sd.efficiency_alt:
                if col in self._aggregates:
                    del self._aggregates[col]
                self._aggregates[sd.efficiency_alt] = "AVG"
                self._order_col = sd.efficiency_alt
                if not any("COUNT(*)" in h for h in self._havings):
                    self._havings.append("COUNT(*) >= 4")
                return

    def _get_cast_type(self, col: str) -> str:
        """Determine CAST type for a column from DomainKnowledge."""
        for sd in DomainKnowledge.STATS.values():
            if sd.column == col:
                return sd.cast_type
        return "INTEGER"

    def _expand_filter(self, key: str, val: Any) -> tuple[str, list[Any]]:
        """Expand a named filter key into a WHERE clause + params."""
        if key == "season":
            return "seasonIndex = ?", [str(val)]
        if key == "stage":
            stage_map = {"regular": "1", "playoffs": "2", "preseason": "0"}
            return "stageIndex = ?", [stage_map.get(str(val).lower(), str(val))]
        if key == "team":
            return "teamName = ?", [val]
        if key == "user":
            if self._table == "games":
                return "(homeUser = ? OR awayUser = ?)", [val, val]
            return "teamName = (SELECT teamName FROM teams WHERE userName = ? LIMIT 1)", [val]
        if key == "pos":
            return "pos = ?", [val]
        if key == "status":
            return "status IN ('2','3')", []
        return f"{key} = ?", [val]


# ══════════════════════════════════════════════════════════════════════════════
#  HIGH-LEVEL DOMAIN FUNCTIONS (Layer 1)
# ══════════════════════════════════════════════════════════════════════════════

def h2h(u1: str, u2: str, season: int | None = None) -> tuple[str, tuple]:
    """Head-to-head record — wraps existing deterministic SQL."""
    intents = _get_codex_intents()
    if intents is None:
        raise RuntimeError("codex_intents not available")
    return intents.get_h2h_sql_and_params(u1, u2, season)


def owner_record(user: str, season: int | None = None) -> tuple[str, tuple]:
    """Owner's win/loss record across their tenure."""
    params: list[Any] = [user, user, user, user, user]
    sql = """
        SELECT
            g.seasonIndex,
            SUM(CASE WHEN g.winner_user = ? THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN g.loser_user = ? THEN 1 ELSE 0 END) AS losses,
            COUNT(*) AS games_played
        FROM games g
        JOIN owner_tenure ot
          ON (g.homeTeamName = ot.teamName OR g.awayTeamName = ot.teamName)
          AND g.seasonIndex = ot.seasonIndex
        WHERE ot.userName = ?
          AND g.status IN ('2','3')
          AND g.stageIndex = '1'
          AND (g.homeUser = ? OR g.awayUser = ?)
    """
    if season is not None:
        sql += "  AND g.seasonIndex = ?\n"
        params.append(str(season))
    sql += """
        GROUP BY g.seasonIndex
        ORDER BY CAST(g.seasonIndex AS INTEGER)
    """
    return sql, tuple(params)


def team_record(team: str, season: int | None = None) -> tuple[str, tuple]:
    """Team's win/loss record."""
    params: list[Any] = [team, team, team, team]
    sql = """
        SELECT
            seasonIndex,
            SUM(CASE WHEN winner_team = ? THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN loser_team = ? THEN 1 ELSE 0 END) AS losses,
            COUNT(*) AS games_played
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND (homeTeamName = ? OR awayTeamName = ?)
    """
    if season is not None:
        sql += "  AND seasonIndex = ?\n"
        params.append(str(season))
    sql += """
        GROUP BY seasonIndex
        ORDER BY CAST(seasonIndex AS INTEGER)
    """
    return sql, tuple(params)


def standings_query(
    division: str | None = None,
    conference: str | None = None,
) -> tuple[str, tuple]:
    """Current standings query."""
    params: list[Any] = []
    sql = (
        "SELECT teamName, CAST(totalWins AS INTEGER) AS wins,"
        " CAST(totalLosses AS INTEGER) AS losses,"
        " CAST(ptsFor AS INTEGER) AS pf,"
        " CAST(ptsAgainst AS INTEGER) AS pa,"
        " divisionName, conferenceName, seed"
        "\nFROM standings"
    )
    wheres: list[str] = []
    if division:
        wheres.append("divisionName = ?")
        params.append(division)
    if conference:
        wheres.append("conferenceName = ?")
        params.append(conference)
    if wheres:
        sql += "\nWHERE " + " AND ".join(wheres)
    sql += "\nORDER BY CAST(seed AS INTEGER)"
    return sql, tuple(params)


def streak_query(user: str) -> tuple[str, tuple]:
    """Last 20 games for computing current streak."""
    sql = """
        SELECT winner_user, seasonIndex, weekIndex,
               homeTeamName, awayTeamName, homeScore, awayScore
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND (homeUser = ? OR awayUser = ?)
        ORDER BY CAST(seasonIndex AS INTEGER) DESC,
                 CAST(weekIndex AS INTEGER) DESC
        LIMIT 20
    """
    return sql, (user, user)


def stat_leaders(
    stat: str,
    season: int | None = None,
    sort: str = "best",
    limit: int = 10,
) -> tuple[str, tuple]:
    """Player stat leaders with domain-aware sorting."""
    result = DomainKnowledge.lookup(stat)
    if result is None:
        raise ValueError(f"Unknown stat: {stat!r}")
    _, sd = result

    q = (
        Query(sd.table)
        .select("fullName", "teamName")
        .filter(stage="regular")
        .aggregate(**{sd.column: sd.agg})
        .group_by("fullName", "teamName")
        .sort_by(sd.column, sort)
        .limit(limit)
    )
    if sd.pos_filter:
        q.pos(sd.pos_filter)
    if season is not None:
        q.filter(season=season)
    return q.build()


def team_stat_leaders(
    stat: str,
    season: int | None = None,
    sort: str = "best",
    limit: int = 10,
) -> tuple[str, tuple]:
    """Team stat leaders."""
    result = DomainKnowledge.lookup(stat)
    if result is None:
        raise ValueError(f"Unknown stat: {stat!r}")
    _, sd = result

    table = "team_stats" if sd.category == "team" else sd.table
    q = (
        Query(table)
        .select("teamName")
        .filter(stage="regular")
        .aggregate(**{sd.column: sd.agg})
        .group_by("teamName")
        .sort_by(sd.column, sort)
        .limit(limit)
    )
    if season is not None:
        q.filter(season=season)
    return q.build()


def roster_query(
    team: str,
    pos: str | None = None,
) -> tuple[str, tuple]:
    """Team roster query."""
    q = (
        Query("players")
        .select(
            "(firstName || ' ' || lastName) AS fullName",
            "pos", "CAST(playerBestOvr AS INTEGER) AS ovr",
            "dev", "age", "jerseyNum",
        )
        .filter(team=team)
        .where("teamName != 'Free Agent'")
    )
    if pos:
        q.pos(pos)
    sql, params = q.build()
    sql += "\nORDER BY CAST(playerBestOvr AS INTEGER) DESC"
    return sql, params


def free_agents_query(
    pos: str | None = None,
    min_ovr: int | None = None,
) -> tuple[str, tuple]:
    """Free agent query."""
    q = (
        Query("players")
        .select(
            "(firstName || ' ' || lastName) AS fullName",
            "pos", "CAST(playerBestOvr AS INTEGER) AS ovr",
            "dev", "age",
        )
        .where("isFA = '1'")
    )
    if pos:
        q.pos(pos)
    if min_ovr is not None:
        q.where("CAST(playerBestOvr AS INTEGER) >= ?", min_ovr)
    sql, params = q.build()
    sql += "\nORDER BY CAST(playerBestOvr AS INTEGER) DESC"
    return sql, params


def draft_picks_query(
    team: str | None = None,
    season: int | None = None,
    round_num: int | None = None,
) -> tuple[str, tuple]:
    """Draft history from player_draft_map (NOT players table)."""
    q = Query("player_draft_map").select(
        "extendedName", "drafting_team", "drafting_season",
        "draftRound", "draftPick", "pos", "dev",
        "CAST(playerBestOvr AS INTEGER) AS ovr", "was_traded",
    )
    if team:
        q.where("drafting_team = ?", team)
    if season is not None:
        q.where("drafting_season = ?", str(season))
    if round_num is not None:
        q.where("draftRound = ?", str(round_num + 1))
    sql, params = q.build()
    sql += "\nORDER BY CAST(drafting_season AS INTEGER), CAST(draftRound AS INTEGER), CAST(draftPick AS INTEGER)"
    return sql, params


def abilities_query(
    team: str | None = None,
    player: str | None = None,
) -> tuple[str, tuple]:
    """Player abilities (X-Factor/Superstar)."""
    q = Query("player_abilities").select(
        "firstName", "lastName", "teamName", "title", "description",
    )
    if team:
        q.where("teamName = ?", team)
    if player:
        q.where("(firstName || ' ' || lastName) LIKE ?", f"%{player}%")
    sql, params = q.build()
    sql += "\nORDER BY teamName, firstName"
    return sql, params


def trades_query(
    team: str | None = None,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Trade history query."""
    q = (
        Query("trades")
        .select("team1Name", "team2Name", "status", "seasonIndex", "team1Sent", "team2Sent")
        .where("status = 'approved'")
    )
    if team:
        q.where("(team1Name = ? OR team2Name = ?)", team, team)
    if season is not None:
        q.filter(season=season)
    sql, params = q.build()
    sql += "\nORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC"
    return sql, params


def owner_history_query(
    user: str | None = None,
    team: str | None = None,
) -> tuple[str, tuple]:
    """Owner tenure history."""
    q = Query("owner_tenure").select("teamName", "userName", "seasonIndex", "games_played")
    if user:
        q.where("userName = ?", user)
    if team:
        q.where("teamName = ?", team)
    sql, params = q.build()
    sql += "\nORDER BY CAST(seasonIndex AS INTEGER)"
    return sql, params


def game_extremes(
    extreme_type: str = "blowout",
    season: int | None = None,
    limit: int = 5,
) -> tuple[str, tuple]:
    """Extreme games (biggest blowout, highest scoring, closest)."""
    params: list[Any] = []
    sql = """
        SELECT homeTeamName, awayTeamName, homeScore, awayScore,
               seasonIndex, weekIndex, homeUser, awayUser,
               ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin,
               (CAST(homeScore AS INTEGER) + CAST(awayScore AS INTEGER)) AS total_pts
        FROM games
        WHERE status IN ('2','3') AND stageIndex = '1'
    """
    if season is not None:
        sql += "  AND seasonIndex = ?\n"
        params.append(str(season))

    order = {
        "blowout": "margin DESC",
        "closest": "margin ASC",
        "highest_scoring": "total_pts DESC",
    }
    sql += f"ORDER BY {order.get(extreme_type, 'margin DESC')}\nLIMIT {limit}"
    return sql, tuple(params)


def recent_games_query(
    user: str,
    limit: int = 5,
    opponent: str | None = None,
) -> tuple[str, tuple]:
    """Recent games for a user."""
    params: list[Any] = [user, user]
    sql = """
        SELECT homeTeamName, awayTeamName, homeScore, awayScore,
               seasonIndex, weekIndex, homeUser, awayUser, winner_user
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND (homeUser = ? OR awayUser = ?)
    """
    if opponent:
        sql += "  AND (homeUser = ? OR awayUser = ?)\n"
        params.extend([opponent, opponent])
    sql += f"""
        ORDER BY CAST(seasonIndex AS INTEGER) DESC,
                 CAST(weekIndex AS INTEGER) DESC
        LIMIT {limit}
    """
    return sql, tuple(params)


# ── Cross-Season Functions (NEW for v3) ──────────────────────────────────────

def compare_seasons(
    stat: str,
    user_or_team: str,
    season1: int,
    season2: int,
) -> tuple[str, tuple]:
    """Compare a stat between two seasons for a user/team."""
    result = DomainKnowledge.lookup(stat)
    if result is None:
        raise ValueError(f"Unknown stat: {stat!r}")
    _, sd = result

    sql = f"""
        SELECT seasonIndex,
               {sd.agg}(CAST({sd.column} AS {sd.cast_type})) AS stat_value,
               COUNT(*) AS games
        FROM {sd.table}
        WHERE stageIndex = '1'
          AND seasonIndex IN (?, ?)
          AND (teamName = ? OR fullName LIKE ?)
        GROUP BY seasonIndex
        ORDER BY CAST(seasonIndex AS INTEGER)
    """
    return sql, (str(season1), str(season2), user_or_team, f"%{user_or_team}%")


def improvement_leaders(
    stat: str,
    season1: int,
    season2: int,
    limit: int = 10,
) -> tuple[str, tuple]:
    """Players who improved the most in a stat between two seasons."""
    result = DomainKnowledge.lookup(stat)
    if result is None:
        raise ValueError(f"Unknown stat: {stat!r}")
    _, sd = result

    sql = f"""
        SELECT s1.fullName, s1.teamName,
               s1.val AS season1_val, s2.val AS season2_val,
               (s2.val - s1.val) AS improvement
        FROM (
            SELECT fullName, teamName,
                   {sd.agg}(CAST({sd.column} AS {sd.cast_type})) AS val
            FROM {sd.table}
            WHERE seasonIndex = ? AND stageIndex = '1'
            GROUP BY fullName, teamName
            HAVING COUNT(*) >= 4
        ) s1
        JOIN (
            SELECT fullName,
                   {sd.agg}(CAST({sd.column} AS {sd.cast_type})) AS val
            FROM {sd.table}
            WHERE seasonIndex = ? AND stageIndex = '1'
            GROUP BY fullName
            HAVING COUNT(*) >= 4
        ) s2 ON s1.fullName = s2.fullName
        ORDER BY improvement DESC
        LIMIT {limit}
    """
    return sql, (str(season1), str(season2))


def career_trajectory(user: str, stat: str) -> tuple[str, tuple]:
    """Season-by-season stat trajectory for a user's team."""
    result = DomainKnowledge.lookup(stat)
    if result is None:
        raise ValueError(f"Unknown stat: {stat!r}")
    _, sd = result

    sql = f"""
        SELECT s.seasonIndex,
               {sd.agg}(CAST(s.{sd.column} AS {sd.cast_type})) AS stat_value,
               COUNT(*) AS games
        FROM {sd.table} s
        JOIN owner_tenure ot
          ON s.teamName = ot.teamName
          AND s.seasonIndex = ot.seasonIndex
        WHERE ot.userName = ?
          AND s.stageIndex = '1'
        GROUP BY s.seasonIndex
        ORDER BY CAST(s.seasonIndex AS INTEGER)
    """
    return sql, (user,)


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS (Layer 3)
# ══════════════════════════════════════════════════════════════════════════════

def current_season() -> int:
    """Get the current TSL season number."""
    dm = _get_dm()
    return dm.CURRENT_SEASON if dm and hasattr(dm, "CURRENT_SEASON") else 6


def current_week() -> int:
    """Get the current TSL week number (1-based)."""
    dm = _get_dm()
    return dm.CURRENT_WEEK if dm and hasattr(dm, "CURRENT_WEEK") else 1


def resolve_user(name: str) -> str | None:
    """Resolve a loose name to a db_username."""
    utils = _get_codex_utils()
    return utils.fuzzy_resolve_user(name) if utils else None


def resolve_team(name: str) -> str | None:
    """Resolve a team name/abbreviation to canonical teamName."""
    intents = _get_codex_intents()
    if intents and hasattr(intents, "_resolve_team"):
        return intents._resolve_team(name)
    return name if name else None


def compare_datasets(
    dataset1: list[dict],
    dataset2: list[dict],
    key: str,
    metric: str = "delta",
) -> list[dict]:
    """Compare two datasets by a common key field.

    metric: "delta" for absolute difference, "pct_change" for percentage.
    """
    map1 = {row.get(key): row for row in dataset1}
    map2 = {row.get(key): row for row in dataset2}
    results = []

    for k in set(map1) | set(map2):
        r1, r2 = map1.get(k, {}), map2.get(k, {})
        result: dict[str, Any] = {"key": k, "data1": r1, "data2": r2}
        for field_name in r1:
            v1, v2 = r1.get(field_name), r2.get(field_name)
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                if metric == "delta":
                    result[f"{field_name}_delta"] = v2 - v1
                elif metric == "pct_change" and v1 != 0:
                    result[f"{field_name}_pct"] = ((v2 - v1) / v1) * 100
        results.append(result)

    return results


def summarize(dataset: list[dict]) -> dict:
    """Summarize a dataset: row count and numeric column stats."""
    if not dataset:
        return {"rows": 0}

    summary: dict[str, Any] = {"rows": len(dataset)}
    for col in dataset[0]:
        values = []
        for row in dataset:
            v = row.get(col)
            if isinstance(v, (int, float)):
                values.append(v)
            elif isinstance(v, str):
                try:
                    values.append(float(v))
                except (ValueError, TypeError):
                    continue
        if values:
            summary[col] = {
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "sum": sum(values),
                "count": len(values),
            }
    return summary
