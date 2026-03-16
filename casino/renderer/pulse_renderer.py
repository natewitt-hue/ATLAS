"""
pulse_renderer.py — ATLAS FLOW · Pulse Dashboard Renderer
──────────────────────────────────────────────────────────────────────────────
Renders the live Flow Pulse dashboard card — a 700px wide summary of all
active casino/sportsbook/prediction-market activity in the server.

V6 design language: dark bg, gold accents, Outfit + JetBrains Mono,
noise texture, glass-morphism section cells.

Usage:
    from casino.renderer.pulse_renderer import build_pulse_data, render_pulse_card
    data = build_pulse_data(...)
    png_bytes = await render_pulse_card(data)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import html as html_mod
from dataclasses import dataclass, field
from typing import List, Optional


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

def _esc(text) -> str:
    return html_mod.escape(str(text))


_NOISE_SVG = (
    "data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E"
    "%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' "
    "numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E"
    "%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"
)


def _highlight_rows_html(highlights: List[HighlightRow]) -> str:
    if not highlights:
        return (
            '<div style="padding:10px 0;text-align:center;color:#9a9280;'
            'font-size:12px;font-family:\'JetBrains Mono\',monospace;">'
            "No recent activity</div>"
        )
    rows = []
    for h in highlights:
        if h.is_loss:
            row_bg = "background:rgba(248,113,113,0.09);border-left:2px solid rgba(248,113,113,0.4);"
        else:
            row_bg = "background:rgba(255,255,255,0.02);border-left:2px solid transparent;"
        amount_color = "#F87171" if h.is_loss else "#4ADE80"
        rows.append(f"""
<div style="display:grid;grid-template-columns:26px 1fr 84px 48px;align-items:center;
    gap:6px;padding:6px 8px;border-radius:4px;{row_bg}margin-bottom:3px;">
  <div style="font-size:16px;text-align:center;">{h.icon}</div>
  <div style="font-size:13px;color:#c0b8a8;line-height:1.3;">{h.description_html}</div>
  <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;
      color:{amount_color};text-align:right;">{_esc(h.amount_html)}</div>
  <div style="font-size:11px;color:#9a9280;text-align:right;
      font-family:'JetBrains Mono',monospace;">{_esc(h.time_ago)}</div>
