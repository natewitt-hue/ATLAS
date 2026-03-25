# Tier 1 Advanced Metrics — QueryBuilder Expansion

**Date:** 2026-03-25
**Status:** Draft
**Scope:** `oracle_query_builder.py` + `oracle_agent.py` (`_API_REFERENCE`)

---

## Summary

Expand the Oracle QueryBuilder API with 38 new queryable metrics (21 domain functions + 17 DomainKnowledge stat entries) derived from columns that already exist in `tsl_history.db` but are currently unused. All metrics are low-complexity (pure SQL or single-table queries). Owner-scoped metrics use a shared `_owner_games_cte()` helper that filters by user identity across all their games, excluding CPU opponents.

No new files. No new slash commands. No new dependencies. The code-gen agent (Tier 2) gains these capabilities automatically via updated `_API_REFERENCE` documentation.

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where to add functions | `oracle_query_builder.py` | Single source of truth for QueryBuilder API; agent prompt already references it |
| Ownership boundary | Shared `owner_games()` helper | 12 owner-scoped metrics need identical filtering; DRY, testable, one place to fix bugs |
| How agent discovers functions | Update `_API_REFERENCE` in `oracle_agent.py` | Hardcoded docstring is how the code-gen agent learns the API surface |
| New DomainKnowledge entries | Yes — add missing stat aliases | Enables `stat_leaders()` to work with new stats like "yards per attempt", "catch percentage" |
| Standings-based metrics | Query `standings` table directly | Current-season only (32 rows), but has pre-computed SoS, home/away splits, rankings |
| Historical owner metrics | Compute from `games` table | Standings only has current season; `games` has all 6 seasons |

---

## Ownership Boundary Helper

### `_owner_games_cte()` — Core Primitive

Every owner-scoped metric needs the same base query: "give me all completed, non-CPU games for this owner, optionally filtered by season." This is an **internal helper** (not exposed to the agent) that returns a CTE string + params. Public domain functions call it and append their aggregation query.

```python
def _owner_games_cte(
    user: str,
    season: int | None = None,
    include_playoffs: bool = False,
) -> tuple[str, list]:
    """Returns (cte_sql, params) for the owner_games CTE.

    Domain functions call this, then append their own SELECT against the CTE.
    Scopes by user identity (homeUser/awayUser match), NOT by franchise.
    This means if a user controlled the 49ers in S3 then the Cowboys in S4,
    querying their games returns BOTH — attributed correctly via is_home/user_score.

    CTE columns:
        seasonIndex, weekIndex, stageIndex,
        homeTeamName, awayTeamName, homeScore (INTEGER), awayScore (INTEGER),
        homeUser, awayUser, winner_user, loser_user,
        user_team (the teamName this user was playing as),
        opp_team (the opponent teamName),
        is_home (1 if user was home team, 0 if away),
        user_score (points scored by this user),
        opp_score (points scored by opponent),
        margin (user_score - opp_score, positive = win),
        won (1 if user won, 0 if lost)
    """
```

Key behaviors:
- Filters by user identity: `(g.homeUser = ? OR g.awayUser = ?)` — works across all teams the user has ever controlled
- Filters `status IN ('2','3')` (completed games)
- Filters `stageIndex = '1'` by default; `include_playoffs=True` uses `stageIndex IN ('1','2')`
- Excludes CPU: `homeUser != 'CPU' AND awayUser != 'CPU' AND homeUser != '' AND awayUser != ''`
- Computes derived columns: `user_team`, `opp_team`, `is_home`, `user_score`, `opp_score`, `margin`, `won`
- The `user` param appears **7 times** in the CTE (4 CASE expressions + 2 WHERE matches + 1 for user_team). Document this count prominently in the code.

Public functions consume it like:

```python
def pythagorean_wins(user: str, season: int | None = None) -> tuple[str, tuple]:
    """Expected wins based on points scored/allowed (Pythagorean formula)."""
    cte, params = _owner_games_cte(user, season)
    sql = cte + """
        SELECT seasonIndex,
               SUM(user_score) AS points_for,
               SUM(opp_score) AS points_against,
               SUM(won) AS actual_wins,
               COUNT(*) AS games_played
        FROM og
        GROUP BY seasonIndex
        ORDER BY CAST(seasonIndex AS INTEGER)
    """
    return sql, tuple(params)
```

A public `owner_games()` wrapper is also provided for the code-gen agent to use directly for custom queries.

---

## New Domain Functions (Layer 1)

### Group A: Owner-Scoped Game Metrics (use `owner_games()`)

All return `(sql, params)` tuples following existing convention.

#### A1. `pythagorean_wins(user, season=None)`
- Aggregates `user_score` and `opp_score` from `owner_games()`
- Computes: `PF^2.37 / (PF^2.37 + PA^2.37) * games_played` per season
- Returns: `seasonIndex, actual_wins, expected_wins, luck_factor, games_played`
- Note: Pythagorean exponent 2.37 is the NFL standard. Applied in Python post-query since SQLite lacks power functions — the SQL returns PF, PA, wins, games; Python computes the formula.

#### A2. ~~`luck_factor()`~~ — REMOVED
- Dropped: identical SQL to `pythagorean_wins()`. Luck factor is just `actual_wins - expected_wins`, which the agent can compute in Python from `pythagorean_wins()` output. Document this in the few-shot examples instead of adding a duplicate function.

#### A3. `home_away_record(user, season=None)`
- Groups `owner_games()` by `is_home`
- Returns: `location ("Home"/"Away"), wins, losses, games, win_pct`

#### A4. `blowout_frequency(user, season=None, margin_threshold=17)`
- Filters `owner_games()` by `ABS(margin) >= threshold`
- Returns: `seasonIndex, blowout_wins, blowout_losses, total_games, blowout_pct`

#### A5. `close_game_record(user, season=None, margin_threshold=7)`
- Filters `owner_games()` by `ABS(margin) <= threshold`
- Returns: `seasonIndex, close_wins, close_losses, total_close, close_win_pct`

#### A6. `scoring_margin_distribution(user, season=None)`
- Buckets margins into ranges: 1-3, 4-7, 8-14, 15-21, 22+
- Returns: `margin_bucket, wins, losses, total`

#### A7. `first_half_second_half(user, season=None)`
- Splits by `weekIndex < 8` vs `weekIndex >= 8` (0-based)
- Returns: `half ("First 8"/"Last 8+"), wins, losses, win_pct`

#### A8. `owner_scoring_trend(user, season=None)`
- Per-week PPG from `owner_games()`
- Returns: `weekIndex, avg_user_score, avg_opp_score, margin`

#### A9. `owner_consistency(user, min_games=15)`
- Standard deviation of per-season win totals (only seasons with >= min_games)
- Returns: `seasons_played, avg_wins, stddev_wins, best_season_wins, worst_season_wins`
- Note: SQLite lacks STDDEV — compute in a subquery or return raw per-season wins for Python-side calculation.

#### A10. `owner_career_summary(user)`
- Comprehensive: total W/L, career win%, seasons played, teams controlled, Pythagorean luck
- Returns: `total_wins, total_losses, career_win_pct, seasons, teams (comma-separated)`

#### A11. `owner_improvement_arc(user)`
- Win% per season for plotting trajectory
- Returns: `seasonIndex, wins, losses, win_pct, games_played`

#### A12. `owner_division_record(user, season=None)`
- Filters `owner_games()` to games where opponent is in the same division
- Requires join to `teams` to get `divName` for both user's team and opponent
- Returns: `seasonIndex, div_wins, div_losses, total_div_games`

### Group B: Player Offensive Metrics (new DomainKnowledge entries + stat_leaders support)

These add new entries to `DomainKnowledge.STATS` so the existing `stat_leaders()` function automatically supports them. No new domain functions needed — just registry entries.

