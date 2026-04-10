# Adversarial Review: test_query_builder.py

**Verdict:** LIVE (pytest-discoverable, but DUPLICATE of `tests/test_query_builder.py`)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 629
**Reviewer:** Claude (delegated subagent)
**Total findings:** 7 (1 critical, 2 warnings, 4 observations)

## Summary

Comprehensive pytest test suite for `oracle_query_builder.py`. The file uses standard pytest classes (`TestDomainKnowledge`, `TestQueryBuilder`, `TestDomainFunctions`, `TestUtilities`, `TestSQLSafety`) and IS pytest-discoverable. However, **a duplicate file with the same name exists at `tests/test_query_builder.py`** — pytest will collect BOTH and run them, leading to duplicate execution and potential conflicts. The classification doc lists `test_query_builder.py` (root) as having zero importers and `tests/test_query_builder.py` as one of the importers of `oracle_query_builder.py`. This file is the "fat" version (629 LOC, ~95 tests) while `tests/test_query_builder.py` is much smaller. Recommend consolidating.

## Findings

### CRITICAL #1: Duplicate test file with same name in two pytest-discoverable locations
**Location:** `C:/Users/natew/Desktop/discord_bot/test_query_builder.py:1` AND `C:/Users/natew/Desktop/discord_bot/tests/test_query_builder.py`
**Confidence:** 0.95
**Risk:** Both files exist at pytest-discoverable locations. Pytest's `rootdir` discovery would collect both, but the two files may have overlapping class names (`TestDomainKnowledge`, etc.) which causes a `pytest.ImportError` because pytest can't import two modules with the same name. In practice, one of two outcomes:
  (a) pytest collects only one (whichever it finds first) and silently skips the other — meaning ~95 tests in this file may never run despite a passing CI signal;
  (b) pytest errors out at collection with `ImportError: import file mismatch`, causing the whole test suite to fail.
This is an active footgun. The classification doc shows `tests/test_query_builder.py` as the LIVE file with importer count = 1; the root file has 0 importers, which is consistent with pytest collecting only one of them and the importer count metric only counting the "live" one.
**Vulnerability:** Two `test_query_builder.py` files in the same project.
**Impact:** Either dead tests (95 cases never run) or a CI failure on collection. Both are bad outcomes for a test suite that's supposed to gate Phase 1 of Oracle v3.
**Fix:** Delete one. Most likely, the root version is the "real" comprehensive test and `tests/test_query_builder.py` is the slim sibling. Move comprehensive tests to `tests/test_query_builder_full.py` (rename) and delete the original `tests/test_query_builder.py` to remove the collision.

### WARNING #1: Filter parameters typed inconsistently
**Location:** `C:/Users/natew/Desktop/discord_bot/test_query_builder.py:185-219`
**Confidence:** 0.7
**Risk:** Test asserts string forms of int filters: `assert "6" in params` (line 188), `assert "1" in params` (line 193), `assert "2" in params` (line 197). This implies `Query.filter(season=6)` stores params as strings, not ints. CLAUDE.md notes "MaddenStats stores numeric fields as strings" — so this is correct behavior for the schema. But `assert 4 in params` (line 255, 364) expects an int. The test is inconsistent — sometimes asserts string, sometimes int. If the QueryBuilder ever normalizes types (e.g. all string), the int asserts break. If it normalizes the other way, the string asserts break.
**Vulnerability:** No clear contract on param types.
**Impact:** Brittle test that could fail on unrelated refactors.
**Fix:** Standardize on string params (matching schema) and document the contract.

### WARNING #2: `test_filter_user_non_games_table` doesn't check the resulting params
**Location:** `C:/Users/natew/Desktop/discord_bot/test_query_builder.py:211-214`
**Confidence:** 0.6
**Risk:** Test only asserts `"SELECT teamName FROM teams" in sql` — it doesn't assert that the user param is correctly bound or that the subquery filters by `userName`. A regression where the subquery accidentally filters by `teamName = ?` would still pass this test.
**Impact:** Subquery contract is under-tested.
**Fix:** Assert the full subquery shape.

### OBSERVATION #1: Dead-candidate is wrong — pytest discovers it
**Location:** `C:/Users/natew/Desktop/discord_bot/test_query_builder.py:1-629`
**Confidence:** 0.95
**Risk:** The classification doc flagged this as DEAD-CANDIDATE because no `import test_query_builder` exists. But pytest discovers test files by glob, not import. This file IS live in the test suite (subject to the duplicate-name issue above).
**Fix:** Update orphan classifier to special-case `test_*.py` files at pytest-rootdir level.

### OBSERVATION #2: SQL injection test is asserting non-injection (good but limited)
**Location:** `C:/Users/natew/Desktop/discord_bot/test_query_builder.py:603-611`
**Confidence:** 0.7
**Risk:** `test_parameterized_no_string_interpolation` asserts `"TheWitt" not in sql` and `"TheWitt" in params`. This is a good test for the most common injection vector but doesn't cover edge cases like:
  - User input with `?` characters
  - User input with placeholder-like patterns
  - User input injected into table or column names (which are NOT parameterized — `Query("nonexistent_table")` only checks against a whitelist).
**Impact:** SQL safety coverage is partial.
**Fix:** Add edge case tests for malicious table names and column names that bypass the whitelist.

### OBSERVATION #3: 95+ tests but no parameterized tests
**Location:** `C:/Users/natew/Desktop/discord_bot/test_query_builder.py:46-630`
**Confidence:** 0.5
**Risk:** Test functions like `test_passing_stats_are_qb_filtered` iterate through hardcoded lists. pytest's `parametrize` would make individual test names visible in CI output, helping pinpoint which stat key broke without re-reading the test source.
**Fix:** Use `@pytest.mark.parametrize`.

### OBSERVATION #4: `test_all_valid_tables` enumerates 11 tables — must stay in sync with whitelist
**Location:** `C:/Users/natew/Desktop/discord_bot/test_query_builder.py:175-184`
**Confidence:** 0.6
**Risk:** Hardcoded table list duplicates the whitelist that lives inside `oracle_query_builder.Query.__init__`. If a new table is added to the whitelist, this test still passes (it just doesn't test the new table). If a table is removed, this test fails — which is good but the failure mode is confusing.
**Fix:** Import the whitelist from `oracle_query_builder` and iterate it.

## Cross-cutting Notes

The duplicate file issue (`test_query_builder.py` at root AND in `tests/`) is the headline finding. Recommend a project-wide audit: `find . -name "test_*.py" | xargs -n1 basename | sort | uniq -d` to surface other duplicates. The orphan classifier should also surface name collisions explicitly.
