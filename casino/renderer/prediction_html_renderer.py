"""
prediction_html_renderer.py — FLOW Prediction Markets · V6 HTML Card Renderer
──────────────────────────────────────────────────────────────────────────────
Playwright HTML → PNG renderer for prediction market cards.
Reuses V6 shared infrastructure from casino_html_renderer.py.

Usage:
    from casino.renderer.prediction_html_renderer import render_market_list_card
    png_bytes = await render_market_list_card(markets, page, total_pages)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Optional

from atlas_html_engine import (
    render_card,
    wrap_card,
    esc,
    build_header_html,
    build_data_grid_html,
    build_footer_html,
)

# ── Category badge colors (hex) ──────────────────────────────────────────────

CATEGORY_COLORS: dict[str, str] = {
    "Elections":     "#5B9BD5",
    "Government":    "#3498DB",
    "Pop Culture":   "#FF69B4",
    "Entertainment": "#E91E63",
    "Economics":     "#27AE60",
    "Science":       "#9B59B6",
    "Tech":          "#1ABC9C",
    "AI":            "#00CED1",
    "World":         "#E67E22",
    "Other":         "#95A5A6",
}


def _category_color(category: str) -> str:
    """Get hex color for a category label like '🏛️ Politics'."""
    parts = category.split(" ", 1)
    name = parts[1] if len(parts) > 1 else parts[0]
    return CATEGORY_COLORS.get(name, CATEGORY_COLORS["Other"])


# ── Prediction-specific CSS ──────────────────────────────────────────────────

def _prediction_css() -> str:
    return """

/* ── Market list row ── */
.market-list-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 20px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.market-list-row:last-child { border-bottom: none; }

.market-index {
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 13px;
  color: var(--text-dim);
  width: 20px;
  text-align: center;
  flex-shrink: 0;
}

.category-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 10px;
  color: #fff;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  white-space: nowrap;
  flex-shrink: 0;
}

.market-title-text {
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 13px;
  color: var(--text-primary);
  flex: 1;
  line-height: 1.3;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.odds-pill {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 12px;
  display: flex;
  gap: 0;
  border-radius: 6px;
  overflow: hidden;
  white-space: nowrap;
  flex-shrink: 0;
}

.odds-yes {
  background: rgba(74,222,128,0.15);
  color: var(--win);
  padding: 3px 7px;
  border-right: 1px solid rgba(255,255,255,0.06);
}

.odds-no {
  background: rgba(248,113,113,0.12);
  color: var(--loss);
  padding: 3px 7px;
}

/* ── Market detail ── */
.market-detail-body {
  padding: 12px 20px 8px;
}

.market-question {
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 16px;
  color: var(--text-primary);
  line-height: 1.4;
  margin-bottom: 14px;
}

.prob-bar {
  display: flex;
  height: 28px;
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 14px;
  background: rgba(255,255,255,0.05);
}

.prob-fill-yes {
  background: var(--win);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 12px;
  color: var(--bg);
  min-width: 40px;
}

.prob-fill-no {
  background: var(--loss);
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 12px;
  color: var(--bg);
  min-width: 40px;
}

.price-boxes {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 12px;
}

.price-box {
  background: rgba(255,255,255,0.03);
  border-radius: 8px;
  padding: 12px 14px;
  text-align: center;
  border-top: 1px solid rgba(255,255,255,0.06);
  border-left: 1px solid rgba(255,255,255,0.04);
  border-bottom: 1px solid rgba(0,0,0,0.3);
  border-right: 1px solid rgba(0,0,0,0.2);
}

.price-box.yes {
  border-top: 2px solid rgba(74,222,128,0.4);
}

.price-box.no {
  border-top: 2px solid rgba(248,113,113,0.4);
}

.price-side {
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 4px;
}

.price-side.yes { color: var(--win); }
.price-side.no  { color: var(--loss); }

.price-value {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 26px;
  color: var(--text-primary);
  line-height: 1;
  margin-bottom: 2px;
}

.price-profit {
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 12px;
}

.price-profit.yes { color: #6EE7A0; }
.price-profit.no  { color: #FCA5A5; }

.meta-line {
  display: flex;
  justify-content: space-between;
  padding: 4px 20px 12px;
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.3px;
}

.position-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 5px;
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 11px;
  color: #fff;
  margin-top: 8px;
}

.position-badge.yes {
  background: rgba(74,222,128,0.15);
  border: 1px solid rgba(74,222,128,0.35);
  color: var(--win);
}

.position-badge.no {
  background: rgba(248,113,113,0.15);
  border: 1px solid rgba(248,113,113,0.35);
  color: var(--loss);
}

/* ── Bet confirmation ── */
.bet-detail-body {
  padding: 10px 20px;
}

.bet-market-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 13px;
  color: var(--text-sub);
  line-height: 1.3;
  margin-bottom: 10px;
}

.bet-info-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 6px;
  margin-bottom: 4px;
}

