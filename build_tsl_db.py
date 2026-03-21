"""
build_tsl_db.py v3
─────────────────────────────────────────────────────────────────────────────
Two modes:

  1. MANUAL (one-time or recovery):
       Place CSV files in the same directory and run:
       python build_tsl_db.py

  2. AUTO (called on every /wittsync):
       sync_tsl_db() fetches fresh CSVs directly from the MaddenStats API
       and does a full rebuild — no manual files needed.
       Called automatically from bot.py after dm.load_all() completes.

v3 changes:
  - ADD: sync_tsl_db() — full rebuild from live API CSV exports
  - ADD: sync_tsl_db() returns a result dict with row counts and any errors
  - ADD: All CSV fetch errors are non-fatal — DB rebuild continues with
         whatever data was successfully fetched
  - KEEP: build_db() manual path unchanged for recovery/bootstrap
─────────────────────────────────────────────────────────────────────────────
"""

import io
import os
import shutil
import sqlite3
import csv
import time
import requests
import logging

log = logging.getLogger(__name__)

DB_PATH   = os.path.join(os.path.dirname(__file__), "tsl_history.db")
CSV_DIR   = os.path.dirname(__file__)
API_BASE  = "https://mymadden.com/api/lg/tsl"

_TABLE_SCHEMAS = {
    "games": (
        "homeTeamId TEXT, awayTeamId TEXT, homeTeamName TEXT, awayTeamName TEXT, "
        "homeUser TEXT, awayUser TEXT, homeScore TEXT, awayScore TEXT, "
        "weekIndex TEXT, seasonIndex TEXT, status TEXT, scheduleId TEXT, "
        "winner_user TEXT, loser_user TEXT, winner_team TEXT, loser_team TEXT"
    ),
    "offensive_stats": (
        "rosterId TEXT, extendedName TEXT, teamName TEXT, seasonIndex TEXT, "
        "weekIndex TEXT, pos TEXT, passYds TEXT, passTDs TEXT, passInts TEXT, "
        "rushYds TEXT, rushTDs TEXT, recYds TEXT, recTDs TEXT, receptions TEXT"
    ),
    "defensive_stats": (
        "rosterId TEXT, extendedName TEXT, teamName TEXT, seasonIndex TEXT, "
        "weekIndex TEXT, pos TEXT, defTotalTackles TEXT, defSacks TEXT, "
        "defInts TEXT, defForcedFum TEXT"
    ),
    "standings": (
        "teamId TEXT, teamName TEXT, totalWins TEXT, totalLosses TEXT, "
        "totalTies TEXT, seasonIndex TEXT, divName TEXT, confName TEXT"
    ),
    "teams": (
        "teamId TEXT, teamName TEXT, abbrName TEXT, userName TEXT, "
        "divName TEXT, confName TEXT, ovrRating TEXT"
    ),
    "trades": (
        "tradeId TEXT, seasonIndex TEXT, weekIndex TEXT"
    ),
    "players": (
        "rosterId TEXT, firstName TEXT, lastName TEXT, teamName TEXT, "
        "pos TEXT, age TEXT, playerBestOvr TEXT, dev TEXT, devTrait TEXT, "
        "draftRound TEXT, draftPick TEXT, rookieYear TEXT"
    ),
    "player_abilities": (
        "rosterId TEXT, firstName TEXT, lastName TEXT, teamName TEXT, "
        "pos TEXT, abilityTitle TEXT"
    ),
}

_HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer":          "https://mymadden.com/lg/tsl",
    "User-Agent":       "ATLAS-Bot/3.0",
}

# CSV export endpoint → table name mapping
_CSV_EXPORTS = [
    ("/export/games",            "games_raw"),
    ("/export/offensive",        "offensive_stats"),
    ("/export/defensive",        "defensive_stats"),
    ("/export/standings",        "standings"),
    ("/export/teams",            "teams"),
    ("/export/trades",           "trades"),
    ("/export/players",          "players"),
    ("/export/playerAbilities",  "player_abilities"),
]


