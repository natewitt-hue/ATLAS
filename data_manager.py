"""
data_manager.py
Loads every TSL JSON file once at startup, aggregates per-game rows into
season totals, and exposes clean DataFrames + lookup helpers to the rest
of the bot.
"""

import json
import os
import pandas as pd

# ── Constants ───────────────────────────────────────────────────────────────

DATA_DIR = "league_data"

# Auto-detected at load time from info.json
CURRENT_SEASON: int = 5
REGULAR_STAGE:  int = 1   # stageIndex=1 is regular season


# ── Low-level helpers ────────────────────────────────────────────────────────

def _load(filename: str):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        print(f"[DataManager] WARNING: {filename} not found.")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[DataManager] ERROR loading {filename}: {e}")
        return []


def _to_numeric(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def _current_regular(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to current season, regular season stage."""
    if df.empty:
        return df
    return df[
        (df["seasonIndex"] == CURRENT_SEASON) &
        (df["stageIndex"]  == REGULAR_STAGE)
    ].copy()


# ── Offense ──────────────────────────────────────────────────────────────────

OFF_NUM = [
    "passAtt", "passComp", "passTDs", "passInts", "passYds", "passSacks",
    "rushAtt", "rushYds", "rushTDs", "rushFum", "rushBrokenTackles",
    "rushYdsAfterContact", "rush20PlusYds",
    "recCatches", "recDrops", "recYds", "recTDs", "recYdsAfterCatch",
    "offPts",
]
OFF_GROUP = ["fullName", "extendedName", "teamName", "pos"]


def _build_offense() -> pd.DataFrame:
    raw = pd.DataFrame(_load("offensive.json"))
    if raw.empty:
        return raw
    raw = _to_numeric(raw, OFF_NUM + ["seasonIndex", "stageIndex"])
    agg = _current_regular(raw).groupby(OFF_GROUP, as_index=False)[OFF_NUM].sum()
    # Derived rates (safe divide)
    def safe_div(a, b): return (a / b.replace(0, float("nan"))).round(1)
    agg["passCompPct"]   = safe_div(agg["passComp"],  agg["passAtt"])
    agg["passYdsPerAtt"] = safe_div(agg["passYds"],   agg["passAtt"])
    agg["rushYdsPerAtt"] = safe_div(agg["rushYds"],   agg["rushAtt"])
    agg["recYdsPerCatch"]= safe_div(agg["recYds"],    agg["recCatches"])
    return agg


# ── Defense ──────────────────────────────────────────────────────────────────

DEF_NUM = [
    "defTotalTackles", "defSacks", "defSafeties", "defInts", "defIntReturnYds",
    "defForcedFum", "defFumRec", "defTDs", "defCatchAllowed", "defDeflections",
    "defPts",
]
DEF_GROUP = ["fullName", "extendedName", "teamName", "pos"]


def _build_defense() -> pd.DataFrame:
    raw = pd.DataFrame(_load("defensive.json"))
    if raw.empty:
        return raw
    raw = _to_numeric(raw, DEF_NUM + ["seasonIndex", "stageIndex"])
    return _current_regular(raw).groupby(DEF_GROUP, as_index=False)[DEF_NUM].sum()


# ── Team Stats ───────────────────────────────────────────────────────────────

TS_NUM = [
    "defForcedFum", "defFumRec", "defIntsRec", "defPassYds", "defRushYds",
    "defRedZoneFGs", "defRedZones", "defRedZoneTDs", "defSacks", "defTotalYds",
    "off4thDownAtt", "off4thDownConv", "offFumLost", "offIntsLost",
    "off1stDowns", "offPassTDs", "offPassYds", "offRushTDs", "offRushYds",
    "offRedZoneFGs", "offRedZones", "offRedZoneTDs", "offSacks", "offTotalYds",
    "penalties", "penaltyYds", "off3rdDownAtt", "off3rdDownConv",
    "off2ptAtt", "off2ptConv", "tODiff", "tOGiveAways", "tOTakeaways",
]


def _build_team_stats() -> pd.DataFrame:
    raw = pd.DataFrame(_load("teamStats.json"))
    if raw.empty:
        return raw
    raw = _to_numeric(raw, TS_NUM + ["seasonIndex", "stageIndex"])
    agg = _current_regular(raw).groupby("teamName", as_index=False)[TS_NUM].sum()
    # Recalculate percentages
    def safe_pct(a, b): return (a / b.replace(0, float("nan")) * 100).round(1)
    agg["off3rdDownConvPct"] = safe_pct(agg["off3rdDownConv"], agg["off3rdDownAtt"])
    agg["off4thDownConvPct"] = safe_pct(agg["off4thDownConv"], agg["off4thDownAtt"])
    agg["offRedZonePct"]     = safe_pct(agg["offRedZoneTDs"],  agg["offRedZones"])
    agg["defRedZonePct"]     = safe_pct(agg["defRedZoneTDs"],  agg["defRedZones"])
    return agg


# ── Standings ────────────────────────────────────────────────────────────────

def _build_standings() -> pd.DataFrame:
    raw = pd.DataFrame(_load("standings.json"))
    if raw.empty:
        return raw
    return raw[raw["seasonIndex"] == CURRENT_SEASON].copy()


# ── Teams (metadata + colors) ────────────────────────────────────────────────

def _build_teams() -> pd.DataFrame:
    raw = pd.DataFrame(_load("teams.json"))
    if raw.empty:
        return raw
    # Convert int colors to hex strings
    if "primaryColor" in raw.columns:
        raw["primaryHex"]   = raw["primaryColor"].apply(lambda c: f"#{int(c):06X}" if pd.notna(c) else "#808080")
    if "secondaryColor" in raw.columns:
        raw["secondaryHex"] = raw["secondaryColor"].apply(lambda c: f"#{int(c):06X}" if pd.notna(c) else "#ffffff")
    return raw


# ── Players (roster info + ratings) ─────────────────────────────────────────

PLAYER_KEEP = [
    # Identity
    "rosterId", "firstName", "lastName", "age", "pos", "jerseyNum",
    "dev", "teamName", "teamId", "isActive", "isFA", "retired",
    "injuryType", "injuryLength", "isOnIR", "isOnPracticeSquad",
    # Contract / cap
    "capHit", "contractYearsLeft", "contractSalary",
    "value", "playerBestOvr", "legacyScore",
    # Physical — required for position change validation (Section 6.4)
    "height", "weight",
    # Speed / athleticism
    "speedRating", "agilityRating", "changeOfDirectionRating",
    "strengthRating", "throwPowerRating", "throwAccRating", "awareRating",
    # Blocking (WR→TE, FB→TE, RB→FB)
    "runBlockRating", "impactBlockRating", "leadBlockRating",
    # Coverage / DB (S→CB, CB→S)
    "manCoverRating", "zoneCoverRating", "pursuitRating",
    # Tackling / defense (S→LB)
    "tackleRating", "hitPowerRating", "blockShedRating",
    # Receiving / routes (TE→WR, HB/TE→WR Slot)
    "catchRating", "routeRunDeepRating", "routeRunShortRating",
    "releaseRating", "breakTackleRating",
    # Ball carrier (WR→HB, RB→FB)
    "carryRating", "bCVRating",
    # Abilities
    "ability1", "ability2", "ability3", "ability4", "ability5", "ability6",
]


def _build_players() -> pd.DataFrame:
    raw = pd.DataFrame(_load("players.json"))
    if raw.empty:
        return raw
    keep = [c for c in PLAYER_KEEP if c in raw.columns]
    df = raw[keep].copy()
    df["fullName"] = df["firstName"] + " " + df["lastName"]
    return df


# ── Games ────────────────────────────────────────────────────────────────────

def _build_games() -> pd.DataFrame:
    raw = pd.DataFrame(_load("games.json"))
    if raw.empty:
        return raw
    raw = _to_numeric(raw, ["homeScore", "awayScore", "seasonIndex", "stageIndex", "weekIndex", "status"])
    return raw


# ── Trades ───────────────────────────────────────────────────────────────────

def _build_trades() -> pd.DataFrame:
    return pd.DataFrame(_load("trades.json"))


# ── Player Abilities ─────────────────────────────────────────────────────────

def _build_abilities() -> pd.DataFrame:
    return pd.DataFrame(_load("playerAbilities.json"))


# ── Public DataFrames (populated by load_all) ────────────────────────────────

df_offense    = pd.DataFrame()
df_defense    = pd.DataFrame()
df_team_stats = pd.DataFrame()
df_standings  = pd.DataFrame()
df_teams      = pd.DataFrame()
df_players    = pd.DataFrame()
df_games      = pd.DataFrame()
df_trades     = pd.DataFrame()
df_abilities  = pd.DataFrame()
league_info   = {}


def load_all():
    """Call once at bot startup."""
    global df_offense, df_defense, df_team_stats, df_standings
    global df_teams, df_players, df_games, df_trades, df_abilities
    global league_info, CURRENT_SEASON

    print("[DataManager] Loading all TSL data...")

    raw_info = _load("info.json")
    if raw_info:
        league_info    = raw_info
        CURRENT_SEASON = raw_info.get("seasonIndex", 5)

    df_offense    = _build_offense()
    df_defense    = _build_defense()
    df_team_stats = _build_team_stats()
    df_standings  = _build_standings()
    df_teams      = _build_teams()
    df_players    = _build_players()
    df_games      = _build_games()
    df_trades     = _build_trades()
    df_abilities  = _build_abilities()

    # Invalidate reasoning schema cache so it rebuilds with fresh data
    try:
        import reasoning
        reasoning.invalidate_schema_cache()
    except Exception:
        pass

    print(
        f"[DataManager] Ready — "
        f"Season {CURRENT_SEASON} | "
        f"Offense: {len(df_offense)} players | "
        f"Defense: {len(df_defense)} players | "
        f"TeamStats: {len(df_team_stats)} teams | "
        f"Players: {len(df_players)} roster | "
        f"Games: {len(df_games)} | "
        f"Trades: {len(df_trades)} | "
        f"Abilities: {len(df_abilities)}"
    )


def get_league_status() -> str:
    return (
        f"Season {league_info.get('seasonIndex','?')} | "
        f"{league_info.get('stageName','?')} — "
        f"{league_info.get('weekName','?')}"
    )


# ── Lookup helpers ────────────────────────────────────────────────────────────

def get_team_color(team_name: str) -> int:
    """Returns Discord-compatible int color for a team's primary color."""
    if df_teams.empty or "nickName" not in df_teams.columns:
        return 0x36393F
    row = df_teams[df_teams["nickName"].str.lower() == team_name.lower()]
    if row.empty:
        row = df_teams[df_teams["displayName"].str.lower().str.contains(team_name.lower(), na=False)]
    if not row.empty and "primaryColor" in row.columns:
        return int(row.iloc[0]["primaryColor"])
    return 0x36393F


def get_team_abbr(team_name: str) -> str:
    if df_teams.empty:
        return team_name[:3].upper()
    row = df_teams[df_teams["nickName"].str.lower() == team_name.lower()]
    if row.empty:
        row = df_teams[df_teams["displayName"].str.lower().str.contains(team_name.lower(), na=False)]
    if not row.empty:
        return row.iloc[0].get("abbrName", team_name[:3].upper())
    return team_name[:3].upper()


def get_team_owner(team_name: str) -> str:
    if df_teams.empty:
        return "Unknown"
    row = df_teams[df_teams["nickName"].str.lower() == team_name.lower()]
    if row.empty:
        row = df_teams[df_teams["displayName"].str.lower().str.contains(team_name.lower(), na=False)]
    if not row.empty:
        return row.iloc[0].get("userName", "Unknown")
    return "Unknown"


def get_player_profile(full_name: str) -> dict | None:
    """Return the players.json row for a player as a dict."""
    if df_players.empty:
        return None
    row = df_players[df_players["fullName"].str.lower() == full_name.lower()]
    if row.empty:
        last = full_name.split()[-1]
        row = df_players[df_players["lastName"].str.lower() == last.lower()]
    if not row.empty:
        return row.iloc[0].to_dict()
    return None


def get_team_record(team_name: str) -> str:
    if df_standings.empty:
        return "?-?-?"
    row = df_standings[df_standings["teamName"].str.lower() == team_name.lower()]
    if not row.empty:
        r = row.iloc[0]
        return f"{int(r.get('totalWins',0))}-{int(r.get('totalLosses',0))}-{int(r.get('totalTies',0))}"
    return "?-?-?"


def get_h2h_record(team_a: str, team_b: str) -> dict:
    """Historical head-to-head record across all seasons."""
    if df_games.empty:
        return {"a_wins": 0, "b_wins": 0, "ties": 0}
    a, b = team_a.lower(), team_b.lower()
    mask = (
        (df_games["homeTeamName"].str.lower() == a) & (df_games["awayTeamName"].str.lower() == b) |
        (df_games["homeTeamName"].str.lower() == b) & (df_games["awayTeamName"].str.lower() == a)
    ) & (df_games["status"] == 3)
    games = df_games[mask]
    a_wins = b_wins = ties = 0
    for _, g in games.iterrows():
        hs, aws = g["homeScore"], g["awayScore"]
        home = g["homeTeamName"].lower()
        if hs == aws:
            ties += 1
        elif (home == a and hs > aws) or (home == b and aws > hs):
            a_wins += 1
        else:
            b_wins += 1
    return {"a_wins": a_wins, "b_wins": b_wins, "ties": ties}


def get_last_n_games(team_name: str, n: int = 5) -> list[dict]:
    """Last N completed games for a team."""
    if df_games.empty:
        return []
    t = team_name.lower()
    mask = (
        (df_games["homeTeamName"].str.lower() == t) |
        (df_games["awayTeamName"].str.lower() == t)
    ) & (df_games["status"] == 3) & (df_games["seasonIndex"] == CURRENT_SEASON)
    recent = df_games[mask].sort_values(["stageIndex","weekIndex"], ascending=False).head(n)
    results = []
    for _, g in recent.iterrows():
        home = g["homeTeamName"]
        away = g["awayTeamName"]
        hs, aws = int(g["homeScore"]), int(g["awayScore"])
        is_home = home.lower() == t
        team_score = hs if is_home else aws
        opp_score  = aws if is_home else hs
        opp        = away if is_home else home
        results.append({
            "opponent": opp,
            "team_score": team_score,
            "opp_score": opp_score,
            "win": team_score > opp_score,
            "week": int(g["weekIndex"]),
        })
    return results


def get_weekly_results(season: int = None, week: int = None) -> list[dict]:
    """Get all completed games for a given season/week. Defaults to last completed week."""
    if df_games.empty:
        return []
    s = season or CURRENT_SEASON
    completed = df_games[(df_games["seasonIndex"] == s) & (df_games["status"] == 3)]
    w = week if week is not None else int(completed["weekIndex"].max())
    games = completed[completed["weekIndex"] == w]
    results = []
    for _, g in games.iterrows():
        results.append({
            "home": g["homeTeamName"],
            "away": g["awayTeamName"],
            "home_score": int(g["homeScore"]),
            "away_score": int(g["awayScore"]),
            "home_user": g.get("homeUser", ""),
            "away_user": g.get("awayUser", ""),
            "week": int(g["weekIndex"]),
        })
    return results


def get_trades(season: int = None, status: str = "accepted") -> list[dict]:
    """Return trades filtered by season and status."""
    if df_trades.empty:
        return []
    mask = df_trades["status"] == status
    if season:
        mask &= df_trades["seasonIndex"] == season
    return df_trades[mask].to_dict("records")


def get_player_abilities(player_name: str) -> list[str]:
    """Return active ability titles for a player."""
    if df_abilities.empty:
        return []
    fn = player_name.lower()
    mask = (
        (df_abilities["firstName"] + " " + df_abilities["lastName"]).str.lower() == fn
    )
    rows = df_abilities[mask & (df_abilities["isEmpty"] == 0)]
    return rows["title"].tolist()


# ═══════════════════════════════════════════════════════════════════════════════
# DISCORD HISTORY — SQLite + FTS5 Database
# ═══════════════════════════════════════════════════════════════════════════════
"""
Discord message archive stored in a local SQLite database with FTS5 full-text
search. This enables exact counting, date lookups, and keyword searches that
a vector DB cannot handle.

Schema:
  messages(id TEXT PK, timestamp TEXT, author TEXT, content TEXT, channel TEXT)
  messages_fts  — FTS5 virtual table (content='messages', tokenize='unicode61')

Run ingestion once via CLI:
  python data_manager.py --ingest-discord /path/to/messages.jsonl

Or call ingest_discord_jsonl() from any script.
"""

import sqlite3
import time
import argparse
from pathlib import Path

DISCORD_DB_PATH = os.environ.get("DISCORD_DB_PATH", "discord_history.db")

# ── Read-only connection factory ──────────────────────────────────────────────

def _get_discord_db(readonly: bool = True) -> sqlite3.Connection:
    """
    Open the Discord history SQLite database.
    readonly=True uses immutable URI mode — safe for bot queries.
    readonly=False opens for writes (ingestion only).
    """
    db_path = Path(DISCORD_DB_PATH)
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(
                f"Discord history DB not found at '{DISCORD_DB_PATH}'. "
                "Run ingestion first: python data_manager.py --ingest-discord messages.jsonl"
            )
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)

    conn.row_factory = sqlite3.Row   # rows behave like dicts
    return conn


def discord_db_exists() -> bool:
    """Returns True if the Discord history DB has been built and has rows."""
    try:
        conn = _get_discord_db(readonly=True)
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


# ── Schema creation ───────────────────────────────────────────────────────────

_CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id        TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    author    TEXT NOT NULL,
    channel   TEXT NOT NULL DEFAULT '',
    content   TEXT NOT NULL
);
"""

# FTS5 virtual table — indexes content for fast MATCH queries
# tokenize='unicode61 remove_diacritics 2' handles accented chars + Unicode
_CREATE_FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
USING fts5(
    content,
    author UNINDEXED,
    timestamp UNINDEXED,
    channel UNINDEXED,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
"""

