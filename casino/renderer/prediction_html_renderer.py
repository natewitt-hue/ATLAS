"""
prediction_html_renderer.py — FLOW Prediction Markets · V3 HTML Card Renderer
──────────────────────────────────────────────────────────────────────────────
Playwright HTML → PNG renderer for prediction market cards.
V3: Jewel-glow badges, neon-tube probability bars, recessed wells, left-stripe
portfolio rows, and a new position-detail card.

Usage:
    from casino.renderer.prediction_html_renderer import render_market_list_card
    png_bytes = await render_market_list_card(markets, page, total_pages)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Optional

from format_utils import fmt_volume
from atlas_html_engine import (
    render_card,
    wrap_card,
    esc,
    icon_pill,
    build_header_html,
    build_data_grid_html,
    build_footer_html,
)

# ── Category badge colors (hex) ──────────────────────────────────────────────

try:
    from polymarket_cog import CATEGORY_COLORS_HEX
except ImportError:
    CATEGORY_COLORS_HEX: dict[str, str] = {
        "Elections": "#5B9BD5", "Government": "#3498DB",
        "Pop Culture": "#FF69B4", "Entertainment": "#E91E63",
        "Economics": "#27AE60", "Science": "#9B59B6",
        "Tech": "#1ABC9C", "AI": "#00CED1",
        "World": "#E67E22", "Other": "#95A5A6",
    }

_DEFAULT_CATEGORY_COLOR = "#95A5A6"

# Category → data-cat mapping for jewel-glow CSS
_CAT_DATA_MAP: dict[str, str] = {
    "Elections": "pol", "Government": "pol", "Politics": "pol",
    "Economics": "econ", "Finance": "econ", "Business": "econ",
    "Pop Culture": "ent", "Entertainment": "ent", "Sports": "ent",
    "Tech": "econ", "AI": "econ", "Science": "econ",
    "World": "pol", "Other": "other",
}


def _category_color(category: str) -> str:
    """Get hex color for a category label like '🏛️ Politics'."""
    parts = category.split(" ", 1)
    name = parts[1] if len(parts) > 1 else parts[0]
    return CATEGORY_COLORS_HEX.get(name, _DEFAULT_CATEGORY_COLOR)


def _cat_data(category: str) -> str:
    """Map category to data-cat attribute for jewel-glow CSS."""
    parts = category.split(" ", 1)
    name = parts[1] if len(parts) > 1 else parts[0]
    return _CAT_DATA_MAP.get(name, "other")


def _jewel_badge(text: str, category: str) -> str:
    """Build a jewel-glow category badge."""
    data = _cat_data(category)
    return f'<span class="category-badge" data-cat="{data}">{esc(text)}</span>'


# ── Prediction-specific CSS (v3 — jewel-glow, neon-tube, recessed wells) ─────

def _prediction_css() -> str:
    return """

/* ══ V3 Prediction Header ══ */
.pred-header {
  display: flex;
  align-items: flex-start;
  padding: 14px 20px 10px;
  gap: 12px;
}
.pred-header-left { display: flex; align-items: flex-start; gap: 12px; flex: 1; }
.pred-header-right { text-align: right; flex-shrink: 0; }
.pred-header-right .stat-value {
  font-family: var(--font-mono), monospace;
  font-weight: 600; font-size: 14px; color: var(--text-primary);
}
.pred-header-right .stat-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 12px; color: var(--text-muted);
}

.globe-icon {
  width: 40px; height: 40px; border-radius: 10px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: radial-gradient(circle at 40% 35%, rgba(124,138,255,0.2), rgba(124,138,255,0.06));
  border: 1px solid rgba(124,138,255,0.12);
  position: relative; overflow: hidden;
}
.globe-icon::before, .globe-icon::after {
  content: ''; position: absolute;
  background: rgba(124,138,255,0.25);
}
.globe-icon::before { width: 1px; height: 60%; top: 20%; left: 50%; }
.globe-icon::after { width: 60%; height: 1px; top: 50%; left: 20%; }

.pf-icon {
  width: 40px; height: 40px; border-radius: 10px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: radial-gradient(circle at 40% 35%, rgba(212,175,55,0.2), rgba(212,175,55,0.06));
  border: 1px solid rgba(212,175,55,0.12);
  font-size: 18px;
}

.pred-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 600; font-size: 14px; letter-spacing: 2.5px;
  color: rgba(255,255,255,0.6); text-transform: uppercase;
}
.pred-subtitle {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 12px; color: var(--text-muted);
}
.page-pill {
  display: inline-block; margin-top: 4px;
  font-family: var(--font-mono), monospace; font-size: 12px; font-weight: 600;
  color: rgba(124,138,255,0.75);
  background: rgba(88,101,242,0.08); border: 1px solid rgba(88,101,242,0.12);
  border-radius: 5px; padding: 2px 8px;
}

/* ══ Jewel-Glow Category Badges ══ */
.category-badge {
  display: inline-flex; align-items: center; justify-content: center;
  padding: 3px 10px; min-width: 48px;
  border-radius: 5px;
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 11px; letter-spacing: 1px;
  text-transform: uppercase; white-space: nowrap; flex-shrink: 0;
}
.category-badge[data-cat="econ"] {
  color: var(--jewel-blue);
  background: rgba(74,158,255,0.06); border: 1px solid rgba(74,158,255,0.14);
  box-shadow: inset 0 0 8px rgba(74,158,255,0.08);
  text-shadow: 0 0 6px rgba(74,158,255,0.4);
}
.category-badge[data-cat="pol"] {
  color: var(--jewel-purple);
  background: rgba(168,85,247,0.06); border: 1px solid rgba(168,85,247,0.14);
  box-shadow: inset 0 0 8px rgba(168,85,247,0.08);
  text-shadow: 0 0 6px rgba(168,85,247,0.4);
}
.category-badge[data-cat="ent"] {
  color: var(--jewel-amber);
  background: rgba(255,183,77,0.06); border: 1px solid rgba(255,183,77,0.14);
  box-shadow: inset 0 0 8px rgba(255,183,77,0.08);
  text-shadow: 0 0 6px rgba(255,183,77,0.4);
}
.category-badge[data-cat="other"] {
  color: rgba(255,255,255,0.5);
  background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
  box-shadow: inset 0 0 6px rgba(255,255,255,0.03);
}

/* ══ Market List Rows (v3) ══ */
.market-list-row {
  border-radius: 10px;
  background: rgba(255,255,255,0.015);
  border: 1px solid rgba(255,255,255,0.025);
  padding: 14px 18px;
  margin: 0 20px 8px;
  display: flex; align-items: center; gap: 12px;
}
.market-list-row:last-child { margin-bottom: 0; }

.mrow-body { flex: 1; min-width: 0; }
.mrow-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 14px; color: var(--text-primary);
  line-height: 1.3;
  overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.mrow-meta {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 12px; color: var(--text-muted);
  margin-top: 3px;
}
.mrow-meta .dot {
  display: inline-block; width: 3px; height: 3px; border-radius: 50%;
  background: rgba(255,255,255,0.2); vertical-align: middle; margin: 0 6px;
}
.mrow-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }

/* ══ Neon-Tube Probability Bar ══ */
.prob-bar {
  position: relative;
  height: 8px; border-radius: 4px;
  background: rgba(237,66,69,0.15);
  border: 1px solid rgba(255,255,255,0.03);
  overflow: visible;
}
.prob-bar.large { height: 16px; border-radius: 8px; }
.prob-fill {
  height: 100%; border-radius: inherit;
  background: linear-gradient(90deg, #3DBE6F, #57F287);
  box-shadow: 0 0 8px rgba(87,242,135,0.25);
  position: relative;
}
.prob-bar.large .prob-fill {
  background: linear-gradient(180deg, #5EE89A, #3DBE6F);
  box-shadow: 0 0 14px rgba(87,242,135,0.3), inset 0 1px 2px rgba(255,255,255,0.15);
}
.prob-bar.large .prob-fill::after {
  content: ''; position: absolute; right: 0; top: 0; bottom: 0; width: 2px;
  background: rgba(255,255,255,0.8);
  box-shadow: 0 0 6px rgba(255,255,255,0.4);
}
.prob-tick {
  position: absolute; left: 50%; top: 0; bottom: 0; width: 1px;
  background: rgba(255,255,255,0.06); z-index: 1;
}

.prob-labels {
  display: flex; justify-content: space-between; margin-bottom: 6px;
  font-family: var(--font-mono), monospace; font-weight: 700; font-size: 14px;
}
.prob-labels .yes-label { color: var(--win); }
.prob-labels .no-label { color: var(--loss); }

.odds-display {
  font-family: var(--font-mono), monospace; font-weight: 700; font-size: 15px;
  white-space: nowrap;
}
.odds-display .yes-price { color: #5EE89A; text-shadow: 0 0 6px rgba(94,232,154,0.3); }
.odds-display .slash { color: rgba(255,255,255,0.30); margin: 0 2px; }
.odds-display .no-price { color: #F06B6B; }

/* ══ Recessed Wells ══ */
.buy-wells {
  display: grid; grid-template-columns: 1fr 1fr; gap: 14px;
  padding: 0 20px; margin-bottom: 16px;
}
.buy-well {
  background: var(--panel-bg);
  border-radius: 10px; padding: 16px;
  box-shadow: inset 0 2px 4px rgba(0,0,0,0.4), inset 0 0 1px rgba(255,255,255,0.05);
  text-align: center;
}
.buy-well.yes {
  border: 1px solid rgba(87,242,135,0.1);
  box-shadow: inset 0 2px 4px rgba(0,0,0,0.4), inset 0 0 20px rgba(87,242,135,0.03);
}
.buy-well.no {
  border: 1px solid rgba(237,66,69,0.1);
  box-shadow: inset 0 2px 4px rgba(0,0,0,0.4), inset 0 0 20px rgba(237,66,69,0.03);
}
.buy-well .side-label {
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 12px; letter-spacing: 2px;
  text-transform: uppercase; margin-bottom: 8px;
}
.buy-well.yes .side-label { color: var(--win); }
.buy-well.no .side-label { color: var(--loss); }
.buy-well .price-big {
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 38px; color: var(--text-primary); line-height: 1;
}
.buy-well .price-unit {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 12px; color: var(--text-muted); margin: 6px 0;
}
.buy-well .payout-text {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 13px; color: var(--text-sub);
}
.buy-well .payout-text em {
  font-style: normal; font-weight: 700; color: var(--gold);
}

.recessed-strip {
  display: grid; gap: 6px; padding: 10px 20px;
}
.recessed-strip.cols-3 { grid-template-columns: repeat(3, 1fr); }
.recessed-strip.cols-4 { grid-template-columns: repeat(4, 1fr); }
.strip-cell {
  background: var(--panel-bg);
  border: 1px solid var(--panel-border);
  border-radius: 8px; padding: 10px 8px; text-align: center;
  box-shadow: inset 0 1px 3px rgba(0,0,0,0.3);
}
.strip-cell .cell-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 10px; letter-spacing: 1px;
  color: var(--text-muted); text-transform: uppercase; margin-bottom: 4px;
}
.strip-cell .cell-value {
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 15px; color: var(--text-primary);
}
.strip-cell .cell-value.big { font-size: 22px; }
.strip-cell .cell-value.green { color: var(--win); }
.strip-cell .cell-value.red { color: var(--loss); }
.strip-cell .cell-value.gold { color: var(--gold); }
.strip-cell .cell-sub {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 10px; color: var(--text-muted); margin-top: 2px;
}

.status-pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-family: var(--font-display), sans-serif; font-weight: 600; font-size: 12px;
}
.status-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--win); box-shadow: 0 0 6px rgba(87,242,135,0.4);
}
.status-dot.closed { background: var(--loss); box-shadow: 0 0 6px rgba(237,66,69,0.4); }

/* ══ Portfolio Rows (v3 — left stripe) ══ */
.portfolio-row {
  display: flex; align-items: center; gap: 12px;
  border-radius: 10px;
  background: rgba(255,255,255,0.015);
  border: 1px solid rgba(255,255,255,0.025);
  padding: 12px 16px; margin: 0 20px 8px;
  position: relative; overflow: hidden;
}
.portfolio-row:last-child { margin-bottom: 0; }
.portfolio-row.settled { opacity: 0.6; }
.portfolio-row.settled.lost { opacity: 0.45; }

.port-stripe {
  width: 4px; align-self: stretch; border-radius: 2px; flex-shrink: 0;
}
.port-stripe.yes {
  background: linear-gradient(180deg, #5EE89A, #3DBE6F);
  box-shadow: 0 0 6px rgba(87,242,135,0.3);
}
.port-stripe.no {
  background: linear-gradient(180deg, #F06B6B, #ED4245);
  box-shadow: 0 0 6px rgba(237,66,69,0.3);
}

.port-body { flex: 1; min-width: 0; }
.port-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 14px; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.port-meta {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 12px; color: var(--text-muted); margin-top: 2px;
}
.port-meta .dot {
  display: inline-block; width: 3px; height: 3px; border-radius: 50%;
  background: rgba(255,255,255,0.2); vertical-align: middle; margin: 0 5px;
}

.port-col { text-align: right; flex-shrink: 0; min-width: 60px; }
.port-col .col-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 10px; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.5px;
}
.port-col .col-value {
  font-family: var(--font-mono), monospace;
  font-weight: 600; font-size: 15px; color: var(--text-sub);
}
.port-col .col-value.payout { color: var(--win); }
.port-col .col-value.payout-gold { color: var(--gold); }
.port-col .col-value.payout-loss { color: var(--loss); }

.port-action {
  font-family: var(--font-display), sans-serif; font-weight: 600; font-size: 12px;
  color: rgba(124,138,255,0.7);
  background: rgba(88,101,242,0.06); border: 1px solid rgba(88,101,242,0.12);
  border-radius: 5px; padding: 3px 10px; flex-shrink: 0;
}
.status-badge {
  font-family: var(--font-mono), monospace; font-weight: 700; font-size: 11px;
  padding: 3px 10px; border-radius: 5px; flex-shrink: 0;
}
.status-badge.won {
  color: var(--gold); background: rgba(212,175,55,0.1); border: 1px solid rgba(212,175,55,0.12);
}
.status-badge.lost {
  color: #F06B6B; background: rgba(237,66,69,0.08); border: 1px solid rgba(237,66,69,0.1);
}

.section-label {
  padding: 8px 20px 4px;
  font-family: var(--font-display), sans-serif;
  font-weight: 600; font-size: 12px; color: var(--text-muted); letter-spacing: 0.5px;
}
.section-label .dot-icon {
  display: inline-block; width: 6px; height: 6px; border-radius: 50%;
  vertical-align: middle; margin-right: 6px;
}
.section-label .dot-icon.open { background: var(--win); }
.section-label .dot-icon.settled { background: var(--text-dim); border: 1px solid var(--text-muted); }

/* ══ Position Detail (v3) ══ */
.pos-side-pill {
  display: inline-flex; align-items: center;
  padding: 4px 14px; border-radius: 5px;
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 11px; letter-spacing: 1.5px; text-transform: uppercase;
}
.pos-side-pill.yes {
  color: var(--win);
  background: rgba(87,242,135,0.08); border: 1px solid rgba(87,242,135,0.15);
  box-shadow: inset 0 0 8px rgba(87,242,135,0.06);
}
.pos-side-pill.no {
  color: var(--loss);
  background: rgba(237,66,69,0.08); border: 1px solid rgba(237,66,69,0.15);
  box-shadow: inset 0 0 8px rgba(237,66,69,0.06);
}

.sell-well {
  background: var(--panel-bg);
  border: 1px solid rgba(255,183,77,0.08); border-radius: 10px;
  padding: 16px 18px; margin: 0 20px 12px;
  box-shadow: inset 0 2px 4px rgba(0,0,0,0.4), inset 0 0 20px rgba(255,183,77,0.02);
}
.sell-well .sell-header {
  font-family: var(--font-display), sans-serif;
  font-weight: 600; font-size: 12px; letter-spacing: 1.5px;
  color: rgba(255,255,255,0.5); text-transform: uppercase; margin-bottom: 12px;
}
.sell-well .sell-row {
  display: flex; justify-content: space-between; align-items: center; padding: 5px 0;
}
.sell-well .sell-row .sell-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 13px; color: var(--text-sub);
}
.sell-well .sell-row .sell-value {
  font-family: var(--font-mono), monospace;
  font-weight: 600; font-size: 15px; color: var(--text-primary);
}
.sell-well .sell-row .sell-value.gold { color: var(--gold); }
.sell-well .sell-row .sell-value.bright { color: #fff; }
.sell-divider {
  height: 1px; margin: 8px 0;
  background: linear-gradient(90deg, transparent, rgba(255,183,77,0.15), transparent);
}
.sell-well .sell-pnl {
  display: flex; justify-content: space-between; align-items: center; padding: 8px 0 0;
}
.sell-well .sell-pnl .pnl-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 600; font-size: 14px; color: rgba(255,255,255,0.6);
}
.sell-well .sell-pnl .pnl-value {
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 22px; color: var(--win);
  text-shadow: 0 0 10px rgba(87,242,135,0.3);
}
.sell-well .sell-pnl .pnl-value.loss {
  color: var(--loss); text-shadow: 0 0 10px rgba(237,66,69,0.3);
}

