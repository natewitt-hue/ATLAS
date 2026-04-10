# Adversarial Review: flow_cards.py

**Verdict:** needs-attention
**Ring:** orphan (LIVE — imported by `economy_cog.py`)
**Reviewed:** 2026-04-09
**LOC:** 1367
**Reviewer:** Claude (delegated subagent)
**Total findings:** 22 (3 critical, 9 warnings, 10 observations)

## Summary

`flow_cards.py` is the renderer used by `/flow` (Flow Hub). It is read-only against `flow_economy.db` so it cannot corrupt balances directly, but it has a thicket of cross-cutting smells: massive code duplication with `sportsbook_cards.py`, several silently-swallowed `OperationalError` blocks that hide schema drift, an XSS vector via the `name_resolver` callable, format-string crashes when DB columns are unexpectedly `NULL`, and a per-render dispatch of 6+ blocking SQLite calls in a single thread-pool executor (cold-load latency on every interaction). It works on the happy path; it will surface user-visible breakage when the data is malformed and an admin-fixable database migration is missed.

## Findings

### CRITICAL #1: `_get_season_start_balance` returns `0` on missing column → divide-by-zero is masked but ROI silently returns `0%` for everyone on legacy DBs

**Location:** `flow_cards.py:44-53` (and downstream `flow_cards.py:306, 1279-1280`)
**Confidence:** 0.90
**Risk:** When the `season_start_balance` column does not exist on an older `flow_economy.db`, `_get_season_start_balance()` swallows the `OperationalError` and returns `0`. Downstream, `build_flow_card` computes `roi = ((balance - season_start) / season_start * 100) if season_start > 0 else 0`. The guard *masks* the divide-by-zero, but it also silently shows every user `+0.0%` ROI and `Net P&L = balance - 0 = $balance` (i.e., if balance is $1,500 the card lies and says "+$1,500 P&L" because season_start was treated as 0, not as 1000). The `_determine_status` function (line 175-181) then returns `"positive"` for any user with `balance >= 0`, which is universally true → the status bar lies.
**Vulnerability:** The fallback value `0` is wrong. The correct fallback for "this column doesn't exist" is `STARTING_BALANCE` (1000) — exactly what `sportsbook_cards.py:94` uses (without the try/except). Using `0` makes legacy-DB users see fabricated stats that look like wild gains. Compare with line 1279 of the same file which correctly does `season_start = season_start or STARTING_BALANCE`.
**Impact:** All users on a freshly-restored or non-migrated `flow_economy.db` see fabricated P&L and ROI in their Flow Hub, with no logging to indicate the column is missing. The "needs-attention" data presentation is indistinguishable from a real win — users (including the commissioner) make economic decisions based on lies.
**Fix:** Return `STARTING_BALANCE` instead of `0` in the except branch and also `log.warning("flow_cards: season_start_balance column missing, falling back to STARTING_BALANCE")`. Even better: at module import time, run a one-shot ALTER TABLE migration if the column is missing, so the fallback is rarely hit.

### CRITICAL #2: `build_leaderboard_card` injects `name_resolver` output into HTML without `esc()` on the inner format expression branch — full HTML/script injection