# Triggers to keep FTS in sync with the main table
_CREATE_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, author, timestamp, channel)
    VALUES (new.rowid, new.content, new.author, new.timestamp, new.channel);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, author, timestamp, channel)
    VALUES('delete', old.rowid, old.content, old.author, old.timestamp, old.channel);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, author, timestamp, channel)
    VALUES('delete', old.rowid, old.content, old.author, old.timestamp, old.channel);
    INSERT INTO messages_fts(rowid, content, author, timestamp, channel)
    VALUES (new.rowid, new.content, new.author, new.timestamp, new.channel);
END;
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_author    ON messages(author);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_channel   ON messages(channel);
"""


def _init_discord_schema(conn: sqlite3.Connection):
    """Create all tables, FTS virtual table, triggers, and indexes."""
    conn.executescript(
        _CREATE_MESSAGES_TABLE +
        _CREATE_FTS_TABLE +
        _CREATE_FTS_TRIGGERS +
        _CREATE_INDEXES
    )
    conn.commit()


# ── DiscordChatExporter JSON parser ──────────────────────────────────────────

import datetime as _dt


def _parse_dce_message(msg: dict, channel_name: str, fallback_id_prefix: str) -> tuple | None:
    """
    Parse a single message object from a DiscordChatExporter JSON file.

    DiscordChatExporter message structure (relevant fields):
      {
        "id":        "1234567890",
        "type":      "Default",           # skip non-Default types (pins, joins, etc.)
        "timestamp": "2023-07-14T21:33:05.123+00:00",
        "content":   "yo that was dogshit",
        "author": {
          "id":       "987654321",
          "name":     "Diddy",            # the display name we care about
          "discriminator": "0",
          "isBot":    false
        },
        "attachments": [...],
        "embeds":      [...],
        "reactions":   [...],
        "mentions":    [...]
      }

    Args:
        msg:               Dict for one message from the "messages" array.
        channel_name:      Channel name extracted from the file's root object.
        fallback_id_prefix: Used when message has no id (rare).

    Returns:
        (id, timestamp, author, channel, content) tuple ready for SQLite INSERT,
        or None if the message should be skipped.
    """
    # ── Skip non-Default message types (pins, joins, boosts, etc.)
    msg_type = msg.get("type", "Default")
    if isinstance(msg_type, str) and msg_type not in ("Default", "Reply", ""):
        return None
    if isinstance(msg_type, int) and msg_type not in (0, 19):
        # 0 = Default, 19 = Reply in Discord's integer type system
        return None

    # ── Message ID
    msg_id = str(msg.get("id") or f"{fallback_id_prefix}_{msg.get('timestamp','')}")
    if not msg_id:
        return None

    # ── Content — combine text content and any sticker names
    content_parts = []
    raw_content = str(msg.get("content") or "").strip()
    if raw_content:
        content_parts.append(raw_content)

    # Include sticker names as searchable text
    for sticker in msg.get("stickers") or []:
        name = sticker.get("name", "")
        if name:
            content_parts.append(f"[sticker: {name}]")

    content = " ".join(content_parts).strip()

    # Skip purely empty messages (attachments-only, reactions-only, etc.)
    # We keep very short content (single emoji, "lol", etc.) — min_length is applied upstream
    if not content:
        return None

    # ── Author
    author_obj = msg.get("author") or {}
    if isinstance(author_obj, dict):
        # Skip bots
        if author_obj.get("isBot") or author_obj.get("is_bot") or author_obj.get("bot"):
            return None
        # Prefer display name ("name") over username for TSL nicknames
        author = (
            author_obj.get("name") or
            author_obj.get("nickname") or
            author_obj.get("username") or
            author_obj.get("global_name") or
            "unknown"
        )
    else:
        author = str(author_obj) or "unknown"

    author = str(author).strip()[:100]
    if not author:
        author = "unknown"

    # ── Timestamp
    # DCE format: "2023-07-14T21:33:05.123+00:00" — strip timezone, keep ISO prefix
    ts_raw = str(msg.get("timestamp") or "")
    if ts_raw:
        # Normalise to "YYYY-MM-DDTHH:MM:SS" (drop sub-seconds and timezone)
        ts = ts_raw[:19]
    else:
        ts = ""

    return (msg_id, ts, author, channel_name, content)


def _collect_json_files(path: Path) -> list[Path]:
    """
    Given a path that is either a single .json file or a directory,
    return a sorted list of all .json files to ingest.

    Sorting is done naturally so "part 1" comes before "part 10".
    """
    import re as _re_nat

    def _natural_key(p: Path) -> list:
        """Natural sort: split on digit runs so 'part 2' < 'part 10'."""
        parts = _re_nat.split(r"(\d+)", p.name)
        return [int(x) if x.isdigit() else x.lower() for x in parts]

    if path.is_file():
        if path.suffix.lower() == ".json":
            return [path]
        raise ValueError(f"Expected a .json file or directory, got: {path}")

    if path.is_dir():
        files = sorted(
            [f for f in path.iterdir() if f.is_file() and f.suffix.lower() == ".json"],
            key=_natural_key,
        )
        if not files:
            raise FileNotFoundError(f"No .json files found in directory: {path}")
        return files

    raise FileNotFoundError(f"Path does not exist: {path}")


def _print_progress(
    file_idx: int,
    total_files: int,
    filename: str,
    stats: dict,
    elapsed: float,
    final: bool = False,
) -> None:
    """
    Print a single-line progress indicator to stdout.

    Example output:
      [  3/200] TSL - general [part 3].json | total: 28,450 read | 27,901 inserted | 412.0 msg/s
    """
    rate = stats["total_read"] / max(elapsed, 0.001)
    tag  = "DONE" if final else f"{file_idx:>3}/{total_files}"
    # Truncate long filenames for terminal readability
    fname_display = filename if len(filename) <= 48 else "…" + filename[-47:]
    print(
        f"[{tag}] {fname_display:<50} | "
        f"total: {stats['total_read']:>9,} read | "
        f"{stats['inserted']:>9,} inserted | "
        f"{rate:>7,.0f} msg/s",
        flush=True,
    )


# ── Main ingestion entry point ────────────────────────────────────────────────

def ingest_discord(
    source: str,
    reset: bool = False,
    batch_size: int = 5_000,
    min_length: int = 1,
) -> dict:
    """
    Ingest Discord message history exported by DiscordChatExporter into
    discord_history.db. Accepts either:

      • A single .json file  (one DCE export)
      • A directory          (scanned for all *.json files, processed in
                              natural sort order — "part 1" before "part 10")

    DiscordChatExporter JSON root structure:
      {
        "guild":    { "id": "...", "name": "TSL" },
        "channel":  { "id": "...", "name": "general", "type": "GuildTextChat" },
        "messages": [ { "id", "type", "timestamp", "content", "author", ... }, ... ]
      }

    The function:
      1. Discovers all target .json files (sorted naturally).
      2. Opens a single persistent SQLite connection for the whole run.
      3. Disables triggers during bulk load for speed; rebuilds FTS at the end.
      4. Prints a per-file progress line and a final summary.

    Args:
        source:     Path to a .json file OR a directory of .json files.
        reset:      Drop and recreate the DB before ingesting (clean slate).
        batch_size: Rows per INSERT transaction. 5,000 is a safe default;
                    increase to 20,000 on machines with ≥16 GB RAM.
        min_length: Minimum character length of content to index. 1 keeps
                    short reactions ("lol", "lmao") while dropping empty strings.

    Returns:
        dict: {
            total_files, files_ok, files_failed,
            total_read, inserted, skipped, errors,
            elapsed_s
        }
    """
    source_path = Path(source)
    json_files  = _collect_json_files(source_path)
    total_files = len(json_files)

    print(f"\n[DiscordDB] Found {total_files} JSON file(s) to ingest from: {source_path}")
    print(f"[DiscordDB] Target DB: {Path(DISCORD_DB_PATH).resolve()}")
    print(f"[DiscordDB] Batch size: {batch_size:,} | Min content length: {min_length}\n")

    # ── Open DB and initialise schema ─────────────────────────────────────────
    conn = _get_discord_db(readonly=False)
    _init_discord_schema(conn)

    if reset:
        print("[DiscordDB] --reset: dropping existing data for a clean rebuild...")
        conn.executescript(
            "DROP TABLE IF EXISTS messages_fts;"
            "DROP TABLE IF EXISTS messages;"
            "DROP TRIGGER IF EXISTS messages_ai;"
            "DROP TRIGGER IF EXISTS messages_ad;"
            "DROP TRIGGER IF EXISTS messages_au;"
        )
        _init_discord_schema(conn)
        print("[DiscordDB] Schema recreated.\n")

    # ── Disable FTS triggers for bulk load — we'll rebuild at the end ─────────
    # Dropping triggers speeds up bulk INSERT by ~3-4x on 2M rows.
    conn.executescript(
        "DROP TRIGGER IF EXISTS messages_ai;"
        "DROP TRIGGER IF EXISTS messages_ad;"
        "DROP TRIGGER IF EXISTS messages_au;"
    )
    conn.commit()

    # ── Performance pragmas (write-ahead log, larger page cache) ─────────────
    conn.executescript(
        "PRAGMA journal_mode = WAL;"
        "PRAGMA synchronous  = NORMAL;"
        "PRAGMA cache_size   = -65536;"   # 64 MB page cache
        "PRAGMA temp_store   = MEMORY;"
    )

    # ── Aggregate stats ───────────────────────────────────────────────────────
    stats = {
        "total_files":  total_files,
        "files_ok":     0,
        "files_failed": 0,
        "total_read":   0,
        "inserted":     0,
        "skipped":      0,
        "errors":       0,
    }

    batch: list[tuple] = []
    start = time.time()

    def _flush(b: list[tuple]) -> None:
        """INSERT OR IGNORE a batch of (id, ts, author, channel, content) tuples."""
        if not b:
            return
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO messages(id, timestamp, author, channel, content) "
                "VALUES (?, ?, ?, ?, ?)",
                b,
            )
            conn.commit()
            stats["inserted"] += len(b)
        except sqlite3.Error as e:
            print(f"\n[DiscordDB] ⚠  Batch insert error: {e}")
            stats["errors"] += len(b)

    # ── Per-file processing loop ──────────────────────────────────────────────
    for file_idx, json_path in enumerate(json_files, start=1):
        try:
            with open(json_path, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as e:
            print(f"\n[DiscordDB] ✗ JSON parse error in {json_path.name}: {e}")
            stats["files_failed"] += 1
            continue
        except OSError as e:
            print(f"\n[DiscordDB] ✗ Cannot open {json_path.name}: {e}")
            stats["files_failed"] += 1
            continue

        # ── Extract channel name from DCE root object ─────────────────────
        channel_obj  = data.get("channel") or {}
        channel_name = str(
            channel_obj.get("name") or
            channel_obj.get("id") or
            json_path.stem          # fall back to filename without extension
        )[:80]

        # ── Pull the messages array ───────────────────────────────────────
        messages = data.get("messages") or []
        if not isinstance(messages, list):
            # Some exporters put messages at the root level as a list
            if isinstance(data, list):
                messages = data
            else:
                print(f"\n[DiscordDB] ✗ No 'messages' array in {json_path.name} — skipping.")
                stats["files_failed"] += 1
                continue

        file_read = 0
        for msg in messages:
            if not isinstance(msg, dict):
                stats["skipped"] += 1
                continue

            stats["total_read"] += 1
            file_read += 1

            row = _parse_dce_message(
                msg,
                channel_name=channel_name,
                fallback_id_prefix=f"{json_path.stem}_{file_read}",
            )

            if row is None:
                stats["skipped"] += 1
                continue

            # Apply minimum content length filter
            if len(row[4]) < min_length:   # row[4] is content
                stats["skipped"] += 1
                continue

            batch.append(row)

            if len(batch) >= batch_size:
                _flush(batch)
                batch.clear()

        stats["files_ok"] += 1

        # Print per-file progress line
        _print_progress(
            file_idx, total_files, json_path.name,
            stats, time.time() - start,
        )

    # ── Flush any remaining rows ──────────────────────────────────────────────
    if batch:
        _flush(batch)
        batch.clear()

    # ── Rebuild FTS index (much faster than per-row trigger inserts) ──────────
    try:
        print("\n[DiscordDB] Rebuilding FTS5 full-text index (this may take 1–3 minutes)...")
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.commit()
        print("[DiscordDB] FTS index rebuilt successfully.")
    except sqlite3.Error as e:
        print(f"[DiscordDB] ⚠  FTS rebuild warning: {e}")

    # ── Restore live-update triggers for future bot queries ───────────────────
    try:
        conn.executescript(_CREATE_FTS_TRIGGERS)
        conn.commit()
    except sqlite3.Error as e:
        print(f"[DiscordDB] ⚠  Could not restore FTS triggers: {e}")

    # ── Final stats ───────────────────────────────────────────────────────────
    conn.close()
    elapsed = time.time() - start
    stats["elapsed_s"] = round(elapsed, 1)

    _print_progress(
        total_files, total_files, "── COMPLETE ──",
        stats, elapsed, final=True,
    )
    print(
        f"\n{'─'*70}\n"
        f"  Files:    {stats['files_ok']:,} OK  |  {stats['files_failed']:,} failed\n"
        f"  Messages: {stats['total_read']:,} read  |  {stats['inserted']:,} inserted  |  "
        f"{stats['skipped']:,} skipped  |  {stats['errors']:,} errors\n"
        f"  Time:     {elapsed:.1f}s  ({stats['total_read']/max(elapsed,0.001):,.0f} msg/s avg)\n"
        f"{'─'*70}\n"
    )
    return stats


# Backwards-compatible alias for any code that still calls the old name
def ingest_discord_jsonl(filepath: str, reset: bool = False,
                         batch_size: int = 5_000, min_length: int = 1) -> dict:
    """Alias for ingest_discord() — accepts a single file path for backwards compatibility."""
    return ingest_discord(filepath, reset=reset, batch_size=batch_size, min_length=min_length)


# ── Schema introspection (for LLM context) ────────────────────────────────────

DISCORD_DB_SCHEMA_TEXT = """
SQLite database: discord_history.db
Table: messages
  id        TEXT  — Discord message snowflake ID (PRIMARY KEY)
  timestamp TEXT  — ISO 8601 datetime string, e.g. '2023-07-14T21:33:05'
  author    TEXT  — Discord username of the sender (e.g. 'Diddy', 'WittGPT')
  channel   TEXT  — Channel name or ID (e.g. 'general', 'trash-talk')
  content   TEXT  — Raw message text

