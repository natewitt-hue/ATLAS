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

from datetime import datetime, timezone

from atlas_html_engine import render_card, wrap_card, esc


# ── Shared helpers ────────────────────────────────────────────────────────────

def _now_ts() -> str:
    """Return a human-readable UTC timestamp for the card footer."""
    return datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")


def _commentary_html(commentary: str) -> str:
    """Render the optional ATLAS-voiced one-liner commentary block."""
    if not commentary:
        return ""
    return f"""
<div style="
    margin:var(--space-md) 0 0;
    padding:10px 14px;
    border-radius:var(--border-radius);
    background:rgba(255,255,255,0.03);
    border-left:3px solid rgba(212,175,55,0.5);
">
  <div style="
      font-size:12px;
      font-style:italic;
      color:var(--text-warm);
      line-height:1.45;
  ">{esc(commentary)}</div>
</div>"""


def _footer_html(player: str) -> str:
    """Standard card footer: player name + timestamp."""
    return f"""
<div style="
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding-top:10px;
    margin-top:var(--space-md);
    border-top:1px solid rgba(255,255,255,0.06);
">
  <div style="
      font-size:12px;
      font-weight:600;
      color:var(--push);
      font-family:var(--font-display),sans-serif;
  ">{esc(player)}</div>
  <div style="
      font-size:var(--font-xs);
      color:var(--text-warm-dim);
      font-family:var(--font-mono),monospace;
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
    theme_id: str | None = None,
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
    """
    commentary_block = _commentary_html(commentary)
    footer_block = _footer_html(player)

    inner_html = f"""
  <!-- 5px status bar -->
  <div style="height:5px;width:100%;{status_bar_css}"></div>

  <div style="padding:18px 22px;">

    <!-- ── Header ──────────────────────────────────────────────────── -->
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:var(--space-lg);">
      <div style="
          width:36px;height:36px;border-radius:9px;
          background:{icon_bg};
          display:flex;align-items:center;justify-content:center;
          font-size:var(--font-lg);flex-shrink:0;
      ">{icon_emoji}</div>
      <div>
        <div style="font-size:var(--font-lg);font-weight:800;letter-spacing:1px;color:var(--text-primary);">
          {esc(game_label)}
        </div>
        <div style="font-size:10px;color:var(--text-warm-dim);font-family:var(--font-mono),monospace;letter-spacing:1.5px;">
          {esc(event_label)}
        </div>
      </div>
    </div>

    <!-- ── Body ────────────────────────────────────────────────────── -->
    {body_html}

    {commentary_block}
    {footer_block}

  </div><!-- /inner padding -->
"""
    return wrap_card(inner_html, theme_id=theme_id)


# ── Jackpot card ──────────────────────────────────────────────────────────────

def _build_jackpot_html(player: str, amount: int, multiplier: float, commentary: str, theme_id: str | None = None) -> str:
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

  <div style="font-size:var(--font-xs);font-weight:700;color:var(--gold);letter-spacing:4px;
      text-transform:uppercase;margin-bottom:var(--space-sm);">JACKPOT HIT</div>

  <div style="
      font-family:var(--font-display),sans-serif;
      font-size:58px;
      font-weight:800;
      background:linear-gradient(180deg,var(--gold-light),var(--gold));
      -webkit-background-clip:text;
      -webkit-text-fill-color:transparent;
      background-clip:text;
      filter:drop-shadow(0 2px 10px rgba(212,175,55,0.45));
      line-height:1.05;
      margin-bottom:6px;
  ">{esc(amount_str)}</div>

  <div style="
      display:inline-block;
      padding:var(--space-xs) 14px;
      border-radius:20px;
      background:rgba(212,175,55,0.15);
      border:1px solid rgba(212,175,55,0.35);
      font-family:var(--font-mono),monospace;
      font-size:var(--font-base);
      font-weight:700;
      color:var(--gold-light);
      margin-bottom:10px;
  ">{esc(mult_str)} multiplier</div>

  <div style="font-size:14px;color:var(--text-warm);margin-top:var(--space-xs);">
    <span style="color:var(--push);font-weight:700;">{esc(player)}</span> hit the progressive jackpot
  </div>
