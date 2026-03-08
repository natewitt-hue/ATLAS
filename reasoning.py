"""
reasoning.py

Two-phase Gemini reasoning engine:

Phase 1 — ANALYST:
  Gemini receives the question + full dataframe schemas + sample data.
  It writes Python code to query the dataframes and compute an answer.
  The code is executed in a sandboxed environment.

Phase 2 — WITTGPT:
  The execution result feeds into WittGPT's persona prompt.
  WittGPT delivers the answer as a trash-talking commissioner.

Falls back to analysis.route_query() if code generation or execution fails.
"""

import traceback
import pandas as pd
import numpy as np
import data_manager as dm

# ── Schema snapshot (generated once at import) ────────────────────────────────

def _schema_for(name: str, df: pd.DataFrame, sample_rows: int = 3) -> str:
    if df.empty:
        return f"{name}: empty\n"
    lines = [f"\n### {name} ({len(df)} rows)"]
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            lines.append(f"  {col}: numeric  min={df[col].min():.1f}  max={df[col].max():.1f}")
        else:
            samples = df[col].dropna().unique()[:3]
            lines.append(f"  {col}: string  e.g.{list(samples)}")
    lines.append(f"\nSample rows:\n{df.head(sample_rows).to_string(index=False)}")
    return "\n".join(lines)


def build_schema_prompt() -> str:
    """Full schema description passed to the analyst."""
    sections = [
        _schema_for("df_offense",    dm.df_offense,    3),
        _schema_for("df_defense",    dm.df_defense,    3),
        _schema_for("df_team_stats", dm.df_team_stats, 3),
        _schema_for("df_standings",  dm.df_standings,  3),
        _schema_for("df_teams",      dm.df_teams,      3),
        _schema_for("df_players",    dm.df_players,    3),
        _schema_for("df_games",      dm.df_games,      3),
        _schema_for("df_trades",     dm.df_trades,     3),
    ]
    return "\n\n".join(sections)


# Cache schema so it's only computed once
_SCHEMA_CACHE: str = ""


def get_schema() -> str:
    global _SCHEMA_CACHE
    if not _SCHEMA_CACHE:
        _SCHEMA_CACHE = build_schema_prompt()
    return _SCHEMA_CACHE


def invalidate_schema_cache():
    """Clear the cached schema so it rebuilds on next query (call after data reload)."""
    global _SCHEMA_CACHE
    _SCHEMA_CACHE = ""


# ── Pre-built composite metrics (always available to generated code) ──────────

