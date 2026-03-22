"""
casino_html_renderer.py — ATLAS FLOW Casino · Unified HTML Card Renderer
─────────────────────────────────────────────────────────────────────────────
Playwright HTML → PNG renderer for all casino game cards.
V6 design language: dark bg, gold accents, Outfit + JetBrains Mono,
noise texture, glass-morphism cells.

Uses the unified render engine from atlas_html_engine.py.

Usage:
    from casino.renderer.casino_html_renderer import render_blackjack_card
    png_bytes = await render_blackjack_card(data)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math
from typing import Optional

from atlas_style_tokens import Tokens
from atlas_html_engine import (
    render_card,
    wrap_card,
    esc,
    card_img_src,
    card_back_src,
    slot_icon_src,
    icon_pill,
    SLOT_ICON_CONFIG,
    build_header_html,
    build_data_grid_html,
    build_footer_html,
    build_streak_badge_html,
    build_near_miss_html,
    build_jackpot_footer_html,
)


# ══════════════════════════════════════════════════════════════════════════════
#  BLACKJACK RENDERER — v6 Photorealistic CSS Cards
# ══════════════════════════════════════════════════════════════════════════════

# Suit class mapping: red suits vs black suits
_BJ_SUIT_CLASS = {"♠": "s-b", "♥": "s-r", "♦": "s-r", "♣": "s-b"}


def _bj_card_html(rank: str, suit: str) -> str:
    """Generate a single CSS-rendered playing card (full-size, 108×152px)."""
    sc = _BJ_SUIT_CLASS.get(suit, "s-b")
    r = esc(rank)
    s = esc(suit)
    return (
        f'<div class="pc">'
        f'<div class="c-corner {sc}"><span class="c-rank">{r}</span>'
        f'<span class="c-suit">{s}</span></div>'
        f'<span class="suit-c {sc}">{s}</span>'
        f'<div class="c-corner-b {sc}"><span class="c-rank">{r}</span>'
        f'<span class="c-suit">{s}</span></div>'
        f'</div>'
    )


def _bj_card_back_html() -> str:
    """CSS-rendered card back (hidden dealer card)."""
    return (
        '<div class="pc card-back">'
        '<div class="back-pattern"></div>'
        '</div>'
    )


def _bj_mini_card_html(rank: str, suit: str) -> str:
    """Generate a mini CSS card for the cinematic result screen (67×93px)."""
    sc = _BJ_SUIT_CLASS.get(suit, "s-b")
    r = esc(rank)
    s = esc(suit)
    return (
        f'<div class="mc">'
        f'<div class="c-corner {sc}"><span class="c-rank">{r}</span>'
        f'<span class="c-suit">{s}</span></div>'
        f'<span class="suit-c {sc}">{s}</span>'
        f'</div>'
    )


def _bj_hand_html(hand: list[tuple[str, str]], hide_second: bool = False) -> str:
    """Build a fanned row of CSS playing cards."""
    parts = []
    for i, (value, suit) in enumerate(hand):
        if hide_second and i == 1:
            parts.append(_bj_card_back_html())
        else:
            parts.append(_bj_card_html(value, suit))
    return "\n".join(parts)


def _bj_mini_hand_html(hand: list[tuple[str, str]]) -> str:
    """Build a fanned row of mini CSS cards for result screen."""
    return "\n".join(_bj_mini_card_html(v, s) for v, s in hand)


def _bj_outcome(status: str) -> tuple[str, str, str]:
    """Parse status string into (outcome, badge_text, status_description).

    Returns:
        outcome: "win" | "loss" | "blackjack" | "push" | "active"
        badge_text: Display text for the badge pill
        status_description: Human-readable description for banner
    """
    if not status:
        return "active", "IN PLAY", ""
    s = status.lower()
    if "blackjack" in s:
        return "blackjack", "BJ!", status
    if "win" in s or ("bust" in s and "dealer" in s):
        return "win", "WIN", status
    if "bust" in s or "loss" in s:
        return "loss", "BUST", status
    if "push" in s:
        return "push", "PUSH", status
    return "active", "IN PLAY", status


def _bj_chip_html(amount: int, outcome: str) -> str:
    """Generate a footer chip element next to wager."""
    chip_class = {
        "win": "chip-g", "blackjack": "chip-au",
        "loss": "chip-r", "active": "chip-k", "push": "chip-k",
    }.get(outcome, "chip-k")
    return f'<div class="bj-chip {chip_class}">$</div>'


def _bj_shoe_html(shoe_remaining: int | None, shoe_total: int = 312) -> str:
    """Generate shoe indicator for header."""
    if shoe_remaining is None:
        return ""
    pct = shoe_remaining / shoe_total
    low_class = " shoe-low" if pct < 0.3 else ""
    return (
        f'<div class="shoe-indicator{low_class}">'
        f'<span class="shoe-lbl">SHOE</span>'
        f'<span class="shoe-val">{shoe_remaining}/{shoe_total}</span>'
        f'</div>'
    )


# ── Blackjack v6 CSS (700px) ───────────────────────────────────────────────

_BLACKJACK_CSS = """<style>
/* ═══ BLACKJACK v6 — Photorealistic Card Design ═══ */

/* Blackjack-specific custom properties */
.card {
  --bj-bg-table: #090a0d;
  --bj-bg-footer: #08090b;
  --bj-text-white: #fafafa;
  --bj-text-ghost: #7c7d86;
  --bj-border-hair: rgba(255,255,255,0.04);
  --bj-border-glass: rgba(255,255,255,0.06);
  --bj-gold-hot: #ffe066;
  --bj-bj-gold: #ffd700;
}

/* ═══ HEADER ═══ */
.bj-header {
  padding: 17px 26px 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--bj-border-hair);
  position: relative;
}
.bj-header::before {
  content: '';
  position: absolute;
  top: 0; left: 8%; right: 8%; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(240,201,64,0.12), transparent);
}
.bj-h-left { display: flex; align-items: center; gap: 13px; }
.bj-h-icon {
  width: 40px; height: 40px;
  border-radius: 7px;
  object-fit: cover;
  border: 1px solid rgba(240,201,64,0.12);
  box-shadow: 0 2px 8px rgba(0,0,0,0.7), 0 0 10px rgba(240,201,64,0.06);
}
.bj-h-title {
  font-weight: 900;
  font-size: 15px;
  letter-spacing: 0.24em;
  text-transform: uppercase;
  background: linear-gradient(135deg, #FFDF73 0%, #D4AF37 40%, #FFF3A1 50%, #B8860B 60%, #FFDF73 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  filter: drop-shadow(0px 2px 8px rgba(255, 215, 0, 0.3));
}
.bj-h-sub {
  font-size: 11px;
  font-weight: 500;
  color: var(--bj-text-ghost);
  letter-spacing: 0.03em;
}
.bj-h-right { text-align: right; }
.bj-h-player {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
}
.bj-h-season {
  font-size: 11px;
  font-weight: 500;
  color: var(--bj-text-ghost);
}

/* ═══ SHOE INDICATOR ═══ */
.shoe-indicator {
  display: flex; align-items: center; gap: 5px;
  margin-top: 3px;
}
.shoe-lbl {
  font-size: 9px; font-weight: 700;
  letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--bj-text-ghost);
}
.shoe-val {
  font-family: var(--font-mono), monospace;
  font-size: 10px; font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--bj-text-ghost);
}
.shoe-low .shoe-val { color: var(--gold); }
.shoe-low .shoe-lbl { color: var(--gold); }

/* ═══ RESULT BANNER ═══ */
.bj-banner {
  padding: 16px 26px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: relative;
  overflow: hidden;
}
.bj-banner::before {
  content: '';
  position: absolute;
  inset: 0;
}
.bj-banner.bj-ban-win::before {
  background:
    radial-gradient(ellipse 50% 200% at 0% 50%, rgba(0,230,118,0.18) 0%, transparent 60%),
    radial-gradient(ellipse 30% 150% at 100% 50%, rgba(0,230,118,0.06) 0%, transparent 50%);
}
.bj-banner.bj-ban-win { border-bottom: 1px solid rgba(0,230,118,0.12); }

.bj-banner.bj-ban-loss::before {
  background:
    radial-gradient(ellipse 50% 200% at 0% 50%, rgba(255,51,82,0.18) 0%, transparent 60%),
    radial-gradient(ellipse 30% 150% at 100% 50%, rgba(255,51,82,0.06) 0%, transparent 50%);
}
.bj-banner.bj-ban-loss { border-bottom: 1px solid rgba(255,51,82,0.12); }

.bj-banner.bj-ban-bj::before {
  background:
    radial-gradient(ellipse 60% 200% at 50% 50%, rgba(255,215,0,0.2) 0%, transparent 55%),
    radial-gradient(ellipse 30% 100% at 0% 50%, rgba(255,215,0,0.08) 0%, transparent 40%);
}
.bj-banner.bj-ban-bj { border-bottom: 1px solid rgba(255,215,0,0.15); }

.bj-banner.bj-ban-push { border-bottom: 1px solid var(--bj-border-hair); }

.bj-b-left { display: flex; align-items: center; gap: 12px; position: relative; z-index: 1; }

.bj-pill {
  padding: 4px 14px;
  border-radius: 4px;
  font-weight: 800;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.bj-pill.bj-p-win { background: rgba(0,230,118,0.14); border: 1px solid rgba(0,230,118,0.3); color: var(--win); box-shadow: 0 0 8px rgba(0,230,118,0.12); }
.bj-pill.bj-p-loss { background: rgba(255,51,82,0.14); border: 1px solid rgba(255,51,82,0.3); color: var(--loss); box-shadow: 0 0 8px rgba(255,51,82,0.12); }
.bj-pill.bj-p-bj { background: rgba(255,215,0,0.16); border: 1px solid rgba(255,215,0,0.35); color: var(--bj-bj-gold); box-shadow: 0 0 12px rgba(255,215,0,0.15); }
.bj-pill.bj-p-push { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); color: var(--bj-text-ghost); }

.bj-b-text {
  font-weight: 700;
  font-size: 16px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  position: relative; z-index: 1;
}
.bj-b-text.bj-t-win { color: var(--win); }
.bj-b-text.bj-t-loss { color: var(--loss); }
.bj-b-text.bj-t-bj { color: var(--bj-bj-gold); text-shadow: 0 0 14px rgba(255,215,0,0.3); }
.bj-b-text.bj-t-push { color: var(--bj-text-ghost); }

.bj-b-pnl {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 28px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  position: relative; z-index: 1;
}
.bj-b-pnl.bj-pnl-pos { color: var(--win); text-shadow: 0 0 18px rgba(0,230,118,0.35); mix-blend-mode: screen; }
.bj-b-pnl.bj-pnl-neg { color: var(--loss); text-shadow: 0 0 18px rgba(255,51,82,0.35); }
.bj-b-pnl.bj-pnl-bj { color: var(--bj-bj-gold); text-shadow: 0 0 22px rgba(255,215,0,0.4); }

/* ═══ TABLE — 3-Layer Playing Surface ═══ */
.bj-table {
  padding: 26px;
  position: relative;
  background: var(--bj-bg-table);
}
/* L2: Heavy vignette — edges go DARK */
.bj-table::before {
  content: '';
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse 65% 60% at 50% 50%, transparent 20%, rgba(0,0,0,0.55) 100%);
  z-index: 0;
  pointer-events: none;
}
/* L3: Directional spotlight — table center */
.bj-table::after {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 45% 50% at 50% 45%, rgba(255,255,255,0.025) 0%, transparent 70%),
    radial-gradient(ellipse 30% 35% at 50% 45%, rgba(240,201,64,0.03) 0%, transparent 60%);
  z-index: 0;
  pointer-events: none;
}
/* Win spotlight */
.bj-table.bj-lit-win::after {
  background:
    radial-gradient(ellipse 45% 50% at 50% 45%, rgba(0,230,118,0.03) 0%, transparent 70%),
    radial-gradient(ellipse 30% 35% at 50% 45%, rgba(0,230,118,0.02) 0%, transparent 60%);
  mix-blend-mode: color-dodge;
}
/* Loss spotlight */
.bj-table.bj-lit-loss::after {
  background:
    radial-gradient(ellipse 45% 50% at 50% 45%, rgba(255,51,82,0.02) 0%, transparent 70%);
}

.bj-t-content {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* ─── HAND SECTION ─── */
.bj-hand { display: flex; flex-direction: column; gap: 12px; position: relative; }

.bj-hand-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.bj-hand-lbl {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--bj-text-ghost);
}
.bj-hand-val {
  font-family: var(--font-mono), monospace;
  font-size: 36px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  color: var(--bj-text-white);
  text-shadow: 0 2px 8px rgba(0,0,0,0.7);
}
.bj-hand-val.bj-bust {
  color: var(--loss);
  text-decoration: line-through;
  text-decoration-thickness: 3px;
  text-decoration-color: rgba(255,51,82,0.55);
  text-shadow: 0 0 14px rgba(255,51,82,0.25), 0 2px 8px rgba(0,0,0,0.7);
}

/* ─── Active hand glow ring ─── */
.bj-hand.bj-active {
  padding: 12px 14px;
  margin: -12px -14px;
  border-radius: 14px;
  position: relative;
}
.bj-hand.bj-active::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 14px;
  border: 1px solid rgba(240,201,64,0.1);
  background: radial-gradient(ellipse 90% 80% at 50% 55%, rgba(240,201,64,0.035) 0%, transparent 65%);
  pointer-events: none;
}

/* ─── CARDS ROW ─── */
.bj-cards {
  display: flex;
  padding: 8px 0 6px 6px;
}

/* ═══ PLAYING CARD — 108×152px (scaled from 74×104) ═══ */
.pc {
  width: 108px;
  height: 152px;
  border-radius: 11px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 9px 12px;
  position: relative;
  transform-origin: bottom center;
  flex-shrink: 0;
}
.pc:not(:first-child) { margin-left: -15px; }

/* Fan physics — imperfect, hand-held feel */
.bj-cards .pc:nth-child(1) { transform: rotate(-6deg) translateY(3px); z-index: 1; }
.bj-cards .pc:nth-child(2) { transform: rotate(-1deg) translateY(-1px); z-index: 2; }
.bj-cards .pc:nth-child(3) { transform: rotate(4deg) translateY(2px); z-index: 3; }
.bj-cards .pc:nth-child(4) { transform: rotate(8deg) translateY(5px); z-index: 4; }
.bj-cards .pc:nth-child(5) { transform: rotate(11deg) translateY(9px); z-index: 5; }

