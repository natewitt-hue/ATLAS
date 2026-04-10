# Adversarial Review: test_all_renders.py

**Verdict:** LIVE (CLI script — keep but harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 381
**Reviewer:** Claude (delegated subagent)
**Total findings:** 7 (1 critical, 2 warnings, 4 observations)

## Summary

Manual render-pipeline smoke test that exercises every card renderer with sample data and writes PNGs to `test_renders/`. Has `if __name__ == "__main__":` and is run as `python test_all_renders.py`. Not dead — and per the orphan classification doc, `card_renderer.py` lists this as one of its two importers, so this file is partly responsible for keeping that module classified LIVE. However, the script imports a private internal type `flow_live_cog.PlayerSession` via direct attribute mutation, breaking encapsulation.

## Findings

### CRITICAL #1: Test silently couples to PlayerSession internals
**Location:** `C:/Users/natew/Desktop/discord_bot/test_all_renders.py:163, 166-178`
**Confidence:** 0.85
**Risk:** Imports `PlayerSession` from `flow_live_cog` then directly assigns 12 internal fields (`session.started_at`, `session.last_activity`, `session.total_games`, `session.wins`, `session.losses`, `session.pushes`, `session.net_profit`, `session.biggest_win`, `session.biggest_loss`, `session.current_streak`, `session.best_streak`, `session.games_by_type`). If `PlayerSession` ever adds `__slots__` or becomes a frozen dataclass, this test breaks loudly. Worse, if `PlayerSession` adds a new field that the renderer reads but this test doesn't set, the renderer crashes on `AttributeError` only at test time and the test "successfully" reports a render failure that's actually a fixture gap.
**Vulnerability:** Test fixture is hand-rolled instead of using a constructor or factory, so it's never in sync with production usage.
**Impact:** Render tests pass on stale fixtures while the production rendering path fails. Worse: if PlayerSession constructor changes its required args, test still works because it bypasses construction by mutating attributes after the fact.
**Fix:** Add a `PlayerSession.from_dict()` factory or `make_test_session()` helper in `flow_live_cog` and use it here.

### WARNING #1: `init_pool()` / `drain_pool()` not in try/finally
**Location:** `C:/Users/natew/Desktop/discord_bot/test_all_renders.py:23-24, 361`
**Confidence:** 0.85
**Risk:** `init_pool()` is awaited at the top of `main()`. `drain_pool()` is awaited near the bottom (line 361). If ANY rendering call between those points raises an unhandled exception, `drain_pool()` is never called and Playwright pages leak — `pool` may hold open browser tabs/processes that prevent the script from exiting cleanly. The harness is supposed to close pages back into the pool but a fatal exception leaves them dangling.
**Vulnerability:** Per ATLAS focus block: "Resource leaks: Playwright pages not returned to the pool."
**Impact:** Leaked playwright browsers on failed test runs; subsequent runs may hit "address in use" or memory pressure.
**Fix:** Wrap the body in `try/finally: await drain_pool()`.

### WARNING #2: `test_card` swallows exceptions silently as "FAIL" with truncated info
**Location:** `C:/Users/natew/Desktop/discord_bot/test_all_renders.py:28-40`
**Confidence:** 0.75
**Risk:** `except Exception as e: ... print(f"  FAIL {name}: {e}")` — the exception is reduced to its `str()`, which often loses traceback info that's the actual debugging signal. Also a `KeyboardInterrupt` (CancelledError in async) is NOT caught here, but a TypeError or AttributeError is, and both look the same. A test author can't tell from the output whether the renderer is broken or whether the test data is malformed.
**Vulnerability:** Wide `except Exception` swallows class.
**Impact:** Diagnosing render failures requires re-running with manual instrumentation.
**Fix:** Use `traceback.format_exc()` and print to stderr with the full stack.

### OBSERVATION #1: Dead-candidate but live CLI
**Location:** `C:/Users/natew/Desktop/discord_bot/test_all_renders.py:380-381`
**Confidence:** 0.95
**Risk:** Has main entrypoint, named `test_*.py` so pytest may discover it but it isn't actually a pytest file (no `def test_xxx` functions, just a `main()`). pytest would import it, see no test functions, and skip it. So the name is misleading.
**Fix:** Either rename to `render_smoke.py` or refactor into proper pytest functions.

### OBSERVATION #2: Test deduplicates from card_renderer.py - keeping it alive
**Location:** `C:/Users/natew/Desktop/discord_bot/test_all_renders.py:304`
**Confidence:** 0.85
**Risk:** `from card_renderer import render_trade_card` is one of only TWO importers of `card_renderer.py` (per classification doc). If this script is moved or quarantined, `card_renderer.py` becomes a candidate for cleanup. The test is acting as artificial life support for production code via test imports.
**Impact:** Hides the true import graph.
**Fix:** Document this dependency, or migrate the trade card test to a real pytest under `tests/`.

### OBSERVATION #3: Hardcoded sample data drifts from real schemas
**Location:** `C:/Users/natew/Desktop/discord_bot/test_all_renders.py:307-326`
**Confidence:** 0.55
**Risk:** Trade data sample includes specific fields (`status`, `band`, `team_a_name`, `team_a_owner`, `players_a`, etc.). If the trade engine adds a required field, the test won't break (defaults likely), but the rendered card silently omits the new field.
**Impact:** Render schema drift goes unnoticed.
**Fix:** Build sample data from a schema definition or test factory.

### OBSERVATION #4: `Group A` is missing from the script
**Location:** `C:/Users/natew/Desktop/discord_bot/test_all_renders.py:42, 109, 157, 229, 301, 342`
**Confidence:** 0.7
**Risk:** Sections are labeled `Group B`, `Group C`, ..., `Group G` — there's no `Group A`. Either it was deleted in a refactor or it's elsewhere. The asymmetry signals incomplete cleanup.
**Impact:** Confused future maintainer thinks they're missing tests.
**Fix:** Renumber B–G as A–F or document why A is absent.

## Cross-cutting Notes

This script is the only smoke test for the rendering pipeline. The classification doc says `card_renderer.py` has importers `genesis_cog.py, test_all_renders.py` — meaning if this test is removed, the rendering pipeline is left without ANY automated coverage. Recommend: convert this entire script into proper pytest tests under `tests/test_renders.py` so they actually run in CI and the import graph is honest.
