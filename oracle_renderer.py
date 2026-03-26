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
from data_manager import week_label as _week_label
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
.oracle-content .analysis-h2 {{
  font-size: 12px;
  font-weight: 800;
  color: {accent};
  letter-spacing: 0.5px;
  margin: 12px 0 4px;
  padding-bottom: 3px;
  border-bottom: 1px solid {accent}33;
  white-space: normal;
}}
.oracle-content .analysis-h3 {{
  font-size: 11px;
  font-weight: 700;
  color: {accent}BB;
  margin: 8px 0 2px;
  white-space: normal;
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
    """Build a two-column stat comparison table for matchup cards with per-row edge indicators."""
    ta = comp.get("team_a", {})
    tb = comp.get("team_b", {})

    def _cmp(a_raw, b_raw, lower_better: bool = False):
        """Return (a_wins, b_wins) booleans. (False, False) when values are equal or unknown."""
        try:
            a = float(str(a_raw).replace("#", "").replace("+", "").strip())
            b = float(str(b_raw).replace("#", "").replace("+", "").strip())
            if a == b or a == 0 and b == 0:
                return False, False
            if lower_better:
                return a < b, a > b
            return a > b, a < b
        except Exception:
            return False, False

    off_a = ta.get("off_rank", 0) or 0
    off_b = tb.get("off_rank", 0) or 0
    def_a = ta.get("def_rank", 0) or 0
    def_b = tb.get("def_rank", 0) or 0
    to_a  = ta.get("to_diff", 0) or 0
    to_b  = tb.get("to_diff", 0) or 0
    diff_a = ta.get("diff", 0) or 0
    diff_b = tb.get("diff", 0) or 0

    rows = [
        ("Rank",     ta.get("rank","?"),    tb.get("rank","?"),    *_cmp(ta.get("rank","?"), tb.get("rank","?"), lower_better=True)),
        ("Record",   ta.get("record","?"),  tb.get("record","?"),  *_cmp(ta.get("win_pct",0), tb.get("win_pct",0))),
        ("OVR",      ta.get("ovr","?"),     tb.get("ovr","?"),     *_cmp(ta.get("ovr",0), tb.get("ovr",0))),
        ("PPG",      str(ta.get("ppg",0.0)),str(tb.get("ppg",0.0)),*_cmp(ta.get("ppg",0), tb.get("ppg",0))),
        ("PA/G",     str(ta.get("pa",0.0)), str(tb.get("pa",0.0)), *_cmp(ta.get("pa",0), tb.get("pa",0), lower_better=True)),
        ("Net Pts",  f"{diff_a:+d}",        f"{diff_b:+d}",        *_cmp(diff_a, diff_b)),
        ("Off Rank", f"#{off_a}" if off_a else "–", f"#{off_b}" if off_b else "–", *_cmp(off_a, off_b, lower_better=True)),
        ("Def Rank", f"#{def_a}" if def_a else "–", f"#{def_b}" if def_b else "–", *_cmp(def_a, def_b, lower_better=True)),
        ("TO Diff",  f"{to_a:+d}",          f"{to_b:+d}",          *_cmp(to_a, to_b)),
    ]

    name_a = esc(str(ta.get("name", "Team A")))
    name_b = esc(str(tb.get("name", "Team B")))

    edges_a = sum(1 for *_, aw, bw in rows if aw)
    edges_b = sum(1 for *_, aw, bw in rows if bw)
    total_e = edges_a + edges_b or 1
    pct_a   = round(edges_a / total_e * 100)

    rows_html = ""
    for i, (label, val_a, val_b, a_wins, b_wins) in enumerate(rows):
        row_bg = "background:rgba(255,255,255,0.025);" if i % 2 == 0 else ""
        base_cell = "font-size:12px;font-weight:700;text-align:center;padding:3px 6px;border-radius:3px;"
        if a_wins:
            sty_a = f"{base_cell}color:{accent};background:{accent}1A;"
            sty_b = f"{base_cell}color:rgba(255,255,255,0.4);"
        elif b_wins:
            sty_a = f"{base_cell}color:rgba(255,255,255,0.4);"
            sty_b = f"{base_cell}color:{accent};background:{accent}1A;"
        else:
            sty_a = sty_b = f"{base_cell}color:#fff;"

        rows_html += (
            f'<div style="display:grid;grid-template-columns:76px 1fr 1fr;'
            f'gap:4px;padding:5px 8px;{row_bg}">'
            f'<div style="font-size:9px;font-weight:700;color:{accent}88;'
            f'text-transform:uppercase;letter-spacing:0.9px;align-self:center;">{esc(label)}</div>'
            f'<div style="{sty_a}">{esc(str(val_a))}</div>'
            f'<div style="{sty_b}">{esc(str(val_b))}</div>'
            f'</div>'
        )

    edge_bar = (
        f'<div style="padding:6px 8px 8px;border-top:1px solid {accent}22;">'
        f'<div style="font-size:9px;font-weight:700;color:{accent}77;'
        f'text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;">Statistical Edge</div>'
        f'<div style="display:flex;border-radius:3px;overflow:hidden;height:4px;">'
        f'<div style="width:{pct_a}%;background:{accent};"></div>'
        f'<div style="flex:1;background:rgba(255,255,255,0.12);"></div>'
        f'</div>'
        f'<div style="display:flex;justify-content:space-between;margin-top:3px;">'
        f'<div style="font-size:9px;color:{accent};">{name_a} · {edges_a}/{total_e} cats</div>'
        f'<div style="font-size:9px;color:rgba(255,255,255,0.35);">{name_b} · {edges_b}/{total_e} cats</div>'
        f'</div>'
        f'</div>'
    )

    return (
        f'<div style="margin-bottom:14px;border:1px solid {accent}33;border-radius:8px;overflow:hidden;">'
        f'<div style="display:grid;grid-template-columns:76px 1fr 1fr;'
        f'padding:8px 8px 7px;background:{accent}18;">'
        f'<div></div>'
        f'<div style="font-size:13px;font-weight:800;color:{accent};text-align:center;">{name_a}</div>'
        f'<div style="font-size:13px;font-weight:800;color:{accent};text-align:center;">{name_b}</div>'
        f'</div>'
        f'{rows_html}'
        f'{edge_bar}'
        f'</div>'
    )


def _discord_md_to_html(text: str) -> str:
    """Convert Discord/markdown to HTML for card rendering."""
    import re
    # H3 headings: ### text → styled heading
    text = re.sub(r'^### (.+)$', r'<div class="analysis-h3">\1</div>', text, flags=re.MULTILINE)
    # H2 headings: ## text → styled heading (strip — sections already have labels)
    text = re.sub(r'^## (.+)$', r'<div class="analysis-h2">\1</div>', text, flags=re.MULTILINE)
    # Bold: **text** → <b>text</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Italic (not adjacent to another *): *text* → <i>text</i>
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    return text


def _build_analysis_body(result, accent: str, meta: dict, season: int, week: int) -> str:
    """Build inner HTML for a standard analysis card."""
    title_escaped = esc(result.title)
    label = esc(meta["label"])
    icon = meta["icon"]
    season_label = esc(f"Season {season} · {_week_label(week)}")
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
        conf_level = result.confidence or "Medium"
        conf_escaped = esc(conf_level)
        conf_color = {
            "High":   Tokens.WIN,
            "Medium": Tokens.ORANGE,
            "Low":    Tokens.ROSE,
        }.get(conf_level, accent)

        # Win probability bar from comparison_data (if available)
        winprob_html = ""
        cd = getattr(result, "comparison_data", None)
        if cd:
            ta_m = cd.get("team_a", {})
            tb_m = cd.get("team_b", {})
            try:
                wp_a = float(ta_m.get("win_pct", 0) or 0)
                wp_b = float(tb_m.get("win_pct", 0) or 0)
                ovr_a = float(str(ta_m.get("ovr", 85)).replace("?", "85"))
                ovr_b = float(str(tb_m.get("ovr", 85)).replace("?", "85"))
                # Blend win% (80%) with OVR gap (20%)
                total_wp = (wp_a + wp_b) or 1.0
                base_prob = wp_a / total_wp
                ovr_adj = (ovr_a - ovr_b) * 0.005  # 0.5% per OVR point
                prob_a = max(0.12, min(0.88, base_prob + ovr_adj))
                pct_a = round(prob_a * 100)
                pct_b = 100 - pct_a
                n_a = esc(str(ta_m.get("name", "")))
                n_b = esc(str(tb_m.get("name", "")))
                winprob_html = (
                    f'<div style="margin-bottom:10px;">'
                    f'<div style="font-size:9px;font-weight:700;color:{accent}77;'
                    f'text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;">Win Probability</div>'
                    f'<div style="display:flex;border-radius:4px;overflow:hidden;height:6px;background:rgba(255,255,255,0.1);">'
                    f'<div style="width:{pct_a}%;background:{accent};"></div>'
                    f'</div>'
                    f'<div style="display:flex;justify-content:space-between;margin-top:3px;">'
                    f'<div style="font-size:10px;font-weight:700;color:{accent};">{n_a} {pct_a}%</div>'
                    f'<div style="font-size:10px;color:rgba(255,255,255,0.4);">{n_b} {pct_b}%</div>'
                    f'</div>'
                    f'</div>'
                )
            except Exception:
                pass

        prediction_html = f"""
        <div class="oracle-prediction">
          {winprob_html}
          <div style="display:flex;align-items:center;justify-content:space-between;">
            <div>
              <div class="oracle-prediction-label">Prediction</div>
              <div class="oracle-prediction-score">{pred_escaped}</div>
            </div>
            <div class="oracle-confidence">
              <div class="oracle-confidence-label">Confidence</div>
              <div class="oracle-confidence-value" style="color:{conf_color};">{conf_escaped}</div>
            </div>
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
