# Adversarial Review: conftest.py

**Verdict:** LIVE (pytest auto-discovery)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 203
**Reviewer:** Claude (delegated subagent)
**Total findings:** 6 (0 critical, 2 warnings, 4 observations)

## Summary

Root-level pytest conftest providing the `test_db` session fixture used by tests in `tests/`. Static grep correctly finds zero `import conftest` statements because pytest auto-discovers conftest files by name — this is NOT dead code. The fixture, however, is fragile because the inline DDL is a snapshot that already drifts from the production schema and silently masks schema drift bugs in tests.

## Findings

### WARNING #1: Inline DDL drifts from production schema
**Location:** `C:/Users/natew/Desktop/discord_bot/conftest.py:12-191`
**Confidence:** 0.9
**Risk:** The `_DDL` constant hand-codes 11 tables (`games`, `teams`, `standings`, `offensive_stats`, `defensive_stats`, `team_stats`, `players`, `player_abilities`, `owner_tenure`, `player_draft_map`, `trades`). Per CLAUDE.md, the canonical schema lives in `tsl_history.db` and is built/synced by `build_tsl_db.sync_tsl_db`. Any column added in production (e.g. new `winPct`, `seed` migrations) will not appear in this fixture, so tests that exercise SELECTs over those columns will fail with "no such column" errors that look like test bugs.
**Vulnerability:** No reflection from real schema — purely declarative.
**Impact:** Schema drift bugs are masked. New columns added to production silently break the fixture without anyone noticing until a test fails for "wrong" reasons.
**Fix:** Either (a) reflect the schema from a freshly-built test DB via `build_tsl_db.sync_tsl_db()` or (b) move the DDL to a SQL file shared with `build_tsl_db.py` so both stay in sync.

### WARNING #2: Fixture is session-scoped but DB has no rows
**Location:** `C:/Users/natew/Desktop/discord_bot/conftest.py:194-203`
**Confidence:** 0.75
**Risk:** Fixture creates an empty schema and returns a path. Any test that opens it will get zero rows back from any query. If the test author assumes the fixture will seed sample rows (a reasonable assumption given the explicit DDL effort), the test passes vacuously.
**Vulnerability:** Empty-state semantics are silent. A test like `assert len(rows) > 0` would obviously fail, but a test like `assert all(r.season == 5 for r in rows)` passes the empty DB by default.
**Impact:** False-positive test passes — tests that should be testing real data behavior pass against an empty DB.
**Fix:** Either rename to `empty_test_db` to make the empty contract explicit, or add a sibling `seeded_test_db` fixture with sample rows.

### OBSERVATION #1: All columns typed as TEXT — inconsistent with real DB
**Location:** `C:/Users/natew/Desktop/discord_bot/conftest.py:13-191`
**Confidence:** 0.85
**Risk:** Per CLAUDE.md, MaddenStats stores numeric fields as strings, so the production DB columns are also TEXT. However, `owner_tenure.games_played` is `INTEGER` here while the rest of the table uses TEXT — inconsistent. If the production schema uses INTEGER for `games_played`, tests that filter `WHERE games_played > 5` will work in tests but compare lexicographically in production.
**Impact:** Off-by-one type bugs only surface in production.
**Fix:** Match production DB types exactly — use `PRAGMA table_info(games)` from a real DB to generate the DDL.

### OBSERVATION #2: Cannot find conftest.py in tests/ directory
**Location:** `C:/Users/natew/Desktop/discord_bot/conftest.py:1`
**Confidence:** 0.85
**Risk:** Pytest looks for conftest.py at every level from rootdir down. Having only a root-level conftest means tests in `tests/` get this fixture but can't override or extend it locally without monkey-patching. Future test additions in `tests/` may conflict.
**Impact:** Test extensibility cliff — new test categories can't add scoped fixtures without touching root conftest.
**Fix:** Move shared fixtures into `tests/conftest.py` and keep root conftest minimal.

### OBSERVATION #3: Synchronous sqlite3 in fixture used by `asyncio_mode = auto`
**Location:** `C:/Users/natew/Desktop/discord_bot/conftest.py:199-202`
**Confidence:** 0.6
**Risk:** `pytest.ini` sets `asyncio_mode = auto`, so all test functions run inside an event loop. Synchronous sqlite3 calls in the fixture body are fine because the fixture itself is sync, but any test that re-opens this DB from inside an async test still has to remember to use `aiosqlite` or `asyncio.to_thread`. The fixture model encourages a sync mindset that conflicts with the async-first codebase.
**Impact:** Encourages blocking-call test patterns that don't reflect real bot operation.
**Fix:** Provide an `async_test_db` fixture using `aiosqlite.connect` for parity with production code paths.

### OBSERVATION #4: `_DDL` is module-private but spans 180 lines
**Location:** `C:/Users/natew/Desktop/discord_bot/conftest.py:12-191`
**Confidence:** 0.5
**Risk:** Maintenance — adding/removing a column requires editing this file plus `build_tsl_db.py` plus the production DB. Three places to drift.
**Impact:** Drift over time.
**Fix:** Single source of truth — load DDL from a shared SQL file.

## Cross-cutting Notes

The classifier missed conftest.py because pytest auto-discovers it by filename, not by import. Recommend updating the orphan classifier to special-case `conftest.py` files. The same logic likely applies to any future `pytest_plugins.py` or `setup.cfg`-declared fixtures.