.after-sale-strip {
  margin: 0 20px 12px; padding: 8px 14px;
  background: var(--panel-bg); border: 1px solid var(--panel-border);
  border-radius: 8px;
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 12px; color: var(--text-muted);
}

/* ══ Shared elements ══ */
.market-title-text {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 14px; color: var(--text-primary);
  flex: 1; line-height: 1.3;
  overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}

.market-question {
  font-family: var(--font-display), sans-serif;
  font-weight: 700; font-size: 16px; color: var(--text-primary);
  line-height: 1.4; margin-bottom: 14px;
}

.pred-footer {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 20px 14px;
}
.pred-footer .hint {
  font-family: var(--font-display), sans-serif;
  font-weight: 500; font-size: 12px; color: var(--text-muted);
}
.pred-footer .count {
  font-family: var(--font-mono), monospace;
  font-weight: 600; font-size: 12px; color: var(--text-sub);
}
.pred-footer .bal {
  font-family: var(--font-mono), monospace; font-weight: 700; font-size: 14px;
}
.pred-footer .bal .lbl { color: var(--gold-dim); }
.pred-footer .bal .amt { color: var(--text-primary); }

.meta-line {
  display: flex; justify-content: space-between;
  padding: var(--space-xs) 20px var(--space-md);
  font-family: var(--font-display), sans-serif;
  font-weight: 600; font-size: var(--font-xs); color: var(--text-muted);
  letter-spacing: 0.3px;
}

/* ══ Bet confirmation (kept from v2) ══ */
.bet-detail-body { padding: 10px 20px; }
.bet-market-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 600; font-size: var(--font-sm); color: var(--text-sub);
  line-height: 1.3; margin-bottom: 10px;
}
.bet-info-grid {
  display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px;
  margin-bottom: var(--space-xs);
}
.bet-info-cell {
  background: var(--panel-bg); border-radius: var(--border-radius);
  padding: var(--space-sm) 10px; text-align: center;
  border: 1px solid var(--panel-border);
  box-shadow: inset 0 1px 3px rgba(0,0,0,0.3);
}
.bet-info-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 700; font-size: 10px; color: var(--gold-dim);
  letter-spacing: 1.2px; text-transform: uppercase; margin-bottom: 3px;
}
.bet-info-value {
  font-family: var(--font-mono), monospace;
  font-weight: 800; font-size: 16px; color: var(--text-primary);
}
.bet-info-value.yes { color: var(--win); }
.bet-info-value.no  { color: var(--loss); }

/* ══ Position badge (market detail — existing position indicator) ══ */
.position-badge {
  display: inline-block; padding: 3px 10px; border-radius: 5px;
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: var(--font-xs); letter-spacing: 0.5px;
  margin-top: var(--space-sm);
}
.position-badge.yes {
  color: var(--win);
  background: rgba(74,222,128,0.08); border: 1px solid rgba(74,222,128,0.15);
  box-shadow: inset 0 0 8px rgba(74,222,128,0.06);
}
.position-badge.no {
  color: var(--loss);
  background: rgba(248,113,113,0.08); border: 1px solid rgba(248,113,113,0.15);
  box-shadow: inset 0 0 8px rgba(248,113,113,0.06);
}

/* ══ Resolution (kept from v2) ══ */
.resolution-body { padding: 10px 20px; }
.resolution-result {
  font-family: var(--font-mono), monospace;
  font-weight: 800; font-size: 28px; text-align: center; margin-bottom: var(--space-md);
}
.resolution-result.yes { color: var(--win); }
.resolution-result.no  { color: var(--loss); }
.winners-row {
  display: flex; align-items: center; gap: var(--space-sm);
  padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
}
.winners-row:last-child { border-bottom: none; }
.winner-rank {
  font-family: var(--font-mono), monospace; font-weight: 700; font-size: 12px;
  color: var(--gold); width: var(--space-xl); text-align: center;
}
.winner-name {
  font-family: var(--font-display), sans-serif; font-weight: 600;
  font-size: var(--font-sm); color: var(--text-primary); flex: 1;
}
.winner-payout {
  font-family: var(--font-mono), monospace; font-weight: 800; font-size: 14px; color: var(--win);
}

/* ══ Legacy compat — odds-pill used by curated/daily-drop ══ */
.odds-pill {
  font-family: var(--font-mono), monospace; font-weight: 800; font-size: 12px;
  display: flex; gap: 0; border-radius: 6px; overflow: hidden;
  white-space: nowrap; flex-shrink: 0;
}
.odds-yes { background: rgba(74,222,128,0.15); color: var(--win); padding: 3px 7px; border-right: 1px solid rgba(255,255,255,0.06); }
.odds-no { background: rgba(248,113,113,0.12); color: var(--loss); padding: 3px 7px; }
.market-index {
  font-family: var(--font-mono), monospace; font-weight: 700;
  font-size: var(--font-sm); color: var(--text-dim);
  width: 20px; text-align: center; flex-shrink: 0;
}

/* ══ Legacy prob-fill (used by curated/daily-drop spotlight) ══ */
.prob-fill-yes {
  background: linear-gradient(90deg, #3DBE6F, #57F287);
  box-shadow: 0 0 8px rgba(87,242,135,0.25);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono), monospace; font-weight: 800; font-size: 12px;
  color: #111; min-width: 40px;
}
.prob-fill-no {
  background: var(--loss);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono), monospace; font-weight: 800; font-size: 12px;
  color: #111; min-width: 40px;
}
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fmt_end_date(end_date: str) -> str:
    """Format ISO date for display."""
    if not end_date:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return end_date[:10]


def _implied_profit(price: float) -> str:
    """Calculate implied profit % from price."""
    if price <= 0 or price >= 1:
        return ""
    return f"+{((1 / price) - 1) * 100:.0f}%"