| Key | Table | Column | Agg | Pos Filter | Notes |
|-----|-------|--------|-----|------------|-------|
| `"yards per attempt"` | offensive_stats | passYdsPerAtt | AVG | QB | cast_type=REAL |
| `"pass attempts"` | offensive_stats | passAtt | SUM | QB | |
| `"sacks taken"` | offensive_stats | passSacks | SUM | QB | Lower = better for the QB |
| `"longest pass"` | offensive_stats | passLongest | MAX | QB | agg=MAX not SUM |
| `"rush attempts"` | offensive_stats | rushAtt | SUM | None | |
| `"yards per carry"` | offensive_stats | rushYdsPerAtt | AVG | None | cast_type=REAL |
| `"broken tackles"` | offensive_stats | rushBrokenTackles | SUM | None | |
| `"yards after contact"` | offensive_stats | rushYdsAfterContact | SUM | None | |
| `"longest rush"` | offensive_stats | rushLongest | MAX | None | agg=MAX |
| `"20 yard runs"` | offensive_stats | rush20PlusYds | SUM | None | |
| `"catch percentage"` | offensive_stats | recCatchPct | AVG | None | cast_type=REAL |
| `"yards per catch"` | offensive_stats | recYdsPerCatch | AVG | None | cast_type=REAL |
| `"yac per catch"` | offensive_stats | recYacPerCatch | AVG | None | cast_type=REAL |
| `"longest reception"` | offensive_stats | recLongest | MAX | None | agg=MAX |

### Group C: Player Defensive Metrics (new DomainKnowledge entries)

| Key | Table | Column | Agg | Pos Filter | Notes |
|-----|-------|--------|-----|------------|-------|
| `"catches allowed"` | defensive_stats | defCatchAllowed | SUM | None | invert_sort=True (lower = better) |
| `"int return yards"` | defensive_stats | defIntReturnYds | SUM | None | |
| `"safeties"` | defensive_stats | defSafeties | SUM | None | |

### Group D: Standings Metrics (new domain functions)

#### D1. `team_efficiency(team=None)`
- Queries `standings` for offensive/defensive yardage, points, turnover diff
- Returns: `teamName, offTotalYds, offPassYds, offRushYds, defTotalYds, defPassYds, defRushYds, ptsFor, ptsAgainst, netPts, tODiff, winPct`
- If `team` provided, filters to that team. Otherwise returns all 32.

#### D2. `strength_of_schedule(team=None)`
- Queries `standings` for SoS fields
- Returns: `teamName, totalSoS, playedSoS, remainingSoS, initialSoS, totalWins, totalLosses`
- Pre-computed by the API — just expose it.

#### D3. `team_home_away(team=None)`
- Queries `standings` for home/away splits
- Returns: `teamName, homeWins, homeLosses, awayWins, awayLosses`

#### D4. `team_division_standings(division=None, conference=None)`
- Queries `standings` for division/conference records
- Returns: `teamName, divWins, divLosses, confWins, confLosses, divisionName, conferenceName`

#### D5. `team_rankings(team=None)`
- Queries `standings` for all rank columns
- Returns: `teamName, rank, prevRank, offTotalYdsRank, defTotalYdsRank, ptsForRank, ptsAgainstRank`
- Useful for "where does this team rank in offense/defense?"

### Group E: Composite Player Functions

#### E1. `qb_composite(season=None, limit=10)`
- Queries `offensive_stats` for QBs with multiple stat columns
- Returns: `fullName, teamName, passerRating, passCompPct, passTDs, passInts, passYdsPerAtt, passSacks, composite_score`
- `composite_score` = weighted formula (passerRating normalized + TD:INT ratio + YPA - sack_rate)
- Minimum games filter: `HAVING COUNT(*) >= 4`

#### E2. `rb_composite(season=None, limit=10)`
- Returns: `fullName, teamName, rushYds, rushTDs, rushYdsPerAtt, rushBrokenTackles, rushYdsAfterContact, rushFum, composite_score`
- `composite_score` = weighted (YPC + broken_tackles/att + YAC/att - fumble_rate)

