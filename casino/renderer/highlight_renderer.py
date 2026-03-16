"""
highlight_renderer.py — ATLAS FLOW · Highlight Broadcast Card Renderer
──────────────────────────────────────────────────────────────────────────────
Renders individual V6-styled PNG cards for instant highlights posted to
#flow-live:
  - Jackpot hits (gold themed, big number)
  - PvP flip results (matchup card with winner/loser)
  - Crash Last Man Standing (multiplier showcase)
  - Prediction market resolutions (market title + YES/NO result)
  - Sportsbook parlay hits (legs + total payout)

V6 design language: dark bg, gold accents, Outfit + JetBrains Mono,
noise texture, glass-morphism cells.

Usage:
    from casino.renderer.highlight_renderer import render_jackpot_card
    png_bytes = await render_jackpot_card(player="Nate", amount=50000, multiplier=50.0)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import html as html_mod
from datetime import datetime


# ── Shared helpers ────────────────────────────────────────────────────────────

def _esc(text) -> str:
    return html_mod.escape(str(text))


_NOISE_SVG = (
    "data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E"
    "%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' "
    "numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E"
    "%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"
)


def _now_ts() -> str:
    """Return a human-readable UTC timestamp for the card footer."""
    return datetime.utcnow().strftime("%b %d, %Y · %H:%M UTC")


def _commentary_html(commentary: str) -> str:
    """Render the optional ATLAS-voiced one-liner commentary block."""
    if not commentary:
        return ""
    return f"""
<div style="
    margin:12px 0 0;
    padding:10px 14px;
    border-radius:8px;
    background:rgba(255,255,255,0.03);
    border-left:3px solid rgba(212,175,55,0.5);
">
  <div style="
      font-size:12px;
      font-style:italic;
      color:#b0a890;
      line-height:1.45;
  ">{_esc(commentary)}</div>
</div>"""


def _footer_html(player: str) -> str:
    """Standard card footer: player name + timestamp."""
    return f"""
<div style="
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding-top:10px;
    margin-top:12px;
    border-top:1px solid rgba(255,255,255,0.06);
">
  <div style="
      font-size:12px;
      font-weight:600;
      color:#FBBF24;
      font-family:'Outfit',sans-serif;
  ">{_esc(player)}</div>
  <div style="
      font-size:11px;
      color:#9a9280;
      font-family:'JetBrains Mono',monospace;
      letter-spacing:0.5px;
  ">{_now_ts()}</div>
</div>"""


def _wrap_card(
    status_bar_css: str,
    icon_emoji: str,
    icon_bg: str,
    game_label: str,
    event_label: str,
    body_html: str,
    commentary: str,
    player: str,
    font_css: str,
) -> str:
    """Build a full HTML document around the card shell.

    Args:
        status_bar_css: inline CSS for the 5px top bar (background property).
        icon_emoji: HTML entity string for the icon pill, e.g. '&#127921;'.
        icon_bg: CSS color/gradient for the icon pill background.
        game_label: short game name shown in the header (e.g. 'SLOTS').
        event_label: sub-label shown under game name (e.g. 'JACKPOT HIT').
        body_html: the event-specific HTML content block.
        commentary: ATLAS one-liner (may be empty).
        player: player display name for the footer.
        font_css: @font-face declarations from _font_face_css().
    """
    commentary_block = _commentary_html(commentary)
    footer_block = _footer_html(player)

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

  <!-- 5px status bar -->
  <div style="height:5px;width:100%;{status_bar_css}position:relative;z-index:2;"></div>

  <div style="position:relative;z-index:2;padding:18px 22px;">

    <!-- ── Header ──────────────────────────────────────────────────── -->
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
      <div style="
          width:36px;height:36px;border-radius:9px;
          background:{icon_bg};
          display:flex;align-items:center;justify-content:center;
          font-size:18px;flex-shrink:0;
      ">{icon_emoji}</div>
      <div>
        <div style="font-size:18px;font-weight:800;letter-spacing:1px;color:#e8e0d0;">
          {_esc(game_label)}
        </div>
        <div style="font-size:10px;color:#9a9280;font-family:'JetBrains Mono',monospace;letter-spacing:1.5px;">
          {_esc(event_label)}
        </div>
      </div>
    </div>

    <!-- ── Body ────────────────────────────────────────────────────── -->
    {body_html}

    {commentary_block}
    {footer_block}

  </div><!-- /inner padding -->
</div><!-- /card -->
</body>
</html>"""


