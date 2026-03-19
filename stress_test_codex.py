"""
Stress test for Codex NL→SQL→NL pipeline — 10 real TSL history questions.
Exercises atlas_ai + codex_cog.gemini_sql() + codex_cog.gemini_answer() end-to-end.
Run: python stress_test_codex.py
"""
import asyncio
import sqlite3
import json
import time
import sys
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(override=True)

# Patch dm.CURRENT_SEASON before codex_cog imports it
import data_manager as dm
dm.CURRENT_SEASON = "6"

from codex_cog import gemini_sql, gemini_answer, extract_sql
from build_member_db import get_alias_map

DB_PATH = "tsl_history.db"

QUESTIONS = [
    "What is BDiddy86's record vs TheWitt all time?",
    "Who is the worst passer in TSL history? Minimum 50 attempts.",
    "Who has the most wins all time?",
    "What is TrombettaThanYou's all-time record?",
    "Who leads the league in rushing TDs this season?",
    "Which team has the most trades this season?",
    "Who has the most sacks all time?",
    "What is the highest scoring game in TSL history?",
    "How many games has Find_the_Door played total?",
    "Who has the best record in season 5?",
]

async def run_question(i: int, question: str, alias_map: dict):
    """Run one question through the full Codex pipeline."""
    start = time.time()
    errors = []

    # Step 1: NL → SQL
    sql = await gemini_sql(question, alias_map=alias_map)
    sql_time = time.time() - start

    if not sql:
        return {
            "q": question, "status": "FAIL", "error": "No SQL generated",
            "sql": None, "rows": None, "answer": None, "time": sql_time,
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
            "sql": sql, "rows": None, "answer": None, "time": time.time() - start,
        }

    # Step 3: SQL results → NL answer
    answer = await gemini_answer(question, sql, rows)
    total_time = time.time() - start

    return {
        "q": question, "status": "PASS", "error": None,
        "sql": sql, "rows": rows[:5], "row_count": len(rows),
        "answer": answer, "time": total_time,
    }


async def main():
    try:
        alias_map = get_alias_map()
    except Exception:
        alias_map = {}  # tsl_members table may not exist yet

    print(f"\n{'='*70}")
    print(f"  TSL Codex Stress Test — 10 Database Questions (NL → SQL → NL)")
    print(f"{'='*70}\n")

    results = []
    for i, q in enumerate(QUESTIONS, 1):
        r = await run_question(i, q, alias_map)
        results.append(r)

        status = r["status"]
        elapsed = r["time"]

        print(f"┌─ Q{i:02d} [{status}] ({elapsed:.1f}s)")
        print(f"│ Question: {q}")
        if r["sql"]:
            # Truncate long SQL for display
            sql_display = r["sql"].replace("\n", " ")
            if len(sql_display) > 100:
                sql_display = sql_display[:100] + "..."
            print(f"│ SQL: {sql_display}")
        if r.get("row_count") is not None:
            print(f"│ Rows: {r['row_count']}")
        if r.get("rows"):
            for row in r["rows"][:3]:
                print(f"│   {row}")
        if r["answer"]:
            # Word-wrap the answer
            words = r["answer"].split()
            line = "│ Answer: "
            for w in words:
                if len(line) + len(w) + 1 > 78:
                    print(line)
                    line = "│   " + w
                else:
                    line += (" " + w) if len(line) > 10 else w

            if line.strip("│ "):
                print(line)
        if r["error"]:
            print(f"│ ERROR: {r['error']}")
        print(f"└{'─'*69}\n")

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] != "PASS")
    total_time = sum(r["time"] for r in results)

    print(f"{'='*70}")
    print(f"  Results: {passed}/10 passed, {failed} failed")
    print(f"  Total time: {total_time:.1f}s ({total_time/10:.1f}s avg)")
    print(f"{'='*70}\n")

if __name__ == "__main__":
    asyncio.run(main())
