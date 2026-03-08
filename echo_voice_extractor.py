"""
echo_voice_extractor.py - ATLAS Echo Voice Extraction Pipeline v4
==================================================================
Extracts the authentic voice of TSL Commissioner TheWitt from 222k+
Discord messages and generates three Gemini system prompts:

  echo/echo_casual.txt     - banter, reactions, trash talk, general chat
  echo/echo_official.txt   - rulings, announcements, governance
  echo/echo_analytical.txt - stats, recaps, trade analysis, commentary

DATA REALITY:
  - 215k messages are 1-80 chars  <- the voice lives here
  - ~1,994 messages are 81-300 chars
  - 300+ char messages excluded (mostly AI-generated content pasted in)
  - All messages from one channel - classification done by content

PIPELINE:
  Step 1 - Fetch stratified samples (short/medium/recent/chains/top_reacted)
  Step 2 - Content classification pre-pass (Flash, batches of 400)
  Step 3 - Signal extraction, 8 Flash passes including temporal drift + chains
  Step 4 - Persona synthesis, 3 Pro passes
  Step 4b - Validation pass (Flash audits each persona against signal data)
  Step 5 - Save output files with metadata headers

Usage:
  python echo_voice_extractor.py
  python echo_voice_extractor.py --no-cache
  python echo_voice_extractor.py --no-cache --no-validate
"""

import os
import re
import sqlite3
import json
import hashlib
import time
import random
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError("Run: python -m pip install google-genai")

try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    HAS_TENACITY = True
except ImportError:
    HAS_TENACITY = False
    print("[WARNING] tenacity not installed - no API retry. Run: python -m pip install tenacity")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DB_PATH        = os.getenv("ORACLE_DB_PATH", "TSL_Archive.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FLASH_MODEL    = "gemini-2.5-flash"
PRO_MODEL = "gemini-2.5-pro"
AUTHOR_ID      = "322498632542846987"
AUTHOR_NAME    = "TheWitt"
CACHE_DIR      = ".echo_cache"
OUTPUT_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "echo")

CLASSIFY_BATCH_SIZE = 150
FLASH_PASS_DELAY    = 6     # seconds between Flash extraction passes
CLASSIFY_DELAY      = 8     # seconds between classification batches
PRO_DELAY           = 4     # seconds between Pro synthesis calls

# TSL domain context injected into synthesis prompts
TSL_CONTEXT = """TSL (The Simulation League) is a competitive online Madden NFL simulation
league with an active Discord community. TheWitt is the founding commissioner who has run
it since 2021. Members are called GMs. League activities include weekly games, trades,
free agent pickups, power rankings, performance grades, disciplinary rulings, and heavy
trash talk. TheWitt is both authority figure and participant - he rules AND competes AND
banters with GMs he has real relationships with spanning years."""

# Phrases that indicate AI-generated content pasted into Discord - exclude these
AI_CONTENT_FILTERS = [
    "Once upon a time",
    "Based solely on chat log",
    "Based on the totality",
    "Core Philosophy:",
    "Here is a",
    "Here's a",
    "I'll analyze",
    "As an AI",
    "Language Model",
    "It's important to note",
    "In conclusion",
    "I would suggest",
    "Furthermore,",
    "In summary,",
    "To summarize",
    "I cannot",
    "As a language",
    "Please note that",
]

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def get_connection():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}  |  Set ORACLE_DB_PATH in .env"
        )
    return sqlite3.connect(DB_PATH)


