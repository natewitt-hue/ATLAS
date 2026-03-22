# ATLAS Unified Style System — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify all 21 card renders under a single style token system, HTML rendering engine with page pool, 2x DPI, and 480px mobile-first width.

**Architecture:** Create `atlas_style_tokens.py` (single source of truth for all visual constants) and `atlas_html_engine.py` (page pool + unified render API). Migrate all cards to use tokens via CSS custom properties. Existing HTML cards get tokenized in-place; Pillow-based ATLASCard hub cards get rewritten as HTML templates.

**Tech Stack:** Python 3.14, Playwright (async), CSS custom properties, base64-embedded fonts (Outfit + JetBrains Mono)

**Spec:** `docs/superpowers/specs/2026-03-16-unified-style-system-design.md`

---

## Chunk 1: Foundation

### Task 1: Create `atlas_style_tokens.py`

**Files:**
- Create: `atlas_style_tokens.py`

- [ ] **Step 1: Create the token module**

```python
"""
atlas_style_tokens.py — ATLAS Unified Style Token System
Single source of truth for all visual constants across every card renderer.
Generates CSS custom properties from Python constants.
"""

class Tokens:
    """All visual constants for ATLAS card rendering."""

    # ── Colors ──
    BG_PRIMARY = "#111111"
    BG_DEEP = "#0A0A0A"
    GOLD = "#D4AF37"
    GOLD_BRIGHT = "#F0D060"
    GOLD_DIM = "#8C7324"
    GOLD_LIGHT = "#FFDA50"
    WIN = "#4ADE80"
    WIN_DARK = "#22C55E"
    LOSS = "#F87171"
    LOSS_DARK = "#EF4444"
    PUSH = "#FBBF24"
    PUSH_DARK = "#D97706"
    TEXT_PRIMARY = "#e8e0d0"
    TEXT_SECONDARY = "#AAAAAA"
    TEXT_MUTED = "#888888"
    TEXT_DIM = "#555555"
    PANEL_BG = "rgba(255,255,255,0.04)"
    PANEL_BORDER = "rgba(255,255,255,0.08)"

    # ── Typography ──
    FONT_DISPLAY = "Outfit"
    FONT_MONO = "JetBrains Mono"
    FONT_XS = "11px"
    FONT_SM = "13px"
    FONT_BASE = "15px"
    FONT_LG = "18px"
    FONT_XL = "24px"
    FONT_HERO = "42px"
    FONT_DISPLAY_SIZE = "56px"

    # ── Spacing ──
    SPACE_XS = "4px"
    SPACE_SM = "8px"
    SPACE_MD = "12px"
    SPACE_LG = "16px"
    SPACE_XL = "24px"
    SPACE_XXL = "32px"
    CARD_PADDING = "16px"
    SECTION_GAP = "12px"

    # ── Layout ──
    CARD_WIDTH = 480  # px (int for Playwright viewport)
    DPI_SCALE = 2
    BORDER_RADIUS = "8px"
    BORDER_RADIUS_SM = "4px"
    STATUS_BAR_HEIGHT = "5px"
    NOISE_OPACITY = "0.035"

    # ── CSS variable mapping ──
    _CSS_MAP = {
        "bg": BG_PRIMARY,
        "bg-deep": BG_DEEP,
        "gold": GOLD,
        "gold-bright": GOLD_BRIGHT,
        "gold-dim": GOLD_DIM,
        "gold-light": GOLD_LIGHT,
        "win": WIN,
        "win-dark": WIN_DARK,
        "loss": LOSS,
        "loss-dark": LOSS_DARK,
        "push": PUSH,
        "push-dark": PUSH_DARK,
        "text-primary": TEXT_PRIMARY,
        "text-sub": TEXT_SECONDARY,
        "text-muted": TEXT_MUTED,
        "text-dim": TEXT_DIM,
        "panel-bg": PANEL_BG,
        "panel-border": PANEL_BORDER,
        "font-display": FONT_DISPLAY,
        "font-mono": FONT_MONO,
        "font-xs": FONT_XS,
        "font-sm": FONT_SM,
        "font-base": FONT_BASE,
        "font-lg": FONT_LG,
        "font-xl": FONT_XL,
        "font-hero": FONT_HERO,
        "font-display-size": FONT_DISPLAY_SIZE,
        "space-xs": SPACE_XS,
        "space-sm": SPACE_SM,
        "space-md": SPACE_MD,
        "space-lg": SPACE_LG,
        "space-xl": SPACE_XL,
        "space-xxl": SPACE_XXL,
        "card-padding": CARD_PADDING,
        "section-gap": SECTION_GAP,
        "card-width": f"{CARD_WIDTH}px",
        "border-radius": BORDER_RADIUS,
        "border-radius-sm": BORDER_RADIUS_SM,
        "status-bar-height": STATUS_BAR_HEIGHT,
        "noise-opacity": NOISE_OPACITY,
    }

    @classmethod
    def to_css_vars(cls) -> str:
        """Generate CSS :root block with all token variables."""
        lines = [f"  --{k}: {v};" for k, v in cls._CSS_MAP.items()]
        return ":root {\n" + "\n".join(lines) + "\n}"
```