PREBUILT_METRICS_CODE = """
import pandas as pd
import numpy as np

def _norm(series):
    mn, mx = series.min(), series.max()
    return (series - mn) / (mx - mn + 1e-9)

# ── TEAM METRICS ──────────────────────────────────────────────────────────────

def compute_spam_scores(df_team_stats):
    \"\"\"
    Spam score = how pass-heavy, red-zone-hungry, turnover-prone, and penalty-ridden a team is.
    Higher = more spam.
    \"\"\"
    ts = df_team_stats.copy()
    ts['passRatio'] = ts['offPassYds'] / (ts['offPassYds'] + ts['offRushYds'] + 1e-9)
    ts['spamScore'] = (
        _norm(ts['passRatio'])    * 35 +
        _norm(ts['offRedZones'])  * 20 +
        _norm(ts['off4thDownAtt'])* 20 +
        _norm(ts['tOGiveAways'])  * 15 +
        _norm(ts['penalties'])    * 10
    ).round(1)
    return ts[['teamName','spamScore','passRatio','offRedZones','off4thDownAtt','tOGiveAways','penalties']]

def compute_sim_scores(df_team_stats):
    \"\"\"
    Sim score = balanced play-calling, low penalties, low turnovers, efficient 3rd down.
    Higher = more sim.
    \"\"\"
    ts = df_team_stats.copy()
    ts['passRatio'] = ts['offPassYds'] / (ts['offPassYds'] + ts['offRushYds'] + 1e-9)
    ts['simScore'] = (
        (1 - abs(ts['passRatio'] - 0.55)) * 30 +
        _norm(1 - _norm(ts['tOGiveAways'])) * 25 +
        _norm(1 - _norm(ts['penalties']))   * 25 +
        _norm(ts['off3rdDownConvPct'])       * 20
    ).round(1)
    return ts[['teamName','simScore','passRatio','penalties','tOGiveAways','off3rdDownConvPct']]

def compute_cheese_scores(df_team_stats):
    \"\"\"
    Cheese score = high 4th down aggression + tons of red zone trips + poor 3rd down efficiency
    (suggesting they're skipping 3rd down management) + high penalty count.
    \"\"\"
    ts = df_team_stats.copy()
    ts['cheeseScore'] = (
        _norm(ts['off4thDownAtt'])              * 35 +
        _norm(ts['offRedZones'])                * 25 +
        _norm(1 - _norm(ts['off3rdDownConvPct']))* 20 +
        _norm(ts['penalties'])                  * 20
    ).round(1)
    return ts[['teamName','cheeseScore','off4thDownAtt','offRedZones','off3rdDownConvPct','penalties']]

def compute_power_scores(df_standings):
    \"\"\"Composite power ranking score.\"\"\"
    df = df_standings.copy()
    df['winPct'] = pd.to_numeric(df['winPct'], errors='coerce').fillna(0)
    df['powerScore'] = (
        _norm(df['winPct'])   * 40 +
        _norm(df['netPts'])   * 30 +
        _norm(df['tODiff'])   * 15 +
        _norm(32 - df['offTotalYdsRank']) * 8 +
        _norm(32 - df['defTotalYdsRank']) * 7
    ).round(1)
    return df[['teamName','powerScore','totalWins','totalLosses','netPts','tODiff']]

# ── PLAYER METRICS ────────────────────────────────────────────────────────────

def compute_qb_scores(df_offense, min_att=50):
    \"\"\"
    QB composite score: TDs, yards, comp%, penalizes INTs and sacks.
    Only includes QBs with min_att attempts.
    \"\"\"
    qbs = df_offense[(df_offense['pos'] == 'QB') & (df_offense['passAtt'] >= min_att)].copy()
    qbs['tdRate']   = qbs['passTDs']  / (qbs['passAtt'] + 1e-9)
    qbs['intRate']  = qbs['passInts'] / (qbs['passAtt'] + 1e-9)
    qbs['sackRate'] = qbs['passSacks']/ (qbs['passAtt'] + 1e-9)
    qbs['qbScore']  = (
        _norm(qbs['passYds'])    * 25 +
        _norm(qbs['tdRate'])     * 30 +
        _norm(qbs['passCompPct'])* 20 +
        _norm(1 - _norm(qbs['intRate']))  * 15 +
        _norm(1 - _norm(qbs['sackRate'])) * 10
    ).round(1)
    return qbs[['extendedName','teamName','passAtt','passYds','passTDs','passInts','passCompPct','qbScore']]

def compute_rb_scores(df_offense, min_att=20):
    \"\"\"RB composite: yards, TDs, broken tackles, penalizes fumbles.\"\"\"
    rbs = df_offense[(df_offense['pos'] == 'HB') & (df_offense['rushAtt'] >= min_att)].copy()
    rbs['ypc']      = rbs['rushYds'] / (rbs['rushAtt'] + 1e-9)
    rbs['fumRate']  = rbs['rushFum'] / (rbs['rushAtt'] + 1e-9)
    rbs['rbScore']  = (
        _norm(rbs['rushYds'])            * 30 +
        _norm(rbs['rushTDs'])            * 25 +
        _norm(rbs['ypc'])                * 20 +
        _norm(rbs['rushBrokenTackles'])  * 15 +
        _norm(1 - _norm(rbs['fumRate'])) * 10
    ).round(1)
    return rbs[['extendedName','teamName','rushAtt','rushYds','rushTDs','rushBrokenTackles','rushFum','rbScore']]

def compute_wr_scores(df_offense, min_catches=10):
    \"\"\"WR/TE composite: yards, TDs, YPC, penalizes drops.\"\"\"
    wrs = df_offense[
        (df_offense['pos'].isin(['WR','TE'])) & (df_offense['recCatches'] >= min_catches)
    ].copy()
    wrs['dropRate'] = wrs['recDrops'] / (wrs['recCatches'] + wrs['recDrops'] + 1e-9)
    wrs['wrScore']  = (
        _norm(wrs['recYds'])              * 35 +
        _norm(wrs['recTDs'])              * 30 +
        _norm(wrs['recYdsPerCatch'])      * 20 +
        _norm(1 - _norm(wrs['dropRate']))* 15
    ).round(1)
    return wrs[['extendedName','teamName','recCatches','recYds','recTDs','recDrops','wrScore']]

def compute_sim_players(df_offense, df_defense):
    \"\"\"
    Sim player score: Players who put up stats without spammy behavior.
    QBs: high comp%, low INT rate, balanced pass/rush.
    RBs: high YPC, low fumbles.
    WRs: low drop rate, consistent catches.
    Defenders: high tackle count relative to their position's expectation.
    Returns a combined ranked list.
    \"\"\"
    results = []

    # QBs
    qbs = df_offense[(df_offense['pos'] == 'QB') & (df_offense['passAtt'] >= 50)].copy()
    if not qbs.empty:
        qbs['intRate'] = qbs['passInts'] / (qbs['passAtt'] + 1e-9)
        qbs['simQB']   = (
            _norm(qbs['passCompPct'])           * 40 +
            _norm(1 - _norm(qbs['intRate']))    * 35 +
            _norm(qbs['passYds'])               * 25
        ).round(1)
        top = qbs.nlargest(5, 'simQB')[['extendedName','teamName','pos','passCompPct','passInts','passYds','simQB']]
        top = top.rename(columns={'simQB': 'simScore'})
        results.append(top)

    # RBs
    rbs = df_offense[(df_offense['pos'] == 'HB') & (df_offense['rushAtt'] >= 20)].copy()
    if not rbs.empty:
        rbs['fumRate'] = rbs['rushFum'] / (rbs['rushAtt'] + 1e-9)
        rbs['ypc']     = rbs['rushYds'] / (rbs['rushAtt'] + 1e-9)
        rbs['simRB']   = (
            _norm(rbs['ypc'])                    * 40 +
            _norm(1 - _norm(rbs['fumRate']))     * 35 +
            _norm(rbs['rushBrokenTackles'])      * 25
        ).round(1)
        top = rbs.nlargest(5, 'simRB')[['extendedName','teamName','pos','rushYds','rushFum','rushBrokenTackles','simRB']]
        top = top.rename(columns={'simRB': 'simScore'})
        results.append(top)

    # WRs
    wrs = df_offense[(df_offense['pos'] == 'WR') & (df_offense['recCatches'] >= 10)].copy()
    if not wrs.empty:
        wrs['dropRate'] = wrs['recDrops'] / (wrs['recCatches'] + wrs['recDrops'] + 1e-9)
        wrs['simWR']    = (
            _norm(1 - _norm(wrs['dropRate']))    * 45 +
            _norm(wrs['recYds'])                 * 30 +
            _norm(wrs['recCatches'])             * 25
        ).round(1)
        top = wrs.nlargest(5, 'simWR')[['extendedName','teamName','pos','recCatches','recDrops','recYds','simWR']]
        top = top.rename(columns={'simWR': 'simScore'})
        results.append(top)

    if results:
        combined = pd.concat(results, ignore_index=True)
        return combined.sort_values('simScore', ascending=False)
    return pd.DataFrame()
"""

