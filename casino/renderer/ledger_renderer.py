"""
ledger_renderer.py — TSL Casino Ledger Card Renderer
─────────────────────────────────────────────────────────────────────────────
Renders premium ledger entry cards for the #casino-ledger feed.
Uses the same font/color assets as card_renderer.py.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

# ── Paths ─────────────────────────────────────────────────────────────────────
_DIR       = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(_DIR, "fonts")
_FONT_BOLD = os.path.join(_FONTS_DIR, "Montserrat-Bold.ttf")
_FONT_REG  = os.path.join(_FONTS_DIR, "Montserrat-Regular.ttf")

# ── Color palette (matches card_renderer.py) ──────────────────────────────────
GOLD       = (212, 175, 55)
GOLD_DIM   = (140, 115, 36)
SILVER     = (220, 220, 230)
BLACK_BG   = (22,  22,  28)
BLACK_BG2  = (26,  26,  36)
OFF_WHITE  = (232, 224, 208)

WIN_GREEN   = (34, 197, 94)
WIN_GREEN_BG = (34, 197, 94, 30)
LOSS_RED    = (239, 68, 68)
LOSS_RED_BG = (239, 68, 68, 30)
PUSH_AMBER  = (245, 158, 11)
PUSH_AMBER_BG = (245, 158, 11, 30)

# ── Dimensions ────────────────────────────────────────────────────────────────
CARD_W   = 600
CARD_H   = 200
CARD_H_BIG = 240
PAD      = 20
TOP_BAR  = 3
BIG_THRESHOLD = 500

# ── Game metadata ─────────────────────────────────────────────────────────────
GAME_INFO = {
    "blackjack":    {"label": "BLACKJACK",   "icon": "\u2663"},  # ♣
    "slots":        {"label": "SLOTS",       "icon": "\u2b50"},  # ⭐
    "crash":        {"label": "CRASH",       "icon": "\u25b2"},  # ▲
    "coinflip":     {"label": "COIN FLIP",   "icon": "\u25cf"},  # ●
    "coinflip_pvp": {"label": "PVP FLIP",    "icon": "\u2694"},  # ⚔ (fallback)
    "scratch":      {"label": "SCRATCH",     "icon": "\u2605"},  # ★
}

OUTCOME_STYLE = {
    "win":  {"label": "WIN",  "color": WIN_GREEN,  "bg": WIN_GREEN_BG},
    "loss": {"label": "LOSS", "color": LOSS_RED,   "bg": LOSS_RED_BG},
    "push": {"label": "PUSH", "color": PUSH_AMBER, "bg": PUSH_AMBER_BG},
}


def _load_font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    """Load Montserrat font with system fallbacks matching card_renderer.py."""
    path = _FONT_BOLD if bold else _FONT_REG
    try:
        return ImageFont.truetype(path, size)
    except (IOError, OSError):
        pass
    for fb in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]:
        try:
            return ImageFont.truetype(fb, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def render_ledger_card(
    player_name: str,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
) -> io.BytesIO:
    """
    Render a premium casino ledger card as a PNG.
    Returns a BytesIO buffer ready for discord.File().
    """
    is_big = wager >= BIG_THRESHOLD
    h = CARD_H_BIG if is_big else CARD_H

    # ── Create canvas ─────────────────────────────────────────────────────
    img = Image.new("RGBA", (CARD_W, h), BLACK_BG + (255,))
    draw = ImageDraw.Draw(img)

    # Subtle vertical gradient
    for y in range(h):
        t = y / h
        r = int(BLACK_BG[0] * (1 - t) + BLACK_BG2[0] * t)
        g = int(BLACK_BG[1] * (1 - t) + BLACK_BG2[1] * t)
        b = int(BLACK_BG[2] * (1 - t) + BLACK_BG2[2] * t)
        draw.line([(0, y), (CARD_W, y)], fill=(r, g, b, 255))

    game = GAME_INFO.get(game_type, {"label": game_type.upper(), "icon": "\u2b22"})
    style = OUTCOME_STYLE.get(outcome, OUTCOME_STYLE["loss"])

    # ── Fonts ─────────────────────────────────────────────────────────────
    font_title  = _load_font(True,  22)
    font_small  = _load_font(False, 12)   # labels + footer
    font_value  = _load_font(True,  18)
    font_badge  = _load_font(True,  14)
    font_icon   = _load_font(True,  26)
    font_big    = _load_font(True,  13)

    # ── Gold top accent bar ───────────────────────────────────────────────
    border_w = 4 if is_big else 2
    # Top bar
    draw.rectangle([(0, 0), (CARD_W, TOP_BAR)], fill=GOLD)
    # Side borders
    draw.rectangle([(0, 0), (border_w, h)], fill=GOLD)
    draw.rectangle([(CARD_W - border_w, 0), (CARD_W, h)], fill=GOLD)
    # Bottom bar
    draw.rectangle([(0, h - TOP_BAR), (CARD_W, h)], fill=GOLD)

    # If big wager, add glow effect on edges
    if is_big:
        glow_color = style["color"]
        for i in range(8):
            alpha = int(40 * (1 - i / 8))
            glow = (*glow_color, alpha)
            draw.rectangle([(border_w + i, TOP_BAR + i),
                            (CARD_W - border_w - i, h - TOP_BAR - i)],
                           outline=glow)

    y_cursor = TOP_BAR + 14

    # ── Game icon + name (left) ───────────────────────────────────────────
    draw.text((PAD + 4, y_cursor - 4), game["icon"], fill=GOLD, font=font_icon)
    draw.text((PAD + 36, y_cursor + 2), game["label"], fill=OFF_WHITE, font=font_title)

    # ── Outcome badge (right) ─────────────────────────────────────────────
    mult_str = f"{multiplier:.1f}x" if multiplier != int(multiplier) else f"{int(multiplier)}x"
    badge_text = f" {style['label']}  {mult_str} "
    bbox = font_badge.getbbox(badge_text)
    badge_w = bbox[2] - bbox[0] + 16
    badge_h = bbox[3] - bbox[1] + 10
    badge_x = CARD_W - PAD - badge_w
    badge_y = y_cursor

    # Badge background
    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
        radius=4,
        fill=(*style["color"][:3], 35),
        outline=(*style["color"][:3], 120),
    )
    draw.text((badge_x + 8, badge_y + 3), badge_text, fill=style["color"], font=font_badge)

    y_cursor += 38

    # ── Gold separator line ───────────────────────────────────────────────
    draw.line([(PAD, y_cursor), (CARD_W - PAD, y_cursor)], fill=GOLD_DIM, width=1)
    y_cursor += 14

    # ── Data row: labels ──────────────────────────────────────────────────
    col_x = [PAD + 4, 155, 300, 450]
    labels = ["PLAYER", "WAGER", "PAYOUT", "P&L"]
    for i, label in enumerate(labels):
        draw.text((col_x[i], y_cursor), label, fill=GOLD_DIM, font=font_small)

    y_cursor += 18

    # ── Data row: values ──────────────────────────────────────────────────
    pl = payout - wager
    if pl > 0:
        pl_str, pl_color = f"+{pl:,}", WIN_GREEN
    elif pl < 0:
        pl_str, pl_color = f"{pl:,}", LOSS_RED
    else:
        pl_str, pl_color = "0", SILVER

    # Truncate player name if too long
    name_display = player_name if len(player_name) <= 14 else player_name[:12] + ".."

    values = [
        (name_display, OFF_WHITE),
        (f"{wager:,}", OFF_WHITE),
        (f"{payout:,}", OFF_WHITE),
        (pl_str, pl_color),
    ]
    for i, (val, color) in enumerate(values):
        draw.text((col_x[i], y_cursor), val, fill=color, font=font_value)

    y_cursor += 28

    # ── Big wager highlight row ───────────────────────────────────────────
    if is_big:
        y_cursor += 4
        if outcome == "win":
            highlight = f"MASSIVE WIN  —  {payout:,} BUCKS PAYOUT"
            hl_color = WIN_GREEN
        elif outcome == "loss":
            highlight = f"HIGH ROLLER LOSS  —  {wager:,} BUCKS GONE"
            hl_color = LOSS_RED
        else:
            highlight = f"HIGH STAKES PUSH  —  {wager:,} BUCKS RETURNED"
            hl_color = PUSH_AMBER

        # Highlight bar background
        draw.rounded_rectangle(
            (PAD, y_cursor, CARD_W - PAD, y_cursor + 22),
            radius=3,
            fill=(*hl_color[:3], 20),
        )
        draw.text((PAD + 10, y_cursor + 3), highlight, fill=hl_color, font=font_big)
        y_cursor += 28

    # ── Bottom footer bar ─────────────────────────────────────────────────
    footer_y = h - TOP_BAR - 28
    draw.line([(PAD, footer_y), (CARD_W - PAD, footer_y)], fill=GOLD_DIM, width=1)

    balance_str = f"BALANCE:  {new_balance:,}"
    time_str = datetime.now(timezone.utc).strftime("%I:%M %p")

    draw.text((PAD + 4, footer_y + 8), balance_str, fill=SILVER, font=font_small)
    # Right-align time
    time_bbox = font_small.getbbox(time_str)
    time_w = time_bbox[2] - time_bbox[0]
    draw.text((CARD_W - PAD - time_w - 4, footer_y + 8), time_str, fill=GOLD_DIM, font=font_small)

    # ── Export to PNG buffer ──────────────────────────────────────────────
    # Convert RGBA → RGB for smaller file size
    out = Image.new("RGB", img.size, BLACK_BG)
    out.paste(img, mask=img.split()[3])

    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