#### E3. `wr_composite(season=None, limit=10)`
- Returns: `fullName, teamName, recYds, recTDs, recCatchPct, recYdsPerCatch, recYacPerCatch, recDrops, composite_score`
- `composite_score` = weighted (catch% + YPC + YAC/catch + TDs - drop_rate)

#### E4. `defensive_composite(season=None, limit=10)`
- Returns: `fullName, teamName, defTotalTackles, defSacks, defInts, defForcedFum, defDeflections, defTDs, composite_score`
- `composite_score` = weighted (sacks*2 + INTs*3 + FF*2 + TDs*6 + deflections + tackles*0.5)

---

## DomainKnowledge Additions

Add to the existing `DomainKnowledge.STATS` dict. 17 new entries total (14 offense + 3 defense from Groups B and C above).

**Prerequisite:** Expand `StatDef.agg` type annotation from `Literal["SUM", "AVG"]` to `Literal["SUM", "AVG", "MAX", "MIN"]`. `Query.aggregate()` already validates and accepts MAX/MIN (line 275), but the `StatDef` dataclass restricts the type hint. This is required before adding the 4 MAX-aggregated entries (longest pass/rush/reception, 20 yard runs).

**Aliases:** Add common shorthand aliases to improve natural language matching:
- `"ypa"` → same StatDef as `"yards per attempt"`
- `"ypc"` → same StatDef as `"yards per carry"`
- `"catch pct"` → same StatDef as `"catch percentage"`
- `"yac"` → same StatDef as `"yac per catch"`
- `"broken tackles"` + `"broken tackle rate"` (both map to rushBrokenTackles)

---

## _API_REFERENCE Updates (oracle_agent.py)

Add the following sections to `_API_REFERENCE`:

### New Layer 1 Functions

```
### Owner-Scoped Metrics (all use ownership-boundary filtering)

owner_games(user: str, season: int | None = None, include_playoffs: bool = False) -> (sql, params)
    Base game set for an owner respecting tenure. Excludes CPU games.
    Returns: seasonIndex, weekIndex, homeTeamName, awayTeamName, homeScore, awayScore,
    is_home, user_score, opp_score, margin, won.
    Use as foundation for custom owner queries.

pythagorean_wins(user: str, season: int | None = None) -> (sql, params)
    Expected wins from Pythagorean formula (PF^2.37 / (PF^2.37 + PA^2.37)).
    Returns: seasonIndex, points_for, points_against, actual_wins, games_played.
    NOTE: Compute expected_wins in Python: (pf**2.37 / (pf**2.37 + pa**2.37)) * games

home_away_record(user: str, season: int | None = None) -> (sql, params)
    Owner's record split by home/away. Returns: location, wins, losses, win_pct.

blowout_frequency(user: str, season: int | None = None, margin_threshold: int = 17) -> (sql, params)
    How often an owner wins/loses by 17+ points.

close_game_record(user: str, season: int | None = None, margin_threshold: int = 7) -> (sql, params)
    Owner's record in games decided by 7 or fewer points. Clutch metric.

scoring_margin_distribution(user: str, season: int | None = None) -> (sql, params)
    Win/loss margin histogram in buckets: 1-3, 4-7, 8-14, 15-21, 22+.

first_half_second_half(user: str, season: int | None = None) -> (sql, params)
    Record in first 8 weeks vs last 8+ weeks. Slow starter or fast finisher?

owner_scoring_trend(user: str, season: int | None = None) -> (sql, params)
    Per-week PPG for an owner. Shows mid-season surges and collapses.

owner_consistency(user: str, min_games: int = 15) -> (sql, params)
    Career win consistency. Returns per-season win counts (compute stddev in Python).

owner_career_summary(user: str) -> (sql, params)
    Comprehensive career stats: total W/L, win%, seasons, teams controlled.

owner_improvement_arc(user: str) -> (sql, params)
    Win% per season for trajectory plotting.

owner_division_record(user: str, season: int | None = None) -> (sql, params)
    Owner's record in intra-division games.

### Team Standings Metrics

team_efficiency(team: str | None = None) -> (sql, params)
    Offensive/defensive yardage, points, turnover diff from standings.

strength_of_schedule(team: str | None = None) -> (sql, params)
    Pre-computed SoS: totalSoS, playedSoS, remainingSoS.

team_home_away(team: str | None = None) -> (sql, params)
    Home/away W/L splits from standings.

team_division_standings(division: str | None = None, conference: str | None = None) -> (sql, params)
    Division and conference records.

team_rankings(team: str | None = None) -> (sql, params)
    All rank columns: offense, defense, points for/against ranks.

### Composite Player Scores

qb_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top QBs by weighted composite: passer rating, TD:INT, YPA, sack rate.

rb_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top RBs by weighted composite: YPC, broken tackles, YAC, fumble rate.

wr_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top WRs by weighted composite: catch%, YPC, YAC, drops.

defensive_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top defenders by weighted composite: sacks, INTs, FF, TDs, deflections.
```

