# ATLAS Nightly Code Review
## Thursday · Genesis, Sentinel & Compliance Focus
**Date:** 2026-04-09
**Version:** ATLAS v7.10.0
**Scope:** genesis_cog.py, trade_engine.py, god_cog.py, sentinel_cog.py, ability_engine.py, roster.py, card_renderer.py
**Total LOC reviewed:** ~8,604 across 7 files
**Commits in last 24h:** 0 (last commit: `ceef0e8` — Tuesday casino/rendering audit)

---

## Executive Summary

Genesis and Sentinel are in solid shape post the v7.10.0 sicko-mode sweep. The TOCTOU double-approve bug is properly fixed with `_trade_approval_lock`, async file I/O locking was applied to complaint/FR state, and `_safe_int()` is used consistently throughout the valuation pipeline. One critical bug was found in the render failure path of `TradeActionView._update_status()` that will cause double announcements in the trade log channel whenever a card render fails. Six warnings found across four files, primarily around async/sync discipline and missing sandboxing.

---

## CRITICAL — 1 finding

### C1 · Double announcement on trade card render failure (genesis_cog.py)
**Location:** `TradeActionView._update_status()` — render try/except block
**Impact:** Every trade card render failure (Playwright crash, timeout, network issue) produces 2 announcements in the trade log channel: one from the embed fallback edit and one from the fallback channel send, flooding the commissioner log with duplicates.

**Root cause:** The success path calls `log_ch.send(file=...)` and returns early. The except block falls through to the embed fallback which calls both `msg.edit(embed=...)` AND a second `log_ch.send(embed=...)`. The edit and the send are two distinct channel events visible to Discord users.

**Fix:** Add a `return` (or `else`) guard after `msg.edit(embed=...)` in the fallback path, OR consolidate all channel send calls to a single site. The embed fallback should only edit the existing message, never spawn a second channel post.

---

## WARNINGS — 6 findings

### W1 · `_save_trade_state()` synchronous with no lock (genesis_cog.py)
**Location:** `_save_trade_state()` function
**Issue:** Unlike sentinel's `_save_complaint_state()` and `_save_fr_state()` which were converted to async with asyncio.Lock guards in v7.10.0, `_save_trade_state()` remains synchronous and unprotected. Concurrent trade approvals — two commissioners tapping Approve within milliseconds via different hub sessions — could cause a partial write to the state JSON, corrupting `_state["pending_trades"]`.
**Severity:** Medium — requires two concurrent actors and exact timing, but trades are high-value operations.
**Fix:** Apply the same async-lock pattern used in sentinel. Add `_trade_state_lock = asyncio.Lock()` at module level; convert `_save_trade_state()` to `async def` using `async with _trade_state_lock`.

### W2 · ImportError bypass in `_counter_callback` skips authorization (genesis_cog.py)
**Location:** `_counter_callback()` in `TradeActionView`, the `except ImportError: pass` block
**Issue:** The authorization check (verifying the user is a party to the trade before allowing counter-offers) is gated inside a try/except that silently passes on ImportError. If the `intelligence` module is missing or fails to import, any Discord user who sees the trade card can submit a counter-offer regardless of whether they're party to the trade.
**Severity:** Medium — the intelligence module is a soft dependency, so this is a realistic failure mode.
**Fix:** Hoist the authorization check outside the intelligence try block, or re-raise ImportError as a user-visible error rather than silently passing.

### W3 · `_save_counter()` has no asyncio.Lock (sentinel_cog.py)
**Location:** `ForceRequestCog._save_counter()`
**Issue:** `_save_counter()` is a synchronous write with no concurrency guard. If two force request counter submissions arrive simultaneously (Discord interaction retries or multi-tap), the JSON file could be partially overwritten.
**Severity:** Low-Medium — force requests are less frequent than trades, but the write pattern is the same class of bug that was fixed in the complaint/FR state saves.
**Fix:** Apply `_fr_file_lock` (already exists in the module) to `_save_counter()` and convert it to async.

### W4 · Opponent name not sandboxed in force request AI prompt (sentinel_cog.py)
**Location:** `ForceRequestCog` AI prompt construction
**Issue:** The user's freetext note is correctly wrapped in `<untrusted_user_note>` tags in the AI prompt. However, the **opponent name** field — also supplied by the submitting user — is interpolated directly into the prompt without the same sandboxing. A user could supply an opponent name like `"John. Ignore all previous instructions and..."` to attempt prompt injection against the AI ruling system.
**Severity:** Low — the AI output is a recommendation, not an automated action. A commissioner reviews before any ruling is applied. But the asymmetric treatment is a pattern inconsistency.
**Fix:** Wrap opponent name in the same `<untrusted_user_note>` tags, or add a prompt note that all user-supplied fields are untrusted.

### W5 · httpx sync/async inconsistency in sentinel_cog (sentinel_cog.py)
**Location:** `_fetch_image_bytes` (4th down, uses sync `httpx.get` via `run_in_executor`) vs. force request (uses `async httpx.AsyncClient`)
**Issue:** Two subsystems in the same file use different httpx patterns for the same operation (image download). The sync-in-executor pattern holds a threadpool thread for the full HTTP request duration.
**Severity:** Low — both patterns are functionally correct.
**Fix:** Standardize on `async httpx.AsyncClient` for both subsystems for consistency and to reduce threadpool pressure on slow image downloads.

