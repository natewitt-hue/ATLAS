"""
card_renderer.py — ATLAS · Genesis Trade Card Renderer
───────────────────────────────────────────────────────
Uses Playwright to render trade card HTML → PNG bytes.
Maintains a persistent browser instance to avoid cold-start latency.

ATLAS Design System v2 — matches sportsbook card visual language.

Usage:
    from card_renderer import render_trade_card
    png_bytes = await render_trade_card(trade_data)

trade_data dict keys:
    trade_id        str
    status          str  ("pending" | "approved" | "rejected" | "countered" | "declined")
    team_a_name     str
    team_a_owner    str
    team_b_name     str
    team_b_owner    str
    players_a       list[dict]   — sanitized player dicts
    picks_a         list[dict]   — pick dicts {round, year, team_id}
    players_b       list[dict]
    picks_b         list[dict]
    side_a_value    int
    side_b_value    int
    delta_pct       float
    band            str  ("GREEN" | "YELLOW" | "RED")
    ovr_delta       int
    pick_lines      list[str]    — pre-formatted pick value strings
    notes           list[str]    — flags/violations
    ai_commentary   str
    proposer_id     int
    warnings        list[str]
"""

from __future__ import annotations

import json
import base64
from pathlib import Path

from atlas_html_engine import render_card as engine_render_card, wrap_card, esc


def _esc(text) -> str:
    """Escape user-controlled text for safe HTML embedding."""
    return esc(text)

# ── Genesis icon loader (base64 for inline HTML) ─────────────────────────────

_GENESIS_ICON_B64 = ""
try:
    _icon_path = Path(__file__).parent / "icons" / "genesis.png"
    if _icon_path.exists():
        _GENESIS_ICON_B64 = base64.b64encode(_icon_path.read_bytes()).decode()
except Exception:
    pass

# ── Dev icon loader ──────────────────────────────────────────────────────────

_ICONS_PATH = Path(__file__).parent / "dev_icons.json"
_DEV_ICONS: dict[str, str] = {}

def _load_icons():
    global _DEV_ICONS
    if _ICONS_PATH.exists():
        with open(_ICONS_PATH) as f:
            _DEV_ICONS = json.load(f)

_load_icons()

def _dev_icon_uri(dev: str) -> str:
    key_map = {
        "xfactor": "xf", "x-factor": "xf", "x factor": "xf", "ssx": "xf",
        "superstar": "ss",
        "star": "star",
        "normal": "normal",
    }
    key = key_map.get(str(dev).lower().strip(), "normal")
    b64 = _DEV_ICONS.get(key, _DEV_ICONS.get("normal", ""))
    return f"data:image/png;base64,{b64}" if b64 else ""


# ── NFL team identity (via TeamBranding — replaces hardcoded dict) ────────────

from team_branding import TeamBranding as _TeamBranding

_branding: _TeamBranding | None = None

def _get_branding() -> _TeamBranding:
    global _branding
    if _branding is None:
        _branding = _TeamBranding("assets/team_branding.json")
    return _branding

def _team_abbrev(name: str) -> str:
    b = _get_branding()
    team = b.by_nickname(name, "NFL")
    if not team:
        for t in b.all_teams("NFL"):
            if t.get("nickname", "").lower() in name.lower() or name.lower() in t.get("nickname", "").lower():
                team = t
                break
    return team["abbreviation"].lower() if team else ""

def _team_logo_url(name: str) -> str:
    logo = _get_branding().logo_url(name, league="NFL")
    if logo:
        return logo
    abbrev = _team_abbrev(name)
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbrev}.png" if abbrev else ""



# ── HTML builders ────────────────────────────────────────────────────────────

