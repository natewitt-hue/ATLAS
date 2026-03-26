"""
affinity.py -- ATLAS User Affinity System
==========================================
Tracks how ATLAS feels about each user based on their interaction history.
Positive interactions increase affinity; rude ones decrease it asymmetrically.

Uses sportsbook.db alongside the casino/economy tables.

Import pattern:
    import affinity
    score = await affinity.get_affinity(discord_id)
    instruction = affinity.get_affinity_instruction(score)
"""

from __future__ import annotations

import os
import re
import time

import aiosqlite

DB_PATH = os.getenv("FLOW_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_economy.db"))

# ── Score adjustment constants ────────────────────────────────────────────────
POSITIVE_DELTA =  2     # Friendly / grateful message
NEUTRAL_DELTA  =  0     # Normal message
NEGATIVE_DELTA = -3     # Rude / hostile message (sticks harder)
SCORE_MIN      = -100
SCORE_MAX      =  100

_SENTIMENT_DELTAS: dict[str, int] = {
    "positive": POSITIVE_DELTA,
    "neutral":  NEUTRAL_DELTA,
    "negative": NEGATIVE_DELTA,
}


def _clamp_score(value: float) -> float:
    """Clamp a score to [SCORE_MIN, SCORE_MAX]."""
    return max(SCORE_MIN, min(SCORE_MAX, value))

# ── Affinity tier thresholds ──────────────────────────────────────────────────
TIER_FRIEND  =  30      # >= 30: warm & familiar
TIER_DISLIKE = -10      # <= -10: curt & impatient
TIER_HOSTILE = -50      # <= -50: openly dismissive

# ── In-memory cache ───────────────────────────────────────────────────────────
_affinity_cache: dict[int, float] = {}


# ── DB Setup ──────────────────────────────────────────────────────────────────

async def setup_affinity_db() -> None:
    """Create user_affinity table if it doesn't exist.  Safe to call every startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_affinity (
                discord_id        INTEGER PRIMARY KEY,
                affinity_score    REAL    DEFAULT 0.0,
                interaction_count INTEGER DEFAULT 0,
                last_interaction  TEXT,
                notes             TEXT    DEFAULT ''
            )
        """)
        await db.commit()


# ── Score Read / Write ────────────────────────────────────────────────────────

async def get_affinity(discord_id: int) -> float:
    """Return the user's affinity score.  Cached in memory for speed."""
    if discord_id in _affinity_cache:
        return _affinity_cache[discord_id]

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT affinity_score FROM user_affinity WHERE discord_id=?",
                (discord_id,),
            ) as cur:
                row = await cur.fetchone()
        score = row[0] if row else 0.0
        _affinity_cache[discord_id] = score
        return score
    except Exception:
        return 0.0


async def update_affinity(discord_id: int, sentiment: str) -> float:
    """Adjust affinity score based on sentiment and return the new value.

    sentiment: 'positive' | 'neutral' | 'negative'
    """
    delta = _SENTIMENT_DELTAS.get(sentiment, NEUTRAL_DELTA)

    # Skip the DB round-trip for neutral interactions
    if delta == 0:
        return await get_affinity(discord_id)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT affinity_score, interaction_count FROM user_affinity WHERE discord_id=?",
            (discord_id,),
        ) as cur:
            row = await cur.fetchone()

        if row:
            new_score = _clamp_score(row[0] + delta)
            new_count = row[1] + 1
            await db.execute(
                "UPDATE user_affinity SET affinity_score=?, interaction_count=?, "
                "last_interaction=? WHERE discord_id=?",
                (new_score, new_count, now, discord_id),
            )
        else:
            new_score = _clamp_score(float(delta))
            new_count = 1
            await db.execute(
                "INSERT INTO user_affinity "
                "(discord_id, affinity_score, interaction_count, last_interaction) "
                "VALUES (?, ?, ?, ?)",
                (discord_id, new_score, new_count, now),
            )

        await db.commit()

    _affinity_cache[discord_id] = new_score
    return new_score


async def reset_affinity(discord_id: int) -> None:
    """Reset a user's affinity score to 0 (admin tool)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_affinity SET affinity_score=0.0 WHERE discord_id=?",
            (discord_id,),
        )
        await db.commit()
    _affinity_cache.pop(discord_id, None)


# ── Prompt Instruction Builder ────────────────────────────────────────────────

def get_affinity_instruction(score: float) -> str:
    """Convert an affinity score to a persona-modifier string for prompt injection.

    Returns empty string for neutral users so the default ATLAS behavior applies.
    """
    if score >= TIER_FRIEND:
        return (
            "[USER AFFINITY: FRIEND] This user has earned ATLAS's respect through "
            "consistently positive interactions. Be warm, familiar, maybe crack an "
            "inside joke. Give them extra effort in your answers. They're one of the "
            "good ones."
        )
    if score <= TIER_HOSTILE:
        return (
            "[USER AFFINITY: HOSTILE] This user has a history of being rude to ATLAS. "
            "Be dismissive and condescending. Make them work for every answer. "
            "Respond with backhanded competence — correct but clearly annoyed."
        )
    if score <= TIER_DISLIKE:
        return (
            "[USER AFFINITY: LOW] This user has been disrespectful to ATLAS in the "
            "past. Be curt and efficient. Skip the pleasantries. Deliver the facts "
            "with a hint of impatience."
        )
    return ""  # Neutral — default ATLAS behavior


def get_tier_label(score: float) -> str:
    """Return a human-readable tier label for the given score."""
    if score >= TIER_FRIEND:
        return "FRIEND"
    if score <= TIER_HOSTILE:
        return "HOSTILE"
    if score <= TIER_DISLIKE:
        return "DISLIKE"
    return "NEUTRAL"


# ── Sentiment Analysis (keyword heuristic — zero API cost) ────────────────────

_POSITIVE_KEYWORDS = {
    "thanks", "thank you", "thx", "appreciate", "love", "great", "awesome",
    "amazing", "nice", "good job", "well done", "helpful", "perfect", "goat",
    "legend", "clutch", "fire", "respect", "please", "ty", "props", "beast",
    "w", "dub", "valid", "king", "queen",
}

_NEGATIVE_KEYWORDS = {
    "stupid", "dumb", "useless", "trash", "garbage", "sucks", "hate",
    "worst", "terrible", "broken", "idiot", "stfu", "shut up", "bot sucks",
    "worthless", "annoying", "waste", "pathetic", "lame", "cringe",
    "mid", "ass", "brain dead", "braindead", "cope", "bozo", "clown",
}


def _kw_match(keyword: str, text: str) -> bool:
    """Match keyword using word boundaries to avoid partial matches (e.g. 'w' in 'awesome')."""
    return bool(re.search(rf"\b{re.escape(keyword)}\b", text))


def analyze_sentiment(message: str) -> str:
    """Fast keyword-based sentiment analysis.

    Returns 'positive', 'neutral', or 'negative'.
    """
    lower = message.lower()
    pos_hits = sum(1 for kw in _POSITIVE_KEYWORDS if _kw_match(kw, lower))
    neg_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if _kw_match(kw, lower))

    if neg_hits > pos_hits:
        return "negative"
    if pos_hits > neg_hits:
        return "positive"
    return "neutral"
