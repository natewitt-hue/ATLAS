# ATLAS Codebase Audit Report — 2026-03-18

## Section 1: Executive Summary

| # | Issue | Severity | File(s) | Line(s) | Fix |
|---|-------|----------|---------|---------|-----|
| 1 | Gold color mismatch (`0xC9962A` vs `#D4AF37`) | **Critical** | `atlas_colors.py` | 38 | Update `TSL_GOLD` to `0xD4AF37` |
| 2 | 11 hardcoded Gemini personas | **High** | `oracle_cog.py` (9), `codex_cog.py` (1), `polymarket_cog.py` (1) | See §5 | Replace with `get_persona()` |
| 3 | 14 files using `discord.Color.*()` | **High** | boss_cog, flow_sportsbook, sentinel_cog, casino/games/\*, oracle_cog, commish_cog, roster | See §3 | Replace with `AtlasColors` constants |
| 4 | Duplicate `CATEGORY_COLORS` across systems | **Medium** | `polymarket_cog.py:173`, `prediction_html_renderer.py:29` | — | Single source + conversion helper |
| 5 | Duplicate `_american_fmt()` | **Medium** | `sportsbook_cards.py:740` | 740 | Import from `odds_utils` |
| 6 | Duplicate `_fmt_volume()` / `fmt_volume()` | **Medium** | `prediction_html_renderer.py:456`, `polymarket_cog.py:691` | — | Extract to shared `format_utils.py` |
| 7 | 3 TODOs outstanding | **Low** | `constants.py:3`, `sentinel_cog.py:53,1354` | — | Document/defer |
| 8 | Renderer hardcoded typography/spacing | **Low** | 6 renderer files (~1000+ lines) | — | Future PR |

---

## Section 2: Stale Code Inventory

All passes **CLEAN** — no violations found:

| Check | Result | Notes |
|-------|--------|-------|
| QUARANTINE imports | ✅ Clean | No active code imports from `QUARANTINE/` or `Quarantine_Archive/` |
| PIL/Pillow in card rendering | ✅ Clean | Only `crop_icons.py`, `process_icons.py`, `upload_emoji.py` (icon tools — acceptable) |
| Old class names (`ATLASCard`, `CardSection`, `HTMLRenderer`) | ✅ Clean | Only exist in quarantined files |
| `render_to_png()` calls | ✅ Clean | No active references |
| Dead imports | ✅ Clean | No clearly unused imports (try/except guarded imports excluded per spec) |

---

## Section 3: Color/Style Audit

### Gold Unification

| Location | Current Value | Target Value | Format |
|----------|--------------|--------------|--------|
| `atlas_colors.py:38` (TSL_GOLD) | `0xC9962A` (darker amber) | `0xD4AF37` | Discord int |
| `atlas_colors.py:27` (CASINO) | `0xC9962A` | `0xD4AF37` | Discord int |
| `atlas_colors.py:56` (_MODULE_MAP "casino") | `0xC9962A` | `0xD4AF37` | hex int |
| `atlas_style_tokens.py:13` (GOLD) | `#D4AF37` ✅ | — already correct | CSS hex |
| `constants.py:14` (ATLAS_GOLD) | Re-export of TSL_GOLD | Inherits automatically | — |

**Canonical value:** `#D4AF37` / `0xD4AF37` (traditional gold, brighter).

### discord.Color.*() Violations

| File | Lines | Methods Used |
|------|-------|-------------|
| `boss_cog.py` | 1565, 1607, 1816, 1872, 1911, 1945, 2016, 2246 | `.gold()`, `.blurple()`, `.green()`, `.red()`, `.orange()` |
| `flow_sportsbook.py` | ~14 occurrences | Various |
| `sentinel_cog.py` | 676-679 (RULING_COLORS), 849, 893 | `.green()`, `.red()`, `.gold()`, `.greyple()` |
| `genesis_cog.py` | 442 | `.from_rgb()` — but `BAND_CONFIG` already uses AtlasColors ✅ |
| `casino/games/blackjack.py` | Multiple | Various |
| `casino/games/coinflip.py` | Multiple | Various |
| `casino/games/crash.py` | Multiple | Various |
| `casino/games/slots.py` | Multiple | Various |
| `oracle_cog.py` | Multiple | Various |
| `commish_cog.py` | Multiple | Various |
| `roster.py` | Multiple | Various |

### Color Mapping Guide

| `discord.Color.*()` | → `AtlasColors` Constant |
|---------------------|--------------------------|
| `.gold()` | `AtlasColors.TSL_GOLD` |
| `.green()` | `AtlasColors.SUCCESS` |
| `.red()` | `AtlasColors.ERROR` |
| `.blurple()` | `AtlasColors.INFO` |
| `.greyple()` | `AtlasColors.INFO` |
| `.orange()` | `AtlasColors.WARNING` |
| `.from_rgb(...)` | Evaluate case-by-case |

### Domain-Specific Color Dicts

| Dict | File | Status |
|------|------|--------|
| `RULING_COLORS` | `sentinel_cog.py:676-679` | Structure OK, **values** must use AtlasColors |
| `BAND_CONFIG` | `genesis_cog.py:423-427` | Already uses AtlasColors ✅ |
| `CATEGORY_COLORS` | `polymarket_cog.py:173` + `prediction_html_renderer.py:29` | **Duplicate** — consolidate |

