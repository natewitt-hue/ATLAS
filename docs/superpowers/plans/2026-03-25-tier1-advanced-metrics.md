# Tier 1 Advanced Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand `oracle_query_builder.py` with 37 new queryable metrics (20 public functions + 1 internal helper + 17 stat entries + 5 aliases) derived from unused columns in `tsl_history.db`, and update `oracle_agent.py`'s API reference and sandbox environment so the code-gen agent can use them immediately.

**Architecture:** All new functions live in `oracle_query_builder.py` only — no new files. Owner-scoped metrics share a single `_owner_games_cte()` internal helper (two-level CTE: `og_raw` + `og`). The agent discovers functions via `_API_REFERENCE` in `oracle_agent.py` and calls them via `build_agent_env()` which must explicitly inject every new public function name.

**Tech Stack:** Python 3.14, SQLite (via `codex_utils.run_sql`), existing `Query` builder, `DomainKnowledge.STATS` dict.

---

## File Map

| File | Changes |
|------|---------|
| `oracle_query_builder.py` | Fix `StatDef.agg` Literal. Add `_owner_games_cte()` + `owner_games()`. Add 11 owner-scoped functions. Add 5 standings functions. Add 4 composite functions. Add 17 stat entries + 5 aliases. |
| `oracle_agent.py` | Update `_API_REFERENCE` docstring. Update `_FEW_SHOT_EXAMPLES`. Update `build_agent_env()` with 21 new function references. |

---

## Task 1: Fix `StatDef.agg` Literal + Add MAX stat entries foundation

**Files:**
- Modify: `oracle_query_builder.py:82` (StatDef dataclass)

This is a prerequisite — 4 new stat entries use `agg="MAX"` which the current `Literal["SUM", "AVG"]` type annotation rejects. The runtime already handles MAX (line 275), so this is a type-hint-only fix. Do it first so all subsequent stat additions type-check correctly.

- [ ] **Step 1: Open `oracle_query_builder.py` and locate line 82**

The line currently reads:
```python
agg: Literal["SUM", "AVG"]                       # Aggregation type
```

- [ ] **Step 2: Widen the Literal to include MAX and MIN**

Change line 82 to:
```python
agg: Literal["SUM", "AVG", "MAX", "MIN"]         # Aggregation type
```

- [ ] **Step 3: Verify no tests exist yet (expected)**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "from oracle_query_builder import StatDef; s = StatDef('offensive_stats','passLongest','MAX','QB','offense'); print(s)"
```
Expected: prints the StatDef without error.

- [ ] **Step 4: Commit**

```bash
git add oracle_query_builder.py
git commit -m "fix: widen StatDef.agg Literal to include MAX/MIN for new stat entries"
```

---

## Task 2: Add Group B + C DomainKnowledge stat entries (17 entries + 5 aliases)

**Files:**
- Modify: `oracle_query_builder.py` — `DomainKnowledge.STATS` dict (after line 160)

Add all 14 offensive + 3 defensive stat entries, then 5 aliases. These extend `stat_leaders()` with zero new code — just registry entries.

- [ ] **Step 1: Write a quick smoke test before touching anything**

```bash
python -c "
from oracle_query_builder import stat_leaders, current_season
try:
    sql, p = stat_leaders('broken tackles')
    print('BEFORE: broken tackles raises ValueError (expected)')
except ValueError as e:
    print(f'BEFORE OK: {e}')
"
```
Expected output: `BEFORE OK: Unknown stat: 'broken tackles'`

- [ ] **Step 2: Add the 14 offensive stat entries after the existing receiving block**

In `oracle_query_builder.py`, locate the comment `# ── Individual Defense` (around line 130) and insert before it:

```python
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
```

- [ ] **Step 3: Add the 3 defensive stat entries after existing defense stats**

Locate the `# ── Team Defense` comment (around line 141) and insert before it:

```python
        # ── Individual Defense (extended) ────────────────────
        "catches allowed":      StatDef("defensive_stats", "defCatchAllowed",   "SUM", None,   "defense", invert_sort=True),
        "int return yards":     StatDef("defensive_stats", "defIntReturnYds",   "SUM", None,   "defense"),
        "safeties":             StatDef("defensive_stats", "defSafeties",       "SUM", None,   "defense"),
```