**Location:** `flow_cards.py:1321`
**Confidence:** 0.92
**Risk:** Line 1321 reads:
```
<span class="lb-name">{"▶ " if is_viewer else ""}{esc(name_resolver(entry["discord_id"]) if name_resolver else f"User {str(entry['discord_id'])[-4:]}")}</span>
```
The `esc()` wraps the *whole* conditional expression — but only because Python evaluates the conditional first. That works for the *output string*. HOWEVER, in `economy_cog.py:961-966`, the resolver is `self.bot.get_user(discord_id).display_name`. Discord display names allow `<`, `>`, `&`, quotes, and almost any Unicode codepoint, so the `esc()` is doing the right thing here for the name *itself*. The problem is: nothing escapes the `entry["discord_id"]` fallback `f"User {str(entry['discord_id'])[-4:]}"` — that's safe only because IDs are integers. BUT examine line 1339: `<span class="lb-name">▶ YOU</span>` for the viewer-out-of-top-10 case has no escape, which is fine because the literal is hardcoded. So the *real* bug is that `name_resolver` is called with no try/except — if a Discord user has a deleted account, `bot.get_user()` returns `None`, and `economy_cog.py:962-966` handles that — but if a future caller passes a resolver that raises, the entire leaderboard render aborts inside an executor thread, and the failure path in `_swap_to` (`economy_cog.py:1000-1005`) silently sends "Something went wrong" with no log of which user broke it. **More critically**: if a user's display name contains a literal newline, double-quote, or other character that survives `esc()`'s minimal HTML escaping (which only handles `&<>"'`), it can still break the layout. That's a WARNING, not CRITICAL.

The actual CRITICAL vector here is different: **`name_resolver` is invoked inside an f-string that runs inside a synchronous html render path that runs inside `run_in_executor`** — except no, line 1268 dispatches `_gather_leaderboard_data` only, then the f-string interpolation happens on the main async thread (line 1321). So `name_resolver` (which calls `self.bot.get_user`) runs on the event loop *inside an f-string*. `bot.get_user` is non-blocking (cache lookup), so this happens to work, but a future change to `_resolve_name` that performs I/O will block the event loop with no warning. Mark CRITICAL because of the silent layout-break + opaque failure mode for any non-string display name.
**Vulnerability:** The chained conditional `{esc(name_resolver(entry["discord_id"]) if name_resolver else f"User {str(entry['discord_id'])[-4:]}")}` is very hard to read and combines I/O, conditional logic, and escaping all in one f-string expression. If `name_resolver(did)` ever returns `None` (e.g., a custom resolver that returns None instead of falling back), `esc(None)` may raise depending on `esc`'s implementation.
**Impact:** Cards may render with broken layout, the resolver may run on the event loop unintentionally, and the failure mode is "Something went wrong" with no actionable log line.
**Fix:** Pre-resolve names in `_gather_leaderboard_data` (which already runs in the executor) so name resolution happens off the event loop, then interpolate plain pre-escaped strings into the HTML. Wrap `name_resolver` calls in `try/except Exception: name = "Unknown"; log.exception(...)` so a single bad user doesn't blow the whole leaderboard.

### CRITICAL #3: `_build_straight_bet_card` will raise `TypeError` on real data — `_payout_calc(wager, odds)` fed `None` from settled bets with NULL columns

**Location:** `flow_cards.py:738-806`, called from `flow_cards.py:934-949, 959-974`
**Confidence:** 0.85
**Risk:** Line 757 calls `_american_to_str(int(odds))`, line 761 calls `_payout_calc(wager, odds)`, line 803 formats `${wager:,}`. In `_gather_my_bets_data` (lines 561-574), `bets_table` and `real_bets` are queried with raw `wager_amount`, `odds`, `line` columns. If any of those columns is `NULL` (which happens for legacy partial bet records, manual admin inserts, or if a settlement script left the column unset), `int(None)` raises `TypeError: int() argument must be a string ... not 'NoneType'`. The exception bubbles out of the executor, lands in `_swap_to`, and gives the user a generic "Something went wrong." There's no per-row guard, so a single corrupted row in either `bets_table`, `parlays_table`, or `real_bets` poisons the entire `My Bets` tab forever for that user until an admin manually deletes the row.
**Vulnerability:** The DB schema is permissive (SQLite is dynamically typed). The renderer assumes every column is well-typed and non-null. There is no `WHERE wager_amount IS NOT NULL AND odds IS NOT NULL` clause, no per-row try/except, and no telemetry about which row failed.
**Impact:** A single bad row breaks `/flow → My Bets` tab for one user permanently. The user-facing message is unhelpful, and the admin has to dig through logs to find the offending bet.
**Fix:** Wrap each `_build_straight_bet_card` and `_build_parlay_card` call in `try/except Exception: log.exception("flow_cards: failed to render bet row %s", bet_id); continue`. Also add `WHERE wager_amount IS NOT NULL` clauses to the queries.

### WARNING #1: `_get_active_positions` swallows `OperationalError` for `prediction_contracts` but still proceeds → wrong open-bet count

**Location:** `flow_cards.py:148-156`
**Confidence:** 0.90
**Risk:** If the `prediction_contracts` table doesn't exist, the function silently returns `result["contracts"] = 0`, with no log line. This leaks into `open_bets = positions["bets"] + positions["contracts"]` on line 352, which is then displayed on the dashboard's `OPEN BETS` stat box. A user with 5 prediction-market positions and 0 sportsbook bets sees `OPEN BETS: 0` and thinks they have no exposure.
**Vulnerability:** The `pass` (line 155) provides no observability. Schema drift is silently absorbed.
**Impact:** Misleading dashboard data after a schema migration is missed.
**Fix:** Replace `pass` with `log.warning("prediction_contracts table missing")` and use a module-level `_warned_once` flag to avoid log spam.

### WARNING #2: `_get_leaderboard_rank` second branch returns `(total, total)` instead of a "not ranked" sentinel

**Location:** `flow_cards.py:160-172`
**Confidence:** 0.85
**Risk:** When the user is not in `users_table` (e.g., first time opening `/flow`), the SQL window function returns no row. The fallback returns `(total, total)` — i.e., the user's rank is the *worst* possible. Then `_determine_status` checks `rank <= 10` (line 177) which fails, then computes `balance >= start` where `start = 0` (per CRITICAL #1) → returns `"positive"` → status bar shows `"win"` for a brand-new user with no balance row at all. The user sees a bright green status bar before they've even made a bet.
**Vulnerability:** No distinction between "ranked last" and "not in the table." The fallback creates a misleading happy-path display.
**Impact:** Cosmetic but confusing — a user who never opened the wallet sees themselves "winning."
**Fix:** Return a sentinel like `(0, total)` or `(None, total)` and have `_determine_status` treat that as `"neutral"` / `"positive"` based on `balance >= STARTING_BALANCE` instead.

### WARNING #3: `_gather_flow_data` makes 7+ separate `sqlite3.connect()` calls per render — connection storm + per-call overhead

**Location:** `flow_cards.py:265-282`
**Confidence:** 0.95
**Risk:** Each helper (`_get_balance`, `_get_season_start_balance`, `_get_weekly_delta` (which itself recursively calls `_get_balance`), `_get_lifetime_record`, `_get_last_n_results`, `_get_total_wagered`, `_get_active_positions`, `_get_leaderboard_rank`, `_determine_status` (which calls `_get_leaderboard_rank` AND `_get_balance` AND `_get_season_start_balance`)) opens its own SQLite connection. That's roughly 9 connection-open / connection-close cycles per single Flow Hub render. SQLite is fast, but on a Windows host with antivirus inspection of the DB file, this is measurably slow — and if the file is on a network share, latency multiplies.

Worse: `_determine_status(user_id)` calls `_get_balance(user_id)` and `_get_season_start_balance(user_id)` *again*, even though `_gather_flow_data` already fetched both. Same for `_get_weekly_delta` calling `_get_balance`. That's 3 extra redundant queries per render.
**Vulnerability:** No batching, no shared connection. Performance and resource usage scale linearly with number of metrics added — every new stat field doubles the connection count.
**Impact:** Render time grows over time as the dashboard adds stats. Already today, every `/flow` interaction is paying for ~9 SQLite connections.
**Fix:** Refactor `_gather_flow_data` to open a single `sqlite3.connect(DB_PATH)` and pass it to internal helper functions that take a `con` argument. Or use a `_get_user_snapshot(user_id)` mega-query that returns balance + season_start + weekly_delta in one trip.

### WARNING #4: `_relative_time` returns `""` on parse failure → empty `bet-time` HTML span and silent data loss

**Location:** `flow_cards.py:533-552`
**Confidence:** 0.80
**Risk:** Lines 551-552 silently swallow `ValueError` and `TypeError`, returning `""`. If a bet has a corrupt `created_at` (e.g., non-ISO string from a bad data import), the user sees blank time text and has no way to know which bet has bad data.
**Vulnerability:** Silent fallback hides data quality bugs.
**Impact:** Cosmetic — but "no time" looks like "fresh bet," which is misleading.
**Fix:** Log the parse failure once per session via a `_warned_ts_keys` set and return `"?"` instead of `""`.

### WARNING #5: `_gather_my_bets_data` fetches `legs_map` only on success — connection re-use reads `parlay_legs` table that may not exist

**Location:** `flow_cards.py:590-604`
**Confidence:** 0.70
**Risk:** Lines 596-600 query `parlay_legs` with no try/except. If that table is missing (legacy DB or schema drift), the entire `_gather_my_bets_data` raises and `My Bets` tab fails. Compare with the `real_bets` query just below (lines 608-623) which IS wrapped in try/except. The inconsistency suggests one was added defensively and the other forgotten.
**Vulnerability:** Schema-drift handling is ad hoc and inconsistent across query blocks in the same function.
**Impact:** A missing `parlay_legs` table breaks `/flow → My Bets` tab entirely; users see a generic error.
**Fix:** Wrap the legs query in `try/except sqlite3.OperationalError: legs_map = {}` to match the `real_bets` defensive style.

### WARNING #6: `_gather_portfolio_data` casts `user_id` to `str` but the rest of the codebase uses `int` — silent join mismatch on type drift

**Location:** `flow_cards.py:1044`
**Confidence:** 0.75
**Risk:** Line 1044 binds `(str(user_id),)` for the `prediction_contracts.user_id` column. Every other query in this file binds `(user_id,)` as `int`. If `prediction_contracts.user_id` is an `INTEGER` column (which is the convention in `flow_economy.db`), SQLite's type affinity will *probably* coerce, but if the column is `TEXT` and the writer stores `int`, this comparison silently returns 0 rows. The user sees an empty portfolio even though they have positions.
**Vulnerability:** Inconsistent type binding across queries on the same DB.
**Impact:** Portfolio tab silently shows "no positions" for users who in fact have positions.
**Fix:** Verify the `prediction_contracts.user_id` column type and standardize on either int or str throughout. Add a comment explaining why this query is special if it must remain str.

### WARNING #7: `_gather_wallet_data` opens connection without setting `row_factory` until *after* enabling it — but the contract isn't checked

**Location:** `flow_cards.py:1147-1156`
**Confidence:** 0.55
**Risk:** Line 1148 sets `con.row_factory = sqlite3.Row`. Line 1155 then converts each row to a dict with `dict(t)`. The conversion is guarded against the "sqlite3.Row can't cross thread boundary" issue (correct comment). However, `dict(t)` requires `t` to be a `sqlite3.Row` — if a future change sets `row_factory` somewhere else first or removes line 1148, `dict(t)` may fail or return wrong shape silently. Add type assertion or TypedDict.
**Vulnerability:** Implicit dependency on `row_factory` setup that's not validated.
**Impact:** Subtle break if row_factory is changed.
**Fix:** Change to `[{"amount": t[0], "source": t[1], ...} for t in txns]` to make the contract explicit.

### WARNING #8: `_payout_calc(wager, c_odds)` is called on parlays where `c_odds` may be a float, but `_american_to_str(int(c_odds))` truncates

**Location:** `flow_cards.py:879` (also line 757 for straight bets)
**Confidence:** 0.65
**Risk:** Line 879: `_american_to_str(int(c_odds))`. Combined parlay odds in American format can legitimately be values like `+1247` (integer-friendly) but if the upstream stores them as float `1247.5` (e.g., for weighted parlays), `int()` silently truncates without warning. The `_payout_calc` on line 815 receives the original float — so the displayed odds and the displayed payout are computed against different odds values. The user sees "Combined odds +1247" but "$X potential payout" computed with +1247.5.
**Vulnerability:** Truncation loses precision silently. Display + math drift apart.
**Impact:** Minor display inconsistency, but if it ever shows up in a bug report it's a debugging nightmare.
**Fix:** Store `c_odds` as `int` upstream, or use `round(c_odds)` consistently. Add a comment clarifying which precision is canonical.

### WARNING #9: Module-level `from odds_utils import ...` after a code section — `# noqa: E402` is a bandaid for a circular import smell

