"""
reasoning.py — ATLAS Two-Phase Gemini Reasoning Engine
─────────────────────────────────────────────────────────────────────────────
Phase 1 — ANALYST:
  Gemini receives the question + full dataframe schemas + sample data.
  It writes Python code to query the dataframes and compute an answer.
  The code is executed in a sandboxed environment.

Phase 2 — ATLAS:
  The execution result feeds into ATLAS's persona prompt.
  ATLAS delivers the answer as a trash-talking commissioner.

Falls back to analysis.route_query() if code generation or execution fails.

Fixes applied (v2):
  - _SCHEMA_CACHE now uses a 5-minute TTL (_SCHEMA_TIMESTAMP) so the schema
    automatically refreshes after every sync cycle without requiring a restart.
    bot.py also explicitly resets _SCHEMA_CACHE / _SCHEMA_TIMESTAMP after
    every successful _run_sync() call for immediate invalidation.
  - get_schema() now always regenerates if DataFrames are all empty (startup
    race condition where schema was cached before load_all() ran).
  - All original pre-built metric functions, REASONING_TRIGGERS, Text-to-SQL
    pipeline, and self-correcting execution loop are fully preserved.

Fixes applied (v3):
  - dm.df_trades now exists in data_manager — build_schema_prompt() and
    build_exec_env() no longer crash with AttributeError.
  - discord_db_exists(), get_discord_db_schema(), _get_discord_db() are now
    proper functions in data_manager — Text-to-SQL pipeline is fully wired.

Fixes applied (v4 — WittGPT Code Review rebuild):
  - FIX #10: exec() sandbox now restricts __builtins__ to a safe whitelist.
             If Gemini hallucinates `import os; os.system(...)`, the sandbox
             blocks it instead of executing arbitrary system commands.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import traceback
import time
import re as _re
import sqlite3
import pandas as pd
import numpy as np
import data_manager as dm
import atlas_ai
from atlas_ai import Tier

# ── Schema cache with TTL ─────────────────────────────────────────────────────
_SCHEMA_CACHE:     str   = ""
_SCHEMA_TIMESTAMP: float = 0.0
_SCHEMA_TTL:       int   = 300   # seconds — rebuild schema if older than 5 min


# ── Schema snapshot helpers ───────────────────────────────────────────────────

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
    """Full schema description passed to the analyst phase."""
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


def _all_dfs_empty() -> bool:
    """True if every DataFrame is still empty — indicates load_all() hasn't run yet."""
    return all(
        df.empty for df in [
            dm.df_offense, dm.df_defense, dm.df_team_stats,
            dm.df_standings, dm.df_teams, dm.df_players,
        ]
    )


def get_schema() -> str:
    """
    Return the cached DataFrame schema string, rebuilding it when:
      - The cache is empty (first call or explicit invalidation by bot.py)
      - The TTL has expired (default: 5 minutes)
      - All DataFrames are empty (load_all() hasn't run yet — defer caching)
    """
    global _SCHEMA_CACHE, _SCHEMA_TIMESTAMP

    cache_stale  = (time.time() - _SCHEMA_TIMESTAMP) > _SCHEMA_TTL
    data_missing = _all_dfs_empty()

    if not _SCHEMA_CACHE or cache_stale:
        if data_missing:
            return "(DataFrames not yet loaded — sync in progress)"
        _SCHEMA_CACHE     = build_schema_prompt()
        _SCHEMA_TIMESTAMP = time.time()

    return _SCHEMA_CACHE


# ═════════════════════════════════════════════════════════════════════════════
#  PRE-BUILT COMPOSITE METRICS  (injected into generated code sandbox)
# ═════════════════════════════════════════════════════════════════════════════

PREBUILT_METRICS_CODE = """
import pandas as pd
import numpy as np

def _norm(series):
    mn, mx = series.min(), series.max()
    return (series - mn) / (mx - mn + 1e-9)

# ── TEAM METRICS ──────────────────────────────────────────────────────────────

def compute_spam_scores(df_team_stats):
    \"\"\"Spam score = how pass-heavy, red-zone-hungry, turnover-prone, penalty-ridden a team is.\"\"\"
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
    \"\"\"Sim score = balanced play-calling, low penalties, low turnovers, efficient 3rd down.\"\"\"
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
    \"\"\"Cheese score = 4th-down aggression + red-zone trips + poor 3rd down + penalties.\"\"\"
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
    \"\"\"QB composite: TDs, yards, comp%, penalises INTs and sacks.\"\"\"
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
    \"\"\"RB composite: yards, TDs, broken tackles, penalises fumbles.\"\"\"
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
    \"\"\"WR/TE composite: yards, TDs, YPC, penalises drops.\"\"\"
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
    \"\"\"Most sim players by position — low spam indicators, consistent production.\"\"\"
    results = []

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


# ═════════════════════════════════════════════════════════════════════════════
#  FIX #10: SAFE BUILTINS WHITELIST FOR EXEC SANDBOX
# ═════════════════════════════════════════════════════════════════════════════
#
# Without this, Gemini-generated code has access to ALL Python builtins
# including __import__, open(), exec(), eval(), compile(), etc.
# If Gemini hallucinates `import os; os.system("rm -rf /")`, the original
# code would have executed it.
#
# This whitelist allows only safe, computation-oriented builtins.
# __import__ is explicitly excluded — the sandbox pre-loads pd and np,
# and the PREBUILT_METRICS_CODE handles its own imports internally.

_SAFE_BUILTINS = {
    # Type constructors
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "frozenset": frozenset, "bytes": bytes, "bytearray": bytearray,
    "complex": complex,
    # Iteration & comprehension
    "range": range, "enumerate": enumerate, "zip": zip,
    "map": map, "filter": filter, "reversed": reversed,
    "iter": iter, "next": next,
    # Aggregation & comparison
    "len": len, "sum": sum, "min": min, "max": max,
    "abs": abs, "round": round, "pow": pow, "divmod": divmod,
    "sorted": sorted, "any": any, "all": all,
    # Type checking (object, type, super REMOVED — sandbox escape vectors)
    "isinstance": isinstance, "issubclass": issubclass,
    "callable": callable, "hasattr": hasattr,
    # Safe getattr that blocks dunder access
    "getattr": lambda obj, name, *default: (
        getattr(obj, name, *default) if not name.startswith("_")
        else (default[0] if default else None)
    ),
    # String & repr
    "repr": repr, "format": format, "chr": chr, "ord": ord,
    "hex": hex, "oct": oct, "bin": bin, "ascii": ascii,
    # Output (safe — just prints to stdout, which we capture)
    "print": print,
    # Misc safe
    "id": id, "hash": hash,
    "staticmethod": staticmethod, "classmethod": classmethod,
    "property": property,
    # Exceptions (needed for try/except in generated code)
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "AttributeError": AttributeError,
    "ZeroDivisionError": ZeroDivisionError, "RuntimeError": RuntimeError,
    "StopIteration": StopIteration, "NotImplementedError": NotImplementedError,
}


# ═════════════════════════════════════════════════════════════════════════════
#  SANDBOXED EXECUTION
# ═════════════════════════════════════════════════════════════════════════════

def build_exec_env() -> dict:
    """
    Returns the globals dict available to generated code.
    Always takes a fresh copy of each DataFrame so sandbox mutations
    don't affect the live data_manager state.

    FIX #10: __builtins__ is now restricted to _SAFE_BUILTINS.
    This prevents generated code from calling __import__, open(),
    exec(), eval(), compile(), or any other dangerous builtin.
    """
    env = {
        "__builtins__": _SAFE_BUILTINS,   # FIX #10: restricted builtins
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
        "result":        None,   # generated code MUST assign this
    }
    # PREBUILT_METRICS_CODE uses `import pandas as pd` and `import numpy as np`
    # internally. We exec it in a SEPARATE env with FULL builtins (intentional —
    # this is our own trusted code, not LLM-generated) so those imports work,
    # then inject only the resulting functions into the restricted sandbox.
    # TRUST BOUNDARY: PREBUILT_METRICS_CODE is a hardcoded constant defined in this
    # module, not user input. Unrestricted builtins are intentional for metric computation.
    prebuilt_env = {"pd": pd, "np": np}
    exec(PREBUILT_METRICS_CODE, prebuilt_env)
    # Copy only the functions we defined (skip __builtins__, pd, np)
    for key, val in prebuilt_env.items():
        if callable(val) and not key.startswith("_") and key not in ("pd", "np"):
            env[key] = val
    # Also inject the _norm helper since metric functions reference it
    if "_norm" in prebuilt_env:
        env["_norm"] = prebuilt_env["_norm"]
    return env


def safe_exec(code: str) -> tuple[any, str]:
    """
    Execute generated code in a sandboxed env.
    Returns (result, error_message).
    result is whatever the code assigned to the `result` variable.
    """
    env = build_exec_env()
    try:
        exec(code, env)
        result = env.get("result")
        if isinstance(result, pd.DataFrame):
            result = result.head(15).to_string(index=False)
        elif isinstance(result, pd.Series):
            result = result.head(15).to_string()
        return result, ""
    except Exception:
        return None, traceback.format_exc()


# ═════════════════════════════════════════════════════════════════════════════
#  ANALYST PROMPTS
# ═════════════════════════════════════════════════════════════════════════════

ANALYST_SYSTEM = """
You are a precise Python data analyst for the TSL Madden Franchise league.

