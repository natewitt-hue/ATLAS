# Adversarial Review: casino/renderer/prediction_html_renderer.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 1839
**Reviewer:** Claude (delegated subagent)
**Total findings:** 21 (2 critical, 10 warnings, 9 observations)

## Summary

This is a pure HTML-card rendering module with no money, DB, or Discord-API surface of its own, so most of the Ring-1 polymarket_cog BLOCK findings do not propagate here. The real risks are a hard, exploitable HTML-injection path via unescaped `side`/`status` fields in portfolio rows plus a `style=` CSS-variable splice that accepts an attacker-controllable token name, and a lattice of silent `.get()` defaults that will mis-render financial numbers (percentages, cents, payouts) when Polymarket data is missing, zero, None, or the wrong type. Pagination math and "end dates in the past" are wrong by construction. Not a ship-blocker, but the two criticals must be fixed before any user-supplied Polymarket text flows through this renderer.

## Findings

### CRITICAL #1: HTML injection via unescaped `side`/`status`/`action_html` in portfolio rows

**Location:** `casino/renderer/prediction_html_renderer.py:1073-1128`
**Confidence:** 0.90
**Risk:** `_build_portfolio_row` reads `pos.get("side", "YES").upper()` at line 1075 and then injects `side` directly into `meta_html` at line 1093 (`meta_parts.append(side)`) without passing it through `esc()`. The other meta fields — `qty` (line 1094) and `buy_price` (line 1096) — are interpolated via f-strings that also bypass `esc()`. Because Polymarket event data is ingested verbatim into the `prediction_contracts` DB (per Ring-1 review of polymarket_cog), and the upstream cog may at some point store the Polymarket "outcome" label (not just "YES"/"NO") into `side`, any arbitrary string passed in as `side` — including `"</div><script>x</script>"` — will be rendered raw inside the card HTML and then evaluated by Playwright/Chromium during screenshot.
**Vulnerability:** Playwright renders a full Chromium page and executes JavaScript on `domcontentloaded`. An injected `<script>` or `<style>` block inside the portfolio card body will execute in the renderer's Chromium context. Because the page pool is shared across renders (`_pool` in `atlas_html_engine.py`), a persistent injection (e.g., installing a service-worker or leaking state into `window.localStorage`) would contaminate subsequent renders in the pool, even for other users. Further, `esc(title)` is called at line 1116 — the fact that the author remembered to escape `title` but not `side`/`qty`/`buy_price` is a sign the mental model is inconsistent, not a deliberate choice.
**Impact:** Cross-render contamination inside the Playwright pool, potential SSRF if the injected script exfiltrates data via `fetch()`, or image corruption at minimum. The file is the display layer for a file that Ring 1 found had 8 critical findings including idempotency holes — if an attacker can also persistently poison the renderer, the blast radius compounds.
**Fix:** Wrap every non-numeric interpolated field in `esc()`. Specifically:
```python
meta_parts.append(esc(side))
meta_parts.append(esc(f"{qty} contracts"))
if buy_price:
    meta_parts.append(esc(f"@{buy_price:.0%}"))
```
Apply the same audit to every other f-string in this file that interpolates a `dict.get()` value: `_build_position_detail_html` (line 1160: `side = position.get("side", "YES").upper()` used in class name AND pill text), `render_bet_confirmation_card` (line 935-954: `side.upper()` used as both class and text without `esc()`), and `render_sell_confirmation_card` (line 1775-1795: same pattern). A grep for `dict.get\(.*\).*f".*\{` through the file will surface the rest.

---

### CRITICAL #2: CSS-variable splice accepts attacker-controlled value in position detail card

