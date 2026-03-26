"""
oracle_agent.py — Code-Gen Agent for Oracle v3 Phase 3
═══════════════════════════════════════════════════════════════════════════════
Generates Python code against the QueryBuilder API, runs in a sandbox,
retries on failure. Replaces Tier 2 (Gemini classification) and Tier 3 (NL→SQL).

Public API:
    run_agent(question, caller_db, alias_map, conv_context, schema) → AgentResult
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from dataclasses import dataclass, field

import atlas_ai
from atlas_ai import Tier
from reasoning import _SAFE_BUILTINS, validate_sandbox_ast, _UnsafeCodeError

log = logging.getLogger("oracle_agent")

MAX_RETRIES = 2  # 3 total attempts (1 + 2 retries)


# ══════════════════════════════════════════════════════════════════════════════
#  RESULT TYPE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentResult:
    """Result from the Code-Gen Agent."""
    data: list[dict] | str | dict   # Query results
    sql: str                         # Last SQL executed (for footer)
    code: str                        # Generated Python code
    error: str | None                # None on success
    attempts: int                    # 1-3


# ══════════════════════════════════════════════════════════════════════════════
#  API REFERENCE (injected into system prompt)
# ══════════════════════════════════════════════════════════════════════════════

_API_REFERENCE = """
## Available Functions (all synchronous, already in scope)

### SQL Execution
run_sql(sql: str, params: tuple = ()) -> tuple[list[dict], str | None]
    Run a parameterized SELECT query. Returns (rows, error).
    Max 50 rows. Read-only. Error is None on success, error string on failure.

### Layer 1: Domain Functions (return (sql, params) — pass to run_sql())

h2h(u1: str, u2: str, season: int | None = None) -> (sql, params)
    Head-to-head record between two owners. Returns per-season breakdown
    with u1_wins, u2_wins, games_played columns.

owner_record(user: str, season: int | None = None) -> (sql, params)
    Owner's win/loss record. Returns wins, losses, games_played per season.

team_record(team: str, season: int | None = None) -> (sql, params)
    Team's win/loss record by team name (e.g., "Lions", "Packers").

standings_query(division: str | None = None, conference: str | None = None) -> (sql, params)
    Current standings. Returns teamName, wins, losses, pf, pa, divisionName, seed.

streak_query(user: str) -> (sql, params)
    Last 20 games for a user. Compute streak in Python from winner_user column.

stat_leaders(stat: str, season: int | None = None, sort: str = "best", limit: int = 10) -> (sql, params)
    Player stat leaders. Domain-aware: sort="best" auto-inverts for defense stats.
    sort="worst" switches to efficiency_alt (e.g., passYds worst → passerRating).
    Valid stats: "passing yards", "passing touchdowns", "rushing yards", "rushing touchdowns",
    "receiving yards", "receiving touchdowns", "receptions", "catches", "drops",
    "yards after catch", "passer rating", "completion percentage", "completions",
    "interceptions thrown", "fumbles", "tackles", "sacks", "interceptions",
    "forced fumbles", "fumble recoveries", "defensive tds", "pass deflections",
    "yards per attempt", "ypa", "pass attempts", "sacks taken", "longest pass",
    "rush attempts", "yards per carry", "ypc", "broken tackles", "broken tackle rate",
    "yards after contact", "longest rush", "20 yard runs",
    "catch percentage", "catch pct", "yards per catch", "yac per catch", "yac",
    "longest reception", "catches allowed", "int return yards", "safeties".

team_stat_leaders(stat: str, season: int | None = None, sort: str = "best", limit: int = 10) -> (sql, params)
    Team stat leaders. Valid stats: "team total yards", "team pass yards", "team rush yards",
    "team total yards allowed", "team pass yards allowed", "team rush yards allowed",
    "team sacks", "team takeaways", "team turnover diff", "team pass tds", "team rush tds",
    "penalties", "penalty yards".

roster_query(team: str, pos: str | None = None) -> (sql, params)
    Team roster sorted by OVR. Returns fullName, pos, ovr, dev, age.

