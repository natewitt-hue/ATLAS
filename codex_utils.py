"""
codex_utils.py — Shared SQL, schema, and identity-resolution utilities.

Extracted from codex_cog.py to break the oracle_cog → codex_cog dependency
chain and prevent circular imports (Finding #14).

Used by: codex_cog.py, oracle_cog.py, codex_intents.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from difflib import get_close_matches

import atlas_ai
from atlas_ai import Tier
import data_manager as dm

log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
DB_PATH  = os.path.join(os.path.dirname(__file__), "tsl_history.db")
MAX_ROWS = 50


# ── DB helpers ──────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def run_sql(sql: str, params: tuple = ()) -> tuple[list[dict], str | None]:
    """Execute SQL, return (rows, error).  Supports parameterized queries."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped.upper().startswith("SELECT"):
        return [], "Only SELECT queries are allowed"
    if ";" in stripped:
        return [], "Multi-statement queries are not allowed"
    try:
        conn = get_db()
        try:
            conn.execute("PRAGMA query_only = ON")
            cur = conn.execute(stripped, params)
            rows = [dict(r) for r in cur.fetchall()]
            return rows[:MAX_ROWS], None
        finally:
            conn.close()
    except Exception as e:
        return [], str(e)


async def run_sql_async(sql: str, params: tuple = ()) -> tuple[list[dict], str | None]:
    """Non-blocking wrapper for run_sql — dispatches to thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_sql, sql, params)


def extract_sql(text: str) -> str | None:
    """Pull SQL out of AI response."""
    # Pattern 1: fenced — DOTALL needed for multi-line SQL inside ```
    match = re.search(r"```(?:sql)?\s*(SELECT.+?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # Pattern 2: unfenced — NO DOTALL so we don't swallow explanation text
    # after the SQL. MULTILINE makes $ match end-of-line, not end-of-string.
    match = re.search(r"(SELECT\s.+?);?\s*$", text, re.MULTILINE | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def validate_sql(sql: str) -> list[str]:
    """Check generated SQL for common TSL-specific pitfalls.

    Returns a list of warning strings (empty = no issues found).
    These are injected into the self-correction prompt if the query fails,
    giving the AI targeted hints about what went wrong.
    """
    warnings = []
    sql_upper = sql.upper()

    # Check 1: games table queried without status filter
    if "GAMES" in sql_upper and "STATUS" not in sql_upper:
        if "offensive_stats" not in sql.lower() and "defensive_stats" not in sql.lower():
            warnings.append(
                "Missing status IN ('2','3') filter on games table — "
                "will include unplayed/scheduled games."
            )

    # Check 2: Numeric comparison without CAST on TEXT columns
    numeric_patterns = [
        r"ORDER BY\s+\w*(?:score|wins|losses|yds|tds|ints|tackles|sacks|rating|ovr)\w*\s+(?:ASC|DESC)",
        r"(?:>|<|>=|<=)\s*\d+",
    ]
    has_numeric_op = any(re.search(p, sql, re.IGNORECASE) for p in numeric_patterns)
    if has_numeric_op and "CAST(" not in sql_upper:
        warnings.append(
            "Numeric comparison or ordering without CAST() — "
            "all columns are TEXT, so '9' > '80' in string comparison."
        )

    # Check 3: Using players.teamName for draft queries
    if "DRAFT" in sql_upper and "PLAYERS" in sql_upper and "PLAYER_DRAFT_MAP" not in sql_upper:
        warnings.append(
            "Draft query uses players table — should use player_draft_map instead "
            "(players.teamName shows current team, not drafting team)."
        )

    # Check 4: Using fullName on players table (doesn't exist)
    if "PLAYERS" in sql_upper and "FULLNAME" in sql_upper:
        if "OFFENSIVE_STATS" not in sql_upper and "DEFENSIVE_STATS" not in sql_upper:
            warnings.append(
                "players table has no fullName column — "
                "use (firstName || ' ' || lastName) instead."
            )

    return warnings


async def retry_sql(
    sql: str,
    schema: str,
    *,
    params: tuple = (),
) -> tuple[list[dict], str, str | None, int, list[str]]:
    """Execute SQL with 3-attempt progressive retry cascade.

    Attempt 1: Execute as-is.
    Attempt 2: Haiku + error + validate_sql() hints + full schema.
    Attempt 3: Opus + both previous attempts + "think step by step".

    params are only used for Attempt 1. AI-regenerated SQL in Attempts
    2/3 is executed without params since the AI produces literal values.

    Returns:
        (rows, final_sql, error, attempt_num, warnings)
        - rows: query results (capped at MAX_ROWS)
        - final_sql: the SQL that ultimately ran (may differ from input)
        - error: None on success, error string if all 3 attempts failed
        - attempt_num: 1 = first try, 2 = Haiku self-correct, 3 = Opus rescue
        - warnings: validate_sql() output (empty list if not used)
    """
    # ── Attempt 1 ─────────────────────────────────────────
    rows, error_1 = await run_sql_async(sql, params)
    if not error_1:
        return rows, sql, None, 1, []

    sql_1 = sql  # preserve original for attempt 3

    # ── Attempt 2: Haiku + validation hints ─────────────
    warnings = validate_sql(sql)
    hint_block = "\n".join(f"- {w}" for w in warnings) if warnings else ""

    fix_prompt_2 = (
        f"This SQLite query failed:\n{sql_1}\n\n"
        f"Error: {error_1}\n\n"
        f"REMINDER: ALL columns are TEXT. Use CAST(col AS INTEGER) for math.\n"
        f"{f'Validation warnings:\n{hint_block}\n' if hint_block else ''}"
        f"Fix the query. Return ONLY valid SQLite SQL.\n\n"
        f"Schema:\n{schema}"
    )
    # Safety: AI-generated SQL in retries is still guarded by run_sql()'s
    # SELECT-only check, preventing INSERT/UPDATE/DELETE injection.
    fix_result_2 = await atlas_ai.generate(
        fix_prompt_2, tier=Tier.HAIKU, max_tokens=500, temperature=0.02
    )
    sql_2 = extract_sql(fix_result_2.text)
    if not sql_2:
        print(f"[retry_sql] Attempt 2: AI returned no SQL, skipping to Attempt 3")
        error_2 = error_1  # carry forward for Attempt 3 prompt
        sql_2 = sql_1
    else:
        rows, error_2 = await run_sql_async(sql_2)
        if not error_2:
            return rows, sql_2, None, 2, warnings

    # ── Attempt 3: Opus + full history + reasoning ────────
    fix_prompt_3 = (
        f"Two SQL attempts against this schema both failed.\n\n"
        f"Attempt 1:\n{sql_1}\nError: {error_1}\n\n"
        f"Attempt 2:\n{sql_2}\nError: {error_2}\n\n"
        f"{f'Validation warnings:\n{hint_block}\n' if hint_block else ''}"
        f"Think step by step: which tables and columns are needed, "
        f"what JOINs are required, and what CAST operations are necessary. "
        f"Then write the corrected SQL. Return ONLY valid SQLite SQL.\n\n"
        f"Schema:\n{schema}"
    )
    fix_result_3 = await atlas_ai.generate(
        fix_prompt_3, tier=Tier.OPUS, max_tokens=800, temperature=0.02
    )
    sql_3 = extract_sql(fix_result_3.text)
    if not sql_3:
        print(f"[retry_sql] Attempt 3: AI returned no SQL, giving up")
        return [], sql_2, error_2, 3, warnings
    rows, error_3 = await run_sql_async(sql_3)
    if not error_3:
        return rows, sql_3, None, 3, warnings

    return [], sql_3, error_3, 3, warnings


# ── Schema fed to AI for SQL generation ──────────────────────────────────────

def _build_schema() -> str:
    return f"""DATABASE: tsl_history.db  ─  The Simulation League (TSL) Madden franchise history

