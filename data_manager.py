"""
data_manager.py — ATLAS League Data Manager (MaddenStats API)
─────────────────────────────────────────────────────────────────────────────
Fetches all TSL data directly from the MaddenStats public API.
No Render server, no Snallabot exports, no cold starts.

API base: https://mymadden.com/api/lg/tsl/

Public DataFrames (populated after load_all()):
  df_standings   — 32 teams, W/L/T, yardage ranks, etc.
  df_teams       — 32 teams with owner usernames
  df_games       — current week schedule with home/away team names
  df_players     — stat leaders (pass/rush/rec/def) used as player index
  df_offense     — passing + rushing + receiving leaders
  df_defense     — defensive leaders (sacks/ints/tackles)
  df_team_stats  — alias for df_standings (all team stats live there)
  df_trades      — all accepted trades this season
  df_power       — MaddenStats power rankings (rank, record, phase ranks)

Constants:
  API_BASE        — MaddenStats API root
  CURRENT_SEASON  — live season (1-indexed, from /info)
  CURRENT_WEEK    — live week   (1-indexed, from /info)
  CURRENT_STAGE   — current stageIndex (1 = regular season)
  REGULAR_STAGE   — always 1 for this league

Helper functions:
  get_league_status()             → "Season X | Week Y"
  get_team_record(team)           → "W-L-T" string
  get_team_owner(team_name)       → string
  get_last_n_games(team, n)       → list of recent game dicts
  get_h2h_record(team_a, team_b)  → {a_wins, b_wins, ties}
  get_weekly_results(week)        → completed games (status 2 or 3) for a given week
  discord_db_exists()             → bool
  get_discord_db_schema()         → schema string for LLM
  _get_discord_db(readonly)       → sqlite3.Connection

MM Export field reference (games.csv):
  id, scheduleId, seasonIndex, stageIndex, weekIndex(0-based),
  homeTeamId, awayTeamId, homeTeamName, awayTeamName,
  homeScore, awayScore, status(1=sched,2=live,3=final),
  homeUser, awayUser, gameTime
  Team names use nickName (Ravens, Bears, etc.)

─────────────────────────────────────────────────────────────────────────────
Fixes applied (v2 — WittGPT Code Review rebuild):
  - BUG #1:  get_rings_count() cached at load_all() time — no more N live
             API calls per trade evaluation.
  - BUG #3:  Bare `except: pass` replaced with `except Exception as e: log.warning()`
             throughout so real errors are no longer silently swallowed.
  - BUG #5:  `import io` moved to top-level (was inside _fetch_csv).
  - BUG #6:  `import math` inside load_all player age derivation replaced
             with top-level import.
  - FIX #11: get_position_scarcity() results cached at load_all() time.
             _scarcity_cache is a module-level dict rebuilt on every sync.
  - FIX #8:  _startup_done flag support — load_all() is safe to call
             multiple times but callers (bot.py) can now guard against it.
  - ADD:     flag_stat_padding(), snapshot_week_stats(), get_stat_delta()
             merged from data_manager_additions.py. Fixes live blowout_monitor
             AttributeError crash (monitor was failing silently every 15 min).
             snapshot_week_stats() now called automatically at end of load_all().
  - ADD:     get_week(), get_season(), get_draft_picks() convenience helpers.
─────────────────────────────────────────────────────────────────────────────
Fixes applied (v3 — ATLAS v1.4.2 Code Review):
  - FIX:     Rebranded docstring + User-Agent from WittGPT → ATLAS.
  - FIX:     Dead autograde callback block removed from load_all() — it ran
             in a thread executor where asyncio.get_running_loop() always
             raised RuntimeError, making it a silent no-op. Autograde is
             handled by bot.py /wittsync after load_all returns.
  - FIX:     Off-by-one in fallback week fetch range (_l_week+2 → _l_week+1)
             — was fetching one weekIndex past what exists every sync.
  - FIX:     get_last_n_games() completion filter now uses status field
             instead of score sniffing — 0-0 completed games no longer dropped.
  - FIX:     get_weekly_results() debug print → log.debug() — no longer
             spams terminal on every call.
  - FIX:     PRAGMA table_info() now quotes table name (SQL hygiene).
  - DOC:     get_h2h_record() docstring clarifies current-season-only scope.
  - DOC:     _rebuild_rings_cache() docstring warns about wins>=14 proxy
             not detecting actual Super Bowl wins, and sequential API cost.
─────────────────────────────────────────────────────────────────────────────
"""

import io
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
import requests
import pandas as pd
import logging

log = logging.getLogger(__name__)

# ── API Configuration ─────────────────────────────────────────────────────────
LEAGUE_SLUG = "tsl"
API_BASE    = f"https://mymadden.com/api/lg/{LEAGUE_SLUG}"

_HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer":          f"https://mymadden.com/lg/{LEAGUE_SLUG}",
    "User-Agent":       "ATLAS-Bot/1.4",
}

# ── League state (atomically swapped by load_all()) ──────────────────────────
REGULAR_STAGE  = 1     # stageIndex for regular season in this league
_CHAMPIONSHIP_STAGE_MIN = 200  # stageIndex >= 200 indicates championship rounds

# Legacy compat shim
DATA_DIR = ""
BASE_URL = API_BASE


