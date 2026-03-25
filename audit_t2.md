# ATLAS Pre-7.0 Audit — Terminal 2: Casino + Render Pipeline

**Auditor:** Terminal 2 (Claude Code)
**Date:** 2026-03-25
**Files in scope:** 27 files (casino/*, renderers, style tokens, engine, echo, affinity, utilities)

---

## BUGS (P0/P1)

### B1 — [P1] play_again.py: Race condition on balance check before replay
**File:** `casino/play_again.py:100-112`
**Issue:** `_on_play` edits the message view (disabling buttons) BEFORE checking balance. If `bal < self.wager`, it re-enables buttons and tries to `interaction.response.send_message()` — but the interaction was already consumed by the `edit_message` call on line 100. This means the "insufficient funds" error message will throw `InteractionResponded`.
**Same bug on:** `_on_double` (lines 130-146) — identical pattern.
**Fix:** Move the balance check before the `edit_message` call, or use `followup.send()` for the error.

### B2 — [P1] play_again.py: "Casino Hub" button consumes no interaction response
**File:** `casino/play_again.py:159-176`
**Issue:** `_on_hub` calls `interaction.message.edit(view=self)` which does NOT consume the interaction response. Then it calls `cog.casino_hub(interaction)` which starts with `interaction.response.defer()`. This works, BUT if the `message.edit()` call fails silently (e.g. ephemeral timeout), the hub opens fine. However, there's no guard against the cog being None AND the interaction already being consumed — the fallback `interaction.response.send_message()` on line 174 will fail if `casino_hub` was called but errored after `defer()`.

### B3 — [P1] prediction_html_renderer.py: Foreign blue palette in CATEGORY_COLORS_HEX
**File:** `casino/renderer/prediction_html_renderer.py:34-40`
**Issue:** The fallback `CATEGORY_COLORS_HEX` dict contains foreign blues (`#5B9BD5`, `#3498DB`) and teals (`#1ABC9C`, `#00CED1`) that don't exist in the ATLAS token system. The jewel-glow CSS (lines 145-167) properly maps categories to `data-cat` attributes with on-brand token colors (`var(--jewel-blue)`, `var(--jewel-purple)`, `var(--jewel-amber)`). However, `_category_color()` on line 54 returns these raw hex values, and if any renderer uses `_category_color()` for inline `style=` overrides, it will bypass the jewel-glow CSS and render off-brand.
**Risk:** If `polymarket_cog.CATEGORY_COLORS_HEX` is importable (normal case), this fallback is never used. But the fallback values should still be correct.

### B4 — [P1] blackjack.py: Near-miss detection checks score 22 which is unreachable
**File:** `casino/games/blackjack.py:92-93`
**Issue:** `_detect_near_miss` checks `if p_score == 22` for "Busted by ONE" — but `_hand_value()` can return any value > 21 for a bust (it correctly counts aces), and 22 is just one of many bust values. A hand totaling 23, 24, etc. with a near-miss bust scenario is missed. The intent is to detect "busted by exactly 1 over 21" which would be `p_score == 22`, so this is actually correct for the intended semantics. **Downgraded to P2 — minor UX gap, not a bug.**

### B5 — [P0] atlas_html_engine.py: Page pool — no recovery if browser disconnects mid-render
**File:** `atlas_html_engine.py:656-689`
**Issue:** In `render_card()`, if the browser disconnects between `acquire()` and the `finally: release()` — e.g., Chromium OOM-killed — the `release()` method calls `page.is_closed()` which returns True, then creates a new page via `_new_page()`. BUT `_new_page()` calls `_get_browser()` which only reconnects if `_browser is None or not _browser.is_connected()`. If the browser object exists but is in a zombie state where `is_connected()` is stale, the pool gets poisoned with dead pages.
**Mitigation:** The `acquire()` method (line 618-624) has a health-check loop that replaces dead pages, and pages are recycled every 100 renders. The `_get_browser()` reconnect logic is gated on `is_connected()`. This is defense-in-depth but there's a gap: if the browser object's `is_connected()` returns True but all pages are dead, the pool exhaust error on line 624 fires. Recovery requires bot restart.
**Fix:** Add a `try/except` in `_new_page()` that forces browser reconnection on page creation failure.

### B6 — [P1] crash.py: Multiplier capped at 100x in curve but MAX_CRASH_MULTIPLIER is 1000x
**File:** `casino/games/crash.py:125, 50`
**Issue:** `_current_multiplier()` caps at `100.0` via `min(...)`, but `MAX_CRASH_MULTIPLIER` is defined as `1000.0` and used on line 364 for cashout capping. The crash_point (from DB) could theoretically be > 100, but the multiplier curve will never reach it — the round would run forever approaching 100x but never crashing. If the DB generates a crash_point > 100, the round would never end.
**Impact:** Depends on `create_crash_round()` in casino_db — if it generates crash points > 100, this is a live bug. The standard crash point distribution likely caps well below 100x, so this is probably safe but the inconsistency is a code smell.
**Fix:** Align the cap — use `MAX_CRASH_MULTIPLIER` in the curve or lower `MAX_CRASH_MULTIPLIER` to 100.

### B7 — [P1] highlight_renderer.py: _wrap_card builds its own status bar, bypasses wrap_card's status_class
**File:** `casino/renderer/highlight_renderer.py:82-144`
**Issue:** The local `_wrap_card()` function builds an inline-styled `<div style="height:5px;...">` for the status bar, then calls `wrap_card(inner_html, theme_id=theme_id)` with NO `status_class`. This means the engine's `.status-bar` div gets an empty class (renders as the default gold gradient from base CSS), AND the highlight card has a SECOND status bar from the inline HTML. Result: **two status bars** — one from the engine (default gold) and one from the highlight renderer (custom gradient).
**Fix:** Pass the status as `status_class` to `wrap_card()` and remove the inline status bar div. OR accept this as intentional (highlights use custom gradients not covered by engine classes) and suppress the engine's bar by passing a custom class.

---

## UX ISSUES (P1/P2)

### U1 — [P2] session_recap_renderer.py: Game pills capped at 2 + overflow, no hard layout guard
**File:** `casino/renderer/session_recap_renderer.py:186-211`
**Issue:** The game breakdown shows up to 2 game pills + an "+N more" overflow indicator. This is well-designed (max 3 pills visible). No layout breakage risk. **No issue found — design is correct.**

### U2 — [P2] ledger_renderer.py: Timestamps use 24h UTC format (%H:%M UTC)
**File:** `casino/renderer/ledger_renderer.py:230, 305`
**Issue:** Confirmed — `datetime.now(timezone.utc).strftime("%H:%M UTC")` is correct 24h format. **No issue — QW fix confirmed applied.**

### U3 — [P2] pulse_renderer.py: "FLOW PULSE" version tag
**File:** `casino/renderer/pulse_renderer.py:246`
**Issue:** The header reads `FLOW PULSE` with subtitle `LIVE ACTIVITY DASHBOARD`. No "v1.1" version tag present. **QW fix confirmed — version tag already removed.**

### U4 — [P2] echo_cog.py / echo_loader.py: Persona system graceful degradation
**File:** `echo_loader.py`
**Issue:** The persona is now fully inline (hardcoded in `_UNIFIED_PERSONA` string). No file I/O at all — `load_all_personas()` just returns a status dict. The old `echo/*.txt` file system has been completely replaced. **No degradation risk — the system cannot fail.**

### U5 — [P2] Casino game cards: Status bar consistency
**Files:** `casino/renderer/casino_html_renderer.py`, all game entry points
**Issue:** All casino game renderers use `wrap_card(body, status_class)` correctly:
- Blackjack: passes outcome-mapped status through `_bj_outcome()` → correct
- Slots: passes outcome directly → correct
- Crash: passes `"loss"` for crashed, no status for live → correct
- Coinflip: passes outcome → correct
**No issue found — status bars are consistent.**

### U6 — [P1] affinity.py: Scores are bounded [-100, +100]
**File:** `affinity.py:39-41`
**Issue:** `_clamp_score()` correctly clamps to `[SCORE_MIN, SCORE_MAX]` = `[-100, 100]`. Scores cannot overflow. **No issue found.**

---

## CONSISTENCY / DEBT (P2/P3)

### C1 — [P2] prediction_html_renderer.py: Fallback CATEGORY_COLORS_HEX uses non-token hex values
**File:** `casino/renderer/prediction_html_renderer.py:34-40`
**Issue:** The fallback dict has foreign colors that don't map to any ATLAS token. Should use Tokens values:
- `#5B9BD5` → should be `Tokens.JEWEL_BLUE` (`#5CB3FF`)
- `#3498DB` → should be `Tokens.BLUE_LIGHT` (`#60A5FA`)
- `#1ABC9C` → no direct token; closest is Tokens.WIN (`#4ADE80`) or a new teal token
- `#00CED1` → same as above
- `#E91E63` → should be `Tokens.PINK` (`#F472B6`)
- `#27AE60` → should be `Tokens.WIN_DARK` (`#22C55E`)
- `#9B59B6` → should be `Tokens.PURPLE` (`#C084FC`)
- `#E67E22` → should be `Tokens.ORANGE` (`#FB923C`)
- `#95A5A6` → should be `Tokens.SLATE` (`#94A3B8`)
**Fix:** Replace fallback hex values with Tokens constants.

### C2 — [P3] highlight_renderer.py: Inline styles instead of CSS classes
**File:** `casino/renderer/highlight_renderer.py` (entire file)
**Issue:** Every element uses inline `style=` attributes with hardcoded values instead of CSS classes referencing token variables. The `_commentary_html()`, `_footer_html()`, and all card body builders use inline styles. This works but is harder to maintain and theme.
**Note:** The file uses `var(--token)` CSS variables in the inline styles, which is the correct pattern even if inline. This is cosmetic debt, not a bug.

### C3 — [P2] atlas_colors.py vs atlas_style_tokens.py: Color sync check
**File:** `atlas_colors.py`, `atlas_style_tokens.py`
**Issue:** Cross-reference check:
- `AtlasColors.CASINO` = `0xD4AF37` ↔ `Tokens.GOLD` = `#D4AF37` ✅ Match
- `AtlasColors.TSL_DARK` = `0x0A0A0A` ↔ `Tokens.BG_DEEP` = `#0A0A0A` ✅ Match
- `AtlasColors.TSL_GOLD` = `0xD4AF37` ↔ `Tokens.GOLD` = `#D4AF37` ✅ Match
- `AtlasColors.SUCCESS` = `0x34A853` vs `Tokens.WIN` = `#4ADE80` — **Mismatch** (embed green ≠ card green)
- `AtlasColors.ERROR` = `0xEA4335` vs `Tokens.LOSS` = `#F87171` — **Mismatch** (embed red ≠ card red)
- `AtlasColors.WARNING` = `0xFBBC04` vs `Tokens.PUSH` = `#FBBF24` — **Close but different**
**Assessment:** The embed colors (AtlasColors) are for Discord embeds which render differently than HTML cards. Using identical values would look wrong in Discord's embed renderer. These are intentionally different palettes for different rendering contexts. **No action needed.**

### C4 — [P3] card_renderer.py: Trade card has own CSS rather than using shared classes
**File:** `card_renderer.py:265-609`
**Issue:** The trade card defines ~350 lines of custom CSS. However, it correctly uses `var(--token)` variables throughout and calls `wrap_card()` for the shell. The custom CSS is necessary because trade cards have a fundamentally different layout (matchup, asset columns, fairness bar) that can't reuse the standard casino card classes.
**Assessment:** Correct usage of the pipeline. No bypass.

### C5 — [P3] ledger_renderer.py: Duplicate _esc() wrapper
**File:** `casino/renderer/ledger_renderer.py:168-170`
**Issue:** Defines `_esc(text)` which just calls `esc(text)` — redundant wrapper. Also in `card_renderer.py:46-48`.
**Fix:** Remove the wrapper, use `esc()` directly.

### C6 — [P2] highlight_renderer.py: Dual status bar (see B7)
Cross-referenced from B7. The highlight renderer builds its own status bar AND gets one from `wrap_card()`.

### C7 — [P3] format_utils.py: Minimal utility — only contains `fmt_volume()`
**File:** `format_utils.py`
**Issue:** Single-function module. No duplication with `atlas_style_tokens.py` (which has no formatting functions). Clean.

### C8 — [P3] embed_helpers.py: Depends on `constants.py` (ATLAS_GOLD, ATLAS_ICON_URL)
**File:** `embed_helpers.py:13`
**Issue:** Imports from `constants` module (not in audit scope). No duplication with tokens — `embed_helpers` is for Discord embeds, `atlas_style_tokens` is for HTML cards. Clean separation.

---

## OUT-OF-SCOPE FLAGS

### O1 — casino_db.py: `create_crash_round()` generates crash_point — not audited
The crash point generation and seeding logic is in `create_crash_round()` which was read but the RNG seeding mechanism should be verified separately. The seed is stored in DB and the crash point uses `hashlib` + `secrets` — appears sound but needs cryptographic review.

### O2 — flow_wallet.py: `debit()` and `credit()` atomicity
`casino_db.deduct_wager()` correctly wraps `flow_wallet.debit()` inside `BEGIN IMMEDIATE` with user lock. The wallet-level atomicity was not audited (out of scope).

### O3 — sportsbook_cards.py / flow_cards.py: Hub card renderers
Referenced in `casino.py:393-394` but not in this terminal's scope.

### O4 — polymarket_cog.py: Source of truth for CATEGORY_COLORS_HEX
The prediction renderer's fallback dict should match `polymarket_cog.CATEGORY_COLORS_HEX`. That file is out of scope.

### O5 — flow_wallet.py: `get_theme_for_render()` function
Called by every game module but defined outside scope.

---

## FIXES APPLIED

### Fix 1 — B1: play_again.py — Race condition on interaction response (P1)
**Files:** `casino/play_again.py`
**`_on_play` (line ~84):** Moved `get_balance()` check BEFORE any `interaction.message.edit()` call. Removed the pre-edit button disable/re-enable dance. Now: check balance → if insufficient, send ephemeral error via `interaction.response` (which hasn't been consumed yet) → if sufficient, disable all and launch game. The old code consumed the interaction response with `edit_message`, then tried to use `interaction.response.send_message` for the error — which would throw `InteractionResponded`.
**`_on_double` (line ~117):** Same fix applied. Balance check moved before interaction consumption.

### Fix 2 — B3/C1: prediction_html_renderer.py — Foreign blue palette (P1/P2)
**File:** `casino/renderer/prediction_html_renderer.py:30-41`
**Change:** Replaced all hardcoded foreign hex values in the fallback `CATEGORY_COLORS_HEX` dict with `Tokens.*` constants from `atlas_style_tokens.py`. Added `from atlas_style_tokens import Tokens` import.
**Mapping:** `#5B9BD5`→`Tokens.JEWEL_PURPLE`, `#3498DB`→`Tokens.JEWEL_PURPLE`, `#FF69B4`→`Tokens.PINK`, `#E91E63`→`Tokens.PINK`, `#27AE60`→`Tokens.WIN_DARK`, `#9B59B6`→`Tokens.PURPLE`, `#1ABC9C`→`Tokens.JEWEL_BLUE`, `#00CED1`→`Tokens.JEWEL_BLUE`, `#E67E22`→`Tokens.ORANGE`, `#95A5A6`→`Tokens.SLATE`.

### Fix 3 — B5: atlas_html_engine.py — Page pool browser recovery (P0)
**File:** `atlas_html_engine.py:601-616`
**Change:** Added `try/except` to `PagePool._new_page()`. If page creation fails (browser crashed/disconnected), it now calls `close_browser()` to clean up the stale singleton, then `_get_browser()` to reconnect, then retries page creation once. This prevents the pool from getting poisoned with dead pages when Chromium OOM-kills or disconnects.

### Fix 4 — B6: crash.py — Multiplier curve cap alignment (P1)
**File:** `casino/games/crash.py:125`
**Change:** Replaced hardcoded `100.0` cap in `_current_multiplier()` with `MAX_CRASH_MULTIPLIER` constant (1000.0). The curve now uses the same cap as the cashout logic, preventing theoretical infinite-round scenarios if a crash point > 100 is generated.

### Fix 5 — B7: highlight_renderer.py — Dual status bar (P1)
**File:** `casino/renderer/highlight_renderer.py:109-144`
**Change:** Removed the inline `<div style="height:5px;...">` status bar from `inner_html`. Instead, the highlight-specific gradient CSS is now injected as a `<style>.status-bar { ... }</style>` override inside the card body, which overrides the engine's default `.status-bar` gradient. This means the engine's single `.status-bar` div renders with the highlight's custom gradient — one bar, correct color, no duplication.

### Fix 6 — C5: Redundant _esc() wrappers (P3)
**Files:** `casino/renderer/ledger_renderer.py:168-170`, `card_renderer.py:46-48`
**Change:** Replaced function-based `_esc()` wrappers with direct alias `_esc = esc`. No behavior change — all callsites still work via the alias. Eliminates unnecessary indirection.