# ─────────────────────────────────────────────────────────────────────────────
#  LOW-LEVEL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_csv_from_api(endpoint: str, timeout: int = 90) -> list[dict]:
    """Fetch a CSV export endpoint from MaddenStats. Returns list of row dicts."""
    url = f"{API_BASE}{endpoint}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            log.warning(f"[TSL-DB] {endpoint} → HTTP {r.status_code}")
            return []
        text = r.text.strip()
        if not text:
            log.warning(f"[TSL-DB] {endpoint} → empty response")
            return []
        # Parse CSV
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        print(f"  [API] {endpoint} → {len(rows)} rows")
        return rows
    except requests.exceptions.Timeout:
        log.warning(f"[TSL-DB] {endpoint} → TIMEOUT")
    except Exception as e:
        log.warning(f"[TSL-DB] {endpoint} → {e}")
    return []


def _load_rows_into_table(conn: sqlite3.Connection, table_name: str,
                          rows: list[dict], transform=None) -> int:
    """Drop + recreate a table from a list of row dicts. Returns row count."""
    if not rows:
        # Create an empty table with proper schema so downstream queries
        # don't crash with missing columns or "no such table".
        schema = _TABLE_SCHEMAS.get(table_name)
        if schema:
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            conn.execute(f'CREATE TABLE "{table_name}" ({schema})')
        else:
            conn.execute(f'CREATE TABLE IF NOT EXISTS [{table_name}] (placeholder TEXT)')
        return 0
    if transform:
        rows = [transform(r) for r in rows if r is not None]
        rows = [r for r in rows if r]
    if not rows:
        return 0

    # NOTE: All columns stored as TEXT — numeric comparisons in SQL require
    # explicit CAST (e.g., CAST(totalWins AS INTEGER) > 10). This is a known
    # trade-off for schema flexibility with varying CSV export formats.
    cols = list(rows[0].keys())
    placeholders = ",".join(["?" for _ in cols])
    col_defs = ",".join([f'"{c}" TEXT' for c in cols])

    conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
    # Python 3.7+ guarantees dict insertion order, so values() aligns with keys()
    conn.executemany(
        f'INSERT INTO "{table_name}" VALUES ({placeholders})',
        [list(r.values()) for r in rows]
    )
    conn.commit()
    return len(rows)


def _add_indexes(conn: sqlite3.Connection):
    """Add query indexes after all tables are loaded."""
    indexes = [
        ("idx_games_season",    "games(seasonIndex)"),
        ("idx_games_home_user", "games(homeUser)"),
        ("idx_games_away_user", "games(awayUser)"),
        ("idx_games_winner",    "games(winner_user)"),
        ("idx_teams_user",      "teams(userName)"),
        ("idx_off_season",      "offensive_stats(seasonIndex)"),
        ("idx_off_player",      "offensive_stats(extendedName)"),
        ("idx_off_team",        "offensive_stats(teamName)"),
        ("idx_def_season",      "defensive_stats(seasonIndex)"),
        ("idx_def_player",      "defensive_stats(extendedName)"),
        ("idx_ts_season",       "team_stats(seasonIndex)"),   # only exists via CSV build, not API sync
        ("idx_ts_team",         "team_stats(teamName)"),     # only exists via CSV build, not API sync
        ("idx_players_team",    "players(teamName)"),
        ("idx_players_pos",     "players(pos)"),
        ("idx_trades_season",   "trades(seasonIndex)"),
    ]
    for name, target in indexes:
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
        except Exception as e:
            log.debug(f"[TSL-DB] Index {name} skipped: {e}")


def _transform_game(r: dict) -> dict:
    """Add winner_user/loser_user/winner_team/loser_team to a game row."""
    try:
        home = int(r.get("homeScore") or 0)
        away = int(r.get("awayScore") or 0)
    except (ValueError, TypeError):
        home = away = 0

    if home > away:
        r["winner_user"] = r.get("homeUser")
        r["loser_user"]  = r.get("awayUser")
        r["winner_team"] = r.get("homeTeamName")
        r["loser_team"]  = r.get("awayTeamName")
    elif away > home:
        r["winner_user"] = r.get("awayUser")
        r["loser_user"]  = r.get("homeUser")
        r["winner_team"] = r.get("awayTeamName")
        r["loser_team"]  = r.get("homeTeamName")
    else:
        r["winner_user"] = r["loser_user"] = r["winner_team"] = r["loser_team"] = None
    return r