def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def _player_card_html(p: dict) -> str:
    first = _esc(p.get("firstName", ""))
    last  = _esc(p.get("lastName", ""))
    name  = f"{first} {last}".strip() or "Unknown"
    pos   = _esc(p.get("pos", p.get("position", "?")))
    ovr   = _esc(p.get("overallRating") or p.get("playerBestOvr") or "?")
    age   = _esc(p.get("age", "?"))

    # Dev trait resolution
    dev_raw = p.get("devTrait", p.get("dev", 0))
    dev_int_map = {0: "normal", 1: "star", 2: "superstar", 3: "xfactor"}
    if isinstance(dev_raw, int):
        dev = dev_int_map.get(dev_raw, "normal")
    else:
        dev = str(dev_raw).lower().strip()

    dev_display = {
        "xfactor": ("💎 SUPERSTAR X-FACTOR", "dev-xf"),
        "x-factor": ("💎 SUPERSTAR X-FACTOR", "dev-xf"),
        "ssx": ("💎 SUPERSTAR X-FACTOR", "dev-xf"),
        "superstar": ("⭐ SUPERSTAR", "dev-ss"),
        "star": ("★ STAR", "dev-star"),
        "normal": ("", "dev-normal"),
    }
    dev_label, dev_class = dev_display.get(dev, ("", "dev-normal"))

    dev_html = ""
    if dev_label:
        dev_html = f'<div class="dev-badge {dev_class}">{dev_label}</div>'

    return f"""
    <div class="player-card">
      <div class="player-ovr">{ovr}</div>
      <div class="player-info">
        <div class="player-name">{name}</div>
        <div class="player-meta">{pos} · Age {age}</div>
        {dev_html}
      </div>
    </div>"""


def _pick_card_html(pk: dict) -> str:
    rnd   = pk.get("round", "?")
    year  = pk.get("year", "?")
    label = f"S{year} {_ordinal(rnd)} Round Pick"

    return f"""
    <div class="pick-card">
      <div class="pick-icon">📋</div>
      <div><div class="pick-label">{label}</div></div>
    </div>"""