def _wrap_prediction_card(status_class: str, content: str, theme_id: str | None = None) -> str:
    """Wrap prediction market content with base engine CSS + prediction-specific CSS."""
    base_html = wrap_card(content, status_class, theme_id=theme_id)
    return base_html.replace("</style>", f"{_prediction_css()}</style>", 1)


def _price_cents(price: float) -> str:
    """Format a price as cents display (e.g., 0.38 → '38')."""
    return f"{int(round(price * 100))}"


def _recessed_strip(cells: list[tuple[str, str, str]], cols: int = 4) -> str:
    """Build a recessed summary/meta strip.

    Each cell is (label, value_html, css_class_for_value).
    """
    cells_html = ""
    for label, value, cls in cells:
        cells_html += f"""
        <div class="strip-cell">
          <div class="cell-label">{esc(label)}</div>
          <div class="cell-value {cls}">{value}</div>
        </div>"""
    return f'<div class="recessed-strip cols-{cols}">{cells_html}</div>'


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET LIST CARD (State 1 — browse view) — V3
# ══════════════════════════════════════════════════════════════════════════════

def _build_market_list_html(
    markets: list[dict],
    page: int,
    total_pages: int,
    filter_label: str = "All Categories",
    total_markets: int = 0,
) -> str:
    """Build HTML for the v3 market list card."""
    total = total_markets or len(markets)

    header = f"""
    <div class="pred-header">
      <div class="pred-header-left">
        <div class="globe-icon"></div>
        <div>
          <div class="pred-title">PREDICTION MARKETS</div>
          <div class="pred-subtitle">ATLAS Flow &middot; Real-world events</div>
        </div>
      </div>
      <div class="pred-header-right">
        <div class="stat-value">{total} open</div>
        <div class="stat-label">markets</div>
        <div class="page-pill">Page {page} / {total_pages}</div>
      </div>
    </div>"""

    rows_html = ""
    for i, m in enumerate(markets):
        title = m.get("title", "Untitled")
        category = m.get("category", "Other")
        yes_price = m.get("yes_price", 0.5)
        no_price = m.get("no_price", 1 - yes_price)
        yes_pct = max(2, min(98, int(round(yes_price * 100))))
        slug = m.get("slug", "")
        volume = m.get("volume", 0)
        end_str = _fmt_end_date(m.get("end_date", ""))

        parts = category.split(" ", 1)
        cat_name = parts[1] if len(parts) > 1 else parts[0]
        badge = _jewel_badge(cat_name, category)

        meta_parts = []
        if slug:
            meta_parts.append(esc(slug[:20].upper()))
        if end_str:
            meta_parts.append(esc(end_str))
        if volume:
            meta_parts.append(f"{fmt_volume(volume)} vol")
        meta_html = '<span class="dot"></span>'.join(meta_parts) if meta_parts else ""

        yes_cents = _price_cents(yes_price)
        no_cents = _price_cents(no_price)

        rows_html += f"""
        <div class="market-list-row">
          {badge}
          <div class="mrow-body">
            <div class="mrow-title">{esc(title)}</div>
            <div class="mrow-meta">{meta_html}</div>
          </div>
          <div class="mrow-right">
            <div class="prob-bar" style="width:90px;">
              <div class="prob-tick"></div>
              <div class="prob-fill" style="width:{yes_pct}%;"></div>
            </div>
            <div class="odds-display">
              <span class="yes-price">{yes_cents}&cent;</span><span class="slash">/</span><span class="no-price">{no_cents}&cent;</span>
            </div>
          </div>
        </div>"""

    if not markets:
        rows_html = """
        <div style="padding: 24px 20px; text-align: center; color: var(--text-muted);
                    font-family: var(--font-display), sans-serif; font-size: 14px;">
          No markets found. Try a different category.
        </div>"""

    shown = len(markets)
    start = (page - 1) * shown + 1 if shown else 0
    end = start + shown - 1 if shown else 0

    footer = f"""
    <div class="pred-footer">
      <span class="hint">Select a market below to view details</span>
      <span class="count">{start}–{end} of {total}</span>
    </div>"""

    return f"""
    {header}
    <div class="gold-divider"></div>
    {rows_html}
    <div class="gold-divider"></div>
    {footer}
    """


async def render_market_list_card(
    markets: list[dict],
    page: int,
    total_pages: int,
    filter_label: str = "All Categories",
    theme_id: str | None = None,
    total_markets: int = 0,
) -> bytes:
    """Render market list browse card to PNG bytes."""
    content = _build_market_list_html(markets, page, total_pages, filter_label, total_markets)
    html = _wrap_prediction_card("active", content, theme_id=theme_id)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DETAIL CARD (State 2 — single market) — V3
# ══════════════════════════════════════════════════════════════════════════════

def _build_market_detail_html(
    title: str,
    category: str,
    yes_price: float,
    no_price: float,
    volume: float,
    liquidity: float = 0,
    end_date: str = "",
    user_position: str | None = None,
    user_contracts: int = 0,
    user_cost: int = 0,
    user_balance: int = 0,
) -> str:
    """Build HTML for the v3 market detail card."""
    parts = category.split(" ", 1)
    cat_name = parts[1] if len(parts) > 1 else parts[0]
    badge = _jewel_badge(cat_name, category)

    yes_pct = max(2, min(98, int(round(yes_price * 100))))
    yes_cents = _price_cents(yes_price)
    no_cents = _price_cents(no_price)

    # Header: category tag left, volume right
    header = f"""
    <div class="pred-header">
      <div class="pred-header-left">
        {badge}
      </div>
      <div class="pred-header-right">
        <div class="stat-value">{fmt_volume(volume)}</div>
        <div class="stat-label">contracts traded</div>
      </div>
    </div>
    <div style="padding: 0 20px 12px;">
      <div class="market-question">{esc(title)}</div>
    </div>"""

    # Probability bar (large variant)
    prob_bar = f"""
    <div style="padding: 0 20px 16px;">
      <div class="prob-labels">
        <span class="yes-label">YES &mdash; {yes_price:.0%}</span>
        <span class="no-label">NO &mdash; {no_price:.0%}</span>
      </div>
      <div class="prob-bar large">
        <div class="prob-tick"></div>
        <div class="prob-fill" style="width:{yes_pct}%;"></div>
      </div>
    </div>"""

    # Buy wells
    buy_wells = f"""
    <div class="buy-wells">
      <div class="buy-well yes">
        <div class="side-label">BUY YES</div>
        <div class="price-big">{yes_cents}</div>
        <div class="price-unit">$ per contract</div>
        <div class="payout-text">Pays <em>$100</em> if YES</div>
      </div>
      <div class="buy-well no">
        <div class="side-label">BUY NO</div>
        <div class="price-big">{no_cents}</div>
        <div class="price-unit">$ per contract</div>
        <div class="payout-text">Pays <em>$100</em> if NO</div>
      </div>
    </div>"""

    # Position badge (if user has existing position)
    position_html = ""
    if user_position:
        side_class = "yes" if "YES" in user_position.upper() else "no"
        pos_text = f"YOUR BET: {user_position}"
        if user_contracts:
            pos_text += f" &times; {user_contracts}"
        position_html = f'<div style="padding: 0 20px 10px;"><div class="position-badge {side_class}">{pos_text}</div></div>'

    # Meta strip
    end_str = _fmt_end_date(end_date) or "TBD"
    meta_strip = _recessed_strip([
        ("Category", esc(cat_name), ""),
        ("Expires", esc(end_str), ""),
        ("Status", '<span class="status-pill"><span class="status-dot"></span> Open</span>', ""),
    ], cols=3)

    # Footer with balance
    footer = f"""
    <div class="pred-footer">
      <span class="hint">Click button below to open wager modal</span>
      <span class="bal"><span class="lbl">BAL </span><span class="amt">${user_balance:,}</span></span>
    </div>"""

    return f"""
    {header}
    <div class="gold-divider"></div>
    {prob_bar}
    {buy_wells}
    {position_html}
    <div class="gold-divider"></div>
    {meta_strip}
    <div class="gold-divider"></div>
    {footer}
    """


async def render_market_detail_card(
    title: str,
    category: str,
    yes_price: float,
    no_price: float,
    volume: float,
    liquidity: float = 0,
    end_date: str = "",
    user_position: str | None = None,
    user_contracts: int = 0,
    user_cost: int = 0,
    theme_id: str | None = None,
    user_balance: int = 0,
) -> bytes:
    """Render single-market detail card to PNG bytes."""
    content = _build_market_detail_html(
        title, category, yes_price, no_price, volume,
        liquidity, end_date, user_position, user_contracts, user_cost,
        user_balance,
    )
    html = _wrap_prediction_card("active", content, theme_id=theme_id)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  BET CONFIRMATION CARD
# ══════════════════════════════════════════════════════════════════════════════

async def render_bet_confirmation_card(
    market_title: str,
    side: str,
    price: float,
    quantity: int,
    cost: int,
    potential_payout: int,
    balance: int,
    player_name: str,
    txn_id: str | None = None,
    theme_id: str | None = None,
) -> bytes:
    """Render bet confirmation card to PNG bytes."""
    outcome = "win" if side.upper() == "YES" else "loss"
    side_class = "yes" if side.upper() == "YES" else "no"

    header = build_header_html(
        icon=icon_pill("predictions", "\U0001f4ca"),
        title="PREDICTION MARKET",
        players=[player_name],
        outcome=outcome,
        badge_text=f"BET {side.upper()}",
        txn_id=txn_id,
        subtitle="FLOW Markets",
    )

    body = f"""
    <div class="bet-detail-body">
      <div class="bet-market-title">{esc(market_title)}</div>
      <div class="bet-info-grid">
        <div class="bet-info-cell">
          <div class="bet-info-label">Side</div>
          <div class="bet-info-value {side_class}">{side.upper()}</div>
        </div>
        <div class="bet-info-cell">
          <div class="bet-info-label">Price</div>
          <div class="bet-info-value">{price:.0%}</div>
        </div>
        <div class="bet-info-cell">
          <div class="bet-info-label">Qty</div>
          <div class="bet-info-value">&times;{quantity}</div>
        </div>
      </div>
    </div>
    """

    data_grid = build_data_grid_html(cost, potential_payout, balance)
    footer = build_footer_html(balance)

    content = f"""
    {header}
    <div class="gold-divider"></div>
    {body}
    <div class="gold-divider"></div>
    {data_grid}
    <div class="gold-divider"></div>
    {footer}
    """

    html = _wrap_prediction_card(outcome, content, theme_id=theme_id)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  PORTFOLIO CARD — V3 (open/settled split, left-stripe rows)
# ══════════════════════════════════════════════════════════════════════════════

async def render_portfolio_card(
    positions: list[dict],
    player_name: str,
    total_invested: int,
    total_potential: int,
    balance: int,
    theme_id: str | None = None,
    realized_pnl: int = 0,
) -> bytes:
    """Render portfolio card to PNG bytes.

    Each position dict: {title, side, qty, cost, payout, buy_price, status}
    status is 'open', 'won', or 'lost' (defaults to 'open' if missing).
    """
    # Split positions
    open_pos = [p for p in positions if p.get("status", "open") == "open"]
    settled_pos = [p for p in positions if p.get("status", "open") != "open"]
    open_count = len(open_pos)

    header = f"""
    <div class="pred-header">
      <div class="pred-header-left">
        <div class="pf-icon">\U0001f4cb</div>
        <div>
          <div class="pred-title">YOUR PORTFOLIO</div>
          <div class="pred-subtitle">{esc(player_name)} &middot; {len(positions)} positions</div>
        </div>
      </div>
    </div>"""

    # Summary strip
    max_payout_cls = "green" if total_potential > total_invested else ""
    pnl_cls = "green" if realized_pnl > 0 else ("red" if realized_pnl < 0 else "")
    pnl_str = f"+${realized_pnl:,}" if realized_pnl > 0 else f"-${abs(realized_pnl):,}" if realized_pnl < 0 else "$0"

    summary = _recessed_strip([
        ("Open", f"{open_count}", "big"),
        ("Invested", f"${total_invested:,}", "big"),
        ("Max Pay", f"${total_potential:,}", f"big {max_payout_cls}"),
        ("Real P&amp;L", pnl_str, f"big {pnl_cls}"),
    ], cols=4)

    # Open section
    open_html = ""
    if open_pos:
        open_html += '<div class="section-label"><span class="dot-icon open"></span>Open positions</div>'
        for pos in open_pos:
            open_html += _build_portfolio_row(pos, settled=False)

    # Settled section
    settled_html = ""
    if settled_pos:
        settled_html += '<div class="section-label"><span class="dot-icon settled"></span>Settled</div>'
        for pos in settled_pos:
            settled_html += _build_portfolio_row(pos, settled=True)

    if not positions:
        open_html = """
        <div style="padding: 20px; text-align: center; color: var(--text-muted);
                    font-family: var(--font-display), sans-serif; font-size: var(--font-sm);">
          No positions yet — browse markets to get started
        </div>"""

    footer = f"""
    <div class="pred-footer">
      <span class="count">{len(positions)} most recent positions</span>
      <span class="bal"><span class="lbl">BAL </span><span class="amt">${balance:,}</span></span>
    </div>"""

    content = f"""
    {header}
    <div class="gold-divider"></div>
    {summary}
    <div class="gold-divider"></div>
    {open_html}
    {settled_html}
    <div class="gold-divider"></div>
    {footer}
    """

    html = _wrap_prediction_card("active", content, theme_id=theme_id)
    return await render_card(html)


def _build_portfolio_row(pos: dict, settled: bool = False) -> str:
    """Build a single portfolio row with left stripe."""
    side = pos.get("side", "YES").upper()
    side_class = "yes" if side == "YES" else "no"
    title = pos.get("title", "")[:50]
    qty = pos.get("qty", 0)
    cost = pos.get("cost", 0)
    payout = pos.get("payout", 0)
    buy_price = pos.get("buy_price", 0)
    status = pos.get("status", "open")

    settled_cls = ""
    if settled:
        settled_cls = " settled" + (" lost" if status == "lost" else "")

    # Meta line: ticker · side · qty · buy price
    slug = pos.get("slug", "")
    meta_parts = []
    if slug:
        meta_parts.append(esc(slug[:15].upper()))
    meta_parts.append(side)
    meta_parts.append(f"{qty} contracts")
    if buy_price:
        meta_parts.append(f"@{buy_price:.0%}")
    meta_html = '<span class="dot"></span>'.join(meta_parts)

    # Cost + payout columns
    payout_cls = "payout" if status == "open" else ("payout-gold" if status == "won" else "payout-loss")
    payout_val = f"${payout:,}" if payout else "$0"

    # Action/status badge
    action_html = ""
    if status == "open":
        action_html = '<span class="port-action">Details</span>'
    elif status == "won":
        action_html = '<span class="status-badge won">WON</span>'
    elif status == "lost":
        action_html = '<span class="status-badge lost">LOST</span>'

    return f"""
    <div class="portfolio-row{settled_cls}">
      <div class="port-stripe {side_class}"></div>
      <div class="port-body">
        <div class="port-title">{esc(title)}</div>
        <div class="port-meta">{meta_html}</div>
      </div>
      <div class="port-col">
        <div class="col-label">Cost</div>
        <div class="col-value">${cost:,}</div>
      </div>
      <div class="port-col">
        <div class="col-label">Payout</div>
        <div class="col-value {payout_cls}">{payout_val}</div>
      </div>
      {action_html}
    </div>"""


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION DETAIL / SELL CARD — V3 (NEW)
# ══════════════════════════════════════════════════════════════════════════════

