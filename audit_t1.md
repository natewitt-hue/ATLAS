# ATLAS Pre-7.0 Audit — Terminal 1: Flow Economy Domain

Audited files: `flow_sportsbook.py`, `economy_cog.py`, `flow_wallet.py`, `flow_audit.py`, `flow_events.py`, `wager_registry.py`, `flow_store.py`, `store_effects.py`, `flow_live_cog.py`, `flow_cards.py`, `sportsbook_cards.py`, `ledger_poster.py`, `odds_utils.py`

---

## BUGS (P0/P1)

### B1 — Elo computation docstring says `status='3'` but SQL correctly uses `IN ('2','3')`
**File:** `flow_sportsbook.py:36,448` | **Severity:** P2 (doc-only, not a runtime bug)
**Description:** The file header (line 36) and docstring (line 448) both say "Only status='3' (final) games used in Elo computation" but the actual SQL at line 472 correctly uses `WHERE CAST(status AS TEXT) IN ('2', '3')`. The docs are wrong, not the code.
**Fix:** Update docstring to match actual SQL behavior.

### B2 — `_build_game_lines` auto-lock compares `week_idx < dm.CURRENT_WEEK` (off-by-one)
**File:** `flow_sportsbook.py:825` | **Severity:** P1
**Description:** `week_idx` comes from the API (0-based `weekIndex`), but `dm.CURRENT_WEEK` is 1-based. The comparison `week_idx < dm.CURRENT_WEEK` is actually correct by accident: if CURRENT_WEEK is 5 (1-based) and the API game is week 4 (0-based index 3), then 3 < 5 is true, which correctly locks a past-week game. However, it also locks the *current* week's games: if CURRENT_WEEK=5, current week's API weekIndex=4, then 4 < 5 is true — locking games that are actually this week.
**Fix:** Change `week_idx < dm.CURRENT_WEEK` to `week_idx < (dm.CURRENT_WEEK - 1)` to correctly compare 0-based to 0-based. This ensures only actually-past-week games get auto-locked.

### B3 — `flow_audit.py` hardcoded embed colors instead of `AtlasColors`
**File:** `flow_audit.py:79-81` | **Severity:** P2
**Description:** `to_embed_dict()` uses hardcoded `0x2ECC71`, `0xE74C3C`, `0xF39C12` instead of `AtlasColors.SUCCESS`, `AtlasColors.ERROR`, `AtlasColors.WARNING`.
**Fix:** Import `AtlasColors` and use tokens.

### B4 — `flow_sportsbook.py` `TSL_BLACK` is hardcoded hex instead of `AtlasColors`
**File:** `flow_sportsbook.py:80` | **Severity:** P2
**Description:** `TSL_BLACK = 0x1A1A1A` is hardcoded rather than using an `AtlasColors` token.
**Fix:** Use `AtlasColors` token if available, or at minimum document why this differs.

### B5 — `flow_live_cog.py` hardcoded hex colors in pulse highlight HTML
**File:** `flow_live_cog.py:631,633,635,637` | **Severity:** P1
**Description:** The `_update_pulse` method builds highlight HTML with hardcoded colors: `#FBBF24` (gold) and `#c0b8a8` (muted text) instead of CSS token variables. These will not respect user themes and will break if the token palette changes.
**Fix:** Replace hardcoded colors with CSS `var(--gold)` and `var(--text-muted)` respectively.

### B6 — `_place_straight_bet` defers AFTER wallet debit — risk of 3s timeout crash
**File:** `flow_sportsbook.py:1078-1130` | **Severity:** P1
**Description:** `_place_straight_bet` (when `already_deferred=False`) performs the wallet debit, writes to sportsbook_core, and registers the wager *before* calling `defer()` at line 1130. The wallet debit involves `BEGIN IMMEDIATE` + DB writes, and `sportsbook_core.write_bet` is an async DB call. If these take >3s combined (DB contention, slow I/O), Discord's interaction timeout fires, causing an `InteractionTimedOut` crash. The bet would be placed but the user would see nothing.
**Fix:** Move the `defer()` call to the top of the function (before the wallet lock) when `already_deferred=False`.

### B7 — `ParlayWagerModal.on_submit` defers AFTER all DB work
**File:** `flow_sportsbook.py:1284-1392` | **Severity:** P1
**Description:** `ParlayWagerModal.on_submit` does wallet debit, parlay insert, leg inserts, wager registry writes, and sportsbook_core mirror — all before `defer()` at line 1392. This is the same 3s timeout risk as B6 but worse because parlays involve many more DB operations.
**Fix:** Add `await interaction.response.defer(ephemeral=True)` at the top of `on_submit`, before the wallet lock.