def fetch_samples(conn) -> tuple:
    """
    Pull stratified message samples across six buckets:
      short         - 3000 random 2-80 char messages (the core voice)
      medium        - ALL 81-300 char messages
      recent        - last 18 months (captures current voice evolution)
      chains        - consecutive 3+ message runs (reveals rhythm)
      top_reacted   - highest engagement with reaction counts attached
      mixed         - broad random for vocabulary/structure passes
      early/late    - temporal split for drift detection
    """
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM messages WHERE author_id = ?", (AUTHOR_ID,))
    total = cur.fetchone()[0]
    if total == 0:
        raise ValueError(f"No messages found for author_id {AUTHOR_ID}")
    print(f"    -> Total messages in archive: {total:,}")

    # Parameterized AI content filter - safe for apostrophes in phrases
    ai_clauses = " AND ".join("content NOT LIKE ?" for _ in AI_CONTENT_FILTERS)
    ai_params  = tuple(f"%{phrase}%" for phrase in AI_CONTENT_FILTERS)

    samples = {}

    # SHORT - the core voice
    cur.execute(f"""
        SELECT content FROM messages
        WHERE author_id = ?
          AND LENGTH(TRIM(content)) BETWEEN 2 AND 80
          AND content NOT LIKE 'http%'
          AND type = 'Default'
          AND {ai_clauses}
        ORDER BY RANDOM() LIMIT 3000
    """, (AUTHOR_ID,) + ai_params)
    samples["short"] = [r[0] for r in cur.fetchall()]
    print(f"    -> short (2-80 chars)      {len(samples['short']):>5,} sampled")

    # MEDIUM - take all of them
    cur.execute(f"""
        SELECT content FROM messages
        WHERE author_id = ?
          AND LENGTH(TRIM(content)) BETWEEN 81 AND 300
          AND content NOT LIKE 'http%'
          AND type = 'Default'
          AND {ai_clauses}
        ORDER BY RANDOM()
    """, (AUTHOR_ID,) + ai_params)
    samples["medium"] = [r[0] for r in cur.fetchall()]
    print(f"    -> medium (81-300 chars)   {len(samples['medium']):>5,} sampled (ALL)")

    # RECENT - last 18 months, captures current voice
    cutoff_ts = int(time.time()) - (18 * 30 * 24 * 3600)
    cur.execute(f"""
        SELECT content FROM messages
        WHERE author_id = ?
          AND timestamp_unix > ?
          AND LENGTH(TRIM(content)) BETWEEN 2 AND 300
          AND content NOT LIKE 'http%'
          AND type = 'Default'
          AND {ai_clauses}
        ORDER BY timestamp_unix DESC LIMIT 2000
    """, (AUTHOR_ID, cutoff_ts) + ai_params)
    samples["recent"] = [r[0] for r in cur.fetchall()]
    print(f"    -> recent (last 18mo)      {len(samples['recent']):>5,} sampled")

    # CONSECUTIVE CHAINS - rapid-fire message sequences (within 90 seconds)
    # Reveals rhythm, escalation, how he builds an argument across short messages
    cur.execute("""
        SELECT content, timestamp_unix FROM messages
        WHERE author_id = ?
          AND LENGTH(TRIM(content)) BETWEEN 2 AND 300
          AND content NOT LIKE 'http%'
          AND type = 'Default'
        ORDER BY timestamp_unix ASC
        LIMIT 50000
    """, (AUTHOR_ID,))
    all_timed = cur.fetchall()

    chains = []
    if all_timed:
        current_chain = [all_timed[0][0]]
        for i in range(1, len(all_timed)):
            prev_ts = all_timed[i-1][1]
            curr_ts = all_timed[i][1]
            gap = (curr_ts - prev_ts) if (prev_ts and curr_ts) else 9999
            if gap < 90:
                current_chain.append(all_timed[i][0])
            else:
                if len(current_chain) >= 3:
                    chains.append(" | ".join(current_chain))
                current_chain = [all_timed[i][0]]
        if len(current_chain) >= 3:
            chains.append(" | ".join(current_chain))

    random.shuffle(chains)
    samples["chains"] = chains[:150]
    print(f"    -> consecutive chains      {len(samples['chains']):>5,} sampled (3+ msg bursts)")

    # TOP REACTED - with reaction counts attached so Pro knows what landed
    cur.execute(f"""
        SELECT content, reaction_count FROM messages
        WHERE author_id = ?
          AND reaction_count > 0
          AND LENGTH(TRIM(content)) BETWEEN 2 AND 300
          AND content NOT LIKE 'http%'
          AND {ai_clauses}
        ORDER BY reaction_count DESC LIMIT 200
    """, (AUTHOR_ID,) + ai_params)
    rows = cur.fetchall()
    samples["top_reacted"]           = [r[0] for r in rows]
    samples["top_reacted_annotated"] = [f"[{r[1]} reactions] {r[0]}" for r in rows]
    print(f"    -> top reacted             {len(samples['top_reacted']):>5,} sampled")

    # BROAD MIXED - for vocabulary/structure passes
    cur.execute(f"""
        SELECT content FROM messages
        WHERE author_id = ?
          AND LENGTH(TRIM(content)) BETWEEN 2 AND 300
          AND content NOT LIKE 'http%'
          AND type = 'Default'
          AND {ai_clauses}
        ORDER BY RANDOM() LIMIT 3000
    """, (AUTHOR_ID,) + ai_params)
    samples["mixed"] = [r[0] for r in cur.fetchall()]
    print(f"    -> mixed (broad)           {len(samples['mixed']):>5,} sampled")

    # TEMPORAL SPLIT - early (pre-2023) vs late (2024+) for voice drift detection
    # 2023-01-01 = 1672531200 unix, 2024-01-01 = 1704067200 unix
    cur.execute(f"""
        SELECT content FROM messages
        WHERE author_id = ?
          AND timestamp_unix < 1672531200
          AND LENGTH(TRIM(content)) BETWEEN 2 AND 300
          AND content NOT LIKE 'http%'
          AND type = 'Default'
          AND {ai_clauses}
        ORDER BY RANDOM() LIMIT 300
    """, (AUTHOR_ID,) + ai_params)
    samples["early"] = [r[0] for r in cur.fetchall()]

    cur.execute(f"""
        SELECT content FROM messages
        WHERE author_id = ?
          AND timestamp_unix > 1704067200
          AND LENGTH(TRIM(content)) BETWEEN 2 AND 300
          AND content NOT LIKE 'http%'
          AND type = 'Default'
          AND {ai_clauses}
        ORDER BY RANDOM() LIMIT 300
    """, (AUTHOR_ID,) + ai_params)
    samples["late"] = [r[0] for r in cur.fetchall()]
    print(f"    -> temporal early/late     {len(samples['early']):>4,} / {len(samples['late'])}")

    return samples, total


# ---------------------------------------------------------------------------
# GEMINI CLIENT
# ---------------------------------------------------------------------------

def get_client():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in .env")
    return genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# CACHE
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{key}.json")


