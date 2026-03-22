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

    # ═══════════════════════════════════════════════════════════════
    # New themes — v6.10.0
    # ═══════════════════════════════════════════════════════════════

    "midnight_circuit": {
        "label": "Midnight Circuit",
        "emoji": "🔌",
        "vars": {
            "bg":               "#060A14",
            "gold":             "#4A9EFF",
            "gold-bright":      "#8AC4FF",
            "gold-dim":         "#1E4A80",
            "gold-light":       "#C0E0FF",
            "win":              "#4AE88A",
            "win-dark":         "#1E7A44",
            "loss":             "#FF6A7A",
            "loss-dark":        "#A02A3A",
            "text-primary":     "#D8E4F4",
            "text-sub":         "#6080A0",
            "text-muted":       "#3A5478",
            "text-dim":         "#1E3048",
            "panel-bg":         "rgba(74,158,255,0.04)",
            "panel-border":     "rgba(74,158,255,0.10)",
            "panel-border-top": "rgba(74,158,255,0.12)",
        },
        "overlays": ["scanlines", "hexgrid", "hud_accent", "rim_accent"],
        "hero_class": "hero-gradient-circuit",
        "extra_css": """
.hero-gradient-circuit {
    background: linear-gradient(135deg, #4A9EFF, #8AC4FF 35%, #C0E0FF 50%, #8AC4FF 65%, #4A9EFF);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 0 20px rgba(74,158,255,0.3));
}
""",
        "status_gradient": "linear-gradient(90deg, #1E4A80, #4A9EFF, #8AC4FF, #4A9EFF, #1E4A80)",
        "divider_style": "linear-gradient(90deg, transparent, #1E4A80, transparent)",
        "card_border": "1px solid rgba(74,158,255,0.10)",
        "stat_left_border_default": "2px solid #1E4A80",
        "stat_left_border_accent":  "2px solid #4A9EFF",
        "stat_left_border_win":     "2px solid #4AE88A",
        "stat_box_shadow": "inset 0 1px 0 rgba(74,158,255,0.04), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(74,232,138,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(74,232,138,0.04)",
    },

    "venom_strike": {
        "label": "Venom Strike",
        "emoji": "🐍",
        "vars": {
            "bg":               "#06080A",
            "gold":             "#A0FF2E",
            "gold-bright":      "#C8FF70",
            "gold-dim":         "#4A7A14",
            "gold-light":       "#E0FFAA",
            "win":              "#2EE8D4",
            "win-dark":         "#14806E",
            "loss":             "#FF4A6A",
            "loss-dark":        "#A01A34",
            "text-primary":     "#D8E8D0",
            "text-sub":         "#6A8A5A",
            "text-muted":       "#3A5E2A",
            "text-dim":         "#1E3418",
            "panel-bg":         "rgba(160,255,46,0.03)",
            "panel-border":     "rgba(160,255,46,0.08)",
            "panel-border-top": "rgba(160,255,46,0.10)",
        },
        "overlays": ["crt_lines", "rim_accent"],
        "hero_class": "hero-glow-venom",
        "extra_css": """
.hero-glow-venom {
    color: #C8FF70;
    text-shadow: 0 0 30px rgba(160,255,46,0.5), 0 0 60px rgba(160,255,46,0.25), 0 2px 4px rgba(0,0,0,0.5);
}
""",
        "status_gradient": "linear-gradient(90deg, #4A7A14, #A0FF2E, #C8FF70, #A0FF2E, #4A7A14)",
        "divider_style": "linear-gradient(90deg, #A0FF2E, #4A7A14 30%, transparent)",
        "card_border": "1px solid rgba(160,255,46,0.08)",
        "stat_left_border_default": "2px solid #1E3418",
        "stat_left_border_accent":  "2px solid #A0FF2E",
        "stat_left_border_win":     "2px solid #2EE8D4",
        "stat_box_shadow": "inset 0 1px 0 rgba(160,255,46,0.03), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(46,232,212,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(46,232,212,0.04)",
    },

    "arctic_fox": {
        "label": "Arctic Fox",
        "emoji": "🦊",
        "vars": {
            "bg":               "#0A0C10",
            "gold":             "#FF8844",
            "gold-bright":      "#FFAA70",
            "gold-dim":         "#804422",
            "gold-light":       "#FFD0AA",
            "win":              "#4AD4A8",
            "win-dark":         "#1E7A5A",
            "loss":             "#A078E8",
            "loss-dark":        "#5A3A8A",
            "text-primary":     "#E8ECF0",
            "text-sub":         "#8090A0",
            "text-muted":       "#5A6878",
            "text-dim":         "#2E3A48",
            "panel-bg":         "rgba(255,136,68,0.03)",
            "panel-border":     "rgba(255,136,68,0.08)",
            "panel-border-top": "rgba(255,136,68,0.10)",
        },
        "overlays": ["vignette_cool", "rim_accent"],
        "hero_class": "hero-gradient-fox",
        "extra_css": """
.hero-gradient-fox {
    background: linear-gradient(180deg, #FFD0AA, #FFAA70 40%, #FF8844 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 2px 0 rgba(0,0,0,0.5));
}
""",
        "status_gradient": "linear-gradient(90deg, #804422, #FF8844, #FFAA70, #FF8844, #804422)",
        "divider_style": "linear-gradient(90deg, transparent, #804422, transparent)",
        "card_border": "1px solid rgba(255,136,68,0.08)",
        "stat_left_border_default": "2px solid #804422",
        "stat_left_border_accent":  "2px solid #FF8844",
        "stat_left_border_win":     "2px solid #4AD4A8",
        "stat_box_shadow": "inset 0 1px 0 rgba(255,136,68,0.03), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(74,212,168,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(74,212,168,0.04)",
    },

    "shadow_broker": {
        "label": "Shadow Broker",
        "emoji": "🕶️",
        "vars": {
            "bg":               "#08090C",
            "gold":             "#A8B8C8",
            "gold-bright":      "#C8D8E8",
            "gold-dim":         "#5A6878",
            "gold-light":       "#E0E8F0",
            "win":              "#5ACA8A",
            "win-dark":         "#2A7A4A",
            "loss":             "#E87A5A",
            "loss-dark":        "#8A3A22",
            "text-primary":     "#D0D8E0",
            "text-sub":         "#7A8494",
            "text-muted":       "#4E5868",
            "text-dim":         "#2A3040",
            "panel-bg":         "rgba(168,184,200,0.04)",
            "panel-border":     "rgba(168,184,200,0.08)",
            "panel-border-top": "rgba(168,184,200,0.10)",
        },
        "overlays": ["scanlines", "hud_accent", "rim_accent"],
        "hero_class": "hero-stamp-broker",
        "extra_css": """
.hero-stamp-broker {
    color: #C8D8E8;
    text-shadow: 2px 2px 0 #5A6878, 0 0 20px rgba(200,216,232,0.2);
}
""",
        "status_gradient": "linear-gradient(90deg, #5A6878, #A8B8C8, #C8D8E8, #A8B8C8, #5A6878)",
        "divider_style": "linear-gradient(90deg, transparent, #5A6878, transparent)",
        "card_border": "1px solid rgba(168,184,200,0.08)",
        "stat_left_border_default": "2px solid #5A6878",
        "stat_left_border_accent":  "2px solid #A8B8C8",
        "stat_left_border_win":     "2px solid #5ACA8A",
        "stat_box_shadow": "inset 0 1px 0 rgba(168,184,200,0.03), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(90,202,138,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(90,202,138,0.04)",
    },

    "glacier_mint": {
        "label": "Glacier Mint",
        "emoji": "🍃",
        "vars": {
            "bg":               "#060C0A",
            "gold":             "#4AE8B8",
            "gold-bright":      "#60E8C0",
            "gold-dim":         "#1E7A5A",
            "gold-light":       "#80E8C8",
            "win":              "#7AB8FF",
            "win-dark":         "#3A5E8A",
            "loss":             "#E85A7A",
            "loss-dark":        "#8A2240",
            "text-primary":     "#D8EEE8",
            "text-sub":         "#6A9488",
            "text-muted":       "#3E6858",
            "text-dim":         "#1E3A30",
            "panel-bg":         "rgba(74,232,184,0.03)",
            "panel-border":     "rgba(74,232,184,0.08)",
            "panel-border-top": "rgba(74,232,184,0.10)",
        },
        "overlays": ["hexgrid", "vignette_cool", "rim_accent"],
        "hero_class": "hero-gradient-mint",
        "extra_css": """
.hero-gradient-mint {
    background: linear-gradient(180deg, #80E8C8, #60E8C0 40%, #4AE8B8 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 2px 0 rgba(0,0,0,0.5));
}
""",
        "status_gradient": "linear-gradient(90deg, #1E7A5A, #4AE8B8, #60E8C0, #4AE8B8, #1E7A5A)",
        "divider_style": "linear-gradient(90deg, transparent, #1E7A5A, transparent)",
        "card_border": "1px solid rgba(74,232,184,0.08)",
        "stat_left_border_default": "2px solid #1E7A5A",
        "stat_left_border_accent":  "2px solid #4AE8B8",
        "stat_left_border_win":     "2px solid #7AB8FF",
        "stat_box_shadow": "inset 0 1px 0 rgba(74,232,184,0.03), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(122,184,255,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(122,184,255,0.04)",
    },

    "blackout_protocol": {
        "label": "Blackout Protocol",
        "emoji": "🔒",
        "vars": {
            "bg":               "#060606",
            "gold":             "#FF1A1A",
            "gold-bright":      "#FF5A5A",
            "gold-dim":         "#6E0A0A",
            "gold-light":       "#FF9A9A",
            "win":              "#1AE87A",
            "win-dark":         "#0A7A3A",
            "loss":             "#5A8AFF",
            "loss-dark":        "#2A4480",
            "text-primary":     "#D0CCCC",
            "text-sub":         "#787474",
            "text-muted":       "#4E4A4A",
            "text-dim":         "#2A2828",
            "panel-bg":         "rgba(255,26,26,0.03)",
            "panel-border":     "rgba(255,26,26,0.08)",
            "panel-border-top": "rgba(255,26,26,0.10)",
        },
        "overlays": ["crt_lines", "scanlines", "vignette_heavy", "hud_accent", "rim_accent"],
        "hero_class": "hero-glow-blackout",
        "extra_css": """
.hero-glow-blackout {
    color: #FF5A5A;
    text-shadow: 0 0 30px rgba(255,26,26,0.5), 0 0 60px rgba(255,26,26,0.25), 0 2px 4px rgba(0,0,0,0.5);
}
""",
        "status_gradient": "linear-gradient(90deg, #6E0A0A, #FF1A1A, #FF5A5A, #FF1A1A, #6E0A0A)",
        "divider_style": "radial-gradient(ellipse at center, #6E0A0A, transparent 70%)",
        "card_border": "1px solid rgba(255,26,26,0.08)",
        "stat_left_border_default": "2px solid #6E0A0A",
        "stat_left_border_accent":  "2px solid #FF1A1A",
        "stat_left_border_win":     "2px solid #1AE87A",
        "stat_box_shadow": "inset 0 1px 0 rgba(255,26,26,0.03), 0 2px 8px rgba(0,0,0,0.3)",
        "stat_box_shadow_win": "inset 0 1px 0 rgba(26,232,122,0.04), 0 2px 8px rgba(0,0,0,0.3), 0 0 16px rgba(26,232,122,0.04)",
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

    # ── Generic overlays (use CSS variables — any theme can reference these) ──

    "vignette_standard": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;background:radial-gradient(ellipse at 50% 30%,transparent 40%,rgba(0,0,0,0.35) 100%);"></div>',

    "vignette_heavy": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;background:radial-gradient(ellipse at 50% 30%,transparent 30%,rgba(0,0,0,0.5) 100%);"></div>',

    "hud_accent": """<div style="position:absolute;top:8px;left:8px;width:12px;height:12px;border-top:1.5px solid color-mix(in srgb, var(--gold) 25%, transparent);border-left:1.5px solid color-mix(in srgb, var(--gold) 25%, transparent);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;top:8px;right:8px;width:12px;height:12px;border-top:1.5px solid color-mix(in srgb, var(--gold) 25%, transparent);border-right:1.5px solid color-mix(in srgb, var(--gold) 25%, transparent);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:8px;left:8px;width:12px;height:12px;border-bottom:1.5px solid color-mix(in srgb, var(--gold) 12%, transparent);border-left:1.5px solid color-mix(in srgb, var(--gold) 12%, transparent);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:8px;right:8px;width:12px;height:12px;border-bottom:1.5px solid color-mix(in srgb, var(--gold) 12%, transparent);border-right:1.5px solid color-mix(in srgb, var(--gold) 12%, transparent);z-index:3;pointer-events:none;"></div>""",

    "crosshair_accent": """<div style="position:absolute;top:50%;left:12px;width:8px;height:1px;background:color-mix(in srgb, var(--gold) 20%, transparent);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;top:50%;right:12px;width:8px;height:1px;background:color-mix(in srgb, var(--gold) 20%, transparent);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;top:12px;left:50%;width:1px;height:8px;background:color-mix(in srgb, var(--gold) 15%, transparent);z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:12px;left:50%;width:1px;height:8px;background:color-mix(in srgb, var(--gold) 15%, transparent);z-index:3;pointer-events:none;"></div>""",

    "deco_accent": """<div style="position:absolute;top:6px;left:6px;width:20px;height:20px;border-top:2px solid color-mix(in srgb, var(--gold) 20%, transparent);border-left:2px solid color-mix(in srgb, var(--gold) 20%, transparent);border-top-left-radius:4px;z-index:3;pointer-events:none;"></div>
<div style="position:absolute;top:6px;right:6px;width:20px;height:20px;border-top:2px solid color-mix(in srgb, var(--gold) 20%, transparent);border-right:2px solid color-mix(in srgb, var(--gold) 20%, transparent);border-top-right-radius:4px;z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:6px;left:6px;width:20px;height:20px;border-bottom:2px solid color-mix(in srgb, var(--gold) 10%, transparent);border-left:2px solid color-mix(in srgb, var(--gold) 10%, transparent);border-bottom-left-radius:4px;z-index:3;pointer-events:none;"></div>
<div style="position:absolute;bottom:6px;right:6px;width:20px;height:20px;border-bottom:2px solid color-mix(in srgb, var(--gold) 10%, transparent);border-right:2px solid color-mix(in srgb, var(--gold) 10%, transparent);border-bottom-right-radius:4px;z-index:3;pointer-events:none;"></div>""",

    "rim_accent": '<div style="position:absolute;top:4px;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,color-mix(in srgb, var(--gold-bright) 15%, transparent),transparent);z-index:3;pointer-events:none;"></div>',

    "hexgrid": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;opacity:0.03;background-image:url(\'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="28" height="49"><path d="M14 0l14 24.5L14 49 0 24.5z" fill="none" stroke="white" stroke-width="0.5"/></svg>\');background-size:28px 49px;"></div>',

    "sonar": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;opacity:0.04;background:repeating-radial-gradient(circle at 50% 60%,transparent,transparent 40px,rgba(255,255,255,0.03) 41px,transparent 42px);"></div>',

    "starfield": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;opacity:0.06;background-image:radial-gradient(1px 1px at 10% 20%,white,transparent),radial-gradient(1px 1px at 30% 70%,white,transparent),radial-gradient(1px 1px at 60% 15%,white,transparent),radial-gradient(1px 1px at 80% 55%,white,transparent),radial-gradient(1px 1px at 45% 85%,white,transparent),radial-gradient(1px 1px at 90% 40%,white,transparent);"></div>',

    "crt_lines": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,0,0,0.15) 3px,rgba(0,0,0,0.15) 4px);opacity:0.4;"></div>',

    "heavy_grain": '<div style="position:absolute;inset:0;z-index:1;pointer-events:none;opacity:0.08;background-image:url(\'data:image/svg+xml,<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg"><filter id="g"><feTurbulence type="fractalNoise" baseFrequency="0.65" numOctaves="3" stitchTiles="stitch"/></filter><rect width="100%25" height="100%25" filter="url(%23g)"/></svg>\');"></div>',
}


def get_theme(theme_id: str | None = None) -> dict:
    """Return theme dict, falling back to default if invalid."""
    if theme_id and theme_id in THEMES:
        return THEMES[theme_id]
    return THEMES[DEFAULT_THEME]


def get_overlay_html(overlay_keys: list[str]) -> str:
    """Return combined HTML for a list of overlay keys."""
    return "\n".join(OVERLAYS.get(k, "") for k in overlay_keys)
