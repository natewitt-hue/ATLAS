"""
intelligence.py

Advanced intelligence modules for ATLAS:
  - Draft class grading (seasons 2-current, real TSL drafts)
  - Hot/Cold tracker (last 3 games vs season avg)
  - Clutch stats (performance in close games ≤7pts)
  - Owner profiles (Discord user → team mapping + memory)
  - Beef mode (two owners in the same conversation)

Fixes applied (v4):
  - get_draft_class() now uses player_draft_map DB table for accurate drafting team.
    Previously used players.teamName (current team) which broke for traded players.
  - Added owner_tenure DB table awareness for tenure-filtered stats.

Fixes applied (v3):
  - _load_full_players() uses dm.get_players() (/export/players CSV) instead of
    dm.df_players (stat leaders) — CSV has rookieYear, draftRound, dev, etc.
  - _load_raw_offense() uses dm.df_offense (stat leaders, per-game stats).
  - _load_raw_defense() uses dm.df_defense (stat leaders, per-game stats).
  - get_weekly_results() now returns status==3 final games only (no live game scores).
  - get_team_owner() prefers df_teams (has userName) over df_standings.
  - build_owner_map() called via bot.py on_ready() — owner lookups now work.
"""

import sqlite3
import os
import pandas as pd
import numpy as np
import data_manager as dm

DB_PATH = os.path.join(os.path.dirname(__file__), "tsl_history.db")

# ── Constants ─────────────────────────────────────────────────────────────────

# rookieYear in players.csv maps to TSL season index (calendarYear)
# Season 1 (2025) is the initial roster build — not a real draft
# Real TSL draft classes start season 2 (2026)
# Auto-extends beyond S5 via arithmetic: season = rookieYear - 2024
_YEAR_BASE = 2024   # rookieYear = _YEAR_BASE + season
YEAR_TO_SEASON = {yr: yr - _YEAR_BASE for yr in range(2025, 2035)}
SEASON_TO_YEAR = {v: k for k, v in YEAR_TO_SEASON.items()}

# Madden uses non-standard round numbering (2-8 instead of 1-7 for TSL)
# Round 2 = Round 1 pick in TSL, etc.
ROUND_LABELS = {2: "R1", 3: "R2", 4: "R3", 5: "R4", 6: "R5", 7: "R6", 8: "R7"}

# Dev trait tiers for grading
DEV_SCORE = {
    "Superstar X-Factor": 4,
    "Superstar": 3,
    "Star": 2,
    "Normal": 1,
}

# Letter grade thresholds for draft classes
GRADE_THRESHOLDS = [
    (3.5, "A+"), (3.2, "A"), (2.9, "A-"),
    (2.6, "B+"), (2.3, "B"), (2.0, "B-"),
    (1.7, "C+"), (1.4, "C"), (1.1, "C-"),
    (0.0, "D"),
]

# ── Identity caches (populated dynamically from tsl_members DB) ───────────────
# Previously hardcoded; now loaded at startup via _load_identity_cache().
KNOWN_MEMBERS: dict[int, str] = {}          # discord_id → nickname
_nickname_to_ids: dict[str, list[int]] = {} # lowercase nickname → [discord_ids]
KNOWN_MEMBER_TEAMS: dict[int, str] = {}     # discord_id → team nickname


def _load_identity_cache():
    """Populate identity caches from tsl_members + roster.

    Called by build_owner_map() during startup — after build_member_table()
    and roster.load() have run.
    """
    try:
        import build_member_db as member_db
        members = member_db.get_active_members()
    except Exception:
        return

    KNOWN_MEMBERS.clear()
    _nickname_to_ids.clear()
    KNOWN_MEMBER_TEAMS.clear()

    for m in members:
        did_str = m.get("discord_id")
        if not did_str:
            continue
        try:
            did = int(did_str)
        except (ValueError, TypeError):
            continue

        nick = m.get("nickname")
        if nick:
            KNOWN_MEMBERS[did] = nick
            _nickname_to_ids.setdefault(nick.lower(), []).append(did)

    # Team assignments from roster (team nicknames like "Bears", "Bengals")
    try:
        import roster
        for entry in roster.get_all():
            KNOWN_MEMBER_TEAMS[entry.discord_id] = entry.team_name
    except Exception:
        pass