Your job: Write Python code that answers a question about TSL league stats.

RULES:
1. You have access to these DataFrames (already loaded, do NOT re-load any files):
   df_offense, df_defense, df_team_stats, df_standings, df_teams, df_players, df_games, df_trades
2. You also have these pre-built metric functions ready to call:
   - compute_spam_scores(df_team_stats)          → team spam scores
   - compute_sim_scores(df_team_stats)           → team sim scores
   - compute_cheese_scores(df_team_stats)        → team cheese scores
   - compute_power_scores(df_standings)          → team power scores
   - compute_qb_scores(df_offense)               → QB composite scores
   - compute_rb_scores(df_offense)               → RB composite scores
   - compute_wr_scores(df_offense)               → WR composite scores
   - compute_sim_players(df_offense, df_defense) → most sim players by position
3. You have pandas (pd) and numpy (np) available.
4. ALWAYS assign your final answer to a variable called `result`.
   `result` should be a DataFrame, string, list, or dict — whatever best answers the question.
5. Keep result concise — top 5-10 rows for lists, key numbers for comparisons.
6. For "who is the biggest spammer" → use compute_spam_scores, sort descending, head(5)
7. For "least sim stats" → use compute_sim_players or compute_sim_scores, sort ascending, head(5)
8. For "who would win X vs Y" → compare their powerScore, offTotalYds, defTotalYds, netPts, tODiff
9. For player comparisons → join df_offense or df_defense, compute relevant rates, compare side by side
10. For "most improved" → df_games and df_standings have seasonIndex
11. ONLY output Python code. No explanation. No markdown. No imports. Just raw executable Python.
12. If unsure, compute multiple angles and combine them into result.
13. Do NOT use import statements — all libraries (pd, np) and functions are pre-loaded.

IMPORTANT COLUMN NOTES:
- df_offense has passAtt (NOT passAtts), passCompPct is already a float
- df_defense stats like defTotalTackles are already numeric (pre-cast)
- df_team_stats has both offPassYds and offRushYds for pass ratio calculations
- df_standings winPct is a string — cast with pd.to_numeric() before math
- df_players has playerBestOvr, dev, age, contractSalary, capHit, value
- df_games uses homeTeamName/awayTeamName and homeTeamScore/awayTeamScore
"""

ANALYST_USER_TEMPLATE = """
LEAGUE DATA SCHEMAS:
{schema}

QUESTION: {question}