### Renderer Typography/Spacing (Deferred)

Extensive hardcoding across 6 renderer files. Compliance estimates:
- `flow_cards.py` — ~70% (uses CSS vars, some hardcoded font names)
- `casino_html_renderer.py` — ~40% (font names hardcoded, CSS vars for colors)
- `session_recap_renderer.py` — ~30%
- `highlight_renderer.py` — ~25%
- `prediction_html_renderer.py` — ~20%
- `ledger_renderer.py` — unassessed

**Decision:** Too large for this PR (~1000+ lines). Flagged for dedicated follow-up.

---

## Section 4: Duplicate Code Map

| # | Duplicate | Location A | Location B | Resolution |
|---|-----------|-----------|-----------|------------|
| 1 | `_american_fmt()` / `american_to_str()` | `sportsbook_cards.py:740` | `odds_utils.py:4` | Identical impl. Delete local, import from `odds_utils`. |
| 2 | `_fmt_volume()` / `fmt_volume()` | `prediction_html_renderer.py:456` | `polymarket_cog.py:691` | Minor K formatting diff (`.0f` vs `.1f`). Extract to `format_utils.py`. |
| 3 | `CATEGORY_COLORS` (int) / `CATEGORY_COLORS` (hex) | `polymarket_cog.py:173` | `prediction_html_renderer.py:29` | Keep in polymarket_cog as source. Add hex conversion helper. |
| 4 | `_now()` / `_now_ts()` | `flow_wallet.py:56` | `highlight_renderer.py:30` | Different purposes (ISO vs display). Rename for clarity only — no consolidation. |

---

## Section 5: Pattern Violations

### Hardcoded Personas (11 violations)

| File | Line | Current String | Fix |
|------|------|---------------|-----|
| `oracle_cog.py` | 550 | `"You are ATLAS Oracle — the intelligence system..."` | `get_persona("analytical")` |
| `oracle_cog.py` | 1647 | `"You are ATLAS Oracle, TSL analytics intelligence..."` | `get_persona("analytical")` |
| `oracle_cog.py` | 2459 | `"You are ATLAS Oracle, TSL predictive intelligence..."` | `get_persona("analytical")` |
| `oracle_cog.py` | 2782 | `"You are ATLAS Oracle, TSL matchup intelligence..."` | `get_persona("analytical")` |
| `oracle_cog.py` | 3117 | `"You are ATLAS Echo. Write a punchy 2–3 sentence..."` | `get_persona("casual")` |
| `oracle_cog.py` | 3516 | `"You are ATLAS in Scout mode..."` | `get_persona("analytical")` |
| `oracle_cog.py` | 3872 | `"You are ATLAS Echo. Write a vivid 3–4 sentence..."` | `get_persona("casual")` |
| `oracle_cog.py` | +2 more | Additional inline persona strings | `get_persona()` |
| `codex_cog.py` | 82-87 | `ATLAS_PERSONA` constant + fallback at line 97 | Remove constant, use `get_persona("analytical")` |
| `polymarket_cog.py` | 2807 | `"You are ATLAS, the editorial voice..."` | Remove fallback string |

### Other Pattern Checks

| Check | Result | Notes |
|-------|--------|-------|
| `generate_content` not in executor | ✅ Clean* | `sentinel_cog.py:2420` is in `_sync` function (already called via executor) |
| `view=None` in `followup.send()` | ✅ Clean | No violations |
| `_startup_done` guard | ✅ Present | `bot.py:183,429-434` |
| Select menu >25 options | ⚠️ Low risk | `oracle_cog.py:3762` truncates to 25 ✅; lines 3764, 3790 unverified at runtime |
| Cog load guards | ✅ Present | `bot.py:213-218` — all loads in try/except |

### TODOs/FIXMEs/HACKs

| File | Line | Content |
|------|------|---------|
| `constants.py` | 3 | `TODO: Host on a permanent URL (GitHub raw, S3, Imgur) — signed Discord CDN link will expire` |
| `sentinel_cog.py` | 53 | `TODO: Unify on async httpx — requests is only used in _fetch_image_bytes()` |
| `sentinel_cog.py` | 1354 | `TODO: Extract parity state into standalone module to break genesis ↔ sentinel coupling` |

---

## Section 6: Recommended Fix Order

### Critical — Visual inconsistency
1. Gold unification: Update `TSL_GOLD` from `0xC9962A` → `0xD4AF37` in `atlas_colors.py`

### High — Behavioral drift
2. Replace 11 hardcoded personas with `get_persona()` calls
3. Replace `discord.Color.*()` with `AtlasColors` constants across 14 files

### Medium — Consolidation
4. Delete duplicate `_american_fmt()`, import from `odds_utils`
5. Extract shared `fmt_volume()` to `format_utils.py`
6. Consolidate `CATEGORY_COLORS` to single source

### Deferred
7. Renderer typography/spacing tokenization (future PR)
8. TODO cleanup (constants CDN URL, httpx migration, parity extraction)
9. Timestamp helper renaming