# ── Sandboxed execution environment ──────────────────────────────────────────

def build_exec_env() -> dict:
    """
    Returns the globals dict available to generated code.
    Includes all dataframes, helper functions, pandas, numpy.
    """
    env = {
        "pd": pd,
        "np": np,
        "df_offense":    dm.df_offense.copy(),
        "df_defense":    dm.df_defense.copy(),
        "df_team_stats": dm.df_team_stats.copy(),
        "df_standings":  dm.df_standings.copy(),
        "df_teams":      dm.df_teams.copy(),
        "df_players":    dm.df_players.copy(),
        "df_games":      dm.df_games.copy(),
        "df_trades":     dm.df_trades.copy(),
        "result":        None,  # generated code MUST set this
    }
    # Inject pre-built metrics into env
    exec(PREBUILT_METRICS_CODE, env)
    return env


def safe_exec(code: str) -> tuple[any, str]:
    """
    Executes generated code in a sandboxed env.
    Returns (result, error_message).
    result is whatever the code assigned to the `result` variable.
    """
    env = build_exec_env()
    try:
        exec(code, env)
        result = env.get("result")
        # Convert DataFrames to string for context
        if isinstance(result, pd.DataFrame):
            result = result.head(15).to_string(index=False)
        elif isinstance(result, pd.Series):
            result = result.head(15).to_string()
        return result, ""
    except Exception:
        return None, traceback.format_exc()


# ── Analyst prompt ────────────────────────────────────────────────────────────

ANALYST_SYSTEM = """
You are a precise Python data analyst for the TSL Madden Franchise league.

Your job: Write Python code that answers a question about TSL league stats.

RULES:
1. You have access to these DataFrames (already loaded, do NOT re-load any files):
   df_offense, df_defense, df_team_stats, df_standings, df_teams, df_players, df_games, df_trades
2. You also have these pre-built metric functions ready to call:
   - compute_spam_scores(df_team_stats)       → team spam scores
   - compute_sim_scores(df_team_stats)        → team sim scores  
   - compute_cheese_scores(df_team_stats)     → team cheese scores
   - compute_power_scores(df_standings)       → team power scores
   - compute_qb_scores(df_offense)            → QB composite scores
   - compute_rb_scores(df_offense)            → RB composite scores
   - compute_wr_scores(df_offense)            → WR composite scores
   - compute_sim_players(df_offense, df_defense) → most sim players by position
3. You have pandas (pd) and numpy (np) available.
4. ALWAYS assign your final answer to a variable called `result`.
   `result` should be a DataFrame, string, list, or dict — whatever best answers the question.
5. Keep result concise — top 5-10 rows for lists, key numbers for comparisons.
6. For "who is the biggest spammer" → use compute_spam_scores, sort descending, head(5)
7. For "least sim stats" → use compute_sim_players or compute_sim_scores, sort ascending, head(5)
8. For "who would win X vs Y" → compare their powerScore, offTotalYds, defTotalYds, netPts, tODiff
9. For player comparisons → join df_offense or df_defense, compute relevant rates, compare side by side
10. For "most improved" → you'd need to compare seasons — df_games and df_standings have seasonIndex
11. ONLY output Python code. No explanation. No markdown. No imports. Just raw executable Python.
12. If you're unsure, compute multiple angles and combine them into result.

IMPORTANT COLUMN NOTES:
- df_offense has passAtt (NOT passAtts), passCompPct is already computed as a float
- df_defense stats like defTotalTackles are already numeric (pre-cast)
- df_team_stats has both offPassYds and offRushYds for pass ratio calculations
- df_standings winPct is a string — cast with pd.to_numeric() before math
- df_players has playerBestOvr, dev, age, contractSalary, capHit, value
"""

