# Adversarial Review: stress_test_history.py

**Verdict:** LIVE (CLI script — keep but harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 282
**Reviewer:** Claude (delegated subagent)
**Total findings:** 6 (1 critical, 2 warnings, 3 observations)

## Summary

Manual stress test for the TSL History database — 30 questions through the Codex pipeline. Has `if __name__ == "__main__":` and is run as `python stress_test_history.py`. Not dead. Same pattern as `stress_test_codex.py` (and shares the global state corruption hazard) plus a parallel-list maintenance footgun where QUESTIONS and CATEGORIES must stay aligned by hand.

## Findings

### CRITICAL #1: Hardcoded `dm.CURRENT_SEASON = "6"` corrupts global state
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_history.py:21-22`
**Confidence:** 0.95
**Risk:** Same as `stress_test_codex.py`. Patches `data_manager.CURRENT_SEASON` to literal `"6"` before importing codex_cog. No restore. Per CLAUDE.md, `_build_schema()` dynamically reads this value, so all schema-dependent prompts get the stale value as long as the process is alive. If a future season != 6, this script will silently exercise stale prompts.
**Vulnerability:** No `try/finally`, no warning, no validation that the original value differed.
**Impact:** Stress test results misleading once season advances.
**Fix:** Read actual current season or accept as CLI arg. Restore in a `finally`.

### WARNING #1: `QUESTIONS` and `CATEGORIES` are parallel lists with no length check
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_history.py:37-112, 239`
**Confidence:** 0.85
**Risk:** Two parallel lists of 30 items each. The `print_result(i, result, CATEGORIES[i])` call relies on perfect index alignment. If a contributor adds a new question to `QUESTIONS` and forgets to add to `CATEGORIES`, `CATEGORIES[i]` raises `IndexError` mid-run, killing the rest of the test. If they add to `CATEGORIES` and forget `QUESTIONS`, the labels misalign silently.
**Vulnerability:** Parallel-list anti-pattern with no validation.
**Impact:** Silent label misalignment is the worse outcome — operator sees wrong category for the wrong question.
**Fix:** Use `[(question, category), ...]` tuples or a dataclass. Add `assert len(QUESTIONS) == len(CATEGORIES)` at module load.

### WARNING #2: Blocking sqlite3 inside async function + no timeout
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_history.py:137-148, 213-218`
**Confidence:** 0.9
**Risk:** Same as `stress_test_codex.py`. `sqlite3.connect/execute/close` inside `async def run_question`, no `asyncio.to_thread`, no per-query timeout. The `main()` function also has a synchronous `sqlite3.connect` for DB stats which is fine because it's not in an async context, but the `run_question` violation is real.
**Vulnerability:** Blocking I/O in async code.
**Impact:** Pathological queries hang the script indefinitely.
**Fix:** Wrap in `await asyncio.to_thread(...)` and add `asyncio.wait_for(timeout=30)`.

### OBSERVATION #1: Dead-candidate but live CLI
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_history.py:281-282`
**Confidence:** 0.95
**Risk:** Has main entrypoint, undocumented.
**Fix:** Move to `scripts/tests/` or document in README.

### OBSERVATION #2: `extract_sql` imported but never used
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_history.py:24`
**Confidence:** 0.95
**Risk:** Same dead import as stress_test_codex.py — `from codex_cog import gemini_sql, gemini_answer, extract_sql` and `extract_sql` is never used.
**Impact:** Cosmetic; signals copy-paste.
**Fix:** Remove the import.

### OBSERVATION #3: Test questions hardcode owner names + dev trait values
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_history.py:39-78`
**Confidence:** 0.6
**Risk:** Questions like "Which team has drafted the most X-Factor players? (devTrait=3)" embed the devTrait integer. Per CLAUDE.md, devTrait mapping is `0=Normal, 1=Star, 2=Superstar, 3=Superstar X-Factor`. If TSL ever migrates the mapping (e.g. to use string labels), this question silently produces wrong-but-plausible results.
**Impact:** Subtle drift between test fixture and production schema.
**Fix:** Reference `dm.DEV_TRAIT_MAP[3]` instead of the literal integer in the prompt.

## Cross-cutting Notes

This file, `stress_test_codex.py`, and `stress_test_ai.py` are clearly forks of the same template. The `dm.CURRENT_SEASON = "6"` patch is a copy-paste hazard (2 instances confirmed); fixing it once doesn't fix it everywhere. Recommend a shared `_stress_test_harness.py` providing the boilerplate (CLI args, env loading, season patching context manager, async sqlite helper).