IMPORTANT RULES:
- All column values are stored as TEXT even if they look like numbers. Cast with CAST(col AS INTEGER) or CAST(col AS REAL) when doing math/comparisons.
- seasonIndex: '1'=Season 1 (2025), '2'=Season 2 (2026)... '{dm.CURRENT_SEASON}'=Season {dm.CURRENT_SEASON} (current)
- stageIndex: '0'=Preseason, '1'=Regular Season, '2'=Playoffs
- weekIndex is 0-based

TABLE: games
  Columns: id, scheduleId, seasonIndex, stageIndex, weekIndex,
           homeTeamId, awayTeamId, homeTeamName, awayTeamName,
           homeScore, awayScore, status, homeUser, awayUser,
           winner_user, loser_user, winner_team, loser_team
  Notes: status IN ('2','3') means completed. homeUser/awayUser are the owner usernames.
         winner_user/loser_user are pre-computed from scores.
         To find games involving a user: WHERE homeUser='X' OR awayUser='X'
         Head-to-head: WHERE (homeUser='A' AND awayUser='B') OR (homeUser='B' AND awayUser='A')
         NOTE: Overtime is NOT trackable — the API stores final scores only (no isOvertime flag).
               Completed games always have a winner; tied final scores don't exist.
  Win percentage pattern (avoids duplicate owners from home/away split):
    SELECT u AS owner,
      SUM(CASE WHEN winner_user=u THEN 1 ELSE 0 END) AS wins,
      COUNT(*) AS total_games
    FROM (
      SELECT homeUser AS u, winner_user FROM games WHERE status IN ('2','3') AND homeUser != ''
      UNION ALL
      SELECT awayUser AS u, winner_user FROM games WHERE status IN ('2','3') AND awayUser != ''
    ) GROUP BY u HAVING COUNT(*) >= 20 ORDER BY CAST(wins AS REAL)/total_games DESC
  Single-team records (e.g. "most points by one team in a game"):
    SELECT teamName, CAST(score AS INTEGER) AS pts, seasonIndex, weekIndex FROM (
      SELECT homeTeamName AS teamName, homeScore AS score, seasonIndex, weekIndex FROM games WHERE status IN ('2','3')
      UNION ALL
      SELECT awayTeamName AS teamName, awayScore AS score, seasonIndex, weekIndex FROM games WHERE status IN ('2','3')
    ) ORDER BY pts DESC LIMIT 1