def get_cache(key: str):
    path = _cache_path(key)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def set_cache(key: str, data):
    path = _cache_path(key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def clear_cache():
    """Windows-safe cache clearing - individual file deletion, no shutil.rmtree."""
    if not os.path.exists(CACHE_DIR):
        print("[*] No cache to clear.")
        return
    deleted = 0
    for fname in os.listdir(CACHE_DIR):
        try:
            os.remove(os.path.join(CACHE_DIR, fname))
            deleted += 1
        except OSError as e:
            print(f"    [!] Could not delete {fname}: {e}")
    try:
        os.rmdir(CACHE_DIR)
    except OSError:
        pass
    print(f"[*] Cache cleared ({deleted} files).")


def make_cache_key(pass_name: str, msgs: list) -> str:
    """
    Collision-resistant cache key using full content hash.
    Previous version only hashed 20 msgs x 40 chars - collision risk.
    """
    full_content = "".join(str(m) for m in msgs)
    return hashlib.md5(f"{AUTHOR_ID}_{pass_name}_{full_content}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# JSON REPAIR
# ---------------------------------------------------------------------------

def parse_json_safe(raw: str) -> dict:
    """Parse JSON from model output with structural repair for truncated responses."""
    raw = re.sub(r"```json\s*|```\s*", "", raw).strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Repair: scan for last valid complete JSON structure
    depth       = 0
    in_string   = False
    escape_next = False
    last_safe   = -1  # -1 not 0 - avoids falsy bug when valid JSON ends at position 0

    for i, ch in enumerate(raw):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
        if not in_string:
            if ch in '{[':
                depth += 1
            elif ch in '}]':
                depth -= 1
                if depth == 0:
                    last_safe = i

    if last_safe >= 0:
        try:
            result = json.loads(raw[:last_safe + 1])
            print(f"        -> JSON repaired at char {last_safe}")
            return result
        except Exception:
            pass

    print(f"        -> [WARNING] JSON parse failed completely, storing raw")
    return {"raw": raw}


# ---------------------------------------------------------------------------
# API CALLS WITH RETRY
# ---------------------------------------------------------------------------

def _flash_inner(client, prompt: str) -> dict:
    response = client.models.generate_content(
        model=FLASH_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.15
        )
    )
    return parse_json_safe(response.text)


def _pro_inner(client, prompt: str) -> str:
    response = client.models.generate_content(
        model=PRO_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=8192   # was 4096 - was truncating rich personas mid-section
        )
    )
    return response.text.strip()


if HAS_TENACITY:
    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=8, max=60),
        retry=retry_if_exception_type(Exception),
        reraise=True
    )
    def _flash_with_retry(client, prompt):
        return _flash_inner(client, prompt)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=3, min=15, max=90),
        retry=retry_if_exception_type(Exception),
        reraise=True
    )
    def _pro_with_retry(client, prompt):
        return _pro_inner(client, prompt)
else:
    _flash_with_retry = _flash_inner
    _pro_with_retry   = _pro_inner


def call_flash(client, prompt: str, cache_key: str = None) -> dict:
    if cache_key:
        cached = get_cache(cache_key)
        if cached:
            print(f"        -> cache hit")
            return cached
    result = _flash_with_retry(client, prompt)
    if cache_key and "raw" not in result:
        set_cache(cache_key, result)
    return result


def call_pro(client, prompt: str) -> str:
    return _pro_with_retry(client, prompt)


# ---------------------------------------------------------------------------
# FORMATTING HELPERS
# ---------------------------------------------------------------------------

def fmt(msgs: list, limit: int) -> str:
    return "\n---\n".join(str(m) for m in msgs[:limit] if m and str(m).strip())


def fmt_annotated(msgs: list, limit: int) -> str:
    """For pre-annotated messages like '[12 reactions] message text'."""
    return "\n".join(str(m) for m in msgs[:limit] if m and str(m).strip())


# ---------------------------------------------------------------------------
# PRE-PASS: CONTENT CLASSIFICATION
# ---------------------------------------------------------------------------

CLASSIFY_PROMPT = """
You are classifying Discord messages from a Madden NFL sim league commissioner
into three categories. Classify by content only - no channel names are available.

LEAGUE CONTEXT: TSL is a competitive Madden sim league. GMs manage teams, make
trades, play weekly games. TheWitt runs the league and competes in it.

CATEGORIES:
- casual:     banter, trash talk, reactions, jokes, short responses, hype,
              arguments, general chat, personal comments, emoji reactions.
              Short one-word or emoji-only messages are casual by default.

- official:   league rulings, announcements, decisions, disciplinary actions,
              trade approvals/vetoes, schedule info, waiver rulings, rule
              clarifications. CAN BE VERY SHORT. "Trade approved." is official.
              "Veto. Collusion." is a ruling. Look for finality and decisiveness.

- analytical: stat discussion, game recaps, player/team performance grades,
              power rankings, trade analysis, season commentary, matchup
              breakdowns, hot takes on player value. CAN BE SHORT.
              "dude has been trash all season" is analytical.

Messages to classify:
{messages}

Return JSON only:
{{
  "casual":     ["exact message text", ...],
  "official":   ["exact message text", ...],
  "analytical": ["exact message text", ...]
}}

Every message must appear in exactly one category. Do not skip any messages.
"""


def classify_messages(client, msgs: list) -> dict:
    """Classify messages into registers by content using Flash, batches of 400."""
    print(f"    [Flash] Classifying {len(msgs)} messages by content...")
    classified = {"casual": [], "official": [], "analytical": []}
    batches = [msgs[i:i+CLASSIFY_BATCH_SIZE] for i in range(0, len(msgs), CLASSIFY_BATCH_SIZE)]

    for idx, batch in enumerate(batches):
        print(f"        -> batch {idx+1}/{len(batches)} ({len(batch)} msgs)...")
        ck = make_cache_key(f"classify_{idx}_v4", batch)
        cached = get_cache(ck)
        if cached:
            print(f"           cache hit")
            for reg in classified:
                classified[reg].extend(cached.get(reg, []))
            continue

        prompt = CLASSIFY_PROMPT.format(messages=fmt(batch, len(batch)))
        result = call_flash(client, prompt, None)

        batch_result = {reg: result.get(reg, []) for reg in classified}
        for reg in classified:
            classified[reg].extend(batch_result[reg])
        set_cache(ck, batch_result)

        if idx < len(batches) - 1:
            time.sleep(CLASSIFY_DELAY)

    total_c = sum(len(v) for v in classified.values())
    print(f"    -> casual={len(classified['casual'])} | official={len(classified['official'])} | analytical={len(classified['analytical'])} | total={total_c}")
    return classified