</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,var(--gold),var(--gold-light),var(--gold));",
        icon_emoji="&#127881;",
        icon_bg="linear-gradient(135deg,var(--gold),var(--gold-light))",
        game_label="PROGRESSIVE JACKPOT",
        event_label="JACKPOT HIT · FLOW CASINO",
        body_html=body,
        commentary=commentary,
        player=player,
        theme_id=theme_id,
    )


async def render_jackpot_card(
    player: str,
    amount: int,
    multiplier: float,
    commentary: str = "",
    theme_id: str | None = None,
) -> bytes:
    """Render a jackpot hit highlight card to PNG bytes."""
    doc = _build_jackpot_html(player, amount, multiplier, commentary, theme_id=theme_id)
    return await render_card(doc)


# ── PvP Coinflip card ─────────────────────────────────────────────────────────

def _build_pvp_html(winner: str, loser: str, amount: int, commentary: str, theme_id: str | None = None) -> str:
    amount_str = f"${amount:,}"

    body = f"""
<div style="
    border-radius:12px;
    padding:20px;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.08);
">
  <!-- VS matchup row -->
  <div style="display:flex;align-items:center;justify-content:space-between;gap:var(--space-md);">

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
          color:var(--win);
          letter-spacing:1.5px;
          margin-bottom:var(--space-sm);
      ">WINNER</div>
      <div style="
          font-size:var(--font-xl);
          font-weight:800;
          color:var(--text-primary);
          line-height:1.2;
          word-break:break-word;
      ">{esc(winner)}</div>
      <div style="
          font-family:var(--font-mono),monospace;
          font-size:20px;
          font-weight:700;
          color:var(--win);
          margin-top:6px;
      ">+{esc(amount_str)}</div>
    </div>

    <!-- VS divider -->
    <div style="
        flex-shrink:0;
        width:44px;height:44px;
        border-radius:50%;
        background:rgba(255,255,255,0.06);
        border:1px solid rgba(255,255,255,0.12);
        display:flex;align-items:center;justify-content:center;
        font-size:var(--font-sm);font-weight:800;color:var(--text-warm-dim);
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
          color:var(--loss);
          letter-spacing:1.5px;
          margin-bottom:var(--space-sm);
      ">ELIMINATED</div>
      <div style="
          font-size:var(--font-xl);
          font-weight:800;
          color:var(--text-warm-dim);
          line-height:1.2;
          word-break:break-word;
      ">{esc(loser)}</div>
      <div style="
          font-family:var(--font-mono),monospace;
          font-size:20px;
          font-weight:700;
          color:var(--loss);
          margin-top:6px;
      ">-{esc(amount_str)}</div>
    </div>

  </div>

  <!-- Pot total -->
  <div style="
      margin-top:var(--space-lg);
      text-align:center;
      padding:var(--space-sm) 14px;
      border-radius:var(--border-radius);
      background:rgba(212,175,55,0.07);
      border:1px solid rgba(212,175,55,0.15);
      font-size:var(--font-sm);
      color:var(--text-warm);
      font-family:var(--font-mono),monospace;
  ">Pot: <span style="color:var(--gold);font-weight:700;">{esc(amount_str)} each</span> &middot; Total moved: <span style="color:var(--gold);font-weight:700;">{esc(f"${amount * 2:,}")}</span></div>

</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,var(--win),#22d86e,var(--win));",
        icon_emoji="&#129689;",
        icon_bg="linear-gradient(135deg,var(--win),#22d86e)",
        game_label="PvP COINFLIP",
        event_label="HEAD-TO-HEAD RESULT · FLOW CASINO",
        body_html=body,
        commentary=commentary,
        player=winner,
        theme_id=theme_id,
    )


async def render_pvp_card(
    winner: str,
    loser: str,
    amount: int,
    commentary: str = "",
    theme_id: str | None = None,
) -> bytes:
    """Render a PvP coinflip result highlight card to PNG bytes."""
    doc = _build_pvp_html(winner, loser, amount, commentary, theme_id=theme_id)
    return await render_card(doc)


# ── Crash Last Man Standing card ──────────────────────────────────────────────

def _build_crash_lms_html(player: str, multiplier: float, payout: int, commentary: str, theme_id: str | None = None) -> str:
    mult_str = f"{multiplier:,.2f}x"
    payout_str = f"${payout:,}"

    # Multiplier color: green for high, yellow for mid, red-ish for low
    if multiplier >= 10.0:
        mult_color = "var(--gold-light)"
        mult_glow = "rgba(212,175,55,0.45)"
    elif multiplier >= 3.0:
        mult_color = "var(--win)"
        mult_glow = "rgba(74,222,128,0.35)"
    else:
        mult_color = "var(--push)"
        mult_glow = "rgba(251,191,36,0.30)"

    body = f"""
