"""
atlas_card_renderer.py — ATLAS™ Universal Card Rendering Engine
═══════════════════════════════════════════════════════════════════════════════
A reusable Pillow-based rendering system that generates premium dark-themed
image cards for any ATLAS module. Each module provides its own icon, title,
subtitle, and content blocks; the renderer handles all visual consistency.

Usage:
    from atlas_card_renderer import ATLASCard, CardSection

    card = ATLASCard(
        module_icon="icons/sportsbook.png",
        module_title="ATLAS SPORTSBOOK",
        module_subtitle="GLOBAL WAGERING",
        version="v5.0",
    )
    card.add_section(CardSection.hero_number("YOUR BALANCE", "$939", delta="+$42 this week"))
    card.add_section(CardSection.sparkline("7-DAY", data_points=[897, 880, 882, 916, 918, 929, 939]))
    card.add_section(CardSection.win_loss_ticker([True, True, False, True, None], record="3-1-1"))
    card.add_section(CardSection.info_panel([
        {"label": "OPEN BETS", "value": "3", "sub": "$185 wagered", "sub_highlight": "$185"},
        {"label": "POTENTIAL PAYOUT", "value": "$340", "sub": "if all bets hit", "value_color": "green"},
    ]))
    card.add_section(CardSection.sport_footer(
        sports=["TSL", "NFL", "NBA", "MLB", "NHL"],
        active="TSL",
        controller_icon=True,
    ))
    card.set_status_bar("positive")  # "positive", "negative", "top10"
    img = card.render()
    img.save("sportsbook_card.png")
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ═════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION & CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Card dimensions — sized for mobile readability (1.5× scale)
CARD_WIDTH = 750
CARD_CORNER_RADIUS = 18

# Resolve asset paths relative to this file's directory
_BASE_DIR = Path(__file__).parent
FONT_DIR = _BASE_DIR / "fonts"
ICON_DIR = _BASE_DIR / "icons"
ASSET_DIR = _BASE_DIR / "card_assets"

# ── Color Palette ─────────────────────────────────────────────────────────────
class Colors:
    # Backgrounds
    BG_PRIMARY = (17, 17, 17)          # #111111
    BG_DEEP = (10, 10, 10)             # #0A0A0A
    BG_PANEL = (255, 255, 255, 6)      # rgba panel bg (2.5% white)
    BG_FOOTER = (0, 0, 0, 64)          # rgba footer bg

    # Gold accent
    GOLD = (212, 175, 55)              # #D4AF37
    GOLD_DIM = (138, 122, 69)          # #8A7A45
    GOLD_BRIGHT = (240, 208, 96)       # #F0D060

    # Text
    WHITE = (255, 255, 255)
    TEXT_PRIMARY = (255, 255, 255)
    TEXT_SECONDARY = (170, 170, 170)    # #AAAAAA
    TEXT_DIM = (74, 74, 74)             # #4A4A4A
    TEXT_MUTED = (51, 51, 51)           # #333333
    TEXT_VERSION = (51, 51, 51)         # #333333

    # Accent colors
    GREEN = (74, 222, 128)             # #4ADE80
    GREEN_DARK = (34, 197, 94)         # #22C55E
    RED = (248, 113, 113)              # #F87171
    RED_DARK = (239, 68, 68)           # #EF4444
    PUSH_GRAY = (85, 85, 85)           # #555555

    # Borders / Dividers
    BORDER_GOLD_BRIGHT = (212, 175, 55, 76)   # gold 30%
    BORDER_GOLD_DIM = (212, 175, 55, 15)      # gold 6%
    BORDER_WHITE_SUBTLE = (255, 255, 255, 15)  # white 6%
    BORDER_WHITE_BEVEL = (255, 255, 255, 10)   # white 4% (inner bevel)
    BORDER_DARK = (0, 0, 0, 76)               # black 30% (shadow edge)
    DIVIDER = (255, 255, 255, 8)               # white 3%

    # Status bar
    STATUS_POSITIVE = [(74, 222, 128), (34, 197, 94), (74, 222, 128)]
    STATUS_NEGATIVE = [(248, 113, 113), (239, 68, 68), (248, 113, 113)]
    STATUS_TOP10 = [(212, 175, 55), (240, 208, 96), (212, 175, 55)]

    # Green pill bg
    GREEN_PILL_BG = (74, 222, 128, 20)        # green 8%
    GREEN_PILL_BORDER = (74, 222, 128, 30)     # green 12%

    # Active sport pill
    ACTIVE_PILL_BG = (212, 175, 55, 25)       # gold 10%
    ACTIVE_PILL_BORDER = (212, 175, 55, 38)    # gold 15%


# ── Font Loading ──────────────────────────────────────────────────────────────
class Fonts:
    """Lazy font loader — caches fonts at first access."""
    _cache: dict[str, ImageFont.FreeTypeFont] = {}

    @classmethod
    def _load(cls, name: str, size: int) -> ImageFont.FreeTypeFont:
        key = f"{name}_{size}"
        if key not in cls._cache:
            path = FONT_DIR / name
            if not path.exists():
                # Fallback to system fonts
                try:
                    cls._cache[key] = ImageFont.truetype("arial.ttf", size)
                except OSError:
                    cls._cache[key] = ImageFont.load_default()
                return cls._cache[key]
            cls._cache[key] = ImageFont.truetype(str(path), size)
        return cls._cache[key]

    # Display font (Outfit)
    @classmethod
    def display_regular(cls, size: int) -> ImageFont.FreeTypeFont:
        return cls._load("Outfit-Regular.ttf", size)

    @classmethod
    def display_semibold(cls, size: int) -> ImageFont.FreeTypeFont:
        return cls._load("Outfit-SemiBold.ttf", size)

    @classmethod
    def display_bold(cls, size: int) -> ImageFont.FreeTypeFont:
        return cls._load("Outfit-Bold.ttf", size)

    @classmethod
    def display_extrabold(cls, size: int) -> ImageFont.FreeTypeFont:
        return cls._load("Outfit-ExtraBold.ttf", size)

    # Mono font (JetBrains Mono)
    @classmethod
    def mono_regular(cls, size: int) -> ImageFont.FreeTypeFont:
        return cls._load("JetBrainsMono-Regular.ttf", size)

    @classmethod
    def mono_bold(cls, size: int) -> ImageFont.FreeTypeFont:
        return cls._load("JetBrainsMono-Bold.ttf", size)

    @classmethod
    def mono_extrabold(cls, size: int) -> ImageFont.FreeTypeFont:
        return cls._load("JetBrainsMono-ExtraBold.ttf", size)


# ═════════════════════════════════════════════════════════════════════════════
#  DRAWING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _rounded_rect(draw: ImageDraw.Draw, xy: tuple, radius: int,
                  fill=None, outline=None, width=1):
    """Draw a rounded rectangle (Pillow >= 8.2 has built-in, but we add fallback)."""
    try:
        draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)
    except AttributeError:
        # Fallback for older Pillow
        x0, y0, x1, y1 = xy
        draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
        draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
        draw.pieslice([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=fill)
        draw.pieslice([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=fill)
        draw.pieslice([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=fill)
        draw.pieslice([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=fill)


def _draw_gradient_line(img: Image.Image, y: int, x_start: int, x_end: int,
                        color_start: tuple, color_end: tuple):
    """Draw a 1px horizontal gradient line."""
    draw = ImageDraw.Draw(img, 'RGBA')
    width = x_end - x_start
    for x in range(width):
        t = x / max(width - 1, 1)
        r = int(color_start[0] + (color_end[0] - color_start[0]) * t)
        g = int(color_start[1] + (color_end[1] - color_start[1]) * t)
        b = int(color_start[2] + (color_end[2] - color_start[2]) * t)
        a = int(color_start[3] + (color_end[3] - color_start[3]) * t) if len(color_start) > 3 else 255
        draw.point((x_start + x, y), fill=(r, g, b, a))


def _draw_status_bar(img: Image.Image, y: int, height: int, status: str,
                     width: int = CARD_WIDTH):
    """Draw the dynamic status bar."""
    colors_map = {
        "positive": Colors.STATUS_POSITIVE,
        "negative": Colors.STATUS_NEGATIVE,
        "top10": Colors.STATUS_TOP10,
    }
    colors = colors_map.get(status, Colors.STATUS_POSITIVE)
    draw = ImageDraw.Draw(img)
    w = width
    for x in range(w):
        t = x / max(w - 1, 1)
        if t < 0.5:
            t2 = t * 2
            c = tuple(int(colors[0][i] + (colors[1][i] - colors[0][i]) * t2) for i in range(3))
        else:
            t2 = (t - 0.5) * 2
            c = tuple(int(colors[1][i] + (colors[2][i] - colors[1][i]) * t2) for i in range(3))
        for dy in range(height):
            draw.point((x, y + dy), fill=c)


def _apply_noise(img: Image.Image, opacity: float = 0.03):
    """Apply a subtle noise texture overlay."""
    noise_path = ASSET_DIR / "noise_texture.png"
    if noise_path.exists():
        noise = Image.open(noise_path).convert('RGBA')
        # Tile the noise across the image
        noise_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        for y in range(0, img.height, noise.height):
            for x in range(0, img.width, noise.width):
                noise_layer.paste(noise, (x, y))
        img = Image.alpha_composite(img.convert('RGBA'), noise_layer)
        return img
    else:
        # Generate noise procedurally
        noise_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        pixels = noise_layer.load()
        random.seed(42)
        alpha = int(255 * opacity)
        for y in range(img.height):
            for x in range(img.width):
                v = random.randint(0, 255)
                pixels[x, y] = (v, v, v, alpha)
        return Image.alpha_composite(img.convert('RGBA'), noise_layer)


def _apply_radial_glow(img: Image.Image, center: tuple, radius: int,
                       color: tuple, intensity: float = 0.07):
    """Apply a subtle radial gold glow."""
    glow = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    cx, cy = center
    for r in range(radius, 0, -1):
        t = 1.0 - (r / radius)
        alpha = int(255 * intensity * t * t)
        alpha = min(alpha, 255)
        c = (*color[:3], alpha)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    return Image.alpha_composite(img, glow)


def _draw_drop_shadow(img: Image.Image, xy: tuple, radius: int,
                      shadow_offset: int = 3, shadow_alpha: int = 90):
    """Draw a drop shadow behind a rounded rectangle area."""
    x0, y0, x1, y1 = xy
    shadow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer, 'RGBA')
    _rounded_rect(shadow_draw,
                  (x0 + shadow_offset, y0 + shadow_offset,
                   x1 + shadow_offset, y1 + shadow_offset),
                  radius, fill=(0, 0, 0, shadow_alpha))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=3))
    return Image.alpha_composite(img, shadow_layer)


def _draw_beveled_panel(img: Image.Image, draw: ImageDraw.Draw, xy: tuple,
                        radius: int = 8, fill=(255, 255, 255, 6)):
    """Draw a panel with inner bevel (light top+left, dark bottom+right) and drop shadow."""
    x0, y0, x1, y1 = xy

    # Drop shadow
    img = _draw_drop_shadow(img, xy, radius)
    draw = ImageDraw.Draw(img, 'RGBA')

    # Main panel
    _rounded_rect(draw, xy, radius, fill=fill)

    # Inner bevel — top and left 1px lighter
    _rounded_rect(draw, (x0, y0, x1, y0 + 1), 0,
                  fill=(255, 255, 255, 15))  # top edge
    _rounded_rect(draw, (x0, y0, x0 + 1, y1), 0,
                  fill=(255, 255, 255, 10))  # left edge

    # Bottom and right 1px darker
    _rounded_rect(draw, (x0, y1 - 1, x1, y1), 0,
                  fill=(0, 0, 0, 76))  # bottom edge
    _rounded_rect(draw, (x1 - 1, y0, x1, y1), 0,
                  fill=(0, 0, 0, 50))  # right edge

    return img, draw


def _text_width(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont) -> int:
    """Get text width using the best available method."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except AttributeError:
        w, _ = draw.textsize(text, font=font)
        return w