# ── Jackpot card ──────────────────────────────────────────────────────────────

def _build_jackpot_html(player: str, amount: int, multiplier: float, commentary: str, font_css: str) -> str:
    amount_str = f"${amount:,}"
    mult_str = f"{multiplier:,.1f}x"

    body = f"""
<div style="
    border-radius:12px;
    padding:22px 20px;
    background:linear-gradient(135deg,rgba(212,175,55,0.14),rgba(212,175,55,0.05));
    border:1px solid rgba(212,175,55,0.30);
    text-align:center;
    position:relative;
    overflow:hidden;
">
  <!-- shimmer lines -->
  <div style="position:absolute;top:0;left:0;right:0;height:1px;
      background:linear-gradient(90deg,transparent,rgba(212,175,55,0.5),transparent);"></div>
  <div style="position:absolute;bottom:0;left:0;right:0;height:1px;
      background:linear-gradient(90deg,transparent,rgba(212,175,55,0.25),transparent);"></div>

  <div style="font-size:11px;font-weight:700;color:#D4AF37;letter-spacing:4px;
      text-transform:uppercase;margin-bottom:8px;">JACKPOT HIT</div>

  <div style="
      font-family:'Outfit',sans-serif;
      font-size:58px;
      font-weight:800;
      background:linear-gradient(180deg,#FFDA50,#D4AF37);
      -webkit-background-clip:text;
      -webkit-text-fill-color:transparent;
      background-clip:text;
      filter:drop-shadow(0 2px 10px rgba(212,175,55,0.45));
      line-height:1.05;
      margin-bottom:6px;
  ">{_esc(amount_str)}</div>

  <div style="
      display:inline-block;
      padding:4px 14px;
      border-radius:20px;
      background:rgba(212,175,55,0.15);
      border:1px solid rgba(212,175,55,0.35);
      font-family:'JetBrains Mono',monospace;
      font-size:15px;
      font-weight:700;
      color:#FFDA50;
      margin-bottom:10px;
  ">{_esc(mult_str)} multiplier</div>

  <div style="font-size:14px;color:#b0a890;margin-top:4px;">
    <span style="color:#FBBF24;font-weight:700;">{_esc(player)}</span> hit the progressive jackpot
  </div>
</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,#D4AF37,#FFDA50,#D4AF37);",
        icon_emoji="&#127881;",
        icon_bg="linear-gradient(135deg,#D4AF37,#FFDA50)",
        game_label="PROGRESSIVE JACKPOT",
        event_label="JACKPOT HIT · FLOW CASINO",
        body_html=body,
        commentary=commentary,
        player=player,
        font_css=font_css,
    )


async def render_jackpot_card(
    player: str,
    amount: int,
    multiplier: float,
    commentary: str = "",
) -> bytes:
    """Render a jackpot hit highlight card to PNG bytes."""
    from casino.renderer.casino_html_renderer import _font_face_css, _render_card_html

    font_css = _font_face_css()
    doc = _build_jackpot_html(player, amount, multiplier, commentary, font_css)
    return await _render_card_html(doc, width=732)


# ── PvP Coinflip card ─────────────────────────────────────────────────────────

def _build_pvp_html(winner: str, loser: str, amount: int, commentary: str, font_css: str) -> str:
    amount_str = f"${amount:,}"

    body = f"""
<div style="
    border-radius:12px;
    padding:20px;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.08);
