"""
oracle_analysis.py — ATLAS Oracle Intelligence Pipelines v1.0
─────────────────────────────────────────────────────────────────────────────
Eight predefined analysis functions powering the Oracle Intelligence Hub.
Each function gathers all relevant TSL data, builds a richly structured
prompt, calls atlas_ai.generate(), and returns an AnalysisResult.

Workstream WS-2 — no Discord imports. Pure data + AI logic.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import atlas_ai
from atlas_ai import Tier

_log = logging.getLogger("oracle_analysis")

# ── Optional database access ──────────────────────────────────────────────────
run_sql = None
fuzzy_resolve_user = None
try:
    from codex_utils import run_sql, fuzzy_resolve_user
except ImportError:
    _log.warning("[oracle_analysis] codex_utils not available — SQL queries disabled")

# ── Persona ───────────────────────────────────────────────────────────────────
try:
    from echo_loader import get_persona
except ImportError:
    get_persona = lambda _: "You are ATLAS, the TSL intelligence system."


# ══════════════════════════════════════════════════════════════════════════════
#  RESULT DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AnalysisResult:
    title: str
    analysis_type: str          # "matchup" | "rivalry" | "gameplan" | "team" |
                                # "owner" | "player" | "power" | "dynasty"
    sections: list[dict]        # [{label, content, data_rows?}]
    prediction: str | None      # e.g. "Chiefs 31 – Raiders 17" (matchup only)
    confidence: str | None      # "High" | "Medium" | "Low"
    metadata: dict = field(default_factory=dict)  # {tier, model, season, week}


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_team(name: str, dm) -> str | None:
    """Fuzzy-match a team name to a canonical team name from dm.df_teams."""
    if dm.df_teams.empty:
        return None
    name_lower = name.lower().strip()
    # Exact match first (nickName or cityName)
    for col in ("nickName", "cityName", "teamName"):
        if col not in dm.df_teams.columns:
            continue
        for val in dm.df_teams[col].dropna():
            if val.lower() == name_lower:
                return val
    # Partial match
    for col in ("nickName", "cityName", "teamName"):
        if col not in dm.df_teams.columns:
            continue
        for val in dm.df_teams[col].dropna():
            if name_lower in val.lower() or val.lower() in name_lower:
                return val
    return None


def _resolve_owner(name: str, dm) -> str | None:
    """Fuzzy-match an owner name to a canonical DB username from dm.df_teams."""
    if dm.df_teams.empty or fuzzy_resolve_user is None:
        return None
    result = fuzzy_resolve_user(name)
    if result:
        return result
    # Fallback: check userName column directly
    name_lower = name.lower().strip()
    for val in dm.df_teams["userName"].dropna():
        if name_lower in val.lower() or val.lower() in name_lower:
            return val
    return None


def _resolve_player(name: str, dm) -> str | None:
    """Fuzzy-match a player name against dm.df_players (partial match)."""
    if dm.df_players is None or dm.df_players.empty:
        return None
    name_lower = name.lower().strip()
    for idx, row in dm.df_players.iterrows():
        full = str(row.get("fullName", "")).lower()
        if name_lower in full or full in name_lower:
            return row.get("fullName", name)
    return name  # Return as-is and let SQL do the LIKE match


def _build_tsl_context(dm) -> str:
    """Boilerplate TSL league context block for AI prompts."""
    return (
        f"LEAGUE: The Simulation League (TSL) — Madden NFL sim league, 31 active teams, "
        f"{dm.CURRENT_SEASON} Super Bowl seasons of history.\n"
        f"CURRENT SEASON: Season {dm.CURRENT_SEASON}, Week {dm.CURRENT_WEEK}.\n"
    )


def _standings_block(dm, team_name: str | None = None) -> str:
    """Format standings data as text context. Filter to team_name if provided."""
    if dm.df_standings.empty:
        return ""
    df = dm.df_standings
    if team_name:
        mask = df["teamName"].str.lower() == team_name.lower()
        if not mask.any():
            mask = df["teamName"].str.lower().str.contains(team_name.lower(), na=False)
        df = df[mask]
    if df.empty:
        return ""
    lines = []
    for _, row in df.head(16).iterrows():
        tn = row.get("teamName", "?")
        w = row.get("totalWins", 0)
        l = row.get("totalLosses", 0)
        pf = row.get("totalPtsFor", "?")
        pa = row.get("totalPtsAgainst", "?")
        lines.append(f"  {tn}: {w}-{l}  PF:{pf}  PA:{pa}")
    return "STANDINGS:\n" + "\n".join(lines)


def _roster_block(team_name: str) -> str:
    """Pull top-25 players for a team from tsl_history.db."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT firstName || ' ' || lastName AS name, pos, "
        "CAST(playerBestOvr AS INTEGER) AS ovr, dev "
        "FROM players WHERE teamName = ? AND isFA != '1' "
        "ORDER BY CAST(playerBestOvr AS INTEGER) DESC LIMIT 25",
        (team_name,),
    )
    if err or not rows:
        return ""
    lines = [f"  {r['name']} ({r.get('pos','?')}) OVR {r.get('ovr','?')} [{r.get('dev','Normal')}]" for r in rows]
    return f"ROSTER ({team_name}):\n" + "\n".join(lines)


