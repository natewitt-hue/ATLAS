"""
atlas_themes.py — ATLAS Card Theme Registry
═══════════════════════════════════════════════════════
Each theme overrides only the tokens that differ from base.
The engine merges: base tokens → theme overrides → render.

Usage:
    from atlas_themes import get_theme, THEMES, DEFAULT_THEME
    theme = get_theme("miami_vice")
"""

DEFAULT_THEME = "obsidian_gold"

THEMES = {
    "obsidian_gold": {
        "label": "Obsidian Gold",
        "emoji": "🟡",
        "vars": {
            "bg":             "#0D0D0F",
            "gold":           "#E2C05C",
            "gold-bright":    "#F5DFA0",
            "gold-dim":       "#9E8B4E",
            "gold-light":     "#F5DFA0",
            "win":            "#34D399",
            "win-dark":       "#059669",
            "loss":           "#FB7185",
            "loss-dark":      "#E11D48",
            "text-primary":   "#F0EAD6",
            "text-sub":       "#6B6458",
            "text-muted":     "#9E8B4E",
            "text-dim":       "#6B6458",
            "panel-bg":       "#141416",
            "panel-border":   "rgba(226,192,92,0.04)",
            "panel-border-top": "rgba(226,192,92,0.08)",
        },
        "overlays": ["scanlines", "vignette_warm", "hud_gold", "rim_gold"],
        "hero_class": "hero-gradient-gold",
        "extra_css": """
.hero-gradient-gold {
    background: linear-gradient(135deg, #C9A84C, #F5DFA0 35%, #E8D48A 50%, #F5DFA0 65%, #C9A84C);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 0 20px rgba(245,223,160,0.2));
}
""",
        "status_gradient": "linear-gradient(90deg, #8B7530, #C9A84C, #F5DFA0, #C9A84C, #8B7530)",
        "divider_style": "linear-gradient(90deg, transparent, #5C4D28, transparent)",
        "card_border": "1px solid rgba(226,192,92,0.08)",
        "stat_left_border_default": "2px solid #5C4D28",
        "stat_left_border_accent":  "2px solid #E2C05C",
        "stat_left_border_win":     "2px solid #34D399",
        "stat_box_shadow": "inset 0 1px 0 rgba(245,223,160,0.03), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(52,211,153,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(52,211,153,0.04)",
    },

    "miami_vice": {
        "label": "Miami Vice",
        "emoji": "🌴",
        "vars": {
            "bg":             "#08080F",
            "gold":           "#FF6AC2",
            "gold-bright":    "#00FFD1",
            "gold-dim":       "#7B6A99",
            "gold-light":     "#00FFD1",
            "win":            "#00FFD1",
            "win-dark":       "#00C9A7",
            "loss":           "#FF2E97",
            "loss-dark":      "#DB2777",
            "text-primary":   "#F0F0F8",
            "text-sub":       "#5A5478",
            "text-muted":     "#7B6A99",
            "text-dim":       "#5A5478",
            "panel-bg":       "#0F0F1A",
            "panel-border":   "rgba(255,255,255,0.02)",
            "panel-border-top": "rgba(255,255,255,0.05)",
        },
        "overlays": ["scanlines", "vignette_cool", "hud_vice", "rim_vice"],
        "hero_class": "hero-gradient-neon",
        "extra_css": """
.hero-gradient-neon {
    background: linear-gradient(135deg, #00C9A7, #00FFD1 30%, #80FFE8 50%, #00FFD1 70%, #00C9A7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 0 24px rgba(0,255,209,0.3)) drop-shadow(0 0 8px rgba(0,255,209,0.2));
}
""",
        "status_gradient": "linear-gradient(90deg, #FF2E97, #FF6AC2, #00FFD1, #00C9A7, #FF2E97)",
        "divider_style": "linear-gradient(90deg, transparent, rgba(255,46,151,0.3), rgba(0,255,209,0.3), transparent)",
        "card_border": "1px solid rgba(0,255,200,0.06)",
        "stat_left_border_default": "2px solid #2A1F3D",
        "stat_left_border_accent":  "2px solid #FF6AC2",
        "stat_left_border_win":     "2px solid #00FFD1",
        "stat_box_shadow": "inset 0 1px 0 rgba(255,255,255,0.02), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(0,255,209,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 20px rgba(0,255,209,0.04)",
    },

    "digital_rain": {
        "label": "Digital Rain",
        "emoji": "🟢",
        "vars": {
            "bg":               "#030A03",
            "gold":             "#00C83C",
            "gold-bright":      "#33FF77",
            "gold-dim":         "#1B5E20",
            "gold-light":       "#66FF99",
            "win":              "#33FF77",
            "win-dark":         "#1B8A2E",
            "loss":             "#FF4444",
            "loss-dark":        "#B71C1C",
            "text-primary":     "#7AE89A",
            "text-sub":         "#4CAF6A",
            "text-muted":       "#1B5E20",
            "text-dim":         "#0D3D0D",
            "panel-bg":         "rgba(2, 16, 2, 0.85)",
            "panel-border":     "rgba(0, 200, 60, 0.08)",
            "panel-border-top": "rgba(0, 200, 60, 0.04)",
        },
        "overlays": ["scanlines", "vignette_matrix", "hud_matrix", "rim_matrix"],
        "hero_class": "hero-gradient-matrix",
        "extra_css": """
.hero-gradient-matrix {
    background: linear-gradient(135deg, #00C83C 0%, #33FF77 30%, #AAFFCC 50%, #33FF77 70%, #00C83C 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 0 16px rgba(0, 200, 60, 0.6));
}
""",
        "status_gradient": "linear-gradient(90deg, #021A02, #00C83C, #33FF77, #00C83C, #021A02)",
        "divider_style": "linear-gradient(90deg, transparent, #00C83C, transparent)",
        "card_border": "1px solid rgba(0, 200, 60, 0.15)",
        "stat_left_border_default": "2px solid #0A3A0A",
        "stat_left_border_accent":  "2px solid #00C83C",
        "stat_left_border_win":     "2px solid #33FF77",
        "stat_box_shadow": "inset 0 1px 0 rgba(0, 200, 60, 0.04), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(51, 255, 119, 0.06), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(51, 255, 119, 0.08)",
    },
}


# ── Overlay HTML fragments ────────────────────────────────────────────────────

OVERLAYS = {
    "scanlines": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(255,255,255,0.008) 2px,rgba(255,255,255,0.008) 4px);"></div>',

    "vignette_warm": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;background:radial-gradient(ellipse at 50% 30%,transparent 40%,rgba(0,0,0,0.35) 100%);"></div>',

    "vignette_cool": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;background:radial-gradient(ellipse at 50% 30%,transparent 35%,rgba(0,0,0,0.4) 100%);"></div>',

    "hud_gold": """<div style="position:absolute;top:8px;left:8px;width:12px;height:12px;border-top:1.5px solid rgba(226,192,92,0.25);border-left:1.5px solid rgba(226,192,92,0.25);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;top:8px;right:8px;width:12px;height:12px;border-top:1.5px solid rgba(226,192,92,0.25);border-right:1.5px solid rgba(226,192,92,0.25);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:8px;left:8px;width:12px;height:12px;border-bottom:1.5px solid rgba(226,192,92,0.12);border-left:1.5px solid rgba(226,192,92,0.12);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:8px;right:8px;width:12px;height:12px;border-bottom:1.5px solid rgba(226,192,92,0.12);border-right:1.5px solid rgba(226,192,92,0.12);z-index:3;pointer-events:none;"></div>""",

    "hud_vice": """<div style="position:absolute;top:8px;left:8px;width:12px;height:12px;border-top:1.5px solid rgba(0,255,209,0.2);border-left:1.5px solid rgba(0,255,209,0.2);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;top:8px;right:8px;width:12px;height:12px;border-top:1.5px solid rgba(255,46,151,0.2);border-right:1.5px solid rgba(255,46,151,0.2);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:8px;left:8px;width:12px;height:12px;border-bottom:1.5px solid rgba(255,46,151,0.1);border-left:1.5px solid rgba(255,46,151,0.1);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:8px;right:8px;width:12px;height:12px;border-bottom:1.5px solid rgba(0,255,209,0.1);border-right:1.5px solid rgba(0,255,209,0.1);z-index:3;pointer-events:none;"></div>""",

    "rim_gold": '<div style="position:absolute;top:4px;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,rgba(245,223,160,0.15),transparent);z-index:3;pointer-events:none;"></div>',

    "rim_vice": '<div style="position:absolute;top:4px;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,255,209,0.12),rgba(255,46,151,0.12),transparent);z-index:3;pointer-events:none;"></div>',

    "vignette_matrix": """<div style="position:absolute;inset:0;pointer-events:none;
    background:radial-gradient(ellipse at center, transparent 50%, rgba(1, 4, 1, 0.7) 100%);
    z-index:4;"></div>""",

    "hud_matrix": """<div style="position:absolute;inset:12px;pointer-events:none;z-index:6;opacity:0.2;
    background:
        linear-gradient(#00C83C,#00C83C) 0 0/12px 1px no-repeat,
        linear-gradient(#00C83C,#00C83C) 0 0/1px 12px no-repeat,
        linear-gradient(#00C83C,#00C83C) 100% 0/12px 1px no-repeat,
        linear-gradient(#00C83C,#00C83C) 100% 0/1px 12px no-repeat,
        linear-gradient(#00C83C,#00C83C) 0 100%/12px 1px no-repeat,
        linear-gradient(#00C83C,#00C83C) 0 100%/1px 12px no-repeat,
        linear-gradient(#00C83C,#00C83C) 100% 100%/12px 1px no-repeat,
        linear-gradient(#00C83C,#00C83C) 100% 100%/1px 12px no-repeat;
"></div>""",

    "rim_matrix": """<div style="height:1px;width:100%;
    background:linear-gradient(90deg, transparent, rgba(0, 200, 60, 0.5), transparent);
"></div>""",
}


def get_theme(theme_id: str | None = None) -> dict:
    """Return theme dict, falling back to default if invalid."""
    if theme_id and theme_id in THEMES:
        return THEMES[theme_id]
    return THEMES[DEFAULT_THEME]


def get_overlay_html(overlay_keys: list[str]) -> str:
    """Return combined HTML for a list of overlay keys."""
    return "\n".join(OVERLAYS.get(k, "") for k in overlay_keys)
