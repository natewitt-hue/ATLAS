"""
codex_intents.py — Intent Detection Layer for ATLAS Codex
─────────────────────────────────────────────────────────────────────────────
Three-tier query pipeline that intercepts known question patterns with
deterministic SQL before falling through to AI-powered NL→SQL.

Tier 1: Regex pre-flight (instant, 100% reliable)
Tier 2: AI structured classification (flexible, ~1s)
Tier 3: Existing gemini_sql() pipeline (unchanged fallback)

Public API:
  detect_intent(question, caller_db, resolved_names) → IntentResult
  get_h2h_sql_and_params(u1, u2) → (sql, params)
  check_self_reference_collision(caller_db, resolved_names) → str | None
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import atlas_ai
from atlas_ai import Tier

log = logging.getLogger("codex_intents")

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
    # Word-number seasons: "season three" → 3
    m = re.search(
        r'(?:season)\s+(one|two|three|four|five|six|seven|eight|nine|ten)',
        text, re.IGNORECASE,
    )
    if m:
        return _WORD_NUMS.get(m.group(1).lower())
    if re.search(r'\bthis\s+season\b|\bcurrent\s+season\b', text, re.IGNORECASE):
        return _current_season()
    if re.search(r'\blast\s+season\b|\bprevious\s+season\b', text, re.IGNORECASE):
        return _current_season() - 1
    return None


_WORD_NUMS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'fifteen': 15, 'twenty': 20,
}


def _extract_limit(text: str, default: int = 10) -> int:
    """Extract a numeric limit like 'top 5' or 'last three'."""
    m = re.search(r'(?:top|last|recent)\s+(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(
        r'(?:top|last|recent)\s+(one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty)',
        text, re.IGNORECASE,
    )
    if m:
        return _WORD_NUMS.get(m.group(1).lower(), default)
    return default


def _normalize_question(text: str) -> str:
    """Preprocess question text: expand contractions, strip possessives before keywords."""
    text = re.sub(r"\bwhat's\b", "what is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwho's\b", "who is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhow's\b", "how is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwhere's\b", "where is", text, flags=re.IGNORECASE)
    # Strip possessives before known intent keywords (Lions' record → Lions record)
    text = re.sub(
        r"(\w+)'s?\s+(record|draft|stats?|games?|streak|roster|abilities|x-factor|trades?|wins?|losses?|history|offense|defense|players?|picks?|team|schedule)",
        r"\1 \2", text, flags=re.IGNORECASE,
    )
    return text


def _resolve_team(text: str) -> str | None:
    """Resolve a text fragment to a canonical team name via _TEAM_ALIASES.
    _TEAM_ALIASES is defined below, before the team-based intents.
    Uses exact key match only — no substring matching to avoid false positives
    like 'was' → Commanders or 'no' → Saints."""
    team_input = text.strip().lower()
    return _TEAM_ALIASES.get(team_input)


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


# ── Stat Registry (extensible stat keyword → SQL mapping) ─────────────────

STAT_REGISTRY = {
    # Passing (sorted longest-first for matching)
    'passing touchdowns': ('offensive_stats', 'passTDs', 'SUM', 'QB'),
    'passing yards': ('offensive_stats', 'passYds', 'SUM', 'QB'),
    'passing tds': ('offensive_stats', 'passTDs', 'SUM', 'QB'),
    'pass tds': ('offensive_stats', 'passTDs', 'SUM', 'QB'),
    'pass yards': ('offensive_stats', 'passYds', 'SUM', 'QB'),
    'interceptions thrown': ('offensive_stats', 'passInts', 'SUM', 'QB'),
    'passer rating': ('offensive_stats', 'passerRating', 'AVG', 'QB'),
    'completion percentage': ('offensive_stats', 'passCompPct', 'AVG', 'QB'),
    'completions': ('offensive_stats', 'passComp', 'SUM', 'QB'),
    # Rushing
    'rushing touchdowns': ('offensive_stats', 'rushTDs', 'SUM', None),
    'rushing yards': ('offensive_stats', 'rushYds', 'SUM', None),
    'rushing tds': ('offensive_stats', 'rushTDs', 'SUM', None),
    'rush yards': ('offensive_stats', 'rushYds', 'SUM', None),
    'rush tds': ('offensive_stats', 'rushTDs', 'SUM', None),
    'fumbles': ('offensive_stats', 'rushFum', 'SUM', None),
    # Receiving
    'receiving touchdowns': ('offensive_stats', 'recTDs', 'SUM', None),
    'receiving yards': ('offensive_stats', 'recYds', 'SUM', None),
    'receiving tds': ('offensive_stats', 'recTDs', 'SUM', None),
    'receptions': ('offensive_stats', 'recCatches', 'SUM', None),
    'catches': ('offensive_stats', 'recCatches', 'SUM', None),
    'drops': ('offensive_stats', 'recDrops', 'SUM', None),
    'yards after catch': ('offensive_stats', 'recYdsAfterCatch', 'SUM', None),
    # Defense
    'forced fumbles': ('defensive_stats', 'defForcedFum', 'SUM', None),
    'fumble recoveries': ('defensive_stats', 'defFumRec', 'SUM', None),
    'defensive tds': ('defensive_stats', 'defTDs', 'SUM', None),
    'defensive touchdowns': ('defensive_stats', 'defTDs', 'SUM', None),
    'pass deflections': ('defensive_stats', 'defDeflections', 'SUM', None),
    'deflections': ('defensive_stats', 'defDeflections', 'SUM', None),
    'tackles': ('defensive_stats', 'defTotalTackles', 'SUM', None),
    'sacks': ('defensive_stats', 'defSacks', 'SUM', None),
    'interceptions': ('defensive_stats', 'defInts', 'SUM', None),
}

# Pre-sorted keys by length (longest first) for correct matching
_STAT_KEYS_SORTED = sorted(STAT_REGISTRY.keys(), key=len, reverse=True)


def _lookup_stat(text: str):
    """Find the best matching stat in STAT_REGISTRY (longest match first)."""
    text_lower = text.lower()
    for key in _STAT_KEYS_SORTED:
        if key in text_lower:
            return key, STAT_REGISTRY[key]
    return None, None


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
    # "Chokolate_Thunda's record vs MeLLoW_FiRe", "Witt's record vs JT"
    r"\b(\S+?)(?:'s)?\s+record\s+(?:vs\.?|against|with|versus)\s+(\S+)",
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

    # "my" / "i" → use caller identity
    if owner1.lower() in ('my', 'i', 'me'):
        owner1 = caller_db
    if owner2.lower() in ('my', 'i', 'me'):
        owner2 = caller_db

    # Reject common English words that aren't owner names
    _STOP_WORDS = {'games', 'game', 'record', 'the', 'what', 'is', 'last', 'recent',
                   'this', 'that', 'season', 'all', 'time', 'how', 'did', 'does', 'do',
                   'score', 'scores', 'result', 'results'}
    if owner1.lower() in _STOP_WORDS or owner2.lower() in _STOP_WORDS:
        return None  # Not a real H2H — fall through to other intents

    # If either name is a team name, this is a game_score query, not H2H
    if _resolve_team(owner1) or _resolve_team(owner2):
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
    r"\b(?:my|(\S+?)(?:'s)?)\s+record\s+(?:this|in|for|last|previous)?\s*(?:season|s)\s*(\d+)?",
    # "my season record", "Witt's season 5 record" (reversed word order)
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:last\s+|previous\s+)?season\s*(\d+)?\s+record",
    # "my wins and losses this season"
    r"\b(?:my|(\S+?)(?:'s)?)\s+wins?\s+(?:and\s+)?loss(?:es)?\s*(?:this\s+season)?",
    # "how am I doing this season", "how is Witt doing"
    r'\bhow\s+(?:am\s+i|is\s+(\S+))\s+doing\s*(?:this\s+season)?',
    # "how many wins do I have this season"
    r'\bhow\s+many\s+(?:wins?|losses?|games?)\s+(?:do(?:es)?|have|has)\s+(?:i|(\S+))\s+have\s+(?:this|in|for|last)\s+season',
])
def _build_season_record(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    owner = None
    for g in groups:
        if g and not g.isdigit():
            # If the captured name is a team name, fall through to team_record
            if _resolve_team(g):
                return None
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
    # "how many games have I won", "how many wins does Witt have" (no season qualifier)
    r'\bhow\s+many\s+(?:total\s+)?(?:wins?|games?|losses?)\s+(?:do(?:es)?|have|has|did)\s+(?:i|(\S+))\s+(?:have|played|won|lost)\b',
    # "my record" (bare, no season qualifier → all-time)
    r'\b(?:my)\s+record\b(?!\s+(?:this|last|in|season|vs|against|versus))',
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
    r'\b(?:who|which\s+owner)\s+(?:has|have)\s+(?:the\s+)?(?:most|fewest|least)\s+(wins?|losses|games|championships?)',
    r'\btop\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty)?\s*(rushers?|passers?|receivers?|tacklers?)',
    r'\b(?:leading|best|top|worst|bottom)\s+(?:\d+\s+)?(passers?|rushers?|receivers?|scorers?|owners?)',
    r'\bleaderboard\s+(?:for\s+)?(passing|rushing|receiving|tackles|sacks|interceptions)',
    r'\bwinningest\s+(?:owners?|coaches?|players?)',
    # "worst owner", "best owner this season"
    r'\b(?:worst|best)\s+owner',
    # "owner with the most/least losses"
    r'\bowner\s+with\s+(?:the\s+)?(?:most|fewest|least)\s+(wins?|losses)',
])
def _build_leaderboard(match, caller_db, question, resolved_names):
    text_lower = question.lower()
    season = _extract_season(question)
    limit = _extract_limit(question, default=10)

    # Determine if this is an owner wins/losses leaderboard or player stat leaderboard
    is_owner_query = any(kw in text_lower for kw in ['wins', 'winningest', 'championships', 'owner'])
    sort_worst = any(kw in text_lower for kw in ['worst', 'bottom', 'fewest', 'least', 'lowest'])
    sort_dir = 'ASC' if sort_worst else 'DESC'

    if is_owner_query and not ('loss' in text_lower):
        # Owner wins leaderboard — "worst owner" sorts ASC (fewest wins)
        sql = f"""
            SELECT winner_user AS owner, COUNT(*) AS total_wins
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND winner_user IS NOT NULL AND winner_user != ''
        """
        params_list = []
        if season:
            sql += " AND seasonIndex = ?"
            params_list.append(str(season))
        sql += f" GROUP BY winner_user ORDER BY total_wins {sort_dir} LIMIT ?"
        params_list.append(limit)
        return IntentResult(
            intent="leaderboard", sql=sql, params=tuple(params_list), tier=1,
            meta={"type": "leaderboard", "stat": "wins", "sort": sort_dir.lower()}
        )

    if 'loss' in text_lower:
        # Owner losses leaderboard
        sql = """
            SELECT loser_user AS owner, COUNT(*) AS total_losses
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND loser_user IS NOT NULL AND loser_user != ''
        """
        params_list = []
        if season:
            sql += " AND seasonIndex = ?"
            params_list.append(str(season))
        sql += f" GROUP BY loser_user ORDER BY total_losses {sort_dir} LIMIT ?"
        params_list.append(limit)
        return IntentResult(
            intent="leaderboard", sql=sql, params=tuple(params_list), tier=1,
            meta={"type": "leaderboard", "stat": "losses", "sort": sort_dir.lower()}
        )

    # Player stat leaderboard
    # (primary_col, secondary_col, table, pos_filter, worst_col)
    # worst_col: efficiency metric to use for "worst" queries (None = use primary)
    stat_map = {
        'pass': ('passYds', 'passTDs', 'offensive_stats', 'QB', 'passerRating'),
        'rush': ('rushYds', 'rushTDs', 'offensive_stats', 'HB', None),
        'receiv': ('recYds', 'recTDs', 'offensive_stats', None, None),
        'tackl': ('defTotalTackles', None, 'defensive_stats', None, None),
        'sack': ('defSacks', None, 'defensive_stats', None, None),
        'intercept': ('defInts', None, 'defensive_stats', None, None),
    }

    stat_key = None
    for key in stat_map:
        if key in text_lower:
            stat_key = key
            break

    if not stat_key:
        return None  # Fall through to Tier 2/3

    primary_col, secondary_col, table, pos_filter, worst_col = stat_map[stat_key]
    sort_asc = any(kw in text_lower for kw in ['worst', 'bottom', 'fewest', 'least', 'lowest'])
    sort_dir = 'ASC' if sort_asc else 'DESC'

    # "Worst passer" → use efficiency metric (passer rating) instead of volume
    if sort_asc and worst_col:
        select_cols = f"ROUND(AVG(CAST({worst_col} AS REAL)), 1) AS total_stat"
        if secondary_col:
            select_cols += f", SUM(CAST({secondary_col} AS INTEGER)) AS total_secondary"
    else:
        select_cols = f"SUM(CAST({primary_col} AS INTEGER)) AS total_stat"
        if secondary_col:
            select_cols += f", SUM(CAST({secondary_col} AS INTEGER)) AS total_secondary"

    sql = f"""
        SELECT extendedName AS player_name, teamName, {select_cols},
               COUNT(*) AS games_played
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
    # Minimum games filter for "worst" queries — exclude one-game backups
    having = " HAVING COUNT(*) >= 4" if sort_asc else ""
    sql += f" GROUP BY extendedName{having} ORDER BY total_stat {sort_dir} LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="leaderboard", sql=sql, params=tuple(params_list), tier=1,
        meta={"type": "leaderboard", "stat": stat_key, "sort": sort_dir.lower()}
    )


