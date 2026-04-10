# Adversarial Review: analysis.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 606
**Reviewer:** Claude (delegated subagent)
**Total findings:** 16 (3 critical, 7 warnings, 6 observations)

## Summary

Core analytics shim. Synchronous DataFrame pipeline called exclusively from async Discord handlers, with multiple broken contracts against `data_manager`: `get_last_n_games` is now `async` but is awaited nowhere, `get_weekly_results` returns camelCase user keys but analysis reads snake_case, and `df_team_stats` is an alias for `df_standings` (so red-zone / third-down columns silently never resolve). Shipping as-is, any `team_profile` path crashes or produces garbage, and `weekly_recap` context strings raise `KeyError`.

## Findings

### CRITICAL #1: `team_profile` stuffs an un-awaited coroutine into `result["recent"]`, then iterates it
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:182, 567-576`
**Confidence:** 0.97
**Risk:** `dm.get_last_n_games(team_name, 5)` is declared `async def get_last_n_games(...)` in `data_manager.py:814`. Calling it without `await` from a synchronous function returns a coroutine object. Line 182 stores that coroutine as `result["recent"]`. Line 567 downstream tests `if d["recent"]:` (a coroutine is truthy) and line 569 iterates it in `for g in d["recent"]` — iterating a coroutine raises `TypeError: 'coroutine' object is not iterable`.
**Vulnerability:** analysis.py is strictly synchronous, but data_manager has migrated `get_last_n_games` to async. The `team_profile` path is entered from `route_query` → `head_to_head` and from oracle_cog's stats commands, every one of which runs inside an async command handler after `defer()`. There is no `run_coroutine_threadsafe`, no `asyncio.run`, no `await`, no `to_thread`. The `"recent"` field has never been a list on this code path.
**Impact:** Any `/stats` command that resolves to `team_profile` or `h2h` either crashes (TypeError while rendering context string) or produces a bogus `"Last 5 form:"` line. `build_context_string` is fed to Gemini, so the whole ATLAS response errors out. Additionally, Python emits `RuntimeWarning: coroutine 'get_last_n_games' was never awaited` on every call, which masks unrelated warnings.
**Fix:** Either (a) convert `team_profile`/`head_to_head`/`route_query` to `async def` and `await dm.get_last_n_games(...)`, or (b) add a sync shim in `data_manager` that runs the coroutine via `asyncio.to_thread` helpers. Prefer (a) — propagate async through the analytics layer, since callers are already async.

### CRITICAL #2: `build_context_string` reads `g['home_user']` / `g['away_user']`, but `get_weekly_results` emits `homeUser` / `awayUser`
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:527-532`
**Confidence:** 0.95
**Risk:** `weekly_recap` returns `data["games"]` directly from `dm.get_weekly_results(...)`. In `data_manager.py:913-914` and `944-945` the per-game dict is constructed with keys `"homeUser"` and `"awayUser"`. Line 531 of analysis.py reads `g['home_user']` and `g['away_user']`, which do not exist. Dict subscript raises `KeyError`.
**Vulnerability:** No `.get(...)` fallback, no schema validation, no test coverage. The function is invoked from any oracle `/stats recap` command, from the `btn_recap` button, and anywhere `route_query` hits the "recap"/"last week"/"results" keyword branch (line 395). Every single path explodes on the first game in the loop.
**Impact:** `/stats recap` slash command, oracle recap buttons, and route_query recap fallback raise `KeyError: 'home_user'`. Users see a generic interaction failure.
**Fix:** Change line 531 to `g.get('homeUser', '')` and `g.get('awayUser', '')`, or normalize keys to snake_case in data_manager. Analysis-side fix is one line and safest for Ring 2.

### CRITICAL #3: `dm.df_team_stats` is an alias for `df_standings`, so red-zone / third-down / penalty columns silently never match
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:217-224, 497-500`
**Confidence:** 0.92
**Risk:** In `data_manager.py:683-684` the module wires `df_team_stats = _l_df_standings` — i.e., the two names point to the **same** standings DataFrame. Standings from `/standings` do not include `offRedZonePct`, `off3rdDownConvPct`, `defRedZonePct`, `penalties`, or `penaltyYds`. Line 221-224 call `r.get("offRedZonePct")` etc., which return `None`, silently producing `"Red Zone %": None` dict entries. Line 223 calls `int(r.get('penalties', 0))` which becomes a hard-coded `"0 (0 yds)"` lie when the column is missing. Line 499-500 build stat leaders on `dm.df_team_stats` for red-zone columns that never exist, so `stat_leaders` returns `[]` for every query matching "red zone".
**Vulnerability:** `stat_leaders` has a silent early-return on missing `stat_col`: `if df.empty or stat_col not in df.columns: return []`. No log, no warning, no fallback. Callers never know the columns are missing. The team_profile enrichment at 217-224 silently stuffs `None` values into the offense/defense dicts, rendered later as `"Red Zone %: None"`.
**Impact:** Every user query about "red zone", "3rd down", or "penalties" returns a lie or empty output. `team_profile` embeds show `None` fields. This looks like missing data but is a module-wiring bug in `data_manager` that analysis.py will never observe. A silent data-fidelity issue in an analytics module is worst-case — users make decisions on false output.
**Fix:** Either load a real team-stats source into `_l_df_team_stats` in data_manager, or defensively check `stat_col in df.columns` and log a warning when `df_team_stats is df_standings`. At a minimum, `team_profile` should skip the enrichment block when `dm.df_team_stats is dm.df_standings` (identity check) and log a warning so the mis-wiring is visible.

### WARNING #1: Synchronous DataFrame copies and sorts on the event loop in hot analytics paths
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:40, 234, 246, 278, 365, 454`
**Confidence:** 0.80
**Risk:** Every public function — `stat_leaders`, `team_profile`, `power_rankings`, `recent_trades`, `_keyword_stats` — calls `df.copy()` on shared DataFrames and then performs sorts, pandas-to-numeric coercions, and boolean indexing. These are synchronous CPU-bound operations called from async Discord handlers. `_keyword_stats` can invoke `stat_leaders` 12+ times in a single query (see line 436-500).
**Vulnerability:** Nothing inside analysis.py is wrapped in `asyncio.to_thread(...)`. Every call path from oracle_cog (`/stats`, recap buttons) runs inline in the event loop. One slow call per request is tolerable; 12 chained calls in one query is a discoverable stall during peak TSL hours or when a nightly audit / Playwright render is also active.
**Impact:** Discord heartbeats can miss. Users notice interaction-failed responses.
**Fix:** Wrap analytics entry points in `asyncio.to_thread` at the caller, or convert analysis.py to async-first and let each public function `await asyncio.to_thread(self._sync_impl, ...)`.

### WARNING #2: `stat_leaders` full-DataFrame copy per call in hot loop
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:40-45`
**Confidence:** 0.70
**Risk:** Line 40 unconditionally runs `temp = df.copy()`, then line 41 mutates `temp[stat_col] = pd.to_numeric(...)`. This is a full DataFrame copy for leaderboard requests that only need a single column. For `_keyword_stats` calling this 12+ times, the caller pays 12 full DataFrame copies per query.
**Vulnerability:** No column-subset projection. Hot-path allocation pressure can stall the event loop.
**Impact:** Latency spikes on leaderboard requests. GC churn.
**Fix:** Project only required columns before copy: `temp = df[[stat_col] + [c for c in [min_col, "extendedName", "fullName", "teamName", "pos"] if c and c in df.columns]].copy()`. Or use `df.nlargest(top_n, stat_col)` (no copy needed).

### WARNING #3: `power_rankings` normalize() collapses when all teams have identical values (preseason)
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:285-303`
**Confidence:** 0.78
**Risk:** `normalize(series)` returns `(series - mn) / (mx - mn + 1e-9)`. When `mn == mx`, the `1e-9` floor prevents DivisionByZero but the result is `~0` for every team. That silently zeros out any dimension where all teams have the same value (common in preseason for `offTotalYdsRank` / `defTotalYdsRank`). The composite collapses onto `winPct * 40 + netPts * 30`, silently dropping 15 weight on turnover diff and 15 combined on off/def rank.
**Vulnerability:** No preseason guard. When `offTotalYdsRank` is missing entirely, `.fillna(16)` at line 282 makes the column constant.
**Impact:** During weeks 0-2, power rankings quietly ignore rank dimensions. Users making betting decisions on early-season rankings get distorted scores.
**Fix:** Early-return `[]` or a "not yet ranked" payload if `df["offTotalYdsRank"].nunique() <= 1` or if current week < 3. Or log a warning when any dimension has `mn == mx`.