TABLE: teams
  Columns: teamId, cityName, abbrName, nickName, displayName, logoId,
           primaryColor, secondaryColor, ovrRating, defScheme, offScheme,
           divName, injuryCount, userName, playerCount, capRoomFormatted,
           capSpentFormatted, capAvailableFormatted
  Notes: userName is the current owner. One row per franchise (current season snapshot).

TABLE: standings  (Season 6 / current week only)
  Columns: id, teamId, teamName, teamOvr, calendarYear, seasonIndex, stageIndex, weekIndex,
           divisionName, conferenceName, totalWins, totalLosses, totalTies,
           confWins, confLosses, confTies, divWins, divLosses, divTies,
           homeWins, homeLosses, awayWins, awayLosses,
           offTotalYds, offPassYds, offRushYds, defTotalYds, defPassYds, defRushYds,
           ptsFor, ptsAgainst, netPts, rank, seed, playoffStatus,
           tODiff, winLossStreak, winPct, capRoom, capAvailable, capSpent,
           initialSoS, totalSoS, playedSoS, remainingSoS

TABLE: offensive_stats  (per-game player offensive stats, all seasons)
  Columns: id, fullName, extendedName, seasonIndex, stageIndex, weekIndex, gameId,
           teamId, teamName, rosterId, pos,
           passAtt, passComp, passCompPct, passTDs, passInts, passYds, passSacks,
           passerRating, passYdsPerAtt, passYdsPerGame, passPts,
           rushAtt, rushYds, rushTDs, rushFum, rushLongest, rushBrokenTackles,
           rushYdsAfterContact, rushYdsPerAtt, rushYdsPerGame, rushPts,
           recCatches, recDrops, recCatchPct, recYds, recYdsPerCatch,
           recYdsPerGame, recTDs, recLongest, recYdsAfterCatch, recPts, offPts
  Notes: pos values include QB, HB, FB, WR, TE, OL, etc.
         This table has NO status column — it only contains stats from completed games, so no filtering needed.
         To join with games: JOIN games ON offensive_stats.gameId = games.id

TABLE: defensive_stats  (per-game player defensive stats, all seasons)
  Columns: statId, fullName, extendedName, seasonIndex, stageIndex, weekIndex,
           gameId, teamId, teamName, rosterId, pos,
           defTotalTackles, defSacks, defSafeties, defInts, defIntReturnYds,
           defForcedFum, defFumRec, defTDs, defCatchAllowed, defDeflections, defPts
  Notes: pos values include DT, LE, RE, LOLB, MLB, ROLB, CB, FS, SS
         This table has NO status column — it only contains stats from completed games, so no filtering needed.
         To join with games: JOIN games ON defensive_stats.gameId = games.id

TABLE: team_stats  (per-game team stats, all seasons)
  Columns: statId, seasonIndex, stageIndex, weekIndex, gameId,
           teamId, teamName,
           defForcedFum, defFumRec, defIntsRec, defPassYds, defRushYds,
           defRedZoneFGs, defRedZones, defRedZonePct, defRedZoneTDs, defSacks, defTotalYds,
           off4thDownAtt, off4thDownConv, off4thDownConvPct,
           offFumLost, offIntsLost, off1stDowns,
           offPassTDs, offPassYds, offRushTDs, offRushYds,
           offRedZoneFGs, offRedZones, offRedZoneTDs, offSacks, offTotalYds,
           penalties, penaltyYds,
           off3rdDownAtt, off3rdDownConv, off3rdDownConvPct,
           tODiff, tOGiveAways, tOTakeaways

TABLE: trades  (all trade history)
  Columns: id, team1_id, team1Name, team2_id, team2Name, status,
           seasonIndex, stageIndex, weekIndex, team1Sent, team2Sent
  Notes: status = 'approved' / 'denied' / 'pending'
         team1Sent/team2Sent contain comma-separated asset descriptions with values.

TABLE: players  (current roster snapshot)
  *** CRITICAL: This table does NOT have a fullName column. Use firstName, lastName instead. ***
  *** To display full name: (firstName || ' ' || lastName) AS fullName ***
  Columns: rosterId, firstName, lastName, age, height, weight, pos, jerseyNum,
           college, yearsPro, dev, devTrait, teamId, teamName, isFA, isOnIR,
           playerBestOvr, capHit, contractSalary, contractYearsLeft,
           speedRating, strengthRating, agilityRating, awareRating, catchRating,
           routeRunShortRating, routeRunMedRating, routeRunDeepRating,
           throwPowerRating, throwAccShortRating, throwAccMedRating, throwAccDeepRating,
           carryRating, jukeMoveRating, spinMoveRating, truckRating, breakTackleRating,
           tackleRating, hitPowerRating, pursuitRating, playRecRating, manCoverRating,
           zoneCoverRating, pressRating, blockSheddingRating, runBlockRating,
           passBlockRating, impactBlockRating, kickPowerRating, kickAccuracyRating
  Notes: dev values: 'Normal', 'Star', 'Superstar', 'Superstar X-Factor'
         devTrait column: '0'=Normal, '1'=Star, '2'=Superstar, '3'=Superstar X-Factor
         For X-Factor queries: use dev='Superstar X-Factor' OR devTrait='3'
         isFA='1' means free agent. teamName='Free Agent' for unsigned players.
         WARNING: 800+ players have teamName='Free Agent' but isFA='0' (reserve pool).
                  For active roster queries, ALWAYS exclude: WHERE teamName != 'Free Agent'

TABLE: player_abilities  (X-Factor/Superstar abilities)
  Columns: rosterId, firstName, lastName, teamName, title, description,
           startSeasonIndex, endSeasonIndex
  Notes: An ability with no endSeasonIndex is still active.

TABLE: owner_tenure  ← USE THIS for any "owner-filtered" historical queries
  Columns: teamName, userName, seasonIndex, games_played
  Notes: Ground truth of who owned which team in which season.
         ALWAYS join this table when the question is about a specific owner's
         performance with their team across seasons.
  Example: "How has TheWitt done as the Lions owner?" →
    JOIN owner_tenure ot ON g.homeTeamName=ot.teamName AND g.seasonIndex=ot.seasonIndex
    WHERE ot.userName='TheWitt'

TABLE: player_draft_map  ← USE THIS for any draft history queries
  Columns: rosterId, extendedName, drafting_team, drafting_season,
           draftRound, draftPick, current_team, dev, playerBestOvr, pos,
           rookieYear, was_traded
  Notes: drafting_team = team that ORIGINALLY drafted the player.
         DO NOT use players.teamName for draft queries.
         draftRound: 2=R1, 3=R2, 4=R3, 5=R4, 6=R5, 7=R6, 8=R7
         was_traded=1 means player was later moved from their drafting team.
         dev values: 'Normal', 'Star', 'Superstar', 'Superstar X-Factor' (same as players table)

COMMON JOINS:
- team_stats JOIN games ON team_stats.gameId = games.id
- offensive_stats.gameId links to games.id
- Use games.homeUser/awayUser to get owner context for team_stats rows
- owner_tenure JOIN games ON teamName+seasonIndex to filter to an owner's tenure
- player_draft_map for ANY draft-related query (not players table)

OWNER-FILTERED QUERY PATTERN:
  SELECT ... FROM games g
  JOIN owner_tenure ot ON (g.homeTeamName=ot.teamName OR g.awayTeamName=ot.teamName)
    AND g.seasonIndex=ot.seasonIndex
  WHERE ot.userName='[owner]' AND (g.homeUser='[owner]' OR g.awayUser='[owner]')
"""


def _build_known_users_block() -> str:
    """Return a KNOWN OWNERS section for AI SQL prompts. Empty string if not loaded."""
    _ensure_codex_identity()
    if not KNOWN_USERS:
        return ""
    lines = "\n".join(f"  - {u}" for u in sorted(KNOWN_USERS))
    return (
        "\nKNOWN TSL OWNERS (exact db_username values — use these in WHERE clauses):\n"
        + lines + "\n"
    )


def _get_db_schema():
    """Rebuild schema each call so CURRENT_SEASON is always fresh.
    Appends KNOWN_USERS block so AI always has exact owner username strings.
    """
    return _build_schema() + _build_known_users_block()


# ── Known users — loaded dynamically from tsl_members DB ─────────────────────
# Previously hardcoded lists; now populated on first use from build_member_db.
# KNOWN_USERS = list of db_usernames (exact strings as stored in tsl_history.db)
# NICKNAME_TO_USER = alias → db_username map (nicknames, PSN, discord usernames, etc.)
KNOWN_USERS: list[str] = []
NICKNAME_TO_USER: dict[str, str] = {}
_codex_identity_loaded = False


def _ensure_codex_identity():
    """Lazy-load identity data from tsl_members DB on first use."""
    global KNOWN_USERS, NICKNAME_TO_USER, _codex_identity_loaded
    if _codex_identity_loaded:
        return
    try:
        import build_member_db as member_db
        KNOWN_USERS = member_db.get_known_users()
        NICKNAME_TO_USER = member_db.get_alias_map()  # all aliases → db_username
        _codex_identity_loaded = True
    except Exception as e:
        print(f"[codex_utils] Failed to load identity data: {e}")


def refresh_codex_identity():
    """Reload identity data after sync_tsl_db populates the teams table.

    Call this after startup sync so that members whose db_username was
    auto-filled from teams.userName are included in the alias map.
    """
    global KNOWN_USERS, NICKNAME_TO_USER, _codex_identity_loaded
    try:
        import build_member_db as member_db
        KNOWN_USERS = member_db.get_known_users()
        NICKNAME_TO_USER = member_db.get_alias_map()
        _codex_identity_loaded = True
        print(f"[codex_utils] Identity refreshed: {len(KNOWN_USERS)} users, {len(NICKNAME_TO_USER)} aliases")
    except Exception as e:
        print(f"[codex_utils] Failed to refresh identity data: {e}")


def fuzzy_resolve_user(name: str) -> str | None:
    """Resolve a loose username/nickname to the closest known TSL owner.

    Priority order:
      1. Nickname dict  — no length gate (handles "jt", "kg", "troy", etc.)
      2. Exact case-insensitive match against KNOWN_USERS
      3. Fuzzy match:
           - 3-4 chars: cutoff 0.80 (tight — prevents "the"→TheWitt FPs)
           - 5+ chars:  cutoff 0.65 (standard)
      4. Substring fallback — min 4 chars
    """
    _ensure_codex_identity()
    nl = name.lower()
    # 1. Nickname dict — no length gate; handles short aliases like "jt", "kg", "troy"
    if nl in NICKNAME_TO_USER:
        return NICKNAME_TO_USER[nl]
    # 2. Exact match (case-insensitive)
    for u in KNOWN_USERS:
        if u.lower() == nl:
            return u
    # 3. Fuzzy match — tighter cutoff for short names to block common-word false positives
    if len(name) >= 3:
        cutoff  = 0.80 if len(name) < 5 else 0.65
        matches = get_close_matches(nl, [u.lower() for u in KNOWN_USERS], n=1, cutoff=cutoff)
        if matches:
            return next(u for u in KNOWN_USERS if u.lower() == matches[0])
    # 4. Substring fallback — min 4 chars to avoid "the" → TheWitt
    if len(name) >= 4:
        for u in KNOWN_USERS:
            if nl in u.lower():
                return u
    return None


async def ai_resolve_names(question: str) -> dict[str, str]:
    """AI-powered name resolution fallback.

    When regex-based resolve_names_in_question() can't find any TSL member
    references, this function asks AI to identify which members are mentioned.
    Uses the full KNOWN_USERS list + NICKNAME_TO_USER map as context.

    Returns alias_map: {mentioned_text: db_username} or empty dict.
    """
    _ensure_codex_identity()
    if not KNOWN_USERS:
        return {}

    # Build compact member list for the prompt
    member_lines = []
    # Reverse the alias map to group aliases by db_username
    user_aliases: dict[str, list[str]] = {}
    for alias, db_u in NICKNAME_TO_USER.items():
        user_aliases.setdefault(db_u, []).append(alias)
    for db_u in KNOWN_USERS:
        aliases = user_aliases.get(db_u, [])
        # Filter out the db_username itself and show only unique short aliases
        short_aliases = [a for a in aliases if a.lower() != db_u.lower()][:5]
        if short_aliases:
            member_lines.append(f"  {db_u} (aka: {', '.join(short_aliases)})")
        else:
            member_lines.append(f"  {db_u}")

    prompt = f"""You are a name resolver for The Simulation League (TSL), a Madden NFL sim league.

TSL MEMBERS (db_username and known aliases):
{chr(10).join(member_lines)}

USER QUESTION: "{question}"

TASK: Identify which TSL members are referenced in this question.
- Match nicknames, abbreviations, partial names, team references, or any other identifier
- "my" / "me" / "I" should NOT be resolved here (handled separately)
- If no TSL members are referenced, return empty names list
- Only return members you are confident about (>80% sure)

Return ONLY valid JSON, no explanation:
{{"names": [{{"mentioned": "text from question", "resolved": "exact db_username"}}]}}
"""

    try:
        result = await atlas_ai.generate(
            prompt, tier=Tier.HAIKU, max_tokens=300,
            temperature=0.05, json_mode=True,
        )
        data = json.loads(result.text)
        alias_map = {}
        for entry in data.get("names", []):
            mentioned = entry.get("mentioned", "")
            resolved = entry.get("resolved", "")
            if mentioned and resolved and resolved in KNOWN_USERS:
                alias_map[mentioned] = resolved
        return alias_map
    except Exception:
        log.error("AI alias resolution failed for question")
        return {}


def resolve_names_in_question(question: str) -> tuple[str, dict[str, str]]:
    """
    Scan the question for TSL owner references and annotate with DB usernames.
    Returns (annotated_question, alias_map).
    """
    alias_map: dict[str, str] = {}
    tokens = question.split()
    candidates = tokens + [f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens) - 1)]

    for candidate in candidates:
        clean = candidate.strip("?!.,':\"\t").strip()
        if len(clean) < 2:
            continue
        resolved = fuzzy_resolve_user(clean)
        if resolved and clean.lower() != resolved.lower():
            alias_map[clean] = resolved

    if not alias_map:
        return question, {}

    annotated = question
    for nickname, username in alias_map.items():
        annotated = annotated.replace(nickname, f"{nickname} (username: '{username}')")

    return annotated, alias_map


# ── Live data snapshot ────────────────────────────────────────────────────────

def _build_tsl_snapshot() -> str:
    """Build a short text block of live TSL data from data_manager.

    Includes current season/week/stage, this week's schedule with scores,
    and top standings. Grounds the AI in live data before SQL generation so
    schedule and basic standings questions can be answered without a DB query.
    """
    if dm.df_games.empty:
        return ""

    stage_label = "Playoffs" if dm.CURRENT_STAGE > 1 else "Regular Season"
    lines: list[str] = [
        f"[TSL Live Data — Season {dm.CURRENT_SEASON}, {dm.week_label(dm.CURRENT_WEEK)} ({stage_label})]",
        "This week's matchups:",
    ]

    # Score map for completed games
    score_map: dict[str, str] = {}
    try:
        for r in dm.get_weekly_results():
            key = f"{r['away']} @ {r['home']}"
            score_map[key] = (
                f"{r['away']} {r['away_score']} — {r['home']} {r['home_score']} (Final)"
            )
    except Exception:
        pass

    seen: set[str] = set()
    for _, row in dm.df_games.iterrows():
        key = str(row.get("matchup_key", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        score_txt = score_map.get(key)
        lines.append(f"  {score_txt if score_txt else key + ' (Pending)'}")

    # Top standings
    if not dm.df_standings.empty and "teamName" in dm.df_standings.columns:
        lines.append("Current standings (top 8 by wins):")
        try:
            df = dm.df_standings.copy()
            for col in ("totalWins", "totalLosses"):
                if col in df.columns:
                    df[col] = df[col].astype(str).str.extract(r"(\d+)").fillna(0).astype(int)
            if "totalWins" in df.columns:
                df = df.sort_values("totalWins", ascending=False)
            for _, srow in df.head(8).iterrows():
                name = srow.get("teamName", "?")
                w = int(srow.get("totalWins", 0))
                l = int(srow.get("totalLosses", 0))
                t = int(srow.get("totalTies", 0))
                record = f"{w}-{l}" + (f"-{t}" if t else "")
                lines.append(f"  {name}: {record}")
        except Exception:
            pass

    return "\n".join(lines)


# ── Unified NL→SQL→answer pipeline (for @mention and Oracle Ask modals) ───────

_MENTION_SQL_PROMPT = """\
You are a SQLite expert for The Simulation League (TSL) Madden franchise database.
Your job: convert natural-language questions into a single correct SQLite SELECT query.

{schema}
{alias_block}{conv_block}
RULES:
1. Return ONLY the raw SQL query — no markdown, no explanation, no code fences.
2. ALL columns are stored as TEXT. Use CAST(col AS INTEGER) or CAST(col AS REAL) for math/comparisons.
3. Completed games: ALWAYS filter status IN ('2','3'). Never include unplayed games.
4. Default to stageIndex='1' (regular season) unless the user asks about playoffs.
5. For owner queries: use EXACT usernames from the KNOWN TSL OWNERS list. Never use LIKE or wildcards.
6. For "record" questions: count wins AND losses separately, not just total games.
7. When a user could appear as home OR away, handle both: (homeUser='X' OR awayUser='X').
8. For owner-specific history across seasons, ALWAYS JOIN owner_tenure to track team changes.
9. For draft queries, use player_draft_map — NEVER players.teamName.
10. Limit results to 30 rows unless the user needs more.
11. Never use DROP, INSERT, UPDATE, DELETE, or any DDL.

