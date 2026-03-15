"""
analysis.py
Stat query engine. Handles name detection, stat leader tables,
player/team profiles, head-to-head, power rankings, and weekly recaps.
Returns structured data dicts that embeds.py turns into Discord embeds.
"""

import pandas as pd
import data_manager as dm

# ── Participation minimums (filter benchwarmers) ─────────────────────────────
QB_MIN_ATT    = 50
RB_MIN_ATT    = 20
REC_MIN_CATCH = 10
DEF_MIN_TKL   = 5


# ── Stat leader helper ────────────────────────────────────────────────────────

def _fmt_value(val) -> str:
    """Format a stat value cleanly — integers as ints, floats to 1 decimal, percentages as %."""
    try:
        f = float(val)
        if f == int(f):
            return str(int(f))
        return f"{f:.1f}"
    except (TypeError, ValueError):
        return str(val)


def stat_leaders(df: pd.DataFrame, stat_col: str, top_n: int = 5,
                 ascending: bool = False,
                 min_col: str = None, min_val: float = 0) -> list[dict]:
    """
    Returns a list of dicts: {name, team, pos, value}
    sorted by stat_col. Optionally filters by min_col >= min_val.
    """
    if df.empty or stat_col not in df.columns:
        return []
    temp = df.copy()
    temp[stat_col] = pd.to_numeric(temp[stat_col], errors="coerce").fillna(0)
    if min_col and min_col in temp.columns:
        temp[min_col] = pd.to_numeric(temp[min_col], errors="coerce").fillna(0)
        temp = temp[temp[min_col] >= min_val]
    top = temp.sort_values(stat_col, ascending=ascending).head(top_n)
    rows = []
    for _, r in top.iterrows():
        name = r.get("extendedName") or r.get("fullName") or r.get("teamName", "Unknown")
        rows.append({
            "name":  name,
            "team":  r.get("teamName", ""),
            "pos":   r.get("pos", ""),
            "value": _fmt_value(r[stat_col]),
        })
    return rows


# ── Name detection ────────────────────────────────────────────────────────────

def find_players(query: str) -> list[str]:
    """Returns fullName keys for players mentioned in the query."""
    q = query.lower()
    matches, seen = [], set()
    for df in [dm.df_offense, dm.df_defense]:
        if df.empty or "fullName" not in df.columns:
            continue
        for _, row in df.drop_duplicates("fullName").iterrows():
            full  = (row.get("extendedName") or row.get("fullName", "")).lower()
            short = row.get("fullName", "").lower()
            last  = full.split()[-1] if full else ""
            if full in q or short in q or (len(last) > 3 and last in q):
                key = row["fullName"]
                if key not in seen:
                    seen.add(key)
                    matches.append(key)
    return matches


def find_teams(query: str) -> list[str]:
    """Returns teamName values for teams mentioned in the query."""
    q = query.lower()
    matches = []
    if dm.df_standings.empty or "teamName" not in dm.df_standings.columns:
        return matches
    for team in dm.df_standings["teamName"].dropna().unique():
        parts = [team.lower()] + [w.lower() for w in team.split() if len(w) > 3]
        if any(p in q for p in parts):
            matches.append(team)
    return matches


# ── Player profile ────────────────────────────────────────────────────────────

