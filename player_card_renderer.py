"""
player_card_renderer.py — ATLAS™ Player Profile Card Renderer
═══════════════════════════════════════════════════════════════════════════════
Premium Pillow-based player card generator in the ATLAS dark-gold visual
language.  Renders a full scouting-report-style card with:

  • Header bar (TSL branding + team color accent)
  • Player identity block (name, team, photo placeholder, OVR diamond)
  • Bio panel (position, archetype, age, exp, college, measurables, value)
  • Contract panel (salary, cap hit, years remaining)
  • Dev trait badge + abilities row
  • 3-row attribute grid (6 columns) with color-coded ratings

Usage:
    from player_card_renderer import render_player_card

    img = render_player_card({
        "firstName": "Drake", "lastName": "Maye",
        "team": "New England Patriots", "teamAbbr": "NE",
        "position": "QB", "jerseyNum": 10,
        "archetype": "Field General QB",
        "age": 28, "experience": 6, "college": "North Carolina",
        "height": "6'4\"", "weight": 223,
        "overallRating": 99, "playerValue": 9174.4,
        "devTrait": "Star",
        "salary": "$136 M", "capHit": "$45.41 M", "yearsLeft": "4 / 7",
        "abilities": [],
        "attributes": {
            "SPD": 89, "ACC": 89, "AGI": 82, "COD": 78, "STR": 62, "AWR": 94,
            "TGH": 90, "INJ": 89, "STA": 87, "THP": 94, "SAC": 96, "MAC": 98,
            "DAC": 99, "TOR": 98, "TUP": 98, "PAC": 99, "BKS": 88,
        },
    })
    img.save("drake_maye.png")
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ═════════════════════════════════════════════════════════════════════════════
#  PATHS & CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

_BASE = Path(__file__).parent
FONT_DIR = _BASE / "fonts"
ASSET_DIR = _BASE / "card_assets"

CARD_W = 800
PAD = 36

# NFL team primary colors (subset — extend as needed)
TEAM_COLORS: dict[str, tuple[tuple, tuple]] = {
    # abbr: (primary, secondary)
    "NE":  ((1, 36, 86),    (198, 12, 48)),
    "KC":  ((227, 24, 55),  (255, 184, 28)),
    "BUF": ((0, 51, 141),   (198, 12, 48)),
    "MIA": ((0, 142, 151),  (252, 76, 2)),
    "NYJ": ((18, 87, 64),   (255, 255, 255)),
    "BAL": ((26, 25, 95),   (158, 124, 12)),
    "CIN": ((251, 79, 20),  (0, 0, 0)),
    "CLE": ((49, 29, 0),    (255, 60, 0)),
    "PIT": ((255, 182, 18), (16, 24, 32)),
    "HOU": ((3, 32, 47),    (167, 25, 48)),
    "IND": ((0, 44, 95),    (162, 170, 173)),
    "JAX": ((0, 103, 120),  (215, 162, 42)),
    "TEN": ((12, 35, 64),   (75, 146, 219)),
    "DEN": ((251, 79, 20),  (0, 34, 68)),
    "LV":  ((0, 0, 0),      (165, 172, 175)),
    "LAC": ((0, 128, 198),  (255, 194, 14)),
    "DAL": ((0, 53, 148),   (134, 147, 151)),
    "NYG": ((1, 35, 82),    (163, 13, 45)),
    "PHI": ((0, 76, 84),    (165, 172, 175)),
    "WAS": ((90, 20, 20),   (255, 182, 18)),
    "CHI": ((11, 22, 42),   (200, 56, 3)),
    "DET": ((0, 118, 182),  (176, 183, 188)),
    "GB":  ((24, 48, 40),   (255, 184, 28)),
    "MIN": ((79, 38, 131),  (255, 198, 47)),
    "ATL": ((167, 25, 48),  (0, 0, 0)),
    "CAR": ((0, 133, 202),  (16, 24, 32)),
    "NO":  ((211, 188, 141),(16, 24, 32)),
    "TB":  ((213, 10, 10),  (52, 48, 43)),
    "ARI": ((151, 35, 63),  (0, 0, 0)),
    "LAR": ((0, 53, 148),   (255, 209, 0)),
    "SF":  ((170, 0, 0),    (173, 153, 93)),
    "SEA": ((0, 34, 68),    (105, 190, 40)),
}

# ═════════════════════════════════════════════════════════════════════════════
#  PALETTE
# ═════════════════════════════════════════════════════════════════════════════

class C:
    """Color palette."""
    BG          = (12, 12, 16)
    BG_PANEL    = (20, 20, 26)
    BG_CELL     = (28, 28, 36)
    GOLD        = (212, 175, 55)
    GOLD_DIM    = (140, 115, 36)
    WHITE       = (255, 255, 255)
    SILVER      = (185, 185, 195)
    DIM         = (100, 100, 110)
    MUTED       = (60, 60, 68)
    GREEN       = (74, 222, 128)
    RED         = (248, 113, 113)
    BLUE        = (96, 165, 250)
    CYAN        = (34, 211, 238)
    AMBER       = (251, 191, 36)
    # Rating tiers
    ELITE       = (74, 222, 128)    # 95+
    GREAT       = (96, 165, 250)    # 90-94
    GOOD        = (212, 175, 55)    # 80-89
    AVG         = (185, 185, 195)   # 70-79
    BELOW       = (248, 113, 113)   # <70


def _rating_color(val: int) -> tuple:
    if val >= 95: return C.ELITE
    if val >= 90: return C.GREAT
    if val >= 80: return C.GOOD
    if val >= 70: return C.AVG
    return C.BELOW


# ═════════════════════════════════════════════════════════════════════════════
#  FONT LOADER
# ═════════════════════════════════════════════════════════════════════════════

_font_cache: dict[str, ImageFont.FreeTypeFont] = {}

def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = f"{name}:{size}"
    if key not in _font_cache:
        path = FONT_DIR / name
        if path.exists():
            _font_cache[key] = ImageFont.truetype(str(path), size)
        else:
            try:
                _font_cache[key] = ImageFont.truetype("arial.ttf", size)
            except OSError:
                _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _outfit(size: int) -> ImageFont.FreeTypeFont:
    return _font("Outfit-Bold.ttf", size)

def _outfit_sb(size: int) -> ImageFont.FreeTypeFont:
    return _font("Outfit-SemiBold.ttf", size)

def _outfit_r(size: int) -> ImageFont.FreeTypeFont:
    return _font("Outfit-Regular.ttf", size)

def _outfit_xb(size: int) -> ImageFont.FreeTypeFont:
    return _font("Outfit-ExtraBold.ttf", size)

def _mono(size: int) -> ImageFont.FreeTypeFont:
    return _font("JetBrainsMono-Bold.ttf", size)

def _mono_xb(size: int) -> ImageFont.FreeTypeFont:
    return _font("JetBrainsMono-ExtraBold.ttf", size)

def _mono_r(size: int) -> ImageFont.FreeTypeFont:
    return _font("JetBrainsMono-Regular.ttf", size)


# ═════════════════════════════════════════════════════════════════════════════
#  DRAWING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _tw(draw: ImageDraw.Draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]

def _th(draw: ImageDraw.Draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]

def _center_text(draw, text, font, y, color, x_start=0, x_end=CARD_W):
    w = _tw(draw, text, font)
    draw.text(((x_start + x_end - w) // 2, y), text, font=font, fill=color)

def _right_text(draw, text, font, y, color, x_right=CARD_W - PAD):
    w = _tw(draw, text, font)
    draw.text((x_right - w, y), text, font=font, fill=color)


def _rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _gradient_h(img: Image.Image, xy, color_l, color_r):
    """Horizontal gradient fill inside a rectangle."""
    x0, y0, x1, y1 = xy
    w = x1 - x0
    for x in range(w):
        t = x / max(w - 1, 1)
        r = int(color_l[0] + (color_r[0] - color_l[0]) * t)
        g = int(color_l[1] + (color_r[1] - color_l[1]) * t)
        b = int(color_l[2] + (color_r[2] - color_l[2]) * t)
        a = 255
        if len(color_l) > 3:
            a = int(color_l[3] + (color_r[3] - color_l[3]) * t)
        for dy in range(y1 - y0):
            img.putpixel((x0 + x, y0 + dy), (r, g, b, a))


def _draw_diamond(draw, cx, cy, size, fill, outline=None):
    """Draw a diamond (rotated square) shape."""
    points = [(cx, cy - size), (cx + size, cy), (cx, cy + size), (cx - size, cy)]
    draw.polygon(points, fill=fill, outline=outline)


def _add_glow(img, center, radius, color, intensity=0.06):
    glow = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    cx, cy = center
    for r in range(radius, 0, -2):
        t = 1.0 - (r / radius)
        a = int(255 * intensity * t * t)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color[:3], min(a, 255)))
    return Image.alpha_composite(img, glow)


def _panel_shadow(img, xy, radius=10, offset=4, alpha=100):
    x0, y0, x1, y1 = xy
    shadow = Image.new('RGBA', img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow, 'RGBA')
    _rounded_rect(sd, (x0 + offset, y0 + offset, x1 + offset, y1 + offset),
                  radius, fill=(0, 0, 0, alpha))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=5))
    return Image.alpha_composite(img, shadow)


def _beveled_panel(img, xy, radius=10, fill=None):
    """Draw a panel with subtle bevel and shadow, return updated img."""
    fill = fill or C.BG_PANEL
    img = _panel_shadow(img, xy, radius)
    draw = ImageDraw.Draw(img, 'RGBA')
    _rounded_rect(draw, xy, radius, fill=fill)
    x0, y0, x1, y1 = xy
    # Top bevel
    draw.line([(x0 + radius, y0), (x1 - radius, y0)], fill=(255, 255, 255, 18), width=1)
    # Left bevel
    draw.line([(x0, y0 + radius), (x0, y1 - radius)], fill=(255, 255, 255, 10), width=1)
    return img


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN RENDERER
# ═════════════════════════════════════════════════════════════════════════════

def render_player_card(data: dict) -> Image.Image:
    """
    Render a premium ATLAS player profile card.

    Parameters
    ----------
    data : dict
        Player data with keys: firstName, lastName, team, teamAbbr, position,
        jerseyNum, archetype, age, experience, college, height, weight,
        overallRating, playerValue, devTrait, salary, capHit, yearsLeft,
        abilities (list[str]), attributes (dict[str, int]).

    Returns
    -------
    PIL.Image.Image
    """
    abbr = data.get("teamAbbr", "NE")
    team_primary, team_secondary = TEAM_COLORS.get(abbr, ((100, 100, 100), (200, 200, 200)))
    ovr = data.get("overallRating", 0)
    attrs = data.get("attributes", {})
    abilities = data.get("abilities", [])

    # ── Calculate height ──────────────────────────────────────────────────
    # Header: 56, Identity block: 200, Bio+Contract: 155, Dev/Abilities: 60
    # Attr grid: rows * 90 + gaps, Footer: 48
    attr_count = len(attrs)
    attr_cols = 6
    attr_rows = math.ceil(attr_count / attr_cols) if attr_count else 0
    attr_height = attr_rows * 82 + (attr_rows - 1) * 8 + 24 if attr_rows else 0

    total_h = 56 + 210 + 160 + 62 + attr_height + 52
    CARD_H = total_h

    # ── Create canvas ─────────────────────────────────────────────────────
    img = Image.new('RGBA', (CARD_W, CARD_H), C.BG)

    # Background glows
    img = _add_glow(img, (0, 0), 350, team_primary, 0.08)
    img = _add_glow(img, (CARD_W, CARD_H), 250, team_secondary, 0.04)
    img = _add_glow(img, (CARD_W // 2, 120), 200, C.GOLD, 0.03)

    draw = ImageDraw.Draw(img, 'RGBA')

    y = 0

    # ═══════════════════════════════════════════════════════════════════════
    #  HEADER BAR — team color gradient + TSL branding
    # ═══════════════════════════════════════════════════════════════════════
    header_h = 56

    # Team color gradient bar (left to right, primary → dark)
    bar = Image.new('RGBA', (CARD_W, CARD_H), (0, 0, 0, 0))
    _gradient_h(bar, (0, 0, CARD_W, header_h),
                (*team_primary, 220), (*team_primary[:2] + (max(team_primary[2] - 40, 0),), 160))
    img = Image.alpha_composite(img, bar)
    draw = ImageDraw.Draw(img, 'RGBA')

    # Diagonal accent stripe
    stripe_x = CARD_W - 220
    points = [(stripe_x, 0), (stripe_x + 60, 0),
              (stripe_x + 20, header_h), (stripe_x - 40, header_h)]
    draw.polygon(points, fill=(*team_secondary, 70))

    # TSL text
    draw.text((PAD, 14), "TSL", font=_outfit_xb(26), fill=C.WHITE)

    # Module label
    _right_text(draw, "ATLAS  ORACLE", _outfit_sb(16), 18, (*C.GOLD, 200))

    # Bottom edge
    draw.line([(0, header_h - 1), (CARD_W, header_h - 1)], fill=(*C.GOLD, 60), width=1)

    y = header_h

    # ═══════════════════════════════════════════════════════════════════════
    #  IDENTITY BLOCK — name, team, photo placeholder, OVR diamond
    # ═══════════════════════════════════════════════════════════════════════
    ident_h = 210
    ident_y = y

    # Photo placeholder (left side) — silhouette box with team color border
    photo_x = PAD
    photo_y = ident_y + 20
    photo_w = 150
    photo_h = 170
    img = _beveled_panel(img, (photo_x, photo_y, photo_x + photo_w, photo_y + photo_h),
                         radius=12, fill=(30, 30, 38))
    draw = ImageDraw.Draw(img, 'RGBA')
    _rounded_rect(draw, (photo_x, photo_y, photo_x + photo_w, photo_y + photo_h),
                  12, outline=(*team_primary, 120), width=2)

    # Silhouette icon (simplified player shape)
    cx = photo_x + photo_w // 2
    cy = photo_y + photo_h // 2 - 5
    # Head
    draw.ellipse([cx - 22, cy - 45, cx + 22, cy - 1], fill=(50, 50, 60))
    # Shoulders/body
    draw.rounded_rectangle([cx - 38, cy + 5, cx + 38, cy + 65], radius=14, fill=(50, 50, 60))
    # Jersey number overlay
    _center_text(draw, f"#{data.get('jerseyNum', '')}", _mono_xb(28),
                 cy + 12, (*team_secondary, 140), photo_x, photo_x + photo_w)

    # Player name (right of photo)
    name_x = photo_x + photo_w + 24
    name_y = ident_y + 24

    first = data.get("firstName", "")
    last = data.get("lastName", "")
    draw.text((name_x, name_y), first, font=_outfit_r(28), fill=C.SILVER)
    draw.text((name_x, name_y + 32), last.upper(), font=_outfit_xb(44), fill=C.WHITE)

    # Team name
    draw.text((name_x, name_y + 82), data.get("team", ""), font=_outfit_sb(18), fill=C.DIM)

    # ── OVR Diamond (right side) ──────────────────────────────────────────
    ovr_cx = CARD_W - PAD - 70
    ovr_cy = ident_y + 85
    diamond_size = 58

    # Diamond shadow
    shadow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer, 'RGBA')
    _draw_diamond(sd, ovr_cx + 3, ovr_cy + 3, diamond_size + 2, (0, 0, 0, 120))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6))
    img = Image.alpha_composite(img, shadow_layer)
    draw = ImageDraw.Draw(img, 'RGBA')

    # Diamond fill — gradient effect via layered diamonds
    if ovr >= 95:
        d_fill = C.ELITE
        d_outline = (100, 255, 160)
    elif ovr >= 90:
        d_fill = C.GREAT
        d_outline = (130, 190, 255)
    elif ovr >= 80:
        d_fill = C.GOLD
        d_outline = C.GOLD_DIM
    else:
        d_fill = C.SILVER
        d_outline = C.DIM

    # Outer glow diamond
    _draw_diamond(draw, ovr_cx, ovr_cy, diamond_size + 4, None, outline=(*d_outline, 40))
    # Main diamond
    _draw_diamond(draw, ovr_cx, ovr_cy, diamond_size, d_fill)
    # Inner highlight
    _draw_diamond(draw, ovr_cx, ovr_cy - 2, diamond_size - 8, None,
                  outline=(*C.WHITE, 30))

    # OVR number
    ovr_text = str(ovr)
    ovr_font = _mono_xb(44)
    ovr_w = _tw(draw, ovr_text, ovr_font)
    ovr_h = _th(draw, ovr_text, ovr_font)
    draw.text((ovr_cx - ovr_w // 2, ovr_cy - ovr_h // 2 - 2), ovr_text,
              font=ovr_font, fill=C.BG)

    # "OVR" label
    _center_text(draw, "OVR", _outfit_sb(13), ovr_cy + diamond_size - 8,
                 C.DIM, ovr_cx - 40, ovr_cx + 40)

    y += ident_h

    # ═══════════════════════════════════════════════════════════════════════
    #  BIO + CONTRACT PANELS (side by side)
    # ═══════════════════════════════════════════════════════════════════════
    panel_y = y
    panel_h = 140
    gap = 12
    left_w = (CARD_W - PAD * 2 - gap) * 55 // 100
    right_w = CARD_W - PAD * 2 - gap - left_w

    # ── Bio Panel ─────────────────────────────────────────────────────────
    bio_xy = (PAD, panel_y, PAD + left_w, panel_y + panel_h)
    img = _beveled_panel(img, bio_xy, radius=10)
    draw = ImageDraw.Draw(img, 'RGBA')

    # Section label
    draw.text((PAD + 16, panel_y + 10), "PLAYER INFO", font=_outfit_sb(11), fill=C.MUTED)

    # Bio lines
    pos = data.get("position", "")
    num = data.get("jerseyNum", "")
    arch = data.get("archetype", "")
    bio_lines = [
        f"{pos} #{num}  •  {arch}",
        f"Age {data.get('age', '')}  •  Exp: {data.get('experience', '')} Years",
        f"{data.get('college', '')}",
        f"{data.get('height', '')}  {data.get('weight', '')} lbs",
        f"Value: {data.get('playerValue', '')}",
    ]

    lf = _mono_r(14)
    ly = panel_y + 30
    for line in bio_lines:
        draw.text((PAD + 16, ly), line, font=lf, fill=C.SILVER)
        ly += 20

    # ── Contract Panel ────────────────────────────────────────────────────
    contract_x = PAD + left_w + gap
    contract_xy = (contract_x, panel_y, contract_x + right_w, panel_y + panel_h)
    img = _beveled_panel(img, contract_xy, radius=10)
    draw = ImageDraw.Draw(img, 'RGBA')

    draw.text((contract_x + 16, panel_y + 10), "CONTRACT", font=_outfit_sb(11), fill=C.MUTED)

    # Contract icon (small gold circle)
    icon_cx = contract_x + right_w - 30
    icon_cy = panel_y + 20
    draw.ellipse([icon_cx - 8, icon_cy - 8, icon_cx + 8, icon_cy + 8], fill=(*C.GOLD, 40))
    draw.text((icon_cx - 4, icon_cy - 6), "$", font=_mono(12), fill=C.GOLD)

    contract_lines = [
        ("Salary", data.get("salary", "—")),
        ("Cap Hit", data.get("capHit", "—")),
        ("Years Left", data.get("yearsLeft", "—")),
    ]

    cy = panel_y + 34
    for label, val in contract_lines:
        draw.text((contract_x + 16, cy), label, font=_outfit_r(13), fill=C.DIM)
        _right_text(draw, val, _mono(15), cy, C.WHITE, contract_x + right_w - 16)
        cy += 30

    y = panel_y + panel_h + 12

    # ═══════════════════════════════════════════════════════════════════════
    #  DEV TRAIT + ABILITIES ROW
    # ═══════════════════════════════════════════════════════════════════════
    dev_y = y
    dev_h = 44

    # Dev trait badge
    dev_trait = data.get("devTrait", "Normal")
    dev_colors = {
        "Normal":     (C.DIM, C.MUTED),
        "Star":       (C.GOLD, C.GOLD_DIM),
        "Superstar":  (C.CYAN, (0, 160, 180)),
        "X-Factor":   ((200, 100, 255), (140, 60, 200)),
    }
    dev_fg, dev_bg = dev_colors.get(dev_trait, (C.DIM, C.MUTED))

    dev_font = _outfit(16)
    dev_text = f"★  {dev_trait}" if dev_trait != "Normal" else dev_trait
    dev_tw = _tw(draw, dev_text, dev_font)
    dev_pw = dev_tw + 28

    dev_x = PAD
    _rounded_rect(draw, (dev_x, dev_y, dev_x + dev_pw, dev_y + dev_h - 6),
                  8, fill=(*dev_bg, 40))
    _rounded_rect(draw, (dev_x, dev_y, dev_x + dev_pw, dev_y + dev_h - 6),
                  8, outline=(*dev_fg, 80), width=1)
    draw.text((dev_x + 14, dev_y + 8), dev_text, font=dev_font, fill=dev_fg)

    # Abilities
    ab_x = dev_x + dev_pw + 16
    if abilities:
        for ab_name in abilities:
            ab_font = _outfit_sb(13)
            ab_w = _tw(draw, ab_name, ab_font)
            ab_pw = ab_w + 20
            _rounded_rect(draw, (ab_x, dev_y + 2, ab_x + ab_pw, dev_y + dev_h - 8),
                          6, fill=(50, 50, 60))
            draw.text((ab_x + 10, dev_y + 9), ab_name, font=ab_font, fill=C.SILVER)
            ab_x += ab_pw + 8
    else:
        draw.text((ab_x, dev_y + 8), "No abilities", font=_outfit_r(15), fill=C.MUTED)

    y += dev_h + 10

    # ═══════════════════════════════════════════════════════════════════════
    #  ATTRIBUTE GRID — 6 columns × N rows
    # ═══════════════════════════════════════════════════════════════════════
    attr_list = list(attrs.items())
    cols = attr_cols
    rows = math.ceil(len(attr_list) / cols) if attr_list else 0

    cell_gap = 8
    cell_w = (CARD_W - PAD * 2 - cell_gap * (cols - 1)) // cols
    cell_h = 74

    grid_y = y + 4

    # Section label
    draw.text((PAD, grid_y), "ATTRIBUTES", font=_outfit_sb(12), fill=C.MUTED)
    grid_y += 20

    for i, (attr_name, attr_val) in enumerate(attr_list):
        row = i // cols
        col = i % cols
        items_in_row = min(cols, len(attr_list) - row * cols)
        # Center partial last row
        row_width = items_in_row * cell_w + (items_in_row - 1) * cell_gap
        row_offset = (CARD_W - PAD * 2 - row_width) // 2 if items_in_row < cols else 0
        cx = PAD + row_offset + col * (cell_w + cell_gap)
        ry = grid_y + row * (cell_h + cell_gap)

        # Cell background
        cell_xy = (cx, ry, cx + cell_w, ry + cell_h)
        img = _beveled_panel(img, cell_xy, radius=8, fill=C.BG_CELL)
        draw = ImageDraw.Draw(img, 'RGBA')

        val_color = _rating_color(attr_val)

        # Top accent line (colored by rating)
        draw.line([(cx + 6, ry), (cx + cell_w - 6, ry)],
                  fill=(*val_color, 100), width=2)

        # Attribute name
        _center_text(draw, attr_name, _outfit_sb(12), ry + 8, C.DIM, cx, cx + cell_w)

        # Attribute value — large, color-coded
        val_text = str(attr_val)
        val_font = _mono_xb(30)
        _center_text(draw, val_text, val_font, ry + 26, val_color, cx, cx + cell_w)

        # Rating bar at bottom of cell
        bar_x = cx + 8
        bar_y = ry + cell_h - 10
        bar_w = cell_w - 16
        bar_fill = int(bar_w * min(attr_val, 99) / 99)
        _rounded_rect(draw, (bar_x, bar_y, bar_x + bar_w, bar_y + 4), 2,
                      fill=(40, 40, 48))
        if bar_fill > 0:
            _rounded_rect(draw, (bar_x, bar_y, bar_x + bar_fill, bar_y + 4), 2,
                          fill=(*val_color, 180))

    y = grid_y + rows * (cell_h + cell_gap)

    # ═══════════════════════════════════════════════════════════════════════
    #  FOOTER BAR
    # ═══════════════════════════════════════════════════════════════════════
    footer_y = y + 4
    footer_h = 40

    # Gradient separator
    draw.line([(PAD, footer_y), (CARD_W - PAD, footer_y)], fill=(*C.GOLD, 40), width=1)

    # Footer text
    draw.text((PAD, footer_y + 12), "ATLAS™  •  THE SIMULATION LEAGUE",
              font=_outfit_r(12), fill=C.MUTED)
    _right_text(draw, f"{abbr}  {pos}", _mono_r(12), footer_y + 12, (*team_primary, 150))

    # ═══════════════════════════════════════════════════════════════════════
    #  POST-PROCESSING — noise + outer border
    # ═══════════════════════════════════════════════════════════════════════
    # Subtle noise
    noise_path = ASSET_DIR / "noise_texture.png"
    if noise_path.exists():
        noise = Image.open(noise_path).convert('RGBA')
        nlayer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        for ny in range(0, img.height, noise.height):
            for nx in range(0, img.width, noise.width):
                nlayer.paste(noise, (nx, ny))
        img = Image.alpha_composite(img, nlayer)

    draw = ImageDraw.Draw(img, 'RGBA')

    # Outer gold border
    _rounded_rect(draw, (0, 0, CARD_W - 1, CARD_H - 1), 14,
                  outline=(*C.GOLD, 35), width=1)
    # Team color accent border inside
    _rounded_rect(draw, (2, 2, CARD_W - 3, CARD_H - 3), 12,
                  outline=(*team_primary, 25), width=1)

    # Gold status bar at very bottom (3px)
    for x in range(CARD_W):
        t = x / max(CARD_W - 1, 1)
        # Fade: transparent → gold → transparent
        a = int(80 * math.sin(t * math.pi))
        draw.point((x, CARD_H - 1), fill=(*C.GOLD, a))
        draw.point((x, CARD_H - 2), fill=(*C.GOLD, a // 2))

    return img


# ═════════════════════════════════════════════════════════════════════════════
#  CONVENIENCE
# ═════════════════════════════════════════════════════════════════════════════

def player_card_to_bytes(img: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return buf


# ═════════════════════════════════════════════════════════════════════════════
#  SELF-TEST — Drake Maye card from screenshot
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    card_data = {
        "firstName": "Drake",
        "lastName": "Maye",
        "team": "New England Patriots",
        "teamAbbr": "NE",
        "position": "QB",
        "jerseyNum": 10,
        "archetype": "Field General QB",
        "age": 28,
        "experience": 6,
        "college": "North Carolina",
        "height": "6'4\"",
        "weight": 223,
        "overallRating": 99,
        "playerValue": 9174.4,
        "devTrait": "Star",
        "salary": "$136 M",
        "capHit": "$45.41 M",
        "yearsLeft": "4 / 7",
        "abilities": [],
        "attributes": {
            "SPD": 89, "ACC": 89, "AGI": 82, "COD": 78, "STR": 62, "AWR": 94,
            "TGH": 90, "INJ": 89, "STA": 87, "THP": 94, "SAC": 96, "MAC": 98,
            "DAC": 99, "TOR": 98, "TUP": 98, "PAC": 99, "BKS": 88,
        },
    }

    img = render_player_card(card_data)
    img.save("drake_maye_card.png")
    print(f"Rendered: {img.size[0]}×{img.size[1]}px → drake_maye_card.png")