### Updated stat_leaders Valid Stats

Add to the existing list:
```
"yards per attempt", "pass attempts", "sacks taken", "longest pass",
"rush attempts", "yards per carry", "broken tackles", "yards after contact",
"longest rush", "20 yard runs", "catch percentage", "yards per catch",
"yac per catch", "longest reception", "catches allowed", "int return yards", "safeties"
```

### New Few-Shot Examples

```python
Q: "How lucky is Witt this season?"
sql, params = pythagorean_wins(resolve_user("Witt"), season=current_season())
rows, error = run_sql(sql, params)
# Compute in Python:
for r in rows:
    pf, pa = r["points_for"], r["points_against"]
    exp = (pf**2.37 / (pf**2.37 + pa**2.37)) * r["games_played"]
    r["expected_wins"] = round(exp, 1)
    r["luck"] = round(r["actual_wins"] - exp, 1)
result = rows

Q: "Who has the best record in close games?"
# Get all owners from owner_tenure, then query each
sql_owners, p_owners = (
    Query("owner_tenure")
    .select("DISTINCT userName")
    .build()
)
owners, _ = run_sql(sql_owners, p_owners)
result = []
for o in owners:
    sql, params = close_game_record(o["userName"], season=current_season())
    rows, _ = run_sql(sql, params)
    if rows:
        result.append({"owner": o["userName"], **rows[0]})
result = sorted(result, key=lambda x: x.get("close_win_pct", 0), reverse=True)[:10]

Q: "What's the strength of schedule for the Ravens?"
sql, params = strength_of_schedule(team="Ravens")
result, error = run_sql(sql, params)

Q: "Who's the best QB this season?"
sql, params = qb_composite(season=current_season())
result, error = run_sql(sql, params)

Q: "Who leads in broken tackles?"
sql, params = stat_leaders("broken tackles", season=current_season())
result, error = run_sql(sql, params)
```

---

## Files Modified

| File | Changes |
|------|---------|
| `oracle_query_builder.py` | Expand `StatDef.agg` Literal to include MAX/MIN. Add `_owner_games_cte()` internal helper + `owner_games()` public wrapper. Add 12 owner-scoped functions (A1-A12), 5 standings functions (D1-D5), 4 composite functions (E1-E4). Add 17+5 DomainKnowledge entries (including aliases). |
| `oracle_agent.py` | Update `_API_REFERENCE` with new function docs. Update `_FEW_SHOT_EXAMPLES` with new examples. **Update `build_agent_env()`** (around line 366) to inject all 21 new public functions into the sandbox environment dict so the code-gen agent can actually call them at runtime. |

No other files affected. No new dependencies. No schema changes.

**Critical:** The `build_agent_env()` function in `oracle_agent.py` explicitly maps function names to callable references for the sandbox. Every new public domain function must be added there. Missing entries will cause the agent to generate code that references undefined names → runtime `NameError`.

---

## Implementation Notes

