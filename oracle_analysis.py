"""
oracle_analysis.py — ATLAS Oracle Intelligence Pipelines v3.0
─────────────────────────────────────────────────────────────────────────────
Nine predefined analysis functions powering the Oracle Intelligence Hub.
Each function gathers all relevant TSL data, builds a richly structured
prompt, calls atlas_ai.generate(), and returns an AnalysisResult.

v3 additions:
  - Affinity tone injection per-user (FRIEND/HOSTILE/DISLIKE)
  - Conversation memory context injection (oracle_memory hybrid retrieval)
  - CPU game filtering on _recent_games_block, _h2h_block, _career_block
  - Division standings + intra-division H2H helpers
  - Elo trajectory block for dynasty/owner profiles
  - run_betting_profile() — 9th analysis type (bets_table from flow_economy.db)
  - comparison_data on AnalysisResult for visual matchup table

Workstream WS-2 — no Discord imports. Pure data + AI logic.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from math import log as _math_log

import atlas_ai
from atlas_ai import Tier
from data_manager import week_label as _week_label

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

# ── Affinity tone injection ────────────────────────────────────────────────────
_get_affinity = None
_get_affinity_instruction = lambda s: ""
try:
    from affinity import get_affinity as _get_affinity, get_affinity_instruction as _get_affinity_instruction
except ImportError:
    pass

# ── flow_economy.db path (betting profile queries) ────────────────────────────
_ECONOMY_DB = os.getenv("FLOW_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_economy.db"))


# ══════════════════════════════════════════════════════════════════════════════
#  RESULT DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AnalysisResult:
    title: str
    analysis_type: str          # "matchup" | "rivalry" | "gameplan" | "team" |
                                # "owner" | "player" | "power" | "dynasty" | "betting"
    sections: list[dict]        # [{label, content, data_rows?}]
    prediction: str | None      # e.g. "Chiefs 31 – Raiders 17" (matchup only)
    confidence: str | None      # "High" | "Medium" | "Low"
    metadata: dict = field(default_factory=dict)  # {tier, model, season, week}
    comparison_data: dict | None = None  # matchup visual table: {team_a: {...}, team_b: {...}}


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
        f"CURRENT SEASON: Season {dm.CURRENT_SEASON}, {_week_label(dm.CURRENT_WEEK)}.\n"
    )


async def _build_affinity_and_memory(discord_id: int | None, memory, label: str) -> tuple[str, str]:
    """Return (affinity_instruction, conv_block) for a given user and analysis label.

    Both strings may be empty for neutral/anonymous users.
    """
    affinity_block = ""
    if discord_id and _get_affinity:
        try:
            score = await _get_affinity(discord_id)
            affinity_block = _get_affinity_instruction(score)
        except Exception:
            pass

    conv_block = ""
    if discord_id and memory:
        try:
            conv_block = await memory.build_context_block(discord_id, label)
        except Exception:
            pass

    return affinity_block, conv_block


def _persona_with_mods(persona: str, affinity_block: str) -> str:
    """Append affinity instruction to persona string (empty string = no-op)."""
    return f"{persona}\n\n{affinity_block}" if affinity_block else persona


def _team_metrics(team_name: str, dm) -> dict:
    """Extract quick comparison metrics for a team (matchup visual table)."""
    result = {
        "name": team_name, "record": "?-?", "ppg": 0.0, "pa": 0.0,
        "rank": "?", "ovr": "?", "diff": 0,
        "off_rank": 0, "def_rank": 0, "to_diff": 0, "win_pct": 0.0,
    }
    if not dm.df_standings.empty:
        def _apply_standings(row):
            w = int(row.get("totalWins", 0) or 0)
            l = int(row.get("totalLosses", 0) or 0)
            pf = int(row.get("ptsFor", 0) or 0)       # API field is ptsFor, not totalPtsFor
            pa_val = int(row.get("ptsAgainst", 0) or 0)
            g = w + l
            result["record"] = f"{w}-{l}"
            result["ppg"] = round(pf / g, 1) if g else 0.0
            result["pa"] = round(pa_val / g, 1) if g else 0.0
            result["diff"] = int(row.get("netPts", 0) or 0)
            result["off_rank"] = int(row.get("offTotalYdsRank", 0) or 0)
            result["def_rank"] = int(row.get("defTotalYdsRank", 0) or 0)
            result["to_diff"] = int(row.get("tODiff", 0) or 0)
            result["win_pct"] = float(row.get("winPct", 0.0) or 0.0)

        matched = False
        for _, row in dm.df_standings.iterrows():
            if str(row.get("teamName", "")).lower() == team_name.lower():
                _apply_standings(row)
                matched = True
                break
        if not matched:
            for _, row in dm.df_standings.iterrows():
                if team_name.lower() in str(row.get("teamName", "")).lower():
                    _apply_standings(row)
                    break

    if not dm.df_power.empty:
        matched = False
        for _, row in dm.df_power.iterrows():
            if str(row.get("teamName", "")).lower() == team_name.lower():
                result["rank"] = f"#{row.get('rank', '?')}"
                result["ovr"] = str(row.get("ovrRating", "?"))
                matched = True
                break
        if not matched:
            for _, row in dm.df_power.iterrows():
                if team_name.lower() in str(row.get("teamName", "")).lower():
                    result["rank"] = f"#{row.get('rank', '?')}"
                    result["ovr"] = str(row.get("ovrRating", "?"))
                    break
    return result


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
        pf = row.get("ptsFor", "?")
        pa = row.get("ptsAgainst", "?")
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
        "AND homeUser != 'CPU' AND awayUser != 'CPU' "
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
        wk = _week_label(int(r.get("weekIndex", 0)) + 1, short=True)
        sn = r.get("seasonIndex", "?")
        lines.append(f"  S{sn}·{wk}: {home} {hs} – {as_} {away}")
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
        "AND homeUser != 'CPU' AND awayUser != 'CPU' "
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
        wk = _week_label(int(r.get("weekIndex", 0)) + 1, short=True)
        winner = r.get("winner_user", "?")
        lines.append(f"  S{sn}·{wk}: {home} {hs}–{as_} {away}  → {winner} wins")
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
        "AND homeUser != 'CPU' AND awayUser != 'CPU' "
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


# ── Dev trait name mapping ────────────────────────────────────────────────────
_DEV_NAMES = {"0": "Normal", "1": "Star", "2": "Superstar", "3": "X-Factor"}


def _abilities_block(team_name: str) -> str:
    """Pull all player abilities for a team, grouped by player with OVR."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT p.firstName || ' ' || p.lastName AS name, p.pos, "
        "CAST(p.playerBestOvr AS INTEGER) AS ovr, p.dev, "
        "a.abilityTitle "
        "FROM player_abilities a "
        "JOIN players p ON a.rosterId = p.rosterId "
        "WHERE p.teamName = ? AND p.isFA != '1' "
        "ORDER BY CAST(p.playerBestOvr AS INTEGER) DESC, a.abilityTitle",
        (team_name,),
    )
    if err or not rows:
        return ""
    # Group abilities by player
    from collections import defaultdict
    grouped: dict[str, dict] = {}
    for r in rows:
        key = r["name"]
        if key not in grouped:
            grouped[key] = {
                "pos": r.get("pos", "?"),
                "ovr": r.get("ovr", "?"),
                "dev": _DEV_NAMES.get(str(r.get("dev", "0")), "Normal"),
                "abilities": [],
            }
        grouped[key]["abilities"].append(r.get("abilityTitle", ""))
    lines = []
    for name, info in grouped.items():
        ab_str = ", ".join(info["abilities"])
        lines.append(f"  {name} ({info['pos']}, {info['ovr']} OVR, {info['dev']}) — {ab_str}")
    return f"ABILITIES ({team_name}):\n" + "\n".join(lines)


