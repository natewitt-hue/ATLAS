# Adversarial Review: atlas_home_renderer.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 452
**Reviewer:** Claude (delegated subagent)
**Total findings:** 22 (2 critical, 9 warnings, 11 observations)

## Summary

This renderer mostly follows the ATLAS HTML engine pipeline correctly — the hot-path HTML outputs go through `esc()`, the caller defers before gather/render, and the blocking DB reads run in an executor. However, it leaks two real correctness bugs (theme palette colors spliced raw into `style=""` → XSS if themes ever become user-editable, and a Prediction P&L calculation that double-counts unresolved contracts) and a blanket silent-exception pattern that swallows every conceivable DB failure without logging. The result: `/atlas` will render a card filled with `0` / `—` placeholders and never tell anyone the DB call failed.

## Findings

### CRITICAL #1: Theme palette colors spliced raw into inline `style=""` without escaping

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:342-353`
**Confidence:** 0.90
**Risk:** Each swatch value is pulled from `theme.get("vars", {})` and interpolated directly into an HTML attribute: `f'...background:{color};...'`. There is no `esc()`, no CSS sanitization, no attribute quoting safety. If a theme's `bg`/`gold`/`gold-bright`/`win`/`loss`/`text-primary` value contains a `"` character (or more perniciously a `;`, `}`, or JavaScript-expression payload), the attribute breaks out and arbitrary HTML/CSS is injected into the rendered page.
**Vulnerability:** The ATLAS focus block from Ring 1's `atlas_html_engine` audit explicitly flagged: "theme strings spliced raw into HTML (RCE if themes user-uploadable)." This file reproduces the same antipattern: `atlas_html_engine.wrap_card()` at L572/577 already splices `status_gradient`/`card_border` raw; `atlas_home_renderer._build_theme_preview_html` adds six more unescaped splice points (lines 346-349), plus `hero_class` (line 390, escaped but then used as CSS class which widens the attack surface if CSS inheritance is used), plus the theme `label` text (line 391, that IS escaped — fine). Currently themes live in-repo as Python dicts, so exploit requires code-push. But the moment anyone builds a `/theme create` or a theme import feature — both plausible follow-ups for a system literally named "theme selector" — this is a stored-XSS-via-Playwright-DOM primitive. Playwright pages have file:// access in some configs; injected `<script>` could read local files via `file://` fetch. Also impacts all theme previews immediately, so a malicious theme in a PR would render for every user cycling themes.
**Impact:** Code execution inside the Playwright render context (chromium with `headless=True`); exfiltration of any file the bot process can read (flow_economy.db, .env with DISCORD_TOKEN/ANTHROPIC_API_KEY/GEMINI_API_KEY). Defense-in-depth violation even if themes stay curated.
**Fix:** Validate theme color strings against a strict regex before splicing — accept only `#[0-9a-fA-F]{3,8}`, `rgb(...)`, `rgba(...)`, `hsl(...)`, `hsla(...)`, and `var(--*)` patterns. Reject anything else at theme load time (in `atlas_themes.py`). Additionally, use CSS variables exclusively in rendered output and put all theme values into the `:root {}` block that `wrap_card()` already builds — never splice them into `style=""` in the body. This file's swatch rendering should output `<div class="swatch swatch-bg"></div>` with the color set via a pre-built CSS rule, not `style="background:{color}"`.

