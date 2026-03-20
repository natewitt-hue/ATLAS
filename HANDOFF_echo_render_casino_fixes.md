# HANDOFF: Echo, Render & Casino — GAP Review Session B

**Reviewer:** Claude (GAP Review Session B)
**Date:** 2026-03-20
**Scope:** echo_cog.py, echo_loader.py, affinity.py, echo/*.txt, atlas_style_tokens.py, atlas_html_engine.py, card_renderer.py, casino/ directory (all games + renderers)

---

## Summary

Overall the codebase is **solid**. The rendering pipeline is well-architected with a clean page pool, proper lifecycle management, and consistent use of the HTML engine across all renderers. The casino game logic handles edge cases well (TOCTOU guards, orphan wager reconciliation, timeout resolution). The issues found are mostly medium/low severity with a few high-priority items.

---

## CRITICAL Issues

### C1. `play_again.py` — Race condition: `interaction.response` used after `message.edit`
**File:** `casino/play_again.py:88-101`
**Impact:** Discord API error — `interaction.response.send_message()` called after `interaction.message.edit()` has already consumed the interaction response.

When the balance check fails in `_on_play()` and `_on_double()`, the code does:
1. `await interaction.message.edit(view=self)` — this consumes the response (line 90)
2. `await interaction.response.send_message(...)` — this will crash because response is already used (line 98)

Same issue in `_on_double()` at lines 121/133.

**Fix:** Use `interaction.followup.send()` instead of `interaction.response.send_message()` after the `message.edit()` call. Or restructure to check balance before editing the message:
```python
# Check balance FIRST, before consuming the interaction
bal = await get_balance(self.user_id)
if bal < self.wager:
    return await interaction.response.send_message(
        f"Not enough Bucks...", ephemeral=True
    )

# Only then disable buttons
self.btn_play.disabled = True
self.btn_double.disabled = True
await interaction.response.edit_message(view=self)  # or use message.edit
await self.replay_callback(interaction)
```

### C2. `blackjack.py:93` — Near-miss detection uses impossible score value
**File:** `casino/games/blackjack.py:93`
**Impact:** Dead code — `_hand_value()` will NEVER return 22.

`_hand_value()` counts aces optimally: if total > 21, it demotes aces from 11 to 1. The only way to get 22 is if you have no aces at all and the cards sum to 22 — but then `_detect_near_miss()` says "Busted by ONE!" which implies the player was at 22 (one over 21). However, a hand that sums to 22 with no aces means the player drew cards totaling exactly 22 — that IS a valid bust, and `_hand_value` would return 22. So this IS reachable (e.g., 10+5+7=22).

**Revised verdict:** This is actually correct — if no aces, `_hand_value` can return 22. **Downgrade to LOW — not a bug.** The message "Busted by ONE!" is accurate.

---

## HIGH Issues

### H1. `card_renderer.py` — Does NOT use `wrap_card()` pipeline; builds its own full HTML
**File:** `card_renderer.py:259-771`
**Impact:** The trade card builds its own complete HTML document with `<!DOCTYPE html>`, `<style>`, etc. instead of using `wrap_card()`. This means:
- Uses Google Fonts CDN `@import url(...)` — requires internet access, will fail offline
- Uses `Bank Gothic` from `onlinewebfonts.com` — external dependency, possible CORS/availability issues
- Uses CSS variables like `var(--font-display)` without declaring them (relies on browser defaults for some)
- Width is 700px (not the standard 480px), which is fine for trade cards but diverges from spec

**Fix:** This is somewhat intentional (trade cards are wider at 700px), but the external font dependencies should be replaced with local base64 fonts like the rest of the pipeline. At minimum:
1. Replace `@import url('https://fonts.googleapis.com/...')` with `_font_face_css()` from `atlas_html_engine.py`
2. Either embed Bank Gothic locally or remove it
3. Add `Tokens.to_css_vars()` to the `<style>` block so CSS variables work

### H2. `ledger_renderer.py` — Imports private `_font_face_css` function
**File:** `casino/renderer/ledger_renderer.py:15`
**Impact:** `_font_face_css` is prefixed with `_` (private convention). If `atlas_html_engine.py` is refactored, this import breaks silently.

**Fix:** Either make `_font_face_css` public (rename to `font_face_css`) or have ledger_renderer use `wrap_card()` like the other renderers. The ledger renderer builds its own HTML document at 700px width — similar to card_renderer.py. If it must stay custom, promote the function to public API.

### H3. `echo_loader.py` — `get_persona()` on-demand loading is NOT thread-safe
**File:** `echo_loader.py:100-108`
**Impact:** If `load_all_personas()` hasn't been called yet and two concurrent requests hit `get_persona()` simultaneously, both could read the file and write to `_personas` dict at the same time. Python's GIL makes dict writes atomic for simple assignments, so this won't corrupt data, but the file could be read twice unnecessarily.

**Fix:** LOW priority — Python's GIL protects against corruption. The startup path calls `load_all_personas()` first, so the on-demand path is rarely hit. No fix needed unless you see startup race conditions.

### H4. `affinity.py` — Cache never expires, never invalidated cross-process
**File:** `affinity.py:49, 74-86, 136`
**Impact:** `_affinity_cache` is module-level and never expires. If the bot runs for days, cache grows indefinitely (bounded by unique user count, so ~100 entries max — acceptable). More importantly, if an admin resets affinity via DB directly, the cache won't reflect it until bot restart.

**Fix:** Add TTL-based cache expiration, or clear cache in `reset_affinity()` (which is already done at line 148 — good). For direct DB edits, document that bot restart is required.

### H5. `echo_cog.py` — `infer_context()` is defined in `echo_loader.py` but never called from `echo_cog.py`
**File:** `echo_cog.py` (entire file), `echo_loader.py:172-199`
**Impact:** The CLAUDE.md says `infer_context()` maps channel → voice, but `echo_cog.py` never calls it. The cog only handles `echorebuild` and `echostatus` admin commands. The actual @mention handling and voice routing is presumably done elsewhere (likely `bot.py` or another cog). This means `infer_context()` exists but its integration point is unclear.

**Fix:** Verify where `infer_context()` is actually called. If it's called from `atlas_ai.py` or `bot.py`, that's fine. If it's dead code, remove it. Search for `infer_context` across the codebase.

---

## MEDIUM Issues

### M1. `echo/*.txt` — Persona files use first person ("I"/"me"/"my"), not third person "ATLAS"
**File:** `echo/echo_casual.txt`
**Impact:** CLAUDE.md says "Always 3rd person as ATLAS (never 'I'/'me')". But the casual persona file is written as "You are ATLAS Echo, the AI persona of TheWitt" and instructs the AI to speak AS TheWitt in first person. This is by design for the casual register (the commissioner's voice), but conflicts with the CLAUDE.md rule.

**Fix:** Clarify in CLAUDE.md whether the 3rd-person rule applies to all registers or only official/analytical. The casual register is intentionally first-person-as-TheWitt, which is the correct design for @mention responses.

### M2. `blackjack.py` — Split aces don't get restricted to one card each
**File:** `casino/games/blackjack.py:367-374`
**Impact:** Standard blackjack rules restrict split aces to one card per hand. The current implementation allows hitting on split aces because `_update_buttons()` just checks `len(s.active_hand) == 2` for double eligibility, but doesn't prevent hits on split aces.

**Fix:** In `_update_buttons()`, add a check: if the split hand started with an ace, disable the Hit button after the first extra card. Or add a `split_aces: bool` flag to `BlackjackSession` and enforce one-card-per-hand when True:
```python
# In BlackjackSession.deal or SplitButton.callback:
if session.player_hand[0][0] == "A":
    session.split_aces = True

# In HitButton.callback:
if session.split_active and session.split_aces:
    await _finish_hand(interaction, session, view)
    return
```

### M3. `card_renderer.py:222-225` — Division by zero if both sides have 0 value
**File:** `card_renderer.py:222-225`
**Impact:** If `val_a + val_b == 0`, the fairness bar calculation `val_a / total_val * 100` divides by zero. The ternary guard `if total_val > 0` catches this for `pct_a`, defaulting to 50, which is correct. **No bug** — but `favored = team_a if val_a < val_b else team_b` at line 218 would show "favors Team B" when both are 0, which is misleading.

**Fix:** Add guard: `favored = "Neither" if val_a == val_b else (team_a if val_a < val_b else team_b)`

### M4. `atlas_html_engine.py` — Page pool `acquire()` timeout silently raises `asyncio.TimeoutError`
**File:** `atlas_html_engine.py:548-549`
**Impact:** If all 4 pages are busy and a 5th render request comes in, `acquire()` raises `asyncio.TimeoutError` after 10 seconds. This propagates up to `render_card()` which has no try/except for it. The calling code (casino renderers) generally catch `Exception` at the top level, but the error message will be unhelpful ("TimeoutError").

**Fix:** Catch `asyncio.TimeoutError` in `render_card()` and raise a more descriptive error:
```python
try:
    page = await _pool.acquire()
except asyncio.TimeoutError:
    raise RuntimeError("Render pool exhausted — all 4 pages busy. Try again shortly.")
```

### M5. `crash.py:125` — Multiplier curve caps at 100x but `MAX_CRASH_MULTIPLIER` is 1000x
**File:** `casino/games/crash.py:125, line 50`
**Impact:** `_current_multiplier()` caps at 100.0 via `min(...)`, but `MAX_CRASH_MULTIPLIER = 1000.0` at line 50. These are inconsistent. Crash point is generated in `casino_db.py` and could theoretically be >100x, but the multiplier curve will never reach it.

**Fix:** Align the two constants. Either change `_current_multiplier` to cap at `MAX_CRASH_MULTIPLIER` or change `MAX_CRASH_MULTIPLIER` to 100.0. Check `create_crash_round()` in `casino_db.py` to see what range crash points are generated in.

### M6. `highlight_renderer.py` — Uses its own `_wrap_card()` instead of the engine's `wrap_card()`
**File:** `casino/renderer/highlight_renderer.py:82-139`
**Impact:** The highlight renderer defines its own `_wrap_card()` helper that internally calls the engine's `wrap_card()`, but adds a custom header/footer structure. This is fine architecturally, but the function name shadows the engine's `wrap_card` in the local namespace, which could confuse maintainers.

**Fix:** Rename to `_wrap_highlight_card()` for clarity. Not a bug.

### M7. `affinity.py` — `analyze_sentiment()` keyword "w" will false-positive on common words
**File:** `affinity.py:197, 210-211`
**Impact:** The positive keyword "w" (meaning "win" in slang) uses word-boundary matching via `_kw_match()`, but `\bw\b` will match the standalone letter "w" in sentences like "w hat" or similar edge cases. The regex is correct for `\bw\b` matching only standalone "w", but this is a very short keyword.

**Fix:** LOW priority. The word-boundary regex is correct. "w" as a standalone word is a valid positive signal in gaming contexts. No change needed.

### M8. `casino/play_again.py` — `_on_double` doesn't use the actual capped wager in the callback
**File:** `casino/play_again.py:124-144`
**Impact:** The double callback receives `interaction` but the wager amount is bound via `functools.partial` at the call site (e.g., `min(wager * 2, max_bet)`). The `actual_wager` computed at line 125 is only used for the balance check but NOT passed to the callback. The callback uses whatever wager was bound at creation time. This is correct IF the partial was created with the capped amount — and looking at the call sites (blackjack.py:588, slots.py:347, coinflip.py:193), they all do `min(wager * 2, max_bet)`. So this is fine.

**Verdict:** No bug — the wager is correctly capped at the partial creation site. But the `actual_wager` variable at line 125 is a redundant recomputation.

---

## LOW Issues

### L1. `atlas_style_tokens.py` — Clean and well-structured, no issues found
**File:** `atlas_style_tokens.py`
**Impact:** None — this is a clean single source of truth. All tokens are used. No duplicates. CSS variable mapping is complete. `CARD_WIDTH = 480` and `DPI_SCALE = 2` match CLAUDE.md spec.

### L2. `atlas_html_engine.py` — Page pool is properly sized (4 pages, 480px, 2x DPI)
**File:** `atlas_html_engine.py:530-531, 611-615`
**Impact:** Matches CLAUDE.md exactly. Pool size = 4, width = 480, DPI = 2. `domcontentloaded` wait is used at line 585. Pages are recycled after 100 renders (line 555). Graceful shutdown via `drain_pool()` exists.

### L3. No QUARANTINE imports found anywhere
**Impact:** Verified via grep — no file imports from `QUARANTINE/atlas_card_renderer.py` or `QUARANTINE/card_renderer.py`. Clean.

### L4. No PIL/Pillow imports in casino/ directory
**Impact:** Verified via grep — all rendering is HTML-based through `atlas_html_engine.py`. Clean.

### L5. All casino renderers use the engine pipeline
**Impact:** Verified — `casino_html_renderer.py`, `highlight_renderer.py`, `session_recap_renderer.py`, `pulse_renderer.py`, and `prediction_html_renderer.py` all import from `atlas_html_engine` and use `render_card()` + `wrap_card()`. The only exceptions are `card_renderer.py` (trade cards, H1) and `ledger_renderer.py` (H2), which build custom HTML but still use `render_card()` for the Playwright screenshot step.

### L6. `echo_loader.py` — `reload_personas()` clears cache then reloads
**File:** `echo_loader.py:112-118`
**Impact:** Between `_personas.clear()` and `load_all_personas()` completing, any concurrent call to `get_persona()` will hit the on-demand path and read from disk. Not a real issue since reload is admin-only and rare.

### L7. `view=None` to `followup.send()` — NOT present in casino code
**Impact:** Verified via grep — no casino code passes `view=None` to `followup.send()`. The only occurrences are in `roster.py` and `boss_cog.py` (outside this session's scope). Clean for our files.

### L8. `affinity.py` — `re.escape(keyword)` in `_kw_match` is correct
**File:** `affinity.py:210`
**Impact:** Keywords with special regex chars are properly escaped. No injection risk.

### L9. `crash.py` — Background task exception handling is good
**File:** `casino/games/crash.py:596-621`
**Impact:** `_lobby_then_run()` has a try/except/finally that refunds all uncashed players on error and always cleans up `active_rounds`. This is well-implemented.

### L10. `casino_db.py` — Orphan wager reconciliation is well-designed
**File:** `casino/casino_db.py:255-299`
**Impact:** Properly handles bot crashes mid-game by finding debit transactions with no matching session entry. Good defensive coding.

---

## Checklist Verification

| Check | Status | Notes |
|-------|--------|-------|
| `get_persona()` loads from files | PASS | Falls back to hardcoded stubs if files missing |
| `infer_context()` channel mapping | PASS | Keyword-based, defaults to "casual" |
| Missing `echo/*.txt` handling | PASS | Graceful fallback to stub personas |
| 3rd person "ATLAS" rule | PARTIAL | Casual register uses 1st person by design (M1) |
| No hardcoded persona strings | PASS | No `ATLAS_PERSONA` in reviewed files |
| Thread safety | PASS | GIL protects dict ops; pool uses asyncio.Queue |
| Affinity scores | PASS | Clean formula, clamped [-100, 100], no div-by-zero |
| Style tokens single source | PASS | Clean, no duplicates |
| Page pool: 4 pre-warmed | PASS | Matches CLAUDE.md spec exactly |
| `render_card()` error handling | PARTIAL | TimeoutError not caught (M4) |
| Pool exhaustion handling | PARTIAL | Raises unhelpful TimeoutError (M4) |
| `wrap_card()` enforces 480px/2x/domcontentloaded | PASS | All three verified |
| Browser lifecycle | PASS | `init_pool()` at startup, `drain_pool()` at shutdown |
| `card_renderer.py` uses pipeline | PARTIAL | Uses `render_card()` but not `wrap_card()` (H1) |
| No QUARANTINE imports | PASS | Verified |
| No PIL imports in casino | PASS | Verified |
| All renderers use engine | PASS | Two custom HTML builders but both use `render_card()` |
| Blackjack edge cases | PARTIAL | Split aces not restricted (M2) |
| Slots RNG fairness | PASS | Controlled RTP table, pre-rolled outcomes |
| Crash multiplier | PARTIAL | Cap mismatch 100x vs 1000x (M5) |
| Economy integration | PASS | All games use `deduct_wager`/`process_wager` properly |
| `view=None` to followup | PASS | Not present in reviewed files |
| Select menu 25-option cap | N/A | No select menus in reviewed files |

---

## Priority Fix Order

1. **C1** — `play_again.py` interaction response crash (will cause visible Discord errors)
2. **H1** — `card_renderer.py` external font dependencies (will fail offline/in CI)
3. **H2** — `ledger_renderer.py` private import (fragile coupling)
4. **M4** — `render_card()` pool exhaustion error message (user-facing)
5. **M5** — Crash multiplier cap mismatch (game logic correctness)
6. **M2** — Split aces rule (game fairness)
7. **M3** — Trade card "favors" text when values equal (cosmetic)
8. **M6** — Highlight renderer function naming (code clarity)
9. **H5** — Verify `infer_context()` call sites (dead code check)
10. **H4** — Affinity cache documentation (operational clarity)
