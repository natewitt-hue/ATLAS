# Adversarial Review: sportsbook_cards.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 1483
**Reviewer:** Claude (delegated subagent)
**Total findings:** 28 (4 critical, 11 warnings, 13 observations)

## Summary

This renderer ships a documented CLAUDE.md rule violation (`_get_season_start_balance` has no `OperationalError` guard), plus an HTML injection surface in the real-match card where remote logo URLs and team colors are interpolated directly into `src`/`style` attributes without escaping. On top of that, the data-gather helpers leak DB connections (7+ per card), double-query the balance, and silently substitute `STARTING_BALANCE` for unknown users, which makes cards lie about phantom accounts. Fix the injection + schema-drift rule first; the performance and consistency issues are close behind.

## Findings

### CRITICAL #1: `_get_season_start_balance` violates documented schema-drift rule

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:87-94`
**Confidence:** 1.0
**Risk:** CLAUDE.md explicitly states: "`sportsbook_cards._get_season_start_balance()` must wrap in `try/except sqlite3.OperationalError`. Column may not exist on older DBs." The current implementation has zero exception handling. On any older DB without `season_start_balance` column, sqlite raises `OperationalError: no such column: season_start_balance` which propagates up through `_determine_status()` → `_gather_sportsbook_data()` → `build_sportsbook_card()` → the Discord command. The whole sportsbook hub card crashes.
**Vulnerability:** No `try/except sqlite3.OperationalError` around the `SELECT season_start_balance` query. No fallback value. Also used by `build_stats_card` via `_gather_stats_data` at line 502, which computes `roi = ((balance - season_start) / season_start * 100)` at line 535 — ROI stat card ALSO crashes on schema drift.
**Impact:** Total sportsbook card failure on any deployment with a pre-migration DB. Silent in dev (fresh schema), explosive on older production DBs or after a partial rollout. Users see interaction errors; admins see stack traces.
**Fix:**
```python
def _get_season_start_balance(user_id: int) -> int:
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT season_start_balance FROM users_table WHERE discord_id = ?",
                (user_id,)
            ).fetchone()
        return row[0] if row else STARTING_BALANCE
    except sqlite3.OperationalError:
        # Column missing on older DBs — fall back to current balance
        return _get_balance(user_id)
```

### CRITICAL #2: HTML/CSS injection via unescaped logo URLs and team colors in real match card

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:843-871`
**Confidence:** 0.95
**Risk:** `event.get("home_logo_url", "")` and `event.get("away_logo_url", "")` are interpolated directly into `<img src="{away_logo}" style="...">` without any escaping. Same for `home_color`/`away_color` which get injected into inline `style="background:{a_color};"` blocks and used inside interpolation like `style="color:{h_color};"`. If the `real_events` table has a team with a logo URL containing a quote (`"`), attribute context breaks; more critically, `event` dicts come from ESPN API / admin data — an attacker-controlled or corrupted team record with `"><script>` in the URL can break out of the `src` attribute.
**Vulnerability:** No `esc()` call on any of: `away_logo`, `home_logo`, `home_color`, `away_color`. These are used in attribute context (`src="..."`) and style context (`background:{a_color}`). Style-context color injection is a live CSS-injection vector even if the Playwright render is server-side only — the PNG rendered to Discord can contain arbitrary embedded CSS that affects layout, including exfiltration vectors (background-image: url(attacker.example/leak)).
**Impact:** If `real_events` is populated from any admin-editable path, an attacker with write access can inject arbitrary HTML/CSS into card renders. Even without malice, a team with a `"` in its logo URL silently corrupts every card render for that matchup. The Playwright page will either crash or render garbage, and the failure is hidden inside a try/except-less render call.
**Fix:**
```python
away_logo = esc(event.get("away_logo_url", ""))
home_logo = esc(event.get("home_logo_url", ""))
# Validate colors: must match hex or rgb pattern
import re
_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgb\([^)]+\)|[a-zA-Z]+)$")
home_color = event.get("home_color", "") or ""
away_color = event.get("away_color", "") or ""
if not _COLOR_RE.match(home_color): home_color = ""
if not _COLOR_RE.match(away_color): away_color = ""
```
Also validate that logo URLs are http(s) and come from an allowlist, or host them locally.