def player_profile(full_name: str) -> dict:
    """Full stat card for a player. Returns structured dict."""
    result = {
        "type":    "player",
        "name":    full_name,
        "team":    "",
        "pos":     "",
        "offense": {},
        "defense": {},
        "bio":     {},
        "abilities": [],
    }

    # Roster bio — search _players_cache directly (dm.get_player_profile doesn't exist)
    profile = None
    for p in dm.get_players():
        pname = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
        if pname.lower() == full_name.lower() or (p.get("extendedName") or "").lower() == full_name.lower():
            profile = p
            break
    if profile:
        result["name"]  = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
        result["team"]  = profile.get("teamName", "")
        result["pos"]   = profile.get("pos", "")
        result["bio"]   = {
            "Age":             profile.get("age"),
            "Dev":             profile.get("dev"),
            "OVR":             profile.get("playerBestOvr"),
            "Speed":           profile.get("speedRating"),
            "Jersey":          profile.get("jerseyNum"),
            "Contract":        f"{profile.get('contractYearsLeft', 0)}yr",
            "Cap Hit":         str(profile.get('capHit', '?')),
        }
        # Abilities: filter full cache by player name
        abilities_raw = dm.get_player_abilities()
        result["abilities"] = [
            a.get("title", "") for a in abilities_raw
            if f"{a.get('firstName', '')} {a.get('lastName', '')}".strip().lower() == full_name.lower()
            and a.get("title")
        ]

    # Offense stats
    for df in [dm.df_offense]:
        if df.empty or "fullName" not in df.columns:
            continue
        row = df[df["fullName"] == full_name]
        if row.empty:
            last = full_name.split()[-1]
            row = df[df["fullName"].str.contains(last, case=False, na=False)]
        if not row.empty:
            r = row.iloc[0]
            result["team"] = result["team"] or r.get("teamName", "")
            result["pos"]  = result["pos"]  or r.get("pos", "")
            # Only show non-zero stats
            num = row.select_dtypes(include="number").columns
            result["offense"] = {c: r[c] for c in num if r[c] != 0}

    # Defense stats
    for df in [dm.df_defense]:
        if df.empty or "fullName" not in df.columns:
            continue
        row = df[df["fullName"] == full_name]
        if row.empty:
            last = full_name.split()[-1]
            row = df[df["fullName"].str.contains(last, case=False, na=False)]
        if not row.empty:
            r = row.iloc[0]
            result["team"] = result["team"] or r.get("teamName", "")
            result["pos"]  = result["pos"]  or r.get("pos", "")
            num = row.select_dtypes(include="number").columns
            result["defense"] = {c: r[c] for c in num if r[c] != 0}

    return result


# ── Team profile ──────────────────────────────────────────────────────────────