<div style="
    border-radius:12px;
    padding:20px;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.08);
    text-align:center;
">
  <div style="font-size:var(--font-xs);font-weight:700;color:var(--loss);letter-spacing:3px;
      text-transform:uppercase;margin-bottom:var(--space-md);">Last Man Standing</div>

  <!-- Big multiplier -->
  <div style="
      font-family:var(--font-display),sans-serif;
      font-size:72px;
      font-weight:800;
      color:{mult_color};
      filter:drop-shadow(0 2px 12px {mult_glow});
      line-height:1.0;
      margin-bottom:var(--space-xs);
  ">{esc(mult_str)}</div>

  <div style="font-size:var(--font-sm);color:var(--text-warm-dim);font-family:var(--font-mono),monospace;
      letter-spacing:1px;margin-bottom:var(--space-lg);">CRASH MULTIPLIER</div>

  <!-- Payout pill -->
  <div style="
      display:inline-block;
      padding:var(--space-sm) var(--space-xl);
      border-radius:10px;
      background:rgba(74,222,128,0.12);
      border:1px solid rgba(74,222,128,0.3);
      font-family:var(--font-mono),monospace;
      font-size:22px;
      font-weight:800;
      color:var(--win);
  ">+{esc(payout_str)}</div>

  <div style="font-size:14px;color:var(--text-warm);margin-top:14px;">
    <span style="color:var(--push);font-weight:700;">{esc(player)}</span> rode it out solo — last one alive
  </div>
</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,var(--loss),#ff4d4d,var(--loss));",
        icon_emoji="&#128293;",
        icon_bg="linear-gradient(135deg,var(--loss),#ff4d4d)",
        game_label="CRASH",
        event_label="LAST MAN STANDING · FLOW CASINO",
        body_html=body,
        commentary=commentary,
        player=player,
        theme_id=theme_id,
    )


async def render_crash_lms_card(
    player: str,
    multiplier: float,
    payout: int,
    commentary: str = "",
    theme_id: str | None = None,
) -> bytes:
    """Render a Crash Last Man Standing highlight card to PNG bytes."""
    doc = _build_crash_lms_html(player, multiplier, payout, commentary, theme_id=theme_id)
    return await render_card(doc)


# ── Prediction market resolution card ────────────────────────────────────────

def _build_prediction_html(
    title: str,
    resolution: str,
    winners: int,
    payout: int,
    commentary: str,
    theme_id: str | None = None,
) -> str:
    payout_str = f"${payout:,}"
    is_yes = resolution.upper().startswith("Y")
    res_label = "YES" if is_yes else "NO"
    res_color = "var(--win)" if is_yes else "var(--loss)"
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
      color:var(--text-primary);
      line-height:1.35;
      margin-bottom:14px;
      padding-bottom:var(--space-md);
      border-bottom:1px solid rgba(255,255,255,0.07);
  ">{esc(title)}</div>

  <!-- Resolution row -->
  <div style="display:flex;align-items:center;gap:var(--space-lg);margin-bottom:var(--space-lg);">
    <div style="
        padding:10px 22px;
        border-radius:10px;
        background:{res_bg};
        border:1px solid {res_border};
        font-family:var(--font-mono),monospace;
        font-size:26px;
        font-weight:800;
        color:{res_color};
        flex-shrink:0;
    ">{res_label}</div>
    <div>
      <div style="font-size:var(--font-xs);color:var(--text-warm-dim);font-family:var(--font-mono),monospace;
          letter-spacing:1px;margin-bottom:3px;">RESOLVED</div>
      <div style="font-size:var(--font-sm);color:var(--text-warm);">
        <span style="color:var(--text-primary);font-weight:600;">{esc(winner_text)}</span> share the pot
      </div>
    </div>
  </div>

  <!-- Total payout pill -->
  <div style="
      display:flex;
      align-items:center;
      justify-content:space-between;
      padding:10px 14px;
      border-radius:var(--border-radius);
      background:rgba(212,175,55,0.07);
      border:1px solid rgba(212,175,55,0.18);
  ">
    <div style="font-size:12px;color:var(--text-warm-dim);font-family:var(--font-mono),monospace;letter-spacing:0.5px;">
      TOTAL PAYOUT
    </div>
    <div style="
        font-family:var(--font-mono),monospace;
        font-size:20px;
        font-weight:800;
        color:var(--gold);
    ">{esc(payout_str)}</div>
  </div>