def _build_derived_tables(conn: sqlite3.Connection):
    """
    Build owner_tenure and player_draft_map from the loaded raw tables.
    These are derived — never fetched directly from the API.
    """
    # ── owner_tenure ─────────────────────────────────────────────────────────
    conn.execute("DROP TABLE IF EXISTS owner_tenure")
    conn.execute("""
        CREATE TABLE owner_tenure (
            teamName     TEXT,
            userName     TEXT,
            seasonIndex  TEXT,
            games_played INTEGER,
            PRIMARY KEY (teamName, userName, seasonIndex)
        )
    """)
    conn.execute("""
        INSERT INTO owner_tenure (teamName, userName, seasonIndex, games_played)
        SELECT team, user, seasonIndex, COUNT(*) as games_played
        FROM (
            SELECT homeTeamName as team, homeUser as user, seasonIndex
            FROM games
            WHERE homeUser NOT IN ('CPU','') AND homeUser IS NOT NULL
              AND status IN ('2','3')
            UNION ALL
            SELECT awayTeamName, awayUser, seasonIndex
            FROM games
            WHERE awayUser NOT IN ('CPU','') AND awayUser IS NOT NULL
              AND status IN ('2','3')
        )
        GROUP BY team, user, seasonIndex
    """)
    conn.commit()
    ot_count = conn.execute("SELECT COUNT(*) FROM owner_tenure").fetchone()[0]
    print(f"  [DERIVED] owner_tenure: {ot_count} records")

    # ── player_draft_map ──────────────────────────────────────────────────────
    # Uses rosterId-based matching against stats tables (preferred over name
    # matching which fails on abbreviated names like "T.Hill" vs "Tyreek Hill").
    conn.execute("DROP TABLE IF EXISTS player_draft_map")
    conn.execute("""
        CREATE TABLE player_draft_map (
            rosterId        TEXT PRIMARY KEY,
            extendedName    TEXT,
            drafting_team   TEXT,
            drafting_season TEXT,
            draftRound      TEXT,
            draftPick       TEXT,
            current_team    TEXT,
            dev             TEXT,
            playerBestOvr   TEXT,
            pos             TEXT,
            rookieYear      TEXT,
            was_traded      INTEGER
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO player_draft_map
        SELECT
            p.rosterId,
            p.firstName || ' ' || p.lastName   AS extendedName,
            COALESCE(first_off.teamName, first_def.teamName, p.teamName) AS drafting_team,
            COALESCE(first_off.seasonIndex, first_def.seasonIndex, p.rookieYear) AS drafting_season,
            p.draftRound,
            p.draftPick,
            p.teamName AS current_team,
            p.dev,
            p.playerBestOvr,
            p.pos,
            p.rookieYear,
            CASE WHEN COALESCE(first_off.teamName, first_def.teamName, p.teamName)
                      != p.teamName THEN 1 ELSE 0 END AS was_traded
        FROM players p
        LEFT JOIN (
            SELECT rosterId, teamName, MIN(CAST(seasonIndex AS INTEGER)) AS seasonIndex
            FROM offensive_stats GROUP BY rosterId
        ) first_off ON p.rosterId = first_off.rosterId
        LEFT JOIN (
            SELECT rosterId, teamName, MIN(CAST(seasonIndex AS INTEGER)) AS seasonIndex
            FROM defensive_stats GROUP BY rosterId
        ) first_def ON p.rosterId = first_def.rosterId
    """)
    conn.commit()
    pdm_count = conn.execute("SELECT COUNT(*) FROM player_draft_map").fetchone()[0]
    print(f"  [DERIVED] player_draft_map: {pdm_count} players mapped")

    return ot_count, pdm_count


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN SYNC FUNCTION  (called by bot.py on every /wittsync)
# ─────────────────────────────────────────────────────────────────────────────

def sync_tsl_db(
    players: list | None = None,
    abilities: list | None = None,
) -> dict:
    """
    Full rebuild of tsl_history.db from live MaddenStats API CSV exports.

    Optional pre-loaded data (pass from data_manager to avoid duplicate API hits):
      players   — list of dicts from dm.get_players()   (/export/players)
      abilities — list of dicts from dm.get_player_abilities() (/export/playerAbilities)

    When provided, those endpoints are skipped entirely — no re-fetch.

    Returns a result dict:
      {
        "success": bool,
        "tables": {table_name: row_count},
        "errors": [str],
        "elapsed": float,
        "games": int,
        "players": int,
      }
    """
    start = time.time()
    result = {"success": False, "tables": {}, "errors": [], "elapsed": 0.0}

    print("[TSL-DB] Starting full DB rebuild from MaddenStats API...")

    # Log which endpoints we're skipping due to pre-loaded data
    if players:
        print(f"  [SKIP] /export/players — using {len(players)} rows from data_manager")
    if abilities:
        print(f"  [SKIP] /export/playerAbilities — using {len(abilities)} rows from data_manager")

    try:
        # Write to a temp file first — swap atomically at the end
        # so tsl_history.db is never in a half-built state
        tmp_path = DB_PATH + ".tmp"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        conn = sqlite3.connect(tmp_path)

        # ── Fetch all CSV exports (skip endpoints with pre-loaded data) ───────
        all_data: dict[str, list[dict]] = {}
        for endpoint, table_name in _CSV_EXPORTS:
            # Use pre-loaded data if caller provided it — skip the API call
            if endpoint == "/export/players" and players:
                all_data[table_name] = players
                continue
            if endpoint == "/export/playerAbilities" and abilities:
                all_data[table_name] = abilities
                continue

            rows = _fetch_csv_from_api(endpoint)
            if not rows:
                result["errors"].append(f"No data from {endpoint}")
            all_data[table_name] = rows

        # ── Load games (with winner transform) ────────────────────────────────
        game_rows = all_data.get("games_raw", [])
        n_games = _load_rows_into_table(conn, "games", game_rows, _transform_game)
        result["tables"]["games"] = n_games

        # ── Load all other tables straight ────────────────────────────────────
        for table_name in ["offensive_stats", "defensive_stats",
                           "standings", "teams", "trades", "players", "player_abilities"]:
            rows = all_data.get(table_name, [])
            n = _load_rows_into_table(conn, table_name, rows)
            result["tables"][table_name] = n

        # ── Build derived tables ──────────────────────────────────────────────
        if n_games > 0:
            ot, pdm = _build_derived_tables(conn)
            result["tables"]["owner_tenure"]   = ot
            result["tables"]["player_draft_map"] = pdm
        else:
            result["errors"].append("Skipped derived tables — no game rows loaded")

        # ── Add indexes ───────────────────────────────────────────────────────
        _add_indexes(conn)

        # ── Preserve non-API tables from the old DB ─────────────────────────
        # Other modules (setup_cog, build_member_db, codex_cog) store config
        # and state tables in tsl_history.db. The atomic swap would destroy
        # them, so we copy them into the new DB before swapping.
        _PRESERVE_TABLES = [
            "server_config", "guild_registry", "guild_roles", "guild_emojis",
            "tsl_members", "conversation_history", "balance_snapshots",
        ]
        if os.path.exists(DB_PATH):
            try:
                conn.execute("ATTACH DATABASE ? AS old_db", (DB_PATH,))
                old_tables = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM old_db.sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for tbl in _PRESERVE_TABLES:
                    if tbl in old_tables:
                        # Copy schema then data
                        schema_row = conn.execute(
                            "SELECT sql FROM old_db.sqlite_master WHERE type='table' AND name=?",
                            (tbl,)
                        ).fetchone()
                        if schema_row and schema_row[0]:
                            conn.execute(schema_row[0])
                            conn.execute(f"INSERT INTO main.{tbl} SELECT * FROM old_db.{tbl}")
                conn.commit()
                conn.execute("DETACH DATABASE old_db")
            except Exception as e:
                log.warning("[TSL-DB] Could not preserve non-API tables: %s", e)

        # Ensure all data is flushed to disk before the atomic swap
        conn.execute("PRAGMA synchronous = FULL")
        conn.commit()
        conn.close()

        # Clean up temp DB journal files before swap
        for suffix in ("-wal", "-shm", "-journal"):
            tmp_jrnl = tmp_path + suffix
            if os.path.exists(tmp_jrnl):
                try:
                    os.remove(tmp_jrnl)
                except OSError:
                    pass

        # Clean stale WAL/SHM from the OLD db BEFORE swapping, so new
        # connections never see orphaned WAL files referencing the wrong
        # db structure (which triggers WAL recovery + exclusive locks).
        for suffix in ("-wal", "-shm", "-journal"):
            stale = DB_PATH + suffix
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError as e:
                    log.warning("[TSL-DB] Could not remove stale %s: %s", stale, e)

        # ── Atomic swap (with retry for Windows file locking) ────────────────
        swapped = False
        for attempt in range(3):
            try:
                if os.path.exists(DB_PATH):
                    os.replace(tmp_path, DB_PATH)
                else:
                    os.rename(tmp_path, DB_PATH)
                swapped = True
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(1)
        if not swapped:
            # Fallback: copy over + remove tmp
            shutil.copy2(tmp_path, DB_PATH)
            os.remove(tmp_path)

        elapsed = round(time.time() - start, 1)
        result["success"]  = True
        result["elapsed"]  = elapsed
        result["games"]    = n_games
        result["players"]  = result["tables"].get("players", 0)

        print(
            f"[TSL-DB] ✅ Rebuild complete in {elapsed}s — "
            f"{n_games} games | {result['players']} players"
        )

    except Exception as e:
        result["errors"].append(f"Fatal: {e}")
        log.exception("[TSL-DB] Rebuild failed")
        # Clean up temp file if it exists
        if os.path.exists(DB_PATH + ".tmp"):
            try:
                os.remove(DB_PATH + ".tmp")
            except Exception:
                pass

    # Enable WAL mode for concurrent read/write access (best-effort,
    # outside try block so a lock here doesn't mark the rebuild as failed).
    # Note: explicit close() required — sqlite3's `with` only commits, it
    # does NOT close the connection, which would leak a shared lock.
    try:
        wal_conn = sqlite3.connect(DB_PATH, timeout=10)
        wal_conn.execute("PRAGMA journal_mode=WAL")
        wal_conn.close()
    except Exception:
        pass  # WAL will be set by _ensure_table() on first access

    result["elapsed"] = round(time.time() - start, 1)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  MANUAL BUILD  (run directly: python build_tsl_db.py)
