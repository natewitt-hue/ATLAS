# Adversarial Review: pulse_renderer.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 416
**Reviewer:** Claude (delegated subagent)
**Total findings:** 10 (1 critical, 3 warnings, 6 observations)

## Summary

Pulse dashboard HTML→PNG renderer for the Flow Live engagement cog. The Ring 1 review flagged that the pulse jackpot last-winner shows a raw Discord snowflake — this renderer is confirmed to be the pass-through display point, though the root cause is in the caller (`flow_live_cog.py:611`). Also: no input validation on integer fields (crash risk on None), `_NOISE_SVG` is defined but unused (dead code), and the refresh interval is caller-supplied with no bounds checking.

## Findings

### CRITICAL #1: Raw Discord snowflake passed through `jackpot_last_player` to visible render

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:169-176,171`
**Confidence:** 0.90
**Risk:** Line 171 renders `jackpot_last_player` directly via `esc(data.jackpot_last_player)` inside the jackpot hero. The caller (`flow_live_cog.py:590-612`) reads `winner_row[0]` from the `casino_jackpots` table — which is a `discord_id` (a snowflake integer stored as int or text in SQLite). There is NO `guild.get_member(...)` resolution in the caller; it just does `data["last_player"] = str(winner_row[0])`. The renderer faithfully renders whatever it is handed, so the image shows "Last hit: 829384723948473928 won $X,XXX" as raw digits.
**Vulnerability:** The renderer is a downstream display point for a privacy leak that originates upstream. Per `CLAUDE.md`: "Identity resolution: API usernames have underscores/case mismatches. Use `_resolve_owner()` fuzzy lookup" — the same principle applies to Discord IDs → display names. The renderer SHOULD either (a) reject a numeric-string `jackpot_last_player` with a sanity check, or (b) document that the caller MUST resolve to a display name.
**Impact:** Public channel exposure of raw Discord snowflakes. Users can cross-reference snowflakes via Discord's developer mode to identify accounts. Minor privacy leak; primary concern is the aesthetic regression.
**Fix:** Add a validation step in `_build_pulse_html`: `if data.jackpot_last_player and data.jackpot_last_player.isdigit(): data.jackpot_last_player = f"Player #{data.jackpot_last_player[-4:]}"` as a defensive fallback. Document the caller contract: `jackpot_last_player` must be a resolved display name. The PROPER fix is in `flow_live_cog.py:609` — resolve the winner snowflake via `guild.get_member(int(data["last_player"])).display_name`.

### WARNING #1: `data.active_bj`, `data.slots_spins_today`, `data.sb_bets`, `data.pred_open` — no type coercion

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:312-313,332,354,373`
**Confidence:** 0.75
**Risk:** All integer-typed dashboard stats are rendered via f-string interpolation (`{data.active_bj}` etc). If any caller passes None (e.g., a DB query returned NULL for `slots_spins_today` on a fresh day), the f-string produces `"None"` in the card, breaking the visual and making the number meaningless. Line 313 is `{data.active_bj} active` — a literal "None active" on empty state.
**Vulnerability:** No defensive coercion. The dataclass defines `active_bj: int` but dataclasses don't enforce types at runtime.
**Impact:** Fresh-install empty state shows literal "None" instead of "0".
**Fix:** Add coercion in `_build_pulse_html` OR use `int(data.active_bj or 0)` for each integer field at interpolation time.

### WARNING #2: `refresh_interval` is caller-controlled with no validation

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:166,396,413-415`
**Confidence:** 0.50
**Risk:** `refresh_interval: int = 60` is injected into the HTML as `Updates every {refresh_interval}s`. If the caller passes 0 or a negative value, the card displays "Updates every 0s" (meaningless) or "Updates every -30s" (broken). No bounds check.
**Vulnerability:** Weak input validation on a cosmetic field.
**Impact:** Display-only issue.
**Fix:** Clamp to a reasonable range: `refresh_interval = max(10, min(3600, refresh_interval))`.

### WARNING #3: `_week_label(data.sb_week, short=True)` called with `sb_week=0` by upstream

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:351,649`
**Confidence:** 0.70
**Risk:** `flow_live_cog.py:649` passes `sb_week=0` as a placeholder for the sportsbook section. `_week_label(0, short=True)` may return an empty string, "W0", or raise depending on the implementation in `data_manager`. Without seeing that function I cannot be sure, but "Week 0" is semantically invalid — the caller is using 0 as a sentinel for "no data", which the renderer does not detect.
**Vulnerability:** Sentinel value leaked into display logic.
**Impact:** Header badge may show "W0" or an empty box.
**Fix:** Check `if data.sb_week > 0:` in the renderer and substitute "—" otherwise.