/* ─── CREAM CARDS ─── */
.cream .pc {
  background: linear-gradient(150deg, #faf5e8 0%, #f2ebd4 45%, #e6dcc2 100%);
  border: 1px solid rgba(160,150,125,0.3);
  box-shadow:
    0 1px 1px rgba(0,0,0,0.2),
    0 2px 4px rgba(0,0,0,0.25),
    0 6px 12px rgba(0,0,0,0.3),
    0 14px 28px rgba(0,0,0,0.25),
    0 20px 40px rgba(0,0,0,0.15);
}
/* Top-left directional sheen */
.cream .pc::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 10px;
  background: linear-gradient(130deg, rgba(255,255,255,0.6) 0%, rgba(255,255,255,0.15) 15%, transparent 40%);
  pointer-events: none;
  z-index: 3;
}
/* Air-cushion dimple texture */
.cream .pc::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 10px;
  background-image: repeating-linear-gradient(45deg, rgba(0,0,0,0.03) 0px, rgba(0,0,0,0.03) 1px, transparent 1px, transparent 2px),
                    repeating-linear-gradient(-45deg, rgba(0,0,0,0.03) 0px, rgba(0,0,0,0.03) 1px, transparent 1px, transparent 2px);
  pointer-events: none;
  z-index: 4;
}
.cream .s-r { color: #b8312a; }
.cream .s-b { color: #141418; }
.cream .suit-c.s-r { opacity: 0.35; }
.cream .suit-c.s-b { opacity: 0.25; }

/* Directional shadow physics — fixed overhead light */
.cream .bj-cards .pc:nth-child(1) { box-shadow: 3px 6px 12px rgba(0,0,0,0.35), 2px 14px 28px rgba(0,0,0,0.25), 1px 20px 40px rgba(0,0,0,0.15); }
.cream .bj-cards .pc:nth-child(2) { box-shadow: 1px 6px 12px rgba(0,0,0,0.3), 0px 14px 28px rgba(0,0,0,0.25), 0px 20px 40px rgba(0,0,0,0.15); }
.cream .bj-cards .pc:nth-child(3) { box-shadow: -2px 6px 12px rgba(0,0,0,0.35), -1px 14px 28px rgba(0,0,0,0.25), -1px 20px 40px rgba(0,0,0,0.15); }
.cream .bj-cards .pc:nth-child(4) { box-shadow: -4px 6px 14px rgba(0,0,0,0.4), -3px 14px 28px rgba(0,0,0,0.3), -2px 20px 40px rgba(0,0,0,0.15); }
.cream .bj-cards .pc:nth-child(5) { box-shadow: -5px 6px 16px rgba(0,0,0,0.45), -4px 14px 28px rgba(0,0,0,0.3), -3px 20px 40px rgba(0,0,0,0.18); }

/* ─── GLASS CARDS ─── */
.glass .pc {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  box-shadow:
    0 1px 1px rgba(0,0,0,0.15),
    0 2px 4px rgba(0,0,0,0.2),
    0 6px 12px rgba(0,0,0,0.25),
    0 14px 28px rgba(0,0,0,0.2),
    0 20px 40px rgba(0,0,0,0.1),
    inset 0 1px 0 rgba(255,255,255,0.07);
}
.glass .pc::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 10px;
  background: linear-gradient(130deg, rgba(255,255,255,0.12) 0%, transparent 25%);
  pointer-events: none;
  z-index: 3;
}
.glass .s-r { color: #ff5068; text-shadow: 0 0 8px rgba(255,80,104,0.4); }
.glass .s-b { color: #e4e4ec; text-shadow: 0 0 5px rgba(228,228,236,0.08); }
.glass .suit-c.s-r { opacity: 0.50; filter: drop-shadow(0 0 5px rgba(255,80,104,0.3)); }
.glass .suit-c.s-b { opacity: 0.35; }

/* Directional shadow physics — glass */
.glass .bj-cards .pc:nth-child(1) { box-shadow: 3px 6px 12px rgba(0,0,0,0.3), 2px 14px 28px rgba(0,0,0,0.2), 1px 20px 40px rgba(0,0,0,0.1), inset 0 1px 0 rgba(255,255,255,0.07); }
.glass .bj-cards .pc:nth-child(2) { box-shadow: 1px 6px 12px rgba(0,0,0,0.25), 0px 14px 28px rgba(0,0,0,0.2), 0px 20px 40px rgba(0,0,0,0.1), inset 0 1px 0 rgba(255,255,255,0.07); }
.glass .bj-cards .pc:nth-child(3) { box-shadow: -2px 6px 12px rgba(0,0,0,0.3), -1px 14px 28px rgba(0,0,0,0.2), -1px 20px 40px rgba(0,0,0,0.1), inset 0 1px 0 rgba(255,255,255,0.07); }
.glass .bj-cards .pc:nth-child(4) { box-shadow: -4px 6px 14px rgba(0,0,0,0.35), -3px 14px 28px rgba(0,0,0,0.25), -2px 20px 40px rgba(0,0,0,0.12), inset 0 1px 0 rgba(255,255,255,0.07); }
.glass .bj-cards .pc:nth-child(5) { box-shadow: -5px 6px 16px rgba(0,0,0,0.4), -4px 14px 28px rgba(0,0,0,0.25), -3px 20px 40px rgba(0,0,0,0.14), inset 0 1px 0 rgba(255,255,255,0.07); }

/* Ambient color bleed behind glass */
.glass .bj-table {
  background:
    radial-gradient(ellipse 40% 35% at 30% 40%, rgba(60,80,220,0.04) 0%, transparent 60%),
    radial-gradient(ellipse 35% 30% at 70% 60%, rgba(240,201,64,0.025) 0%, transparent 50%),
    var(--bj-bg-table);
}

/* ─── Card typography ─── */
.c-corner { display: flex; flex-direction: column; align-items: flex-start; line-height: 1; position: relative; z-index: 2; }
.c-corner-b { display: flex; flex-direction: column; align-items: flex-end; line-height: 1; transform: rotate(180deg); position: relative; z-index: 2; }
.c-rank {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 25px;
  line-height: 1;
}
.cream .c-rank { text-shadow: none; }
.glass .c-rank { text-shadow: 0 1px 3px rgba(0,0,0,0.5); }
.c-suit { font-size: 17px; line-height: 1; margin-top: -3px; }
.suit-c {
  position: absolute;
  top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  font-size: 52px;
  z-index: 1;
}

/* ─── CARD BACK ─── */
.card-back {
  background: linear-gradient(145deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%) !important;
  border: 1px solid rgba(255,255,255,0.08) !important;
  box-shadow:
    0 1px 1px rgba(0,0,0,0.2),
    0 2px 4px rgba(0,0,0,0.25),
    0 6px 12px rgba(0,0,0,0.3),
    0 14px 28px rgba(0,0,0,0.25),
    0 20px 40px rgba(0,0,0,0.15) !important;
}
.card-back::before {
  content: '' !important;
  position: absolute !important;
  inset: 6px !important;
  border-radius: 6px !important;
  border: 2px solid rgba(255,255,255,0.06) !important;
  background: repeating-linear-gradient(
    45deg,
    transparent, transparent 4px,
    rgba(255,255,255,0.02) 4px, rgba(255,255,255,0.02) 5px
  ) !important;
  z-index: 2 !important;
  pointer-events: none !important;
}
.card-back::after { display: none !important; }
.back-pattern {
  position: absolute;
  inset: 10px;
  border-radius: 4px;
  background: radial-gradient(ellipse at center, rgba(212,175,55,0.06) 0%, transparent 70%);
}

/* ─── TABLE DIVIDER ─── */
.bj-t-div {
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(240,201,64,0.08) 15%, rgba(240,201,64,0.08) 85%, transparent);
}

/* ═══ STATS FOOTER ═══ */
.bj-footer {
  padding: 19px 26px 22px;
  background: var(--bj-bg-footer);
  border-top: 1px solid var(--bj-border-hair);
  position: relative;
}
.bj-footer::after {
  content: '';
  position: absolute;
  bottom: 0; left: 12%; right: 12%; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(240,201,64,0.06), transparent);
}
.bj-f-row { display: flex; justify-content: space-between; }
.bj-f-stat { display: flex; flex-direction: column; gap: 5px; }
.bj-f-stat:first-child { align-items: flex-start; }
.bj-f-stat:nth-child(2), .bj-f-stat:nth-child(3) { align-items: center; }
.bj-f-stat:last-child { align-items: flex-end; }
.bj-f-lbl {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--bj-text-ghost);
}
.bj-f-val {
  font-family: var(--font-mono), monospace;
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--text-dim);
  letter-spacing: -0.02em;
}
.bj-f-val.bj-fv-pos { color: var(--win); text-shadow: 0 0 10px rgba(0,230,118,0.2); }
.bj-f-val.bj-fv-neg { color: var(--loss); text-shadow: 0 0 10px rgba(255,51,82,0.2); }
.bj-f-val.bj-fv-bal { color: var(--gold); text-shadow: 0 0 14px rgba(240,201,64,0.25); }