</div>"""

    return _wrap_card(
        status_bar_css=f"background:{res_color};",
        icon_emoji="&#128202;",
        icon_bg="linear-gradient(135deg,var(--pink),#c044a0)",
        game_label="PREDICTION MARKET",
        event_label="MARKET RESOLVED · FLOW PREDICTIONS",
        body_html=body,
        commentary=commentary,
        player=f"{winners:,} winner{'s' if winners != 1 else ''}",
        theme_id=theme_id,
    )


async def render_prediction_card(
    title: str,
    resolution: str,
    winners: int,
    payout: int,
    commentary: str = "",
    theme_id: str | None = None,
) -> bytes:
    """Render a prediction market resolution highlight card to PNG bytes."""
    doc = _build_prediction_html(title, resolution, winners, payout, commentary, theme_id=theme_id)
    return await render_card(doc)


# ── Sportsbook parlay card ────────────────────────────────────────────────────

def _format_leg_label(lp: dict) -> str:
    """Format a single leg pick for display: 'KC -3', 'LAC ML', 'O45.5'."""
    bt = lp["bet_type"]
    if bt in ("Over", "Under"):
        return f"{bt[0]}{lp['line']:g}" if lp["line"] else bt
    if bt == "Moneyline":
        return f"{lp['pick']} ML"
    # Spread
    line = lp["line"]
    if line:
        return f"{lp['pick']} {line:+g}"
    return lp["pick"]


def _build_parlay_html(
    player: str,
    legs: int,
    odds: str,
    payout: int,
    commentary: str,
    leg_picks: list[dict] | None = None,
    theme_id: str | None = None,
) -> str:
    payout_str = f"${payout:,}"

    # Build leg picks strip if available
    picks_html = ""
    if leg_picks:
        pills = []
        for lp in leg_picks:
            icon = {"Won": "&#10003;", "Lost": "&#10007;", "Push": "&#8212;"}.get(lp["status"], "&#10003;")
            color = {"Won": "var(--win)", "Lost": "var(--loss-dark)", "Push": "var(--text-warm-dim)"}.get(lp["status"], "var(--win)")
            border_c = {"Won": "rgba(74,222,128,0.2)", "Lost": "rgba(239,68,68,0.2)", "Push": "rgba(154,146,128,0.2)"}.get(lp["status"], "rgba(74,222,128,0.2)")
            label = esc(_format_leg_label(lp))
            pills.append(
                f'<span style="display:inline-block;padding:4px 10px;margin:3px;'
                f'border-radius:6px;background:rgba(255,255,255,0.05);'
                f'border:1px solid {border_c};'
                f'font-family:var(--font-mono),monospace;font-size:var(--font-xs);'
                f'color:{color};font-weight:600;letter-spacing:0.5px;">'
                f'{label} {icon}</span>'
            )
        picks_html = f"""
  <!-- Leg picks -->
  <div style="text-align:center;margin-bottom:var(--space-lg);">
    <div style="font-size:10px;color:var(--text-warm-dim);font-family:var(--font-mono),monospace;
        letter-spacing:1px;margin-bottom:var(--space-xs);">PICKS</div>
    <div style="display:flex;flex-wrap:wrap;justify-content:center;">
      {"".join(pills)}
    </div>
  </div>
