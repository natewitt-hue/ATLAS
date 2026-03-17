"""
test_oracle_stress.py - Oracle Stress Test Harness
Runs 10 targeted questions through the full intent detection pipeline
without Discord, printing tier, SQL, params, row count, and sample data.
"""
import asyncio
import sys
import os

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from codex_cog import (
    fuzzy_resolve_user,
    resolve_names_in_question,
    run_sql,
    _ensure_codex_identity,
)
from codex_intents import detect_intent, check_self_reference_collision

# Bootstrap identity system
_ensure_codex_identity()

# ── Test cases ──────────────────────────────────────────────────────────────
# (question, simulated_caller_db, description)
TEST_CASES = [
    ("what is my record vs diddy", "TheWitt",
     "Q1: H2H regex + nickname 'diddy' + caller identity"),

    ("JT vs Tuna", "TestCaller",
     "Q2: Two short nicknames, no keywords"),

    ("how are the Saints doing", "TestCaller",
     "Q3: Team record regex"),

    ("my season record", "TheWitt",
     "Q4: REVERSED word order (season before record)"),

    ("Witt's all-time record", "TestCaller",
     "Q5: Possessive 's + alltime_record intent"),

    ("top 5 passers this season", "TestCaller",
     "Q6: Leaderboard + season filter"),

    ("who did New Orleans draft", "TestCaller",
     "Q7: Multi-word team name in draft"),

    ("what is Chokolate_Thunda's record vs MeLLoW_FiRe", "TestCaller",
     "Q8: Exact DB usernames with underscores + mixed case"),

    ("my last 5 games vs Killa", "TheWitt",
     "Q9: Recent games WITH opponent filter (not supported)"),

    ("what is Shottaz record this season", "TestCaller",
     "Q10: NULL db_username member resolution"),
]


async def run_test(question: str, caller_db: str, desc: str) -> dict:
    """Run a single test through the full pipeline."""
    # Step 1: Resolve names in question
    annotated_q, alias_map = resolve_names_in_question(question)

    # Step 2: Self-reference collision check
    collision = check_self_reference_collision(caller_db, alias_map)

    # Step 3: Intent detection (Tier 1 regex only, no Gemini client)
    result = await detect_intent(question, caller_db, alias_map, gemini_client=None)

    # Step 4: Execute SQL if we got a match
    rows = []
    error = None
    if result.sql and result.params:
        rows, error = run_sql(result.sql, result.params)

    return {
        "desc": desc,
        "question": question,
        "caller": caller_db,
        "annotated": annotated_q,
        "alias_map": alias_map,
        "collision": collision,
        "intent": result.intent,
        "tier": result.tier,
        "sql": result.sql,
        "params": result.params,
        "meta": result.meta,
        "rows": rows or [],
        "error": error,
    }


async def main():
    print("=" * 80)
    print("ORACLE STRESS TEST - 10 Questions")
    print("=" * 80)

    results = []
    pass_count = 0
    fail_count = 0

    for question, caller, desc in TEST_CASES:
        r = await run_test(question, caller, desc)
        results.append(r)

        # Determine pass/fail
        has_rows = len(r["rows"]) > 0
        is_tier1 = r["tier"] == 1
        no_error = r["error"] is None

        # Special cases
        if "Q4" in desc:
            # Word order - we EXPECT this might fail at Tier 1
            passed = has_rows and no_error
        elif "Q9" in desc:
            # Recent games vs opponent - might return wrong data
            # Check if "Killa" appears in results (opponent filter)
            passed = has_rows and no_error
        else:
            passed = has_rows and no_error

        status = "PASS" if passed else "FAIL"
        if passed:
            pass_count += 1
        else:
            fail_count += 1

        print(f"\n{'─' * 80}")
        print(f"[{status}] {desc}")
        print(f"  Question:  {r['question']}")
        print(f"  Caller:    {r['caller']}")
        print(f"  Alias Map: {r['alias_map']}")
        print(f"  Intent:    {r['intent']}, Tier: {r['tier']}")
        print(f"  Meta:      {r['meta']}")
        if r["collision"]:
            print(f"  COLLISION: {r['collision']}")
        if r["error"]:
            print(f"  SQL ERROR: {r['error']}")
        print(f"  Rows:      {len(r['rows'])}")
        if r["rows"]:
            for row in r["rows"][:3]:
                print(f"    {dict(row) if hasattr(row, 'keys') else row}")
            if len(r["rows"]) > 3:
                print(f"    ... ({len(r['rows']) - 3} more)")

        # Extra analysis for Q9 - check if opponent filter worked
        if "Q9" in desc and r["rows"]:
            killa_resolved = fuzzy_resolve_user("Killa")
            killa_games = 0
            for row in r["rows"]:
                d = dict(row) if hasattr(row, "keys") else {}
                if d.get("homeUser") == killa_resolved or d.get("awayUser") == killa_resolved:
                    killa_games += 1
            print(f"  [Q9 CHECK] Killa resolved to: {killa_resolved}")
            print(f"  [Q9 CHECK] Games vs Killa in results: {killa_games}/{len(r['rows'])}")
            if killa_games < len(r["rows"]):
                print(f"  [Q9 CHECK] *** OPPONENT FILTER NOT APPLIED - returns all recent games ***")

    print(f"\n{'=' * 80}")
    print(f"RESULTS: {pass_count} passed, {fail_count} failed out of {len(TEST_CASES)}")
    print(f"{'=' * 80}")

    return results


if __name__ == "__main__":
    asyncio.run(main())
