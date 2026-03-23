"""
test_oracle_stress.py - Oracle Stress Test Harness
Runs targeted questions through the full intent detection pipeline
without Discord, printing tier, SQL, params, row count, and sample data.
Includes data validation to verify results are factually correct.
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


# ── Data Validators ────────────────────────────────────────────────────────
# Each validator receives (rows, meta, question) and returns (ok, message)

def _val_sort_desc(col):
    """Verify results are sorted descending by column."""
    def validator(rows, meta, question):
        if len(rows) < 2:
            return True, "too few rows to validate sort"
        vals = [dict(r).get(col) for r in rows if dict(r).get(col) is not None]
        if not vals:
            return True, f"column '{col}' not found"
        nums = [float(v) for v in vals]
        for i in range(len(nums) - 1):
            if nums[i] < nums[i + 1]:
                return False, f"NOT sorted DESC by {col}: {nums[i]} < {nums[i+1]} at position {i}"
        return True, f"sorted DESC by {col} ✓"
    return validator


def _val_sort_asc(col):
    """Verify results are sorted ascending by column."""
    def validator(rows, meta, question):
        if len(rows) < 2:
            return True, "too few rows to validate sort"
        vals = [dict(r).get(col) for r in rows if dict(r).get(col) is not None]
        if not vals:
            return True, f"column '{col}' not found"
        nums = [float(v) for v in vals]
        for i in range(len(nums) - 1):
            if nums[i] > nums[i + 1]:
                return False, f"NOT sorted ASC by {col}: {nums[i]} > {nums[i+1]} at position {i}"
        return True, f"sorted ASC by {col} ✓"
    return validator


def _val_col_equals(col, expected):
    """Verify a specific column has the expected value in all rows."""
    def validator(rows, meta, question):
        for i, row in enumerate(rows):
            d = dict(row) if hasattr(row, 'keys') else {}
            val = d.get(col)
            if val is not None and str(val) != str(expected):
                return False, f"row {i}: {col}={val}, expected {expected}"
        return True, f"{col}={expected} ✓"
    return validator


def _val_col_contains(col, substring):
    """Verify a column contains a substring in at least one row."""
    def validator(rows, meta, question):
        for row in rows:
            d = dict(row) if hasattr(row, 'keys') else {}
            val = str(d.get(col, ""))
            if substring.lower() in val.lower():
                return True, f"found '{substring}' in {col} ✓"
        return False, f"'{substring}' not found in any row's {col}"
    return validator


def _val_meta_key(key, expected):
    """Verify a meta key has the expected value."""
    def validator(rows, meta, question):
        val = meta.get(key)
        if val == expected:
            return True, f"meta[{key}]={expected} ✓"
        return False, f"meta[{key}]={val}, expected {expected}"
    return validator


def _val_opponent_filter(opponent_name):
    """Verify all returned games involve the specified opponent."""
    def validator(rows, meta, question):
        resolved = fuzzy_resolve_user(opponent_name)
        if not resolved:
            return True, f"could not resolve '{opponent_name}'"
        total = len(rows)
        matching = 0
        for row in rows:
            d = dict(row) if hasattr(row, 'keys') else {}
            if d.get("homeUser") == resolved or d.get("awayUser") == resolved:
                matching += 1
        if matching < total:
            return False, f"opponent filter failed: {matching}/{total} games vs {resolved}"
        return True, f"all {total} games vs {resolved} ✓"
    return validator


def _val_losses_not_wins():
    """Verify losses leaderboard returns loser_user, not winner_user."""
    def validator(rows, meta, question):
        if not rows:
            return True, "no rows"
        d = dict(rows[0]) if hasattr(rows[0], 'keys') else {}
        if "total_losses" in d:
            return True, "uses total_losses column ✓"
        if "total_wins" in d:
            return False, "WRONG: returns total_wins instead of total_losses"
        return True, "column check inconclusive"
    return validator


def _val_has_rows():
    """Verify at least 1 row returned."""
    def validator(rows, meta, question):
        if len(rows) >= 1:
            return True, f"{len(rows)} rows returned ✓"
        return False, "no rows returned"
    return validator


def _val_min_rows(n):
    """Verify at least n rows returned."""
    def validator(rows, meta, question):
        if len(rows) >= n:
            return True, f"{len(rows)} rows ≥ {n} ✓"
        return False, f"only {len(rows)} rows, expected ≥ {n}"
    return validator


def _val_season_filter(season):
    """Verify seasonIndex matches expected season in all rows."""
    def validator(rows, meta, question):
        expected = str(season)
        for i, row in enumerate(rows):
            d = dict(row) if hasattr(row, 'keys') else {}
            val = d.get("seasonIndex")
            if val is not None and str(val) != expected:
                return False, f"row {i}: seasonIndex={val}, expected {expected}"
        return True, f"seasonIndex={expected} ✓"
    return validator


def _val_team_filter(team):
    """Verify team name appears in results (any team-related column)."""
    def validator(rows, meta, question):
        team_lower = team.lower()
        team_cols = ["homeTeamName", "awayTeamName", "teamName", "team1Name",
                     "team2Name", "drafting_team", "winner_team", "loser_team"]
        for row in rows:
            d = dict(row) if hasattr(row, 'keys') else {}
            for col in team_cols:
                val = d.get(col, "")
                if val and team_lower in str(val).lower():
                    return True, f"found '{team}' in {col} ✓"
        return False, f"'{team}' not found in any team column"
    return validator


def _val_col_not_null(col):
    """Verify column is not None/empty in all rows."""
    def validator(rows, meta, question):
        for i, row in enumerate(rows):
            d = dict(row) if hasattr(row, 'keys') else {}
            val = d.get(col)
            if val is None or str(val).strip() == "":
                return False, f"row {i}: {col} is null/empty"
        return True, f"{col} not null ✓"
    return validator


def _val_meta_key_exists(key):
    """Verify meta dict has key (regardless of value)."""
    def validator(rows, meta, question):
        if key in meta:
            return True, f"meta[{key}] exists ✓"
        return False, f"meta[{key}] missing"
    return validator


# ── Test cases ──────────────────────────────────────────────────────────────
# (question, simulated_caller_db, description, expected_intent, [validators])
TEST_CASES = [
    # === Original 10 (unchanged) ===
    ("what is my record vs diddy", "TheWitt",
     "Q1: H2H regex + nickname 'diddy' + caller identity", "h2h_record", []),

    ("JT vs Tuna", "TestCaller",
     "Q2: Two short nicknames, no keywords", "h2h_record", []),

    ("how are the Saints doing", "TestCaller",
     "Q3: Team record regex", "team_record", []),

    ("my season record", "TheWitt",
     "Q4: REVERSED word order (season before record)", "season_record", []),

    ("Witt's all-time record", "TestCaller",
     "Q5: Possessive 's + alltime_record intent", "alltime_record", []),

    ("top 5 passers this season", "TestCaller",
     "Q6: Leaderboard + season filter", "leaderboard",
     [_val_sort_desc("total_stat")]),

    ("who did New Orleans draft", "TestCaller",
     "Q7: Multi-word team name in draft", "draft_history", []),

    ("what is Chokolate_Thunda's record vs MeLLoW_FiRe", "TestCaller",
     "Q8: Exact DB usernames with underscores + mixed case", "h2h_record", []),

    ("my last 5 games vs Killa", "TheWitt",
     "Q9: Recent games WITH opponent filter", "recent_games",
     [_val_opponent_filter("Killa")]),

    ("what is Shottaz record this season", "TestCaller",
     "Q10: NULL db_username member resolution", "season_record", []),

    # === New Intent Coverage (20 tests) ===
    ("who won the Super Bowl in season 3", "TestCaller",
     "Q11: Super Bowl winner query", "playoff_results", []),

    ("playoff results this season", "TestCaller",
     "Q12: Playoff results current season", "playoff_results", []),

    ("who has the most passing TDs all-time", "TestCaller",
     "Q13: Player stat leaderboard - pass TDs", "player_stats",
     [_val_sort_desc("stat_value")]),

    ("top rushing yards this season", "TestCaller",
     "Q14: Player stat leaderboard - rush yds", "player_stats",
     [_val_sort_desc("stat_value")]),

    ("who leads the league in sacks", "TestCaller",
     "Q15: Player stat - sacks", "player_stats",
     [_val_sort_desc("stat_value")]),

    ("what trades did the Lions make", "TestCaller",
     "Q16: Team trade history", "trade_history", []),

    ("trades this season", "TestCaller",
     "Q17: All trades current season", "trade_history", []),

    ("which team has the best offense", "TestCaller",
     "Q18: Team stat ranking - offense", "team_stats",
     [_val_sort_desc("off_yds")]),

    ("which team scores the most points", "TestCaller",
     "Q19: Team stat - points", "team_stats",
     [_val_sort_desc("pts_for")]),

    ("what was the score of Lions vs Packers", "TestCaller",
     "Q20: Game score lookup", "game_score", []),

    ("score of the Chiefs game", "TestCaller",
     "Q21: Single team game score", "game_score", []),

    ("what teams has Witt owned", "TestCaller",
     "Q22: Owner team history", "owner_history", []),

    ("who owned the Bears in season 2", "TestCaller",
     "Q23: Team owner lookup", "owner_history", []),

    ("biggest blowout ever", "TestCaller",
     "Q24: Records - biggest blowout", "records_extremes",
     [_val_sort_desc("margin")]),

    ("closest game this season", "TestCaller",
     "Q25: Records - closest game", "records_extremes",
     [_val_sort_asc("margin")]),

    ("highest scoring game", "TestCaller",
     "Q26: Records - highest scoring", "records_extremes",
     [_val_sort_desc("total_pts")]),

    ("NFC East standings", "TestCaller",
     "Q27: Division standings", "standings_query",
     [_val_col_equals("divisionName", "NFC East")]),

    ("who is the best QB in the league", "TestCaller",
     "Q28: Best player at position", "roster_query", []),

    ("Lions roster", "TestCaller",
     "Q29: Team roster", "roster_query",
     [_val_sort_desc("ovr")]),

    ("who has x-factor on the Packers", "TestCaller",
     "Q30: Team abilities", "player_abilities_query", []),

    # === Edge Case / Regex Fix Tests (10 tests) ===
    ("my record last season", "TheWitt",
     "Q31: 'last season' parsing", "season_record", []),

    ("top five passers", "TestCaller",
     "Q32: Word number 'five'", "leaderboard",
     [_val_sort_desc("total_stat")]),

    ("what's my record this season", "TheWitt",
     "Q33: Contraction 'what's'", "season_record", []),

    ("the Lions record this season", "TestCaller",
     "Q34: Team possessive / team record", "team_record", []),

    ("how many games have I won", "TheWitt",
     "Q35: 'how many' routing to alltime", "alltime_record", []),

    ("how many wins do I have this season", "TheWitt",
     "Q36: 'how many' + season", "season_record", []),

    ("who has most wins", "TestCaller",
     "Q37: Winningest owner leaderboard", "leaderboard",
     [_val_sort_desc("total_wins")]),

    ("Cowboys record season 4", "TestCaller",
     "Q38: Team + specific season", "team_record", []),

    ("who leads the league in interceptions", "TestCaller",
     "Q39: Defensive stat - interceptions", "player_stats",
     [_val_sort_desc("stat_value")]),

    ("free agents at QB", "TestCaller",
     "Q40: Free agent filter", "roster_query",
     [_val_col_equals("teamName", "Free Agent")]),

    # === Audit Fix Tests (12 tests) ===
    ("who has the most losses", "TestCaller",
     "Q41: Losses leaderboard (not wins)", "leaderboard",
     [_val_losses_not_wins(), _val_sort_desc("total_losses")]),

    ("which team has the worst offense", "TestCaller",
     "Q42: Worst offense sort ASC", "team_stats",
     [_val_sort_asc("off_yds")]),

    ("which team has the worst defense", "TestCaller",
     "Q43: Worst defense sort DESC (most yards)", "team_stats",
     [_val_sort_desc("def_yds")]),

    ("worst passer this season", "TestCaller",
     "Q44: Worst player stat leaderboard", "leaderboard",
     [_val_sort_asc("total_stat"), _val_meta_key("sort", "asc")]),

    ("who has the fewest points", "TestCaller",
     "Q45: Team stats fewest points", "team_stats",
     [_val_sort_asc("pts_for")]),

    ("Bears record this season", "TestCaller",
     "Q46: Team name that was substring-matching 'chi'", "team_record", []),

    ("what was the score of the Commanders game", "TestCaller",
     "Q47: 'was' in question should not match Commanders alias", "game_score", []),

    ("Cowboys record season three", "TestCaller",
     "Q48: Word-number season", "team_record",
     [_val_meta_key("season", 3)]),

    ("Witt's trades this season", "TestCaller",
     "Q49: Possessive + trades keyword", "trade_history", []),

    ("who has the least sacks", "TestCaller",
     "Q50: Least/worst player stat", "player_stats",
     [_val_sort_asc("stat_value"), _val_meta_key("sort", "asc")]),

    ("worst owner this season", "TestCaller",
     "Q51: Worst owner = fewest wins (sort ASC)", "leaderboard",
     [_val_sort_asc("total_wins"), _val_meta_key("sort", "asc")]),

    ("best owner this season", "TestCaller",
     "Q52: Best owner = most wins (sort DESC)", "leaderboard",
     [_val_sort_desc("total_wins"), _val_meta_key("sort", "desc")]),

    # === Category A: Bug-Catching Tests (4 tests) ===
    ("who has the fewest losses", "TestCaller",
     "Q53: Fewest losses = ASC sort (bug fix validation)", "leaderboard",
     [_val_sort_asc("total_losses"), _val_losses_not_wins(), _val_meta_key("sort", "asc")]),

    ("owner with the least losses this season", "TestCaller",
     "Q54: Least losses + season filter", "leaderboard",
     [_val_sort_asc("total_losses"), _val_losses_not_wins()]),

    ("which team has the best offense this season", "TestCaller",
     "Q55: Best offense = most yards (DESC)", "team_stats",
     [_val_sort_desc("off_yds"), _val_has_rows()]),

    ("which team has the worst defense this season", "TestCaller",
     "Q56: Worst defense = most yards allowed (DESC)", "team_stats",
     [_val_sort_desc("def_yds"), _val_has_rows()]),

    # === Category B: Streak Intent Tests (4 tests) ===
    ("my current win streak", "TheWitt",
     "Q57: Streak intent with 'my' caller", "streak",
     [_val_has_rows(), _val_meta_key("owner", "TheWitt")]),

    ("my streak", "TheWitt",
     "Q58: Minimal streak query", "streak",
     [_val_has_rows(), _val_meta_key("owner", "TheWitt")]),

    ("is Killa on a winning streak", "TestCaller",
     "Q59: Streak for named opponent", "streak",
     [_val_has_rows()]),

    ("Witt's losing streak", "TestCaller",
     "Q60: Possessive + streak", "streak",
     [_val_has_rows()]),

    # === Category C: Intent Collision Tests (8 tests) ===
    ("Witt vs Killa", "TestCaller",
     "Q61: Owner names → h2h_record (not game_score)", "h2h_record",
     [_val_has_rows()]),

    ("Lions vs Packers", "TestCaller",
     "Q62: Team names → game_score (not h2h)", "game_score",
     [_val_has_rows(), _val_team_filter("Lions")]),

    ("who has the most wins this season", "TestCaller",
     "Q63: Most wins + season → leaderboard", "leaderboard",
     [_val_sort_desc("total_wins"), _val_has_rows()]),

    ("top QB", "TestCaller",
     "Q64: Short position query → roster_query", "roster_query",
     [_val_sort_desc("ovr"), _val_has_rows()]),

    ("Cowboys draft picks", "TestCaller",
     "Q65: Team + draft → draft_history", "draft_history",
     [_val_has_rows()]),

    ("Packers record", "TestCaller",
     "Q66: Team + record → team_record (not season_record)", "team_record",
     [_val_has_rows()]),

    ("my record", "TheWitt",
     "Q67: Bare 'my record' → alltime_record (no season)", "alltime_record",
     [_val_has_rows()]),

    ("top 5 sacks", "TestCaller",
     "Q68: Top N stat → player_stats", "player_stats",
     [_val_sort_desc("stat_value"), _val_min_rows(5)]),

    # === Category D: Sort Direction Exhaustive Tests (10 tests) ===
    ("who has the most rushing TDs all-time", "TestCaller",
     "Q69: Most rushing TDs = DESC", "player_stats",
     [_val_sort_desc("stat_value"), _val_has_rows()]),

    ("who has the fewest rushing yards", "TestCaller",
     "Q70: Fewest rushing yards = ASC", "player_stats",
     [_val_sort_asc("stat_value"), _val_meta_key("sort", "asc")]),

    ("worst receiver this season", "TestCaller",
     "Q71: Worst receiver leaderboard = ASC", "leaderboard",
     [_val_sort_asc("total_stat"), _val_meta_key("sort", "asc")]),

    ("best passer this season", "TestCaller",
     "Q72: Best passer leaderboard = DESC", "leaderboard",
     [_val_sort_desc("total_stat"), _val_meta_key("sort", "desc")]),

    ("which team allows the most points", "TestCaller",
     "Q73: Most points against = DESC", "team_stats",
     [_val_sort_desc("pts_against"), _val_has_rows()]),

    ("which team has the best defense", "TestCaller",
     "Q74: Best defense = fewest yards (ASC)", "team_stats",
     [_val_sort_asc("def_yds"), _val_has_rows()]),

    ("lowest scoring game ever", "TestCaller",
     "Q75: Lowest scoring = ASC total_pts", "records_extremes",
     [_val_sort_asc("total_pts"), _val_has_rows()]),

    ("most lopsided game this season", "TestCaller",
     "Q76: Most lopsided = DESC margin", "records_extremes",
     [_val_sort_desc("margin"), _val_has_rows()]),

    ("bottom 5 passers", "TestCaller",
     "Q77: Bottom N → ASC leaderboard", "leaderboard",
     [_val_sort_asc("total_stat"), _val_meta_key("sort", "asc")]),

    ("who has the highest passer rating", "TestCaller",
     "Q78: Highest passer rating = DESC", "player_stats",
     [_val_sort_desc("stat_value"), _val_has_rows()]),

    # === Category E: Edge Case Phrasing Tests (8 tests) ===
    ("witt record this season", "TestCaller",
     "Q79: Lowercase name + no possessive", "season_record",
     [_val_has_rows()]),

    ("my games against Tuna", "TheWitt",
     "Q80: 'games against' = recent_games", "recent_games",
     [_val_opponent_filter("Tuna"), _val_has_rows()]),

    ("NFC West standings", "TestCaller",
     "Q81: NFC West division standings", "standings_query",
     [_val_col_equals("divisionName", "NFC West"), _val_has_rows()]),

    ("AFC standings", "TestCaller",
     "Q82: Conference standings", "standings_query",
     [_val_has_rows()]),

    ("who drafted for New England", "TestCaller",
     "Q83: Multi-word team + draft phrasing", "draft_history",
     [_val_has_rows()]),

    ("what abilities does Jalen Hurts have", "TestCaller",
     "Q84: Player abilities by name", "player_abilities_query",
     [_val_has_rows()]),

    ("who's the best owner all time", "TestCaller",
     "Q85: Contraction who's + best owner", "leaderboard",
     [_val_sort_desc("total_wins"), _val_has_rows()]),

    ("how are the Packers doing this season", "TestCaller",
     "Q86: Full sentence team record", "team_record",
     [_val_has_rows()]),

    # === Category F: Data Accuracy for Under-Validated Intents (8 tests) ===
    ("my record vs Killa", "TheWitt",
     "Q87: H2H with data validation", "h2h_record",
     [_val_has_rows(), _val_meta_key("owner2", "KillaE94")]),

    ("Witt's record this season", "TestCaller",
     "Q88: Season record with validation", "season_record",
     [_val_has_rows()]),

    ("my all-time record", "TheWitt",
     "Q89: All-time record with validation", "alltime_record",
     [_val_has_rows()]),

    ("who won the Super Bowl in season 1", "TestCaller",
     "Q90: Super Bowl season 1 (no playoff data in DB)", "playoff_results",
     [_val_meta_key("type", "playoffs")]),

    ("what was the score of the Cowboys game this season", "TestCaller",
     "Q91: Game score + team filter", "game_score",
     [_val_has_rows(), _val_team_filter("Cowboys")]),

    ("who did the Eagles draft in season 3", "TestCaller",
     "Q92: Draft history + season filter", "draft_history",
     [_val_has_rows()]),

    ("Lions trades this season", "TestCaller",
     "Q93: Trade history + team filter", "trade_history",
     [_val_meta_key("team", "Lions")]),

    ("what teams has Killa owned", "TestCaller",
     "Q94: Owner history with validation", "owner_history",
     [_val_has_rows()]),

    # === Category G: SQL Injection Safety Tests (4 tests) ===
    ("my record; DROP TABLE games", "TheWitt",
     "Q95: SQL injection semicolon", "alltime_record",
     [_val_has_rows()]),

    ("top passers' OR 1=1 --", "TestCaller",
     "Q96: SQL injection quote + OR", "leaderboard",
     [_val_has_rows()]),

    ('who has the most wins" UNION SELECT * FROM games --', "TestCaller",
     "Q97: SQL injection UNION", "leaderboard",
     [_val_sort_desc("total_wins")]),

    ("my all-time record; DELETE FROM games", "TheWitt",
     "Q98: SQL injection in alltime query — semicolon safely ignored", "alltime_record",
     [_val_has_rows()]),
]


async def run_test(question: str, caller_db: str, desc: str, force_agent: bool = False) -> dict:
    """Run a single test through the full pipeline."""
    # Step 1: Resolve names in question
    annotated_q, alias_map = resolve_names_in_question(question)

    # Step 2: Self-reference collision check
    collision = check_self_reference_collision(caller_db, alias_map)

    # Step 3a: Force agent path (bypass Tier 1 regex)
    if force_agent:
        from oracle_agent import run_agent
        from codex_utils import _build_schema
        schema = _build_schema()
        agent_result = await run_agent(
            question=annotated_q,
            caller_db=caller_db,
            alias_map=alias_map,
            schema=schema,
        )
        if isinstance(agent_result.data, list):
            rows = agent_result.data
        elif isinstance(agent_result.data, str):
            rows = [{"answer": agent_result.data}]
        elif isinstance(agent_result.data, dict):
            rows = [agent_result.data]
        else:
            rows = []
        return {
            "desc": desc,
            "question": question,
            "caller": caller_db,
            "annotated": annotated_q,
            "alias_map": alias_map,
            "collision": collision,
            "intent": "agent",
            "tier": 2,
            "sql": agent_result.sql,
            "params": (),
            "meta": {},
            "rows": rows,
            "error": agent_result.error,
            "attempts": agent_result.attempts,
        }

    # Step 3b: Intent detection (Tier 1 regex)
    result = await detect_intent(question, caller_db, alias_map)

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
    import argparse
    parser = argparse.ArgumentParser(description="Oracle Stress Test Harness")
    parser.add_argument("--agent", action="store_true",
                        help="Force ALL questions through Code-Gen Agent (bypass Tier 1 regex)")
    args = parser.parse_args()

    mode = "AGENT FORCED" if args.agent else "STANDARD (Tier 1 regex)"
    print("=" * 80)
    print(f"ORACLE STRESS TEST - {len(TEST_CASES)} Questions [{mode}]")
    print("=" * 80)

    results = []
    pass_count = 0
    fail_count = 0
    intent_match_count = 0
    tier_pass_count = 0
    data_pass_count = 0
    data_tested_count = 0

    for test_entry in TEST_CASES:
        question, caller, desc, expected_intent = test_entry[:4]
        validators = test_entry[4] if len(test_entry) > 4 else []

        r = await run_test(question, caller, desc, force_agent=args.agent)
        results.append(r)

        # Validate intent/tier
        has_rows = len(r["rows"]) > 0
        no_error = r["error"] is None
        intent_match = r["intent"] == expected_intent if not args.agent else True
        tier_ok = r["tier"] <= 2

        # Run data validators (run even with 0 rows for validators like _val_has_rows)
        data_issues = []
        if validators:
            data_tested_count += 1
            for val_fn in validators:
                ok, msg = val_fn(r["rows"], r["meta"], r["question"])
                if not ok:
                    data_issues.append(msg)

        data_ok = len(data_issues) == 0

        # Core pass criteria: correct intent, tier ≤ 2, no SQL error, data valid
        passed = no_error and intent_match and tier_ok and data_ok

        if intent_match:
            intent_match_count += 1
        if tier_ok:
            tier_pass_count += 1
        if data_ok and validators:
            data_pass_count += 1

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
        if "attempts" in r:
            print(f"  Attempts:  {r['attempts']}")
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

        # Data validation results
        if validators:
            for val_fn in validators:
                ok, msg = val_fn(r["rows"], r["meta"], r["question"])
                v_status = "✓" if ok else "✗"
                print(f"  DATA [{v_status}]: {msg}")
        if data_issues:
            for issue in data_issues:
                print(f"  DATA FAIL: {issue}")

    print(f"\n{'=' * 80}")
    print(f"RESULTS: {pass_count}/{len(TEST_CASES)} passed")
    print(f"  Intent match:    {intent_match_count}/{len(TEST_CASES)}")
    print(f"  Tier ≤ 2:        {tier_pass_count}/{len(TEST_CASES)}")
    print(f"  Rows > 0:        {sum(1 for r in results if len(r['rows']) > 0)}/{len(TEST_CASES)}")
    print(f"  No SQL error:    {sum(1 for r in results if r['error'] is None)}/{len(TEST_CASES)}")
    print(f"  Data validated:  {data_pass_count}/{data_tested_count} (of {data_tested_count} tested)")
    print(f"{'=' * 80}")

    return results


if __name__ == "__main__":
    asyncio.run(main())
