"""
atlas_html_engine.py — ATLAS Unified HTML Render Engine

Centralised browser management, page pooling, font/asset loading,
shared CSS, and HTML builder helpers used by every card renderer.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_mod
import logging
from pathlib import Path

from atlas_style_tokens import Tokens

log = logging.getLogger("atlas.render")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DIR = Path(__file__).parent
_FONTS_DIR = _DIR / "fonts"
_CARDS_DIR = _DIR / "casino" / "renderer" / "cards"
_SLOT_ICONS_DIR = _DIR / "casino" / "renderer" / "slot_icons"
_GAME_ICONS_DIR = _DIR / "icons"
_ACH_ICONS_DIR = _DIR / "icons" / "achievements"

# ---------------------------------------------------------------------------
# Browser singleton
# ---------------------------------------------------------------------------
_browser = None
_pw_context_manager = None
_pw_instance = None


async def _get_browser():
    global _browser, _pw_context_manager, _pw_instance
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright

        _pw_context_manager = async_playwright()
        _pw_instance = await _pw_context_manager.__aenter__()
        _browser = await _pw_instance.chromium.launch(headless=True)
    return _browser


async def close_browser():
    global _browser, _pw_context_manager, _pw_instance
    if _browser:
        await _browser.close()
        _browser = None
    if _pw_context_manager:
        try:
            await _pw_context_manager.__aexit__(None, None, None)
        except Exception:
            pass
        _pw_context_manager = None
        _pw_instance = None


# ---------------------------------------------------------------------------
# Font loading + base64 caching
# ---------------------------------------------------------------------------
_FONT_CACHE: dict[str, str] = {}


def _load_font_b64(name: str) -> str:
    if name not in _FONT_CACHE:
        path = _FONTS_DIR / name
        if path.exists():
            _FONT_CACHE[name] = base64.b64encode(path.read_bytes()).decode()
        else:
            log.warning("Font file missing: %s", name)
            _FONT_CACHE[name] = ""
    return _FONT_CACHE[name]


def _font_face_css() -> str:
    fonts = [
        ("Outfit", "Outfit-Regular.ttf", 400),
        ("Outfit", "Outfit-SemiBold.ttf", 600),
        ("Outfit", "Outfit-Bold.ttf", 700),
        ("Outfit", "Outfit-ExtraBold.ttf", 800),
        ("JetBrains Mono", "JetBrainsMono-Regular.ttf", 400),
        ("JetBrains Mono", "JetBrainsMono-Bold.ttf", 700),
        ("JetBrains Mono", "JetBrainsMono-ExtraBold.ttf", 800),
    ]
    css = ""
    for family, filename, weight in fonts:
        b64 = _load_font_b64(filename)
        if b64:
            css += f"""