def _text_height(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont) -> int:
    """Get text height."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[3] - bbox[1]
    except AttributeError:
        _, h = draw.textsize(text, font=font)
        return h


# ═════════════════════════════════════════════════════════════════════════════
#  CARD SECTION DEFINITIONS
# ═════════════════════════════════════════════════════════════════════════════

class SectionType(Enum):
    HERO_NUMBER = "hero_number"
    SPARKLINE = "sparkline"
    WIN_LOSS_TICKER = "win_loss_ticker"
    INFO_PANEL = "info_panel"
    SPORT_FOOTER = "sport_footer"
    STAT_GRID = "stat_grid"
    DIVIDER = "divider"
    TEXT_BLOCK = "text_block"
    # V6 section types
    HERO_BALANCE = "hero_balance"
    RANK_DELTA_ROW = "rank_delta_row"
    GOLD_DIVIDER = "gold_divider"
    DARK_FOOTER = "dark_footer"


@dataclass
class CardSection:
    """A content section that the card renderer knows how to draw."""
    type: SectionType
    data: dict = field(default_factory=dict)

    @staticmethod
    def hero_number(label: str, value: str, delta: str = None,
                    delta_positive: bool = True) -> CardSection:
        return CardSection(SectionType.HERO_NUMBER, {
            "label": label, "value": value,
            "delta": delta, "delta_positive": delta_positive,
        })

    @staticmethod
    def sparkline(label: str, data_points: list[float]) -> CardSection:
        return CardSection(SectionType.SPARKLINE, {
            "label": label, "points": data_points,
        })

    @staticmethod
    def win_loss_ticker(results: list[Optional[bool]], record: str = "") -> CardSection:
        """results: True=win, False=loss, None=push"""
        return CardSection(SectionType.WIN_LOSS_TICKER, {
            "results": results, "record": record,
        })

    @staticmethod
    def info_panel(panels: list[dict]) -> CardSection:
        """panels: [{"label": str, "value": str, "sub": str, "sub_highlight": str, "value_color": str}]"""
        return CardSection(SectionType.INFO_PANEL, {"panels": panels})

    @staticmethod
    def sport_footer(sports: list[str], active: str = None,
                     controller_icon: bool = False,
                     tagline: str = "Select below ↓") -> CardSection:
        return CardSection(SectionType.SPORT_FOOTER, {
            "sports": sports, "active": active,
            "controller_icon": controller_icon, "tagline": tagline,
        })

    @staticmethod
    def stat_grid(stats: list[dict], columns: int = 2,
                  style: str = "classic") -> CardSection:
        """stats: [{"label": str, "value": str, "value_color": str}]
        style: "classic" (beveled panels) or "v6" (flat, gold labels, centered)
        """
        return CardSection(SectionType.STAT_GRID, {
            "stats": stats, "columns": columns, "style": style,
        })

    @staticmethod
    def divider() -> CardSection:
        return CardSection(SectionType.DIVIDER, {})

    @staticmethod
    def text_block(text: str, color: tuple = None, centered: bool = True,
                   font_size: int = 11) -> CardSection:
        return CardSection(SectionType.TEXT_BLOCK, {
            "text": text, "color": color, "centered": centered,
            "font_size": font_size,
        })

    # ── V6 Section Factories ─────────────────────────────────────────────────

    @staticmethod
    def hero_balance(balance: int) -> CardSection:
        """Massive centered balance with gold dollar sign."""
        return CardSection(SectionType.HERO_BALANCE, {"balance": balance})

    @staticmethod
    def rank_delta_row(rank: int, total_users: int, weekly_delta: int) -> CardSection:
        """Rank pill + weekly delta pill, centered side by side."""
        return CardSection(SectionType.RANK_DELTA_ROW, {
            "rank": rank, "total_users": total_users,
            "weekly_delta": weekly_delta,
        })

    @staticmethod
    def gold_divider() -> CardSection:
        """Full-width gold gradient divider line."""
        return CardSection(SectionType.GOLD_DIVIDER, {})

    @staticmethod
    def dark_footer(left_text: str, right_text: str) -> CardSection:
        """Dark watermark footer with left/right text."""
        return CardSection(SectionType.DARK_FOOTER, {
            "left": left_text, "right": right_text,
        })


# ═════════════════════════════════════════════════════════════════════════════
#  ATLAS CARD — MAIN RENDERER
# ═════════════════════════════════════════════════════════════════════════════

class ATLASCard:
    """
    The main card renderer. Build a card by adding sections, then call render().

    Produces a Pillow Image object ready to save as PNG or pass to discord.py
    via io.BytesIO.
    """

    def __init__(
        self,
        module_icon: str | Path = None,
        module_title: str = "ATLAS MODULE",
        module_subtitle: str = "",
        version: str = "v1.0",
        accent_color: tuple = None,  # Override gold accent if desired
        width: int = CARD_WIDTH,
        corner_radius: int = CARD_CORNER_RADIUS,
    ):
        self.module_icon = Path(module_icon) if module_icon else None
        self.module_title = module_title
        self.module_subtitle = module_subtitle
        self.version = version
        self.accent_color = accent_color or Colors.GOLD
        self.width = width
        self.corner_radius = corner_radius
        self.sections: list[CardSection] = []
        self.status_bar: str | None = None  # "positive", "negative", "top10"
        self.status_bar_position: str = "bottom"  # "top" or "bottom"

    def add_section(self, section: CardSection) -> ATLASCard:
        self.sections.append(section)
        return self

    def set_status_bar(self, status: str, position: str = "bottom") -> ATLASCard:
        self.status_bar = status
        self.status_bar_position = position
        return self

    def render(self) -> Image.Image:
        """Render the full card and return as a Pillow Image."""
        w = self.width
        cr = self.corner_radius

        # Phase 1: Calculate total height
        height = self._calculate_height()

        # Phase 2: Create base image with background
        img = Image.new('RGBA', (w, height), Colors.BG_PRIMARY)
        img = self._draw_background(img)

        # Phase 3: Status bar at top (V6 style) — drawn before header
        y_cursor = 0
        if self.status_bar and self.status_bar_position == "top":
            _draw_status_bar(img, 0, 5, self.status_bar, w)
            y_cursor = 5

        # Phase 4: Draw header
        y_cursor = self._draw_header(img, y_cursor)

        # Phase 5: Draw each section
        for section in self.sections:
            img, y_cursor = self._draw_section(img, section, y_cursor)

        # Phase 6: Apply noise texture
        img = _apply_noise(img)

        # Phase 7: Status bar at bottom (classic style)
        if self.status_bar and self.status_bar_position == "bottom":
            _draw_status_bar(img, height - 6, 6, self.status_bar, w)

        # Phase 8: Outer border
        border_alpha = 46 if self.status_bar_position == "top" else 30
        draw = ImageDraw.Draw(img, 'RGBA')
        _rounded_rect(draw, (0, 0, w - 1, height - 1),
                       cr, outline=(212, 175, 55, border_alpha), width=1)

        return img

    # ── Height Calculation ────────────────────────────────────────────────────
    def _calculate_height(self) -> int:
        v6 = self.version == "" or self.version is None
        h = 5 if (self.status_bar and self.status_bar_position == "top") else 0
        h += 90 if v6 else 75  # Header (V6 is taller: 56px icon + padding)
        for s in self.sections:
            if s.type == SectionType.HERO_NUMBER:
                h += 142
            elif s.type == SectionType.SPARKLINE:
                pass
            elif s.type == SectionType.WIN_LOSS_TICKER:
                h += 44
            elif s.type == SectionType.INFO_PANEL:
                h += 108
            elif s.type == SectionType.SPORT_FOOTER:
                h += 56
            elif s.type == SectionType.STAT_GRID:
                rows = math.ceil(len(s.data["stats"]) / s.data["columns"])
                style = s.data.get("style", "classic")
                if style == "v6":
                    h += (rows * 82) + 4  # 2px gaps, no extra padding
                else:
                    h += 24 + (rows * 90) + 12
            elif s.type == SectionType.DIVIDER:
                h += 18
            elif s.type == SectionType.TEXT_BLOCK:
                h += 44
            # V6 section types
            elif s.type == SectionType.HERO_BALANCE:
                h += 150  # label + 104px number + padding
            elif s.type == SectionType.RANK_DELTA_ROW:
                h += 72  # pills + padding
            elif s.type == SectionType.GOLD_DIVIDER:
                h += 1
            elif s.type == SectionType.DARK_FOOTER:
                h += 52  # 14px padding top/bottom + text
        if self.status_bar and self.status_bar_position == "bottom":
            h += 6
        return h

    # ── Background ────────────────────────────────────────────────────────────
    def _draw_background(self, img: Image.Image) -> Image.Image:
        w = self.width
        img = _apply_radial_glow(img, (int(w * 0.15), -20),
                                  200, self.accent_color, 0.07)
        img = _apply_radial_glow(img, (int(w * 0.85), img.height + 20),
                                  150, self.accent_color, 0.03)
        return img

    # ── Header ────────────────────────────────────────────────────────────────
    def _draw_header(self, img: Image.Image, y_start: int = 0) -> int:
        draw = ImageDraw.Draw(img, 'RGBA')
        w = self.width
        pad_x = 36
        v6 = self.version == "" or self.version is None

        if v6:
            # V6 header: bigger icon (56px), no version, gold subtitle
            header_h = 90
            icon_size = 56
            icon_x = pad_x
            icon_y = y_start + (header_h - icon_size) // 2

            # Icon container: dark bg with gold border
            container_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            container_draw = ImageDraw.Draw(container_layer, 'RGBA')
            _rounded_rect(container_draw,
                          (icon_x, icon_y, icon_x + icon_size, icon_y + icon_size),
                          14, fill=(10, 10, 10, 255))
            _rounded_rect(container_draw,
                          (icon_x, icon_y, icon_x + icon_size, icon_y + icon_size),
                          14, outline=(212, 175, 55, 38), width=1)
            img = Image.alpha_composite(img, container_layer)
            draw = ImageDraw.Draw(img, 'RGBA')

            # Icon image inside container
            if self.module_icon and self.module_icon.exists():
                icon_img = Image.open(self.module_icon).convert('RGBA')
                inner = 48
                icon_img = icon_img.resize((inner, inner), Image.LANCZOS)
                offset = (icon_size - inner) // 2
                img.paste(icon_img, (icon_x + offset, icon_y + offset), icon_img)
                draw = ImageDraw.Draw(img, 'RGBA')

            # Title: Outfit ExtraBold 26px
            title_x = icon_x + icon_size + 16
            title_font = Fonts.display_extrabold(26)
            draw.text((title_x, icon_y + 6), self.module_title,
                      font=title_font, fill=Colors.WHITE)

            # Subtitle: Outfit Bold 14px, gold at 70%
            if self.module_subtitle:
                sub_font = Fonts.display_bold(14)
                sub_color = (*self.accent_color[:3], 179)  # 70% opacity
                draw.text((title_x, icon_y + 38), self.module_subtitle,
                          font=sub_font, fill=sub_color)

            return y_start + header_h
        else:
            # Classic header
            header_h = 75
            icon_size = 48
            icon_x = pad_x
            icon_y = y_start + (header_h - icon_size) // 2

            if self.module_icon and self.module_icon.exists():
                icon_img = Image.open(self.module_icon).convert('RGBA')
                icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
                mask = Image.new('L', (icon_size, icon_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle([0, 0, icon_size - 1, icon_size - 1],
                                             radius=10, fill=255)
                img.paste(icon_img, (icon_x, icon_y), mask)

            title_x = icon_x + icon_size + 14
            title_font = Fonts.display_bold(22)
            draw.text((title_x, icon_y + 2), self.module_title,
                      font=title_font, fill=Colors.TEXT_PRIMARY)

            if self.module_subtitle:
                sub_font = Fonts.display_semibold(13)
                sub_color = (*self.accent_color[:3], 191)
                draw.text((title_x, icon_y + 28), self.module_subtitle,
                          font=sub_font, fill=sub_color)

            ver_font = Fonts.mono_regular(12)
            ver_w = _text_width(draw, self.version, ver_font)
            draw.text((w - pad_x - ver_w, icon_y + 12),
                      self.version, font=ver_font, fill=Colors.TEXT_MUTED)

            _draw_gradient_line(img, y_start + header_h - 1, pad_x, w - pad_x,
                               (*self.accent_color, 76), (*self.accent_color, 0))

            return y_start + header_h

    # ── Section Router ────────────────────────────────────────────────────────
    def _draw_section(self, img: Image.Image, section: CardSection,
                      y: int) -> tuple[Image.Image, int]:
        handlers = {
            SectionType.HERO_NUMBER: self._draw_hero,
            SectionType.WIN_LOSS_TICKER: self._draw_ticker,
            SectionType.INFO_PANEL: self._draw_info_panel,
            SectionType.SPORT_FOOTER: self._draw_sport_footer,
            SectionType.STAT_GRID: self._draw_stat_grid,
            SectionType.DIVIDER: self._draw_divider,
            SectionType.TEXT_BLOCK: self._draw_text_block,
            # V6
            SectionType.HERO_BALANCE: self._draw_hero_balance,
            SectionType.RANK_DELTA_ROW: self._draw_rank_delta_row,
            SectionType.GOLD_DIVIDER: self._draw_gold_divider,
            SectionType.DARK_FOOTER: self._draw_dark_footer,
        }
        handler = handlers.get(section.type)
        if handler:
            return handler(img, section.data, y)
        return img, y

    # ── Hero Number ───────────────────────────────────────────────────────────
    def _draw_hero(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        w = self.width
        pad_y = 32

        # Label
        label_font = Fonts.display_semibold(14)
        label = data["label"].upper()
        label_w = _text_width(draw, label, label_font)
        draw.text(((w - label_w) // 2, y + pad_y),
                  label, font=label_font, fill=Colors.TEXT_DIM)

        # Main value
        value = data["value"]
        if value.startswith("$"):
            dollar_font = Fonts.mono_extrabold(42)
            num_font = Fonts.mono_extrabold(72)
            num_text = value[1:]

            dollar_w = _text_width(draw, "$", dollar_font)
            num_w = _text_width(draw, num_text, num_font)
            num_h = _text_height(draw, num_text, num_font)
            total_w = dollar_w + 3 + num_w
            start_x = (w - total_w) // 2

            val_y = y + pad_y + 24
            dollar_h = _text_height(draw, "$", dollar_font)
            dollar_y_offset = num_h - dollar_h
            draw.text((start_x, val_y + dollar_y_offset), "$", font=dollar_font,
                      fill=(255, 255, 255, 76))
            draw.text((start_x + dollar_w + 3, val_y), num_text,
                      font=num_font, fill=Colors.TEXT_PRIMARY)
        else:
            num_font = Fonts.mono_extrabold(72)
            num_w = _text_width(draw, value, num_font)
            val_y = y + pad_y + 24
            draw.text(((w - num_w) // 2, val_y), value,
                      font=num_font, fill=Colors.TEXT_PRIMARY)

        # Sub row: delta pill + sparkline
        sub_y = val_y + 80
        sub_elements = []

        if data.get("delta"):
            delta_text = f"▲ {data['delta']}" if data.get("delta_positive", True) else f"▼ {data['delta']}"
            delta_font = Fonts.mono_bold(18)
            delta_w = _text_width(draw, delta_text, delta_font)
            pill_w = delta_w + 30
            pill_h = 32
            sub_elements.append(("delta", pill_w, pill_h, delta_text, delta_font))

        spark_data = None
        for s in self.sections:
            if s.type == SectionType.SPARKLINE:
                spark_data = s.data
                break

        spark_label_w = 0
        spark_chart_w = 120
        if spark_data:
            spark_label_font = Fonts.display_semibold(11)
            spark_label_w = _text_width(draw, spark_data["label"], spark_label_font) + 8
            spark_total_w = spark_label_w + spark_chart_w
            sub_elements.append(("spark", spark_total_w, 40, spark_data, spark_label_font))

        gap = 28
        total_sub_w = sum(e[1] for e in sub_elements) + gap * (len(sub_elements) - 1) if sub_elements else 0
        sub_x = (w - total_sub_w) // 2

        for elem in sub_elements:
            if elem[0] == "delta":
                _, pw, ph, dt, df = elem
                delta_color = Colors.GREEN if data.get("delta_positive", True) else Colors.RED
                _rounded_rect(draw, (sub_x, sub_y, sub_x + pw, sub_y + ph), 6,
                              fill=(*delta_color[:3], 20))
                _rounded_rect(draw, (sub_x, sub_y, sub_x + pw, sub_y + ph), 6,
                              outline=(*delta_color[:3], 30), width=1)
                tw = _text_width(draw, dt, df)
                draw.text((sub_x + (pw - tw) // 2, sub_y + 5), dt,
                          font=df, fill=delta_color)
                sub_x += pw + gap

            elif elem[0] == "spark":
                _, sw, sh, sd, sf = elem
                draw.text((sub_x, sub_y + 6), sd["label"],
                          font=sf, fill=Colors.TEXT_MUTED)
                chart_x = sub_x + spark_label_w
                chart_y = sub_y
                self._draw_sparkline_inline(img, sd["points"],
                                            chart_x, chart_y, spark_chart_w, sh)
                sub_x += sw + gap

        return img, sub_y + 44

    # ── Sparkline (inline drawing helper) ─────────────────────────────────────
    def _draw_sparkline_inline(self, img: Image.Image, points: list[float],
                                x: int, y: int, w: int, h: int):
        if not points or len(points) < 2:
            return
        draw = ImageDraw.Draw(img, 'RGBA')

        min_val = min(points)
        max_val = max(points)
        val_range = max_val - min_val or 1

        # Convert to pixel coordinates
        coords = []
        for i, val in enumerate(points):
            px = x + int(i * w / (len(points) - 1))
            py = y + h - int(((val - min_val) / val_range) * (h - 6)) - 3
            coords.append((px, py))

        # Fill area
        fill_points = coords + [(coords[-1][0], y + h), (coords[0][0], y + h)]
        # Draw fill as semi-transparent
        fill_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        fill_draw = ImageDraw.Draw(fill_layer)
        fill_draw.polygon(fill_points, fill=(*Colors.GREEN[:3], 25))
        img_comp = Image.alpha_composite(img, fill_layer)
        # Copy back
        img.paste(img_comp)

        # Draw line
        draw = ImageDraw.Draw(img, 'RGBA')
        for i in range(len(coords) - 1):
            draw.line([coords[i], coords[i + 1]], fill=Colors.GREEN, width=2)

        # End dot
        last = coords[-1]
        draw.ellipse([last[0] - 3, last[1] - 3, last[0] + 3, last[1] + 3],
                     fill=Colors.GREEN)

    # ── Win/Loss Ticker ───────────────────────────────────────────────────────
    def _draw_ticker(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        w = self.width
        results = data["results"]
        record = data.get("record", "")

        label_font = Fonts.display_semibold(11)
        record_font = Fonts.mono_regular(15)

        label_text = "LAST " + str(len(results))
        label_w = _text_width(draw, label_text, label_font)

        dot_size = 15
        dot_gap = 6
        dots_w = len(results) * dot_size + (len(results) - 1) * dot_gap
        record_w = _text_width(draw, record, record_font) if record else 0

        gap = 12
        total_w = label_w + gap + dots_w + (gap + record_w if record else 0)
        start_x = (w - total_w) // 2

        # Label
        draw.text((start_x, y + 6), label_text, font=label_font,
                  fill=Colors.TEXT_MUTED)

        # Dots
        dx = start_x + label_w + gap
        for r in results:
            if r is True:
                color = Colors.GREEN
            elif r is False:
                color = Colors.RED
            else:
                color = Colors.PUSH_GRAY
            _rounded_rect(draw, (dx, y + 4, dx + dot_size, y + 4 + dot_size), 3,
                          fill=color)
            dx += dot_size + dot_gap

        # Record
        if record:
            draw.text((dx + gap - dot_gap, y + 4), record,
                      font=record_font, fill=Colors.PUSH_GRAY)

        return img, y + 44

    # ── Info Panel ────────────────────────────────────────────────────────────
    def _draw_info_panel(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        panels = data["panels"]
        pad_x = 36
        panel_y = y + 6
        panel_h = 84
        panel_w = (self.width - pad_x * 2 - 3) // len(panels)

        for i, panel in enumerate(panels):
            px = pad_x + i * (panel_w + 3)
            py = panel_y

            # Beveled panel
            radius = 12 if i == 0 else (12 if i == len(panels) - 1 else 0)
            img, draw = _draw_beveled_panel(img, ImageDraw.Draw(img, 'RGBA'),
                                            (px, py, px + panel_w, py + panel_h),
                                            radius=radius)

            # Label
            label_font = Fonts.display_semibold(12)
            draw.text((px + 16, py + 10), panel["label"].upper(),
                      font=label_font, fill=Colors.TEXT_DIM)

            # Value
            value_font = Fonts.mono_bold(26)
            value_color = Colors.TEXT_PRIMARY
            if panel.get("value_color") == "green":
                value_color = Colors.GREEN
            elif panel.get("value_color") == "red":
                value_color = Colors.RED
            elif panel.get("value_color") == "gold":
                value_color = Colors.GOLD
            draw.text((px + 16, py + 30), panel["value"],
                      font=value_font, fill=value_color)

            # Sub text
            if panel.get("sub"):
                sub_font = Fonts.display_regular(13)
                sub_text = panel["sub"]
                highlight = panel.get("sub_highlight")
                if highlight and highlight in sub_text:
                    # Split and draw highlight in gold
                    parts = sub_text.split(highlight)
                    sx = px + 16
                    sy = py + 62
                    hl_font = Fonts.display_semibold(13)
                    for j, part in enumerate(parts):
                        if part:
                            draw.text((sx, sy), part, font=sub_font,
                                      fill=Colors.PUSH_GRAY)
                            sx += _text_width(draw, part, sub_font)
                        if j < len(parts) - 1:
                            draw.text((sx, sy), highlight, font=hl_font,
                                      fill=Colors.GOLD)
                            sx += _text_width(draw, highlight, hl_font)
                else:
                    draw.text((px + 16, py + 62), sub_text,
                              font=sub_font, fill=Colors.PUSH_GRAY)

        return img, panel_y + panel_h + 18

    # ── Sport Footer ──────────────────────────────────────────────────────────
    def _draw_sport_footer(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        w = self.width
        pad_x = 36
        footer_h = 56

        # Footer background
        draw.rectangle([0, y, w, y + footer_h],
                       fill=(0, 0, 0, 140))
        # Top border
        draw.line([(0, y), (w, y)], fill=(255, 255, 255, 20), width=1)

        # Sports
        sport_font = Fonts.display_semibold(15)
        sx = pad_x
        sy = y + (footer_h - 30) // 2

        for sport in data["sports"]:
            is_active = sport == data.get("active")
            sw = _text_width(draw, sport, sport_font)

            if is_active:
                # Active pill
                pill_w = sw + 44  # Extra space for dot + controller
                _rounded_rect(draw, (sx, sy, sx + pill_w, sy + 30), 6,
                              fill=(*self.accent_color[:3], 25))
                _rounded_rect(draw, (sx, sy, sx + pill_w, sy + 30), 6,
                              outline=(*self.accent_color[:3], 38), width=1)

                # Green live dot
                dot_x = sx + 8
                dot_cy = sy + 15
                draw.ellipse([dot_x, dot_cy - 4, dot_x + 7, dot_cy + 3],
                             fill=Colors.GREEN)

                # Controller icon (simplified for Pillow)
                if data.get("controller_icon"):
                    cx = dot_x + 14
                    cy = sy + 5
                    # Simple controller shape
                    draw.rounded_rectangle([cx, cy + 3, cx + 18, cy + 15], radius=4,
                                            outline=self.accent_color, width=1)
                    draw.point((cx + 6, cy + 9), fill=self.accent_color)
                    draw.point((cx + 12, cy + 9), fill=self.accent_color)
                    text_x = cx + 22
                else:
                    text_x = dot_x + 14

                draw.text((text_x, sy + 5), sport, font=sport_font,
                          fill=self.accent_color)
                sx += pill_w + 8
            else:
                draw.text((sx, sy + 5), sport, font=sport_font,
                          fill=(120, 120, 120))
                sx += sw + 18

        # Tagline (right-aligned)
        if data.get("tagline"):
            tag_font = Fonts.display_regular(12)
            tag_w = _text_width(draw, data["tagline"], tag_font)
            draw.text((w - pad_x - tag_w, sy + 8),
                      data["tagline"], font=tag_font, fill=(90, 90, 90))

        return img, y + footer_h

    # ── Stat Grid ─────────────────────────────────────────────────────────────
    def _draw_stat_grid(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        stats = data["stats"]
        cols = data["columns"]
        style = data.get("style", "classic")
        w = self.width
        pad_x = 36

        if style == "v6":
            return self._draw_stat_grid_v6(img, stats, cols, y, w, pad_x)

        # Classic style (beveled panels)
        gap = 12
        cell_w = (w - pad_x * 2 - gap * (cols - 1)) // cols
        cell_h = 78

        cy = y + 12
        for i, stat in enumerate(stats):
            col = i % cols
            row = i // cols
            cx = pad_x + col * (cell_w + gap)
            ry = cy + row * (cell_h + gap)

            img, draw = _draw_beveled_panel(img, ImageDraw.Draw(img, 'RGBA'),
                                            (cx, ry, cx + cell_w, ry + cell_h),
                                            radius=9)

            label_font = Fonts.display_semibold(12)
            draw.text((cx + 14, ry + 10), stat["label"].upper(),
                      font=label_font, fill=Colors.TEXT_DIM)

            val_font = Fonts.mono_bold(24)
            val_color = Colors.TEXT_PRIMARY
            vc = stat.get("value_color", "")
            if vc == "green":
                val_color = Colors.GREEN
            elif vc == "red":
                val_color = Colors.RED
            elif vc == "gold":
                val_color = Colors.GOLD
            draw.text((cx + 14, ry + 36), stat["value"],
                      font=val_font, fill=val_color)

        rows = math.ceil(len(stats) / cols)
        return img, cy + rows * (cell_h + gap) + 6

    def _draw_stat_grid_v6(self, img: Image.Image, stats: list, cols: int,
                            y: int, w: int, pad_x: int) -> tuple[Image.Image, int]:
        """V6 flat stat grid: subtle bg, gold labels, centered text, rounded outer corners."""
        gap = 2
        grid_w = w - pad_x * 2
        cell_w = (grid_w - gap * (cols - 1)) // cols
        cell_h = 78
        rows = math.ceil(len(stats) / cols)
        grid_radius = 16

        cy = y
        for i, stat in enumerate(stats):
            col = i % cols
            row = i // cols
            cx = pad_x + col * (cell_w + gap)
            ry = cy + row * (cell_h + gap)

            # Determine corner radius for this cell
            r_tl = grid_radius if (row == 0 and col == 0) else 0
            r_tr = grid_radius if (row == 0 and col == cols - 1) else 0
            r_bl = grid_radius if (row == rows - 1 and col == 0) else 0
            r_br = grid_radius if (row == rows - 1 and col == cols - 1) else 0

            # Draw cell background on alpha layer
            cell_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            cell_draw = ImageDraw.Draw(cell_layer, 'RGBA')

            # Use max radius for rounded_rectangle, then mask corners
            max_r = max(r_tl, r_tr, r_bl, r_br)
            if max_r > 0:
                # For cells with mixed corners, draw rounded rect and overlay
                # square corners where needed
                _rounded_rect(cell_draw,
                              (cx, ry, cx + cell_w, ry + cell_h),
                              max_r, fill=(255, 255, 255, 5))
                # Square off corners that shouldn't be rounded
                sq = max_r
                if r_tl == 0:
                    cell_draw.rectangle([cx, ry, cx + sq, ry + sq],
                                        fill=(255, 255, 255, 5))
                if r_tr == 0:
                    cell_draw.rectangle([cx + cell_w - sq, ry,
                                         cx + cell_w, ry + sq],
                                        fill=(255, 255, 255, 5))
                if r_bl == 0:
                    cell_draw.rectangle([cx, ry + cell_h - sq,
                                         cx + sq, ry + cell_h],
                                        fill=(255, 255, 255, 5))
                if r_br == 0:
                    cell_draw.rectangle([cx + cell_w - sq, ry + cell_h - sq,
                                         cx + cell_w, ry + cell_h],
                                        fill=(255, 255, 255, 5))
            else:
                cell_draw.rectangle([cx, ry, cx + cell_w, ry + cell_h],
                                     fill=(255, 255, 255, 5))

            img = Image.alpha_composite(img, cell_layer)
            draw = ImageDraw.Draw(img, 'RGBA')

            # Label: centered, gold at 55% opacity
            label_font = Fonts.display_bold(13)
            label_text = stat["label"].upper()
            lw = _text_width(draw, label_text, label_font)
            draw.text((cx + (cell_w - lw) // 2, ry + 14),
                      label_text, font=label_font,
                      fill=(212, 175, 55, 140))

            # Value: centered, 34px
            val_font = Fonts.mono_extrabold(34)
            val_color = Colors.WHITE
            vc = stat.get("value_color", "")
            if vc == "green":
                val_color = Colors.GREEN
            elif vc == "red":
                val_color = Colors.RED
            elif vc == "gold":
                val_color = Colors.GOLD
            vw = _text_width(draw, stat["value"], val_font)
            draw.text((cx + (cell_w - vw) // 2, ry + 36),
                      stat["value"], font=val_font, fill=val_color)

        return img, cy + rows * (cell_h + gap) + 4

    # ── Divider ───────────────────────────────────────────────────────────────
    def _draw_divider(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        w = self.width
        draw.line([(36, y + 9), (w - 36, y + 9)],
                  fill=(255, 255, 255, 10), width=1)
        return img, y + 18

    # ── Text Block ────────────────────────────────────────────────────────────
    def _draw_text_block(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        w = self.width
        font = Fonts.display_regular(int(data.get("font_size", 11) * 1.5))
        color = data.get("color") or Colors.TEXT_SECONDARY
        text = data["text"]

        if data.get("centered"):
            tw = _text_width(draw, text, font)
            x = (w - tw) // 2
        else:
            x = 36
        draw.text((x, y + 12), text, font=font, fill=color)
        return img, y + 44

    # ═════════════════════════════════════════════════════════════════════════
    #  V6 SECTION RENDERERS
    # ═════════════════════════════════════════════════════════════════════════

    def _draw_hero_balance(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        """Massive centered balance: gold $ sign + white number."""
        draw = ImageDraw.Draw(img, 'RGBA')
        w = self.width
        balance = data["balance"]
        balance_str = f"{balance:,}"

        # "YOUR BALANCE" label
        label_font = Fonts.display_bold(16)
        label_text = "YOUR BALANCE"
        lw = _text_width(draw, label_text, label_font)
        draw.text(((w - lw) // 2, y + 32), label_text,
                  font=label_font, fill=(212, 175, 55, 166))  # gold ~65%

        # Dollar sign (64px, gold, superscript offset)
        dollar_font = Fonts.mono_bold(64)
        num_font = Fonts.mono_extrabold(104)

        dollar_w = _text_width(draw, "$", dollar_font)
        num_w = _text_width(draw, balance_str, num_font)
        total_w = dollar_w + 2 + num_w
        start_x = (w - total_w) // 2

        num_y = y + 56
        # Dollar sign raised ~20px above number baseline
        num_h = _text_height(draw, balance_str, num_font)
        dollar_h = _text_height(draw, "$", dollar_font)
        dollar_y = num_y + (num_h - dollar_h) - 20

        draw.text((start_x, dollar_y), "$", font=dollar_font,
                  fill=(212, 175, 55))  # gold
        draw.text((start_x + dollar_w + 2, num_y), balance_str,
                  font=num_font, fill=Colors.WHITE)

        return img, y + 150

    def _draw_rank_delta_row(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        """Rank pill + delta pill, centered side by side."""
        w = self.width
        rank = data["rank"]
        total = data["total_users"]
        delta = data["weekly_delta"]
        is_top10 = rank <= 10

        # ── Rank pill ──
        rank_text = f"#{rank}"
        rank_label = "RANK"
        of_text = f"of {total}"

        rank_num_font = Fonts.mono_extrabold(28)
        rank_lbl_font = Fonts.display_bold(10)
        rank_of_font = Fonts.mono_regular(13)

        tmp_draw = ImageDraw.Draw(img, 'RGBA')
        rank_num_w = _text_width(tmp_draw, rank_text, rank_num_font)
        rank_lbl_w = _text_width(tmp_draw, rank_label, rank_lbl_font)
        rank_of_w = _text_width(tmp_draw, of_text, rank_of_font)
        rank_right_w = max(rank_lbl_w, rank_of_w)
        rank_pill_w = 22 + rank_num_w + 8 + rank_right_w + 22
        rank_pill_h = 44

        # ── Delta pill ──
        if delta >= 0:
            delta_text = f"▲ +${delta:,} this week"
        else:
            delta_text = f"▼ -${abs(delta):,} this week"
        delta_font = Fonts.mono_bold(18)
        delta_w = _text_width(tmp_draw, delta_text, delta_font)
        delta_pill_w = delta_w + 40
        delta_pill_h = 44

        # Center the two pills
        gap = 20
        total_w = rank_pill_w + gap + delta_pill_w
        start_x = (w - total_w) // 2

        # Draw rank pill
        pill_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        pill_draw = ImageDraw.Draw(pill_layer, 'RGBA')

        rx = start_x
        ry = y + 16

        if is_top10:
            pill_bg = (212, 175, 55, 15)     # gold 6%
            pill_border = (212, 175, 55, 36)  # gold 14%
            rank_num_color = (212, 175, 55)
            rank_lbl_color = (212, 175, 55, 153)  # 60%
        else:
            pill_bg = (255, 255, 255, 5)
            pill_border = (255, 255, 255, 15)
            rank_num_color = (102, 102, 102)
            rank_lbl_color = (85, 85, 85, 153)

        _rounded_rect(pill_draw, (rx, ry, rx + rank_pill_w, ry + rank_pill_h),
                       24, fill=pill_bg)
        _rounded_rect(pill_draw, (rx, ry, rx + rank_pill_w, ry + rank_pill_h),
                       24, outline=pill_border, width=1)
        img = Image.alpha_composite(img, pill_layer)
        draw = ImageDraw.Draw(img, 'RGBA')

        # Rank number
        draw.text((rx + 22, ry + 7), rank_text,
                  font=rank_num_font, fill=rank_num_color)

        # RANK label + "of N"
        right_x = rx + 22 + rank_num_w + 8
        draw.text((right_x, ry + 8), rank_label,
                  font=rank_lbl_font, fill=rank_lbl_color)
        draw.text((right_x, ry + 22), of_text,
                  font=rank_of_font, fill=(85, 85, 85))

        # Draw delta pill
        dx = start_x + rank_pill_w + gap
        delta_positive = delta >= 0

        if delta_positive:
            d_bg = (74, 222, 128, 18)
            d_border = (74, 222, 128, 36)
            d_color = Colors.GREEN
        else:
            d_bg = (248, 113, 113, 18)
            d_border = (248, 113, 113, 36)
            d_color = Colors.RED

        delta_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        delta_draw = ImageDraw.Draw(delta_layer, 'RGBA')
        _rounded_rect(delta_draw, (dx, ry, dx + delta_pill_w, ry + delta_pill_h),
                       24, fill=d_bg)
        _rounded_rect(delta_draw, (dx, ry, dx + delta_pill_w, ry + delta_pill_h),
                       24, outline=d_border, width=1)
        img = Image.alpha_composite(img, delta_layer)
        draw = ImageDraw.Draw(img, 'RGBA')

        dtw = _text_width(draw, delta_text, delta_font)
        draw.text((dx + (delta_pill_w - dtw) // 2, ry + 11),
                  delta_text, font=delta_font, fill=d_color)

        return img, y + 72

    def _draw_gold_divider(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        """Full-width gold gradient line that fades at edges."""
        w = self.width
        draw = ImageDraw.Draw(img, 'RGBA')
        gold = self.accent_color[:3]
        alpha_max = 51  # ~20% opacity

        for x in range(w):
            # Fade: 0→full over first 15%, hold, full→0 over last 15%
            t = x / max(w - 1, 1)
            if t < 0.15:
                a = int(alpha_max * (t / 0.15))
            elif t > 0.85:
                a = int(alpha_max * ((1.0 - t) / 0.15))
            else:
                a = alpha_max
            draw.point((x, y), fill=(*gold, a))

        return img, y + 1

    def _draw_dark_footer(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        """Dark watermark footer with barely-visible text."""
        w = self.width
        footer_h = 52
        pad_x = 36

        # Footer background
        footer_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        footer_draw = ImageDraw.Draw(footer_layer, 'RGBA')
        footer_draw.rectangle([0, y, w, y + footer_h], fill=(0, 0, 0, 71))
        img = Image.alpha_composite(img, footer_layer)
        draw = ImageDraw.Draw(img, 'RGBA')

        # Left text
        foot_font = Fonts.mono_regular(11)
        ghost_color = (38, 38, 38)  # #262626
        draw.text((pad_x, y + 18), data["left"],
                  font=foot_font, fill=ghost_color)

        # Right text
        rw = _text_width(draw, data["right"], foot_font)
        draw.text((w - pad_x - rw, y + 18), data["right"],
                  font=foot_font, fill=ghost_color)

        return img, y + footer_h


# ═════════════════════════════════════════════════════════════════════════════
#  CONVENIENCE: Quick card generation for discord.py
# ═════════════════════════════════════════════════════════════════════════════

def card_to_discord_file(card: ATLASCard, filename: str = "card.png") -> bytes:
    """Render a card and return bytes suitable for discord.File(io.BytesIO(...))."""
    import io
    img = card.render()
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


# ═════════════════════════════════════════════════════════════════════════════
#  SELF-TEST
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Quick test render
    card = ATLASCard(
        module_icon=ICON_DIR / "sportsbook.png",
        module_title="ATLAS SPORTSBOOK",
        module_subtitle="GLOBAL WAGERING",
        version="v5.0",
    )
    card.add_section(CardSection.hero_number(
        "YOUR BALANCE", "$939", delta="+$42 this week", delta_positive=True
    ))
    card.add_section(CardSection.sparkline("7-DAY", [897, 880, 882, 916, 918, 929, 939]))
    card.add_section(CardSection.win_loss_ticker(
        [True, True, False, True, None], record="3-1-1"
    ))
    card.add_section(CardSection.info_panel([
        {"label": "OPEN BETS", "value": "3", "sub": "$185 wagered", "sub_highlight": "$185"},
        {"label": "POTENTIAL PAYOUT", "value": "$340", "sub": "if all bets hit", "value_color": "green"},
    ]))
    card.add_section(CardSection.sport_footer(
        sports=["TSL", "NFL", "NBA", "MLB", "NHL"],
        active="TSL", controller_icon=True,
    ))
    card.set_status_bar("positive")

    img = card.render()
    img.save("test_card.png")
    print(f"Test card saved: {img.size}")
