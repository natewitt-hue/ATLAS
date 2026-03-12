"""
cortex_engine.py - ATLAS Cortex Database Layer
================================================
Standalone DB query module for the Cortex cognitive intelligence pipeline.
Zero dependency on Oracle. Reads from TSL_Archive.db directly.

Builds three evidence packs:
  cognitive    - broad vocab baseline + peak candidates + message chains
  fact_check   - medium/long messages only, maximizes checkable claim density
  tone         - lightweight sample for irony pre-pass (prevents literal misreads)

Usage:
  from cortex_engine import CortexEngine
  engine = CortexEngine()
  packs = engine.get_evidence_packs("TheWitt")

Run diagnostics:
  python cortex_engine.py --subject TheWitt
"""

import sqlite3
import os
import random
from collections import defaultdict
import argparse
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("ORACLE_DB_PATH", "TSL_Archive.db")

# ---------------------------------------------------------------------------
# SAMPLE SIZE CONFIG
# All sizes chosen for high analytical confidence at ~$0.30-0.40 Flash cost.
# ---------------------------------------------------------------------------

SAMPLE_TONE              = 200    # lightweight irony pre-pass -- enough to build irony profile
SAMPLE_BROAD             = 2000   # vocabulary/structure baseline -- captures full lexical range
SAMPLE_PEAK_MEDIUM       = 500    # top cognitive-scored medium msgs (81-300 chars)
                                  # ALL long msgs (300+) are always included on top of this
SAMPLE_CHAINS            = 200    # rapid-fire chains, stratified across archive timeline
CHAIN_GAP_SECONDS        = 90     # max gap between messages to count as same chain
CHAIN_MIN_LENGTH         = 3      # minimum messages to qualify as a chain
SAMPLE_FACTCHECK         = 2000   # medium+long only -- maximizes checkable claim density
                                  # short messages almost never contain verifiable factual claims

# Phrases that indicate AI-generated content pasted into Discord -- exclude these
AI_CONTENT_FILTERS = [
    "Once upon a time", "Based solely on chat log", "Based on the totality",
    "Core Philosophy:", "Here is a", "Here's a", "I'll analyze", "As an AI",
    "Language Model", "It's important to note", "In conclusion", "I would suggest",
    "Furthermore,", "In summary,", "To summarize", "I cannot", "As a language",
    "Please note that",
]