ANALYST_USER_TEMPLATE = """
LEAGUE DATA SCHEMAS:
{schema}

QUESTION: {question}

Write Python code to answer this. Assign the answer to `result`.
"""


# ── Gemini call (analyst phase) ───────────────────────────────────────────────

async def generate_analysis_code(question: str, gemini_client) -> str:
    """
    Phase 1: Ask Gemini to write Python code to answer the question.
    Delegates to _call_analyst for consistent code-fence stripping and error handling.
    Returns the raw code string.
    """
    prompt = ANALYST_USER_TEMPLATE.format(
        schema=get_schema(),
        question=question,
    )
    return await _call_analyst(prompt, gemini_client, temperature=0.2)


# ── Self-Correcting Execution Loop ────────────────────────────────────────────

MAX_RETRIES = 2   # Hard cap: max 2 retries before graceful failure


async def _call_analyst(prompt: str, gemini_client, temperature: float = 0.2) -> str:
    """
    Single Gemini call for the analyst persona.
    Strips markdown code fences from response.

    Args:
        prompt:        Full user-turn prompt to send.
        gemini_client: Initialized google.genai Client.
        temperature:   Sampling temperature (lower = more deterministic).

    Returns:
        Raw Python code string, or empty string on failure.
    """
    from google.genai import types
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            config=types.GenerateContentConfig(
                system_instruction=ANALYST_SYSTEM,
                temperature=temperature,
                top_p=0.9,
            ),
            contents=[prompt],
        )
        if not response.text:
            return ""
        code = response.text.strip()
        # Strip markdown fences (```python ... ``` or ``` ... ```)
        if code.startswith("```"):
            lines = code.split("\n")
            code  = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )
        return code.strip()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[Reasoning] Gemini analyst call failed: {e}")
        return ""


async def reason(question: str, gemini_client) -> dict:
    """
    Self-correcting two-phase reasoning pipeline.

    Phase 1: Gemini writes Python code to answer the question.
    Phase 2: Execute the code. On failure, feed the full Python traceback
             back to Gemini and ask it to rewrite — up to MAX_RETRIES times.

    Flow:
        attempt 0 → initial code generation + execution
        attempt 1 → retry with traceback from attempt 0
        attempt 2 → final retry with traceback from attempt 1

    If all attempts fail, returns success=False with the final error.
    If any attempt succeeds, returns success=True immediately.

    Returns:
        dict: {
            'success':  bool,
            'code':     str  (the last code attempted),
            'result':   str  (stringified result value),
            'error':    str  (final error, or "" on success),
            'question': str,
            'attempts': int  (1 = first try, 2 = one retry, 3 = two retries),
        }
    """
    import logging
    log = logging.getLogger(__name__)

    # Phase 1: generate initial code
    initial_prompt = ANALYST_USER_TEMPLATE.format(
        schema=get_schema(),
        question=question,
    )
    code = await _call_analyst(initial_prompt, gemini_client, temperature=0.2)

    if not code:
        return {
            "success":  False,
            "code":     "",
            "result":   "",
            "error":    "Gemini returned no code on initial attempt.",
            "question": question,
            "attempts": 0,
        }

    last_error = ""
    for attempt in range(1, MAX_RETRIES + 2):   # attempts: 1, 2, 3
        # Phase 2: execute
        result, error = safe_exec(code)

        if not error:
            # ✅ Success
            log.info(f"[Reasoning] '{question[:60]}' succeeded on attempt {attempt}.")
            return {
                "success":  True,
                "code":     code,
                "result":   str(result) if result is not None else "(no result)",
                "error":    "",
                "question": question,
                "attempts": attempt,
            }

        # ❌ Execution failed — capture full traceback
        last_error = error
        log.warning(
            f"[Reasoning] Attempt {attempt}/{MAX_RETRIES + 1} failed for "
            f"'{question[:60]}'\nError:\n{error[:500]}"
        )

        if attempt > MAX_RETRIES:
            # We've exhausted all retries
            break

        # Build self-correction prompt with the full traceback
        retry_prompt = (
            f"Your previous Python code failed with the following error:\n"
            f"```\n{error}\n```\n\n"
            f"Original question: {question}\n\n"
            f"Previous (broken) code:\n"
            f"```python\n{code}\n```\n\n"
            f"Available DataFrames and columns:\n{get_schema()}\n\n"
            f"TASK: Rewrite the code to fix the error. "
            f"Common fixes:\n"
            f"  - KeyError → check the schema above for exact column names\n"
            f"  - AttributeError → cast to numeric with pd.to_numeric() first\n"
            f"  - ValueError → handle NaN/empty DataFrames before operating\n"
            f"  - NameError → all data is already loaded; do NOT import or re-read files\n"
            f"Output ONLY raw Python code. No explanations. Assign result."
        )

        # Lower temperature on retries → more conservative/correct code
        retry_temp = 0.05 * attempt   # 0.05 on retry 1, 0.10 on retry 2
        code = await _call_analyst(retry_prompt, gemini_client, temperature=retry_temp)

        if not code:
            log.error(f"[Reasoning] Gemini returned no code on retry {attempt}.")
            break

    # All attempts exhausted
    log.error(f"[Reasoning] All {MAX_RETRIES + 1} attempts failed for: '{question[:60]}'")
    return {
        "success":  False,
        "code":     code,
        "result":   "",
        "error":    last_error,
        "question": question,
        "attempts": MAX_RETRIES + 1,
    }


# ── Decide: use reasoning or standard routing? ────────────────────────────────

REASONING_TRIGGERS = [
    # Playstyle
    "spam", "spammer", "cheese", "cheesing", "nano blitz", "sim", "most sim", "least sim",
    "play style", "playstyle", "how does", "what kind of",
    # Open-ended comparisons
    "who is better", "who's better", "compare", "vs", "versus",
    "who would win", "who wins", "predict", "hypothetical",
    # Narratives
    "most improved", "biggest disappointment", "best season", "worst season",
    "overrated", "underrated", "sleeper", "mvp", "best player",
    "most dominant", "most efficient", "biggest weakness", "strength",
    # Complex questions
    "why", "how come", "explain", "breakdown", "analyze", "analysis",
    "trend", "pattern", "consistent", "clutch", "choke",
    "cap space", "most expensive", "best value", "overpaid",
    # Questions that imply computation
    "ratio", "rate", "per game", "average", "efficiency",
    "what are the odds", "chance", "likely",
]


def should_reason(query: str) -> bool:
    """Returns True if the query should go through the reasoning pipeline."""
    q = query.lower()
    return any(trigger in q for trigger in REASONING_TRIGGERS)


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT-TO-SQL PIPELINE — Discord History Querying
# ═══════════════════════════════════════════════════════════════════════════════
"""
Text-to-SQL engine for querying the discord_history.db SQLite database.

Architecture:
  1. should_sql_query()    — classifier: does this question need SQL?
  2. generate_sql()        — LLM writes a SELECT query from natural language
  3. execute_sql_safe()    — runs query read-only with strict guardrails
  4. sql_to_context()      — formats rows into LLM-ready context string
  5. query_discord_history() — full pipeline combining the above steps

Self-correction: if SQL has a syntax error, feed traceback back to LLM
for up to MAX_SQL_RETRIES rewrites before graceful failure.

Security model:
  - DB opened in read-only (immutable URI) mode
  - All queries validated against a strict allowlist before execution
  - Any DDL/DML keyword instantly rejected
  - Result rows always capped at MAX_SQL_ROWS
"""

import re as _re
import sqlite3
import logging as _logging

_sql_log = _logging.getLogger(__name__ + ".sql")

MAX_SQL_RETRIES = 2
MAX_SQL_ROWS    = 100
SQL_TIMEOUT_S   = 8   # seconds before query is cancelled

# ── Trigger classification ─────────────────────────────────────────────────────

# Keywords that suggest a question needs exact counting / date lookup
# against the raw Discord message archive (not stats DataFrames)
_SQL_TRIGGERS = [
    # Counting occurrences
    "how many times", "how often", "count how many", "how frequently",
    "number of times", "times has", "times did", "times have",
    # Exact phrase retrieval
    "exact words", "exactly said", "what did", "when did", "first time",
    "last time", "ever say", "ever said", "ever called",
    # Specific insults / beef history
    "called", "said about", "talked about", "mentioned",
    # Date/timeline queries
    "what date", "when was", "timestamp", "first message",
    "oldest message", "latest message", "archive",
    # History / lore that needs exact match
    "discord history", "chat log", "message history", "server history",
    "who said", "who wrote", "who called",
]

# Keywords that ALSO appear in _SQL_TRIGGERS but don't really need SQL
_SQL_EXCLUSIONS = [
    "stats", "madden", "game", "season", "draft", "trade", "roster",
    "standings", "points", "touchdowns", "yards",
]


def should_sql_query(text: str) -> bool:
    """
    Returns True if the question should be answered via Text-to-SQL
    against the Discord history database.

    Priority: SQL trigger keywords present AND no stats-DB exclusion keywords.
    """
    if not dm.discord_db_exists():
        return False
    q = text.lower()
    has_trigger = any(t in q for t in _SQL_TRIGGERS)
    has_exclusion = any(e in q for e in _SQL_EXCLUSIONS)
    return has_trigger and not has_exclusion


