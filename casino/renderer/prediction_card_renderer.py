"""
prediction_card_renderer.py — Pillow-rendered prediction market cards
======================================================================
Renders dark-themed prediction market cards for the /predictions browser.
Follows the same visual language as ledger_renderer.py (dark navy, gold accents).

Returns io.BytesIO PNG buffers ready for discord.File.
"""

from __future__ import annotations

import io
import os
import textwrap
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# ── Paths ─────────────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSETS_FONTS = os.path.join(_DIR, "..", "..", "assets", "fonts")
_CASINO_FONTS = os.path.join(_DIR, "fonts")

# Inter font paths (primary)
_INTER_BOLD = os.path.join(_ASSETS_FONTS, "Inter-Bold.ttf")
_INTER_SEMI = os.path.join(_ASSETS_FONTS, "Inter-SemiBold.ttf")
_INTER_REG = os.path.join(_ASSETS_FONTS, "Inter-Regular.ttf")

# Montserrat fallback paths
_MONT_BOLD = os.path.join(_CASINO_FONTS, "Montserrat-Bold.ttf")
_MONT_REG = os.path.join(_CASINO_FONTS, "Montserrat-Regular.ttf")

# ── Color palette ─────────────────────────────────────────────────────────────
BG_DARK = (26, 26, 46)         # #1a1a2e
BG_CARD = (30, 32, 52)         # slightly lighter card bg
BG_HEADER = (20, 20, 36)       # header bar
GOLD = (212, 175, 55)
GOLD_DIM = (140, 115, 36)
WHITE = (255, 255, 255)
OFF_WHITE = (200, 200, 210)
MUTED = (130, 130, 150)
YES_GREEN = (34, 197, 94)
NO_RED = (239, 68, 68)
BAR_BG = (50, 50, 70)

# Category badge colors (hex -> RGB)
CATEGORY_BADGE_COLORS = {
    "Politics":     (52, 152, 219),
    "Entertainment": (233, 30, 99),
    "Crypto":       (243, 156, 18),
    "Economics":    (39, 174, 96),
    "Science":      (155, 89, 182),
    "Tech":         (26, 188, 156),
    "AI":           (0, 206, 209),
    "World":        (230, 126, 34),
    "Other":        (149, 165, 166),
}

# ── Card dimensions ───────────────────────────────────────────────────────────
CARD_W = 650
CARD_H = 200
PAD = 20
BADGE_H = 24
BAR_H = 20
BAR_RADIUS = 10

# Page layout
PAGE_HEADER_H = 50
PAGE_GAP = 12