# ── Intent 5: Recent Games ──────────────────────────────────────────────────

@_register("recent_games", [
    # "my last 5 games vs Killa" — with opponent filter
    r"\b(?:my|(\S+?)(?:'s)?)\s+last\s+(\d+)\s+games?\s+(?:vs\.?|against|versus)\s+(\S+)",
    # "my last 5 games" — no opponent
    r"\b(?:my|(\S+?)(?:'s)?)\s+last\s+(\d+)\s+games?(?!\s+(?:vs|against|versus))",
    r"\b(?:my|(\S+?)(?:'s)?)\s+recent\s+(?:games?|results?|matchups?)",
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:last|most\s+recent)\s+game\b",
    # "my games against Tuna", "my games vs Killa"
    r"\b(?:my|(\S+?)(?:'s)?)\s+games?\s+(?:vs\.?|against|versus)\s+(\S+)",
])
def _build_recent_games(match, caller_db, question, resolved_names):
    groups = list(match.groups())  # Keep positional structure
    owner = None
    count = 5  # default
    opponent = None

    # Check if "my" was used (first group is None because (?:my|(\S+?)...) matched "my")
    used_my = bool(re.search(r'\bmy\b', question, re.IGNORECASE)) and groups[0] is None

    non_none = [g for g in groups if g]
    for g in non_none:
        if g.isdigit():
            count = int(g)
        elif used_my and owner is None:
            # "my" was used → caller is owner, this name is the opponent
            owner = caller_db
            opponent = _resolve_name(g, resolved_names) or g
        elif owner is None:
            owner = _resolve_name(g, resolved_names) or g
        elif opponent is None:
            opponent = _resolve_name(g, resolved_names) or g

    if not owner:
        owner = caller_db
    if not owner:
        return None

    if opponent:
        # Recent games filtered by opponent
        sql = """
            SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
                   homeScore, awayScore, homeUser, awayUser,
                   winner_user, loser_user
            FROM games
            WHERE status IN ('2','3')
              AND stageIndex = '1'
              AND ((homeUser = ? AND awayUser = ?)
                OR (homeUser = ? AND awayUser = ?))
            ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
            LIMIT ?
        """
        params = (owner, opponent, opponent, owner, count)
        return IntentResult(
            intent="recent_games", sql=sql, params=params, tier=1,
            meta={"owner": owner, "opponent": opponent, "count": count, "type": "game_log"}
        )
    else:
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
    # "is Killa on a winning streak" (must be before generic pattern)
    r'\b(?:am\s+i|is\s+(\S+))\s+on\s+a\s+(?:win|los)',
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:current\s+)?(?:win(?:ning)?|los(?:s|ing))\s+streak",
    r"\b(?:my|(\S+?)(?:'s)?)\s+(?:current\s+)?streak\b",
])
def _build_streak(match, caller_db, question, resolved_names):
    _STREAK_STOP = {'a', 'an', 'the', 'is', 'on', 'am', 'i', 'winning', 'losing',
                    'win', 'loss', 'current', 'streak'}
    groups = [g for g in match.groups() if g and g.lower() not in _STREAK_STOP]
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
    # "Packers record" (bare team + record)
    r'\b(?:the\s+)?(\w+(?:\s+\w+)?)\s+record\b',
])
def _build_team_record(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if not groups:
        return None

    team_name = _resolve_team(groups[0])
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
    r'\b(?:who\s+did\s+)?(?:the\s+)?(\w+(?:\s+\w+)?)\s+draft\b',
    r"\b(\w+(?:\s+\w+)?)(?:'s)?\s+draft\s+(?:picks?|history|class)",
    # "who drafted for New England"
    r'\bwho\s+drafted\s+(?:for\s+)?(?:the\s+)?(\w+(?:\s+\w+)?)',
])
def _build_draft_history(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if not groups:
        return None

    team_name = _resolve_team(groups[0])
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


# ── Intent 9: Game Score ────────────────────────────────────────────────────

@_register("game_score", [
    # "what was the score of Lions vs Packers"
    r'\bscore\s+(?:of\s+)?(?:the\s+)?(\w+(?:\s+\w+)?)\s+(?:vs\.?|versus|against|and)\s+(\w+(?:\s+\w+)?)',
    # "score of the Chiefs game"
    r'\bscore\s+(?:of\s+)?(?:the\s+)?(\w+(?:\s+\w+)?)\s+game',
    # "Lions vs Packers score/result"
    r'\b(\w+(?:\s+\w+)?)\s+(?:vs\.?|versus)\s+(\w+(?:\s+\w+)?)\s+(?:score|result)',
    # "Lions vs Packers" (bare team vs team — handled if both resolve to teams)
    r'\b(\w+(?:\s+\w+)?)\s+(?:vs\.?|versus)\s+(\w+(?:\s+\w+)?)\b',
])
def _build_game_score(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if not groups:
        return None

    team1 = _resolve_team(groups[0])
    team2 = _resolve_team(groups[1]) if len(groups) > 1 else None
    if not team1:
        return None

    season = _extract_season(question)
    limit = _extract_limit(question, default=5)

    if team2:
        sql = """
            SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
                   homeScore, awayScore, homeUser, awayUser, winner_team
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND ((homeTeamName = ? AND awayTeamName = ?)
                OR (homeTeamName = ? AND awayTeamName = ?))
        """
        params_list = [team1, team2, team2, team1]
    else:
        sql = """
            SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
                   homeScore, awayScore, homeUser, awayUser, winner_team
            FROM games
            WHERE status IN ('2','3') AND stageIndex = '1'
              AND (homeTeamName = ? OR awayTeamName = ?)
        """
        params_list = [team1, team1]

    if season:
        sql += " AND seasonIndex = ?"
        params_list.append(str(season))
    sql += " ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="game_score", sql=sql, params=tuple(params_list), tier=1,
        meta={"team1": team1, "team2": team2, "type": "score"}
    )


# ── Intent 10: Playoff Results ─────────────────────────────────────────────

@_register("playoff_results", [
    r'\b(?:who\s+won|winner\s+of)\s+(?:the\s+)?(?:super\s*bowl|championship|title)',
    r'\bsuper\s*bowl\s+(?:results?|winners?|history|champs?|champions?)',
    r'\bplayoff\s+(?:results?|games?|scores?|bracket)',
    r'\bchampionship\s+game\s+(?:scores?|results?)',
])
def _build_playoff_results(match, caller_db, question, resolved_names):
    text_lower = question.lower()
    season = _extract_season(question)
    limit = _extract_limit(question, default=10)

    if any(kw in text_lower for kw in ['super bowl', 'superbowl', 'championship', 'title']):
        sql = """
            SELECT seasonIndex, homeTeamName, awayTeamName, homeScore, awayScore,
                   homeUser, awayUser, winner_team, winner_user
            FROM games
            WHERE status IN ('2','3') AND CAST(stageIndex AS INTEGER) >= 200
        """
    else:
        sql = """
            SELECT seasonIndex, weekIndex, stageIndex, homeTeamName, awayTeamName,
                   homeScore, awayScore, winner_team, winner_user
            FROM games
            WHERE status IN ('2','3') AND CAST(stageIndex AS INTEGER) >= 2
        """

    params_list = []
    if season:
        sql += " AND seasonIndex = ?"
        params_list.append(str(season))
    sql += " ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="playoff_results", sql=sql, params=tuple(params_list), tier=1,
        meta={"type": "playoffs"}
    )


# ── Intent 11: Player Stats ───────────────────────────────────────────────

@_register("player_stats", [
    # "who has the most passing TDs all-time", "who leads in sacks"
    r'\b(?:who\s+(?:has|leads?|is\s+leading)\s+(?:the\s+)?(?:most|highest|league\s+in))\s+(\w[\w\s]*)',
    # "who has the worst/least/fewest passing yards"
    r'\b(?:who\s+(?:has|is)\s+(?:the\s+)?(?:worst|least|fewest|lowest))\s+(\w[\w\s]*)',
    # "top rushing yards this season"
    r'\btop\s+(?:\d+\s+)?(?:in\s+)?(passing\s+(?:yards?|tds?|touchdowns?)|rushing\s+(?:yards?|tds?|touchdowns?)|receiving\s+(?:yards?|tds?|touchdowns?)|tackles?|sacks?|interceptions?|forced\s+fumbles?|fumble\s+recoveries?|deflections?|passer\s+rating)',
])
def _build_player_stats(match, caller_db, question, resolved_names):
    stat_key, stat_info = _lookup_stat(question)
    if not stat_info:
        return None

    table, column, agg, pos_filter = stat_info
    cast_type = 'REAL' if agg == 'AVG' else 'INTEGER'
    season = _extract_season(question)
    limit = _extract_limit(question, default=10)

    # Detect "worst"/"least"/"lowest"/"bottom"/"fewest" → sort ascending
    text_lower = question.lower()
    sort_asc = any(kw in text_lower for kw in ['worst', 'least', 'lowest', 'bottom', 'fewest'])
    sort_dir = 'ASC' if sort_asc else 'DESC'

    sql = f"""
        SELECT extendedName AS player_name, teamName,
               {agg}(CAST({column} AS {cast_type})) AS stat_value,
               COUNT(*) AS games_played
        FROM {table}
        WHERE stageIndex = '1'
    """
    params_list = []
    if pos_filter:
        sql += " AND pos = ?"
        params_list.append(pos_filter)
    if season:
        sql += " AND seasonIndex = ?"
        params_list.append(str(season))
    # Minimum games filter for "worst" queries — exclude one-game backups
    having = " HAVING COUNT(*) >= 4" if sort_asc else ""
    sql += f" GROUP BY extendedName{having} ORDER BY stat_value {sort_dir} LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="player_stats", sql=sql, params=tuple(params_list), tier=1,
        meta={"stat": stat_key, "sort": "asc" if sort_asc else "desc", "type": "player_stats"}
    )


