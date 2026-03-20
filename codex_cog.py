"""
codex_cog.py v1.4  ─  ATLAS Historical Intelligence (Codex Module)
Uses Gemini to convert natural language questions → SQL → natural language answers
against the TSL SQLite database.

Commands:
  /ask          <question>        Ask ATLAS anything about TSL history
  /ask_debug    <question>        [Admin] Show generated SQL + raw rows — diagnostics
  /h2h          <user1> <user2>  Head-to-head record
  /season_recap <season>         Full season recap

v1.3 fixes:
  - FIX: Docstring corrected — file is codex_cog.py (was still saying history_cog.py).
  - FIX: Gemini client uses os.getenv (was os.environ — crashed on missing key).
  - FIX: /season_recap uses dm.CURRENT_SEASON instead of hardcoded 6.

v1.4 fixes:
  - ADD:  /ask_debug admin command — referenced in /ask error messages since v1.3
          but never implemented in this file. Migrated from history_cog.py.
  - FIX:  Class renamed HistoryCog → CodexCog. setup() was instantiating
          HistoryCog(bot) which still worked only because the name matched the
          class in this file — confusing and fragile.
  - FIX:  ATLAS_PERSONA replaced with get_persona("analytical") from echo_loader
          for all three Gemini answer calls. Falls back to inline stub if echo_loader
          unavailable. DB_SCHEMA SQL-generation prompt intentionally unchanged.
  - FIX:  All three embed footers corrected "Oracle Module" → "Codex Module".
  - FIX:  /h2h footer season range now dynamic via dm.CURRENT_SEASON.
  - FIX:  DB_SCHEMA season comment now dynamic so Gemini always sees correct
          current season number instead of hardcoded '6'.
"""

import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import json
import os
import re
from collections import Counter
import atlas_ai
from atlas_ai import Tier
from difflib import get_close_matches

import data_manager as dm
from atlas_colors import AtlasColors

# Optional modules
try:
    import affinity as _affinity_mod
except ImportError:
    _affinity_mod = None

try:
    from build_member_db import get_db_username_for_discord_id as _get_db_username
    from build_member_db import resolve_db_username as _resolve_db_username
except ImportError:
    _get_db_username = None
    _resolve_db_username = None

try:
    from codex_intents import detect_intent, check_self_reference_collision, get_h2h_sql_and_params
except ImportError:
    detect_intent = None
    check_self_reference_collision = None
    get_h2h_sql_and_params = None

# ── Config ──────────────────────────────────────────────────────────────────
DB_PATH   = os.path.join(os.path.dirname(__file__), "tsl_history.db")
MAX_ROWS  = 50
MAX_CHARS = 3000
_EMBED_DESC_LIMIT = 4096


def _truncate_for_embed(text: str, limit: int = _EMBED_DESC_LIMIT) -> str:
    """Truncate text to fit within Discord embed description limit."""
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."

# ── Echo persona loader — analytical register for answer generation ───────────
try:
    from echo_loader import get_persona as _get_persona
except ImportError:
    _get_persona = lambda _mode="casual": "You are ATLAS."

def _answer_persona() -> str:
    return _get_persona("analytical")

# ── Conversation Memory (shared module) ──────────────────────────────────────
from conversation_memory import add_conversation_turn, build_conversation_block


# ── Schema fed to Gemini for SQL generation ──────────────────────────────────
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

# Called at prompt-build time so the season number is always current
def _get_db_schema():
    """Rebuild schema each call so CURRENT_SEASON is always fresh."""
    return _build_schema()

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
        print(f"[codex_cog] Failed to load identity data: {e}")


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
        print(f"[codex_cog] Identity refreshed: {len(KNOWN_USERS)} users, {len(NICKNAME_TO_USER)} aliases")
    except Exception as e:
        print(f"[codex_cog] Failed to refresh identity data: {e}")


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
        import json as _json
        data = _json.loads(result.text)
        alias_map = {}
        for entry in data.get("names", []):
            mentioned = entry.get("mentioned", "")
            resolved = entry.get("resolved", "")
            if mentioned and resolved and resolved in KNOWN_USERS:
                alias_map[mentioned] = resolved
        return alias_map
    except Exception:
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