**Location:** `casino/renderer/prediction_html_renderer.py:1169-1197`
**Confidence:** 0.88
**Risk:** Line 1175 computes `delta_cls = "green" if delta >= 0 else "red"` — so far so good. But line 1197 builds a style attribute that interpolates `delta_cls` **directly into an inline `style=` CSS expression**:
```python
("Current", f'{current_cents}&cent;<div class="cell-sub" style="color:var(--{delta_cls})">{delta_sign}{delta_cents}&cent; {delta_arrow}</div>', "big"),
```
Today `delta_cls` is controlled by a static ternary, so the actual splice is safe — but `current_cents` and `delta_cents` are **derived from `position.get("current_price", buy_price)` without bounds-checking or escape.** `_price_cents` returns `f"{int(round(price * 100))}"` which, if fed a non-numeric or NaN, will either raise or produce odd output. More critically, the whole string is then injected into a CSS rule; even though cents values are numeric, a future refactor that passes a string category or label through the same `_recessed_strip` cell builder will splice arbitrary text into `style=`. The `style=` in `render_market_list_card` at line 738 (`style="width:{yes_pct}%;"`) has the same splice pattern and is only safe today because `yes_pct` is coerced to `int`.
**Vulnerability:** CSS inside Chromium can trigger `url()` requests, `@import` fetches, or expression-based DOM traversal. Any future refactor that forgets to coerce-to-int before splicing into `style=` will silently create a CSS-injection hole with no reviewer flag. The renderer currently has zero defense-in-depth: no CSP, no escape layer for CSS values, no attribute-level quoting beyond the f-string's own `"..."`.
**Impact:** Same as finding #1 — Chromium in the pool can fetch attacker URLs, exfiltrate per-render state, and the output image may differ from what the bot thinks it rendered (CSS layout injection). Less immediately exploitable than #1 because `delta_cls` is fixed, but the pattern is a loaded gun.
**Fix:** Never splice variables into `style="..."` literally. Either (a) move the color choice into a class (`class="cell-sub delta-green"`), or (b) if you must use inline style, hard-code the palette: `style="color:#57F287"` for green, `style="color:#ED4245"` for red. Add a project-wide lint rule banning `style=".*\{.*\}.*"` f-strings in any `*_html_renderer.py` file. Audit the companion line 738 (`style="width:{yes_pct}%;"`) and line 836 (`style="width:{yes_pct}%;"`) and line 1613-1614 (`style="width: {max(0.02, yes_p):.0%};"`) — these are currently safe only because of upstream coercions that aren't type-enforced.

---

### WARNING #1: Pagination "start/end" math uses per-page count, not a fixed page size

**Location:** `casino/renderer/prediction_html_renderer.py:753-761`
**Confidence:** 0.95
**Risk:** The display line `{start}–{end} of {total}` is computed as:
```python
shown = len(markets)
start = (page - 1) * shown + 1 if shown else 0
end = start + shown - 1 if shown else 0
```
This is mathematically wrong on any page that doesn't have exactly `PAGE_SIZE` entries. Example: if the UI shows 10 markets per page and the user is on page 3 of 3 with 4 leftover markets, `shown=4`, so `start = (3-1)*4 + 1 = 9` and `end = 9 + 3 = 12`. The correct answer (with `PAGE_SIZE=10`) is `start=21, end=24`. Every non-last page is also wrong unless it happens to be full.
**Vulnerability:** The function never receives `PAGE_SIZE`. There is no way to recover the correct `start` without it. The original author likely meant to accept a `per_page` parameter but only wired in `total_markets`.
**Impact:** The footer "11–18 of 47" label on every non-last page shows a wrong range. User-visible display bug, not a data bug, but user-facing on a public-channel browse card.
**Fix:** Add a `per_page: int` parameter to `render_market_list_card` and `_build_market_list_html`, or compute it as `max(shown, total // max(1, total_pages))`. Callers must pass the canonical page size.

---

### WARNING #2: `_fmt_end_date` silently renders expired markets with no "EXPIRED" indicator

**Location:** `casino/renderer/prediction_html_renderer.py:626-635, 810-872`
**Confidence:** 0.85
**Risk:** `_fmt_end_date` parses the ISO `end_date` and formats it as `%b %d, %Y`, but never checks whether the date is in the past. `_build_market_detail_html` at line 867-871 also hard-codes the status strip as `<span class="status-dot"></span> Open` regardless of actual expiration.
**Vulnerability:** A market whose `end_date` is yesterday will still render "Open" with a green dot and the footer saying "click below to open wager modal". If the cog's sync loop (per Ring-1 finding) falls behind and stale rows persist, users will see betting prompts on closed markets. The renderer doesn't know the true status because the cog doesn't pass one.
**Impact:** User-visible error — can lead to confused support requests and, indirectly, wasted DB writes if the cog allows the bet attempt to reach the balance-check stage before failing.
**Fix:** Either (a) take an explicit `status` parameter and route expired → `closed` styling, or (b) parse `end_date` in `_fmt_end_date`, compare to `datetime.now(tz=UTC)`, and return `"EXPIRED"` or similar; then in `_build_market_detail_html` flip the dot class to `.closed`.