- [ ] **Step 4: Add the 5 alias entries after all stat definitions (before the closing `}`)**

```python
        # ── Aliases (shorthand for NL matching) ──────────────
        "ypa":                  StatDef("offensive_stats", "passYdsPerAtt",      "AVG", "QB",   "offense", cast_type="REAL"),
        "ypc":                  StatDef("offensive_stats", "rushYdsPerAtt",      "AVG", None,   "offense", cast_type="REAL"),
        "catch pct":            StatDef("offensive_stats", "recCatchPct",        "AVG", None,   "offense", cast_type="REAL"),
        "yac":                  StatDef("offensive_stats", "recYacPerCatch",     "AVG", None,   "offense", cast_type="REAL"),
        "broken tackle rate":   StatDef("offensive_stats", "rushBrokenTackles",  "SUM", None,   "offense"),
```

- [ ] **Step 5: Smoke test all new entries work with stat_leaders()**

```bash
python -c "
from oracle_query_builder import stat_leaders, current_season
tests = [
    'broken tackles', 'yards per carry', 'catch percentage',
    'yards per attempt', 'longest rush', 'catches allowed',
    'safeties', 'ypa', 'ypc', 'yac',
]
for t in tests:
    try:
        sql, p = stat_leaders(t)
        print(f'OK: {t}')
    except Exception as e:
        print(f'FAIL: {t} -> {e}')
"
```
Expected: all 10 lines print `OK: <stat>`.

- [ ] **Step 6: Commit**

```bash
git add oracle_query_builder.py
git commit -m "feat: add 17 new DomainKnowledge stat entries + 5 aliases (Groups B, C)"
```

---

## Task 3: Add `_owner_games_cte()` internal helper + `owner_games()` public wrapper

**Files:**
- Modify: `oracle_query_builder.py` — add after the last existing Layer 1 function

This is the shared primitive that all 11 owner-scoped functions depend on. Build and test it in isolation before adding any consumers.

- [ ] **Step 1: Add `_owner_games_cte()` after the last existing Layer 1 function**

Find the end of the existing Layer 1 functions (after `career_trajectory` around line 800+) and add:

```python
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
        6x in CASE expressions: user_team, opp_team, is_home, user_score, opp_score, won
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
```

- [ ] **Step 2: Add `owner_games()` public wrapper immediately after**

```python
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
```

- [ ] **Step 3: Smoke test the CTE produces valid SQL**

```bash
python -c "
import sqlite3, os
from oracle_query_builder import owner_games
sql, params = owner_games('TheWitt')
print('SQL generated OK, param count:', len(params))
print(sql[:300])
db = r'C:\Users\natew\Desktop\discord_bot\tsl_history.db'
if os.path.exists(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchmany(3)
        print(f'Live query: {len(rows)} rows returned')
        if rows: print(dict(rows[0]))
    except Exception as e:
        print(f'Query error: {e}')
    conn.close()
"
```
Expected: `SQL generated OK, param count: 8`, followed by a sample row (or 0 rows if username doesn't match — that's OK, no error is the goal).

- [ ] **Step 4: Commit**

```bash
git add oracle_query_builder.py
git commit -m "feat: add _owner_games_cte() internal helper + owner_games() public wrapper"
```

---

## Task 4: Add Group A owner-scoped metric functions (11 functions)

**Files:**
- Modify: `oracle_query_builder.py` — after `owner_games()`

All 11 functions call `_owner_games_cte()` and append their aggregation SELECT.

- [ ] **Step 1: Add `pythagorean_wins()`**

```python
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
```

- [ ] **Step 2: Add `home_away_record()`**

```python
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
```

- [ ] **Step 3: Add `blowout_frequency()`**

```python
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
    # Note: 3 uses of margin_threshold in the SELECT after the CTE params
    params_extended = list(params) + [margin_threshold, margin_threshold, margin_threshold]
    return sql, tuple(params_extended)
```

- [ ] **Step 4: Add `close_game_record()`**