@dataclass
class LeagueState:
    """All mutable league data — swapped atomically by load_all()."""
    CURRENT_SEASON:   int = 6
    CURRENT_WEEK:     int = 4
    CURRENT_STAGE:    int = 1
    last_sync_ts:     float = 0.0
    df_standings:     pd.DataFrame = field(default_factory=pd.DataFrame)
    df_teams:         pd.DataFrame = field(default_factory=pd.DataFrame)
    df_games:         pd.DataFrame = field(default_factory=pd.DataFrame)
    df_players:       pd.DataFrame = field(default_factory=pd.DataFrame)
    df_offense:       pd.DataFrame = field(default_factory=pd.DataFrame)
    df_defense:       pd.DataFrame = field(default_factory=pd.DataFrame)
    df_team_stats:    pd.DataFrame = field(default_factory=pd.DataFrame)
    df_trades:        pd.DataFrame = field(default_factory=pd.DataFrame)
    df_power:         pd.DataFrame = field(default_factory=pd.DataFrame)
    df_all_games:     pd.DataFrame = field(default_factory=pd.DataFrame)
    _team_id_to_name: dict = field(default_factory=dict)
    _team_id_to_abbr: dict = field(default_factory=dict)
    _players_cache:   list = field(default_factory=list)
    _abilities_cache: list = field(default_factory=list)
    _rings_cache:     dict = field(default_factory=dict)
    _scarcity_cache:  dict = field(default_factory=dict)


_state = LeagueState()


def __getattr__(name: str):
    """Module-level __getattr__ (PEP 562) — backward compat for dm.FIELD access."""
    if hasattr(_state, name):
        return getattr(_state, name)
    raise AttributeError(f"module 'data_manager' has no attribute {name}")


# ── Normalize API nicknames → official NFL team names ─────────────────────────
_NICK_NORMALIZE: dict[str, str] = {
    "Vikes":   "Vikings",
    "G-Men":   "Giants",
    "GMen":    "Giants",
    "Phins":   "Dolphins",
    "Bolts":   "Chargers",
    "Pats":    "Patriots",
    "Pack":    "Packers",
    "Niners":  "49ers",
    "Boys":    "Cowboys",
    "Skins":   "Commanders",
    "Nats":    "Commanders",
    "Jags":    "Jaguars",
    "Cards":   "Cardinals",
    "Bucs":    "Buccaneers",
}

def _normalize_nick(name: str) -> str:
    """Map informal API nicknames to official NFL team names."""
    return _NICK_NORMALIZE.get(name, name)

# ── Discord DB path ───────────────────────────────────────────────────────────
_DB_PATH = os.path.join(os.path.dirname(__file__), "discord_history.db")

# ── Autograde callback (set by sportsbook cog after load_all fires) ───────────
# Call signature: async def callback() — no args
_autograde_callback = None