---

### WARNING #3: `_implied_profit` returns empty string at boundary prices with no signal to caller

**Location:** `casino/renderer/prediction_html_renderer.py:638-642`
**Confidence:** 0.7
**Risk:** The function is dead code — grep shows it's never called in this file or any caller. It also has a subtle bug: `if price <= 0 or price >= 1: return ""`. A legit Polymarket price of exactly 0.01 or 0.99 is valid, but prices can drift to 0.995 during resolution, and the function will return an empty string — the caller cannot distinguish "invalid price" from "too risky to quote". Since it's unreached the impact is zero today, but future wiring will silently swallow corner cases.
**Vulnerability:** Dead code that looks like it does something rots. A reviewer adding a `Profit:` column in the portfolio row will reach for this helper and not realize it has an asymptote cliff at the boundaries.
**Impact:** Latent bug, zero current impact.
**Fix:** Remove the function, or add a sentinel return (`None`) that forces callers to handle the boundary explicitly.

---

### WARNING #4: `render_resolution_card` truncates winners to 5 silently and loses the "you were on the list" signal

**Location:** `casino/renderer/prediction_html_renderer.py:1296-1308`
**Confidence:** 0.85
**Risk:** `for i, w in enumerate(winners[:5])` hard-codes a 5-row cap. There is no "and 8 more…" indicator, no total-winners count, and if the caller passes 100 winners the card silently displays only the top 5. The function signature accepts `total_won`, `total_lost`, `total_voided` but these values are NEVER interpolated into the body HTML of `render_resolution_card` — look at lines 1310-1315; only `market_title`, `result_class`, `result`, and `winners_html` are used. `total_won`/`total_lost`/`total_voided` are accepted as parameters and then ignored entirely.
**Vulnerability:** This is a silent contract violation. The caller (`polymarket_cog.py` line 3008) computes a totals roll-up, passes it to the renderer, and the renderer drops it. Users who were outside the top 5 winners see no indication they won.
**Impact:** UX confusion on every resolution of a market with >5 winners; the data exists but the renderer loses it. Also, wasted computation in the caller.
**Fix:** Interpolate the dropped params into the resolution body: add a "X winners share $Y · Z lost bets" summary line. If totals aren't wanted, remove the params from the signature to fail loud on the next caller update.

---

### WARNING #5: `user_cost` parameter in `render_market_detail_card` is accepted but unused

**Location:** `casino/renderer/prediction_html_renderer.py:790-915`
**Confidence:** 0.95
**Risk:** `_build_market_detail_html` and `render_market_detail_card` both accept `user_cost: int = 0` (line 800, 904) but the parameter is never read inside either function body. The "position badge" path at line 858-864 only uses `user_position` and `user_contracts`. `user_cost` is pure dead weight.
**Vulnerability:** An admin debugging "the position badge shows wrong cost" will look at this function, not find `user_cost` in the body, and waste time. Worse, it implies the UI was supposed to display cost-basis and someone ripped it out mid-feature.
**Impact:** Low — stale signature. But a real bug-trap for maintenance.
**Fix:** Either wire `user_cost` into the position badge (`f"YOUR BET: {user_position} × {user_contracts} @ ${user_cost}"`) or drop the parameter and remove it from the cog call site at polymarket_cog.py:1561.

---

### WARNING #6: `_jewel_badge` falls back to data-cat="other" for unknown categories but `_category_color` falls back to a hard-coded hex

