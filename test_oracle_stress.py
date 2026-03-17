"""
test_oracle_stress.py - Oracle Stress Test Harness
Runs 40 targeted questions through the full intent detection pipeline
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
# (question, simulated_caller_db, description, expected_intent)
TEST_CASES = [
    # === Original 10 (unchanged) ===
    ("what is my record vs diddy", "TheWitt",
     "Q1: H2H regex + nickname 'diddy' + caller identity", "h2h_record"),

    ("JT vs Tuna", "TestCaller",
     "Q2: Two short nicknames, no keywords", "h2h_record"),

    ("how are the Saints doing", "TestCaller",
     "Q3: Team record regex", "team_record"),

    ("my season record", "TheWitt",
     "Q4: REVERSED word order (season before record)", "season_record"),

    ("Witt's all-time record", "TestCaller",
     "Q5: Possessive 's + alltime_record intent", "alltime_record"),

    ("top 5 passers this season", "TestCaller",
     "Q6: Leaderboard + season filter", "leaderboard"),

    ("who did New Orleans draft", "TestCaller",
     "Q7: Multi-word team name in draft", "draft_history"),

    ("what is Chokolate_Thunda's record vs MeLLoW_FiRe", "TestCaller",
     "Q8: Exact DB usernames with underscores + mixed case", "h2h_record"),

    ("my last 5 games vs Killa", "TheWitt",
     "Q9: Recent games WITH opponent filter", "recent_games"),

    ("what is Shottaz record this season", "TestCaller",
     "Q10: NULL db_username member resolution", "season_record"),

    # === New Intent Coverage (20 tests) ===
    ("who won the Super Bowl in season 3", "TestCaller",
     "Q11: Super Bowl winner query", "playoff_results"),

    ("playoff results this season", "TestCaller",
     "Q12: Playoff results current season", "playoff_results"),

    ("who has the most passing TDs all-time", "TestCaller",
     "Q13: Player stat leaderboard - pass TDs", "player_stats"),

    ("top rushing yards this season", "TestCaller",
     "Q14: Player stat leaderboard - rush yds", "player_stats"),

    ("who leads the league in sacks", "TestCaller",
     "Q15: Player stat - sacks", "player_stats"),

    ("what trades did the Lions make", "TestCaller",
     "Q16: Team trade history", "trade_history"),

    ("trades this season", "TestCaller",
     "Q17: All trades current season", "trade_history"),

    ("which team has the best offense", "TestCaller",
     "Q18: Team stat ranking - offense", "team_stats"),

    ("which team scores the most points", "TestCaller",
     "Q19: Team stat - points", "team_stats"),

    ("what was the score of Lions vs Packers", "TestCaller",
     "Q20: Game score lookup", "game_score"),

    ("score of the Chiefs game", "TestCaller",
     "Q21: Single team game score", "game_score"),

    ("what teams has Witt owned", "TestCaller",
     "Q22: Owner team history", "owner_history"),

    ("who owned the Bears in season 2", "TestCaller",
     "Q23: Team owner lookup", "owner_history"),

    ("biggest blowout ever", "TestCaller",
     "Q24: Records - biggest blowout", "records_extremes"),

    ("closest game this season", "TestCaller",
     "Q25: Records - closest game", "records_extremes"),

    ("highest scoring game", "TestCaller",
     "Q26: Records - highest scoring", "records_extremes"),

    ("NFC East standings", "TestCaller",
     "Q27: Division standings", "standings_query"),

    ("who is the best QB in the league", "TestCaller",
     "Q28: Best player at position", "roster_query"),

    ("Lions roster", "TestCaller",
     "Q29: Team roster", "roster_query"),

    ("who has x-factor on the Packers", "TestCaller",
     "Q30: Team abilities", "player_abilities_query"),

    # === Edge Case / Regex Fix Tests (10 tests) ===
    ("my record last season", "TheWitt",
     "Q31: 'last season' parsing", "season_record"),

    ("top five passers", "TestCaller",
     "Q32: Word number 'five'", "leaderboard"),

    ("what's my record this season", "TheWitt",
     "Q33: Contraction 'what's'", "season_record"),

    ("the Lions record this season", "TestCaller",
     "Q34: Team possessive / team record", "team_record"),

    ("how many games have I won", "TheWitt",
     "Q35: 'how many' routing to alltime", "alltime_record"),

    ("how many wins do I have this season", "TheWitt",
     "Q36: 'how many' + season", "season_record"),

    ("who has most wins", "TestCaller",
     "Q37: Winningest owner leaderboard", "leaderboard"),

    ("Cowboys record season 4", "TestCaller",
     "Q38: Team + specific season", "team_record"),

    ("who leads the league in interceptions", "TestCaller",
     "Q39: Defensive stat - interceptions", "player_stats"),

    ("free agents at QB", "TestCaller",
     "Q40: Free agent filter", "roster_query"),

    # === Audit Fix Tests (10 tests) ===
    ("who has the most losses", "TestCaller",
     "Q41: Losses leaderboard (not wins)", "leaderboard"),

    ("which team has the worst offense", "TestCaller",
     "Q42: Worst offense sort ASC", "team_stats"),

    ("which team has the worst defense", "TestCaller",
     "Q43: Worst defense sort DESC (most yards)", "team_stats"),

    ("worst passer this season", "TestCaller",
     "Q44: Worst player stat leaderboard", "leaderboard"),

    ("who has the fewest points", "TestCaller",
     "Q45: Team stats fewest points", "team_stats"),

    ("Bears record this season", "TestCaller",
     "Q46: Team name that was substring-matching 'chi'", "team_record"),

    ("what was the score of the Commanders game", "TestCaller",
     "Q47: 'was' in question should not match Commanders alias", "game_score"),

    ("Cowboys record season three", "TestCaller",
     "Q48: Word-number season", "team_record"),

    ("Witt's trades this season", "TestCaller",
     "Q49: Possessive + trades keyword", "trade_history"),

    ("who has the least sacks", "TestCaller",
     "Q50: Least/worst player stat", "player_stats"),
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
    if result.sql and result.params is not None:
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
    print(f"ORACLE STRESS TEST - {len(TEST_CASES)} Questions")
    print("=" * 80)

    results = []
    pass_count = 0
    fail_count = 0
    intent_match_count = 0
    tier_pass_count = 0

    for question, caller, desc, expected_intent in TEST_CASES:
        r = await run_test(question, caller, desc)
        results.append(r)

        # Validate
        has_rows = len(r["rows"]) > 0
        no_error = r["error"] is None
        intent_match = r["intent"] == expected_intent
        tier_ok = r["tier"] <= 2

        # Core pass criteria: correct intent, tier ≤ 2, no SQL error
        # has_rows is informational — some intents (e.g. playoffs) may have no data in DB
        passed = no_error and intent_match and tier_ok

        if intent_match:
            intent_match_count += 1
        if tier_ok:
            tier_pass_count += 1

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
        intent_status = "✓" if intent_match else "✗"
        tier_status = "✓" if tier_ok else "✗"
        print(f"  Intent:    {r['intent']} (expected: {expected_intent}) [{intent_status}]")
        print(f"  Tier:      {r['tier']} [{tier_status}]")
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
                print(f"  [Q9 CHECK] *** OPPONENT FILTER NOT APPLIED ***")

    print(f"\n{'=' * 80}")
    print(f"RESULTS: {pass_count}/{len(TEST_CASES)} passed")
    print(f"  Intent match: {intent_match_count}/{len(TEST_CASES)}")
    print(f"  Tier ≤ 2:     {tier_pass_count}/{len(TEST_CASES)}")
    print(f"  Rows > 0:     {sum(1 for r in results if len(r['rows']) > 0)}/{len(TEST_CASES)}")
    print(f"  No SQL error: {sum(1 for r in results if r['error'] is None)}/{len(TEST_CASES)}")
    print(f"{'=' * 80}")

    return results


if __name__ == "__main__":
    asyncio.run(main())