### CRITICAL #3: `_get_total_won` unguarded `ZeroDivisionError` on odds=0

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:262-273`
**Confidence:** 0.9
**Risk:** The two loops compute `int(wager * 100 / abs(odds))` with NO guard for `odds == 0`. Compare with `_get_open_bets` at lines 181-194 which has a `if odds == 0: payout += wager` branch. If any settled winning bet was written with `odds=0` (possible from a cancelled-game settlement path, or a bad odds cache hit in the Elo engine), the STATS CARD crashes with `ZeroDivisionError` at card render time.
**Vulnerability:** Defensive programming was applied in one function (`_get_open_bets`) but NOT the parallel function (`_get_total_won`). The inconsistency is an audit marker that the author knew about the edge case but forgot to fix both sites. `parlays_table.combined_odds` is also vulnerable on the same line.
**Impact:** Stats card crashes for any user who has ever won a bet at 0 odds. The crash is silent from the user's perspective — the Discord command just errors out. Every call to `build_stats_card` for that user is permanently broken until the bad row is hand-cleaned from SQLite.
**Fix:** Mirror the `_get_open_bets` pattern:
```python
for wager, odds in rows:
    if odds == 0:
        total += wager
    elif odds > 0:
        total += wager + int(wager * odds / 100)
    else:
        total += wager + int(wager * 100 / abs(odds))
```
Also audit `_gather_parlay_analytics_data` line 1249 (`sum(payout_calc(w, o) for w, o in won_rows)`) for the same crash.

### CRITICAL #4: Silent "phantom user" card for unknown user_ids

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:78-94, 199-213`
**Confidence:** 0.9
**Risk:** `_get_balance` and `_get_season_start_balance` return `STARTING_BALANCE` (1000) when the user has NO row in `users_table`. `_get_leaderboard_rank` returns `(total, total)` as a fallback (line 213), meaning an unknown user gets displayed as "last place of N" — not "not a user." The sportsbook hub card cheerfully renders `$1,000 YOUR BALANCE · #31 of 31 · 0-0 record` for any random Discord snowflake, including users who never joined the league. This is a data integrity lie: cards look legitimate for accounts that do not exist.
**Vulnerability:** No "is this user actually in the economy?" check anywhere in the data-gather helpers. The card builders accept raw `user_id: int` with no identity resolution and no "unknown user" fallback branch. There is no link to the TSL identity registry (`build_member_db.get_db_username_for_discord_id`), even though CLAUDE.md requires that as the single source of truth.
**Impact:** (1) UX bug: users running `/flow` on a non-league member's profile see a fake account. (2) Observability hole: admins cannot tell "user with 0 bets" from "user who doesn't exist." (3) Leaderboard rank display is wrong — "#31 of 31" is a legitimate rank, not an error signal. Any downstream reconciliation (e.g., a user merge, or a leaderboard audit) will silently ingest these phantom records.
**Fix:** Add an explicit "user exists" check at the top of `_gather_sportsbook_data`/`_gather_stats_data`:
```python
def _user_exists(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT 1 FROM users_table WHERE discord_id = ?", (user_id,)
        ).fetchone() is not None
```
Card builders should raise a typed `UnknownUserError` or render a dedicated "not enrolled" card when the user has no row.

