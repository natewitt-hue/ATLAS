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

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import json
import os
import re
from collections import Counter
from google import genai
from difflib import get_close_matches

import data_manager as dm

# ── Config ──────────────────────────────────────────────────────────────────
DB_PATH   = os.path.join(os.path.dirname(__file__), "tsl_history.db")
MAX_ROWS  = 50
MAX_CHARS = 3000

ATLAS_PERSONA = """
You are ATLAS — the official AI intelligence system for The Simulation League (TSL).
You speak with authority, confidence, and sharp wit. You know TSL inside and out.
Use football slang, stat callouts, and dramatic flair. Never be boring.
Keep responses under 400 words unless asked for full breakdowns.
"""

# ── Echo persona loader — analytical register for answer generation ───────────
# Defined after ATLAS_PERSONA so the fallback can reference it safely.
try:
    from echo_loader import get_persona as _get_persona
    def _answer_persona() -> str:
        return _get_persona("analytical")
except ImportError:
    def _answer_persona() -> str:
        return ATLAS_PERSONA

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

TABLE: defensive_stats  (per-game player defensive stats, all seasons)
  Columns: statId, fullName, extendedName, seasonIndex, stageIndex, weekIndex,
           gameId, teamId, teamName, rosterId, pos,
           defTotalTackles, defSacks, defSafeties, defInts, defIntReturnYds,
           defForcedFum, defFumRec, defTDs, defCatchAllowed, defDeflections, defPts
  Notes: pos values include DT, LE, RE, LOLB, MLB, ROLB, CB, FS, SS

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
  Columns: rosterId, firstName, lastName, age, height, weight, pos, jerseyNum,
           college, yearsPro, dev, teamId, teamName, isFA, isOnIR,
           playerBestOvr, capHit, contractSalary, contractYearsLeft,
           speedRating, strengthRating, agilityRating, awareRating, catchRating,
           routeRunShortRating, routeRunMedRating, routeRunDeepRating,
           throwPowerRating, throwAccShortRating, throwAccMedRating, throwAccDeepRating,
           carryRating, jukeMoveRating, spinMoveRating, truckRating, breakTackleRating,
           tackleRating, hitPowerRating, pursuitRating, playRecRating, manCoverRating,
           zoneCoverRating, pressRating, blockSheddingRating, runBlockRating,
           passBlockRating, impactBlockRating, kickPowerRating, kickAccuracyRating
  Notes: dev values: 'Normal', 'Star', 'Superstar', 'XFactor'
         isFA='1' means free agent. teamName='Free Agent' for unsigned players.

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
DB_SCHEMA = _build_schema()

# ── Known users — EXACT strings as stored in games.csv/tsl_history.db ────────
# These are the historical usernames. Do NOT change to current Discord display names.
KNOWN_USERS = [
    'AgrarianPeasant','B3AST_M0DE_NC','BDiddy86','BennyGalactic',
    'BramptonWasteMan','Chokolate_Thunda','CoolSkillsBroo','D-TownDon',
    'DirtyWeenur','Drakee_GG','DrewBreesus2192','DunkinDonutz915',
    'Find_the_Door','Gi0D0g88','JB3v3','Jnolte','KJJ205','Keem_50kFG',
    'KillaE94','KingCaleb_18','KingMak__','LIXODYSSEY','Maola11',
    'Masrimadden','MeLLoW_FiRe','MizzGMB','Mr_Clutch723','NutzonJorge',
    'OliveiraYourFace','Ronfk','Saucy0134','SuaveShaunTTV','Swole_Shell50',
    'TheGasGOD_423','TheNotoriousLTH','TheWitt','The_KG_518',
    'TrombettaThanYou','Villanova46','Will_Chamberlain','YoungSeeThrough',
    'ayyepea','cfar89','kickerbog10','quickcroom','thekingf_1014'
]

# Nickname → DB username map for fuzzy resolution
NICKNAME_TO_USER = {
    "jt":        "TrombettaThanYou",
    "killa":     "KillaE94",
    "nova":      "Villanova46",
    "ken":       "KJJ205",
    "jo":        "OliveiraYourFace",
    "jorge":     "NutzonJorge",
    "witt":      "TheWitt",
    "lth":       "TheNotoriousLTH",
    "chok":      "Chokolate_Thunda",
    "diddy":     "BDiddy86",
    "bdiddy":    "BDiddy86",
    "keem":      "Keem_50kFG",
    "kg":        "The_KG_518",
    "ronfk":     "Ronfk",
    "ron":       "Ronfk",
    "shelly":    "Swole_Shell50",
    "ruck":      "quickcroom",
    "tuna":      "SuaveShaunTTV",
    "shaun":     "SuaveShaunTTV",
}


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
        if len(clean) < 3:
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_sql(sql: str) -> tuple[list[dict], str | None]:
    """Execute SQL, return (rows, error)."""
    try:
        conn = get_db()
        cur = conn.execute(sql)
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