"""

    body = f"""
<div style="
    border-radius:12px;
    padding:20px;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.08);
">

  <!-- Legs + odds row -->
  <div style="display:flex;gap:10px;margin-bottom:var(--space-lg);">

    <!-- Leg count -->
    <div style="
        flex:1;text-align:center;
        padding:14px;
        border-radius:10px;
        background:rgba(74,222,128,0.08);
        border:1px solid rgba(74,222,128,0.2);
    ">
      <div style="font-size:var(--font-xs);color:var(--text-warm-dim);font-family:var(--font-mono),monospace;
          letter-spacing:1px;margin-bottom:var(--space-xs);">LEGS</div>
      <div style="
          font-family:var(--font-mono),monospace;
          font-size:40px;
          font-weight:800;
          color:var(--win);
          line-height:1.0;
      ">{legs}</div>
      <div style="font-size:var(--font-xs);color:var(--text-warm-dim);margin-top:var(--space-xs);">ALL HIT</div>
    </div>

    <!-- Odds -->
    <div style="
        flex:1;text-align:center;
        padding:14px;
        border-radius:10px;
        background:rgba(212,175,55,0.08);
        border:1px solid rgba(212,175,55,0.2);
    ">
      <div style="font-size:var(--font-xs);color:var(--text-warm-dim);font-family:var(--font-mono),monospace;
          letter-spacing:1px;margin-bottom:var(--space-xs);">ODDS</div>
      <div style="
          font-family:var(--font-mono),monospace;
          font-size:32px;
          font-weight:800;
          color:var(--gold);
          line-height:1.0;
      ">{esc(odds)}</div>
      <div style="font-size:var(--font-xs);color:var(--text-warm-dim);margin-top:var(--space-xs);">AMERICAN</div>
    </div>

  </div>

  {picks_html}

  <!-- Payout hero -->
  <div style="
      text-align:center;
      padding:var(--space-lg);
      border-radius:10px;
      background:linear-gradient(135deg,rgba(74,222,128,0.12),rgba(74,222,128,0.04));
      border:1px solid rgba(74,222,128,0.25);
      position:relative;
      overflow:hidden;
  ">
    <div style="position:absolute;top:0;left:0;right:0;height:1px;
        background:linear-gradient(90deg,transparent,rgba(74,222,128,0.45),transparent);"></div>
    <div style="font-size:var(--font-xs);color:var(--text-warm-dim);font-family:var(--font-mono),monospace;
        letter-spacing:2px;margin-bottom:6px;">TOTAL PAYOUT</div>
    <div style="
        font-family:var(--font-display),sans-serif;
        font-size:46px;
        font-weight:800;
        color:var(--win);
        filter:drop-shadow(0 2px var(--space-sm) rgba(74,222,128,0.35));
        line-height:1.0;
    ">+{esc(payout_str)}</div>
    <div style="font-size:var(--font-sm);color:var(--text-warm);margin-top:var(--space-sm);">
      <span style="color:var(--push);font-weight:700;">{esc(player)}</span> cashed a {legs}-leg parlay
    </div>
  </div>

</div>"""

    return _wrap_card(
        status_bar_css="background:linear-gradient(90deg,var(--win),#22d86e,var(--win));",
        icon_emoji="&#127936;",
        icon_bg="linear-gradient(135deg,var(--win),#22d86e)",
        game_label="SPORTSBOOK PARLAY",
        event_label=f"{legs}-LEG PARLAY HIT · FLOW SPORTSBOOK",
        body_html=body,
        commentary=commentary,
        player=player,
        theme_id=theme_id,
    )


async def render_parlay_card(
    player: str,
    legs: int,
    odds: str,
    payout: int,
    commentary: str = "",
    leg_picks: list[dict] | None = None,
    theme_id: str | None = None,
) -> bytes:
    """Render a sportsbook parlay hit highlight card to PNG bytes."""
    doc = _build_parlay_html(player, legs, odds, payout, commentary, leg_picks=leg_picks, theme_id=theme_id)
    return await render_card(doc)