/* Chip next to wager */
.bj-chip-row { display: flex; align-items: center; gap: 7px; }
.bj-chip {
  width: 28px; height: 28px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono), monospace;
  font-size: 9px; font-weight: 800;
  position: relative;
  box-shadow: 0 2px 4px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.15), inset 0 -1px 0 rgba(0,0,0,0.35);
}
.bj-chip::before { content: ''; position: absolute; inset: 4px; border-radius: 50%; border: 1.5px dashed rgba(255,255,255,0.3); }
.chip-g { background: linear-gradient(145deg, #1a9e4a, #0d7a35); color: #c0ffd8; }
.chip-r { background: linear-gradient(145deg, #c0392b, #8e2218); color: #ffc8c8; }
.chip-au { background: linear-gradient(145deg, #d4a820, #a88518); color: #fff8dc; }
.chip-k { background: linear-gradient(145deg, #3a3a3a, #1a1a1a); color: #bbb; }

/* ═══════════════════════════════════════════════════════
   CINEMATIC RESULT SCREEN
   ═══════════════════════════════════════════════════════ */
.bj-result {
  position: relative;
  overflow: hidden;
}
/* Grain */
.bj-result::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.75' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");
  background-size: 100px;
  pointer-events: none;
  z-index: 1;
}
/* Hard vignette */
.bj-result::after {
  content: '';
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse 70% 65% at 50% 40%, transparent 25%, rgba(0,0,0,0.6) 100%);
  pointer-events: none;
  z-index: 2;
}
.bj-result.bj-res-win {
  background:
    radial-gradient(ellipse 55% 40% at 50% 20%, rgba(0,230,118,0.18) 0%, transparent 55%),
    radial-gradient(ellipse 80% 50% at 50% 90%, rgba(0,0,0,0.6) 0%, transparent 50%),
    var(--bg, #0c0d10);
}
.bj-result.bj-res-loss {
  background:
    radial-gradient(ellipse 55% 40% at 50% 20%, rgba(255,51,82,0.14) 0%, transparent 55%),
    radial-gradient(ellipse 80% 50% at 50% 90%, rgba(0,0,0,0.6) 0%, transparent 50%),
    var(--bg, #0c0d10);
}
.bj-result.bj-res-bj {
  background:
    radial-gradient(ellipse 50% 35% at 50% 18%, rgba(255,215,0,0.22) 0%, transparent 50%),
    radial-gradient(ellipse 70% 30% at 50% 85%, rgba(255,215,0,0.06) 0%, transparent 50%),
    radial-gradient(ellipse 80% 50% at 50% 90%, rgba(0,0,0,0.6) 0%, transparent 50%),
    var(--bg, #0c0d10);
}

.bj-r-inner {
  position: relative;
  z-index: 3;
  padding: 34px 28px 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
}
.bj-r-sub {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-dim);
  letter-spacing: 0.04em;
}
.bj-r-big {
  font-weight: 900;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  line-height: 1;
}
.bj-r-big.bj-rb-win {
  font-size: 64px;
  color: var(--win);
  text-shadow: 0 0 30px rgba(0,230,118,0.5), 0 0 60px rgba(0,230,118,0.2), 0 0 100px rgba(0,230,118,0.08), 0 3px 6px rgba(0,0,0,0.7);
}
.bj-r-big.bj-rb-loss {
  font-size: 70px;
  color: var(--loss);
  text-shadow: 0 0 30px rgba(255,51,82,0.45), 0 0 60px rgba(255,51,82,0.15), 0 3px 6px rgba(0,0,0,0.7);
}
.bj-r-big.bj-rb-bj {
  font-size: 54px;
  background: linear-gradient(135deg, #FFDF73 0%, #D4AF37 40%, #FFF3A1 50%, #B8860B 60%, #FFDF73 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  filter: drop-shadow(0px 2px 8px rgba(255, 215, 0, 0.3)) drop-shadow(0px 0px 30px rgba(255, 215, 0, 0.2));
}

/* Mini hands for result screen */
.bj-r-hands { display: flex; gap: 28px; justify-content: center; margin: 4px 0; }
.bj-r-hand { display: flex; flex-direction: column; align-items: center; gap: 8px; }
.bj-r-hand-lbl {
  font-size: 11px; font-weight: 700;
  letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--bj-text-ghost);
}
.bj-r-hand-val {
  font-family: var(--font-mono), monospace;
  font-size: 20px; font-weight: 800;
  font-variant-numeric: tabular-nums;
  color: var(--bj-text-white);
}
.bj-r-hand-val.bj-bust { color: var(--loss); text-decoration: line-through; text-decoration-thickness: 2px; text-decoration-color: rgba(255,51,82,0.5); }

.bj-r-cards { display: flex; padding-left: 4px; }

/* Mini cards — 67×93px (scaled from 46×64) */
.mc {
  width: 67px; height: 93px;
  border-radius: 7px;
  display: flex; flex-direction: column; justify-content: space-between;
  padding: 6px 7px;
  position: relative;
  transform-origin: bottom center;
  flex-shrink: 0;
}
.mc:not(:first-child) { margin-left: -9px; }
.bj-r-cards .mc:nth-child(1) { transform: rotate(-4deg) translateY(1px); z-index: 1; }
.bj-r-cards .mc:nth-child(2) { transform: rotate(1deg); z-index: 2; }
.bj-r-cards .mc:nth-child(3) { transform: rotate(5deg) translateY(2px); z-index: 3; }
.bj-r-cards .mc:nth-child(4) { transform: rotate(8deg) translateY(4px); z-index: 4; }

.mc .c-rank { font-size: 17px; }
.mc .c-suit { font-size: 13px; margin-top: -1px; }
.mc .suit-c { font-size: 29px; }

.cream .mc {
  background: linear-gradient(150deg, #faf5e8, #e6dcc2);
  border: 1px solid rgba(160,150,125,0.25);
  box-shadow: 0 2px 6px rgba(0,0,0,0.35), 0 8px 18px rgba(0,0,0,0.2);
}
.glass .mc {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.08);
  backdrop-filter: blur(12px);
  box-shadow: 0 2px 6px rgba(0,0,0,0.25), 0 8px 18px rgba(0,0,0,0.15), inset 0 1px 0 rgba(255,255,255,0.05);
}

/* Result P&L */
.bj-r-pnl {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 52px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  margin-top: 4px;
}
.bj-r-pnl.bj-rp-win { color: var(--win); text-shadow: 0 0 28px rgba(0,230,118,0.4), 0 2px 4px rgba(0,0,0,0.6); mix-blend-mode: screen; }
.bj-r-pnl.bj-rp-loss { color: var(--loss); text-shadow: 0 0 28px rgba(255,51,82,0.35), 0 2px 4px rgba(0,0,0,0.6); }
.bj-r-pnl.bj-rp-bj { color: var(--bj-bj-gold); text-shadow: 0 0 28px rgba(255,215,0,0.4), 0 2px 4px rgba(0,0,0,0.6); }

/* Messy chip stack */
.bj-r-chips { display: flex; gap: 3px; align-items: flex-end; margin-top: 4px; }
.bj-r-chip {
  width: 42px; height: 42px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono), monospace;
  font-size: 11px; font-weight: 800;
  position: relative;
  box-shadow: 0 3px 8px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.2), inset 0 -2px 0 rgba(0,0,0,0.35);
}
.bj-r-chip::before { content: ''; position: absolute; inset: 5px; border-radius: 50%; border: 1.5px dashed rgba(255,255,255,0.3); }
.bj-r-chips .bj-r-chip:nth-child(1) { transform: rotate(-8deg) translateY(0px); }
.bj-r-chips .bj-r-chip:nth-child(2) { transform: rotate(3deg) translateY(-4px); margin-left: -4px; width: 39px; height: 39px; font-size: 10px; }
.bj-r-chips .bj-r-chip:nth-child(3) { transform: rotate(-2deg) translateY(1px); margin-left: -3px; }
.bj-r-chips .bj-r-chip:nth-child(4) { transform: rotate(6deg) translateY(-2px); margin-left: -5px; width: 45px; height: 45px; font-size: 12px; }
.bj-r-chips .bj-r-chip:nth-child(5) { transform: rotate(-5deg) translateY(2px); margin-left: -2px; }

.bj-r-bal-row { display: flex; align-items: baseline; gap: 10px; margin-top: 6px; }
.bj-r-bal-lbl { font-size: 12px; font-weight: 600; color: var(--bj-text-ghost); letter-spacing: 0.1em; text-transform: uppercase; }
.bj-r-bal-val {
  font-family: var(--font-mono), monospace;
  font-size: 24px; font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--gold);
  text-shadow: 0 0 14px rgba(240,201,64,0.25);
}
</style>"""


def _build_blackjack_html(
    dealer_hand: list[tuple[str, str]],
    player_hand: list[tuple[str, str]],
    dealer_score: int | str,
    player_score: int | str,
    hide_dealer: bool = True,
    status: str = "",
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
    shoe_remaining: int | None = None,
    card_style: str = "cream",
) -> str:
    """Build the full blackjack card HTML with CSS-rendered playing cards."""

    outcome, badge_text, status_desc = _bj_outcome(status)

    # Near-miss overrides loss outcome
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"
        badge_text = "SO CLOSE"

    # Score display
    dealer_score_display = "?" if hide_dealer else str(dealer_score)
    dealer_bust = not hide_dealer and isinstance(dealer_score, int) and dealer_score > 21
    player_bust = isinstance(player_score, int) and player_score > 21

    # Dealer hand: active glow when dealer wins
    dealer_active = outcome == "loss" and not player_bust
    # Player hand: active glow during play or when player wins
    player_active = outcome in ("active", "win", "blackjack")

    # Table lighting state
    table_lit = ""
    if outcome == "win" or outcome == "blackjack":
        table_lit = " bj-lit-win"
    elif outcome == "loss":
        table_lit = " bj-lit-loss"

    # Build card HTML
    dealer_cards_html = _bj_hand_html(dealer_hand, hide_second=hide_dealer)
    player_cards_html = _bj_hand_html(player_hand)

    # Banner (only on resolved hands)
    banner_html = ""
    if status and outcome != "active":
        pnl = payout - wager
        ban_class = {"win": "bj-ban-win", "loss": "bj-ban-loss", "blackjack": "bj-ban-bj", "push": "bj-ban-push"}.get(outcome, "")
        pill_class = {"win": "bj-p-win", "loss": "bj-p-loss", "blackjack": "bj-p-bj", "push": "bj-p-push"}.get(outcome, "")
        text_class = {"win": "bj-t-win", "loss": "bj-t-loss", "blackjack": "bj-t-bj", "push": "bj-t-push"}.get(outcome, "")
        pnl_class = "bj-pnl-pos" if pnl > 0 else "bj-pnl-neg" if pnl < 0 else ""
        if outcome == "blackjack":
            pnl_class = "bj-pnl-bj"
        pnl_str = f"+${pnl:,}" if pnl > 0 else f"-${abs(pnl):,}" if pnl < 0 else "$0"
        banner_html = f"""
        <div class="bj-banner {ban_class}">
          <div class="bj-b-left">
            <span class="bj-pill {pill_class}">{esc(badge_text)}</span>
            <span class="bj-b-text {text_class}">{esc(status_desc)}</span>
          </div>
          <div class="bj-b-pnl {pnl_class}">{pnl_str}</div>
        </div>"""

    # Footer stats
    footer_html = ""
    if status and outcome != "active":
        pnl = payout - wager
        pnl_str = f"+${pnl:,}" if pnl > 0 else f"-${abs(pnl):,}" if pnl < 0 else "$0"
        pnl_class = "bj-fv-pos" if pnl > 0 else "bj-fv-neg" if pnl < 0 else ""
        chip = _bj_chip_html(wager, outcome)
        footer_html = f"""
        <div class="bj-footer">
          <div class="bj-f-row">
            <div class="bj-f-stat">
              <span class="bj-f-lbl">Wager</span>
              <div class="bj-chip-row">{chip}<span class="bj-f-val">${wager:,}</span></div>
            </div>
            <div class="bj-f-stat">
              <span class="bj-f-lbl">Payout</span>
              <span class="bj-f-val">${payout:,}</span>
            </div>
            <div class="bj-f-stat">
              <span class="bj-f-lbl">P&amp;L</span>
              <span class="bj-f-val {pnl_class}">{pnl_str}</span>
            </div>
            <div class="bj-f-stat">
              <span class="bj-f-lbl">Balance</span>
              <span class="bj-f-val bj-fv-bal">${balance:,}</span>
            </div>
          </div>
        </div>"""
    else:
        footer_html = f"""
        <div class="bj-footer">
          <div class="bj-f-row">
            <div class="bj-f-stat">
              <span class="bj-f-lbl">Wager</span>
              <span class="bj-f-val">${wager:,}</span>
            </div>
            <div class="bj-f-stat" style="margin-left:auto;">
              <span class="bj-f-lbl">Balance</span>
              <span class="bj-f-val bj-fv-bal">${balance:,}</span>
            </div>
          </div>
        </div>"""

    # Reusable shared components
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)
    shoe_html = _bj_shoe_html(shoe_remaining)

    content = f"""
    {_BLACKJACK_CSS}

    <!-- BJ Header -->
    <div class="bj-header">
      <div class="bj-h-left">
        <div class="bj-h-title">BLACKJACK</div>
        <div class="bj-h-sub">FLOW Casino</div>
      </div>
      <div class="bj-h-right">
        <div class="bj-h-player">{esc(player_name)}</div>
        {shoe_html}
      </div>
    </div>

    {banner_html}

    <!-- Table Surface -->
    <div class="bj-table{table_lit} {esc(card_style)}">
      <div class="bj-t-content">
        <div class="bj-hand{"  bj-active" if dealer_active else ""}">
          <div class="bj-hand-head">
            <span class="bj-hand-lbl">Dealer</span>
            <span class="bj-hand-val{"  bj-bust" if dealer_bust else ""}">{esc(dealer_score_display)}</span>
          </div>
          <div class="bj-cards">{dealer_cards_html}</div>
        </div>

        <div class="bj-t-div"></div>

        <div class="bj-hand{"  bj-active" if player_active else ""}">
          <div class="bj-hand-head">
            <span class="bj-hand-lbl">Player</span>
            <span class="bj-hand-val{"  bj-bust" if player_bust else ""}">{esc(str(player_score))}</span>
          </div>
          <div class="bj-cards">{player_cards_html}</div>
        </div>
      </div>
    </div>

    {footer_html}
    {streak_badge}
    {near_miss_banner}
    {jackpot_footer}"""

    return wrap_card(content, status_class, theme_id=theme_id)


async def render_blackjack_card(
    dealer_hand: list[tuple[str, str]],
    player_hand: list[tuple[str, str]],
    dealer_score: int | str,
    player_score: int | str,
    hide_dealer: bool = True,
    status: str = "",
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
    shoe_remaining: int | None = None,
    card_style: str = "cream",
) -> bytes:
    """Render a blackjack card to PNG bytes."""
    html = _build_blackjack_html(
        dealer_hand, player_hand, dealer_score, player_score,
        hide_dealer, status, wager, payout, balance, player_name, txn_id,
        streak_info, near_miss_msg, jackpot_info, theme_id=theme_id,
        shoe_remaining=shoe_remaining, card_style=card_style,
    )
    return await render_card(html)


# ── Cinematic Result Screen ─────────────────────────────────────────────────

def _build_blackjack_result_html(
    player_name: str,
    dealer_cards: list[tuple[str, str]],
    player_cards: list[tuple[str, str]],
    dealer_total: int,
    player_total: int,
    result: str,
    pnl: int,
    wager: int,
    balance: int,
    theme_id: str | None = None,
    card_style: str = "cream",
) -> str:
    """Build the cinematic blackjack result screen HTML."""

    # Result class and text mapping
    if result == "blackjack":
        res_class = "bj-res-bj"
        big_text = "BLACKJACK"
        big_class = "bj-rb-bj"
        pnl_class = "bj-rp-bj"
        chip_class = "chip-au"
        sub_text = f"Natural 21 &middot; 3:2 Payout"
    elif result == "win":
        res_class = "bj-res-win"
        big_text = "YOU WIN"
        big_class = "bj-rb-win"
        pnl_class = "bj-rp-win"
        chip_class = "chip-g"
        dealer_bust = dealer_total > 21
        sub_text = f"Dealer Busts &middot; {dealer_total}" if dealer_bust else f"Dealer {dealer_total} &middot; Player {player_total}"
    else:  # loss
        res_class = "bj-res-loss"
        big_text = "BUST"
        big_class = "bj-rb-loss"
        pnl_class = "bj-rp-loss"
        chip_class = "chip-r"
        player_bust = player_total > 21
        sub_text = f"Player Busts &middot; {player_total}" if player_bust else f"Dealer {dealer_total} &middot; Player {player_total}"

    dealer_bust = dealer_total > 21
    player_bust_flag = player_total > 21

    dealer_mini = _bj_mini_hand_html(dealer_cards)
    player_mini = _bj_mini_hand_html(player_cards)

    pnl_str = f"+${pnl:,}" if pnl > 0 else f"-${abs(pnl):,}" if pnl < 0 else "$0"

    # Chip stack (5 chips with denomination labels)
    chips_html = "".join(
        f'<div class="bj-r-chip {chip_class}">${abs(pnl) // max(i, 1) % 100 or 5}</div>'
        for i in range(1, 6)
    ) if pnl != 0 else ""

    content = f"""
    {_BLACKJACK_CSS}

    <div class="bj-result {res_class} {esc(card_style)}">
      <div class="bj-r-inner">
        <div class="bj-r-sub">{sub_text}</div>
        <div class="bj-r-big {big_class}">{big_text}</div>

        <div class="bj-r-hands">
          <div class="bj-r-hand">
            <div class="bj-r-hand-lbl">Dealer</div>
            <div class="bj-r-cards">{dealer_mini}</div>
            <div class="bj-r-hand-val{"  bj-bust" if dealer_bust else ""}">{dealer_total}</div>
          </div>
          <div class="bj-r-hand">
            <div class="bj-r-hand-lbl">Player</div>
            <div class="bj-r-cards">{player_mini}</div>
            <div class="bj-r-hand-val{"  bj-bust" if player_bust_flag else ""}">{player_total}</div>
          </div>
        </div>

        <div class="bj-r-pnl {pnl_class}">{pnl_str}</div>

        <div class="bj-r-chips">{chips_html}</div>

        <div class="bj-r-bal-row">
          <span class="bj-r-bal-lbl">Balance</span>
          <span class="bj-r-bal-val">${balance:,}</span>
        </div>
      </div>
    </div>"""

    return wrap_card(content, result, theme_id=theme_id)


async def render_blackjack_result(
    player_name: str,
    dealer_cards: list[tuple[str, str]],
    player_cards: list[tuple[str, str]],
    dealer_total: int,
    player_total: int,
    result: str,
    pnl: int,
    wager: int,
    balance: int,
    theme_id: str | None = None,
    card_style: str = "cream",
) -> bytes:
    """Render the cinematic blackjack result screen to PNG bytes."""
    html = _build_blackjack_result_html(
        player_name, dealer_cards, player_cards, dealer_total,
        player_total, result, pnl, wager, balance,
        theme_id=theme_id, card_style=card_style,
    )
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  SLOTS RENDERER
# ══════════════════════════════════════════════════════════════════════════════

# Fallback emoji for when slot icon PNGs aren't available
_SLOT_EMOJI_FALLBACK = {
    "shield": "🛡️", "crown": "👑", "trophy": "🏆",
    "wild": "✦", "football": "🏈", "star": "⭐", "coin": "🪙",
}

_SLOT_TIER_COLORS = {
    "jackpot": Tokens.GOLD_LIGHT,
    "legend": Tokens.GOLD,
    "epic": Tokens.PURPLE,
    "wild": "#F5E6C8",
    "rare": Tokens.BLUE_LIGHT,
    "common": Tokens.TEXT_PRIMARY,
    "base": Tokens.TEXT_MUTED,
}


def _build_slots_html(
    reels: list[str],
    revealed: int = 3,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    result_msg: str = "",
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
) -> str:
    """Build the full slots card HTML."""

    is_triple = revealed == 3 and len(set(reels)) == 1
    is_win = payout > 0

    if is_triple and reels[0] == "shield":
        outcome = "jackpot"
        badge_text = "JACKPOT!"
    elif is_win:
        outcome = "win"
        badge_text = "WIN"
    elif revealed == 3 and not is_win:
        outcome = "loss"
        badge_text = "LOSS"
    else:
        outcome = "active"
        badge_text = "SPINNING..."

    # Near-miss overrides loss to amber
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"
        badge_text = "SO CLOSE"

    header = build_header_html(icon_pill("slots", "🎰"), "SLOTS", [player_name], status_class, badge_text, txn_id)
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)

    # Build reel windows
    reels_html = ""
    for i in range(3):
        is_revealed = i < revealed
        symbol = reels[i] if i < len(reels) else "coin"
        cfg = SLOT_ICON_CONFIG.get(symbol, {})
        tier = cfg.get("tier", "base")
        tier_color = _SLOT_TIER_COLORS.get(tier, "#888")

        border_style = f"border: 2px solid {tier_color};" if is_revealed else "border: 2px solid #333;"
        glow = f"box-shadow: 0 0 20px {tier_color}40;" if is_revealed and tier in ("jackpot", "legend") else ""

        if is_triple and is_revealed:
            glow = f"box-shadow: 0 0 25px {tier_color}60;"

        icon_src = slot_icon_src(symbol)
        if is_revealed:
            if icon_src:
                inner = f'<img src="{icon_src}" class="slot-icon-img">'
            else:
                emoji = _SLOT_EMOJI_FALLBACK.get(symbol, "?")
                inner = f'<span class="slot-emoji">{emoji}</span>'
        else:
            inner = '<span class="slot-unknown">?</span>'

        reels_html += f"""
        <div class="reel-window" style="{border_style} {glow}">
          {inner}
        </div>"""

    # Payline
    payline = '<div class="payline"></div>' if revealed == 3 else ""

    # Result message
    result_html = ""
    if result_msg and revealed == 3:
        result_color = "#FFDA50" if is_triple else "var(--win)" if is_win else "var(--text-muted)"
        result_html = f'<div class="slots-result" style="color: {result_color};">{esc(result_msg)}</div>'

    data_grid = build_data_grid_html(wager, payout, balance) if revealed == 3 else ""
    footer = build_footer_html(balance)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)

    game_css = """
    <style>
    .reels-container {
      display: flex;
      justify-content: center;
      gap: var(--space-lg);
      padding: var(--space-xl) 20px;
      position: relative;
    }
    .reel-window {
      width: 150px;
      height: 150px;
      border-radius: 12px;
      background: rgba(0,0,0,0.4);
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
    }
    .slot-icon-img {
      width: 100px;
      height: 100px;
      object-fit: contain;
    }
    .slot-emoji {
      font-size: 64px;
    }
    .slot-unknown {
      font-family: var(--font-mono), monospace;
      font-size: 48px;
      font-weight: 800;
      color: #333;
    }
    .payline {
      position: absolute;
      left: 20px;
      right: 20px;
      top: 50%;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--gold) 10%, var(--gold) 90%, transparent);
      pointer-events: none;
    }
    .slots-result {
      text-align: center;
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: var(--font-lg);
      padding: 0 20px var(--space-md);
      letter-spacing: 0.5px;
    }
    </style>"""

    content = f"""
    {game_css}
    {header}
    {streak_badge}
    <div class="gold-divider"></div>

    <div class="reels-container">
      {reels_html}
      {payline}
    </div>

    {result_html}
    {near_miss_banner}

    <div class="gold-divider"></div>
    {data_grid}
    {"<div class='gold-divider'></div>" if data_grid else ""}
    {footer}
    {jackpot_footer}"""

    return wrap_card(content, status_class, theme_id=theme_id)


async def render_slots_card(
    reels: list[str],
    revealed: int = 3,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    result_msg: str = "",
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
) -> bytes:
    """Render a slots card to PNG bytes."""
    html = _build_slots_html(
        reels, revealed, wager, payout, balance, result_msg, player_name, txn_id,
        streak_info, near_miss_msg, jackpot_info, theme_id=theme_id,
    )
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  CRASH RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def _build_crash_html(
    current_mult: float,
    crashed: bool = False,
    cashed_out: bool = False,
    cashout_mult: Optional[float] = None,
    history: Optional[list[float]] = None,
    players_in: int = 0,
    total_wagered: int = 0,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    players: Optional[list[str]] = None,
    txn_id: Optional[str] = None,
    is_live: bool = False,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
) -> str:
    """Build the full crash card HTML with rocket launch theme."""

    if cashed_out:
        outcome = "win"
        badge_text = f"CASHED {cashout_mult or current_mult:.2f}x"
    elif crashed:
        outcome = "loss"
        badge_text = f"CRASHED {current_mult:.2f}x"
    else:
        outcome = "active"
        badge_text = "CLIMBING..."

    # Near-miss overrides loss to amber
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"

    display_players = players or [player_name]
    header = build_header_html(icon_pill("crash", "🚀"), "CRASH", display_players, status_class, badge_text, txn_id)
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)

    # Altitude gauge
    gauge_mult = cashout_mult if (cashed_out and cashout_mult is not None) else current_mult
    max_gauge = 25.0
    fill_pct = min(100.0, max(0.0, math.log(max(gauge_mult, 1.001)) / math.log(max_gauge) * 100))

    if cashed_out:
        fill_gradient = f"linear-gradient(to top, {Tokens.WIN}, {Tokens.WIN_DARK})"
    elif crashed:
        fill_gradient = f"linear-gradient(to top, {Tokens.LOSS}, {Tokens.LOSS_DARK})"
    else:
        fill_gradient = f"linear-gradient(to top, {Tokens.WIN}, {Tokens.GOLD}, {Tokens.LOSS})"

    gauge_markers = ""
    for mult_val in [1, 2, 5, 10, 25]:
        pos = math.log(max(mult_val, 1.001)) / math.log(max_gauge) * 100
        gauge_markers += f"""
        <div class="gauge-marker" style="bottom: {pos}%;">
          <div class="gauge-tick"></div>
          <span class="gauge-label">{mult_val}x</span>
        </div>"""

    # Cashout/crash dot on gauge
    gauge_dot = ""
    if cashed_out:
        gauge_dot = f'<div class="gauge-dot win" style="bottom: {fill_pct}%;"></div>'
    elif crashed:
        gauge_dot = f'<div class="gauge-dot loss" style="bottom: {fill_pct}%;"></div>'

    # Rocket area
    if crashed:
        rocket_html = """
        <div class="explosion">
          <div class="explosion-inner"></div>
        </div>"""
    else:
        rocket_html = '<div class="rocket">🚀</div>'

    # Multiplier display
    if crashed:
        mult_color = "var(--loss)"
        mult_sub = "CRASHED"
    elif cashed_out:
        mult_color = "var(--win)"
        mult_sub = "CASHED OUT"
    else:
        mult_color = "var(--gold)"
        mult_sub = "Climbing..."

    profit_pill = ""
    if cashed_out and payout > wager:
        profit = payout - wager
        profit_pill = f'<div class="profit-pill">+${profit:,}</div>'

    # Recent history pills
    history_html = ""
    if history:
        pills = ""
        for i, h in enumerate(history[-6:]):
            is_current = (i == len(history[-6:]) - 1) and is_live
            if h < 2.0:
                pill_color = Tokens.LOSS_LIGHT
            elif h < 10.0:
                pill_color = Tokens.WIN
            else:
                pill_color = Tokens.GOLD_LIGHT
            glow_class = "current-pill" if is_current else ""
            pills += f'<span class="history-pill {glow_class}" style="color: {pill_color};">{h:.2f}x</span>'
        history_html = f'<div class="history-row">{pills}</div>'

    # Data section: live round shows players+wagered, finished shows grid
    if is_live:
        data_section = f"""
        <div class="live-info">
          <span class="live-stat">👥 {players_in} Players</span>
          <span class="live-stat">💰 ${total_wagered:,} Wagered</span>
        </div>"""
    else:
        data_section = build_data_grid_html(wager, payout, balance)

    footer = build_footer_html(balance)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)

    game_css = """
    <style>
    .crash-content {
      display: flex;
      align-items: stretch;
      padding: 20px;
      gap: 20px;
      min-height: 280px;
    }

    /* Altitude gauge */
    .gauge-container {
      width: 80px;
      display: flex;
      flex-direction: column;
      align-items: center;
      position: relative;
    }
    .gauge-track {
      width: var(--space-sm);
      flex: 1;
      background: rgba(255,255,255,0.06);
      border-radius: var(--border-radius-sm);
      position: relative;
      overflow: visible;
    }
    .gauge-fill {
      position: absolute;
      bottom: 0;
      left: 0;
      right: 0;
      border-radius: var(--border-radius-sm);
      transition: height 0.3s ease;
    }
    .gauge-marker {
      position: absolute;
      left: 20px;
      display: flex;
      align-items: center;
      gap: 6px;
      transform: translateY(50%);
    }
    .gauge-tick {
      width: 10px;
      height: 2px;
      background: var(--gold);
    }
    .gauge-label {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: var(--font-sm);
      color: var(--gold);
      white-space: nowrap;
    }
    .gauge-dot {
      position: absolute;
      left: 50%;
      transform: translate(-50%, 50%);
      width: 10px;
      height: 10px;
      border-radius: 50%;
      z-index: 5;
    }
    .gauge-dot.win {
      background: var(--win);
      box-shadow: 0 0 10px var(--win);
    }
    .gauge-dot.loss {
      background: var(--loss);
      box-shadow: 0 0 10px var(--loss);
    }

    /* Rocket area */
    .rocket-area {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .rocket {
      font-size: var(--font-display-size);
      transform: rotate(-15deg);
      filter: drop-shadow(0 0 var(--space-md) rgba(212,175,55,0.5));
    }
    .explosion {
      width: 180px;
      height: 180px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255,200,0,0.6) 0%, rgba(255,120,0,0.4) 40%, rgba(255,50,0,0.2) 70%, transparent 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      filter: blur(3px);
    }
    .explosion-inner {
      width: 80px;
      height: 80px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255,255,200,0.8) 0%, rgba(255,160,0,0.6) 50%, transparent 100%);
    }

    /* Multiplier display */
    .mult-display {
      width: 200px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: var(--space-xs);
    }
    .mult-value {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: 64px;
      line-height: 1;
      letter-spacing: -2px;
    }
    .mult-sub {
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: 2px;
      text-transform: uppercase;
      opacity: 0.7;
    }
    .profit-pill {
      margin-top: var(--space-sm);
      padding: var(--space-xs) 14px;
      border-radius: 20px;
      background: rgba(74,222,128,0.12);
      border: 1px solid rgba(74,222,128,0.3);
      color: var(--win);
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: var(--font-sm);
    }

    /* History */
    .history-row {
      display: flex;
      justify-content: center;
      gap: var(--space-sm);
      padding: var(--space-sm) 20px var(--space-md);
      flex-wrap: wrap;
    }
    .history-pill {
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: var(--font-sm);
      padding: 3px 10px;
      border-radius: 12px;
      background: rgba(255,255,255,0.04);
    }
    .history-pill.current-pill {
      color: #fff !important;
      border: 1px solid currentColor;
      box-shadow: 0 0 var(--space-sm) currentColor;
    }

    /* Live info */
    .live-info {
      display: flex;
      justify-content: center;
      gap: var(--space-xl);
      padding: var(--space-md) 20px;
    }
    .live-stat {
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: 14px;
      color: var(--text-muted);
    }
    </style>"""

    mult_display_val = cashout_mult if cashed_out else current_mult

    content = f"""
    {game_css}
    {header}
    {streak_badge}
    <div class="gold-divider"></div>

    <div class="crash-content">
      <!-- Altitude gauge -->
      <div class="gauge-container">
        <div class="gauge-track">
          <div class="gauge-fill" style="height: {fill_pct}%; background: {fill_gradient};"></div>
          {gauge_dot}
          {gauge_markers}
        </div>
      </div>

      <!-- Rocket -->
      <div class="rocket-area">
        {rocket_html}
      </div>

      <!-- Multiplier -->
      <div class="mult-display">
        <div class="mult-value" style="color: {mult_color};">{mult_display_val:.2f}x</div>
        <div class="mult-sub" style="color: {mult_color};">{mult_sub}</div>
        {profit_pill}
      </div>
    </div>

    {history_html}
    {near_miss_banner}

    <div class="gold-divider"></div>
    {data_section}
    <div class="gold-divider"></div>
    {footer}
    {jackpot_footer}"""

    return wrap_card(content, status_class, theme_id=theme_id)


async def render_crash_card(
    current_mult: float,
    crashed: bool = False,
    cashed_out: bool = False,
    cashout_mult: Optional[float] = None,
    history: Optional[list[float]] = None,
    players_in: int = 0,
    total_wagered: int = 0,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    players: Optional[list[str]] = None,
    txn_id: Optional[str] = None,
    is_live: bool = False,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
) -> bytes:
    """Render a crash card to PNG bytes."""
    html = _build_crash_html(
        current_mult, crashed, cashed_out, cashout_mult, history,
        players_in, total_wagered, wager, payout, balance,
        player_name, players, txn_id, is_live,
        streak_info, near_miss_msg, jackpot_info, theme_id=theme_id,
    )
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  COIN FLIP RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def _build_coinflip_html(
    result: str,  # "heads" or "tails"
    player_pick: str,  # "heads" or "tails"
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    # PvP fields
    is_pvp: bool = False,
    opponent_name: Optional[str] = None,
    opponent_pick: Optional[str] = None,
    # Momentum fields
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
) -> str:
    """Build the full coin flip card HTML."""

    won = result == player_pick

    if is_pvp:
        if won:
            outcome = "win"
            badge_text = f"{player_name.upper()} WINS"
        else:
            outcome = "loss"
            badge_text = f"{(opponent_name or 'Opponent').upper()} WINS"
        display_players = [player_name, opponent_name or "Opponent"]
    else:
        if won:
            outcome = "win"
            badge_text = "WIN"
        else:
            outcome = "loss"
            badge_text = "LOSS"
        display_players = [player_name]

    # Near-miss overrides loss to amber (edge tease)
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"

    header = build_header_html(icon_pill("coinflip", "🪙"), "COIN FLIP", display_players, status_class, badge_text, txn_id)
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)

    # Coin visual
    is_heads = result == "heads"
    coin_letter = "H" if is_heads else "T"
    coin_gradient = "linear-gradient(135deg, #D4AF37, #FFDA50, #B8942D)" if is_heads else "linear-gradient(135deg, #C0C0C0, #E8E8E8, #A0A0A0)"
    coin_rim = "var(--gold)" if is_heads else "#C0C0C0"

    # Player pick indicator
    pick_icon = "✓" if won else "✗"
    pick_color = "var(--win)" if won else "var(--loss)"

    # PvP layout
    pvp_html = ""
    if is_pvp:
        p1_won = won
        p2_won = not won
        p1_color = "var(--win)" if p1_won else "var(--text-muted)"
        p2_color = "var(--win)" if p2_won else "var(--text-muted)"
        p1_opacity = "1" if p1_won else "0.5"
        p2_opacity = "1" if p2_won else "0.5"
        pvp_html = f"""
        <div class="pvp-row">
          <div class="pvp-player" style="color: {p1_color}; opacity: {p1_opacity};">
            <div class="pvp-name">{esc(player_name)}</div>
            <div class="pvp-pick">{esc(player_pick.upper())}</div>
          </div>
          <div class="pvp-vs">VS</div>
          <div class="pvp-player" style="color: {p2_color}; opacity: {p2_opacity};">
            <div class="pvp-name">{esc(opponent_name or 'Opponent')}</div>
            <div class="pvp-pick">{esc((opponent_pick or '').upper())}</div>
          </div>
        </div>"""
    else:
        pvp_html = f"""
        <div class="pick-display">
          <span style="color: {pick_color}; font-size: 20px;">{pick_icon}</span>
          <span class="pick-text">You picked <strong>{esc(player_pick.upper())}</strong></span>
        </div>"""

    data_grid = build_data_grid_html(wager, payout, balance)
    footer = build_footer_html(balance)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)

    game_css = f"""
    <style>
    .coin-area {{
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: var(--space-xl) 20px var(--space-lg);
    }}
    .coin {{
      width: 140px;
      height: 140px;
      border-radius: 50%;
      background: {coin_gradient};
      display: flex;
      align-items: center;
      justify-content: center;
      border: 4px solid {coin_rim};
      box-shadow: 0 var(--space-sm) var(--space-xl) rgba(0,0,0,0.4), inset 0 2px var(--space-xs) rgba(255,255,255,0.3);
      transform: perspective(400px) rotateX(10deg);
      font-family: var(--font-display), sans-serif;
      font-weight: 800;
      font-size: var(--font-display-size);
      color: rgba(0,0,0,0.25);
    }}
    .coin-result {{
      text-align: center;
      font-family: var(--font-display), sans-serif;
      font-weight: 800;
      font-size: 22px;
      color: var(--text-primary);
      margin-top: var(--space-md);
      letter-spacing: 2px;
    }}
    .pick-display {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: var(--space-sm);
      margin-top: var(--space-sm);
    }}
    .pick-text {{
      font-family: var(--font-display), sans-serif;
      font-size: 14px;
      color: var(--text-muted);
    }}
    .pick-text strong {{
      color: var(--text-primary);
    }}

    /* PvP */
    .pvp-row {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: var(--space-xl);
      margin-top: var(--space-md);
    }}
    .pvp-player {{
      text-align: center;
    }}
    .pvp-name {{
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: 16px;
    }}
    .pvp-pick {{
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: 12px;
      margin-top: 2px;
      opacity: 0.7;
    }}
    .pvp-vs {{
      font-family: var(--font-display), sans-serif;
      font-weight: 800;
      font-size: 14px;
      color: var(--text-dim);
      letter-spacing: 2px;
    }}
    </style>"""

    content = f"""
    {game_css}
    {header}
    {streak_badge}
    <div class="gold-divider"></div>

    <div class="coin-area">
      <div class="coin">{coin_letter}</div>
      <div class="coin-result">{result.upper()}</div>
      {pvp_html}
    </div>

    {near_miss_banner}

    <div class="gold-divider"></div>
    {data_grid}
    <div class="gold-divider"></div>
    {footer}
    {jackpot_footer}"""

    return wrap_card(content, status_class, theme_id=theme_id)


async def render_coinflip_card(
    result: str,
    player_pick: str,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    is_pvp: bool = False,
    opponent_name: Optional[str] = None,
    opponent_pick: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
    theme_id: str | None = None,
) -> bytes:
    """Render a coin flip card to PNG bytes."""
    html = _build_coinflip_html(
        result, player_pick, wager, payout, balance, player_name, txn_id,
        is_pvp, opponent_name, opponent_pick,
        streak_info, near_miss_msg, jackpot_info, theme_id=theme_id,
    )
    return await render_card(html)


# ═════════════════════════════════════════════════════════════════════════════
#  SCRATCH CARD
# ═════════════════════════════════════════════════════════════════════════════

def _build_scratch_html(
    tiles: list[int],
    revealed: int = 0,
    is_match: bool = False,
    player_name: str = "Player",
    total: int = 0,
    balance: int = 0,
    theme_id: str | None = None,
) -> str:
    """Build scratch card HTML with V6 design. 3 tiles, sequential reveal."""

    # Determine outcome state
    all_revealed = revealed >= 3
    if all_revealed and is_match:
        status_class = "jackpot"
        outcome = "jackpot"
        badge_text = "TRIPLE MATCH!"
    elif all_revealed:
        status_class = "win"
        outcome = "win"
        badge_text = f"+${total:,}"
    else:
        status_class = "push"
        outcome = "active"
        badge_text = f"{3 - revealed} LEFT"

    header = build_header_html(
        icon=icon_pill("scratch", "\U0001f3ab"),
        title="DAILY SCRATCH",
        players=[player_name],
        outcome=outcome,
        badge_text=badge_text,
    )

    # Build tile HTML
    tiles_html = ""
    for i in range(3):
        if i < revealed:
            # Revealed tile
            value = tiles[i]
            glow = "glow" if (is_match and all_revealed) else ""
            tiles_html += f"""
            <div class="scratch-tile revealed {glow}">
              <div class="tile-value">${value:,}</div>
            </div>"""
        else:
            # Unrevealed tile
            tiles_html += """
            <div class="scratch-tile hidden">
              <div class="tile-mystery">?</div>
            </div>"""

    # Result message
    if all_revealed and is_match:
        result_html = f"""
        <div class="scratch-result match">
          \U0001f3c6 TRIPLE MATCH — ${tiles[0]:,} × 3 = +${total:,}!
        </div>"""
    elif all_revealed:
        result_html = f"""
        <div class="scratch-result normal">
          \u2705 You won ${total:,}!
        </div>"""
    else:
        remaining = 3 - revealed
        result_html = f"""
        <div class="scratch-result pending">
          Tap Scratch! to reveal ({remaining} tile{"s" if remaining > 1 else ""} left)
        </div>"""

    footer = build_footer_html(balance)

    game_css = """<style>
    .scratch-area {
      display: flex;
      justify-content: center;
      gap: 14px;
      padding: 20px var(--space-xl) 10px;
    }
    .scratch-tile {
      width: 160px;
      height: 120px;
      border-radius: 12px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      position: relative;
      overflow: hidden;
    }
    .scratch-tile.hidden {
      background: rgba(140,115,36,0.12);
      border: 2px solid var(--gold-dim);
      box-shadow: inset 0 2px var(--space-sm) rgba(0,0,0,0.5), inset 0 -1px 2px rgba(255,255,255,0.03);
      /* Cross-hatch pattern */
      background-image:
        repeating-linear-gradient(
          45deg,
          transparent,
          transparent var(--space-sm),
          rgba(212,175,55,0.06) var(--space-sm),
          rgba(212,175,55,0.06) 9px
        ),
        repeating-linear-gradient(
          -45deg,
          transparent,
          transparent var(--space-sm),
          rgba(212,175,55,0.06) var(--space-sm),
          rgba(212,175,55,0.06) 9px
        );
    }
    .tile-mystery {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: var(--font-hero);
      color: var(--gold-dim);
      opacity: 0.5;
    }
    .scratch-tile.revealed {
      background: rgba(212,175,55,0.08);
      border: 2px solid var(--gold);
      box-shadow: inset 0 2px 6px rgba(0,0,0,0.45), inset 0 -1px 3px rgba(255,255,255,0.04);
    }
    /* Scratched grain texture on revealed tiles */
    .scratch-tile.revealed::before {
      content: '';
      position: absolute;
      inset: 0;
      border-radius: 10px;
      opacity: 0.07;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 128 128' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='1.2' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E");
      pointer-events: none;
    }
    .scratch-tile.revealed.glow {
      border-color: var(--gold-light);
      box-shadow: 0 0 20px rgba(212,175,55,0.35), inset 0 2px var(--space-sm) rgba(0,0,0,0.4), inset 0 -1px 3px rgba(255,218,80,0.06);
    }
    .tile-value {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: 32px;
      color: var(--text-primary);
      line-height: 1;
    }
    .scratch-tile.glow .tile-value {
      color: var(--gold-light);
    }
    .tile-label {
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: var(--font-xs);
      color: var(--gold-dim);
      letter-spacing: 2px;
      margin-top: 6px;
    }
    .scratch-result {
      text-align: center;
      padding: var(--space-sm) 20px 14px;
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: 14px;
    }
    .scratch-result.match {
      color: var(--gold-light);
      font-size: 16px;
    }
    .scratch-result.normal {
      color: var(--win);
    }
    .scratch-result.pending {
      color: var(--text-muted);
      font-weight: 600;
    }
    </style>"""

    content = f"""
    {game_css}
    {header}
    <div class="gold-divider"></div>

    <div class="scratch-area">
      {tiles_html}
    </div>

    {result_html}

    <div class="gold-divider"></div>
    {footer}"""

    return wrap_card(content, status_class, theme_id=theme_id)


async def render_scratch_card_v6(
    tiles: list[int],
    revealed: int = 0,
    is_match: bool = False,
    player_name: str = "Player",
    total: int = 0,
    balance: int = 0,
    theme_id: str | None = None,
) -> bytes:
    """Render a scratch card to PNG bytes using V6 Playwright renderer."""
    html = _build_scratch_html(tiles, revealed, is_match, player_name, total, balance, theme_id=theme_id)
    return await render_card(html)