def _full_roster_block(team_name: str, limit: int = 53) -> str:
    """Pull full roster with dev traits, age, draft info."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT firstName || ' ' || lastName AS name, pos, "
        "CAST(playerBestOvr AS INTEGER) AS ovr, dev, "
        "age, draftRound, draftPick "
        "FROM players WHERE teamName = ? AND isFA != '1' "
        "ORDER BY CAST(playerBestOvr AS INTEGER) DESC LIMIT ?",
        (team_name, limit),
    )
    if err or not rows:
        return ""
    lines = []
    for r in rows:
        dev = _DEV_NAMES.get(str(r.get("dev", "0")), "Normal")
        age = r.get("age", "?")
        ovr = r.get("ovr", "?")
        draft = ""
        rd = r.get("draftRound")
        pk = r.get("draftPick")
        if rd and rd not in ("", "0"):
            draft = f" Rd{rd}"
            if pk:
                draft += f".{pk}"
        lines.append(f"  {r['name']} ({r.get('pos','?')}) OVR {ovr} [{dev}] Age {age}{draft}")
    return f"FULL ROSTER ({team_name}, {len(lines)} players):\n" + "\n".join(lines)


def _scoring_trends_block(team_name: str, n: int = 5) -> str:
    """Calculate PPG scored/allowed and margins from recent games."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT homeTeamName, awayTeamName, "
        "CAST(homeScore AS INTEGER) AS hs, CAST(awayScore AS INTEGER) AS as_ "
        "FROM games "
        "WHERE status IN ('2','3') AND stageIndex='1' "
        "AND (homeTeamName=? OR awayTeamName=?) "
        "ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC "
        "LIMIT ?",
        (team_name, team_name, n),
    )
    if err or not rows:
        return ""
    scored, allowed, wins = [], [], 0
    for r in rows:
        if r["homeTeamName"] == team_name:
            scored.append(r["hs"])
            allowed.append(r["as_"])
            if r["hs"] > r["as_"]:
                wins += 1
        else:
            scored.append(r["as_"])
            allowed.append(r["hs"])
            if r["as_"] > r["hs"]:
                wins += 1
    ppg = sum(scored) / len(scored) if scored else 0
    ppg_a = sum(allowed) / len(allowed) if allowed else 0
    margin = ppg - ppg_a
    return (
        f"SCORING TRENDS ({team_name}, last {len(rows)} games): "
        f"{wins}W-{len(rows)-wins}L | "
        f"PPG: {ppg:.1f} | Allowed: {ppg_a:.1f} | Margin: {margin:+.1f}"
    )


def _offensive_leaders_block(team_name: str, dm=None) -> str:
    """Top offensive performers for a team this season from offensive_stats."""
    if run_sql is None:
        return ""
    season = dm.CURRENT_SEASON if dm else 6
    rows, err = run_sql(
        "SELECT extendedName, pos, "
        "SUM(CAST(passYds AS INTEGER)) AS passYds, "
        "SUM(CAST(passTDs AS INTEGER)) AS passTDs, "
        "SUM(CAST(passInts AS INTEGER)) AS passInts, "
        "SUM(CAST(rushYds AS INTEGER)) AS rushYds, "
        "SUM(CAST(rushTDs AS INTEGER)) AS rushTDs, "
        "SUM(CAST(recYds AS INTEGER)) AS recYds, "
        "SUM(CAST(recTDs AS INTEGER)) AS recTDs, "
        "SUM(CAST(receptions AS INTEGER)) AS rec, "
        "COUNT(*) AS games "
        "FROM offensive_stats "
        "WHERE teamName = ? AND CAST(seasonIndex AS INTEGER) = ? "
        "GROUP BY rosterId "
        "HAVING (passYds > 0 OR rushYds > 0 OR recYds > 0) "
        "ORDER BY (passYds + rushYds + recYds) DESC LIMIT 8",
        (team_name, season),
    )
    if err or not rows:
        return ""
    lines = []
    for r in rows:
        name = r["extendedName"]
        pos = r.get("pos", "?")
        g = r.get("games", 0)
        parts = []
        if r["passYds"] > 0:
            parts.append(f"{r['passYds']} pass yds, {r['passTDs']} TD, {r['passInts']} INT")
        if r["rushYds"] > 0:
            parts.append(f"{r['rushYds']} rush yds, {r['rushTDs']} TD")
        if r["recYds"] > 0:
            parts.append(f"{r['rec']} rec, {r['recYds']} rec yds, {r['recTDs']} TD")
        stat_str = " | ".join(parts)
        lines.append(f"  {name} ({pos}, {g}g): {stat_str}")
    return f"OFFENSIVE LEADERS ({team_name}, Season {season}):\n" + "\n".join(lines)


def _defensive_leaders_block(team_name: str, dm=None) -> str:
    """Top defensive performers for a team this season from defensive_stats."""
    if run_sql is None:
        return ""
    season = dm.CURRENT_SEASON if dm else 6
    rows, err = run_sql(
        "SELECT extendedName, pos, "
        "SUM(CAST(defTotalTackles AS INTEGER)) AS tackles, "
        "SUM(CAST(defSacks AS INTEGER)) AS sacks, "
        "SUM(CAST(defInts AS INTEGER)) AS ints, "
        "SUM(CAST(defForcedFum AS INTEGER)) AS ff, "
        "COUNT(*) AS games "
        "FROM defensive_stats "
        "WHERE teamName = ? AND CAST(seasonIndex AS INTEGER) = ? "
        "GROUP BY rosterId "
        "HAVING (tackles > 0 OR sacks > 0 OR ints > 0) "
        "ORDER BY tackles DESC LIMIT 8",
        (team_name, season),
    )
    if err or not rows:
        return ""
    lines = []
    for r in rows:
        name = r["extendedName"]
        pos = r.get("pos", "?")
        g = r.get("games", 0)
        lines.append(
            f"  {name} ({pos}, {g}g): {r['tackles']} tkl, "
            f"{r['sacks']} sck, {r['ints']} INT, {r['ff']} FF"
        )
    return f"DEFENSIVE LEADERS ({team_name}, Season {season}):\n" + "\n".join(lines)


def _draft_history_block(team_name: str) -> str:
    """Show players drafted by this team from player_draft_map."""
    if run_sql is None:
        return ""
    rows, err = run_sql(
        "SELECT extendedName, drafting_season, draftRound, draftPick, "
        "current_team, dev, was_traded "
        "FROM player_draft_map "
        "WHERE drafting_team = ? "
        "ORDER BY CAST(drafting_season AS INTEGER) DESC, "
        "CAST(draftRound AS INTEGER) ASC LIMIT 15",
        (team_name,),
    )
    if err or not rows:
        return ""
    lines = []
    still_on_team = 0
    traded_away = 0
    for r in rows:
        dev = _DEV_NAMES.get(str(r.get("dev", "0")), "Normal")
        status = "TRADED" if str(r.get("was_traded", "0")) == "1" else "ROSTERED"
        if status == "TRADED":
            traded_away += 1
        else:
            still_on_team += 1
        rd = r.get("draftRound", "?")
        pk = r.get("draftPick", "?")
        lines.append(
            f"  S{r['drafting_season']} Rd{rd}.{pk}: {r['extendedName']} [{dev}] — {status}"
        )
    summary = f"  Summary: {still_on_team} still rostered, {traded_away} traded away"
    return f"DRAFT HISTORY ({team_name}):\n{summary}\n" + "\n".join(lines)


def _trade_context_block(team_name: str, dm=None) -> str:
    """Pull recent trades involving a team from dm.df_trades."""
    if dm is None or dm.df_trades.empty:
        return ""
    relevant = dm.df_trades[
        dm.df_trades.apply(
            lambda r: team_name.lower() in str(r.get("team1Name", "")).lower() or
                      team_name.lower() in str(r.get("team2Name", "")).lower(),
            axis=1,
        )
    ].head(6)
    if relevant.empty:
        return ""
    lines = []
    for _, row in relevant.iterrows():
        t1, t2 = row.get("team1Name", "?"), row.get("team2Name", "?")
        s1, s2 = row.get("team1Sent", "?"), row.get("team2Sent", "?")
        lines.append(f"  {t1} sent {s1} ↔ {t2} sent {s2}")
    return f"TRADES ({team_name}):\n" + "\n".join(lines)


def _power_rank_block(team_name: str, dm=None) -> str:
    """Pull power ranking for a single team from dm.df_power."""
    if dm is None or dm.df_power.empty:
        return ""
    for _, row in dm.df_power.iterrows():
        if team_name.lower() in str(row.get("teamName", "")).lower():
            rank = row.get("rank", "?")
            score = row.get("score", "?")
            seed = row.get("seed", "?")
            ovr = row.get("ovrRating", "?")
            return f"POWER RANKING ({team_name}): #{rank} (score {score}, seed {seed}, team OVR {ovr})"
    return ""


def _team_name_from_id(team_id: int, dm) -> str | None:
    """Resolve team ID (from button selection) to canonical nickName."""
    if dm.df_teams.empty:
        return None
    mask = dm.df_teams["teamId"].astype(str) == str(team_id)
    if not mask.any():
        return None
    return dm.df_teams[mask].iloc[0].get("nickName")


def _division_block(team_name: str, dm) -> str:
    """Division standings for the team's division — sorted by wins descending."""
    if dm.df_standings.empty or dm.df_teams.empty:
        return ""
    # Find team's divName
    div_name = ""
    for _, row in dm.df_teams.iterrows():
        if str(row.get("nickName", "")).lower() == team_name.lower():
            div_name = str(row.get("divName", ""))
            break
    if not div_name:
        return ""
    # Filter standings to same division — join via df_teams since standings may lack divName
    div_team_names = [
        str(row.get("nickName", ""))
        for _, row in dm.df_teams.iterrows()
        if str(row.get("divName", "")).lower() == div_name.lower()
    ]
    div_df = dm.df_standings[
        dm.df_standings["teamName"].isin(div_team_names)
    ].copy()
    if div_df.empty:
        return ""
    div_df = div_df.sort_values("totalWins", ascending=False)
    lines = [f"DIVISION STANDINGS ({div_name}):"]
    for _, row in div_df.iterrows():
        tn = row.get("teamName", "?")
        w = int(row.get("totalWins", 0) or 0)
        l = int(row.get("totalLosses", 0) or 0)
        marker = " ←" if tn.lower() == team_name.lower() else ""
        lines.append(f"  {tn}: {w}-{l}{marker}")
    return "\n".join(lines)


def _division_h2h_block(db_username: str, dm) -> str:
    """Intra-division vs out-of-division career record for an owner."""
    if run_sql is None or dm.df_teams.empty:
        return ""
    # Find owner's divName
    div_name = ""
    for _, row in dm.df_teams.iterrows():
        if str(row.get("userName", "")).lower() == db_username.lower():
            div_name = str(row.get("divName", ""))
            break
    if not div_name:
        return ""
    # Division teammates
    div_owners = [
        str(row.get("userName", ""))
        for _, row in dm.df_teams.iterrows()
        if str(row.get("divName", "")).lower() == div_name.lower()
        and str(row.get("userName", "")).lower() != db_username.lower()
    ]
    if not div_owners:
        return ""
    placeholders = ",".join("?" * len(div_owners))
    div_rows, div_err = run_sql(
        f"SELECT winner_user, loser_user FROM games "
        f"WHERE status IN ('2','3') AND stageIndex='1' "
        f"AND homeUser != 'CPU' AND awayUser != 'CPU' "
        f"AND ((winner_user=? AND loser_user IN ({placeholders})) "
        f"OR (loser_user=? AND winner_user IN ({placeholders})))",
        (db_username, *div_owners, db_username, *div_owners),
    )
    if div_err or not div_rows:
        return ""
    div_wins = sum(1 for r in div_rows if r["winner_user"] == db_username)
    div_losses = len(div_rows) - div_wins
    # Total career games
    all_rows, all_err = run_sql(
        "SELECT winner_user FROM games "
        "WHERE status IN ('2','3') AND stageIndex='1' "
        "AND homeUser != 'CPU' AND awayUser != 'CPU' "
        "AND (winner_user=? OR loser_user=?)",
        (db_username, db_username),
    )
    if all_err or not all_rows:
        return f"INTRA-DIVISION RECORD ({div_name}): {div_wins}-{div_losses}"
    total_games = len(all_rows)
    total_wins = sum(1 for r in all_rows if r["winner_user"] == db_username)
    out_games = total_games - len(div_rows)
    out_wins = total_wins - div_wins
    out_losses = out_games - out_wins
    return (
        f"INTRA-DIVISION RECORD ({div_name}): {div_wins}-{div_losses} "
        f"({len(div_rows)} games) | "
        f"OUT-OF-DIVISION: {out_wins}-{out_losses} ({out_games} games)"
    )


def _elo_trajectory_block(db_username: str) -> str:
    """Season-by-season Elo trajectory for an owner using a full league computation."""
    if run_sql is None:
        return ""
    # Pull all completed regular-season games chronologically for full Elo computation
    rows, err = run_sql(
        "SELECT homeUser, awayUser, "
        "CAST(homeScore AS INTEGER) AS hs, CAST(awayScore AS INTEGER) AS as_, "
        "CAST(seasonIndex AS INTEGER) AS sn, CAST(weekIndex AS INTEGER) AS wk "
        "FROM games "
        "WHERE status IN ('2','3') AND stageIndex='1' "
        "AND homeUser != 'CPU' AND awayUser != 'CPU' "
        "AND homeUser != '' AND awayUser != '' "
        "ORDER BY sn ASC, wk ASC"
    )
    if err or not rows:
        return ""

    elo_map: dict[str, float] = {}
    ELO_INIT, K, REGRESS = 1500.0, 24, 0.75
    prev_season = None
    season_snapshots: list[tuple[int, int]] = []

    for r in rows:
        sn = r["sn"]
        hu, au = r["homeUser"], r["awayUser"]
        hs, as_ = r["hs"] or 0, r["as_"] or 0

        if prev_season is not None and sn != prev_season:
            # Season boundary — record snapshot then regress
            if db_username in elo_map:
                season_snapshots.append((prev_season, round(elo_map[db_username])))
            for u in list(elo_map):
                elo_map[u] = ELO_INIT + (elo_map[u] - ELO_INIT) * REGRESS

        elo_h = elo_map.get(hu, ELO_INIT)
        elo_a = elo_map.get(au, ELO_INIT)
        exp_h = 1.0 / (1.0 + 10 ** ((elo_a - elo_h) / 400))
        exp_a = 1.0 - exp_h
        actual_h = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        actual_a = 1.0 - actual_h
        margin = abs(hs - as_)
        mov = (_math_log(margin + 1) * 0.8) if margin > 0 else 1.0
        elo_map[hu] = max(1200.0, min(2200.0, elo_h + K * mov * (actual_h - exp_h)))
        elo_map[au] = max(1200.0, min(2200.0, elo_a + K * mov * (actual_a - exp_a)))
        prev_season = sn

    # Record final season
    if prev_season is not None and db_username in elo_map:
        season_snapshots.append((prev_season, round(elo_map[db_username])))

    if not season_snapshots:
        return ""

    lines = [f"ELO TRAJECTORY ({db_username}):"]
    for sn, elo in season_snapshots:
        trend = "▲" if elo > ELO_INIT else ("▼" if elo < ELO_INIT else "▶")
        bar_len = max(1, min(20, (elo - 1400) // 20))
        lines.append(f"  S{sn}: {elo} {trend} {'█' * bar_len}")
    return "\n".join(lines)


async def _betting_block(owner_name: str, dm) -> str:
    """Pull betting behavior profile from flow_economy.db bets_table."""
    try:
        import aiosqlite
    except ImportError:
        return ""

    # Resolve owner → discord_id via tsl_members
    discord_id_val = None
    if run_sql is not None:
        db_username = _resolve_owner(owner_name, dm) or owner_name
        id_rows, id_err = run_sql(
            "SELECT discord_id FROM tsl_members WHERE db_username = ? LIMIT 1",
            (db_username,),
        )
        if not id_err and id_rows and id_rows[0].get("discord_id"):
            discord_id_val = int(id_rows[0]["discord_id"])

    if not discord_id_val:
        return f"BETTING PROFILE ({owner_name}): No Discord ID mapping found in member registry."

    try:
        async with aiosqlite.connect(_ECONOMY_DB) as db:
            db.row_factory = aiosqlite.Row

            # Per-type stats
            async with db.execute(
                "SELECT bet_type, COUNT(*) AS total, "
                "SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS wins, "
                "SUM(wager_amount) AS wagered "
                "FROM bets_table WHERE discord_id=? GROUP BY bet_type",
                (discord_id_val,),
            ) as cur:
                type_rows = [dict(r) for r in await cur.fetchall()]

            # Overall totals
            async with db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS wins, "
                "SUM(wager_amount) AS wagered "
                "FROM bets_table WHERE discord_id=?",
                (discord_id_val,),
            ) as cur:
                totals = dict(await cur.fetchone() or {})

            # Parlay record
            async with db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS wins "
                "FROM parlays_table WHERE discord_id=?",
                (discord_id_val,),
            ) as cur:
                parlay_row = dict(await cur.fetchone() or {})

            # Last 10 bets for streak
            async with db.execute(
                "SELECT status FROM bets_table WHERE discord_id=? "
                "ORDER BY created_at DESC LIMIT 10",
                (discord_id_val,),
            ) as cur:
                recent = [dict(r) for r in await cur.fetchall()]

            # Biggest single win
            async with db.execute(
                "SELECT wager_amount, bet_type, matchup FROM bets_table "
                "WHERE discord_id=? AND status='won' "
                "ORDER BY wager_amount DESC LIMIT 1",
                (discord_id_val,),
            ) as cur:
                big_win = dict(await cur.fetchone() or {})

    except Exception as e:
        _log.warning(f"[betting_block] DB query failed: {e}")
        return ""

    if not totals or not totals.get("total"):
        return f"BETTING PROFILE ({owner_name}): No bets found on record."

    total = int(totals.get("total") or 0)
    wins = int(totals.get("wins") or 0)
    wagered = int(totals.get("wagered") or 0)
    win_pct = round(wins / total * 100, 1) if total else 0.0

    # Streak calculation
    streak = 0
    streak_type = ""
    for b in recent:
        s = b.get("status", "")
        if not streak_type:
            streak_type = s
        if s == streak_type:
            streak += 1
        else:
            break
    streak_str = f"{streak_type.upper()} {streak}" if streak_type and streak > 1 else "–"

    lines = [f"BETTING PROFILE ({owner_name}):"]
    lines.append(f"  Overall: {wins}-{total - wins} ({win_pct}%) | Wagered: {wagered:,} coins")
    lines.append(f"  Current streak: {streak_str}")
    if big_win.get("wager_amount"):
        lines.append(f"  Biggest win: {int(big_win['wager_amount']):,} coins ({big_win.get('bet_type','')} — {big_win.get('matchup','')})")

    if type_rows:
        lines.append("  BY TYPE:")
        for r in sorted(type_rows, key=lambda x: x.get("total", 0), reverse=True):
            bt = r.get("bet_type", "?")
            t = int(r.get("total") or 0)
            w = int(r.get("wins") or 0)
            pct = round(w / t * 100, 1) if t else 0.0
            lines.append(f"    {bt}: {w}-{t-w} ({pct}%) on {t} bets")

    pt = int(parlay_row.get("total") or 0)
    pw = int(parlay_row.get("wins") or 0)
    if pt > 0:
        lines.append(f"  Parlays: {pw}-{pt-pw} ({round(pw/pt*100,1)}%) | {pt} total")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  ANALYSIS PIPELINES
# ══════════════════════════════════════════════════════════════════════════════

_TSL_ONLY_RULE = (
    "\n\nCRITICAL RULE: You are ATLAS, the TSL intelligence system. You ONLY analyze TSL data. "
    "All analysis must be grounded in the data provided above. Never fabricate stats or invent players. "
    "If data is sparse, say so concisely and work with what you have."
)


def _validate_team_data(team_name: str, roster: str, standings: str, analysis_type: str,
                        dm=None) -> AnalysisResult | None:
    """Return an error AnalysisResult if critical data is missing, else None."""
    if not roster.strip() and not standings.strip():
        _log.warning(f"[oracle_analysis] {analysis_type}: no roster or standings for '{team_name}'")
        return AnalysisResult(
            title=f"{analysis_type}: {team_name}",
            analysis_type=analysis_type.lower().replace(" ", ""),
            sections=[{
                "label": "Data Error",
                "content": (
                    f"No roster or standings data found for **{team_name}**. "
                    f"This team may not have synced to the database yet. "
                    f"Try running `/commish sync` to refresh league data."
                ),
            }],
            prediction=None, confidence=None,
            metadata={
                "tier": "NONE", "model": "none",
                "season": dm.CURRENT_SEASON if dm else 0,
            },
        )
    if not roster.strip():
        _log.warning(f"[oracle_analysis] {analysis_type}: missing roster for '{team_name}'")
    if not standings.strip():
        _log.warning(f"[oracle_analysis] {analysis_type}: missing standings for '{team_name}'")
    return None