### CRITICAL #2: Blanket `except Exception: pass` swallows all DB failures with zero logging

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:51-214`
**Confidence:** 0.95
**Risk:** Nine separate `try / except Exception: pass` blocks plus the outer one at line 213 catch every conceivable failure mode — `sqlite3.OperationalError` (missing column, locked DB, corrupt file), `sqlite3.DatabaseError`, `ValueError` from casts, `ZeroDivisionError` (see WARNING #2), `KeyError`, even `KeyboardInterrupt` if raised in a weird place (until Python 3.14 fixes that). None of them log. None of them increment a metric. The user sees a card full of zeros/dashes and has no way to know the query failed.
**Vulnerability:** This is a direct violation of the CLAUDE.md rule: "Silent `except Exception: pass` in admin-facing views is prohibited. Always `log.exception(...)`." While `/atlas` is user-facing not admin-facing, the same reasoning applies — a silent-failing query in a renderer is strictly worse than a visible error because it looks correct. In particular, the outer `except Exception: pass` at line 213 means if `users_table` doesn't exist or has a schema mismatch, the ENTIRE function returns the default dict with all zeros, and there is no way to tell this apart from a user who actually has zero activity. Also note the specific CLAUDE.md gotcha: `season_start_balance` "Must wrap in try/except `sqlite3.OperationalError`. Column may not exist on older DBs." — line 56 doesn't catch that narrowly; instead it relies on the outer blanket handler, which means a missing column in `users_table` nukes every other section of the card, not just the ROI calculation.
**Impact:** On a real DB schema regression or permission issue, every user's `/atlas` card silently looks empty. Ops has zero observability. Silent data loss from the user's perspective.
**Fix:** (1) Replace each `except Exception: pass` with `except sqlite3.OperationalError as e: log.warning("section <name> failed: %s", e)` or `log.exception(...)`. (2) Narrow the `season_start_balance` read to catch only `sqlite3.OperationalError` as mandated by CLAUDE.md, and put each independent query inside its own scope so one failure doesn't zero out every other section. (3) Introduce a module-level `log = logging.getLogger("atlas.home")`. (4) Outer handler at line 213 should log at ERROR and re-raise or set a `data["_error"] = True` flag so the caller can render a fallback badge.

### WARNING #1: Prediction P&L double-counts unresolved contracts

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:158-160`
**Confidence:** 0.92
**Risk:** `total_cost = sum(c or 0 for _, c, _ in pred_rows)` sums cost across ALL contracts (including pending/unresolved). `total_payout = sum(p or 0 for s, _, p in pred_rows if s == "resolved")` sums payout ONLY for resolved rows. `pred_pnl = total_payout - total_cost` then subtracts ALL costs from RESOLVED-ONLY payouts. Any user with open pending contracts will see their PnL understated by the cost of every pending bet — even if every single resolved bet has been a winner.
**Vulnerability:** Classic asymmetric filtering bug. The prediction markets subsystem (per CLAUDE.md module map) creates `prediction_contracts` rows with status progression `pending` → `resolved`. Active users will routinely have multiple open contracts at any time, so this misreport is not a rare edge case — it's the default state for any engaged predictor.
**Impact:** Every engaged prediction market user sees a falsely negative PnL. Damage to trust in the stats card. Users will ask "why does my card say -$8,000 when I've never lost a market"; commissioner can't easily reproduce because it depends on how many unresolved contracts they happen to hold.
**Fix:** Change to `total_cost = sum(c or 0 for s, c, _ in pred_rows if s == "resolved")`. Alternatively, track realized vs unrealized separately and display both.

### WARNING #2: ZeroDivisionError on american == 0 parlay odds — caught and silenced

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:195-199`
**Confidence:** 0.85
**Risk:** `american = int(parlay_row[0])`. The `if american > 0` branch is safe, but the `else` branch does `100 / abs(american) + 1` — if `american == 0`, `abs(0) == 0` and this raises `ZeroDivisionError`. American odds 0 is nonsensical in real sportsbooks but the DB can easily contain it from a migration or a malformed insert. The outer `except Exception: pass` swallows the error and keeps `best_parlay_odds = 0.0`, which then renders as `"—"` at line 297 (due to the truthiness guard). The defect is masked entirely.
**Vulnerability:** There's no data validation on `combined_odds` at write time (per the focus block hint that `flow_economy.db` has float/int confusion). A zero value propagates through silently. Worse, any error in the outer try wipes the parlay section AND all subsequent sections if the exception lands differently.
**Impact:** Best parlay badge is always "—" for any user who has a zero-odds row in their parlay history. Silent.
**Fix:** Narrow to `if american > 0:` ... `elif american < 0:` ... `else: pass` (treat 0 as invalid and skip). Log at warning level when encountered.

### WARNING #3: Rank lookup is O(n) Python-side instead of O(log n) SQL

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:69-76`
**Confidence:** 0.85
**Risk:** `SELECT discord_id FROM users_table ORDER BY balance DESC` pulls every row into Python, then iterates to find the user. For a league with 31 active teams and ~88+ registered Discord members (per CLAUDE.md identity section), this is small today. But `flow_economy.db` also has all casino players, who could number in the thousands across Discord community members who aren't teams. With no index on `balance`, ORDER BY is a full scan + sort every call. Executed inside the DB lock held during the entire `gather_home_data` call.
**Vulnerability:** Unbounded query on user-triggered hot path. Runs once per `/atlas` invocation. The `_DB_TIMEOUT = 10` seconds at line 19 applies to the CONNECT only, not individual queries — once inside, a slow sort blocks the executor thread.
**Impact:** Performance degradation proportional to total user count. `/atlas` gets slower for every user as community grows. At current scale, not an issue; at 10k rows becomes noticeable.
**Fix:** Replace with `SELECT COUNT(*) FROM users_table WHERE balance > (SELECT balance FROM users_table WHERE discord_id = ?)` plus a separate `SELECT COUNT(*) FROM users_table` for total. Add index `CREATE INDEX IF NOT EXISTS idx_users_balance ON users_table(balance DESC)`. Both queries return in constant time with a 2-column secondary index.