```python
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
```

- [ ] **Step 5: Add `scoring_margin_distribution()`**

```python
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
```

- [ ] **Step 6: Add `first_half_second_half()`**

```python
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
```

- [ ] **Step 7: Add `owner_scoring_trend()`**

```python
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
```

- [ ] **Step 8: Add `owner_consistency()`**

```python
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
```

- [ ] **Step 9: Add `owner_career_summary()`**

```python
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
```

- [ ] **Step 10: Add `owner_improvement_arc()`**

```python
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
```

- [ ] **Step 11: Add `owner_division_record()`**

```python
def owner_division_record(
    user: str,
    season: int | None = None,
) -> tuple[str, tuple]:
    """Owner's record in intra-division games.

    Joins teams table on displayName to determine division membership.
    IMPORTANT: Uses teams.displayName (e.g., '49ers') NOT nickName ('Niners').
    Uses LEFT JOIN with COALESCE fallback for teams not found in the teams table
    (e.g., franchises that changed names across seasons).
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
```

- [ ] **Step 12: Smoke test all 11 owner functions**

```bash
python -c "
import os, sqlite3
from oracle_query_builder import (
    pythagorean_wins, home_away_record, blowout_frequency,
    close_game_record, scoring_margin_distribution,
    first_half_second_half, owner_scoring_trend,
    owner_consistency, owner_career_summary,
    owner_improvement_arc, owner_division_record,
)

user = 'TheWitt'
fns = [
    ('pythagorean_wins',          pythagorean_wins(user)),
    ('home_away_record',          home_away_record(user)),
    ('blowout_frequency',         blowout_frequency(user)),
    ('close_game_record',         close_game_record(user)),
    ('scoring_margin_distribution', scoring_margin_distribution(user)),
    ('first_half_second_half',    first_half_second_half(user)),
    ('owner_scoring_trend',       owner_scoring_trend(user, season=6)),
    ('owner_consistency',         owner_consistency(user)),
    ('owner_career_summary',      owner_career_summary(user)),
    ('owner_improvement_arc',     owner_improvement_arc(user)),
    ('owner_division_record',     owner_division_record(user)),
]

db = r'C:\Users\natew\Desktop\discord_bot\tsl_history.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

for name, (sql, params) in fns:
    try:
        rows = conn.execute(sql, params).fetchmany(2)
        print(f'OK  {name}: {len(rows)} row(s)')
    except Exception as e:
        print(f'FAIL {name}: {e}')

conn.close()
"
```
Expected: all 11 lines print `OK  <function>: N row(s)`.

- [ ] **Step 13: Commit**

```bash
git add oracle_query_builder.py
git commit -m "feat: add 11 owner-scoped metric functions (pythagorean_wins through owner_division_record)"
```

---

## Task 5: Add Group D standings functions (5 functions)

**Files:**
- Modify: `oracle_query_builder.py` — after owner functions

These query the `standings` table directly (current season, pre-computed by the API).

- [ ] **Step 1: Add all 5 standings functions**

```python
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
    (CAST(ptsFor AS INTEGER) - CAST(ptsAgainst AS INTEGER)) AS netPts,
    CAST(tODiff       AS INTEGER) AS tODiff,
    ROUND(CAST(totalWins AS REAL) / NULLIF(CAST(totalWins AS INTEGER) + CAST(totalLosses AS INTEGER), 0), 3) AS winPct
FROM standings
"""
    if team:
        sql += "WHERE teamName = ?\n"
        params.append(team)
    sql += "ORDER BY netPts DESC"
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
    CAST(divWins   AS INTEGER) AS divWins,
    CAST(divLosses AS INTEGER) AS divLosses,
    CAST(confWins  AS INTEGER) AS confWins,
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
```

- [ ] **Step 2: Smoke test standings functions**