### WARNING #1: Float vs int balance corruption path

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:78-84, 404, 450`
**Confidence:** 0.85
**Risk:** `_get_balance` returns `row[0]` with no coercion. CLAUDE.md explicitly flags "Float vs int balance corruption in `flow_economy.db`." If the `balance` column drifts to REAL (e.g., a migration leaves an existing row as float), the downstream format specs `f"${balance:,}"` at lines 450, 598, 1144 will render `$1000.0` instead of `$1,000`, or with sub-cent trailing digits for damaged rows. `_get_weekly_delta` at line 108 does `_get_balance(user_id) - row[0]` — arithmetic between int and float silently coerces up, corrupting the delta as a float.
**Vulnerability:** No `int()` coercion on any DB read. No guard on the balance column type. Python's int/float mix is implicit and silent.
**Impact:** Display bugs ("$1000.0 this week"), format spec errors (`{delta:+d}` on a float would raise), and downstream arithmetic contaminated.
**Fix:** Coerce all balance reads: `return int(row[0]) if row else STARTING_BALANCE`. Do the same for `_get_season_start_balance`, `_get_weekly_delta`, and all other integer-expected columns.

### WARNING #2: `_determine_status` triple-query and double-fetch on every gather

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:276-283, 386-395, 497-512`
**Confidence:** 0.9
**Risk:** `_determine_status` calls `_get_leaderboard_rank` (full table scan), `_get_balance`, and `_get_season_start_balance` — three DB queries, each opening a new connection. `_gather_sportsbook_data` has already fetched `_get_balance(user_id)` once on line 389; now `_determine_status` fetches it again. Stats card version is worse: gathers balance, season_start, then calls `_determine_status` which refetches both. Every card render does 9-12 sqlite round-trips instead of the 3-4 it needs, and all the writes share zero connection state.
**Vulnerability:** No shared cursor; no `_gather_*` connection; each helper opens/closes its own `sqlite3.connect`. Pattern is inconsistent with `_gather_parlay_analytics_data` which DOES share a connection (line 1223).
**Impact:** Card render latency balloons under contention. On a locked DB (concurrent bet write), each `_get_*` helper can independently time out or retry. The retry cascade makes sqlite lock contention worse. On high-activity Discord commands this turns a ~50ms render into a multi-second stall.
**Fix:** Push a single `sqlite3.connect` into each `_gather_*` function and pass the connection to helpers, or rewrite to a single multi-statement fetch. Also pass already-fetched `balance` into `_determine_status(balance, rank)` instead of re-querying.

### WARNING #3: `_get_open_bets` integer division truncation and None-odds crash

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:181-194`
**Confidence:** 0.8
**Risk:** The payout math `int(wager * odds / 100)` truncates toward zero. For a $3 wager at +150 odds, this computes `int(3 * 150 / 100) = int(4.5) = 4`, but the correct payout is $4 (rounding down is acceptable). However, the same expression with a negative wager or a fractional wager from legacy data silently loses cents. More importantly: `odds` can be `None` from a raw SQLite read if the column is nullable. `odds == 0` is guarded, but `odds == None` raises `TypeError: '>' not supported between instances of 'NoneType' and 'int'`.
**Vulnerability:** No explicit None check. `rows` from the DB can include None values for `odds` or `wager_amount` depending on schema. `odds > 0` on None raises, which kills the sportsbook card.
**Impact:** Any user with a legacy pending bet with null odds crashes their own card render.
**Fix:** Explicit None coercion: `wager = int(wager or 0); odds = int(odds or 0)` before the conditional ladder. Apply the same fix in `_get_total_won`.

### WARNING #4: `_get_weekly_delta` hides real loss for users with no old snapshots

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:97-109`
**Confidence:** 0.75
**Risk:** The query looks up "most recent snapshot older than 7 days." If the user has NO such snapshot (newly joined, or `balance_snapshots` backfill is missing), it returns `0`. The card then displays `+$0 this week` — which is a lie if the user has been actively losing but joined <7 days ago. More insidiously: if the snapshotter (`db_migration_snapshots.take_daily_snapshot`) stops running, every user eventually falls into the "no old snapshot" branch and ALL cards show 0 delta. The failure is invisible.
**Vulnerability:** Missing snapshot treated identically to "no change." No sentinel for "data unavailable" vs "actual zero delta."
**Impact:** Silent observability gap. A broken snapshotter is undetectable from the card output.
**Fix:** Return `None` (or a typed "unavailable" sentinel) when no baseline snapshot exists, and render "—" instead of "+$0" in the card. Also add a separate monitoring query for "how many users have snapshots older than 7 days."