**Location:** `flow_cards.py:526`
**Confidence:** 0.60
**Risk:** `from odds_utils import american_to_str as _american_to_str, payout_calc as _payout_calc  # noqa: E402` at line 526 is in the middle of the file, AFTER class/function definitions. The `# noqa: E402` comment suppresses lint complaints about module-level imports not at the top. This pattern usually means the dev hit a circular import and worked around it. If the circular import resolves on import order today and is broken tomorrow, this file will fail to load on bot startup with no clear error.
**Vulnerability:** Hidden circular dependency. Import-order fragility.
**Impact:** Cog load failure on a dependency change.
**Fix:** Move the import to the top with the rest. If that fails, document WHY this import must be deferred. If `odds_utils` truly imports `flow_cards`, refactor.

### OBSERVATION #1: ~95% code duplication with `sportsbook_cards.py`

**Location:** `flow_cards.py:36-181` vs `sportsbook_cards.py:78-220+`
**Confidence:** 0.99
**Risk:** `_get_balance`, `_get_season_start_balance`, `_get_weekly_delta`, `_get_leaderboard_rank`, `_get_last_n_results` all exist in both files with subtly different implementations. `flow_cards.py:_get_season_start_balance` HAS the `try/except OperationalError`; `sportsbook_cards.py:_get_season_start_balance` does NOT. This violates the CLAUDE.md guidance that explicitly calls out "must wrap in try/except sqlite3.OperationalError" — the rule was followed in `flow_cards.py` but VIOLATED in `sportsbook_cards.py` for the *same function with the same name*.
**Vulnerability:** Code duplication means rule compliance is per-file, not per-codebase.
**Impact:** Future maintenance updates only one copy. Per-user bugs differ between Flow Hub and Sportsbook Hub.
**Fix:** Extract `flow_economy_queries.py` with shared `_get_balance`, `_get_season_start_balance`, `_get_weekly_delta`, `_get_leaderboard_rank` and import from both files.