# ═════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL HTTP HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get(path: str, params: dict | None = None, timeout: int = 20) -> dict | list | None:
    """GET one endpoint. Returns parsed JSON or None on any failure."""
    url = f"{API_BASE}{path}"
    try:
        r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
        if r.status_code == 200:
            if not r.text.strip():
                log.warning(f"[API] {path} → 200 but empty body")
                return None
            return r.json()
        log.warning(f"[API] {path} → HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        log.warning(f"[API] {path} → TIMEOUT")
    except Exception as e:
        log.warning(f"[API] {path} → {e}")
    return None


def _fetch_csv(path: str, timeout: int = 60) -> list:
    """
    Fetch a CSV endpoint (like /export/players, /export/playerAbilities).
    Returns a list of dicts (one per row), or [] on failure.
    """
    url = f"{API_BASE}{path}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            log.warning(f"[CSV] {path} → HTTP {r.status_code}")
            return []
        text = r.text.strip()
        if not text:
            log.warning(f"[CSV] {path} → empty body")
            return []
        # FIX #5: io imported at top of file now
        df = pd.read_csv(io.StringIO(text))
        records = df.where(pd.notnull(df), None).to_dict(orient="records")
        log.debug(f"[CSV] {path} → {len(records)} rows loaded")
        return records
    except requests.exceptions.Timeout:
        log.warning(f"[CSV] {path} → TIMEOUT")
    except Exception as e:
        log.warning(f"[CSV] {path} → {e}")
    return []


def _paginate(path: str, params: dict | None = None, max_pages: int = 10) -> list:
    """Walk paginated MaddenStats endpoints up to max_pages."""
    all_items: list = []
    page = 1
    while page <= max_pages:
        p = dict(params or {})
        p["page"]     = page
        p["per_page"] = 50
        result = _get(path, params=p)
        if result is None:
            break
        items = result.get("data", []) if isinstance(result, dict) else result
        if not items:
            break
        all_items.extend(items)
        last_page = result.get("last_page", 1) if isinstance(result, dict) else 1
        if page >= last_page:
            break
        page += 1
        time.sleep(0.03)
    return all_items


def _df(records: list) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    try:
        return pd.DataFrame(records)
    except Exception as e:
        log.warning(f"[df] build error: {e}")
        return pd.DataFrame()


# ═════════════════════════════════════════════════════════════════════════════
#  TEAM NAME HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def team_name(team_id: int | str) -> str:
    """Resolve a teamId → nickName, e.g. 774242306 → 'Bengals'."""
    return _state._team_id_to_name.get(int(team_id), str(team_id))

def team_abbr(team_id: int | str) -> str:
    """Resolve a teamId → abbreviation, e.g. 774242306 → 'CIN'."""
    return _state._team_id_to_abbr.get(int(team_id), str(team_id))


# ═════════════════════════════════════════════════════════════════════════════
#  RINGS CACHE BUILDER  (FIX #1 — runs once per load_all, not per trade)
# ═════════════════════════════════════════════════════════════════════════════

def _rebuild_rings_cache(abbr_map: dict[int, str], season: int, stage: int) -> dict[int, int]:
    """
    Build rings cache from tsl_history.db championship records.
    Falls back to empty cache if DB is unavailable.

    NOTE: Rebuilt once per load_all(). Rings are historical (championships
    already played) and only change when tsl_history.db is re-synced, so
    mid-session invalidation is unnecessary.
    """
    cache: dict[int, int] = {tid: 0 for tid in abbr_map}

    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsl_history.db")
        if not os.path.exists(db_path):
            log.warning("[Rings] tsl_history.db not found, returning empty cache")
            return cache

        conn = sqlite3.connect(db_path, timeout=5)
        # Champions are determined by winning the final game of championship rounds
        rows = conn.execute("""
            SELECT g.winner_team, COUNT(*) as ring_count
            FROM games g
            WHERE CAST(g.stageIndex AS INTEGER) >= ?
              AND g.status IN ('2', '3')
              AND g.winner_team IS NOT NULL
              AND g.winner_team != ''
            GROUP BY g.winner_team
        """, (_CHAMPIONSHIP_STAGE_MIN,)).fetchall()
        conn.close()

        # Map team names back to team IDs
        name_to_id: dict[str, int] = {}
        for tid, abbr in abbr_map.items():
            if abbr:
                name_to_id[abbr.lower()] = tid

        for winner_team, count in rows:
            if winner_team:
                tid = name_to_id.get(winner_team.lower())
                if tid is not None:
                    cache[tid] = count

        log.info(f"[Rings] Cache built from DB: {sum(cache.values())} total rings across {sum(1 for v in cache.values() if v > 0)} teams")
    except Exception as e:
        log.warning(f"[Rings] Error building cache from DB: {e}")

    return cache


# ═════════════════════════════════════════════════════════════════════════════
#  SCARCITY CACHE BUILDER  (FIX #11 — compute once, not per player_value call)
# ═════════════════════════════════════════════════════════════════════════════

def _rebuild_scarcity_cache(players: list) -> dict[str, dict]:
    """
    Compute position scarcity from the full player roster.
    Called once during load_all() — eliminates iterating 1700+ players per trade asset.

    NOTE: Scarcity only shifts when /wittsync refreshes the roster DataFrame.
    Mid-session trades move existing players; they don't change league-wide
    positional counts enough to invalidate this cache.
    """
    EXPECTED: dict[str, int] = {
        "QB": 32, "HB": 64, "WR": 96, "TE": 64, "LT": 32, "LG": 32, "C": 32,
        "RG": 32, "RT": 32, "LE": 32, "RE": 32, "DT": 64, "LOLB": 32, "MLB": 32,
        "ROLB": 32, "CB": 64, "FS": 32, "SS": 32, "K": 32, "P": 32,
        "LEDGE": 32, "REDGE": 32, "MIKE": 32, "WILL": 32, "SAM": 32,
    }
    POS_ALIAS: dict[str, str] = {
        "LE": "LEDGE", "RE": "REDGE", "LOLB": "WILL", "ROLB": "SAM", "MLB": "MIKE",
    }

    counts: dict[str, int] = {}
    for p in players:
        pos_raw = str(p.get("position", p.get("pos", "")) or "").upper()
        pos = POS_ALIAS.get(pos_raw, pos_raw)
        counts[pos] = counts.get(pos, 0) + 1

    result = {}
    for pos, expected in EXPECTED.items():
        count = counts.get(pos, 0)
        ratio = count / expected if expected > 0 else 1.0
        if ratio < 0.60:
            cls = "Scarce"
        elif ratio > 1.30:
            cls = "Saturated"
        else:
            cls = "Normal"
        result[pos] = {"count": count, "expected": expected, "scarcity_class": cls}

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN LOAD
# ═════════════════════════════════════════════════════════════════════════════

def load_all() -> None:
    """Pull all TSL data from MaddenStats and populate module-level DataFrames.

    Uses local variables during fetch so the live globals are never empty.
    Swaps everything atomically at the end.
    """
    global _state, _roster_by_id

    print("--- FETCHING TSL DATA FROM MYMADDEN API ---")

    # ── Local staging variables — globals stay untouched until the swap ────
    _l_season = _state.CURRENT_SEASON
    _l_week   = _state.CURRENT_WEEK
    _l_stage  = _state.CURRENT_STAGE

    info = _get("/info")
    if info:
        season_val = info.get("season", _l_season)
        _l_season  = int(season_val["id"] if isinstance(season_val, dict) else season_val)
        _l_week    = int(info.get("week",       _l_week))
        _l_stage   = int(info.get("stageIndex", _l_stage))
        stage_name = info.get("stageName", "Regular Season")
        print(f"✅ Live Data: Season {_l_season} | {stage_name} | Week {_l_week}")
    else:
        print(f"⚠️  MaddenStats unreachable — using cached defaults: S{_l_season} W{_l_week}")

    # ── Teams ──────────────────────────────────────────────────────────────
    raw_teams_resp = _get("/teams/all")
    teams_raw = raw_teams_resp.get("data", []) if isinstance(raw_teams_resp, dict) else (raw_teams_resp or [])
    _l_name_map = {}
    _l_abbr_map = {}
    for t in teams_raw:
        tid = int(t.get("id", 0))
        if tid:
            _l_name_map[tid] = _normalize_nick(t.get("nickName") or t.get("displayName", ""))
            _l_abbr_map[tid] = t.get("abbrName", "")
        # Normalize nickName in raw data so df_teams is also clean
        if "nickName" in t:
            t["nickName"] = _normalize_nick(t["nickName"])
    _l_df_teams = _df(teams_raw)

    # Local lookup helpers (use the local map, not the global one)
    def _l_team_name(team_id):
        return _l_name_map.get(int(team_id), str(team_id))

    def _l_team_abbr(team_id):
        return _l_abbr_map.get(int(team_id), "")

    # ── Standings ──────────────────────────────────────────────────────────
    raw_standings_resp = _get("/standings")
    standings_raw = raw_standings_resp.get("data", []) if isinstance(raw_standings_resp, dict) else (raw_standings_resp or [])
    for s in standings_raw:
        tid = int(s.get("teamId", 0))
        if tid and tid not in _l_name_map:
            _l_name_map[tid] = _normalize_nick(s.get("teamName", ""))
        # Also normalize teamName in the raw standings data
        if "teamName" in s:
            s["teamName"] = _normalize_nick(s["teamName"])
    _l_df_standings = _df(standings_raw)

    # ── Games ──────────────────────────────────────────────────────────────
    raw_games_resp = _get("/games/schedule")
    games_raw = raw_games_resp.get("data", []) if isinstance(raw_games_resp, dict) else (raw_games_resp or [])
    for g in games_raw:
        g["homeTeamName"] = _l_team_name(g.get("homeTeamId", 0))
        g["awayTeamName"] = _l_team_name(g.get("awayTeamId", 0))
        g["homeTeamAbbr"] = _l_team_abbr(g.get("homeTeamId", 0))
        g["awayTeamAbbr"] = _l_team_abbr(g.get("awayTeamId", 0))
        g["matchup_key"]  = f"{g['awayTeamName']} @ {g['homeTeamName']}"
    _l_df_games = _df(games_raw)

    # ── All scores ─────────────────────────────────────────────────────────
    all_scores_resp = _get(f"/games/scores/{_l_season}/{_l_stage}")
    all_scores_raw  = all_scores_resp.get("data", []) if isinstance(all_scores_resp, dict) else (all_scores_resp or [])

    if len(all_scores_raw) <= 16:
        all_scores_raw = []
        # _l_week is 1-based; weekIndex is 0-based.  range(0, _l_week) loads
        # up to weekIndex (_l_week-1), which is the current week.  We add +1
        # so the sportsbook can show next-week games for betting.
        for w_idx in range(0, _l_week + 1):
            resp = _get(f"/games/scores/{_l_season}/{_l_stage}/{w_idx}")
            chunk = resp.get("data", []) if isinstance(resp, dict) else (resp or [])
            all_scores_raw.extend(chunk)
            if chunk:
                print(f"  Loaded weekIndex={w_idx}: {len(chunk)} games")

    for g in all_scores_raw:
        if not g.get("homeTeamName"):
            g["homeTeamName"] = _l_team_name(g.get("homeTeamId", 0))
        if not g.get("awayTeamName"):
            g["awayTeamName"] = _l_team_name(g.get("awayTeamId", 0))

    _l_df_all_games = _df(all_scores_raw)
    print(f"✅ Full season games loaded: {len(_l_df_all_games)} rows")

    # ── Power rankings ─────────────────────────────────────────────────────
    raw_power_resp = _get("/power/full")
    power_raw = raw_power_resp.get("data", []) if isinstance(raw_power_resp, dict) else (raw_power_resp or [])
    for p in power_raw:
        t = p.pop("team", {}) or {}
        p["teamName"]    = _normalize_nick(t.get("nickName", t.get("displayName", "")))
        p["abbrName"]    = t.get("abbrName", "")
        p["userName"]    = t.get("userName", "")
        p["ovrRating"]   = t.get("ovrRating", 0)
        p["confName"]    = t.get("confName", "")
        p["divName"]     = t.get("divName", "")
        p["seed"]        = t.get("seed", 0)
        p["winPct"]      = t.get("winPct", "0.000")
    _l_df_power = _df(power_raw)

    # ── Stats ──────────────────────────────────────────────────────────────
    print("Fetching stat leaders...")

    print("  → passing stats...")
    pass_stats = _paginate("/stats/players/passStats",     max_pages=999)
    print("  → rush leaders...")
    rush_stats = _paginate("/stats/players/rushLeaders",   max_pages=999)
    print("  → rec leaders...")
    rec_stats  = _paginate("/stats/players/recLeaders",    max_pages=999)
    for p in pass_stats: p["statType"] = "passing"
    for p in rush_stats: p["statType"] = "rushing"
    for p in rec_stats:  p["statType"] = "receiving"
    _l_df_offense = _df(pass_stats + rush_stats + rec_stats)

    print("  → sack leaders...")
    sack_stats = _paginate("/stats/players/sackLeaders",    max_pages=999)
    print("  → int leaders...")
    int_stats  = _paginate("/stats/players/intLeaders",     max_pages=999)
    print("  → tackle leaders...")
    tck_stats  = _paginate("/stats/players/tackleLeaders",  max_pages=999)
    for p in sack_stats: p["statType"] = "sacks"
    for p in int_stats:  p["statType"] = "interceptions"
    for p in tck_stats:  p["statType"] = "tackles"

    # Load full defensive stats from CSV export (has defTotalTackles, defFumRec, etc.)
    print("  → defensive export (full stats)...")
    def_export = _fetch_csv("/export/defensive")
    if def_export:
        # Normalize team names in defensive export
        for d in def_export:
            if "teamName" not in d:
                tid = d.get("teamId")
                if tid:
                    try:
                        d["teamName"] = _l_name_map.get(int(tid), "")
                    except (ValueError, TypeError):
                        pass
            elif d.get("teamName"):
                d["teamName"] = _normalize_nick(d["teamName"])

        _l_df_def_full = _df(def_export)

        # Filter to current season + regular season, then aggregate per player
        if "seasonIndex" in _l_df_def_full.columns:
            _l_df_def_full["seasonIndex"] = pd.to_numeric(_l_df_def_full["seasonIndex"], errors="coerce")
        if "stageIndex" in _l_df_def_full.columns:
            _l_df_def_full["stageIndex"]  = pd.to_numeric(_l_df_def_full["stageIndex"],  errors="coerce")
        cur = _l_df_def_full[
            (_l_df_def_full["seasonIndex"] == _l_season) &
            (_l_df_def_full["stageIndex"] == REGULAR_STAGE)
        ].copy()

        # Aggregate per-game rows into season totals per player
        _def_num_cols = [c for c in cur.columns if c.startswith("def") and c != "defPts"]
        for c in _def_num_cols:
            cur[c] = pd.to_numeric(cur[c], errors="coerce").fillna(0)

        # Keep identity columns + sum stat columns
        id_cols = ["rosterId", "fullName", "extendedName", "teamName", "pos"]
        id_cols = [c for c in id_cols if c in cur.columns]
        if id_cols and _def_num_cols:
            _l_df_defense = cur.groupby(id_cols, as_index=False)[_def_num_cols].sum()
        else:
            _l_df_defense = cur

        print(f"     {len(def_export)} raw rows → {len(_l_df_defense)} player season totals")
    else:
        # Fallback to stat leader endpoints (limited columns)
        print("     ⚠️  /export/defensive empty — falling back to stat leader endpoints")
        _l_df_defense = _df(sack_stats + int_stats + tck_stats)

    _l_df_players = _df(pass_stats + rush_stats + rec_stats + sack_stats + int_stats + tck_stats)

    # ── Trades ─────────────────────────────────────────────────────────────
    print("Fetching trades...")
    trades_raw = _paginate(
        "/trades/search",
        params={"status": "accepted", "season": _l_season},
        max_pages=99,
    )
    _l_df_trades = _df(trades_raw)

    # ── Full roster ────────────────────────────────────────────────────────
    print("Fetching full roster...")
    players_raw = _fetch_csv("/export/players", timeout=120)
    if not players_raw:
        print("     ⚠️  /export/players returned empty — draft history and ability auditing disabled")
    else:
        for p in players_raw:
            if "pos" not in p and "position" in p:
                p["pos"] = p["position"]

            if "overallRating" not in p or not p.get("overallRating"):
                raw_ovr = p.get("playerBestOvr")
                if raw_ovr is not None:
                    p["overallRating"] = raw_ovr

            # FIX #6: math imported at top level — no more `import math` per player
            if "age" not in p or p.get("age") is None:
                try:
                    years_pro = int(float(p.get("yearsPro", 1) or 1))
                    rookie_yr = int(float(p.get("rookieYear", _l_season) or _l_season))
                    seasons_played = max(_l_season - rookie_yr, 0)
                    p["age"] = 22 + seasons_played
                except (ValueError, TypeError):
                    p["age"] = 25

            dev_raw = str(p.get("dev") or "")
            if not dev_raw:
                p["dev"] = "Normal"

            tid = p.get("teamId")
            if tid and "teamName" not in p:
                try:
                    p["teamName"] = _l_name_map.get(int(tid), "Free Agent")
                except (ValueError, TypeError):
                    p["teamName"] = "Free Agent"
        print(f"     {len(players_raw)} players loaded")
    _l_players_cache = players_raw

    print("Fetching player abilities...")
    abilities_raw = _fetch_csv("/export/playerAbilities")
    if not abilities_raw:
        print("     0 ability records — /export/playerAbilities returned empty")
    else:
        print(f"     {len(abilities_raw)} ability records loaded")
    _l_abilities_cache = abilities_raw

    # ── FIX #1: Build rings cache (once per sync, not per trade) ──────────
    print("Building rings cache...")
    _l_rings_cache = _rebuild_rings_cache(_l_abbr_map, _l_season, REGULAR_STAGE)

    # ── FIX #11: Build scarcity cache (once per sync, not per player_value) ─
    _l_scarcity_cache = _rebuild_scarcity_cache(_l_players_cache)
    print(f"[Scarcity] Cache built: {len(_l_scarcity_cache)} positions indexed")

    # ══════════════════════════════════════════════════════════════════════
    # ATOMIC SWAP — single pointer reassignment; GIL guarantees atomicity
    # ══════════════════════════════════════════════════════════════════════
    _state = LeagueState(
        CURRENT_SEASON   = _l_season,
        CURRENT_WEEK     = _l_week,
        CURRENT_STAGE    = _l_stage,
        last_sync_ts     = time.time(),
        df_teams         = _l_df_teams,
        df_standings     = _l_df_standings,
        df_team_stats    = _l_df_standings,
        df_games         = _l_df_games,
        df_all_games     = _l_df_all_games,
        df_power         = _l_df_power,
        df_offense       = _l_df_offense,
        df_defense       = _l_df_defense,
        df_players       = _l_df_players,
        df_trades        = _l_df_trades,
        _team_id_to_name = _l_name_map,
        _team_id_to_abbr = _l_abbr_map,
        _players_cache   = _l_players_cache,
        _abilities_cache = _l_abilities_cache,
        _rings_cache     = _l_rings_cache,
        _scarcity_cache  = _l_scarcity_cache,
    )

    _rebuild_roster_index()
    print(f"     {len(_roster_by_id)} players indexed by rosterId")

    print(
        f"✅ Load complete — "
        f"{len(_state.df_players)} players | "
        f"{len(_state.df_games)} games | "
        f"{len(_state.df_standings)} teams | "
        f"{len(_state.df_trades)} trades"
    )

    # NOTE: autograde callback is NOT fired here because load_all() runs in a
    # thread executor (no event loop). bot.py's /wittsync handles autograde
    # directly via `await dm._autograde_callback()` after load_all returns.

    # ── Snapshot stats for blowout_monitor delta detection ────────────────────
    snapshot_week_stats(_state.CURRENT_WEEK)


# ═════════════════════════════════════════════════════════════════════════════
#  PUBLIC HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def get_league_status() -> str:
    return f"Season {_state.CURRENT_SEASON} | Week {_state.CURRENT_WEEK}"


def get_sync_age_text() -> str | None:
    """Return a stale-data warning string if last sync was >30 minutes ago, else None."""
    if _state.last_sync_ts == 0.0:
        return "Data not yet loaded"
    age_min = (time.time() - _state.last_sync_ts) / 60
    if age_min > 30:
        hours = int(age_min // 60)
        mins = int(age_min % 60)
        if hours > 0:
            return f"Data as of {hours}h {mins}m ago"
        return f"Data as of {mins}m ago"
    return None


def get_players() -> list:
    """Return full roster from /export/players CSV. Contains rookieYear, draftRound, dev, etc."""
    return _state._players_cache


def get_player_abilities() -> list:
    return _state._abilities_cache


def find_trades_by_player(player_name: str) -> list[dict]:
    """Search df_trades for any trade mentioning a player by name.

    Returns list of dicts with keys: team1Name, team2Name, team1Sent, team2Sent,
    seasonIndex, weekIndex.  Most recent trade first.
    """
    if _state.df_trades.empty:
        return []
    name_lower = player_name.lower()
    results = []
    for _, row in _state.df_trades.iterrows():
        t1s = str(row.get("team1Sent", "")).lower()
        t2s = str(row.get("team2Sent", "")).lower()
        if name_lower in t1s or name_lower in t2s:
            results.append({
                "team1Name":   str(row.get("team1Name", "")),
                "team2Name":   str(row.get("team2Name", "")),
                "team1Sent":   str(row.get("team1Sent", "")),
                "team2Sent":   str(row.get("team2Sent", "")),
                "seasonIndex": row.get("seasonIndex", ""),
                "weekIndex":   row.get("weekIndex", ""),
            })
    # Most recent first
    results.sort(key=lambda t: (int(t.get("seasonIndex") or 0), int(t.get("weekIndex") or 0)), reverse=True)
    return results


def get_team_record(team: str) -> str:
    """Return 'W-L-T' string for a team. '?-?-?' if not found."""
    if _state.df_standings.empty:
        return "?-?-?"
    mask = _state.df_standings["teamName"].str.lower() == team.lower()
    if not mask.any():
        return "?-?-?"
    row = _state.df_standings[mask].iloc[0]
    w = int(row.get("totalWins",   0))
    l = int(row.get("totalLosses", 0))
    t = int(row.get("totalTies",   0))
    return f"{w}-{l}-{t}"


def get_team_owner(team_name: str) -> str:
    """Return Discord userName for a given team nickName."""
    # Check df_teams first (has userName directly)
    if not _state.df_teams.empty:
        for col in ("nickName", "displayName"):
            if col in _state.df_teams.columns:
                mask = _state.df_teams[col].str.lower() == team_name.lower()
                if mask.any():
                    val = _state.df_teams[mask].iloc[0].get("userName", "")
                    if val:
                        return str(val)

    # Fallback: standings partial match
    if not _state.df_standings.empty and "teamName" in _state.df_standings.columns:
        mask = _state.df_standings["teamName"].str.lower().str.contains(team_name.lower(), na=False)
        if mask.any():
            val = _state.df_standings[mask].iloc[0].get("userName", "")
            if val:
                return str(val)

    return "Unknown"


def get_last_n_games(team: str, n: int = 5) -> list[dict]:
    abbr = ""
    if not _state.df_teams.empty:
        for col in ("nickName", "displayName"):
            if col in _state.df_teams.columns:
                mask = _state.df_teams[col].str.lower() == team.lower()
                if mask.any():
                    abbr = _state.df_teams[mask].iloc[0].get("abbrName", "")
                    break
    if not abbr:
        return []

    data = _get(f"/teams/{abbr}/games/{_state.CURRENT_SEASON}/{_state.CURRENT_STAGE}")
    if not data:
        return []

    games = data if isinstance(data, list) else data.get("data", [])
    completed = [
        g for g in games
        if int(g.get("status", 0) or 0) in (2, 3)
    ]
    completed.sort(
        key=lambda g: (g.get("seasonIndex", 0), g.get("weekIndex", 0)),
        reverse=True,
    )
    results = []
    for g in completed[:n]:
        results.append({
            "week":       int(g.get("week", g.get("weekIndex", 0))),
            "home":       team_name(g.get("homeTeamId", 0)),
            "away":       team_name(g.get("awayTeamId", 0)),
            "home_score": int(g.get("homeScore", 0)),
            "away_score": int(g.get("awayScore", 0)),
        })
    return results


def get_weekly_results(week: int | None = None) -> list[dict]:
    """
    Return completed games (status 2 or 3) for the given week.
    Uses df_all_games (full season load) as primary source.
    Falls back to live API call if df_all_games is empty.

    MM export schema:
      weekIndex = week - 1 (0-based)
      seasonIndex = 1-based TSL season
      stageIndex  = 1 for Regular Season
      status      = 1 scheduled | 2 completed | 3 final
      homeTeamName / awayTeamName = nickName (Ravens, Bears, etc.)
    """
    target     = week if week is not None else _state.CURRENT_WEEK
    week_index = target - 1  # weekIndex is 0-based in MM exports

    src = _state.df_all_games if not _state.df_all_games.empty else _state.df_games

    if not src.empty:
        df = src.copy()

        for col in ["weekIndex", "week", "seasonIndex", "stageIndex", "status"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(-1).astype(int)

        if "weekIndex" in df.columns:
            df = df[df["weekIndex"] == week_index]
        elif "week" in df.columns:
            df = df[df["week"] == target]

        if "seasonIndex" in df.columns:
            df = df[df["seasonIndex"] == _state.CURRENT_SEASON]

        if "stageIndex" in df.columns:
            df = df[df["stageIndex"] == _state.CURRENT_STAGE]

        # Completed games (status 2 or 3). Both have final scores.
        if "status" in df.columns:
            df = df[df["status"].isin([2, 3])]

        results = []
        for _, g in df.iterrows():
            hs  = int(pd.to_numeric(g.get("homeScore", 0), errors="coerce") or 0)
            aws = int(pd.to_numeric(g.get("awayScore", 0), errors="coerce") or 0)
            home = str(g.get("homeTeamName") or team_name(g.get("homeTeamId", 0)))
            away = str(g.get("awayTeamName") or team_name(g.get("awayTeamId", 0)))
            results.append({
                "week":       target,
                "home":       home,
                "away":       away,
                "home_score": hs,
                "away_score": aws,
                "homeUser":   str(g.get("homeUser", "")),
                "awayUser":   str(g.get("awayUser", "")),
            })

        log.debug(f"get_weekly_results: Week {target} (weekIndex={week_index}), "
                  f"source has {len(src)} rows, found {len(results)} final games")
        return results

    # Fallback: live API call
    raw = _get(f"/games/scores/{_state.CURRENT_SEASON}/{_state.CURRENT_STAGE}")
    if isinstance(raw, dict):
        scores = raw.get("data", [])
    else:
        scores = raw or []

    results = []
    for g in scores:
        if pd.to_numeric(g.get("weekIndex", -1), errors="coerce") != week_index:
            continue
        if int(g.get("status", 0)) not in (2, 3):  # completed or final
            continue
        hs  = int(g.get("homeScore", 0) or 0)
        aws = int(g.get("awayScore", 0) or 0)
        home_obj = g.get("homeTeam") or {}
        away_obj = g.get("awayTeam") or {}
        results.append({
            "week":       target,
            "home":       _normalize_nick(g.get("homeTeamName") or home_obj.get("nickName") or home_obj.get("displayName") or team_name(g.get("homeTeamId", 0))),
            "away":       _normalize_nick(g.get("awayTeamName") or away_obj.get("nickName") or away_obj.get("displayName") or team_name(g.get("awayTeamId", 0))),
            "home_score": hs,
            "away_score": aws,
            "homeUser":   str(g.get("homeUser", "")),
            "awayUser":   str(g.get("awayUser", "")),
        })

    return results


def get_h2h_record(team_a: str, team_b: str) -> dict:
    """
    Head-to-head record between two teams for the CURRENT SEASON only.
    Uses df_all_games which is loaded from /games/scores/{season}/{stage}.
    For all-time H2H, query tsl_history.db directly via Codex.
    """
    a_wins = b_wins = ties = 0
    src = _state.df_all_games if not _state.df_all_games.empty else _state.df_games
    if src.empty:
        return {"a_wins": a_wins, "b_wins": b_wins, "ties": ties}
    a_l, b_l = team_a.lower(), team_b.lower()
    for _, g in src.iterrows():
        h   = str(g.get("homeTeamName", "")).lower()
        aw  = str(g.get("awayTeamName", "")).lower()
        hs  = int(pd.to_numeric(g.get("homeScore", 0), errors="coerce") or 0)
        aws = int(pd.to_numeric(g.get("awayScore", 0), errors="coerce") or 0)
        status = int(pd.to_numeric(g.get("status", 0), errors="coerce") or 0)
        if status not in (2, 3):  # final only
            continue
        if not ({h, aw} == {a_l, b_l}):
            continue
        a_score = hs if h == a_l else aws
        b_score = hs if h == b_l else aws
        if a_score > b_score:   a_wins += 1
        elif b_score > a_score: b_wins += 1
        else:                   ties   += 1
    return {"a_wins": a_wins, "b_wins": b_wins, "ties": ties}


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD DB HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def discord_db_exists() -> bool:
    return os.path.isfile(_DB_PATH)


_discord_schema_cache: str | None = None
_discord_schema_ts: float = 0.0
_SCHEMA_TTL = 300  # 5 minutes
_wal_enabled = False


def get_discord_db_schema() -> str:
    """Return schema string for TSL_Archive.db. Cached for 5 min to avoid repeated 1.3GB reads."""
    global _discord_schema_cache, _discord_schema_ts, _wal_enabled
    import time as _t
    now = _t.time()
    if _discord_schema_cache and (now - _discord_schema_ts) < _SCHEMA_TTL:
        return _discord_schema_cache
    if not discord_db_exists():
        return "Discord history DB not found."
    try:
        con = sqlite3.connect(_DB_PATH)
        # Enable WAL mode for better concurrent read performance
        if not _wal_enabled:
            con.execute("PRAGMA journal_mode=WAL")
            _wal_enabled = True
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        lines = []
        for t in tables:
            cur.execute(f'PRAGMA table_info("{t}")')
            cols = [r[1] for r in cur.fetchall()]
            lines.append(f"{t}({', '.join(cols)})")
        con.close()
        result = "\n".join(lines)
        _discord_schema_cache = result
        _discord_schema_ts = now
        return result
    except Exception as e:
        return f"Error reading schema: {e}"


def _get_discord_db(readonly: bool = True) -> sqlite3.Connection:
    if not discord_db_exists():
        raise FileNotFoundError(f"Discord DB not found at {_DB_PATH}")
    if readonly:
        uri = f"file:{_DB_PATH}?mode=ro"
        return sqlite3.connect(uri, uri=True, detect_types=sqlite3.PARSE_DECLTYPES)
    return sqlite3.connect(_DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)


# ═════════════════════════════════════════════════════════════════════════════
#  TRADE ENGINE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

_roster_by_id: dict[int, dict] = {}


def _rebuild_roster_index() -> None:
    global _roster_by_id
    _roster_by_id = {}
    for p in _state._players_cache:
        rid = p.get("rosterId") or p.get("id")
        if rid is not None:
            _roster_by_id[int(rid)] = p


def get_contract_details(roster_id: int) -> dict:
    p = _roster_by_id.get(int(roster_id), {})
    years = int(p.get("contractYearsLeft", 2) or 2)
    cap_raw = float(p.get("capPercent", 0) or p.get("capHit", 0) or 0)
    cap_pct = cap_raw if cap_raw >= 0.5 else cap_raw * 100
    signable = years > 0
    return {
        "years_remaining": years,
        "cap_pct":         round(cap_pct, 2),
        "signable_flag":   signable,
    }


def get_team_record_dict(team_id: int) -> dict:
    default = {"wins": 0, "losses": 0, "ties": 0}
    if _state.df_standings.empty:
        return default
    mask = _state.df_standings["teamId"] == team_id if "teamId" in _state.df_standings.columns else None
    if mask is None or not mask.any():
        name = _state._team_id_to_name.get(int(team_id), "")
        if name and "teamName" in _state.df_standings.columns:
            mask = _state.df_standings["teamName"].str.lower() == name.lower()
    if mask is None or not mask.any():
        return default
    row = _state.df_standings[mask].iloc[0]
    return {
        "wins":   int(row.get("totalWins",   0) or 0),
        "losses": int(row.get("totalLosses", 0) or 0),
        "ties":   int(row.get("totalTies",   0) or 0),
    }


def get_position_scarcity() -> dict[str, dict]:
    """
    Return cached position scarcity data.
    FIX #11: No longer iterates _players_cache on every call —
    uses _scarcity_cache built once during load_all().
    Falls back to live computation if cache is empty (pre-load_all).
    """
    if _state._scarcity_cache:
        return _state._scarcity_cache
    # Fallback: compute live (only before first load_all)
    return _rebuild_scarcity_cache(_state._players_cache)


def get_rings_count(team_id: int) -> int:
    """
    Return cached ring count for a team.
    FIX #1: No longer makes N live API calls per trade evaluation —
    uses _rings_cache built once during load_all().
    Falls back to 0 if cache is empty (pre-load_all).
    """
    return _state._rings_cache.get(int(team_id), 0)


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE WRAPPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_week() -> int:
    """Return current league week (1-indexed)."""
    return _state.CURRENT_WEEK


def get_season() -> int:
    """Return current league season (1-indexed)."""
    return _state.CURRENT_SEASON


def get_draft_picks(team_id: int, year: int | None = None) -> list[dict]:
    """
    Return draft picks for a team.
    NOTE: MaddenStats does not yet expose a /draftPicks endpoint.
    Returns [] until that endpoint exists — commissioner must track picks
    manually via the /trade wizard for now.
    """
    return []


# ─────────────────────────────────────────────────────────────────────────────
# STAT PADDING DETECTION — used by blowout_monitor in bot.py
# Snapshots cumulative player stats each sync; diffs consecutive snapshots
# to compute single-game deltas and flag suspiciously large outputs.
# ─────────────────────────────────────────────────────────────────────────────

_weekly_stat_snapshots: dict[int, dict] = {}   # week → {str(rosterId) → {stat: int}}

_PADDING_STAT_FIELDS = ["passYds", "rushYds", "recYds", "passTDs", "rushTDs", "recTDs"]

_PADDING_THRESHOLDS: dict[str, int] = {
    "passYds": 450,
    "rushYds": 225,
    "recYds":  225,
}


def snapshot_week_stats(week: int) -> None:
    """
    Cache cumulative stats for all players at the given week.
    Called automatically at the end of load_all() so every data sync
    produces a new snapshot — no manual calls needed.
    """
    snapshot: dict[str, dict] = {}
    _warned_missing = False
    for p in _state._players_cache:
        pid = p.get("rosterId")
        if pid:
            if not _warned_missing and not any(f in p for f in _PADDING_STAT_FIELDS):
                log.warning("[dm] snapshot_week_stats: player cache missing expected stat fields %s — "
                            "values will default to 0", _PADDING_STAT_FIELDS)
                _warned_missing = True
            snapshot[str(pid)] = {
                f: int(p.get(f, 0) or 0) for f in _PADDING_STAT_FIELDS
            }
    _weekly_stat_snapshots[week] = snapshot
    log.info(f"[dm] Week {week} stat snapshot stored ({len(snapshot)} players)")


def get_stat_delta(player_id: int, week: int) -> dict[str, int]:
    """
    Single-game stat delta for one player: current week minus previous week.
    Returns zeros if snapshots aren't available (safe pre-load_all no-op).
    On week 1, treats cumulative stats as the single-game total.
    """
    zero = {f: 0 for f in _PADDING_STAT_FIELDS}
    pid  = str(player_id)
    curr = _weekly_stat_snapshots.get(week, {}).get(pid)
    prev = _weekly_stat_snapshots.get(week - 1, {}).get(pid)
    if not curr:
        return zero
    if not prev:
        # First week in snapshots — full cumulative total is the game total
        return {f: curr.get(f, 0) for f in _PADDING_STAT_FIELDS}
    return {f: max(0, curr.get(f, 0) - prev.get(f, 0)) for f in _PADDING_STAT_FIELDS}


def flag_stat_padding(week: int) -> list[dict]:
    """
    Scan every player for single-game stat spikes after a given week.
    Returns list of {player_id, name, team, stat, delta, threshold}.

    Called by blowout_monitor in bot.py every 15 minutes.
    Returns [] safely if snapshots haven't been populated yet (pre-load_all).
    """
    if not _weekly_stat_snapshots:
        return []

    flags: list[dict] = []
    for p in _state._players_cache:
        pid = p.get("rosterId")
        if not pid:
            continue
        name  = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
        team  = p.get("teamName", "?")
        delta = get_stat_delta(int(pid), week)

        for stat, threshold in _PADDING_THRESHOLDS.items():
            if delta.get(stat, 0) > threshold:
                flags.append({
                    "player_id": pid,
                    "name":      name,
                    "team":      team,
                    "stat":      stat,
                    "delta":     delta[stat],
                    "threshold": threshold,
                })
    return flags
