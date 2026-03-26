"""
codex_cog.py v1.4  ─  ATLAS Historical Intelligence (Codex Module)
Uses Gemini to convert natural language questions → SQL → natural language answers
against the TSL SQLite database.

Commands:
  /ask_debug    <question>        [Admin] Show generated SQL + raw rows — diagnostics
  (Note: /ask removed in v5.0 — use /oracle hub instead)
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
import logging
import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
import atlas_ai
from atlas_ai import AIResult, Tier
from difflib import get_close_matches

import data_manager as dm
from atlas_colors import AtlasColors

log = logging.getLogger(__name__)

# Optional modules
try:
    import affinity as _affinity_mod
except ImportError:
    _affinity_mod = None

try:
    from build_member_db import get_db_username_for_discord_id as _get_db_username
    from build_member_db import resolve_db_username as _resolve_db_username
    from build_member_db import upsert_member as _upsert_member
except ImportError:
    _get_db_username = None
    _resolve_db_username = None
    _upsert_member = None

try:
    from codex_intents import detect_intent, check_self_reference_collision, get_h2h_sql_and_params
except ImportError:
    detect_intent = None
    check_self_reference_collision = None
    get_h2h_sql_and_params = None

# ── Config ──────────────────────────────────────────────────────────────────
MAX_CHARS = 3000
_EMBED_DESC_LIMIT = 4096

# ── Shared utils (extracted to codex_utils.py) ─────────────────────────────
from codex_utils import (
    DB_PATH, MAX_ROWS, get_db, run_sql, run_sql_async,
    extract_sql, validate_sql, retry_sql,
    fuzzy_resolve_user, resolve_names_in_question, ai_resolve_names,
    _build_schema, _get_db_schema,
    KNOWN_USERS, NICKNAME_TO_USER,
    _ensure_codex_identity, refresh_codex_identity,
)


def _truncate_for_embed(text: str, limit: int = _EMBED_DESC_LIMIT) -> str:
    """Truncate text to fit within Discord embed description limit."""
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."

# ── Query Cache — Tier 3 NL→SQL result caching ──────────────────────────────

@dataclass
class _CacheEntry:
    sql: str
    rows: list[dict]
    attempt: int
    warnings: list[str]
    created_at: float = field(default_factory=time.time)


class _QueryCache:
    """LRU cache for Tier 3 NL→SQL pipeline results."""

    def __init__(self, max_entries: int = 200, ttl_seconds: int = 300):
        self._cache: dict[str, _CacheEntry] = {}
        self._max = max_entries
        self._ttl = ttl_seconds
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _normalize(question: str) -> str:
        q = question.lower().strip()
        q = re.sub(r'\s+', ' ', q)
        q = q.rstrip('?.!')
        return q

    def make_key(self, question: str, caller_db: str | None) -> str:
        return f"{self._normalize(question)}|{caller_db or 'anon'}"

    def get(self, key: str) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if not entry or (time.time() - entry.created_at) > self._ttl:
            if entry:
                del self._cache[key]
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def set(self, key: str, sql: str, rows: list[dict], attempt: int, warnings: list[str]) -> None:
        if len(self._cache) >= self._max:
            # Evict oldest 25%
            by_age = sorted(self._cache, key=lambda k: self._cache[k].created_at)
            for k in by_age[:self._max // 4]:
                del self._cache[k]
        self._cache[key] = _CacheEntry(sql=sql, rows=rows, attempt=attempt, warnings=warnings)

    def clear(self) -> None:
        n = len(self._cache)
        self._cache.clear()
        if n:
            print(f"[QueryCache] Cleared {n} entries")

    def __len__(self) -> int:
        return len(self._cache)


_query_cache = _QueryCache()


def clear_query_cache() -> None:
    """Public API for bot.py to invalidate cache after sync_tsl_db()."""
    _query_cache.clear()


# ── Echo persona loader — analytical register for answer generation ───────────
try:
    from echo_loader import get_persona as _get_persona
except ImportError:
    _get_persona = lambda _mode="casual": "You are ATLAS."

def _answer_persona() -> str:
    return _get_persona("analytical")

# ── Conversation Memory (shared module) ──────────────────────────────────────
from conversation_memory import add_conversation_turn, build_conversation_block


# ── Schema, identity, SQL pipeline now in codex_utils.py ────────────────────


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
12. For owner ranking queries ("best/worst owner", "winningest", "owner with most/fewest wins"):
    - ALWAYS JOIN owner_tenure and require COUNT(DISTINCT seasonIndex) >= 3 AND SUM(games_played) >= 30.
      This excludes drive-by members who played a few games and left.
    - For "worst" queries: rank by win PERCENTAGE (wins/total_games*100), not raw win count.
    - For "best" queries: raw win count is fine, but still require the minimum games threshold.
13. owner_tenure has columns: teamName, userName, seasonIndex, games_played.
    It tracks every owner's game count per team per season. Use it for career-span queries.

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

Q: "Who are the worst owners of all time?"
SQL: SELECT ot_agg.userName AS owner, SUM(CASE WHEN g.winner_user=ot_agg.userName THEN 1 ELSE 0 END) AS wins, COUNT(*) AS total_games, ROUND(CAST(SUM(CASE WHEN g.winner_user=ot_agg.userName THEN 1 ELSE 0 END) AS REAL)/COUNT(*)*100,1) AS win_pct FROM (SELECT userName, COUNT(DISTINCT seasonIndex) AS seasons, SUM(games_played) AS career_games FROM owner_tenure GROUP BY userName HAVING COUNT(DISTINCT seasonIndex) >= 3 AND SUM(games_played) >= 30) ot_agg JOIN games g ON g.status IN ('2','3') AND g.stageIndex='1' AND (g.homeUser=ot_agg.userName OR g.awayUser=ot_agg.userName) GROUP BY ot_agg.userName ORDER BY win_pct ASC LIMIT 10

Now generate a query for this question:
"{question}"
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, temperature=0.05)
    return extract_sql(result.text)


async def gemini_answer(
    question: str, sql: str, rows: list[dict],
    conv_context: str = "",
) -> AIResult:
    """Format SQL results into natural language via AI. Returns full AIResult."""
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

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET)
    return result


# ── Cog ──────────────────────────────────────────────────────────────────────

class CodexCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot  = bot

    # ── /ask removed — use /oracle hub instead ─────────────────────────────

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
                await interaction.followup.send(
                    "❌ Couldn't generate a query — try rephrasing your question, or use a simpler format like \"compare X and Y\" or \"who has the most wins\".",
                    ephemeral=True,
                )
                return

            rows, error = await run_sql_async(sql)

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
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Debug error: `{e}`", ephemeral=True)

    # ── H2H and Season Recap _impl methods (called by oracle HubView buttons) ──

    async def _h2h_impl(self, interaction: discord.Interaction, owner1: str, owner2: str):
        """Head-to-head record — used by oracle HubView H2H modal."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        u1 = fuzzy_resolve_user(owner1)
        u2 = fuzzy_resolve_user(owner2)

        if not u1:
            await interaction.followup.send(f"Couldn't find an owner matching `{owner1}`.", ephemeral=True)
            return
        if not u2:
            await interaction.followup.send(f"Couldn't find an owner matching `{owner2}`.", ephemeral=True)
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
                    'S' || seasonIndex || ' ' || """ + dm.WEEK_LABEL_SQL + """ ||
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

        rows, error = await run_sql_async(sql, params)
        if error or not rows:
            await interaction.followup.send(
                f"No completed regular season games found between **{u1}** and **{u2}**.",
                ephemeral=True,
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
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _season_recap_impl(self, interaction: discord.Interaction, season: int):
        """Season recap — used by oracle HubView Season Recap modal."""
        await interaction.response.defer(thinking=True)

        if season < 1 or season > dm.CURRENT_SEASON:
            await interaction.followup.send(f"Valid seasons are 1 through {dm.CURRENT_SEASON}.", ephemeral=True)
            return

        sql = """
        SELECT winner_user, loser_user, winner_team, loser_team,
               homeScore, awayScore, weekIndex
        FROM games
        WHERE seasonIndex=? AND stageIndex='1' AND status IN ('2','3')
        ORDER BY CAST(weekIndex AS INTEGER)
        """
        rows, _ = await run_sql_async(sql, (str(season),))

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
        try:
            count = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            print(f"[CodexCog] tsl_history.db OK — {count} games in DB ✅")
        finally:
            conn.close()
    except Exception as e:
        print(f"[CodexCog] ⚠️  WARNING: tsl_history.db check failed: {e}")
        print(f"[CodexCog] ⚠️  Run build_tsl_db.py to populate the database!")
    await bot.add_cog(CodexCog(bot))
    print("ATLAS: Codex · Historical Intelligence loaded. 📜")