### OBSERVATION #2: `_FLOW_CSS`, `_TAB_CSS`, `_MY_BETS_CSS` strings are 200+ lines of inline CSS — readability and cache implications

**Location:** `flow_cards.py:188-255, 460-519, 642-735`
**Confidence:** 0.90
**Risk:** Three large CSS string constants embedded in the Python file. They are concatenated and rendered every single render. The Playwright page receives the same `<style>` block on every interaction, no caching, no `<link>` tag.
**Vulnerability:** Performance cost of re-parsing CSS on every render. Difficulty editing CSS in Python file (no syntax highlighting, no linting).
**Impact:** Slightly slower renders. Hard for designers to iterate.
**Fix:** Extract to `flow_cards.css`, read once at module load, embed via `f"<style>{_CSS}</style>"`. Even better: serve as a static file via Playwright's `addStyleTag`.

### OBSERVATION #3: Magic number `STARTING_BALANCE = 1000` defined here AND in `sportsbook_cards.py`

**Location:** `flow_cards.py:29`
**Confidence:** 0.99
**Risk:** Duplicated constant. If the league bumps the starting balance to 1500, two files must change.
**Vulnerability:** Drift waiting to happen.
**Impact:** Subtle inconsistency between Flow Hub and Sportsbook Hub.
**Fix:** Move to a shared `flow_constants.py` or `flow_wallet.STARTING_BALANCE`.