class CortexEngine:

    def execute_sql(self, sql, params=()):
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def _ai_filter_clause(self):
        """Returns (WHERE clause string, params tuple) for AI content exclusion."""
        if not AI_CONTENT_FILTERS:
            return "1=1", ()
        clauses = " AND ".join("content NOT LIKE ?" for _ in AI_CONTENT_FILTERS)
        params  = tuple(f"%{p}%" for p in AI_CONTENT_FILTERS)
        return clauses, params

    def _build_chains(self, timed_messages):
        """
        Group consecutive messages within CHAIN_GAP_SECONDS into chains.
        Each chain = one logical thought written line-by-line across rapid messages.
        timed_messages must be sorted by timestamp_unix ASC.
        Returns list of chains, each chain is a list of message dicts.
        """
        if not timed_messages:
            return []

        chains        = []
        current_chain = [timed_messages[0]]

        for i in range(1, len(timed_messages)):
            prev_ts = timed_messages[i - 1].get("timestamp_unix")
            curr_ts = timed_messages[i].get("timestamp_unix")

            # If either timestamp is None, treat as a chain break
            if prev_ts is None or curr_ts is None:
                if len(current_chain) >= CHAIN_MIN_LENGTH:
                    chains.append(current_chain)
                current_chain = [timed_messages[i]]
                continue

            gap = curr_ts - prev_ts

            if gap <= CHAIN_GAP_SECONDS:
                current_chain.append(timed_messages[i])
            else:
                if len(current_chain) >= CHAIN_MIN_LENGTH:
                    chains.append(current_chain)
                current_chain = [timed_messages[i]]

        if len(current_chain) >= CHAIN_MIN_LENGTH:
            chains.append(current_chain)

        return chains

    def _cognitive_density_score(self, text):
        """
        Score a message for cognitive density signals.
        Used to rank medium messages for peak candidate selection.
        Higher score = more likely to reveal cognitive ceiling.
        """
        if text is None:
            return 0

        t  = str(text)
        tl = t.lower()
        s  = 0

        # Length signals
        if len(t) > 100: s += 3
        if len(t) > 200: s += 2
        if len(t) > 400: s += 2

        # Subordinate clause / structured reasoning markers
        reasoning_words = [
            "because", "therefore", "however", "although", "which means",
            "the reason", "considering", "in other words", "the fact that",
            "even though", "as a result", "that being said", "regardless",
            "the issue is", "what happens is", "the problem is",
        ]
        if any(w in tl for w in reasoning_words): s += 3

        # Numerical / statistical reasoning
        if any(c.isdigit() for c in t): s += 1
        if "%" in t or "$" in t:        s += 2

        # Analogy / cross-domain connection signals
        analogy_words = [
            "like ", "similar to", "compared to", "think of it as",
            "basically", "essentially", "analogous",
        ]
        if any(w in tl for w in analogy_words): s += 1

        # Evidence-based argumentation
        evidence_words = [
            "actually", "technically", "specifically", "according to",
            "stats show", "look at", "the data", "historically", "the thing is",
        ]
        if any(w in tl for w in evidence_words): s += 2

        # Abstract concept discussion (not just event reporting)
        abstract_words = [
            "means", "implies", "suggests", "pattern", "tendency",
            "consistent", "in general", "overall",
        ]
        if any(w in tl for w in abstract_words): s += 2

        return s

    def get_evidence_packs(self, nickname: str) -> dict | None:
        """
        Pull all three Cortex evidence packs for a subject.
        Returns None if no messages found.
        """
        print(f"\n[Cortex Engine] Building evidence packs for: {nickname}")
        search_term = f"%{nickname.replace('%', '')}%"

        ai_clause, ai_params = self._ai_filter_clause()

        # -- Full message fetch (ordered by time for chain detection) ----------
        base_sql = f"""
            SELECT message_id, author_nickname, timestamp_unix, content
            FROM messages
            WHERE author_nickname LIKE ?
              AND LENGTH(TRIM(content)) > 3
              AND content NOT LIKE 'http%'
              AND type = 'Default'
              AND {ai_clause}
            ORDER BY timestamp_unix ASC
        """
        all_msgs = self.execute_sql(base_sql, (search_term,) + ai_params)

        if not all_msgs:
            print(f"    [-] No messages found for '{nickname}'.")
            return None

        # -- Length stratification ---------------------------------------------
        short_msgs  = [m for m in all_msgs if len(str(m.get("content", "")).strip()) <= 80]
        medium_msgs = [m for m in all_msgs if 80 < len(str(m.get("content", "")).strip()) <= 300]
        long_msgs   = [m for m in all_msgs if len(str(m.get("content", "")).strip()) > 300]

        print(f"    -> Total substantive: {len(all_msgs):,}")
        print(f"       Short  (<=80):    {len(short_msgs):,}")
        print(f"       Medium (81-300):  {len(medium_msgs):,}")
        print(f"       Long   (300+):    {len(long_msgs):,}  [ALL included in peak pack]")

        # -- PACK 1: TONE (lightweight irony pre-pass) -------------------------
        # Weighted toward medium so irony in structured sentences is captured
        tone_medium = random.sample(medium_msgs, min(len(medium_msgs), SAMPLE_TONE // 2))
        tone_short  = random.sample(short_msgs,  min(len(short_msgs),  SAMPLE_TONE // 2))
        tone_pack   = tone_medium + tone_short
        random.shuffle(tone_pack)
        print(f"    -> Tone pack:         {len(tone_pack):,} msgs")

        # -- PACK 2: COGNITIVE -------------------------------------------------

        # A: Broad vocab baseline -- weighted toward medium/long
        broad_medium = random.sample(medium_msgs, min(len(medium_msgs), SAMPLE_BROAD // 2))
        broad_short_n = max(0, SAMPLE_BROAD - len(broad_medium))
        broad_short  = random.sample(short_msgs, min(len(short_msgs), broad_short_n))
        broad_sample = broad_medium + broad_short
        random.shuffle(broad_sample)

        # B: Peak candidates -- ALL longs + top cognitive-scored mediums
        scored_medium = sorted(
            medium_msgs,
            key=lambda x: self._cognitive_density_score(x.get("content", "")),
            reverse=True
        )
        peak_medium = scored_medium[:SAMPLE_PEAK_MEDIUM]
        peak_candidates = long_msgs[:] + peak_medium  # ALL longs, never capped
        print(f"    -> Cognitive broad:   {len(broad_sample):,} msgs")
        print(f"    -> Peak candidates:   {len(peak_candidates):,} msgs  "
              f"({len(long_msgs)} long + {len(peak_medium)} top-scored medium)")

        # Print sample of 3 peak candidates for visual verification
        sample_peek = peak_candidates[:3] if len(peak_candidates) >= 3 else peak_candidates
        for idx, msg in enumerate(sample_peek):
            content = str(msg.get("content", ""))[:80]
            score = self._cognitive_density_score(msg.get("content", ""))
            print(f"       [peak sample {idx+1}] score={score} | {content}")

        # C: Message chains -- stratified across archive timeline
        # Divide archive into 4 time buckets, sample chains from each
        all_chains  = self._build_chains(all_msgs)
        chains_per_bucket = SAMPLE_CHAINS // 4

        # Sort chains by their start timestamp and bucket them
        all_chains_sorted = sorted(all_chains, key=lambda c: c[0].get("timestamp_unix") or 0)
        bucket_size = max(1, len(all_chains_sorted) // 4)

        sampled_chains = []
        for i in range(4):
            if i < 3:
                bucket = all_chains_sorted[i * bucket_size:(i + 1) * bucket_size]
            else:
                # Last bucket captures remainder to avoid dropping tail chains
                bucket = all_chains_sorted[i * bucket_size:]

            # Score chains by cognitive density of combined text
            scored_bucket = sorted(
                bucket,
                key=lambda c: sum(self._cognitive_density_score(m.get("content", "")) for m in c),
                reverse=True
            )
            sampled_chains.extend(scored_bucket[:chains_per_bucket])

        # Cap at SAMPLE_CHAINS to stay within budget
        sampled_chains = sampled_chains[:SAMPLE_CHAINS]

        # Format chains as grouped units
        formatted_chains = []
        for chain in sampled_chains:
            start_ts = chain[0].get("timestamp_unix") or 0
            end_ts   = chain[-1].get("timestamp_unix") or 0
            formatted_chains.append({
                "chain_length":      len(chain),
                "timespan_seconds":  end_ts - start_ts,
                "messages": [
                    {"id": m["message_id"], "text": m.get("content", "")}
                    for m in chain
                ],
                "combined_text": " / ".join(str(m.get("content", "")) for m in chain),
            })

        print(f"    -> Chains total:      {len(all_chains):,} found | "
              f"{len(formatted_chains)} sampled (stratified across archive)")

        cognitive_pack = {
            "broad_sample":    broad_sample,
            "peak_candidates": peak_candidates,
            "message_chains":  formatted_chains,
            "stats": {
                "total_messages":  len(all_msgs),
                "short_count":     len(short_msgs),
                "medium_count":    len(medium_msgs),
                "long_count":      len(long_msgs),
                "chains_total":    len(all_chains),
                "chains_sampled":  len(formatted_chains),
            },
        }

        # -- PACK 3: FACT CHECK ------------------------------------------------
        # Medium + long only -- short messages almost never contain verifiable claims
        # Time-stratified to cover full archive, not just recent messages
        fact_medium = random.sample(medium_msgs, min(len(medium_msgs), SAMPLE_FACTCHECK // 2))
        fact_long   = random.sample(long_msgs,   min(len(long_msgs),   SAMPLE_FACTCHECK // 2))
        factcheck_pack = fact_medium + fact_long
        random.shuffle(factcheck_pack)
        print(f"    -> Fact check pool:   {len(factcheck_pack):,} msgs "
              f"({len(fact_medium)} medium + {len(fact_long)} long)")

        return {
            "tone":       tone_pack,
            "cognitive":  cognitive_pack,
            "fact_check": factcheck_pack,
            "nickname":   nickname,
        }


# ---------------------------------------------------------------------------
# DIAGNOSTICS -- run standalone to verify DB connection and sample counts
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ATLAS Cortex Engine Diagnostics")
    parser.add_argument("--subject", required=True, help="Nickname to run diagnostics for")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"[-] DB not found at: {DB_PATH}")
        print(f"    Set ORACLE_DB_PATH in your .env file.")
    else:
        engine = CortexEngine()
        packs  = engine.get_evidence_packs(args.subject)
        if packs:
            cog   = packs["cognitive"]
            stats = cog["stats"]
            print(f"\n[Diagnostics Complete]")
            print(f"  Total messages in archive : {stats['total_messages']:,}")
            print(f"  Short  (<=80 chars)       : {stats['short_count']:,}")
            print(f"  Medium (81-300 chars)     : {stats['medium_count']:,}")
            print(f"  Long   (300+ chars)       : {stats['long_count']:,}")
            print(f"  Chains found              : {stats['chains_total']:,}")
            print(f"  Chains sampled            : {stats['chains_sampled']:,}")
            print(f"  Broad vocab sample        : {len(cog['broad_sample']):,}")
            print(f"  Peak candidates           : {len(cog['peak_candidates']):,}")
            print(f"  Fact check pool           : {len(packs['fact_check']):,}")
            print(f"  Tone sample               : {len(packs['tone']):,}")
