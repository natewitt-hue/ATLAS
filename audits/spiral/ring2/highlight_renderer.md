# Adversarial Review: casino/renderer/highlight_renderer.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 714
**Reviewer:** Claude (delegated subagent)
**Total findings:** 12 (0 critical, 5 warnings, 7 observations)

## Summary

Thin HTML-string builder over `atlas_html_engine.render_card`/`wrap_card`. No DB access, no wallet calls, no state, and pages are held only inside `render_card` (which has its own try/finally). Primary risks are correctness on the edges: fragile `leg_picks` dict contract without key guards, a `.status-bar` CSS override injection that crosses the theme boundary and clobbers Ring 1 theme status gradients, and responsibility-leak for the Ring 1 "raw 18-digit Discord ID" bug sitting one layer upstream in `flow_live_cog.py`. No data-loss / financial ledger exposure in this file.

## Findings

### WARNING #1: `.status-bar` override silently wins over theme `status_gradient`

**Location:** `casino/renderer/highlight_renderer.py:112-116` (and all 5 callers: `:206, :334, :419, :526, :691`)
**Confidence:** 0.90
**Risk:** Every highlight card emits `<style>.status-bar { <hard-coded gradient> }</style>` inside the card body. `atlas_html_engine.wrap_card` (lines 570-572) renders the top bar with an inline `style="background:<theme_status_gradient>;..."` attribute when a theme is active. However this highlight-local override ships a *second* style block that lives *after* `_SHARED_CSS` in the document head ordering because `wrap_card` injects the card body *inside* `<body>`, and the inline `<style>` tag inside the body still applies globally and has identical specificity. The inline `style="background:..."` attribute from the theme code will still win (inline beats stylesheet), so the *theme* gradient actually survives — but only because of CSS specificity accident, not design. If anyone refactors `wrap_card` to emit `.status-bar` as a class-based gradient (not inline style), every highlight card instantly reverts to the hard-coded non-themed gradient and themes break silently across Jackpot / PvP / Crash / Prediction / Parlay cards simultaneously.
**Vulnerability:** The file constructs cross-module CSS from the *body* to override a rule defined by its caller. It does not use the theme-aware `wrap_card(status_class=...)` parameter that `atlas_html_engine` exposes for exactly this purpose.
**Impact:** Silent theme regression on all 5 highlight card types after any future refactor of `wrap_card`'s status bar rendering. Users on custom themes see stock gold/green/red bars, bug invisible until a bored eye notices.
**Fix:** Delete `status_override_css`. Replace `status_bar_css=` at every call site with `status_class=` passing one of the documented tokens (`"jackpot"`, `"win"`, `"loss"`) and thread it through `_wrap_card` to `wrap_card(..., status_class=...)`. For card types that need a non-standard gradient (prediction uses raw `{res_color}`), extend `wrap_card` with a named status or accept `status_class="win"`/`"loss"` instead of inlining CSS.

### WARNING #2: `_format_leg_label` assumes all required dict keys exist

**Location:** `casino/renderer/highlight_renderer.py:553-564` and `:582-586`
**Confidence:** 0.85
**Risk:** `lp["bet_type"]`, `lp["pick"]`, `lp["line"]`, `lp["status"]` are all raw subscript accesses. `flow_sportsbook.get_parlay_display_info` (line 3047) constructs the dicts from SQL rows and — per schema at `flow_sportsbook.py:199-212` — all four columns are `NOT NULL`, so the happy path is safe *today*. But a single migration drift (a column renamed, a legacy row inserted before `line REAL NOT NULL DEFAULT 0` was added, or any other producer feeding `render_parlay_card` with a shorter dict from tests/backfill/other cogs) raises `KeyError` mid-render. The caller in `flow_live_cog._post_instant_highlight:709-750` wraps everything in `except Exception: log.exception(...); return` which silently drops the highlight — the commissioner sees nothing in `#flow-live`, with only a log line to explain it.
**Vulnerability:** No defensive `.get(...)` with defaults, no validation of the dict shape at the public `render_parlay_card` entry point, and the contract is documented nowhere in the highlight module itself (you have to read `flow_sportsbook.py:3035`).
**Impact:** One-shot silent highlight drop after any schema change or legacy data surfacing. Hard to notice because parlay hits are rare events.
**Fix:** In `_format_leg_label`, use `lp.get("bet_type", "")`, `lp.get("pick", "")`, `lp.get("line", 0) or 0`, `lp.get("status", "")`. Add a docstring to `render_parlay_card` spelling out the required dict keys, or wrap the pills loop in `try/except KeyError` and drop the offending leg with a `log.warning` while still rendering the rest of the card.