### WARNING #5: `_ticker_html` empty-results path leaves orphaned dividers

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:362-379, 461-465`
**Confidence:** 0.8
**Risk:** `_ticker_html` returns `""` on empty results (user has no settled bets). The card HTML template at lines 461-465 wraps it as:
```html
<div class="gold-divider"></div>
<!-- Win/Loss Ticker -->
{ticker}
<div class="gold-divider"></div>
```
When `ticker == ""`, the card renders two consecutive gold dividers with nothing between them — a visible layout glitch on the brand-new user card (who arguably is the most important first impression).
**Vulnerability:** No conditional wrapping. The ticker is unconditionally surrounded by dividers regardless of content.
**Impact:** Ugly first-time-user card — two empty dividers stacked. Minor but unprofessional.
**Fix:** Build the ticker block (dividers + content) as a single conditional string:
```python
ticker_block = f"<div class='gold-divider'></div>{ticker}<div class='gold-divider'></div>" if ticker else ""
```

### WARNING #6: `build_real_match_detail_card` non-numeric odds crash

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:820, 828, 838`
**Confidence:** 0.85
**Risk:** `int(ml_val)`, `int(sp_odds)`, `int(ou_odds)`, `float(sp_val)`, `float(ou_total)` all assume numeric strings. ESPN odds columns can contain "EVEN", "PK", or "OFF" as string sentinels. `int("EVEN")` raises `ValueError`. There is no try/except wrapping the `_cell` calls; a single bad row aborts the whole card render.
**Vulnerability:** No numeric validation. Assumes upstream has sanitized ESPN data.
**Impact:** One bad odds value from ESPN kills every real-sports match detail card for that matchup.
**Fix:** Wrap each conversion:
```python
try:
    ml_int = int(ml_val)
    grid_rows += _cell(team, american_to_str(ml_int))
except (TypeError, ValueError):
    grid_rows += _EMPTY
```

### WARNING #7: `_cell` helper's `int(value.replace("+", ""))` is fragile and dead-ends on negative

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:797-810`
**Confidence:** 0.75
**Risk:** The inner helper tries to detect favorite/underdog by `int(value.replace("+", ""))`. This strips "+" but not whitespace or the minus sign. For `value = "-165"`, `int("-165") = -165` — negative, so `cls = "fav"`. Works. For `value = "+150"`, `int("150") = 150` — positive, so `cls = "dog"`. Works. But for `value = "EVEN"`, `int("EVEN")` raises `ValueError` — caught by the bare `except (ValueError, AttributeError)`. For `value = "+ 150"` with a space, it raises. The bigger bug is it applies `cls` only to moneyline, but since `_cell` is also used for spread display (line 828 passes `f"{float(sp_val):+g}"` which is "+3.5" or "-3.5"), a spread of "-3.5" will be styled as "fav" — misleading visual.
**Vulnerability:** `_cell` is shared between moneyline (where fav/dog semantics hold) and spread (where they don't). The styling lie is silent.
**Impact:** Spread cells look like favorite/underdog MLs. Minor UI confusion.
**Fix:** Add a `cell_kind` parameter and only compute `cls` when `kind == "ml"`.

### WARNING #8: `build_bet_confirm_card` and `build_parlay_confirm_card` mishandle odds=0

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:952, 1012, 1020`
**Confidence:** 0.75
**Risk:** `odds_str = f"+{odds}" if odds > 0 else str(odds)` — for `odds == 0`, this returns `"0"`, displayed on the card as odds of 0. No American odds reading is actually 0 (it would be ±100 for even money). A zero odds value is a sentinel for "bad data," not a valid display. Showing `0` to a user about to confirm a bet is actively misleading.
**Vulnerability:** No guard for the invalid-odds case. Reuses same bug in parlay leg loop (line 1020).
**Impact:** A user could see a bet confirmation card showing "odds: 0" and think it's a bug, not a sign their bet is corrupted. Or worse, click confirm.
**Fix:** Raise or return an error card when `odds == 0`.