async def render_position_detail_card(
    position: dict,
    sell_qty: int = 0,
    user_balance: int = 0,
    player_name: str = "",
    theme_id: str | None = None,
) -> bytes:
    """Render position detail card to PNG bytes.

    position dict: {title, side, qty, buy_price, cost, current_price, market_id, slug}
    sell_qty: 0 = just viewing, >0 = show sell preview for that quantity.
    """
    content = _build_position_detail_html(position, sell_qty, user_balance, player_name)
    # Green status bar for position cards (unrealized value signal)
    html = _wrap_prediction_card("win", content, theme_id=theme_id)
    return await render_card(html)


def _build_position_detail_html(
    position: dict,
    sell_qty: int,
    user_balance: int,
    player_name: str,
) -> str:
    """Build HTML for the position detail / sell preview card."""
    side = position.get("side", "YES").upper()
    side_class = "yes" if side == "YES" else "no"
    title = position.get("title", "Unknown Market")
    slug = position.get("slug", "")
    qty = position.get("qty", 0)
    buy_price = position.get("buy_price", 0)
    cost = position.get("cost", 0)
    current_price = position.get("current_price", buy_price)

    avg_cost_cents = _price_cents(buy_price)
    current_cents = _price_cents(current_price)
    delta = current_price - buy_price
    delta_cents = int(round(delta * 100))
    delta_sign = "+" if delta >= 0 else ""
    delta_arrow = "\u2191" if delta >= 0 else "\u2193"
    delta_cls = "green" if delta >= 0 else "red"

    # Header
    header = f"""
    <div class="pred-header">
      <div class="pred-header-left">
        <div>
          <span class="pos-side-pill {side_class}">YOUR {side} POSITION</span>
          <div class="market-question" style="margin-top:10px; margin-bottom:0;">{esc(title)}</div>
          <div class="pred-subtitle" style="margin-top:4px;">{esc(slug.upper())}</div>
        </div>
      </div>
      <div class="pred-header-right">
        <span class="status-pill"><span class="status-dot"></span> Market open</span>
      </div>
    </div>"""

    # Position grid
    pos_grid = _recessed_strip([
        ("Contracts", f"{qty}", "big"),
        ("Avg Cost", f"{avg_cost_cents}&cent;", "big"),
        ("Total Cost", f"${cost:,}", "big"),
        ("Current", f'{current_cents}&cent;<div class="cell-sub" style="color:var(--{delta_cls})">{delta_sign}{delta_cents}&cent; {delta_arrow}</div>', "big"),
    ], cols=4)

    # Sell preview section
    sell_html = ""
    if sell_qty > 0:
        sell_price_bucks = int(round(current_price * 100))
        proceeds = sell_price_bucks * sell_qty
        cost_basis = int(round(buy_price * 100)) * sell_qty
        pnl = proceeds - cost_basis
        pnl_sign = "+" if pnl >= 0 else "-"
        pnl_cls = "" if pnl >= 0 else " loss"

        remaining = qty - sell_qty
        remaining_payout = remaining * 100  # $100 per contract if wins

        sell_html = f"""
    <div class="gold-divider"></div>
    <div class="sell-well">
      <div class="sell-header">SELL CONTRACTS</div>
      <div class="sell-row">
        <span class="sell-label">Quantity to sell</span>
        <span class="sell-value bright">{sell_qty}</span>
      </div>
      <div class="sell-divider"></div>
      <div class="sell-row">
        <span class="sell-label">Your avg buy price</span>
        <span class="sell-value">{avg_cost_cents}&cent;/contract</span>
      </div>
      <div class="sell-row">
        <span class="sell-label">Current sell price</span>
        <span class="sell-value bright">{current_cents}&cent;/contract</span>
      </div>
      <div class="sell-row">
        <span class="sell-label">You'll receive</span>
        <span class="sell-value gold">${proceeds:,}</span>
      </div>
      <div class="sell-row">
        <span class="sell-label">Original cost ({sell_qty} contracts)</span>
        <span class="sell-value">${cost_basis:,}</span>
      </div>
      <div class="sell-divider"></div>
      <div class="sell-pnl">
        <span class="pnl-label">Profit on this sale</span>
        <span class="pnl-value{pnl_cls}">{pnl_sign}${abs(pnl):,}</span>
      </div>
    </div>
    <div class="after-sale-strip">
      After this sale: {remaining} remaining &middot; @{avg_cost_cents}&cent;
      &nbsp;|&nbsp; If {side} wins: pays ${remaining_payout:,}
    </div>"""

    # Footer
    footer = f"""
    <div class="pred-footer">
      <span class="hint">Sell price updates with live data</span>
      <span class="bal"><span class="lbl">BAL </span><span class="amt">${user_balance:,}</span></span>
    </div>"""

    return f"""
    {header}
    <div class="gold-divider"></div>
    {pos_grid}
    {sell_html}
    <div class="gold-divider"></div>
    {footer}
    """


# ══════════════════════════════════════════════════════════════════════════════
#  RESOLUTION CARD
# ══════════════════════════════════════════════════════════════════════════════

async def render_resolution_card(
    market_title: str,
    result: str,
    winners: list[dict],
    total_won: int,
    total_lost: int,
    total_voided: int = 0,
    theme_id: str | None = None,
) -> bytes:
    """Render market resolution announcement card to PNG bytes.

    Each winner dict: {name, qty, payout, profit}
    """
    is_yes = result.upper() == "YES"
    outcome = "win" if is_yes else "loss"
    result_class = "yes" if is_yes else "no"

    header = build_header_html(
        icon=icon_pill("predictions", "\U0001f3c6"),
        title="MARKET RESOLVED",
        players=[],
        outcome=outcome,
        badge_text=f"RESULT: {result.upper()}",
        subtitle="FLOW Markets",
    )

    winners_html = ""
    for i, w in enumerate(winners[:5]):
        rank = i + 1
        name = w.get("name", "Unknown")
        payout = w.get("payout", 0)
        profit = w.get("profit", 0)

        winners_html += f"""
        <div class="winners-row">
          <div class="winner-rank">#{rank}</div>
          <div class="winner-name">{esc(name)}</div>
          <div class="winner-payout">+${profit:,}</div>
        </div>"""

    body = f"""
    <div class="resolution-body">
      <div class="bet-market-title">{esc(market_title)}</div>
      <div class="resolution-result {result_class}">{result.upper()}</div>
      {winners_html}
    </div>"""

    content = f"""
    {header}
    <div class="gold-divider"></div>
    {body}
    <div class="gold-divider"></div>
    """

    html = _wrap_prediction_card(outcome, content, theme_id=theme_id)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  CURATED LIST CARD (10-market composite with sentiment)
# ══════════════════════════════════════════════════════════════════════════════

def _build_curated_list_html(
    markets: list[dict],
    filter_label: str = "Curated \u00b7 All Categories",
) -> str:
    """Build HTML for a curated 10-market list with community sentiment bars."""
    header = build_header_html(
        icon=icon_pill("predictions", "\U0001f4ca"),
        title="PREDICTION MARKETS",
        players=[],
        outcome="active",
        badge_text=f"{len(markets)} CURATED",
        subtitle="FLOW Markets",
    )

    rows_html = ""
    for i, m in enumerate(markets):
        title = m.get("title", "Untitled")
        category = m.get("category", "Other")
        yes_price = m.get("yes_price", 0.5)
        no_price = m.get("no_price", 1 - yes_price)
        cat_color = _category_color(category)
        parts = category.split(" ", 1)
        cat_name = parts[1] if len(parts) > 1 else parts[0]

        # Community sentiment
        sentiment = m.get("sentiment", {})
        sentiment_html = ""
        if sentiment.get("total", 0) > 0:
            yes_pct = sentiment.get("yes_pct", 50)
            sentiment_html = f"""
            <div class="sentiment-bar">
              <div class="sentiment-fill" style="width: {yes_pct}%;"></div>
              <span class="sentiment-label">{esc(sentiment.get('label', ''))}</span>
            </div>"""
        elif sentiment:
            sentiment_html = """
            <div class="sentiment-bar empty">
              <span class="sentiment-label">Be the first</span>
            </div>"""

        rows_html += f"""
        <div class="market-list-row">
          <div class="market-index">{i + 1}</div>
          <div style="flex: 1; min-width: 0;">
            <div style="display: flex; align-items: center; gap: var(--space-sm); margin-bottom: 3px;">
              <div class="category-badge" style="background: {cat_color};">{esc(cat_name)}</div>
              <div class="market-title-text">{esc(title)}</div>
            </div>
            {sentiment_html}
          </div>
          <div class="odds-pill">
            <span class="odds-yes">{yes_price:.0%}</span>
            <span class="odds-no">{no_price:.0%}</span>
          </div>
        </div>"""

    if not markets:
        rows_html = """
        <div style="padding: var(--space-xl) 20px; text-align: center; color: var(--text-muted);
                    font-family: var(--font-display), sans-serif; font-size: 14px;">
          No curated markets available. Check back soon.
        </div>"""

    filter_html = f"""
    <div style="padding: 6px 20px 2px; font-family: var(--font-display), sans-serif;
                font-weight: 600; font-size: 10px; color: var(--text-dim);
                letter-spacing: 1px; text-transform: uppercase;">
      {esc(filter_label)}
    </div>"""

    return f"""
    {header}
    <div class="gold-divider"></div>
    {filter_html}
    {rows_html}
    <div class="gold-divider"></div>
    """


def _curated_css() -> str:
    """Extra CSS for curated list and daily drop cards."""
    return """
/* \u2500\u2500 Sentiment bar \u2500\u2500 */
.sentiment-bar {
  height: var(--space-xs);
  border-radius: 2px;
  background: rgba(248,113,113,0.25);
  position: relative;
  margin-top: 2px;
  overflow: hidden;
}
.sentiment-bar.empty {
  background: rgba(255,255,255,0.06);
}
.sentiment-fill {
  height: 100%;
  background: rgba(74,222,128,0.5);
  border-radius: 2px;
}
.sentiment-label {
  position: absolute;
  right: 0;
  top: -14px;
  font-family: var(--font-mono), monospace;
  font-weight: 600;
  font-size: 9px;
  color: var(--text-dim);
  white-space: nowrap;
}

/* \u2500\u2500 Daily Drop spotlight \u2500\u2500 */
.spotlight-section {
  padding: 14px 20px;
}
.spotlight-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: var(--border-radius-sm);
  font-family: var(--font-display), sans-serif;
  font-weight: 800;
  font-size: 10px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: #111;
  background: var(--gold);
  margin-bottom: var(--space-sm);
}
.spotlight-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 16px;
  color: var(--text-primary);
  line-height: 1.3;
  margin-bottom: var(--space-sm);
}
.spotlight-analysis {
  font-family: var(--font-display), sans-serif;
  font-weight: 400;
  font-size: 12px;
  color: var(--text-sub);
  line-height: 1.5;
  margin-bottom: 10px;
  font-style: italic;
}
.supporting-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
  padding: var(--space-sm) 20px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.supporting-row:last-child { border-bottom: none; }
.supporting-hook {
  font-family: var(--font-display), sans-serif;
  font-weight: 400;
  font-size: var(--font-xs);
  color: var(--text-muted);
  line-height: 1.3;
  margin-top: 2px;
}
.leaderboard-section {
  padding: var(--space-sm) 20px;
}
.leaderboard-row {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-xs) 0;
}
.leaderboard-name {
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-primary);
  flex: 1;
}
.leaderboard-stat {
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 12px;
  color: var(--win);
}

/* \u2500\u2500 Price alert \u2500\u2500 */
.alert-body {
  padding: 14px 20px;
}
.alert-direction {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 28px;
  text-align: center;
  margin-bottom: var(--space-sm);
}
.alert-direction.up { color: var(--win); }
.alert-direction.down { color: var(--loss); }
.alert-prices {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: var(--space-md);
  margin-bottom: var(--space-md);
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: var(--font-lg);
}
.alert-old { color: var(--text-muted); text-decoration: line-through; }
.alert-arrow { color: var(--text-dim); font-size: 14px; }
.alert-new { color: var(--text-primary); }
.alert-holders {
  text-align: center;
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: var(--font-xs);
  color: var(--text-muted);
}
"""


async def render_curated_list_card(
    markets: list[dict],
    filter_label: str = "Curated \u00b7 All Categories",
    theme_id: str | None = None,
) -> bytes:
    """Render curated 10-market list card to PNG bytes."""
    content = _build_curated_list_html(markets, filter_label)
    base_html = wrap_card(content, "active", theme_id=theme_id)
    html = base_html.replace("</style>", f"{_prediction_css()}{_curated_css()}</style>", 1)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  DAILY DROP CARD (spotlight + 4 supporting + leaderboard)
# ══════════════════════════════════════════════════════════════════════════════