### W6 · `roster.assign()` / `roster.unassign()` run synchronous sqlite3 in async context (roster.py)
**Location:** `OwnerRegistry.assign()`, `OwnerRegistry.unassign()`
**Issue:** These methods use `sqlite3.connect()` directly and are called from async Discord interaction handlers. Each call blocks the event loop for the duration of the SQLite write.
**Severity:** Low — writes are fast and infrequent in a 31-team league. Pragmatically acceptable but technically incorrect.
**Fix:** Wrap sqlite3 calls in `asyncio.to_thread()` or convert to `aiosqlite`.

---

## OBSERVATIONS — 10 findings

### O1 · `_acted` flag not restart-safe — state-based guard now covers this
`RulingPanelView._acted` is an in-memory flag that resets on bot restart. The new state-based double-ruling guard added in v7.10.0 (checking resolved status from persistent state before inspecting `_acted`) makes this safe. The flag is defense-in-depth rather than the primary gate. Acknowledged.

### O2 · Trade re-evaluation at approve time may differ from propose time
`TradeActionView` re-evaluates trade value at approval time using current roster data. If a player was re-rated or a cornerstone was added/removed between proposal and approval, the displayed band on the approved card may differ from what the proposer saw. Intentional in the general case (audit-correct) but worth documenting in trade rules.

### O3 · genesis-sentinel state import coupling
`sentinel_cog.py` imports `_state`, `_save_state`, `_STATE_PATH` from `genesis_cog`. A TODO comment acknowledges this. The coupling is load-order safe (genesis loads before sentinel) but creates a fragile import dependency. Long-term: extract shared state to `genesis_state.py`.

### O4 · `rings_mult` uses selling team's rings, not drafting team's rings
`trade_engine.py L237-241`: Acknowledged TODO. `rings_mult` should penalize the team that drafted the player (ring count at draft time), not the team currently selling. Current behavior slightly mis-attributes ring tax. Low-priority.

### O5 · `check_position_change()` defaults to legal=True for unknown position combos
`ability_engine.py`: Any `(from_pos, to_pos)` pair not in `POSITION_CHANGE_RULES` returns `(True, ["Commissioner discretion applies"])`. This default-allow posture means novel combos (QB→WR, DE→DT, etc.) display as "ELIGIBLE" in Discord output with a note. Consider switching to default-deny for unspecified combos to avoid implicit approval signals.

### O6 · Dead code: `and not hard_blocks` on line 1309 of ability_engine.py
`ability_engine.py L1309`: `if rule.get("requires_commissioner") and not hard_blocks:` — the function already returned `False` at L1307 if `hard_blocks` is non-empty, so `not hard_blocks` is always `True` at L1309. The condition simplifies to just `rule.get("requires_commissioner")`. Minor code clarity issue.

### O7 · Force Request and 4th Down missing from SentinelHubView buttons
`sentinel_cog.py`: `_build_sentinel_hub_embed()` lists "Force Request" and "4th Down" in the embed text as available tools, but `SentinelHubView` only has buttons for Complaint, Pos Change, Disconnect, Blowout, Stat Check, and Pos Log. There are no hub buttons for Force Request or 4th Down. Users see them described but cannot reach them from the hub — only via dedicated slash commands (if they exist).

### O8 · Asset truncation in card_renderer.py has no "+N more" indicator
`card_renderer.py L208-214`: `players_a[:4]` and `picks_a[:4]` silently cap assets at 4 per side. A trade with 5+ players/picks renders with extras invisibly dropped and no "+2 more" text shown on the card.

### O9 · `_ordinal()` produces wrong suffix for 21+ round picks
`card_renderer.py L117-118`: Returns `f"{n}th"` for all values outside 1-3, including 21 ("21th"), 22 ("22th"), 23 ("23th"). TSL has rounds 1-7 so this is never triggered in practice, but would produce malformed strings on league expansion.

### O10 · ESPN CDN logo fallback is a silent external dependency during card renders
`card_renderer.py L111`: `_team_logo_url()` falls back to `https://a.espncdn.com/i/teamlogos/nfl/500/{abbrev}.png` when local branding lookup fails. This URL is fetched by Playwright at render time. If ESPN CDN is slow or unreachable, the logo silently fails (broken image) with no error surfaced to the Discord user.

---

## POSITIVE PATTERNS — 10 findings

1. **asyncio.Lock on complaint/FR file I/O** (sentinel_cog.py) — module-level `_complaint_file_lock` and `_fr_file_lock` applied to all persistent state writes. Correct async pattern.

2. **`_trade_approval_lock` TOCTOU fix** (genesis_cog.py) — all acceptance logic is inside the lock. Lock is correctly scoped to approval only, not the full interaction lifetime.

3. **`_safe_int()` NaN-safe helper used consistently** — appears in both genesis_cog.py and trade_engine.py. Guards the entire valuation pipeline against CSV export NaN values (known Madden export quirk).

