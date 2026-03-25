"""
oracle_renderer.py — ATLAS Oracle Card Renderer v1.0
─────────────────────────────────────────────────────────────────────────────
PNG card templates for the Oracle Intelligence Hub.
Two variants:
  - render_oracle_card()  → standard analysis card (6 of 8 types)
  - render_matchup_card() → matchup variant with prediction block

Pipeline: build_body_html() → wrap_card() → render_card() → PNG bytes

Workstream WS-3 — no Discord imports, no database calls.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import html as html_mod
import io

from atlas_html_engine import esc, render_card, wrap_card
from atlas_style_tokens import Tokens

# ── ATLAS identity ─────────────────────────────────────────────────────────────
try:
    from constants import ATLAS_ICON_URL
except ImportError:
    ATLAS_ICON_URL = ""

# ── Analysis type → display label + accent color ──────────────────────────────
_TYPE_META: dict[str, dict] = {
    "matchup":  {"label": "Matchup Analysis",  "icon": "🏈", "accent": Tokens.GOLD},
    "rivalry":  {"label": "Rivalry History",   "icon": "⚔️",  "accent": Tokens.ROSE},
    "gameplan": {"label": "Game Plan",         "icon": "🎯", "accent": Tokens.ORANGE},
    "team":     {"label": "Team Report",       "icon": "📊", "accent": Tokens.BLUE_LIGHT},
    "owner":    {"label": "Owner Profile",     "icon": "👤", "accent": Tokens.PURPLE},
    "player":   {"label": "Player Scout",      "icon": "🔭", "accent": Tokens.BLUE_SKY},
    "power":    {"label": "Power Rankings",    "icon": "📈", "accent": Tokens.WIN},
    "dynasty":  {"label": "Dynasty Profile",   "icon": "🏛️",  "accent": Tokens.GOLD_BRIGHT},
    "betting":  {"label": "Betting Profile",   "icon": "💰", "accent": Tokens.WIN},
}

_DEFAULT_META = {"label": "Oracle Analysis", "icon": "🔮", "accent": Tokens.GOLD}


# ── CSS for Oracle cards ──────────────────────────────────────────────────────
def _oracle_css(accent: str) -> str:
    return f"""