### WARNING #4: Fav game renders literal string "None" when game_type is NULL

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:138-139`
**Confidence:** 0.90
**Risk:** `str(fav[0]).capitalize()` — if `fav[0]` is `None` (which happens if `game_type` column permits NULL and a session row has it unset), `str(None) == "None"` and `.capitalize() == "None"`. The card displays a literal "None" string as the favorite game.
**Vulnerability:** The `GROUP BY game_type` will produce a NULL group if any row has NULL game_type. The `ORDER BY cnt DESC LIMIT 1` might pick that NULL row if it has the highest count.
**Impact:** User sees "None" in the casino favorite game slot, which looks like a placeholder but is actually a real render of a real NULL DB value.
**Fix:** `WHERE game_type IS NOT NULL` in the SQL, or `if fav and fav[0]: data["fav_game"] = str(fav[0]).capitalize()`.

### WARNING #5: Real sportsbook pushes silently dropped, breaking Win Rate symmetry

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:104-113`
**Confidence:** 0.80
**Risk:** The TSL bets loop (lines 92-101) filters `status IN ('Won','Lost','Push')` and increments `record_p` for push. The real_bets loop (lines 104-113) filters `status IN ('Won','Lost')` — push is not even queried. Later, line 208 computes `total = total_w + total_l + data["record_p"]` and line 210 `win_rate = total_w / total * 100`. This is asymmetric: TSL pushes dilute the win_rate denominator, but real sports pushes do not.
**Vulnerability:** Two different contracts in the same function with no comment explaining the divergence. Either real_bets can't push (possible — some sportsbooks settle pushes as full refunds) or this is an oversight.
**Impact:** Users with real-sports pushes see a slightly inflated win rate compared to what the same formula would produce for TSL. Minor but user-visible stat discrepancy.
**Fix:** Either intentionally document the difference with a comment, or add `'Push'` to the real_bets filter and track `real_bet_p` separately, summing to `record_p` at line 208.

### WARNING #6: Status string match is case-sensitive and fragile

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:93, 97-99, 106, 109-111, 153, 168, 189`
**Confidence:** 0.80
**Risk:** Every query matches status via string literals: `'Won'`, `'Lost'`, `'Push'`, `'resolved'`. There's no normalization. If the producing code ever writes `'won'` (lower), `'WON'` (upper), `'win'`, or `'W'`, this entire file silently produces zeros for that user.
**Vulnerability:** Cross-module contract with `bets_table`, `real_bets`, `prediction_contracts`, `parlays_table` schemas — all implicitly coupled to a specific capitalization. No test forces consistency.
**Impact:** A schema migration or a new code path writing lowercase statuses would zero out everyone's record card without error.
**Fix:** `LOWER(status) IN ('won','lost','push')` in SQL and normalize in Python: `s_norm = s.lower() if s else ""; if s_norm == "won": ...`. Or add a constant map `_STATUS_WIN = {"Won", "won", "WIN", "W"}` and check membership.

### WARNING #7: Non-user users get rank 0, display "#0 of N" without distinction

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:33, 73-76, 271-272`
**Confidence:** 0.75
**Risk:** `data["rank"]` defaults to 0. If the user is not in `users_table` (first-time `/atlas` before any wallet activity), the for-loop at 73-76 finds nothing and `rank` stays 0. The card then renders `"#0"` in the rank slot and `"of {total_users}"` — which looks like an ordinal but is actually a sentinel.
**Vulnerability:** Silent distinction between "rank 0" (impossible) and "not ranked yet" (the real state). Users will file bug reports about "why am I rank 0".
**Impact:** Confusing UX on first use.
**Fix:** Check if `data["rank"] == 0` in `_build_home_html` and render `"—"` or `"Unranked"` instead. Alternatively, compute rank as NULL-able and treat that as unranked.