Virtual table: messages_fts  (FTS5 full-text index over content)
  Use: SELECT m.* FROM messages m
       JOIN messages_fts f ON m.rowid = f.rowid
       WHERE messages_fts MATCH 'keyword'
       ORDER BY rank;

Useful query patterns:
  -- Count how many times an author said something:
  SELECT COUNT(*) FROM messages WHERE author LIKE '%Diddy%' AND content LIKE '%asshole%';

  -- FTS keyword search with ranking:
  SELECT m.author, m.timestamp, m.content
  FROM messages m JOIN messages_fts f ON m.rowid = f.rowid
  WHERE messages_fts MATCH 'keyword OR "exact phrase"'
  ORDER BY rank LIMIT 20;

  -- First/last time something was said:
  SELECT author, timestamp, content FROM messages
  WHERE content LIKE '%phrase%' ORDER BY timestamp ASC LIMIT 1;

  -- Most active users:
  SELECT author, COUNT(*) as msg_count FROM messages
  GROUP BY author ORDER BY msg_count DESC LIMIT 10;

  -- Messages by author in a date range:
  SELECT * FROM messages
  WHERE author LIKE '%username%'
    AND timestamp BETWEEN '2023-01-01' AND '2023-12-31'
  LIMIT 50;

IMPORTANT SQL RULES:
  1. NEVER use DROP, DELETE, UPDATE, INSERT, CREATE, ALTER, PRAGMA, ATTACH, or any DDL/DML.
  2. Only SELECT statements are allowed.
  3. Always LIMIT results (max 100 rows) to avoid timeouts.
  4. For fuzzy author matches use: author LIKE '%name%' (case-insensitive in SQLite by default for ASCII).
  5. FTS MATCH syntax: single words, "quoted phrases", OR, NOT, author:username
  6. Avoid MATCH and LIKE on the same query — use one or the other.
  7. Always return timestamp, author, content columns so results are human-readable.