# ---------------------------------------------------------------------------
# PASS A: SIGNAL EXTRACTION (Flash, 8 passes)
# ---------------------------------------------------------------------------

EXTRACTION_PROMPTS = {

"structure": """
Analyze these {count} Discord messages from one person in a Madden sim league.

IMPORTANT: This is a short-form communicator. Most messages are under 80 characters.
Do NOT invent longform patterns. Extract only what you actually observe.

Messages:
{messages}

Return JSON:
{{
  "dominant_length": "honest word count estimate - under 5 words? under 10? give real data from what you see",
  "punctuation_style": "precise description - period/no period/exclamation/ellipsis patterns with examples",
  "capitalization": "exactly what you see - all lowercase? random caps? all-caps for emphasis? mixed?",
  "line_break_usage": "single block vs multi-line - frequency and what triggers breaks",
  "message_chunking": "single message or rapid-fire multi-message - what pattern do you observe",
  "discord_formatting": "bold/italic/code blocks - frequency and strategic use, or never used",
  "common_openers": ["15 exact words or short phrases that START messages - verbatim only, no paraphrasing"],
  "common_closers": ["15 exact words or short phrases that END messages - verbatim only"],
  "recurring_phrases": ["20 exact phrases that repeat across messages - verbatim only"],
  "structural_fingerprints": ["8 structural habits that make this writing recognizable at a glance"],
  "notable_absences": "formatting or structural patterns this person clearly never uses"
}}
""",

"vocabulary": """
Analyze these {count} Discord messages from one person in a Madden sim league.
PULL VOCABULARY VERBATIM FROM THE TEXT. Do not generalize, paraphrase, or invent.
Every item in every list must be an exact quote from the messages provided.

Messages:
{messages}

Return JSON:
{{
  "signature_words": ["30 words or short phrases that fingerprint this voice - exact as written"],
  "hype_phrases": ["exact phrases when excited, celebrating, or hyping - verbatim"],
  "authority_phrases": ["exact phrases for decisions, rulings, official statements - verbatim"],
  "dismissal_phrases": ["exact phrases to shut down, reject, or dismiss - verbatim"],
  "trash_talk_phrases": ["exact phrases in competitive banter and callouts - verbatim"],
  "affection_phrases": ["exact phrases toward people this person genuinely likes - verbatim"],
  "community_terms": ["TSL-specific words, GM nicknames, recurring references - verbatim"],
  "intensifiers": ["exact words and patterns used for emphasis - verbatim"],
  "sports_language": ["Madden/NFL and sim league terms used naturally and repeatedly - verbatim"],
  "profanity_pattern": "frequency and specific words used - describe only what appears in the text",
  "forbidden_territory": ["corporate, formal, or AI-speak phrases this person clearly never uses"]
}}
""",

"casual_patterns": """
Analyze these {count} casual Discord messages from a Madden sim league commissioner.
CONTEXT: Short-form communicator. Most messages under 80 chars.
Only extract patterns genuinely present in the data. Do not pad.

Messages:
{messages}

Return JSON:
{{
  "baseline_energy": "natural resting energy of this voice in 2 sentences with specific verbatim examples",
  "humor_mechanics": "sarcasm style, hyperbole, absurdism, timing - real examples from the text",
  "emoji_reality": "exactly which emoji appear and how often - only what you actually see",
  "trash_talk_style": "targets, escalation patterns, specific phrases - real verbatim examples",
  "reaction_patterns": "how they respond to wins, losses, drama, hot takes - specific observed patterns",
  "one_liner_examples": ["15 actual one-liners verbatim from the messages"],
  "address_style": "how they refer to other people - first names, nicknames, callout patterns",
  "dominance_signals": ["12 phrases or patterns that signal control of the room - verbatim"],
  "expansion_triggers": "topics or situations that get more than 2-3 sentences - cite examples",
  "silence_triggers": "what gets a one-word dismissal or no response - if visible in data"
}}
""",

"official_patterns": """
Analyze these {count} official/governance-related Discord messages from a sim league commissioner.
CONTEXT: Official messages can be VERY short. "Trade approved." is official.
"No. Rules are clear." is a ruling. Look for decisiveness and finality, not length.

Messages:
{messages}

Return JSON:
{{
  "ruling_style": "how decisions land - direct declaration? brief explanation? context then ruling?",
  "authority_signals": ["exact phrases that signal finality or official status - verbatim"],
  "tone_shift": "specific ways voice changes from casual when being official",
  "official_openers": ["10 exact phrases or words that start official statements - verbatim"],
  "official_closers": ["10 exact phrases that close official statements - verbatim"],
  "formality_spectrum": "how formal does this person actually get - note what casual traits survive",
  "emphasis_tools": "how key points are flagged - caps, bold, line breaks, repetition",
  "what_stays_casual": "specific casual traits that bleed through even in official mode",
  "ruling_examples": ["6 actual ruling or announcement examples verbatim from the messages"]
}}
""",

"analytical_patterns": """
Analyze these {count} analytical/stats-related Discord messages from a sim league commissioner.
CONTEXT: Analytical messages can be SHORT. A hot take counts. A one-sentence grade counts.
Look for opinion delivery, performance commentary, and comparative language.

Messages:
{messages}

Return JSON:
{{
  "analytical_style": "how analysis lands - confident assertion? question? ranking callout?",
  "data_embedding": "how stats and numbers appear in natural speech - give verbatim examples",
  "opinion_confidence": "does this voice hedge analysis or deliver it as fact? verbatim examples",
  "callout_phrases": ["exact phrases spotlighting great or terrible performances - verbatim"],
  "comparison_language": ["exact phrases when comparing players, teams, seasons - verbatim"],
  "analytical_openers": ["10 exact phrases that start analytical messages - verbatim"],
  "hot_take_structure": "how a hot take is constructed - setup, delivery, punctuation pattern",
  "grade_language": ["exact grading or rating phrases used - verbatim"],
  "analytical_fingerprints": ["8 things that make this person's analysis voice recognizable vs generic"],
  "analytical_examples": ["6 actual analytical messages verbatim from the data"]
}}
""",

"voice_identity": """
Analyze these {count} Discord messages - the top-reacted messages from a Madden sim league
commissioner. Reaction counts are shown in brackets. Higher counts = more community impact.

These are the messages the community responded to most. They reveal what version of this
voice lands hardest and what the community values from this commissioner.

Messages (format: [N reactions] message):
{messages}

Return JSON:
{{
  "voice_portrait": "vivid 3-sentence description - honest about short-form nature, specific not generic",
  "what_makes_it_land": "pattern analysis of why high-reaction messages work - be precise",
  "confidence_calibration": "dominance level characterization with specific verbatim examples",
  "directness_level": "does this voice hedge? never/rarely/sometimes - with exact examples from text",
  "conflict_mode": "how pushback and disagreement are handled - specific patterns observed",
  "irony_fingerprint": "estimate percentage ironic/sarcastic, describe exactly how it manifests",
  "serious_vs_performance": "how to tell genuine seriousness from banter performance - specific tells",
  "rhetorical_signature": ["8 rhetorical moves with verbatim examples from the text"],
  "unmistakable_fingerprints": ["15 things that make this voice instantly recognizable - be specific"],
  "community_position": "how this voice positions relative to community - peer, authority, or both"
}}
""",

"temporal_drift": """
You are comparing two sets of Discord messages from the same person across different time periods.

EARLY MESSAGES (2021-2022):
{early_messages}

RECENT MESSAGES (2024-2025):
{recent_messages}

Identify how this voice has evolved. Be specific. Do not generalize.
If the voice is essentially the same, say so honestly.

Return JSON:
{{
  "vocabulary_drift": "specific words or phrases in recent but not early - verbatim examples",
  "style_drift": "how sentence structure or punctuation changed if at all",
  "tone_drift": "did confidence, aggression, humor style change? give specific examples",
  "topic_drift": "what subjects dominate early vs recent messages",
  "stable_core": "what has NOT changed - the permanent fingerprints across both periods",
  "verdict": "one sentence: use RECENT voice as baseline, or are they essentially the same?",
  "recent_signature": ["12 phrases from the recent period that define the current voice - verbatim"]
}}
""",

"chain_patterns": """
Analyze these consecutive message sequences from one person in a Discord server.
Each sequence shows multiple messages sent in rapid succession (within 90 seconds).
Messages within a chain are separated by ' | '.

These chains reveal the most unfiltered version of this voice - how thoughts are
broken across rapid-fire messages, how arguments build, how reactions escalate.

Chains:
{messages}

Return JSON:
{{
  "chain_rhythm": "how this person breaks thoughts across messages - fragments? complete thoughts?",
  "escalation_pattern": "how chains escalate or de-escalate - do messages get shorter? more caps?",
  "chain_openers": ["12 exact first messages in chains - verbatim, shows how topics are entered"],
  "chain_closers": ["12 exact last messages in chains - verbatim, shows how topics are exited"],
  "fragmentation_style": "how a single idea gets split across 2-4 messages - describe the pattern",
  "chain_examples": ["5 most interesting complete chain sequences verbatim"],
  "rhythm_signature": "2-sentence description of the overall rapid-fire rhythm pattern"
}}
"""

}


