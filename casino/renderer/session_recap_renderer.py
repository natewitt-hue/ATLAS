"""
session_recap_renderer.py — ATLAS FLOW Casino · Session Recap Card Renderer
─────────────────────────────────────────────────────────────────────────────
Produces a V6-styled PNG card summarizing a player's gaming session.

Sections:
  - Header: player name + session duration
  - Hero P&L: large net profit/loss number
  - Stats row: games played | wins | losses | pushes
  - Game breakdown: per-game-type stats with icon pills
  - Highlight moments: top 3 notable events from the session
  - Streak badge (if applicable)
  - ATLAS commentary line

Usage:
    from casino.renderer.session_recap_renderer import render_session_recap
    png_bytes = await render_session_recap(session, display_name, commentary)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flow_live_cog import PlayerSession

from atlas_html_engine import render_card, wrap_card, esc

# ── Game type icon mapping ────────────────────────────────────────────────────

GAME_ICONS: dict[str, str] = {
    "blackjack": "&#127183;",
    "slots": "&#127920;",
    "crash": "&#128640;",
    "coinflip": "&#129689;",
    "coinflip_pvp": "&#9876;&#65039;",
}

GAME_LABELS: dict[str, str] = {
    "blackjack": "Blackjack",
    "slots": "Slots",
    "crash": "Crash",
    "coinflip": "Coinflip",
    "coinflip_pvp": "PvP Flip",
}


# ── Helper: format duration ───────────────────────────────────────────────────

def _format_duration(started_at: float, last_activity: float) -> str:
    """Format session duration as 'Xm' or 'Xh Ym'."""
    elapsed = max(0, int(last_activity - started_at))
    minutes = elapsed // 60
    hours = minutes // 60
    rem_min = minutes % 60
    if hours > 0:
        return f"{hours}h {rem_min}m" if rem_min else f"{hours}h"
    return f"{minutes}m" if minutes > 0 else "< 1m"


# ── Helper: format currency ───────────────────────────────────────────────────

def _fmt_pnl(amount: int) -> str:
    if amount > 0:
        return f"+${amount:,}"
    if amount < 0:
        return f"-${abs(amount):,}"
    return "$0"


# ── Helper: derive top highlight events ──────────────────────────────────────

def _build_highlight_events(session: PlayerSession) -> list[dict]:
    """
    Extract up to 3 notable game events for display.
    Priority: biggest absolute P&L first, then jackpots/special outcomes.
    Returns list of dicts with keys: icon, label, amount, color, note.
    """
    events = getattr(session, "events", [])
    if not events:
        return []

    # Score each event by notability
    scored: list[tuple[int, object]] = []
    for ev in events:
        net = getattr(ev, "net_profit", 0)
        score = abs(net)
        # Boost jackpots/specials
        extra = getattr(ev, "extra", {}) or {}
        if extra.get("jackpot"):
            score += 100_000
        if extra.get("blackjack"):
            score += 10_000
        if getattr(ev, "multiplier", 1.0) >= 5.0:
            score += 5_000
        scored.append((score, ev))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:3]

    highlights = []
    for _, ev in top:
        game_type = getattr(ev, "game_type", "unknown")
        net = getattr(ev, "net_profit", 0)
        multiplier = getattr(ev, "multiplier", 1.0)
        outcome = getattr(ev, "outcome", "")
        extra = getattr(ev, "extra", {}) or {}

        icon = GAME_ICONS.get(game_type, "&#127922;")
        label = GAME_LABELS.get(game_type, game_type.title())

        # Build note
        note = ""
        if extra.get("jackpot"):
            note = "JACKPOT"
        elif extra.get("blackjack"):
            note = "BLACKJACK"
        elif multiplier >= 10.0:
            note = f"{multiplier:.1f}x"
        elif multiplier >= 2.0:
            note = f"{multiplier:.1f}x"
        elif outcome == "push":
            note = "PUSH"

        color = "green" if net > 0 else "red" if net < 0 else "amber"
        highlights.append({
            "icon": icon,
            "label": label,
            "amount": _fmt_pnl(net),
            "color": color,
            "note": note,
        })

    return highlights


# ── HTML builder: session recap ───────────────────────────────────────────────

def _build_session_recap_html(
    session: PlayerSession,
    display_name: str,
    commentary: str = "",
    theme_id: str | None = None,
) -> str:
    """Build the full session recap card HTML."""

    # Status bar class
    if session.net_profit > 0:
        status_class = "win"
    elif session.net_profit < 0:
        status_class = "loss"
    else:
        status_class = "push"

    # P&L colors
    pnl_color = "green" if session.net_profit > 0 else "red" if session.net_profit < 0 else "amber"
    pnl_str = _fmt_pnl(session.net_profit)

    # Duration
    duration = _format_duration(session.started_at, session.last_activity)

    # Streak badge
    streak_html = ""
    if session.current_streak >= 3:
        s = session.current_streak
        if s >= 10:
            css_class, icon, label = "legendary", "&#128293;", f"W{s}"
        elif s >= 7:
            css_class, icon, label = "fire", "&#128293;&#128293;", f"W{s}"
        elif s >= 5:
            css_class, icon, label = "fire", "&#128293;", f"W{s}"
        else:
            css_class, icon, label = "hot", "&#128293;", f"W{s}"
        streak_html = f'<span class="streak-badge {css_class}">{icon} {label}</span>'
    elif session.current_streak <= -5:
        s = abs(session.current_streak)
        streak_html = f'<span class="streak-badge cold">&#10052;&#65039; L{s}</span>'

    # Best streak note
    best_streak_html = ""
    if session.best_streak >= 3:
        best_streak_html = f'<span class="best-streak-note">Best streak: W{session.best_streak}</span>'

    # Game breakdown pills
    game_breakdown_html = ""
    if session.games_by_type:
        pills = []
        for game_type, count in sorted(session.games_by_type.items(), key=lambda x: x[1], reverse=True):
            icon = GAME_ICONS.get(game_type, "&#127922;")
            label = GAME_LABELS.get(game_type, game_type.title())
            pills.append(
                f'<div class="game-pill">'
                f'  <span class="game-pill-icon">{icon}</span>'
                f'  <span class="game-pill-label">{esc(label)}</span>'
                f'  <span class="game-pill-count">{count}x</span>'
                f'</div>'
            )
        game_breakdown_html = f"""
    <div class="section-label">GAME BREAKDOWN</div>
    <div class="game-pills-row">
      {''.join(pills)}
    </div>"""

    # Highlight moments
    highlights = _build_highlight_events(session)
    highlights_html = ""
    if highlights:
        rows = []
        for h in highlights:
            note_html = f'<span class="hl-note">{esc(h["note"])}</span>' if h["note"] else ""
            rows.append(
                f'<div class="highlight-row">'
                f'  <span class="hl-icon">{h["icon"]}</span>'
                f'  <span class="hl-label">{esc(h["label"])}</span>'
                f'  {note_html}'
                f'  <span class="hl-amount {esc(h["color"])}">{esc(h["amount"])}</span>'
                f'</div>'
            )
        highlights_html = f"""
    <div class="section-label">HIGHLIGHTS</div>
    <div class="highlights-block">
      {''.join(rows)}
    </div>"""

    # Commentary line
    commentary_html = ""
    if commentary:
        commentary_html = f"""
    <div class="gold-divider"></div>
    <div class="commentary-line">{esc(commentary)}</div>"""

    # Win rate
    win_rate = 0
    if session.total_games > 0:
        win_rate = round(session.wins / session.total_games * 100)

    # Biggest win/loss cells (only show if nonzero)
    best_cells_html = ""
    if session.biggest_win or session.biggest_loss:
        bw_str = f"+${session.biggest_win:,}" if session.biggest_win else "—"
        bl_str = f"-${abs(session.biggest_loss):,}" if session.biggest_loss else "—"
        best_cells_html = f"""
      <div class="data-cell">
        <div class="data-label">BEST WIN</div>
        <div class="data-value green">{bw_str}</div>
      </div>
      <div class="data-cell">
        <div class="data-label">WORST LOSS</div>
        <div class="data-value red">{bl_str}</div>
      </div>"""

    body_html = f"""
