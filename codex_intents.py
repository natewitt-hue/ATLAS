"""
codex_intents.py — Intent Detection Layer for ATLAS Codex
─────────────────────────────────────────────────────────────────────────────
Three-tier query pipeline that intercepts known question patterns with
deterministic SQL before falling through to Gemini NL→SQL.

Tier 1: Regex pre-flight (instant, 100% reliable)
Tier 2: Gemini structured classification (flexible, ~1s)
Tier 3: Existing gemini_sql() pipeline (unchanged fallback)

Public API:
  detect_intent(question, caller_db, resolved_names, gemini_client) → IntentResult
  get_h2h_sql_and_params(u1, u2) → (sql, params)
  check_self_reference_collision(caller_db, resolved_names) → str | None
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

try:
    import data_manager as dm
except ImportError:
    dm = None

# Lazy import of fuzzy_resolve_user to avoid circular imports
_fuzzy_resolve = None

def _get_fuzzy_resolver():
    global _fuzzy_resolve
    if _fuzzy_resolve is None:
        try:
            from codex_cog import fuzzy_resolve_user
            _fuzzy_resolve = fuzzy_resolve_user
        except ImportError:
            _fuzzy_resolve = lambda x: None
    return _fuzzy_resolve


def _resolve_name(name: str, resolved_names: dict[str, str]) -> str | None:
    """
    Resolve a name from a regex capture group to a db_username.
    Checks the pre-built alias_map first, then falls back to fuzzy_resolve_user
    (which handles short nicknames like 'JT' with no length gate).
    """
    if not name:
        return None
    # Check alias_map (from resolve_names_in_question)
    result = resolved_names.get(name)
    if result:
        return result
    # Fallback to fuzzy resolver (handles short nicknames)
    resolver = _get_fuzzy_resolver()
    return resolver(name)


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class IntentResult:
    """Result of intent detection."""
    intent: str              # e.g. "h2h_record", "season_record", "unknown"
    sql: str | None = None   # Parameterized SQL template
    params: tuple | None = None  # Values for ? placeholders
    tier: int = 3            # 1=regex, 2=gemini classification, 3=fallthrough
    meta: dict = field(default_factory=dict)  # Extra info for answer formatting


# ── Helpers ──────────────────────────────────────────────────────────────────

def _current_season() -> int:
    return dm.CURRENT_SEASON if dm else 6


def _extract_season(text: str) -> int | None:
    """Extract season number from text, or None if not specified."""
    m = re.search(r'(?:season|s)\s*(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.search(r'\bthis\s+season\b', text, re.IGNORECASE):
        return _current_season()
    return None


def _extract_limit(text: str, default: int = 10) -> int:
    """Extract a numeric limit like 'top 5' or 'last 3'."""
    m = re.search(r'(?:top|last|recent)\s+(\d+)', text, re.IGNORECASE)
    return int(m.group(1)) if m else default


# ── Self-reference collision ─────────────────────────────────────────────────

def check_self_reference_collision(
    caller_db: str | None,
    resolved_names: dict[str, str],
) -> str | None:
    """
    Check if "my"/"me" and a name in the question both resolve to the same person.
    Returns a user-facing error message if collision detected, None otherwise.
    """
    if not caller_db or not resolved_names:
        return None
    for nickname, db_user in resolved_names.items():
        if db_user == caller_db:
            return (
                f"It looks like **{nickname}** resolves to your own account "
                f"(**{caller_db}**). Did you mean a different opponent?"
            )
    return None


# ── Shared H2H SQL ───────────────────────────────────────────────────────────

def get_h2h_sql_and_params(
    u1: str, u2: str, season: int | None = None
) -> tuple[str, tuple]:
    """
    Deterministic H2H SQL — single source of truth.
    Used by codex_cog._h2h_impl, oracle_cog.H2HModal, and intent detection.
    """
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
    """
    params = [u1, u2, u1, u2, u2, u1]

    if season is not None:
        sql += "  AND seasonIndex = ?\n"
        params.append(str(season))

    sql += """
        GROUP BY seasonIndex
        ORDER BY CAST(seasonIndex AS INTEGER)
    """
    return sql, tuple(params)