### SQLite Limitations
- **No POWER function**: Pythagorean formula (`PF^2.37`) must be computed in Python after the SQL returns raw PF/PA/wins/games. The SQL aggregates; Python computes the exponent.
- **No STDDEV function**: `owner_consistency()` returns per-season win counts. Standard deviation computed in Python by the code-gen agent.
- **All TEXT columns**: Every numeric comparison needs `CAST(col AS INTEGER)` or `CAST(col AS REAL)`. The Query builder handles this automatically for aggregations, but raw WHERE clauses need manual CAST.

### _owner_games_cte() Implementation Strategy
Use a CTE (Common Table Expression) pattern. The `user` param is repeated **7 times** (documented in-code):

```python
def _owner_games_cte(
    user: str,
    season: int | None = None,
    include_playoffs: bool = False,
) -> tuple[str, list]:
    """Returns (cte_sql, params) for the owner_games CTE.

    Domain functions call this, append their SELECT against `og`, and
    combine params: tuple(cte_params + their_own_params).

    The `user` param appears 7 times:  # ← document this prominently
      - 2x in CASE for user_team/opp_team
      - 1x in CASE for is_home
      - 1x in CASE for user_score
      - 1x in CASE for opp_score
      - 1x in CASE for won (winner_user match)
      - But wait — we need homeUser match in WHERE too.
    Total: user appears in 4 CASE blocks + 2 WHERE conditions = 7 params.
    """
    # 7 occurrences of user in the CTE
    params = [user] * 7
    stages = "('1','2')" if include_playoffs else "('1')"

    cte = f"""WITH og AS (
    SELECT
        g.seasonIndex, g.weekIndex, g.stageIndex,
        g.homeTeamName, g.awayTeamName,
        CAST(g.homeScore AS INTEGER) AS homeScore,
        CAST(g.awayScore AS INTEGER) AS awayScore,
        g.homeUser, g.awayUser, g.winner_user, g.loser_user,
        CASE WHEN g.homeUser = ? THEN g.homeTeamName
             ELSE g.awayTeamName END AS user_team,
        CASE WHEN g.homeUser = ? THEN g.awayTeamName
             ELSE g.homeTeamName END AS opp_team,
        CASE WHEN g.homeUser = ? THEN 1 ELSE 0 END AS is_home,
        CASE WHEN g.homeUser = ?
             THEN CAST(g.homeScore AS INTEGER)
             ELSE CAST(g.awayScore AS INTEGER) END AS user_score,
        CASE WHEN g.homeUser = ?
             THEN CAST(g.awayScore AS INTEGER)
             ELSE CAST(g.homeScore AS INTEGER) END AS opp_score,
        CASE WHEN g.winner_user = ? THEN 1 ELSE 0 END AS won
    FROM games g
    WHERE g.status IN ('2','3')
      AND g.stageIndex IN {stages}
      AND (g.homeUser = ? OR g.awayUser = ?)
      AND g.homeUser != 'CPU' AND g.awayUser != 'CPU'
      AND g.homeUser != '' AND g.awayUser != ''
)
"""
    # The WHERE clause has 2 user refs but they're in an OR, using a single ?
    # Wait — (homeUser = ? OR awayUser = ?) needs 2 params.
    # Recount: 6 CASE params + 2 WHERE params = 8 total. Fix:
    params = [user] * 8

    if season is not None:
        # Inject season filter before the closing paren of the CTE
        # Easiest: add to WHERE before the closing )
        cte = cte.replace(
            "AND g.homeUser != '' AND g.awayUser != ''",
            "AND g.homeUser != '' AND g.awayUser != ''\n      AND g.seasonIndex = ?"
        )
        params.append(str(season))
    return cte, params
```

Note: The CTE now includes `user_team`, `opp_team`, and computes `margin` is NOT in the CTE (to keep it simple) — downstream functions compute `(user_score - opp_score)` as needed in their own SELECT. This avoids adding another derived column to every row.