def _recent_games_block(team_name: str, n: int = 5) -> str:
    """Pull last N completed games for a team."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT homeTeamName, awayTeamName, homeScore, awayScore, "
        "seasonIndex, weekIndex "
        "FROM games "
        "WHERE status IN ('2','3') AND stageIndex='1' "
        "AND (homeTeamName=? OR awayTeamName=?) "
        "ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC "
        "LIMIT ?",
        (team_name, team_name, n),
    )
    if err or not rows:
        return ""
    lines = []
    for r in rows:
        home, away = r["homeTeamName"], r["awayTeamName"]
        hs, as_ = r["homeScore"], r["awayScore"]
        wk = int(r.get("weekIndex", 0)) + 1
        sn = r.get("seasonIndex", "?")
        lines.append(f"  S{sn}·W{wk}: {home} {hs} – {as_} {away}")
    return f"RECENT GAMES ({team_name}, last {n}):\n" + "\n".join(lines)


def _h2h_block(user_a: str, user_b: str) -> str:
    """Pull full H2H record between two DB usernames."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT homeTeamName, awayTeamName, homeScore, awayScore, "
        "winner_user, loser_user, seasonIndex, weekIndex "
        "FROM games "
        "WHERE status IN ('2','3') AND stageIndex='1' "
        "AND ((winner_user=? AND loser_user=?) OR (winner_user=? AND loser_user=?)) "
        "ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC",
        (user_a, user_b, user_b, user_a),
    )
    if err or not rows:
        return "No head-to-head history found."
    a_wins = sum(1 for r in rows if r.get("winner_user") == user_a)
    b_wins = sum(1 for r in rows if r.get("winner_user") == user_b)
    lines = [f"H2H RECORD: {user_a} {a_wins}–{b_wins} {user_b} ({len(rows)} games)"]
    for r in rows[:10]:
        home, away = r["homeTeamName"], r["awayTeamName"]
        hs, as_ = r["homeScore"], r["awayScore"]
        sn = r.get("seasonIndex", "?")
        wk = int(r.get("weekIndex", 0)) + 1
        winner = r.get("winner_user", "?")
        lines.append(f"  S{sn}·W{wk}: {home} {hs}–{as_} {away}  → {winner} wins")
    if len(rows) > 10:
        lines.append(f"  ... and {len(rows) - 10} more games")
    return "\n".join(lines)


