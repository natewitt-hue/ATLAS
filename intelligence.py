"""
intelligence.py

Advanced intelligence modules for WittGPT:
  - Draft class grading (seasons 2-5, real TSL drafts)
  - Hot/Cold tracker (last 3 games vs season avg)
  - Clutch stats (performance in close games ≤7pts)
  - Owner profiles (Discord user → team mapping + memory)
  - Beef mode (two owners in the same conversation)
"""

import json
import os
import pandas as pd
import numpy as np
import data_manager as dm

# ── Constants ─────────────────────────────────────────────────────────────────

# rookieYear in players.json maps to TSL season index
# Season 1 (2025) is the initial roster build — not a real draft
# Real TSL draft classes start season 2 (2026)
YEAR_TO_SEASON = {2025: 1, 2026: 2, 2027: 3, 2028: 4, 2029: 5}
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

# ── Draft class analysis ──────────────────────────────────────────────────────

def _load_full_players() -> pd.DataFrame:
    """Load players.json with draft columns not in dm.df_players."""
    path = os.path.join(dm.DATA_DIR, "players.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    df = pd.DataFrame(raw)
    df["fullName"] = df["firstName"] + " " + df["lastName"]
    for col in ["draftRound", "draftPick", "rookieYear", "playerBestOvr", "yearsPro"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def _letter_grade(score: float) -> str:
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def get_draft_class(season: int) -> dict:
    """
    Full draft class breakdown for a given TSL season (2-5).
    Returns structured dict with picks, grades, steals, busts.
    """
    if season < 2 or season > dm.CURRENT_SEASON:
        return {"error": f"No real draft data for season {season}. TSL drafts started season 2."}

    df = _load_full_players()
    target_year = SEASON_TO_YEAR.get(season)
    if not target_year:
        return {"error": f"Unknown season {season}"}

    cls = df[df["rookieYear"] == target_year].copy()
    if cls.empty:
        return {"error": f"No players found for season {season} draft."}

    cls["devScore"]    = cls["dev"].map(DEV_SCORE).fillna(1)
    cls["roundLabel"]  = cls["draftRound"].map(ROUND_LABELS).fillna("UDFA")
    cls["pickValueRaw"]= cls["draftRound"] * 32 + cls["draftPick"]

    # Class-level grade = weighted avg of devScore (60%) + OVR normalized (40%)
    ovr_norm = (cls["playerBestOvr"] - 60).clip(0) / 40  # 60 = baseline, 99 = max
    cls["gradeScore"] = cls["devScore"] * 0.6 + ovr_norm * 0.4

    class_grade_score = cls["gradeScore"].mean()
    letter = _letter_grade(class_grade_score)

    # Best picks (steals) = high devScore relative to pick position (later rounds)
    cls["stealScore"] = cls["devScore"] / (cls["draftRound"].clip(2, 8) / 2)
    steals = cls.nlargest(5, "stealScore")[
        ["firstName", "lastName", "teamName", "pos", "roundLabel", "draftPick",
         "playerBestOvr", "dev"]
    ].to_dict("records")

    # Busts = R1/R2 picks with Normal dev and low OVR
    early = cls[cls["draftRound"].isin([2, 3])].copy()
    busts_df = early[
        (early["dev"] == "Normal") | (early["playerBestOvr"] < 75)
    ].sort_values("playerBestOvr")
    busts = busts_df[
        ["firstName", "lastName", "teamName", "pos", "roundLabel", "draftPick",
         "playerBestOvr", "dev"]
    ].head(5).to_dict("records")

    # Top picks overall
    top_picks = cls.nlargest(8, "playerBestOvr")[
        ["firstName", "lastName", "teamName", "pos", "roundLabel", "draftPick",
         "playerBestOvr", "dev"]
    ].to_dict("records")

    # Per-team grades
    team_grades = (
        cls.groupby("teamName")
        .agg(
            picks=("firstName", "count"),
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

    # Dev breakdown
    dev_counts = cls["dev"].value_counts().to_dict()

    return {
        "type":        "draft_class",
        "season":      season,
        "year":        target_year,
        "total_picks": len(cls),
        "letter_grade":letter,
        "grade_score": round(class_grade_score, 2),
        "dev_counts":  dev_counts,
        "avg_ovr":     round(cls["playerBestOvr"].mean(), 1),
        "top_picks":   top_picks,
        "steals":      steals,
        "busts":       busts,
        "team_grades": team_grades.head(10).to_dict("records"),
    }


def compare_draft_classes() -> dict:
    """Compare all TSL draft classes (seasons 2-5) side by side."""
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
    path = os.path.join(dm.DATA_DIR, "offensive.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    df = pd.DataFrame(raw)
    NUM = ["passAtt", "passYds", "passTDs", "passInts", "passSacks",
           "rushAtt", "rushYds", "rushTDs", "rushFum",
           "recCatches", "recYds", "recTDs", "recDrops",
           "seasonIndex", "stageIndex", "weekIndex"]
    for c in NUM:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _load_raw_defense() -> pd.DataFrame:
    path = os.path.join(dm.DATA_DIR, "defensive.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    df = pd.DataFrame(raw)
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
    # Try offense first, then defense
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
            continue

        player_df = df[
            (df["fullName"] == player_name) &
            (df["seasonIndex"] == dm.CURRENT_SEASON) &
            (df["stageIndex"] == dm.REGULAR_STAGE)
        ].sort_values("weekIndex")

        if player_df.empty:
            # Try last name match
            last = player_name.split(".")[-1] if "." in player_name else player_name.split()[-1]
            player_df = df[
                (df["fullName"].str.contains(last, case=False, na=False)) &
                (df["seasonIndex"] == dm.CURRENT_SEASON) &
                (df["stageIndex"] == dm.REGULAR_STAGE)
            ].sort_values("weekIndex")

        if player_df.empty:
            continue

        # Only keep stat cols that are non-zero for this player
        active_cols = [c for c in stat_cols if c in player_df.columns and player_df[c].sum() > 0]
        if not active_cols:
            continue

        season_avg  = player_df[active_cols].mean()
        last_n_avg  = player_df.tail(last_n)[active_cols].mean()
        last_n_games = player_df.tail(last_n)[["weekIndex"] + active_cols].to_dict("records")

        # Determine overall trend
        deltas = {}
        for col in active_cols:
            sa = season_avg[col]
            la = last_n_avg[col]
            if sa > 0:
                delta_pct = ((la - sa) / sa) * 100
                deltas[col] = round(delta_pct, 1)

        # Positive stats where higher = better
        positive_stats = ["passYds", "passTDs", "rushYds", "rushTDs",
                          "recYds", "recTDs", "recCatches",
                          "defTotalTackles", "defSacks", "defInts", "defForcedFum"]
        # Negative stats where lower = better
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

        # Find player's team
        team = player_df.iloc[-1].get("teamName", "")
        pos  = player_df.iloc[-1].get("pos", "")
        name_display = player_df.iloc[-1].get("extendedName") or player_name

        return {
            "type":        "hot_cold",
            "name":        name_display,
            "team":        team,
            "pos":         pos,
            "trend":       trend,
            "trend_score": round(trend_score, 1),
            "season_avg":  season_avg.round(1).to_dict(),
            "last_n_avg":  last_n_avg.round(1).to_dict(),
            "deltas":      deltas,
            "last_n_games":last_n_games,
            "last_n":      last_n,
            "group":       group,
        }

    return {"type": "hot_cold", "error": f"No per-game data found for {player_name}"}


# ── Clutch stats ──────────────────────────────────────────────────────────────

def get_clutch_records(margin: int = 7) -> dict:
    """Team records in close games (margin ≤ N points)."""
    games = dm.df_games.copy()
    cur = games[
        (games["seasonIndex"] == dm.CURRENT_SEASON) &
        (games["stageIndex"] == dm.REGULAR_STAGE) &
        (games["status"] == 3)
    ].copy()

    cur["margin"] = abs(cur["homeScore"] - cur["awayScore"])
    close = cur[cur["margin"] <= margin]
    all_teams = cur["homeTeamName"].unique()

    rows = []
    for team in all_teams:
        # All season record
        hw = ((cur["homeTeamName"] == team) & (cur["homeScore"] > cur["awayScore"])).sum()
        aw = ((cur["awayTeamName"] == team) & (cur["awayScore"] > cur["homeScore"])).sum()
        hl = ((cur["homeTeamName"] == team) & (cur["homeScore"] < cur["awayScore"])).sum()
        al = ((cur["awayTeamName"] == team) & (cur["awayScore"] < cur["homeScore"])).sum()

        # Clutch record
        cw = (
            ((close["homeTeamName"] == team) & (close["homeScore"] > close["awayScore"])) |
            ((close["awayTeamName"] == team) & (close["awayScore"] > close["homeScore"]))
        ).sum()
        cl = (
            ((close["homeTeamName"] == team) & (close["homeScore"] < close["awayScore"])) |
            ((close["awayTeamName"] == team) & (close["awayScore"] < close["homeScore"]))
        ).sum()

        rows.append({
            "team":          team,
            "overall_wins":  int(hw + aw),
            "overall_losses":int(hl + al),
            "clutch_wins":   int(cw),
            "clutch_losses": int(cl),
            "clutch_games":  int(cw + cl),
            "clutch_winpct": round(cw / (cw + cl) if (cw + cl) > 0 else 0, 3),
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

# In-memory owner profile store
# Structure: { discord_user_id: OwnerProfile }
_owner_profiles: dict[int, dict] = {}

# Username → team mapping (built from teams.json)
_username_to_team: dict[str, str] = {}
_team_to_username: dict[str, str] = {}

# ── Known Discord ID → nickname table ────────────────────────────────────────
# Maps Discord user ID → preferred nickname WittGPT uses for them.
# A second entry for the same person (e.g. Jo has two IDs) uses the same nickname.
KNOWN_MEMBERS: dict[int, str] = {
    1253510201626329208: "Jo",
    456226577798135808:  "Jo",        # Jo's second account
    478233196408995850:  "RonDon",
    606222129779965972:  "Will",
    138759200812695554:  "Chok",
    374225201501700097:  "Don",
    413478492563570701:  "Jadon",
    406316042076422155:  "Shaun",
    972717991886213140:  "AP",
    705567998710382722:  "Diddy",
    590340736705363978:  "Benny",
    287696480888684545:  "Bodak",
    966861614546559086:  "Bryan",
    402604212732821504:  "Cfar",
    520354406001016833:  "Clutch",
    762900687536390154:  "GIO",
    937303476034228245:  "Jmoney",
    808838150083706920:  "Johnson",
    871448457414598737:  "JT",
    308657934815068161:  "Ken",
    346817461527642112:  "KG",
    217340612452679682:  "Khaled",
    600087875970924557:  "Jorge",
    209416082786746368:  "Killa",
    966021499322515466:  "Nastymofo",
    710648515566633052:  "Neff",
    694316056206114827:  "Newman",
    968230853920559114:  "Pnick",
    432242024163442688:  "Rissa",
    934556990045310996:  "Shelly",
    634221098250010634:  "Shottaz",
    208978020210442240:  "Signman",
    1012890489114083329: "Troy",
    809583145908305940:  "Zee",
    801157966056259604:  "Noodle",
}

# Explicit Discord ID → team overrides
# Used when nickname doesn't substring-match the teams.json userName
KNOWN_MEMBER_TEAMS: dict[int, str] = {
    972717991886213140:  "Saints",      # AP        = AgrarianPeasant
    705567998710382722:  "Pack",        # Diddy     = BDiddy86
    374225201501700097:  "Broncos",     # Don       = D-TownDon
    413478492563570701:  "Jets",        # Jadon     = Jnolte
    762900687536390154:  "Bolts",       # GIO       = Gi0D0g88
    937303476034228245:  "Cowboys",     # Jmoney    = Find_the_Door
    808838150083706920:  "Niners",      # Johnson   = Drakee_GG
    346817461527642112:  "Vikes",       # KG        = B3AST_M0DE_NC
    217340612452679682:  "Chiefs",      # Khaled    = DrewBreesus2192
    600087875970924557:  "Phins",       # Jorge     = MizzGMB
    209416082786746368:  "Jags",        # Killa     = MeLLoW_FiRe
    966021499322515466:  "Seahawks",    # Nastymofo = CoolSkillsBroo
    694316056206114827:  "Texans",      # Newman    = TheNotoriousLTH
    432242024163442688:  "Panthers",    # Rissa     = YoungSeeThrough
    634221098250010634:  "Steelers",    # Shottaz   = TheGasGOD_423
    208978020210442240:  "Bucs",        # Signman   = kickerbog10
    1012890489114083329: "Bears",       # Troy      = KingCaleb_18
    809583145908305940:  "Falcons",     # Zee       = LIXODYSSEY
    287696480888684545:  "Browns",      # Bodak     = BramptonWasteMan
    966861614546559086:  "Bills",       # Bryan     = JB3v3
    402604212732821504:  "Rams",        # Cfar      = cfar89
    520354406001016833:  "Cardinals",   # Clutch    = Mr_Clutch723
    590340736705363978:  "Colts",       # Benny     = BennyGalactic
    478233196408995850:  "Commanders",  # RonDon    = Ronfk
    606222129779965972:  "Eagles",      # Will      = Will_Chamberlain
    406316042076422155:  "Ravens",      # Shaun     = SuaveShaunTTV
    710648515566633052:  "Pats",        # Neff      = Saucy0134
    871448457414598737:  "Bengals",     # JT        = current Bengals owner
    934556990045310996:  "Raiders",     # Shelly    = current Raiders owner
    # Jo (both IDs) = former owner, no team
    # Noodle = no team assigned
}


# Build reverse mapping: nickname (lowercase) → list of Discord IDs
_nickname_to_ids: dict[str, list[int]] = {}
for _uid, _nick in KNOWN_MEMBERS.items():
    _nickname_to_ids.setdefault(_nick.lower(), []).append(_uid)


def get_nickname(discord_user_id: int) -> str | None:
    """Return the TSL nickname for a Discord user ID, or None if unknown."""
    return KNOWN_MEMBERS.get(discord_user_id)


def get_ids_for_nickname(nickname: str) -> list[int]:
    """Return all known Discord IDs for a given nickname."""
    return _nickname_to_ids.get(nickname.lower(), [])


def build_owner_map():
    """Build username ↔ team lookup from df_teams, cross-referenced with KNOWN_MEMBERS."""
    global _username_to_team, _team_to_username
    if dm.df_teams.empty:
        return
    for _, row in dm.df_teams.iterrows():
        uname = row.get("userName", "").strip()
        team  = row.get("nickName", "")
        if uname and team:
            _username_to_team[uname.lower()] = team
            _team_to_username[team.lower()]  = uname

    # Also map TSL nicknames → team where the nickname matches or partially matches a userName
    for discord_id, nickname in KNOWN_MEMBERS.items():
        nick_lower = nickname.lower()
        # Direct match
        if nick_lower in _username_to_team:
            continue
        # Partial match (e.g. "Shaun" matches "SuaveShaunTTV")
        for uname, team in _username_to_team.items():
            if nick_lower in uname:
                _username_to_team[nick_lower] = team
                break


def get_owner_team(discord_username: str) -> str | None:
    """Look up which TSL team a Discord username owns."""
    return _username_to_team.get(discord_username.lower())


def get_team_owner_username(team_name: str) -> str | None:
    return _team_to_username.get(team_name.lower())


def get_or_create_profile(discord_user_id: int, discord_username: str) -> dict:
    """
    Get or create a persistent owner profile.
    Resolves TSL nickname from KNOWN_MEMBERS, team from KNOWN_MEMBER_TEAMS.
    """
    if discord_user_id not in _owner_profiles:
        nickname = get_nickname(discord_user_id) or discord_username
        # Team lookup: explicit override first, then username map, then nickname partial match
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
    """Track that two users are beefing."""
    for uid, oid in [(user_a_id, user_b_id), (user_b_id, user_a_id)]:
        if uid in _owner_profiles:
            profile = _owner_profiles[uid]
            existing = next((b for b in profile["beefs"] if b["opponent_id"] == oid), None)
            if existing:
                existing["count"] += 1
            else:
                profile["beefs"].append({"opponent_id": oid, "count": 1})


def get_owner_context(discord_user_id: int, discord_username: str) -> str:
    """
    Build a context string about the owner for WittGPT.
    Uses TSL nickname from KNOWN_MEMBERS when available.
    """
    profile = get_or_create_profile(discord_user_id, discord_username)
    nickname = profile.get("nickname", discord_username)
    team = profile.get("team")

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
            form = " ".join("W" if g["win"] else "L" for g in recent)
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
    active_users_in_channel: list[dict],  # list of {id, username}
) -> dict | None:
    """
    Detects if a beef situation exists:
    - Another league owner is @mentioned in the message, OR
    - The message content targets another owner's team by name

    Returns beef context dict if detected, None otherwise.
    """
    content_lower = message_content.lower()
    current_team = get_owner_team(current_username)

    for user in active_users_in_channel:
        uid = user.get("id")
        uname = user.get("username", "")
        if uid == current_user_id:
            continue

        opponent_team = get_owner_team(uname)
        if not opponent_team:
            continue

        # Check if opponent is @mentioned or their team is mentioned
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
    """Build context string for WittGPT in beef mode."""
    a_team = beef.get("challenger_team", "Unknown")
    b_team = beef.get("opponent_team",   "Unknown")
    h2h    = beef.get("h2h", {})

    lines = [
        f"[BEEF MODE ACTIVATED]",
        f"{beef['challenger']} ({a_team}) is coming at {beef['opponent']} ({b_team})",
        f"All-time H2H: {a_team} {h2h.get('a_wins',0)} — {h2h.get('b_wins',0)} {b_team}",
    ]

    # Add quick stats for both teams
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
    """
    Builds full leaderboard snapshot for auto-posting.
    Combines power rankings + stat leaders + hot/cold flags.
    """
    from analysis import power_rankings, stat_leaders

    pr = power_rankings()

    # Stat leaders
    leaders = {
        "Pass Yds":   stat_leaders(dm.df_offense, "passYds",        min_col="passAtt",    min_val=50,  top_n=3),
        "Rush Yds":   stat_leaders(dm.df_offense, "rushYds",        min_col="rushAtt",    min_val=20,  top_n=3),
        "Rec Yds":    stat_leaders(dm.df_offense, "recYds",         min_col="recCatches", min_val=10,  top_n=3),
        "Sacks":      stat_leaders(dm.df_defense, "defSacks",       top_n=3),
        "INTs":       stat_leaders(dm.df_defense, "defInts",        top_n=3),
        "Tackles":    stat_leaders(dm.df_defense, "defTotalTackles",min_col="defTotalTackles", min_val=5, top_n=3),
    }

    return {
        "type":          "leaderboard",
        "power_rankings": pr[:10],
        "stat_leaders":  leaders,
        "season":        dm.CURRENT_SEASON,
        "status":        dm.get_league_status(),
    }


# ── Reaction pagination helper ────────────────────────────────────────────────

class PaginatedResult:
    """
    Holds a multi-page result set for reaction-based pagination.
    Pages are pre-built embed dicts. Bot stores these by message_id.
    """
    def __init__(self, pages: list, title: str = ""):
        self.pages     = pages
        self.title     = title
        self.current   = 0
        self.total     = len(pages)

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


# Registry of active paginated messages: { discord_message_id: PaginatedResult }
_paginated_messages: dict[int, PaginatedResult] = {}


def register_pagination(message_id: int, pages: list, title: str = "") -> PaginatedResult:
    pr = PaginatedResult(pages, title)
    _paginated_messages[message_id] = pr
    return pr


def get_pagination(message_id: int) -> PaginatedResult | None:
    return _paginated_messages.get(message_id)


def cleanup_pagination(message_id: int):
    _paginated_messages.pop(message_id, None)