.bet-info-cell {
  background: rgba(255,255,255,0.03);
  border-radius: 8px;
  padding: 8px 10px;
  text-align: center;
  border-top: 1px solid rgba(255,255,255,0.06);
  border-left: 1px solid rgba(255,255,255,0.04);
  border-bottom: 1px solid rgba(0,0,0,0.3);
  border-right: 1px solid rgba(0,0,0,0.2);
}

.bet-info-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 10px;
  color: var(--gold-dim);
  letter-spacing: 1.2px;
  text-transform: uppercase;
  margin-bottom: 3px;
}

.bet-info-value {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 16px;
  color: var(--text-primary);
}

.bet-info-value.yes { color: var(--win); }
.bet-info-value.no  { color: var(--loss); }

/* ── Portfolio ── */
.portfolio-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 20px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.portfolio-row:last-child { border-bottom: none; }

.portfolio-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-primary);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.portfolio-side {
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  flex-shrink: 0;
}

.portfolio-side.yes {
  background: rgba(74,222,128,0.12);
  color: var(--win);
}

.portfolio-side.no {
  background: rgba(248,113,113,0.12);
  color: var(--loss);
}

.portfolio-qty {
  font-family: var(--font-mono), monospace;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-muted);
  flex-shrink: 0;
  width: 40px;
  text-align: right;
}

.portfolio-cost {
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 13px;
  color: var(--text-primary);
  flex-shrink: 0;
  width: 70px;
  text-align: right;
}

.portfolio-summary {
  display: flex;
  justify-content: space-between;
  padding: 10px 20px;
}

.portfolio-stat {
  text-align: center;
}

.portfolio-stat-label {
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 10px;
  color: var(--gold-dim);
  letter-spacing: 1.2px;
  text-transform: uppercase;
  margin-bottom: 2px;
}

.portfolio-stat-value {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 18px;
  color: var(--text-primary);
}

/* ── Resolution ── */
.resolution-body {
  padding: 10px 20px;
}

.resolution-result {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 28px;
  text-align: center;
  margin-bottom: 12px;
}

.resolution-result.yes { color: var(--win); }
.resolution-result.no  { color: var(--loss); }

.winners-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.winners-row:last-child { border-bottom: none; }

.winner-rank {
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 12px;
  color: var(--gold);
  width: 24px;
  text-align: center;
}

.winner-name {
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 13px;
  color: var(--text-primary);
  flex: 1;
}

.winner-payout {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 14px;
  color: var(--win);
}
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_volume(vol: float) -> str:
    """Format volume for display."""
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"${vol / 1_000:.0f}K"
    return f"${vol:.0f}"


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


def _wrap_prediction_card(status_class: str, content: str) -> str:
    """Wrap prediction market content with base engine CSS + prediction-specific CSS."""
    base_html = wrap_card(content, status_class)
    # Inject prediction-specific CSS before </head>
    return base_html.replace("</style>", f"{_prediction_css()}</style>", 1)


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET LIST CARD (State 1 — browse view)
# ══════════════════════════════════════════════════════════════════════════════