.oracle-header {{
  padding: 16px 20px 12px;
  background: linear-gradient(135deg, rgba(0,0,0,0.3) 0%, rgba(20,20,30,0.8) 100%);
  border-bottom: 1px solid {accent}44;
  display: flex;
  align-items: center;
  gap: 12px;
}}
.oracle-icon {{
  font-size: 28px;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: {accent}22;
  border-radius: 10px;
  flex-shrink: 0;
}}
.oracle-title-group {{
  flex: 1;
  min-width: 0;
}}
.oracle-label {{
  font-size: 11px;
  font-weight: 700;
  color: {accent};
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 2px;
}}
.oracle-title {{
  font-size: 16px;
  font-weight: 700;
  color: {Tokens.TEXT_PRIMARY};
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.oracle-meta {{
  font-size: 11px;
  color: {Tokens.TEXT_MUTED};
  text-align: right;
  white-space: nowrap;
}}
.oracle-body {{
  padding: 16px 20px;
}}
.oracle-section {{
  margin-bottom: 12px;
}}
.oracle-section-label {{
  font-size: 11px;
  font-weight: 700;
  color: {accent};
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid {accent}33;
}}
.oracle-content {{
  font-size: 13px;
  line-height: 1.6;
  color: {Tokens.TEXT_PRIMARY};
  white-space: pre-wrap;
  word-wrap: break-word;
}}
.oracle-content b, .oracle-content strong {{
  color: #fff;
  font-weight: 700;
}}
.oracle-prediction {{
  margin: 14px 0 0;
  padding: 14px 16px;
  background: {accent}18;
  border: 1px solid {accent}55;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}}
.oracle-prediction-label {{
  font-size: 10px;
  font-weight: 700;
  color: {accent};
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 4px;
}}
.oracle-prediction-score {{
  font-size: 20px;
  font-weight: 800;
  color: {Tokens.TEXT_PRIMARY};
  font-family: 'JetBrains Mono', monospace;
}}
.oracle-confidence {{
  text-align: right;
}}
.oracle-confidence-label {{
  font-size: 10px;
  color: {Tokens.TEXT_MUTED};
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-bottom: 2px;
}}
.oracle-confidence-value {{
  font-size: 15px;
  font-weight: 700;
  color: {accent};
}}
.oracle-footer {{
  padding: 10px 20px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-top: 1px solid rgba(255,255,255,0.05);
}}
.oracle-footer-brand {{
  font-size: 11px;
  color: {Tokens.TEXT_MUTED};
  letter-spacing: 0.5px;
}}
.oracle-footer-model {{
  font-size: 10px;
  color: {Tokens.TEXT_DIM};
}}
"""


def _sparkline_svg(values: list[float], width: int = 220, height: int = 36, color: str = "#FFD700") -> str:
    """Render a list of floats as an inline SVG polyline sparkline."""
    if len(values) < 2:
        return ""
    mn, mx = min(values), max(values)
    span = mx - mn or 1
    step = width / (len(values) - 1)
    points = []
    for i, v in enumerate(values):
        x = round(i * step, 1)
        y = round(height - ((v - mn) / span) * (height - 4) - 2, 1)
        points.append(f"{x},{y}")
    pts_str = " ".join(points)
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="overflow:visible">'
        f'<polyline points="{pts_str}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{points[-1].split(",")[0]}" cy="{points[-1].split(",")[1]}" '
        f'r="3" fill="{color}"/>'
        f'</svg>'
    )


def _build_comparison_table_html(comp: dict, accent: str) -> str:
    """Build a two-column stat comparison table for matchup cards."""
    ta = comp.get("team_a", {})
    tb = comp.get("team_b", {})
    rows = [
        ("Rank",   ta.get("rank", "?"),           tb.get("rank", "?")),
        ("Record", ta.get("record", "?-?"),       tb.get("record", "?-?")),
        ("OVR",    ta.get("ovr", "?"),             tb.get("ovr", "?")),
        ("PPG",    str(ta.get("ppg", 0.0)),        str(tb.get("ppg", 0.0))),
        ("PA/G",   str(ta.get("pa", 0.0)),         str(tb.get("pa", 0.0))),
        ("Diff",   f"{ta.get('diff', 0):+d}" if isinstance(ta.get('diff'), int) else "?",
                   f"{tb.get('diff', 0):+d}" if isinstance(tb.get('diff'), int) else "?"),
    ]
    name_a = esc(str(ta.get("name", "Team A")))
    name_b = esc(str(tb.get("name", "Team B")))
    rows_html = ""
    for i, (label, val_a, val_b) in enumerate(rows):
        bg = f"background:rgba(255,255,255,0.03);" if i % 2 == 0 else ""
        rows_html += (
            f'<div style="display:grid;grid-template-columns:90px 1fr 1fr;'
            f'gap:4px;padding:5px 8px;{bg}border-radius:4px;">'
            f'<div style="font-size:10px;font-weight:700;color:{accent};'
            f'text-transform:uppercase;letter-spacing:0.8px;align-self:center;">{esc(label)}</div>'
            f'<div style="font-size:12px;font-weight:600;color:#fff;text-align:center;">{esc(str(val_a))}</div>'
            f'<div style="font-size:12px;font-weight:600;color:#fff;text-align:center;">{esc(str(val_b))}</div>'
            f'</div>'
        )
    return (
        f'<div style="margin-bottom:14px;border:1px solid {accent}33;border-radius:8px;overflow:hidden;">'
        f'<div style="display:grid;grid-template-columns:90px 1fr 1fr;gap:4px;'
        f'padding:7px 8px;background:{accent}22;">'
        f'<div style="font-size:10px;color:{accent};font-weight:700;letter-spacing:0.5px;"></div>'
        f'<div style="font-size:12px;font-weight:800;color:{accent};text-align:center;">{name_a}</div>'
        f'<div style="font-size:12px;font-weight:800;color:{accent};text-align:center;">{name_b}</div>'
        f'</div>'
        f'{rows_html}'
        f'</div>'
    )


def _discord_md_to_html(text: str) -> str:
    """Convert basic Discord markdown (**bold**, *italic*) to HTML for card rendering."""
    import re
    # Bold: **text** → <b>text</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Italic: *text* → <i>text</i>
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    return text


def _build_analysis_body(result, accent: str, meta: dict, season: int, week: int) -> str:
    """Build inner HTML for a standard analysis card."""
    title_escaped = esc(result.title)
    label = esc(meta["label"])
    icon = meta["icon"]
    season_label = esc(f"Season {season} · Week {week}")
    model_label = esc(result.metadata.get("model", "") or "")

    # Comparison table (matchup only — when comparison_data is present)
    compare_html = ""
    if getattr(result, "comparison_data", None):
        compare_html = _build_comparison_table_html(result.comparison_data, accent)

    # Build section blocks
    sections_html = ""
    for section in result.sections:
        sec_label = esc(section.get("label", "Analysis"))
        raw_content = section.get("content", "")
        # Convert Discord markdown and HTML-escape remaining unsafe chars
        content_html = _discord_md_to_html(html_mod.escape(raw_content).replace("&amp;", "&"))
        sections_html += f"""
        <div class="oracle-section">
          <div class="oracle-section-label">{sec_label}</div>
          <div class="oracle-content">{content_html}</div>
        </div>"""

    # Prediction block (matchup only)
    prediction_html = ""
    if result.prediction:
        pred_escaped = esc(result.prediction)
        conf_escaped = esc(result.confidence or "Medium")
        prediction_html = f"""
        <div class="oracle-prediction">
          <div>
            <div class="oracle-prediction-label">Prediction</div>
            <div class="oracle-prediction-score">{pred_escaped}</div>
          </div>
          <div class="oracle-confidence">
            <div class="oracle-confidence-label">Confidence</div>
            <div class="oracle-confidence-value">{conf_escaped}</div>
          </div>
        </div>"""

    return f"""
    <div class="oracle-header">
      <div class="oracle-icon">{icon}</div>
      <div class="oracle-title-group">
        <div class="oracle-label">{label}</div>
        <div class="oracle-title">{title_escaped}</div>
      </div>
      <div class="oracle-meta">{season_label}</div>
    </div>
    <div class="oracle-body">
      {compare_html}
      {sections_html}
      {prediction_html}
    </div>
    <div class="oracle-footer">
      <div class="oracle-footer-brand">ATLAS™ Oracle Intelligence</div>
      <div class="oracle-footer-model">{model_label}</div>
    </div>"""


async def render_oracle_card(result, *, theme_id: str | None = None) -> bytes:
    """
    Render an AnalysisResult to a PNG card.
    Works for all 8 analysis types — matchup prediction block included when present.

    Parameters
    ----------
    result : AnalysisResult
        The analysis result from oracle_analysis.py.

    Returns
    -------
    bytes
        PNG image bytes ready for discord.File().
    """
    meta = _TYPE_META.get(result.analysis_type, _DEFAULT_META)
    accent = meta["accent"]
    season = result.metadata.get("season", 0)
    week = result.metadata.get("week", 0)

    body_html = _build_analysis_body(result, accent, meta, season, week)
    css = _oracle_css(accent)

    # Inject Oracle-specific CSS into the card via a wrapped template
    # We embed the CSS as a style block inside the body HTML, which wrap_card
    # will include alongside the shared card CSS.
    body_with_css = f"<style>{css}</style>{body_html}"
    full_html = wrap_card(body_with_css, "", theme_id=theme_id)
    return await render_card(full_html)


async def render_oracle_card_to_file(result, filename: str = "oracle_card.png", *, theme_id: str | None = None):
    """
    Render and return a discord.File-compatible bytes buffer.
    Import discord locally to avoid circular imports.
    """
    import discord
    png_bytes = await render_oracle_card(result, theme_id=theme_id)
    buf = io.BytesIO(png_bytes)
    buf.seek(0)
    return discord.File(buf, filename=filename)