- [ ] **Step 2: Verify tokens load**

Run: `python -c "from atlas_style_tokens import Tokens; print(Tokens.to_css_vars()[:200])"`
Expected: CSS :root block with `--bg: #111111; --gold: #D4AF37; ...`

- [ ] **Step 3: Commit**

```bash
git add atlas_style_tokens.py
git commit -m "feat: add unified style token system (atlas_style_tokens.py)"
```

---

### Task 2: Create `atlas_html_engine.py`

**Files:**
- Create: `atlas_html_engine.py`
- Reference: `card_renderer.py:108-136` (browser singleton to move)
- Reference: `casino/renderer/casino_html_renderer.py:36-68` (font loading to move)

- [ ] **Step 1: Create the engine module**

The engine must include:
1. Browser singleton (moved from `card_renderer.py`)
2. Font loading + base64 caching (moved from `casino_html_renderer.py`)
3. PagePool class with acquire timeout + viewport reset
4. `render_card()` — unified render function
5. `wrap_card()` — base HTML template with token CSS injection
6. `init_pool()` / `drain_pool()` — lifecycle hooks for bot.py

Key implementation details:
- Copy `_get_browser()` and `close_browser()` from `card_renderer.py:108-136`
- Copy `_load_font_b64()`, `_font_face_css()`, and `_FONT_CACHE` from `casino/renderer/casino_html_renderer.py:32-68`
- Copy `_card_b64()`, `card_img_src()`, `card_back_src()` asset helpers from `casino/renderer/casino_html_renderer.py:72-94`
- Copy `SLOT_ICON_CONFIG` dict from `casino/renderer/casino_html_renderer.py:101-108`
- Copy `_slot_icon_b64()`, `slot_icon_src()` from `casino/renderer/casino_html_renderer.py:98-127`
- Copy `_esc()` HTML escape helper from `casino/renderer/casino_html_renderer.py:132-133` — expose as `esc()` (public API)
- PagePool uses `asyncio.wait_for` with 10s timeout on acquire
- PagePool resets viewport on release and recycles pages after 100 renders
- `render_card()` uses `wait_until="domcontentloaded"` (not `networkidle`)
- `wrap_card()` injects `Tokens.to_css_vars()` and `_font_face_css()` into base template
- Base template includes noise texture SVG overlay (copy from `casino_html_renderer.py:174-181`)
- `deviceScaleFactor: 2` set on page creation in pool warm

The `_base_css()` function from `casino_html_renderer.py:138-410` contains shared CSS for headers, data grids, footers, streak badges, near-miss banners, status bars, and gold dividers. This CSS should be included in `wrap_card()` output so all cards inherit these shared styles. Copy it and replace hardcoded values with `var(--token-name)` references. Key replacements:
- `width: 700px` → `width: var(--card-width)`
- `border-radius: 14px` → `border-radius: var(--border-radius)`
- Hardcoded color values like `#111111` → `var(--bg)`, `#D4AF37` → `var(--gold)`, etc.
- Hardcoded font sizes → `var(--font-*)` references

- [ ] **Step 2: Verify engine imports and pool creation work**

Run: `python -c "from atlas_html_engine import render_card, wrap_card, init_pool; print('Engine loaded OK')"`
Expected: `Engine loaded OK`

- [ ] **Step 3: Commit**

```bash
git add atlas_html_engine.py
git commit -m "feat: add unified HTML render engine with page pool (atlas_html_engine.py)"
```

---

### Task 3: Wire engine into `bot.py`

**Files:**
- Modify: `bot.py:187-245` (setup_hook) and `bot.py:730-737` (shutdown)

- [ ] **Step 1: Add pool init to setup_hook**

In `bot.py`, after the wallet/UI state init block (~line 233) and before `bot.tree.sync()`, add:

