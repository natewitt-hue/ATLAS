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


def _draw_status_bar(img: Image.Image, y: int, height: int, status: str):
    """Draw the dynamic status bar at the bottom of the card."""
    colors_map = {
        "positive": Colors.STATUS_POSITIVE,
        "negative": Colors.STATUS_NEGATIVE,
        "top10": Colors.STATUS_TOP10,
    }
    colors = colors_map.get(status, Colors.STATUS_POSITIVE)
    draw = ImageDraw.Draw(img)
    w = CARD_WIDTH
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
    """Apply a subtle noise texture overlay.

    Uses a pre-baked noise_texture.png if available (tiles it across the
    image).  Falls back to procedural per-pixel random noise generated with
    a fixed seed (42) for deterministic output.
    """
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
    """Apply a subtle radial gold glow.

    Pre-renders concentric circles with quadratic alpha falloff onto a
    separate RGBA layer, then composites via Image.alpha_composite for
    correct blending (equivalent to GaussianBlur-based glow but with
    explicit radial control).
    """
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
    def stat_grid(stats: list[dict], columns: int = 2) -> CardSection:
        """stats: [{"label": str, "value": str, "value_color": str}]"""
        return CardSection(SectionType.STAT_GRID, {
            "stats": stats, "columns": columns,
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
    ):
        self.module_icon = Path(module_icon) if module_icon else None
        self.module_title = module_title
        self.module_subtitle = module_subtitle
        self.version = version
        self.accent_color = accent_color or Colors.GOLD
        self.sections: list[CardSection] = []
        self.status_bar: str | None = None  # "positive", "negative", "top10"

    def add_section(self, section: CardSection) -> ATLASCard:
        self.sections.append(section)
        return self

    def set_status_bar(self, status: str) -> ATLASCard:
        self.status_bar = status
        return self

    def render(self) -> Image.Image:
        """Render the full card and return as a Pillow Image."""
        # Phase 1: Calculate total height
        height = self._calculate_height()

        # Phase 2: Create base image with background
        img = Image.new('RGBA', (CARD_WIDTH, height), Colors.BG_PRIMARY)
        img = self._draw_background(img)

        # Phase 3: Draw header
        y_cursor = self._draw_header(img)

        # Phase 4: Draw each section
        for section in self.sections:
            img, y_cursor = self._draw_section(img, section, y_cursor)

        # Phase 5: Apply noise texture
        img = _apply_noise(img)

        # Phase 6: Status bar (after noise so it's crisp)
        if self.status_bar:
            _draw_status_bar(img, height - 6, 6, self.status_bar)

        # Phase 7: Outer border (gold glow)
        draw = ImageDraw.Draw(img, 'RGBA')
        _rounded_rect(draw, (0, 0, CARD_WIDTH - 1, height - 1),
                       CARD_CORNER_RADIUS,
                       outline=(212, 175, 55, 30), width=1)

        return img

    # ── Height Calculation ────────────────────────────────────────────────────
    def _calculate_height(self) -> int:
        h = 75  # Header
        for s in self.sections:
            if s.type == SectionType.HERO_NUMBER:
                h += 142
            elif s.type == SectionType.SPARKLINE:
                # Sparkline is drawn inline with HERO_NUMBER — it occupies the
                # same vertical band, so it adds no extra height.  A SPARKLINE
                # section must immediately follow a HERO_NUMBER section.
                pass
            elif s.type == SectionType.WIN_LOSS_TICKER:
                h += 44
            elif s.type == SectionType.INFO_PANEL:
                h += 108
            elif s.type == SectionType.SPORT_FOOTER:
                h += 56
            elif s.type == SectionType.STAT_GRID:
                rows = math.ceil(len(s.data["stats"]) / s.data["columns"])
                h += 24 + (rows * 90) + 12
            elif s.type == SectionType.DIVIDER:
                h += 18
            elif s.type == SectionType.TEXT_BLOCK:
                h += 44
        if self.status_bar:
            h += 6
        return h

    # ── Background ────────────────────────────────────────────────────────────
    def _draw_background(self, img: Image.Image) -> Image.Image:
        # Radial gold glow top-left
        img = _apply_radial_glow(img, (int(CARD_WIDTH * 0.15), -20),
                                  200, self.accent_color, 0.07)
        # Subtle glow bottom-right
        img = _apply_radial_glow(img, (int(CARD_WIDTH * 0.85), img.height + 20),
                                  150, self.accent_color, 0.03)
        return img

    # ── Header ────────────────────────────────────────────────────────────────
    def _draw_header(self, img: Image.Image) -> int:
        draw = ImageDraw.Draw(img, 'RGBA')
        y = 0
        pad_x = 36
        header_h = 75

        # Module icon
        icon_size = 48
        icon_x = pad_x
        icon_y = (header_h - icon_size) // 2

        if self.module_icon and self.module_icon.exists():
            icon_img = Image.open(self.module_icon).convert('RGBA')
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            # Create rounded mask
            mask = Image.new('L', (icon_size, icon_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle([0, 0, icon_size - 1, icon_size - 1],
                                         radius=10, fill=255)
            img.paste(icon_img, (icon_x, icon_y), mask)

        # Title
        title_x = icon_x + icon_size + 14
        title_font = Fonts.display_bold(22)
        draw.text((title_x, icon_y + 2), self.module_title, font=title_font,
                  fill=Colors.TEXT_PRIMARY)

        # Subtitle
        if self.module_subtitle:
            sub_font = Fonts.display_semibold(13)
            sub_color = (*self.accent_color[:3], 191)  # 75% opacity
            draw.text((title_x, icon_y + 28), self.module_subtitle,
                      font=sub_font, fill=sub_color)

        # Version
        ver_font = Fonts.mono_regular(12)
        ver_w = _text_width(draw, self.version, ver_font)
        draw.text((CARD_WIDTH - pad_x - ver_w, icon_y + 12),
                  self.version, font=ver_font, fill=Colors.TEXT_MUTED)

        # Gold gradient divider line under header
        _draw_gradient_line(img, header_h - 1, pad_x, CARD_WIDTH - pad_x,
                           (*self.accent_color, 76), (*self.accent_color, 0))

        return header_h

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
        }
        handler = handlers.get(section.type)
        if handler:
            return handler(img, section.data, y)
        return img, y

    # ── Hero Number ───────────────────────────────────────────────────────────
    def _draw_hero(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        pad_y = 32

        # Label
        label_font = Fonts.display_semibold(14)
        label = data["label"].upper()
        label_w = _text_width(draw, label, label_font)
        draw.text(((CARD_WIDTH - label_w) // 2, y + pad_y),
                  label, font=label_font, fill=Colors.TEXT_DIM)

        # Main value
        value = data["value"]
        # Split dollar sign from number if present
        if value.startswith("$"):
            dollar_font = Fonts.mono_extrabold(42)
            num_font = Fonts.mono_extrabold(72)
            num_text = value[1:]

            dollar_w = _text_width(draw, "$", dollar_font)
            num_w = _text_width(draw, num_text, num_font)
            num_h = _text_height(draw, num_text, num_font)
            total_w = dollar_w + 3 + num_w
            start_x = (CARD_WIDTH - total_w) // 2

            val_y = y + pad_y + 24
            # Align $ baseline with number baseline
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
            draw.text(((CARD_WIDTH - num_w) // 2, val_y), value,
                      font=num_font, fill=Colors.TEXT_PRIMARY)

        # Sub row: delta pill + sparkline
        sub_y = val_y + 80
        sub_elements = []

        # Delta pill
        if data.get("delta"):
            delta_text = f"▲ {data['delta']}" if data.get("delta_positive", True) else f"▼ {data['delta']}"
            delta_font = Fonts.mono_bold(18)
            delta_w = _text_width(draw, delta_text, delta_font)
            pill_w = delta_w + 30
            pill_h = 32
            sub_elements.append(("delta", pill_w, pill_h, delta_text, delta_font))

        # Sparkline (search for sparkline section data)
        spark_data = None
        for s in self.sections:
            if s.type == SectionType.SPARKLINE:
                spark_data = s.data
                break

        spark_label_w = 0
        spark_chart_w = 120
        spark_total_w = 0
        if spark_data:
            spark_label_font = Fonts.display_semibold(11)
            spark_label_w = _text_width(draw, spark_data["label"], spark_label_font) + 8
            spark_total_w = spark_label_w + spark_chart_w
            sub_elements.append(("spark", spark_total_w, 40, spark_data, spark_label_font))

        # Center the sub elements
        gap = 28
        total_sub_w = sum(e[1] for e in sub_elements) + gap * (len(sub_elements) - 1) if sub_elements else 0
        sub_x = (CARD_WIDTH - total_sub_w) // 2

        for elem in sub_elements:
            if elem[0] == "delta":
                _, pw, ph, dt, df = elem
                delta_color = Colors.GREEN if data.get("delta_positive", True) else Colors.RED
                # Pill background
                _rounded_rect(draw, (sub_x, sub_y, sub_x + pw, sub_y + ph), 6,
                              fill=(*delta_color[:3], 20))
                _rounded_rect(draw, (sub_x, sub_y, sub_x + pw, sub_y + ph), 6,
                              outline=(*delta_color[:3], 30), width=1)
                # Text
                tw = _text_width(draw, dt, df)
                draw.text((sub_x + (pw - tw) // 2, sub_y + 5), dt,
                          font=df, fill=delta_color)
                sub_x += pw + gap

            elif elem[0] == "spark":
                _, sw, sh, sd, sf = elem
                # Label
                draw.text((sub_x, sub_y + 6), sd["label"],
                          font=sf, fill=Colors.TEXT_MUTED)
                # Draw sparkline
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
        # Copy composited result back into the mutable img
        img.paste(img_comp, (0, 0))

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
        start_x = (CARD_WIDTH - total_w) // 2

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
        panel_w = (CARD_WIDTH - pad_x * 2 - 3) // len(panels)  # -3 for divider

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
        pad_x = 36
        footer_h = 56

        # Footer background
        draw.rectangle([0, y, CARD_WIDTH, y + footer_h],
                       fill=(0, 0, 0, 140))
        # Top border
        draw.line([(0, y), (CARD_WIDTH, y)], fill=(255, 255, 255, 20), width=1)

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
            draw.text((CARD_WIDTH - pad_x - tag_w, sy + 8),
                      data["tagline"], font=tag_font, fill=(90, 90, 90))

        return img, y + footer_h

    # ── Stat Grid ─────────────────────────────────────────────────────────────
    def _draw_stat_grid(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        stats = data["stats"]
        cols = data["columns"]
        pad_x = 36
        gap = 12
        cell_w = (CARD_WIDTH - pad_x * 2 - gap * (cols - 1)) // cols
        cell_h = 78

        cy = y + 12
        for i, stat in enumerate(stats):
            col = i % cols
            row = i // cols
            cx = pad_x + col * (cell_w + gap)
            ry = cy + row * (cell_h + gap)

            # Beveled panel
            img, draw = _draw_beveled_panel(img, ImageDraw.Draw(img, 'RGBA'),
                                            (cx, ry, cx + cell_w, ry + cell_h),
                                            radius=9)

            # Label
            label_font = Fonts.display_semibold(12)
            draw.text((cx + 14, ry + 10), stat["label"].upper(),
                      font=label_font, fill=Colors.TEXT_DIM)

            # Value
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

    # ── Divider ───────────────────────────────────────────────────────────────
    def _draw_divider(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        draw.line([(36, y + 9), (CARD_WIDTH - 36, y + 9)],
                  fill=(255, 255, 255, 8), width=1)
        return img, y + 18

    # ── Text Block ────────────────────────────────────────────────────────────
    def _draw_text_block(self, img: Image.Image, data: dict, y: int) -> tuple[Image.Image, int]:
        draw = ImageDraw.Draw(img, 'RGBA')
        font = Fonts.display_regular(int(data.get("font_size", 11) * 1.5))
        color = data.get("color") or Colors.TEXT_SECONDARY
        text = data["text"]

        if data.get("centered"):
            tw = _text_width(draw, text, font)
            x = (CARD_WIDTH - tw) // 2
        else:
            x = 36
        draw.text((x, y + 12), text, font=font, fill=color)
        return img, y + 44


# ═════════════════════════════════════════════════════════════════════════════
#  CONVENIENCE: Quick card generation for discord.py
# ═════════════════════════════════════════════════════════════════════════════

def card_to_discord_file(card: ATLASCard, filename: str = "card.png") -> "io.BytesIO":
    """Render a card and return a BytesIO buffer suitable for discord.File(...)."""
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