def team_profile(team_name: str) -> dict:
    """Full team card — standings, offense, defense, top players, recent form."""
    result = {
        "type":        "team",
        "team":        team_name,
        "owner":       dm.get_team_owner(team_name),
        "record":      dm.get_team_record(team_name),
        "standings":   {},
        "offense":     {},
        "defense":     {},
        "top_players": {},
        "recent":      dm.get_last_n_games(team_name, 5),
    }
    t = team_name.lower()

    # Standings block
    if not dm.df_standings.empty:
        row = dm.df_standings[dm.df_standings["teamName"].str.lower() == t]
        if row.empty:
            row = dm.df_standings[dm.df_standings["teamName"].str.lower().str.contains(t, na=False)]
        if not row.empty:
            r = row.iloc[0]
            result["team"] = r["teamName"]
            result["standings"] = {
                "Rank":         f"#{int(r.get('rank', 0))}",
                "Record":       f"{int(r.get('totalWins',0))}-{int(r.get('totalLosses',0))}-{int(r.get('totalTies',0))}",
                "Pts For":      r.get("ptsFor"),
                "Pts Against":  r.get("ptsAgainst"),
                "Net Pts":      r.get("netPts"),
                "TO Diff":      r.get("tODiff"),
                "Win %":        r.get("winPct"),
            }
            result["offense"] = {
                "Total Yds":    r.get("offTotalYds"),
                "Pass Yds":     r.get("offPassYds"),
                "Rush Yds":     r.get("offRushYds"),
                "Off Yds Rank": f"#{int(r.get('offTotalYdsRank', 0))}",
            }
            result["defense"] = {
                "Yds Allowed":  r.get("defTotalYds"),
                "Pass Allowed": r.get("defPassYds"),
                "Rush Allowed": r.get("defRushYds"),
                "Def Yds Rank": f"#{int(r.get('defTotalYdsRank', 0))}",
            }

    # Team stats enrichment (red zone, 3rd down, penalties)
    if not dm.df_team_stats.empty:
        ts = dm.df_team_stats[dm.df_team_stats["teamName"].str.lower() == t]
        if not ts.empty:
            r = ts.iloc[0]
            result["offense"]["Red Zone %"]  = r.get("offRedZonePct")
            result["offense"]["3rd Down %"]  = r.get("off3rdDownConvPct")
            result["offense"]["Penalties"]   = f"{int(r.get('penalties',0))} ({int(r.get('penaltyYds',0))} yds)"
            result["defense"]["Red Zone % Allowed"] = r.get("defRedZonePct")

    # Top skill players
    for label, sort_col, mc, mv in [
        ("QB",    "passYds",   "passAtt",    QB_MIN_ATT),
        ("RB",    "rushYds",   "rushAtt",    RB_MIN_ATT),
        ("WR/TE", "recYds",    "recCatches", REC_MIN_CATCH),
    ]:
        if dm.df_offense.empty:
            continue
        tdf = dm.df_offense[dm.df_offense["teamName"].str.lower().str.contains(t, na=False)].copy()
        tdf[sort_col] = pd.to_numeric(tdf[sort_col], errors="coerce").fillna(0)
        if mc in tdf.columns:
            tdf[mc] = pd.to_numeric(tdf[mc], errors="coerce").fillna(0)
            tdf = tdf[tdf[mc] >= mv]
        if not tdf.empty:
            top = tdf.sort_values(sort_col, ascending=False).iloc[0]
            name = top.get("extendedName") or top.get("fullName")
            result["top_players"][label] = f"{name} — {sort_col}: {top[sort_col]}"

    # Top defender
    if not dm.df_defense.empty:
        tdf = dm.df_defense[dm.df_defense["teamName"].str.lower().str.contains(t, na=False)].copy()
        tdf["defTotalTackles"] = pd.to_numeric(tdf["defTotalTackles"], errors="coerce").fillna(0)
        if not tdf.empty:
            top = tdf.sort_values("defTotalTackles", ascending=False).iloc[0]
            name = top.get("extendedName") or top.get("fullName")
            result["top_players"]["DEF"] = f"{name} — tackles: {top['defTotalTackles']}"

    return result


# ── Head-to-head ──────────────────────────────────────────────────────────────

def head_to_head(team_a: str, team_b: str) -> dict:
    h2h = dm.get_h2h_record(team_a, team_b)
    return {
        "type":    "h2h",
        "team_a":  team_profile(team_a),
        "team_b":  team_profile(team_b),
        "h2h":     h2h,
    }


# ── Power rankings ────────────────────────────────────────────────────────────

def power_rankings() -> list[dict]:
    """
    Composite score = (win% * 40) + (netPts_norm * 30) + (tODiff_norm * 15) + (offRank_inv * 8) + (defRank_inv * 7)
    Returns sorted list of {rank, team, owner, record, score, wins, net_pts, to_diff}.
    """
    if dm.df_standings.empty:
        return []

    df = dm.df_standings.copy()
    df["winPct"]    = pd.to_numeric(df["winPct"],    errors="coerce").fillna(0)
    df["netPts"]    = pd.to_numeric(df["netPts"],    errors="coerce").fillna(0)
    df["tODiff"]    = pd.to_numeric(df["tODiff"],    errors="coerce").fillna(0)
    df["offTotalYdsRank"] = pd.to_numeric(df["offTotalYdsRank"], errors="coerce").fillna(16)
    df["defTotalYdsRank"] = pd.to_numeric(df["defTotalYdsRank"], errors="coerce").fillna(16)

    def normalize(series):
        mn, mx = series.min(), series.max()
        return (series - mn) / (mx - mn + 1e-9)

    # Canonical power-ranking formula — weights must sum to 100:
    #   Win%: 40, Net Points: 30, Turnover Diff: 15, Off Yds Rank: 8, Def Yds Rank: 7
    WEIGHT_WIN_PCT  = 40
    WEIGHT_NET_PTS  = 30
    WEIGHT_TO_DIFF  = 15
    WEIGHT_OFF_RANK = 8
    WEIGHT_DEF_RANK = 7

    df["score"] = (
        normalize(df["winPct"])    * WEIGHT_WIN_PCT +
        normalize(df["netPts"])    * WEIGHT_NET_PTS +
        normalize(df["tODiff"])    * WEIGHT_TO_DIFF +
        normalize(32 - df["offTotalYdsRank"]) * WEIGHT_OFF_RANK +
        normalize(32 - df["defTotalYdsRank"]) * WEIGHT_DEF_RANK
    ).round(1)

    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    result = []
    for i, row in df.iterrows():
        team = row["teamName"]
        result.append({
            "rank":    i + 1,
            "team":    team,
            "owner":   dm.get_team_owner(team),
            "record":  f"{int(row.get('totalWins',0))}-{int(row.get('totalLosses',0))}",
            "score":   round(float(row["score"]), 1),
            "wins":    int(row.get("totalWins", 0)),
            "net_pts": int(row.get("netPts", 0)),
            "to_diff": int(row.get("tODiff", 0)),
        })
    return result


