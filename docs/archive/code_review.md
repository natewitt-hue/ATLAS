# ATLAS Code Review — 2026-03-10

**Codebase:** ~40 Python files, ~35,800 LOC
**Reviewed by:** 4 parallel agents (Bug Hunter, Stale/Outdated Code, Logic & Architecture, Visual/UX)
**Scope:** All production .py files (excluding QUARANTINE/, __pycache__/, worktrees)

---

## Executive Summary

| Severity | Count | Description |
|----------|-------|-------------|
| **P0 — Critical** | 6 | Will crash or corrupt data at runtime |
| **P1 — High** | 14 | Significant bugs or data integrity risks |
| **P2 — Medium** | 23 | Should fix but not immediately dangerous |
| **P3 — Low** | 25 | Cleanup, consistency, nice-to-have |
| **Total** | **68** | |

### Top 5 Most Urgent Fixes

1. **sentinel_cog.py:1291** — `from parity_cog import` always fails; causes NameError crash on position change denial AND silently bypasses cornerstone protection system
2. **sentinel_cog.py:2306** — `GEMINI_API_KEY` undefined variable; 4th Down Analyzer is completely broken
3. **codex_cog.py:797** — SQL injection in `/h2h` command; user-resolved names interpolated via f-strings
4. **codex_cog.py:329** — `DB_SCHEMA` computed once at import time with wrong `CURRENT_SEASON` value
5. **status==3 filter bug** — 4 locations (data_manager, intelligence, flow_sportsbook) silently drop completed games

---

## 1. Bug Hunter Findings

### P0 — Critical

#### BUG-01: SQL injection in `/h2h` command
**File:** codex_cog.py:797-813
**Description:** User-resolved usernames (`u1`, `u2`) from fuzzy matching are interpolated directly into SQL via f-strings without parameterization. A malicious entry in `NICKNAME_TO_USER` or the member DB could inject SQL.
**Evidence:** `sql = f"""SELECT ... WHERE homeUser = '{u1}' AND awayUser = '{u2}' ..."""`
**Fix:** Use parameterized queries: `run_sql(sql, (u1, u2, u2, u1))` with `?` placeholders.

#### BUG-02: SQL injection in season recap
**File:** codex_cog.py:876-882
**Description:** Same f-string SQL pattern for the `season` parameter.
**Evidence:** `sql = f"""SELECT ... WHERE seasonIndex='{season}' ..."""`
**Fix:** Use `?` parameterized queries.

#### BUG-03: DB_SCHEMA cached with wrong season at import time
**File:** codex_cog.py:329
**Description:** `DB_SCHEMA = _build_schema()` is evaluated once at import time when `dm.CURRENT_SEASON` is still the hardcoded default `6`. The schema string permanently contains the wrong season number for all Gemini SQL prompts.
**Fix:** Make `DB_SCHEMA` a function call (like reasoning.py's `get_schema()` with TTL caching), or re-compute after `load_all()`.

### P1 — High

#### BUG-04: status==3 filter drops completed games (4 locations)
**File:** data_manager.py:710, 743, 779 + intelligence.py:461
**Description:** `get_weekly_results()`, `get_h2h_record()`, and `get_clutch_records()` filter for `status == 3` only. Per CLAUDE.md, completed games should use `status IN (2, 3)`. Status 2 games have valid final scores.
**Fix:** Change all to `.isin([2, 3])` or `not in (2, 3)`.

#### BUG-05: Casino get_balance() race condition
**File:** casino/casino_db.py:194-212
**Description:** `get_balance()` opens a new connection without `BEGIN IMMEDIATE`. Two concurrent calls for a new user could both see `row is None`, both insert, and the balance could be stale between INSERT and re-SELECT.
**Fix:** Wrap in `BEGIN IMMEDIATE` like other balance-modifying functions.

#### BUG-06: Blackjack double-down wager corruption
**File:** casino/games/blackjack.py:309, 216
**Description:** `session.wager *= 2` modifies wager in-place. If the player also split, `on_timeout` calculates `total_wager = s.wager * 2`, using the already-doubled wager — resulting in 4x the original bet. `process_wager` records an inflated wager, corrupting house bank tracking.
**Fix:** Store `session.original_wager` separately; use it for split calculations.

#### BUG-07: Crash cashout double-payout race
**File:** casino/casino_db.py:558-583
**Description:** `cashout_crash_bet()` reads and updates without `BEGIN IMMEDIATE`. Two rapid Cash Out clicks could both read `status='active'`, both compute payout, and both return non-zero — causing `process_wager` to be called twice.
**Fix:** Add `await db.execute("BEGIN IMMEDIATE")` before the read.

#### BUG-08: Coinflip accept double-deduction race
**File:** casino/games/coinflip.py:137-158
**Description:** `self.resolved` flag is checked and set with async operations in between. Two rapid button clicks could both pass the check before `self.resolved = True`, deducting the opponent's wager twice.
**Fix:** Move `self.resolved = True` immediately after the check (before `deduct_wager`), or use `asyncio.Lock`.

#### BUG-09: Daily scratch TOCTOU race
**File:** casino/casino_db.py:432, 450-465
**Description:** `can_claim_scratch()` check runs on a separate connection before `BEGIN IMMEDIATE`. Two simultaneous claims could both pass the check and both credit the balance.
**Fix:** Move the eligibility check inside the `BEGIN IMMEDIATE` transaction block.

#### BUG-10: SQLite connection leak in run_sql()
**File:** codex_cog.py:433-447
**Description:** `conn = get_db()` is not wrapped in `try/finally`. If `conn.execute()` or `cur.fetchall()` throws, the connection is never closed.
**Fix:** Use `try: ... finally: conn.close()` or a context manager.

### P2 — Medium

#### BUG-11: Off-by-one in fallback week fetch loop
**File:** data_manager.py:410-419
**Description:** `range(0, _l_week + 1)` where `_l_week` is 1-based CURRENT_WEEK. This fetches one extra week beyond current (weekIndex 0 through `_l_week`).
**Fix:** Change to `range(0, _l_week)`.

#### BUG-12: safe_exec has no timeout
**File:** reasoning.py:361
**Description:** If Gemini generates an infinite loop, `exec(code, env)` blocks the thread executor forever with no timeout mechanism.
**Fix:** Add a timeout via `concurrent.futures` or AST statement count limit.

#### BUG-13: Unbounded _conv_cache growth
**File:** codex_cog.py:95
**Description:** `_conv_cache` dict grows without limit across all users. Only per-user trimming exists, no global eviction.
**Fix:** Add LRU-style eviction or periodic pruning.

#### BUG-14: Unbounded _affinity_cache growth
**File:** affinity.py:48
**Description:** Same unbounded growth pattern for `_affinity_cache`.
**Fix:** Use a bounded LRU cache.

#### BUG-15: Crash round task fire-and-forget
**File:** casino/games/crash.py:446
**Description:** `asyncio.create_task(_lobby_then_run(...))` has no exception handler. If the task fails, the round stays in `active_rounds` forever, blocking the channel.
**Fix:** Add `task.add_done_callback()` that cleans up `active_rounds` and refunds players.

### P3 — Low

#### BUG-16: response.text None checks missing (3 locations)
**Files:** bot.py:284, codex_cog.py:530, codex_cog.py:575
**Description:** `response.text.strip()` without None check. Gemini can return `None` text on safety filter or empty response.
**Fix:** `response.text.strip() if response.text else "..."`.

#### BUG-17: Deprecated /wittsync sends double messages
**File:** bot.py:643
**Description:** `_sync_impl(interaction)` sends a followup, then the wrapper sends another followup with a deprecation tip. User sees two messages.
**Fix:** Append tip inside `_sync_impl` or accept the behavior.

#### BUG-18: economy_cog assumes single guild
**File:** economy_cog.py:313
**Description:** `self.bot.guilds[0]` hardcodes first guild for stipend processing.
**Fix:** Fine for TSL's single-server setup. Document the assumption.

---

## 2. Stale/Outdated Code Findings

### P1 — High

#### STALE-01: history_cog fallback import in oracle_cog
**File:** oracle_cog.py:637-663
**Description:** Fallback `from history_cog import ...` references a file renamed to codex_cog.py in v1.4. Masks real import errors.
**Fix:** Remove the entire `except ImportError: try: from history_cog import ...` block (lines 654-666).

#### STALE-02: ATLAS_ICON_URL is an expiring Discord CDN link (4 files)
**Files:** bot.py:164, oracle_cog.py:676, genesis_cog.py:63, sentinel_cog.py:60
**Description:** Signed Discord CDN link with `ex=`, `is=`, `hm=` expiration tokens. Will/has expired, breaking all embed thumbnails. Duplicated in 4 separate files.
**Fix:** Host on permanent CDN (GitHub raw, Imgur). Define once and import everywhere.

#### STALE-03: KNOWN_MEMBERS/KNOWN_MEMBER_TEAMS hardcoded (will drift)
**File:** intelligence.py:65-139
**Description:** 31-entry hardcoded Discord ID → nickname and Discord ID → team dicts. The `tsl_members` table is the canonical source for this data. These dicts will silently drift.
**Fix:** Replace with lookups against `tsl_members` via `build_member_db.py`.

#### STALE-04: status==3 bug in intelligence.py
**File:** intelligence.py:461
**Description:** `get_clutch_records()` filters `status == 3` only — same bug as BUG-04.
**Fix:** Change to `.isin([2, 3])`.

### P2 — Medium

#### STALE-05: ATLAS_PERSONA hardcoded fallback in codex_cog
**File:** codex_cog.py:64-69
**Description:** Redundant hardcoded persona string when echo_loader already provides fallback stubs.
**Fix:** Low priority. Can simplify to use echo_loader directly.

#### STALE-06: Stale docstrings reference old file names
**Files:** build_member_db.py:1227,1268 + ability_engine.py:962
**Description:** Docstrings reference `history_cog`, `stats_hub_cog`, `positionchange_cog`, `ability_cog` — all renamed/merged.
**Fix:** Update to `codex_cog`, `oracle_cog`, `sentinel_cog`, `genesis_cog`.

#### STALE-07: 6 deprecated slash commands still registered
**Files:** bot.py:637-652, echo_cog.py:121-144, awards_cog.py:99-109
**Description:** `/wittsync`, `/rebuilddb`, `/echorebuild`, `/echostatus`, `/createpoll`, `/closepoll` — marked "remove in Phase 5" but Phase 5 never happened. Wastes Discord command slots.
**Fix:** Remove all 6 deprecated wrappers.

#### STALE-08: export_code_snapshot() runs on every boot
**File:** bot.py:656-672
**Description:** Concatenates all .py files into `ATLAS_Full_Code.txt` on every startup — ~30k lines written to disk each boot.
**Fix:** Gate behind an env variable or remove (CLAUDE.md provides better context transfer).

#### STALE-09: Dead functions (3 locations)
**Files:** lore_rag.py:40 (`is_lore_query`), analysis.py:593 (`generate_bar_chart`), echo_loader.py:208-215 (3 `PERSONA_*` compat functions)
**Description:** Functions defined but never imported or called anywhere.
**Fix:** Remove all.

#### STALE-10: Dead pagination system
**File:** intelligence.py:729-767
**Description:** `PaginatedResult` class + 3 helper functions for reaction-based pagination. Superseded by v2.0 button Views.
**Fix:** Remove entirely.

#### STALE-11: Empty on_ready listener
**File:** echo_cog.py:33-36
**Description:** `on_ready` with `pass` body — persona loading handled in bot.py.
**Fix:** Remove.

### P3 — Low

#### STALE-12: Gemini model names hardcoded in multiple files
**Files:** bot.py:271 (`gemini-2.0-flash`), echo_voice_extractor.py:63-64 (`gemini-2.5-flash`, `gemini-2.5-pro`), reasoning.py
**Description:** Different model versions across files. Not centralized.
**Fix:** Extract to env variables or shared constants.

#### STALE-13: Gold color constant mismatch in player_picker
**File:** player_picker.py:70-71
**Description:** `TSL_GOLD = 0xD4AF37` vs bot.py's `ATLAS_GOLD = discord.Color.from_rgb(201, 150, 42)` (0xC9962A).
**Fix:** Import from shared palette.

#### STALE-14: Debug print() calls should use logging (3 files)
**Files:** lore_rag.py:71,213 + card_renderer.py:779 + awards_cog.py:24,32
**Description:** Runtime error handlers using `print()` instead of `logging.warning/error()`.
**Fix:** Replace with structured logging calls.

#### STALE-15: echo_voice_extractor.py — operational tool in project root
**File:** echo_voice_extractor.py (1222 lines)
**Description:** Heavyweight extraction pipeline run infrequently via `/atlas echorebuild`. Lives in project root cluttering the codebase.
**Fix:** Move to `tools/` subdirectory. Update echo_cog.py import path.

#### STALE-16: analysis.py + intelligence.py overlap
**Files:** analysis.py (618 lines) vs intelligence.py (767 lines)
**Description:** Both serve oracle_cog with overlapping team/owner analytics. intelligence.py imports from analysis.py.
**Fix:** Consider consolidating into one analytics module.

#### STALE-17: ability_engine.py stale docstring
**File:** ability_engine.py:10-13
**Description:** References `/abilityaudit` and `/abilitycheck` as standalone commands; they're now Genesis hub buttons.
**Fix:** Update docstring.

---

## 3. Logic & Architecture Findings

### P0 — Critical

#### LOGIC-01: parity_cog import crash — NameError on position change denial
**File:** sentinel_cog.py:1291, 2040
**Description:** `from parity_cog import _state, _save_state, _STATE_PATH` always fails (parity_cog.py doesn't exist). Fallback defines `_save_parity_state()` but line 2040 calls `_save_state()` — the import-only name. Every position change denial crashes with `NameError`.
**Impact:** Position change denial is completely broken.
**Fix:** Change import to `from genesis_cog import _state, _save_state, _STATE_PATH`.

#### LOGIC-02: Cornerstone protection silently bypassed
**File:** sentinel_cog.py:1290-1323, 1902
**Description:** Because the parity_cog import fails, sentinel_cog creates its OWN local `_state = {}` (line 1298). This is separate from genesis_cog's `_state`. The cornerstone check at line 1902 always gets an empty dict.
**Impact:** Position changes for cornerstone-designated players are NEVER blocked. The entire cornerstone protection system is bypassed.
**Fix:** Same fix as LOGIC-01 — correct the import source.

#### LOGIC-03: GEMINI_API_KEY undefined in 4th Down Analyzer
**File:** sentinel_cog.py:2306
**Description:** Uses bare variable `GEMINI_API_KEY` which is never defined or imported. The module-level client at line 647 correctly uses `os.getenv()`, but the 4th Down section doesn't.
**Impact:** Every `/fourthdown` invocation crashes with `NameError`. The feature is completely non-functional.
**Fix:** Change to `client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))`.

### P1 — High

#### LOGIC-04: Elo computation uses status='3' only
**File:** flow_sportsbook.py:386
**Description:** `WHERE CAST(status AS TEXT) = '3'` excludes status 2 completed games from Elo calculation.
**Impact:** Elo ratings computed from incomplete data → inaccurate sportsbook lines for every matchup.
**Fix:** Change to `WHERE CAST(status AS TEXT) IN ('2','3')`.

#### LOGIC-05: Draft season select menu — 25-option time bomb
**File:** oracle_cog.py:3515-3521
**Description:** `DraftSeasonView` builds one option per season (1 through `dm.CURRENT_SEASON`). At 25+ seasons, Discord's 25-item limit causes an API error. Currently safe at season 6, but the league processes multiple seasons per year.
**Impact:** Draft History feature will crash when league reaches season 25.
**Fix:** Add `[-25:]` truncation or pagination.

#### LOGIC-06: Gemini response.text None crash (2 sentinel locations)
**Files:** sentinel_cog.py:761, 2320
**Description:** Force request and 4th Down Analyzer call `.strip()` or `.upper()` on `response.text` without None check. Gemini returns None on safety filter.
**Impact:** Force request ruling and 4th down analysis crash when Gemini refuses.
**Fix:** `(response.text or "").strip()` and `return response.text or "INCONCLUSIVE"`.

#### LOGIC-07: Sportsbook identity resolution bypasses tsl_members
**File:** flow_sportsbook.py:527-538
**Description:** Owner names resolved via `power_map["userName"]` and fuzzy-matched against Elo cache. Neither path uses `get_alias_map()` or `tsl_members`. API usernames with underscores/case mismatches silently fall back to default 1500 Elo.
**Impact:** Some owners get inaccurate sportsbook lines due to Elo lookup miss.
**Fix:** Route through `get_alias_map()` before Elo lookup.

### P2 — Medium

#### LOGIC-08: Parlay carts lost on restart
**File:** flow_sportsbook.py:814
**Description:** `_parlay_carts: dict[int, list[dict]] = {}` is in-memory only. Bot restart loses all in-progress parlay builds.
**Impact:** Users lose partially-built parlays with no warning.
**Fix:** Persist to SQLite or warn users.

#### LOGIC-09: Dead async wrapper invites misuse
**File:** sentinel_cog.py:2299-2301
**Description:** `_analyze_screenshot` is `async` but calls blocking `_analyze_screenshot_sync` directly. Callers correctly use `run_in_executor` instead, but the wrapper's existence invites future misuse.
**Fix:** Remove the unused wrapper.

#### LOGIC-10: Sportsbook balance check not in same transaction as deduction
**File:** flow_sportsbook.py:1093-1102
**Description:** Balance is checked in one DB call, then deducted in another. No transactional guarantee (though asyncio's single-thread model makes exploitation unlikely currently).
**Fix:** Move check+deduct into a single `BEGIN IMMEDIATE` transaction.

---

## 4. Visual/UX Findings

### P1 — High

#### UX-01: 5+ different "gold" colors across cogs — no shared palette
**Files:** oracle_cog.py:681,701 + flow_sportsbook.py:74 + casino/casino.py:46 + genesis_cog.py:2007 + player_picker.py:70
**Description:** At least 5 distinct gold shades used: `rgb(201,150,42)`, `rgb(250,189,47)`, `0xD4AF37`, `0xC9962A`, and inline `rgb(212,175,55)`. Genesis uses `discord.Color.blurple()`. Casino uses `discord.Color.teal()`. Makes the bot feel like separate products stitched together.
**Fix:** Create `atlas_palette.py` with shared color constants. Import everywhere.

#### UX-02: Dead buttons — no on_timeout on most Views
**Files:** casino/casino.py:162 + flow_sportsbook.py:1243,1342 + oracle_cog.py:2714,3412 + sentinel_cog.py
**Description:** Most Views with finite timeouts (120-300s) have NO `on_timeout` handler. Buttons silently die after timeout — users click and nothing happens, with no visual indication of expiry. Only blackjack and coinflip PVP implement `on_timeout`.
**Fix:** Add `on_timeout` to all finite-timeout Views: disable buttons and edit message with "Session expired" notice.

#### UX-03: Hub landing embeds sent as ephemeral (conflicts with hub pattern)
**Files:** oracle_cog.py:3998 + sentinel_cog.py:2842 + genesis_cog.py (hub send) + casino/casino.py:264
**Description:** `/oracle`, `/rulehub`, `/rosterhub`, `/casino` all send hub landing embeds as ephemeral. Per the stated Hub pattern, main landing embeds should be public so the channel can see them and buttons can be shared. Only `/sportsbook` correctly sends publicly.
**Fix:** Remove `ephemeral=True` from hub landing sends, or document the all-ephemeral pattern as intentional.

#### UX-04: Sentinel hub buttons are dead ends
**File:** sentinel_cog.py:2687-2704
**Description:** "Force Request" and "4th Down" hub buttons just tell the user to use the slash command instead. Users click expecting functionality and get a text rejection.
**Fix:** Open the workflow directly from the button, or provide a channel link/redirect.

### P2 — Medium

#### UX-05: 6+ inconsistent footer formats
**Files:** oracle_cog.py:136,1232 + flow_sportsbook.py:1052 + casino/casino.py:290 + sentinel_cog.py:192 + genesis_cog.py:541
**Description:** Footers vary: "TSL Analytics", "ATLAS™ Oracle", "TSL Sportsbook", "TSL Casino", "TSL Commissioner Office", "TSL Trade Engine v2.7". Some have icon_url, most don't.
**Fix:** Standardize to `"ATLAS™ [Module] · [Context]"` format with icon_url.

#### UX-06: Duplicate card_renderer.py in casino/renderer/
**Files:** casino/renderer/card_renderer.py (800px) vs casino/renderer/casino_card_renderer.py (920px)
**Description:** Near-identical 600-line files differing only in dimension constants. `casino_card_renderer.py` appears unused (blackjack/slots import `card_renderer.py`). Maintenance hazard.
**Fix:** Delete `casino_card_renderer.py` if unused, or consolidate with configurable dimensions.

#### UX-07: Casino stats inline fields stack poorly on mobile
**File:** casino/casino.py:220-227
**Description:** Per-game stat fields use `inline=True`, creating half-width blocks that squeeze text on mobile.
**Fix:** Use `inline=False` for full-width readability.

#### UX-08: Sportsbook game card columns stack on mobile
**File:** flow_sportsbook.py:1403-1419
**Description:** 3 inline fields (Spread, ML, O/U) with backtick-formatted text that can't word-wrap. Stacks poorly on mobile.
**Fix:** Use a single non-inline code block field.

#### UX-09: Crash chart small text unreadable on mobile
**File:** casino/renderer/card_renderer.py:451-458
**Description:** Crash chart at 500x300px with 10px history text → ~5px on mobile. Nearly illegible.
**Fix:** Scale to 600x350px. Boost history font to 14px.

#### UX-10: Slot machine image cramped on mobile
**File:** casino/renderer/card_renderer.py:375-382
**Description:** Slots at 420x220px with 14px title and 11px info → ~7px and ~5.5px on mobile.
**Fix:** Scale to 600x300px.

#### UX-11: Google Fonts CDN dependency in trade card renderer
**File:** card_renderer.py:299
**Description:** Playwright trade card uses `@import url('https://fonts.googleapis.com')`. Requires internet during render. `wait_until="networkidle"` hangs if CDN is slow.
**Fix:** Bundle fonts locally or inline as base64 `@font-face`.

#### UX-12: Casino slots/blackjack missing "thinking" indicator
**Files:** casino/games/slots.py:97,265 + casino/games/blackjack.py:520
**Description:** `defer()` called without `thinking=True`. User sees command vanish with no feedback for ~1s.
**Fix:** Add `thinking=True` to defers.

#### UX-13: CategoryView buttons not disabled after selection
**File:** sentinel_cog.py:420
**Description:** Complaint filing CategoryView doesn't disable buttons after selection. User can click a second category and potentially file duplicate complaints.
**Fix:** Disable all buttons after the first selection.

### P3 — Low

#### UX-14: Sportsbook "My Bets" could exceed field limits with many bets
**File:** flow_sportsbook.py:1667-1690
**Description:** Bet lines concatenated with no truncation guard. Unlikely to hit 1024 chars with normal usage but possible.
**Fix:** Add `[:950]` truncation with "...and N more" suffix.

#### UX-15: H2H rivalry embed could hit field limit
**File:** oracle_cog.py:2831-2873
**Description:** Season-by-season breakdown in one field. With many seasons, could approach 1024 chars. Also uses f-string SQL (see BUG-01).
**Fix:** Add truncation guard.

#### UX-16: Sportsbook status embed approaches 25-field limit
**File:** flow_sportsbook.py:1993-2015
**Description:** 16 games + 7 header fields = 23 fields. Barely under Discord's 25-field limit.
**Fix:** Use a single description code block instead of per-game fields.

#### UX-17: Font path ordering — Linux paths checked first on Windows
**Files:** casino/renderer/card_renderer.py:45-51 + casino_card_renderer.py + ledger_renderer.py
**Description:** Fallback font paths list Linux paths before Windows. On Windows dev machine, each unicode glyph font load fails 3 times before finding Windows path.
**Fix:** Detect platform at import time; only include relevant paths.

#### UX-18: Ledger card small text
**File:** casino/renderer/ledger_renderer.py:38-39
**Description:** 12px label and 11px footer fonts are small at native 600px, near-illegible on mobile (~6px).
**Fix:** Increase to 14px label, 13px footer.

#### UX-19: Sportsbook grading embed uses empty spacer field
**File:** flow_sportsbook.py:1916-1921
**Description:** 6 inline fields including a `"\u200b"` spacer that looks broken on mobile.
**Fix:** Use 3 inline + 1 non-inline summary. Remove spacer.

#### UX-20: Hot/cold stat trends could exceed field limit
**File:** oracle_cog.py:119
**Description:** Player with 5+ stat categories could push "Stat Trends" field past 1024 chars.
**Fix:** Limit to top 4 categories or add `[:1024]` guard.

#### UX-21: Oracle hub uses monospace code block for navigation
**File:** oracle_cog.py:1220-1227
**Description:** Code blocks use smaller monospace font on mobile. Alignment assumptions break across platforms.
**Fix:** Use bold markdown with newlines instead.

#### UX-22: Game card bet buttons stay active after bet placement
**File:** flow_sportsbook.py:1240-1340
**Description:** Bet buttons remain clickable after modal closes. User could accidentally open multiple bet slips.
**Fix:** Track placed bets and disable used bet types.

#### UX-23: CoinPickView buttons not visually disabled after choice
**File:** casino/casino.py:140-155
**Description:** After picking Heads/Tails, both buttons still appear clickable. No visual confirmation of choice.
**Fix:** Disable all buttons and edit message before `self.stop()`.

---

## Cross-Cutting Concerns

### 1. status IN (2,3) Filter — Systematic Bug
Found in **5 locations** across 3 files. The CLAUDE.md explicitly warns about this. A project-wide audit of all game status filters should be done.

| File | Line | Function |
|------|------|----------|
| data_manager.py | 710 | `get_weekly_results()` |
| data_manager.py | 743 | `get_weekly_results()` fallback |
| data_manager.py | 779 | `get_h2h_record()` |
| intelligence.py | 461 | `get_clutch_records()` |
| flow_sportsbook.py | 386 | Elo rating SQL |

### 2. response.text None Check — Systematic Gap
Gemini SDK returns `None` for `.text` on safety filter or empty response. Found **6 locations** without None checks. Should be standardized with a helper: `def safe_text(response) -> str: return (response.text or "").strip()`.

### 3. ATLAS_ICON_URL — Expiring CDN Link in 4 Files
Should be hosted on a permanent CDN and defined once in a shared module.

### 4. Casino Race Conditions — Economy Exploit Surface
Four separate race conditions in the casino subsystem that could allow balance manipulation:
- get_balance TOCTOU (casino_db.py:194)
- crash cashout double-payout (casino_db.py:558)
- coinflip accept double-deduction (coinflip.py:137)
- daily scratch TOCTOU (casino_db.py:432)

All share the same pattern: reading state in one connection/check, then modifying in another. Fix by moving all reads inside `BEGIN IMMEDIATE` transactions.

### 5. No Shared Color Palette
At least 5 distinct "gold" colors and 3 distinct "green/red" color sets across the codebase. A single `atlas_palette.py` would fix this.

---

## Recommended Fix Priority

### Sprint 1 — Critical Crashes (fix immediately)
1. sentinel_cog.py — Fix `parity_cog` import → `genesis_cog` (LOGIC-01, LOGIC-02)
2. sentinel_cog.py — Fix `GEMINI_API_KEY` → `os.getenv(...)` (LOGIC-03)
3. codex_cog.py — Parameterize SQL in `/h2h` and `_season_recap_impl` (BUG-01, BUG-02)
4. codex_cog.py — Make `DB_SCHEMA` dynamic (BUG-03)

### Sprint 2 — Data Integrity (fix this week)
5. Fix all 5 `status == 3` → `status IN (2, 3)` locations (BUG-04, STALE-04, LOGIC-04)
6. Fix 4 casino race conditions with `BEGIN IMMEDIATE` (BUG-05, BUG-07, BUG-08, BUG-09)
7. Fix blackjack wager corruption (BUG-06)
8. Fix codex_cog connection leak (BUG-10)
9. Add response.text None guards (BUG-16, LOGIC-06)

### Sprint 3 — Stale Code Cleanup (fix this cycle)
10. Remove `history_cog` fallback import (STALE-01)
11. Host ATLAS_ICON_URL permanently, centralize (STALE-02)
12. Replace hardcoded KNOWN_MEMBERS with tsl_members lookup (STALE-03)
13. Remove 6 deprecated slash commands (STALE-07)
14. Remove dead functions/classes (STALE-09, STALE-10, STALE-11)

### Sprint 4 — UX Polish (fix when convenient)
15. Create shared `atlas_palette.py` (UX-01)
16. Add `on_timeout` to all finite-timeout Views (UX-02)
17. Standardize footer format (UX-05)
18. Fix mobile readability for casino renders (UX-09, UX-10, UX-18)
19. Bundle trade card fonts locally (UX-11)
20. Add `thinking=True` to casino defers (UX-12)

---

*Generated by 4 parallel Claude Code review agents on 2026-03-10.*
