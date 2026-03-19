"""
Stress test for TSL History — 30 real database questions.
Exercises atlas_ai + codex_cog.gemini_sql() + codex_cog.gemini_answer() end-to-end.
Run: python stress_test_history.py
"""
import asyncio
import sqlite3
import json
import time
import sys
import os
import textwrap

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(override=True)

# Patch dm.CURRENT_SEASON before codex_cog imports it
import data_manager as dm
dm.CURRENT_SEASON = "6"

from codex_cog import gemini_sql, gemini_answer, extract_sql

# tsl_members may not exist if bot hasn't started
try:
    from build_member_db import get_alias_map
    ALIAS_MAP = get_alias_map()
except Exception:
    ALIAS_MAP = {}

DB_PATH = "tsl_history.db"

# ── 30 Questions ─────────────────────────────────────────────────────────────

QUESTIONS = [
    # Head-to-Head Records (5)
    "What is BDiddy86's record vs TheWitt all time?",
    "What is TrombettaThanYou's record vs Ronfk all time?",
    "What is Find_the_Door's record vs Mr_Clutch723 all time?",
    "What is D-TownDon's record vs Chokolate_Thunda all time?",
    "Who has TheWitt beaten the most?",

    # Owner Records & Rankings (5)
    "What is TrombettaThanYou's all-time win/loss record?",
    "Who has the most losses all time?",
    "What is Saucy0134's record in season 5?",
    "Who has the best win percentage all time? Minimum 20 games.",
    "Rank the top 10 owners by total wins.",

    # Player Stats — Offense (5)
    "Who leads all time in passing TDs?",
    "Who has the most rushing yards this season (season 6)?",
    "Top 5 receivers by receiving yards all time.",
    "Which QB has the highest passer rating this season? Minimum 100 attempts.",
    "Who has the most fumbles all time?",

    # Player Stats — Defense (5)
    "Who has the most sacks all time?",
    "Who leads in interceptions this season?",
    "Top 5 players in total tackles all time.",
    "Who has the most forced fumbles all time?",
    "Which player has the most defensive TDs all time?",

    # Game Records (5)
    "What is the highest scoring game in TSL history?",
    "What is the biggest blowout (margin of victory) in TSL history?",
    "How many games have gone to overtime in TSL history?",
    "What is the most points scored by one team in a single game?",
    "How many total games have been played in TSL?",

    # Complex / Cross-Table (5)
    "Which team has drafted the most X-Factor players? (devTrait=3)",
    "How many trades have happened total across all seasons?",
    "Which team has the most players on their roster right now?",
    "Who has the highest-rated player at each position this season?",
    "What is each team's all-time record? Show wins and losses by team name.",
]

CATEGORIES = [
    "Head-to-Head Records",
    "Head-to-Head Records",
    "Head-to-Head Records",
    "Head-to-Head Records",
    "Head-to-Head Records",
    "Owner Records & Rankings",
    "Owner Records & Rankings",
    "Owner Records & Rankings",
    "Owner Records & Rankings",
    "Owner Records & Rankings",
    "Player Stats — Offense",
    "Player Stats — Offense",
    "Player Stats — Offense",
    "Player Stats — Offense",
    "Player Stats — Offense",
    "Player Stats — Defense",
    "Player Stats — Defense",
    "Player Stats — Defense",
    "Player Stats — Defense",
    "Player Stats — Defense",
    "Game Records",
    "Game Records",
    "Game Records",
    "Game Records",
    "Game Records",
    "Complex / Cross-Table",
    "Complex / Cross-Table",
    "Complex / Cross-Table",
    "Complex / Cross-Table",
    "Complex / Cross-Table",
]


async def run_question(i: int, question: str) -> dict:
    """Run one question through the full Codex pipeline."""
    start = time.time()

    # Step 1: NL → SQL
    try:
        sql = await gemini_sql(question, alias_map=ALIAS_MAP)
    except Exception as e:
        return {
            "q": question, "status": "FAIL", "error": f"SQL gen error: {e}",
            "sql": None, "rows": None, "row_count": 0, "answer": None,
            "time": time.time() - start,
        }

    if not sql:
        return {
            "q": question, "status": "FAIL", "error": "No SQL generated",
            "sql": None, "rows": None, "row_count": 0, "answer": None,
            "time": time.time() - start,
        }

    # Step 2: Execute SQL
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows_raw = conn.execute(sql).fetchall()
        rows = [dict(r) for r in rows_raw]
        conn.close()
    except Exception as e:
        return {
            "q": question, "status": "SQL_ERROR", "error": str(e),
            "sql": sql, "rows": None, "row_count": 0, "answer": None,
            "time": time.time() - start,
        }

    # Step 3: SQL results → NL answer
    try:
        answer = await gemini_answer(question, sql, rows)
    except Exception as e:
        return {
            "q": question, "status": "ANSWER_ERROR", "error": str(e),
            "sql": sql, "rows": rows[:10], "row_count": len(rows),
            "answer": None, "time": time.time() - start,
        }

    return {
        "q": question, "status": "PASS", "error": None,
        "sql": sql, "rows": rows[:10], "row_count": len(rows),
        "answer": answer, "time": time.time() - start,
    }