# ── Intent 12: Trade History ──────────────────────────────────────────────

@_register("trade_history", [
    r'\b(?:what\s+)?trades?\s+(?:did|has|have)\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+(?:made?|done|completed)',
    r'\b(?:the\s+)?(\w+(?:\s+\w+)?)\s+trades?\s*(?:this|in|for|last)?\s*(?:season)?',
    r'\btrades?\s+(?:this|in|for|last)\s+season',
    r'\brecent\s+trades?\b',
    r'\btrade\s+history\b',
])
def _build_trade_history(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    season = _extract_season(question)
    limit = _extract_limit(question, default=20)

    team_name = _resolve_team(groups[0]) if groups else None

    sql = """
        SELECT team1Name, team2Name, seasonIndex, team1Sent, team2Sent
        FROM trades WHERE status IN ('approved', 'accepted')
    """
    params_list = []
    if team_name:
        sql += " AND (team1Name LIKE ? OR team2Name LIKE ?)"
        params_list.extend([f"%{team_name}%", f"%{team_name}%"])
    if season:
        sql += " AND seasonIndex = ?"
        params_list.append(str(season))
    elif not team_name:
        # No team and no season — default to current season
        sql += " AND seasonIndex = ?"
        params_list.append(str(_current_season()))
    sql += " ORDER BY CAST(seasonIndex AS INTEGER) DESC LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="trade_history", sql=sql, params=tuple(params_list), tier=1,
        meta={"team": team_name, "type": "trades"}
    )


# ── Intent 13: Team Stats (uses standings table) ─────────────────────────

@_register("team_stats", [
    r'\b(?:which|what)\s+team\s+(?:has|have|is)\s+(?:the\s+)?(?:best|worst|most|least|highest|lowest)\s+(offense|defense|offence|defence|points?|scoring)',
    r'\b(?:which|what)\s+team\s+scores?\s+(?:the\s+)?most\s+points?',
    r'\bwho\s+(?:has|have)\s+(?:the\s+)?(?:most|fewest|least|lowest|highest)\s+(points?|offense|defense|offence|defence|scoring)',
    # "which team allows/gives up the most points"
    r'\b(?:which|what)\s+team\s+(?:allows?|gives?\s+up)\s+(?:the\s+)?(?:most|fewest|least)\s+points?',
])
def _build_team_stats(match, caller_db, question, resolved_names):
    text_lower = question.lower()

    # Detect worst/least/lowest qualifier to flip sort direction
    flip = any(kw in text_lower for kw in ['worst', 'least', 'lowest', 'fewest'])

    # Determine sort column and direction
    if any(kw in text_lower for kw in ['offense', 'offence', 'offensive']):
        sort_col = 'CAST(offTotalYds AS INTEGER)'
        sort_dir = 'ASC' if flip else 'DESC'
    elif any(kw in text_lower for kw in ['defense', 'defence', 'defensive']):
        sort_col = 'CAST(defTotalYds AS INTEGER)'
        # Defense: best = fewest yards (ASC), worst = most yards (DESC)
        sort_dir = 'DESC' if flip else 'ASC'
    elif any(kw in text_lower for kw in ['allows', 'gives up', 'gives']):
        sort_col = 'CAST(ptsAgainst AS INTEGER)'
        sort_dir = 'ASC' if flip else 'DESC'
    elif any(kw in text_lower for kw in ['points', 'scoring', 'scores']):
        sort_col = 'CAST(ptsFor AS INTEGER)'
        sort_dir = 'ASC' if flip else 'DESC'
    else:
        sort_col = 'CAST(ptsFor AS INTEGER)'
        sort_dir = 'ASC' if flip else 'DESC'

    limit = _extract_limit(question, default=10)

    sql = f"""
        SELECT teamName,
               CAST(offTotalYds AS INTEGER) AS off_yds,
               CAST(defTotalYds AS INTEGER) AS def_yds,
               CAST(ptsFor AS INTEGER) AS pts_for,
               CAST(ptsAgainst AS INTEGER) AS pts_against,
               CAST(tODiff AS INTEGER) AS to_diff
        FROM standings
        ORDER BY {sort_col} {sort_dir}
        LIMIT ?
    """
    return IntentResult(
        intent="team_stats", sql=sql, params=(limit,), tier=1,
        meta={"type": "team_stats"}
    )


# ── Intent 14: Owner History ─────────────────────────────────────────────

@_register("owner_history", [
    r'\b(?:what\s+)?teams?\s+(?:has|have|did)\s+(\S+)\s+(?:owned?|run|managed|coached)',
    r"\b(\S+)\s+(?:team|ownership)\s+history",
    r'\bwho\s+(?:owned?|ran|managed)\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+in\s+season\s*(\d+)',
    r'\bwho\s+(?:owned?|ran|managed)\s+(?:the\s+)?(\w+)',
])
def _build_owner_history(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if not groups:
        return None

    text_lower = question.lower()

    # "who owned the Bears in season 2" — team lookup
    if 'who' in text_lower and ('owned' in text_lower or 'ran' in text_lower or 'managed' in text_lower):
        team_name = _resolve_team(groups[0])
        if not team_name:
            return None
        season = _extract_season(question)
        sql = """
            SELECT userName, teamName, seasonIndex, games_played
            FROM owner_tenure WHERE teamName LIKE ?
        """
        params_list = [f"%{team_name}%"]
        if season:
            sql += " AND seasonIndex = ?"
            params_list.append(str(season))
        sql += " ORDER BY CAST(seasonIndex AS INTEGER)"
        return IntentResult(
            intent="owner_history", sql=sql, params=tuple(params_list), tier=1,
            meta={"team": team_name, "type": "owner_history"}
        )

    # "what teams has Witt owned" — owner lookup
    owner = _resolve_name(groups[0], resolved_names) or groups[0]
    sql = """
        SELECT teamName, seasonIndex, games_played
        FROM owner_tenure WHERE userName = ?
        ORDER BY CAST(seasonIndex AS INTEGER)
    """
    return IntentResult(
        intent="owner_history", sql=sql, params=(owner,), tier=1,
        meta={"owner": owner, "type": "owner_history"}
    )


# ── Intent 15: Records / Extremes ────────────────────────────────────────

@_register("records_extremes", [
    r'\bbiggest\s+(?:blowout|blowouts?|win|margin)',
    r'\bclosest\s+(?:game|games?|finish|finishes)',
    r'\bhighest\s+scoring\s+(?:game|games?)',
    r'\blowest\s+scoring\s+(?:game|games?)',
    r'\bmost\s+(?:lopsided|one[\s-]?sided)\s+(?:game|games?)',
])
def _build_records_extremes(match, caller_db, question, resolved_names):
    text_lower = question.lower()
    season = _extract_season(question)
    limit = _extract_limit(question, default=5)

    if 'biggest' in text_lower or 'blowout' in text_lower or 'lopsided' in text_lower or 'one-sided' in text_lower:
        sort_expr = "margin DESC"
    elif 'closest' in text_lower:
        sort_expr = "margin ASC"
    elif 'highest' in text_lower:
        sort_expr = "total_pts DESC"
    elif 'lowest' in text_lower:
        sort_expr = "total_pts ASC"
    else:
        sort_expr = "margin DESC"

    sql = f"""
        SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
               homeScore, awayScore, homeUser, awayUser,
               ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin,
               (CAST(homeScore AS INTEGER) + CAST(awayScore AS INTEGER)) AS total_pts
        FROM games
        WHERE status IN ('2','3') AND stageIndex = '1'
    """
    params_list = []
    if season:
        sql += " AND seasonIndex = ?"
        params_list.append(str(season))
    sql += f" ORDER BY {sort_expr} LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="records_extremes", sql=sql, params=tuple(params_list), tier=1,
        meta={"type": "records"}
    )


# ── Intent 16: Standings ─────────────────────────────────────────────────

# Division aliases for standings queries
_DIV_ALIASES = {
    'nfc east': 'NFC East', 'nfc west': 'NFC West', 'nfc north': 'NFC North', 'nfc south': 'NFC South',
    'afc east': 'AFC East', 'afc west': 'AFC West', 'afc north': 'AFC North', 'afc south': 'AFC South',
}