<style>
/* Session recap component styles */
.sr-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px var(--space-sm);
}}
.sr-header-left {{
  display: flex;
  flex-direction: column;
  gap: 2px;
}}
.player-name {{
  font-family: var(--font-display), sans-serif;
  font-weight: 800;
  font-size: 20px;
  color: var(--text-primary);
  letter-spacing: 0.5px;
}}
.session-sub {{
  font-family: var(--font-mono), monospace;
  font-weight: 600;
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 1px;
  text-transform: uppercase;
}}
.sr-header-right {{
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-xs);
}}
.duration-badge {{
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: var(--font-xs);
  color: var(--gold);
  background: rgba(212,175,55,0.1);
  border: 1px solid rgba(212,175,55,0.25);
  border-radius: var(--border-radius);
  padding: 3px 10px;
  letter-spacing: 0.5px;
}}
/* Hero P&L */
.hero-pnl {{
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--space-md) 20px var(--space-sm);
}}
.hero-pnl-label {{
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: var(--font-xs);
  color: var(--gold-dim);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: var(--space-xs);
}}
.hero-pnl-value {{
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 48px;
  letter-spacing: -1px;
  line-height: 1;
}}
.hero-pnl-value.green {{ color: var(--win); }}
.hero-pnl-value.red   {{ color: var(--loss); }}
.hero-pnl-value.amber {{ color: var(--push); }}
/* Stats grid overrides */
.data-grid.cols4 {{ grid-template-columns: repeat(4, 1fr); }}
.data-grid.cols2 {{ grid-template-columns: repeat(2, 1fr); }}
.data-grid {{ padding: var(--space-sm) 20px 10px; }}
/* Section label */
.section-label {{
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 10px;
  color: var(--gold-dim);
  letter-spacing: 2px;
  text-transform: uppercase;
  padding: 2px 20px 6px;
}}
/* Game breakdown pills */
.game-pills-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 0 20px var(--space-md);
}}
.game-pill {{
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px var(--space-md);
  background: rgba(255,255,255,0.04);
  border-radius: 20px;
  border: 1px solid rgba(212,175,55,0.15);
}}
.game-pill-icon {{ font-size: 14px; }}
.game-pill-label {{
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 12px;
  color: var(--text-sub);
}}
.game-pill-count {{
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: var(--font-xs);
  color: var(--gold);
}}
/* Highlights block */
.highlights-block {{
  padding: 0 20px var(--space-md);
  display: flex;
  flex-direction: column;
  gap: 5px;
}}
.highlight-row {{
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  background: rgba(255,255,255,0.025);
  border-radius: var(--border-radius);
  padding: 7px var(--space-md);
  border-left: 2px solid rgba(212,175,55,0.2);
}}
.hl-icon {{ font-size: var(--font-base); flex-shrink: 0; }}
.hl-label {{
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: var(--font-sm);
  color: var(--text-sub);
  flex: 1;
}}
.hl-note {{
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: 10px;
  color: var(--gold);
  background: rgba(212,175,55,0.1);
  border-radius: var(--border-radius-sm);
  padding: 2px 6px;
  letter-spacing: 0.5px;
}}
.hl-amount {{
  font-family: var(--font-mono), monospace;
  font-weight: 800;
  font-size: 14px;
  flex-shrink: 0;
}}
.hl-amount.green {{ color: var(--win); }}
.hl-amount.red   {{ color: var(--loss); }}
.hl-amount.amber {{ color: var(--push); }}
/* Best streak note */
.best-streak-note {{
  font-family: var(--font-mono), monospace;
  font-weight: 600;
  font-size: 10px;
  color: var(--gold-dim);
  letter-spacing: 0.3px;
}}
/* Commentary line */
.commentary-line {{
  font-family: var(--font-display), sans-serif;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-muted);
  font-style: italic;
  text-align: center;
  padding: var(--space-sm) var(--space-xl) var(--space-md);
  line-height: 1.4;
}}
/* Win rate bar */
.win-rate-row {{
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px 10px;
}}
.win-rate-label {{
  font-family: var(--font-display), sans-serif;
  font-weight: 700;
  font-size: 10px;
  color: var(--gold-dim);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  white-space: nowrap;
}}
.win-rate-bar-bg {{
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: rgba(255,255,255,0.07);
  overflow: hidden;
}}
.win-rate-bar-fill {{
  height: 100%;
  border-radius: 3px;
  background: linear-gradient(90deg, #4ADE80, #22C55E);
}}
.win-rate-pct {{
  font-family: var(--font-mono), monospace;
  font-weight: 700;
  font-size: var(--font-xs);
  color: var(--win);
  white-space: nowrap;
}}
</style>

  <!-- Header -->
  <div class="sr-header">
    <div class="sr-header-left">
      <div class="player-name">{esc(display_name)}</div>
      <div class="session-sub">Session Recap</div>
    </div>
    <div class="sr-header-right">
      <div class="duration-badge">&#128336; {esc(duration)}</div>
      {streak_html}
      {best_streak_html}
    </div>
  </div>

  <div class="gold-divider"></div>

  <!-- Hero P&L -->
  <div class="hero-pnl">
    <div class="hero-pnl-label">Net P&amp;L</div>
    <div class="hero-pnl-value {esc(pnl_color)}">{esc(pnl_str)}</div>
  </div>

  <div class="gold-divider"></div>

  <!-- Primary stats row: Games / Wins / Losses / Pushes -->
  <div class="data-grid cols4">
    <div class="data-cell">
      <div class="data-label">Games</div>
      <div class="data-value">{session.total_games}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">Wins</div>
      <div class="data-value green">{session.wins}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">Losses</div>
      <div class="data-value red">{session.losses}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">Pushes</div>
      <div class="data-value amber">{session.pushes}</div>
    </div>
  </div>

  <!-- Win rate bar -->
  <div class="win-rate-row">
    <div class="win-rate-label">Win Rate</div>
    <div class="win-rate-bar-bg">
      <div class="win-rate-bar-fill" style="width:{win_rate}%"></div>
    </div>
    <div class="win-rate-pct">{win_rate}%</div>
  </div>

  <!-- Best win / Worst loss cells (conditional) -->
  {f'<div class="data-grid cols2">{best_cells_html}</div>' if best_cells_html else ''}

  <div class="gold-divider"></div>

  <!-- Game breakdown -->
  {game_breakdown_html}

  <!-- Highlight moments -->
  {highlights_html}

  <!-- Commentary -->
  {commentary_html}
"""

    return wrap_card(body_html, status_class, theme_id=theme_id)


# ── Public API ────────────────────────────────────────────────────────────────

async def render_session_recap(
    session: PlayerSession,
    display_name: str,
    commentary: str = "",
    theme_id: str | None = None,
) -> bytes:
    """
    Render a session recap card as PNG bytes.

    Args:
        session:      PlayerSession dataclass with accumulated stats/events.
        display_name: Discord display name to show on the card.
        commentary:   Optional ATLAS commentary line (roast or hype).

    Returns:
        PNG bytes of the rendered card.
    """
    html = _build_session_recap_html(session, display_name, commentary, theme_id=theme_id)
    return await render_card(html)
