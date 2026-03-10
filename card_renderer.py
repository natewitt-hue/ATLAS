"""
card_renderer.py — TSL Trade Card Image Renderer
─────────────────────────────────────────────────
Uses Playwright to render trade_card.html → PNG bytes.
Maintains a persistent browser instance to avoid cold-start latency.

Usage:
    from card_renderer import render_trade_card
    png_bytes = await render_trade_card(trade_data)

trade_data dict keys:
    trade_id        str
    status          str  ("pending" | "approved" | "rejected" | "countered")
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

import asyncio
import json
import os
import base64
from pathlib import Path
from string import Template

# ── Dev icon loader ───────────────────────────────────────────────────────────

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


# ── Browser singleton ─────────────────────────────────────────────────────────

_browser = None
_playwright_instance = None

async def _get_browser():
    global _browser, _playwright_instance
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright
        _playwright_instance = async_playwright()
        pw = await _playwright_instance.__aenter__()
        _browser = await pw.chromium.launch(headless=True)
    return _browser


async def close_browser():
    """Call on bot shutdown to clean up."""
    global _browser, _playwright_instance
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright_instance:
        try:
            await _playwright_instance.__aexit__(None, None, None)
        except Exception:
            pass
        _playwright_instance = None


# ── HTML builders ─────────────────────────────────────────────────────────────

def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def _player_card_html(p: dict) -> str:
    first = p.get("firstName", "")
    last  = p.get("lastName", "")
    name  = f"{first} {last}".strip() or "Unknown"
    pos   = p.get("pos", p.get("position", "?"))
    ovr   = p.get("overallRating") or p.get("playerBestOvr") or "?"
    age   = p.get("age", "?")
    dev   = str(p.get("dev", "Normal"))
    icon  = _dev_icon_uri(dev)

    dev_label_map = {
        "xfactor": "X-FACTOR", "x-factor": "X-FACTOR", "ssx": "X-FACTOR",
        "superstar": "SUPERSTAR",
        "star": "STAR",
        "normal": "NORMAL",
    }
    dev_label = dev_label_map.get(dev.lower().strip(), dev.upper())

    dev_class_map = {
        "X-FACTOR": "dev-xf", "SUPERSTAR": "dev-ss",
        "STAR": "dev-star", "NORMAL": "dev-normal",
    }
    dev_class = dev_class_map.get(dev_label, "dev-normal")

    icon_html = f'<img src="{icon}" class="dev-icon" alt="{dev_label}">' if icon else ""

    return f"""
    <div class="player-card">
      <div class="player-main">
        <div class="player-ovr">{ovr}</div>
        <div class="player-info">
          <div class="player-name">{name}</div>
          <div class="player-meta">{pos} &nbsp;·&nbsp; Age {age}</div>
        </div>
      </div>
      <div class="dev-badge {dev_class}">
        {icon_html}
        <span>{dev_label}</span>
      </div>
    </div>"""


def _pick_card_html(pk: dict, value_str: str = "") -> str:
    rnd   = pk.get("round", "?")
    year  = pk.get("year", "?")
    label = f"{_ordinal(rnd)} Round Pick"
    # Get team abbr if available
    team_note = ""
    team_id = pk.get("team_id")
    if team_id:
        try:
            import data_manager as dm
            if not dm.df_teams.empty:
                row = dm.df_teams[dm.df_teams["id"].astype(str) == str(team_id)]
                if not row.empty:
                    abbr = row.iloc[0].get("abbrName", "")
                    if abbr:
                        team_note = f" ({abbr})"
        except Exception:
            pass

    return f"""
    <div class="pick-card">
      <div class="pick-icon">📋</div>
      <div class="pick-info">
        <div class="pick-label">{year} {label}{team_note}</div>
        {f'<div class="pick-value">{value_str}</div>' if value_str else ""}
      </div>
    </div>"""


def _fairness_bar_html(side_a_value: int, side_b_value: int, team_a: str, team_b: str) -> str:
    total = side_a_value + side_b_value
    if total == 0:
        pct_a = 50
    else:
        pct_a = round(side_a_value / total * 100)
    pct_b = 100 - pct_a
    return f"""
    <div class="fairness-bar-wrap">
      <div class="fairness-labels">
        <span class="fl-team">{team_a}</span>
        <span class="fl-pct">{pct_a}%</span>
        <span class="fl-spacer"></span>
        <span class="fl-pct">{pct_b}%</span>
        <span class="fl-team">{team_b}</span>
      </div>
      <div class="fairness-bar">
        <div class="fb-a" style="width:{pct_a}%"></div>
        <div class="fb-b" style="width:{pct_b}%"></div>
      </div>
    </div>"""


def _build_html(data: dict) -> str:
    # Status config
    status = data.get("status", "pending")
    status_cfg = {
        "pending":   ("#C9A84C", "⏳", "PENDING REVIEW"),
        "approved":  ("#22C55E", "✅", "APPROVED"),
        "rejected":  ("#EF4444", "❌", "REJECTED"),
        "countered": ("#A855F7", "🔄", "COUNTER OFFERED"),
    }
    status_color, status_emoji, status_label = status_cfg.get(status, status_cfg["pending"])

    band = data.get("band", "GREEN")
    band_cfg = {
        "GREEN":  ("#22C55E", "🟢", "FAIR TRADE"),
        "YELLOW": ("#EAB308", "🟡", "NEEDS REVIEW"),
        "RED":    ("#EF4444", "🔴", "LOPSIDED"),
    }
    band_color, band_emoji, band_label = band_cfg.get(band, band_cfg["GREEN"])

    team_a = data.get("team_a_name", "Team A")
    team_b = data.get("team_b_name", "Team B")
    owner_a = data.get("team_a_owner", "")
    owner_b = data.get("team_b_owner", "")

    # Build asset columns
    players_a = data.get("players_a", [])
    players_b = data.get("players_b", [])
    picks_a   = data.get("picks_a", [])
    picks_b   = data.get("picks_b", [])

    assets_a = "".join(_player_card_html(p) for p in players_a[:3])
    assets_a += "".join(_pick_card_html(pk) for pk in picks_a[:4])
    if not assets_a:
        assets_a = '<div class="empty-assets">Nothing</div>'

    assets_b = "".join(_player_card_html(p) for p in players_b[:3])
    assets_b += "".join(_pick_card_html(pk) for pk in picks_b[:4])
    if not assets_b:
        assets_b = '<div class="empty-assets">Nothing</div>'

    # Valuation
    val_a = data.get("side_a_value", 0)
    val_b = data.get("side_b_value", 0)
    delta = data.get("delta_pct", 0.0)
    ovr_delta = data.get("ovr_delta", 0)
    delta_arrow = "▲" if val_a > val_b else "▼"
    favored = team_a if val_a > val_b else team_b

    fairness_bar = _fairness_bar_html(val_a, val_b, team_a, team_b)

    # Notes/flags
    notes = data.get("notes", [])
    notes_html = ""
    if notes:
        items = "".join(f"<li>{n}</li>" for n in notes[:6])
        notes_html = f'<div class="section-block flags-block"><div class="section-title">⚠️ FLAGS</div><ul class="flags-list">{items}</ul></div>'

    # AI commentary
    ai = data.get("ai_commentary", "")
    ai_html = ""
    if ai and "unavailable" not in ai.lower():
        ai_clean = ai.strip().strip("*_")
        ai_html = f"""
        <div class="section-block ai-block">
          <div class="section-title">🤖 ATLAS VERDICT</div>
          <div class="ai-quote">"{ai_clean}"</div>
        </div>"""

    # Footer
    trade_id    = data.get("trade_id", "???")
    proposer_id = data.get("proposer_id", "")

    # TSL medallion SVG (gold ornate circle)
    medallion = """<svg width="64" height="64" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="mg" cx="50%" cy="40%" r="60%">
          <stop offset="0%" stop-color="#FFE080"/>
          <stop offset="60%" stop-color="#C9A84C"/>
          <stop offset="100%" stop-color="#8B6914"/>
        </radialGradient>
      </defs>
      <circle cx="32" cy="32" r="30" fill="url(#mg)" stroke="#8B6914" stroke-width="2"/>
      <circle cx="32" cy="32" r="24" fill="none" stroke="#FFE080" stroke-width="1.2" stroke-dasharray="3 2"/>
      <!-- castle battlements -->
      <rect x="22" y="20" width="4" height="6" rx="0.5" fill="#1a1a2e"/>
      <rect x="28" y="20" width="4" height="6" rx="0.5" fill="#1a1a2e"/>
      <rect x="34" y="20" width="4" height="6" rx="0.5" fill="#1a1a2e"/>
      <rect x="22" y="26" width="16" height="8" rx="0.5" fill="#1a1a2e"/>
      <!-- TSL text -->
      <text x="32" y="42" font-family="Georgia,serif" font-size="9" font-weight="bold"
            fill="#C9A84C" text-anchor="middle" letter-spacing="1">TSL</text>
      <!-- stars above -->
      <text x="32" y="18" font-family="Arial" font-size="6" fill="#FFE080" text-anchor="middle">★ ★ ★</text>
      <!-- star below -->
      <text x="32" y="52" font-family="Arial" font-size="7" fill="#FFE080" text-anchor="middle">★</text>
    </svg>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Oswald:wght@400;600;700&family=Open+Sans:wght@400;600&display=swap');

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    width: 600px;
    background: #0d0d1a;
    font-family: 'Open Sans', sans-serif;
    color: #e8e0d0;
    padding: 0;
  }}

  .card {{
    width: 600px;
    background: linear-gradient(160deg, #12122a 0%, #0d0d1a 40%, #12101f 100%);
    border: 2px solid #C9A84C;
    border-radius: 12px;
    overflow: hidden;
    position: relative;
  }}

  /* Gold corner accents */
  .card::before, .card::after {{
    content: '';
    position: absolute;
    width: 40px; height: 40px;
    border-color: #C9A84C;
    border-style: solid;
    z-index: 10;
  }}
  .card::before {{ top: 8px; left: 8px; border-width: 2px 0 0 2px; border-radius: 4px 0 0 0; }}
  .card::after  {{ bottom: 8px; right: 8px; border-width: 0 2px 2px 0; border-radius: 0 0 4px 0; }}

  /* ── HEADER ── */
  .header {{
    background: linear-gradient(135deg, #1a1a35 0%, #0f0f22 100%);
    border-bottom: 1px solid #C9A84C44;
    padding: 16px 20px 12px;
    display: flex;
    align-items: center;
    gap: 14px;
  }}
  .medallion {{ flex-shrink: 0; }}
  .header-text {{ flex: 1; }}
  .header-title {{
    font-family: 'Oswald', sans-serif;
    font-size: 22px;
    font-weight: 700;
    color: #C9A84C;
    letter-spacing: 3px;
    text-transform: uppercase;
  }}
  .header-sub {{
    font-size: 11px;
    color: #888;
    letter-spacing: 2px;
    margin-top: 2px;
    text-transform: uppercase;
  }}
  .status-badge {{
    padding: 5px 12px;
    border-radius: 20px;
    font-family: 'Oswald', sans-serif;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 1.5px;
    border: 1px solid {status_color};
    color: {status_color};
    background: {status_color}18;
    white-space: nowrap;
  }}

  /* ── MATCHUP BAR ── */
  .matchup {{
    display: flex;
    align-items: center;
    background: linear-gradient(90deg, #1e1e3a 0%, #16162e 50%, #1e1e3a 100%);
    border-bottom: 1px solid #C9A84C33;
    padding: 10px 20px;
  }}
  .team-side {{
    flex: 1;
    text-align: center;
  }}
  .team-side.right {{ text-align: right; }}
  .team-side.left  {{ text-align: left; }}
  .team-name {{
    font-family: 'Oswald', sans-serif;
    font-size: 18px;
    font-weight: 600;
    color: #e8e0d0;
    letter-spacing: 1px;
  }}
  .team-owner {{
    font-size: 11px;
    color: #C9A84C;
    margin-top: 1px;
  }}
  .vs-divider {{
    font-family: 'Oswald', sans-serif;
    font-size: 14px;
    color: #C9A84C;
    padding: 0 16px;
    opacity: 0.7;
  }}

  /* ── ASSETS ── */
  .assets-row {{
    display: flex;
    gap: 0;
    border-bottom: 1px solid #C9A84C22;
  }}
  .assets-col {{
    flex: 1;
    padding: 12px 14px;
    border-right: 1px solid #C9A84C22;
  }}
  .assets-col:last-child {{ border-right: none; }}
  .assets-col-header {{
    font-family: 'Oswald', sans-serif;
    font-size: 11px;
    letter-spacing: 2px;
    color: #C9A84C;
    margin-bottom: 8px;
    text-transform: uppercase;
    border-bottom: 1px solid #C9A84C33;
    padding-bottom: 4px;
  }}

  /* Player card */
  .player-card {{
    background: #1a1a30;
    border: 1px solid #C9A84C33;
    border-radius: 6px;
    padding: 7px 9px;
    margin-bottom: 6px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .player-main {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .player-ovr {{
    font-family: 'Oswald', sans-serif;
    font-size: 22px;
    font-weight: 700;
    color: #C9A84C;
    min-width: 34px;
    text-align: center;
    line-height: 1;
  }}
  .player-info {{ flex: 1; }}
  .player-name {{
    font-size: 13px;
    font-weight: 600;
    color: #f0e8d8;
    line-height: 1.2;
  }}
  .player-meta {{
    font-size: 11px;
    color: #888;
    margin-top: 1px;
  }}
  .dev-badge {{
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 2px 7px;
    border-radius: 10px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 1px;
    align-self: flex-start;
    margin-left: 42px;
  }}
  .dev-icon {{ width: 14px; height: 14px; object-fit: contain; }}
  .dev-xf    {{ background: #2d1052; border: 1px solid #A855F7; color: #d8a4ff; }}
  .dev-ss    {{ background: #2a1f00; border: 1px solid #C9A84C; color: #FFE080; }}
  .dev-star  {{ background: #1a1a2a; border: 1px solid #9CA3AF; color: #D1D5DB; }}
  .dev-normal{{ background: #1e1612; border: 1px solid #78503a; color: #a07858; }}

  /* Pick card */
  .pick-card {{
    background: #161628;
    border: 1px solid #C9A84C22;
    border-radius: 6px;
    padding: 6px 9px;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .pick-icon {{ font-size: 16px; }}
  .pick-label {{
    font-size: 12px;
    font-weight: 600;
    color: #c8c0b0;
  }}
  .pick-value {{ font-size: 10px; color: #888; margin-top: 1px; }}
  .empty-assets {{ font-size: 12px; color: #555; font-style: italic; padding: 4px; }}

  /* ── VALUATION ── */
  .valuation-block {{
    padding: 12px 20px;
    border-bottom: 1px solid #C9A84C22;
    background: #0f0f20;
  }}
  .val-section-title {{
    font-family: 'Oswald', sans-serif;
    font-size: 10px;
    letter-spacing: 2px;
    color: #C9A84C;
    text-transform: uppercase;
    margin-bottom: 8px;
  }}
  .val-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }}
  .val-team {{ font-size: 13px; color: #a09888; }}
  .val-pts  {{
    font-family: 'Oswald', sans-serif;
    font-size: 16px;
    font-weight: 600;
    color: #e8e0d0;
  }}
  .val-highlight {{ color: #C9A84C; }}

  .fairness-bar-wrap {{ margin-top: 10px; }}
  .fairness-labels {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
    font-size: 10px;
  }}
  .fl-team  {{ color: #888; flex: 1; }}
  .fl-team:last-child {{ text-align: right; }}
  .fl-pct   {{
    font-family: 'Oswald', sans-serif;
    font-size: 13px;
    color: #C9A84C;
    padding: 0 6px;
  }}
  .fl-spacer {{ flex: 1; }}
  .fairness-bar {{
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
    background: #222;
    display: flex;
  }}
  .fb-a {{ background: linear-gradient(90deg, #C9A84C, #FFE080); height: 100%; }}
  .fb-b {{ background: linear-gradient(90deg, #4a3060, #7B3FBF); height: 100%; }}

  /* Band + OVR row */
  .metrics-row {{
    display: flex;
    gap: 0;
    border-bottom: 1px solid #C9A84C22;
  }}
  .metric-cell {{
    flex: 1;
    padding: 10px 16px;
    border-right: 1px solid #C9A84C22;
    text-align: center;
  }}
  .metric-cell:last-child {{ border-right: none; }}
  .metric-label {{
    font-size: 9px;
    letter-spacing: 2px;
    color: #666;
    text-transform: uppercase;
    margin-bottom: 4px;
  }}
  .metric-value {{
    font-family: 'Oswald', sans-serif;
    font-size: 16px;
    font-weight: 600;
  }}
  .metric-sub {{ font-size: 10px; color: #666; margin-top: 2px; }}
  .band-green  {{ color: #22C55E; }}
  .band-yellow {{ color: #EAB308; }}
  .band-red    {{ color: #EF4444; }}

  /* ── SECTIONS ── */
  .section-block {{
    padding: 10px 20px;
    border-bottom: 1px solid #C9A84C22;
  }}
  .section-title {{
    font-family: 'Oswald', sans-serif;
    font-size: 10px;
    letter-spacing: 2px;
    color: #C9A84C;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}
  .flags-list {{
    list-style: none;
    padding: 0;
  }}
  .flags-list li {{
    font-size: 11px;
    color: #EF4444;
    padding: 2px 0;
    padding-left: 12px;
    position: relative;
  }}
  .flags-list li::before {{
    content: '›';
    position: absolute;
    left: 0;
    color: #EF4444;
  }}

  .ai-block {{ background: #0d0d1e; }}
  .ai-quote {{
    font-size: 12px;
    font-style: italic;
    color: #c0b898;
    line-height: 1.5;
    border-left: 3px solid #C9A84C;
    padding-left: 10px;
  }}

  /* ── FOOTER ── */
  .card-footer {{
    padding: 8px 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #0a0a16;
  }}
  .footer-id {{
    font-family: 'Oswald', sans-serif;
    font-size: 10px;
    letter-spacing: 1.5px;
    color: #444;
  }}
  .footer-engine {{
    font-size: 9px;
    color: #333;
    letter-spacing: 1px;
    text-transform: uppercase;
  }}
</style>
</head>
<body>
<div class="card">

  <!-- HEADER -->
  <div class="header">
    <div class="medallion">{medallion}</div>
    <div class="header-text">
      <div class="header-title">Trade Proposal</div>
      <div class="header-sub">TSL Trade Engine v2.7</div>
    </div>
    <div class="status-badge">{status_emoji} {status_label}</div>
  </div>

  <!-- MATCHUP -->
  <div class="matchup">
    <div class="team-side left">
      <div class="team-name">{team_a}</div>
      <div class="team-owner">{owner_a}</div>
    </div>
    <div class="vs-divider">↔</div>
    <div class="team-side right">
      <div class="team-name">{team_b}</div>
      <div class="team-owner">{owner_b}</div>
    </div>
  </div>

  <!-- ASSETS -->
  <div class="assets-row">
    <div class="assets-col">
      <div class="assets-col-header">📤 {team_a} sends</div>
      {assets_a}
    </div>
    <div class="assets-col">
      <div class="assets-col-header">📥 {team_b} sends</div>
      {assets_b}
    </div>
  </div>

  <!-- VALUATION -->
  <div class="valuation-block">
    <div class="val-section-title">Trade Health</div>
    <div class="val-row">
      <span class="val-team">{team_a}</span>
      <span class="val-pts {'val-highlight' if val_a > val_b else ''}">{val_a:,} pts</span>
    </div>
    <div class="val-row">
      <span class="val-team">{team_b}</span>
      <span class="val-pts {'val-highlight' if val_b >= val_a else ''}">{val_b:,} pts</span>
    </div>
    {fairness_bar}
    <div style="text-align:center; margin-top:6px; font-size:11px; color:#888;">
      {delta:.1f}% {delta_arrow} favors <strong style="color:#C9A84C">{favored}</strong>
    </div>
  </div>

  <!-- BAND + OVR METRICS -->
  <div class="metrics-row">
    <div class="metric-cell">
      <div class="metric-label">Fairness Band</div>
      <div class="metric-value {f'band-{band.lower()}'}">{band_emoji} {band_label}</div>
      <div class="metric-sub">{'✓ Legal range' if band != 'RED' else '✗ Outside legal range'}</div>
    </div>
    <div class="metric-cell">
      <div class="metric-label">OVR Delta</div>
      <div class="metric-value" style="color:#e8e0d0;">
        {'▲' if ovr_delta > 0 else '▼' if ovr_delta < 0 else '='} {abs(ovr_delta)}
      </div>
      <div class="metric-sub">Combined OVR difference</div>
    </div>
    <div class="metric-cell">
      <div class="metric-label">Decision</div>
      <div class="metric-value" style="color:{band_color}; font-size:13px;">
        {'Commissioner Required' if band in ('RED','YELLOW') else 'Auto-Eligible'}
      </div>
    </div>
  </div>

  <!-- FLAGS -->
  {notes_html}

  <!-- AI COMMENTARY -->
  {ai_html}

  <!-- FOOTER -->
  <div class="card-footer">
    <span class="footer-id">TRADE ID: {trade_id} · PROPOSED BY {proposer_id}</span>
    <span class="footer-engine">TSL Trade Engine v2.7</span>
  </div>

</div>
</body>
</html>"""
    return html


# ── Main render function ──────────────────────────────────────────────────────

async def render_trade_card(data: dict) -> bytes | None:
    """
    Render a trade card to PNG bytes.
    Returns None on failure (caller should fall back to embed).
    """
    try:
        browser = await _get_browser()
        page    = await browser.new_page(viewport={"width": 620, "height": 1200})

        html = _build_html(data)
        await page.set_content(html, wait_until="networkidle")

        # Size to content
        card = await page.query_selector(".card")
        if card:
            box = await card.bounding_box()
            if box:
                await page.set_viewport_size({
                    "width": 620,
                    "height": int(box["height"]) + 20
                })

        png_bytes = await page.screenshot(
            clip={"x": 0, "y": 0, "width": 620, "height": int(box["height"]) + 20} if card and box else None,
            type="png",
        )
        await page.close()
        return png_bytes

    except Exception as e:
        print(f"[card_renderer] Render error: {e}")
        return None