# ── Core DB + Gemini pipeline ────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def run_sql(sql: str, params: tuple = ()) -> tuple[list[dict], str | None]:
    """Execute SQL, return (rows, error).  Supports parameterized queries."""
    try:
        conn = get_db()
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows[:MAX_ROWS], None
    except Exception as e:
        return [], str(e)


def extract_sql(text: str) -> str | None:
    """Pull SQL out of Gemini's response."""
    match = re.search(r"```(?:sql)?\s*(SELECT.+?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"(SELECT\s.+?);?\s*$", text, re.DOTALL | re.IGNORECASE)
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
    # Look for ORDER BY or comparison operators on known numeric columns
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
    rows, error_1 = run_sql(sql, params)
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
    fix_result_2 = await atlas_ai.generate(
        fix_prompt_2, tier=Tier.HAIKU, max_tokens=500, temperature=0.02
    )
    sql_2 = extract_sql(fix_result_2.text)
    if not sql_2:
        print(f"[retry_sql] Attempt 2: extract_sql returned None, reusing previous SQL")
        sql_2 = sql_1
    rows, error_2 = run_sql(sql_2)
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
        print(f"[retry_sql] Attempt 3: extract_sql returned None, reusing previous SQL")
        sql_3 = sql_2
    rows, error_3 = run_sql(sql_3)
    if not error_3:
        return rows, sql_3, None, 3, warnings

    return [], sql_3, error_3, 3, warnings


async def gemini_sql(
    question: str, alias_map: dict | None = None,
    conv_context: str = "",
) -> str | None:
    """Ask AI to generate SQL. Non-blocking via run_in_executor."""
    _sanitize = lambda s: re.sub(r"['\";\\]", "", s)
    alias_block = ""
    if alias_map:
        lines = [f"  '{_sanitize(nick)}' → use username '{_sanitize(user)}' in SQL" for nick, user in alias_map.items()]
        alias_block = "\nRESOLVED NAME ALIASES (use these exact values in WHERE clauses):\n" + "\n".join(lines) + "\n"

    safe_users = [_sanitize(u) for u in KNOWN_USERS]
    known_users_block = (
        "\nVALID homeUser/awayUser VALUES (exact strings stored in the database):\n"
        + ", ".join(f"'{u}'" for u in safe_users)
        + "\nOnly use these exact strings in WHERE clauses involving homeUser or awayUser.\n"
    )

    conv_block = f"\n{conv_context}\n" if conv_context else ""

    prompt = f"""You are a SQLite expert for The Simulation League (TSL) Madden franchise database.
Your job: convert natural-language questions into a single correct SQLite SELECT query.

{_get_db_schema()}
{known_users_block}{alias_block}{conv_block}

RULES:
1. Return ONLY the raw SQL query — no markdown, no explanation, no code fences.
2. ALL columns are stored as TEXT. Use CAST(col AS INTEGER) or CAST(col AS REAL) for math/comparisons.
3. Completed games: ALWAYS filter status IN ('2','3'). Never include unplayed games.
4. Default to stageIndex='1' (regular season) unless the user asks about playoffs.
5. For owner queries: use EXACT usernames from the VALID VALUES list above. Never use LIKE or wildcards.
6. For "record" questions: count wins AND losses separately, not just total games.
7. When a user could appear as home OR away, handle both: (homeUser='X' OR awayUser='X').
8. For owner-specific history across seasons, ALWAYS JOIN owner_tenure to track team changes.
9. For draft queries, use player_draft_map — NEVER players.teamName.
10. Limit results to 30 rows unless the user needs more.
11. Never use DROP, INSERT, UPDATE, DELETE, or any DDL.

COMMON MISTAKES TO AVOID:
- Forgetting status IN ('2','3') → includes unplayed/scheduled games → wrong counts.
- Using players.teamName for draft history → wrong (players move teams). Use player_draft_map.
- Counting only homeUser wins → misses away wins. Always handle both home and away.
- Hardcoding seasonIndex → misses multi-season data. Default to all seasons unless asked.
- Using GROUP BY without handling the home/away split → double-counting.
- Forgetting CAST() on TEXT columns → string comparison instead of numeric → wrong ordering.

FEW-SHOT EXAMPLES:
Q: "Who has the most wins all time?"
SQL: SELECT owner, SUM(wins) AS total_wins FROM (SELECT winner_user AS owner, COUNT(*) AS wins FROM games WHERE status IN ('2','3') AND winner_user != '' GROUP BY winner_user) GROUP BY owner ORDER BY total_wins DESC LIMIT 10

Q: "What is TheWitt's record this season?"
SQL: SELECT SUM(CASE WHEN winner_user='TheWitt' THEN 1 ELSE 0 END) AS wins, SUM(CASE WHEN loser_user='TheWitt' THEN 1 ELSE 0 END) AS losses FROM games WHERE status IN ('2','3') AND seasonIndex='{dm.CURRENT_SEASON}' AND (homeUser='TheWitt' OR awayUser='TheWitt')

Q: "Who leads the league in passing yards this season?"
SQL: SELECT fullName, teamName, SUM(CAST(passYds AS INTEGER)) AS total_pass_yds FROM offensive_stats WHERE seasonIndex='{dm.CURRENT_SEASON}' AND stageIndex='1' AND pos='QB' GROUP BY fullName ORDER BY total_pass_yds DESC LIMIT 10

Q: "Head to head record between TheWitt and KillaE94?"
SQL: SELECT winner_user, COUNT(*) AS wins FROM games WHERE status IN ('2','3') AND ((homeUser='TheWitt' AND awayUser='KillaE94') OR (homeUser='KillaE94' AND awayUser='TheWitt')) GROUP BY winner_user

Q: "Best defensive players on the Ravens?"
SQL: SELECT fullName, pos, SUM(CAST(defTotalTackles AS INTEGER)) AS tackles, SUM(CAST(defSacks AS REAL)) AS sacks, SUM(CAST(defInts AS INTEGER)) AS ints FROM defensive_stats WHERE teamName LIKE '%Ravens%' AND seasonIndex='{dm.CURRENT_SEASON}' AND stageIndex='1' GROUP BY fullName ORDER BY tackles DESC LIMIT 15

Q: "Which team's draft picks have the highest average OVR?"
SQL: SELECT drafting_team, COUNT(*) AS picks, ROUND(AVG(CAST(playerBestOvr AS REAL)),1) AS avg_ovr FROM player_draft_map GROUP BY drafting_team ORDER BY avg_ovr DESC LIMIT 10

Q: "Who has the best record as an away team across all seasons?"
SQL: SELECT awayUser AS owner, SUM(CASE WHEN winner_user=awayUser THEN 1 ELSE 0 END) AS away_wins, COUNT(*) AS away_games, ROUND(CAST(SUM(CASE WHEN winner_user=awayUser THEN 1 ELSE 0 END) AS REAL)/COUNT(*)*100,1) AS win_pct FROM games WHERE status IN ('2','3') AND stageIndex='1' AND awayUser != '' GROUP BY awayUser HAVING COUNT(*) >= 10 ORDER BY win_pct DESC LIMIT 10

Now generate a query for this question:
"{question}"
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, temperature=0.05)
    return extract_sql(result.text)


async def gemini_answer(
    question: str, sql: str, rows: list[dict],
    conv_context: str = "",
) -> str:
    """Format SQL results into natural language via AI. Non-blocking via run_in_executor."""
    results_str = json.dumps(rows, indent=2)
    if len(results_str) > MAX_CHARS:
        results_str = results_str[:MAX_CHARS] + "\n... (truncated)"

    no_data_instruction = ""
    if not rows:
        no_data_instruction = (
            "\nCRITICAL: The query returned NO rows. State clearly that no data was found "
            "for this question. Do NOT invent stats or outcomes.\n"
        )

    conv_block = f"\n{conv_context}\n" if conv_context else ""

    prompt = f"""{_answer_persona()}
{conv_block}
A TSL member asked: "{question}"

Query results ({len(rows)} rows):
{results_str}
{no_data_instruction}
RESPONSE GUIDELINES:
- Lead with the DIRECT answer — the specific stat, name, or fact the user asked about.
- Use **bold** for key numbers and names (Discord markdown).
- Include supporting context: season, team, comparison to others when relevant.
- If multiple rows returned, highlight the top 3-5 and briefly summarize the rest.
- Keep it under 300 words unless the user asked for a full breakdown.
- If data seems incomplete or unexpected, acknowledge it but still give the best answer.
- NEVER repeat the SQL query or mention databases/tables — just deliver the answer.
- NEVER invent stats or outcomes that are not in the results above.
- ALWAYS use third person — refer to players/owners by name, never "I", "me", "my", "we".
- Use sports language and dramatic flair — make numbers tell a story.
"""

    result = await atlas_ai.generate(prompt, tier=Tier.HAIKU)
    return result.text.strip()


# ── Cog ──────────────────────────────────────────────────────────────────────

class CodexCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot  = bot

    # ── /ask ─────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="ask",
        description="Ask ATLAS anything about TSL history — stats, records, rivalries, trades"
    )
    @app_commands.describe(question="Your question about TSL history")
    @app_commands.checks.cooldown(3, 30)  # 3 uses per 30 seconds per user
    async def ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer(thinking=True)

        try:
            # ── 1. Dynamic identity resolution ────────────────────
            caller_db = None
            if _resolve_db_username:
                caller_db = _resolve_db_username(interaction.user.id)
            if not caller_db and _get_db_username:
                caller_db = _get_db_username(interaction.user.id)
            if not caller_db:
                caller_db = fuzzy_resolve_user(interaction.user.name) or interaction.user.name

            # ── 2. Resolve names in question ──────────────────────
            caller_context = (
                f"[Context: the person asking is TSL owner with db_username='{caller_db}'. "
                f"When the question uses 'me', 'my', or 'I', use '{caller_db}' in SQL WHERE clauses.]"
            )
            question_with_context = f"{caller_context} {question}"
            annotated_question, alias_map = resolve_names_in_question(question_with_context)

            # ── 2b. AI name resolution fallback ────────────────────
            # If regex didn't find any names, try AI resolution
            if not alias_map:
                ai_aliases = await ai_resolve_names(question)
                if ai_aliases:
                    alias_map = ai_aliases
                    for nickname, username in alias_map.items():
                        annotated_question = annotated_question.replace(
                            nickname, f"{nickname} (username: '{username}')"
                        )

            # ── 3. Self-reference collision check ─────────────────
            if check_self_reference_collision and alias_map:
                collision_msg = check_self_reference_collision(caller_db, alias_map)
                if collision_msg:
                    await interaction.followup.send(f"⚠️ {collision_msg}")
                    return

            conv_block = await build_conversation_block(interaction.user.id, source="codex")

            # Affinity tone (answer only, not SQL)
            affinity_block = ""
            if _affinity_mod:
                try:
                    score = await _affinity_mod.get_affinity(interaction.user.id)
                    affinity_block = _affinity_mod.get_affinity_instruction(score)
                except Exception:
                    pass

            # ── 4. Three-tier intent detection ────────────────────
            intent_result = None
            if detect_intent:
                intent_result = await detect_intent(
                    question, caller_db, alias_map
                )

            # ── 5. Tier 1/2: Deterministic SQL ────────────────────
            if intent_result and intent_result.tier < 3 and intent_result.sql:
                rows, error = run_sql(intent_result.sql, intent_result.params)
                if not error:
                    answer_context = "\n".join(filter(None, [conv_block, affinity_block]))
                    answer = await gemini_answer(
                        question, intent_result.sql, rows,
                        conv_context=answer_context,
                    )
                    await add_conversation_turn(interaction.user.id, question, answer, sql=intent_result.sql, source="codex")

                    embed = discord.Embed(
                        title="📊 TSL Historical Intelligence",
                        description=_truncate_for_embed(answer),
                        color=AtlasColors.TSL_GOLD
                    )
                    embed.set_author(
                        name="ATLAS · Autonomous TSL League Administration System",
                        icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
                    )
                    footer_parts = [f"🔍 {len(rows)} records analyzed"]
                    tier_label = "Tier 1 (regex)" if intent_result.tier == 1 else "Tier 2 (classified)"
                    footer_parts.append(f"⚡ {intent_result.intent} via {tier_label}")
                    if alias_map:
                        resolved_str = ", ".join(f"{k}→{v}" for k, v in alias_map.items())
                        footer_parts.append(f"🔎 Resolved: {resolved_str}")
                    if conv_block:
                        footer_parts.append("💬 Conversational")
                    embed.set_footer(
                        text=" | ".join(footer_parts) + " · ATLAS™ Codex Module",
                        icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
                    )
                    await interaction.followup.send(embed=embed)
                    return

            # ── 6. Tier 3: Existing NL→SQL pipeline (unchanged) ───
            sql = await gemini_sql(
                annotated_question, alias_map,
                conv_context=conv_block,
            )
            if not sql:
                await interaction.followup.send(
                    "📊 Couldn't generate a query for that one. Try rephrasing — "
                    "be specific about player names, seasons, or owners."
                )
                return

            schema = _get_db_schema()
            rows, sql, error, attempt, warnings = await retry_sql(sql, schema)
            if error:
                await interaction.followup.send(
                    "⚠️ ATLAS couldn't find an answer for that query. Try rephrasing:\n"
                    "• Use full player names ('Patrick Mahomes' not 'Mahomes')\n"
                    "• Specify the season ('in season 95' not 'this year')\n"
                    "• Ask about one thing at a time\n"
                    "• Use `/ask_debug` for technical details"
                )
                return

            answer_context = "\n".join(filter(None, [conv_block, affinity_block]))
            answer = await gemini_answer(
                question, sql, rows,
                conv_context=answer_context,
            )

            # ── Store conversation turn ─────────────────────────
            await add_conversation_turn(interaction.user.id, question, answer, sql=sql or "", source="codex")

            embed = discord.Embed(
                title="📊 TSL Historical Intelligence",
                description=_truncate_for_embed(answer),
                color=AtlasColors.TSL_GOLD  # ATLAS Gold
            )
            embed.set_author(
                name="ATLAS · Autonomous TSL League Administration System",
                icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
            )
            footer_parts = [f"🔍 {len(rows)} records analyzed"]
            footer_parts.append("🧠 Tier 3 (NL→SQL)")
            if attempt > 1:
                footer_parts.append("⚠️ Self-corrected" if attempt == 2 else "🧠 Opus rescue")
            if alias_map:
                resolved_str = ", ".join(f"{k}→{v}" for k, v in alias_map.items())
                footer_parts.append(f"🔎 Resolved: {resolved_str}")
            if conv_block:
                footer_parts.append("💬 Conversational")
            embed.set_footer(
                text=" | ".join(footer_parts) + " · 💡 Try /oracle for more modes · ATLAS™ Codex Module",
                icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Something broke: `{e}`")

    @ask.error
    async def ask_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Slow down — try again in {error.retry_after:.0f}s", ephemeral=True
            )

    # ── /ask_debug (admin only) ───────────────────────────────────────────────

    async def _ask_debug_impl(self, interaction: discord.Interaction, question: str):
        """Core ask_debug logic — shared by /commish askdebug and deprecated /ask_debug."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            try:
                from build_member_db import get_db_username_for_discord_id
                caller_db = get_db_username_for_discord_id(interaction.user.id)
            except Exception:
                caller_db = None
            if not caller_db:
                caller_db = fuzzy_resolve_user(interaction.user.name) or interaction.user.name

            caller_context = (
                f"[Context: the person asking is TSL owner with db_username='{caller_db}'. "
                f"When the question uses 'me', 'my', or 'I', use '{caller_db}' in SQL WHERE clauses.]"
            )
            annotated_question, alias_map = resolve_names_in_question(f"{caller_context} {question}")
            sql = await gemini_sql(annotated_question, alias_map)
            if not sql:
                await interaction.followup.send("❌ No SQL generated.")
                return

            rows, error = run_sql(sql)

            embed = discord.Embed(title="🔧 ATLAS Codex — Debug", color=AtlasColors.TSL_GOLD)
            embed.set_author(
                name="ATLAS · Autonomous TSL League Administration System",
                icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
            )
            embed.add_field(name="Question", value=question, inline=False)
            sql_display = sql if len(sql) < 1000 else sql[:997] + "..."
            embed.add_field(name="Generated SQL", value=f"```sql\n{sql_display}\n```", inline=False)
            if error:
                embed.add_field(name="❌ SQL Error", value=f"```{error}```", inline=False)
            else:
                embed.add_field(name="Rows Returned", value=str(len(rows)), inline=True)
                if rows:
                    preview = json.dumps(rows[:3], indent=2)
                    if len(preview) > 900:
                        preview = preview[:897] + "..."
                    embed.add_field(name="First 3 Rows", value=f"```json\n{preview}\n```", inline=False)
            if alias_map:
                embed.add_field(
                    name="Name Resolution",
                    value="\n".join(f"{k} → {v}" for k, v in alias_map.items()),
                    inline=False
                )
            embed.set_footer(text="ATLAS™ Codex Module · Debug · Admin only")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"Debug error: `{e}`")

    # ── H2H and Season Recap _impl methods (called by oracle HubView buttons) ──

    async def _h2h_impl(self, interaction: discord.Interaction, owner1: str, owner2: str):
        """Head-to-head record — used by oracle HubView H2H modal."""
        await interaction.response.defer(thinking=True)

        u1 = fuzzy_resolve_user(owner1)
        u2 = fuzzy_resolve_user(owner2)

        if not u1:
            await interaction.followup.send(f"Couldn't find an owner matching `{owner1}`.")
            return
        if not u2:
            await interaction.followup.send(f"Couldn't find an owner matching `{owner2}`.")
            return

        if get_h2h_sql_and_params:
            sql, params = get_h2h_sql_and_params(u1, u2)
        else:
            sql = """
            SELECT
                seasonIndex,
                SUM(CASE WHEN winner_user = ? THEN 1 ELSE 0 END) AS u1_wins,
                SUM(CASE WHEN winner_user = ? THEN 1 ELSE 0 END) AS u2_wins,
                COUNT(*) AS games_played,
                GROUP_CONCAT(
                    'S' || seasonIndex || ' W' || (CAST(weekIndex AS INTEGER)+1) ||
                    ': ' || homeTeamName || ' ' || homeScore || '-' || awayScore || ' ' || awayTeamName
                ) AS game_log
            FROM games
            WHERE status IN ('2','3')
              AND stageIndex = '1'
              AND ((homeUser = ? AND awayUser = ?)
                OR (homeUser = ? AND awayUser = ?))
            GROUP BY seasonIndex
            ORDER BY CAST(seasonIndex AS INTEGER)
            """
            params = (u1, u2, u1, u2, u2, u1)

        rows, error = run_sql(sql, params)
        if error or not rows:
            await interaction.followup.send(
                f"No completed regular season games found between **{u1}** and **{u2}**."
            )
            return

        total_u1    = sum(int(r['u1_wins'] or 0) for r in rows)
        total_u2    = sum(int(r['u2_wins'] or 0) for r in rows)
        total_games = sum(int(r['games_played'] or 0) for r in rows)

        summary_prompt = f"""{_answer_persona()}

Head-to-head data for {u1} vs {u2} in TSL (regular season only):
- {u1} all-time wins: {total_u1}
- {u2} all-time wins: {total_u2}
- Total games played: {total_games}
- Season breakdown: {json.dumps([dict(r) for r in rows], indent=2)}

Write a punchy 3-4 sentence rivalry summary. Call out the dominant party if clear,
note any sweep seasons, and make it entertaining.
"""
        result = await atlas_ai.generate(summary_prompt, tier=Tier.HAIKU)
        summary = result.text.strip()

        embed = discord.Embed(title=f"Rivalry Report: {u1} vs {u2}", color=AtlasColors.TSL_GOLD)
        embed.set_author(
            name="ATLAS · Autonomous TSL League Administration System",
            icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
        )
        embed.add_field(
            name="All-Time Record (Regular Season)",
            value=f"**{u1}**: {total_u1}W  |  **{u2}**: {total_u2}W  |  {total_games} games",
            inline=False
        )
        breakdown = ""
        for r in rows:
            w1 = int(r['u1_wins'] or 0)
            w2 = int(r['u2_wins'] or 0)
            marker = "W" if w1 > w2 else ("L" if w2 > w1 else "T")
            breakdown += f"Season {r['seasonIndex']}: {u1} {w1}-{w2} {u2} {marker}\n"
        embed.add_field(name="Season-by-Season", value=breakdown or "No data", inline=False)
        embed.add_field(name="ATLAS Says", value=summary, inline=False)
        embed.set_footer(
            text=f"ATLAS Codex Module · Regular season only · All seasons 1-{dm.CURRENT_SEASON}",
            icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
        )
        await interaction.followup.send(embed=embed)

    async def _season_recap_impl(self, interaction: discord.Interaction, season: int):
        """Season recap — used by oracle HubView Season Recap modal."""
        await interaction.response.defer(thinking=True)

        if season < 1 or season > dm.CURRENT_SEASON:
            await interaction.followup.send(f"Valid seasons are 1 through {dm.CURRENT_SEASON}.")
            return

        sql = """
        SELECT winner_user, loser_user, winner_team, loser_team,
               homeScore, awayScore, weekIndex
        FROM games
        WHERE seasonIndex=? AND stageIndex='1' AND status IN ('2','3')
        ORDER BY CAST(weekIndex AS INTEGER)
        """
        rows, _ = run_sql(sql, (str(season),))

        wins   = Counter()
        losses = Counter()
        for r in rows:
            if r['winner_user']: wins[r['winner_user']]   += 1
            if r['loser_user']:  losses[r['loser_user']]  += 1

        leaderboard = sorted(wins.keys(), key=lambda u: wins[u], reverse=True)[:5]
        top_str = "\n".join([f"{u}: {wins[u]}W-{losses[u]}L" for u in leaderboard])

        prompt = f"""{_answer_persona()}

Season {season} TSL regular season data:
- Total games played: {len(rows)}
- Top 5 records:
{top_str}
- All game results: {json.dumps(rows[:40], indent=2)}

Write a vivid Season {season} recap. Highlight who dominated, any upsets or notable
storylines from the records, and tease the playoff picture.
Keep it under 350 words.
"""
        result = await atlas_ai.generate(prompt, tier=Tier.HAIKU)

        embed = discord.Embed(
            title=f"TSL Season {season} Recap",
            description=_truncate_for_embed(result.text.strip()),
            color=AtlasColors.TSL_GOLD
        )
        embed.set_author(
            name="ATLAS · Autonomous TSL League Administration System",
            icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
        )
        embed.set_footer(
            text=f"ATLAS Codex Module · Season {season} · {len(rows)} regular season games",
            icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    # DB health check on load — warns loudly if tsl_history.db is missing or empty
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        conn.close()
        print(f"[CodexCog] tsl_history.db OK — {count} games in DB ✅")
    except Exception as e:
        print(f"[CodexCog] ⚠️  WARNING: tsl_history.db check failed: {e}")
        print(f"[CodexCog] ⚠️  Run build_tsl_db.py to populate the database!")
    await bot.add_cog(CodexCog(bot))
    print("ATLAS: Codex · Historical Intelligence loaded. 📜")
