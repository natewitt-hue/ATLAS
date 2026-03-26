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
            # Always require min games for "worst" to prevent single-game outliers
            if not any("COUNT(*)" in h for h in self._havings):
                self._havings.append("COUNT(*) >= 4")
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

    def execute(self) -> tuple[list[dict], str | None]:
        """Build and execute synchronously via codex_utils.run_sql().

        Returns (rows, error) matching the run_sql() contract so callers
        can handle errors without catching exceptions.
        """
        utils = _get_codex_utils()
        if utils is None:
            return [], "codex_utils not available"
        sql, params = self.build()
        return utils.run_sql(sql, params)

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
            SUM(CASE WHEN winner_user = ? THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN loser_user = ? THEN 1 ELSE 0 END) AS losses,
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
        .where(
            "EXISTS (SELECT 1 FROM games g WHERE g.seasonIndex = seasonIndex"
            " AND g.weekIndex = weekIndex AND g.status IN ('2','3'))"
        )
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
        .where(
            "EXISTS (SELECT 1 FROM games g WHERE g.seasonIndex = seasonIndex"
            " AND g.weekIndex = weekIndex AND g.status IN ('2','3'))"
        )
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
#  OWNER-SCOPED METRICS — shared CTE primitive + public functions
# ══════════════════════════════════════════════════════════════════════════════

def _owner_games_cte(
    user: str,
    season: int | None = None,
    include_playoffs: bool = False,
) -> tuple[str, list]:
    """Returns (cte_sql, params) for a two-level owner_games CTE.

    Internal helper — not exposed to the agent. Public functions call this,
    then append their own SELECT against `og`.

    Two-level structure:
        og_raw: base game rows filtered by user identity + completion status
        og:     adds pre-computed `margin` = user_score - opp_score

    The `user` param appears 8 times in og_raw:  # document this count
        5x homeUser CASE: user_team, opp_team, is_home, user_score, opp_score
        1x winner_user CASE: won
        2x in WHERE clause:     (homeUser = ? OR awayUser = ?)
    Total: params = [user] * 8  (plus optional season param)

    CTE output columns (from og):
        seasonIndex, weekIndex, stageIndex,
        homeTeamName, awayTeamName, homeScore (INTEGER), awayScore (INTEGER),
        homeUser, awayUser, winner_user, loser_user,
        user_team, opp_team,
        is_home (1 if user was home, 0 if away),
        user_score, opp_score,
        won (1 if user won, 0 if lost),
        margin (user_score - opp_score, positive = win)
    """
    if not user:
        raise ValueError("_owner_games_cte: user must be a non-empty string")
    stages = "('1','2')" if include_playoffs else "('1')"
    params: list = [user] * 8  # 6 CASE + 2 WHERE — see docstring

    cte = f"""WITH og_raw AS (
    SELECT
        g.seasonIndex, g.weekIndex, g.stageIndex,
        g.homeTeamName, g.awayTeamName,
        CAST(g.homeScore AS INTEGER) AS homeScore,
        CAST(g.awayScore AS INTEGER) AS awayScore,
        g.homeUser, g.awayUser, g.winner_user, g.loser_user,
        CASE WHEN g.homeUser = ? THEN g.homeTeamName ELSE g.awayTeamName  END AS user_team,
        CASE WHEN g.homeUser = ? THEN g.awayTeamName ELSE g.homeTeamName  END AS opp_team,
        CASE WHEN g.homeUser = ? THEN 1 ELSE 0                            END AS is_home,
        CASE WHEN g.homeUser = ?
             THEN CAST(g.homeScore AS INTEGER)
             ELSE CAST(g.awayScore AS INTEGER)                            END AS user_score,
        CASE WHEN g.homeUser = ?
             THEN CAST(g.awayScore AS INTEGER)
             ELSE CAST(g.homeScore AS INTEGER)                            END AS opp_score,
        CASE WHEN g.winner_user = ? THEN 1 ELSE 0                        END AS won
    FROM games g
    WHERE g.status IN ('2','3')
      AND g.stageIndex IN {stages}
      AND (g.homeUser = ? OR g.awayUser = ?)
      AND g.homeUser NOT IN ('CPU', '')
      AND g.awayUser NOT IN ('CPU', '')
"""
    if season is not None:
        cte += "      AND g.seasonIndex = ?\n"
        params.append(str(season))

    cte += """),
og AS (
    SELECT *, (user_score - opp_score) AS margin FROM og_raw
)
"""
    return cte, params


def owner_games(
    user: str,
    season: int | None = None,
    include_playoffs: bool = False,
) -> tuple[str, tuple]:
    """All completed games for an owner — foundation for custom owner queries.

    Returns CTE + SELECT * FROM og. The code-gen agent uses this directly
    for one-off owner queries not covered by the specific metric functions.

    Excludes CPU games. Scopes by user identity across all teams they have
    ever controlled (not by franchise).
    """
    cte, params = _owner_games_cte(user, season, include_playoffs)
    return cte + "SELECT * FROM og\nORDER BY CAST(seasonIndex AS INTEGER), CAST(weekIndex AS INTEGER)", tuple(params)


def pythagorean_wins(
    user: str,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Expected wins from Pythagorean formula (PF^2.37 / (PF^2.37 + PA^2.37)).

    SQLite lacks POWER(), so this returns raw PF/PA/actual_wins/games_played.
    The code-gen agent computes expected_wins in Python:
        exp = (pf**2.37 / (pf**2.37 + pa**2.37)) * games_played
        luck = actual_wins - exp
    """
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    seasonIndex,
    SUM(user_score)  AS points_for,
    SUM(opp_score)   AS points_against,
    SUM(won)         AS actual_wins,
    COUNT(*)         AS games_played
FROM og
GROUP BY seasonIndex
ORDER BY CAST(seasonIndex AS INTEGER)
"""
    return sql, tuple(params)


def home_away_record(
    user: str,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Owner's record split by home vs away."""
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    CASE WHEN is_home = 1 THEN 'Home' ELSE 'Away' END AS location,
    SUM(won)                     AS wins,
    SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END) AS losses,
    COUNT(*)                     AS games,
    ROUND(CAST(SUM(won) AS REAL) / COUNT(*), 3) AS win_pct
FROM og
GROUP BY is_home
ORDER BY is_home DESC
"""
    return sql, tuple(params)


def blowout_frequency(
    user: str,
    season: int | None = None,
    margin_threshold: int = 17,
) -> tuple[str, tuple]:
    """How often an owner wins or loses by margin_threshold+ points."""
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    seasonIndex,
    SUM(CASE WHEN won = 1 AND ABS(margin) >= ? THEN 1 ELSE 0 END) AS blowout_wins,
    SUM(CASE WHEN won = 0 AND ABS(margin) >= ? THEN 1 ELSE 0 END) AS blowout_losses,
    COUNT(*) AS total_games,
    ROUND(CAST(SUM(CASE WHEN ABS(margin) >= ? THEN 1 ELSE 0 END) AS REAL) / COUNT(*), 3) AS blowout_pct
FROM og
GROUP BY seasonIndex
ORDER BY CAST(seasonIndex AS INTEGER)
"""
    params_extended = list(params) + [margin_threshold, margin_threshold, margin_threshold]
    return sql, tuple(params_extended)


def close_game_record(
    user: str,
    season: int | None = None,
    margin_threshold: int = 7,
) -> tuple[str, tuple]:
    """Owner's record in games decided by margin_threshold or fewer points. Clutch metric."""
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    seasonIndex,
    SUM(CASE WHEN won = 1 AND ABS(margin) <= ? THEN 1 ELSE 0 END) AS close_wins,
    SUM(CASE WHEN won = 0 AND ABS(margin) <= ? THEN 1 ELSE 0 END) AS close_losses,
    SUM(CASE WHEN ABS(margin) <= ? THEN 1 ELSE 0 END) AS total_close,
    ROUND(
        CAST(SUM(CASE WHEN won = 1 AND ABS(margin) <= ? THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(SUM(CASE WHEN ABS(margin) <= ? THEN 1 ELSE 0 END), 0),
        3
    ) AS close_win_pct
FROM og
GROUP BY seasonIndex
ORDER BY CAST(seasonIndex AS INTEGER)
"""
    params_extended = list(params) + [margin_threshold] * 5
    return sql, tuple(params_extended)


def scoring_margin_distribution(
    user: str,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Win/loss count bucketed by margin ranges: 1-3, 4-7, 8-14, 15-21, 22+."""
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    CASE
        WHEN ABS(margin) BETWEEN 1 AND 3   THEN '1-3'
        WHEN ABS(margin) BETWEEN 4 AND 7   THEN '4-7'
        WHEN ABS(margin) BETWEEN 8 AND 14  THEN '8-14'
        WHEN ABS(margin) BETWEEN 15 AND 21 THEN '15-21'
        WHEN ABS(margin) >= 22             THEN '22+'
        ELSE '0 (tie)'
    END AS margin_bucket,
    SUM(won)                                     AS wins,
    SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END)     AS losses,
    COUNT(*)                                     AS total
FROM og
GROUP BY margin_bucket
ORDER BY MIN(ABS(margin))
"""
    return sql, tuple(params)


def first_half_second_half(
    user: str,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Record in first 8 weeks vs last 8+ weeks. Identifies slow starters / fast finishers.

    weekIndex is 0-based in DB. Weeks 0-7 = first half, 8+ = second half.
    """
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    CASE WHEN CAST(weekIndex AS INTEGER) < 8 THEN 'First 8' ELSE 'Last 8+' END AS half,
    SUM(won)                                 AS wins,
    SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END) AS losses,
    ROUND(CAST(SUM(won) AS REAL) / COUNT(*), 3) AS win_pct
FROM og
GROUP BY half
ORDER BY half
"""
    return sql, tuple(params)


def owner_scoring_trend(
    user: str,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Per-week scoring trend. Shows mid-season surges and collapses."""
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    seasonIndex,
    weekIndex,
    ROUND(AVG(user_score), 1) AS avg_user_score,
    ROUND(AVG(opp_score),  1) AS avg_opp_score,
    ROUND(AVG(margin),     1) AS margin
FROM og
GROUP BY seasonIndex, weekIndex
ORDER BY CAST(seasonIndex AS INTEGER), CAST(weekIndex AS INTEGER)
"""
    return sql, tuple(params)


def owner_consistency(
    user: str,
    min_games: int = 15,
) -> tuple[str, tuple]:
    """Career win consistency. Returns per-season win counts for stddev computation.

    SQLite lacks STDDEV. The code-gen agent computes stddev in Python:
        import statistics
        wins = [r['wins'] for r in rows]
        stddev = statistics.stdev(wins) if len(wins) > 1 else 0
    """
    cte, params = _owner_games_cte(user)  # No season filter — all-time
    sql = cte + """
SELECT
    seasonIndex,
    SUM(won)         AS wins,
    SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END) AS losses,
    COUNT(*)         AS games_played