Now generate a query for this question:
"{question}"
"""

_MENTION_ANSWER_PROMPT = """\
{persona}
{conv_block}
A TSL member asked: "{question}"

Query results ({n_rows} rows):
{results_str}
{no_data_instruction}
RESPONSE GUIDELINES:
- Lead with the DIRECT answer — the specific stat, name, or fact.
- Use **bold** for key numbers and names (Discord markdown).
- Include supporting context: season, team, comparison when relevant.
- Keep it under 300 words unless a full breakdown was requested.
- NEVER repeat the SQL query or mention databases/tables.
- NEVER invent stats not in the results above.
- ALWAYS use third person. Refer to players/owners by name.
- Use sports language and dramatic flair.
"""


async def tsl_ask_async(
    question: str,
    conv_context: str = "",
) -> tuple[str | None, str | None]:
    """Full NL→SQL→answer pipeline for @mention and Oracle Ask modals.

    Reuses the existing retry_sql() cascade and codex_utils infrastructure
    without touching the /codex slash command flow in codex_cog.py.

    Returns:
        (answer_str, sql_used) on success
        (None, None) if the question cannot be answered from the TSL DB
    """
    _ensure_codex_identity()

    # 1. Name resolution
    annotated_q, alias_map = resolve_names_in_question(question)

    _san = lambda s: re.sub(r"['\";\\]", "", s)
    alias_block = ""
    if alias_map:
        alias_lines = [
            f"  '{_san(nick)}' → use username '{_san(user)}' in SQL"
            for nick, user in alias_map.items()
        ]
        alias_block = (
            "\nRESOLVED NAME ALIASES (use these exact values in WHERE clauses):\n"
            + "\n".join(alias_lines) + "\n"
        )

    conv_block = f"\n{conv_context}\n" if conv_context else ""

    # 2. Schema + live snapshot
    schema = _get_db_schema()
    snapshot = _build_tsl_snapshot()
    full_schema = f"{schema}\n\n{snapshot}" if snapshot else schema

    # 3. Generate SQL
    sql_prompt = _MENTION_SQL_PROMPT.format(
        schema=full_schema,
        alias_block=alias_block,
        conv_block=conv_block,
        question=annotated_q,
    )
    try:
        sql_result = await atlas_ai.generate(sql_prompt, tier=Tier.SONNET, temperature=0.05)
        sql = extract_sql(sql_result.text)
    except Exception as e:
        log.warning(f"[tsl_ask] SQL generation failed: {e}")
        return None, None

    if not sql:
        return None, None

    # 4. Execute with self-correcting retry cascade
    try:
        rows, final_sql, error, _attempt, _warnings = await retry_sql(sql, schema)
    except Exception as e:
        log.warning(f"[tsl_ask] SQL execution failed: {e}")
        return None, None

    if error or not rows:
        return None, final_sql

    # 5. Format answer in ATLAS voice
    try:
        from echo_loader import get_persona as _gp
    except ImportError:
        _gp = lambda mode="analytical": "You are ATLAS. Speak in third person. Be concise and direct."

    results_str = json.dumps(rows[:50], indent=2)
    if len(results_str) > 6000:
        results_str = results_str[:6000] + "\n... (truncated)"

    answer_prompt = _MENTION_ANSWER_PROMPT.format(
        persona=_gp("analytical"),
        conv_block=conv_block,
        question=question,
        n_rows=len(rows),
        results_str=results_str,
        no_data_instruction="",
    )
    try:
        answer_result = await atlas_ai.generate(answer_prompt, tier=Tier.SONNET)
        return answer_result.text, final_sql
    except Exception as e:
        log.warning(f"[tsl_ask] Answer formatting failed: {e}")
        return None, final_sql