async def gemini_sql(question: str, client, alias_map: dict | None = None) -> str | None:
    """Ask Gemini to generate SQL. Non-blocking via run_in_executor."""
    alias_block = ""
    if alias_map:
        lines = [f"  '{nick}' → use username '{user}' in SQL" for nick, user in alias_map.items()]
        alias_block = "\nRESOLVED NAME ALIASES (use these exact values in WHERE clauses):\n" + "\n".join(lines) + "\n"

    known_users_block = (
        "\nVALID homeUser/awayUser VALUES (exact strings stored in the database):\n"
        + ", ".join(f"'{u}'" for u in KNOWN_USERS)
        + "\nOnly use these exact strings in WHERE clauses involving homeUser or awayUser.\n"
    )

    prompt = f"""You are a SQLite expert for The Simulation League (TSL) Madden database.

{DB_SCHEMA}
{known_users_block}{alias_block}
Generate a single valid SQLite SELECT query to answer this question:
"{question}"

Rules:
- Return ONLY the SQL query, no explanation, no markdown fences.
- Cast numeric columns when doing comparisons: CAST(col AS INTEGER)
- Limit results to 30 rows unless the user specifically needs more.
- For user/owner questions, query homeUser/awayUser using EXACT usernames from the valid values list above.
- Default to stageIndex='1' (regular season) unless the user asks about playoffs.
- Never use DROP, INSERT, UPDATE, DELETE, or any DDL.
"""

    def _call():
        return client.models.generate_content(model="gemini-2.0-flash", contents=prompt)

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, _call)
    return extract_sql(response.text)


async def gemini_answer(question: str, sql: str, rows: list[dict], client) -> str:
    """Format SQL results into natural language. Non-blocking via run_in_executor."""
    results_str = json.dumps(rows, indent=2)
    if len(results_str) > MAX_CHARS:
        results_str = results_str[:MAX_CHARS] + "\n... (truncated)"

    no_data_instruction = ""
    if not rows:
        no_data_instruction = (
            "\nCRITICAL: The query returned NO rows. State clearly that no data was found "
            "for this question. Do NOT invent stats or outcomes.\n"
        )

    prompt = f"""{_answer_persona()}

A TSL member asked: "{question}"

I ran this SQL query:
{sql}

Results:
{results_str}
{no_data_instruction}
Using these exact results, answer the question.
Be accurate to the data — do not invent stats or outcomes not in the results.
Don't repeat the SQL or mention databases — just give the answer.
"""

    def _call():
        return client.models.generate_content(model="gemini-2.0-flash", contents=prompt)

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, _call)
    return response.text.strip()


# ── Cog ──────────────────────────────────────────────────────────────────────

class CodexCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot  = bot
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("[CodexCog] ⚠️  GEMINI_API_KEY not set — /ask, /h2h, /season_recap will fail.")
        self.gemini = genai.Client(api_key=api_key) if api_key else None

    # ── /ask ─────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="ask",
        description="Ask ATLAS anything about TSL history — stats, records, rivalries, trades"
    )
    @app_commands.describe(question="Your question about TSL history")
    @app_commands.checks.cooldown(3, 30)  # 3 uses per 30 seconds per user
    async def ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer(thinking=True)

        if not self.gemini:
            await interaction.followup.send("❌ ATLAS AI is offline — GEMINI_API_KEY not configured.")
            return

        try:
            # Resolve caller via Discord ID — immune to username changes.
            # Falls back to fuzzy username match if not in registry yet.
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
            question_with_context = f"{caller_context} {question}"
            annotated_question, alias_map = resolve_names_in_question(question_with_context)

            sql = await gemini_sql(annotated_question, self.gemini, alias_map)
            if not sql:
                await interaction.followup.send(
                    "📊 Couldn't generate a query for that one. Try rephrasing — "
                    "be specific about player names, seasons, or owners."
                )
                return

            rows, error = run_sql(sql)
            if error:
                # Self-correct once with TEXT casting reminder
                fix_prompt = (
                    f"This SQLite query for a Madden database failed:\n{sql}\n\n"
                    f"Error: {error}\n\n"
                    f"REMINDER: ALL columns are stored as TEXT. Always use CAST(col AS INTEGER) "
                    f"for numeric comparisons.\n"
                    f"Fix the query. Return ONLY valid SQLite SQL, no explanation.\n\n"
                    f"Schema:\n{DB_SCHEMA}"
                )
                def _fix():
                    return self.gemini.models.generate_content(
                        model="gemini-2.0-flash", contents=fix_prompt
                    )
                loop = asyncio.get_running_loop()
                fix_response = await loop.run_in_executor(None, _fix)
                sql = extract_sql(fix_response.text) or sql
                rows, error = run_sql(sql)
                if error:
                    await interaction.followup.send(
                        f"⚠️ Query failed after auto-correction. "
                        f"Try `/ask_debug` for details, or rephrase using exact usernames."
                    )
                    return

            # Use original question (no context prefix) for the natural-language answer
            answer = await gemini_answer(question, sql, rows, self.gemini)

            embed = discord.Embed(
                title="📊 TSL Historical Intelligence",
                description=answer,
                color=0xC9962A  # ATLAS Gold
            )
            embed.set_author(
                name="ATLAS · Autonomous TSL League Administration System",
                icon_url="https://cdn.discordapp.com/attachments/977007320259244055/1479928571022544966/ATLASLOGO.png?ex=69add263&is=69ac80e3&hm=227036e833a3ca497e5ece0bf88f0aca593f08f138eab6482f9bddc9dd320cd9&"
            )
            footer_parts = [f"🔍 {len(rows)} records analyzed"]
            if alias_map:
                resolved_str = ", ".join(f"{k}→{v}" for k, v in alias_map.items())
                footer_parts.append(f"🔎 Resolved: {resolved_str}")
            embed.set_footer(
                text=" | ".join(footer_parts) + " · ATLAS™ Codex Module",
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

        if not self.gemini:
            await interaction.followup.send("❌ ATLAS AI is offline — GEMINI_API_KEY not configured.")
            return

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
            sql = await gemini_sql(annotated_question, self.gemini, alias_map)
            if not sql:
                await interaction.followup.send("❌ No SQL generated.")
                return

            rows, error = run_sql(sql)

            embed = discord.Embed(title="🔧 ATLAS Codex — Debug", color=0xC9962A)
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

    # ── Deprecated wrapper (remove in Phase 5) ──────────────────────────────
    @app_commands.command(
        name="ask_debug",
        description="[Deprecated] Use /commish askdebug instead."
    )
    @app_commands.describe(question="Your question about TSL history")
    async def ask_debug(self, interaction: discord.Interaction, question: str):
        admin_ids = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
        if interaction.user.id not in admin_ids:
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await self._ask_debug_impl(interaction, question)

    # ── H2H and Season Recap _impl methods (called by oracle HubView buttons) ──

    async def _h2h_impl(self, interaction: discord.Interaction, owner1: str, owner2: str):
        """Head-to-head record — used by oracle HubView H2H modal."""
        await interaction.response.defer(thinking=True)

        if not self.gemini:
            await interaction.followup.send("ATLAS AI is offline — GEMINI_API_KEY not configured.")
            return

        u1 = fuzzy_resolve_user(owner1)
        u2 = fuzzy_resolve_user(owner2)

        if not u1:
            await interaction.followup.send(f"Couldn't find an owner matching `{owner1}`.")
            return
        if not u2:
            await interaction.followup.send(f"Couldn't find an owner matching `{owner2}`.")
            return

        sql = f"""
        SELECT
            seasonIndex,
            SUM(CASE WHEN winner_user = '{u1}' THEN 1 ELSE 0 END) AS u1_wins,
            SUM(CASE WHEN winner_user = '{u2}' THEN 1 ELSE 0 END) AS u2_wins,
            COUNT(*) AS games_played,
            GROUP_CONCAT(
                'S' || seasonIndex || ' W' || (CAST(weekIndex AS INTEGER)+1) ||
                ': ' || homeTeamName || ' ' || homeScore || '-' || awayScore || ' ' || awayTeamName
            ) AS game_log
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND ((homeUser = '{u1}' AND awayUser = '{u2}')
            OR (homeUser = '{u2}' AND awayUser = '{u1}'))
        GROUP BY seasonIndex
        ORDER BY CAST(seasonIndex AS INTEGER)
        """

        rows, error = run_sql(sql)
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
        def _call():
            return self.gemini.models.generate_content(model="gemini-2.0-flash", contents=summary_prompt)
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call)
        summary = response.text.strip()

        embed = discord.Embed(title=f"Rivalry Report: {u1} vs {u2}", color=0xC9962A)
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

        sql = f"""
        SELECT winner_user, loser_user, winner_team, loser_team,
               homeScore, awayScore, weekIndex
        FROM games
        WHERE seasonIndex='{season}' AND stageIndex='1' AND status IN ('2','3')
        ORDER BY CAST(weekIndex AS INTEGER)
        """
        rows, _ = run_sql(sql)

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
        def _call():
            return self.gemini.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call)

        embed = discord.Embed(
            title=f"TSL Season {season} Recap",
            description=response.text.strip(),
            color=0xC9962A
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