FROM og
GROUP BY seasonIndex
HAVING COUNT(*) >= ?
ORDER BY CAST(seasonIndex AS INTEGER)
"""
    params_extended = list(params) + [min_games]
    return sql, tuple(params_extended)


def owner_career_summary(user: str) -> tuple[str, tuple]:
    """Comprehensive career totals: W/L, win%, seasons, teams controlled."""
    cte, params = _owner_games_cte(user)
    sql = cte + """
SELECT
    COUNT(DISTINCT seasonIndex)                  AS seasons_played,
    SUM(won)                                     AS total_wins,
    SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END)     AS total_losses,
    COUNT(*)                                     AS total_games,
    ROUND(CAST(SUM(won) AS REAL) / COUNT(*), 3)  AS career_win_pct,
    GROUP_CONCAT(DISTINCT user_team)             AS teams_controlled
FROM og
"""
    return sql, tuple(params)


def owner_improvement_arc(user: str) -> tuple[str, tuple]:
    """Win% per season for trajectory plotting. All-time, no season filter."""
    cte, params = _owner_games_cte(user)
    sql = cte + """
SELECT
    seasonIndex,
    SUM(won)                                     AS wins,
    SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END)     AS losses,
    COUNT(*)                                     AS games_played,
    ROUND(CAST(SUM(won) AS REAL) / COUNT(*), 3)  AS win_pct