Write Python code to answer this. Assign the answer to `result`.
"""


# ═════════════════════════════════════════════════════════════════════════════
#  GEMINI CALL HELPER
# ═════════════════════════════════════════════════════════════════════════════

async def _call_analyst(prompt: str, temperature: float = 0.2) -> str:
    """
    Single AI call for the analyst persona via atlas_ai.
    Strips markdown code fences from the response.
    Returns raw Python code string, or empty string on failure.
    """
    import logging
    log = logging.getLogger(__name__)

    try:
        result = await atlas_ai.generate(
            prompt,
            system=ANALYST_SYSTEM,
            tier=Tier.SONNET,
            temperature=temperature,
        )
        if not result.text:
            return ""
        code = result.text.strip()
        if code.startswith("```"):
            lines = code.split("\n")
            code  = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )
        return code.strip()
    except Exception as e:
        log.error(f"[Reasoning] Analyst call failed: {e}")
        return ""


async def generate_analysis_code(question: str) -> str:
    """Phase 1: Ask AI to write Python code to answer the question."""
    prompt = ANALYST_USER_TEMPLATE.format(schema=get_schema(), question=question)
    return await _call_analyst(prompt, temperature=0.2)


# ═════════════════════════════════════════════════════════════════════════════
#  SELF-CORRECTING REASONING PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

MAX_RETRIES = 2   # hard cap: max 2 retries before graceful failure


async def reason(question: str) -> dict:
    """
    Self-correcting two-phase reasoning pipeline.

    Phase 1: Gemini writes Python code to answer the question.
    Phase 2: Execute the code. On failure, feed the full Python traceback
             back to Gemini and ask it to rewrite — up to MAX_RETRIES times.

    Returns:
        dict: {
            'success':  bool,
            'code':     str,
            'result':   str,
            'error':    str,
            'question': str,
            'attempts': int,
        }
    """
    import logging
    log = logging.getLogger(__name__)

    initial_prompt = ANALYST_USER_TEMPLATE.format(schema=get_schema(), question=question)
    code = await _call_analyst(initial_prompt, temperature=0.2)

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
        result, error = safe_exec(code)

        if not error:
            log.info(f"[Reasoning] '{question[:60]}' succeeded on attempt {attempt}.")
            return {
                "success":  True,
                "code":     code,
                "result":   str(result) if result is not None else "(no result)",
                "error":    "",
                "question": question,
                "attempts": attempt,
            }

        last_error = error
        log.warning(
            f"[Reasoning] Attempt {attempt}/{MAX_RETRIES + 1} failed for "
            f"'{question[:60]}'\nError:\n{error[:500]}"
        )

        if attempt > MAX_RETRIES:
            break

        retry_prompt = (
            f"Your previous Python code failed with the following error:\n"
            f"```\n{error}\n```\n\n"
            f"Original question: {question}\n\n"
            f"Previous (broken) code:\n"
            f"```python\n{code}\n```\n\n"
            f"Available DataFrames and columns:\n{get_schema()}\n\n"
            f"TASK: Rewrite the code to fix the error.\n"
            f"Common fixes:\n"
            f"  - KeyError → check exact column names in schema above\n"
            f"  - AttributeError → cast with pd.to_numeric() first\n"
            f"  - ValueError → handle NaN/empty DataFrames before operating\n"
            f"  - NameError → all data is already loaded; do NOT import or re-read files\n"
            f"Output ONLY raw Python code. No explanations. Assign result."
        )

        retry_temp = 0.05 * attempt
        code = await _call_analyst(retry_prompt, temperature=retry_temp)

        if not code:
            log.error(f"[Reasoning] Gemini returned no code on retry {attempt}.")
            break

    log.error(f"[Reasoning] All {MAX_RETRIES + 1} attempts failed for: '{question[:60]}'")
    return {
        "success":  False,
        "code":     code,
        "result":   "",
        "error":    last_error,
        "question": question,
        "attempts": MAX_RETRIES + 1,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

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
    # Computation-implied
    "ratio", "rate", "per game", "average", "efficiency",
    "what are the odds", "chance", "likely",
]


def should_reason(query: str) -> bool:
    """Returns True if the query should go through the reasoning pipeline."""
    q = query.lower()
    return any(trigger in q for trigger in REASONING_TRIGGERS)


def get_intent(user_input: str) -> str:
    """
    Lightweight keyword-based intent classifier.
    Used as a fast pre-filter before calling Gemini for classification.
    Returns: STATS | HISTORY | LORE | RULES | OTHER
    """
    q = user_input.lower()
    if any(k in q for k in ["how many times", "how often", "who said", "what did", "when did",
                              "discord history", "chat log", "archive", "ever say"]):
        return "HISTORY"
    if any(k in q for k in ["rule", "setting", "penalty", "allowed", "banned", "legal"]):
        return "RULES"
    if any(k in q for k in ["standings", "stats", "yards", "touchdowns", "roster", "trade",
                              "draft", "record", "season", "week"]):
        return "STATS"
    if any(k in q for k in ["beef", "rivalry", "drama", "lore", "remember"]):
        return "LORE"
    return "OTHER"


# ═════════════════════════════════════════════════════════════════════════════
#  TEXT-TO-SQL PIPELINE  (Discord History Querying)
# ═════════════════════════════════════════════════════════════════════════════

import logging as _logging

_sql_log = _logging.getLogger(__name__ + ".sql")

MAX_SQL_RETRIES = 2
MAX_SQL_ROWS    = 100
SQL_TIMEOUT_S   = 8   # seconds before query is cancelled

# ── Trigger / exclusion classification ───────────────────────────────────────

_SQL_TRIGGERS = [
    "how many times", "how often", "count how many", "how frequently",
    "number of times", "times has", "times did", "times have",
    "exact words", "exactly said", "what did", "when did", "first time",
    "last time", "ever say", "ever said", "ever called",
    "called", "said about", "talked about", "mentioned",
    "what date", "when was", "timestamp", "first message",
    "oldest message", "latest message", "archive",
    "discord history", "chat log", "message history", "server history",
    "who said", "who wrote", "who called",
]

_SQL_EXCLUSIONS = [
    "stats", "madden", "game", "season", "draft", "trade", "roster",
    "standings", "points", "touchdowns", "yards",
]


def should_sql_query(text: str) -> bool:
    """
    Returns True if the question should be answered via Text-to-SQL
    against the Discord history database.
    """
    if not dm.discord_db_exists():
        return False
    q = text.lower()
    return any(t in q for t in _SQL_TRIGGERS) and not any(e in q for e in _SQL_EXCLUSIONS)


# ── SQL security guardrails ───────────────────────────────────────────────────

_BANNED_SQL_KEYWORDS = _re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|CREATE|ALTER|ATTACH|DETACH|PRAGMA|"
    r"VACUUM|REINDEX|ANALYZE|REPLACE|UPSERT|GRANT|REVOKE|TRUNCATE|"
    r"EXEC|EXECUTE|LOAD_EXTENSION)\b",
    _re.IGNORECASE,
)

# NOTE: This regex alone doesn't prevent injection. It works together with
# _BANNED_SQL_KEYWORDS (checked in _sanitize_sql) to reject dangerous queries.
_SELECT_PATTERN    = _re.compile(r"^\s*(--[^\n]*)?\s*SELECT\b", _re.IGNORECASE | _re.DOTALL)
_ALLOWED_TABLES    = {"messages", "messages_fts"}
_TABLE_REF_PATTERN = _re.compile(
    r"\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)|\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    _re.IGNORECASE,
)


def _validate_sql(sql: str) -> tuple[bool, str]:
    """
    Validate a SQL string against security rules.
    Returns (is_safe, detail) where detail is the (possibly patched) SQL
    on success, or a reason string on failure.
    """
    sql_stripped = sql.strip()

    if not _SELECT_PATTERN.match(sql_stripped):
        return False, "Only SELECT statements are permitted."

    banned_match = _BANNED_SQL_KEYWORDS.search(sql_stripped)
    if banned_match:
        return False, f"Banned keyword detected: '{banned_match.group()}'."

    for match in _TABLE_REF_PATTERN.finditer(sql_stripped):
        table = (match.group(1) or match.group(2) or "").lower()
        if table and table not in _ALLOWED_TABLES:
            return False, f"Unauthorized table: '{table}'. Only 'messages' and 'messages_fts' allowed."

    if "LIMIT" not in sql_stripped.upper():
        sql_stripped = sql_stripped.rstrip(";") + f"\nLIMIT {MAX_SQL_ROWS};"

    return True, sql_stripped


def _sanitize_sql(sql: str) -> str:
    """Strip markdown fences and ensure a LIMIT clause is present."""
    sql = sql.strip()
    if sql.startswith("```"):
        sql = "\n".join(
            l for l in sql.split("\n")
            if not l.strip().startswith("```")
        ).strip()
    if sql.lower().startswith("sql"):
        sql = sql[3:].strip()
    if "LIMIT" not in sql.upper():
        sql = sql.rstrip(";") + f"\nLIMIT {MAX_SQL_ROWS};"
    return sql


# ── SQL generation (LLM phase) ────────────────────────────────────────────────

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


async def generate_sql(question: str) -> str:
    """Ask the LLM to write a SQLite query for the given question."""
    schema = await asyncio.get_running_loop().run_in_executor(None, dm.get_discord_db_schema)
    try:
        result = await atlas_ai.generate(
            _SQL_USER_TEMPLATE.format(question=question),
            system=_SQL_SYSTEM_PROMPT.format(schema=schema),
            tier=Tier.SONNET,
            temperature=0.05,
        )
        raw = result.text.strip() if result.text else ""
        return _sanitize_sql(raw)
    except Exception as e:
        _sql_log.error(f"[SQL] generate_sql error: {e}")
        return ""


# ── Safe SQL execution ────────────────────────────────────────────────────────

class _SQLResult:
    __slots__ = ("rows", "columns", "error", "row_count", "sql_used")

    def __init__(self):
        self.rows:      list[dict] = []
        self.columns:   list[str]  = []
        self.error:     str        = ""
        self.row_count: int        = 0
        self.sql_used:  str        = ""


def execute_sql_safe(sql: str) -> _SQLResult:
    """
    Execute a validated SQL query against the Discord history DB.
    Read-only, row-limited, and timeout-guarded.
    """
    import threading
    result          = _SQLResult()
    result.sql_used = sql

    is_safe, detail = _validate_sql(sql)
    if not is_safe:
        result.error = f"SQL security validation failed: {detail}"
        _sql_log.warning(f"[SQL] Rejected: {detail}\nSQL: {sql[:200]}")
        return result

    if is_safe and detail != sql:
        sql             = detail
        result.sql_used = sql

    try:
        conn = dm._get_discord_db(readonly=True)

        def _timeout():
            try:
                conn.interrupt()
            except Exception:
                pass

        timer = threading.Timer(SQL_TIMEOUT_S, _timeout)
        timer.start()
        try:
            cursor   = conn.execute(sql)
            columns  = [desc[0] for desc in (cursor.description or [])]
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


# ── Result formatting ─────────────────────────────────────────────────────────

def _format_sql_result(result: _SQLResult, question: str) -> str:
    """Format SQL result rows into a context string for ATLAS."""
    lines = [
        "[DISCORD ARCHIVE QUERY]",
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

    if result.row_count == 1 and result.columns and len(result.columns) == 1:
        val = list(result.rows[0].values())[0]
        lines.append(f"RESULT: {result.columns[0]} = {val}")
        return "\n".join(lines)

    lines.append("\nRESULTS:")
    for i, row in enumerate(result.rows[:20], 1):
        ts      = row.get("timestamp", "")[:16]
        author  = row.get("author", "")
        content = row.get("content", "")[:200]
        if not content and not author:
            lines.append(f"  [{i}] {dict(row)}")
        else:
            lines.append(f"  [{i}] {author} ({ts}): {content}")

    if result.row_count > 20:
        lines.append(f"  ... and {result.row_count - 20} more rows (truncated)")

    return "\n".join(lines)


# ── Full pipeline ─────────────────────────────────────────────────────────────

async def query_discord_history(question: str) -> dict:
    """
    Full Text-to-SQL pipeline with self-correction.

    Flow:
      1. Generate SQL from natural language
      2. Validate and execute
      3. On error: feed full error + broken SQL back to LLM, retry (MAX_SQL_RETRIES)
      4. Format results as LLM-ready context string

    Returns:
        dict: { 'success', 'context', 'sql', 'rows', 'error', 'attempts' }
    """
    sql = await generate_sql(question)
    if not sql:
        return {
            "success": False,
            "context": "[DISCORD ARCHIVE QUERY]\nERROR: LLM returned no SQL.",
            "sql":     "",
            "rows":    [],
            "error":   "No SQL generated.",
            "attempts": 0,
        }

    last_error = ""
    for attempt in range(1, MAX_SQL_RETRIES + 2):
        result = await asyncio.get_running_loop().run_in_executor(None, execute_sql_safe, sql)

        if not result.error:
            context = _format_sql_result(result, question)
            _sql_log.info(
                f"[SQL] Success on attempt {attempt}: {result.row_count} rows | "
                f"Q: '{question[:60]}'"
            )
            return {
                "success":  True,
                "context":  context,
                "sql":      sql,
                "rows":     result.rows,
                "error":    "",
                "attempts": attempt,
            }

        last_error = result.error
        _sql_log.warning(f"[SQL] Attempt {attempt}/{MAX_SQL_RETRIES + 1} failed: {last_error}")

        if attempt > MAX_SQL_RETRIES:
            break

        schema     = await asyncio.get_running_loop().run_in_executor(None, dm.get_discord_db_schema)
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
            fix_result = await atlas_ai.generate(
                fix_prompt,
                system=_SQL_SYSTEM_PROMPT.format(schema=schema),
                tier=Tier.SONNET,
                temperature=0.02,
            )
            if fix_result.text:
                sql = _sanitize_sql(fix_result.text.strip())
                _sql_log.info(f"[SQL] Retry {attempt} rewritten SQL: {sql[:150]}")
            else:
                _sql_log.error(f"[SQL] LLM returned no SQL on retry {attempt}.")
                break
        except Exception as e:
            _sql_log.error(f"[SQL] Retry {attempt} error: {e}")
            break

    _sql_log.error(f"[SQL] All {MAX_SQL_RETRIES + 1} attempts failed for: '{question[:60]}'")
    return {
        "success":  False,
        "context":  (
            f"[DISCORD ARCHIVE QUERY]\n"
            f"ERROR after {MAX_SQL_RETRIES + 1} attempts: {last_error}\n"
            "(Couldn't retrieve data — roast them based on what you know.)"
        ),
        "sql":      sql,
        "rows":     [],
        "error":    last_error,
        "attempts": MAX_SQL_RETRIES + 1,
    }
