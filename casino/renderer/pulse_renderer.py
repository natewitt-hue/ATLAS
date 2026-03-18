"""
pulse_renderer.py — ATLAS FLOW · Pulse Dashboard Renderer
──────────────────────────────────────────────────────────────────────────────
Renders the live Flow Pulse dashboard card — a 480px wide, mobile-optimized
summary of all active casino/sportsbook/prediction-market activity.

V7 design: narrower single-column layout so Discord mobile doesn't shrink
text to unreadable sizes. (Discord renders images at chat width ~375px;
a 480px source image scales to ~78% vs the old 700px scaling to ~54%.)

Usage:
    from casino.renderer.pulse_renderer import build_pulse_data, render_pulse_card
    data = build_pulse_data(...)
    png_bytes = await render_pulse_card(data)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from atlas_html_engine import render_card, wrap_card, esc


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class HighlightRow:
    """One row in the Recent Highlights feed."""
    icon: str              # emoji HTML entity, e.g. "&#128293;"
    description_html: str  # pre-formatted HTML with colored spans
    amount_html: str       # e.g. "+$1,200" or "-$1,400"
    time_ago: str          # e.g. "2m", "5m"
    is_loss: bool          # controls row styling (red tint vs default)


@dataclass
class PulseData:
    """All data needed to render the pulse dashboard card."""
    # Blackjack
    active_bj: int
    bj_players: List[str]
    bj_streak_player: Optional[str]
    bj_streak_count: int

    # Slots
    slots_spins_today: int
    slots_top_player: Optional[str]
    slots_top_amount: int
    slots_top_mult: int

    # Sportsbook
    sb_week: int
    sb_bets: int
    sb_volume: int
    sb_hot_player: Optional[str]
    sb_hot_desc: str

    # Predictions
    pred_open: int
    pred_hot_title: str
    pred_yes_pct: int
    pred_no_pct: int
    pred_volume: int

    # Progressive jackpot
    jackpot_amount: int
    jackpot_last_player: Optional[str]
    jackpot_last_amount: int
    jackpot_last_ago: str

    # Highlight feed
    highlights: List[HighlightRow] = field(default_factory=list)


def build_pulse_data(
    *,
    active_bj: int,
    bj_players: List[str],
    bj_streak_player: Optional[str],
    bj_streak_count: int,
    slots_spins_today: int,
    slots_top_player: Optional[str],
    slots_top_amount: int,
    slots_top_mult: int,
    sb_week: int,
    sb_bets: int,
    sb_volume: int,
    sb_hot_player: Optional[str],
    sb_hot_desc: str,
    pred_open: int,
    pred_hot_title: str,
    pred_yes_pct: int,
    pred_no_pct: int,
    pred_volume: int,
    jackpot_amount: int,
    jackpot_last_player: Optional[str],
    jackpot_last_amount: int,
    jackpot_last_ago: str,
    highlights: List[HighlightRow],
) -> PulseData:
    """Factory: construct a PulseData from named kwargs."""
    return PulseData(
        active_bj=active_bj,
        bj_players=bj_players,
        bj_streak_player=bj_streak_player,
        bj_streak_count=bj_streak_count,
        slots_spins_today=slots_spins_today,
        slots_top_player=slots_top_player,
        slots_top_amount=slots_top_amount,
        slots_top_mult=slots_top_mult,
        sb_week=sb_week,
        sb_bets=sb_bets,
        sb_volume=sb_volume,
        sb_hot_player=sb_hot_player,
        sb_hot_desc=sb_hot_desc,
        pred_open=pred_open,
        pred_hot_title=pred_hot_title,
        pred_yes_pct=pred_yes_pct,
        pred_no_pct=pred_no_pct,
        pred_volume=pred_volume,
        jackpot_amount=jackpot_amount,
        jackpot_last_player=jackpot_last_player,
        jackpot_last_amount=jackpot_last_amount,
        jackpot_last_ago=jackpot_last_ago,
        highlights=highlights,
    )


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _highlight_rows_html(highlights: List[HighlightRow]) -> str:
    if not highlights:
        return (
            '<div style="padding:10px 0;text-align:center;color:#9a9280;'
            'font-size:12px;font-family:var(--font-mono),monospace;">'
            "No recent activity</div>"
        )
    rows = []
    for h in highlights:
        if h.is_loss:
            row_bg = "background:rgba(248,113,113,0.09);border-left:2px solid rgba(248,113,113,0.4);"
        else:
            row_bg = "background:rgba(255,255,255,0.02);border-left:2px solid transparent;"
        amount_color = "var(--loss)" if h.is_loss else "var(--win)"
        rows.append(f"""