FROM og
GROUP BY seasonIndex
ORDER BY CAST(seasonIndex AS INTEGER)
"""
    return sql, tuple(params)


def owner_division_record(
    user: str,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Owner's record in intra-division games.

    Joins teams table on displayName to determine division membership.
    LEFT JOINs exclude teams not present in the current teams table
    (games vs. teams without a divName entry are excluded by the WHERE clause).
    """
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
SELECT
    og.seasonIndex,
    SUM(og.won)                                      AS div_wins,
    SUM(CASE WHEN og.won = 0 THEN 1 ELSE 0 END)      AS div_losses,
    COUNT(*)                                         AS total_div_games
FROM og
LEFT JOIN teams ut ON og.user_team = ut.displayName
LEFT JOIN teams ot ON og.opp_team  = ot.displayName
WHERE ut.divName IS NOT NULL
  AND ot.divName IS NOT NULL
  AND ut.divName = ot.divName
GROUP BY og.seasonIndex
ORDER BY CAST(og.seasonIndex AS INTEGER)
"""
    return sql, tuple(params)


# ══════════════════════════════════════════════════════════════════════════════
#  STANDINGS-BASED METRICS (Group D)
# ══════════════════════════════════════════════════════════════════════════════

def team_efficiency(team: str | None = None) -> tuple[str, tuple]:
    """Offensive/defensive yardage, points scored/allowed, turnover diff from standings."""
    params: list = []
    sql = """
SELECT
    teamName,
    CAST(offTotalYds  AS INTEGER) AS offTotalYds,
    CAST(offPassYds   AS INTEGER) AS offPassYds,
    CAST(offRushYds   AS INTEGER) AS offRushYds,
    CAST(defTotalYds  AS INTEGER) AS defTotalYds,
    CAST(defPassYds   AS INTEGER) AS defPassYds,
    CAST(defRushYds   AS INTEGER) AS defRushYds,
    CAST(ptsFor       AS INTEGER) AS ptsFor,
    CAST(ptsAgainst   AS INTEGER) AS ptsAgainst,
    CAST(netPts       AS INTEGER) AS netPts,
    CAST(tODiff       AS INTEGER) AS tODiff,
    ROUND(CAST(winPct AS REAL), 3) AS winPct
FROM standings
"""
    if team:
        sql += "WHERE teamName = ?\n"
        params.append(team)
    sql += "ORDER BY CAST(netPts AS INTEGER) DESC"
    return sql, tuple(params)


def strength_of_schedule(team: str | None = None) -> tuple[str, tuple]:
    """Pre-computed strength of schedule from standings table."""
    params: list = []
    sql = """
SELECT
    teamName,
    CAST(totalSoS     AS REAL) AS totalSoS,
    CAST(playedSoS    AS REAL) AS playedSoS,
    CAST(remainingSoS AS REAL) AS remainingSoS,
    CAST(initialSoS   AS REAL) AS initialSoS,
    CAST(totalWins    AS INTEGER) AS totalWins,
    CAST(totalLosses  AS INTEGER) AS totalLosses
FROM standings
"""
    if team:
        sql += "WHERE teamName = ?\n"
        params.append(team)
    sql += "ORDER BY CAST(totalSoS AS REAL) DESC"
    return sql, tuple(params)


def team_home_away(team: str | None = None) -> tuple[str, tuple]:
    """Home/away win-loss splits from standings."""
    params: list = []
    sql = """