">
  <!-- VS matchup row -->
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">

    <!-- Winner -->
    <div style="flex:1;text-align:center;">
      <div style="
          display:inline-block;
          padding:3px 10px;
          border-radius:20px;
          background:rgba(74,222,128,0.15);
          border:1px solid rgba(74,222,128,0.4);
          font-size:10px;
          font-weight:700;
          color:#4ADE80;
          letter-spacing:1.5px;
          margin-bottom:8px;
      ">WINNER</div>
      <div style="
          font-size:24px;
          font-weight:800;
          color:#e8e0d0;
          line-height:1.2;
          word-break:break-word;
      ">{_esc(winner)}</div>
      <div style="
          font-family:'JetBrains Mono',monospace;
          font-size:20px;
          font-weight:700;
          color:#4ADE80;
          margin-top:6px;
      ">+{_esc(amount_str)}</div>
    </div>

    <!-- VS divider -->
    <div style="
        flex-shrink:0;
        width:44px;height:44px;
        border-radius:50%;
        background:rgba(255,255,255,0.06);
        border:1px solid rgba(255,255,255,0.12);
        display:flex;align-items:center;justify-content:center;
        font-size:13px;font-weight:800;color:#9a9280;
    ">VS</div>

    <!-- Loser -->
    <div style="flex:1;text-align:center;">
      <div style="
          display:inline-block;
          padding:3px 10px;
          border-radius:20px;
          background:rgba(248,113,113,0.12);
          border:1px solid rgba(248,113,113,0.35);
          font-size:10px;
          font-weight:700;
          color:#F87171;
          letter-spacing:1.5px;
          margin-bottom:8px;
      ">ELIMINATED</div>
      <div style="
          font-size:24px;
          font-weight:800;
          color:#9a9280;
          line-height:1.2;
          word-break:break-word;
      ">{_esc(loser)}</div>
      <div style="
          font-family:'JetBrains Mono',monospace;
          font-size:20px;
          font-weight:700;
          color:#F87171;
          margin-top:6px;
      ">-{_esc(amount_str)}</div>
    </div>

  </div>

  <!-- Pot total -->
  <div style="
      margin-top:16px;
      text-align:center;
      padding:8px 14px;
      border-radius:8px;
      background:rgba(212,175,55,0.07);
      border:1px solid rgba(212,175,55,0.15);
      font-size:13px;
      color:#b0a890;
      font-family:'JetBrains Mono',monospace;
  ">Pot: <span style="color:#D4AF37;font-weight:700;">{_esc(amount_str)} each</span> &middot; Total moved: <span style="color:#D4AF37;font-weight:700;">{_esc(f"${amount * 2:,}")}</span></div>

</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,#4ADE80,#22d86e,#4ADE80);",
        icon_emoji="&#129689;",
        icon_bg="linear-gradient(135deg,#4ADE80,#22d86e)",
        game_label="PvP COINFLIP",
        event_label="HEAD-TO-HEAD RESULT · FLOW CASINO",
        body_html=body,
        commentary=commentary,
        player=winner,
        font_css=font_css,
    )


async def render_pvp_card(
    winner: str,
    loser: str,
    amount: int,
    commentary: str = "",
) -> bytes:
    """Render a PvP coinflip result highlight card to PNG bytes."""
    from casino.renderer.casino_html_renderer import _font_face_css, _render_card_html

    font_css = _font_face_css()
    doc = _build_pvp_html(winner, loser, amount, commentary, font_css)
    return await _render_card_html(doc, width=732)


# ── Crash Last Man Standing card ──────────────────────────────────────────────

def _build_crash_lms_html(player: str, multiplier: float, payout: int, commentary: str, font_css: str) -> str:
    mult_str = f"{multiplier:,.2f}x"
    payout_str = f"${payout:,}"

    # Multiplier color: green for high, yellow for mid, red-ish for low
    if multiplier >= 10.0:
        mult_color = "#FFDA50"
        mult_glow = "rgba(212,175,55,0.45)"
    elif multiplier >= 3.0:
        mult_color = "#4ADE80"
        mult_glow = "rgba(74,222,128,0.35)"
    else:
        mult_color = "#FBBF24"
        mult_glow = "rgba(251,191,36,0.30)"

    body = f"""
<div style="
    border-radius:12px;
    padding:20px;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.08);
    text-align:center;
">
  <div style="font-size:11px;font-weight:700;color:#F87171;letter-spacing:3px;
      text-transform:uppercase;margin-bottom:12px;">Last Man Standing</div>

  <!-- Big multiplier -->
  <div style="
      font-family:'Outfit',sans-serif;
      font-size:72px;
      font-weight:800;
      color:{mult_color};
      filter:drop-shadow(0 2px 12px {mult_glow});
      line-height:1.0;
      margin-bottom:4px;
  ">{_esc(mult_str)}</div>

  <div style="font-size:13px;color:#9a9280;font-family:'JetBrains Mono',monospace;
      letter-spacing:1px;margin-bottom:16px;">CRASH MULTIPLIER</div>

  <!-- Payout pill -->
  <div style="
      display:inline-block;
      padding:8px 24px;
      border-radius:10px;
      background:rgba(74,222,128,0.12);
      border:1px solid rgba(74,222,128,0.3);
      font-family:'JetBrains Mono',monospace;
      font-size:22px;
      font-weight:800;
      color:#4ADE80;
  ">+{_esc(payout_str)}</div>

  <div style="font-size:14px;color:#b0a890;margin-top:14px;">
    <span style="color:#FBBF24;font-weight:700;">{_esc(player)}</span> rode it out solo &mdash; last one alive
  </div>
</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,#F87171,#ff4d4d,#F87171);",
        icon_emoji="&#128293;",
        icon_bg="linear-gradient(135deg,#F87171,#ff4d4d)",
        game_label="CRASH",
        event_label="LAST MAN STANDING · FLOW CASINO",
        body_html=body,
        commentary=commentary,
        player=player,
        font_css=font_css,
    )


async def render_crash_lms_card(
    player: str,
    multiplier: float,
    payout: int,
    commentary: str = "",
) -> bytes:
    """Render a Crash Last Man Standing highlight card to PNG bytes."""
    from casino.renderer.casino_html_renderer import _font_face_css, _render_card_html

    font_css = _font_face_css()
    doc = _build_crash_lms_html(player, multiplier, payout, commentary, font_css)
    return await _render_card_html(doc, width=732)


# ── Prediction market resolution card ────────────────────────────────────────

def _build_prediction_html(
    title: str,
    resolution: str,
    winners: int,
    payout: int,
    commentary: str,
    font_css: str,
) -> str:
    payout_str = f"${payout:,}"
    is_yes = resolution.upper().startswith("Y")
    res_label = "YES" if is_yes else "NO"
    res_color = "#4ADE80" if is_yes else "#F87171"
    res_bg = "rgba(74,222,128,0.12)" if is_yes else "rgba(248,113,113,0.12)"
    res_border = "rgba(74,222,128,0.35)" if is_yes else "rgba(248,113,113,0.35)"

    winner_text = f"{winners:,} winner{'s' if winners != 1 else ''}"

    body = f"""
<div style="
    border-radius:12px;
    padding:20px;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.08);
">
  <!-- Market title -->
  <div style="
      font-size:17px;
      font-weight:700;
      color:#e8e0d0;
      line-height:1.35;
      margin-bottom:14px;
      padding-bottom:12px;
      border-bottom:1px solid rgba(255,255,255,0.07);
  ">{_esc(title)}</div>

  <!-- Resolution row -->
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;">
    <div style="
        padding:10px 22px;
        border-radius:10px;
        background:{res_bg};
        border:1px solid {res_border};
        font-family:'JetBrains Mono',monospace;
        font-size:26px;
        font-weight:800;
        color:{res_color};
        flex-shrink:0;
    ">{res_label}</div>
    <div>
      <div style="font-size:11px;color:#9a9280;font-family:'JetBrains Mono',monospace;
          letter-spacing:1px;margin-bottom:3px;">RESOLVED</div>
      <div style="font-size:13px;color:#b0a890;">
        <span style="color:#e8e0d0;font-weight:600;">{_esc(winner_text)}</span> share the pot
      </div>
    </div>
  </div>

  <!-- Total payout pill -->
  <div style="
      display:flex;
      align-items:center;
      justify-content:space-between;
      padding:10px 14px;
      border-radius:8px;
      background:rgba(212,175,55,0.07);
      border:1px solid rgba(212,175,55,0.18);
  ">
    <div style="font-size:12px;color:#9a9280;font-family:'JetBrains Mono',monospace;letter-spacing:0.5px;">
      TOTAL PAYOUT
    </div>
    <div style="
        font-family:'JetBrains Mono',monospace;
        font-size:20px;
        font-weight:800;
        color:#D4AF37;
    ">{_esc(payout_str)}</div>
  </div>

</div>"""

    return _wrap_card(
        status_bar_css=f"background:{res_color};",
        icon_emoji="&#128202;",
        icon_bg="linear-gradient(135deg,#F472B6,#c044a0)",
        game_label="PREDICTION MARKET",
        event_label="MARKET RESOLVED · FLOW PREDICTIONS",
        body_html=body,
        commentary=commentary,
        player=f"{winners:,} winner{'s' if winners != 1 else ''}",
        font_css=font_css,
    )


async def render_prediction_card(
    title: str,
    resolution: str,
    winners: int,
    payout: int,
    commentary: str = "",
) -> bytes:
    """Render a prediction market resolution highlight card to PNG bytes."""
    from casino.renderer.casino_html_renderer import _font_face_css, _render_card_html

    font_css = _font_face_css()
    doc = _build_prediction_html(title, resolution, winners, payout, commentary, font_css)
    return await _render_card_html(doc, width=732)


# ── Sportsbook parlay card ────────────────────────────────────────────────────

def _build_parlay_html(
    player: str,
    legs: int,
    odds: str,
    payout: int,
    commentary: str,
    font_css: str,
) -> str:
    payout_str = f"${payout:,}"

    body = f"""
<div style="
    border-radius:12px;
    padding:20px;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.08);
">

  <!-- Legs + odds row -->
  <div style="display:flex;gap:10px;margin-bottom:16px;">

    <!-- Leg count -->
    <div style="
        flex:1;text-align:center;
        padding:14px;
        border-radius:10px;
        background:rgba(74,222,128,0.08);
        border:1px solid rgba(74,222,128,0.2);
    ">
      <div style="font-size:11px;color:#9a9280;font-family:'JetBrains Mono',monospace;
          letter-spacing:1px;margin-bottom:4px;">LEGS</div>
      <div style="
          font-family:'JetBrains Mono',monospace;
          font-size:40px;
          font-weight:800;
          color:#4ADE80;
          line-height:1.0;
      ">{legs}</div>
      <div style="font-size:11px;color:#9a9280;margin-top:4px;">ALL HIT</div>
    </div>

    <!-- Odds -->
    <div style="
        flex:1;text-align:center;
        padding:14px;
        border-radius:10px;
        background:rgba(212,175,55,0.08);
        border:1px solid rgba(212,175,55,0.2);
    ">
      <div style="font-size:11px;color:#9a9280;font-family:'JetBrains Mono',monospace;
          letter-spacing:1px;margin-bottom:4px;">ODDS</div>
      <div style="
          font-family:'JetBrains Mono',monospace;
          font-size:32px;
          font-weight:800;
          color:#D4AF37;
          line-height:1.0;
      ">{_esc(odds)}</div>
      <div style="font-size:11px;color:#9a9280;margin-top:4px;">AMERICAN</div>
    </div>

  </div>

  <!-- Payout hero -->
  <div style="
      text-align:center;
      padding:16px;
      border-radius:10px;
      background:linear-gradient(135deg,rgba(74,222,128,0.12),rgba(74,222,128,0.04));
      border:1px solid rgba(74,222,128,0.25);
      position:relative;
      overflow:hidden;
  ">
    <div style="position:absolute;top:0;left:0;right:0;height:1px;
        background:linear-gradient(90deg,transparent,rgba(74,222,128,0.45),transparent);"></div>
    <div style="font-size:11px;color:#9a9280;font-family:'JetBrains Mono',monospace;
        letter-spacing:2px;margin-bottom:6px;">TOTAL PAYOUT</div>
    <div style="
        font-family:'Outfit',sans-serif;
        font-size:46px;
        font-weight:800;
        color:#4ADE80;
        filter:drop-shadow(0 2px 8px rgba(74,222,128,0.35));
        line-height:1.0;
    ">+{_esc(payout_str)}</div>
    <div style="font-size:13px;color:#b0a890;margin-top:8px;">
      <span style="color:#FBBF24;font-weight:700;">{_esc(player)}</span> cashed a {legs}-leg parlay
    </div>
  </div>

</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,#4ADE80,#22d86e,#4ADE80);",
        icon_emoji="&#127936;",
        icon_bg="linear-gradient(135deg,#4ADE80,#22d86e)",
        game_label="SPORTSBOOK PARLAY",
        event_label=f"{legs}-LEG PARLAY HIT · FLOW SPORTSBOOK",
        body_html=body,
        commentary=commentary,
        player=player,
        font_css=font_css,
    )


async def render_parlay_card(
    player: str,
    legs: int,
    odds: str,
    payout: int,
    commentary: str = "",
) -> bytes:
    """Render a sportsbook parlay hit highlight card to PNG bytes."""
    from casino.renderer.casino_html_renderer import _font_face_css, _render_card_html

    font_css = _font_face_css()
    doc = _build_parlay_html(player, legs, odds, payout, commentary, font_css)
    return await _render_card_html(doc, width=732)