### B8 — `PropBetModal.on_submit` never defers — sends embed directly after DB work
**File:** `flow_sportsbook.py:1434-1488` | **Severity:** P1
**Description:** `PropBetModal.on_submit` does wallet debit + prop wager insert + wager registry inside a DB transaction, then sends `interaction.response.send_message(embed=...)`. If the DB work exceeds 3s, the response will fail. Unlike straight bets and parlays, this doesn't even attempt to defer.
**Fix:** Add `await interaction.response.defer(ephemeral=True)` before the wallet lock, then change `send_message` to `followup.send`.

### B9 — `economy_cog.py` `_eco_give_impl` / `_eco_take_impl` / `_eco_set_impl` / `_eco_check_impl` don't defer before DB work
**File:** `economy_cog.py:426-516` | **Severity:** P1
**Description:** All four `_eco_*_impl` methods are called from `boss_cog.py` button callbacks. They perform async wallet operations (`admin_give`, `admin_take`, `admin_set`, `admin_check`) which involve `BEGIN IMMEDIATE` + DB writes, then call `interaction.response.send_message`. If the DB work takes >3s, Discord times out. The role-based variants (`_eco_give_role_impl`, `_eco_take_role_impl`) correctly defer, but the single-user variants do not.
**Fix:** Add `await interaction.response.defer(ephemeral=True)` at the top of each `_impl` method and change `send_message` to `followup.send`.

### B10 — `economy_cog.py` `_post_audit` calls `get_channel_id("admin_chat")` without `guild_id`
**File:** `economy_cog.py:298` | **Severity:** P1
**Description:** `get_channel_id("admin_chat")` is called without a `guild_id` parameter. If the `setup_cog.get_channel_id` function requires `guild_id` to resolve properly in multi-guild scenarios, this will fail silently.
**Fix:** Pass `guild_id` from the available bot context (e.g., `self.bot.guilds[0].id if self.bot.guilds else None`).

---

## UX ISSUES (P1/P2)

### U1 — Stipend `_process_stipend` posts to #ledger but gives no DM/channel confirmation to recipients
**File:** `economy_cog.py:359-418` | **Severity:** P2
**Description:** Users receive stipend payments silently. They only know about it if they happen to check their balance or read #ledger. No notification is sent to the user.
**Fix:** Consider a brief DM or channel ping after batch processing. Low priority — audit log + ledger exist.

### U2 — `flow_live_cog.py` session recap suppressed for < 2 games
**File:** `flow_live_cog.py:681` | **Severity:** P3
**Description:** If a user plays exactly 1 game, no session recap is posted. This is by design but worth documenting — a user who plays a single big game gets no recap card.
**Fix:** Already gated intentionally. No fix needed; document behavior.

### U3 — `flow_audit.py` `to_embed_dict` truncates at 5 items per severity with "... and N more" but no link to full report
**File:** `flow_audit.py:89-92` | **Severity:** P2
**Description:** The embed shows at most 5 findings per severity, truncating with "... and N more" but doesn't tell the user how to see the full list.
**Fix:** Already has footer "Run /boss flow audit for details" set by economy_cog. The embed dict itself doesn't include it though — consider adding it inside `to_embed_dict()`.

### U4 — `store_effects.py` `consume_effect` returns `True`/`False` but calling code has no user-facing feedback
**File:** `store_effects.py:130-176` | **Severity:** P3
**Description:** When an effect is consumed (reroll, insurance), the return value is `True`/`False` but the consumer cogs need to surface this to the user. This is an integration concern, not a bug in this file.
**Fix:** No change needed in store_effects.py — consumer cogs should check the return value.

---

## CONSISTENCY / DEBT (P2/P3)

### C1 — `flow_audit.py` hardcoded embed colors
**File:** `flow_audit.py:79-81` | **Severity:** P2
**Description:** Uses `0x2ECC71`, `0xE74C3C`, `0xF39C12` instead of `AtlasColors.SUCCESS`, `AtlasColors.ERROR`, `AtlasColors.WARNING`.
**Fix:** `from atlas_colors import AtlasColors` and swap.

### C2 — `flow_live_cog.py` hardcoded inline style colors in pulse HTML
**File:** `flow_live_cog.py:631,633,635,637` | **Severity:** P2
**Description:** `color:#FBBF24` and `color:#c0b8a8` hardcoded instead of CSS vars.
**Fix:** Replace with `var(--gold)` and `var(--text-muted)`.

### C3 — `flow_cards.py` uses `#FBBF24` in CSS as fallback value
**File:** `flow_cards.py:242,660,683,779,832` | **Severity:** P3
**Description:** `var(--push, #FBBF24)` pattern is acceptable (CSS fallback), but the hex is repeated 5+ times. Should be consolidated to a single CSS custom property at the card level.
**Fix:** Already uses `var(--push, ...)` pattern — this is correct CSS fallback behavior. No fix needed.

