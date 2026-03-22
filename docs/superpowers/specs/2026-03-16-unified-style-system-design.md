# ATLAS Unified Style System — Design Spec

## Context

ATLAS renders card images (blackjack, slots, session recaps, dashboards, trade cards) that are sent as Discord attachments. The current rendering stack has two problems:

1. **Readability on both mobile and desktop**: Cards rendered at 700-800px width get compressed ~54% on mobile Discord, making text tiny. Even on desktop, font sizes and visual hierarchy are not optimized for quick scanning.
2. **Split rendering stack**: Simple games (blackjack, slots, crash, scratch) use Pillow/PIL, while complex cards (session recap, pulse, highlights, trades) use Playwright HTML→PNG. Colors, fonts, and spacing are defined in 4+ different files with no single source of truth.

**Goal**: Create a unified style system and rendering pipeline that makes every ATLAS card readable on any device, maintainable from a single config, and performant enough for interactive games.

---

## Architecture Overview

Five changes, layered on each other:

1. **Unified Style Token System** (`atlas_style_tokens.py`) — single source of truth for all visual constants
2. **Full HTML Migration** — move all Pillow renders to Playwright/HTML
3. **High-DPI Rendering** — `deviceScaleFactor: 2` for crisp text on all devices
4. **Page Pool Optimizer** — pre-warmed browser pages for fast renders
5. **Mobile-First Design Defaults** — 700px card width, larger fonts

---

## 1. Style Token System

### File: `atlas_style_tokens.py`

A single Python module that defines all visual constants and generates CSS custom properties.

### Token Categories

**Colors:**
| Token | Value | Usage |
|-------|-------|-------|
| `bg_primary` | `#111111` | Card background |
| `bg_deep` | `#0A0A0A` | Deeper layer / hero sections |
| `gold` | `#D4AF37` | TSL signature accent |
| `gold_bright` | `#F0D060` | Highlights, active states |
| `win` | `#4ADE80` | Positive outcomes |
| `loss` | `#F87171` | Negative outcomes |
| `push` | `#FBBF24` | Neutral / push |
| `text_primary` | `#e8e0d0` | Main text (warm off-white, matches existing cards) |
| `text_secondary` | `#AAAAAA` | Labels, secondary info |
| `text_dim` | `#555555` | Disabled / muted |
| `panel_bg` | `rgba(255,255,255,0.04)` | Glass-morphism panels |
| `panel_border` | `rgba(255,255,255,0.08)` | Panel borders |

**Typography (mobile-first, bumped from current):**
| Token | Value | Previous | Usage |
|-------|-------|----------|-------|
| `font_display` | `Outfit` | Same | Display/heading font |
| `font_mono` | `JetBrains Mono` | Same | Data/stats font |
| `font_xs` | `11px` | 10px | Tiny labels |
| `font_sm` | `13px` | 12px | Secondary text |
| `font_base` | `15px` | 14px | Body text |
| `font_lg` | `18px` | 16px | Section headers |
| `font_xl` | `24px` | 22px | Titles |
| `font_hero` | `42px` | 36px | Hero numbers |
| `font_display_size` | `56px` | 48px | Giant display numbers |

**Spacing:**
| Token | Value |
|-------|-------|
| `space_xs` | `4px` |
| `space_sm` | `8px` |
| `space_md` | `12px` |
| `space_lg` | `16px` |
| `space_xl` | `24px` |
| `space_xxl` | `32px` |
| `card_padding` | `16px` |
| `section_gap` | `12px` |

**Layout:**
| Token | Value | Previous |
|-------|-------|----------|
| `card_width` | `700px` | 700-800px |
| `dpi_scale` | `2` | 1 (implicit) |
| `border_radius` | `8px` | varies |
| `border_radius_sm` | `4px` | varies |
| `status_bar_height` | `5px` | 5px |
| `noise_opacity` | `0.03` | varies |

### API

```python
class Tokens:
    # All constants as class attributes
    CARD_WIDTH = 700
    DPI_SCALE = 2
    GOLD = "#D4AF37"
    FONT_SIZE_BASE = 15
    # ... etc

    @staticmethod
    def to_css_vars() -> str:
        """Generates :root { --gold: #D4AF37; --font-base: 15px; ... }"""
        # Iterates over all token attributes, converts to CSS custom properties

    @staticmethod
    def to_css_block() -> str:
        """Full CSS block including :root vars + base card styles"""
```

### Existing files that currently define visual constants (to be consolidated):
- `atlas_colors.py` — module Discord.Color objects (keep for embed colors, but card rendering pulls from tokens)
- `atlas_card_renderer.py` lines 40-90 — Pillow color/font constants (removed after migration)
- `casino/renderer/card_renderer.py` — Pillow game render constants (removed after migration)
- `casino/renderer/casino_html_renderer.py` — inline CSS color values (replaced by token vars)
- `casino/renderer/session_recap_renderer.py` — inline CSS (replaced by token vars)
- `casino/renderer/pulse_renderer.py` — inline CSS (replaced by token vars)
- `casino/renderer/highlight_renderer.py` — inline CSS (replaced by token vars)
- `card_renderer.py` (root) — trade card CSS (replaced by token vars)

---

## 2. Unified HTML Engine

### File: `atlas_html_engine.py`

Replaces the current split rendering pipeline with a single module.

### Page Pool

```python
class PagePool:
    def __init__(self, size: int = 4, width: int = 700, scale: int = 2):
        self._size = size
        self._width = width
        self._scale = scale
        self._available: asyncio.Queue[Page] = asyncio.Queue()
        self._max_renders_per_page = 100

    async def warm(self):
        """Called on bot startup. Creates `size` pre-warmed pages."""
        browser = await _get_browser()
        for _ in range(self._size):
            page = await browser.new_page(
                viewport={"width": self._width, "height": 1200},
                device_scale_factor=self._scale
            )
            await self._available.put(page)

    async def acquire(self, timeout: float = 10.0) -> Page:
        """Get a page from the pool. Raises TimeoutError after `timeout` seconds."""
        return await asyncio.wait_for(self._available.get(), timeout=timeout)

    async def release(self, page: Page):
        """Return a page to the pool. Resets viewport and recycles if render count exceeded."""
        # Reset viewport to default in case a custom width was used
        await page.set_viewport_size({"width": self._width, "height": 1200})
        page._render_count = getattr(page, '_render_count', 0) + 1
        if page._render_count >= self._max_renders_per_page:
            await page.close()
            browser = await _get_browser()
            page = await browser.new_page(
                viewport={"width": self._width, "height": 1200},
                device_scale_factor=self._scale
            )
        await self._available.put(page)

    async def drain(self):
        """Called on bot shutdown. Closes all pages."""
        while not self._available.empty():
            page = await self._available.get()
            await page.close()
```

**Pool size rationale**: 4 pages handles typical concurrent load (2-3 simultaneous blackjack games + background pulse render). If contention is observed, increase to 6-8.

### Render API

```python
# Singleton pool
_pool: PagePool | None = None

async def init_pool():
    """Called from bot.py setup_hook()"""
    global _pool
    _pool = PagePool(size=4, width=Tokens.CARD_WIDTH, scale=Tokens.DPI_SCALE)
    await _pool.warm()

async def render_card(html: str, width: int | None = None) -> bytes:
    """Render HTML to PNG bytes. Uses page pool + 2x DPI."""
    page = await _pool.acquire()
    try:
        if width and width != Tokens.CARD_WIDTH:
            await page.set_viewport_size({"width": width, "height": 1200})
        await page.set_content(html, wait_until="domcontentloaded")
        card = await page.query_selector(".card")
        clip = await card.bounding_box()
        return await page.screenshot(clip=clip, type="png")
    finally:
        await _pool.release(page)
```

### Base Template

```python
BASE_TEMPLATE = """<!DOCTYPE html>
<html><head><style>
{font_faces}
:root {{ {css_vars} }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: transparent; font-family: var(--font-display), sans-serif; }}
.card {{
    width: var(--card-width);
    background: var(--bg-primary);
    border-radius: var(--border-radius);
    overflow: hidden;
    position: relative;
}}
</style></head>
<body>{body}</body></html>"""

def wrap_card(body_html: str) -> str:
    return BASE_TEMPLATE.format(
        font_faces=_font_face_css(),
        css_vars=Tokens.to_css_vars(),
        body=body_html
    )
```

### Performance: `domcontentloaded` vs `networkidle`
Current code uses `wait_until="networkidle"` which waits for all network activity to cease. Since all fonts and assets are base64-embedded inline, there are no network requests. Switching to `"domcontentloaded"` saves ~30-50ms per render for free.

### Files replaced by `atlas_html_engine.py`:
- `casino/renderer/casino_html_renderer.py` — `_render_card_html()` function → replaced by `render_card()`
- `card_renderer.py` (root) — `_get_browser()`, `close_browser()`, `_load_font_b64()`, `_font_face_css()` → moved into engine
- `atlas_card_renderer.py` — entire Pillow rendering pipeline → removed after migration

---

## 3. Complete Card Inventory & Migration Strategy

### Full inventory: 22 card renders across the codebase