</div>""")
    return "\n".join(rows)


def _build_pulse_html(data: PulseData) -> str:
    from casino.renderer.casino_html_renderer import _font_face_css

    font_css = _font_face_css()

    # Jackpot hero
    jackpot_str = f"${data.jackpot_amount:,}"
    if data.jackpot_last_player:
        last_hit_line = (
            f'Last hit: <span style="color:#D4AF37;">{_esc(data.jackpot_last_player)}</span>'
            f' won ${data.jackpot_last_amount:,}'
        )
    else:
        last_hit_line = "No jackpot hit yet"
    jackpot_ago = _esc(data.jackpot_last_ago)

    # BJ card sub-line
    if data.bj_streak_player and data.bj_streak_count > 0:
        bj_streak_line = (
            f'<span style="color:#D4AF37;">{_esc(data.bj_streak_player)}</span>'
            f' on a {data.bj_streak_count}-win streak'
        )
    else:
        bj_streak_line = '<span style="color:#9a9280;">No active streak</span>'

    if data.bj_players:
        bj_players_line = ", ".join(_esc(p) for p in data.bj_players[:4])
        if len(data.bj_players) > 4:
            bj_players_line += f" +{len(data.bj_players) - 4}"
    else:
        bj_players_line = '<span style="color:#9a9280;">No active games</span>'

    # Slots sub-line
    if data.slots_top_player:
        slots_sub = (
            f'Top: <span style="color:#D4AF37;">{_esc(data.slots_top_player)}</span>'
            f' ${data.slots_top_amount:,} ({data.slots_top_mult}x)'
        )
    else:
        slots_sub = '<span style="color:#9a9280;">No spins yet today</span>'

    # Sportsbook sub-line
    if data.sb_hot_player and data.sb_hot_desc:
        sb_sub = (
            f'<span style="color:#D4AF37;">{_esc(data.sb_hot_player)}</span>'
            f' {_esc(data.sb_hot_desc)}'
        )
    else:
        sb_sub = '<span style="color:#9a9280;">No active bets</span>'

    # Predictions sub-line
    if data.pred_hot_title:
        pred_title_html = f'<div style="font-size:14px;font-weight:600;color:#e8e0d0;margin-bottom:4px;">{_esc(data.pred_hot_title)}</div>'
        pred_sub = (
            f'YES {data.pred_yes_pct}% &middot; NO {data.pred_no_pct}%'
            f' &middot; ${data.pred_volume:,} volume'
        )
    else:
        pred_title_html = '<div style="font-size:14px;font-weight:600;color:#9a9280;margin-bottom:4px;">No active markets</div>'
        pred_sub = ""

    highlights_html = _highlight_rows_html(data.highlights)

    # Footer totals (derived from highlights — sum wins/losses)
    total_won = sum(
        0 for h in data.highlights if not h.is_loss
    )
    # We don't have raw totals here — placeholders for caller to supply
    # Use the jackpot data and sportsbook volume as rough proxies
    footer_won = f"${data.sb_volume:,}" if data.sb_volume else "$0"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
{font_css}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #0a0a0a;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 16px;
  font-family: 'Outfit', system-ui, sans-serif;
}}
</style>
</head>
<body>
<div class="card" style="
    width:700px;
    border-radius:14px;
    overflow:hidden;
    position:relative;
    background:#111111;
    border:1px solid rgba(212,175,55,0.18);
    font-family:'Outfit',system-ui,sans-serif;
    color:#fff;
">

  <!-- Noise texture overlay -->
  <div style="position:absolute;inset:0;opacity:0.035;
      background-image:url('{_NOISE_SVG}');
      pointer-events:none;z-index:1;"></div>

  <!-- Gold status bar -->
  <div style="height:5px;width:100%;
      background:linear-gradient(90deg,#D4AF37,#FFDA50,#D4AF37);
      position:relative;z-index:2;"></div>

  <div style="position:relative;z-index:2;padding:18px 22px;">

    <!-- ── Header ─────────────────────────────────────────────────── -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
      <div style="display:flex;align-items:center;gap:10px;">
        <!-- Zap icon pill -->
        <div style="width:32px;height:32px;border-radius:8px;
            background:linear-gradient(135deg,#D4AF37,#FFDA50);
            display:flex;align-items:center;justify-content:center;
            font-size:16px;">&#9889;</div>
        <div>
          <div style="font-size:18px;font-weight:800;letter-spacing:1px;color:#e8e0d0;">FLOW PULSE</div>
          <div style="font-size:10px;color:#9a9280;font-family:'JetBrains Mono',monospace;letter-spacing:1px;">LIVE ACTIVITY DASHBOARD</div>
        </div>
      </div>
      <!-- LIVE indicator -->
      <div style="display:flex;align-items:center;gap:6px;">
        <div style="width:8px;height:8px;border-radius:50%;background:#4ADE80;
            box-shadow:0 0 6px #4ADE80;animation:pulse 2s infinite;"></div>
        <span style="font-size:12px;font-weight:700;color:#4ADE80;
            font-family:'JetBrains Mono',monospace;letter-spacing:1px;">LIVE</span>
      </div>
    </div>

    <!-- ── Progressive Jackpot Hero ───────────────────────────────── -->
    <div style="
        border-radius:10px;
        padding:18px 20px;
        margin-bottom:12px;
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

      <div style="font-size:13px;font-weight:700;color:#D4AF37;letter-spacing:4px;
          text-transform:uppercase;margin-bottom:6px;">Progressive Jackpot</div>
      <div style="
          font-family:'Outfit',sans-serif;
          font-size:48px;
          font-weight:800;
          background:linear-gradient(180deg,#FFDA50,#D4AF37);
          -webkit-background-clip:text;
          -webkit-text-fill-color:transparent;
          background-clip:text;
          filter:drop-shadow(0 2px 8px rgba(212,175,55,0.4));
          line-height:1.1;
          margin-bottom:8px;
      ">{jackpot_str}</div>
      <div style="font-size:13px;color:#b0a890;">{last_hit_line}</div>
      <div style="font-size:11px;color:#9a9280;margin-top:2px;
          font-family:'JetBrains Mono',monospace;">{jackpot_ago}</div>
    </div>

    <!-- ── 2×2 Game Grid ──────────────────────────────────────────── -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px;">

      <!-- Blackjack -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid #4ADE80;
          padding:12px 14px;
      ">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
          <div style="width:24px;height:24px;border-radius:6px;background:#4ADE80;
              display:flex;align-items:center;justify-content:center;font-size:13px;">&#127137;</div>
          <span style="font-size:15px;font-weight:700;color:#e8e0d0;">BLACKJACK</span>
        </div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:700;
            color:#D4AF37;margin-bottom:5px;">{data.active_bj} active</div>
        <div style="font-size:12px;color:#c0b8a8;margin-bottom:3px;">{bj_streak_line}</div>
        <div style="font-size:11px;color:#9a9280;">{bj_players_line}</div>
      </div>

      <!-- Slots -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid #60A5FA;
          padding:12px 14px;
      ">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
          <div style="width:24px;height:24px;border-radius:6px;background:#60A5FA;
              display:flex;align-items:center;justify-content:center;font-size:13px;">&#127922;</div>
          <span style="font-size:15px;font-weight:700;color:#e8e0d0;">SLOTS</span>
        </div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:700;
            color:#D4AF37;margin-bottom:5px;">{data.slots_spins_today:,} spins today</div>
        <div style="font-size:12px;color:#c0b8a8;">{slots_sub}</div>
      </div>

      <!-- Sportsbook -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid #D4AF37;
          padding:12px 14px;
      ">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
          <div style="display:flex;align-items:center;gap:8px;">
            <div style="width:24px;height:24px;border-radius:6px;background:#D4AF37;
                display:flex;align-items:center;justify-content:center;font-size:13px;">&#127936;</div>
            <span style="font-size:15px;font-weight:700;color:#e8e0d0;">SPORTSBOOK</span>
          </div>
          <div style="font-size:10px;font-weight:700;color:#111;background:#D4AF37;
              padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;">WK {data.sb_week}</div>
        </div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;
            color:#D4AF37;margin-bottom:5px;">{data.sb_bets} bets &middot; ${data.sb_volume:,} vol</div>
        <div style="font-size:12px;color:#c0b8a8;">{sb_sub}</div>
      </div>

      <!-- Predictions -->
      <div style="
          background:rgba(255,255,255,0.04);
          border-radius:10px;
          border:1px solid rgba(255,255,255,0.07);
          border-left:3px solid #F472B6;
          padding:12px 14px;
      ">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
          <div style="display:flex;align-items:center;gap:8px;">
            <div style="width:24px;height:24px;border-radius:6px;background:#F472B6;
                display:flex;align-items:center;justify-content:center;font-size:13px;">&#128202;</div>
            <span style="font-size:15px;font-weight:700;color:#e8e0d0;">PREDICTIONS</span>
          </div>
          <div style="font-size:10px;font-weight:700;color:#111;background:#F472B6;
              padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;">{data.pred_open} OPEN</div>
        </div>
        {pred_title_html}
        <div style="font-size:11px;color:#9a9280;font-family:'JetBrains Mono',monospace;">{pred_sub}</div>
      </div>

    </div><!-- /game grid -->

    <!-- ── Divider ─────────────────────────────────────────────────── -->
    <div style="height:1px;background:linear-gradient(90deg,transparent,rgba(212,175,55,0.3),transparent);
        margin:4px 0 12px;"></div>

    <!-- ── Recent Highlights ───────────────────────────────────────── -->
    <div style="margin-bottom:12px;">
      <div style="font-size:11px;font-weight:600;color:#D4AF37;letter-spacing:1px;
          text-transform:uppercase;margin-bottom:8px;">Recent Highlights</div>
      {highlights_html}
    </div>

    <!-- ── Footer ─────────────────────────────────────────────────── -->
    <div style="display:flex;align-items:center;justify-content:space-between;
        padding-top:8px;border-top:1px solid rgba(255,255,255,0.06);">
      <div style="font-size:11px;color:#a8a090;font-family:'JetBrains Mono',monospace;">
        FLOW PULSE v1.0 &middot; Updates every 60s
      </div>
      <div style="display:flex;gap:12px;">
        <span style="font-size:11px;color:#4ADE80;font-family:'JetBrains Mono',monospace;">
          Today: {footer_won} vol
        </span>
      </div>
    </div>

  </div><!-- /inner padding -->
</div><!-- /card -->
</body>
</html>"""


# ── Renderer ─────────────────────────────────────────────────────────────────

async def render_pulse_card(data: PulseData) -> bytes:
    """Render the pulse dashboard card to PNG bytes via Playwright."""
    from casino.renderer.casino_html_renderer import _render_card_html

    html = _build_pulse_html(data)
    return await _render_card_html(html, width=732)  # 700px card + 16px padding each side