### WARNING #3: Over/Under with `line == 0.0` yields bare `"O"`/`"U"` label

**Location:** `casino/renderer/highlight_renderer.py:556-558`
**Confidence:** 0.75
**Risk:** `return f"{bt[0]}{lp['line']:g}" if lp["line"] else bt` — when `lp["line"]` is exactly `0.0` (falsy), the short-circuit returns the full bet type (`"Over"` / `"Under"`). For a Moneyline or Spread that's fine, but for an Over/Under at line 0.0 (e.g. a defensive-shutout prop) the fallback is `"Over"` or `"Under"` with no side number — while for a Spread at 0.0 you already get `"KC"` (bare pick, also missing the "+0" pick-em annotation). The two branches disagree on how to display a "zero-line" edge case.
**Vulnerability:** Truthiness-testing a float field that legitimately carries 0.0. The schema (`line REAL NOT NULL DEFAULT 0`) means any pick that forgot to set the line value gets silently stamped with 0.0 and mis-labeled here.
**Impact:** Ugly / confusing display string on a subset of parlay pill legs. Not a data bug, but it makes the card look buggy — users screenshot it and report it as such.
**Fix:** Use `lp["line"] is not None` or `lp.get("line") not in (None,)` as the truthiness check, and distinguish an Over/Under at zero explicitly (`"O0"`), or fall back to joining `bet_type` + ` 0` for spreads.

### WARNING #4: `render_prediction_card` ships `theme_id=None` from the only real caller

**Location:** `casino/renderer/highlight_renderer.py:538-548` (signature) — real-call site at `flow_live_cog.py:743-747`
**Confidence:** 0.90
**Risk:** The prediction highlight path hard-codes `theme_id=None` upstream ("no individual user, pass None") which means prediction resolution highlights are the only card type that cannot be themed. This file accepts a `theme_id` parameter but the production caller never passes anything real. Either the parameter is dead in practice (and should be removed to avoid misleading future readers) or the caller is wrong (and should pick a guild-level or commissioner-level theme). Right now the file exports a theming hook that silently does nothing in production.
**Vulnerability:** Unused abstraction and misleading signature. A future dev looking at `render_prediction_card(..., theme_id=X)` and passing a real theme will discover at runtime that no live code path exercises it — brittle surface.
**Impact:** Prediction cards look inconsistent with all other highlight types when themes roll out. Also wastes Ring 2 theme splicing effort for this subsystem.
**Fix:** Either (a) document in the docstring that prediction cards are *intentionally* theme-neutral and drop the parameter, or (b) resolve a default theme at the caller (e.g. commissioner theme, guild theme, or the top-1 winner's theme) and thread it through.

### WARNING #5: `_format_leg_label` ignores unknown `bet_type` values

**Location:** `casino/renderer/highlight_renderer.py:553-564`
**Confidence:** 0.70
**Risk:** The three recognized branches are `Over`/`Under`, `Moneyline`, and implicit "Spread" fallback. If `bet_type` ever carries a new value (team total, player prop, first-half spread, etc. — some of which are already being added across the sportsbook module per `real_sportsbook_cog`/`espn_odds` landscape), the else branch produces `f"{lp['pick']} {line:+g}"` — which is a *spread-style* label applied to an unknown bet type. Silent data corruption in the display, not an exception.
**Vulnerability:** No default/unknown handling. Spread is implicit, not explicit.
**Impact:** New bet types ship as mislabeled "spread" pills until a manual QA catches it.
**Fix:** Make the Spread branch explicit (`if bt == "Spread": ...`) and add an `else: return f"{lp['pick']} ({bt})"` catch-all that preserves the bet type name.

### OBSERVATION #1: Ring 1 "raw 18-digit Discord ID" bug belongs to the caller, not this file

**Location:** `casino/renderer/highlight_renderer.py:218-227` (and `flow_live_cog.py:713-718`)
**Confidence:** 0.95
**Risk:** The prompt flagged a Ring 1 bug where the Jackpot card shows raw 18-digit Discord IDs instead of resolved member names. The renderer is innocent — it faithfully escapes whatever string lands in `player`. The bug is at `flow_live_cog.py:713-714` where `member = guild.get_member(event.discord_id)` returns `None` if the bot's member cache is cold or the member left the guild, and the fallback `player = member.display_name if member else str(event.discord_id)` then passes a raw snowflake ID downstream. This module simply escapes and renders it (line 201).
**Vulnerability:** Defensive responsibility lives upstream, but `render_jackpot_card` has no signature-level way to reject a snowflake-shaped input (`"12345678901234567890"`).
**Impact:** Not this file's bug, but this is where it is visually surfaced. Fix is in `flow_live_cog.py:713-714` — use `await guild.fetch_member(event.discord_id)` on cache miss, or resolve via `tsl_members` registry (`build_member_db.get_discord_id_for_db_username` inverse), or via `atlas_send._display_name` helper if that utility exists.
**Fix:** (For the Ring 1 fix in flow_live_cog:) call `fetch_member` on cache miss and fall back to `member.name` / db lookup before exposing a raw snowflake. (For defense in depth here:) in `render_jackpot_card`, detect an all-digit string > 15 chars and replace with `"Unknown player"`.

### OBSERVATION #2: Duplicate `_wrap_card` inline helper vs engine `wrap_card`

**Location:** `casino/renderer/highlight_renderer.py:82-146`
**Confidence:** 0.80
**Risk:** There are now two `wrap_card` functions in the codebase: `atlas_html_engine.wrap_card` (the official one) and this local `_wrap_card` (which calls the engine version at the end). The docstring says the local version is "intentionally distinct" because highlight cards need a different header/status-bar structure, but the difference is only the `status_bar_css` override + custom icon HTML — both of which could be parameters on the engine version. Two overlapping wrappers is a smell because any future change to `wrap_card` (theme plumbing, observability, error handling) must be mirrored or it quietly diverges.
**Vulnerability:** No shared test between the two, no type signature enforcement.
**Impact:** Any future Ring 1 change to the engine `wrap_card` silently skips all highlight cards because this file calls the engine but doesn't inherit its parameter evolution.
**Fix:** Extend `atlas_html_engine.wrap_card` with an optional `header=` and `status_class=` (or `status_override=`) parameter and delete this local copy. Or at minimum, add a failing assertion in `atlas_html_engine.wrap_card.__doc__` that references this consumer so future refactors remember it.

### OBSERVATION #3: Fragile string-based resolution check for YES/NO

**Location:** `casino/renderer/highlight_renderer.py:454-455`
**Confidence:** 0.85
**Risk:** `is_yes = resolution.upper().startswith("Y")` — any resolution value that starts with Y (e.g. `"YIELDED"`, `"Year-end"`, `"YTBD"`, `"Yes, but..."`, `"YOLO"`) is silently treated as YES. A resolution like `"Declined"` or `"N/A"` is correctly treated as NO, but the check is not robust against future prediction market types with more than two outcomes.
**Vulnerability:** String prefix matching instead of strict equality or enum lookup.
**Impact:** Wrong-colored resolution card on edge cases. Users see a green YES pill for a market that actually resolved otherwise.
**Fix:** `is_yes = resolution.strip().upper() == "YES"` with an explicit `elif resolution.strip().upper() == "NO": ...` and an `else` branch rendering a neutral "RESOLVED" pill for multi-outcome markets.

### OBSERVATION #4: No `NaN`/`inf` guard on `multiplier` / `amount`

**Location:** `casino/renderer/highlight_renderer.py:151-153, :360-362, :446-453`
**Confidence:** 0.65
**Risk:** `amount_str = f"${amount:,}"` and `mult_str = f"{multiplier:,.1f}x"` crash with `ValueError` on `float('nan')` or `float('inf')` if an upstream bug ever lets those leak in. `multiplier` in particular comes from Crash / Slots logic, where division-by-very-small-numbers is plausible. No `math.isfinite()` guard.
**Vulnerability:** Caller error handling (`flow_live_cog:748-749` `except Exception: log.exception(...); return`) catches it but the highlight is silently dropped.
**Impact:** Low-probability silent highlight-drop on a pathological multiplier.
**Fix:** Wrap in `if not math.isfinite(multiplier): multiplier = 0.0` at the top of each `_build_*_html`, or defer to a utility helper in `format_utils.py`.

### OBSERVATION #5: `_commentary_html` injects into `style` attribute without guarding `var(--...)` content

**Location:** `casino/renderer/highlight_renderer.py:35-53`
**Confidence:** 0.55
**Risk:** All CSS in the HTML is static — no interpolation from user-supplied fields into style attributes, so there's no CSS-injection surface. Good. However the commentary text is escaped via `esc()` which correctly prevents HTML injection, but the *structure* (italic, border-left gold) is fixed, meaning long multi-line commentary or commentary containing newlines will wrap awkwardly in the small 12px container.
**Vulnerability:** No length cap on commentary and no newline-to-`<br>` translation; HTML escape collapses newlines to raw `\n` in the rendered DOM (invisible).
**Impact:** Multi-sentence ATLAS commentary renders as one run-on line. Minor visual bug.
**Fix:** Cap commentary to N chars (e.g. 200) with ellipsis, or replace `\n` with `<br>` *before* escaping (need a custom multiline-safe helper that escapes each line then joins with `<br>`).

### OBSERVATION #6: Hard-coded colors bypass `atlas_colors`/`atlas_themes` palette

**Location:** `casino/renderer/highlight_renderer.py:159, :206, :251, :292, :334, :421, :457, :666, :691` (and many sub-lines)
**Confidence:** 0.70
**Risk:** Many color values are hard-coded inline (`rgba(212,175,55,...)`, `#22d86e`, `#ff4d4d`, `rgba(74,222,128,...)`, etc.) instead of drawn from `atlas_style_tokens` or `atlas_colors`. CLAUDE.md declares `atlas_style_tokens.py` as "single source of truth for colors, fonts, spacing, layout" — this file routinely bypasses that for RGBA color stops. The CSS vars (`--gold`, `--win`, etc.) ARE used in most places, but the supporting rgba() glows and borders are inline.
**Vulnerability:** Theme system cannot recolor these stops. A "red winter" theme that changes `--win` will leave highlight cards with the wrong accent glow because the glows are hardcoded as `rgba(74,222,128,...)`.
**Impact:** Theme system leakage. Custom themes look partially applied on highlight cards.
**Fix:** Promote all `rgba(74,222,128,...)` and `rgba(212,175,55,...)` stops into CSS custom properties (e.g. `--win-glow`, `--gold-glow`, `--loss-glow`) defined in `atlas_style_tokens.py` so themes can override them via the `theme["vars"]` mechanism at `atlas_html_engine.py:558-561`.

### OBSERVATION #7: `typing` import not used — `list[dict]` only works on Python 3.9+

**Location:** `casino/renderer/highlight_renderer.py:21, :573, :709`
**Confidence:** 0.95
**Risk:** `from __future__ import annotations` at line 21 defers evaluation of `list[dict]` annotations, so 3.9+ compatibility is fine at import time. But `leg_picks: list[dict]` leaves the `dict` parameter type unconstrained. A typo like `lp["game_type"]` vs `lp["bet_type"]` won't be caught by any type checker. Given that the codebase mandates Python 3.14, this is a typing quality issue, not a runtime issue.
**Vulnerability:** Stale typing surface. No TypedDict.
**Impact:** Developer experience regression; future changes to parlay dict contract aren't caught by static analysis.
**Fix:** Define `class ParlayLegDict(TypedDict): pick: str; bet_type: str; line: float; status: str` at module top (or import from a shared location) and use it in `_format_leg_label` and `render_parlay_card`. Mirrors the `atlas_focus` guidance on brittle contracts between modules.

## Cross-cutting Notes

Pattern for other files in Ring 2 Batch B (casino renderers):
- The `status_bar_css` override injection pattern (CSS override from the body into the `.status-bar` class defined by the engine) is a cross-cutting anti-pattern likely mirrored in `session_recap_renderer.py`, `pulse_renderer.py`, and `casino_html_renderer.py`. Grep for `<style>.status-bar {{ ` to find the others — they all risk the same silent theme-regression identified in WARNING #1 if `atlas_html_engine.wrap_card` evolves.
- The raw-Discord-ID-as-player-name issue is a Ring 1 bug that surfaces in *every* highlight / recap / pulse card type. The fix belongs in a shared helper (maybe `atlas_send._resolve_display_name(guild, discord_id) -> str` that falls back through cache → fetch → tsl_members → `"Unknown player"`), not per-renderer.
- All 5 public `render_*_card` functions use positional args + escalating `theme_id` keyword. A TypedDict or dataclass `HighlightContext` would both document the dict contract and prevent keyword-arg drift.