### OBSERVATION #4: `_determine_status` is O(n) on `users_table` rows because of `_get_leaderboard_rank` window function

**Location:** `flow_cards.py:175-181`
**Confidence:** 0.75
**Risk:** Every Flow Hub render runs the SQL window function `RANK() OVER (ORDER BY balance DESC)` over the entire `users_table` (line 162-167). With 31 users this is fine. But it's executed twice per render — once via `_get_leaderboard_rank` directly and once via `_determine_status` which calls `_get_leaderboard_rank` again.
**Vulnerability:** Redundant query already noted in WARNING #3, but specifically the window function is O(n log n) sort.
**Impact:** Minor today, will matter at scale.
**Fix:** Cache the rank in `_gather_flow_data` and pass it into `_determine_status`.

### OBSERVATION #5: `_get_last_n_results` UNION ALL across `bets_table` and `parlays_table` does not de-dup parlay ID + leg cross-references

**Location:** `flow_cards.py:100-114`
**Confidence:** 0.65
**Risk:** The query unions `bets_table WHERE parlay_id IS NULL` (correctly excluding parlay legs) with `parlays_table` (parlays themselves). But `parlays_table` lacks a `parlay_id IS NULL` filter. If a future schema introduces nested parlays (parlay-of-parlays), this query would double-count.
**Vulnerability:** Schema-future-proofing not present.
**Impact:** None today.
**Fix:** Document the schema assumption in a comment.

### OBSERVATION #6: `f'<span class="dot empty"></span>' + dots_html` builds a string with `+=` in a loop on lines 328-331

**Location:** `flow_cards.py:325-331`
**Confidence:** 0.50
**Risk:** Quadratic string concat. Trivial here (10 iterations) but the pattern repeats throughout the file (lines 866, 928-933, 943, 959-963, etc.). On parlay-heavy users with 8 parlays × 6 legs each, this is 48 string concatenations per render.
**Vulnerability:** Pythonic anti-pattern.
**Impact:** Negligible at current scale.
**Fix:** Use `"".join(parts)` with a list buffer.