@font-face {{
  font-family: '{family}';
  src: url('data:font/ttf;base64,{b64}') format('truetype');
  font-weight: {weight};
  font-style: normal;
  font-display: block;
}}"""
    return css


# ---------------------------------------------------------------------------
# Card asset loaders
# ---------------------------------------------------------------------------
_CARD_B64_CACHE: dict[str, str] = {}


def _card_b64(filename: str) -> str:
    if filename not in _CARD_B64_CACHE:
        path = _CARDS_DIR / filename
        if path.exists():
            _CARD_B64_CACHE[filename] = base64.b64encode(path.read_bytes()).decode()
        else:
            _CARD_B64_CACHE[filename] = ""
    return _CARD_B64_CACHE[filename]


def card_img_src(suit_code: str, value: str) -> str:
    """Return data-URI src for a playing card image."""
    b64 = _card_b64(f"{suit_code}{value}.png")
    return f"data:image/png;base64,{b64}" if b64 else ""


def card_back_src() -> str:
    """Return data-URI src for the card back image."""
    b64 = _card_b64("back.png")
    return f"data:image/png;base64,{b64}" if b64 else ""


# ---------------------------------------------------------------------------
# Slot icon loaders
# ---------------------------------------------------------------------------
SLOT_ICON_CONFIG = {
    "shield":   {"file": "shield.png",   "label": "TSL Shield",  "mult": 50, "tier": "jackpot", "weight": 2},
    "crown":    {"file": "crown.png",    "label": "Crown",       "mult": 20, "tier": "legend",  "weight": 5},
    "trophy":   {"file": "trophy.png",   "label": "Trophy",      "mult": 10, "tier": "epic",    "weight": 8},
    "wild":     {"file": "wild.png",     "label": "Wild",        "mult": 0,  "tier": "wild",    "weight": 0},
    "football": {"file": "football.png", "label": "Football",    "mult": 5,  "tier": "rare",    "weight": 15},
    "star":     {"file": "star.png",     "label": "Star",        "mult": 3,  "tier": "common",  "weight": 20},
    "coin":     {"file": "coin.png",     "label": "Coin",        "mult": 2,  "tier": "base",    "weight": 30},
}

_SLOT_ICON_B64_CACHE: dict[str, str] = {}


def _slot_icon_b64(symbol: str) -> str:
    if symbol not in _SLOT_ICON_B64_CACHE:
        cfg = SLOT_ICON_CONFIG.get(symbol)
        if cfg:
            path = _SLOT_ICONS_DIR / cfg["file"]
            if path.exists():
                _SLOT_ICON_B64_CACHE[symbol] = base64.b64encode(path.read_bytes()).decode()
            else:
                _SLOT_ICON_B64_CACHE[symbol] = ""
        else:
            _SLOT_ICON_B64_CACHE[symbol] = ""
    return _SLOT_ICON_B64_CACHE[symbol]


def slot_icon_src(symbol: str) -> str:
    """Return data-URI src for a slot icon."""
    b64 = _slot_icon_b64(symbol)
    return f"data:image/png;base64,{b64}" if b64 else ""


# ---------------------------------------------------------------------------
# Game & achievement icon loaders
# ---------------------------------------------------------------------------
_GAME_ICON_B64_CACHE: dict[str, str] = {}
_ACH_ICON_B64_CACHE: dict[str, str] = {}


def game_icon_src(name: str) -> str:
    """Return data-URI src for icons/{name}.png."""
    if name not in _GAME_ICON_B64_CACHE:
        path = _GAME_ICONS_DIR / f"{name}.png"
        _GAME_ICON_B64_CACHE[name] = base64.b64encode(path.read_bytes()).decode() if path.exists() else ""
    b64 = _GAME_ICON_B64_CACHE[name]
    return f"data:image/png;base64,{b64}" if b64 else ""


def achievement_icon_src(name: str) -> str:
    """Return data-URI src for icons/achievements/{name}.png."""
    if name not in _ACH_ICON_B64_CACHE:
        path = _ACH_ICONS_DIR / f"{name}.png"
        _ACH_ICON_B64_CACHE[name] = base64.b64encode(path.read_bytes()).decode() if path.exists() else ""
    b64 = _ACH_ICON_B64_CACHE[name]
    return f"data:image/png;base64,{b64}" if b64 else ""


# ---------------------------------------------------------------------------
# Icon pill helper (public API)
# ---------------------------------------------------------------------------
def icon_pill(name: str, fallback: str = "") -> str:
    """Return <img> tag for a game icon, or fallback emoji if icon not found."""
    src = game_icon_src(name)
    if src:
        return f'<img src="{src}">'
    return fallback


# ---------------------------------------------------------------------------
# HTML escape helper (public API)
# ---------------------------------------------------------------------------
def esc(text) -> str:
    """HTML-escape any value for safe embedding in templates."""
    return html_mod.escape(str(text))


# ---------------------------------------------------------------------------
# Shared V6 CSS (token-driven)
# ---------------------------------------------------------------------------
_SHARED_CSS = """\
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background: transparent;
  font-family: 'Outfit', sans-serif;
  color: #fff;
  padding: 0;
}

.card {
  width: var(--card-width);
  border-radius: var(--border-radius);
  overflow: hidden;
  position: relative;
  background: var(--bg);
  border: 1px solid rgba(212,175,55,0.18);
}

/* Noise texture overlay */
.card::before {
  content: '';
  position: absolute;
  inset: 0;
  opacity: var(--noise-opacity);
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  pointer-events: none;
  z-index: 1;
}

.card > * { position: relative; z-index: 2; }

/* Status bar */
.status-bar { height: 5px; width: 100%; }
.status-bar.win      { background: linear-gradient(90deg, #4ADE80, #22C55E, #4ADE80); }
.status-bar.loss     { background: linear-gradient(90deg, #F87171, #EF4444, #F87171); }
.status-bar.push     { background: linear-gradient(90deg, #FBBF24, #D97706, #FBBF24); }
.status-bar.jackpot  { background: linear-gradient(90deg, #D4AF37, #FFDA50, #D4AF37); }
.status-bar.blackjack{ background: linear-gradient(90deg, #D4AF37, #FFDA50, #D4AF37); }
.status-bar.near_miss{ background: linear-gradient(90deg, #F59E0B, #FBBF24, #F59E0B); }

/* Header */
.header {
  display: flex;
  align-items: center;
  padding: 14px 20px 10px;
}
.header-left {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  flex: 1;
}
.game-icon-pill {
  width: 32px; height: 32px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
  background: rgba(212,175,55,0.15);
  color: var(--gold);
  flex-shrink: 0;
  overflow: hidden;
}
.game-icon-pill img {
  width: 100%; height: 100%;
  object-fit: cover; border-radius: 8px;
}
.game-title-group { display: flex; flex-direction: column; }
.game-title {
  font-family: 'Outfit', sans-serif;
  font-weight: 800;
  font-size: 20px;
  color: var(--text-primary);
  letter-spacing: 1.5px;
  line-height: 1.2;
}
.game-subtitle {
  font-family: 'Outfit', sans-serif;
  font-weight: 600;
  font-size: 11px;
  color: var(--text-sub);
  letter-spacing: 0.5px;
}
.txn-id {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 600;
  font-size: 9px;
  color: var(--text-dim);
  margin-top: 2px;
}

/* Center username */
.header-center { display: flex; align-items: center; justify-content: center; flex: 1; }
.username-badge {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  font-size: 15px;
  color: var(--text-primary);
  white-space: nowrap;
  background: rgba(255,255,255,0.05);
  border-radius: 8px;
  padding: 4px 12px;
}

/* Right badge */
.header-right { display: flex; align-items: center; justify-content: flex-end; flex: 1; }
.result-badge {
  padding: 5px 14px;
  border-radius: 8px;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.5px;
  white-space: nowrap;
}
.result-badge.win     { background: rgba(74,222,128,0.12); border: 1px solid rgba(74,222,128,0.35); color: #4ADE80; }
.result-badge.loss    { background: rgba(248,113,113,0.12); border: 1px solid rgba(248,113,113,0.35); color: #F87171; }
.result-badge.push    { background: rgba(251,191,36,0.12); border: 1px solid rgba(251,191,36,0.35); color: #FBBF24; }
.result-badge.jackpot { background: rgba(212,175,55,0.15); border: 1px solid rgba(212,175,55,0.4); color: #FFDA50; }
.result-badge.blackjack { background: rgba(212,175,55,0.15); border: 1px solid rgba(212,175,55,0.4); color: #FFDA50; }
.result-badge.active  { background: rgba(212,175,55,0.1); border: 1px solid rgba(212,175,55,0.3); color: #D4AF37; }
.result-badge.near_miss { background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.35); color: #FBBF24; }

/* Streak badge */
.streak-badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: 12px;
  font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 11px;
  letter-spacing: 0.5px; margin-left: 6px;
}
.streak-badge.hot { background: rgba(251,146,60,0.15); border: 1px solid rgba(251,146,60,0.35); color: #FB923C; }
.streak-badge.fire { background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.35); color: #F87171; }
.streak-badge.legendary { background: rgba(212,175,55,0.15); border: 1px solid rgba(212,175,55,0.4); color: #FFDA50; }
.streak-badge.cold { background: rgba(96,165,250,0.12); border: 1px solid rgba(96,165,250,0.3); color: #60A5FA; }

/* Near-miss banner */
.near-miss-banner {
  text-align: center; padding: 6px 20px;
  font-family: 'Outfit', sans-serif; font-weight: 700; font-size: 14px;
  color: #FBBF24; background: rgba(245,158,11,0.08); letter-spacing: 0.5px;
}

/* Jackpot footer */
.jackpot-footer {
  display: flex; align-items: center; justify-content: center; gap: 16px;
  padding: 6px 20px 10px;
  font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 10px;
  color: var(--text-dim); letter-spacing: 0.5px;
}
.jackpot-footer .jp-tier { color: var(--gold-dim); }

/* Gold divider */
.gold-divider {
  height: 1px; margin: 0 20px;
  background: linear-gradient(90deg, transparent, rgba(212,175,55,0.3) 15%, rgba(212,175,55,0.3) 85%, transparent);
}

/* Data grid */
.data-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; padding: 12px 20px; }
.data-cell {
  background: rgba(255,255,255,0.03); border-radius: 8px; padding: 10px 12px; text-align: center;
  border-top: 1px solid rgba(255,255,255,0.06); border-left: 1px solid rgba(255,255,255,0.04);
  border-bottom: 1px solid rgba(0,0,0,0.3); border-right: 1px solid rgba(0,0,0,0.2);
}
.data-label {
  font-family: 'Outfit', sans-serif; font-weight: 700; font-size: 12px;
  color: var(--gold-dim); letter-spacing: 1.5px; margin-bottom: 4px; text-transform: uppercase;
}
.data-value { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 18px; color: var(--text-primary); }
.data-value.green { color: var(--win); }
.data-value.red   { color: var(--loss); }
.data-value.amber { color: var(--push); }
.data-value.gold  { color: var(--gold); }

/* Footer */
.footer { display: flex; align-items: center; justify-content: center; padding: 10px 20px 14px; }
.footer-balance { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 20px; }
.footer-balance .label { color: var(--gold); }
.footer-balance .amount { color: var(--text-primary); }
"""


# ---------------------------------------------------------------------------
# Shared HTML builder helpers (public API)
# ---------------------------------------------------------------------------
def build_header_html(
    icon: str,
    title: str,
    players: list[str],
    outcome: str,
    badge_text: str,
    txn_id: str | None = None,
    subtitle: str = "FLOW Casino",
) -> str:
    """Build the standard card header with icon, title, player badges, and result badge."""
    txn_html = f'<div class="txn-id">TXN #{esc(txn_id)}</div>' if txn_id else ""
    if players:
        player_badges = " ".join(
            f'<span class="username-badge">{esc(p)}</span>' for p in players
        )
        players_html = player_badges
    else:
        players_html = ""
    return f"""
    <div class="header">
      <div class="header-left">
        <div class="game-icon-pill">{icon}</div>
        <div class="game-title-group">
          <div class="game-title">{esc(title)}</div>
          <div class="game-subtitle">{esc(subtitle)}</div>
          {txn_html}
        </div>
      </div>
      <div class="header-center">{players_html}</div>
      <div class="header-right">
        <div class="result-badge {esc(outcome)}">{esc(badge_text)}</div>
      </div>
    </div>"""


def build_data_grid_html(wager: int, payout: int, balance: int) -> str:
    """Build the 4-column data grid (Wager / Payout / P&L / Balance)."""
    pl = payout - wager
    pl_color = "green" if pl > 0 else "red" if pl < 0 else "amber"
    pl_str = f"+${pl:,}" if pl > 0 else f"-${abs(pl):,}" if pl < 0 else "$0"
    payout_color = "green" if payout > wager else "red" if payout < wager else ""
    return f"""
    <div class="data-grid">
      <div class="data-cell"><div class="data-label">Wager</div><div class="data-value">${wager:,}</div></div>
      <div class="data-cell"><div class="data-label">Payout</div><div class="data-value {payout_color}">${payout:,}</div></div>
      <div class="data-cell"><div class="data-label">P&amp;L</div><div class="data-value {pl_color}">{pl_str}</div></div>
      <div class="data-cell"><div class="data-label">Balance</div><div class="data-value">${balance:,}</div></div>
    </div>"""


def build_footer_html(balance: int) -> str:
    """Build the balance footer row."""
    return f"""
    <div class="footer">
      <div class="footer-balance">
        <span class="label">Balance:</span>
        <span class="amount"> ${balance:,}</span>
      </div>
    </div>"""


def build_streak_badge_html(streak_info: dict | None) -> str:
    """Build a streak badge (hot/fire/legendary/cold) or empty string."""
    if not streak_info:
        return ""
    s_type = streak_info.get("type", "")
    s_len = streak_info.get("len", 0)
    if s_type == "win" and s_len >= 3:
        if s_len >= 10:
            css_class, icon, label = "legendary", "\U0001f525", f"W{s_len}"
        elif s_len >= 7:
            css_class, icon, label = "fire", "\U0001f525\U0001f525", f"W{s_len}"
        elif s_len >= 5:
            css_class, icon, label = "fire", "\U0001f525", f"W{s_len}"
        else:
            css_class, icon, label = "hot", "\U0001f525", f"W{s_len}"
        return f'<span class="streak-badge {css_class}">{icon} {label}</span>'
    elif s_type == "loss" and s_len >= 5:
        return f'<span class="streak-badge cold">\u2744\ufe0f L{s_len}</span>'
    return ""


def build_near_miss_html(near_miss_msg: str | None) -> str:
    """Build a near-miss banner or empty string."""
    if not near_miss_msg:
        return ""
    return f'<div class="near-miss-banner">{esc(near_miss_msg)}</div>'


def build_jackpot_footer_html(jackpot_info: dict | None) -> str:
    """Build the jackpot pool footer or empty string."""
    if not jackpot_info:
        return ""
    parts = []
    for tier in ("mini", "major", "grand"):
        if tier in jackpot_info:
            pool = jackpot_info[tier].get("pool", 0)
            parts.append(f'<span class="jp-tier">{tier.upper()}</span> ${pool:,}')
    if not parts:
        return ""
    return f'<div class="jackpot-footer">\U0001f48e {"&middot;".join(parts)}</div>'


# ---------------------------------------------------------------------------
# wrap_card — base HTML template with token CSS injection
# ---------------------------------------------------------------------------
def wrap_card(body_html: str, status_class: str = "", *, theme_id: str | None = None) -> str:
    """Wrap body content in full card HTML with tokens CSS + shared styles.

    Parameters
    ----------
    body_html : str
        Inner HTML content for the card.
    status_class : str
        CSS class for the status bar gradient (win/loss/push/jackpot/etc.).
    theme_id : str | None
        Optional theme key from atlas_themes.THEMES.  When provided, theme
        CSS variable overrides, overlay HTML, extra CSS, and status/border
        styles are injected into the card shell.
    """
    # ── Theme layer (no-op when theme_id is None) ─────────────────────────
    theme_css = ""
    overlay_html = ""
    extra_css = ""
    card_border_attr = ""
    status_bar_attr = f'class="status-bar {esc(status_class)}"'

    if theme_id:
        from atlas_themes import get_theme, get_overlay_html

        theme = get_theme(theme_id)

        # Merge theme CSS variable overrides (second :root block wins cascade)
        if theme.get("vars"):
            lines = [f"  --{k}: {v};" for k, v in theme["vars"].items()]
            theme_css = ":root {\n" + "\n".join(lines) + "\n}"

        # Overlay HTML fragments (scanlines, vignette, HUD brackets, rim light)
        overlay_html = get_overlay_html(theme.get("overlays", []))

        # Extra CSS (hero gradient classes, theme-specific overrides)
        extra_css = theme.get("extra_css", "")

        # Status bar gradient override (inline style beats CSS class)
        sg = theme.get("status_gradient")
        if sg:
            status_bar_attr = f'class="status-bar" style="background:{sg};height:5px;width:100%;"'

        # Card border override
        cb = theme.get("card_border")
        if cb:
            card_border_attr = f' style="border:{cb}"'

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
{_font_face_css()}
{Tokens.to_css_vars()}
{theme_css}
{_SHARED_CSS}
{extra_css}
</style>
</head>
<body>
<div class="card"{card_border_attr}>
  <div {status_bar_attr}></div>
  {overlay_html}
  {body_html}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# PagePool — reusable Playwright page pool with viewport reset
# ---------------------------------------------------------------------------
class PagePool:
    """Pool of Playwright pages for concurrent rendering."""

    def __init__(self, size: int = 4, width: int = 480, scale: int = 2):
        self._size = size
        self._width = width
        self._scale = scale
        self._available: asyncio.Queue = asyncio.Queue()
        self._max_renders_per_page = 100
        self._render_counts: dict[int, int] = {}

    async def _new_page(self):
        """Create a fresh page from the current browser."""
        browser = await _get_browser()
        return await browser.new_page(
            viewport={"width": self._width, "height": 1200},
            device_scale_factor=self._scale,
        )

    async def warm(self):
        """Pre-create pages and add them to the pool."""
        for _ in range(self._size):
            page = await self._new_page()
            await self._available.put(page)

    async def acquire(self, timeout: float = 10.0):
        """Get a live page from the pool, replacing dead pages."""
        page = await asyncio.wait_for(self._available.get(), timeout=timeout)
        # Health check: replace zombie pages left by a browser crash
        for _ in range(self._size):
            if not page.is_closed():
                return page
            log.warning("Dead page detected in pool — replacing")
            page = await self._new_page()
        return page

    async def release(self, page):
        """Return a page to the pool, recycling dead or exhausted pages."""
        try:
            if page.is_closed():
                raise RuntimeError("page closed")
            await page.set_viewport_size({"width": self._width, "height": 1200})
            pid = id(page)
            self._render_counts[pid] = self._render_counts.get(pid, 0) + 1
            if self._render_counts[pid] >= self._max_renders_per_page:
                del self._render_counts[pid]
                await page.close()
                raise RuntimeError("recycling")
        except Exception:
            # Replace dead or recycled page with a fresh one
            page = await self._new_page()
        await self._available.put(page)

    async def drain(self):
        """Close all pooled pages."""
        while not self._available.empty():
            page = await self._available.get()
            await page.close()


# ---------------------------------------------------------------------------
# render_card — unified render function
# ---------------------------------------------------------------------------
_pool: PagePool | None = None


async def render_card(html: str, width: int | None = None) -> bytes:
    """Render HTML to a PNG screenshot, clipping to the .card element."""
    if _pool is None:
        raise RuntimeError("Page pool not initialised — call init_pool() first")
    try:
        page = await _pool.acquire()
    except asyncio.TimeoutError:
        raise RuntimeError(
            "Render pool exhausted — all slots busy. Try again in a moment."
        ) from None
    try:
        if width and width != Tokens.CARD_WIDTH:
            await page.set_viewport_size({"width": width, "height": 1200})
        await page.set_content(html, wait_until="domcontentloaded", timeout=10_000)
        card = await page.query_selector(".card")
        if card:
            box = await card.bounding_box()
            if box:
                await page.set_viewport_size(
                    {
                        "width": width or Tokens.CARD_WIDTH,
                        "height": int(box["height"]) + 4,
                    }
                )
                clip = {
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                }
                return await page.screenshot(clip=clip, type="png", timeout=10_000)
        return await page.screenshot(type="png", timeout=10_000)
    finally:
        await _pool.release(page)


# ---------------------------------------------------------------------------
# Lifecycle hooks (called from bot.py)
# ---------------------------------------------------------------------------
async def init_pool():
    """Create and warm the page pool. Call once at bot startup."""
    global _pool
    _pool = PagePool(size=4, width=Tokens.CARD_WIDTH, scale=Tokens.DPI_SCALE)
    await _pool.warm()


async def drain_pool():
    """Drain the page pool and close the browser. Call at bot shutdown."""
    global _pool
    if _pool:
        await _pool.drain()
        _pool = None
    await close_browser()