```python
    # HTML render engine — page pool for card rendering
    try:
        from atlas_html_engine import init_pool
        await init_pool()
        print("ATLAS: HTML render engine initialized.")
    except Exception as e:
        print(f"ATLAS: Render engine init failed: {e}")
```

- [ ] **Step 2: Add pool drain to shutdown**

In `bot.py`, in the shutdown section (~line 733), replace the existing `close_browser` call with:

```python
        try:
            from atlas_html_engine import drain_pool
            await drain_pool()
        except Exception:
            pass
```

- [ ] **Step 3: Bump ATLAS_VERSION**

Change `bot.py:166`:
```python
ATLAS_VERSION = "2.14.0"  # Unified style system
```

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "feat: wire HTML render engine pool init/drain into bot lifecycle"
```

---

## Chunk 2: Migrate Existing HTML Cards (Groups B-F)

All cards in Groups B-F are already HTML-rendered via Playwright. The migration pattern is:

1. Replace the `_render_card_html()` or `_render_html_to_png()` call with `render_card()` from `atlas_html_engine`
2. Replace `_wrap_card()` calls with `wrap_card()` from `atlas_html_engine` (which injects tokens CSS)
3. Remove the local `_base_css()` / `_wrap_card()` / `_render_card_html()` functions
4. Remove local `_font_face_css()` / `_load_font_b64()` imports (now in engine)
5. Update the render width from 700/720 to 480 (default in engine)

**The CSS token replacement is NOT needed per-card.** Since `_base_css()` is a shared function in `casino_html_renderer.py`, tokenizing it once (in Task 2 when building `wrap_card()`) automatically applies to all cards that use it. Per-card game-specific CSS that uses hardcoded colors should also be updated to use `var(--*)` references.

### Task 4: Migrate `casino_html_renderer.py` (5 casino games)

**Files:**
- Modify: `casino/renderer/casino_html_renderer.py`

This file contains 5 game card renderers (blackjack, slots, crash, coinflip, scratch) plus shared CSS/builders. The migration:

- [ ] **Step 1: Update imports**

At top of file, add:
```python
from atlas_html_engine import render_card, wrap_card, card_img_src, card_back_src, slot_icon_src
```

- [ ] **Step 2: Remove local duplicates**

Remove or comment out these local functions that are now in the engine:
- `_load_font_b64()` and `_FONT_CACHE` (lines 32-43)
- `_font_face_css()` (lines 46-68)
- `_card_b64()` and `_CARD_B64_CACHE` (lines 72-83)
- `card_img_src()` and `card_back_src()` (lines 86-94)
- `_slot_icon_b64()` and `_SLOT_ICON_B64_CACHE` (lines 98-122)
- `slot_icon_src()` (lines 125-127)
- `_esc()` (lines 132-133) — replace with `from atlas_html_engine import esc`
- `_base_css()` (lines 138-410) — now part of engine's `wrap_card()`
- `_wrap_card()` (lines 541-555) — replaced by engine's `wrap_card()`
- `_render_card_html()` (lines 560-587) — replaced by engine's `render_card()`

- [ ] **Step 3: Update `_wrap_card` calls**

Replace all calls to `_wrap_card(status_class, content)` with `wrap_card(status_class, content)` from the engine. The engine's `wrap_card()` needs to accept a `status_class` parameter to set the status bar. Update the engine's `wrap_card()` signature:

```python
def wrap_card(body_html: str, status_class: str = "") -> str:
```

And include the status bar div in the template:
```html
<div class="card">
  <div class="status-bar {status_class}"></div>
  {body}
</div>
```

- [ ] **Step 4: Update render calls**

Replace all `await _render_card_html(html)` with `await render_card(html)`. The default width in the engine is 480, which replaces the old 720.

For each of the 5 render functions:
- `render_blackjack_card()` line 798: `return await render_card(html)`
- `render_slots_card()` (find the return): `return await render_card(html)`
- `render_crash_card()` (find the return): `return await render_card(html)`
- `render_coinflip_card()` (find the return): `return await render_card(html)`
- `render_scratch_card_v6()` (find the return): `return await render_card(html)`

- [ ] **Step 5: Update game-specific CSS**

Each game has inline `<style>` blocks with hardcoded values. Replace hardcoded colors/sizes with `var(--*)` tokens. Example replacements:
- `font-size: 14px` → `font-size: var(--font-base)`
- `color: #FBBF24` → `color: var(--push)`
- `font-size: 18px` → `font-size: var(--font-lg)`
- `font-size: 48px` → `font-size: var(--font-hero)` (or keep specific if intentional)