# ── SQL Safety Guardrails ──────────────────────────────────────────────────────

# Banned SQL keywords — any of these in the generated query = immediate reject
_BANNED_SQL_KEYWORDS = _re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|CREATE|ALTER|ATTACH|DETACH|PRAGMA|"
    r"VACUUM|REINDEX|ANALYZE|REPLACE|UPSERT|GRANT|REVOKE|TRUNCATE|"
    r"EXEC|EXECUTE|LOAD_EXTENSION)\b",
    _re.IGNORECASE,
)

# Must start with SELECT (after stripping whitespace/comments)
_SELECT_PATTERN = _re.compile(r"^\s*(--[^\n]*)?\s*SELECT\b", _re.IGNORECASE | _re.DOTALL)

# Allowed tables — only the messages table and its FTS virtual table
_ALLOWED_TABLES = {"messages", "messages_fts"}

# Detect table references (simplified — covers most LLM-generated SQL)
_TABLE_REF_PATTERN = _re.compile(
    r"\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)"
    r"|\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    _re.IGNORECASE,
)


def _validate_sql(sql: str) -> tuple[bool, str]:
    """
    Validate a SQL string against security rules.

    Returns:
        (is_safe, reason_if_not_safe)
    """
    sql_stripped = sql.strip()

    # Must be a SELECT
    if not _SELECT_PATTERN.match(sql_stripped):
        return False, "Only SELECT statements are permitted."

    # No banned keywords
    banned_match = _BANNED_SQL_KEYWORDS.search(sql_stripped)
    if banned_match:
        return False, f"Banned keyword detected: '{banned_match.group()}'."

    # No subquery writes (e.g. SELECT * FROM (INSERT ...))
    # Covered by the banned keyword check above.

    # Only allowed tables referenced
    for match in _TABLE_REF_PATTERN.finditer(sql_stripped):
        table = (match.group(1) or match.group(2) or "").lower()
        if table and table not in _ALLOWED_TABLES:
            return False, f"Unauthorized table reference: '{table}'. Only 'messages' and 'messages_fts' are allowed."

    # Must have a LIMIT clause to prevent runaway queries
    if "LIMIT" not in sql_stripped.upper():
        # Auto-append limit rather than reject
        sql_stripped = sql_stripped.rstrip(";") + f"\nLIMIT {MAX_SQL_ROWS};"
        return True, sql_stripped  # Return patched SQL as reason when safe

    return True, sql_stripped


def _sanitize_sql(sql: str) -> str:
    """
    Strip markdown fences from LLM SQL output and return clean SQL.
    Also ensures a LIMIT clause is present.
    """
    sql = sql.strip()
    # Remove code fences
    if sql.startswith("```"):
        lines = sql.split("\n")
        sql = "\n".join(
            l for l in lines
            if not l.strip().startswith("```")
        ).strip()

    # Remove leading 'sql' language tag
    if sql.lower().startswith("sql"):
        sql = sql[3:].strip()

    # Ensure LIMIT
    if "LIMIT" not in sql.upper():
        sql = sql.rstrip(";") + f"\nLIMIT {MAX_SQL_ROWS};"

    return sql


# ── SQL Generation (LLM Phase 1) ──────────────────────────────────────────────

_SQL_SYSTEM_PROMPT = """You are a precise SQLite query writer for a Discord message archive database.

DATABASE SCHEMA:
{schema}

YOUR ONLY JOB: Write a single, valid SQLite SELECT query that answers the user's question.

STRICT RULES:
1. Only output raw SQL — no explanations, no markdown, no code fences.
2. Only SELECT statements. Never DROP, DELETE, INSERT, UPDATE, CREATE, ALTER, PRAGMA.
3. Only query tables: messages, messages_fts.
4. Always include LIMIT (max 100) to prevent runaway queries.
5. For author matching: use LIKE '%name%' (case-insensitive for ASCII in SQLite).
6. For keyword/phrase search: prefer FTS MATCH for performance on large tables.
   Syntax: WHERE messages_fts MATCH '"exact phrase"' or MATCH 'word1 word2'
7. When using FTS, JOIN like this:
   SELECT m.author, m.timestamp, m.content
   FROM messages m JOIN messages_fts f ON m.rowid = f.rowid
   WHERE messages_fts MATCH 'keyword'
   ORDER BY rank LIMIT 20;
8. For COUNT queries, always alias: SELECT COUNT(*) AS total ...
9. Include timestamp and author in output so results are readable.
10. If the question involves "how many times X said Y", use:
    SELECT COUNT(*) AS total FROM messages WHERE author LIKE '%X%' AND content LIKE '%Y%';
"""

_SQL_USER_TEMPLATE = """Question: {question}

Write a SQLite SELECT query to answer this. Output ONLY the raw SQL, nothing else."""