def _career_block(db_username: str) -> str:
    """Pull career record + championship history for an owner."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT seasonIndex, "
        "SUM(CASE WHEN winner_user=? THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN loser_user=? THEN 1 ELSE 0 END) AS losses "
        "FROM games "
        "WHERE status IN ('2','3') AND stageIndex='1' "
        "AND (winner_user=? OR loser_user=?) "
        "GROUP BY seasonIndex ORDER BY CAST(seasonIndex AS INTEGER) ASC",
        (db_username, db_username, db_username, db_username),
    )
    if err or not rows:
        return f"No career data found for {db_username}."
    total_w = sum(r["wins"] for r in rows)
    total_l = sum(r["losses"] for r in rows)
    lines = [f"CAREER RECORD ({db_username}): {total_w}W–{total_l}L across {len(rows)} seasons"]
    for r in rows:
        sn = r["seasonIndex"]
        w, l = r["wins"], r["losses"]
        pct = round(w / (w + l) * 100) if (w + l) > 0 else 0
        lines.append(f"  Season {sn}: {w}-{l}  ({pct}%)")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  ANALYSIS PIPELINES
# ══════════════════════════════════════════════════════════════════════════════

_TSL_ONLY_RULE = (
    "\n\nCRITICAL RULE: You are ATLAS, the TSL intelligence system. You ONLY analyze TSL data. "
    "All analysis must be grounded in the data provided above. Never fabricate stats or invent players. "
    "If data is sparse, say so concisely and work with what you have."
)


async def run_matchup_analysis(team_a: str, team_b: str, dm, bot=None) -> AnalysisResult:
    """Deep head-to-head matchup analysis with score prediction."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    # Gather data
    standings_a = _standings_block(dm, team_a)
    standings_b = _standings_block(dm, team_b)
    roster_a = _roster_block(team_a)
    roster_b = _roster_block(team_b)
    games_a = _recent_games_block(team_a, 5)
    games_b = _recent_games_block(team_b, 5)

    # Resolve usernames for H2H
    owner_a = _resolve_owner(team_a, dm)
    owner_b = _resolve_owner(team_b, dm)
    h2h = ""
    if owner_a and owner_b:
        h2h = _h2h_block(owner_a, owner_b)

    # Power rankings context
    power_context = ""
    if not dm.df_power.empty:
        for _, row in dm.df_power.iterrows():
            tn = row.get("teamName", "")
            if team_a.lower() in tn.lower() or team_b.lower() in tn.lower():
                rank = row.get("rank", "?")
                score = row.get("score", "?")
                power_context += f"  {tn} Power Rank: #{rank} (score {score})\n"

    data_block = "\n\n".join(filter(None, [ctx, standings_a, standings_b, roster_a, roster_b, games_a, games_b, h2h, power_context]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Matchup Analysis — {team_a} vs {team_b}

Write a deep pre-game breakdown covering:
1. OFFENSIVE EDGE: Compare passing and rushing attacks. Name specific players with OVR ratings.
2. DEFENSIVE EDGE: Which defense has the advantage and why. Name key defensive players.
3. KEY MATCHUP: The single most important individual matchup that could decide the game.
4. PREDICTION: Give a concrete score prediction (e.g. "Chiefs 28 – Raiders 17") with a one-line rationale. Do NOT hedge. Commit to a number.
5. CONFIDENCE: Rate your prediction High / Medium / Low based on data quality.

Format using Discord markdown (**bold** for player names and scores).
Be direct. Be specific. ATLAS doesn't hedge.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=700)
    text = result.text.strip()

    # Parse out prediction line
    prediction = None
    confidence = None
    for line in text.split("\n"):
        if "PREDICTION" in line.upper() and "–" in line:
            parts = line.split(":", 1)
            if len(parts) > 1:
                prediction = parts[1].strip().split(".")[0].strip()
        if "CONFIDENCE" in line.upper():
            for level in ("High", "Medium", "Low"):
                if level.lower() in line.lower():
                    confidence = level
                    break

    return AnalysisResult(
        title=f"Matchup Analysis: {team_a} vs {team_b}",
        analysis_type="matchup",
        sections=[{"label": "Analysis", "content": text}],
        prediction=prediction,
        confidence=confidence,
        metadata={"tier": "OPUS", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_rivalry_history(owner_a: str, owner_b: str, dm, bot=None) -> AnalysisResult:
    """Full historical rivalry breakdown between two TSL owners."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    db_a = _resolve_owner(owner_a, dm) or owner_a
    db_b = _resolve_owner(owner_b, dm) or owner_b
    h2h = _h2h_block(db_a, db_b)
    career_a = _career_block(db_a)
    career_b = _career_block(db_b)

    # Trades between the two owners
    trades_context = ""
    if not dm.df_trades.empty and "team1Name" in dm.df_trades.columns:
        relevant = dm.df_trades[
            dm.df_trades.apply(
                lambda r: (
                    (owner_a.lower() in str(r.get("team1Name", "")).lower() or
                     db_a.lower() in str(r.get("team1Name", "")).lower()) and
                    (owner_b.lower() in str(r.get("team2Name", "")).lower() or
                     db_b.lower() in str(r.get("team2Name", "")).lower())
                ) or (
                    (owner_b.lower() in str(r.get("team1Name", "")).lower() or
                     db_b.lower() in str(r.get("team1Name", "")).lower()) and
                    (owner_a.lower() in str(r.get("team2Name", "")).lower() or
                     db_a.lower() in str(r.get("team2Name", "")).lower())
                ),
                axis=1,
            )
        ]
        if not relevant.empty:
            trade_lines = []
            for _, row in relevant.head(5).iterrows():
                t1 = row.get("team1Name", "?")
                t2 = row.get("team2Name", "?")
                s1 = row.get("team1Sent", "?")
                s2 = row.get("team2Sent", "?")
                trade_lines.append(f"  {t1} sent {s1} ↔ {t2} sent {s2}")
            trades_context = "TRADES BETWEEN THEM:\n" + "\n".join(trade_lines)

    data_block = "\n\n".join(filter(None, [ctx, h2h, career_a, career_b, trades_context]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Rivalry History — {db_a} vs {db_b}

Write a compelling rivalry narrative covering:
1. THE RECORD: Who holds the historical edge and by how much.
2. ERA ANALYSIS: Have the fortunes ever shifted? Who dominated which era?
3. DEFINING MOMENTS: The 1-2 most significant games in this rivalry (biggest margin, playoff clash, etc).
4. THE DYNAMIC: What does this rivalry mean in the TSL ecosystem? Beef? Mutual respect? One-sided?
5. CURRENT TRAJECTORY: Based on recent form, where is this rivalry headed?

Format with **bold** for names and key stats. Make it feel like a genuine sports rivalry piece.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=700)

    return AnalysisResult(
        title=f"Rivalry History: {db_a} vs {db_b}",
        analysis_type="rivalry",
        sections=[{"label": "Rivalry Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "OPUS", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_game_plan(target_team: str, requesting_user_team: str | None, dm, bot=None) -> AnalysisResult:
    """Tactical blueprint for beating a specific opponent."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    roster_target = _roster_block(target_team)
    games_target = _recent_games_block(target_team, 5)
    standings_target = _standings_block(dm, target_team)

    # Defensive gaps: what they allow
    def_gaps = ""
    if run_sql is not None:
        rows, err = run_sql(
            "SELECT awayTeamName AS opp, homeScore, awayScore, homeTeamName "
            "FROM games WHERE status IN ('2','3') AND stageIndex='1' "
            "AND homeTeamName=? ORDER BY CAST(seasonIndex AS INTEGER) DESC, "
            "CAST(weekIndex AS INTEGER) DESC LIMIT 6",
            (target_team,),
        )
        if not err and rows:
            pts_allowed = [int(r.get("awayScore", 0)) for r in rows if r.get("awayScore")]
            avg_allowed = sum(pts_allowed) / len(pts_allowed) if pts_allowed else 0
            def_gaps = f"POINTS ALLOWED (home, last 6): avg {avg_allowed:.1f} pts/game\n  " + \
                       "  ".join([f"{r['awayTeamName']} scored {r['awayScore']}" for r in rows[:4]])

    # Requester's roster for personalized advice
    req_roster = ""
    if requesting_user_team:
        req_roster = _roster_block(requesting_user_team)

    data_block = "\n\n".join(filter(None, [ctx, standings_target, roster_target, games_target, def_gaps, req_roster]))

    user_context = f"The user asking owns {requesting_user_team}. Personalize the game plan to their roster where possible." if requesting_user_team else ""

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Game Plan — How to beat {target_team}

{user_context}

Write a concrete tactical blueprint with exactly 3 game plan points:
1. EXPLOIT THIS WEAKNESS: The single biggest vulnerability in their defense/offense. Be specific about which position group to attack.
2. NEUTRALIZE THIS THREAT: The one thing {target_team} does well that must be taken away. Who specifically is the threat?
3. EXECUTE THIS IDENTITY: The offensive or defensive identity that wins this game. What style/approach beats them?

Then give a one-paragraph OVERALL ASSESSMENT of how hard this team is to beat right now.

Use **bold** for player names, positions, and key tactical terms. Be specific — not generic football advice.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=650)

    return AnalysisResult(
        title=f"Game Plan: How to Beat {target_team}",
        analysis_type="gameplan",
        sections=[{"label": "Tactical Blueprint", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "OPUS", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_team_report(team_name: str, dm, bot=None) -> AnalysisResult:
    """Full team deep dive — identity, trajectory, strengths and weaknesses."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    standings = _standings_block(dm, team_name)
    roster = _roster_block(team_name)
    games = _recent_games_block(team_name, 6)

    # Recent trades
    trade_context = ""
    if not dm.df_trades.empty:
        relevant = dm.df_trades[
            dm.df_trades.apply(
                lambda r: team_name.lower() in str(r.get("team1Name", "")).lower() or
                          team_name.lower() in str(r.get("team2Name", "")).lower(),
                axis=1,
            )
        ].head(4)
        if not relevant.empty:
            lines = []
            for _, row in relevant.iterrows():
                t1, t2 = row.get("team1Name", "?"), row.get("team2Name", "?")
                s1, s2 = row.get("team1Sent", "?"), row.get("team2Sent", "?")
                lines.append(f"  {t1} sent {s1} ↔ {t2} sent {s2}")
            trade_context = f"RECENT TRADES ({team_name}):\n" + "\n".join(lines)

    power_rank = ""
    if not dm.df_power.empty:
        for _, row in dm.df_power.iterrows():
            if team_name.lower() in str(row.get("teamName", "")).lower():
                power_rank = f"POWER RANKING: #{row.get('rank','?')} (score {row.get('score','?')})"
                break

    data_block = "\n\n".join(filter(None, [ctx, standings, power_rank, roster, games, trade_context]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Team Report — {team_name}

Write a complete team analysis covering:
1. IDENTITY: What kind of team is {team_name}? Are they run-first? Pass-heavy? Defensive? Describe their playstyle identity in 2 sentences.
2. STRENGTHS: The 2 things this team does best. Name the specific players responsible.
3. WEAKNESSES: The 1-2 most exploitable gaps in their roster or strategy.
4. KEY PLAYER: The one player who most defines this team's ceiling. What happens if he underperforms?
5. TRAJECTORY: Trending up, down, or plateauing? What does the rest of their season look like?

Use **bold** for player names and ratings. Be direct — no hedging.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=600)

    return AnalysisResult(
        title=f"Team Report: {team_name}",
        analysis_type="team",
        sections=[{"label": "Team Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_owner_profile(owner_name: str, dm, bot=None) -> AnalysisResult:
    """Owner profile — current season, playstyle, and tendencies."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    db_username = _resolve_owner(owner_name, dm) or owner_name
    career = _career_block(db_username)

    # Find their team
    team_name = None
    if not dm.df_teams.empty:
        mask = dm.df_teams["userName"].str.lower() == db_username.lower()
        if mask.any():
            team_name = dm.df_teams[mask].iloc[0].get("nickName", "")

    standings = _standings_block(dm, team_name) if team_name else ""
    roster = _roster_block(team_name) if team_name else ""
    games = _recent_games_block(team_name, 5) if team_name else ""

    # Trade activity
    trade_context = ""
    if not dm.df_trades.empty and team_name:
        relevant = dm.df_trades[
            dm.df_trades.apply(
                lambda r: team_name.lower() in str(r.get("team1Name", "")).lower() or
                          team_name.lower() in str(r.get("team2Name", "")).lower(),
                axis=1,
            )
        ].head(5)
        if not relevant.empty:
            lines = []
            for _, row in relevant.iterrows():
                t1, t2 = row.get("team1Name", "?"), row.get("team2Name", "?")
                s1, s2 = row.get("team1Sent", "?"), row.get("team2Sent", "?")
                lines.append(f"  {t1} sent {s1} ↔ {t2} sent {s2}")
            trade_context = f"TRADES ({team_name}):\n" + "\n".join(lines)

    data_block = "\n\n".join(filter(None, [ctx, career, standings, roster, games, trade_context]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Owner Profile — {db_username}

Write a complete owner profile covering:
1. CURRENT SEASON: How are they performing this season? Record, momentum, trend.
2. PLAYSTYLE IDENTITY: What kind of manager are they? Trader? Builder? Run-first? Pass-heavy? Defend-first?
3. STRENGTHS: What do they consistently do well as an owner?
4. THIS SEASON'S OUTLOOK: Based on roster and schedule position, what's realistic for them?
5. CAREER CONTEXT: How does this season fit their historical baseline?

Use **bold** for their name and key stats. Keep it analytical — not fluff.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=600)

    return AnalysisResult(
        title=f"Owner Profile: {db_username}",
        analysis_type="owner",
        sections=[{"label": "Profile", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK, "team": team_name},
    )


async def run_player_scout(player_name: str, dm, bot=None) -> AnalysisResult:
    """Full scouting report on a TSL player."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    # Pull player data from tsl_history.db
    player_data = ""
    abilities_data = ""
    if run_sql is not None:
        rows, err = run_sql(
            "SELECT firstName || ' ' || lastName AS name, pos, age, "
            "CAST(playerBestOvr AS INTEGER) AS ovr, dev, teamName, "
            "speedRating, strengthRating, agilityRating, awareRating, "
            "throwPowerRating, throwAccShortRating, throwAccMedRating, throwAccDeepRating, "
            "catchRating, routeRunShortRating, routeRunMedRating, routeRunDeepRating, "
            "carryRating, jukeMoveRating, truckRating, breakTackleRating, "
            "tackleRating, hitPowerRating, pursuitRating, manCoverRating, zoneCoverRating, "
            "pressRating, blockSheddingRating, capHit, contractYearsLeft "
            "FROM players "
            "WHERE firstName || ' ' || lastName LIKE ? "
            "AND isFA != '1' LIMIT 3",
            (f"%{player_name}%",),
        )
        if not err and rows:
            player_data = "PLAYER DATA:\n" + json.dumps(rows[:2], indent=2)

        ab_rows, ab_err = run_sql(
            "SELECT title, description FROM player_abilities "
            "WHERE firstName || ' ' || lastName LIKE ? LIMIT 6",
            (f"%{player_name}%",),
        )
        if not ab_err and ab_rows:
            ab_lines = [f"  [{r['title']}]: {r['description']}" for r in ab_rows]
            abilities_data = "ABILITIES:\n" + "\n".join(ab_lines)

    data_block = "\n\n".join(filter(None, [ctx, player_data, abilities_data]))

    if not player_data:
        return AnalysisResult(
            title=f"Player Scout: {player_name}",
            analysis_type="player",
            sections=[{"label": "Not Found", "content": f"No TSL player found matching '{player_name}'. Check the spelling or use their full name."}],
            prediction=None,
            confidence=None,
            metadata={"tier": "SONNET", "model": "none", "season": dm.CURRENT_SEASON},
        )

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Player Scout Report — {player_name}

Write a complete scouting report covering:
1. OVERALL GRADE: Give a letter grade (A+/A/B+/B/C+/C/D) with a one-sentence justification.
2. ELITE ATTRIBUTES: The 2-3 ratings that make this player dangerous. Cite the actual numbers.
3. WEAKNESSES: The 1-2 ratings that limit this player. What game situations expose him?
4. DEVELOPMENT TRAJECTORY: What does their dev trait (Normal/Star/Superstar/XFactor) mean for their future value in TSL?
5. ABILITIES: Explain their active abilities and how they synergize (or don't).
6. BOTTOM LINE: One sentence on what this player is worth to a TSL roster.

Use **bold** for ratings and the grade. Be specific — cite actual rating numbers.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=600)

    return AnalysisResult(
        title=f"Player Scout: {player_name}",
        analysis_type="player",
        sections=[{"label": "Scout Report", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_power_rankings(dm, bot=None) -> AnalysisResult:
    """Tiered power rankings with riser/faller analysis."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    # Full standings
    standings = _standings_block(dm)

    # Power rankings data
    power_context = ""
    if not dm.df_power.empty:
        lines = []
        for _, row in dm.df_power.head(16).iterrows():
            tn = row.get("teamName", "?")
            rank = row.get("rank", "?")
            score = row.get("score", "?")
            lines.append(f"  #{rank} {tn} (power score: {score})")
        power_context = "CURRENT POWER RANKINGS:\n" + "\n".join(lines)

    # Momentum: last 4 weeks results (win/loss streak)
    momentum = ""
    if run_sql is not None and not dm.df_standings.empty:
        streak_lines = []
        for _, row in dm.df_standings.iterrows():
            tn = row.get("teamName", "")
            streak = row.get("winLossStreak", "0")
            if streak:
                try:
                    s = int(str(streak))
                    if s >= 3:
                        streak_lines.append(f"  {tn}: W{s} streak")
                    elif s <= -2:
                        streak_lines.append(f"  {tn}: L{abs(s)} streak")
                except Exception:
                    pass
        if streak_lines:
            momentum = "NOTABLE STREAKS:\n" + "\n".join(streak_lines)

    data_block = "\n\n".join(filter(None, [ctx, power_context, standings, momentum]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: TSL Power Rankings — Season {dm.CURRENT_SEASON}, Week {dm.CURRENT_WEEK}

Write a power rankings analysis covering:
1. ELITE TIER: Name the 2-3 teams that are clearly the best right now. What separates them?
2. CONTENDER TIER: The 4-6 teams with legitimate playoff upside. What do they need to do?
3. FRINGE / BUBBLE: Teams fighting for their playoff lives. Who's most dangerous from here?
4. REBUILDING: Teams that are done this season but may matter next year.
5. BIGGEST RISER: The one team trending up most dramatically. Why?
6. BIGGEST FALLER: The one team that has disappointed most. What went wrong?

Be opinionated. ATLAS has access to the full league data — use it. Name names. Cite records.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=700)

    return AnalysisResult(
        title=f"Power Rankings — Season {dm.CURRENT_SEASON} · Week {dm.CURRENT_WEEK}",
        analysis_type="power",
        sections=[{"label": "Rankings Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_dynasty_profile(owner_name: str, dm, bot=None) -> AnalysisResult:
    """Full legacy analysis — career record, championships, all-time rank."""
    persona = get_persona("analytical")
    ctx = _build_tsl_context(dm)

    db_username = _resolve_owner(owner_name, dm) or owner_name
    career = _career_block(db_username)

    # All-time rankings across all owners
    all_time_context = ""
    if run_sql is not None:
        rows, err = run_sql(
            "SELECT winner_user, COUNT(*) AS wins "
            "FROM games WHERE status IN ('2','3') AND stageIndex='1' "
            "AND winner_user IS NOT NULL AND winner_user != '' "
            "GROUP BY winner_user ORDER BY wins DESC LIMIT 20"
        )
        if not err and rows:
            lines = []
            my_rank = None
            for i, r in enumerate(rows, 1):
                marker = " ← " + db_username if r["winner_user"].lower() == db_username.lower() else ""
                lines.append(f"  #{i} {r['winner_user']}: {r['wins']} wins{marker}")
                if r["winner_user"].lower() == db_username.lower():
                    my_rank = i
            all_time_context = f"ALL-TIME WIN LEADERS (top 20):\n" + "\n".join(lines)
            if my_rank:
                all_time_context += f"\n\n{db_username} all-time rank: #{my_rank} of {len(rows)}+"

    # Best single season
    best_season = ""
    if run_sql is not None:
        rows2, err2 = run_sql(
            "SELECT seasonIndex, "
            "SUM(CASE WHEN winner_user=? THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN loser_user=? THEN 1 ELSE 0 END) AS losses "
            "FROM games WHERE status IN ('2','3') AND stageIndex='1' "
            "AND (winner_user=? OR loser_user=?) "
            "GROUP BY seasonIndex ORDER BY wins DESC LIMIT 3",
            (db_username, db_username, db_username, db_username),
        )
        if not err2 and rows2:
            best = rows2[0]
            best_season = f"BEST SEASON: Season {best['seasonIndex']} — {best['wins']}W-{best['losses']}L"

    # Current season context
    cur_standing = _standings_block(dm)

    data_block = "\n\n".join(filter(None, [ctx, career, all_time_context, best_season, cur_standing]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Dynasty Profile — {db_username}

Write a legacy analysis covering:
1. ALL-TIME STANDING: Where does {db_username} rank in TSL history? Use their actual win rank.
2. ERA ANALYSIS: Were they dominant early, mid, or recently? Did they have a dynasty window?
3. CAREER HIGHLIGHTS: Their best season(s). Peak performance in numbers.
4. CEILING AND FLOOR: What's the highest they've reached? What's the worst they've sunk to?
5. LEGACY LINE: A single definitive sentence on what {db_username}'s TSL legacy is. Make it land.
6. CURRENT SEASON: How does this season fit their arc — rise, fall, or plateau?

Be direct and analytical. This is a definitive assessment, not a tribute piece.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=700)

    return AnalysisResult(
        title=f"Dynasty Profile: {db_username}",
        analysis_type="dynasty",
        sections=[{"label": "Legacy Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "OPUS", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )
