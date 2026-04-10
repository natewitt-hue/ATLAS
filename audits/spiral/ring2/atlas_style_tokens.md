# Adversarial Review: atlas_style_tokens.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 165
**Reviewer:** Claude (delegated subagent)
**Total findings:** 7 (0 critical, 3 warnings, 4 observations)

## Summary

Style token single-source-of-truth per CLAUDE.md, but the `_CSS_MAP` dict is hand-maintained in parallel with the class attributes, creating the exact drift surface the module exists to prevent. Several class attributes (`TEXT_WARM_LIGHT`, `BJ_GOLD_HOT`) have subtle typos or inconsistent whitespace alignment, and the `to_css_vars()` output has no test or snapshot guard. Also duplicates `GOLD = "#D4AF37"` with `atlas_colors.py:TSL_GOLD` — the split between "embed color" and "render token" violates the single-source-of-truth claim.

## Findings

### WARNING #1: `_CSS_MAP` is a parallel source of truth that will drift from class attributes
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_style_tokens.py:91-159`
**Confidence:** 0.95
**Risk:** Every entry in `_CSS_MAP` is hand-written to mirror a class attribute. Adding a new token means editing two places (line 45: `SILVER = "#C0C0C0"` and line 127: `"silver": SILVER,`). Forgetting to add to the CSS map means the Python token is usable in Python but not in any rendered card (since renderers reference `var(--silver)` in CSS and never touch the Python attribute).
**Vulnerability:** Classic dual-source invariant with no test, assert, or generator to enforce sync. The docstring calls this "single source of truth" but the map itself IS a second source.
**Impact:** A new token added without updating `_CSS_MAP` produces `var(--newtoken)` failing to resolve in the rendered card — usually silent (CSS fallback to `inherit` or `initial`), producing visually broken cards with no error.
**Fix:** Derive `_CSS_MAP` programmatically from class attributes:
```python
@classmethod
def to_css_vars(cls) -> str:
    exclude = {"CARD_WIDTH", "DPI_SCALE", "_CSS_MAP"}
    lines = [
        f"  --{name.lower().replace('_', '-')}: {val};"
        for name, val in vars(cls).items()
        if not name.startswith("_") and name not in exclude and isinstance(val, str)
    ]
    return ":root {\n" + "\n".join(lines) + "\n}"
```
Then delete `_CSS_MAP` entirely.

### WARNING #2: `CARD_WIDTH = 700` (int) is special-cased but `DPI_SCALE = 2` and `NOISE_OPACITY` (str) are inconsistent
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_style_tokens.py:83-88, 154`
**Confidence:** 0.85
**Risk:** `CARD_WIDTH` is an int, commented "int for Playwright viewport." But `DPI_SCALE = 2` (int) is NOT in `_CSS_MAP` at all — it's referenced only in Python rendering code. Meanwhile `NOISE_OPACITY = "0.035"` is a string in the map. There is no consistent rule: some tokens are strings (CSS-ready), some are ints (Python-only), some are both. A caller reading the class can't tell which is which without checking the map.
**Vulnerability:** Inconsistent type discipline. The class mixes "CSS string token" and "Python runtime constant" with no separator. Renders that touch `Tokens.CARD_WIDTH` in Python code work, but a refactor that tries to `f"width: {Tokens.CARD_WIDTH}"` in a CSS template gets `"width: 700"` (no unit) and silently breaks layout.
**Impact:** Renderer bugs on any future cross-use.
**Fix:** Split the class into two: `CssTokens` (all strings with units) and `PyTokens` (ints/floats for Playwright / Python math). Or annotate each class attr with a comment indicating which.