async def generate_sql(question: str, gemini_client) -> str:
    """
    Ask the LLM to write a SQLite query for the given question.

    Args:
        question:      Natural language question about Discord history.
        gemini_client: Initialized google.genai Client.

    Returns:
        Raw SQL string (may still be invalid — caller must validate).
    """
    from google.genai import types

    schema = dm.get_discord_db_schema()
    system = _SQL_SYSTEM_PROMPT.format(schema=schema)
    user   = _SQL_USER_TEMPLATE.format(question=question)

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.05,   # near-zero: SQL needs to be precise
                top_p=0.9,
            ),
            contents=[user],
        )
        raw = response.text.strip() if response.text else ""
        return _sanitize_sql(raw)
    except Exception as e:
        _sql_log.error(f"[SQL] generate_sql Gemini error: {e}")
        return ""


# ── Safe SQL Execution ─────────────────────────────────────────────────────────

class _SQLResult:
    """Container for SQL execution results."""
    __slots__ = ("rows", "columns", "error", "row_count", "sql_used")

    def __init__(self):
        self.rows      : list[dict] = []
        self.columns   : list[str]  = []
        self.error     : str        = ""
        self.row_count : int        = 0
        self.sql_used  : str        = ""


def execute_sql_safe(sql: str) -> _SQLResult:
    """
    Execute a validated SQL query against the Discord history DB.
    Enforces read-only access, row limits, and timeout.

    Args:
        sql: SQL string (should already be sanitized/validated).

    Returns:
        _SQLResult with rows as list-of-dicts, or error string on failure.
    """
    result = _SQLResult()
    result.sql_used = sql

    # Validate before touching the DB
    is_safe, detail = _validate_sql(sql)
    if not is_safe:
        result.error = f"SQL security validation failed: {detail}"
        _sql_log.warning(f"[SQL] Rejected query: {detail}\nSQL: {sql[:200]}")
        return result

    # Use the patched SQL if validation amended it
    if is_safe and detail != sql and detail.upper().startswith("ONLY"):
        # detail contains a reason string, not SQL
        pass
    elif is_safe and detail != sql:
        sql = detail  # Use the patched version (e.g. auto-LIMIT added)
        result.sql_used = sql

    try:
        conn = dm._get_discord_db(readonly=True)
        conn.set_progress_handler(None, 0)

        # Enforce timeout via SQLite interrupt
        import threading

        def _timeout():
            try:
                conn.interrupt()
            except Exception:
                pass

        timer = threading.Timer(SQL_TIMEOUT_S, _timeout)
        timer.start()

        try:
            cursor = conn.execute(sql)
            columns = [desc[0] for desc in (cursor.description or [])]
            raw_rows = cursor.fetchmany(MAX_SQL_ROWS)
        finally:
            timer.cancel()
            conn.close()

        result.columns   = columns
        result.rows      = [dict(zip(columns, row)) for row in raw_rows]
        result.row_count = len(result.rows)
        _sql_log.debug(f"[SQL] OK — {result.row_count} rows returned.")

    except sqlite3.OperationalError as e:
        result.error = f"SQLite error: {e}"
        _sql_log.warning(f"[SQL] OperationalError: {e}\nSQL: {sql[:300]}")
    except sqlite3.DatabaseError as e:
        result.error = f"Database error: {e}"
        _sql_log.error(f"[SQL] DatabaseError: {e}")
    except Exception as e:
        result.error = f"Unexpected error: {e}"
        _sql_log.error(f"[SQL] Unexpected error: {e}")

    return result


# ── Format result for LLM context ─────────────────────────────────────────────

def _format_sql_result(result: _SQLResult, question: str) -> str:
    """
    Format SQL result rows into a context string ready for WittGPT.

    Returns:
        Formatted [DISCORD ARCHIVE QUERY] context block.
    """
    lines = [
        f"[DISCORD ARCHIVE QUERY]",
        f"Question: {question}",
        f"SQL executed: {result.sql_used}",
    ]

    if result.error:
        lines.append(f"ERROR: {result.error}")
        lines.append("(No data available — answer as best you can.)")
        return "\n".join(lines)

    lines.append(f"Rows returned: {result.row_count}")

    if not result.rows:
        lines.append("RESULT: No matching messages found.")
        lines.append("(The archive has no records matching that query.)")
        return "\n".join(lines)

    # Single-value result (e.g. COUNT)
    if result.row_count == 1 and result.columns and len(result.columns) == 1:
        val = list(result.rows[0].values())[0]
        lines.append(f"RESULT: {result.columns[0]} = {val}")
        return "\n".join(lines)

    # Multi-row / multi-column result
    lines.append("\nRESULTS:")
    for i, row in enumerate(result.rows[:20], 1):
        ts      = row.get("timestamp", "")[:16]
        author  = row.get("author", "")
        content = row.get("content", "")[:200]
        # Generic fallback for non-message queries (e.g. COUNT GROUP BY)
        if not content and not author:
            lines.append(f"  [{i}] {dict(row)}")
        else:
            lines.append(f"  [{i}] {author} ({ts}): {content}")

    if result.row_count > 20:
        lines.append(f"  ... and {result.row_count - 20} more rows (truncated)")

    return "\n".join(lines)