**Location:** `casino/renderer/prediction_html_renderer.py:44-73`
**Confidence:** 0.8
**Risk:** `_DEFAULT_CATEGORY_COLOR = "#95A5A6"` is a hard-coded hex not in `Tokens`. The function `_category_color` is defined but **is never called anywhere in this file** (grep confirms — it's only defined). The real path is `_cat_data` + the CSS `data-cat` selector. The dead `_category_color` creates the illusion that category styling goes through a Python-side lookup, but it doesn't.
**Vulnerability:** A theme-system refactor (mentioned in CLAUDE.md under "Theme splicing chain") will hit `_category_color`, think it's live, and mis-route the palette. Meanwhile the actual badge CSS is in `_prediction_css()` inline — not in `atlas_themes.py` or any token-driven place.
**Impact:** Theme consistency regression on a future refactor. Today, zero runtime impact.
**Fix:** Delete `_category_color` and `_DEFAULT_CATEGORY_COLOR`, or wire them into `_jewel_badge` (`style="background-color:{_category_color(category)}"`). Also move the hex values into `Tokens` so they go through the theme system.

---

### WARNING #7: `render_curated_list_card` and `render_daily_drop_card` concatenate CSS twice without dedup

**Location:** `casino/renderer/prediction_html_renderer.py:1550-1559, 1689-1691`
**Confidence:** 0.8
**Risk:** Both curated-list and daily-drop wrappers do:
```python
html = base_html.replace("</style>", f"{_prediction_css()}{_curated_css()}</style>", 1)
```
This appends the full `_prediction_css()` string (which is ~540 lines of CSS) plus the `_curated_css()` string into the `<style>` block for every render. The CSS is NOT cached — `_prediction_css()` is a plain function that builds a new string on every call. Under sustained load, this allocates and concatenates ~30 KB of HTML per render.
**Vulnerability:** Not a correctness bug, but a hot-path allocation gotcha. Paired with the per-render Playwright page setup, a burst of concurrent renders will GC-thrash. The `_wrap_prediction_card` helper at line 645-648 has the same issue for non-curated cards.
**Impact:** Renderer throughput cap earlier than expected. At 4 pages in the pool and bursty traffic (post-resolution, daily-drop publish), this becomes latency-visible.
**Fix:** Cache `_prediction_css()` at module import: `_PREDICTION_CSS_CACHED = _prediction_css()` and reference the constant. Same for `_curated_css`. This is the pattern already used by `atlas_html_engine._SHARED_CSS`.

---

### WARNING #8: `_build_market_list_html` iteration leaves `i` unused — likely orphaned rank logic

**Location:** `casino/renderer/prediction_html_renderer.py:702-744`
**Confidence:** 0.6
**Risk:** `for i, m in enumerate(markets):` is used, but `i` is never referenced inside the body. Same story at line 1297 (`for i, w in enumerate(winners[:5]):` — `i` IS used as `rank = i + 1`). The curated list path at line 1347 (`for i, m in enumerate(markets):`) uses `i + 1` for `.market-index`. So two out of three enumerations actually use `i`, but the market-list browse view leaves `i` dead.
**Vulnerability:** Either the browse view was supposed to have a rank badge and someone cut it, or the `enumerate` is a leftover from copy-paste. Either way, `i` is misleading dead weight.
**Impact:** None at runtime. Confuses readers.
**Fix:** Drop `i`: `for m in markets:` if no rank badge is needed, or add `<div class="market-index">{i + 1}</div>` to the row if it is.

---

### WARNING #9: `_fmt_end_date` uses `end_date[:10]` fallback — silent data corruption on malformed input

**Location:** `casino/renderer/prediction_html_renderer.py:626-635`
**Confidence:** 0.75
**Risk:** If ISO parsing fails (`ValueError`, `TypeError`), the function falls back to `return end_date[:10]`. If `end_date` is a non-string (e.g., `None` → the initial `if not end_date: return ""` catches it, good), or an integer, or a bytes object — `end_date[:10]` will either raise `TypeError` (bytes slice returns bytes, not str, which then fails HTML escaping), return garbage, or silently truncate a Unix timestamp. The `except` only catches `ValueError, TypeError` but not `AttributeError`.
**Vulnerability:** Polymarket API has historically returned `end_date_iso` and `endDate` in different formats depending on endpoint. Ring 1 already flagged the polymarket_cog sync as having schema drift concerns. If the date field changes type or becomes a Unix timestamp int, the renderer fails silently with a 10-char garbage string.
**Impact:** User-visible "Bn, 01, Jan" or empty-string dates on every card. Not catastrophic, but an obvious bug-report source.
**Fix:** Add an explicit type check: `if not isinstance(end_date, str): return ""`. Then the fallback `end_date[:10]` is at least bounded. Better: return a sentinel like `"TBD"` so the card shows something semantically honest.

---

### WARNING #10: `yes_pct` / `no_pct` display lies when prices sum ≠ 1.0

**Location:** `casino/renderer/prediction_html_renderer.py:705-707, 808`
**Confidence:** 0.8
**Risk:** Market list: `no_price = m.get("no_price", 1 - yes_price)` — only used as a default. Market detail: `yes_pct = max(2, min(98, int(round(yes_price * 100))))`. The probability bar shows only `yes_pct` width, implicitly treating `no_pct` as `100 - yes_pct`. But Polymarket binary markets can have a 2-3% spread (YES=0.52, NO=0.50) due to taker-vs-maker fees, so summing to 1.02 is real. The label at line 831-833 renders `yes_price:.0%` and `no_price:.0%` independently, so the text shows 52% / 50% while the bar shows 52% width and implicitly 48% NO. User sees conflicting numbers.
**Vulnerability:** When the caller passes independently sourced `yes_price` and `no_price` (as polymarket_cog does — it reads both from Polymarket's order-book tops), the visual bar lies. This is worse on tight markets (48/52 → looks like 50/50) and more misleading on wide markets.
**Impact:** Subtle trader-facing lie. Users who trade on the card will be surprised when the wager modal shows a different quote.
**Fix:** Either (a) renormalize: `total = yes_price + no_price; yes_pct = yes_price / total * 100`, or (b) label the bar "YES share of market" and keep the numeric labels as ground truth — but add a "spread: ±2¢" note.

---

### OBSERVATION #1: `theme_id` is plumbed through 10 render functions but no validation

**Location:** `casino/renderer/prediction_html_renderer.py:772, 894, 933, 995, 1140, 1277, 1553, 1571, 1703, 1772`
**Confidence:** 0.7
**Risk:** Every public render function accepts `theme_id: str | None = None` and passes it to `wrap_card(theme_id=theme_id)`. If `atlas_themes.py` is ever replaced (per CLAUDE.md "Theme splicing chain" concern) or the theme system moves to a stricter contract, this module will silently pass arbitrary strings that may cause CSS injection or fallback to default without signal.
**Vulnerability:** Trust-boundary question: who validates `theme_id`? If the caller passes a user-controlled theme string (e.g., from a `/flow theme` command the user picked), it reaches CSS splice territory in `atlas_themes.py`. This module acts as a pass-through with no sanitization.
**Impact:** Depends entirely on `atlas_themes.wrap_card` behavior. Would need Ring-2 audit of that module to know.
**Fix:** Add a comment `# theme_id validated by atlas_themes.wrap_card` at the top of each function, or wrap with `theme_id = None if theme_id not in VALID_THEMES else theme_id`.

---

### OBSERVATION #2: `_prediction_css` is a 540-line string with inline hex colors, bypassing theme tokens

**Location:** `casino/renderer/prediction_html_renderer.py:78-620`
**Confidence:** 0.95
**Risk:** The CSS string hard-codes dozens of hex values: `#3DBE6F`, `#57F287`, `#F06B6B`, `#ED4245`, `#5EE89A`, `rgba(74,158,255,...)`, `rgba(212,175,55,...)`, `rgba(168,85,247,...)`, etc. None go through `Tokens` or `atlas_themes`. If a user selects a "red" theme, the YES probability bar still shows green because the green is baked into the CSS string, not a CSS variable.
**Vulnerability:** Theme consistency regression. Per CLAUDE.md, `atlas_style_tokens.py` is "Single source of truth for colors". This file has ~50 violations.
**Impact:** Theme switching is partially broken on every prediction-market card. Not a correctness bug, but a visual contract violation.
**Fix:** Replace hex values with CSS custom-property references: `background: var(--win)` instead of `#3DBE6F`. Define new tokens where needed (`--jewel-amber`, `--jewel-purple`) in `atlas_style_tokens.py`.

---

### OBSERVATION #3: `_recessed_strip` signature uses bare `tuple[str, str, str]` with no validation

**Location:** `casino/renderer/prediction_html_renderer.py:656-668`
**Confidence:** 0.6
**Risk:** The `value` field is typed as `str` but many call sites (line 1197, 1028, 868) pass **raw HTML markup** into it (`<div class="cell-sub">...</div>`, `<span class="status-pill">...</span>`). The cell-value is then interpolated with `{value}` (not `esc(value)`), which is consistent but creates a hidden split: some fields are escaped (`esc(label)`), some are raw HTML (`value`). A new contributor adding a string value will not know it's the raw-HTML column and will double-escape or forget to escape.
**Vulnerability:** Invariant violation that looks like a bug when a new call site passes a user-controlled string into the middle `str` slot expecting text behavior.
**Impact:** Future bug risk. Nothing wrong today because all current callers pass either ints, f-strings over ints, or hand-crafted HTML fragments.
**Fix:** Rename the parameter to `value_html` and add a docstring note. Better: split into two functions — `_recessed_strip_text` (escapes value) and `_recessed_strip_html` (raw).

---

### OBSERVATION #4: Category name parsing strips emoji at first space without fallback for emoji-less categories

**Location:** `casino/renderer/prediction_html_renderer.py:56-67, 712-713, 804-805, 1352-1353, 1591-1592, 1623-1624, 1712-1714`
**Confidence:** 0.8
**Risk:** The pattern `parts = category.split(" ", 1); cat_name = parts[1] if len(parts) > 1 else parts[0]` is duplicated **seven times** across the file. It assumes the input is `"emoji Name"` and returns `"Name"`. If the caller ever passes `"Name"` (no emoji), it returns `"Name"` (correct by fallback). But if the caller passes `"Two Words"`, it returns `"Words"` — dropping the first word. Polymarket has categories like `"Pop Culture"`, `"Global Politics"`, `"AI & Tech"` that could trigger this if the emoji is missing.
**Vulnerability:** Silent data mangling. The fallback branch `parts[0]` fires only when there is ZERO space in the string, so `"Pop Culture"` → `["Pop", "Culture"]` → `cat_name = "Culture"` (the emoji stripper drops "Pop" thinking it's the emoji).
**Impact:** Category badges render the wrong name for multi-word categories when upstream data has no emoji prefix.
**Fix:** Extract to a helper: `def _strip_emoji_prefix(cat: str) -> str:` that uses a proper unicode-category check (`unicodedata.category(ch).startswith("So")`) or regex `r'^[^\w]+'` to strip only leading non-word chars. Replace all 7 copies.

---

### OBSERVATION #5: Slug truncation to 15/20 chars silently uppercases and can produce garbled tickers

**Location:** `casino/renderer/prediction_html_renderer.py:717-718, 1091-1092`
**Confidence:** 0.5
**Risk:** `slug[:20].upper()` and `slug[:15].upper()` assume slugs are ASCII. Polymarket slugs are typically ASCII kebab-case, but any unicode in a slug would be truncated mid-codepoint and `.upper()` may expand (German ß → SS) past the intended width.
**Vulnerability:** Low. Slugs are always ASCII today.
**Impact:** Minimal.
**Fix:** Use `slug[:20].upper()` only if `slug.isascii()`, else fall back to a safe default.

---

### OBSERVATION #6: `delta_cls` CSS variable reference uses `var(--{delta_cls})` with hard-coded fallback

**Location:** `casino/renderer/prediction_html_renderer.py:1197`
**Confidence:** 0.7
**Risk:** The CSS string `var(--green)` / `var(--red)` is emitted. Grep `Tokens.GREEN` / `Tokens.RED` at atlas_style_tokens.py does not exist — the tokens are named `--win` and `--loss`. So `var(--green)` and `var(--red)` will resolve to `unset` and fall back to inherited color. This is a cosmetic bug but also exactly the same class of hard-to-debug CSS variable splice flagged in CRITICAL #2.
**Vulnerability:** The delta indicator won't be green-or-red; it will inherit the parent color (probably white).
**Impact:** Visual correctness bug — users can't tell at a glance whether their position is up or down.
**Fix:** Change to `var(--win)` / `var(--loss)` or hard-code `color:#57F287` / `color:#ED4245`.

---

### OBSERVATION #7: `delta_cents` can display as `+-0` or `-0` due to int rounding

**Location:** `casino/renderer/prediction_html_renderer.py:1172-1175`
**Confidence:** 0.6
**Risk:** `delta = current_price - buy_price`; `delta_cents = int(round(delta * 100))`. If `delta = 0.003`, `delta_cents = 0`, and the display shows `+0¢`. If `delta = -0.003`, `delta_cents = 0` and the display shows `+0¢` because `delta < 0` is False only when delta is exactly 0 — wait: `delta_sign = "+" if delta >= 0 else "-"` and `delta_arrow = "↑" if delta >= 0 else "↓"`. For `delta = -0.003`, `delta >= 0` is False → sign `-`, arrow `↓`, BUT `delta_cents = 0`, so display shows `-0¢ ↓`. Ugly.
**Vulnerability:** Cosmetic-only. Happens on fractional penny drifts.
**Impact:** Trivial display noise on thin price movements.
**Fix:** After computing `delta_cents`, set `delta_sign` based on `delta_cents >= 0` instead of `delta >= 0`, OR display "—" if `delta_cents == 0`.

---

### OBSERVATION #8: `render_price_alert_card` decides direction by float comparison with no epsilon

**Location:** `casino/renderer/prediction_html_renderer.py:1698-1755`
**Confidence:** 0.5
**Risk:** `direction = "up" if new_price > old_price else "down"` — if prices are equal (`new_price == old_price`), it silently falls into "down" with `arrow = "↓"`. The caller (polymarket_cog) triggers alerts on a 10% price move threshold per Ring 1, so exact equality should never reach here, but the renderer doesn't defend itself.
**Vulnerability:** If the caller ever passes equal prices (bug in the threshold logic), the alert card shows a red "↓ 0%" which is nonsensical.
**Impact:** Trivial; prerequisites make it rare.
**Fix:** `direction = "up" if new_price > old_price else "down" if new_price < old_price else "flat"` and handle flat.

---

### OBSERVATION #9: No `__all__` or public-API marker; the module exports 10 public functions by convention only

**Location:** `casino/renderer/prediction_html_renderer.py` (entire file)
**Confidence:** 0.6
**Risk:** The module defines 10 render coroutines plus a dozen helpers with leading underscore. There is no `__all__` declaration. A consumer could accidentally import `_build_market_list_html` (private) thinking it's public. Per CLAUDE.md, ATLAS has a single render pipeline contract; the file should declare its surface.
**Vulnerability:** API erosion over time.
**Impact:** Minor hygiene.
**Fix:** Add `__all__ = ["render_market_list_card", "render_market_detail_card", ...]` at the top of the file.

---

## Cross-cutting Notes

**Rendering-subsystem patterns that likely affect other files in this ring:**

1. **`_cat_data` and `_category_color` duplication** — the category-stripping logic (seven copies) and `_cat_data` map likely exist in `casino/renderer/highlight_renderer.py`, `session_recap_renderer.py`, etc. Worth a grep for the string `parts = category.split(" ", 1)` across the whole `casino/renderer/` subtree.

2. **Uncached CSS string builders** — `_prediction_css()` returns a ~30 KB string built on every call. This pattern likely repeats in `casino_html_renderer.py`, `highlight_renderer.py`, etc. Module-level `_*_CSS = _build_*_css()` caching is missing across the subsystem.

3. **Inline `style=` splices with no escape layer** — every other renderer in `casino/renderer/` likely uses the same `f'style="width:{x}%;"'` pattern. Ring-2 batch should cross-check for CSS-variable injection in the theme-splice chain.

4. **No escape on dict-get fields in f-strings** — the `esc(title)` / `esc(player_name)` discipline is inconsistent. Recommend a project-wide grep for `f"....{.*\.get\(.*\)....}"` in renderer files and a hookify rule to enforce `esc()` on any `.get()` result interpolated into HTML.

5. **Deleted/unused parameters** — `total_won`/`total_lost`/`total_voided`/`user_cost` are accepted and ignored. The Ring-1 polymarket_cog audit found extensive dead code; the renderers have caught it too. Suggests a feature was partially reverted without cleaning up the API surface.

6. **Pagination math that depends on `len(markets)`** — almost certainly replicated in other list renderers in the same batch. Worth explicit checks on `sportsbook_cards.py`, `flow_cards.py`, `ledger_renderer.py`.