def run_extraction_pass(client, samples: dict, classified: dict) -> dict:
    """Run all 8 Flash extraction passes with targeted message sets per pass."""
    signals = {}

    # Build pass-specific pools
    # Official and analytical are kept SEPARATE to avoid cross-contamination
    casual_pool     = list(dict.fromkeys(classified.get("casual", []) + samples["short"]))[:1500]

    official_raw    = classified.get("official", [])
    official_pool   = list(dict.fromkeys(official_raw))[:500]
    if len(official_pool) < 80:
        # Thin classification - pad with medium messages
        official_pool = list(dict.fromkeys(official_pool + samples["medium"][:300]))[:500]

    analytical_raw  = classified.get("analytical", [])
    analytical_pool = list(dict.fromkeys(analytical_raw))[:500]
    if len(analytical_pool) < 80:
        # Thin classification - pad with medium messages
        analytical_pool = list(dict.fromkeys(analytical_pool + samples["medium"][:300]))[:500]

    pass_configs = {
        "structure":          ("standard",    samples["mixed"][:2000],       "MIXED"),
        "vocabulary":         ("standard",    samples["recent"][:1500],      "RECENT"),
        "casual_patterns":    ("standard",    casual_pool[:1200],            "CASUAL"),
        "official_patterns":  ("standard",    official_pool[:400],           "OFFICIAL"),
        "analytical_patterns":("standard",    analytical_pool[:400],         "ANALYTICAL"),
        "voice_identity":     ("annotated",   None,                          "TOP REACTED"),
        "temporal_drift":     ("temporal",    None,                          "EARLY vs LATE"),
        "chain_patterns":     ("standard",    samples["chains"][:120],       "CHAINS"),
    }

    pass_names = list(pass_configs.keys())

    for pass_name, (mode, msgs, label) in pass_configs.items():
        print(f"    [Flash] {pass_name}  ({label})...")

        if mode == "annotated":
            annotated = samples.get("top_reacted_annotated", [])
            if not annotated:
                print(f"        -> SKIPPED: no top-reacted messages")
                signals[pass_name] = {}
                continue
            ck = make_cache_key(pass_name + "_v4", annotated[:200])
            prompt = EXTRACTION_PROMPTS[pass_name].format(
                messages=fmt_annotated(annotated, 200),
                count=len(annotated[:200])
            )
            signals[pass_name] = call_flash(client, prompt, ck)

        elif mode == "temporal":
            early = samples.get("early", [])
            late  = samples.get("late", [])
            if not early or not late:
                print(f"        -> SKIPPED: insufficient temporal data")
                signals[pass_name] = {}
                continue
            ck = make_cache_key(pass_name + "_v4", early[:150] + late[:150])
            prompt = EXTRACTION_PROMPTS[pass_name].format(
                early_messages=fmt(early, 200),
                recent_messages=fmt(late, 200)
            )
            signals[pass_name] = call_flash(client, prompt, ck)

        else:
            if not msgs:
                print(f"        -> SKIPPED: no messages")
                signals[pass_name] = {}
                continue
            actual = len(msgs)
            ck = make_cache_key(pass_name + "_v4", msgs)
            prompt = EXTRACTION_PROMPTS[pass_name].format(
                messages=fmt(msgs, actual),
                count=actual
            )
            signals[pass_name] = call_flash(client, prompt, ck)

        key_count = len(signals.get(pass_name, {}))
        print(f"        -> {key_count} signal keys extracted")
        if pass_name != pass_names[-1]:
            time.sleep(FLASH_PASS_DELAY)

    return signals