```bash
python -c "
import os, sqlite3
from oracle_query_builder import (
    team_efficiency, strength_of_schedule,
    team_home_away, team_division_standings, team_rankings,
)

db = r'C:\Users\natew\Desktop\discord_bot\tsl_history.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

tests = [
    ('team_efficiency (all)',     team_efficiency()),
    ('team_efficiency (Ravens)',  team_efficiency('Ravens')),
    ('strength_of_schedule',      strength_of_schedule()),
    ('team_home_away',            team_home_away()),
    ('team_division_standings',   team_division_standings()),
    ('team_rankings',             team_rankings()),
]

for name, (sql, params) in tests:
    try:
        rows = conn.execute(sql, params).fetchmany(3)
        print(f'OK  {name}: {len(rows)} row(s)')
    except Exception as e:
        print(f'FAIL {name}: {e}')

conn.close()
"
```
Expected: all 6 lines print `OK`.

**Note:** If any standings column names (e.g., `totalSoS`, `homeWins`) cause an error, check `standings` schema via:
```bash
python -c "
import sqlite3
conn = sqlite3.connect(r'C:\Users\natew\Desktop\discord_bot\tsl_history.db')
print([d[0] for d in conn.execute('PRAGMA table_info(standings)').fetchall()])
"
```
Then adjust column names in the function accordingly.

- [ ] **Step 3: Commit**

```bash
git add oracle_query_builder.py
git commit -m "feat: add 5 standings-based metric functions (team_efficiency through team_rankings)"
```

---

## Task 6: Add Group E composite player score functions (4 functions)

**Files:**
- Modify: `oracle_query_builder.py` — after standings functions

These compute inline weighted composite scores entirely in SQL. All use `HAVING COUNT(*) >= 4` to filter low-sample players.

- [ ] **Step 1: Add `qb_composite()`**

```python
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
```

- [ ] **Step 2: Add `rb_composite()`**

```python
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
GROUP BY fullName, teamName
HAVING COUNT(*) >= 4
   AND SUM(CAST(rushAtt AS INTEGER)) >= 20
ORDER BY composite_score DESC
LIMIT ?
"""
    params.append(limit)
    return sql, tuple(params)
```

- [ ] **Step 3: Add `wr_composite()`**

```python
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
```

- [ ] **Step 4: Add `defensive_composite()`**

```python
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
```

- [ ] **Step 5: Smoke test all 4 composite functions**

```bash
python -c "
import sqlite3
from oracle_query_builder import qb_composite, rb_composite, wr_composite, defensive_composite

db = r'C:\Users\natew\Desktop\discord_bot\tsl_history.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

fns = [
    ('qb_composite',        qb_composite(season=6)),
    ('rb_composite',        rb_composite(season=6)),
    ('wr_composite',        wr_composite(season=6)),
    ('defensive_composite', defensive_composite(season=6)),
]

for name, (sql, params) in fns:
    try:
        rows = conn.execute(sql, params).fetchmany(3)
        print(f'OK  {name}: {len(rows)} row(s)')
        if rows: print('    top result:', dict(rows[0]))
    except Exception as e:
        print(f'FAIL {name}: {e}')

conn.close()
"
```
Expected: 4 `OK` lines each with a top player result.

- [ ] **Step 6: Commit**

```bash
git add oracle_query_builder.py
git commit -m "feat: add 4 composite player score functions (qb/rb/wr/defensive_composite)"
```

---

## Task 7: Update `oracle_agent.py` — `_API_REFERENCE`, `_FEW_SHOT_EXAMPLES`, `build_agent_env()`

**Files:**
- Modify: `oracle_agent.py`

This is the highest-consequence task. Every new public function must be registered in all three places or the agent will generate code that fails at runtime.

- [ ] **Step 1: Add new Layer 1 sections to `_API_REFERENCE`**

Locate the end of `_API_REFERENCE` (just before `_FEW_SHOT_EXAMPLES`, around line 176). Insert after `career_trajectory` and before the closing `"""`:

```python
owner_games(user: str, season: int | None = None, include_playoffs: bool = False) -> (sql, params)
    All completed non-CPU games for an owner across all teams they've controlled.
    Excludes CPU. Returns: seasonIndex, weekIndex, user_team, opp_team, is_home,
    user_score, opp_score, won, margin. Use as base for custom owner queries.

pythagorean_wins(user: str, season: int | None = None) -> (sql, params)
    Expected wins from Pythagorean formula. Returns: seasonIndex, points_for,
    points_against, actual_wins, games_played.
    NOTE: Compute in Python: exp = (pf**2.37 / (pf**2.37 + pa**2.37)) * games_played
    luck = actual_wins - exp

home_away_record(user: str, season: int | None = None) -> (sql, params)
    Owner's record split by home/away. Returns: location, wins, losses, games, win_pct.

blowout_frequency(user: str, season: int | None = None, margin_threshold: int = 17) -> (sql, params)
    How often an owner wins/loses by 17+ points per season.

close_game_record(user: str, season: int | None = None, margin_threshold: int = 7) -> (sql, params)
    Record in games decided by 7 or fewer points. Clutch metric.

scoring_margin_distribution(user: str, season: int | None = None) -> (sql, params)
    Win/loss count by margin bucket: 1-3, 4-7, 8-14, 15-21, 22+.

first_half_second_half(user: str, season: int | None = None) -> (sql, params)
    Record in first 8 weeks vs last 8+. Slow starter or fast finisher?

owner_scoring_trend(user: str, season: int | None = None) -> (sql, params)
    Per-week avg scoring for an owner. Shows mid-season surges and collapses.

owner_consistency(user: str, min_games: int = 15) -> (sql, params)
    Per-season win counts for all-time consistency analysis.
    NOTE: Compute stddev in Python: import statistics; statistics.stdev([r['wins'] for r in rows])

owner_career_summary(user: str) -> (sql, params)
    Career totals: wins, losses, win%, seasons, teams_controlled (comma-separated).

owner_improvement_arc(user: str) -> (sql, params)
    Win% per season for trajectory plotting. All seasons.

owner_division_record(user: str, season: int | None = None) -> (sql, params)
    Owner's record in intra-division games.

team_efficiency(team: str | None = None) -> (sql, params)
    Offensive/defensive yardage, points scored/allowed, turnover diff. All teams or one.

strength_of_schedule(team: str | None = None) -> (sql, params)
    Pre-computed SoS: totalSoS, playedSoS, remainingSoS, initialSoS.

team_home_away(team: str | None = None) -> (sql, params)
    Home/away W-L splits from standings.

team_division_standings(division: str | None = None, conference: str | None = None) -> (sql, params)
    Division and conference records from standings.

team_rankings(team: str | None = None) -> (sql, params)
    All rank columns: overall rank, prevRank, offense/defense/points ranks.

qb_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top QBs by weighted composite: passer rating, TD:INT, YPA, sack rate.

rb_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top RBs by weighted composite: YPC, broken tackles/att, YAC/att, fumble rate.

wr_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top WRs/TEs by weighted composite: catch%, YPC, YAC/catch, TDs, drop rate.

defensive_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top defenders by weighted composite: sacks, INTs, forced fumbles, TDs, deflections, tackles.
```

Also update the `stat_leaders` valid stats list in `_API_REFERENCE` by appending:
```
"yards per attempt", "ypa", "pass attempts", "sacks taken", "longest pass",
"rush attempts", "yards per carry", "ypc", "broken tackles", "broken tackle rate",
"yards after contact", "longest rush", "20 yard runs",
"catch percentage", "catch pct", "yards per catch", "yac per catch", "yac",
"longest reception", "catches allowed", "int return yards", "safeties"
```

- [ ] **Step 2: Add 4 new few-shot examples to `_FEW_SHOT_EXAMPLES`**

Locate `_FEW_SHOT_EXAMPLES` and append before the closing `"""`:

```python
Q: "How lucky has Witt been this season?"
```python
user = resolve_user("Witt")
sql, params = pythagorean_wins(user, season=current_season())
rows, error = run_sql(sql, params)
for r in rows:
    pf, pa = r["points_for"], r["points_against"]
    if pf + pa > 0:
        exp = (pf**2.37 / (pf**2.37 + pa**2.37)) * r["games_played"]
        r["expected_wins"] = round(exp, 1)
        r["luck"] = round(r["actual_wins"] - exp, 1)
