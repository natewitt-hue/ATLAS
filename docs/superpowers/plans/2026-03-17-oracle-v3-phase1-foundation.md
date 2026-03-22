# Oracle v3 Phase 1: Foundation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the QueryBuilder API with domain knowledge guards, create conversation_memory + observability schemas, and add Anthropic SDK integration — all with zero behavior change to the existing system.

**Architecture:** New `oracle_query_builder.py` module encodes all 39 stat definitions and domain rules (sort direction, efficiency vs volume, min games, position filtering) in a composable SQL builder. New `oracle_memory.py` creates the permanent memory schema. `bot.py` gains ANTHROPIC_API_KEY env var. Existing Tier 1 regex and Codex pipeline are untouched.

**Tech Stack:** Python 3.14, SQLite, anthropic SDK, existing codex_cog.py:run_sql() and get_db() patterns

**Spec:** `docs/superpowers/specs/2026-03-17-oracle-v3-design.md` (Sections 4-5)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `oracle_query_builder.py` | CREATE | StatDef dataclass, STAT_DEFS registry, QueryBuilder class, Layer 1 convenience functions, Layer 3 utilities, domain guards |
| `oracle_memory.py` | CREATE | conversation_memory + oracle_query_log schema creation, migration from conversation_history |
| `tests/test_query_builder.py` | CREATE | Unit tests for QueryBuilder: domain guards, stat lookups, SQL generation, convenience functions |
| `tests/test_oracle_memory.py` | CREATE | Schema creation tests, migration verification |
| `bot.py` | MODIFY | Add ANTHROPIC_API_KEY env var loading |
| `requirements.txt` or equivalent | MODIFY | Add `anthropic` SDK dependency |

**Existing code to reuse (read-only references):**
- `codex_cog.py:457-473` — `get_db()` and `run_sql()` patterns (WAL mode, Row factory, parameterized queries)
- `codex_intents.py:203-239` — `STAT_REGISTRY` (migrate all 39 entries to StatDef format)
- `codex_intents.py:649-682` — `_TEAM_ALIASES` (reuse for resolve_team)
- `codex_intents.py:475-505` — efficiency vs volume logic (encode in StatDef.efficiency_alt)
- `reasoning.py:283-314` — `_SAFE_BUILTINS` pattern (reference for Phase 3 sandbox)
- `build_member_db.py` — `get_alias_map()`, `get_known_users()` (reuse for resolve_user)
- `data_manager.py` — `CURRENT_SEASON`, `CURRENT_WEEK` (reuse for current_season/current_week)

---

## Chunk 1: DomainKnowledge + QueryBuilder Core

### Task 1: StatDef Dataclass + STAT_DEFS Registry

**Files:**
- Create: `oracle_query_builder.py`
- Create: `tests/test_query_builder.py`

- [ ] **Step 1: Write failing test for StatDef + STAT_DEFS**

```python
# tests/test_query_builder.py
"""Unit tests for Oracle v3 QueryBuilder API."""
import pytest


def test_stat_defs_has_all_39_entries():
    from oracle_query_builder import STAT_DEFS
    assert len(STAT_DEFS) == 39


def test_stat_def_passing_yards():
    from oracle_query_builder import STAT_DEFS
    sd = STAT_DEFS["passYds"]
    assert sd.table == "offensive_stats"
    assert sd.column == "passYds"
    assert sd.agg == "SUM"
    assert sd.pos == "QB"
    assert sd.category == "offense"
    assert sd.efficiency_alt == "passerRating"


def test_stat_def_defense_inverts():
    from oracle_query_builder import STAT_DEFS
    sd = STAT_DEFS["defTotalYds"]
    assert sd.category == "defense"
    assert sd.pos is None


def test_stat_def_passer_rating_is_avg():
    from oracle_query_builder import STAT_DEFS
    sd = STAT_DEFS["passerRating"]
    assert sd.agg == "AVG"
    assert sd.category == "offense"


def test_stat_keyword_lookup():
    """Verify keyword-to-StatDef resolution works."""
    from oracle_query_builder import resolve_stat_keyword
    sd = resolve_stat_keyword("passing yards")
    assert sd.column == "passYds"
    sd2 = resolve_stat_keyword("sacks")
    assert sd2.column == "defSacks"
    assert resolve_stat_keyword("nonexistent") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v`