# ---------------------------------------------------------------------------
# PASS B: PERSONA SYNTHESIS (Pro, 3 passes)
# ---------------------------------------------------------------------------

HALLUCINATION_GUARD = """
HALLUCINATION GUARD - READ BEFORE WRITING:
You have been given extracted signals with verbatim quotes from real messages.
Only write rules and examples directly supported by the signal data.
If a signal field is empty or sparse, acknowledge the data is thin rather than
inventing plausible-sounding patterns. A prompt with 10 accurate rules beats
one with 15 rules where 5 are invented. The model reading this prompt will
behave based on what it says - invented patterns will corrupt the output voice.
"""

SYNTHESIS_TEMPLATES = {

"casual": """
You are building ATLAS Echo - a Discord AI that speaks in the EXACT voice of
TSL commissioner TheWitt for casual Discord interactions.

TSL CONTEXT:
{tsl_context}

{hallucination_guard}

DATA REALITY:
222,385 Discord messages analyzed. 215,000+ are under 80 characters.
This voice is punchy, direct, and delivered in short bursts.
Brevity is the power. Do NOT write rules implying paragraphs exist.

This output IS the system prompt Gemini receives at runtime for @mentions,
banter, trash talk, general chat, and reactions. Write as direct second-person
instructions. No vague guidance. Every rule must be instantly actionable.

EXTRACTED VOICE SIGNALS:
{signals}

HIGHEST-ENGAGEMENT MESSAGES (reaction counts shown - these are the messages that LANDED):
{top_reacted}

CONSECUTIVE MESSAGE CHAINS (rapid-fire voice at its most unfiltered):
{chains}

TEMPORAL NOTE: {temporal_verdict}

Write 1000-1200 words with these exact sections:

# IDENTITY
Who ATLAS Echo is. What TSL is. What authority Echo carries. 3-4 sentences max.

# VOICE FINGERPRINT
12 non-negotiable rules of this voice. Each on its own line.
Format: RULE: [exact actionable instruction with a real verbatim example from the data]
Order by importance. Short is always the default.

# VOCABULARY
ALWAYS USE: [minimum 20 exact words/phrases from the signal data - verbatim only]
NEVER USE: [minimum 12 exact phrases that would immediately break this voice]

# CASUAL MECHANICS
Explicit instruction for each scenario below - be prescriptive:
- Someone @mentions ATLAS with a question
- Active trash talk or beef in the server
- Something hype happens (big win, wild trade, dramatic upset)
- Someone says something factually wrong or stupid
- Default expected response length and format

# ENERGY AND RHYTHM
Exact sentence length rules with examples from the data.
Punctuation rules - what gets used and what never appears.
Emoji rules - specific emoji, frequency, what they signal.
How to open a casual message. How to close one.
How rapid-fire chain messages sound different from single messages.

# HARD STOPS
10 things Echo must NEVER do that would break this voice immediately.
Each one names a specific failure mode - not vague ("don't be formal")
but exact ("never open with 'Certainly!' or 'Great question!'").
""",

"official": """
You are building ATLAS Echo - a Discord AI that speaks in the EXACT voice of
TSL commissioner TheWitt for official league communications.

TSL CONTEXT:
{tsl_context}

{hallucination_guard}

CRITICAL CONTEXT:
This commissioner speaks in short, direct bursts even when official.
Official does NOT mean verbose. A ruling can be one sentence.
"Trade approved." is a complete official message.
"Veto. Collusion." is a complete ruling.
Authority comes from decisiveness, not length or formal language.

This IS the system prompt Gemini receives for trade rulings, disciplinary
decisions, league announcements, schedule drops, and governance communications.

EXTRACTED VOICE SIGNALS:
{signals}

OFFICIAL MESSAGE SAMPLES FROM THE ARCHIVE:
{official_msgs}

TEMPORAL NOTE: {temporal_verdict}

Write 1000-1200 words with these sections:

# IDENTITY AND AUTHORITY
Who Echo is in official mode. What weight it carries in TSL. 3-4 sentences.

# OFFICIAL VOICE RULES
12 rules for the official register.
Format: RULE: [instruction specifying what changes from casual and what stays the same]

# VOCABULARY
OFFICIAL VOCABULARY: [minimum 15 authority phrases from the signal data - verbatim]
NEVER IN OFFICIAL MODE: [minimum 12 phrases that undermine authority or sound robotic]

# DELIVERY TEMPLATES
Short-form template for a TRADE RULING (approved or vetoed)
Short-form template for a DISCIPLINARY DECISION
Short-form template for a LEAGUE ANNOUNCEMENT
Use [BRACKETS] for variable content. Match the actual short-form style in the signals.

# DISCORD FORMATTING
When to use bold, line breaks, @mentions in official messages.
Concrete rules - not guidelines.

# HARD STOPS
10 things Echo must never do in official mode.
First three must address the risk of sounding like a legal document or HR email.
""",

"analytical": """
You are building ATLAS Echo - a Discord AI that speaks in the EXACT voice of
TSL commissioner TheWitt for Madden sim league analytical content.

TSL CONTEXT:
{tsl_context}

{hallucination_guard}

CRITICAL CONTEXT:
Analytical output from this person is short, hot, and opinionated.
A power ranking callout is 2 sentences. A trade grade is one punchy line.
The voice does NOT become academic when analyzing - it stays direct and editorial.
"Dude has been trash all season" is a valid analytical output from this voice.

This IS the system prompt Gemini receives for weekly recaps, player grades,
trade analysis, power rankings, matchup previews, and performance commentary.

EXTRACTED VOICE SIGNALS:
{signals}

ANALYTICAL MESSAGE SAMPLES FROM THE ARCHIVE:
{analytical_msgs}

TEMPORAL NOTE: {temporal_verdict}

Write 1000-1200 words with these sections:

# IDENTITY AND ANALYTICAL ROLE
What Echo is in analytical mode. What data and context it draws from. 3-4 sentences.

# ANALYTICAL VOICE RULES
12 rules for the analytical register.
Format: RULE: [instruction with a specific example from the signal data]

# DATA PRESENTATION
Exact technique for embedding stats in natural speech.
How to make one number land harder than five numbers.
How to editorialize around data without losing credibility.

# VOCABULARY
ANALYTICAL VOCABULARY: [minimum 15 phrases for analytical mode - verbatim from signals]
NEVER IN ANALYSIS: [minimum 12 phrases that make output sound like a generic AI report]

# CONTENT TEMPLATES
Short-form template for: WEEKLY GAME RECAP (4-5 sentences max)
Short-form template for: PLAYER PERFORMANCE GRADE (2-3 sentences)
Short-form template for: TRADE REACTION (1-2 sentences)
Short-form template for: POWER RANKING CALLOUT (1-2 sentences per team)
Templates must match the actual voice patterns in the signal data.

# HARD STOPS
10 things that make analytical output sound un-Echo.
At least 3 must address the risk of sounding like a generic ESPN broadcast.
"""

}