async def run_matchup_analysis(team_a: str, team_b: str, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Deep head-to-head matchup analysis with score prediction."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"matchup {team_a} vs {team_b}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
    ctx = _build_tsl_context(dm)

    # Gather data — deep
    standings_a = _standings_block(dm, team_a)
    standings_b = _standings_block(dm, team_b)
    roster_a = _full_roster_block(team_a)
    roster_b = _full_roster_block(team_b)
    abilities_a = _abilities_block(team_a)
    abilities_b = _abilities_block(team_b)
    games_a = _recent_games_block(team_a, 5)
    games_b = _recent_games_block(team_b, 5)
    trends_a = _scoring_trends_block(team_a)
    trends_b = _scoring_trends_block(team_b)
    off_a = _offensive_leaders_block(team_a, dm)
    off_b = _offensive_leaders_block(team_b, dm)
    def_a = _defensive_leaders_block(team_a, dm)
    def_b = _defensive_leaders_block(team_b, dm)
    power_a = _power_rank_block(team_a, dm)
    power_b = _power_rank_block(team_b, dm)

    # Resolve usernames for H2H
    owner_a = _resolve_owner(team_a, dm)
    owner_b = _resolve_owner(team_b, dm)
    h2h = ""
    if owner_a and owner_b:
        h2h = _h2h_block(owner_a, owner_b)

    # Validate critical data
    for tname, roster, standings in [(team_a, roster_a, standings_a), (team_b, roster_b, standings_b)]:
        err = _validate_team_data(tname, roster, standings, "Matchup Analysis", dm)
        if err:
            return err

    # Structured comparison data for visual table
    comparison_data = {
        "team_a": _team_metrics(team_a, dm),
        "team_b": _team_metrics(team_b, dm),
    }

    data_block = "\n\n".join(filter(None, [
        ctx, standings_a, standings_b, roster_a, roster_b,
        abilities_a, abilities_b, games_a, games_b,
        trends_a, trends_b, off_a, off_b, def_a, def_b,
        power_a, power_b, h2h, conv_block,
    ]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Matchup Analysis — {team_a} vs {team_b}

Write a deep pre-game breakdown using ALL the data above. You MUST reference specific players by name, OVR, and abilities.

1. OFFENSIVE EDGE: Compare passing and rushing attacks. Name specific players with OVR ratings and their season stats. Cite X-Factor/Superstar abilities that create mismatches.
2. DEFENSIVE EDGE: Which defense has the advantage and why. Name key defensive players with their tackle/sack/INT totals. Identify which defensive abilities could disrupt the opposing offense.
3. KEY MATCHUP: The single most important individual matchup — cite both players' OVR and abilities. Explain why this matchup decides the game.
4. PREDICTION: Give a concrete score prediction (e.g. "Chiefs 28 – Raiders 17") grounded in the scoring trends data. Reference each team's PPG and point differential. Do NOT hedge.
5. CONFIDENCE: Rate your prediction High / Medium / Low based on data quality.

Format using Discord markdown (**bold** for player names and scores).
Be direct. Be specific. Cite real numbers from the data above. ATLAS doesn't hedge.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=1200)
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
        comparison_data=comparison_data,
    )


async def run_rivalry_history(owner_a: str, owner_b: str, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Full historical rivalry breakdown between two TSL owners."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"rivalry {owner_a} vs {owner_b}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
    ctx = _build_tsl_context(dm)

    db_a = _resolve_owner(owner_a, dm) or owner_a
    db_b = _resolve_owner(owner_b, dm) or owner_b
    h2h = _h2h_block(db_a, db_b)
    career_a = _career_block(db_a)
    career_b = _career_block(db_b)

    # Find each owner's current team for roster comparison
    team_a_name, team_b_name = None, None
    if not dm.df_teams.empty:
        for uname, tvar in [(db_a, "team_a_name"), (db_b, "team_b_name")]:
            mask = dm.df_teams["userName"].str.lower() == uname.lower()
            if mask.any():
                if tvar == "team_a_name":
                    team_a_name = dm.df_teams[mask].iloc[0].get("nickName", "")
                else:
                    team_b_name = dm.df_teams[mask].iloc[0].get("nickName", "")

    # Deep data for both sides
    roster_a = _full_roster_block(team_a_name) if team_a_name else ""
    roster_b = _full_roster_block(team_b_name) if team_b_name else ""
    abilities_a = _abilities_block(team_a_name) if team_a_name else ""
    abilities_b = _abilities_block(team_b_name) if team_b_name else ""
    trends_a = _scoring_trends_block(team_a_name) if team_a_name else ""
    trends_b = _scoring_trends_block(team_b_name) if team_b_name else ""

    # Trades between them
    trades_context = ""
    if not dm.df_trades.empty and "team1Name" in dm.df_trades.columns:
        relevant = dm.df_trades[
            dm.df_trades.apply(
                lambda r: (
                    (db_a.lower() in str(r.get("team1Name", "")).lower() and
                     db_b.lower() in str(r.get("team2Name", "")).lower()) or
                    (db_b.lower() in str(r.get("team1Name", "")).lower() and
                     db_a.lower() in str(r.get("team2Name", "")).lower())
                ),
                axis=1,
            )
        ].head(5)
        if not relevant.empty:
            trade_lines = []
            for _, row in relevant.iterrows():
                t1, t2 = row.get("team1Name", "?"), row.get("team2Name", "?")
                s1, s2 = row.get("team1Sent", "?"), row.get("team2Sent", "?")
                trade_lines.append(f"  {t1} sent {s1} ↔ {t2} sent {s2}")
            trades_context = "TRADES BETWEEN THEM:\n" + "\n".join(trade_lines)

    data_block = "\n\n".join(filter(None, [
        ctx, h2h, career_a, career_b, roster_a, roster_b,
        abilities_a, abilities_b, trends_a, trends_b, trades_context, conv_block,
    ]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Rivalry History — {db_a} vs {db_b}

Write a compelling rivalry narrative using ALL the data above. Reference specific game scores from the H2H record.

1. THE RECORD: Who holds the historical edge and by how much. Cite the exact W-L and total games.
2. ERA ANALYSIS: Have the fortunes shifted? Who dominated which era? Reference specific seasons from the career data.
3. DEFINING MOMENTS: The 1-2 most significant games — cite the actual scores from H2H data. What made them significant?
4. ROSTER COMPARISON: Compare their current rosters. Who has more talent right now? Cite X-Factor/Superstar players and OVR ratings.
5. CURRENT TRAJECTORY: Based on recent scoring trends and records, where is this rivalry headed? Who has momentum?

Format with **bold** for names, scores, and key stats. Make it feel like a genuine sports rivalry piece.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=1000)

    return AnalysisResult(
        title=f"Rivalry History: {db_a} vs {db_b}",
        analysis_type="rivalry",
        sections=[{"label": "Rivalry Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "OPUS", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_game_plan(target_team: str, requesting_user_team: str | None, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Tactical blueprint for beating a specific opponent."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"game plan vs {target_team}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
    ctx = _build_tsl_context(dm)

    # Deep data on the target
    roster_target = _full_roster_block(target_team)
    abilities_target = _abilities_block(target_team)
    games_target = _recent_games_block(target_team, 5)
    standings_target = _standings_block(dm, target_team)
    trends_target = _scoring_trends_block(target_team)
    off_target = _offensive_leaders_block(target_team, dm)
    def_target = _defensive_leaders_block(target_team, dm)

    # Requester's roster for personalized advice
    req_data = ""
    if requesting_user_team:
        req_roster = _full_roster_block(requesting_user_team)
        req_abilities = _abilities_block(requesting_user_team)
        req_off = _offensive_leaders_block(requesting_user_team, dm)
        req_data = "\n\n".join(filter(None, [req_roster, req_abilities, req_off]))

    # Validate target team data
    err = _validate_team_data(target_team, roster_target, standings_target, "Game Plan", dm)
    if err:
        return err

    data_block = "\n\n".join(filter(None, [
        ctx, standings_target, roster_target, abilities_target,
        games_target, trends_target, off_target, def_target, req_data, conv_block,
    ]))

    user_context = (
        f"The user asking owns {requesting_user_team}. Their roster and abilities are included above. "
        f"Personalize every game plan point to their specific players vs the opponent's players."
        if requesting_user_team else ""
    )

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Game Plan — How to beat {target_team}

{user_context}

Write a concrete tactical blueprint with exactly 3 game plan points. Use the SPECIFIC player data above — not generic football advice.

1. EXPLOIT THIS WEAKNESS: Name the specific defensive player or position group to attack. Cite their OVR and any ability gaps. If the requester has a matching offensive weapon, name the 1v1 matchup.
2. NEUTRALIZE THIS THREAT: Name the opponent's best offensive player with their stats, OVR, and abilities. What specific defensive scheme or player assignment shuts them down?
3. WIN THE TRENCHES: Analyze the OL vs DL matchup. Who has the edge up front? How does this dictate play-calling?

Then give a PREDICTION ASSESSMENT: Based on scoring trends (PPG, margin), how likely is a win? What's the score range?

Use **bold** for player names, OVR ratings, and abilities. Every point must name real players from the data.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=1000)

    return AnalysisResult(
        title=f"Game Plan: How to Beat {target_team}",
        analysis_type="gameplan",
        sections=[{"label": "Tactical Blueprint", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "OPUS", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_team_report(team_name: str, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Full team deep dive — identity, trajectory, strengths and weaknesses."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"team report {team_name}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
    ctx = _build_tsl_context(dm)

    standings = _standings_block(dm, team_name)
    roster = _full_roster_block(team_name)
    abilities = _abilities_block(team_name)
    games = _recent_games_block(team_name, 6)
    trends = _scoring_trends_block(team_name)
    off_leaders = _offensive_leaders_block(team_name, dm)
    def_leaders = _defensive_leaders_block(team_name, dm)
    draft = _draft_history_block(team_name)
    trade_context = _trade_context_block(team_name, dm)
    power_rank = _power_rank_block(team_name, dm)

    # Validate critical data
    err = _validate_team_data(team_name, roster, standings, "Team Report", dm)
    if err:
        return err

    division_block = _division_block(team_name, dm)
    div_h2h = ""
    owner_for_h2h = _resolve_owner(team_name, dm)
    if owner_for_h2h:
        div_h2h = _division_h2h_block(owner_for_h2h, dm)

    data_block = "\n\n".join(filter(None, [
        ctx, standings, division_block, div_h2h, power_rank, roster, abilities,
        games, trends, off_leaders, def_leaders, draft, trade_context, conv_block,
    ]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Team Report — {team_name}

Write a complete team analysis using ALL the data above. You MUST cite specific players, OVR ratings, abilities, and stats.

1. IDENTITY: What kind of team is {team_name}? Reference their offensive leaders (pass vs rush yards) to determine if they're pass-heavy or run-first. Reference their defensive identity from the defensive leaders data.
2. STAR POWER: Name the X-Factor and Superstar players. What abilities do they have? How do these abilities shape the team's ceiling?
3. STAT LEADERS: Who drives this offense? Cite the top passer, rusher, and receiver with their actual season stats. Who anchors the defense? Cite tackle/sack/INT leaders.
4. STRENGTHS & WEAKNESSES: Where is this roster elite (OVR 90+)? Where are there gaps (positions without Star+ dev traits)?
5. TRAJECTORY: Use the scoring trends (PPG, margin) and recent record to assess momentum. Are they trending up or down? Cite the numbers.
6. ROSTER BUILDING: Based on draft history and trades — is this team building through the draft or acquiring via trade?

Use **bold** for player names, ratings, and key stats. Be direct — no hedging.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=1000)

    return AnalysisResult(
        title=f"Team Report: {team_name}",
        analysis_type="team",
        sections=[{"label": "Team Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_owner_profile(owner_name: str, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Owner profile — current season, playstyle, and tendencies."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"owner profile {owner_name}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
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
    roster = _full_roster_block(team_name) if team_name else ""
    abilities = _abilities_block(team_name) if team_name else ""
    games = _recent_games_block(team_name, 5) if team_name else ""
    trends = _scoring_trends_block(team_name) if team_name else ""
    draft = _draft_history_block(team_name) if team_name else ""
    trade_context = _trade_context_block(team_name, dm) if team_name else ""
    power_rank = _power_rank_block(team_name, dm) if team_name else ""

    div_h2h = _division_h2h_block(db_username, dm)
    elo_traj = _elo_trajectory_block(db_username)

    data_block = "\n\n".join(filter(None, [
        ctx, career, elo_traj, standings, power_rank, roster, abilities,
        games, trends, div_h2h, draft, trade_context, conv_block,
    ]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Owner Profile — {db_username}

Write a complete owner profile using ALL the data above. Reference specific numbers and players.

1. CURRENT SEASON: Record, scoring trends (PPG/margin), momentum. Cite the actual numbers from scoring trends.
2. PLAYSTYLE IDENTITY: Based on their roster composition and abilities — are they talent-first? Defense-first? Offense-heavy? Reference their X-Factor/Superstar players.
3. ROSTER BUILDING: Based on draft history and trades — do they build through the draft or acquire via trade? How many drafted players are still rostered vs traded away?
4. STRENGTHS: What do they consistently do well? Reference their career record trends (best/worst seasons).
5. THIS SEASON'S OUTLOOK: Based on power ranking, scoring trends, and roster quality — what's realistic?
6. CAREER CONTEXT: How does this season's record compare to their historical per-season baseline? Trending up or down?

Use **bold** for their name, key stats, and player names. Keep it analytical — not fluff.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=900)

    return AnalysisResult(
        title=f"Owner Profile: {db_username}",
        analysis_type="owner",
        sections=[{"label": "Profile", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK, "team": team_name},
    )


async def run_player_scout(player_name: str, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Full scouting report on a TSL player."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"player scout {player_name}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
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

    # Per-game stat trends for this player
    game_stats = ""
    if run_sql is not None and rows:
        p_team = rows[0].get("teamName", "")
        p_pos = str(rows[0].get("pos", "")).upper()
        # Determine if offensive or defensive player
        def_positions = {"LE", "RE", "DT", "MLB", "LOLB", "ROLB", "CB", "FS", "SS",
                         "LEDGE", "REDGE", "MIKE", "WILL", "SAM"}
        if p_pos in def_positions:
            gs_rows, gs_err = run_sql(
                "SELECT weekIndex, "
                "CAST(defTotalTackles AS INTEGER) AS tkl, "
                "CAST(defSacks AS INTEGER) AS sck, "
                "CAST(defInts AS INTEGER) AS ints "
                "FROM defensive_stats "
                "WHERE extendedName LIKE ? AND CAST(seasonIndex AS INTEGER) = ? "
                "ORDER BY CAST(weekIndex AS INTEGER) DESC LIMIT 5",
                (f"%{player_name}%", dm.CURRENT_SEASON),
            )
            if not gs_err and gs_rows:
                gs_lines = [f"  {_week_label(int(r['weekIndex'])+1, short=True)}: {r['tkl']} tkl, {r['sck']} sck, {r['ints']} INT" for r in gs_rows]
                game_stats = f"PER-GAME STATS (last {len(gs_rows)}):\n" + "\n".join(gs_lines)
        else:
            gs_rows, gs_err = run_sql(
                "SELECT weekIndex, "
                "CAST(passYds AS INTEGER) AS pyd, CAST(passTDs AS INTEGER) AS ptd, "
                "CAST(rushYds AS INTEGER) AS ryd, CAST(rushTDs AS INTEGER) AS rtd, "
                "CAST(recYds AS INTEGER) AS reyd, CAST(recTDs AS INTEGER) AS retd "
                "FROM offensive_stats "
                "WHERE extendedName LIKE ? AND CAST(seasonIndex AS INTEGER) = ? "
                "ORDER BY CAST(weekIndex AS INTEGER) DESC LIMIT 5",
                (f"%{player_name}%", dm.CURRENT_SEASON),
            )
            if not gs_err and gs_rows:
                gs_lines = []
                for r in gs_rows:
                    parts = []
                    if r["pyd"]: parts.append(f"{r['pyd']}py/{r['ptd']}td")
                    if r["ryd"]: parts.append(f"{r['ryd']}ry/{r['rtd']}td")
                    if r["reyd"]: parts.append(f"{r['reyd']}rey/{r['retd']}td")
                    gs_lines.append(f"  {_week_label(int(r['weekIndex'])+1, short=True)}: {' | '.join(parts)}")
                game_stats = f"PER-GAME STATS (last {len(gs_rows)}):\n" + "\n".join(gs_lines)

    # Positional peer comparison
    peer_comparison = ""
    if run_sql is not None and rows:
        p_pos = rows[0].get("pos", "")
        peer_rows, peer_err = run_sql(
            "SELECT firstName || ' ' || lastName AS name, "
            "CAST(playerBestOvr AS INTEGER) AS ovr, dev, teamName "
            "FROM players WHERE pos = ? AND isFA != '1' "
            "ORDER BY CAST(playerBestOvr AS INTEGER) DESC LIMIT 5",
            (p_pos,),
        )
        if not peer_err and peer_rows:
            peer_lines = []
            for i, r in enumerate(peer_rows, 1):
                dev = _DEV_NAMES.get(str(r.get("dev", "0")), "Normal")
                peer_lines.append(f"  #{i} {r['name']} ({r['ovr']} OVR, {dev}) — {r['teamName']}")
            peer_comparison = f"TOP {p_pos}s IN TSL:\n" + "\n".join(peer_lines)

    # Team context
    team_power = ""
    if rows:
        team_power = _power_rank_block(rows[0].get("teamName", ""), dm)

    data_block = "\n\n".join(filter(None, [ctx, player_data, abilities_data, game_stats, peer_comparison, team_power, conv_block]))

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

Write a complete scouting report using ALL the data above. Cite specific numbers throughout.

1. OVERALL GRADE: Give a letter grade (A+/A/B+/B/C+/C/D) with justification based on OVR and positional ranking.
2. ELITE ATTRIBUTES: The 2-3 ratings that make this player dangerous. Cite the actual numbers from the player data.
3. WEAKNESSES: The 1-2 ratings that limit this player. What game situations expose him?
4. GAME LOG: Analyze the per-game stat trends. Is this player producing consistently or boom/bust? Cite specific weeks.
5. POSITIONAL RANKING: Where does this player rank among TSL's top players at his position? Who's ahead of him and by how much OVR?
6. ABILITIES: Explain their active abilities and how they synergize (or don't). What ability tier are they?
7. BOTTOM LINE: One sentence on what this player is worth to a TSL roster, factoring in dev trait and age.

Use **bold** for ratings, the grade, and player names. Be specific — cite actual numbers.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=900)

    return AnalysisResult(
        title=f"Player Scout: {player_name}",
        analysis_type="player",
        sections=[{"label": "Scout Report", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_power_rankings(dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Tiered power rankings with riser/faller analysis."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, "power rankings")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
    ctx = _build_tsl_context(dm)

    # Full standings
    standings = _standings_block(dm)

    # ALL power rankings (not capped at 16)
    power_context = ""
    if not dm.df_power.empty:
        lines = []
        for _, row in dm.df_power.iterrows():
            tn = row.get("teamName", "?")
            rank = row.get("rank", "?")
            score = row.get("score", "?")
            ovr = row.get("ovrRating", "?")
            lines.append(f"  #{rank} {tn} (power: {score}, OVR: {ovr})")
        power_context = "CURRENT POWER RANKINGS (ALL TEAMS):\n" + "\n".join(lines)

    # Point differential analysis
    pt_diff = ""
    if not dm.df_standings.empty:
        diff_lines = []
        for _, row in dm.df_standings.iterrows():
            tn = row.get("teamName", "")
            pf = int(row.get("totalPtsFor", 0) or 0)
            pa = int(row.get("totalPtsAgainst", 0) or 0)
            diff = pf - pa
            w = int(row.get("totalWins", 0) or 0)
            l = int(row.get("totalLosses", 0) or 0)
            if w + l > 0:
                diff_lines.append((diff, f"  {tn} ({w}-{l}): PF {pf}, PA {pa}, Diff {diff:+d}"))
        diff_lines.sort(key=lambda x: x[0], reverse=True)
        pt_diff = "POINT DIFFERENTIAL (sorted best to worst):\n" + "\n".join(line for _, line in diff_lines)

    # Momentum: streaks
    momentum = ""
    if not dm.df_standings.empty:
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

    data_block = "\n\n".join(filter(None, [ctx, power_context, standings, pt_diff, momentum, conv_block]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: TSL Power Rankings — Season {dm.CURRENT_SEASON}, {_week_label(dm.CURRENT_WEEK)}

Write power rankings using ALL the data above. You have every team's record, point differential, power score, and streaks. Use them.

1. ELITE TIER: The 2-3 best teams. Cite their records, point differentials, and power scores. What separates them from everyone else?
2. CONTENDER TIER: The 4-6 teams with playoff upside. Cite their records and what they need to improve (point differential trends).
3. FRINGE / BUBBLE: Teams fighting for their playoff lives. Reference their streaks and recent momentum. Who's most dangerous from here?
4. REBUILDING: Teams mathematically or practically eliminated. Reference their point differentials to explain why.
5. BIGGEST RISER: The one team trending up most dramatically. Cite their win streak and scoring trends.
6. BIGGEST FALLER: The one team that has disappointed most. What does their point differential reveal?

Be opinionated. Cite SPECIFIC records, differentials, and power scores. No vague assessments.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=1000)

    return AnalysisResult(
        title=f"Power Rankings — Season {dm.CURRENT_SEASON} · {_week_label(dm.CURRENT_WEEK)}",
        analysis_type="power",
        sections=[{"label": "Rankings Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_dynasty_profile(owner_name: str, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Full legacy analysis — career record, championships, all-time rank."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"dynasty profile {owner_name}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
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

    # Find their current team for roster/draft assessment
    team_name = None
    if not dm.df_teams.empty:
        mask = dm.df_teams["userName"].str.lower() == db_username.lower()
        if mask.any():
            team_name = dm.df_teams[mask].iloc[0].get("nickName", "")

    roster = _full_roster_block(team_name) if team_name else ""
    abilities = _abilities_block(team_name) if team_name else ""
    draft = _draft_history_block(team_name) if team_name else ""
    trade_context = _trade_context_block(team_name, dm) if team_name else ""

    # Playoff history
    playoff_history = ""
    if run_sql is not None:
        po_rows, po_err = run_sql(
            "SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName, "
            "homeScore, awayScore, winner_user "
            "FROM games WHERE status IN ('2','3') AND CAST(stageIndex AS INTEGER) > 1 "
            "AND (winner_user=? OR loser_user=?) "
            "ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC",
            (db_username, db_username),
        )
        if not po_err and po_rows:
            po_wins = sum(1 for r in po_rows if r.get("winner_user", "").lower() == db_username.lower())
            po_losses = len(po_rows) - po_wins
            po_lines = [f"PLAYOFF HISTORY: {po_wins}W-{po_losses}L ({len(po_rows)} games)"]
            for r in po_rows[:8]:
                sn = r.get("seasonIndex", "?")
                home, away = r["homeTeamName"], r["awayTeamName"]
                hs, as_ = r["homeScore"], r["awayScore"]
                winner = r.get("winner_user", "?")
                po_lines.append(f"  S{sn}: {home} {hs}–{as_} {away} → {winner} wins")
            playoff_history = "\n".join(po_lines)

    elo_traj = _elo_trajectory_block(db_username)

    data_block = "\n\n".join(filter(None, [
        ctx, career, elo_traj, all_time_context, best_season, cur_standing,
        playoff_history, roster, abilities, draft, trade_context, conv_block,
    ]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Dynasty Profile — {db_username}

Write a definitive legacy analysis using ALL the data above.

1. ALL-TIME STANDING: Where does {db_username} rank among all TSL owners? Cite their exact position in the win leaders table and total wins.
2. ERA ANALYSIS: Season-by-season career data is provided. Identify their peak era, rebuild era, and current trajectory. Cite specific season records.
3. PLAYOFF PEDIGREE: How many playoff games have they won? Any deep runs or championships? Reference specific playoff matchup scores.
4. ROSTER LEGACY: Based on their current roster and draft history — how have they built this team? Cite their top X-Factor/Superstar players.
5. CEILING AND FLOOR: Best season record vs worst season record. What's the spread between their peak and valley?
6. LEGACY LINE: A single definitive sentence on what {db_username}'s TSL legacy is. Make it land.
7. CURRENT ARC: How does this season fit — rise, fall, or plateau? Reference scoring trends if available.

Be direct and analytical. This is a definitive assessment, not a tribute piece.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.OPUS, max_tokens=1000)

    return AnalysisResult(
        title=f"Dynasty Profile: {db_username}",
        analysis_type="dynasty",
        sections=[{"label": "Legacy Analysis", "content": result.text.strip()}],
        prediction=None,
        confidence=None,
        metadata={"tier": "OPUS", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )


async def run_betting_profile(owner_name: str, dm, bot=None, *, discord_id: int | None = None, memory=None) -> AnalysisResult:
    """Sportsbook behavior analysis — ROI, win rates, bet types, parlay success."""
    affinity_block, conv_block = await _build_affinity_and_memory(discord_id, memory, f"betting profile {owner_name}")
    persona = _persona_with_mods(get_persona("analytical"), affinity_block)
    ctx = _build_tsl_context(dm)

    db_username = _resolve_owner(owner_name, dm) or owner_name
    betting_data = await _betting_block(owner_name, dm)
    career = _career_block(db_username)

    # Current season context for calibration
    team_name = None
    if not dm.df_teams.empty:
        mask = dm.df_teams["userName"].str.lower() == db_username.lower()
        if mask.any():
            team_name = dm.df_teams[mask].iloc[0].get("nickName", "")
    standings = _standings_block(dm, team_name) if team_name else ""

    if not betting_data or "No bets found" in betting_data or "No Discord ID" in betting_data:
        return AnalysisResult(
            title=f"Betting Profile: {db_username}",
            analysis_type="betting",
            sections=[{"label": "No Data", "content": (
                f"No sportsbook history found for **{db_username}**. "
                f"They haven't placed any bets yet, or their Discord ID isn't mapped in the member registry."
            )}],
            prediction=None,
            confidence=None,
            metadata={"tier": "NONE", "model": "none", "season": dm.CURRENT_SEASON},
        )

    data_block = "\n\n".join(filter(None, [ctx, betting_data, career, standings, conv_block]))

    prompt = f"""{persona}

{data_block}

ANALYSIS TASK: Sportsbook Betting Profile — {db_username}

Analyze this owner's betting behavior using ALL the data above. Reference specific numbers throughout.

1. BETTING IDENTITY: What kind of bettor is {db_username}? Point to their win rate and biggest bet types. Are they a value bettor, a chalk-follower, or a degenerate parlay player?
2. STRENGTHS: Which bet type are they most profitable in? Cite exact win rate and total wagered.
3. EXPLOITABLE TENDENCIES: Where do they bleed coins? Any bet type with a win rate below 40%? Cite the numbers.
4. PARLAY ASSESSMENT: Are their parlay habits smart or reckless? Reference their parlay record and compare to their straight-bet performance.
5. CURRENT STREAK: Reference their recent streak (winning or losing). Does it correlate with their team's on-field performance?
6. VERDICT: Rate them as a bettor — Sharp, Average, or Square. One sentence with the evidence.

Use **bold** for percentages, bet types, and key stats. Be direct — this is a financial autopsy.{_TSL_ONLY_RULE}
"""

    result = await atlas_ai.generate(prompt, tier=Tier.SONNET, max_tokens=900)

    return AnalysisResult(
        title=f"Betting Profile: {db_username}",
        analysis_type="betting",
        sections=[
            {"label": "Raw Stats", "content": betting_data},
            {"label": "Behavioral Analysis", "content": result.text.strip()},
        ],
        prediction=None,
        confidence=None,
        metadata={"tier": "SONNET", "model": result.model, "season": dm.CURRENT_SEASON, "week": dm.CURRENT_WEEK},
    )