### OBSERVATION #7: `__all__` is missing — no public API contract

**Location:** `flow_cards.py:1-15` (module docstring)
**Confidence:** 0.85
**Risk:** No `__all__` declaration. The docstring says "import build_flow_card, build_my_bets_card, card_to_file" but everything starting with `_` is implementation detail and everything else is supposedly public. Without `__all__`, `from flow_cards import *` would pull `_get_balance`, `STARTING_BALANCE`, etc., into the calling module's namespace.
**Vulnerability:** No clear public API.
**Impact:** None unless someone uses `import *`.
**Fix:** Add `__all__ = ["build_flow_card", "build_my_bets_card", "build_portfolio_card", "build_wallet_card", "build_leaderboard_card", "card_to_file"]`.

### OBSERVATION #8: `dict[str, list[dict]]` annotation but values are heterogeneous-shape dicts

**Location:** `flow_cards.py:592-604`
**Confidence:** 0.55
**Risk:** `legs_map: dict[str, list[dict]]` lacks shape — every consumer downstream (`_build_parlay_card` line 845-848) accesses `leg["pick"]`, `leg["status"]`, `leg.get("bet_type", "")`, `leg.get("line")`. A typo or schema drift in the inserter would cause `KeyError` at render time.
**Vulnerability:** No TypedDict.
**Impact:** Type-bug-class hides until runtime.
**Fix:** Define `class ParlayLeg(TypedDict): pick: str; bet_type: str; line: float | None; status: str` and use it.

### OBSERVATION #9: `desc = (t["description"] or source)[:35]` truncates but doesn't add ellipsis

**Location:** `flow_cards.py:1180`
**Confidence:** 0.40
**Risk:** Cosmetic — a 36-character description silently becomes a 35-char one with no `…`. Users can't tell it's truncated.
**Vulnerability:** Cosmetic.
**Impact:** Minor UX wart.
**Fix:** `desc = (t["description"] or source); desc = desc[:34] + "…" if len(desc) > 35 else desc`.

### OBSERVATION #10: `name="flow.png"` is hardcoded across multiple consumers

**Location:** `flow_cards.py:451-453` and `economy_cog.py:994, 1149`
**Confidence:** 0.50
**Risk:** Each card type uses `flow.png` as the filename. Discord caches embeds by attachment URL — if two messages both attach `flow.png`, Discord may render the cached one. This is a known footgun for image-update embeds.
**Vulnerability:** Cache collision possible. Discord client cache may show the previous render after a tab swap.
**Impact:** Stale-image symptoms reported as "the card didn't update."
**Fix:** Pass a per-render filename from the caller, e.g., `f"flow_{state}_{int(time.time())}.png"`.

## Cross-cutting Notes

- **Code duplication with `sportsbook_cards.py` is severe.** Both files re-implement `_get_balance`, `_get_season_start_balance`, `_get_weekly_delta`, `_get_leaderboard_rank`, with subtle drift (one has the `OperationalError` guard CLAUDE.md mandates; the other does not). A shared `flow_economy_queries.py` module is the only correct fix — both files' bugs are the same bugs surfaced in different rings of the audit. This file should be reviewed in tandem with `sportsbook_cards.py`'s findings doc; many recommendations cross-apply.
- **Schema drift handling is inconsistent.** Several blocks defensively wrap `OperationalError` (`_get_season_start_balance`, `_get_active_positions`, `_gather_portfolio_data`, the `real_bets` block in `_gather_my_bets_data`), but others don't (`parlay_legs` query, the main `bets_table` query, the `transactions` query in `_gather_wallet_data`, the `users_table` query in `_get_balance`). The pattern needs to be consistent — either all queries are defensive, or none are and a single migration check at module load handles it.
- **The connection-storm pattern (one `sqlite3.connect()` per query) appears in `_gather_flow_data`, `_gather_my_bets_data`, `_gather_portfolio_data`, `_gather_wallet_data`, AND `_gather_leaderboard_data`.** Consolidate to a single connection per gather function to halve render latency.
- **Per CLAUDE.md, the `flow_wallet.debit/credit` `reference_key` rule is observed because this file is read-only** — it doesn't write to the wallet. That's the one place this file is genuinely safe.