SELECT
    teamName,
    CAST(homeWins   AS INTEGER) AS homeWins,
    CAST(homeLosses AS INTEGER) AS homeLosses,
    CAST(awayWins   AS INTEGER) AS awayWins,
    CAST(awayLosses AS INTEGER) AS awayLosses
FROM standings
"""
    if team:
        sql += "WHERE teamName = ?\n"
        params.append(team)
    sql += "ORDER BY (CAST(homeWins AS INTEGER) + CAST(awayWins AS INTEGER)) DESC"
    return sql, tuple(params)


def team_division_standings(
    division: str | None = None,
    conference: str | None = None,
) -> tuple[str, tuple]:
    """Division and conference records from standings."""
    params: list = []
    wheres: list[str] = []
    sql = """
SELECT
    teamName,
    CAST(divWins    AS INTEGER) AS divWins,
    CAST(divLosses  AS INTEGER) AS divLosses,
    CAST(confWins   AS INTEGER) AS confWins,
    CAST(confLosses AS INTEGER) AS confLosses,
    divisionName,
    conferenceName
FROM standings
"""
    if division:
        wheres.append("divisionName = ?")
        params.append(division)
    if conference:
        wheres.append("conferenceName = ?")
        params.append(conference)
    if wheres:
        sql += "WHERE " + " AND ".join(wheres) + "\n"
    sql += "ORDER BY divisionName, CAST(divWins AS INTEGER) DESC"
    return sql, tuple(params)


def team_rankings(team: str | None = None) -> tuple[str, tuple]:
    """All rank columns from standings — useful for 'where does team X rank?'"""
    params: list = []
    sql = """
SELECT
    teamName,
    CAST(rank            AS INTEGER) AS rank,
    CAST(prevRank        AS INTEGER) AS prevRank,
    CAST(offTotalYdsRank AS INTEGER) AS offTotalYdsRank,
    CAST(defTotalYdsRank AS INTEGER) AS defTotalYdsRank,
    CAST(ptsForRank      AS INTEGER) AS ptsForRank,
    CAST(ptsAgainstRank  AS INTEGER) AS ptsAgainstRank
FROM standings
"""
    if team:
        sql += "WHERE teamName = ?\n"
        params.append(team)
    sql += "ORDER BY CAST(rank AS INTEGER)"
    return sql, tuple(params)


# ══════════════════════════════════════════════════════════════════════════════
#  COMPOSITE PLAYER SCORES (Group E)
# Weights are documented as constants. HAVING COUNT(*) >= 4 filters small samples.
# ══════════════════════════════════════════════════════════════════════════════

def qb_composite(
    season: int | None = None,
    limit: int = 10,
) -> tuple[str, tuple]:
    """Top QBs by weighted composite: passer rating (30%), TD:INT (10pts/ratio), YPA (5x), sack rate (-20x).

    Composite weights (tunable):
        rating_weight  = 0.30
        td_int_weight  = 10.0   (applied to TD:INT ratio)
        ypa_weight     = 5.0
        sack_penalty   = 20.0   (negative, applied to sack rate = sacks/attempts)
    """
    params: list = ["1"]  # stageIndex for regular season
    if season is not None:
        season_filter = "AND seasonIndex = ?"
        params.append(str(season))
    else:
        season_filter = ""

    sql = f"""
SELECT
    fullName,
    teamName,
    ROUND(AVG(CAST(passerRating  AS REAL)), 1)  AS passerRating,
    ROUND(AVG(CAST(passCompPct   AS REAL)), 1)  AS passCompPct,
    SUM(CAST(passTDs   AS INTEGER))             AS passTDs,
    SUM(CAST(passInts  AS INTEGER))             AS passInts,
    ROUND(AVG(CAST(passYdsPerAtt AS REAL)), 2)  AS passYdsPerAtt,
    SUM(CAST(passSacks AS INTEGER))             AS passSacks,
    SUM(CAST(passAtt   AS INTEGER))             AS passAtt,
    ROUND(
        AVG(CAST(passerRating AS REAL)) * 0.30
        + (CAST(SUM(CAST(passTDs AS INTEGER)) AS REAL)
           / NULLIF(SUM(CAST(passInts AS INTEGER)), 0)) * 10.0
        + AVG(CAST(passYdsPerAtt AS REAL)) * 5.0
        - (CAST(SUM(CAST(passSacks AS INTEGER)) AS REAL)
           / NULLIF(SUM(CAST(passAtt AS INTEGER)), 0)) * 20.0,
        2
    ) AS composite_score