result = rows
```

Q: "Who has the best record in close games this season?"
```python
sql_owners, p_owners = (
    Query("owner_tenure")
    .select("DISTINCT userName")
    .build()
)
owners, _ = run_sql(sql_owners, p_owners)
result = []
for o in owners:
    u = o["userName"]
    sql, params = close_game_record(u, season=current_season())
    rows, _ = run_sql(sql, params)
    if rows:
        result.append({"owner": u, **rows[0]})
result = sorted(result, key=lambda x: x.get("close_win_pct") or 0, reverse=True)[:10]
```

Q: "What's the strength of schedule for the Ravens?"
```python
sql, params = strength_of_schedule(team="Ravens")
result, error = run_sql(sql, params)
```

Q: "Who's the best QB this season?"
```python
sql, params = qb_composite(season=current_season())
result, error = run_sql(sql, params)
```
```

- [ ] **Step 3: Add all 21 new functions to `build_agent_env()`**

Locate `build_agent_env()` in `oracle_agent.py` (around line 344). After the existing `"career_trajectory": qb.career_trajectory,` entry, add:

```python
        # Owner-scoped metrics (Group A)
        "owner_games":                   qb.owner_games,
        "pythagorean_wins":              qb.pythagorean_wins,
        "home_away_record":              qb.home_away_record,
        "blowout_frequency":             qb.blowout_frequency,
        "close_game_record":             qb.close_game_record,
        "scoring_margin_distribution":   qb.scoring_margin_distribution,
        "first_half_second_half":        qb.first_half_second_half,
        "owner_scoring_trend":           qb.owner_scoring_trend,
        "owner_consistency":             qb.owner_consistency,
        "owner_career_summary":          qb.owner_career_summary,
        "owner_improvement_arc":         qb.owner_improvement_arc,
        "owner_division_record":         qb.owner_division_record,

        # Standings metrics (Group D)
        "team_efficiency":               qb.team_efficiency,
        "strength_of_schedule":          qb.strength_of_schedule,
        "team_home_away":                qb.team_home_away,
        "team_division_standings":       qb.team_division_standings,
        "team_rankings":                 qb.team_rankings,

        # Composite player scores (Group E)
        "qb_composite":                  qb.qb_composite,
        "rb_composite":                  qb.rb_composite,
        "wr_composite":                  qb.wr_composite,
        "defensive_composite":           qb.defensive_composite,
```

- [ ] **Step 4: Verify all 21 names are importable from oracle_query_builder**

```bash
python -c "
import oracle_query_builder as qb
new_fns = [
    'owner_games', 'pythagorean_wins', 'home_away_record', 'blowout_frequency',
    'close_game_record', 'scoring_margin_distribution', 'first_half_second_half',
    'owner_scoring_trend', 'owner_consistency', 'owner_career_summary',
    'owner_improvement_arc', 'owner_division_record',
    'team_efficiency', 'strength_of_schedule', 'team_home_away',
    'team_division_standings', 'team_rankings',
    'qb_composite', 'rb_composite', 'wr_composite', 'defensive_composite',
]
missing = [f for f in new_fns if not hasattr(qb, f)]
if missing:
    print('MISSING:', missing)
else:
    print(f'All {len(new_fns)} functions importable OK')
"
```
Expected: `All 21 functions importable OK`

- [ ] **Step 5: Verify `build_agent_env()` injects all 21**

```bash
python -c "
from oracle_agent import build_agent_env
env = build_agent_env()
new_fns = [
    'owner_games', 'pythagorean_wins', 'home_away_record', 'blowout_frequency',
    'close_game_record', 'scoring_margin_distribution', 'first_half_second_half',
    'owner_scoring_trend', 'owner_consistency', 'owner_career_summary',
    'owner_improvement_arc', 'owner_division_record',
    'team_efficiency', 'strength_of_schedule', 'team_home_away',
    'team_division_standings', 'team_rankings',
    'qb_composite', 'rb_composite', 'wr_composite', 'defensive_composite',
]
missing = [f for f in new_fns if f not in env]
if missing:
    print('NOT IN SANDBOX:', missing)
else:
    print(f'All {len(new_fns)} functions present in sandbox env')
"
```
Expected: `All 21 functions present in sandbox env`

