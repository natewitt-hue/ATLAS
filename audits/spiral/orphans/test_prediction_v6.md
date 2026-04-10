# Adversarial Review: test_prediction_v6.py

**Verdict:** LIVE (CLI script — keep but harden or remove)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 91
**Reviewer:** Claude (delegated subagent)
**Total findings:** 4 (0 critical, 1 warning, 3 observations)

## Summary

Manual preview script for prediction market V6 cards. Renders 2 cards (list + detail) and saves to disk for visual review. Has `if __name__ == "__main__":` and is run as `python test_prediction_v6.py`. Smallest of the test_* files. Genuinely useful as a one-off render preview but provides zero automated assertions and the "v6" naming suggests this is fossil from a versioned migration that may already be complete. The script also lacks `init_pool()` / `drain_pool()` calls — relies on lazy pool init which may leak browsers.

## Findings

### WARNING #1: Missing pool lifecycle management
**Location:** `C:/Users/natew/Desktop/discord_bot/test_prediction_v6.py:12-87`
**Confidence:** 0.8
**Risk:** Unlike `test_all_renders.py` which calls `init_pool()` / `drain_pool()`, this script just calls the renderers directly. The HTML engine (per CLAUDE.md) uses a 4-page pre-warmed pool. If this script triggers lazy pool init and exits via `asyncio.run(main())`, the pool may not be drained — Playwright pages leak as a leaked subprocess.
**Vulnerability:** No try/finally around drain.
**Impact:** Leaked playwright browser processes between runs.
**Fix:** Wrap in try/finally and call `await drain_pool()` even on exception.

### OBSERVATION #1: "v6" version suffix in filename suggests fossil from migration
**Location:** `C:/Users/natew/Desktop/discord_bot/test_prediction_v6.py:1`
**Confidence:** 0.7
**Risk:** The "v6" suffix is unusual for a test file. Since the docstring says "render prediction market V6 cards for preview" and the import path is `casino.renderer.prediction_html_renderer` (not `prediction_v6`), it appears the v6 was a phase tag during a migration. If v6 is now the only version, the suffix is meaningless. If there was a v5 once, this file may be the only thing keeping v6 alive.
**Impact:** Unclear lifecycle — is v6 still the current version?
**Fix:** Rename to `preview_prediction_card.py` or fold into `test_all_renders.py`.

### OBSERVATION #2: Writes PNGs to project root
**Location:** `C:/Users/natew/Desktop/discord_bot/test_prediction_v6.py:65-67, 83-85`
**Confidence:** 0.85
**Risk:** Writes `test_prediction_list.png` and `test_prediction_detail.png` to the project root directory (not `test_renders/` like its sibling). These pollute the project root after each run and may end up in git if not gitignored.
**Impact:** Repo cruft.
**Fix:** Write to `test_renders/` to match `test_all_renders.py` convention.

### OBSERVATION #3: Dead-candidate but live CLI; no cleanup
**Location:** `C:/Users/natew/Desktop/discord_bot/test_prediction_v6.py:90-91`
**Confidence:** 0.95
**Risk:** Has `if __name__ == "__main__":` and the rendered PNGs are written to disk for visual review — clearly meant to be run manually. Not picked up by pytest because the file lacks pytest functions despite the `test_` prefix.
**Fix:** Either rename to `preview_*` or move to `scripts/preview/`. Document in README.

## Cross-cutting Notes

This file is one of three rendering preview scripts (`test_all_renders.py`, `test_prediction_v6.py`, and embedded test code in casino renderers). Recommend consolidating all render preview scripts into a single `scripts/preview/` directory with a uniform CLI interface.