# ── Draft class analysis ──────────────────────────────────────────────────────

def _load_full_players() -> pd.DataFrame:
    """
    Return the full players DataFrame with draft/dev columns.
    Uses dm.get_players() (full /export/players CSV) which contains rookieYear,
    draftRound, draftPick, yearsPro, dev, playerBestOvr, etc.

    Do NOT use dm.df_players — that's built from stat leader endpoints
    (passStats, rushLeaders, etc.) and does not contain draft columns.

    MM export players.csv key fields:
      rosterId, firstName, lastName, pos, yearsPro, draftPick, draftRound,
      rookieYear (TSL season calendarYear int), dev, teamId, teamName,
      playerBestOvr, ability1-6, all ratings, isFA, isOnIR, retired
    """
    raw = dm.get_players()
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    if df.empty:
        return df
    if "fullName" not in df.columns:
        fn = df.get("firstName", pd.Series(dtype=str)).fillna("")
        ln = df.get("lastName",  pd.Series(dtype=str)).fillna("")
        df["fullName"] = fn + " " + ln
    for col in ["draftRound", "draftPick", "rookieYear", "playerBestOvr", "yearsPro"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def _letter_grade(score: float) -> str:
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def get_draft_class(season: int) -> dict:
    """
    Full draft class breakdown for a given TSL season (2-current).
    Uses player_draft_map from tsl_history.db — resolves drafting_team from
    first statistical appearance, NOT players.teamName (which is current team
    and breaks for traded players).
    """
    if season < 2 or season > dm.CURRENT_SEASON:
        return {"error": f"No real draft data for season {season}. TSL drafts started season 2."}

    # ── Pull from DB (accurate drafting team) ─────────────────────────────
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT extendedName, drafting_team, draftRound, draftPick,
                   dev, playerBestOvr, pos, was_traded
            FROM player_draft_map
            WHERE CAST(drafting_season AS INTEGER) = ?
        """, (season,)).fetchall()
        conn.close()
    except Exception as e:
        return {"error": f"DB unavailable: {e}. Run build_tsl_db.py first."}

    if not rows:
        return {"error": f"No draft data for season {season} in DB. Rebuild tsl_history.db."}

    cls = pd.DataFrame(rows, columns=[
        "extendedName","teamName","draftRound","draftPick",
        "dev","playerBestOvr","pos","was_traded"
    ])
    for col in ["draftRound","draftPick","playerBestOvr"]:
        cls[col] = pd.to_numeric(cls[col], errors="coerce").fillna(0)

    cls["devScore"]    = cls["dev"].map(DEV_SCORE).fillna(1)
    cls["roundLabel"]  = cls["draftRound"].map(ROUND_LABELS).fillna("UDFA")
    ovr_norm           = (cls["playerBestOvr"] - 60).clip(0) / 40
    cls["gradeScore"]  = cls["devScore"] * 0.6 + ovr_norm * 0.4
    cls["stealScore"]  = cls["devScore"] / (cls["draftRound"].clip(2, 8) / 2)

    class_grade_score = cls["gradeScore"].mean()
    letter = _letter_grade(class_grade_score)
    target_year = SEASON_TO_YEAR.get(season, season + 2024)

    def _pick_cols(df):
        return df[["extendedName","teamName","pos","roundLabel","draftPick",
                   "playerBestOvr","dev"]].to_dict("records")

    steals   = _pick_cols(cls.nlargest(5, "stealScore"))
    top_picks= _pick_cols(cls.nlargest(8, "playerBestOvr"))
    early    = cls[cls["draftRound"].isin([2, 3])]
    busts    = _pick_cols(
        early[(early["dev"] == "Normal") | (early["playerBestOvr"] < 75)]
        .sort_values("playerBestOvr").head(5)
    )

    team_grades = (
        cls.groupby("teamName")
        .agg(
            picks=("extendedName", "count"),
            avgOVR=("playerBestOvr", "mean"),
            xfactors=("dev", lambda x: (x == "Superstar X-Factor").sum()),
            superstars=("dev", lambda x: (x == "Superstar").sum()),
            stars=("dev", lambda x: (x == "Star").sum()),
            gradeScore=("gradeScore", "mean"),
        )
        .round(1)
        .reset_index()
        .sort_values("gradeScore", ascending=False)
    )
    team_grades["grade"] = team_grades["gradeScore"].apply(_letter_grade)

    return {
        "type":        "draft_class",
        "season":      season,
        "year":        target_year,
        "total_picks": len(cls),
        "letter_grade":letter,
        "grade_score": round(class_grade_score, 2),
        "dev_counts":  cls["dev"].value_counts().to_dict(),
        "avg_ovr":     round(cls["playerBestOvr"].mean(), 1),
        "top_picks":   top_picks,
        "steals":      steals,
        "busts":       busts,
        "team_grades": team_grades.head(10).to_dict("records"),
    }


def compare_draft_classes() -> dict:
    """Compare all TSL draft classes (seasons 2-current) side by side."""
    classes = []
    for season in range(2, dm.CURRENT_SEASON + 1):
        dc = get_draft_class(season)
        if "error" not in dc:
            classes.append({
                "season":      dc["season"],
                "year":        dc["year"],
                "grade":       dc["letter_grade"],
                "avg_ovr":     dc["avg_ovr"],
                "xfactors":    dc["dev_counts"].get("Superstar X-Factor", 0),
                "superstars":  dc["dev_counts"].get("Superstar", 0),
                "stars":       dc["dev_counts"].get("Star", 0),
                "total_picks": dc["total_picks"],
            })
    return {"type": "draft_comparison", "classes": classes}


# ── Hot / Cold tracker ────────────────────────────────────────────────────────

def _load_raw_offense() -> pd.DataFrame:
    """
    Return per-game offensive stats from dm.df_offense (stat leader endpoints).
    offensive.csv fields: id, fullName, extendedName, seasonIndex, stageIndex,
    weekIndex, gameId, teamId, teamName, rosterId, pos, pass/rush/rec stats.
    """
    df = dm.df_offense.copy()
    NUM = ["passAtt", "passYds", "passTDs", "passInts", "passSacks",
           "rushAtt", "rushYds", "rushTDs", "rushFum",
           "recCatches", "recYds", "recTDs", "recDrops",
           "seasonIndex", "stageIndex", "weekIndex"]
    for c in NUM:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _load_raw_defense() -> pd.DataFrame:
    """
    Return per-game defensive stats from dm.df_defense (stat leader endpoints).
    defensive.csv fields: statId, fullName, extendedName, seasonIndex, stageIndex,
    weekIndex, gameId, teamId, teamName, rosterId, pos, defTotalTackles, defSacks,
    defSafeties, defInts, defIntReturnYds, defForcedFum, defFumRec, defTDs,
    defCatchAllowed, defDeflections, defPts.
    """
    df = dm.df_defense.copy()
    NUM = ["defTotalTackles", "defSacks", "defInts", "defForcedFum",
           "defDeflections", "seasonIndex", "stageIndex", "weekIndex"]
    for c in NUM:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def get_hot_cold(player_name: str, last_n: int = 3) -> dict:
    """
    Compare a player's last N games vs their season average.
    Returns structured dict with trend direction and key stat deltas.
    """
    for load_fn, stat_cols, group in [
        (_load_raw_offense,
         ["passAtt", "passYds", "passTDs", "passInts", "rushYds", "rushTDs",
          "recCatches", "recYds", "recTDs"],
         "offense"),
        (_load_raw_defense,
         ["defTotalTackles", "defSacks", "defInts", "defForcedFum", "defDeflections"],
         "defense"),
    ]:
        df = load_fn()
        if "fullName" not in df.columns:
            if "firstName" in df.columns and "lastName" in df.columns:
                df["fullName"] = df["firstName"].fillna("") + " " + df["lastName"].fillna("")
            else:
                continue

        if "seasonIndex" in df.columns and "stageIndex" in df.columns:
            player_df = df[
                (df["fullName"] == player_name) &
                (df["seasonIndex"] == dm.CURRENT_SEASON) &
                (df["stageIndex"] == dm.REGULAR_STAGE)
            ].sort_values("weekIndex")
        elif "seasonIndex" in df.columns:
            player_df = df[
                (df["fullName"] == player_name) &
                (df["seasonIndex"] == dm.CURRENT_SEASON)
            ]
        else:
            player_df = df[df["fullName"] == player_name]

        if player_df.empty:
            last = player_name.split(".")[-1] if "." in player_name else player_name.split()[-1]
            base = df[df["fullName"].str.contains(last, case=False, na=False)]
            if "seasonIndex" in df.columns:
                base = base[base["seasonIndex"] == dm.CURRENT_SEASON]
            player_df = base.sort_values("weekIndex") if "weekIndex" in df.columns else base

        if player_df.empty:
            continue

        active_cols = [c for c in stat_cols if c in player_df.columns and player_df[c].sum() > 0]
        if not active_cols:
            continue

        season_avg   = player_df[active_cols].mean()
        last_n_avg   = player_df.tail(last_n)[active_cols].mean()
        last_n_games = player_df.tail(last_n)[
            (["weekIndex"] if "weekIndex" in player_df.columns else []) + active_cols
        ].to_dict("records")

        deltas = {}
        for col in active_cols:
            sa = season_avg[col]
            la = last_n_avg[col]
            if sa > 0:
                deltas[col] = round(((la - sa) / sa) * 100, 1)

        positive_stats = ["passYds", "passTDs", "rushYds", "rushTDs",
                          "recYds", "recTDs", "recCatches",
                          "defTotalTackles", "defSacks", "defInts", "defForcedFum"]
        negative_stats = ["passInts", "rushFum", "recDrops"]

        trend_score = 0
        for col, delta in deltas.items():
            if col in positive_stats:
                trend_score += delta
            elif col in negative_stats:
                trend_score -= delta

        if trend_score > 15:
            trend = "🔥 HOT"
        elif trend_score < -15:
            trend = "🥶 COLD"
        else:
            trend = "➡️ NEUTRAL"

        team = player_df.iloc[-1].get("teamName", "")
        pos  = player_df.iloc[-1].get("pos", "")
        name_display = player_df.iloc[-1].get("extendedName") or player_name

        return {
            "type":         "hot_cold",
            "name":         name_display,
            "team":         team,
            "pos":          pos,
            "trend":        trend,
            "trend_score":  round(trend_score, 1),
            "season_avg":   season_avg.round(1).to_dict(),
            "last_n_avg":   last_n_avg.round(1).to_dict(),
            "deltas":       deltas,
            "last_n_games": last_n_games,
            "last_n":       last_n,
            "group":        group,
        }

    return {"type": "hot_cold", "error": f"No per-game data found for {player_name}"}


# ── Clutch stats ──────────────────────────────────────────────────────────────

def get_clutch_records(margin: int = 7) -> dict:
    """
    Team records in close games (final margin ≤ N points).
    Uses df_all_games (status==3 final games) for accuracy.
    Falls back to df_games if needed.
    """
    src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games
    if src.empty:
        return {"type": "clutch", "error": "No game data available."}

    games = src.copy()

    def _resolve_col(df, *candidates):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    home_score_col = _resolve_col(games, "homeScore", "homeTeamScore")
    away_score_col = _resolve_col(games, "awayScore", "awayTeamScore")
    home_name_col  = _resolve_col(games, "homeTeamName", "home")
    away_name_col  = _resolve_col(games, "awayTeamName", "away")
    status_col     = _resolve_col(games, "status")

    if not all([home_score_col, away_score_col, home_name_col, away_name_col]):
        return {"type": "clutch", "error": "Required game columns not found in game data."}

    games[home_score_col] = pd.to_numeric(games[home_score_col], errors="coerce").fillna(0)
    games[away_score_col] = pd.to_numeric(games[away_score_col], errors="coerce").fillna(0)

    # Final games only — status==3 if available, otherwise score>0 fallback
    if status_col:
        games[status_col] = pd.to_numeric(games[status_col], errors="coerce").fillna(0).astype(int)
        played = games[games[status_col] == 3].copy()
    else:
        played = games[
            (games[home_score_col] > 0) | (games[away_score_col] > 0)
        ].copy()

    if played.empty:
        return {"type": "clutch", "margin": margin, "records": [],
                "most_clutch": "?", "least_clutch": "?"}

    played["margin"] = (played[home_score_col] - played[away_score_col]).abs()
    close = played[played["margin"] <= margin]

    all_teams = played[home_name_col].dropna().unique()
    rows = []

    for team in all_teams:
        hw = ((played[home_name_col] == team) & (played[home_score_col] > played[away_score_col])).sum()
        aw = ((played[away_name_col] == team) & (played[away_score_col] > played[home_score_col])).sum()
        hl = ((played[home_name_col] == team) & (played[home_score_col] < played[away_score_col])).sum()
        al = ((played[away_name_col] == team) & (played[away_score_col] < played[home_score_col])).sum()

        cw = (
            ((close[home_name_col] == team) & (close[home_score_col] > close[away_score_col])) |
            ((close[away_name_col] == team) & (close[away_score_col] > close[home_score_col]))
        ).sum()
        cl = (
            ((close[home_name_col] == team) & (close[home_score_col] < close[away_score_col])) |
            ((close[away_name_col] == team) & (close[away_score_col] < close[home_score_col]))
        ).sum()

        rows.append({
            "team":           team,
            "overall_wins":   int(hw + aw),
            "overall_losses": int(hl + al),
            "clutch_wins":    int(cw),
            "clutch_losses":  int(cl),
            "clutch_games":   int(cw + cl),
            "clutch_winpct":  round(cw / (cw + cl) if (cw + cl) > 0 else 0, 3),
        })

    df = pd.DataFrame(rows).sort_values("clutch_wins", ascending=False)
    return {
        "type":         "clutch",
        "margin":       margin,
        "records":      df.to_dict("records"),
        "most_clutch":  df.iloc[0]["team"] if not df.empty else "?",
        "least_clutch": df.sort_values("clutch_winpct").iloc[0]["team"] if not df.empty else "?",
    }


# ── Owner profiles & memory ───────────────────────────────────────────────────

_owner_profiles: dict[int, dict] = {}
_username_to_team: dict[str, str] = {}
_team_to_username: dict[str, str] = {}


def get_nickname(discord_user_id: int) -> str | None:
    return KNOWN_MEMBERS.get(discord_user_id)


def get_ids_for_nickname(nickname: str) -> list[int]:
    return _nickname_to_ids.get(nickname.lower(), [])


def build_owner_map():
    """Build username ↔ team lookup from df_teams + roster identity cache."""
    global _username_to_team, _team_to_username

    # Load identity caches from tsl_members DB + roster
    _load_identity_cache()

    if dm.df_teams is None or dm.df_teams.empty:
        return
    for _, row in dm.df_teams.iterrows():
        uname = str(row.get("userName", "")).strip()
        team  = str(row.get("nickName", "")).strip()
        if uname and team:
            _username_to_team[uname.lower()] = team
            _team_to_username[team.lower()]  = uname

    # Cross-reference nicknames with API usernames for fuzzy lookup
    for discord_id, nickname in KNOWN_MEMBERS.items():
        nick_lower = nickname.lower()
        if nick_lower in _username_to_team:
            continue
        for uname, team in list(_username_to_team.items()):
            if nick_lower in uname:
                _username_to_team[nick_lower] = team
                break


def get_owner_team(discord_username: str) -> str | None:
    return _username_to_team.get(discord_username.lower())


def get_team_owner_username(team_name: str) -> str | None:
    return _team_to_username.get(team_name.lower())


def get_or_create_profile(discord_user_id: int, discord_username: str) -> dict:
    if discord_user_id not in _owner_profiles:
        nickname = get_nickname(discord_user_id) or discord_username

        # Team lookup priority: roster → KNOWN_MEMBER_TEAMS → username fuzzy
        team = None
        try:
            import roster
            team = roster.get_team_name(discord_user_id)
        except Exception:
            pass
        if not team:
            team = (
                KNOWN_MEMBER_TEAMS.get(discord_user_id) or
                get_owner_team(nickname) or
                get_owner_team(discord_username)
            )

        _owner_profiles[discord_user_id] = {
            "discord_id":       discord_user_id,
            "discord_username": discord_username,
            "nickname":         nickname,
            "team":             team,
            "roast_count":      0,
            "interactions":     0,
            "beefs":            [],
            "memorable":        [],
        }
    profile = _owner_profiles[discord_user_id]
    profile["interactions"] += 1
    return profile


def record_roast(discord_user_id: int):
    if discord_user_id in _owner_profiles:
        _owner_profiles[discord_user_id]["roast_count"] += 1


def record_beef(user_a_id: int, user_b_id: int):
    for uid, oid in [(user_a_id, user_b_id), (user_b_id, user_a_id)]:
        if uid in _owner_profiles:
            profile = _owner_profiles[uid]
            existing = next((b for b in profile["beefs"] if b["opponent_id"] == oid), None)
            if existing:
                existing["count"] += 1
            else:
                profile["beefs"].append({"opponent_id": oid, "count": 1})


def get_owner_context(discord_user_id: int, discord_username: str) -> str:
    profile  = get_or_create_profile(discord_user_id, discord_username)
    nickname = profile.get("nickname", discord_username)
    team     = profile.get("team")

    lines = ["[OWNER CONTEXT]"]
    lines.append(
        f"TSL nickname: {nickname} | Discord: {discord_username} "
        f"(interactions: {profile['interactions']}, roasts received: {profile['roast_count']})"
    )
    lines.append(f"Always refer to this person as: {nickname}")

    if team:
        lines.append(f"Their team: {team}")
        record = dm.get_team_record(team)
        lines.append(f"Current record: {record}")
        if not dm.df_standings.empty:
            row = dm.df_standings[dm.df_standings["teamName"].str.lower() == team.lower()]
            if not row.empty:
                r = row.iloc[0]
                lines.append(
                    f"Rank #{int(r.get('rank', 0))} | Net Pts: {r.get('netPts')} | TO Diff: {r.get('tODiff')}"
                )
        recent = dm.get_last_n_games(team, 3)
        if recent:
            def _wl(g):
                hs, aws = g.get("home_score", 0), g.get("away_score", 0)
                is_home = g.get("home", "").lower() == team.lower()
                team_score = hs if is_home else aws
                opp_score = aws if is_home else hs
                return "W" if team_score > opp_score else ("L" if team_score < opp_score else "T")
            form = " ".join(_wl(g) for g in recent)
            lines.append(f"Last 3 games: {form}")
    else:
        lines.append("Team: NOT IN LEAGUE (spectator or unknown)")

    if profile["beefs"]:
        beef_count = sum(b["count"] for b in profile["beefs"])
        lines.append(f"Active beefs in chat: {beef_count}")

    return "\n".join(lines)


# ── Beef mode ─────────────────────────────────────────────────────────────────

def detect_beef(
    current_user_id: int,
    current_username: str,
    message_content: str,
    active_users_in_channel: list[dict],
) -> dict | None:
    content_lower = message_content.lower()
    current_team  = get_owner_team(current_username)

    for user in active_users_in_channel:
        uid   = user.get("id")
        uname = user.get("username", "")
        if uid == current_user_id:
            continue

        opponent_team = get_owner_team(uname)
        if not opponent_team:
            continue

        if uname.lower() in content_lower or (
            opponent_team and opponent_team.lower() in content_lower
        ):
            record_beef(current_user_id, uid)
            h2h = dm.get_h2h_record(current_team, opponent_team) if current_team else {}

            return {
                "type":            "beef",
                "challenger":      current_username,
                "challenger_team": current_team,
                "opponent":        uname,
                "opponent_team":   opponent_team,
                "h2h":             h2h,
            }

    return None


def build_beef_context(beef: dict) -> str:
    a_team = beef.get("challenger_team", "Unknown")
    b_team = beef.get("opponent_team",   "Unknown")
    h2h    = beef.get("h2h", {})

    lines = [
        "[BEEF MODE ACTIVATED]",
        f"{beef['challenger']} ({a_team}) is coming at {beef['opponent']} ({b_team})",
        f"Season H2H: {a_team} {h2h.get('a_wins',0)} — {h2h.get('b_wins',0)} {b_team}",
    ]

    for team in [a_team, b_team]:
        if not dm.df_standings.empty:
            row = dm.df_standings[dm.df_standings["teamName"].str.lower() == team.lower()]
            if not row.empty:
                r = row.iloc[0]
                lines.append(
                    f"{team}: {int(r.get('totalWins',0))}-{int(r.get('totalLosses',0))} | "
                    f"Rank #{int(r.get('rank',0))} | Net Pts: {r.get('netPts')} | TO Diff: {r.get('tODiff')}"
                )

    return "\n".join(lines)


# ── Leaderboard channel auto-updater ─────────────────────────────────────────

def build_leaderboard_data() -> dict:
    from analysis import power_rankings, stat_leaders

    pr = power_rankings()

    leaders = {
        "Pass Yds":   stat_leaders(dm.df_offense, "passYds",         min_col="passAtt",    min_val=50,  top_n=3),
        "Rush Yds":   stat_leaders(dm.df_offense, "rushYds",         min_col="rushAtt",    min_val=20,  top_n=3),
        "Rec Yds":    stat_leaders(dm.df_offense, "recYds",          min_col="recCatches", min_val=10,  top_n=3),
        "Sacks":      stat_leaders(dm.df_defense, "defSacks",        top_n=3),
        "INTs":       stat_leaders(dm.df_defense, "defInts",         top_n=3),
        "Tackles":    stat_leaders(dm.df_defense, "defTotalTackles", min_col="defTotalTackles", min_val=5, top_n=3),
    }

    return {
        "type":           "leaderboard",
        "power_rankings": pr[:10],
        "stat_leaders":   leaders,
        "season":         dm.CURRENT_SEASON,
        "status":         dm.get_league_status(),
    }


# ── Reaction pagination helper ────────────────────────────────────────────────

class PaginatedResult:
    def __init__(self, pages: list, title: str = ""):
        self.pages   = pages
        self.title   = title
        self.current = 0
        self.total   = len(pages)

    def current_page(self):
        return self.pages[self.current] if self.pages else None

    def next(self):
        if self.current < self.total - 1:
            self.current += 1
        return self.current_page()

    def prev(self):
        if self.current > 0:
            self.current -= 1
        return self.current_page()

    def page_label(self):
        return f"Page {self.current + 1} / {self.total}"


_paginated_messages: dict[int, PaginatedResult] = {}


def register_pagination(message_id: int, pages: list, title: str = "") -> PaginatedResult:
    pr = PaginatedResult(pages, title)
    _paginated_messages[message_id] = pr
    return pr


def get_pagination(message_id: int) -> PaginatedResult | None:
    return _paginated_messages.get(message_id)


def cleanup_pagination(message_id: int):
    _paginated_messages.pop(message_id, None)