- [ ] **Step 6: Verify blackjack renders**

Run a quick test: create a test script that calls `render_blackjack_card()` with sample data and saves the PNG:

```python
import asyncio
from casino.renderer.casino_html_renderer import render_blackjack_card

async def test():
    png = await render_blackjack_card(
        dealer_hand=[("S", "3"), ("H", "K"), ("D", "J")],
        player_hand=[("S", "10"), ("C", "9")],
        dealer_score=23, player_score=19,
        hide_dealer=False, status="Dealer Busts!",
        wager=100, payout=200, balance=3640,
        player_name="TestPlayer"
    )
    with open("test_bj_unified.png", "wb") as f:
        f.write(png)
    print(f"Rendered {len(png)} bytes")

asyncio.run(test())
```

Run: `python test_render.py`
Expected: `test_bj_unified.png` created, visually matches current design but at 480px width

- [ ] **Step 7: Commit**

```bash
git add casino/renderer/casino_html_renderer.py
git commit -m "refactor: migrate casino game cards to unified engine + tokens"
```

---

### Task 5: Migrate `highlight_renderer.py` (5 highlight cards)

**Files:**
- Modify: `casino/renderer/highlight_renderer.py`

- [ ] **Step 1: Update imports and remove local render/wrap functions**

Add `from atlas_html_engine import render_card, wrap_card, _esc` at top.
Remove any local `_render_card_html`, `_wrap_card`, `_base_css`, `_font_face_css` if present (this file may import from `casino_html_renderer` — trace the imports).

- [ ] **Step 2: Replace render calls**

For all 5 render functions (`render_jackpot_card`, `render_pvp_card`, `render_crash_lms_card`, `render_prediction_card`, `render_parlay_card`), replace:
- `await _render_card_html(html, width=700)` → `await render_card(html)`
- Any `_wrap_card()` calls → `wrap_card()`

- [ ] **Step 3: Tokenize CSS**

Replace hardcoded colors/sizes in inline CSS with `var(--*)` references.

- [ ] **Step 4: Commit**

```bash
git add casino/renderer/highlight_renderer.py
git commit -m "refactor: migrate highlight cards to unified engine + tokens"
```

---

### Task 6: Migrate `session_recap_renderer.py` + `pulse_renderer.py` (Flow Live)

**Files:**
- Modify: `casino/renderer/session_recap_renderer.py`
- Modify: `casino/renderer/pulse_renderer.py`

- [ ] **Step 1: Session recap — update imports, remove local render, tokenize CSS, 700→480**

Same pattern: import from engine, remove local duplicates, replace render calls, tokenize inline CSS, update width.

- [ ] **Step 2: Pulse — update imports, remove local render, tokenize CSS**

Pulse is already 480px. Just tokenize and swap to engine.

- [ ] **Step 3: Commit**

```bash
git add casino/renderer/session_recap_renderer.py casino/renderer/pulse_renderer.py
git commit -m "refactor: migrate flow live cards to unified engine + tokens"
```

---

### Task 7: Migrate `prediction_html_renderer.py` (5 prediction cards)

**Files:**
- Modify: `casino/renderer/prediction_html_renderer.py`

- [ ] **Step 1: Update imports, remove local render/wrap, tokenize CSS**

Same pattern as above for all 5 prediction card functions.

- [ ] **Step 2: Commit**

```bash
git add casino/renderer/prediction_html_renderer.py
git commit -m "refactor: migrate prediction market cards to unified engine + tokens"
```

---

### Task 8: Migrate `card_renderer.py` trade card + `ledger_renderer.py`

**Files:**
- Modify: `card_renderer.py` (root — trade card)
- Modify: `casino/renderer/ledger_renderer.py`

- [ ] **Step 1: Trade card — import from engine, tokenize, swap render call**

In `card_renderer.py`:
- Keep the trade-specific HTML builder functions (`render_trade_card`, `_player_card_html`, etc.)
- Remove `_get_browser()`, `close_browser()`, `_load_font_b64()`, `_font_face_css()` — now in engine
- Import `render_card`, `wrap_card` from engine
- Update `render_trade_card()` to use `await render_card(html)` instead of its own Playwright code
- Tokenize inline CSS

- [ ] **Step 2: Ledger — import from engine, swap `_render_html_to_png()` to `render_card()`**

In `ledger_renderer.py`, replace `_render_html_to_png()` with `render_card()` from engine.