### OBSERVATION #1: `_NOISE_SVG` constant defined but never referenced

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:131-136`
**Confidence:** 0.95
**Risk:** The module defines `_NOISE_SVG` as a data URL for a fractal noise background but it is never used anywhere in `_build_pulse_html`. Dead code.
**Vulnerability:** Developer clutter.
**Impact:** None functional.
**Fix:** Remove.

### OBSERVATION #2: `pred_yes_pct + pred_no_pct != 100` not validated

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:216`
**Confidence:** 0.55
**Risk:** `YES {data.pred_yes_pct}% · NO {data.pred_no_pct}%` is rendered verbatim. If the upstream computation has rounding drift (e.g., 51% + 48% = 99%), the display shows an inconsistent total. Worse, if the caller sends `(75, 50)` by mistake, the card shows "YES 75% · NO 50%" which is visibly wrong.
**Vulnerability:** No invariant check.
**Impact:** Display drift on data bugs.
**Fix:** Assert `pred_yes_pct + pred_no_pct in (99, 100, 101)` and raise or log if out of bounds.

### OBSERVATION #3: Highlight row count is hardcoded to 6 upstream but no cap in renderer

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:139-163`
**Confidence:** 0.50
**Risk:** The renderer accepts arbitrarily many `highlights` and renders all of them. If the caller passes 100 rows (e.g., a debugging pass), the card height explodes. `atlas_html_engine.render_card` uses bounding-box clipping so the image still renders, but at absurd dimensions (possibly truncated by Playwright's max-height limit).
**Vulnerability:** No defensive cap.
**Impact:** Large dashboard images on caller mistake.
**Fix:** `for h in highlights[:10]:` — enforce the visual budget at the renderer.

### OBSERVATION #4: `HighlightRow.description_html` is raw HTML — XSS surface

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:30,157`
**Confidence:** 0.45
**Risk:** `description_html` is a pre-formatted HTML string (comment says "pre-formatted HTML with colored spans") that is interpolated directly into the card without `esc()`. If a player's display name contains HTML tags (e.g., `<script>` or a `</div>` that breaks layout), the caller must remember to escape it. `flow_live_cog.py:631` does `<span style="color:var(--gold);">{name}</span>` with NO escape on `name`.
**Vulnerability:** Display-layer XSS — not a remote-code-execution risk since Playwright renders to image, but it CAN break the layout (CSS injection) or leak unintended content.
**Impact:** A crafted Discord display name (e.g., `</div><div style="position:fixed;top:0;left:0;width:100%;height:100%;background:red">`) could fully obscure the card.
**Fix:** The renderer should accept `description: str` (plain text) plus structured highlight metadata, and build the HTML itself with proper escaping. The current "caller owns the HTML" contract is a security smell.

### OBSERVATION #5: `sb_volume = 0` sentinel leaks into footer display

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:226`
**Confidence:** 0.50
**Risk:** `footer_won = f"${data.sb_volume:,}" if data.sb_volume else "$0"`. The check `if data.sb_volume` treats 0 as falsy and shows "$0", which is fine. But semantically this should show "N/A" or "—" when there is no data vs when there IS data but volume is zero. Caller cannot distinguish "no bets today" from "all bets voided".
**Vulnerability:** Sentinel confusion.
**Impact:** Ambiguous display.
**Fix:** Accept `sb_volume: int | None = None` and distinguish.

### OBSERVATION #6: `@dataclass` with 20+ required positional fields is fragile

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/pulse_renderer.py:36-73,75-126`
**Confidence:** 0.60
**Risk:** `PulseData` has 20+ fields with no defaults, and `build_pulse_data` is a factory with 20+ kwargs that just forwards them 1:1 to the dataclass. This is pure boilerplate. If a new field is added to the dataclass without updating the factory, the factory silently drops it — but if added to the factory without the dataclass, a TypeError fires at instantiation. Tightly coupled with high maintenance cost.
**Vulnerability:** Refactor friction.
**Impact:** None runtime; high maintenance burden.
**Fix:** Either (a) make `build_pulse_data` a thin alias to `PulseData(**kwargs)`, or (b) drop the factory and let the caller call `PulseData(...)` directly. The current middle-ground wastes code.

## Cross-cutting Notes

The critical finding about the raw snowflake display is the same concern Ring 1 flagged for `flow_live_cog.py` — this confirms that the renderer IS the display point, but the bug originates in the caller which fails to resolve the winner ID to a display name. The fix belongs in the caller (`flow_live_cog.py:590-612`), not in the renderer. However, the renderer should have a defensive check for all-digit strings as a second layer.

The `description_html` passing pattern is a broader architectural concern: several ATLAS renderers accept pre-formatted HTML fragments instead of structured data, relying on each caller to sanitize user-controlled strings. This convention breaks `esc()`'s purpose of providing a single escape point. A future hardening pass should refactor all "pass HTML" parameters into "pass structured data + plain text, renderer builds HTML".