@_register("standings_query", [
    r'\b((?:nfc|afc)\s+(?:east|west|north|south))\s+standings?',
    r'\b(?:current\s+)?standings?\b',
    r'\bplayoff\s+(?:picture|race|standings?)',
    r'\bwho\s+leads?\s+(?:the\s+)?(nfc|afc|(?:nfc|afc)\s+(?:east|west|north|south))',
    r'\bdivision\s+(?:standings?|leaders?|rankings?)',
])
def _build_standings(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    limit = _extract_limit(question, default=32)

    sql = """
        SELECT teamName, totalWins, totalLosses, winPct, ptsFor, ptsAgainst,
               seed, rank, divisionName, conferenceName, divWins, divLosses, confWins, confLosses
        FROM standings
        WHERE 1=1
    """
    params_list = []

    # Check for division or conference filter
    text_lower = question.lower()
    div_match = None
    for alias, div_name in _DIV_ALIASES.items():
        if alias in text_lower:
            div_match = div_name
            break

    if div_match:
        sql += " AND divisionName = ?"
        params_list.append(div_match)
    elif 'nfc' in text_lower and not any(d in text_lower for d in ['east', 'west', 'north', 'south']):
        sql += " AND conferenceName = ?"
        params_list.append('NFC')
    elif 'afc' in text_lower and not any(d in text_lower for d in ['east', 'west', 'north', 'south']):
        sql += " AND conferenceName = ?"
        params_list.append('AFC')

    sql += " ORDER BY CAST(rank AS INTEGER) LIMIT ?"
    params_list.append(limit)

    return IntentResult(
        intent="standings_query", sql=sql, params=tuple(params_list), tier=1,
        meta={"type": "standings"}
    )


# ── Intent 17: Roster Query ──────────────────────────────────────────────

_POS_ALIASES = {
    'quarterback': 'QB', 'qb': 'QB', 'qbs': 'QB',
    'running back': 'HB', 'rb': 'HB', 'hb': 'HB', 'halfback': 'HB', 'rbs': 'HB',
    'wide receiver': 'WR', 'wr': 'WR', 'wrs': 'WR', 'receiver': 'WR', 'receivers': 'WR',
    'tight end': 'TE', 'te': 'TE', 'tes': 'TE',
    'linebacker': 'MLB', 'lb': 'MLB', 'lbs': 'MLB',
    'cornerback': 'CB', 'cb': 'CB', 'cbs': 'CB', 'corner': 'CB',
    'safety': 'FS', 'safeties': 'FS', 'fs': 'FS', 'ss': 'SS',
    'defensive end': 'RE', 'de': 'RE', 'des': 'RE',
    'defensive tackle': 'DT', 'dt': 'DT', 'dts': 'DT',
    'kicker': 'K', 'k': 'K',
    'punter': 'P', 'p': 'P',
}

def _resolve_position(text: str) -> str | None:
    text_lower = text.lower().strip()
    return _POS_ALIASES.get(text_lower)


@_register("roster_query", [
    # "Lions roster"
    r'\b(\w+(?:\s+\w+)?)\s+roster\b',
    # "best QB in the league", "highest rated QB", "who is the best QB"
    r'\b(?:best|highest\s+rated|top)\s+(QB|HB|WR|TE|MLB|CB|FS|SS|RE|DT|K|P|quarterback|running\s+back|wide\s+receiver|tight\s+end|linebacker|cornerback|safety|defensive\s+end|defensive\s+tackle|kicker|punter)\b',
    # "free agents at QB"
    r'\bfree\s+agents?\s*(?:at|for)?\s*(QB|HB|WR|TE|MLB|CB|FS|SS|RE|DT|K|P|quarterback|running\s+back|wide\s+receiver|tight\s+end|linebacker|cornerback|safety|defensive\s+end|defensive\s+tackle|kicker|punter)?',
])
def _build_roster_query(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    text_lower = question.lower()
    limit = _extract_limit(question, default=15)

    # Free agent query
    if 'free agent' in text_lower:
        pos = _resolve_position(groups[0]) if groups else None
        sql = """
            SELECT firstName, lastName, pos, teamName,
                   CAST(playerBestOvr AS INTEGER) AS ovr, dev
            FROM players WHERE teamName = 'Free Agent'
        """
        params_list = []
        if pos:
            sql += " AND pos = ?"
            params_list.append(pos)
        sql += " ORDER BY ovr DESC LIMIT ?"
        params_list.append(limit)
        return IntentResult(
            intent="roster_query", sql=sql, params=tuple(params_list), tier=1,
            meta={"type": "roster", "filter": "free_agents"}
        )

    # "best QB in the league"
    if any(kw in text_lower for kw in ['best', 'highest rated', 'top']):
        pos = _resolve_position(groups[0]) if groups else None
        if not pos:
            return None
        # Check for team filter
        team_name = None
        for alias, name in _TEAM_ALIASES.items():
            if alias in text_lower:
                team_name = name
                break

        sql = """
            SELECT firstName, lastName, pos, teamName,
                   CAST(playerBestOvr AS INTEGER) AS ovr, dev
            FROM players WHERE pos = ?
        """
        params_list = [pos]
        if team_name:
            sql += " AND teamName LIKE ?"
            params_list.append(f"%{team_name}%")
        sql += " ORDER BY ovr DESC LIMIT ?"
        params_list.append(limit)
        return IntentResult(
            intent="roster_query", sql=sql, params=tuple(params_list), tier=1,
            meta={"type": "roster", "position": pos}
        )

    # "Lions roster"
    if groups:
        team_name = _resolve_team(groups[0])
        if not team_name:
            return None
        sql = """
            SELECT firstName, lastName, pos,
                   CAST(playerBestOvr AS INTEGER) AS ovr, dev, age, contractYearsLeft
            FROM players WHERE teamName LIKE ?
            ORDER BY ovr DESC LIMIT ?
        """
        return IntentResult(
            intent="roster_query", sql=sql, params=(f"%{team_name}%", limit), tier=1,
            meta={"team": team_name, "type": "roster"}
        )

    return None


# ── Intent 18: Player Abilities ──────────────────────────────────────────

@_register("player_abilities_query", [
    r'\bwho\s+has\s+(?:x[\s-]?factor|superstar)\s+(?:on|for)\s+(?:the\s+)?(\w+(?:\s+\w+)?)',
    # "what abilities does Jalen Hurts have" (must be before generic pattern)
    r'\bwhat\s+abilities\s+does\s+(\w[\w\s]+?)\s+have',
    r'\b(\w+(?:\s+\w+)?)\s+(?:x[\s-]?factors?|superstars?|abilities)\b',
])
def _build_player_abilities(match, caller_db, question, resolved_names):
    groups = [g for g in match.groups() if g]
    if not groups:
        return None

    text_lower = question.lower()

    # Try team resolution first
    team_name = _resolve_team(groups[0])
    if team_name:
        sql = """
            SELECT firstName, lastName, teamName, title, description
            FROM player_abilities WHERE teamName LIKE ?
            ORDER BY firstName
        """
        return IntentResult(
            intent="player_abilities_query", sql=sql, params=(f"%{team_name}%",), tier=1,
            meta={"team": team_name, "type": "abilities"}
        )

    # Individual player lookup
    player_name = groups[0].strip()
    sql = """
        SELECT firstName, lastName, teamName, title, description
        FROM player_abilities WHERE firstName || ' ' || lastName LIKE ?
    """
    return IntentResult(
        intent="player_abilities_query", sql=sql, params=(f"%{player_name}%",), tier=1,
        meta={"player": player_name, "type": "abilities"}
    )


# ── Tier 1: Regex Pre-flight ────────────────────────────────────────────────

def _match_regex(
    question: str,
    caller_db: str | None,
    resolved_names: dict[str, str],
) -> IntentResult | None:
    """Try all regex patterns in priority order. Return first match or None."""
    question = _normalize_question(question)
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
9. game_score — Score of a specific game or matchup between teams
10. playoff_results — Super Bowl winners, championship games, playoff results
11. player_stats — Individual player stats or stat leaders by category
12. trade_history — Trades made by a team or in a season
13. team_stats — Team-level stats (best/worst offense, defense, points)
14. owner_history — What teams an owner has controlled, or who owned a team
15. records_extremes — Biggest blowout, closest game, highest/lowest scoring
16. standings_query — Division/conference standings, playoff picture
17. roster_query — Team rosters, best players at a position, free agents
18. player_abilities_query — X-Factor/Superstar abilities for a team or player
19. unknown — Question doesn't fit any of the above

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
- game_score: {{"team1": str, "team2": str|null, "season": int|null}}
- playoff_results: {{"type": "superbowl"|"playoff", "season": int|null}}
- player_stats: {{"stat_category": str, "season": int|null, "limit": int}}
- trade_history: {{"team": str|null, "season": int|null}}
- team_stats: {{"stat_category": str}}
- owner_history: {{"owner": str|null, "team": str|null, "season": int|null}}
- records_extremes: {{"type": "blowout"|"closest"|"highest"|"lowest", "season": int|null, "limit": int}}
- standings_query: {{"division": str|null, "conference": str|null}}
- roster_query: {{"team": str|null, "position": str|null, "free_agents": bool}}
- player_abilities_query: {{"team": str|null, "player_name": str|null}}
- unknown: {{}}

User question: "{question}"
"""


async def _classify_gemini(
    question: str,
    caller_db: str | None,
    resolved_names: dict[str, str],
) -> IntentResult:
    """Tier 2: Ask Gemini to classify intent + extract params as JSON."""
    prompt = _CLASSIFICATION_PROMPT.format(
        caller_db=caller_db or "unknown",
        current_season=_current_season(),
        question=question,
    )

    try:
        result = await atlas_ai.generate(prompt, tier=Tier.HAIKU, json_mode=True)
        text = result.text.strip()

        data = json.loads(text)
        intent = data.get("intent", "unknown")
        confidence = float(data.get("confidence", 0))
        params = data.get("params", {})

        if confidence < 0.7 or intent == "unknown":
            return IntentResult(intent="unknown", tier=3)

        # Build IntentResult from classified intent
        return _build_from_classification(intent, params, caller_db, resolved_names)

    except Exception as e:
        log.warning(f"[codex_intents] Tier 2 classification failed: {e}")
        return IntentResult(intent="unknown", tier=3)


def _build_from_classification(
    intent: str,
    params: dict,
    caller_db: str | None,
    resolved_names: dict[str, str],
) -> IntentResult:
    """Build IntentResult from AI classification output."""

    if intent == "h2h_record":
        o1 = _resolve_name(params.get("owner1", ""), resolved_names) or params.get("owner1", caller_db)
        o2 = _resolve_name(params.get("owner2", ""), resolved_names) or params.get("owner2")
        if not o1 or not o2:
            return IntentResult(intent="unknown", tier=3)
        sql, sql_params = get_h2h_sql_and_params(o1, o2, params.get("season"))
        return IntentResult(
            intent="h2h_record", sql=sql, params=sql_params, tier=2,
            meta={"owner1": o1, "owner2": o2, "type": "rivalry"}
        )

    if intent == "season_record":
        raw_owner = params.get("owner", "")
        owner = _resolve_name(raw_owner, resolved_names) or raw_owner or caller_db
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
        raw_owner = params.get("owner", "")
        owner = _resolve_name(raw_owner, resolved_names) or raw_owner or caller_db
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
        raw_owner = params.get("owner", "")
        owner = _resolve_name(raw_owner, resolved_names) or raw_owner or caller_db
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
        raw_owner = params.get("owner", "")
        owner = _resolve_name(raw_owner, resolved_names) or raw_owner or caller_db
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

    if intent == "leaderboard":
        stat_type = params.get("stat_type", "wins")
        limit = params.get("limit", 10)
        season = params.get("season")
        st_lower = stat_type.lower()
        if any(kw in st_lower for kw in ['wins', 'winningest']):
            sql = "SELECT winner_user AS owner, COUNT(*) AS total_wins FROM games WHERE status IN ('2','3') AND stageIndex = '1' AND winner_user IS NOT NULL AND winner_user != ''"
            p = []
            if season:
                sql += " AND seasonIndex = ?"
                p.append(str(season))
            sql += " GROUP BY winner_user ORDER BY total_wins DESC LIMIT ?"
            p.append(limit)
            return IntentResult(intent="leaderboard", sql=sql, params=tuple(p), tier=2, meta={"type": "leaderboard", "stat": "wins"})
        if 'loss' in st_lower:
            sql = "SELECT loser_user AS owner, COUNT(*) AS total_losses FROM games WHERE status IN ('2','3') AND stageIndex = '1' AND loser_user IS NOT NULL AND loser_user != ''"
            p = []
            if season:
                sql += " AND seasonIndex = ?"
                p.append(str(season))
            sql += " GROUP BY loser_user ORDER BY total_losses DESC LIMIT ?"
            p.append(limit)
            return IntentResult(intent="leaderboard", sql=sql, params=tuple(p), tier=2, meta={"type": "leaderboard", "stat": "losses"})
        # Player stat leaderboard — try stat registry
        stat_key, stat_info = _lookup_stat(stat_type)
        if stat_info:
            table, column, agg, pos_filter = stat_info
            cast_type = 'REAL' if agg == 'AVG' else 'INTEGER'
            sql = f"SELECT extendedName AS player_name, teamName, {agg}(CAST({column} AS {cast_type})) AS total_stat FROM {table} WHERE stageIndex = '1'"
            p = []
            if pos_filter:
                sql += " AND pos = ?"
                p.append(pos_filter)
            if season:
                sql += " AND seasonIndex = ?"
                p.append(str(season))
            sql += " GROUP BY extendedName ORDER BY total_stat DESC LIMIT ?"
            p.append(limit)
            return IntentResult(intent="leaderboard", sql=sql, params=tuple(p), tier=2, meta={"type": "leaderboard", "stat": stat_key})
        return IntentResult(intent="unknown", tier=3)

    if intent == "team_record":
        team = params.get("team_name", "")
        team_name = _resolve_team(team) if team else None
        if not team_name:
            return IntentResult(intent="unknown", tier=3)
        season = params.get("season", _current_season())
        sql = """SELECT SUM(CASE WHEN winner_team = ? THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN loser_team = ? THEN 1 ELSE 0 END) AS losses
                 FROM games WHERE status IN ('2','3') AND stageIndex = '1'
                   AND seasonIndex = ? AND (homeTeamName = ? OR awayTeamName = ?)"""
        return IntentResult(intent="team_record", sql=sql, params=(team_name, team_name, str(season), team_name, team_name), tier=2, meta={"team": team_name, "season": season, "type": "record"})

    if intent == "draft_history":
        team = params.get("team", "")
        team_name = _resolve_team(team) if team else None
        if not team_name:
            return IntentResult(intent="unknown", tier=3)
        season = params.get("season")
        sql = "SELECT extendedName, drafting_team, drafting_season, draftRound, draftPick, pos, playerBestOvr, dev, was_traded FROM player_draft_map WHERE drafting_team LIKE ?"
        p = [f"%{team_name}%"]
        if season:
            sql += " AND drafting_season = ?"
            p.append(str(season))
        sql += " ORDER BY CAST(draftRound AS INTEGER), CAST(draftPick AS INTEGER)"
        return IntentResult(intent="draft_history", sql=sql, params=tuple(p), tier=2, meta={"team": team_name, "type": "draft_class"})

    if intent == "game_score":
        t1 = _resolve_team(params.get("team1", ""))
        t2 = _resolve_team(params.get("team2", "")) if params.get("team2") else None
        if not t1:
            return IntentResult(intent="unknown", tier=3)
        season = params.get("season")
        if t2:
            sql = "SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName, homeScore, awayScore, homeUser, awayUser, winner_team FROM games WHERE status IN ('2','3') AND stageIndex = '1' AND ((homeTeamName = ? AND awayTeamName = ?) OR (homeTeamName = ? AND awayTeamName = ?))"
            p = [t1, t2, t2, t1]
        else:
            sql = "SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName, homeScore, awayScore, homeUser, awayUser, winner_team FROM games WHERE status IN ('2','3') AND stageIndex = '1' AND (homeTeamName = ? OR awayTeamName = ?)"
            p = [t1, t1]
        if season:
            sql += " AND seasonIndex = ?"
            p.append(str(season))
        sql += " ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC LIMIT 5"
        return IntentResult(intent="game_score", sql=sql, params=tuple(p), tier=2, meta={"team1": t1, "team2": t2, "type": "score"})

    if intent == "playoff_results":
        ptype = params.get("type", "playoff")
        season = params.get("season")
        if ptype == "superbowl":
            sql = "SELECT seasonIndex, homeTeamName, awayTeamName, homeScore, awayScore, homeUser, awayUser, winner_team, winner_user FROM games WHERE status IN ('2','3') AND CAST(stageIndex AS INTEGER) >= 200"
        else:
            sql = "SELECT seasonIndex, weekIndex, stageIndex, homeTeamName, awayTeamName, homeScore, awayScore, winner_team, winner_user FROM games WHERE status IN ('2','3') AND CAST(stageIndex AS INTEGER) >= 2"
        p = []
        if season:
            sql += " AND seasonIndex = ?"
            p.append(str(season))
        sql += " ORDER BY CAST(seasonIndex AS INTEGER) DESC LIMIT 10"
        return IntentResult(intent="playoff_results", sql=sql, params=tuple(p), tier=2, meta={"type": "playoffs"})

    if intent == "player_stats":
        stat_cat = params.get("stat_category", "")
        stat_key, stat_info = _lookup_stat(stat_cat)
        if not stat_info:
            return IntentResult(intent="unknown", tier=3)
        table, column, agg, pos_filter = stat_info
        cast_type = 'REAL' if agg == 'AVG' else 'INTEGER'
        season = params.get("season")
        limit = params.get("limit", 10)
        sort_dir = params.get("sort", "DESC")  # Gemini can pass "ASC" for worst/least
        if sort_dir not in ("ASC", "DESC"):
            sort_dir = "DESC"
        sql = f"SELECT extendedName AS player_name, teamName, {agg}(CAST({column} AS {cast_type})) AS stat_value FROM {table} WHERE stageIndex = '1'"
        p = []
        if pos_filter:
            sql += " AND pos = ?"
            p.append(pos_filter)
        if season:
            sql += " AND seasonIndex = ?"
            p.append(str(season))
        sql += f" GROUP BY extendedName ORDER BY stat_value {sort_dir} LIMIT ?"
        p.append(limit)
        return IntentResult(intent="player_stats", sql=sql, params=tuple(p), tier=2, meta={"stat": stat_key, "sort": sort_dir.lower(), "type": "player_stats"})

    if intent == "trade_history":
        team = params.get("team", "")
        team_name = _resolve_team(team) if team else None
        season = params.get("season")
        sql = "SELECT team1Name, team2Name, seasonIndex, team1Sent, team2Sent FROM trades WHERE status IN ('approved', 'accepted')"
        p = []
        if team_name:
            sql += " AND (team1Name LIKE ? OR team2Name LIKE ?)"
            p.extend([f"%{team_name}%", f"%{team_name}%"])
        if season:
            sql += " AND seasonIndex = ?"
            p.append(str(season))
        sql += " ORDER BY CAST(seasonIndex AS INTEGER) DESC LIMIT 20"
        return IntentResult(intent="trade_history", sql=sql, params=tuple(p), tier=2, meta={"team": team_name, "type": "trades"})

    if intent == "team_stats":
        stat_cat = params.get("stat_category", "points")
        sort_pref = params.get("sort", "best")  # "best" or "worst"
        cat_lower = stat_cat.lower() if stat_cat else "points"
        if any(kw in cat_lower for kw in ['offense', 'offence', 'offensive']):
            sort_col = 'CAST(offTotalYds AS INTEGER)'
            sort_dir = 'ASC' if sort_pref == 'worst' else 'DESC'
        elif any(kw in cat_lower for kw in ['defense', 'defence', 'defensive']):
            sort_col = 'CAST(defTotalYds AS INTEGER)'
            sort_dir = 'DESC' if sort_pref == 'worst' else 'ASC'
        else:
            sort_col = 'CAST(ptsFor AS INTEGER)'
            sort_dir = 'ASC' if sort_pref == 'worst' else 'DESC'
        sql = f"SELECT teamName, CAST(offTotalYds AS INTEGER) AS off_yds, CAST(defTotalYds AS INTEGER) AS def_yds, CAST(ptsFor AS INTEGER) AS pts_for, CAST(ptsAgainst AS INTEGER) AS pts_against, CAST(tODiff AS INTEGER) AS to_diff FROM standings ORDER BY {sort_col} {sort_dir} LIMIT 10"
        return IntentResult(intent="team_stats", sql=sql, params=(), tier=2, meta={"type": "team_stats"})

    if intent == "owner_history":
        owner = params.get("owner")
        team = params.get("team")
        season = params.get("season")
        if owner:
            resolved = _resolve_name(owner, resolved_names) or owner
            sql = "SELECT teamName, seasonIndex, games_played FROM owner_tenure WHERE userName = ? ORDER BY CAST(seasonIndex AS INTEGER)"
            return IntentResult(intent="owner_history", sql=sql, params=(resolved,), tier=2, meta={"owner": resolved, "type": "owner_history"})
        if team:
            team_name = _resolve_team(team)
            if not team_name:
                return IntentResult(intent="unknown", tier=3)
            sql = "SELECT userName, teamName, seasonIndex, games_played FROM owner_tenure WHERE teamName LIKE ?"
            p = [f"%{team_name}%"]
            if season:
                sql += " AND seasonIndex = ?"
                p.append(str(season))
            sql += " ORDER BY CAST(seasonIndex AS INTEGER)"
            return IntentResult(intent="owner_history", sql=sql, params=tuple(p), tier=2, meta={"team": team_name, "type": "owner_history"})
        return IntentResult(intent="unknown", tier=3)

    if intent == "records_extremes":
        rtype = params.get("type", "blowout")
        season = params.get("season")
        limit = params.get("limit", 5)
        sort_map = {"blowout": "margin DESC", "closest": "margin ASC", "highest": "total_pts DESC", "lowest": "total_pts ASC"}
        sort_expr = sort_map.get(rtype, "margin DESC")
        sql = f"SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName, homeScore, awayScore, homeUser, awayUser, ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin, (CAST(homeScore AS INTEGER) + CAST(awayScore AS INTEGER)) AS total_pts FROM games WHERE status IN ('2','3') AND stageIndex = '1'"
        p = []
        if season:
            sql += " AND seasonIndex = ?"
            p.append(str(season))
        sql += f" ORDER BY {sort_expr} LIMIT ?"
        p.append(limit)
        return IntentResult(intent="records_extremes", sql=sql, params=tuple(p), tier=2, meta={"type": "records"})

    if intent == "standings_query":
        division = params.get("division")
        conference = params.get("conference")
        sql = "SELECT teamName, totalWins, totalLosses, winPct, ptsFor, ptsAgainst, seed, rank, divisionName, conferenceName FROM standings WHERE 1=1"
        p = []
        if division:
            sql += " AND divisionName LIKE ?"
            p.append(f"%{division}%")
        elif conference:
            sql += " AND conferenceName = ?"
            p.append(conference.upper())
        sql += " ORDER BY CAST(rank AS INTEGER) LIMIT 32"
        return IntentResult(intent="standings_query", sql=sql, params=tuple(p), tier=2, meta={"type": "standings"})

    if intent == "roster_query":
        team = params.get("team")
        position = params.get("position")
        free_agents = params.get("free_agents", False)
        if free_agents:
            sql = "SELECT firstName, lastName, pos, teamName, CAST(playerBestOvr AS INTEGER) AS ovr, dev FROM players WHERE teamName = 'Free Agent'"
            p = []
            if position:
                pos = _resolve_position(position) or position.upper()
                sql += " AND pos = ?"
                p.append(pos)
            sql += " ORDER BY ovr DESC LIMIT 15"
            return IntentResult(intent="roster_query", sql=sql, params=tuple(p), tier=2, meta={"type": "roster"})
        if team:
            team_name = _resolve_team(team)
            if team_name:
                sql = "SELECT firstName, lastName, pos, CAST(playerBestOvr AS INTEGER) AS ovr, dev, age FROM players WHERE teamName LIKE ? ORDER BY ovr DESC LIMIT 15"
                return IntentResult(intent="roster_query", sql=sql, params=(f"%{team_name}%",), tier=2, meta={"team": team_name, "type": "roster"})
        if position:
            pos = _resolve_position(position) or position.upper()
            sql = "SELECT firstName, lastName, pos, teamName, CAST(playerBestOvr AS INTEGER) AS ovr, dev FROM players WHERE pos = ? ORDER BY ovr DESC LIMIT 10"
            return IntentResult(intent="roster_query", sql=sql, params=(pos,), tier=2, meta={"type": "roster", "position": pos})
        return IntentResult(intent="unknown", tier=3)

    if intent == "player_abilities_query":
        team = params.get("team")
        player_name = params.get("player_name")
        if team:
            team_name = _resolve_team(team)
            if team_name:
                sql = "SELECT firstName, lastName, teamName, title, description FROM player_abilities WHERE teamName LIKE ? ORDER BY firstName"
                return IntentResult(intent="player_abilities_query", sql=sql, params=(f"%{team_name}%",), tier=2, meta={"team": team_name, "type": "abilities"})
        if player_name:
            sql = "SELECT firstName, lastName, teamName, title, description FROM player_abilities WHERE firstName || ' ' || lastName LIKE ?"
            return IntentResult(intent="player_abilities_query", sql=sql, params=(f"%{player_name}%",), tier=2, meta={"player": player_name, "type": "abilities"})
        return IntentResult(intent="unknown", tier=3)

    return IntentResult(intent="unknown", tier=3)


# ── Public API ───────────────────────────────────────────────────────────────

async def detect_intent(
    question: str,
    caller_db: str | None,
    resolved_names: dict[str, str] | None = None,
) -> IntentResult:
    """
    Three-tier intent detection.

    Tier 1: Regex pre-flight (instant)
    Tier 2: AI structured classification (if regex misses)
    Tier 3: Returns IntentResult(tier=3) → caller uses existing gemini_sql() pipeline
    """
    resolved = resolved_names or {}
    question = _normalize_question(question)

    # Tier 1: Regex
    result = _match_regex(question, caller_db, resolved)
    if result:
        return result

    # Tier 2: AI classification
    result = await _classify_gemini(question, caller_db, resolved)
    if result.tier < 3:
        return result

    # Tier 3: Fallthrough
    return IntentResult(intent="unknown", tier=3)