- [ ] **Step 6: Commit**

```bash
git add oracle_agent.py
git commit -m "feat: update oracle_agent _API_REFERENCE, few-shots, and build_agent_env() for 21 new metrics"
```

---

## Task 8: Version bump + final integration test

**Files:**
- Modify: `bot.py` — `ATLAS_VERSION`

- [ ] **Step 1: Bump `ATLAS_VERSION` in `bot.py`**

Find `ATLAS_VERSION` and bump minor version (e.g., `7.6.0` → `7.7.0`).

- [ ] **Step 2: End-to-end integration test — simulate oracle agent code generation**

```bash
python -c "
from oracle_agent import build_agent_env, _safe_run

env = build_agent_env(caller_db='TheWitt')

# Test 1: pythagorean_wins via sandbox
code1 = '''
user = resolve_user('TheWitt') or 'TheWitt'
sql, params = pythagorean_wins(user, season=6)
rows, error = run_sql(sql, params)
for r in rows:
    pf, pa = r['points_for'], r['points_against']
    if pf + pa > 0:
        exp = (pf**2.37 / (pf**2.37 + pa**2.37)) * r['games_played']
        r['expected_wins'] = round(exp, 1)
result = rows
'''
result, error = _safe_run(code1, dict(env))
print('Test 1 (pythagorean_wins):', 'PASS' if error is None else f'FAIL: {error}')

# Test 2: qb_composite via sandbox
code2 = '''
sql, params = qb_composite(season=6)
result, error = run_sql(sql, params)
'''
result2, error2 = _safe_run(code2, dict(env))
print('Test 2 (qb_composite):', 'PASS' if error2 is None else f'FAIL: {error2}')

# Test 3: strength_of_schedule via sandbox
code3 = '''
sql, params = strength_of_schedule()
result, error = run_sql(sql, params)
'''
result3, error3 = _safe_run(code3, dict(env))
print('Test 3 (strength_of_schedule):', 'PASS' if error3 is None else f'FAIL: {error3}')

# Test 4: stat_leaders for new stat via sandbox
code4 = '''
sql, params = stat_leaders('broken tackles', season=6)
result, error = run_sql(sql, params)
'''
result4, error4 = _safe_run(code4, dict(env))
print('Test 4 (stat_leaders broken tackles):', 'PASS' if error4 is None else f'FAIL: {error4}')
"
```
Expected: 4 `PASS` lines.

- [ ] **Step 3: Final commit with version bump**

```bash
git add bot.py
git commit -m "feat: bump ATLAS_VERSION for Tier 1 advanced metrics expansion"
```

- [ ] **Step 4: Push**

```bash
git push
```

---

## Quick Reference: New Public Functions Checklist

All 21 must appear in **both** `oracle_query_builder.py` (defined) **and** `oracle_agent.py` `build_agent_env()` (injected):

| # | Function | Group | File |
|---|----------|-------|------|
| 1 | `owner_games` | A | qb |
| 2 | `pythagorean_wins` | A | qb |
| 3 | `home_away_record` | A | qb |
| 4 | `blowout_frequency` | A | qb |
| 5 | `close_game_record` | A | qb |
| 6 | `scoring_margin_distribution` | A | qb |
| 7 | `first_half_second_half` | A | qb |
| 8 | `owner_scoring_trend` | A | qb |
| 9 | `owner_consistency` | A | qb |
| 10 | `owner_career_summary` | A | qb |
| 11 | `owner_improvement_arc` | A | qb |
| 12 | `owner_division_record` | A | qb |
| 13 | `team_efficiency` | D | qb |
| 14 | `strength_of_schedule` | D | qb |
| 15 | `team_home_away` | D | qb |
| 16 | `team_division_standings` | D | qb |
| 17 | `team_rankings` | D | qb |
| 18 | `qb_composite` | E | qb |
| 19 | `rb_composite` | E | qb |
| 20 | `wr_composite` | E | qb |
| 21 | `defensive_composite` | E | qb |

Internal helper (NOT in sandbox): `_owner_games_cte`