FROM offensive_stats
WHERE stageIndex = ? {season_filter}
  AND pos = 'QB'
GROUP BY fullName, teamName
HAVING COUNT(*) >= 4
ORDER BY composite_score DESC
LIMIT ?
"""
    params.append(limit)
    return sql, tuple(params)


def rb_composite(
    season: int | None = None,
    limit: int = 10,
) -> tuple[str, tuple]:
    """Top RBs by weighted composite: YPC (10x), broken tackles/att (50x), YAC/att (5x), fumble penalty (-30x).

    Composite weights (tunable):
        ypc_weight           = 10.0
        broken_tackle_weight = 50.0  (applied per-carry: broken_tackles / attempts)
        yac_weight           = 5.0   (applied per-carry: yac / attempts)
        fumble_penalty       = 30.0  (negative, fumble rate: fumbles / attempts)

    NOTE: Composite scores are NOT comparable across position groups.
    Use within a single position group only.
    """
    params: list = ["1"]
    if season is not None:
        season_filter = "AND seasonIndex = ?"
        params.append(str(season))
    else:
        season_filter = ""

    sql = f"""
SELECT
    fullName,
    teamName,
    SUM(CAST(rushYds              AS INTEGER)) AS rushYds,
    SUM(CAST(rushTDs              AS INTEGER)) AS rushTDs,
    ROUND(AVG(CAST(rushYdsPerAtt         AS REAL)), 2) AS rushYdsPerAtt,
    SUM(CAST(rushBrokenTackles    AS INTEGER)) AS rushBrokenTackles,
    SUM(CAST(rushYdsAfterContact  AS INTEGER)) AS rushYdsAfterContact,
    SUM(CAST(rushFum              AS INTEGER)) AS rushFum,
    SUM(CAST(rushAtt              AS INTEGER)) AS rushAtt,
    ROUND(
        AVG(CAST(rushYdsPerAtt AS REAL)) * 10.0
        + (CAST(SUM(CAST(rushBrokenTackles AS INTEGER)) AS REAL)
           / NULLIF(SUM(CAST(rushAtt AS INTEGER)), 0)) * 50.0
        + (CAST(SUM(CAST(rushYdsAfterContact AS INTEGER)) AS REAL)
           / NULLIF(SUM(CAST(rushAtt AS INTEGER)), 0)) * 5.0
        - (CAST(SUM(CAST(rushFum AS INTEGER)) AS REAL)
           / NULLIF(SUM(CAST(rushAtt AS INTEGER)), 0)) * 30.0,
        2
    ) AS composite_score
FROM offensive_stats
WHERE stageIndex = ? {season_filter}
  AND pos = 'RB'
GROUP BY fullName, teamName
HAVING COUNT(*) >= 4
   AND SUM(CAST(rushAtt AS INTEGER)) >= 20
ORDER BY composite_score DESC
LIMIT ?
"""
    params.append(limit)
    return sql, tuple(params)


def wr_composite(
    season: int | None = None,
    limit: int = 10,
) -> tuple[str, tuple]:
    """Top WRs/TEs by weighted composite: catch% (50x), YPC (5x), YAC/catch (3x), TD bonus (10x), drop penalty (-20x).

    Composite weights (tunable):
        catch_pct_weight = 50.0
        ypc_weight       = 5.0
        yac_weight       = 3.0   (per-catch YAC)
        td_weight        = 10.0  (per-TD)
        drop_penalty     = 20.0  (negative, drop rate: drops / (catches + drops))

    NOTE: Composite scores are NOT comparable across position groups.
    WR scores scale ~50x higher than QB composites due to catch% (0-100) * 50.0 weight.
    Use within a single position group only.
    """
    params: list = ["1"]
    if season is not None:
        season_filter = "AND seasonIndex = ?"
        params.append(str(season))
    else:
        season_filter = ""

    sql = f"""
