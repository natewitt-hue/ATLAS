# Adversarial Review: card_renderer.py

**Verdict:** needs-attention
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 748
**Reviewer:** Claude (delegated subagent)
**Total findings:** 11 (1 critical, 5 warnings, 5 observations)

> ORPHAN STATUS: LIVE
> This file is not imported through bot.py's direct dependency chain but IS imported by active code: `genesis_cog.py` (3 call sites: lines 744, 862, 1644) and `test_all_renders.py`. Argus's static scan missed it. Review as active production code.

## Summary

This is the actively-used Genesis trade card renderer (the QUARANTINE version is the legacy Pillow renderer; this HTML/Playwright version is the live one). It is mostly a thin HTML builder over `atlas_html_engine.render_card`, but contains a real division-by-zero bug, several places where untrusted dict input bypasses HTML escaping, and an `_ordinal()` function that crashes on `"?"` from the happy-path defaults of `_pick_card_html`. Recommend fix-then-ship.

## Findings

### CRITICAL #1: `_ordinal()` raises `TypeError` when `pk["round"]` is missing

**Location:** `card_renderer.py:117-118, 162-171`
**Confidence:** 0.95
**Risk:** A trade-card render path that triggers `_pick_card_html({})` (or any pick dict missing the `round` key) will crash. `_pick_card_html` defaults `rnd = pk.get("round", "?")` and then calls `_ordinal(rnd)`. `_ordinal` does `{1: "1st", ...}.get(n, f"{n}th")` — for `n="?"` this returns `"?th"`, which is fine — BUT for any non-integer numeric path the f-string will succeed, AND for cases where `rnd` happens to be a numeric string like `"1"` the dict lookup miss returns `"1th"` (an off-by-one labeling bug). The real crash mode is more subtle: any caller passing `rnd=None` (because the field exists with value `None`, which `dict.get()` does NOT replace with the default) goes into `_ordinal(None)` → returns `"Nonethr"` which is silent UI corruption, not a crash.
**Vulnerability:** No type coercion, no `int()` cast, dict lookup miss on the wrong key type. The `pk.get("round", "?")` only triggers the default when the key is *absent*; an explicit `None` value (very common from upstream API/DB joins) passes through.
**Impact:** Trade cards render with garbage labels like "S? ?th Round Pick" or "SNone Nonethr Round Pick", and downstream OCR / log parsing breaks. In the off-by-one "1th" case the user sees a wrong-looking round label and may file a complaint.
**Fix:** Coerce safely: `try: rnd_int = int(rnd) except (TypeError, ValueError): rnd_int = None`. Build label as `f"S{year} {_ordinal(rnd_int)} Round Pick" if rnd_int else f"S{year} Future Pick"`. And in `_ordinal()`, special-case `11, 12, 13 → "th"` (current code returns "11st" for 11).

### WARNING #1: `_ordinal()` returns wrong English for 11/12/13/21st/etc.

**Location:** `card_renderer.py:117-118`
**Confidence:** 0.99
**Risk:** `_ordinal(11)` → `"11th"` is correct only by accident (because the dict miss falls through to `f"{n}th"`). But `_ordinal(21)` → `"21th"` (should be "21st"), `_ordinal(22)` → `"22th"` (should be "22nd"), `_ordinal(23)` → `"23th"` (should be "23rd"). NFL drafts only have 7 rounds so today this is dormant — but pick metadata for compensatory picks, traded future picks, or fantasy modes can produce higher round numbers, and any future code reuse will produce visible wrong labels.
**Vulnerability:** Hardcoded dict only covers 1-3.
**Impact:** UI grammar bug. Low blast radius today, but a latent embarrassment.
**Fix:** Standard ordinal helper: handle `11<=n%100<=13 → "th"`, else use `n%10` to pick `st/nd/rd/th`.

### WARNING #2: `_player_card_html()` does not escape `dev_label` from the dev mapping

**Location:** `card_renderer.py:121-159`
**Confidence:** 0.7
**Risk:** `dev_label` is selected from a hardcoded `dev_display` dict, so on the *current* call paths it cannot be attacker-controlled. But: `dev_label` is interpolated raw into `dev_html` via f-string with no `_esc()` wrapper. If a future code change extends the dict from a config file, JSON, or DB column (which is exactly what `_load_icons()` already does for `dev_icons.json` 12 lines above), the resulting HTML injection silently goes live. This is a "trust me, the dict is hardcoded" assumption that won't survive routine refactors.
**Vulnerability:** The `_esc()` discipline applied to `name`, `pos`, `ovr`, `age` is broken for `dev_label`. There is no defense-in-depth.
**Impact:** Future XSS into a server-side rendered Playwright page. Playwright renders to PNG so no client-side script execution, but malicious HTML can break layout, leak data via image side-channels, or pull cross-origin remote content.
**Fix:** Wrap: `dev_html = f'<div class="dev-badge {_esc(dev_class)}">{_esc(dev_label)}</div>'` and apply the same to all other "trusted constant" interpolations on principle.

