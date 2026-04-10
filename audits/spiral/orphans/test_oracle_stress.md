# Adversarial Review: test_oracle_stress.py

**Verdict:** LIVE (CLI script — keep but harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 754
**Reviewer:** Claude (delegated subagent)
**Total findings:** 8 (1 critical, 3 warnings, 4 observations)

## Summary

Largest of the orphan files. 98 hand-curated test cases for Oracle's intent detection + SQL execution pipeline, with 14 different validators. Has `if __name__ == "__main__":` and per the orphan classification doc is one of two importers of `oracle_agent.py` and `codex_intents.py` — meaning this file is silently propping up classification of other modules. Not dead, but it has fragility issues: SQL injection test cases that don't actually test what they claim, hardcoded owner names that drift when the league roster changes, and no automated CI hookup.

## Findings

### CRITICAL #1: SQL injection tests verify no protection at all
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:559-575`
**Confidence:** 0.85
**Risk:** "Category G: SQL Injection Safety Tests" includes 4 test cases with `[_val_has_rows()]` as the only validator. The validator checks "did the SQL execute and return rows" — that is the OPPOSITE of what should be checked. A SQL injection that successfully ran a `DROP TABLE games` would set `_val_has_rows()` to FALSE (because the table is empty/missing) and the test would FAIL — but for the wrong reason. A SQL injection that ran `UNION SELECT * FROM games` would set `_val_has_rows()` to TRUE and the test would PASS — silently exfiltrating data through the test harness. The injection tests don't actually verify the parameterization is intact; they just verify the pipeline doesn't crash on hostile input.
**Vulnerability:** Validator semantics are inverted vs intent.
**Impact:** False sense of SQL safety. The test pretends to cover injection but covers nothing of the kind.
**Fix:** Add a real validator: `_val_no_unauthorized_tables(["sqlite_master", "users", "tokens"])` that checks the SQL statement for forbidden terms; or assert that the pre-injection sanitization rejected the input by checking `intent != expected_intent` or `tier > 1`.

### WARNING #1: Hardcoded owner names create test rot over time
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:200-575`
**Confidence:** 0.85
**Risk:** Test cases reference 14+ specific TSL owners (`TheWitt`, `Killa`, `Tuna`, `Diddy`, `Witt`, `Chokolate_Thunda`, `MeLLoW_FiRe`, `KillaE94`, `Saucy0134`, `BDiddy86`, `Find_the_Door`, `Mr_Clutch723`, `D-TownDon`, `Shottaz`, `Ronfk`). If any of these leave the league, change handles, or are merged into the alias map, the corresponding test cases silently produce zero rows or wrong matches. Tests like Q87 explicitly check `_val_meta_key("owner2", "KillaE94")` — that's a literal string assertion that breaks the moment Killa's canonical username changes.
**Vulnerability:** Test data is league-state-dependent.
**Impact:** Test rot. Eventually most "PASS" outputs become unreliable signals.
**Fix:** Use synthetic owner fixtures or seed a known-good test DB.

### WARNING #2: `_val_opponent_filter` and `_val_team_filter` don't catch the right errors
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:97-112, 161-173`
**Confidence:** 0.7
**Risk:** Both validators iterate rows and return TRUE on first match, FALSE if zero matches. Neither asserts that ALL rows match the filter. So a query that returns 5 games where 1 is `vs Killa` and 4 are random other opponents would PASS the opponent filter validator. The opponent_filter validator does check "if matching < total" but only after iterating; for `_val_team_filter` there's no such check.
**Vulnerability:** Validator returns prematurely on first match.
**Impact:** False PASSes on partially-correct query results. Subtle bugs in WHERE clauses go undetected.
**Fix:** Iterate ALL rows; track `mismatches`; return False if `mismatches > 0`.

### WARNING #3: Force-agent path swallows SQL execution errors
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:587-620`
**Confidence:** 0.7
**Risk:** When `--agent` is set, the code calls `oracle_agent.run_agent` and unpacks `agent_result.data`. The error from a failed SQL execution is buried in `agent_result.error` but the validators run on `agent_result.data` which is the rows or empty list. A test that should fail with "SQL syntax error" passes silently if the validators happen to return True on `[]`.
**Vulnerability:** Error pathway disconnected from validation pathway.
**Impact:** Agent regressions go undetected.
**Fix:** If `agent_result.error` is non-None, fail the test with that error.

### OBSERVATION #1: Dead-candidate but live test harness propping up other modules
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:753-754`
**Confidence:** 0.95
**Risk:** Per classification doc, this file is one of two importers for `codex_intents.py` and one of two for `oracle_agent.py`. If this is moved/quarantined, those modules' import counts drop and they may be misclassified.
**Fix:** Document role. Convert to proper pytest under `tests/` so it runs in CI.

### OBSERVATION #2: Validator factories have poor closure semantics
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:31-194`
**Confidence:** 0.6
**Risk:** Each validator is built via `def _val_x(arg): def validator(...): ... return validator`. Captures `arg` as closure. If the test list is mutated after building (it's not, but...), the closure could see stale values. More importantly, every validator returns plain `(bool, str)` tuples — no structured error class — making aggregation hard.
**Fix:** Use a `Validator` dataclass with `name`, `check(rows, meta) -> ValidationResult`.

### OBSERVATION #3: Validators are silently NO-OP when column is missing
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:36-43, 55-60`
**Confidence:** 0.7
**Risk:** `_val_sort_desc` returns `True, f"column '{col}' not found"` if no values exist. So a test that expects sorted-DESC by `total_stat` but the SQL returned a column named differently silently PASSES. This is the opposite of strict validation.
**Impact:** Schema/column drift silently passes the test.
**Fix:** Return False if column is missing in expected presence cases. Add an explicit "column may be absent" flag.

### OBSERVATION #4: 754 LOC monolith
**Location:** `C:/Users/natew/Desktop/discord_bot/test_oracle_stress.py:1-754`
**Confidence:** 0.5
**Risk:** Single file with categories A through G plus 98 test cases. Hard to maintain, hard to add a single test case without scrolling 700 lines.
**Fix:** Split into per-category files under `tests/oracle/`.

## Cross-cutting Notes

This file plus `test_query_builder.py` (root version) are the two largest "orphan" test files and both are conscious decisions to test through unconventional entry points. They should both be migrated to `tests/` and made discoverable by pytest. The current state — root-level scripts that share names with files in `tests/` — is a maintenance trap. Note also: `tests/test_query_builder.py` ALSO exists, so the root-level `test_query_builder.py` is a duplicate or successor to the tests/ version.