SELECT
    fullName,
    teamName,
    SUM(CAST(recYds          AS INTEGER))  AS recYds,
    SUM(CAST(recTDs          AS INTEGER))  AS recTDs,
    ROUND(AVG(CAST(recCatchPct    AS REAL)), 1) AS recCatchPct,
    ROUND(AVG(CAST(recYdsPerCatch AS REAL)), 2) AS recYdsPerCatch,
    ROUND(AVG(CAST(recYacPerCatch AS REAL)), 2) AS recYacPerCatch,
    SUM(CAST(recDrops        AS INTEGER))  AS recDrops,
    SUM(CAST(recCatches      AS INTEGER))  AS recCatches,
    ROUND(
        AVG(CAST(recCatchPct AS REAL)) * 50.0
        + AVG(CAST(recYdsPerCatch AS REAL)) * 5.0
        + AVG(CAST(recYacPerCatch AS REAL)) * 3.0
        + (CAST(SUM(CAST(recTDs AS INTEGER)) AS REAL)
           / NULLIF(COUNT(*), 0)) * 10.0
        - (CAST(SUM(CAST(recDrops AS INTEGER)) AS REAL)
           / NULLIF(SUM(CAST(recCatches AS INTEGER)) + SUM(CAST(recDrops AS INTEGER)), 0)) * 20.0,
        2
    ) AS composite_score
FROM offensive_stats
WHERE stageIndex = ? {season_filter}
  AND pos IN ('WR', 'TE')
GROUP BY fullName, teamName
HAVING COUNT(*) >= 4
ORDER BY composite_score DESC
LIMIT ?
"""
    params.append(limit)
    return sql, tuple(params)


def defensive_composite(
    season: int | None = None,
    limit: int = 10,
) -> tuple[str, tuple]:
    """Top defenders by weighted composite: sacks (2x), INTs (3x), forced fumbles (2x), TDs (6x), deflections (1x), tackles (0.5x).

    Composite weights (tunable):
        sack_weight    = 2.0
        int_weight     = 3.0
        ff_weight      = 2.0
        td_weight      = 6.0
        defl_weight    = 1.0
        tackle_weight  = 0.5
    """
    params: list = ["1"]
    if season is not None:
        season_filter = "AND seasonIndex = ?"
        params.append(str(season))
    else:
        season_filter = ""

    sql = f"""
SELECT
    fullName,
    teamName,
    SUM(CAST(defTotalTackles AS INTEGER)) AS defTotalTackles,
    SUM(CAST(defSacks        AS INTEGER)) AS defSacks,
    SUM(CAST(defInts         AS INTEGER)) AS defInts,
    SUM(CAST(defForcedFum    AS INTEGER)) AS defForcedFum,
    SUM(CAST(defDeflections  AS INTEGER)) AS defDeflections,
    SUM(CAST(defTDs          AS INTEGER)) AS defTDs,
    ROUND(
        SUM(CAST(defSacks       AS INTEGER)) * 2.0
        + SUM(CAST(defInts      AS INTEGER)) * 3.0
        + SUM(CAST(defForcedFum AS INTEGER)) * 2.0
        + SUM(CAST(defTDs       AS INTEGER)) * 6.0
        + SUM(CAST(defDeflections AS INTEGER)) * 1.0
        + SUM(CAST(defTotalTackles AS INTEGER)) * 0.5,
        2
    ) AS composite_score
FROM defensive_stats
WHERE stageIndex = ? {season_filter}
GROUP BY fullName, teamName
HAVING COUNT(*) >= 4
ORDER BY composite_score DESC
LIMIT ?
"""
    params.append(limit)
    return sql, tuple(params)


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