#### Group A: ATLASCard (Pillow) — Need full HTML migration
| # | Card | File | Function | Line |
|---|------|------|----------|------|
| 1 | Flow Hub | `flow_cards.py` | `build_flow_card()` | 258 |
| 2 | Sportsbook Hub | `sportsbook_cards.py` | `build_sportsbook_card()` | 315 |
| 3 | Stats Card | `sportsbook_cards.py` | `build_stats_card()` | 388 |
| 4 | Trade Stats | `atlas_card_renderer.py` | `build_trade_stats_card()` | 973 |

#### Group B: Casino HTML v6 — Already HTML, need tokenization + width update
| # | Card | File | Function | Line |
|---|------|------|----------|------|
| 5 | Blackjack | `casino/renderer/casino_html_renderer.py` | `render_blackjack_card()` | 776 |
| 6 | Slots | `casino/renderer/casino_html_renderer.py` | `render_slots_card()` | 980 |
| 7 | Crash | `casino/renderer/casino_html_renderer.py` | `render_crash_card()` | 1345 |
| 8 | Coinflip | `casino/renderer/casino_html_renderer.py` | `render_coinflip_card()` | 1571 |
| 9 | Scratch | `casino/renderer/casino_html_renderer.py` | `render_scratch_card_v6()` | 1790 |

#### Group C: Highlight cards — Already HTML, need tokenization + width update
| # | Card | File | Function | Line |
|---|------|------|----------|------|
| 10 | Jackpot Hit | `casino/renderer/highlight_renderer.py` | `render_jackpot_card()` | 258 |
| 11 | PvP Coinflip | `casino/renderer/highlight_renderer.py` | `render_pvp_card()` | 388 |
| 12 | Crash LMS | `casino/renderer/highlight_renderer.py` | `render_crash_lms_card()` | 475 |
| 13 | Prediction Resolve | `casino/renderer/highlight_renderer.py` | `render_prediction_card()` | 584 |
| 14 | Parlay Hit | `casino/renderer/highlight_renderer.py` | `render_parlay_card()` | 706 |

#### Group D: Flow Live cards — Already HTML, need tokenization + width update
| # | Card | File | Function | Line |
|---|------|------|----------|------|
| 15 | Session Recap | `casino/renderer/session_recap_renderer.py` | `render_session_recap()` | 694 |
| 16 | Pulse Dashboard | `casino/renderer/pulse_renderer.py` | `render_pulse_card()` | 458 |

#### Group E: Prediction Market cards — Already HTML, need tokenization + width update
| # | Card | File | Function | Line |
|---|------|------|----------|------|
| 17 | Market List | `casino/renderer/prediction_html_renderer.py` | `render_market_list_card()` | 567 |
| 18 | Market Detail | `casino/renderer/prediction_html_renderer.py` | `render_market_detail_card()` | 670 |
| 19 | Bet Confirmation | `casino/renderer/prediction_html_renderer.py` | `render_bet_confirmation_card()` | 695 |
| 20 | Portfolio | `casino/renderer/prediction_html_renderer.py` | `render_portfolio_card()` | 761 |
| 21 | Resolution | `casino/renderer/prediction_html_renderer.py` | `render_resolution_card()` | 844 |

#### Group F: Trade & Ledger — Already HTML, need tokenization + width update
| # | Card | File | Function | Line |
|---|------|------|----------|------|
| 22 | Trade Card | `card_renderer.py` | `render_trade_card()` | 806 |
| 23 | Ledger/Transaction | `casino/renderer/ledger_renderer.py` | `render_ledger_card()` | 400 |

**Note:** Legacy Pillow renderers in `casino/renderer/card_renderer.py` (v5 blackjack/slots/crash/scratch) are already dead code — superseded by HTML v6 in `casino_html_renderer.py`.

### Migration order (least risk → most risk):

| Phase | Group | Cards | Work Required |
|-------|-------|-------|---------------|
| 2a | D | Pulse Dashboard, Session Recap | Tokenize CSS, use `render_card()`, 700px |
| 2b | C | 5 Highlight cards | Tokenize CSS, use `render_card()`, 700px |
| 2c | E | 5 Prediction Market cards | Tokenize CSS, use `render_card()`, 700px |
| 2d | B | Blackjack, Slots, Crash, Coinflip, Scratch | Tokenize CSS, use `render_card()`. Already HTML v6 — just swap engine + tokens |
| 2e | F | Trade Card, Ledger | Tokenize CSS, use `render_card()`, 700px. Ledger uses `_render_html_to_png()` |
| 2f | A | Flow Hub, Sportsbook Hub, Stats, Trade Stats | **Full rewrite**: ATLASCard (Pillow) → HTML templates + `render_card()` |

### Per-card migration pattern:

**For Groups B-F (already HTML):**
1. Replace inline CSS color/font values with `var(--token-name)` references
2. Replace boilerplate HTML wrapper with `wrap_card(body_html)`
3. Replace `_render_card_html()` / `_render_html_to_png()` calls with `render_card()` from engine
4. Update `width` parameter to 700 (or use default)
5. Verify visual parity + performance

**For Group A (Pillow → HTML):**
1. Create new HTML template builder function (replaces ATLASCard section builders)
2. Template uses `wrap_card()` from engine for base styles
3. Template uses CSS custom properties from tokens for all values
4. Update the cog to call `render_card()` instead of ATLASCard.render()
5. Verify visual parity + performance
6. Remove old Pillow code after all Group A cards are migrated

---

## 4. Files Created / Modified / Deleted

### Created:
| File | Purpose |
|------|---------|
| `atlas_style_tokens.py` | Style token definitions + CSS generator |
| `atlas_html_engine.py` | Page pool + unified render_card() + wrap_card() API |
| `flow_cards_html.py` | Flow Hub HTML template (replaces ATLASCard in flow_cards.py) |
| `sportsbook_cards_html.py` | Sportsbook Hub + Stats HTML templates (replaces ATLASCard in sportsbook_cards.py) |

### Modified:
| File | Change |
|------|--------|
| `bot.py` | Call `init_pool()` in `setup_hook()`, `drain_pool()` on shutdown, version bump |
| `casino/renderer/casino_html_renderer.py` | Tokenize all 5 game card CSS (BJ, slots, crash, coinflip, scratch), use `render_card()` |
| `casino/renderer/session_recap_renderer.py` | Tokenize CSS, use `render_card()`, 700px |
| `casino/renderer/pulse_renderer.py` | Tokenize CSS, use `render_card()`, 700px |
| `casino/renderer/highlight_renderer.py` | Tokenize all 5 highlight cards CSS, use `render_card()`, 700px |
| `casino/renderer/prediction_html_renderer.py` | Tokenize all 5 prediction cards CSS, use `render_card()` |
| `casino/renderer/ledger_renderer.py` | Tokenize CSS, replace `_render_html_to_png()` with `render_card()` |
| `card_renderer.py` (root) | Move browser singleton + font/asset loaders to engine, tokenize trade card |
| `flow_cards.py` | Replace ATLASCard usage with HTML template + `render_card()` |
| `sportsbook_cards.py` | Replace ATLASCard usage with HTML template + `render_card()` |
| `CLAUDE.md` | Update architecture docs |

### Deleted (after full migration):
| File | Reason |
|------|--------|
| `atlas_card_renderer.py` | Pillow hub card renderer + ATLASCard class — replaced by HTML templates |
| `casino/renderer/card_renderer.py` | Legacy Pillow game renderer (already dead code) — cleanup |

---

## 5. Verification Plan

### Per-card migration testing (all 22 cards):
1. Render each card type with test data
2. Save PNG output and compare visually to current renders
3. Verify text is readable at Discord mobile scale (~375px display)
4. Measure render latency with `time.perf_counter()` — target <100ms

### Integration testing:
1. Run bot locally
2. **Casino games**: Play blackjack (hit/stand/double/split), slots (full animated sequence), crash (cashout), coinflip (solo + PvP), scratch (3 reveals)
3. **Flow Live**: Trigger session recap via idle timeout, verify pulse dashboard auto-update
4. **Highlights**: Trigger each highlight type (jackpot, pvp, crash LMS, prediction resolve, parlay)
5. **Predictions**: Create market, place bets, view portfolio, resolve market — verify all 5 card types
6. **Genesis**: Submit trade proposal, view trade details — verify trade card + trade stats card
7. **Hub cards**: Open Flow hub, Sportsbook hub, Stats card — verify all ATLASCard replacements
8. **Ledger**: View transaction history (if active)
9. Test on Discord mobile client — screenshot and compare readability for each card type

### Performance benchmarks:
1. Measure render times before migration (current baseline)
2. Measure render times after migration (HTML + page pool)
3. Target: blackjack hit renders <100ms
4. Load test: 4 concurrent blackjack games rendering simultaneously

### Regression checks:
1. All 22 card types produce valid PNG output
2. No broken embeds or missing images in Discord
3. Color accuracy matches token definitions
4. Font rendering matches token sizes
5. Playing card face images render correctly (base64 assets)
6. Slot machine icons render correctly (base64 assets)
7. Noise texture overlay present on all cards