The public `owner_games()` wrapper simply returns the CTE + `SELECT * FROM og`:
```python
def owner_games(user, season=None, include_playoffs=False):
    cte, params = _owner_games_cte(user, season, include_playoffs)
    return cte + "SELECT * FROM og", tuple(params)
```

### Composite Score Computation
SQLite can compute weighted composites inline, but **all stat columns must be aggregated** since we GROUP BY player. Use AVG for rate stats, SUM for counting stats:

```sql
SELECT fullName, teamName,
    AVG(CAST(passerRating AS REAL)) AS avg_rating,
    SUM(CAST(passTDs AS INTEGER)) AS total_tds,
    SUM(CAST(passInts AS INTEGER)) AS total_ints,
    AVG(CAST(passYdsPerAtt AS REAL)) AS avg_ypa,
    SUM(CAST(passSacks AS INTEGER)) AS total_sacks,
    SUM(CAST(passAtt AS INTEGER)) AS total_att,
    (AVG(CAST(passerRating AS REAL)) * 0.3
     + (CAST(SUM(CAST(passTDs AS INTEGER)) AS REAL)
        / NULLIF(SUM(CAST(passInts AS INTEGER)), 0)) * 10
     + AVG(CAST(passYdsPerAtt AS REAL)) * 5
     - (CAST(SUM(CAST(passSacks AS INTEGER)) AS REAL)
        / NULLIF(SUM(CAST(passAtt AS INTEGER)), 0)) * 20
    ) AS composite_score
FROM offensive_stats
WHERE pos = 'QB' AND stageIndex = '1'
GROUP BY fullName, teamName
HAVING COUNT(*) >= 4
ORDER BY composite_score DESC
LIMIT 10
```

The exact weights are tunable. Initial values should be documented as constants in the code and adjustable. All 4 composite functions (QB, RB, WR, DEF) must use `HAVING COUNT(*) >= 4` to filter low-sample-size players.

### owner_division_record() Implementation Note
This function requires joining the opponent's team to the `teams` table to get division membership:
```sql
-- After the owner_games CTE:
SELECT og.seasonIndex,
       SUM(og.won) AS div_wins,
       SUM(CASE WHEN og.won = 0 THEN 1 ELSE 0 END) AS div_losses,
       COUNT(*) AS total_div_games
FROM og
JOIN teams ut ON og.user_team = ut.displayName   -- user's team division
JOIN teams ot ON og.opp_team = ot.displayName     -- opponent's division
WHERE ut.divName = ot.divName                      -- same division
GROUP BY og.seasonIndex
```
**Critical join column:** `games.homeTeamName` uses `displayName` format (e.g., "49ers", "Bears"), NOT `nickName` (which is "Niners", "Bears"). The `teams` table has both — `nickName` and `displayName` can differ (49ers vs Niners). Always join on `teams.displayName` to match game data.

### Testing
- Each new function should be callable standalone: `sql, params = pythagorean_wins("TheWitt")` then `run_sql(sql, params)`.
- Verify CPU games are excluded in all owner-scoped queries.
- Verify mid-season transitions work: query 49ers Season 3 should return separate results for Villanova46 (4 games) and Drakee_GG (14 games).
- Verify new DomainKnowledge entries work with `stat_leaders()`: `stat_leaders("broken tackles")` should return results.

---

## Function Count Summary

| Group | Functions | Type |
|-------|-----------|------|
| A: Owner-Scoped | 11 domain functions + 1 public wrapper + 1 internal helper | `OWNER` scope, uses `_owner_games_cte()` |
| B: Offensive Stats | 14 DomainKnowledge entries | Extends `stat_leaders()` automatically |
| C: Defensive Stats | 3 DomainKnowledge entries | Extends `stat_leaders()` automatically |
| D: Standings | 5 domain functions | `TEAM` scope, queries standings table |
| E: Composites | 4 domain functions | Player-level, custom SQL with weights |
| **Total** | **20 new public functions + 1 internal helper + 17+5 stat entries = 37 new queryable metrics** |