# ── Weekly recap ──────────────────────────────────────────────────────────────

def weekly_recap(week: int = None) -> dict:
    """
    Returns structured recap data for the given week (defaults to last completed).
    Includes scores, biggest win, closest game, top performer.
    """
    results = dm.get_weekly_results(week=week)
    if not results:
        return {"type": "recap", "week": week, "games": [], "highlights": {}}

    actual_week = results[0]["week"]
    highlights = {}

    if results:
        biggest = max(results, key=lambda g: abs(g["home_score"] - g["away_score"]))
        closest = min(results, key=lambda g: abs(g["home_score"] - g["away_score"]))
        highlights["biggest_win"] = (
            f"{biggest['home']} def. {biggest['away']} "
            f"{biggest['home_score']}-{biggest['away_score']}"
            if biggest["home_score"] > biggest["away_score"]
            else f"{biggest['away']} def. {biggest['home']} "
            f"{biggest['away_score']}-{biggest['home_score']}"
        )
        highlights["closest_game"] = (
            f"{closest['home']} vs {closest['away']} "
            f"{closest['home_score']}-{closest['away_score']}"
        )

    return {
        "type":   "recap",
        "week":   actual_week,
        "games":  results,
        "highlights": highlights,
    }


# ── Recent trades ─────────────────────────────────────────────────────────────

def recent_trades(season: int = None, n: int = 5) -> dict:
    target_season = season or dm.CURRENT_SEASON
    if dm.df_trades.empty:
        return {"type": "trades", "season": target_season, "trades": []}
    df = dm.df_trades.copy()
    if "seasonIndex" in df.columns:
        df["seasonIndex"] = pd.to_numeric(df["seasonIndex"], errors="coerce")
        df = df[df["seasonIndex"] == target_season]
    if "status" in df.columns:
        df = df[df["status"].str.lower() == "accepted"]
    if "tradeId" in df.columns:
        df = df.sort_values("tradeId", ascending=False)
    trades = df.head(n).to_dict("records")
    return {
        "type":   "trades",
        "season": target_season,
        "trades": trades,
    }


# ── Main query router ─────────────────────────────────────────────────────────

def route_query(query: str) -> dict:
    """
    Master router. Returns a structured dict describing what was found.
    The 'type' key tells embeds.py how to render it.
    Falls back to a keyword stat block for ATLAS's text response.
    """
    q = query.lower()

    # Special commands
    if any(w in q for w in ["power rank", "power ranking", "rankings"]):
        return {"type": "power_rankings", "data": power_rankings()}

    if any(w in q for w in ["recap", "last week", "week recap", "results"]):
        return weekly_recap()

    if any(w in q for w in ["trade", "trades", "who got traded", "trade history"]):
        return recent_trades()

    # Name detection
    teams   = find_teams(q)
    players = find_players(q)

    if len(teams) >= 2:
        return head_to_head(teams[0], teams[1])

    if len(teams) == 1 and not players:
        return team_profile(teams[0])

    if len(players) == 1:
        return player_profile(players[0])

    if len(players) > 1:
        return {
            "type":    "multi_player",
            "players": [player_profile(p) for p in players],
        }

    # Keyword stat tables
    return _keyword_stats(q)