# ── Intent registry ──────────────────────────────────────────────────────────

# Each intent: (name, compiled_patterns, build_fn)
# build_fn(match, caller_db, question, resolved_names) → IntentResult | None
_INTENT_REGISTRY: list[tuple[str, list[re.Pattern], callable]] = []


def _register(name: str, patterns: list[str]):
    """Decorator to register an intent with regex patterns."""
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    def decorator(fn):
        _INTENT_REGISTRY.append((name, compiled, fn))
        return fn
    return decorator


# ── Intent 1: Head-to-Head Record ────────────────────────────────────────────

@_register("h2h_record", [
    # "my record vs JT", "record against Killa"
    r'\b(?:record|h2h|head[\s-]?to[\s-]?head)\s+(?:vs\.?|against|with|versus)\s+(\S+)',
    # "how do I stack up against JT", "how have I done vs Killa"
    r'\bhow\s+(?:do|does|did|have|has)\s+\w+\s+(?:do|done|fare|fared|stack\s+up)\s+(?:vs\.?|against|versus)\s+(\S+)',
    # "X vs Y" or "X versus Y" (two explicit names)
    r'\b(\S+)\s+(?:vs\.?|versus)\s+(\S+)(?:\s+record|\s+h2h|\s+head)?\b',
])
def _build_h2h(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if len(groups) >= 2:
        owner1, owner2 = groups[0], groups[1]
    elif len(groups) == 1:
        owner1, owner2 = caller_db, groups[0]
    else:
        return None

    if not owner1 or not owner2:
        return None

    # Resolve through alias map + fuzzy resolver (handles short nicknames like "JT")
    owner1 = _resolve_name(owner1, resolved_names) or owner1
    owner2 = _resolve_name(owner2, resolved_names) or owner2

    season = _extract_season(question)
    sql, params = get_h2h_sql_and_params(owner1, owner2, season)
    return IntentResult(
        intent="h2h_record", sql=sql, params=params, tier=1,
        meta={"owner1": owner1, "owner2": owner2, "type": "rivalry"}
    )


# ── Intent 2: Season Record ─────────────────────────────────────────────────

@_register("season_record", [
    # "my record this season", "Witt's record in season 5"
    r"\b(?:my|(\S+?)(?:'s)?)\s+record\s+(?:this|in|for)?\s*(?:season|s)\s*(\d+)?",
    # "my wins and losses this season"
    r"\b(?:my|(\S+?)(?:'s)?)\s+wins?\s+(?:and\s+)?loss(?:es)?\s*(?:this\s+season)?",
    # "how am I doing this season", "how is Witt doing"
    r'\bhow\s+(?:am\s+i|is\s+(\S+))\s+doing\s*(?:this\s+season)?',
])
def _build_season_record(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    owner = None
    for g in groups:
        if g and not g.isdigit():
            owner = _resolve_name(g, resolved_names) or g
            break
    if not owner:
        owner = caller_db
    if not owner:
        return None

    season = _extract_season(question) or _current_season()

    sql = """
        SELECT
            SUM(CASE WHEN winner_user = ? THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN loser_user = ? THEN 1 ELSE 0 END) AS losses,
            COUNT(*) AS games_played
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND seasonIndex = ?
          AND (homeUser = ? OR awayUser = ?)
    """
    params = (owner, owner, str(season), owner, owner)
    return IntentResult(
        intent="season_record", sql=sql, params=params, tier=1,
        meta={"owner": owner, "season": season, "type": "record"}
    )


# ── Intent 3: All-Time Record ───────────────────────────────────────────────

@_register("alltime_record", [
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:all[\s-]?time|lifetime|career|overall)\s+record",
    r"\b(?:my|(\S+?)(?:'s)?)\s+record\s+(?:all[\s-]?time|overall|total|ever)",
    r'\bhow\s+many\s+(?:total\s+)?(?:wins?|games?)\s+(?:do(?:es)?|have|has)\s+(?:i|(\S+))\s+have',
])
def _build_alltime_record(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    owner = None
    for g in groups:
        if g:
            owner = _resolve_name(g, resolved_names) or g
            break
    if not owner:
        owner = caller_db
    if not owner:
        return None

    sql = """
        SELECT
            SUM(CASE WHEN winner_user = ? THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN loser_user = ? THEN 1 ELSE 0 END) AS losses,
            COUNT(*) AS games_played
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND (homeUser = ? OR awayUser = ?)
    """
    params = (owner, owner, owner, owner)
    return IntentResult(
        intent="alltime_record", sql=sql, params=params, tier=1,
        meta={"owner": owner, "type": "record"}
    )


# ── Intent 4: Leaderboard ───────────────────────────────────────────────────

@_register("leaderboard", [
    r'\b(?:who|which\s+owner)\s+(?:has|have)\s+(?:the\s+)?most\s+(wins?|losses|games|championships?)',
    r'\btop\s+(\d+)?\s*(rushers?|passers?|receivers?|tacklers?)',
    r'\b(?:leading|best|top)\s+(passers?|rushers?|receivers?|scorers?)',
    r'\bleaderboard\s+(?:for\s+)?(passing|rushing|receiving|tackles|sacks|interceptions)',
    r'\bwinningest\s+(?:owners?|coaches?|players?)',
])
def _build_leaderboard(match, caller_db, question, resolved_names):
    text_lower = question.lower()
    season = _extract_season(question)
    limit = _extract_limit(question, default=10)

    # Determine if this is an owner wins leaderboard or player stat leaderboard
    if any(kw in text_lower for kw in ['wins', 'winningest', 'losses', 'championships']):
        # Owner wins leaderboard
        sql = """
            SELECT winner_user AS owner, COUNT(*) AS total_wins
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND winner_user IS NOT NULL AND winner_user != ''
        """
        params_list = []
        if season:
            sql += " AND seasonIndex = ?"
            params_list.append(str(season))
        sql += " GROUP BY winner_user ORDER BY total_wins DESC LIMIT ?"
        params_list.append(limit)
        return IntentResult(
            intent="leaderboard", sql=sql, params=tuple(params_list), tier=1,
            meta={"type": "leaderboard", "stat": "wins"}
        )

    # Player stat leaderboard
    stat_map = {
        'pass': ('passYds', 'passTDs', 'offensive_stats', 'QB'),
        'rush': ('rushYds', 'rushTDs', 'offensive_stats', 'HB'),
        'receiv': ('recYds', 'recTDs', 'offensive_stats', None),
        'tackl': ('defTotalTackles', None, 'defensive_stats', None),
        'sack': ('defSacks', None, 'defensive_stats', None),
        'intercept': ('defInts', None, 'defensive_stats', None),
    }

    stat_key = None
    for key in stat_map:
        if key in text_lower:
            stat_key = key
            break

    if not stat_key:
        return None  # Fall through to Tier 2/3

    primary_col, secondary_col, table, pos_filter = stat_map[stat_key]
    select_cols = f"SUM(CAST({primary_col} AS INTEGER)) AS total_stat"
    if secondary_col:
        select_cols += f", SUM(CAST({secondary_col} AS INTEGER)) AS total_secondary"

    sql = f"""
        SELECT fullName, teamName, {select_cols}
        FROM {table}
        WHERE stageIndex = '1'
    """
    params_list = []
    if pos_filter:
        sql += f" AND pos = ?"
        params_list.append(pos_filter)
    if season:
        sql += " AND seasonIndex = ?"
        params_list.append(str(season))
    sql += f" GROUP BY fullName ORDER BY total_stat DESC LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="leaderboard", sql=sql, params=tuple(params_list), tier=1,
        meta={"type": "leaderboard", "stat": stat_key}
    )


# ── Intent 5: Recent Games ──────────────────────────────────────────────────

@_register("recent_games", [
    r"\b(?:my|(\S+?)(?:'s)?)\s+last\s+(\d+)\s+games?",
    r"\b(?:my|(\S+?)(?:'s)?)\s+recent\s+(?:games?|results?|matchups?)",
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:last|most\s+recent)\s+game\b",
])
def _build_recent_games(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    owner = None
    count = 5  # default
    for g in groups:
        if g and g.isdigit():
            count = int(g)
        elif g:
            owner = _resolve_name(g, resolved_names) or g
    if not owner:
        owner = caller_db
    if not owner:
        return None

    sql = """
        SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
               homeScore, awayScore, homeUser, awayUser,
               winner_user, loser_user
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND (homeUser = ? OR awayUser = ?)
        ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
        LIMIT ?
    """
    params = (owner, owner, count)
    return IntentResult(
        intent="recent_games", sql=sql, params=params, tier=1,
        meta={"owner": owner, "count": count, "type": "game_log"}
    )


# ── Intent 6: Streak ────────────────────────────────────────────────────────

@_register("streak", [
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:current\s+)?(?:win(?:ning)?|los(?:s|ing))\s+streak",
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:current\s+)?streak\b",
    r'\b(?:am\s+i|is\s+(\S+))\s+on\s+a\s+(?:win|los)',
])
def _build_streak(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    owner = None
    for g in groups:
        if g:
            owner = _resolve_name(g, resolved_names) or g
            break
    if not owner:
        owner = caller_db
    if not owner:
        return None

    sql = """
        SELECT seasonIndex, weekIndex, winner_user, loser_user,
               homeTeamName, awayTeamName, homeScore, awayScore
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND (homeUser = ? OR awayUser = ?)
        ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
        LIMIT 20
    """
    params = (owner, owner)
    return IntentResult(
        intent="streak", sql=sql, params=params, tier=1,
        meta={"owner": owner, "type": "streak", "compute_in_python": True}
    )


# ── Intent 7: Team Record ───────────────────────────────────────────────────

_TEAM_ALIASES = {
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


@_register("team_record", [
    r'\bhow\s+(?:are|is)\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+doing',
    r'\b(?:the\s+)?(\w+(?:\s+\w+)?)\s+record\s+(?:this|in)?\s*(?:season|s)\s*(\d+)?',
])
def _build_team_record(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if not groups:
        return None

    team_input = groups[0].strip().lower()
    team_name = _TEAM_ALIASES.get(team_input)
    if not team_name:
        # Try partial match
        for alias, name in _TEAM_ALIASES.items():
            if team_input in alias or alias in team_input:
                team_name = name
                break
    if not team_name:
        return None  # Not a recognized team — fall through

    season = _extract_season(question) or _current_season()

    sql = """
        SELECT
            SUM(CASE WHEN winner_team = ? THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN loser_team = ? THEN 1 ELSE 0 END) AS losses
        FROM games
        WHERE status IN ('2','3')
          AND stageIndex = '1'
          AND seasonIndex = ?
          AND (homeTeamName = ? OR awayTeamName = ?)
    """
    params = (team_name, team_name, str(season), team_name, team_name)
    return IntentResult(
        intent="team_record", sql=sql, params=params, tier=1,
        meta={"team": team_name, "season": season, "type": "record"}
    )


# ── Intent 8: Draft History ─────────────────────────────────────────────────

@_register("draft_history", [
    r'\b(?:who\s+did\s+)?(?:the\s+)?(\w+)\s+draft\b',
    r"\b(\w+)(?:'s)?\s+draft\s+(?:picks?|history|class)",
])
def _build_draft_history(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if not groups:
        return None

    team_input = groups[0].strip().lower()
    team_name = _TEAM_ALIASES.get(team_input)
    if not team_name:
        for alias, name in _TEAM_ALIASES.items():
            if team_input in alias or alias in team_input:
                team_name = name
                break
    if not team_name:
        return None

    season = _extract_season(question)

    sql = """
        SELECT extendedName, drafting_team, drafting_season,
               draftRound, draftPick, pos, playerBestOvr, dev, was_traded
        FROM player_draft_map
        WHERE drafting_team LIKE ?
    """
    params_list = [f"%{team_name}%"]
    if season:
        sql += " AND drafting_season = ?"
        params_list.append(str(season))
    sql += " ORDER BY CAST(draftRound AS INTEGER), CAST(draftPick AS INTEGER)"

    return IntentResult(
        intent="draft_history", sql=sql, params=tuple(params_list), tier=1,
        meta={"team": team_name, "type": "draft_class"}
    )


# ── Tier 1: Regex Pre-flight ────────────────────────────────────────────────

def _match_regex(
    question: str,
    caller_db: str | None,
    resolved_names: dict[str, str],
) -> IntentResult | None:
    """Try all regex patterns in priority order. Return first match or None."""
    for _name, patterns, build_fn in _INTENT_REGISTRY:
        for pattern in patterns:
            m = pattern.search(question)
            if m:
                result = build_fn(m, caller_db, question, resolved_names)
                if result:
                    return result
    return None


# ── Tier 2: Gemini Structured Classification ─────────────────────────────────

_CLASSIFICATION_PROMPT = """You are an intent classifier for a Madden NFL sim league database.

Given a user question, classify it into one of these intents and extract parameters as JSON.

INTENTS:
1. h2h_record — Head-to-head record between two owners
2. season_record — An owner's win/loss record in a specific season
3. alltime_record — An owner's all-time/career/lifetime win/loss record
4. leaderboard — Rankings (who has the most wins, top rushers, leading passers, etc.)
5. recent_games — An owner's last N games or most recent results
6. streak — An owner's current winning or losing streak
7. team_record — A team's record (by team name, not owner)
8. draft_history — Draft picks for a team, season, or round
9. unknown — Question doesn't fit any of the above

CONTEXT:
- The person asking has db_username: '{caller_db}'
- "my", "me", "I" refer to '{caller_db}'
- Current season: {current_season}
- When no season is specified for season_record, use current season ({current_season})

Respond with ONLY valid JSON, no explanation:
{{"intent": "<intent_name>", "params": {{}}, "confidence": <0.0 to 1.0>}}

Parameter schemas by intent:
- h2h_record: {{"owner1": str, "owner2": str, "season": int|null}}
- season_record: {{"owner": str, "season": int}}
- alltime_record: {{"owner": str}}
- leaderboard: {{"stat_type": str, "limit": int, "season": int|null}}
- recent_games: {{"owner": str, "count": int}}
- streak: {{"owner": str}}
- team_record: {{"team_name": str, "season": int|null}}
- draft_history: {{"team": str|null, "season": int|null}}
- unknown: {{}}

User question: "{question}"
"""


async def _classify_gemini(
    question: str,
    caller_db: str | None,
    resolved_names: dict[str, str],
    gemini_client,
) -> IntentResult:
    """Tier 2: Ask Gemini to classify intent + extract params as JSON."""
    prompt = _CLASSIFICATION_PROMPT.format(
        caller_db=caller_db or "unknown",
        current_season=_current_season(),
        question=question,
    )

    try:
        loop = asyncio.get_running_loop()
        def _call():
            return gemini_client.models.generate_content(
                model="gemini-2.0-flash", contents=prompt
            )
        response = await loop.run_in_executor(None, _call)
        text = response.text.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        data = json.loads(text)
        intent = data.get("intent", "unknown")
        confidence = float(data.get("confidence", 0))
        params = data.get("params", {})

        if confidence < 0.7 or intent == "unknown":
            return IntentResult(intent="unknown", tier=3)

        # Build IntentResult from classified intent
        return _build_from_classification(intent, params, caller_db, resolved_names)

    except Exception:
        return IntentResult(intent="unknown", tier=3)


def _build_from_classification(
    intent: str,
    params: dict,
    caller_db: str | None,
    resolved_names: dict[str, str],
) -> IntentResult:
    """Build IntentResult from Gemini classification output."""

    if intent == "h2h_record":
        o1 = params.get("owner1", caller_db)
        o2 = params.get("owner2")
        if not o1 or not o2:
            return IntentResult(intent="unknown", tier=3)
        sql, sql_params = get_h2h_sql_and_params(o1, o2, params.get("season"))
        return IntentResult(
            intent="h2h_record", sql=sql, params=sql_params, tier=2,
            meta={"owner1": o1, "owner2": o2, "type": "rivalry"}
        )

    if intent == "season_record":
        owner = params.get("owner", caller_db)
        season = params.get("season", _current_season())
        if not owner:
            return IntentResult(intent="unknown", tier=3)
        sql = """
            SELECT
                SUM(CASE WHEN winner_user = ? THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN loser_user = ? THEN 1 ELSE 0 END) AS losses,
                COUNT(*) AS games_played
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND seasonIndex = ? AND (homeUser = ? OR awayUser = ?)
        """
        return IntentResult(
            intent="season_record", sql=sql,
            params=(owner, owner, str(season), owner, owner),
            tier=2, meta={"owner": owner, "season": season, "type": "record"}
        )

    if intent == "alltime_record":
        owner = params.get("owner", caller_db)
        if not owner:
            return IntentResult(intent="unknown", tier=3)
        sql = """
            SELECT
                SUM(CASE WHEN winner_user = ? THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN loser_user = ? THEN 1 ELSE 0 END) AS losses,
                COUNT(*) AS games_played
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND (homeUser = ? OR awayUser = ?)
        """
        return IntentResult(
            intent="alltime_record", sql=sql,
            params=(owner, owner, owner, owner),
            tier=2, meta={"owner": owner, "type": "record"}
        )

    if intent == "recent_games":
        owner = params.get("owner", caller_db)
        count = params.get("count", 5)
        if not owner:
            return IntentResult(intent="unknown", tier=3)
        sql = """
            SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
                   homeScore, awayScore, homeUser, awayUser, winner_user, loser_user
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND (homeUser = ? OR awayUser = ?)
            ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
            LIMIT ?
        """
        return IntentResult(
            intent="recent_games", sql=sql,
            params=(owner, owner, count), tier=2,
            meta={"owner": owner, "count": count, "type": "game_log"}
        )

    if intent == "streak":
        owner = params.get("owner", caller_db)
        if not owner:
            return IntentResult(intent="unknown", tier=3)
        sql = """
            SELECT seasonIndex, weekIndex, winner_user, loser_user,
                   homeTeamName, awayTeamName, homeScore, awayScore
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND (homeUser = ? OR awayUser = ?)
            ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
            LIMIT 20
        """
        return IntentResult(
            intent="streak", sql=sql, params=(owner, owner), tier=2,
            meta={"owner": owner, "type": "streak", "compute_in_python": True}
        )

    # For leaderboard, team_record, draft_history — fall through to Tier 3
    # (too many sub-variants for reliable Gemini param extraction)
    return IntentResult(intent="unknown", tier=3)


# ── Public API ───────────────────────────────────────────────────────────────

async def detect_intent(
    question: str,
    caller_db: str | None,
    resolved_names: dict[str, str] | None = None,
    gemini_client=None,
) -> IntentResult:
    """
    Three-tier intent detection.

    Tier 1: Regex pre-flight (instant)
    Tier 2: Gemini structured classification (if regex misses + client available)
    Tier 3: Returns IntentResult(tier=3) → caller uses existing gemini_sql() pipeline
    """
    resolved = resolved_names or {}

    # Tier 1: Regex
    result = _match_regex(question, caller_db, resolved)
    if result:
        return result

    # Tier 2: Gemini classification
    if gemini_client:
        result = await _classify_gemini(question, caller_db, resolved, gemini_client)
        if result.tier < 3:
            return result

    # Tier 3: Fallthrough
    return IntentResult(intent="unknown", tier=3)