def _load_font(style: str, size: int) -> ImageFont.FreeTypeFont:
    """
    Load font with fallback chain: Inter -> Montserrat -> system -> default.
    style: 'bold', 'semi', 'regular'
    """
    paths = {
        "bold": [_INTER_BOLD, _MONT_BOLD],
        "semi": [_INTER_SEMI, _INTER_BOLD, _MONT_BOLD],
        "regular": [_INTER_REG, _MONT_REG],
    }
    for path in paths.get(style, paths["regular"]):
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    # System font fallback
    for fb in [
        "C:/Windows/Fonts/arialbd.ttf" if style != "regular" else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if style != "regular"
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            return ImageFont.truetype(fb, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _extract_category_name(category: str) -> str:
    """Strip emoji prefix from category label. '🏛️ Politics' -> 'Politics'."""
    parts = category.split(" ", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def _get_badge_color(category: str) -> tuple[int, int, int]:
    """Get RGB color for category badge."""
    name = _extract_category_name(category)
    return CATEGORY_BADGE_COLORS.get(name, CATEGORY_BADGE_COLORS["Other"])


def _draw_rounded_rect(draw: ImageDraw.ImageDraw, xy: tuple, radius: int,
                       fill=None, outline=None, width=1):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int,
               max_lines: int = 2) -> list[str]:
    """Word-wrap text to fit within max_width, limited to max_lines."""
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()
        bbox = font.getbbox(test)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)

    # Truncate last line if it overflows
    if lines:
        last = lines[-1]
        bbox = font.getbbox(last)
        while bbox[2] - bbox[0] > max_width and len(last) > 5:
            last = last[:-4] + "..."
            bbox = font.getbbox(last)
        lines[-1] = last

    return lines


def render_market_card(
    title: str,
    category: str,
    yes_price: float,
    no_price: float,
    volume: float,
    end_date: str = "",
    user_position: str | None = None,
) -> io.BytesIO:
    """
    Render a single prediction market card.

    Returns a BytesIO PNG buffer.
    """
    img = Image.new("RGBA", (CARD_W, CARD_H), BG_CARD)
    draw = ImageDraw.Draw(img)

    # Fonts
    font_title = _load_font("bold", 18)
    font_badge = _load_font("semi", 12)
    font_price = _load_font("bold", 22)
    font_label = _load_font("regular", 12)
    font_meta = _load_font("regular", 11)

    y = PAD

    # ── Category badge ───────────────────────────────────────────────────
    badge_color = _get_badge_color(category)
    cat_name = _extract_category_name(category)
    badge_bbox = font_badge.getbbox(cat_name)
    badge_w = (badge_bbox[2] - badge_bbox[0]) + 16
    badge_h = BADGE_H

    _draw_rounded_rect(draw, (PAD, y, PAD + badge_w, y + badge_h), 4, fill=badge_color)
    draw.text((PAD + 8, y + 4), cat_name, font=font_badge, fill=WHITE)

    # User position badge (if any)
    if user_position:
        pos_text = f"YOUR BET: {user_position}"
        pos_bbox = font_badge.getbbox(pos_text)
        pos_w = (pos_bbox[2] - pos_bbox[0]) + 16
        pos_x = CARD_W - PAD - pos_w
        pos_color = YES_GREEN if "YES" in user_position.upper() else NO_RED
        _draw_rounded_rect(draw, (pos_x, y, pos_x + pos_w, y + badge_h), 4, fill=pos_color)
        draw.text((pos_x + 8, y + 4), pos_text, font=font_badge, fill=WHITE)

    y += badge_h + 8

    # ── Title ────────────────────────────────────────────────────────────
    title_lines = _wrap_text(title, font_title, CARD_W - 2 * PAD, max_lines=2)
    for line in title_lines:
        draw.text((PAD, y), line, font=font_title, fill=WHITE)
        y += 24
    y += 4

    # ── Probability bar ──────────────────────────────────────────────────
    bar_x = PAD
    bar_y = y
    bar_w = CARD_W - 2 * PAD
    yes_pct = max(0, min(1, yes_price))
    no_pct = 1 - yes_pct

    # Background
    _draw_rounded_rect(draw, (bar_x, bar_y, bar_x + bar_w, bar_y + BAR_H),
                       BAR_RADIUS, fill=BAR_BG)

    # YES fill (left side)
    yes_w = max(int(bar_w * yes_pct), BAR_RADIUS * 2) if yes_pct > 0.05 else 0
    if yes_w > 0:
        _draw_rounded_rect(draw, (bar_x, bar_y, bar_x + yes_w, bar_y + BAR_H),
                           BAR_RADIUS, fill=YES_GREEN)

    # Percentage labels on the bar
    if yes_pct > 0.15:
        yes_text = f"{yes_pct:.0%}"
        draw.text((bar_x + 8, bar_y + 3), yes_text, font=font_badge, fill=WHITE)
    if no_pct > 0.15:
        no_text = f"{no_pct:.0%}"
        no_bbox = font_badge.getbbox(no_text)
        no_tw = no_bbox[2] - no_bbox[0]
        draw.text((bar_x + bar_w - no_tw - 8, bar_y + 3), no_text,
                  font=font_badge, fill=WHITE)

    y += BAR_H + 10

    # ── YES/NO price boxes ───────────────────────────────────────────────
    box_w = (CARD_W - 3 * PAD) // 2
    box_h = 36

    # YES box
    _draw_rounded_rect(draw, (PAD, y, PAD + box_w, y + box_h), 6,
                       fill=(34, 197, 94, 40), outline=YES_GREEN, width=1)
    yes_str = f"YES {yes_pct:.0%}"
    draw.text((PAD + 10, y + 8), yes_str, font=font_price, fill=YES_GREEN)

    # Profit % on YES
    if yes_pct > 0 and yes_pct < 1:
        profit_yes = ((1 / yes_pct) - 1) * 100
        profit_text = f"+{profit_yes:.0f}%"
        p_bbox = font_label.getbbox(profit_text)
        p_w = p_bbox[2] - p_bbox[0]
        draw.text((PAD + box_w - p_w - 10, y + 12), profit_text,
                  font=font_label, fill=YES_GREEN)

    # NO box
    no_x = PAD + box_w + PAD
    _draw_rounded_rect(draw, (no_x, y, no_x + box_w, y + box_h), 6,
                       fill=(239, 68, 68, 40), outline=NO_RED, width=1)
    no_str = f"NO {no_pct:.0%}"
    draw.text((no_x + 10, y + 8), no_str, font=font_price, fill=NO_RED)

    # Profit % on NO
    if no_pct > 0 and no_pct < 1:
        profit_no = ((1 / no_pct) - 1) * 100
        profit_text = f"+{profit_no:.0f}%"
        p_bbox = font_label.getbbox(profit_text)
        p_w = p_bbox[2] - p_bbox[0]
        draw.text((no_x + box_w - p_w - 10, y + 12), profit_text,
                  font=font_label, fill=NO_RED)

    y += box_h + 6

    # ── Volume + end date ────────────────────────────────────────────────
    meta_parts = []
    if volume:
        if volume >= 1_000_000:
            meta_parts.append(f"Vol: ${volume / 1_000_000:.1f}M")
        elif volume >= 1_000:
            meta_parts.append(f"Vol: ${volume / 1_000:.0f}K")
        else:
            meta_parts.append(f"Vol: ${volume:.0f}")

    if end_date:
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            meta_parts.append(f"Ends: {dt.strftime('%b %d, %Y')}")
        except (ValueError, TypeError):
            meta_parts.append(f"Ends: {end_date[:10]}")

    if meta_parts:
        meta_text = "  |  ".join(meta_parts)
        draw.text((PAD, y), meta_text, font=font_meta, fill=MUTED)

    # Convert to RGB for PNG
    rgb = Image.new("RGB", img.size, BG_DARK)
    rgb.paste(img, mask=img.split()[3])

    buf = io.BytesIO()
    rgb.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


def render_market_page(
    markets: list[dict],
    page: int,
    total_pages: int,
) -> io.BytesIO:
    """
    Render a page of market cards stacked vertically.

    Each market dict should have:
        title, category, yes_price, no_price, volume, end_date, user_position (optional)

    Returns a BytesIO PNG buffer.
    """
    n = len(markets)
    total_h = PAGE_HEADER_H + n * CARD_H + (n - 1) * PAGE_GAP + PAD

    img = Image.new("RGB", (CARD_W, total_h), BG_DARK)
    draw = ImageDraw.Draw(img)

    # ── Header bar ────────────────────────────────────────────────────────
    font_header = _load_font("bold", 16)
    font_page = _load_font("regular", 13)

    draw.rectangle((0, 0, CARD_W, PAGE_HEADER_H), fill=BG_HEADER)
    draw.text((PAD, 15), "ATLAS PREDICTION MARKETS", font=font_header, fill=GOLD)

    page_text = f"Page {page}/{total_pages}"
    p_bbox = font_page.getbbox(page_text)
    p_w = p_bbox[2] - p_bbox[0]
    draw.text((CARD_W - PAD - p_w, 18), page_text, font=font_page, fill=MUTED)

    # ── Stack cards ───────────────────────────────────────────────────────
    y = PAGE_HEADER_H
    for mkt in markets:
        card_buf = render_market_card(
            title=mkt.get("title", ""),
            category=mkt.get("category", "Other"),
            yes_price=mkt.get("yes_price", 0.5),
            no_price=mkt.get("no_price", 0.5),
            volume=mkt.get("volume", 0),
            end_date=mkt.get("end_date", ""),
            user_position=mkt.get("user_position"),
        )
        card_img = Image.open(card_buf)
        img.paste(card_img, (0, y))
        y += CARD_H + PAGE_GAP

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf
