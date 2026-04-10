# Adversarial Review: stress_test_codex.py

**Verdict:** LIVE (CLI script — keep but harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 138
**Reviewer:** Claude (delegated subagent)
**Total findings:** 6 (1 critical, 2 warnings, 3 observations)

## Summary

Manual stress test for the Codex NL→SQL→NL pipeline. Has `if __name__ == "__main__":` and is run as `python stress_test_codex.py`. Not dead. The script has a serious blocking-call-in-async-loop pattern (`sqlite3.connect` inside `async def`) plus a shared mutable state hazard (`dm.CURRENT_SEASON = "6"` patched globally) and no SQL execution timeout.

## Findings

### CRITICAL #1: Hardcoded `dm.CURRENT_SEASON = "6"` corrupts global state
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_codex.py:19-20`
**Confidence:** 0.95
**Risk:** Patches the module-level `data_manager.CURRENT_SEASON` to a literal `"6"` BEFORE importing `codex_cog`. CLAUDE.md explicitly notes that `_build_schema()` "dynamically includes `dm.CURRENT_SEASON` so Gemini always has current season context." Patching this globally and never restoring it means: (a) if anything else in the same Python process imports data_manager (e.g. by accident in a future REPL session), it gets the stale "6" instead of the real current season; (b) if a future TSL season ≠ 6, the stress test silently exercises stale prompts and the operator may not notice.
**Vulnerability:** No `try/finally` to restore the original value, no guard that the original value was actually 6, no warning printed when patched.
**Impact:** Stress test results misleading once season advances; cross-test contamination if multiple stress scripts run in same process.
**Fix:** Read the actual current season from data_manager or accept it as a CLI arg. At minimum, read and print the original value before patching, then restore in a `finally`.

### WARNING #1: Blocking sqlite3 inside async function
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_codex.py:56-61`
**Confidence:** 0.9
**Risk:** `run_question` is `async def` but contains synchronous `sqlite3.connect/execute/close` calls (no `await asyncio.to_thread`). Per ATLAS focus block: "Blocking calls inside `async` functions (sqlite3, requests, time.sleep). All blocking I/O must go through `asyncio.to_thread()`." The script is single-coroutine so it doesn't deadlock anyone, but the test exercises a different pattern than production code which means a future regression where the same pattern leaks into a cog wouldn't be caught here.
**Vulnerability:** Test code models the wrong I/O pattern.
**Impact:** Doesn't simulate how the production codex_cog actually runs SQL. May mask races or timeout issues.
**Fix:** Wrap sqlite calls in `await asyncio.to_thread(...)`.

### WARNING #2: No SQL execution timeout
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_codex.py:56-66`
**Confidence:** 0.7
**Risk:** Some Codex-generated SQL is computationally expensive (cross-table JOINs across `games`, `offensive_stats`, `player_draft_map`). A pathological generated query can hang for minutes. `sqlite3.connect()` has no default `timeout` for query execution (only for the lock acquire).
**Vulnerability:** No `progress_handler` or timeout wrapper.
**Impact:** Stress test hangs indefinitely on a single bad query, blocking the rest of the run.
**Fix:** Use `conn.set_progress_handler(...)` to abort after N statements, or wrap in `asyncio.wait_for(..., timeout=30)`.

### OBSERVATION #1: Dead-candidate but live CLI
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_codex.py:137-138`
**Confidence:** 0.95
**Risk:** Has main entrypoint, undocumented.
**Fix:** Move to `scripts/tests/` or document in README.

### OBSERVATION #2: `extract_sql` imported but never used
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_codex.py:22`
**Confidence:** 0.95
**Risk:** `from codex_cog import gemini_sql, gemini_answer, extract_sql` — `extract_sql` never referenced in the file. Dead import.
**Impact:** Cosmetic; signals stale code.
**Fix:** Remove the import or use it.

### OBSERVATION #3: Test questions assume Season 6 + specific owner names exist
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_codex.py:27-38`
**Confidence:** 0.6
**Risk:** Questions reference `BDiddy86`, `TheWitt`, `TrombettaThanYou`, `Find_the_Door` — if any of these owners leaves the league or their identity mapping changes, the test silently produces empty results without flagging the issue. The orchestration prints "PASS" if SQL executed, even if zero rows.
**Impact:** Tests produce false PASS on missing data.
**Fix:** Add `expected_min_rows` per question.

## Cross-cutting Notes

The `dm.CURRENT_SEASON = "6"` global patch pattern appears in this file AND `stress_test_history.py:21` (same line). Both should be migrated to a `with patched_season("6"):` context manager that restores on exit. The duplicated boilerplate is the symptom; the missing restore is the disease.