### WARNING #9: `_get_leaderboard_rank` loops instead of using SQL RANK

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:199-213`
**Confidence:** 0.65
**Risk:** The comment explicitly says "Could use SQL RANK() window function for O(1) lookup instead of fetching all rows, but user count is small (~31 owners) so linear scan is fine." This is documented tech debt. As soon as non-owner Discord members start placing bets (which the file implicitly supports — the DB has `discord_id` not `owner_id`), "user count" could be 10× bigger. Also: line 207 says `rank = 1; for did, bal in rows:` — if two users are tied on balance, they get consecutive ranks (1, 2) instead of shared rank (1, 1). That's a tie-handling bug, not just a performance issue.
**Vulnerability:** Assumption "user count is small" is fragile and undocumented beyond an inline comment. Tie-handling is incorrect.
**Impact:** Rank display is wrong for ties. Over time, scan cost grows linearly.
**Fix:** Use SQL: `SELECT COUNT(*)+1 FROM users_table WHERE balance > (SELECT balance FROM users_table WHERE discord_id=?)` for proper tie handling.

### WARNING #10: `_gather_parlay_analytics_data` crashes on any bad winning parlay row

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:1244-1249`
**Confidence:** 0.7
**Risk:** `total_won = sum(payout_calc(w, o) for w, o in won_rows)` — if a single `won_rows` entry has `w=None` or `o=None`, `payout_calc` likely raises TypeError or returns NaN. The whole generator expression aborts and the parlay analytics card fails to render. No row-level try/except.
**Vulnerability:** Bulk aggregation without per-row isolation.
**Impact:** One corrupted parlay = entire analytics card dead.
**Fix:**
```python
total_won = 0
for w, o in won_rows:
    try:
        total_won += payout_calc(w, o)
    except Exception:
        log.exception("bad parlay row: wager=%r odds=%r", w, o)
```

### WARNING #11: `_get_last_n_results` format string inconsistency confuses downstream parsers

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:156`
**Confidence:** 0.6
**Risk:** `record = f"{w}-{l}-{p}" if p else f"{w}-{l}"` — the output format is `W-L-P` or `W-L` depending on whether there are any pushes. Any regex/split downstream (or a future log parser) must handle both forms. If push count is 0 today and becomes >0 tomorrow for the same user, the format changes and any cached string-equality comparison fails.
**Vulnerability:** Variable-schema string output for a display value.
**Impact:** Subtle bugs if any caller parses the record string. Also inconsistent visual — users see "3-2" one week and "3-2-1" the next.
**Fix:** Always return `f"{w}-{l}-{p}"` for consistency.

### OBSERVATION #1: `DB_PATH` evaluated at import time

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:69-70`
**Confidence:** 0.95
**Risk:** `DB_PATH = os.getenv("FLOW_DB_PATH", ...)` at module import. Any runtime change to the env var (e.g., tests that swap DBs) is ignored. Also: re-importing the module doesn't help because Python caches.
**Vulnerability:** Import-time config binding.
**Impact:** Test isolation harder; no hot-swap for DB path.
**Fix:** Wrap in a function: `def _db_path(): return os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))`.