### WARNING #3: `team_a_name` / `team_b_name` are escaped before use, but the escaped form is then passed to `_team_logo_url()` and `_team_abbrev()`

**Location:** `card_renderer.py:195-200`
**Confidence:** 0.9
**Risk:** Lines 195-198 set `team_a = _esc(data.get("team_a_name", "Team A"))`, then line 199 calls `logo_a = _team_logo_url(team_a)`. `_team_logo_url` calls `_team_abbrev(team_a)` which calls `b.by_nickname(name, "NFL")`. If the team name contains characters that get HTML-encoded by `_esc` (e.g. `'`, `&`, `<`), the lookup string sent to `TeamBranding.by_nickname` is `Carolina &amp; Atl` instead of `Carolina & Atl`, and the lookup *silently fails*, falling through to ESPN's fallback URL. Then the *abbrev* lookup at line 100 (`if t.get("nickname", "").lower() in name.lower()`) also operates on the escaped form and may match the wrong team. The fallback ESPN URL is computed from an empty `abbrev`, producing `https://a.espncdn.com/i/teamlogos/nfl/500/.png` — a 404 — so the team logo `<img>` tag silently breaks.
**Vulnerability:** Mixing escape concerns — once a value is HTML-escaped, it's no longer suitable as a lookup key.
**Impact:** Team logos missing from rendered cards for any team whose nickname contains `&`, `<`, `>`, `"`, or `'`. None today (real NFL teams are alphanumeric), but the code is brittle for fantasy / international leagues.
**Fix:** Compute logos *before* escaping: `raw_team_a = data.get("team_a_name", "Team A"); team_a = _esc(raw_team_a); logo_a = _team_logo_url(raw_team_a)`. Same for `team_b`.

### WARNING #4: Module-level `_load_icons()` raises if `dev_icons.json` is malformed