### WARNING #4: `recent_trades` off-by-one risk on `seasonIndex` — API may be 0-based
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:362, 367-368`
**Confidence:** 0.60
**Risk:** `target_season = season or dm.CURRENT_SEASON` is 1-based per ATLAS convention. The raw `/trades/search` API may return each trade's `seasonIndex` as 0-based (analogous to `weekIndex`, the CLAUDE.md-flagged trap). Line 368 compares `seasonIndex == target_season`, producing zero results if the indices are off by one.
**Vulnerability:** Silent: `df.head(n).to_dict("records")` returns `[]` and the UI says "no trades". No assertion confirms alignment. Inference is from the `weekIndex` precedent in CLAUDE.md.
**Impact:** `/stats trades` may show zero trades. Gemini context says "=== RECENT TRADES (Season N) ===" with empty body.
**Fix:** Verify `seasonIndex` semantics against data_manager, then either subtract 1 before comparison or fix at ingestion. Add a log line reporting `len(df)` before and after the seasonIndex filter.

### WARNING #5: `recent_trades` `.str.lower()` crashes on non-string status dtype
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:370`
**Confidence:** 0.65
**Risk:** `df = df[df["status"].str.lower() == "accepted"]` — if the API returns `status` as integer (some endpoints do, per `data_manager.py:884`), `.str.lower()` raises `AttributeError: Can only use .str accessor with string values`. The filter is a no-op on the happy path since the API already filters to "accepted".
**Vulnerability:** No `try/except`, no dtype check.
**Impact:** If API schema drifts to int status, `/stats trades` and `route_query` trade branch crash for every user.
**Fix:** `df[df["status"].astype(str).str.lower() == "accepted"]`, or remove the filter entirely since the server already filters.

### WARNING #6: `find_players` unbounded substring match produces false positives
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:60-76`
**Confidence:** 0.75
**Risk:** Line 71 tests `len(last) > 3 and last in q`. A player "Hill" matches "downhill", "Cook" matches "cookie", "Chase" matches "chasing the MVP". Unanchored substring with no word boundary.
**Vulnerability:** `full.split()[-1]` last-name extraction + raw `in` check.
**Impact:** Silent misrouting — user asks about "chasing the MVP" and gets a player profile for Ja'Marr Chase instead of a stat block. User never realizes the query was misclassified.
**Fix:** Use a regex with word boundaries: `re.search(rf'\b{re.escape(last)}\b', q)`. Consider a negative-list of English words that collide with common surnames.

### WARNING #7: `_keyword_stats` Drop Rate block hard-references columns without existence guard
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:454-460`
**Confidence:** 0.80
**Risk:** Line 454 indexes `dm.df_offense[dm.df_offense["recCatches"] >= REC_MIN_CATCH]`. If `recCatches` is missing (preseason, schema change, fetch failure), raises `KeyError`. Line 455 divides `_wr["recDrops"] / ...` — same vulnerability. `stat_leaders` has a column guard; this inline block does not.
**Vulnerability:** No `if {"recCatches", "recDrops"} - set(dm.df_offense.columns)` guard. No try/except.
**Impact:** `/stats worst drop rate` or any query containing "rec"/"catch" crashes the interaction during preseason or API schema drift.
**Fix:** Add `if {"recCatches", "recDrops"} - set(dm.df_offense.columns): return` at the top of the drop-rate block, or guard individually per column.