def print_result(i: int, result: dict, category: str):
    """Print one result in the box format."""
    status = result["status"]
    elapsed = result["time"]
    q = result["q"]
    sql = result["sql"] or "(none)"
    row_count = result["row_count"]
    rows = result["rows"]
    answer = result["answer"] or result.get("error", "(none)")

    # Wrap answer text
    wrapped_answer = textwrap.fill(answer, width=90, initial_indent="│   ", subsequent_indent="│   ")

    print(f"\n┌─ Q{i+1:02d} [{status}] ({elapsed:.1f}s) — {category}")
    print(f"│ Question: {q}")
    print(f"│ SQL: {sql}")
    print(f"│ Rows: {row_count}")
    if rows:
        for j, row in enumerate(rows[:10]):
            # Compact row display
            compact = {k: v for k, v in row.items() if v is not None and v != ""}
            row_str = json.dumps(compact, ensure_ascii=False)
            if len(row_str) > 120:
                row_str = row_str[:120] + "..."
            print(f"│   [{j}] {row_str}")
    print(f"│ Answer:")
    print(wrapped_answer)
    if result.get("error"):
        print(f"│ Error: {result['error']}")
    print(f"└{'─' * 60}")


async def main():
    print("=" * 70)
    print("  TSL HISTORY STRESS TEST — 30 Questions")
    print(f"  Database: {DB_PATH}")
    print(f"  Season: {dm.CURRENT_SEASON}")
    print(f"  Alias map entries: {len(ALIAS_MAP)}")
    print("=" * 70)

    # Verify DB exists
    if not os.path.exists(DB_PATH):
        print(f"\n❌ Database not found: {DB_PATH}")
        return

    # Quick DB stats
    conn = sqlite3.connect(DB_PATH)
    game_count = conn.execute("SELECT COUNT(*) FROM games WHERE status IN ('2','3')").fetchone()[0]
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    conn.close()
    print(f"  Completed games: {game_count}")
    print(f"  Tables: {', '.join(sorted(tables))}")
    print("=" * 70)

    results = []
    total_start = time.time()

    for i, question in enumerate(QUESTIONS):
        print(f"\n⏳ Running Q{i+1:02d}/{len(QUESTIONS)}...", end="", flush=True)
        result = await run_question(i, question)
        results.append(result)
        print(f" [{result['status']}] ({result['time']:.1f}s)")

    total_time = time.time() - total_start

    # ── Full output ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  FULL RESULTS")
    print("=" * 70)

    for i, result in enumerate(results):
        print_result(i, result, CATEGORIES[i])

    # ── Summary table ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] != "PASS")

    print(f"\n{'Q#':<5} {'Status':<12} {'Time':>6} {'Rows':>6}  {'Category':<25} Question")
    print("-" * 110)
    for i, result in enumerate(results):
        status_icon = "✅" if result["status"] == "PASS" else "❌"
        print(
            f"Q{i+1:02d}  {status_icon} {result['status']:<10} "
            f"{result['time']:>5.1f}s {result['row_count']:>5}  "
            f"{CATEGORIES[i]:<25} {result['q'][:50]}"
        )

    print("-" * 110)
    print(f"\nTotal: {passed}/{len(QUESTIONS)} PASS, {failed} FAIL")
    print(f"Total time: {total_time:.1f}s (avg {total_time/len(QUESTIONS):.1f}s per question)")
    print()

    # Category breakdown
    cat_results = {}
    for i, result in enumerate(results):
        cat = CATEGORIES[i]
        if cat not in cat_results:
            cat_results[cat] = {"pass": 0, "fail": 0}
        if result["status"] == "PASS":
            cat_results[cat]["pass"] += 1
        else:
            cat_results[cat]["fail"] += 1

    print("Category Breakdown:")
    for cat, counts in cat_results.items():
        total = counts["pass"] + counts["fail"]
        print(f"  {cat:<25} {counts['pass']}/{total}")


if __name__ == "__main__":
    asyncio.run(main())