### WARNING #8: `render_home_card` assumes `data["weekly_delta"] >= 0` is a number

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:250-253, 275-282`
**Confidence:** 0.70
**Risk:** `_build_home_html` accesses `data["weekly_delta"]`, `data["season_roi"]`, `data["rank"]`, `data["total_users"]`, `data["balance"]`, `data["net_pnl"]`, `data["record_w/l/p"]`, `data["win_rate"]`, `data["tsl_bet_w/l"]`, `data["best_parlay_odds"]`, `data["real_bet_w/l"]`, `data["casino_sessions"]`, `data["biggest_win"]`, `data["pred_accuracy/markets/pnl"]`, and `data["season"]`. These values are splattered into format strings (`{val:,}`, `{val:+,}`, `{val}%`) without any sanitization. They're also NOT passed through `esc()`. The only thing saving this is that `gather_home_data` casts everything to int/float. But because `atlas_home_cog.py` overlays `data["display_name"]`, `data["role_badge"]`, `data["theme_name"]`, and `data["season"]` on top of the result, and any caller could add/override any key, if some future caller sets `data["balance"] = "bogus"` or the cast at line 60 fails to catch a Decimal or Row object, the string is interpolated raw into HTML. No XSS defense here.
**Vulnerability:** Defense in depth. This file trusts `gather_home_data`'s type discipline, but there's no runtime assertion or schema check. `data["season"]` comes directly from `dm.CURRENT_SEASON` with no type guarantee — if `data_manager` ever returns a string (from e.g. a bad API parse), line 315 splices `Season {data["season"]}` unescaped.
**Impact:** Latent XSS / HTML injection via format-string-vs-esc mismatch.
**Fix:** Either (a) pass all numeric values through `int(val)` / `float(val)` casts at the top of `_build_home_html` (defensive re-cast) or (b) pass everything through `esc()` for belt-and-suspenders. Option (b) is zero-cost — `esc(str(int))` is `str(int)`.

### WARNING #9: `emoji` spliced raw into HTML without `esc()`

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:329, 389`
**Confidence:** 0.75
**Risk:** `emoji = theme.get("emoji", "")` at line 329, then `{emoji}` at line 389 — spliced directly into a `<div>` body without `esc()`. The `label` on line 391 IS escaped. Inconsistent. If an emoji value ever contains `<`, `&`, or quotes (technically a few Unicode graphical sequences do when compared with ZWJ joiners, or someone sets `"emoji": "<img src=x>"` in a future theme), it's raw HTML.
**Vulnerability:** Related to CRITICAL #1 — theme dict values all spliced without discipline. Currently safe because emoji values in the theme file are genuine emoji, but one typo and it becomes an XSS vector.
**Impact:** Minor today; amplifies the CRITICAL #1 attack surface if themes become user-editable.
**Fix:** Use `esc(emoji)` consistently with `esc(label)`.

### OBSERVATION #1: Unused `from typing import Optional` import

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:13`
**Confidence:** 0.98
**Risk:** `Optional` is imported but never referenced — the file uses PEP 604 `str | None` at lines 438 and 445 instead.
**Vulnerability:** Dead import, noise.
**Impact:** Linter warning; none functionally.
**Fix:** Delete the import.

### OBSERVATION #2: Dead conditional `if resolved else 0.0`

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:157`
**Confidence:** 0.95
**Risk:** `data["pred_accuracy"] = round(wins / len(resolved) * 100, 1) if resolved else 0.0` — but this entire statement is inside `if resolved:` on line 154, so the else branch is unreachable dead code.
**Vulnerability:** Dead branch. Suggests incomplete refactor.
**Impact:** None functionally; code smell.
**Fix:** Drop the `if resolved else 0.0` ternary — just `round(wins / len(resolved) * 100, 1)`.

### OBSERVATION #3: Module has no logger and no module docstring version

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:1-19`
**Confidence:** 0.90
**Risk:** No `logging.getLogger(...)` and no `log.exception()` calls anywhere in the file. Closely related to CRITICAL #2 — there is literally no infrastructure for logging errors because no logger was imported.
**Vulnerability:** Observability gap by omission.
**Impact:** Ties in with CRITICAL #2 — even fixing the bare `except: pass` requires adding a logger import.
**Fix:** `import logging` and `log = logging.getLogger("atlas.home.renderer")` at the top of the file.

### OBSERVATION #4: `int()` cast inside `MAX()` could truncate a float

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:124-130`
**Confidence:** 0.65
**Risk:** `MAX(payout - wager)` returns whatever type the underlying columns are stored as. If `payout` and `wager` are stored as REAL (which the focus block hints at: "Float vs int balance corruption in flow_economy.db"), the result is a float. `int(big_win[0])` silently truncates toward zero. Biggest win of 9500.75 displays as 9500.
**Vulnerability:** The focus block specifically calls out float/int confusion in `flow_economy.db`. This renderer doesn't participate in the write path, but it does consume and display values cast to int without any unit handling.
**Impact:** Minor display rounding; no data corruption.
**Fix:** `int(round(big_win[0]))` to match banker's display convention, or accept floats and format with `{:,.0f}`.

### OBSERVATION #5: Sync function relies solely on docstring to enforce executor use

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:22-26`
**Confidence:** 0.85
**Risk:** `gather_home_data` is a sync function that opens a sqlite3 connection and runs 8+ queries. The only guard against accidental direct-call from an async context is the docstring: "Runs synchronously — call via run_in_executor." If a future caller forgets, it blocks the event loop for up to 70+ seconds in the pathological case.
**Vulnerability:** Convention-over-enforcement. The focus block specifically flags "Blocking calls inside async functions (sqlite3, requests, time.sleep). All blocking I/O must go through asyncio.to_thread()."
**Impact:** Latent event loop blocker if a maintainer misreads the signature.
**Fix:** Wrap in an async wrapper that auto-schedules to the executor: `async def gather_home_data_async(user_id): return await asyncio.to_thread(_gather_home_data_sync, user_id)`. Make the sync version private (`_gather_home_data_sync`).

### OBSERVATION #6: `discord` and `io` imports inside function bodies

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:430-431, 447-448`
**Confidence:** 0.85
**Risk:** `import io` and `import discord` at the top of `render_theme_preview_to_file` and `render_home_card_to_file`. These are re-executed on every call, which is only a dict lookup (Python caches modules), but still a smell. If there's a circular import concern, there should be a comment; otherwise hoist to module level.
**Vulnerability:** Possibly intentional to avoid early-import side effects, but undocumented.
**Impact:** None functionally; style inconsistency with the rest of the file (which imports `atlas_html_engine` at top level).
**Fix:** Move to module-level imports OR add a comment explaining the deferred import rationale.

### OBSERVATION #7: Redundant truthiness check on `delta_row`

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:85`
**Confidence:** 0.90
**Risk:** `if delta_row:` — `fetchone()` on a query that uses `SUM()` always returns a single row (even if zero matches) because SUM over an empty set returns NULL, and `(None,)` is still a non-None tuple. The `COALESCE(SUM(amount), 0)` further ensures the value is 0 not None. So `delta_row` is always truthy. The `if` is dead.
**Vulnerability:** None; just redundant.
**Impact:** None.
**Fix:** Remove the check: `data["weekly_delta"] = int(delta_row[0] or 0)` directly.

### OBSERVATION #8: Nondeterministic streak when `created_at` has ties

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:166-181`
**Confidence:** 0.65
**Risk:** `ORDER BY created_at DESC LIMIT 20` — if multiple bets share the same `created_at` timestamp (batch insert, migration), ordering between them is SQLite-implementation-defined. A W4 streak could flip to L2 on the next page load without any user action.
**Vulnerability:** Tie-breaker missing. Normally you'd add `ORDER BY created_at DESC, id DESC` for stability.
**Impact:** Very rare; visible only when multiple bets resolve in the same millisecond.
**Fix:** `ORDER BY created_at DESC, rowid DESC`.

### OBSERVATION #9: `f"+{x}"` sign-building pattern instead of `{x:+}`

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:250-253, 277, 282`
**Confidence:** 0.80
**Risk:** Lines 250-253 build `balance_sign = "+" if x >= 0 else ""` then splice `{balance_sign}{x:,}`. Python format spec `{x:+,}` does this in one step (and correctly handles negatives). The current approach displays `"+0 wk"` for a zero delta in the win color — arguably wrong since 0 is neither positive nor negative, and the `>= 0` check colors it green.
**Vulnerability:** Minor display bug for zero values.
**Impact:** A user with exactly 0 weekly delta sees "+0 wk" in green.
**Fix:** Use `> 0` for the sign check, and use `{x:+,}` format spec. Or better, treat zero as a neutral color (`--text-primary` instead of `--win`/`--loss`).

### OBSERVATION #10: `row[0]`/`row[1]` accessed without bounds safety after schema drift

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:55-66`
**Confidence:** 0.55
**Risk:** `SELECT balance, season_start_balance FROM users_table` — if a DB migration adds a column in-between, the SELECT still binds to 2 columns by name, so this is safe. But the narrow fix for the `season_start_balance` missing-column case (CLAUDE.md gotcha) would change the query — at which point `row[1]` would have to shift. There's no column-by-name access, so future refactors are fragile.
**Vulnerability:** Positional row access vs named-column access.
**Impact:** None today; friction for future changes.
**Fix:** Use `row_factory = sqlite3.Row` on the connection, then `row["balance"]` / `row["season_start_balance"]`.

### OBSERVATION #11: No concurrency guard — two `/atlas` calls run two full DB scans

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_renderer.py:52, 69-76`
**Confidence:** 0.60
**Risk:** Each `/atlas` call opens a fresh connection and runs the full rank scan. With `N` concurrent calls, you get `N` simultaneous full-table sorts holding the SQLite file lock in sequence. SQLite serializes writers; readers don't block, but the default `PRAGMA journal_mode` behavior could still cause contention.
**Vulnerability:** No caching. No single-flight. The rank lookup in particular is pure read-only and could be memoized for 60 seconds.
**Impact:** Multiple simultaneous `/atlas` calls cause DB contention at scale.
**Fix:** Add a small LRU cache on the rank query (e.g. `@functools.lru_cache` with a TTL wrapper) keyed by `user_id`, with a 60-second expiration.

## Cross-cutting Notes

Three patterns in this file will almost certainly appear across the rest of the Ring 2 rendering subsystem and are worth hunting:

1. **Raw theme-value splicing into HTML/CSS** — already flagged in Ring 1's `atlas_html_engine` audit. This file adds at least seven more splice points (`_build_theme_preview_html`). Every file that pulls from `atlas_themes.THEMES[...]["vars"]` or `["emoji"]` or `["status_gradient"]` and interpolates into a string is vulnerable the day themes become user-editable. Mitigation should be centralized in `atlas_themes.py` (validate at load time) and `atlas_html_engine.wrap_card()` (sanitize before splice), not per-renderer.

2. **Bare `except Exception: pass` in database-access helpers** — the `gather_home_data` pattern of "try every query in its own bare handler to degrade gracefully" is probably copied to `flow_cards.py`, `sportsbook_cards.py`, and similar stat gathering modules. The CLAUDE.md rule explicitly mandates `log.exception(...)` instead — worth grep'ing for `except Exception:\s*\n\s*pass` across all card builders.

3. **Numeric fields skip `esc()` while text fields use it** — the mental model is "int is safe." But the moment a maintainer adds a new string field to the gather step and forgets to esc it in the build step, there's an XSS hole. A unit test that runs `_build_home_html(data_with_xss_payloads)` and asserts no `<script>` survives would catch this in CI.