- [ ] **Step 3: Commit**

```bash
git add card_renderer.py casino/renderer/ledger_renderer.py
git commit -m "refactor: migrate trade card + ledger to unified engine + tokens"
```

---

## Chunk 3: Migrate Pillow Hub Cards (Group A) + Cleanup

### Task 9: Create HTML templates for Flow Hub card

**Files:**
- Modify: `flow_cards.py`

- [ ] **Step 1: Study current `build_flow_card()` output**

Read `flow_cards.py` to understand what ATLASCard sections are used (hero numbers, info panels, stat grids, sparklines, tickers, etc.). Build equivalent HTML template.

- [ ] **Step 2: Rewrite `build_flow_card()` to use HTML + `render_card()`**

Replace the ATLASCard builder pattern with an HTML template builder function. Use `wrap_card()` for base styles. Return `await render_card(html)` instead of `ATLASCard.render()`.

Update the caller to handle `bytes` return type instead of `Image.Image`. The `discord.File` creation changes from:
```python
buf = io.BytesIO(); img.save(buf, format='PNG'); buf.seek(0)
discord.File(buf, filename="flow.png")
```
to:
```python
discord.File(io.BytesIO(png_bytes), filename="flow.png")
```

- [ ] **Step 3: Commit**

```bash
git add flow_cards.py
git commit -m "refactor: migrate flow hub card from Pillow to HTML"
```

---

### Task 10: Create HTML templates for Sportsbook + Stats cards

**Files:**
- Modify: `sportsbook_cards.py`

- [ ] **Step 1: Study current `build_sportsbook_card()` and `build_stats_card()`**

Read the ATLASCard section builders. Rebuild as HTML templates.

- [ ] **Step 2: Rewrite both functions to use HTML + `render_card()`**

Same pattern as Task 9. Update callers for bytes return type.

- [ ] **Step 3: Commit**

```bash
git add sportsbook_cards.py
git commit -m "refactor: migrate sportsbook + stats cards from Pillow to HTML"
```

---

### Task 11: Cleanup — Remove deprecated files

**Files:**
- Delete: `atlas_card_renderer.py`
- Delete: `casino/renderer/card_renderer.py`

- [ ] **Step 1: Verify no remaining imports**

Run: `grep -r "from atlas_card_renderer import\|import atlas_card_renderer\|from casino.renderer.card_renderer import\|import casino.renderer.card_renderer" --include="*.py" .`

Expected: No results (all references already migrated)

- [ ] **Step 2: Delete the files**

```bash
rm atlas_card_renderer.py casino/renderer/card_renderer.py
```

- [ ] **Step 3: Update `CLAUDE.md`**

Update the architecture section to reflect the new unified rendering stack:
- Remove references to split Pillow/Playwright stack
- Document `atlas_style_tokens.py` and `atlas_html_engine.py`
- Update the module map

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: remove deprecated Pillow renderers, update docs"
```

---

## Chunk 4: Verification

### Task 12: Render all 21 card types

- [ ] **Step 1: Create a comprehensive test script**

Write `test_all_renders.py` that imports and calls every render function with sample data, saves PNGs, and measures latency. Should cover:
- Blackjack, Slots, Crash, Coinflip, Scratch (casino games)
- 5 Highlight cards (jackpot, pvp, crash LMS, prediction, parlay)
- Session Recap, Pulse Dashboard
- 5 Prediction Market cards
- Trade Card
- Flow Hub, Sportsbook Hub, Stats Card

- [ ] **Step 2: Run and verify**

Run: `python test_all_renders.py`
Expected: All 21 PNGs created, all render times <100ms, no errors

- [ ] **Step 3: Visual inspection**

Open each PNG and verify:
- Card is 480px wide (960px at 2x DPI)
- Text is readable — no labels smaller than 11px
- Colors match token definitions
- Card assets (playing cards, slot icons) render correctly
- Noise texture overlay visible
- Status bars colored correctly

- [ ] **Step 4: Integration test**

Run the bot locally and test each card type through Discord:
1. Casino games: play through blackjack, slots, crash, coinflip, scratch
2. Flow Live: wait for session recap, check pulse dashboard
3. Predictions: browse markets, place bet, view portfolio
4. Genesis: submit trade, view trade card
5. Hub cards: open Flow hub, Sportsbook hub
6. Test on Discord mobile client — verify readability

- [ ] **Step 5: Commit test script**

```bash
git add test_all_renders.py
git commit -m "test: add comprehensive render verification script"
```
