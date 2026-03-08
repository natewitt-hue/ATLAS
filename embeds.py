"""
embeds.py
Converts structured data dicts from analysis.py into Discord Embed objects.
Uses each team's real primary color from teams.json.

v2 additions:
  - trade_grade_embed()     : Renders a graded trade card with equity breakdown
  - player_equity_embed()   : Single player equity score card
  - chart_file()            : Wraps a ChartResult buffer as discord.File
  - recap_event_embed()     : Rich weekly recap embed (event-driven recaps)
  - build_embeds()          : Extended to handle new types
  - sql_result_embed()      : Robust handler for Text-to-SQL results (Fixed)
  - rule_lookup_embed()     : Clean formatting for league rules (Fixed)
"""

import io
import discord
import data_manager as dm

# Fallback color if team not found
DEFAULT_COLOR = 0x2B2D31

def _team_color(team_name: str) -> discord.Color:
    return discord.Color(dm.get_team_color(team_name))

def _form_emoji(win: bool) -> str:
    return "🟢" if win else "🔴"

def _dev_emoji(dev: str) -> str:
    return {"Superstar X-Factor": "⚡", "Superstar": "🌟", "Star": "⭐"}.get(dev, "")

# ── Visual Analytics ─────────────────────────────────────────────────────────

def chart_file(chart_result, filename: str = "chart.png") -> discord.File | None:
    if not chart_result:
        return None
    try:
        chart_result.buf.seek(0)
        return discord.File(chart_result.buf, filename=filename)
    except Exception:
        return None

def chart_embed(title, description, filename):
    """Creates a Discord embed that references an attached image file."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blue()
    )
    embed.set_image(url=f"attachment://{filename}")
    return embed

# ── Robust SQL & Rule Embeds (Fixed) ──────────────────────────────────────────

def sql_result_embed(result, query):
    """
    Creates an embed for SQL results. 
    Handles cases where result is a dictionary OR a raw list to prevent crashes.
    """
    embed = discord.Embed(
        title="📜 TSL History Search",
        description=f"Query: *{query[:100]}*",
        color=discord.Color.from_rgb(88, 101, 242) # Discord Blurple
    )

    # 1. Type-Check: Determine if we have a dictionary or a raw list
    if isinstance(result, dict):
        rows = result.get("rows") or result.get("data") or []
        sql_used = result.get("sql", "N/A")
        attempts = result.get("attempts", 1)
    else:
        rows = result # It's already a list
        sql_used = "N/A"
        attempts = 1

    if not rows:
        embed.add_field(name="Result", value="No matches found in the 2M message archive.")
        return embed

    # 2. Handle 'Count' queries (e.g., "How many times...")
    # Usually returns a single row with one value like [(5,)]
    first_row = rows[0]
    if len(rows) == 1 and isinstance(first_row, (list, tuple)) and len(first_row) == 1:
        count_val = first_row[0]
        embed.add_field(name="Total Occurrences", value=f"**{count_val:,}**" if isinstance(count_val, int) else f"**{count_val}**", inline=False)
    
    # 3. Handle standard message results
    else:
        text_preview = ""
        for row in rows[:5]: # Show first 5 matches
            if isinstance(row, dict):
                ts = str(row.get("timestamp", ""))[:10]
                auth = row.get("author", "Unknown")
                cont = str(row.get("content", ""))[:100]
                text_preview += f"**{auth}** ({ts}): {cont}\n\n"
            else:
                text_preview += f"• {str(row)[:150]}\n"
        
        if len(rows) > 5:
            text_preview += f"*(and {len(rows)-5} more matches)*"
            
        embed.add_field(name=f"Matches Found ({len(rows)})", value=text_preview or "Empty result set.", inline=False)

    if sql_used != "N/A":
        embed.add_field(name="🔍 SQL Engine", value=f"```sql\n{sql_used[:150]}\n```", inline=False)

    embed.set_footer(text=f"TSL Archive | {attempts} attempt(s) | WittGPT Text-to-SQL")
    return embed

def rule_lookup_embed(rule_data, query):
    """Formats league rules into a clean gold card."""
    embed = discord.Embed(
        title=f"📋 TSL Rulebook: {query[:50]}",
        color=discord.Color.gold()
    )
    
    if isinstance(rule_data, dict):
        # Handle dictionary hits from search
        if "text" in rule_data:
            embed.description = rule_data["text"][:1000]
            embed.add_field(name="Section", value=f"§{rule_data.get('section', '?')}")
        else:
            embed.description = rule_data.get("description", "No details available.")
            if "penalty" in rule_data:
                embed.add_field(name="Penalty", value=rule_data["penalty"], inline=False)
    elif isinstance(rule_data, list) and len(rule_data) > 0:
        # Handle list of search hits
        first = rule_data[0]
        embed.description = first.get("text", str(first))[:1000]
    else:
        embed.description = str(rule_data)
        
    embed.set_footer(text="TSL Revolution Rulebook v26 | WittGPT Engine")
    return embed

# ── Trade, Player, and Team Cards ────────────────────────────────────────────

def trade_grade_embed(data: dict) -> discord.Embed:
    winner  = data.get("winner", "?")
    loser   = data.get("loser",  "?")
    grade   = data.get("grade",  "?")
    verdict = data.get("verdict", "")

    color = _grade_color(grade)

    embed = discord.Embed(
        title=f"⚖️ Trade Grade: **{grade}** — {data.get('team_a','?')} ↔ {data.get('team_b','?')}",
        description=(
            f"🏆 **Winner:** {winner}   💀 **Loser:** {loser}\n"
            f"_{verdict}_\n\n"
            f"Equity gap: **{data.get('equity_diff','?')}pts** ({data.get('equity_diff_pct','?')}% imbalance)"
        ),
        color=color,
    )

    def _fmt_assets(assets: list) -> str:
        if not assets: return "_Nothing_"
        lines = []
        for a in assets:
            icon = "🏈" if a["type"] == "player" else "🎟️"
            lines.append(f"{icon} **{a['name']}** — `{a['detail']}` · Equity: **{a['equity']:.0f}**")
        return "\n".join(lines)

    embed.add_field(name=f"📤 {data.get('team_a','A')} Sent", value=_fmt_assets(data.get("side_a_assets", [])), inline=False)
    embed.add_field(name=f"📤 {data.get('team_b','B')} Sent", value=_fmt_assets(data.get("side_b_assets", [])), inline=False)
    return embed

def player_equity_embed(data: dict) -> discord.Embed:
    if "error" in data:
        return discord.Embed(title="Player Equity", description=data["error"], color=discord.Color.red())
    equity = data["total_equity"]
    color = 0x00FF88 if equity >= 75 else 0xCCFF00 if equity >= 55 else 0xFFAA00
    embed = discord.Embed(title=f"💰 {data['name']} — {data['pos']}", description=f"**Total Equity: {equity:.1f}**", color=color)
    
    bar_len = int(equity / 100 * 20)
    bar_fill = "█" * bar_len + "░" * (20 - bar_len)
    embed.add_field(name="Equity Meter", value=f"`{bar_fill}` {equity:.1f}", inline=False)
    return embed

def player_embed(data: dict) -> discord.Embed:
    name = data.get("name", "Unknown")
    team = data.get("team", "FA")
    pos = data.get("pos", "")
    bio = data.get("bio", {})
    abilities = data.get("abilities", [])
    
    embed = discord.Embed(title=f"{name} — {pos} | {team}", color=_team_color(team))
    if bio:
        bio_line = f"Age {bio.get('Age','?')} · OVR {bio.get('OVR','?')} · Dev {bio.get('Dev','?')}"
        embed.add_field(name="📋 Roster", value=bio_line, inline=False)
    if abilities:
        embed.add_field(name="⚡ Abilities", value="\n".join(f"• {a}" for a in abilities), inline=False)
    return embed

def team_embed(data: dict) -> discord.Embed:
    team = data.get("team", "Unknown")
    rec = data.get("record", "?-?-?")
    embed = discord.Embed(title=f"🏈 {team} | {rec}", color=_team_color(team))
    embed.add_field(name="Owner", value=data.get("owner", "Unknown"))
    return embed

# ── Logic Blocks ─────────────────────────────────────────────────────────────

def build_embeds(data: dict) -> list[discord.Embed]:
    t = data.get("type")
    if t == "player": return [player_embed(data)]
    if t == "team": return [team_embed(data)]
    if t == "trade_grade": return [trade_grade_embed(data)]
    if t == "player_equity": return [player_equity_embed(data)]
    if t == "sql_result": return [sql_result_embed(data.get("sql_result", {}), data.get("question", ""))]
    return []

def _grade_color(grade: str) -> int:
    return {
        "A+": 0x00FF88, "A": 0x33FF66, "A-": 0x66FF44,
        "B+": 0x99FF00, "B": 0xCCFF00, "B-": 0xE6E600,
        "C+": 0xFFE600, "C": 0xFFCC00, "C-": 0xFFAA00,
        "D": 0xFF6600, "F": 0xFF2200,
    }.get(grade, 0x808080)

def pos_change_embed(data: dict) -> discord.Embed:
    verdict = data.get("verdict", "UNKNOWN")
    color = {"APPROVED": 0x00C851, "FAILED": 0xFF4444, "BANNED": 0xCC0000}.get(verdict, 0x808080)
    embed = discord.Embed(title=f"📋 {data.get('player','Player')} | Position Change: {verdict}", color=color)
    if data.get("reason"): embed.description = data["reason"]
    return embed