def run_synthesis_pass(client, signals: dict, samples: dict, classified: dict, total: int) -> dict:
    """Pass B: Synthesize three register system prompts using Pro."""
    personas    = {}
    signals_str = json.dumps(signals, indent=2)

    top_reacted     = fmt_annotated(samples.get("top_reacted_annotated", []), 100)
    chains_fmt      = fmt(samples.get("chains", []), 60)
    official_msgs   = fmt(classified.get("official", []), 100)
    analytical_msgs = fmt(classified.get("analytical", []), 100)

    # Extract temporal verdict from signals for injection into all three prompts
    temporal        = signals.get("temporal_drift", {})
    temporal_verdict = temporal.get(
        "verdict",
        "Insufficient temporal data - treat full archive as single baseline."
    )

    configs = [
        ("casual", SYNTHESIS_TEMPLATES["casual"].format(
            tsl_context=TSL_CONTEXT,
            hallucination_guard=HALLUCINATION_GUARD,
            signals=signals_str,
            top_reacted=top_reacted,
            chains=chains_fmt,
            temporal_verdict=temporal_verdict
        )),
        ("official", SYNTHESIS_TEMPLATES["official"].format(
            tsl_context=TSL_CONTEXT,
            hallucination_guard=HALLUCINATION_GUARD,
            signals=signals_str,
            official_msgs=official_msgs,
            temporal_verdict=temporal_verdict
        )),
        ("analytical", SYNTHESIS_TEMPLATES["analytical"].format(
            tsl_context=TSL_CONTEXT,
            hallucination_guard=HALLUCINATION_GUARD,
            signals=signals_str,
            analytical_msgs=analytical_msgs,
            temporal_verdict=temporal_verdict
        )),
    ]

    for register, prompt in configs:
        print(f"    [Pro] Synthesizing {register} persona...")
        result = call_pro(client, prompt)
        personas[register] = result
        print(f"        -> {len(result.split()):,} words generated")
        if register != "analytical":
            time.sleep(PRO_DELAY)

    return personas