def _build_market_list_html(
    markets: list[dict],
    page: int,
    total_pages: int,
    filter_label: str = "All Categories",
) -> str:
    """Build HTML for the market list card (compact rows, scannable)."""
    header = build_header_html(
        icon="📊",
        title="PREDICTION MARKETS",
        players=[],
        outcome="active",
        badge_text=f"Page {page}/{total_pages}",
        subtitle="FLOW Markets",
    )

    rows_html = ""
    for i, m in enumerate(markets):
        idx = (page - 1) * len(markets) + i + 1  # continuous numbering
        title = m.get("title", "Untitled")
        category = m.get("category", "Other")
        yes_price = m.get("yes_price", 0.5)
        no_price = m.get("no_price", 1 - yes_price)
        cat_color = _category_color(category)
        # Strip emoji from category for badge text
        parts = category.split(" ", 1)
        cat_name = parts[1] if len(parts) > 1 else parts[0]

        rows_html += f"""
        <div class="market-list-row">
          <div class="market-index">{idx}</div>
          <div class="category-badge" style="background: {cat_color};">{esc(cat_name)}</div>
          <div class="market-title-text">{esc(title)}</div>
          <div class="odds-pill">
            <span class="odds-yes">{yes_price:.0%}</span>
            <span class="odds-no">{no_price:.0%}</span>
          </div>
        </div>"""

    if not markets:
        rows_html = """
        <div style="padding: 24px 20px; text-align: center; color: var(--text-muted);
                    font-family: var(--font-display), sans-serif; font-size: 14px;">
          No markets found. Try a different category.
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


async def render_market_list_card(
    markets: list[dict],
    page: int,
    total_pages: int,
    filter_label: str = "All Categories",
) -> bytes:
    """Render market list browse card to PNG bytes."""
    content = _build_market_list_html(markets, page, total_pages, filter_label)
    html = _wrap_prediction_card("active", content)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DETAIL CARD (State 2 — single market)
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
) -> str:
    """Build HTML for a detailed single-market card."""
    # Strip emoji for clean display
    parts = category.split(" ", 1)
    cat_name = parts[1] if len(parts) > 1 else parts[0]
    cat_color = _category_color(category)

    header = build_header_html(
        icon="📊",
        title=cat_name.upper(),
        players=[],
        outcome="active",
        badge_text="LIVE",
        subtitle="FLOW Markets",
    )

    # Probability bar
    yes_pct = max(0.02, min(0.98, yes_price))
    no_pct = 1 - yes_pct

    # Price boxes with implied profit
    yes_profit = _implied_profit(yes_price)
    no_profit = _implied_profit(no_price)

    # Position badge
    position_html = ""
    if user_position:
        side_class = "yes" if "YES" in user_position.upper() else "no"
        pos_text = f"YOUR BET: {user_position}"
        if user_contracts:
            pos_text += f" × {user_contracts}"
        position_html = f'<div class="position-badge {side_class}">{esc(pos_text)}</div>'

    # Meta line
    meta_parts = []
    if volume:
        meta_parts.append(f"Vol: {_fmt_volume(volume)}")
    if liquidity:
        meta_parts.append(f"Liq: {_fmt_volume(liquidity)}")
    end_str = _fmt_end_date(end_date)
    if end_str:
        meta_parts.append(f"Ends: {end_str}")

    meta_left = " · ".join(meta_parts[:2]) if meta_parts else ""
    meta_right = meta_parts[2] if len(meta_parts) > 2 else (meta_parts[1] if len(meta_parts) > 1 and not meta_left.count("·") else "")
    # Simplify: just show all in one line
    meta_text = "  |  ".join(meta_parts) if meta_parts else ""

    return f"""
    {header}
    <div class="gold-divider"></div>
    <div class="market-detail-body">
      <div class="market-question">{esc(title)}</div>
      <div class="prob-bar">
        <div class="prob-fill-yes" style="width: {yes_pct:.0%};">YES {yes_price:.0%}</div>
        <div class="prob-fill-no" style="width: {no_pct:.0%};">NO {no_price:.0%}</div>
      </div>
      <div class="price-boxes">
        <div class="price-box yes">
          <div class="price-side yes">YES</div>
          <div class="price-value">{yes_price:.0%}</div>
          <div class="price-profit yes">{esc(yes_profit)}</div>
        </div>
        <div class="price-box no">
          <div class="price-side no">NO</div>
          <div class="price-value">{no_price:.0%}</div>
          <div class="price-profit no">{esc(no_profit)}</div>
        </div>
      </div>
      {position_html}
    </div>
    <div class="meta-line"><span>{esc(meta_text)}</span></div>
    <div class="gold-divider"></div>
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
) -> bytes:
    """Render single-market detail card to PNG bytes."""
    content = _build_market_detail_html(
        title, category, yes_price, no_price, volume,
        liquidity, end_date, user_position, user_contracts, user_cost,
    )
    html = _wrap_prediction_card("active", content)
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
) -> bytes:
    """Render bet confirmation card to PNG bytes."""
    outcome = "win" if side.upper() == "YES" else "loss"
    side_class = "yes" if side.upper() == "YES" else "no"

    header = build_header_html(
        icon="📊",
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
          <div class="bet-info-value">×{quantity}</div>
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

    html = _wrap_prediction_card(outcome, content)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  PORTFOLIO CARD
# ══════════════════════════════════════════════════════════════════════════════

async def render_portfolio_card(
    positions: list[dict],
    player_name: str,
    total_invested: int,
    total_potential: int,
    balance: int,
) -> bytes:
    """Render portfolio card to PNG bytes.

    Each position dict: {title, side, qty, cost, payout, buy_price}
    """
    header = build_header_html(
        icon="📋",
        title="PORTFOLIO",
        players=[player_name],
        outcome="active",
        badge_text=f"{len(positions)} OPEN",
        subtitle="FLOW Markets",
    )

    rows_html = ""
    for pos in positions:
        side = pos.get("side", "YES").upper()
        side_class = "yes" if side == "YES" else "no"
        title = pos.get("title", "")[:45]
        qty = pos.get("qty", 0)
        cost = pos.get("cost", 0)

        rows_html += f"""
        <div class="portfolio-row">
          <div class="portfolio-side {side_class}">{side}</div>
          <div class="portfolio-title">{esc(title)}</div>
          <div class="portfolio-qty">×{qty}</div>
          <div class="portfolio-cost">${cost:,}</div>
        </div>"""

    if not positions:
        rows_html = """
        <div style="padding: 20px; text-align: center; color: var(--text-muted);
                    font-family: var(--font-display), sans-serif; font-size: 13px;">
          No open positions
        </div>"""

    pl = total_potential - total_invested
    pl_color = "var(--win)" if pl > 0 else "var(--loss)" if pl < 0 else "var(--push)"
    pl_str = f"+${pl:,}" if pl > 0 else f"-${abs(pl):,}" if pl < 0 else "$0"

    summary = f"""
    <div class="portfolio-summary">
      <div class="portfolio-stat">
        <div class="portfolio-stat-label">Invested</div>
        <div class="portfolio-stat-value">${total_invested:,}</div>
      </div>
      <div class="portfolio-stat">
        <div class="portfolio-stat-label">Max Payout</div>
        <div class="portfolio-stat-value">${total_potential:,}</div>
      </div>
      <div class="portfolio-stat">
        <div class="portfolio-stat-label">Max P&amp;L</div>
        <div class="portfolio-stat-value" style="color: {pl_color};">{pl_str}</div>
      </div>
    </div>"""

    footer = build_footer_html(balance)

    content = f"""
    {header}
    <div class="gold-divider"></div>
    {rows_html}
    <div class="gold-divider"></div>
    {summary}
    <div class="gold-divider"></div>
    {footer}
    """

    html = _wrap_prediction_card("active", content)
    return await render_card(html)


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
) -> bytes:
    """Render market resolution announcement card to PNG bytes.

    Each winner dict: {name, qty, payout, profit}
    """
    is_yes = result.upper() == "YES"
    outcome = "win" if is_yes else "loss"
    result_class = "yes" if is_yes else "no"

    header = build_header_html(
        icon="🏆",
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

    html = _wrap_prediction_card(outcome, content)
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  CURATED LIST CARD (10-market composite with sentiment)
# ══════════════════════════════════════════════════════════════════════════════

def _build_curated_list_html(
    markets: list[dict],
    filter_label: str = "Curated · All Categories",
) -> str:
    """Build HTML for a curated 10-market list with community sentiment bars."""
    header = build_header_html(
        icon="📊",
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
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 3px;">
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
        <div style="padding: 24px 20px; text-align: center; color: var(--text-muted);
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
/* ── Sentiment bar ── */
.sentiment-bar {
  height: 4px;
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

/* ── Daily Drop spotlight ── */
.spotlight-section {
  padding: 14px 20px;
}
.spotlight-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 4px;
  font-family: var(--font-display), sans-serif;
  font-weight: 800;
  font-size: 10px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--bg);
  background: var(--gold);
  margin-bottom: 8px;
}
.spotlight-title {
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 16px;
  color: var(--text-primary);
  line-height: 1.3;
  margin-bottom: 8px;
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
  gap: 8px;
  padding: 8px 20px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.supporting-row:last-child { border-bottom: none; }
.supporting-hook {
  font-family: var(--font-display), sans-serif;
  font-weight: 400;
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.3;
  margin-top: 2px;
}
.leaderboard-section {
  padding: 8px 20px;
}
.leaderboard-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
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

/* ── Price alert ── */
.alert-body {
  padding: 14px 20px;
}
.alert-direction {
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 28px;
  text-align: center;
  margin-bottom: 8px;
}
.alert-direction.up { color: var(--win); }
.alert-direction.down { color: var(--loss); }
.alert-prices {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 18px;
}
.alert-old { color: var(--text-muted); text-decoration: line-through; }
.alert-arrow { color: var(--text-dim); font-size: 14px; }
.alert-new { color: var(--text-primary); }
.alert-holders {
  text-align: center;
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 11px;
  color: var(--text-muted);
}
"""


async def render_curated_list_card(
    markets: list[dict],
    filter_label: str = "Curated · All Categories",
) -> bytes:
    """Render curated 10-market list card to PNG bytes."""
    content = _build_curated_list_html(markets, filter_label)
    base_html = wrap_card(content, "active")
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
) -> bytes:
    """Render daily drop card to PNG bytes.

    spotlight: {market_id, title, category, yes_price, no_price, analysis}
    supporting: [{market_id, title, category, yes_price, no_price, hook}, ...]
    community: {market_id: {label, yes_pct, total}, ...}
    leaderboard: [{name, profit, streak}, ...]
    """
    header = build_header_html(
        icon="🔥",
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
                    color: var(--text-muted); margin-top: 4px;">
          {esc(sp_sentiment.get('label', ''))}
        </div>"""

    spotlight_html = f"""
    <div class="spotlight-section">
      <div class="spotlight-badge">MARKET OF THE DAY</div>
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
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
                stat_text += f" · {streak}W streak"
            lb_rows += f"""
            <div class="leaderboard-row">
              <div class="winner-rank" style="color: var(--gold);">🏆</div>
              <div class="leaderboard-name">{esc(name)}</div>
              <div class="leaderboard-stat">{stat_text}</div>
            </div>"""

        lb_html = f"""
        <div class="gold-divider"></div>
        <div style="padding: 4px 20px 2px; font-family: var(--font-display), sans-serif;
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
    <div style="padding: 4px 20px 2px; font-family: var(--font-display), sans-serif;
                font-weight: 700; font-size: 10px; color: var(--gold-dim);
                letter-spacing: 1.2px; text-transform: uppercase;">
      ALSO WORTH WATCHING
    </div>
    {supporting_html}
    {lb_html}
    <div class="gold-divider"></div>
    """

    base_html = wrap_card(content, "jackpot")
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
) -> bytes:
    """Render price movement alert card to PNG bytes."""
    direction = "up" if new_price > old_price else "down"
    delta = abs(new_price - old_price)
    arrow = "↑" if direction == "up" else "↓"
    outcome = "win" if direction == "up" else "loss"

    title = market.get("title", "")
    category = market.get("category", "Other")
    cat_color = _category_color(category)
    parts = category.split(" ", 1)
    cat_name = parts[1] if len(parts) > 1 else parts[0]

    header = build_header_html(
        icon="📈" if direction == "up" else "📉",
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
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 10px;">
        <div class="category-badge" style="background: {cat_color};">{esc(cat_name)}</div>
      </div>
      <div class="market-question">{esc(title)}</div>
      <div class="alert-direction {direction}">{arrow} {delta:.0%}</div>
      <div class="alert-prices">
        <span class="alert-old">{old_price:.0%}</span>
        <span class="alert-arrow">→</span>
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

    base_html = wrap_card(content, "push")
    html = base_html.replace("</style>", f"{_prediction_css()}{_curated_css()}</style>", 1)
    return await render_card(html)