async def render_daily_drop_card(
    spotlight: dict,
    supporting: list[dict],
    community: dict,
    leaderboard: list[dict],
    theme_id: str | None = None,
) -> bytes:
    """Render daily drop card to PNG bytes.

    spotlight: {market_id, title, category, yes_price, no_price, analysis}
    supporting: [{market_id, title, category, yes_price, no_price, hook}, ...]
    community: {market_id: {label, yes_pct, total}, ...}
    leaderboard: [{name, profit, streak}, ...]
    """
    header = build_header_html(
        icon=icon_pill("predictions", "\U0001f525"),
        title="DAILY DROP",
        players=[],
        outcome="jackpot",
        badge_text="TODAY'S PICKS",
        subtitle="FLOW Markets",
    )

    # Spotlight section
    sp = spotlight
    cat_color = _category_color(sp.get("category", "Other"))
    parts = sp.get("category", "Other").split(" ", 1)
    cat_name = parts[1] if len(parts) > 1 else parts[0]
    yes_p = sp.get("yes_price", 0.5)
    no_p = sp.get("no_price", 0.5)

    sp_sentiment = community.get(sp.get("market_id", ""), {})
    sp_sentiment_html = ""
    if sp_sentiment.get("total", 0) > 0:
        sp_sentiment_html = f"""
        <div style="font-family: var(--font-mono), monospace; font-size: 10px;
                    color: var(--text-muted); margin-top: var(--space-xs);">
          {esc(sp_sentiment.get('label', ''))}
        </div>"""

    spotlight_html = f"""
    <div class="spotlight-section">
      <div class="spotlight-badge">MARKET OF THE DAY</div>
      <div style="display: flex; align-items: center; gap: var(--space-sm); margin-bottom: 6px;">
        <div class="category-badge" style="background: {cat_color};">{esc(cat_name)}</div>
      </div>
      <div class="spotlight-title">{esc(sp.get('title', ''))}</div>
      <div class="prob-bar">
        <div class="prob-fill-yes" style="width: {max(0.02, yes_p):.0%};">YES {yes_p:.0%}</div>
        <div class="prob-fill-no" style="width: {max(0.02, no_p):.0%};">NO {no_p:.0%}</div>
      </div>
      <div class="spotlight-analysis">{esc(sp.get('analysis', ''))}</div>
      {sp_sentiment_html}
    </div>"""

    # Supporting markets
    supporting_html = ""
    for s in supporting:
        s_cat_color = _category_color(s.get("category", "Other"))
        s_parts = s.get("category", "Other").split(" ", 1)
        s_cat_name = s_parts[1] if len(s_parts) > 1 else s_parts[0]
        s_yes = s.get("yes_price", 0.5)
        s_no = s.get("no_price", 0.5)

        hook_html = ""
        if s.get("hook"):
            hook_html = f'<div class="supporting-hook">{esc(s["hook"])}</div>'

        supporting_html += f"""
        <div class="supporting-row">
          <div style="flex: 1; min-width: 0;">
            <div style="display: flex; align-items: center; gap: 6px; margin-bottom: 2px;">
              <div class="category-badge" style="background: {s_cat_color}; font-size: 9px;">{esc(s_cat_name)}</div>
              <div class="market-title-text" style="font-size: 12px;">{esc(s.get('title', ''))}</div>
            </div>
            {hook_html}
          </div>
          <div class="odds-pill">
            <span class="odds-yes">{s_yes:.0%}</span>
            <span class="odds-no">{s_no:.0%}</span>
          </div>
        </div>"""

    # Leaderboard section
    lb_html = ""
    if leaderboard:
        lb_rows = ""
        for entry in leaderboard[:3]:
            name = entry.get("name", "Unknown")
            profit = entry.get("profit", 0)
            streak = entry.get("streak", 0)
            stat_text = f"+${profit:,}"
            if streak >= 3:
                stat_text += f" \u00b7 {streak}W streak"
            lb_rows += f"""
            <div class="leaderboard-row">
              <div class="winner-rank" style="color: var(--gold);">\U0001f3c6</div>
              <div class="leaderboard-name">{esc(name)}</div>
              <div class="leaderboard-stat">{stat_text}</div>
            </div>"""

        lb_html = f"""
        <div class="gold-divider"></div>
        <div style="padding: var(--space-xs) 20px 2px; font-family: var(--font-display), sans-serif;
                    font-weight: 700; font-size: 10px; color: var(--gold-dim);
                    letter-spacing: 1.2px; text-transform: uppercase;">
          TOP PREDICTORS THIS WEEK
        </div>
        <div class="leaderboard-section">{lb_rows}</div>"""

    content = f"""
    {header}
    <div class="gold-divider"></div>
    {spotlight_html}
    <div class="gold-divider"></div>
    <div style="padding: var(--space-xs) 20px 2px; font-family: var(--font-display), sans-serif;
                font-weight: 700; font-size: 10px; color: var(--gold-dim);
                letter-spacing: 1.2px; text-transform: uppercase;">
      ALSO WORTH WATCHING
    </div>
    {supporting_html}
    {lb_html}
    <div class="gold-divider"></div>
    """

    base_html = wrap_card(content, "jackpot", theme_id=theme_id)
    html = base_html.replace("</style>", f"{_prediction_css()}{_curated_css()}</style>", 1)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  PRICE ALERT CARD
# ══════════════════════════════════════════════════════════════════════════════

async def render_price_alert_card(
    market: dict,
    old_price: float,
    new_price: float,
    holders: int = 0,
    theme_id: str | None = None,
) -> bytes:
    """Render price movement alert card to PNG bytes."""
    direction = "up" if new_price > old_price else "down"
    delta = abs(new_price - old_price)
    arrow = "\u2191" if direction == "up" else "\u2193"
    outcome = "win" if direction == "up" else "loss"

    title = market.get("title", "")
    category = market.get("category", "Other")
    cat_color = _category_color(category)
    parts = category.split(" ", 1)
    cat_name = parts[1] if len(parts) > 1 else parts[0]

    header = build_header_html(
        icon=icon_pill("predictions", "\U0001f4c8" if direction == "up" else "\U0001f4c9"),
        title="PRICE ALERT",
        players=[],
        outcome=outcome,
        badge_text=f"{arrow} {delta:.0%} MOVE",
        subtitle="FLOW Markets",
    )

    holders_text = (
        f"{holders} TSL member{'s' if holders != 1 else ''} holding positions"
        if holders > 0
        else "No TSL positions yet"
    )

    body = f"""
    <div class="alert-body">
      <div style="display: flex; align-items: center; gap: var(--space-sm); margin-bottom: 10px;">
        <div class="category-badge" style="background: {cat_color};">{esc(cat_name)}</div>
      </div>
      <div class="market-question">{esc(title)}</div>
      <div class="alert-direction {direction}">{arrow} {delta:.0%}</div>
      <div class="alert-prices">
        <span class="alert-old">{old_price:.0%}</span>
        <span class="alert-arrow">\u2192</span>
        <span class="alert-new">{new_price:.0%}</span>
      </div>
      <div class="alert-holders">{esc(holders_text)}</div>
    </div>"""

    content = f"""
    {header}
    <div class="gold-divider"></div>
    {body}
    <div class="gold-divider"></div>
    """

    base_html = wrap_card(content, "push", theme_id=theme_id)
    html = base_html.replace("</style>", f"{_prediction_css()}{_curated_css()}</style>", 1)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  SELL CONFIRMATION CARD
# ══════════════════════════════════════════════════════════════════════════════

async def render_sell_confirmation_card(
    market_title: str,
    side: str,
    sell_quantity: int,
    sell_price: float,
    proceeds: int,
    cost_basis: int,
    profit_loss: int,
    balance: int,
    player_name: str,
    theme_id: str | None = None,
) -> bytes:
    """Render sell confirmation card to PNG bytes."""
    outcome = "win" if profit_loss >= 0 else "loss"
    pnl_class = "yes" if profit_loss >= 0 else "no"
    pnl_sign = "+" if profit_loss >= 0 else "-"
    pnl_display = f"{pnl_sign}${abs(profit_loss):,}"

    header = build_header_html(
        icon=icon_pill("predictions", "\U0001f4b0"),
        title="CONTRACTS SOLD",
        players=[player_name],
        outcome=outcome,
        badge_text=f"SOLD {side.upper()}",
        subtitle="FLOW Markets",
    )

    body = f"""
    <div class="bet-detail-body">
      <div class="bet-market-title">{esc(market_title)}</div>
      <div class="bet-info-grid">
        <div class="bet-info-cell">
          <div class="bet-info-label">Side</div>
          <div class="bet-info-value {'yes' if side.upper() == 'YES' else 'no'}">{side.upper()}</div>
        </div>
        <div class="bet-info-cell">
          <div class="bet-info-label">Sell Price</div>
          <div class="bet-info-value">{sell_price:.0%}</div>
        </div>
        <div class="bet-info-cell">
          <div class="bet-info-label">Qty Sold</div>
          <div class="bet-info-value">&times;{sell_quantity}</div>
        </div>
      </div>
    </div>
    """

    data_grid = f"""
    <div class="data-grid">
      <div class="data-row">
        <span class="data-label">Cost Basis</span>
        <span class="data-value">${cost_basis:,}</span>
      </div>
      <div class="data-row">
        <span class="data-label">Proceeds</span>
        <span class="data-value">${proceeds:,}</span>
      </div>
      <div class="data-row highlight">
        <span class="data-label">P/L</span>
        <span class="data-value {pnl_class}">{pnl_display}</span>
      </div>
    </div>
    """

    footer = build_footer_html(balance)

    content = f"""
    {header}
    <div class="gold-divider"></div>
    {body}
    <div class="gold-divider"></div>
    {data_grid}
    <div class="gold-divider"></div>
    {footer}
    """

    html = _wrap_prediction_card(outcome, content, theme_id=theme_id)
    return await render_card(html)