### WARNING #3: Duplicate `GOLD` and `TSL_GOLD` across files — SSOT violation
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_style_tokens.py:13`
**Confidence:** 0.9
**Risk:** `GOLD = "#D4AF37"` is also defined as `TSL_GOLD = discord.Color(0xD4AF37)` in `atlas_colors.py:38` AND `CASINO = discord.Color(0xD4AF37)` in `atlas_colors.py:27`. Three source-of-truth points for the same canonical brand color.
**Vulnerability:** Per CLAUDE.md: "atlas_style_tokens.py | Single source of truth for colors, fonts, spacing, layout." This is the direct contradiction — `atlas_colors.py` is also claiming to own brand colors.
**Impact:** Rebrand drift. Any future tweak to the TSL gold must be made in three places.
**Fix:** Make `atlas_colors.py` derive its values from `atlas_style_tokens.Tokens`, e.g., `TSL_GOLD = discord.Color(int(Tokens.GOLD.lstrip('#'), 16))`.

### OBSERVATION #1: Whitespace alignment is inconsistent across comment columns
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_style_tokens.py:41-47`
**Confidence:** 0.55
**Risk:** Lines 41-47 have comments aligned with trailing spaces (`TEXT_WARM = "#b0a890"       # warm tan`), while lines 11-40 have no such alignment. This is cosmetic but suggests the file has been edited by multiple passes without a consistent style. A linter like `ruff format` will strip or re-align, producing a noisy diff on an unrelated PR.
**Vulnerability:** Style drift.
**Impact:** PR noise on future formatter runs.
**Fix:** Run `ruff format` or `black` once and commit.

### OBSERVATION #2: `BJ_GOLD_HOT = "#ffe066"` comment is "warm gold accent" but no description of when to use vs `BJ_GOLD`
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_style_tokens.py:53-54`
**Confidence:** 0.5
**Risk:** Two blackjack gold tokens — `BJ_GOLD_HOT` (`#ffe066`) and `BJ_GOLD` (`#ffd700`). The comments don't explain the semantic difference, so renderers will pick randomly or pick the wrong one.
**Vulnerability:** Unclear naming + missing usage docs on a visual constant.
**Impact:** Inconsistent blackjack card coloring.
**Fix:** Comment `BJ_GOLD_HOT` as "hover/highlight state" and `BJ_GOLD` as "default fill", or rename to `BJ_GOLD_HOVER` / `BJ_GOLD_BASE`.

### OBSERVATION #3: `JEWEL_*` colors are scoped to "prediction market categories" but hardcoded category mapping is elsewhere
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_style_tokens.py:56-59`
**Confidence:** 0.6
**Risk:** Three "jewel" colors mapped to hardcoded categories (economics/politics/entertainment) in comments. The actual category→color mapping lives in a prediction market renderer — if a new category is added (e.g., "sports"), the tokens file doesn't know about it. Tokens should be neutral; category→token mapping should live in the renderer.
**Vulnerability:** Coupling of token names to a specific feature's category set.
**Impact:** Adding a category requires editing the tokens file, defeating the purpose of a neutral token layer.
**Fix:** Rename to `JEWEL_1`/`JEWEL_2`/`JEWEL_3` or `JEWEL_BLUE`/`JEWEL_PURPLE`/`JEWEL_AMBER` (already done for the latter) and move the category→color mapping into `prediction_html_renderer.py`.

### OBSERVATION #4: `to_css_vars()` has no snapshot test and no stable ordering guarantee across Python versions
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_style_tokens.py:161-165`
**Confidence:** 0.65
**Risk:** The iteration `for k, v in cls._CSS_MAP.items()` uses dict insertion order, which IS stable in Python 3.7+. But there's no test or snapshot that asserts the output. A refactor that reorders `_CSS_MAP` (e.g., alphabetizes it) produces a different CSS `:root` block — if downstream CSS uses specificity ordering, that can cause visual regressions.
**Vulnerability:** Implicit ordering contract with no enforcement.
**Impact:** Potential for subtle CSS specificity bugs on refactor.
**Fix:** Add a snapshot test (`assert Tokens.to_css_vars() == expected_string`) or document that reordering is safe because cascade is by name not declaration order.

## Cross-cutting Notes

The dual-source-of-truth pattern (class attr + mapping dict) is shared with `atlas_colors.py` (which has `SPORTSBOOK` class attr + `_MODULE_MAP` dict). Both should be fixed with the same "derive from class attributes" pattern. Also, the duplicate `GOLD`/`TSL_GOLD`/`CASINO` issue is the biggest architectural smell — the two files claim to be SSOT for overlapping concerns. Recommend a follow-up to unify them: `atlas_style_tokens.py` owns raw colors, `atlas_colors.py` imports and wraps them in `discord.Color` for embed use.