**Location:** `card_renderer.py:61-70`
**Confidence:** 0.85
**Risk:** `_load_icons()` is called at *module import time*. If `dev_icons.json` exists but is malformed (truncated write, bad encoding, manual edit error), `json.load(f)` raises `json.JSONDecodeError`, which crashes the module import. Since this module is imported by `genesis_cog.py` at module level (line 50), a JSON-parse failure takes down the entire genesis cog at startup. The Genesis icon block at lines 51-57 has a `try/except Exception: pass` defending the same risk; the dev icons block at 64-70 does not.
**Vulnerability:** Inconsistent defense-in-depth — one icon loader is wrapped, the other is not.
**Impact:** Bad JSON file → genesis cog fails to load → trade center, parity engine, dev trait commands all silently disappear. No log explanation other than the bare exception.
**Fix:** Wrap the call in try/except matching the genesis icon pattern: `try: with open(...) as f: _DEV_ICONS = json.load(f) except Exception as e: log.warning("Failed to load dev_icons.json: %s", e); _DEV_ICONS = {}`. (And import `logging` to do that properly — currently uses `print()` only, see WARNING #5.)

### WARNING #5: Render errors are swallowed to `print()` with no log/observability

**Location:** `card_renderer.py:738-748`
**Confidence:** 0.95
**Risk:** `render_trade_card()` is the public API, called by `genesis_cog` for every trade approval/rejection card. On any internal failure (Playwright crash, page-pool exhaustion, HTML build error, font load failure), the error is caught by the bare `except Exception as e:` and only `print()`-ed to stdout. No `logging.exception()`, no traceback, no caller signal beyond `None`. The CLAUDE.md "Flow Economy gotchas" rule explicitly prohibits silent except in admin-facing views; this is a slightly less severe variant — it's a *user-facing* card render path, so the impact is just "no card", but the absence of an actual logger means *root cause is unrecoverable* once it happens in production.
**Vulnerability:** Bare except + print() = lossy observability. The `print()` goes to stdout which on Windows under a Discord bot service may not be captured anywhere.
**Impact:** Silent renderer failures appear to users as "trade card embed fallback" and to operators as nothing at all — no way to triage. CLAUDE.md cross-cutting rule violation.
**Fix:** `import logging; log = logging.getLogger("card_renderer")`, then in the except: `log.exception("[card_renderer] Render error for trade %s", data.get("trade_id", "?"))`. Keep the `return None` so the caller's embed fallback still works.

### OBSERVATION #1: `_branding` global is lazily initialized — race risk under concurrent renders

**Location:** `card_renderer.py:88-94`
**Confidence:** 0.6
**Risk:** `_get_branding()` lazily initializes a module-level `_branding` global. Under concurrent trade renders (which is plausible — Playwright page pool of 4 means up to 4 concurrent renders) two threads can both observe `_branding is None` and both construct a new `TeamBranding` instance. The second one wins; the first becomes garbage. This is benign for correctness (both load the same JSON) but wastes a JSON parse. More importantly, if `TeamBranding.__init__` ever picks up state (caches, file watchers, etc.), the race becomes meaningful.
**Vulnerability:** Classic double-checked-lock without a lock.
**Impact:** Wasted CPU on first concurrent render. No correctness impact today.
**Fix:** Either initialize at module top alongside `_DEV_ICONS = {}` (recommended — failure mode mirrors the icon loader), or guard with `threading.Lock`.

### OBSERVATION #2: `pct_a` rounding can produce `pct_b = -1` for extreme deltas

**Location:** `card_renderer.py:228-230`
**Confidence:** 0.75
**Risk:** `pct_a = round(val_a / total_val * 100)` followed by `pct_b = 100 - pct_a`. Python's banker's rounding can produce `pct_a = 100` for `val_a = 99999, val_b = 1`, in which case `pct_b = 0`, fine. But for `val_a = 1, val_b = 99999`, `pct_a = 0`, `pct_b = 100`, also fine. The corner case is `val_a < 0` (a sign error from upstream valuation logic), which would produce `pct_a < 0` and `pct_b > 100`, rendering a fairness bar that overflows its container. There is no clamp.
**Vulnerability:** Trusts upstream `delta_pct`/`side_a_value` validity.
**Impact:** Visual layout corruption on edge-case trade valuations. Cosmetic.
**Fix:** Clamp: `pct_a = max(0, min(100, round(...)))`.

### OBSERVATION #3: Hardcoded asset slice limits to 4 silently truncate large trades

**Location:** `card_renderer.py:208-214`
**Confidence:** 0.9
**Risk:** `players_a[:4]` and `picks_a[:4]` cap each side at 4 player cards + 4 pick cards. Trades with 5+ assets per side render with the extras silently dropped, no `+N more` indicator. Madden allows trades up to 9-10 assets per side in some scenarios.
**Vulnerability:** Magic number `4` with no constant, no overflow indicator, no caller awareness.
**Impact:** Information loss on large trades. Users may approve a trade thinking they see all assets when 1+ is hidden.
**Fix:** Either constant + render `+N more` row, or assert/log when slicing throws away rows.

### OBSERVATION #4: `ai_clean = _esc(ai.strip().strip("*_").strip('"'))` strips characters from BOTH ends, including legitimate punctuation

**Location:** `card_renderer.py:248`
**Confidence:** 0.7
**Risk:** `.strip("*_")` removes asterisks/underscores from both ends to drop markdown bold/italic; `.strip('"')` removes quotes. But `.strip()` removes ALL matching characters from both ends — so an AI commentary like `"**Atlas verdict: trade is fair.**"` correctly becomes `Atlas verdict: trade is fair.`, but `***LOPSIDED***` becomes `LOPSIDED` (good), and `*_**fair**_*` becomes `fair` (good), but `quote: "fair"` becomes `quote: "fair` (wait — `.strip('"')` strips both ends, so `quote: "fair"` → `quote: "fair` is wrong; trailing `"` strips, leading `q` doesn't, so result is `quote: "fair`). Actually re-checking: `.strip('"')` removes leading AND trailing `"`. `quote: "fair"` → leading char `q` is not `"`, so left side untouched, trailing `"` is removed → `quote: "fair`. Result: an unbalanced opening quote remains. The verdict box then renders `"quote: "fair"` (literal) with mismatched quotes.
**Vulnerability:** `.strip()` is the wrong tool for "remove if completely wraps". Should use a regex or len-check.
**Impact:** Malformed AI verdicts with stray punctuation in user-visible cards. Cosmetic.
**Fix:** `if ai.startswith('"') and ai.endswith('"'): ai = ai[1:-1]`. Same pattern for `*` and `_` runs.

### OBSERVATION #5: `theme_id` is forwarded but never validated

**Location:** `card_renderer.py:738-748` and `_build_html` line 174
**Confidence:** 0.5
**Risk:** `theme_id` is passed straight through to `wrap_card()` with no validation. If `wrap_card` lookups it in a registry that returns `None` for unknowns, the engine likely falls back to a default — but if it raises `KeyError` instead, the bare `except Exception` at line 746 swallows it silently. Either way, callers passing a bad theme get a default card with no warning.
**Vulnerability:** Trust-the-caller without sanitization. Combined with WARNING #5's poor logging, debugging "why is my themed card always default?" requires reading source.
**Impact:** Themed-card feature appears broken to users with no error signal. Low severity.
**Fix:** Either validate `theme_id` against a known set with a logged warning on miss, or document the contract loudly in the docstring.

## Cross-cutting Notes

The QUARANTINE/card_renderer.py file is the legacy Pillow version per CLAUDE.md. This live HTML version is the correct active implementation; QUARANTINE/ is correctly NOT imported anywhere live. However, the live `card_renderer.py` shares concerning patterns with `casino/renderer/casino_html_renderer.py` and `casino/renderer/highlight_renderer.py` — the same `print()` for errors instead of `log.exception()`, the same module-level icon loaders without try/except, and the same unsanitized lookup-key reuse after escaping. A coordinated cleanup pass across the renderer family would amortize fix cost.