"""


def get_discord_db_schema() -> str:
    """Returns the schema description string for injection into LLM prompts."""
    if not discord_db_exists():
        return "(Discord history DB not built yet — ingestion required)"
    # Add live stats
    try:
        conn = _get_discord_db(readonly=True)
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        earliest = conn.execute("SELECT MIN(timestamp) FROM messages").fetchone()[0]
        latest   = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
        authors  = conn.execute("SELECT COUNT(DISTINCT author) FROM messages").fetchone()[0]
        conn.close()
        stats_line = (
            f"\nDatabase stats: {total:,} messages | "
            f"{authors:,} unique authors | "
            f"Date range: {earliest[:10] if earliest else '?'} → {latest[:10] if latest else '?'}"
        )
        return DISCORD_DB_SCHEMA_TEXT + stats_line
    except Exception:
        return DISCORD_DB_SCHEMA_TEXT


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    parser = argparse.ArgumentParser(
        description=(
            "TSL Data Manager — Discord History Ingestion Tool\n\n"
            "Ingests Discord message exports from DiscordChatExporter into\n"
            "a local SQLite database (discord_history.db) with FTS5 search.\n\n"
            "Accepts either a single .json file or a directory of .json files.\n"
            "Files in a directory are processed in natural sort order."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--ingest-discord",
        metavar="PATH",
        help=(
            "Path to a single DiscordChatExporter .json file, OR a directory\n"
            "containing multiple .json export files (e.g. '[part 1].json',\n"
            "'[part 2].json', ...). All .json files in the directory are\n"
            "processed automatically in natural sort order."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Drop and recreate the discord_history.db before ingesting.\n"
            "Use this for a clean rebuild. Without --reset, new messages\n"
            "are merged into the existing DB (duplicates are ignored)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5_000,
        help=(
            "Number of rows per SQLite INSERT transaction (default: 5000).\n"
            "Increase to 20000 on machines with ≥16 GB RAM for faster ingest."
        ),
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=1,
        help=(
            "Minimum character length of message content to index (default: 1).\n"
            "Increase to 10 to skip single-character emoji-only messages."
        ),
    )
    parser.add_argument(
        "--db-stats",
        action="store_true",
        help="Print current discord_history.db statistics and exit.",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        help=(
            f"Override the default DB path (default: {DISCORD_DB_PATH}).\n"
            "Can also be set via the DISCORD_DB_PATH environment variable."
        ),
    )

    args = parser.parse_args()

    # Allow overriding DB path via CLI
    if args.db_path:
        import data_manager as _self
        _self.DISCORD_DB_PATH = args.db_path
        DISCORD_DB_PATH = args.db_path

    if args.db_stats:
        print(get_discord_db_schema())
        sys.exit(0)

    elif args.ingest_discord:
        try:
            result = ingest_discord(
                args.ingest_discord,
                reset=args.reset,
                batch_size=args.batch_size,
                min_length=args.min_length,
            )
            # Exit with error code if nothing was inserted
            if result["inserted"] == 0 and result["total_read"] > 0:
                print("[DiscordDB] WARNING: Messages were read but none were inserted.")
                print("  Check --min-length and that the JSON structure is correct.")
                sys.exit(2)
        except FileNotFoundError as e:
            print(f"\nERROR: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"\nUnexpected error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    else:
        parser.print_help()
        print(
            "\nExamples:\n"
            "  # Ingest a single file:\n"
            "  python data_manager.py --ingest-discord exports/chat.json\n\n"
            "  # Ingest an entire folder of DCE files (clean rebuild):\n"
            "  python data_manager.py --ingest-discord exports/ --reset\n\n"
            "  # Check what's in the DB:\n"
            "  python data_manager.py --db-stats\n"
        )