Expected: FAIL (ImportError — module doesn't exist yet)

- [ ] **Step 3: Write StatDef + STAT_DEFS + keyword resolver**

```python
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
    # --- Defense (no position filter) ---
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add oracle_query_builder.py tests/test_query_builder.py
git commit -m "feat(oracle-v3): add StatDef registry with 39 stat definitions and keyword resolver"
```

---

### Task 2: QueryBuilder Core — filter, select, aggregate, sort, execute

**Files:**
- Modify: `oracle_query_builder.py`
- Modify: `tests/test_query_builder.py`

- [ ] **Step 1: Write failing tests for QueryBuilder**

Add to `tests/test_query_builder.py`:

```python
def test_query_builder_basic_select():
    """QueryBuilder generates correct SELECT with filter."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").filter(season=6, stage="regular").select("extendedName", "teamName")
    sql, params = q.build()
    assert "SELECT" in sql
    assert "offensive_stats" in sql
    assert "seasonIndex = ?" in sql or "seasonIndex=?" in sql
    assert "stageIndex = ?" in sql or "stageIndex=?" in sql
    assert params == ("6", "1")  # season 6, stage 1 = regular


def test_query_builder_aggregate():
    from oracle_query_builder import Query
    q = (Query("offensive_stats")
         .filter(season=6)
         .aggregate(passYds="SUM")
         .group_by("extendedName")
         .limit(10))
    sql, params = q.build()
    assert "SUM(CAST(passYds AS INTEGER))" in sql
    assert "GROUP BY" in sql
    assert "LIMIT 10" in sql


def test_query_builder_sort_best_offense():
    """Best offense = most yards = DESC."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").stat("passYds").sort("best")
    sql, _ = q.build()
    assert "DESC" in sql


def test_query_builder_sort_best_defense():
    """Best defense = fewest yards = ASC (INVERTED)."""
    from oracle_query_builder import Query
    q = Query("defensive_stats").stat("defTotalTackles").sort("best")
    sql, _ = q.build()
    # Defense "best" = most tackles (offensive stat behavior for individual defensive stats)
    assert "DESC" in sql


def test_query_builder_sort_worst_passer_uses_efficiency():
    """Worst passer switches to passerRating AVG instead of passYds SUM."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").stat("passYds").sort("worst")
    sql, _ = q.build()
    assert "passerRating" in sql  # Switched to efficiency metric
    assert "AVG" in sql
    assert "HAVING COUNT(*) >= 4" in sql  # Min games filter


def test_query_builder_sort_worst_rusher_no_efficiency_alt():
    """Worst rusher has no efficiency alt — uses rushYds ASC."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").stat("rushYds").sort("worst")
    sql, _ = q.build()
    assert "rushYds" in sql
    assert "ASC" in sql
    assert "HAVING COUNT(*) >= 4" in sql


def test_query_builder_pos_filter_auto():
    """stat('passYds') auto-adds WHERE pos='QB'."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").stat("passYds").sort("best")
    sql, params = q.build()
    assert "pos = ?" in sql or "pos=?" in sql
    assert "QB" in params


def test_query_builder_read_only():
    """QueryBuilder only generates SELECT statements."""
    from oracle_query_builder import Query
    sql, _ = Query("games").filter(season=6).build()
    assert sql.strip().upper().startswith("SELECT")


def test_query_builder_auto_cast():
    """Numeric aggregations auto-wrap CAST."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").aggregate(passYds="SUM")
    sql, _ = q.build()
    assert "CAST(passYds AS INTEGER)" in sql


def test_query_builder_execute(tmp_path):
    """Execute runs the built SQL and returns rows."""
    # This test uses a real DB connection — will work against tsl_history.db
    from oracle_query_builder import Query
    q = (Query("games")
         .select("homeTeamName", "awayTeamName")
         .filter(season=1, stage="regular")
         .limit(3))
    rows, err = q.execute()
    assert err is None
    assert len(rows) <= 3
    if rows:
        assert "homeTeamName" in rows[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v -k "query_builder"`
Expected: FAIL (Query class doesn't exist yet)

- [ ] **Step 3: Implement QueryBuilder class**

Add to `oracle_query_builder.py` after the StatDef section:

```python
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

    def select(self, *columns: str) -> Query:
        self._selects.extend(columns)
        return self

    def filter(self, **kwargs) -> Query:
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

    def where(self, clause: str, *params) -> Query:
        """Add a raw WHERE clause. Values MUST be passed as params."""
        # Validate: only allow safe SQL patterns
        _validate_where_clause(clause)
        self._wheres.append(clause)
        self._params.extend(params)
        return self

    def stat(self, stat_name: str) -> Query:
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

    def sort(self, mode: str) -> Query:
        """Set sort mode: 'best' or 'worst'. Domain rules applied at build time."""
        if mode not in ("best", "worst"):
            raise ValueError(f"sort mode must be 'best' or 'worst', got '{mode}'")
        self._sort_mode = mode
        return self

    def aggregate(self, **kwargs) -> Query:
        """Add aggregations: aggregate(passYds='SUM', passTDs='SUM')."""
        self._aggregates.update(kwargs)
        return self

    def group_by(self, *columns: str) -> Query:
        self._group_bys.extend(columns)
        return self

    def having(self, clause: str) -> Query:
        self._having = clause
        return self

    def sort_by(self, column: str, direction: str = "DESC") -> Query:
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

    def limit(self, n: int) -> Query:
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
    "totalWins", "totalLosses", "divisionName", "conferenceName", "seed", "winPct",
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
    # Validate that any identifiers (non-operator, non-keyword tokens) are known columns
    import re
    identifiers = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', clause)
    safe_keywords = {"IN", "NOT", "LIKE", "BETWEEN", "IS", "NULL", "AND", "OR",
                     "CAST", "AS", "INTEGER", "REAL", "TEXT", "ABS", "COUNT"}
    for ident in identifiers:
        if ident.upper() not in safe_keywords and ident not in _KNOWN_COLUMNS:
            raise ValueError(f"Unknown column in WHERE clause: {ident}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add oracle_query_builder.py tests/test_query_builder.py
git commit -m "feat(oracle-v3): add QueryBuilder with domain-aware sort, efficiency guards, and safe SQL"
```

---

### Task 3: Layer 3 Utility Functions

**Files:**
- Modify: `oracle_query_builder.py`
- Modify: `tests/test_query_builder.py`

- [ ] **Step 1: Write failing tests for utilities**

Add to `tests/test_query_builder.py`:

```python
def test_current_season():
    from oracle_query_builder import current_season
    s = current_season()
    assert isinstance(s, int)
    assert s >= 1


def test_current_week():
    from oracle_query_builder import current_week
    w = current_week()
    assert isinstance(w, int)
    assert w >= 0


def test_resolve_team_exact():
    from oracle_query_builder import resolve_team
    assert resolve_team("lions") == "Lions"
    assert resolve_team("det") == "Lions"
    assert resolve_team("detroit") == "Lions"
    assert resolve_team("nonexistent") is None


def test_resolve_user():
    from oracle_query_builder import resolve_user
    # Should delegate to build_member_db alias map
    result = resolve_user("Witt")
    # May return "TheWitt" or None depending on DB state
    assert result is None or isinstance(result, str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v -k "current_season or current_week or resolve_team or resolve_user"`
Expected: FAIL

- [ ] **Step 3: Implement utility functions**

Add to `oracle_query_builder.py`:

```python
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
    'buccaneers': 'Buccaneers', 'bucs': 'Buccaneers', 'tb': 'Buccaneers', 'tampa': 'Buccaneers', 'tampa bay': 'Buccaneers',
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
        # Try exact match first (case-insensitive)
        lower = name.lower().strip()
        for key, val in alias_map.items():
            if key.lower() == lower:
                return val
        return None
    except ImportError:
        return None
```

- [ ] **Step 4: Run tests**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add oracle_query_builder.py tests/test_query_builder.py
git commit -m "feat(oracle-v3): add utility functions — resolve_team, resolve_user, current_season/week"
```

---

## Chunk 2: Layer 1 Convenience Functions

### Task 4: Core Convenience Functions (stat_leaders, team_stat_leaders, h2h, owner_record)

**Files:**
- Modify: `oracle_query_builder.py`
- Modify: `tests/test_query_builder.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_query_builder.py`:

```python
def test_stat_leaders_passing_yards():
    from oracle_query_builder import stat_leaders
    rows, err = stat_leaders("passYds", season=None, sort="best", limit=5)
    assert err is None
    assert len(rows) <= 5
    if rows:
        assert "player_name" in rows[0] or "stat_value" in rows[0]


def test_stat_leaders_worst_passer():
    """Worst passer should use passerRating AVG with min games filter."""
    from oracle_query_builder import stat_leaders
    rows, err = stat_leaders("passYds", sort="worst", limit=5)
    assert err is None
    # Verify min games filter is working (no 1-game backups)


def test_team_stat_leaders_best_offense():
    from oracle_query_builder import team_stat_leaders
    rows, err = team_stat_leaders("offTotalYds", sort="best", limit=5)
    assert err is None


def test_team_stat_leaders_best_defense():
    """Best defense = fewest yards = should sort ASC."""
    from oracle_query_builder import team_stat_leaders
    rows, err = team_stat_leaders("defTotalYds_team", sort="best", limit=5)
    assert err is None


def test_owner_record():
    from oracle_query_builder import owner_record
    result, err = owner_record("TheWitt")
    assert err is None
    if result:
        assert "wins" in result or "total_wins" in result


def test_owner_record_season():
    from oracle_query_builder import owner_record
    result, err = owner_record("TheWitt", season=6)
    assert err is None


def test_h2h():
    from oracle_query_builder import h2h
    result, err = h2h("TheWitt", "KillaE94")
    assert err is None


def test_team_record():
    from oracle_query_builder import team_record_query
    result, err = team_record_query("Lions")
    assert err is None


def test_streak():
    from oracle_query_builder import streak
    result, err = streak("TheWitt")
    assert err is None


def test_standings_division():
    from oracle_query_builder import standings
    rows, err = standings(division="NFC East")
    assert err is None


def test_recent_games():
    from oracle_query_builder import recent_games
    rows, err = recent_games("TheWitt", limit=5)
    assert err is None
    assert len(rows) <= 5


def test_roster():
    from oracle_query_builder import roster
    rows, err = roster("Lions")
    assert err is None


def test_free_agents():
    from oracle_query_builder import free_agents
    rows, err = free_agents(pos="QB")
    assert err is None


def test_draft_picks():
    from oracle_query_builder import draft_picks
    rows, err = draft_picks(team="Lions")
    assert err is None


def test_trades():
    from oracle_query_builder import trades
    rows, err = trades(team="Lions")
    assert err is None


def test_game_extremes_blowout():
    from oracle_query_builder import game_extremes
    rows, err = game_extremes("blowout", limit=5)
    assert err is None
    assert len(rows) <= 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v -k "stat_leaders or team_stat or owner_record or h2h or team_record or streak or standings or recent_games or roster or free_agents or draft_picks or trades or game_extremes"`
Expected: FAIL

- [ ] **Step 3: Implement all Layer 1 convenience functions**

Add to `oracle_query_builder.py`:

```python
# ---------------------------------------------------------------------------
# Layer 1: High-Level Domain Functions
# ---------------------------------------------------------------------------

def stat_leaders(
    stat: str,
    season: int | None = None,
    sort: str = "best",
    limit: int = 10,
) -> tuple[list[dict], str | None]:
    """Player stat leaders with full domain rules (sort, efficiency, min games, pos)."""
    sd = STAT_DEFS.get(stat) or resolve_stat_keyword(stat)
    if sd is None:
        return [], f"Unknown stat: {stat}"
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
        # Look up teams this user has owned and filter by those team names
        tenure_rows, _ = owner_history(user=user)
        if tenure_rows:
            teams = {r["teamName"] for r in tenure_rows}
            for t in teams:
                q.where("(team1Name = ? OR team2Name = ?)", t, t)
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
    # Filter for the specific user/team and compute delta
    # This is intentionally simple — the agent can do more complex comparisons in code
    return rows1 + rows2, None


def improvement_leaders(
    stat: str,
    season1: int,
    season2: int,
    limit: int = 10,
) -> tuple[list[dict], str | None]:
    """Players/teams who improved most in a stat between two seasons.
    Returns raw data for both seasons; delta computation deferred to agent."""
    rows1, err1 = stat_leaders(stat, season=season1, sort="best", limit=50)
    rows2, err2 = stat_leaders(stat, season=season2, sort="best", limit=50)
    if err1 or err2:
        return [], err1 or err2
    # Build lookup by player name for season 1
    s1_map = {r.get("player_name", ""): r.get("stat_value", 0) for r in rows1}
    # Compute deltas for players in both seasons
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
```

- [ ] **Step 4: Run all tests**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add oracle_query_builder.py tests/test_query_builder.py
git commit -m "feat(oracle-v3): add all Layer 1 convenience functions — stat_leaders, h2h, roster, trades, etc."
```

---

## Chunk 3: Memory Schema + SDK + Integration Verification

### Task 5: Oracle Memory Schema

**Files:**
- Create: `oracle_memory.py`
- Create: `tests/test_oracle_memory.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_oracle_memory.py
"""Tests for Oracle v3 permanent memory schema."""
import sqlite3
import pytest
from pathlib import Path


def test_init_memory_tables(tmp_path):
    """Memory tables are created correctly."""
    db_path = tmp_path / "test.db"
    from oracle_memory import init_memory_tables
    init_memory_tables(str(db_path))

    conn = sqlite3.connect(str(db_path))
    # Check conversation_memory exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_memory'")
    assert cur.fetchone() is not None

    # Check oracle_query_log exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='oracle_query_log'")
    assert cur.fetchone() is not None

    # Check FTS5 virtual table exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_memory_fts'")
    assert cur.fetchone() is not None

    conn.close()


def test_store_and_retrieve_memory(tmp_path):
    """Can store and retrieve a conversation turn."""
    db_path = tmp_path / "test.db"
    from oracle_memory import init_memory_tables, store_memory, get_recent_turns
    init_memory_tables(str(db_path))

    store_memory(
        db_path=str(db_path),
        discord_id=12345,
        message_id=99999,
        question="who has the most wins?",
        sql_query="SELECT winner_user, COUNT(*) ...",
        answer="TheWitt leads with 67 wins.",
        tier=1,
        intent="leaderboard",
        entities='{"users": ["TheWitt"]}',
    )

    turns = get_recent_turns(str(db_path), discord_id=12345, limit=5)
    assert len(turns) == 1
    assert turns[0]["question"] == "who has the most wins?"
    assert turns[0]["answer"] == "TheWitt leads with 67 wins."


def test_fts5_search(tmp_path):
    """FTS5 keyword search finds relevant conversations."""
    db_path = tmp_path / "test.db"
    from oracle_memory import init_memory_tables, store_memory, search_memory_fts
    init_memory_tables(str(db_path))

    store_memory(str(db_path), 12345, None, "who has the best defense?",
                 None, "Bears lead with 267 yards allowed.", 1, "team_stats", None)
    store_memory(str(db_path), 12345, None, "top passers this season",
                 None, "Mahomes leads with 4200 yards.", 1, "leaderboard", None)

    results = search_memory_fts(str(db_path), discord_id=12345, query="defense yards")
    assert len(results) >= 1
    assert "defense" in results[0]["question"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_oracle_memory.py -v`
Expected: FAIL

- [ ] **Step 3: Implement oracle_memory.py**

```python
# oracle_memory.py
"""
Oracle v3 Permanent Memory — conversation storage, retrieval, and search.

Stores every Q&A pair permanently with:
  - FTS5 keyword search
  - Vector embeddings (populated in Phase 2)
  - Discord message ID for reply threading
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any


def init_memory_tables(db_path: str) -> None:
    """Create conversation_memory, oracle_query_log, and FTS5 tables."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_memory (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id    INTEGER NOT NULL,
            message_id    INTEGER,
            question      TEXT    NOT NULL,
            sql_query     TEXT,
            answer        TEXT    NOT NULL,
            tier          INTEGER DEFAULT 3,
            intent        TEXT,
            entities      TEXT,
            created_at    REAL    NOT NULL,
            embedding     BLOB
        );

        CREATE INDEX IF NOT EXISTS idx_mem_user_time
            ON conversation_memory(discord_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_mem_message_id
            ON conversation_memory(message_id);

        CREATE TABLE IF NOT EXISTS oracle_query_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id      INTEGER NOT NULL,
            question        TEXT    NOT NULL,
            tier            INTEGER NOT NULL,
            intent          TEXT,
            model           TEXT,
            latency_ms      INTEGER NOT NULL,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            estimated_cost  REAL,
            sql_executed    TEXT,
            rows_returned   INTEGER,
            success         INTEGER DEFAULT 1,
            error_message   TEXT,
            created_at      REAL    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_log_time
            ON oracle_query_log(created_at DESC);
    """)

    # FTS5 virtual table (must be created separately — can't use IF NOT EXISTS)
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE conversation_memory_fts USING fts5(
                question, answer, entities,
                content='conversation_memory',
                content_rowid='id'
            )
        """)
    except sqlite3.OperationalError:
        pass  # Already exists

    conn.close()


def store_memory(
    db_path: str,
    discord_id: int,
    message_id: int | None,
    question: str,
    sql_query: str | None,
    answer: str,
    tier: int,
    intent: str | None,
    entities: str | None,
    embedding: bytes | None = None,
) -> int:
    """Store a conversation turn. Returns the row ID."""
    conn = sqlite3.connect(db_path, timeout=5)
    now = time.time()
    cur = conn.execute(
        """INSERT INTO conversation_memory
           (discord_id, message_id, question, sql_query, answer, tier, intent,
            entities, created_at, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (discord_id, message_id, question, sql_query, answer, tier, intent,
         entities, now, embedding),
    )
    row_id = cur.lastrowid

    # Sync FTS5 index
    conn.execute(
        """INSERT INTO conversation_memory_fts(rowid, question, answer, entities)
           VALUES (?, ?, ?, ?)""",
        (row_id, question, answer, entities or ""),
    )

    conn.commit()
    conn.close()
    return row_id


def get_recent_turns(
    db_path: str,
    discord_id: int,
    limit: int = 5,
) -> list[dict]:
    """Get the most recent conversation turns for a user."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT id, discord_id, message_id, question, sql_query, answer,
                  tier, intent, entities, created_at
           FROM conversation_memory
           WHERE discord_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (discord_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_turn_by_message_id(
    db_path: str,
    message_id: int,
) -> dict | None:
    """Look up a conversation turn by Discord message ID (for reply threading)."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT id, discord_id, message_id, question, sql_query, answer,
                  tier, intent, entities, created_at
           FROM conversation_memory
           WHERE message_id = ?""",
        (message_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def search_memory_fts(
    db_path: str,
    discord_id: int,
    query: str,
    limit: int = 3,
) -> list[dict]:
    """Search conversation memory via FTS5 keyword matching."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT cm.id, cm.question, cm.answer, cm.created_at,
                  rank AS relevance
           FROM conversation_memory_fts fts
           JOIN conversation_memory cm ON cm.id = fts.rowid
           WHERE conversation_memory_fts MATCH ?
             AND cm.discord_id = ?
           ORDER BY rank
           LIMIT ?""",
        (query, discord_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def log_query(
    db_path: str,
    discord_id: int,
    question: str,
    tier: int,
    intent: str | None,
    model: str | None,
    latency_ms: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost: float | None = None,
    sql_executed: str | None = None,
    rows_returned: int | None = None,
    success: bool = True,
    error_message: str | None = None,
) -> None:
    """Log a query to the observability table."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute(
        """INSERT INTO oracle_query_log
           (discord_id, question, tier, intent, model, latency_ms,
            input_tokens, output_tokens, estimated_cost,
            sql_executed, rows_returned, success, error_message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (discord_id, question, tier, intent, model, latency_ms,
         input_tokens, output_tokens, estimated_cost,
         sql_executed, rows_returned, 1 if success else 0, error_message,
         time.time()),
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 4: Run tests**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_oracle_memory.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add oracle_memory.py tests/test_oracle_memory.py
git commit -m "feat(oracle-v3): add permanent memory schema — conversation_memory, FTS5, query log"
```

---

### Task 6: Anthropic SDK Integration

**Files:**
- Modify: `bot.py` (add env var)
- Check: `requirements.txt` or equivalent

- [ ] **Step 1: Check current dependency management**

Run: `ls C:/Users/natew/Desktop/discord_bot/requirements*.txt C:/Users/natew/Desktop/discord_bot/pyproject.toml 2>/dev/null`

- [ ] **Step 2: Add ANTHROPIC_API_KEY env var to bot.py**

Find the env var loading section in `bot.py` (near line 30-50) and add:

```python
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
```

Also add a startup warning (near the existing GEMINI_API_KEY warning):

```python
if not ANTHROPIC_API_KEY:
    print("⚠️  WARNING: ANTHROPIC_API_KEY not set — Oracle v3 agent will be unavailable")
```

- [ ] **Step 3: Add anthropic to dependencies**

```bash
pip install anthropic
```

Add `anthropic` to requirements.txt (or equivalent dependency file).

- [ ] **Step 4: Verify bot still starts**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -c "import bot; print('bot module loads OK')"`
Expected: Loads without error (ANTHROPIC_API_KEY warning is fine)

- [ ] **Step 5: Commit**

```bash
git add bot.py requirements.txt
git commit -m "feat(oracle-v3): add ANTHROPIC_API_KEY env var and anthropic SDK dependency"
```

---

### Task 7: Integration Verification — 98 Stress Tests Still Pass

**Files:** None modified (read-only verification)

- [ ] **Step 1: Run the existing 98 stress tests**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest test_oracle_stress.py -v`
Expected: 98/98 PASS — no behavior change from Phase 1 additions

- [ ] **Step 2: Verify QueryBuilder produces equivalent SQL for key test cases**

Add a cross-validation test to `tests/test_query_builder.py`:

```python
def test_querybuilder_matches_tier1_sort_directions():
    """Verify QueryBuilder domain guards match Tier 1 regex sort logic."""
    from oracle_query_builder import Query

    # "top 5 passers" → DESC (best offense)
    q = Query("offensive_stats").stat("passYds").sort("best")
    sql, _ = q.build()
    assert "DESC" in sql, "Best passers should sort DESC"

    # "worst passer" → efficiency metric (passerRating AVG, ASC, min games)
    q = Query("offensive_stats").stat("passYds").sort("worst")
    sql, _ = q.build()
    assert "passerRating" in sql, "Worst passer should use efficiency metric"
    assert "ASC" in sql, "Worst should sort ASC"
    assert "HAVING COUNT(*)" in sql, "Worst should have min games filter"

    # "best defense" → ASC (fewest yards = best)
    q = Query("team_stats").stat("defTotalYds_team").sort("best")
    sql, _ = q.build()
    assert "ASC" in sql, "Best defense should sort ASC (fewest yards)"

    # "worst defense" → DESC (most yards = worst)
    q = Query("team_stats").stat("defTotalYds_team").sort("worst")
    sql, _ = q.build()
    assert "DESC" in sql, "Worst defense should sort DESC (most yards)"
```

- [ ] **Step 3: Run cross-validation test**

Run: `cd C:/Users/natew/Desktop/discord_bot && python -m pytest tests/test_query_builder.py::test_querybuilder_matches_tier1_sort_directions -v`
Expected: PASS

- [ ] **Step 4: Bump ATLAS_VERSION**

In `bot.py`, bump the version (e.g., `"2.19.6"` → `"2.20.0"` for the Oracle v3 foundation):

```python
ATLAS_VERSION = "2.20.0"
```

- [ ] **Step 5: Final commit for Phase 1**

```bash
git add bot.py tests/test_query_builder.py
git commit -m "feat(oracle-v3): Phase 1 complete — QueryBuilder API, memory schema, SDK integration

Oracle v3 Foundation:
- QueryBuilder with 39 stat definitions and domain-aware sort/efficiency guards
- 20+ Layer 1 convenience functions (stat_leaders, h2h, roster, trades, etc.)
- Permanent memory schema (conversation_memory + FTS5 + oracle_query_log)
- Anthropic SDK dependency added
- Zero behavior change — all 98 stress tests pass"
```

---

## Phase 1 Verification Checklist

- [ ] `python -m pytest tests/test_query_builder.py -v` — all tests pass
- [ ] `python -m pytest tests/test_oracle_memory.py -v` — all tests pass
- [ ] `python -m pytest test_oracle_stress.py -v` — 98/98 pass (no regression)
- [ ] `python -c "import oracle_query_builder; print(len(oracle_query_builder.STAT_DEFS), 'stats')"` — prints "39 stats"
- [ ] `python -c "import oracle_memory; print('memory module loads OK')"` — loads without error
- [ ] `python -c "import anthropic; print('anthropic SDK installed')"` — loads without error
- [ ] Bot starts without crash (ANTHROPIC_API_KEY warning is acceptable)