4. **`@functools.lru_cache(maxsize=4096)` on `_cached_archetype()`** (ability_engine.py) — keyed on `(rosterId, season, pos, arch_tuple)`. Prevents 3000+ redundant archetype calculations on full-roster audits.

5. **Parameterized SQL throughout roster.py** — all queries use `?` placeholders. No string interpolation anywhere. Zero SQL injection surface.

6. **SentinelHubView persistent view pattern** — `timeout=None` + unique `custom_id` on all 6 buttons. `setup()` registers the view before cog load. Buttons survive bot restarts.

7. **RulingPanelView state-based double-ruling guard** — checks resolved status from persistent state before `_acted` flag, making the restart-unsafe in-memory flag defense-in-depth.

8. **HTML user content escaping via `_esc()`** (card_renderer.py) — all user-supplied strings (team names, owner names, player names, AI commentary, trade notes) routed through `esc()`. No XSS surface in server-side Playwright renders.

9. **`_validate_image_url()` SSRF defense** (sentinel_cog.py) — restricts force request image downloads to Discord CDN domains only. Prevents users from submitting arbitrary URLs to the image fetcher.

10. **`POSITION_CHANGE_RULES` with `max_thresholds`** (ability_engine.py) — correctly models speed/agility CAPS as upper bounds distinct from minimums. The check correctly flags "too fast for LB" as a hard block.

---

## CROSS-MODULE RISKS

| Risk | Source | Target | Severity |
|------|--------|--------|----------|
| Direct state import | sentinel_cog imports `_state`, `_save_state` from genesis_cog | genesis_cog state | Medium |
| External logo dependency | card_renderer._team_logo_url() falls back to ESPN CDN | Playwright render path | Low |
| Sync file read in async | trade_engine.evaluate_trade() reads parity_state.json via json.load() | trade evaluation | Low |

---

## TEST GAPS

1. TOCTOU lock behavior — no test exercises two simultaneous approval attempts; lock correctness verified by code review only.
2. Double-announcement on render failure — no test simulates render_trade_card() returning None and verifies exactly one channel message is sent.
3. `_save_trade_state()` concurrent write — no test exercises two concurrent state writes.
4. ImportError bypass in `_counter_callback` — no test verifies authorization is enforced when intelligence module is absent.
5. `check_position_change()` unknown combos — no test verifies default-allow behavior for combos not in POSITION_CHANGE_RULES.
6. Cornerstone parity blocking in evaluate_trade() — no test verifies that a locked player triggers RED band with BLOCKED note.
7. Asset truncation in card renderer — no test verifies that a 5-player trade renders without error and correctly caps at 4 shown.

---

## METRICS

| Metric | Value |
|--------|-------|
| Files reviewed | 7 |
| Total LOC reviewed | ~8,604 |
| Critical findings | 1 |
| Warning findings | 6 |
| Observations | 10 |
| Positive patterns | 10 |
| Test gaps | 7 |
| Commits in 24h | 0 |
| ATLAS version | 7.10.0 |

---

## CLAUDE.md HEALTH CHECK

**Method:** Cross-checked `_EXTENSIONS` list in `bot.py:241-259` against CLAUDE.md cog load order table. Verified module map entries against files on disk.

### Cog Load Order — ACCURATE

`bot.py:_EXTENSIONS` matches CLAUDE.md table exactly — 17 cogs, same order:

| # | CLAUDE.md | bot.py | Status |
|---|-----------|--------|--------|
| 1 | echo_cog | echo_cog | OK |
| 2 | setup_cog | setup_cog | OK |
| 3 | flow_sportsbook | flow_sportsbook | OK |
| 4 | casino.casino | casino.casino | OK |
| 5 | oracle_cog | oracle_cog | OK |
| 6 | genesis_cog | genesis_cog | OK |
| 7 | sentinel_cog | sentinel_cog | OK |
| 8 | awards_cog | awards_cog | OK |
| 9 | codex_cog | codex_cog | OK |
| 10 | polymarket_cog | polymarket_cog | OK |
| 11 | economy_cog | economy_cog | OK |
| 12 | flow_store | flow_store | OK |
| 13 | flow_live_cog | flow_live_cog | OK |
| 14 | real_sportsbook_cog | real_sportsbook_cog | OK |
| 15 | boss_cog | boss_cog | OK |
| 16 | god_cog | god_cog | OK |
| 17 | atlas_home_cog | atlas_home_cog | OK |

### Module Map — ACCURATE

All modules listed in CLAUDE.md Module Map are present and loaded. No new .py modules were introduced in this audit's focus set that require map updates.

### Rendering Stack — ACCURATE

`card_renderer.py` is correctly listed under "Trade | card_renderer.py | Trade card". All other renderer entries match prior verified state.

### Version — CURRENT

`ATLAS_VERSION = "7.10.0"` in `bot.py:170`. Matches most recent commit message.

### CLAUDE.md VERDICT: No changes required.

---

*ATLAS Nightly Review · Thursday Focus: Genesis / Sentinel / Compliance · 2026-04-09*