### OBSERVATION #2: `STARTING_BALANCE = 1000` magic number with no authoritative source

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:71`
**Confidence:** 0.9
**Risk:** The 1000 value is hardcoded here but also appears in other subsystems (flow_wallet, economy_cog, etc.). If any module drifts, onboarding balances become inconsistent.
**Vulnerability:** Duplicated constant without a single source of truth.
**Fix:** Import from a shared config module (e.g., `flow_wallet.STARTING_BALANCE`).

### OBSERVATION #3: Hardcoded sport pill list with dead "active" flag

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:425-429`
**Confidence:** 0.8
**Risk:** `sports = ["TSL", "NFL", "NBA", "MLB", "NHL"]` with only TSL marked active. The loop builds pills showing "TSL NFL NBA MLB NHL" where the last 4 look greyed out but actually there's no runtime data backing "active" status. The card implies NFL/NBA/MLB/NHL are "inactive" but in reality `real_sportsbook_cog` is live for some of these. Visual state does not match reality.
**Impact:** User confusion — "why does the card say NFL is inactive when /sportsbook has NFL lines?"
**Fix:** Either remove the inactive pills, or drive the list from `real_sportsbook_cog`'s enabled-sports registry.

### OBSERVATION #4: `_sparkline_svg` uses Python `or` for zero-range fallback

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:295`
**Confidence:** 0.6
**Risk:** `rng = max_v - min_v or 1` — works when `max_v == min_v` (rng=0 becomes 1), but is a readability wart. Future maintainer might replace `or` with a `!= 0` test and accidentally allow divide-by-zero when `rng == 0`.
**Fix:** Explicit: `rng = (max_v - min_v) or 1  # flat line -> arbitrary scale`. Add comment.

### OBSERVATION #5: Sparkline hardcoded colors bypass theme

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:301`
**Confidence:** 0.7
**Risk:** `color = "var(--win)" if data[-1] >= data[0] else "var(--loss)"` — uses CSS vars, good. But the sparkline width (440) and height (40) are hardcoded to the default card, ignoring `theme_id`. Theming of stroke width, line smoothing, etc. is impossible.
**Impact:** Theme system cannot customize sparkline appearance.
**Fix:** Read dimensions/stroke from theme tokens.

### OBSERVATION #6: `CSS` duplicated inline in every card body

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:311-348, 653-675, 924-942, 1077-1101, 1200-1218`
**Confidence:** 0.8
**Risk:** Five separate CSS blocks, each duplicated inline in every rendered HTML body. ~2KB of CSS per render, recomputed every time. Not cache-friendly; not server-side pre-compiled.
**Impact:** Wasted render bytes; harder to maintain style consistency.
**Fix:** Move all CSS into `atlas_style_tokens`/a shared stylesheet loaded once by the HTML engine.

### OBSERVATION #7: `build_match_detail_card` unescaped `ou_line`

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:716, 741, 757`
**Confidence:** 0.7
**Risk:** `{game['ou_line']}` is interpolated raw into HTML. `game` comes from `_build_game_lines()` which likely produces numeric values, but if an admin line-override writes a string, XSS surface opens. `away_spread` / `home_spread` ARE escaped (line 716 via `esc(game['away_spread'])`) — inconsistency.
**Fix:** `esc(str(game['ou_line']))`.

### OBSERVATION #8: `build_casino_hub_card` streak threshold is a magic `3`

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:1113, 1116`
**Confidence:** 0.5
**Risk:** `if s_type == "win" and s_count >= 3:` — why 3? No comment, no config. Changing this requires editing renderer code.
**Fix:** Move threshold to config or a constant with a docstring.

### OBSERVATION #9: `_get_weekly_delta` executes `_get_balance` twice via nested connection

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:97-109`
**Confidence:** 0.95
**Risk:** `_get_weekly_delta` opens a connection, does its query, closes the connection, then calls `_get_balance(user_id)` which opens yet ANOTHER connection. Two connections, two sqlite open/close cycles, for a single delta calculation. Same pattern in `_get_sparkline_data` line 124.
**Impact:** Wasted DB round-trips. Combined with WARNING #2, a single card render does 10+ connections.
**Fix:** Pass connection or balance into helper functions.

### OBSERVATION #10: `build_parlay_analytics_card` uses `rowid DESC` for "recent" ordering

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:1285`
**Confidence:** 0.85
**Risk:** `ORDER BY rowid DESC LIMIT 5` — rowid is SQLite-internal. If the table is ever VACUUMed or rebuilt (migration), rowid ordering can drift. For "recent" display, ordering should be by `created_at` or similar explicit timestamp column.
**Impact:** After a VACUUM, "recent parlays" could display in wrong order.
**Fix:** `ORDER BY created_at DESC` (assuming the column exists).

