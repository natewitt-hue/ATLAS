# ATLAS Codebase Audit — Stale Code & Style Conformance

## Context

Old rendering code (Pillow-based) was replaced by an HTML→PNG pipeline (Playwright + `atlas_html_engine.py`). Old color constants, duplicate utilities, and deprecated patterns may still linger in production cog files. A preliminary exploration confirmed:

- **Clean areas:** No QUARANTINE imports, no PIL in card rendering, all Gemini calls properly wrapped in `run_in_executor()`, no `view=None` violations, `_startup_done` guard correct.
- **Real issues:** 7+ hardcoded personas in `oracle_cog.py`, ~10 files using `discord.Color.*()` instead of `AtlasColors`, duplicate utility functions (odds, volume, spread formatting), duplicated color dicts across cog/renderer pairs, two divergent gold values to unify.

This audit documents everything — clean passes serve as proof the area was checked.

## Scope

**Audit:** ALL `.py` files in project root and `casino/` directory.

**Skip:** `QUARANTINE/`, `Quarantine_Archive/`, `.claude/`, `.superpowers/`, `icons/`, `output/`, `test_*` files, utility scripts (`upload_emoji.py`, `process_icons.py`, `crop_icons.py`).

---

## Audit Passes

### Pass 1: Stale Code & Dead References

Search for code that references deprecated systems:

| Target | What to Find |
|--------|-------------|
| QUARANTINE imports | Any `import` or `from` referencing `QUARANTINE/` or `Quarantine_Archive/` |
| PIL/Pillow in cards | `from PIL`, `import PIL`, `Image.new`, `ImageDraw`, `ImageFont` used for card rendering (icon processing is OK) |
| Old class names | `ATLASCard`, `CardSection`, `HTMLRenderer` (old API) |
| Deprecated renders | `render_to_png()` or other old render functions |
| Dead code | Clearly unused imports (skip try/except guarded imports — those are intentional soft fallbacks). Do NOT attempt unreachable-code analysis. |

**Expected:** Mostly clean. Document as verified.

### Pass 2: Style Token Conformance

The style system has a centralization problem. Find every violation:

#### Color Violations

| Check | Detail |
|-------|--------|
| `discord.Color.*()` calls | Files using `.gold()`, `.red()`, `.green()`, `.blurple()`, `.greyple()`, `.orange()`, `.from_rgb()` instead of `AtlasColors` constants |
| Gold unification | Two visually distinct golds exist: `0xC9962A` / `#C9962A` (darker amber, AtlasColors.TSL_GOLD) and `#D4AF37` (traditional gold, atlas_style_tokens GOLD). **Canonical value: `#D4AF37` (0xD4AF37).** Update `AtlasColors.TSL_GOLD` to `0xD4AF37` and update `atlas_style_tokens.GOLD` to match. All downstream uses inherit automatically. |
| Local color dicts | `RULING_COLORS` in sentinel_cog, `BAND_CONFIG` in genesis_cog, `CATEGORY_COLORS` in polymarket_cog — classify as domain-specific (acceptable) vs duplicated (fix). **Note:** Lines inside accepted local dicts that use `discord.Color.*()` are still violations — the dict structure is acceptable but its values should use `AtlasColors` constants. |
| Cross-system duplication | `CATEGORY_COLORS` duplicated between `polymarket_cog.py` (Discord embed ints) and `prediction_html_renderer.py` (CSS hex strings) — consolidate to single source |
| Hardcoded hex in HTML | Colors in HTML renderers that should reference `atlas_style_tokens.py` |

#### Typography/Spacing Violations

| Check | Detail |
|-------|--------|
| Hardcoded fonts | Font names in HTML renderer files (see Rendering Stack in CLAUDE.md: `casino_html_renderer.py`, `highlight_renderer.py`, `session_recap_renderer.py`, `pulse_renderer.py`, `prediction_html_renderer.py`, `card_renderer.py`, `ledger_renderer.py`, `flow_cards.py`, `sportsbook_cards.py`) not using `Tokens.FONT_DISPLAY` or `Tokens.FONT_MONO` |
| Hardcoded sizes | Font sizes in renderer files not using `Tokens.FONT_SIZE_*` |
| Hardcoded spacing | Spacing values in renderer files not using `Tokens.SPACING_*` |

### Pass 3: Duplicate Code & Utility Sprawl

Known duplicates to verify and catalog:

| Function | Location A | Location B | Resolution |
|----------|-----------|-----------|------------|
| `_american_fmt()` | `sportsbook_cards.py:740` | `odds_utils.american_to_str()` | Import from `odds_utils` |
| `fmt_spread()` | `flow_sportsbook.py:732` | Verify no local `fmt(s)` shadows at inner scope | Confirm single definition at module level |
| `_fmt_volume()` | `prediction_html_renderer.py:456` | `polymarket_cog.py:691` (`fmt_volume`) | Consolidate |
| Timestamp helpers | `flow_wallet.py:56` (`_now()`) | `highlight_renderer.py:30` (`_now_ts()`) | Consider shared util |
| PnL formatting | `session_recap_renderer.py:64` | — | Evaluate if needed elsewhere |
| Color mappers | Scattered `_*_color()` in oracle_cog, ledger_renderer, prediction_html_renderer | — | Evaluate centralization |

Also search for any additional duplicates not caught in preliminary scan.

### Pass 4: Architecture & Pattern Violations

Check against CLAUDE.md rules:

| Rule | What to Check |
|------|--------------|
| **Hardcoded personas** | Any Gemini system prompt with literal "You are ATLAS..." instead of `get_persona()` from `echo_loader.py`. Known: 7 in `oracle_cog.py`. `codex_cog.py` has partial fix (`_answer_persona()` calls `get_persona()` but `ATLAS_PERSONA` string still exists as live fallback) — flag for cleanup. `bot.py` fallback stub is acceptable (graceful degradation). |
| Gemini executor wrapping | `generate_content()` calls not in `loop.run_in_executor()` (expect clean) |
| `view=None` | Passed to `followup.send()` (expect clean) |
| `_startup_done` | Guard present in `bot.py` (expect clean) |
| Select menu cap | `@discord.ui.select` with >25 options without pagination |
| Cog load guards | Missing `try/except` on cog loads in `setup_hook()` |
| TODO/FIXME/HACK | Catalog all with file, line, and content |

---

## Output Format

### Section 1: Executive Summary

| Issue | Severity | File(s) | Line(s) | Fix Description |
|-------|----------|---------|---------|-----------------|
| ... | Critical/High/Medium/Low | ... | ... | ... |

### Section 2: Stale Code Inventory

Every instance of deprecated code with file path, line number, what it references, and replacement.

### Section 3: Color/Style Audit

**Color mapping table:**
- Every hardcoded color value → file/line → correct `AtlasColors` constant or `Tokens` value
- Every local style constant → centralized token it duplicates

**Gold unification plan:**
- Chosen canonical value
- All locations to update
- Format conversion approach (int for Discord API, hex for CSS)

### Section 4: Duplicate Code Map

Each set of duplicated functions with file locations and consolidation recommendation.

### Section 5: Pattern Violations

Each CLAUDE.md rule violation with file/line and fix.

### Section 6: Recommended Fix Order

1. **Critical** — breaks things or causes visual inconsistency
2. **Cleanup** — consolidation, dead code removal
3. **Nice-to-have** — style improvements, TODO resolution

---

## Execution Strategy

### Session Flow

1. Read `CLAUDE.md` and memory files
2. Use `superpowers:dispatching-parallel-agents` — 4 Explore agents in parallel:
   - **Agent 1:** Pass 1 (Stale Code & Dead References)
   - **Agent 2:** Pass 2 (Style Token Conformance)
   - **Agent 3:** Pass 3 (Duplicate Code & Utility Sprawl)
   - **Agent 4:** Pass 4 (Architecture & Pattern Violations)
3. Consolidate findings into the output format above
4. Present findings and get user approval before implementing fixes
5. Use `superpowers:brainstorming` to decide consolidation strategy (e.g., where shared utils live, how to restructure color dicts)
6. Use `superpowers:writing-plans` to create fix implementation plan
7. Execute fixes on a feature branch
8. Use `superpowers:verification-before-completion` to verify no regressions
9. PR review via `code-review:code-review`

### Skills Used

| Skill | When |
|-------|------|
| `superpowers:dispatching-parallel-agents` | Run 4 audit passes concurrently |
| `superpowers:brainstorming` | Before implementing fixes — decide consolidation strategy |
| `superpowers:writing-plans` | After audit — plan fix implementation |
| `superpowers:verification-before-completion` | After fixes — verify no regressions |
| `code-review:code-review` | PR review for the fix branch |

---

## Key Decisions

- **Gold unification:** Canonical value is `#D4AF37` / `0xD4AF37`. Update `AtlasColors.TSL_GOLD` from `0xC9962A` to `0xD4AF37`. `atlas_style_tokens.GOLD` already uses `#D4AF37`. All downstream uses inherit.
- **Domain-specific color dicts:** Acceptable if unique to one system (e.g., team colors in oracle). Not acceptable if duplicated across systems (e.g., CATEGORY_COLORS in both polymarket_cog and prediction_html_renderer).
- **Clean passes documented:** Even areas that come back clean are included as proof of verification.