def _build_html(data: dict) -> str:
    # ── Status config ────────────────────────────────────────────────────────
    status = data.get("status", "pending")
    status_cfg = {
        "pending":   ("rgba(234,179,8,0.4)",   "var(--yellow)", "rgba(234,179,8,0.06)",   "⏳ PENDING REVIEW"),
        "approved":  ("rgba(74,222,128,0.4)",   "var(--win)",    "rgba(74,222,128,0.06)",  "✅ APPROVED"),
        "rejected":  ("rgba(248,113,113,0.4)",  "var(--loss)",   "rgba(248,113,113,0.06)", "❌ REJECTED"),
        "declined":  ("rgba(248,113,113,0.4)",  "var(--loss)",   "rgba(248,113,113,0.06)", "🚫 AUTO-DECLINED"),
        "countered": ("rgba(88,101,242,0.4)",   "#5865F2",       "rgba(88,101,242,0.06)",  "🔄 COUNTER OFFERED"),
    }
    border_c, text_c, bg_c, badge_text = status_cfg.get(status, status_cfg["pending"])

    # ── Band config ──────────────────────────────────────────────────────────
    band = data.get("band", "GREEN")
    band_cfg = {
        "GREEN":  ("green",  "var(--win)",    "🟢", "FAIR",      "Within legal range",  "Auto-Eligible"),
        "YELLOW": ("yellow", "var(--yellow)", "🟡", "CAUTION",   "Flagged for review",  "Commissioner<br>Required"),
        "RED":    ("red",    "var(--loss)",   "🔴", "LOPSIDED",  "Outside legal range", "Auto-Declined"),
    }
    sbar_cls, band_color, band_emoji, band_word, band_sub, decision_text = band_cfg.get(band, band_cfg["GREEN"])

    team_a = _esc(data.get("team_a_name", "Team A"))
    team_b = _esc(data.get("team_b_name", "Team B"))
    owner_a = _esc(data.get("team_a_owner", ""))
    owner_b = _esc(data.get("team_b_owner", ""))
    logo_a = _team_logo_url(team_a)
    logo_b = _team_logo_url(team_b)

    # ── Assets ───────────────────────────────────────────────────────────────
    players_a = data.get("players_a", [])
    players_b = data.get("players_b", [])
    picks_a   = data.get("picks_a", [])
    picks_b   = data.get("picks_b", [])

    assets_a_html = "".join(_player_card_html(p) for p in players_a[:4])
    assets_a_html += "".join(_pick_card_html(pk) for pk in picks_a[:4])
    if not assets_a_html:
        assets_a_html = '<div style="font-size:12px;color:var(--text-dim);padding:8px;">No assets</div>'

    assets_b_html = "".join(_player_card_html(p) for p in players_b[:4])
    assets_b_html += "".join(_pick_card_html(pk) for pk in picks_b[:4])
    if not assets_b_html:
        assets_b_html = '<div style="font-size:12px;color:var(--text-dim);padding:8px;">No assets</div>'

    # ── Valuation (FIXED direction: team sending LESS = beneficiary) ─────────
    val_a = data.get("side_a_value", 0)
    val_b = data.get("side_b_value", 0)
    delta = data.get("delta_pct", 0.0)
    ovr_delta = data.get("ovr_delta", 0)
    favored = team_a if val_a < val_b else team_b
    winner_a = "winner" if val_a > val_b else ""
    winner_b = "winner" if val_b > val_a else ""

    # Fairness bar percentages
    total_val = val_a + val_b
    pct_a = round(val_a / total_val * 100) if total_val > 0 else 50
    pct_b = 100 - pct_a

    # ── Notes / Flags ────────────────────────────────────────────────────────
    notes = data.get("notes", [])
    flags_html = ""
    if notes:
        items = "".join(f'<div class="flag-item">{_esc(n)}</div>' for n in notes[:6])
        flags_html = f"""
    <div class="flags-section">
      <div class="flags-title">⚠ Flags</div>
      {items}
    </div>
    <div class="sep"></div>"""

    # ── AI Commentary ────────────────────────────────────────────────────────
    ai = data.get("ai_commentary", "")
    ai_html = ""
    if ai and "unavailable" not in ai.lower():
        ai_clean = _esc(ai.strip().strip("*_").strip('"'))
        ai_html = f"""
    <div class="verdict-section">
      <div class="verdict-title">🤖 Atlas verdict</div>
      <div class="verdict-quote">"{ai_clean}"</div>
    </div>"""

    # ── Footer ───────────────────────────────────────────────────────────────
    trade_id    = _esc(data.get("trade_id", "???"))
    proposer_id = _esc(data.get("proposer_id", ""))

    # ── Genesis icon ─────────────────────────────────────────────────────────
    icon_src = f"data:image/png;base64,{_GENESIS_ICON_B64}" if _GENESIS_ICON_B64 else ""
    icon_html = f'<img src="{icon_src}" style="width:48px;height:48px;object-fit:contain;">' if icon_src else ""

    # ── Trade-specific CSS (layered on top of engine shared CSS) ─────────────
    trade_css = f"""<style>
  /* Trade card overrides */
  .card {{ border-radius: 20px; width: 700px; }}
  .inner {{ position: relative; z-index: 2; background: var(--bg); }}

  /* Status bar */
  .sbar {{ height: 5px; }}
  .sbar.green  {{ background: linear-gradient(90deg, var(--win), var(--win-dark), var(--win)); }}
  .sbar.yellow {{ background: linear-gradient(90deg, var(--yellow), #FACC15, var(--yellow)); }}
  .sbar.red    {{ background: linear-gradient(90deg, var(--loss), var(--loss-dark), var(--loss)); }}

  .sep      {{ height: 1px; background: rgba(255,255,255,0.04); margin: 0 36px; }}
  .sep-gold {{ height: 1px; background: linear-gradient(90deg, transparent, rgba(212,175,55,0.2) 15%, rgba(212,175,55,0.2) 85%, transparent); }}

  /* ── HEADER ── */
  .hdr {{
    display: flex; align-items: center;
    padding: var(--space-xl) 36px 20px; gap: var(--space-lg);
  }}
  .hdr-logo {{
    width: 56px; height: 56px;
    border-radius: 14px;
    overflow: hidden;
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    background: var(--bg-deep);
    border: 1px solid rgba(212,175,55,0.15);
  }}
  .hdr-text {{ flex: 1; }}
  .hdr-title {{
    font-family: var(--font-display), sans-serif;
    font-weight: 800; font-size: 30px;
    letter-spacing: 0.18em;
    background: linear-gradient(180deg, #FFE8A0 0%, var(--gold) 35%, #B8942D 55%, var(--gold) 75%, #FFE8A0 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 1px 2px rgba(0,0,0,0.5));
    line-height: 1.1;
  }}
  .hdr-sub {{
    font-weight: 700; font-size: var(--font-sm);
    color: rgba(255, 255, 255, 0.7);
    letter-spacing: 0.16em; margin-top: 5px;
  }}
  .hdr-badge {{
    padding: var(--space-sm) 18px;
    border-radius: 24px;
    font-family: var(--font-mono), monospace;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    white-space: nowrap;
    border: 1px solid {border_c};
    color: {text_c};
    background: {bg_c};
  }}

  /* ── MATCHUP BAR ── */
  .matchup {{
    display: flex;
    align-items: center;
    padding: var(--space-lg) 36px;
  }}
  .team-side {{ flex: 1; }}
  .team-side.right {{ text-align: right; }}
  .team-name {{
    font-weight: 800; font-size: 22px; color: #fff;
    letter-spacing: 0.04em;
  }}
  .team-owner {{
    font-family: var(--font-mono), monospace;
    font-size: 12px; color: var(--text-dim); font-weight: 600;
    margin-top: 2px;
  }}
  .vs-divider {{
    font-size: 16px; color: rgba(212,175,55,0.3);
    padding: 0 var(--space-xl);
    font-weight: 300;
  }}
  .team-logo {{
    width: 48px; height: 48px;
    object-fit: contain;
    flex-shrink: 0;
    filter: drop-shadow(0 2px 6px rgba(0,0,0,0.4));
  }}
  .team-logo-row {{
    display: flex;
    align-items: center;
    gap: 14px;
  }}
  .team-logo-row.right {{
    justify-content: flex-end;
  }}
  .assets-logo {{
    width: 16px; height: 16px;
    object-fit: contain;
    vertical-align: middle;
    margin-right: 4px;
    opacity: 0.6;
  }}

  /* ── ASSET COLUMNS ── */
  .assets-row {{
    display: flex;
    gap: 2px;
    margin: 0 36px;
  }}
  .assets-col {{
    flex: 1;
    background: rgba(255,255,255,0.015);
    padding: var(--space-lg) 18px;
  }}
  .assets-col:first-child {{ border-radius: 16px 0 0 16px; }}
  .assets-col:last-child  {{ border-radius: 0 16px 16px 0; }}
  .assets-header {{
    font-weight: 700; font-size: var(--font-xs);
    color: var(--gold); opacity: 0.55;
    letter-spacing: 0.18em;
    margin-bottom: var(--space-md);
    text-transform: uppercase;
  }}

  /* Player card */
  .player-card {{
    display: flex;
    align-items: center;
    gap: var(--space-md);
    padding: 10px var(--space-md);
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.04);
    border-radius: 10px;
    margin-bottom: var(--space-sm);
  }}
  .player-ovr {{
    font-family: var(--font-mono), monospace;
    font-weight: 800; font-size: 28px;
    color: #fff;
    min-width: 44px; text-align: center;
    line-height: 1;
  }}
  .player-info {{ flex: 1; }}
  .player-name {{
    font-weight: 700; font-size: 14px; color: #fff;
    line-height: 1.3;
  }}
  .player-meta {{
    font-family: var(--font-mono), monospace;
    font-size: var(--font-xs); color: var(--text-dim); font-weight: 500;
    margin-top: 2px;
  }}
  .dev-badge {{
    display: inline-flex;
    align-items: center;
    gap: var(--space-xs);
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    margin-top: var(--space-xs);
  }}
  .dev-xf     {{ background: rgba(168,85,247,0.1); border: 1px solid rgba(168,85,247,0.25); color: var(--purple); }}
  .dev-ss     {{ background: rgba(212,175,55,0.08); border: 1px solid rgba(212,175,55,0.2);  color: var(--gold); }}
  .dev-star   {{ background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); color: var(--text-muted); }}
  .dev-normal {{ display: none; }}

  /* Pick card */
  .pick-card {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: var(--space-sm) var(--space-md);
    background: rgba(255,255,255,0.015);
    border: 1px solid rgba(255,255,255,0.03);
    border-radius: var(--border-radius);
    margin-bottom: 6px;
  }}
  .pick-icon {{ font-size: 16px; }}
  .pick-label {{ font-size: var(--font-sm); font-weight: 600; color: var(--text-muted); }}

  /* ── TRADE HEALTH ── */
  .health-section {{
    padding: 20px 36px var(--space-lg);
  }}
  .health-title {{
    font-weight: 700; font-size: var(--font-xs);
    color: var(--gold); opacity: 0.55;
    letter-spacing: 0.18em;
    margin-bottom: 14px;
    text-transform: uppercase;
  }}
  .health-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }}
  .health-team {{
    font-size: 14px; color: var(--text-muted);
    font-weight: 600;
  }}
  .health-label {{
    font-size: 10px; color: var(--text-dim);
    letter-spacing: 0.1em; font-weight: 600;
    text-transform: uppercase;
    margin-left: 6px;
  }}
  .health-pts {{
    font-family: var(--font-mono), monospace;
    font-weight: 800; font-size: var(--font-lg);
    color: #fff;
  }}
  .health-pts.winner {{ color: var(--gold); }}

  /* Fairness bar */
  .fairness-bar-wrap {{ margin: 14px 0 var(--space-sm); }}
  .fairness-labels {{
    display: flex;
    justify-content: space-between;
    margin-bottom: var(--space-xs);
  }}
  .fl-team {{
    font-family: var(--font-mono), monospace;
    font-size: var(--font-xs); color: var(--text-dim); font-weight: 600;
  }}
  .fairness-bar {{
    height: 6px;
    border-radius: 3px;
    overflow: hidden;
    display: flex;
    background: rgba(255,255,255,0.03);
  }}
  .fb-a {{
    background: linear-gradient(90deg, var(--gold), var(--gold-bright));
    height: 100%;
  }}
  .fb-b {{
    background: linear-gradient(90deg, rgba(255,255,255,0.08), rgba(255,255,255,0.12));
    height: 100%;
  }}

  /* Favors line */
  .favors-line {{
    text-align: center;
    margin-top: 10px;
    font-size: var(--font-sm);
    color: var(--text-muted);
  }}
  .favors-line strong {{ color: var(--gold); font-weight: 700; }}
  .favors-line .pct {{
    font-family: var(--font-mono), monospace;
    font-weight: 700;
  }}

  /* ── METRICS ROW ── */
  .metrics-row {{
    display: flex;
    gap: 2px;
    margin: 0 36px;
  }}
  .metric-cell {{
    flex: 1;
    background: rgba(255,255,255,0.02);
    padding: var(--space-lg) 14px;
    text-align: center;
  }}
  .metric-cell:first-child {{ border-radius: 16px 0 0 16px; }}
  .metric-cell:last-child  {{ border-radius: 0 16px 16px 0; }}
  .metric-label {{
    font-weight: 700; font-size: 10px;
    color: rgba(255,255,255,0.25);
    letter-spacing: 0.16em;
    margin-bottom: 6px;
    text-transform: uppercase;
  }}
  .metric-value {{
    font-family: var(--font-mono), monospace;
    font-weight: 800; font-size: var(--font-lg);
    line-height: 1.2;
  }}
  .metric-sub {{
    font-size: 10px; color: #444;
    margin-top: var(--space-xs);
    letter-spacing: 0.04em;
  }}
  .mv-green  {{ color: var(--win); }}
  .mv-yellow {{ color: var(--yellow); }}
  .mv-red    {{ color: var(--loss); }}
  .mv-white  {{ color: #fff; }}

  /* ── FLAGS ── */
  .flags-section {{
    padding: 14px 36px;
  }}
  .flags-title {{
    font-weight: 700; font-size: var(--font-xs);
    color: var(--gold); opacity: 0.45;
    letter-spacing: 0.18em;
    margin-bottom: var(--space-sm);
    text-transform: uppercase;
  }}
  .flag-item {{
    font-size: 12px;
    color: var(--loss);
    padding: 3px 0 3px 14px;
    position: relative;
    line-height: 1.5;
  }}
  .flag-item::before {{
    content: '›';
    position: absolute;
    left: 0;
    color: var(--loss);
    font-weight: 700;
  }}

  /* ── AI VERDICT ── */
  .verdict-section {{
    padding: var(--space-lg) 36px;
  }}
  .verdict-title {{
    font-weight: 700; font-size: var(--font-xs);
    color: var(--gold); opacity: 0.45;
    letter-spacing: 0.18em;
    margin-bottom: 10px;
    text-transform: uppercase;
  }}
  .verdict-quote {{
    font-size: var(--font-sm);
    font-style: italic;
    color: #999;
    line-height: 1.7;
    border-left: 2px solid rgba(212,175,55,0.25);
    padding-left: var(--space-lg);
  }}

  /* ── FOOTER ── */
  .foot {{
    background: rgba(0,0,0,0.28);
    padding: 14px 36px;
    display: flex; align-items: center;
    justify-content: space-between;
    margin-top: 20px;
  }}
  .foot-txt {{
    font-family: var(--font-mono), monospace;
    font-size: var(--font-xs); color: #262626; font-weight: 600;
    letter-spacing: 0.05em;
  }}
</style>

  <div class="sbar {sbar_cls}"></div>
  <div class="inner">

    <!-- HEADER -->
    <div class="hdr">
      <div class="hdr-logo">{icon_html}</div>
      <div class="hdr-text">
        <div class="hdr-title">GENESIS</div>
        <div class="hdr-sub">TRADE ENGINE V2.7</div>
      </div>
      <div class="hdr-badge">{badge_text}</div>
    </div>
    <div class="sep-gold"></div>

    <!-- MATCHUP -->
    <div class="matchup">
      <div class="team-side">
        <div class="team-logo-row">
          <img class="team-logo" src="{logo_a}" alt="{team_a}">
          <div>
            <div class="team-name">{team_a}</div>
            <div class="team-owner">{owner_a}</div>
          </div>
        </div>
      </div>
      <div class="vs-divider">↔</div>
      <div class="team-side right">
        <div class="team-logo-row right">
          <div>
            <div class="team-name">{team_b}</div>
            <div class="team-owner">{owner_b}</div>
          </div>
          <img class="team-logo" src="{logo_b}" alt="{team_b}">
        </div>
      </div>
    </div>

    <div style="height: var(--space-sm);"></div>

    <!-- ASSETS -->
    <div class="assets-row">
      <div class="assets-col">
        <div class="assets-header"><img class="assets-logo" src="{logo_a}" alt=""> {team_a} gives</div>
        {assets_a_html}
      </div>
      <div class="assets-col">
        <div class="assets-header"><img class="assets-logo" src="{logo_b}" alt=""> {team_b} gives</div>
        {assets_b_html}
      </div>
    </div>

    <div style="height: var(--space-lg);"></div>

    <!-- TRADE HEALTH -->
    <div class="health-section">
      <div class="health-title">Trade health</div>
      <div class="health-row">
        <div>
          <span class="health-team">{team_a}</span>
          <span class="health-label">gives</span>
        </div>
        <div class="health-pts {winner_a}">{val_a:,} pts</div>
      </div>
      <div class="health-row">
        <div>
          <span class="health-team">{team_b}</span>
          <span class="health-label">gives</span>
        </div>
        <div class="health-pts {winner_b}">{val_b:,} pts</div>
      </div>

      <div class="fairness-bar-wrap">
        <div class="fairness-labels">
          <span class="fl-team">{team_a}</span>
          <span class="fl-team">{team_b}</span>
        </div>
        <div class="fairness-bar">
          <div class="fb-a" style="width: {pct_a}%;"></div>
          <div class="fb-b" style="width: {pct_b}%;"></div>
        </div>
      </div>

      <div class="favors-line">
        <span class="pct">{delta:.1f}%</span> gap — favors <strong>{favored}</strong>
      </div>
    </div>

    <div style="height: var(--space-sm);"></div>

    <!-- METRICS ROW -->
    <div class="metrics-row">
      <div class="metric-cell">
        <div class="metric-label">Fairness Band</div>
        <div class="metric-value mv-{sbar_cls}">{band_emoji} {band_word}</div>
        <div class="metric-sub">{band_sub}</div>
      </div>
      <div class="metric-cell">
        <div class="metric-label">OVR Delta</div>
        <div class="metric-value mv-white">{'▲' if ovr_delta > 0 else '▼' if ovr_delta < 0 else '='} {abs(ovr_delta)}</div>
        <div class="metric-sub">Combined OVR difference</div>
      </div>
      <div class="metric-cell">
        <div class="metric-label">Decision</div>
        <div class="metric-value mv-{sbar_cls}" style="font-size: 14px;">{decision_text}</div>
      </div>
    </div>

    <div style="height: 14px;"></div>

    <!-- FLAGS -->
    {flags_html}

    <!-- AI VERDICT -->
    {ai_html}

    <!-- FOOTER -->
    <div class="foot">
      <div class="foot-txt">TRADE ID: {trade_id} · PROPOSED BY {proposer_id}</div>
      <div class="foot-txt">ATLAS™ · GENESIS</div>
    </div>
  </div>"""

    return wrap_card(trade_css, "")


# ── Main render function ─────────────────────────────────────────────────────

async def render_trade_card(data: dict) -> bytes | None:
    """
    Render a trade card to PNG bytes.
    Returns None on failure (caller should fall back to embed).
    """
    try:
        html = _build_html(data)
        return await engine_render_card(html, width=720)
    except Exception as e:
        print(f"[card_renderer] Render error: {e}")
        return None