### OBSERVATION #11: `_gather_parlay_analytics_data` mixes connection management styles

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:1223-1308`
**Confidence:** 0.8
**Risk:** This function DOES share a single `sqlite3.connect` block across 5 queries (good!) while every other `_gather_*` function does not. The inconsistency suggests the author learned the lesson halfway through development but didn't back-port the fix to `_gather_sportsbook_data`/`_gather_stats_data`.
**Impact:** Refactor debt — next person reading the file has to track two different DB access patterns.
**Fix:** Pick one pattern and apply everywhere.

### OBSERVATION #12: No try/except around `render_card()` in any builder

**Location:** Every `build_*_card` function (lines 398, 515, 678, 778, 945, 1007, 1104, 1321)
**Confidence:** 0.85
**Risk:** `render_card()` can fail (Playwright timeout, page pool exhausted, OOM on HTML too large, etc.). No builder wraps the call in try/except; all failures propagate up the stack raw. Discord-side, this means an ephemeral error with no fallback image.
**Impact:** Any transient Playwright failure takes out the entire sportsbook hub/stats command. No graceful fallback to a text embed.
**Fix:** Each builder should catch render errors and fall back to a small "render unavailable" PNG or raise a typed error the command handler knows to convert to an embed.

### OBSERVATION #13: `card_to_file` does not validate `png_bytes`

**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_cards.py:1481-1483`
**Confidence:** 0.7
**Risk:** `card_to_file(png_bytes)` wraps raw bytes in `discord.File`. No check for `b""`, None, or non-PNG content. If `render_card` ever returns empty bytes (edge case: HTML parse fail on the Playwright side), Discord will reject the upload and the user sees a cryptic error.
**Fix:** `if not png_bytes or not png_bytes.startswith(b"\x89PNG"): raise ValueError("bad PNG")`.

## Cross-cutting Notes

- **Schema-drift defense is inconsistent across the file.** Only `_get_season_start_balance` was flagged in CLAUDE.md, but the same risk applies to EVERY column read here: `balance`, `season_start_balance`, `status`, `parlay_id`, `combined_odds`, `wager_amount`, etc. A single missing column anywhere aborts card rendering. Consider a schema-check function that runs at cog load and logs missing columns before any card render is attempted.
- **Sync `sqlite3.connect` inside functions called from async paths** — the card builders correctly use `run_in_executor` for `_gather_*`, but the individual `_get_*` helpers are also callable from sync paths and there's nothing stopping an accidental direct call from an async coroutine. Add a `# sync only` comment or raise on event loop detection.
- **Identity resolution is absent.** Per CLAUDE.md, `tsl_members` is the "single source of truth for mapping Discord names → in-game DB usernames" but this file reads `discord_id` directly from `users_table` and never touches `tsl_members`. The phantom-user bug (CRITICAL #4) is a direct consequence. Every `_get_*` helper should start with an identity check.
- **Connection leak potential on failure paths.** All `with sqlite3.connect(...)` blocks correctly close the connection, but if any helper is wrapped in a retry loop (which it isn't today), a mid-query exception could leak the implicit transaction. Prefer `try/finally con.close()` for helpers that will eventually be moved into shared-connection mode.
- **Findings from Ring 1 `flow_sportsbook.py` are likely to recur here.** This file queries the same tables (`bets_table`, `parlays_table`, `users_table`) with the same assumptions — any type-coercion or null-handling bug flagged in flow_sportsbook probably has a twin here.