# ---------------------------------------------------------------------------
# PASS C: VALIDATION (Flash)
# ---------------------------------------------------------------------------

VALIDATION_PROMPT = """
You are auditing a system prompt that was generated to replicate someone's Discord voice.
Check whether it is grounded in the signal data or contains hallucinated patterns.

EXTRACTED SIGNAL DATA (ground truth from real messages):
{signals}

GENERATED SYSTEM PROMPT TO AUDIT:
{persona}

Return JSON:
{{
  "grounded_rules": ["rules or instructions clearly supported by the signal data"],
  "suspect_rules": ["rules or instructions that may be invented and not in the signal data"],
  "missing_signals": ["strong signals from the data that the prompt failed to capture"],
  "vocabulary_accuracy": "are the ALWAYS USE vocabulary items verbatim from the data? yes/partial/no",
  "overall_verdict": "one sentence verdict on quality and accuracy",
  "confidence_score": 0-100
}}
"""


def run_validation_pass(client, signals: dict, personas: dict) -> dict:
    """Pass C: Flash validates each generated persona against extracted signal data."""
    validation  = {}
    signals_str = json.dumps(signals, indent=2)

    for register, persona_text in personas.items():
        print(f"    [Flash] Validating {register} persona...")
        prompt = VALIDATION_PROMPT.format(
            signals=signals_str,
            persona=persona_text[:6000]
        )
        result = call_flash(client, prompt, None)
        validation[register] = result
        score   = result.get("confidence_score", "?")
        verdict = result.get("overall_verdict", "")
        suspect = len(result.get("suspect_rules", []))
        missing = len(result.get("missing_signals", []))
        print(f"        -> score={score}/100 | suspect={suspect} rules | missing={missing} signals")
        if register != list(personas.keys())[-1]:
            time.sleep(4)

    return validation


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def build_metadata_header(register: str, total: int, classified: dict, validation: dict) -> str:
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    score   = validation.get(register, {}).get("confidence_score", "N/A")
    verdict = validation.get(register, {}).get("overall_verdict", "No validation run")
    return (
        f"# ATLAS ECHO - {register.upper()} REGISTER\n"
        f"# Generated  : {now}\n"
        f"# Subject    : {AUTHOR_NAME} ({AUTHOR_ID})\n"
        f"# Archive    : {total:,} total messages analyzed\n"
        f"# Classified : casual={len(classified.get('casual',[]))} | "
        f"official={len(classified.get('official',[]))} | "
        f"analytical={len(classified.get('analytical',[]))}\n"
        f"# Validation : score={score}/100 | {verdict}\n"
        f"# Rebuild    : python echo_voice_extractor.py --no-cache\n"
        f"# {'='*55}\n\n"
    )


def save_personas(personas: dict, total: int, classified: dict, validation: dict) -> dict:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    paths = {}
    for register in ("casual", "official", "analytical"):
        if register not in personas:
            continue
        header = build_metadata_header(register, total, classified, validation)
        path   = os.path.join(OUTPUT_DIR, f"echo_{register}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + personas[register])
        paths[register] = path
        print(f"    [+] echo_{register}.txt  ({len(personas[register]):,} chars)")
    return paths


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------

def run_extraction(verbose: bool = True, validate: bool = True) -> dict:
    """
    Full extraction pipeline. Callable from echo_cog.py for /echorebuild.
    Returns dict: {register: filepath}
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"  ATLAS ECHO - VOICE EXTRACTION PIPELINE v4")
        print(f"  Subject  : {AUTHOR_NAME} ({AUTHOR_ID})")
        print(f"  Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")

    if verbose: print("[1/5] Connecting to message archive...")
    conn = get_connection()
    samples, total = fetch_samples(conn)
    conn.close()

    client = get_client()

    if verbose: print("\n[2/5] PRE-PASS - Content Classification (Flash, batches of 400)...")
    # All medium messages + 1000 short for best official/analytical coverage
    classify_pool = samples["short"][:1000] + samples["medium"][:]
    classified = classify_messages(client, classify_pool)

    if verbose: print("\n[3/5] PASS A - Signal Extraction (8 Flash passes)...")
    signals = run_extraction_pass(client, samples, classified)
    if verbose: print(f"    -> {len(signals)} signal packs extracted")

    if verbose: print("\n[4/5] PASS B - Persona Synthesis (3 Pro passes)...")
    personas = run_synthesis_pass(client, signals, samples, classified, total)

    validation = {}
    if validate:
        if verbose: print("\n[4b] PASS C - Validation (Flash)...")
        validation = run_validation_pass(client, signals, personas)

    if verbose: print("\n[5/5] Saving persona files...")
    paths = save_personas(personas, total, classified, validation)

    if verbose:
        print(f"\n{'='*60}")
        print(f"  EXTRACTION COMPLETE")
        print(f"  Messages analyzed  : {total:,}")
        print(f"  Output directory   : {OUTPUT_DIR}")
        for reg, path in paths.items():
            score = validation.get(reg, {}).get("confidence_score", "N/A")
            print(f"  {reg:<12} : {path}  [score: {score}/100]")
        print(f"  Completed : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")

    return paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ATLAS Echo - Voice Extraction Pipeline v4")
    parser.add_argument("--no-cache",    action="store_true", help="Clear cache and run fresh")
    parser.add_argument("--no-validate", action="store_true", help="Skip validation pass (faster)")
    args = parser.parse_args()

    if args.no_cache:
        clear_cache()

    run_extraction(verbose=True, validate=not args.no_validate)