# ─────────────────────────────────────────────────────────────────────────────

def build_db(csv_dir: str = None):
    """
    Build tsl_history.db from local CSV files.
    Use for bootstrapping or recovery when API is unavailable.
    """
    csv_dir = csv_dir or CSV_DIR
    print(f"Building {DB_PATH} from local CSVs in {csv_dir}...")
    conn = sqlite3.connect(DB_PATH)

    file_map = [
        ("games.csv",           "games",            _transform_game),
        ("offensive.csv",       "offensive_stats",  None),
        ("defensive.csv",       "defensive_stats",  None),
        ("teamStats.csv",       "team_stats",       None),
        ("standings.csv",       "standings",        None),
        ("teams.csv",           "teams",            None),
        ("trades.csv",          "trades",           None),
        ("players.csv",         "players",          None),
        ("playerAbilities.csv", "player_abilities", None),
    ]

    for filename, table_name, transform in file_map:
        filepath = os.path.join(csv_dir, filename)
        if not os.path.exists(filepath):
            print(f"  [SKIP] {filename} not found")
            continue
        with open(filepath, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if not rows:
            print(f"  [SKIP] {filename} is empty")
            continue
        n = _load_rows_into_table(conn, table_name, rows, transform)
        print(f"  [OK] {table_name}: {n} rows")

    _build_derived_tables(conn)
    _add_indexes(conn)
    conn.close()
    print(f"\n✅ Done! {DB_PATH} built from local CSVs.")


if __name__ == "__main__":
    build_db()
