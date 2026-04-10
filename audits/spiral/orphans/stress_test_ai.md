# Adversarial Review: stress_test_ai.py

**Verdict:** LIVE (CLI script — keep but harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 173
**Reviewer:** Claude (delegated subagent)
**Total findings:** 5 (0 critical, 2 warnings, 3 observations)

## Summary

Manual stress test for `atlas_ai.generate()` — 50 prompts across Haiku/Sonnet/Opus tiers, plus json/search/system modes. Has `if __name__ == "__main__":` and is run as `python stress_test_ai.py`. Not dead. Has correctness gap (`elapsed` referenced before assignment in the except branch) and lacks any cost guard, which is significant given Opus tier is in the matrix.

## Findings

### WARNING #1: `elapsed` undefined when exception fires before `start = time.time()`
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_ai.py:106-153`
**Confidence:** 0.95
**Risk:** `start = time.time()` is the first line inside the `try` (line 107). If an exception is raised by an earlier expression evaluation (e.g. `q[0]` indexing if QUESTIONS gets a malformed entry), the `except` block at line 148 references `elapsed = time.time() - start` where `start` is undefined → `UnboundLocalError`. The catch swallows the original error and reports the wrong one.
**Vulnerability:** Defensive `elapsed` measurement assumes `start` is always set.
**Impact:** Confusing test failures that mask the real cause. Operator chases an UnboundLocalError instead of the actual API/network failure.
**Fix:** Initialize `start = time.time()` BEFORE the `try` block.

### WARNING #2: No cost guard on Opus stress runs
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_ai.py:65-68, 87-95`
**Confidence:** 0.7
**Risk:** Opus is the highest-cost tier in the AI client. The script runs 3 Opus prompts per execution with `max_tokens=200`, no daily cap, no retry budget. A confused user running this in a loop or accidentally bumping `max_tokens` could burn meaningful budget.
**Impact:** Budget overrun. Per CLAUDE.md, Claude is the primary provider — costs hit the user's Anthropic billing.
**Fix:** Add `--max-cost-usd` arg, estimate per-tier costs, abort if exceeded.

### OBSERVATION #1: Dead-candidate but live CLI
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_ai.py:171-173`
**Confidence:** 0.95
**Risk:** Same as backfill — has main entrypoint, undocumented anywhere.
**Fix:** Document in README or move to `scripts/tests/`.

### OBSERVATION #2: Empty prompt edge case is documented but not validated
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_ai.py:78`
**Confidence:** 0.6
**Risk:** `(Tier.HAIKU, "generate", "")` — empty prompt. If the AI provider rejects empty prompts (some do), the test counts it as FAIL. If the script passed it to atlas_ai which then 400s, the script counts it as FAIL. There's no assertion that empty prompts SHOULD fail; success and failure both look the same.
**Impact:** Edge case isn't actually tested — the test entry is decorative.
**Fix:** Mark empty-prompt entries with `expected="fail"` and check the failure cause.

### OBSERVATION #3: Search mode passes raw text without grounding check
**Location:** `C:/Users/natew/Desktop/discord_bot/stress_test_ai.py:114-118`
**Confidence:** 0.5
**Risk:** `generate_with_search` is called with `system=system or ""` but the QUESTIONS tuple's search entries don't include a system prompt slot. The `or ""` works but indicates the schema isn't enforced — easy to forget.
**Impact:** Inconsistent system prompt usage across modes.
**Fix:** Use a typed dataclass for question entries instead of an untyped tuple.

## Cross-cutting Notes

Three of the four `stress_test_*.py` scripts (this one, codex, history) all share a near-identical pattern: a global QUESTIONS list, an `async def main()`, env setup boilerplate at the top, and per-question latency tracking. They should share common scaffolding (a `_stress_runner.py` helper) so fixes like the `start = time.time()` initialization bug don't have to be applied 3 times. Also, none of them are wired to CI — their value is purely manual.