### OBSERVATION #1: `player_profile` stores raw numpy types in result dict
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:149, 164`
**Confidence:** 0.85
**Risk:** `result["offense"] = {c: r[c] for c in num if r[c] != 0}` stores numpy.int64/float64 scalars. Downstream string formatting works, but `json.dumps(result)` fails without a custom encoder.
**Vulnerability:** No primitive-type conversion. Silent trap for any downstream serialization, caching, or IPC.
**Impact:** Brittle contract if the result dict is cached or passed to another agent.
**Fix:** `result["offense"] = {c: (float(r[c]) if r[c] % 1 else int(r[c])) for c in num if r[c] != 0}`.

### OBSERVATION #2: `player_profile` abilities lookup violates identity resolution rule
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:109-133`
**Confidence:** 0.60
**Risk:** Lines 111 and 131 compare `pname.lower() == full_name.lower()`. For players with punctuation or accents (Ja'Marr Chase, A.J. Brown, José Ramírez), strict equality misses if one source has normalized the string. The CLAUDE.md identity-resolution rule requires `_resolve_owner()` or alias-map fuzzy lookup. Strict equality is used instead.
**Vulnerability:** No fuzzy match, no Unicode normalization.
**Impact:** Player profiles for punctuation-heavy names show "Abilities: (none)" even when abilities exist in the cache.
**Fix:** Strip punctuation before comparison: `re.sub(r"[^\w\s]", "", name).lower()`, or integrate `build_member_db.get_alias_map()`.

### OBSERVATION #3: `route_query` lacks a general try/except guard — downstream crashes bubble to the interaction
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:383-421`
**Confidence:** 0.70
**Risk:** KeyError/TypeError/AttributeError from any downstream helper propagates to the caller. oracle_cog wraps buttons in `_safe_interaction`, but the user sees "ATLAS is down" instead of a structured error.
**Vulnerability:** No logging of the failing query for post-mortem. No structured error type.
**Impact:** Debugging user-reported crashes requires reproducing the exact query. No telemetry.
**Fix:** Wrap `route_query` body in `try/except` and return `{"type": "error", "query": query, "error": str(e)}` with `log.exception(...)`.

### OBSERVATION #4: `team_profile` standings fallback uses unanchored substring match
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:188-190`
**Confidence:** 0.65
**Risk:** Line 190 uses `str.contains(t, na=False)` — substring match. For teams renamed mid-season or with shared substrings, the wrong row is picked. `.iloc[0]` takes the first match regardless of quality.
**Vulnerability:** No scoring, no word-boundary.
**Impact:** Edge case for TSL admin renames. Unlikely under current roster but real.
**Fix:** Already does exact match first; substring fallback should also require word boundaries or at least rank matches by length similarity.

### OBSERVATION #5: Dead-loop pattern — `for df in [dm.df_offense]:`
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:136, 152`
**Confidence:** 0.95
**Risk:** `for df in [dm.df_offense]:` is a one-element loop equivalent to `df = dm.df_offense`. Leftover from a refactor that likely once iterated `[dm.df_offense, dm.df_offense_week]` or similar.
**Vulnerability:** Code smell only; no functional bug. Suggests incomplete refactor.
**Impact:** Reader confusion.
**Fix:** Flatten to `df = dm.df_offense` + an `if df.empty or "fullName" not in df.columns: pass` guard.

### OBSERVATION #6: `_keyword_stats` has dead variable `is_pure_qb` (assigned, never read)
**Location:** `C:/Users/natew/Desktop/discord_bot/analysis.py:432`
**Confidence:** 0.95
**Risk:** `is_pure_qb` is computed but never used in any subsequent branch. Indicates the keyword dispatch is under-tested and maintained ad-hoc. No module-level docstring explaining precedence of the keyword state machine (~40 trigger strings across 6+ branches can double-fire).
**Vulnerability:** Maintainability / regression risk. Any new keyword can silently change behavior.
**Impact:** Subtle UX regression whenever the keyword list is touched.
**Fix:** Remove unused `is_pure_qb`, add a module-level docstring explaining dispatch precedence, or extract the keyword table to a module constant with per-intent comments.

## Cross-cutting Notes

- **Ring 1 data_manager bug visible here:** `dm.df_team_stats is dm.df_standings` is a data_manager wiring bug that analysis.py cannot detect on its own. Any Ring 2 audit of analytics consumers (intelligence.py, oracle_cog.py) will hit the same false-empty results from `stat_leaders` on red-zone / third-down / penalty columns. The fix belongs upstream.
- **Async boundary is actively moving:** `get_last_n_games` is async-only, but `get_team_record`, `get_team_owner`, `get_weekly_results`, `get_h2h_record` are still sync. Any Ring 2 module calling data_manager helpers needs audit for mixed sync/async usage. Recommend a "sync/async boundary" section in CLAUDE.md listing which data_manager functions are which.
- **Column-existence guards are inconsistent:** `stat_leaders` checks `stat_col in df.columns`, `player_profile` checks `"fullName" in df.columns`, but `_keyword_stats` Drop Rate block and `power_rankings` assume columns exist. Pattern should be unified via a `_safe_col(df, col, default)` helper in analysis.py.
- **CLAUDE.md seasonIndex convention isn't documented for trades** — the weekIndex 0-based trap is explicit, but seasonIndex may share the same convention. Worth adding a gotcha row to the MaddenStats API Gotchas table.