<div style="display:grid;grid-template-columns:26px 1fr 84px 48px;align-items:center;
    gap:6px;padding:6px 8px;border-radius:4px;{row_bg}margin-bottom:3px;">
  <div style="font-size:16px;text-align:center;">{h.icon}</div>
  <div style="font-size:13px;color:#c0b8a8;line-height:1.3;">{h.description_html}</div>
  <div style="font-family:var(--font-mono),monospace;font-size:13px;font-weight:700;
      color:{amount_color};text-align:right;">{esc(h.amount_html)}</div>
  <div style="font-size:11px;color:#9a9280;text-align:right;
      font-family:var(--font-mono),monospace;">{esc(h.time_ago)}</div>
</div>""")
    return "\n".join(rows)


def _build_pulse_html(data: PulseData) -> str:
    # Jackpot hero
    jackpot_str = f"${data.jackpot_amount:,}"
    if data.jackpot_last_player:
        last_hit_line = (
            f'Last hit: <span style="color:var(--gold);">{esc(data.jackpot_last_player)}</span>'
            f' won ${data.jackpot_last_amount:,}'
        )
    else:
        last_hit_line = "No jackpot hit yet"
    jackpot_ago = esc(data.jackpot_last_ago)

    # BJ card sub-line
    if data.bj_streak_player and data.bj_streak_count > 0:
        bj_streak_line = (
            f'<span style="color:var(--gold);">{esc(data.bj_streak_player)}</span>'
            f' on a {data.bj_streak_count}-win streak'
        )
    else:
        bj_streak_line = '<span style="color:#9a9280;">No active streak</span>'

    if data.bj_players:
        bj_players_line = ", ".join(esc(p) for p in data.bj_players[:4])
        if len(data.bj_players) > 4:
            bj_players_line += f" +{len(data.bj_players) - 4}"
    else:
        bj_players_line = '<span style="color:#9a9280;">No active games</span>'

    # Slots sub-line
    if data.slots_top_player:
        slots_sub = (
            f'Top: <span style="color:var(--gold);">{esc(data.slots_top_player)}</span>'
            f' ${data.slots_top_amount:,} ({data.slots_top_mult}x)'
        )
    else:
        slots_sub = '<span style="color:#9a9280;">No spins yet today</span>'

    # Sportsbook sub-line
    if data.sb_hot_player and data.sb_hot_desc:
        sb_sub = (
            f'<span style="color:var(--gold);">{esc(data.sb_hot_player)}</span>'
            f' {esc(data.sb_hot_desc)}'
        )
    else:
        sb_sub = '<span style="color:#9a9280;">No active bets</span>'

    # Predictions sub-line
    if data.pred_hot_title:
        pred_title_html = f'<div style="font-size:14px;font-weight:600;color:var(--text-primary);margin-bottom:4px;">{esc(data.pred_hot_title)}</div>'
        pred_sub = (
            f'YES {data.pred_yes_pct}% &middot; NO {data.pred_no_pct}%'
            f' &middot; ${data.pred_volume:,} volume'
        )
    else:
        pred_title_html = '<div style="font-size:14px;font-weight:600;color:#9a9280;margin-bottom:4px;">No active markets</div>'
        pred_sub = ""

    highlights_html = _highlight_rows_html(data.highlights)

    # Footer totals
    footer_won = f"${data.sb_volume:,}" if data.sb_volume else "$0"

    body_html = f"""
<style>
@keyframes pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.4; }}
}}
</style>

  <div style="position:relative;z-index:2;padding:16px 18px;">

    <!-- ── Header ─────────────────────────────────────────────────── -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
      <div style="display:flex;align-items:center;gap:10px;">
        <!-- Zap icon pill -->
        <div style="width:30px;height:30px;border-radius:8px;
            background:linear-gradient(135deg,#D4AF37,#FFDA50);
            display:flex;align-items:center;justify-content:center;
            font-size:15px;">&#9889;</div>
        <div>
          <div style="font-size:17px;font-weight:800;letter-spacing:1px;color:var(--text-primary);">FLOW PULSE</div>
          <div style="font-size:10px;color:#9a9280;font-family:var(--font-mono),monospace;letter-spacing:1px;">LIVE ACTIVITY DASHBOARD</div>
        </div>
      </div>
      <!-- LIVE indicator -->
      <div style="display:flex;align-items:center;gap:6px;">
        <div style="width:8px;height:8px;border-radius:50%;background:var(--win);
            box-shadow:0 0 6px var(--win);animation:pulse 2s infinite;"></div>
        <span style="font-size:12px;font-weight:700;color:var(--win);
            font-family:var(--font-mono),monospace;letter-spacing:1px;">LIVE</span>
      </div>
    </div>

    <!-- ── Progressive Jackpot Hero ───────────────────────────────── -->
    <div style="
        border-radius:10px;
        padding:14px 16px;
        margin-bottom:10px;
        background:linear-gradient(135deg,rgba(212,175,55,0.12),rgba(212,175,55,0.05));
        border:1px solid rgba(212,175,55,0.25);
        text-align:center;
        position:relative;
        overflow:hidden;
    ">
      <!-- shimmer accent lines -->
      <div style="position:absolute;top:0;left:0;right:0;height:1px;
          background:linear-gradient(90deg,transparent,rgba(212,175,55,0.4),transparent);"></div>
      <div style="position:absolute;bottom:0;left:0;right:0;height:1px;
          background:linear-gradient(90deg,transparent,rgba(212,175,55,0.2),transparent);"></div>

      <div style="font-size:12px;font-weight:700;color:var(--gold);letter-spacing:3px;
          text-transform:uppercase;margin-bottom:4px;">Progressive Jackpot</div>
      <div style="
          font-family:var(--font-display),sans-serif;
          font-size:40px;
          font-weight:800;
          background:linear-gradient(180deg,#FFDA50,#D4AF37);
          -webkit-background-clip:text;
          -webkit-text-fill-color:transparent;
          background-clip:text;
          filter:drop-shadow(0 2px 8px rgba(212,175,55,0.4));
          line-height:1.1;
          margin-bottom:6px;
      ">{jackpot_str}</div>
      <div style="font-size:12px;color:#b0a890;">{last_hit_line}</div>
      <div style="font-size:11px;color:#9a9280;margin-top:2px;
          font-family:var(--font-mono),monospace;">{jackpot_ago}</div>
    </div>

    <!-- ── 2×2 Game Grid (stacked pairs for mobile readability) ──── -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;">

      <!-- Blackjack -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid var(--win);
          padding:10px 12px;
      ">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
          <div style="width:22px;height:22px;border-radius:6px;background:var(--win);
              display:flex;align-items:center;justify-content:center;font-size:12px;">&#127137;</div>
          <span style="font-size:14px;font-weight:700;color:var(--text-primary);">BLACKJACK</span>
        </div>
        <div style="font-family:var(--font-mono),monospace;font-size:20px;font-weight:700;
            color:var(--gold);margin-bottom:4px;">{data.active_bj} active</div>
        <div style="font-size:11px;color:#c0b8a8;margin-bottom:2px;">{bj_streak_line}</div>
        <div style="font-size:10px;color:#9a9280;">{bj_players_line}</div>
      </div>

      <!-- Slots -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid #60A5FA;
          padding:10px 12px;
      ">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
          <div style="width:22px;height:22px;border-radius:6px;background:#60A5FA;
              display:flex;align-items:center;justify-content:center;font-size:12px;">&#127922;</div>
          <span style="font-size:14px;font-weight:700;color:var(--text-primary);">SLOTS</span>
        </div>
        <div style="font-family:var(--font-mono),monospace;font-size:20px;font-weight:700;
            color:var(--gold);margin-bottom:4px;">{data.slots_spins_today:,} spins today</div>
        <div style="font-size:11px;color:#c0b8a8;">{slots_sub}</div>
      </div>

      <!-- Sportsbook -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid var(--gold);
          padding:10px 12px;
      ">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
          <div style="display:flex;align-items:center;gap:6px;">
            <div style="width:22px;height:22px;border-radius:6px;background:var(--gold);
                display:flex;align-items:center;justify-content:center;font-size:12px;">&#127936;</div>
            <span style="font-size:14px;font-weight:700;color:var(--text-primary);">SPORTSBOOK</span>
          </div>
          <div style="font-size:9px;font-weight:700;color:var(--bg);background:var(--gold);
              padding:2px 5px;border-radius:4px;font-family:var(--font-mono),monospace;">WK {data.sb_week}</div>
        </div>
        <div style="font-family:var(--font-mono),monospace;font-size:16px;font-weight:700;
            color:var(--gold);margin-bottom:4px;">{data.sb_bets} bets &middot; ${data.sb_volume:,} vol</div>
        <div style="font-size:11px;color:#c0b8a8;">{sb_sub}</div>
      </div>

      <!-- Predictions -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid #F472B6;
          padding:10px 12px;
      ">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
          <div style="display:flex;align-items:center;gap:6px;">
            <div style="width:22px;height:22px;border-radius:6px;background:#F472B6;
                display:flex;align-items:center;justify-content:center;font-size:12px;">&#128202;</div>
            <span style="font-size:14px;font-weight:700;color:var(--text-primary);">PREDICTIONS</span>
          </div>
          <div style="font-size:9px;font-weight:700;color:var(--bg);background:#F472B6;
              padding:2px 5px;border-radius:4px;font-family:var(--font-mono),monospace;">{data.pred_open} OPEN</div>
        </div>
        {pred_title_html}
        <div style="font-size:10px;color:#9a9280;font-family:var(--font-mono),monospace;">{pred_sub}</div>
      </div>

    </div><!-- /game grid -->

    <!-- ── Divider ─────────────────────────────────────────────────── -->
    <div style="height:1px;background:linear-gradient(90deg,transparent,rgba(212,175,55,0.3),transparent);
        margin:4px 0 10px;"></div>

    <!-- ── Recent Highlights ───────────────────────────────────────── -->
    <div style="margin-bottom:10px;">
      <div style="font-size:11px;font-weight:600;color:var(--gold);letter-spacing:1px;
          text-transform:uppercase;margin-bottom:6px;">Recent Highlights</div>
      {highlights_html}
    </div>

    <!-- ── Footer ─────────────────────────────────────────────────── -->
    <div style="display:flex;align-items:center;justify-content:space-between;
        padding-top:6px;border-top:1px solid rgba(255,255,255,0.06);">
      <div style="font-size:10px;color:#a8a090;font-family:var(--font-mono),monospace;">
        FLOW PULSE v1.1 &middot; Updates every 60s
      </div>
      <div style="display:flex;gap:12px;">
        <span style="font-size:10px;color:var(--win);font-family:var(--font-mono),monospace;">
          Today: {footer_won} vol
        </span>
      </div>
    </div>

  </div><!-- /inner padding -->
"""

    return wrap_card(body_html, status_class="jackpot")


# ── Renderer ─────────────────────────────────────────────────────────────────

async def render_pulse_card(data: PulseData) -> bytes:
    """Render the pulse dashboard card to PNG bytes via Playwright."""
    html = _build_pulse_html(data)
    return await render_card(html)