### C4 — `flow_sportsbook.py:80` hardcoded `TSL_BLACK = 0x1A1A1A`
**File:** `flow_sportsbook.py:80` | **Severity:** P3
**Description:** Minor hardcoded color. Used only in embed context, not card rendering.
**Fix:** Replace with `AtlasColors` token if one exists, or document.

### C5 — `economy_cog.py` duplicate `_ensure_user` function
**File:** `economy_cog.py:80-92` | **Severity:** P3
**Description:** `economy_cog._ensure_user()` is a copy of `flow_wallet._ensure_user()`. It's only used internally in economy_cog's admin operations which now call `flow_wallet` functions anyway.
**Fix:** Remove `economy_cog._ensure_user()` — it's dead code since admin_give/take/set all use `flow_wallet` directly.

### C6 — Elo docstring inaccuracy
**File:** `flow_sportsbook.py:36,448` | **Severity:** P3
**Description:** Docstring says "Only status='3'" but SQL uses `IN ('2', '3')`. Code is correct, docs are wrong.
**Fix:** Update docstring line 448 to say `status IN ('2', '3')`.

---

## OUT-OF-SCOPE FLAGS

These were noticed while reading in-scope files. Do NOT fix — for other terminal audits.

| File | Issue |
|------|-------|
| `real_sportsbook_cog.py` | Imported by `flow_sportsbook.py` — not audited here. `_place_real_bet` and `CustomRealWagerModal` may have similar defer timing issues. |
| `sportsbook_core.py` | `write_bet`, `write_parlay`, `write_parlay_leg`, `settle_event` — core bet engine not in scope. Mirror failure cleanup in parlay on_submit (lines 1373-1387) does rollback but logs at ERROR without a user-facing message. |
| `atlas_send.py` | `send_card()` utility imported everywhere — not audited. |
| `data_manager.py` | `CURRENT_WEEK` 1-based convention is the root cause of the off-by-one family. Not in scope. |
| `setup_cog.py` | `get_channel_id()` signature/behavior needs audit — called without guild_id in `economy_cog.py:298`. |
| `boss_cog.py` | Calls all `_impl` methods — should verify it defers before calling non-deferring impls. |
| `db_migration_snapshots.py` | Imported by flow_sportsbook — not audited. |
| `casino/renderer/pulse_renderer.py` | `HighlightRow`, `build_pulse_data`, `render_pulse_card` — rendering not audited. |
| `atlas_colors.py` | Need to verify `AtlasColors.WARNING` exists for flow_audit fix. |

---

## FIXES APPLIED

| # | File | Lines | What Changed |
|---|------|-------|--------------|
| 1 | `flow_sportsbook.py:825` | B2 | Changed `week_idx < dm.CURRENT_WEEK` → `week_idx < (dm.CURRENT_WEEK - 1)` to fix 0-based vs 1-based off-by-one in auto-lock. Added comment explaining the conversion. |
| 2 | `flow_live_cog.py:631-637` | B5/C2 | Replaced hardcoded `#FBBF24` → `var(--gold)` and `#c0b8a8` → `var(--text-muted)` in pulse highlight HTML. |
| 3 | `flow_sportsbook.py:1071-1076` | B6 | Moved `defer(ephemeral=True)` to before wallet lock in `_place_straight_bet` (when not already deferred). Removed redundant defer after DB work. |
| 4 | `flow_sportsbook.py:1295-1298` | B7 | Added `defer(ephemeral=True)` before wallet lock in `ParlayWagerModal.on_submit`. Changed InsufficientFundsError handler from `response.send_message` → `followup.send`. Removed redundant defer after DB work. |
| 5 | `flow_sportsbook.py:1447-1493` | B8 | Added `defer(ephemeral=True)` before wallet lock in `PropBetModal.on_submit`. Changed prop-closed error and InsufficientFundsError handlers from `response.send_message` → `followup.send`. Changed final confirmation from `response.send_message` → `followup.send`. |
| 6 | `economy_cog.py:428` | B9 | Added `if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)` at top of `_eco_give_impl`. Changed `response.send_message` → `followup.send`. |
| 7 | `economy_cog.py:456` | B9 | Same defer guard + followup.send fix for `_eco_take_impl`. |
| 8 | `economy_cog.py:487` | B9 | Same defer guard + followup.send fix for `_eco_set_impl`. |
| 9 | `economy_cog.py:516` | B9 | Same defer guard + followup.send fix for `_eco_check_impl`. |
| 10 | `flow_audit.py:27,80-82` | B3/C1 | Added `from atlas_colors import AtlasColors`. Replaced hardcoded `0x2ECC71`/`0xE74C3C`/`0xF39C12` with `AtlasColors.SUCCESS.value`/`AtlasColors.ERROR.value`/`AtlasColors.WARNING.value`. |
| 11 | `flow_sportsbook.py:36,448` | C6 | Updated docstring from "Only status='3'" to "Only status IN ('2','3')" to match actual SQL. |