def _keyword_stats(q: str) -> dict:
    """Keyword-based stat leader tables. Returns type='stat_block'."""
    is_worst = any(w in q for w in ["worst", "least", "bottom", "suck", "bad", "terrible"])
    asc = is_worst
    blocks = []

    # Detect intent — prevent "interceptions" from triggering "rec" block
    is_pure_defense = any(w in q for w in ["sack", "interception", "pick", "tackle", "forced fumble", "deflect", "def ", "defense"])
    is_pure_qb      = any(w in q for w in ["qb", "quarterback", "passer"])

    # PASSING
    if any(w in q for w in ["pass", "qb", "quarterback", "throw", "passer"]):
        blocks.append(("Pass Yards",      stat_leaders(dm.df_offense, "passYds",     ascending=asc, min_col="passAtt", min_val=QB_MIN_ATT)))
        blocks.append(("Pass TDs",        stat_leaders(dm.df_offense, "passTDs",     ascending=asc, min_col="passAtt", min_val=QB_MIN_ATT)))
        blocks.append(("Interceptions",   stat_leaders(dm.df_offense, "passInts",    ascending=not asc, min_col="passAtt", min_val=QB_MIN_ATT)))
        blocks.append(("Comp %",          stat_leaders(dm.df_offense, "passCompPct", ascending=asc, min_col="passAtt", min_val=QB_MIN_ATT)))

    # RUSHING
    if any(w in q for w in ["rush", "run", "hb", "rb", "running back"]) and not is_pure_defense:
        blocks.append(("Rush Yards",      stat_leaders(dm.df_offense, "rushYds",           ascending=asc, min_col="rushAtt", min_val=RB_MIN_ATT)))
        blocks.append(("Rush TDs",        stat_leaders(dm.df_offense, "rushTDs",           ascending=asc, min_col="rushAtt", min_val=RB_MIN_ATT)))
        blocks.append(("Broken Tackles",  stat_leaders(dm.df_offense, "rushBrokenTackles", ascending=asc, min_col="rushAtt", min_val=RB_MIN_ATT)))
        blocks.append(("Rush Fumbles",    stat_leaders(dm.df_offense, "rushFum",           ascending=not asc, min_col="rushAtt", min_val=RB_MIN_ATT)))

    # RECEIVING — only if query isn't purely defensive
    if any(w in q for w in ["rec", "catch", "wr", "te", "receiver", "target"]) and not is_pure_defense:
        blocks.append(("Rec Yards",   stat_leaders(dm.df_offense, "recYds",     ascending=asc, min_col="recCatches", min_val=REC_MIN_CATCH)))
        blocks.append(("Rec TDs",     stat_leaders(dm.df_offense, "recTDs",     ascending=asc, min_col="recCatches", min_val=REC_MIN_CATCH)))
        blocks.append(("Catches",     stat_leaders(dm.df_offense, "recCatches", ascending=asc, min_col="recCatches", min_val=REC_MIN_CATCH)))
        # Drop Rate = drops / (catches + drops) — worst = highest rate
        _wr = dm.df_offense[dm.df_offense["recCatches"] >= REC_MIN_CATCH].copy()
        _wr["dropRate"] = (_wr["recDrops"] / (_wr["recCatches"] + _wr["recDrops"]).clip(lower=1) * 100).round(1)
        _dr_rows = []
        for _, r in _wr.sort_values("dropRate", ascending=False).head(5).iterrows():
            name = r.get("extendedName") or r.get("fullName", "?")
            _dr_rows.append({"name": name, "team": r.get("teamName",""), "pos": r.get("pos",""), "value": f"{r['dropRate']:.1f}%"})
        blocks.append(("Drop Rate", _dr_rows))

    # DEFENSE
    if any(w in q for w in ["defense", "tackle", "sack", "interception", "int", "pick", "forced", "fumble", "deflect", "db", "lb", "dl", "edge"]):
        specific = any(w in q for w in ["sack", "interception", "int", "pick", "tackle", "fumble", "forced", "deflect"])
        if "sack"    in q: blocks.append(("Sacks",         stat_leaders(dm.df_defense, "defSacks",        ascending=asc)))
        if "interception" in q or "int" in q or "pick" in q:
            blocks.append(("Interceptions", stat_leaders(dm.df_defense, "defInts", ascending=asc)))
        if "tackle"  in q: blocks.append(("Tackles",        stat_leaders(dm.df_defense, "defTotalTackles", ascending=asc, min_col="defTotalTackles", min_val=DEF_MIN_TKL)))
        if "fumble" in q or "forced" in q: blocks.append(("Forced Fum",  stat_leaders(dm.df_defense, "defForcedFum", ascending=asc)))
        if "deflect" in q: blocks.append(("Deflections",    stat_leaders(dm.df_defense, "defDeflections",  ascending=asc)))
        if not specific:
            blocks.append(("Tackles",  stat_leaders(dm.df_defense, "defTotalTackles", ascending=asc, min_col="defTotalTackles", min_val=DEF_MIN_TKL)))
            blocks.append(("Sacks",    stat_leaders(dm.df_defense, "defSacks",        ascending=asc)))
            blocks.append(("INTs",     stat_leaders(dm.df_defense, "defInts",         ascending=asc)))

    # TEAM OFFENSE
    if any(w in q for w in ["team offense", "best offense", "worst offense", "scoring", "pts for", "points for"]):
        blocks.append(("Off Total Yds", stat_leaders(dm.df_standings, "offTotalYds", ascending=asc)))
        blocks.append(("Points For",    stat_leaders(dm.df_standings, "ptsFor",      ascending=asc)))
        blocks.append(("Off Pass Yds",  stat_leaders(dm.df_standings, "offPassYds",  ascending=asc)))
        blocks.append(("Off Rush Yds",  stat_leaders(dm.df_standings, "offRushYds",  ascending=asc)))

    # TEAM DEFENSE
    if any(w in q for w in ["team defense", "best defense", "worst defense", "pts against", "points against"]):
        da = not asc
        blocks.append(("Def Yds Allowed", stat_leaders(dm.df_standings, "defTotalYds", ascending=da)))
        blocks.append(("Pts Against",     stat_leaders(dm.df_standings, "ptsAgainst",  ascending=da)))
        blocks.append(("Pass Allowed",    stat_leaders(dm.df_standings, "defPassYds",  ascending=da)))
        blocks.append(("Rush Allowed",    stat_leaders(dm.df_standings, "defRushYds",  ascending=da)))

    # STANDINGS
    if any(w in q for w in ["standings", "record", "wins", "best team", "worst team", "differential", "turnover"]):
        blocks.append(("Wins",    stat_leaders(dm.df_standings, "totalWins", ascending=asc)))
        blocks.append(("Net Pts", stat_leaders(dm.df_standings, "netPts",    ascending=asc)))
        blocks.append(("TO Diff", stat_leaders(dm.df_standings, "tODiff",    ascending=asc)))

    # RED ZONE / 3RD DOWN
    if any(w in q for w in ["red zone", "redzone", "3rd down", "third down"]):
        blocks.append(("Red Zone %",   stat_leaders(dm.df_team_stats, "offRedZonePct",    ascending=asc)))
        blocks.append(("3rd Down %",   stat_leaders(dm.df_team_stats, "off3rdDownConvPct",ascending=asc)))

    return {
        "type":     "stat_block",
        "is_worst": asc,
        "blocks":   blocks,
    }


def build_context_string(data: dict) -> str:
    """
    Converts the structured data dict into a plain-text context block
    for ATLAS's Gemini prompt.
    """
    t = data.get("type", "")

    if t == "power_rankings":
        lines = ["=== TSL POWER RANKINGS ==="]
        for r in data["data"][:16]:
            lines.append(
                f"#{r['rank']} {r['team']} ({r['record']}) — owner: {r['owner']} | "
                f"score: {r['score']} | net pts: {r['net_pts']} | TO diff: {r['to_diff']}"
            )
        return "\n".join(lines)

    if t == "recap":
        lines = [f"=== WEEK {data['week']} RECAP ==="]
        for g in data["games"]:
            winner = g["home"] if g["home_score"] > g["away_score"] else g["away"]
            lines.append(
                f"{g['home']} {g['home_score']} — {g['away_score']} {g['away']}  "
                f"({g['home_user']} vs {g['away_user']})"
            )
        h = data.get("highlights", {})
        if h.get("biggest_win"):  lines.append(f"Biggest win: {h['biggest_win']}")
        if h.get("closest_game"): lines.append(f"Closest game: {h['closest_game']}")
        return "\n".join(lines)

    if t == "trades":
        lines = [f"=== RECENT TRADES (Season {data['season']}) ==="]
        for tr in data["trades"]:
            lines.append(
                f"{tr['team1Name']} sent: {tr['team1Sent'].strip()}\n"
                f"{tr['team2Name']} sent: {tr['team2Sent'].strip()}\n"
            )
        return "\n".join(lines)

    if t == "player":
        d = data
        lines = [f"=== {d['name']} | {d['pos']} | {d['team']} ==="]
        if d["bio"]:
            lines.append("Bio: " + " | ".join(f"{k}: {v}" for k, v in d["bio"].items()))
        if d["abilities"]:
            lines.append("Abilities: " + ", ".join(d["abilities"]))
        if d["offense"]:
            lines.append("Offense: " + " | ".join(f"{k}: {v}" for k, v in d["offense"].items()))
        if d["defense"]:
            lines.append("Defense: " + " | ".join(f"{k}: {v}" for k, v in d["defense"].items()))
        return "\n".join(lines)

    if t == "team":
        d = data
        lines = [f"=== {d['team']} | {d['record']} | Owner: {d['owner']} ==="]
        if d["standings"]: lines.append("Standings: " + " | ".join(f"{k}: {v}" for k, v in d["standings"].items()))
        if d["offense"]:   lines.append("Offense: "   + " | ".join(f"{k}: {v}" for k, v in d["offense"].items()))
        if d["defense"]:   lines.append("Defense: "   + " | ".join(f"{k}: {v}" for k, v in d["defense"].items()))
        if d["top_players"]: lines.append("Key Players: " + " | ".join(f"{k}: {v}" for k, v in d["top_players"].items()))
        if d["recent"]:
            team = d.get("team", "")
            form = " ".join(
                "W" if (
                    (g.get("home", "") == team and g.get("home_score", 0) > g.get("away_score", 0))
                    or (g.get("away", "") == team and g.get("away_score", 0) > g.get("home_score", 0))
                ) else "L"
                for g in d["recent"]
            )
            lines.append(f"Last 5 form: {form}")
        return "\n".join(lines)

    if t == "h2h":
        a, b = data["team_a"], data["team_b"]
        h = data["h2h"]
        lines = [
            f"=== H2H: {a['team']} vs {b['team']} ===",
            f"All-time: {a['team']} {h['a_wins']} — {h['b_wins']} {b['team']} ({h['ties']} ties)",
            build_context_string(a),
            build_context_string(b),
        ]
        return "\n".join(lines)

    if t == "multi_player":
        return "\n\n".join(build_context_string(p) for p in data["players"])

    if t == "stat_block":
        lines = []
        label = "BOTTOM 5" if data["is_worst"] else "TOP 5"
        for title, rows in data["blocks"]:
            if not rows:
                continue
            lines.append(f"--- {label} | {title} ---")
            for r in rows:
                tag = f"{r['pos']}, {r['team']}" if r["pos"] else r["team"]
                lines.append(f"  {r['name']} ({tag}): {r['value']}")
        return "\n".join(lines)

    return "(No data found)"