free_agents_query(pos: str | None = None, min_ovr: int | None = None) -> (sql, params)
    Free agents sorted by OVR.

draft_picks_query(team: str | None = None, season: int | None = None, round_num: int | None = None) -> (sql, params)
    Draft history from player_draft_map. Returns extendedName, drafting_team, draftRound, draftPick, pos, ovr.

abilities_query(team: str | None = None, player: str | None = None) -> (sql, params)
    X-Factor/Superstar abilities. Search by team or player name.

trades_query(team: str | None = None, season: int | None = None) -> (sql, params)
    Trade history. Returns team1Name, team2Name, team1Sent, team2Sent.

owner_history_query(user: str | None = None, team: str | None = None) -> (sql, params)
    Owner tenure history. Returns teamName, userName, seasonIndex, games_played.

game_extremes(extreme_type: str = "blowout", season: int | None = None, limit: int = 5) -> (sql, params)
    Extreme games. extreme_type: "blowout", "closest", "highest_scoring".

recent_games_query(user: str, limit: int = 5, opponent: str | None = None) -> (sql, params)
    Recent games for a user, optionally filtered by opponent.

compare_seasons(stat: str, user_or_team: str, season1: int, season2: int) -> (sql, params)
    Compare a stat between two seasons for a user or team.

improvement_leaders(stat: str, season1: int, season2: int, limit: int = 10) -> (sql, params)
    Players who improved the most in a stat between two seasons.

career_trajectory(user: str, stat: str) -> (sql, params)
    Season-by-season stat trajectory for an owner's team.

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
    Top RBs (pos=RB only) by weighted composite: YPC, broken tackles/att, YAC/att, fumble rate.

wr_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top WRs/TEs by weighted composite: catch%, YPC, YAC/catch, TDs, drop rate.
    NOTE: Scores are NOT comparable to other position composites.

defensive_composite(season: int | None = None, limit: int = 10) -> (sql, params)
    Top defenders by weighted composite: sacks, INTs, forced fumbles, TDs, deflections, tackles.

### Layer 2: Query Builder (for custom queries not covered by Layer 1)

Query(table: str)
    Fluent SQL builder. Valid tables: games, teams, standings, offensive_stats,
    defensive_stats, team_stats, trades, players, player_abilities, owner_tenure, player_draft_map.

    Methods (all return self for chaining):
    .select(*cols)          — Add SELECT columns
    .filter(season=N, stage="regular"|"playoffs", team="X", user="X", pos="QB", status=True)
    .where(clause, *params) — Raw WHERE with ? placeholders
    .aggregate(col="SUM"|"AVG"|"COUNT"|"MIN"|"MAX")  — Auto-CAST wrapped
    .group_by(*cols)
    .having(clause, *params)
    .sort_by(col, direction="best"|"worst"|"DESC"|"ASC") — Domain-aware sorting
    .limit(n)
    .pos(position)          — Filter by player position
    .build()                — Returns (sql: str, params: tuple)

    Example:
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
    rows, error = run_sql(sql, params)

### Layer 3: Utilities

current_season() -> int           — Current TSL season number
current_week() -> int             — Current TSL week number (1-based)
resolve_user(name: str) -> str | None  — Fuzzy resolve a name to db_username
resolve_team(name: str) -> str | None  — Resolve team name/abbreviation to canonical teamName
compare_datasets(dataset1, dataset2, key, metric="delta"|"pct_change") -> list[dict]
summarize(dataset: list[dict]) -> dict  — Row count + numeric column stats

DomainKnowledge.lookup(text: str) -> tuple[str, StatDef] | None
    Find matching stat definition by text substring. Returns (matched_key, StatDef).
DomainKnowledge.get(name: str) -> StatDef | None
    Exact key lookup.

### Context Variables
CALLER  — str | None, the db_username of the person asking (use for "my"/"me"/"I" queries)
ALIASES — dict[str, str], resolved name aliases {mentioned_text: db_username}

### Database Schema Notes
- All columns stored as TEXT. Use CAST(col AS INTEGER) or CAST(col AS REAL) for math.
- Completed games: status IN ('2','3'). Never include unplayed games.
- Regular season: stageIndex='1'. Playoffs: stageIndex='2'.
- seasonIndex is 1-based. weekIndex is 0-based in DB but CURRENT_WEEK is 1-based.
- Owner queries: use exact usernames. For "my" queries, use CALLER variable.
- For draft queries: use player_draft_map table, NOT players.teamName.
"""

_FEW_SHOT_EXAMPLES = """
## Examples

Q: "Who leads the league in passing yards this season?"
```python
sql, params = stat_leaders("passing yards", season=current_season())
result, error = run_sql(sql, params)
```

Q: "What's Killa's record vs JT?"
```python
u1 = resolve_user("Killa")
u2 = resolve_user("JT")
sql, params = h2h(u1, u2)
result, error = run_sql(sql, params)
```

Q: "Which WRs on the Lions have the most receiving yards this season?"
```python
sql, params = (
    Query("offensive_stats")
    .select("fullName", "teamName")
    .filter(season=current_season(), stage="regular", team="Lions")
    .pos("WR")
    .aggregate(recYds="SUM", recTDs="SUM", recCatches="SUM")
    .group_by("fullName", "teamName")
    .sort_by("recYds", direction="best")
    .limit(10)
    .build()
)
result, error = run_sql(sql, params)
```

Q: "Which owner improved the most in wins from season 4 to season 5?"
```python
# Get records for both seasons
sql1, p1 = (
    Query("games")
    .select("winner_user")
    .filter(season=4, stage="regular", status=True)
    .where("winner_user != ''")
    .build()
)
rows1, _ = run_sql(sql1, p1)

sql2, p2 = (
    Query("games")
    .select("winner_user")
    .filter(season=5, stage="regular", status=True)
    .where("winner_user != ''")
    .build()
)
rows2, _ = run_sql(sql2, p2)

# Count wins per owner per season
wins_s4 = Counter(r["winner_user"] for r in rows1)
wins_s5 = Counter(r["winner_user"] for r in rows2)

# Compute improvement
result = []
for owner in set(wins_s4) | set(wins_s5):
    w4, w5 = wins_s4.get(owner, 0), wins_s5.get(owner, 0)
    result.append({"owner": owner, "season4_wins": w4, "season5_wins": w5, "improvement": w5 - w4})
result = sorted(result, key=lambda x: x["improvement"], reverse=True)[:10]
```

Q: "What's my record this season?"
```python
sql, params = owner_record(CALLER, season=current_season())
result, error = run_sql(sql, params)
```

Q: "How lucky has Witt been this season?"
```python
user = resolve_user("Witt") or "TheWitt"
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
"""


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_system_prompt(
    schema: str,
    caller_db: str | None,
    alias_map: dict[str, str] | None,
    conv_context: str,
) -> str:
    """Assemble the full system prompt for the Code-Gen Agent."""
    parts = [
        "You are a Python code-generation agent for The Simulation League (TSL) "
        "Madden franchise database. Your job: write Python code that answers "
        "a user's question about TSL league data.",
        "",
        "## Database Schema",
        schema or "(schema unavailable)",
        "",
        _API_REFERENCE,
        "",
        "## Rules",
        "1. Write ONLY Python code. No markdown fences, no explanation, no comments except inline.",
        "2. Assign the final answer to `result`. It must be list[dict] (preferred), str, or dict.",
        "3. Prefer Layer 1 domain functions > Query builder > raw run_sql().",
        "4. All domain functions return (sql, params). Pass them to run_sql(sql, params).",
        "5. Do NOT use Query.build() then discard — always pass result to run_sql().",
        "6. Use resolve_user() for loose name lookups. Use CALLER for 'my'/'me'/'I' queries.",
        f"7. Current season: {_get_current_season()}, current week: {_get_current_week()}.",
        "8. Handle empty results: if run_sql returns no rows, set result to a descriptive string.",
        "9. Counter from collections is available in scope.",
        "10. Never use import statements — everything you need is already in scope.",
        "",
    ]

    if caller_db:
        parts.append(f"CALLER identity: '{caller_db}'")
    if alias_map:
        aliases_str = ", ".join(f"'{k}' → '{v}'" for k, v in alias_map.items())
        parts.append(f"Resolved names: {aliases_str}")
    if conv_context:
        parts.append(f"\n## Conversation History\n{conv_context}")

    parts.append("")
    parts.append(_FEW_SHOT_EXAMPLES)

    return "\n".join(parts)


def _get_current_season() -> int:
    try:
        import oracle_query_builder as qb
        return qb.current_season()
    except Exception:
        log.warning("Failed to resolve current season — falling back to data_manager")
        try:
            import data_manager as dm
            return int(dm.CURRENT_SEASON)
        except Exception:
            log.error("Cannot determine current season from any source")
            return 1  # Safer than a stale hardcode — obviously wrong prompts investigation


def _get_current_week() -> int:
    try:
        import oracle_query_builder as qb
        return qb.current_week()
    except Exception:
        log.warning("Failed to resolve current week — falling back to data_manager")
        try:
            import data_manager as dm
            return int(dm.CURRENT_WEEK)
        except Exception:
            log.error("Cannot determine current week from any source")
            return 1


# ══════════════════════════════════════════════════════════════════════════════
#  SANDBOX ENVIRONMENT
# ══════════════════════════════════════════════════════════════════════════════

def _make_capturing_run_sql(capture_list: list):
    """Create a run_sql wrapper that records every SQL statement run."""
    from codex_utils import run_sql as _real_run_sql

    def capturing_run_sql(sql, params=()):
        capture_list.append(sql)
        return _real_run_sql(sql, params)

    return capturing_run_sql


def build_agent_env(
    caller_db: str | None = None,
    alias_map: dict[str, str] | None = None,
    sql_capture: list | None = None,
) -> dict:
    """Build sandboxed globals for agent-generated code.

    Injects QueryBuilder API (Layer 1-3), run_sql, and safe builtins.
    """
    import oracle_query_builder as qb
    from collections import Counter

    capturing = _make_capturing_run_sql(sql_capture if sql_capture is not None else [])

    env = {
        "__builtins__": _SAFE_BUILTINS,
        "result": None,

        # SQL run (capturing proxy)
        "run_sql": capturing,

        # Layer 1: Domain functions
        "h2h": qb.h2h,
        "owner_record": qb.owner_record,
        "team_record": qb.team_record,
        "standings_query": qb.standings_query,
        "streak_query": qb.streak_query,
        "stat_leaders": qb.stat_leaders,
        "team_stat_leaders": qb.team_stat_leaders,
        "roster_query": qb.roster_query,
        "free_agents_query": qb.free_agents_query,
        "draft_picks_query": qb.draft_picks_query,
        "abilities_query": qb.abilities_query,
        "trades_query": qb.trades_query,
        "owner_history_query": qb.owner_history_query,
        "game_extremes": qb.game_extremes,
        "recent_games_query": qb.recent_games_query,
        "compare_seasons": qb.compare_seasons,
        "improvement_leaders": qb.improvement_leaders,
        "career_trajectory": qb.career_trajectory,

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

        # Layer 2: Query builder
        "Query": qb.Query,

        # Layer 3: Utilities
        "DomainKnowledge": qb.DomainKnowledge,
        "current_season": qb.current_season,
        "current_week": qb.current_week,
        "resolve_user": qb.resolve_user,
        "resolve_team": qb.resolve_team,
        "compare_datasets": qb.compare_datasets,
        "summarize": qb.summarize,

        # Caller context
        "CALLER": caller_db,
        "ALIASES": alias_map or {},

        # Extra safe utilities
        "Counter": Counter,
    }
    return env


# ══════════════════════════════════════════════════════════════════════════════
#  CODE EXTRACTION & SANDBOX RUN
# ══════════════════════════════════════════════════════════════════════════════

_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n?(.*?)```", re.DOTALL)


def _extract_code(text: str) -> str:
    """Extract Python code from AI response, stripping markdown fences if present."""
    m = _CODE_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # No fences — treat entire response as code
    return text.strip()


def _safe_run(code: str, env: dict) -> tuple:
    """Run generated code in sandboxed env. Returns (result, error_str | None)."""
    # AST validation — reject dunder traversal before running
    try:
        validate_sandbox_ast(code)
    except _UnsafeCodeError as exc:
        return None, str(exc)
    try:
        compiled = compile(code, "<oracle_agent>", "exec")
        # pylint: disable=exec-used
        _sandbox_exec(compiled, env)
        result = env.get("result")
        return result, None
    except Exception:
        return None, traceback.format_exc()


def _sandbox_exec(compiled_code, env: dict):
    """Isolated function that runs compiled code in the given env.

    Separated so the call stack is clear in tracebacks.
    Security: env["__builtins__"] is restricted to _SAFE_BUILTINS,
    blocking __import__, open, eval, type, and other escape vectors.
    """
    # This is intentional sandboxed code evaluation — the env restricts
    # all dangerous builtins via _SAFE_BUILTINS from reasoning.py
    globs = env
    locs = None  # use globs as locals too
    _run_in_sandbox(compiled_code, globs)


def _run_in_sandbox(code_obj, globs):
    """Actually run the code object. Builtins are restricted via globs['__builtins__']."""
    # Security boundary: __builtins__ is _SAFE_BUILTINS (no import/open/eval/type)
    exec(code_obj, globs)  # noqa: S102 — intentional sandboxed execution


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def run_agent(
    question: str,
    caller_db: str | None = None,
    alias_map: dict[str, str] | None = None,
    conv_context: str = "",
    schema: str = "",
) -> AgentResult:
    """Code-Gen Agent entry point.

    Generates Python code against the QueryBuilder API, runs in sandbox,
    retries up to 2 times on failure.
    """
    system = _build_system_prompt(schema, caller_db, alias_map, conv_context)
    prompt = f"QUESTION: {question}"

    loop = asyncio.get_running_loop()

    code = ""
    last_error = ""

    for attempt in range(1, MAX_RETRIES + 2):  # 1, 2, 3
        # Generate code
        if attempt == 1:
            ai_result = await atlas_ai.generate(
                prompt,
                system=system,
                tier=Tier.SONNET,
                max_tokens=2048,
                temperature=0.05,
            )
        else:
            # Retry with error context
            retry_prompt = (
                f"Your previous code failed with this error:\n\n"
                f"```\n{last_error}\n```\n\n"
                f"Previous code:\n```python\n{code}\n```\n\n"
                f"Original question: {question}\n\n"
                f"Fix the code. Return ONLY Python code, no explanation."
            )
            ai_result = await atlas_ai.generate(
                retry_prompt,
                system=system,
                tier=Tier.SONNET,
                max_tokens=2048,
                temperature=0.1 * attempt,  # slight temp increase on retry
            )

        code = _extract_code(ai_result.text)
        if not code:
            last_error = "Empty code generated"
            continue

        # Run in sandbox
        sql_capture: list[str] = []
        env = build_agent_env(caller_db, alias_map, sql_capture)

        try:
            data, error = await asyncio.wait_for(
                loop.run_in_executor(None, _safe_run, code, env),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            data, error = None, "Sandbox timed out after 15 seconds"

        if error is None and data is not None:
            captured_sql = sql_capture[-1] if sql_capture else ""
            log.info(
                "[oracle_agent] Success on attempt %d | SQLs: %d | question: %s",
                attempt, len(sql_capture), question[:80],
            )
            return AgentResult(
                data=data,
                sql=captured_sql,
                code=code,
                error=None,
                attempts=attempt,
            )

        last_error = error or "Code ran but `result` was None"
        log.warning(
            "[oracle_agent] Attempt %d failed | error: %s | question: %s",
            attempt, last_error[:200], question[:80],
        )

    # All attempts exhausted
    log.error(
        "[oracle_agent] All %d attempts failed for: %s",
        MAX_RETRIES + 1, question[:100],
    )
    return AgentResult(
        data=[],
        sql="",
        code=code,
        error=last_error,
        attempts=MAX_RETRIES + 1,
    )
