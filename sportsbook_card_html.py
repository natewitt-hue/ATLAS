"""
sportsbook_card_html.py — Flow Sportsbook Card (HTML Template)
═══════════════════════════════════════════════════════════════
Builds the HTML for the sportsbook hub card with user-specific data.
Rendered to PNG by card_html_renderer.py.
"""

from pathlib import Path

try:
    import data_manager as dm
except ImportError:
    dm = None

from sportsbook_cards import (
    _get_balance,
    _get_weekly_delta,
    _get_leaderboard_rank,
    _get_lifetime_record,
    _get_current_streak,
    _determine_status,
    STARTING_BALANCE,
)

_DIR = Path(__file__).parent
_FONTS_DIR = _DIR / "fonts"
_ICONS_DIR = _DIR / "icons"


def _file_url(path: Path) -> str:
    """Build a file:// URL with forward slashes (required by Chromium on Windows)."""
    return "file:///" + str(path.resolve()).replace("\\", "/")


def build_sportsbook_html(user_id: int) -> str:
    """Build complete HTML document string for the sportsbook hub card."""

    # ── Data queries ──────────────────────────────────────────────────────────
    balance = _get_balance(user_id)
    delta = _get_weekly_delta(user_id)
    rank, total_users = _get_leaderboard_rank(user_id)
    wins, losses, pushes = _get_lifetime_record(user_id)
    streak = _get_current_streak(user_id)
    status = _determine_status(user_id)

    total_bets = wins + losses + pushes
    win_rate = (wins / total_bets * 100) if total_bets > 0 else 0
    roi = ((balance - STARTING_BALANCE) / STARTING_BALANCE * 100) if STARTING_BALANCE > 0 else 0

    if dm is not None:
        season = dm.CURRENT_SEASON
        week = dm.CURRENT_WEEK + 1
    else:
        season, week = 96, 8

    # ── Format values ─────────────────────────────────────────────────────────
    balance_str = f"{balance:,}"

    # Record
    record_str = f"{wins}-{losses}"
    if pushes:
        record_str += f"-{pushes}"

    # Win rate
    win_rate_str = f"{win_rate:.0f}%"
    win_rate_class = "g" if win_rate >= 50 else ("r" if total_bets > 0 else "")

    # ROI
    roi_str = f"{roi:+.0f}%"
    roi_class = "g" if roi >= 0 else "r"

    # Streak
    streak_class = "g" if streak.startswith("W") else ("r" if streak.startswith("L") else "")

    # Status bar class
    sbar_class = {"top10": "top", "positive": "pos", "negative": "neg"}.get(status, "pos")

    # Delta pill
    if delta >= 0:
        delta_class = "up"
        delta_text = f"▲ +${delta:,} this week"
    else:
        delta_class = "dn"
        delta_text = f"▼ -${abs(delta):,} this week"

    # Rank pill style
    rank_cold = "" if rank <= 10 else " cold"

    # Rank hash/label colors handled by CSS via .cold class

    # ── Asset URLs ────────────────────────────────────────────────────────────
    font_outfit_bold = _file_url(_FONTS_DIR / "Outfit-Bold.ttf")
    font_outfit_extrabold = _file_url(_FONTS_DIR / "Outfit-ExtraBold.ttf")
    font_outfit_semibold = _file_url(_FONTS_DIR / "Outfit-SemiBold.ttf")
    font_jbm_bold = _file_url(_FONTS_DIR / "JetBrainsMono-Bold.ttf")
    font_jbm_extrabold = _file_url(_FONTS_DIR / "JetBrainsMono-ExtraBold.ttf")
    font_jbm_regular = _file_url(_FONTS_DIR / "JetBrainsMono-Regular.ttf")

    icon_url = _file_url(_ICONS_DIR / "sportsbook.png")

    # ── Build HTML ────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  @font-face {{
    font-family: 'Outfit';
    src: url('{font_outfit_semibold}') format('truetype');
    font-weight: 600;
  }}
  @font-face {{
    font-family: 'Outfit';
    src: url('{font_outfit_bold}') format('truetype');
    font-weight: 700;
  }}
  @font-face {{
    font-family: 'Outfit';
    src: url('{font_outfit_extrabold}') format('truetype');
    font-weight: 800;
  }}
  @font-face {{
    font-family: 'JetBrains Mono';
    src: url('{font_jbm_regular}') format('truetype');
    font-weight: 400;
  }}
  @font-face {{
    font-family: 'JetBrains Mono';
    src: url('{font_jbm_bold}') format('truetype');
    font-weight: 600;
  }}
  @font-face {{
    font-family: 'JetBrains Mono';
    src: url('{font_jbm_bold}') format('truetype');
    font-weight: 700;
  }}
  @font-face {{
    font-family: 'JetBrains Mono';
    src: url('{font_jbm_extrabold}') format('truetype');
    font-weight: 800;
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    margin: 0; padding: 0;
    background: transparent;
    display: inline-block;
    font-family: 'Outfit', sans-serif;
  }}

  .card {{
    position: relative;
    width: 700px;
    border-radius: 20px;
    overflow: hidden;
    border: 1.5px solid rgba(212, 175, 55, 0.18);
  }}
  .card::before {{
    content: '';
    position: absolute; inset: 0;
    background: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='256' height='256' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E");
    pointer-events: none; z-index: 1;
  }}
  .inner {{ position: relative; z-index: 2; background: #111; }}

  /* Status bar */
  .sbar {{ height: 5px; }}
  .sbar.pos {{ background: linear-gradient(90deg, #4ADE80, #22C55E, #4ADE80); }}
  .sbar.neg {{ background: linear-gradient(90deg, #F87171, #EF4444, #F87171); }}
  .sbar.top {{ background: linear-gradient(90deg, #D4AF37, #F0D060, #D4AF37); }}

  .sep {{ height: 1px; background: rgba(255,255,255,0.04); margin: 0 36px; }}
  .sep-gold {{ height: 1px; background: linear-gradient(90deg, transparent, rgba(212,175,55,0.2) 15%, rgba(212,175,55,0.2) 85%, transparent); }}

  /* Header */
  .hdr {{
    display: flex; align-items: center;
    padding: 24px 36px 20px; gap: 16px;
  }}
  .hdr-logo {{
    width: 56px; height: 56px;
    border-radius: 14px;
    overflow: hidden;
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    background: #0a0a0a;
    border: 1px solid rgba(212,175,55,0.15);
  }}
  .hdr-logo img {{
    width: 48px; height: 48px;
    object-fit: contain;
  }}
  .hdr-text {{ flex: 1; }}
  .hdr-title {{
    font-weight: 800; font-size: 26px; color: #fff;
    letter-spacing: 0.08em;
  }}
  .hdr-sub {{
    font-weight: 700; font-size: 14px;
    color: #D4AF37;
    letter-spacing: 0.16em; margin-top: 4px;
    opacity: 0.7;
  }}

  /* Hero balance */
  .hero-center {{
    padding: 32px 36px 12px;
    text-align: center;
  }}
  .hero-lbl {{
    font-weight: 700; font-size: 16px;
    color: #D4AF37;
    letter-spacing: 0.25em;
    margin-bottom: 8px;
    opacity: 0.65;
  }}
  .hero-amt {{
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800; font-size: 104px;
    color: #fff;
    line-height: 0.9; letter-spacing: -0.03em;
  }}
  .hero-amt .ds {{
    font-size: 64px; color: #D4AF37; font-weight: 700;
    vertical-align: super; margin-right: 2px;
  }}

  /* Rank + delta row */
  .rank-delta-row {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 20px;
    padding: 16px 36px 28px;
  }}
  .rank-pill {{
    display: flex; align-items: center; gap: 8px;
    background: rgba(212,175,55,0.06);
    border: 1px solid rgba(212,175,55,0.14);
    border-radius: 24px; padding: 10px 22px;
  }}
  .rank-pill.cold {{
    background: rgba(255,255,255,0.02);
    border-color: rgba(255,255,255,0.06);
  }}
  .rank-hash {{
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800; font-size: 28px; color: #D4AF37;
    line-height: 1;
  }}
  .rank-pill.cold .rank-hash {{ color: #666; }}
  .rank-lbl {{
    font-size: 10px; font-weight: 700; color: #D4AF37;
    letter-spacing: 0.16em; opacity: 0.6;
  }}
  .rank-pill.cold .rank-lbl {{ color: #555; }}
  .rank-of {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px; color: #555; font-weight: 600;
  }}

  .delta-pill {{
    display: inline-flex; align-items: center; gap: 6px;
    border-radius: 24px; padding: 10px 20px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 18px; font-weight: 700;
  }}
  .delta-pill.up {{
    background: rgba(74,222,128,0.07);
    border: 1px solid rgba(74,222,128,0.14);
    color: #4ADE80;
  }}
  .delta-pill.dn {{
    background: rgba(248,113,113,0.07);
    border: 1px solid rgba(248,113,113,0.14);
    color: #F87171;
  }}

  /* 2x2 stat grid */
  .stat-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2px;
    margin: 0 36px;
    border-radius: 16px;
    overflow: hidden;
  }}
  .sg-cell {{
    background: rgba(255,255,255,0.02);
    padding: 22px 28px;
    text-align: center;
  }}
  .sg-cell:nth-child(1) {{ border-radius: 16px 0 0 0; }}
  .sg-cell:nth-child(2) {{ border-radius: 0 16px 0 0; }}
  .sg-cell:nth-child(3) {{ border-radius: 0 0 0 16px; }}
  .sg-cell:nth-child(4) {{ border-radius: 0 0 16px 0; }}
  .sg-lbl {{
    font-weight: 700; font-size: 13px;
    color: #D4AF37;
    letter-spacing: 0.18em;
    margin-bottom: 8px;
    opacity: 0.55;
  }}
  .sg-val {{
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800; font-size: 34px;
    color: #fff; line-height: 1;
  }}
  .sg-val.g {{ color: #4ADE80; }}
  .sg-val.r {{ color: #F87171; }}

  /* Footer */
  .foot {{
    background: rgba(0,0,0,0.28);
    padding: 14px 36px;
    display: flex; align-items: center;
    justify-content: space-between;
    margin-top: 24px;
  }}
  .foot-txt {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; color: #262626; font-weight: 600;
    letter-spacing: 0.05em;
  }}
</style>
</head>
<body>

<div class="card">
  <div class="sbar {sbar_class}"></div>
  <div class="inner">
    <div class="hdr">
      <div class="hdr-logo">
        <img src="{icon_url}" width="48" height="48" />
      </div>
      <div class="hdr-text">
        <div class="hdr-title">FLOW SPORTSBOOK</div>
        <div class="hdr-sub">SEASON {season} &middot; WEEK {week}</div>
      </div>
    </div>
    <div class="sep-gold"></div>

    <div class="hero-center">
      <div class="hero-lbl">YOUR BALANCE</div>
      <div class="hero-amt"><span class="ds">$</span>{balance_str}</div>
    </div>

    <div class="rank-delta-row">
      <div class="rank-pill{rank_cold}">
        <div class="rank-hash">#{rank}</div>
        <div class="rank-info">
          <div class="rank-lbl">RANK</div>
          <div class="rank-of">of {total_users}</div>
        </div>
      </div>
      <div class="delta-pill {delta_class}">{delta_text}</div>
    </div>

    <div class="sep"></div>
    <div style="height: 20px;"></div>

    <div class="stat-grid">
      <div class="sg-cell">
        <div class="sg-lbl">RECORD</div>
        <div class="sg-val">{record_str}</div>
      </div>
      <div class="sg-cell">
        <div class="sg-lbl">WIN RATE</div>
        <div class="sg-val {win_rate_class}">{win_rate_str}</div>
      </div>
      <div class="sg-cell">
        <div class="sg-lbl">ROI</div>
        <div class="sg-val {roi_class}">{roi_str}</div>
      </div>
      <div class="sg-cell">
        <div class="sg-lbl">STREAK</div>
        <div class="sg-val {streak_class}">{streak}</div>
      </div>
    </div>

    <div class="foot">
      <div class="foot-txt">ATLAS&trade; &middot; FLOW</div>
      <div class="foot-txt">THE SIMULATION LEAGUE</div>
    </div>
  </div>
</div>

</body>
</html>"""

    return html
