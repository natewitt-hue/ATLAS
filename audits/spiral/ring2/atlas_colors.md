# Adversarial Review: atlas_colors.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 80
**Reviewer:** Claude (delegated subagent)
**Total findings:** 5 (0 critical, 2 warnings, 3 observations)

## Summary

Simple color palette module, but maintains TWO independent sources of truth for the same values (the class attributes and the `_MODULE_MAP` dict), which will drift on the next theme tweak. Also duplicates colors with `atlas_style_tokens.py`'s `GOLD = "#D4AF37"` — per CLAUDE.md these should be unified, not split between "embed color" and "render token" namespaces.

## Findings

### WARNING #1: `_MODULE_MAP` and class attributes are two independent sources of truth
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_colors.py:26-67`
**Confidence:** 0.95
**Risk:** `SPORTSBOOK = discord.Color(0x1A73E8)` on line 26 and `"sportsbook": 0x1A73E8` on line 56 are not linked. If a designer updates `SPORTSBOOK` to `0x0F5FC0`, `by_module("sportsbook")` still returns the old value. The two must be hand-synchronized every time.
**Vulnerability:** Classic "two caches of the same value" drift. No test, no assert, no runtime check binds the two representations. `by_module()` becomes a silent regression surface.
**Impact:** Dynamic module-lookup (`AtlasColors.by_module(...)`) drifts from static references (`AtlasColors.SPORTSBOOK`), producing inconsistent Discord embed colors across the same bot session. Hub embeds via direct class access will look different from programmatically-colored embeds.
**Fix:** Derive `_MODULE_MAP` from the class attributes:
```python
_MODULE_MAP = {
    "sportsbook": SPORTSBOOK.value,
    "casino": CASINO.value,
    ...
}
```
Or better, build it lazily in `by_module()` via a class-level dict comprehension over `vars(cls)`.

### WARNING #2: `TSL_GOLD` and `CASINO` are identical; `GOLD` in `atlas_style_tokens` is a third copy
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_colors.py:27, 38`
**Confidence:** 0.9
**Risk:** `CASINO = 0xD4AF37` (line 27) and `TSL_GOLD = 0xD4AF37` (line 38) are the same hex. Meanwhile `atlas_style_tokens.py:13` defines `GOLD = "#D4AF37"` as the render-side constant. Three copies of the same literal, no shared source.
**Vulnerability:** Per CLAUDE.md: "Single source of truth for colors, fonts, spacing, layout" lives in `atlas_style_tokens.py`. `atlas_colors.py` violates that by duplicating the canonical gold. Any rebrand must edit all three.
**Impact:** Palette drift on a rebrand. The casino embed color and the rendered casino card gold can diverge silently.
**Fix:** Import from the canonical source, e.g., `from atlas_style_tokens import Tokens` then `CASINO = discord.Color(int(Tokens.GOLD.lstrip('#'), 16))`. Or: make `atlas_style_tokens.py` the upstream and generate `atlas_colors.py` from it.

### OBSERVATION #1: `by_module()` silently lowercases and falls back without logging
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_colors.py:69-80`
**Confidence:** 0.7
**Risk:** A typo like `AtlasColors.by_module("sportbook")` (missing 's') silently returns `INFO` blue. No warning, no log, no exception. A developer who mistypes in production ships a wrong-colored embed forever.
**Vulnerability:** Silent fallback without observability. Per CLAUDE.md attack surface: "observability gaps that would hide failure or make recovery harder."
**Impact:** Typos produce wrong colors silently. Low-severity, but easy to catch later.
**Fix:** Log at `WARNING` level when the fallback path is taken, e.g., `log.warning("Unknown module color: %s, using INFO fallback", module_name)`.

### OBSERVATION #2: `TSL_BLACK`'s comment contradicts its use vs `TSL_DARK`
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_colors.py:39-40`
**Confidence:** 0.65
**Risk:** The comments say `TSL_DARK` is "Near-black background" and `TSL_BLACK` is "Sportsbook embed background (slightly lighter than TSL_DARK)" — but a naïve reader would expect `TSL_BLACK` to be *blacker*. The naming is inverted relative to reality. Any future refactor is likely to collapse the "wrong" one.
**Vulnerability:** Misleading naming on a style constant. The first principle of naming: the name should match the thing.
**Impact:** Design smell; potential for the wrong constant to be referenced in a rename PR.
**Fix:** Rename to `TSL_BG_DARKEST` / `TSL_BG_SPORTSBOOK` or reorder so the "blacker" one is named `TSL_BLACK`.

### OBSERVATION #3: `GENESIS` color has "draft (future)" comment but `_MODULE_MAP` doesn't include "genesis" as an alias
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_colors.py:34, 64`
**Confidence:** 0.5
**Risk:** The `GENESIS` class attribute exists and is mapped in `_MODULE_MAP` (line 64) but the comment on line 34 says "draft (future)" — implying it's a placeholder. Per CLAUDE.md, `genesis_cog.py` is actively loaded (cog #6). The comment is stale.
**Vulnerability:** Stale comment on a live constant — readers will assume the color is aspirational and override it locally.
**Impact:** Design/comment rot. Low impact, but a classic "the comment lied" smell.
**Fix:** Remove "(future)" from the comment on line 34.

## Cross-cutting Notes

The two-source-of-truth pattern (`SPORTSBOOK` class attr + `"sportsbook"` dict entry) is a concrete example of what `atlas_style_tokens.py` also does with `_CSS_MAP` — generating a CSS mapping from class attributes. The pattern there is already "declare once, derive the mapping" via a single dict. `atlas_colors.py` should adopt the same pattern to eliminate the drift surface. Also, the split between `atlas_colors.py` (embed colors) and `atlas_style_tokens.py` (render colors) violates the CLAUDE.md "single source of truth" mandate — they should be one module, or one should import from the other.