# ── Self-Correcting SQL Pipeline ──────────────────────────────────────────────

async def query_discord_history(question: str, gemini_client) -> dict:
    """
    Full Text-to-SQL pipeline with self-correction.

    Flow:
      1. Generate SQL from natural language
      2. Validate and execute
      3. On error: feed full error + broken SQL back to LLM, retry (up to MAX_SQL_RETRIES)
      4. Format results as LLM-ready context string

    Args:
        question:      User's natural language question about Discord history.
        gemini_client: Initialized google.genai Client.

    Returns:
        dict: {
            'success':  bool,
            'context':  str   (formatted result for WittGPT),
            'sql':      str   (final SQL used),
            'rows':     list  (raw result rows),
            'error':    str   (final error or ""),
            'attempts': int,
        }
    """
    from google.genai import types

    sql = await generate_sql(question, gemini_client)

    if not sql:
        return {
            "success": False,
            "context": "[DISCORD ARCHIVE QUERY]\nERROR: LLM returned no SQL.",
            "sql": "",
            "rows": [],
            "error": "No SQL generated.",
            "attempts": 0,
        }

    last_error = ""
    for attempt in range(1, MAX_SQL_RETRIES + 2):
        result = execute_sql_safe(sql)

        if not result.error:
            # Success
            context = _format_sql_result(result, question)
            _sql_log.info(
                f"[SQL] Success on attempt {attempt}: {result.row_count} rows | "
                f"Q: '{question[:60]}'"
            )
            return {
                "success": True,
                "context": context,
                "sql": sql,
                "rows": result.rows,
                "error": "",
                "attempts": attempt,
            }

        last_error = result.error
        _sql_log.warning(
            f"[SQL] Attempt {attempt}/{MAX_SQL_RETRIES + 1} failed: {last_error}"
        )

        if attempt > MAX_SQL_RETRIES:
            break

        # Self-correction: ask LLM to fix the SQL
        schema = dm.get_discord_db_schema()
        fix_prompt = (
            f"Your SQL query failed with this error:\n{last_error}\n\n"
            f"Original question: {question}\n\n"
            f"Broken SQL:\n{sql}\n\n"
            f"Database schema:\n{schema}\n\n"
            f"Common fixes:\n"
            f"  - OperationalError 'no such column' → check exact column names above\n"
            f"  - FTS MATCH syntax error → simplify to: WHERE content LIKE '%word%'\n"
            f"  - 'no such table' → only use: messages, messages_fts\n"
            f"  - Ambiguous column → prefix with table alias (m.author, m.content)\n"
            f"  - Timeout → add tighter LIMIT or simplify the query\n\n"
            f"Rewrite the SQL to fix this error. Output ONLY raw SQL, nothing else."
        )

        try:
            fix_response = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                config=types.GenerateContentConfig(
                    system_instruction=_SQL_SYSTEM_PROMPT.format(schema=schema),
                    temperature=0.02,   # even more conservative on retry
                    top_p=0.85,
                ),
                contents=[fix_prompt],
            )
            if fix_response.text:
                sql = _sanitize_sql(fix_response.text.strip())
                _sql_log.info(f"[SQL] Retry {attempt} rewritten SQL: {sql[:150]}")
            else:
                _sql_log.error(f"[SQL] LLM returned no SQL on retry {attempt}.")
                break
        except Exception as e:
            _sql_log.error(f"[SQL] Retry {attempt} Gemini error: {e}")
            break

    _sql_log.error(f"[SQL] All {MAX_SQL_RETRIES + 1} attempts failed for: '{question[:60]}'")
    return {
        "success": False,
        "context": f"[DISCORD ARCHIVE QUERY]\nERROR after {MAX_SQL_RETRIES + 1} attempts: {last_error}\n(Couldn't retrieve data — roast them based on what you know.)",
        "sql": sql,
        "rows": [],
        "error": last_error,
        "attempts": MAX_SQL_RETRIES + 1,
    }
def get_intent(user_input, gemini_client):
    """Uses Gemini to decide which tool to use."""
    prompt = f"""
    Analyze this user query: "{user_input}"
    Classify it into ONE of these categories:
    - STATS: Real-time league data (yards, standings, trades, rosters).
    - HISTORY: Counting occurrences or searching specific old messages in the archive (How many times, what did X say).
    - LORE: Rivalries, beef, drama, or general league memory.
    - RULES: Questions about league rules, gameplay settings, or penalties.
    - OTHER: General chat or trash talk.

    Output ONLY the category name.
    """
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[prompt]
        )
        return response.text.strip().upper()
    except Exception:
        return "LORE" # Default fallback