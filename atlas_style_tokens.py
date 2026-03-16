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
