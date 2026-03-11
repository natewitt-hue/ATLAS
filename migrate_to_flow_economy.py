"""
migrate_to_flow_economy.py — ATLAS Flow Economy DB Migration
-------------------------------------------------------------
One-time script to create flow_economy.db from sportsbook.db.
Hardcodes all table schemas with strict types. Does NOT use
sqlite_master introspection. Copies data via INSERT INTO ... SELECT.

Usage:
    python migrate_to_flow_economy.py            # run migration
    python migrate_to_flow_economy.py --dry-run  # validate only
-------------------------------------------------------------
"""

import os
import sqlite3
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_PATH = os.path.join(_DIR, "sportsbook.db")
DEST_PATH = os.path.join(_DIR, "flow_economy.db")


# ===========================================================================
#  LEGACY TABLE SCHEMAS (hardcoded, strict types)
# ===========================================================================

LEGACY_TABLES = {
    "users_table": """
        CREATE TABLE users_table (
            discord_id           INTEGER PRIMARY KEY,
            balance              INTEGER NOT NULL DEFAULT 1000,
            season_start_balance INTEGER NOT NULL DEFAULT 1000
        )
    """,
    "bets_table": """
        CREATE TABLE bets_table (
            bet_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id   INTEGER NOT NULL,
            week         INTEGER NOT NULL,
            matchup      TEXT    NOT NULL,
            bet_type     TEXT    NOT NULL,
            wager_amount INTEGER NOT NULL,
            odds         INTEGER NOT NULL,
            pick         TEXT    NOT NULL,
            line         REAL    DEFAULT 0.0,
            status       TEXT    NOT NULL DEFAULT 'Pending',
            parlay_id    TEXT    DEFAULT NULL,
            created_at   TEXT    DEFAULT (datetime('now'))
        )
    """,
    "parlays_table": """
        CREATE TABLE parlays_table (
            parlay_id     TEXT    PRIMARY KEY,
            discord_id    INTEGER NOT NULL,
            week          INTEGER NOT NULL,
            legs          TEXT    NOT NULL,
            combined_odds INTEGER NOT NULL,
            wager_amount  INTEGER NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'Pending',
            created_at    TEXT    DEFAULT (datetime('now'))
        )
    """,
    "games_state": """
        CREATE TABLE games_state (
            game_id TEXT    PRIMARY KEY,
            locked  INTEGER NOT NULL DEFAULT 0
        )
    """,
    "line_overrides": """
        CREATE TABLE line_overrides (
            game_id     TEXT PRIMARY KEY,
            home_spread REAL    DEFAULT NULL,
            away_spread REAL    DEFAULT NULL,
            home_ml     INTEGER DEFAULT NULL,
            away_ml     INTEGER DEFAULT NULL,
            ou_line     REAL    DEFAULT NULL,
            set_by      TEXT    DEFAULT NULL,
            set_at      TEXT    DEFAULT (datetime('now'))
        )
    """,
    "prop_bets": """
        CREATE TABLE prop_bets (
            prop_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            week        INTEGER NOT NULL,
            description TEXT    NOT NULL,
            option_a    TEXT    NOT NULL,
            option_b    TEXT    NOT NULL,
            odds_a      INTEGER NOT NULL DEFAULT -110,
            odds_b      INTEGER NOT NULL DEFAULT -110,
            status      TEXT    NOT NULL DEFAULT 'Open',
            result      TEXT    DEFAULT NULL,
            created_by  TEXT    DEFAULT NULL,
            created_at  TEXT    DEFAULT (datetime('now'))
        )
    """,
    "prop_wagers": """
        CREATE TABLE prop_wagers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            prop_id      INTEGER NOT NULL,
            discord_id   INTEGER NOT NULL,
            pick         TEXT    NOT NULL,
            wager_amount INTEGER NOT NULL,
            odds         INTEGER NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'Pending',
            placed_at    TEXT    DEFAULT (datetime('now'))
        )
    """,
    "sportsbook_settings": """
        CREATE TABLE sportsbook_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """,
    # -- Casino tables ---------------------------------------------
    "casino_sessions": """
        CREATE TABLE casino_sessions (
            session_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id  INTEGER NOT NULL,
            game_type   TEXT    NOT NULL,
            wager       INTEGER NOT NULL,
            outcome     TEXT    NOT NULL,
            payout      INTEGER NOT NULL,
            multiplier  REAL    NOT NULL DEFAULT 1.0,
            channel_id  INTEGER,
            played_at   TEXT    NOT NULL
        )
    """,
    "casino_house_bank": """
        CREATE TABLE casino_house_bank (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_type   TEXT    NOT NULL,
            delta       INTEGER NOT NULL,
            session_id  INTEGER,
            recorded_at TEXT    NOT NULL
        )
    """,
    "crash_rounds": """
        CREATE TABLE crash_rounds (
            round_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id  INTEGER NOT NULL,
            crash_point REAL    NOT NULL,
            seed        TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'open',
            started_at  TEXT,
            crashed_at  TEXT,
            created_at  TEXT    NOT NULL
        )
    """,
    "crash_bets": """
        CREATE TABLE crash_bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id     INTEGER NOT NULL,
            discord_id   INTEGER NOT NULL,
            wager        INTEGER NOT NULL,
            cashout_mult REAL    DEFAULT NULL,
            payout       INTEGER NOT NULL DEFAULT 0,
            status       TEXT    NOT NULL DEFAULT 'active'
        )
    """,
    "daily_scratches": """
        CREATE TABLE daily_scratches (
            discord_id INTEGER PRIMARY KEY,
            last_claim  TEXT    NOT NULL
        )
    """,
    "coinflip_challenges": """
        CREATE TABLE coinflip_challenges (
            challenge_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_id INTEGER NOT NULL,
            opponent_id   INTEGER NOT NULL,
            wager         INTEGER NOT NULL,
            channel_id    INTEGER NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'pending',
            winner_id     INTEGER DEFAULT NULL,
            created_at    TEXT    NOT NULL,
            resolved_at   TEXT    DEFAULT NULL
        )
    """,
    # -- Economy tables --------------------------------------------
    "economy_stipends": """
        CREATE TABLE economy_stipends (
            stipend_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type TEXT    NOT NULL,
            target_id   INTEGER NOT NULL,
            amount      INTEGER NOT NULL,
            interval    TEXT    NOT NULL,
            last_paid   TEXT,
            created_by  INTEGER NOT NULL,
            reason      TEXT    NOT NULL DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1
        )
    """,
    "economy_log": """
        CREATE TABLE economy_log (
            log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id  INTEGER NOT NULL,
            action      TEXT    NOT NULL,
            amount      INTEGER NOT NULL,
            old_balance INTEGER,
            new_balance INTEGER,
            reason      TEXT    NOT NULL DEFAULT '',
            admin_id    INTEGER,
            logged_at   TEXT    NOT NULL
        )
    """,
    # -- Prediction market tables ----------------------------------
    "prediction_markets": """
        CREATE TABLE prediction_markets (
            market_id   TEXT PRIMARY KEY,
            event_id    TEXT,
            slug        TEXT NOT NULL,
            title       TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'Other',
            yes_price   REAL NOT NULL DEFAULT 0.5,
            no_price    REAL NOT NULL DEFAULT 0.5,
            volume      REAL NOT NULL DEFAULT 0,
            liquidity   REAL NOT NULL DEFAULT 0,
            volume_24hr REAL NOT NULL DEFAULT 0,
            featured    REAL NOT NULL DEFAULT 0,
            end_date    TEXT,
            status      TEXT NOT NULL DEFAULT 'active',
            result      TEXT,
            resolved_by TEXT NOT NULL DEFAULT 'pending',
            last_synced TEXT
        )
    """,
    "prediction_contracts": """
        CREATE TABLE prediction_contracts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            market_id        TEXT    NOT NULL,
            slug             TEXT    NOT NULL,
            side             TEXT    NOT NULL CHECK(side IN ('YES','NO')),
            buy_price        REAL    NOT NULL,
            quantity         INTEGER NOT NULL DEFAULT 1,
            cost_bucks       INTEGER NOT NULL,
            potential_payout INTEGER NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'open'
                             CHECK(status IN ('open','won','lost','voided')),
            created_at       TEXT    NOT NULL,
            resolved_at      TEXT,
            FOREIGN KEY (market_id) REFERENCES prediction_markets(market_id)
        )
    """,
    # -- Affinity table --------------------------------------------
    "user_affinity": """
        CREATE TABLE user_affinity (
            discord_id        INTEGER PRIMARY KEY,
            affinity_score    REAL    NOT NULL DEFAULT 0.0,
            interaction_count INTEGER NOT NULL DEFAULT 0,
            last_interaction  TEXT,
            notes             TEXT    NOT NULL DEFAULT ''
        )
    """,
}


# ===========================================================================
#  NEW TABLES (flow_wallet + real sportsbook)
# ===========================================================================

NEW_TABLES = {
    "transactions": """
        CREATE TABLE transactions (
            txn_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id    INTEGER NOT NULL,
            amount        INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            source        TEXT    NOT NULL,
            reference_key TEXT    UNIQUE DEFAULT NULL,
            description   TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL
        )
    """,
    "real_events": """
        CREATE TABLE real_events (
            event_id      TEXT PRIMARY KEY,
            sport_key     TEXT NOT NULL,
            sport_title   TEXT NOT NULL,
            home_team     TEXT NOT NULL,
            away_team     TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            home_score    INTEGER DEFAULT NULL,
            away_score    INTEGER DEFAULT NULL,
            completed     INTEGER NOT NULL DEFAULT 0,
            locked        INTEGER NOT NULL DEFAULT 0,
            last_odds_sync  TEXT,
            last_score_sync TEXT
        )
    """,
    "real_odds": """
        CREATE TABLE real_odds (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT    NOT NULL,
            bookmaker    TEXT    NOT NULL,
            market       TEXT    NOT NULL,
            outcome_name TEXT    NOT NULL,
            price        INTEGER NOT NULL,
            point        REAL    DEFAULT NULL,
            last_updated TEXT    NOT NULL,
            UNIQUE(event_id, bookmaker, market, outcome_name)
        )
    """,
    "real_bets": """
        CREATE TABLE real_bets (
            bet_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id   INTEGER NOT NULL,
            event_id     TEXT    NOT NULL,
            sport_key    TEXT    NOT NULL,
            bet_type     TEXT    NOT NULL,
            pick         TEXT    NOT NULL,
            odds         INTEGER NOT NULL,
            line         REAL    DEFAULT NULL,
            wager_amount INTEGER NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'Pending',
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (event_id) REFERENCES real_events(event_id)
        )
    """,
}


# ===========================================================================
#  INDEXES
# ===========================================================================

INDEXES = [
    # Transactions
    "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(discord_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tx_ref  ON transactions(reference_key)",
    # Prediction markets
    "CREATE INDEX IF NOT EXISTS idx_pred_contracts_user   ON prediction_contracts(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_pred_contracts_market ON prediction_contracts(market_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_pred_markets_status   ON prediction_markets(status, category)",
    # Real sportsbook
    "CREATE INDEX IF NOT EXISTS idx_real_bets_user  ON real_bets(discord_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_real_bets_event ON real_bets(event_id, status)",
]


# ===========================================================================
#  SPECIAL-CASE DATA COPIES (tables with schema changes)
# ===========================================================================

# prediction_contracts: user_id TEXT -> INTEGER
SPECIAL_COPIES = {
    "prediction_contracts": """
        INSERT INTO prediction_contracts
            (id, user_id, market_id, slug, side, buy_price, quantity,
             cost_bucks, potential_payout, status, created_at, resolved_at)
        SELECT
            id, CAST(user_id AS INTEGER), market_id, slug, side, buy_price, quantity,
            cost_bucks, potential_payout, status, created_at, resolved_at
        FROM source.prediction_contracts
    """,
}


# ===========================================================================
#  MIGRATION LOGIC
# ===========================================================================

def migrate(dry_run: bool = False) -> dict:
    """
    Create flow_economy.db from sportsbook.db with strict schemas.

    Returns {"success": bool, "tables": int, "rows": int, "errors": list}
    """
    result = {"success": False, "tables": 0, "rows": 0, "errors": []}

    # -- Pre-checks ------------------------------------------------
    if not os.path.exists(SOURCE_PATH):
        result["errors"].append(f"Source DB not found: {SOURCE_PATH}")
        return result

    if os.path.exists(DEST_PATH):
        result["errors"].append(
            f"Destination already exists: {DEST_PATH} — "
            f"delete it manually if you want to re-run migration"
        )
        return result

    if dry_run:
        print("[DRY RUN] Would create flow_economy.db — no changes made.")
        result["success"] = True
        return result

    # -- Create dest DB --------------------------------------------
    dest = sqlite3.connect(DEST_PATH)
    dest.execute("PRAGMA journal_mode=WAL")
    dest.execute("PRAGMA foreign_keys=ON")

    # Attach source
    dest.execute(f"ATTACH DATABASE ? AS source", (SOURCE_PATH,))

    try:
        # -- Create all legacy tables with strict schemas ----------
        for name, ddl in LEGACY_TABLES.items():
            dest.execute(ddl)
            result["tables"] += 1

        # -- Create new tables ------------------------------------
        for name, ddl in NEW_TABLES.items():
            dest.execute(ddl)
            result["tables"] += 1

        # -- Create indexes ---------------------------------------
        for idx_sql in INDEXES:
            dest.execute(idx_sql)

        dest.commit()

        # -- Copy data from source --------------------------------
        # Get list of tables that actually exist in source
        source_tables = {
            row[0]
            for row in dest.execute(
                "SELECT name FROM source.sqlite_master WHERE type='table'"
            ).fetchall()
        }

        for name in LEGACY_TABLES:
            if name not in source_tables:
                print(f"  [SKIP] {name} — not in source DB")
                continue

            if name in SPECIAL_COPIES:
                # Use special copy with type conversion
                dest.execute(SPECIAL_COPIES[name])
            else:
                # Simple copy — all columns match
                # Get column names from dest schema
                cols = [
                    row[1]
                    for row in dest.execute(f"PRAGMA table_info({name})").fetchall()
                ]

                # Get column names from source to find intersection
                source_cols = {
                    row[1]
                    for row in dest.execute(
                        f"PRAGMA source.table_info({name})"
                    ).fetchall()
                }

                # Only copy columns that exist in both source and dest
                shared_cols = [c for c in cols if c in source_cols]
                col_list = ", ".join(shared_cols)

                dest.execute(
                    f"INSERT INTO {name} ({col_list}) "
                    f"SELECT {col_list} FROM source.{name}"
                )

            count = dest.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            result["rows"] += count
            print(f"  [OK] {name}: {count:,} rows")

        dest.commit()

        # -- Seed sportsbook_settings defaults if empty -----------
        existing_keys = dest.execute(
            "SELECT COUNT(*) FROM sportsbook_settings"
        ).fetchone()[0]
        if existing_keys == 0:
            defaults = [
                ("casino_open", "1"),
                ("casino_blackjack_open", "1"),
                ("casino_crash_open", "1"),
                ("casino_slots_open", "1"),
                ("casino_coinflip_open", "1"),
                ("casino_max_bet", "100"),
                ("casino_daily_min", "25"),
                ("casino_daily_max", "150"),
                ("casino_hub_channel", ""),
                ("casino_blackjack_channel", ""),
                ("casino_crash_channel", ""),
                ("casino_slots_channel", ""),
                ("casino_coinflip_channel", ""),
            ]
            dest.executemany(
                "INSERT OR IGNORE INTO sportsbook_settings (key, value) VALUES (?, ?)",
                defaults,
            )
            dest.commit()
            print(f"  [OK] sportsbook_settings: seeded {len(defaults)} defaults")

        # -- Validate row counts ----------------------------------
        print("\n-- Validation --")
        mismatches = []
        for name in LEGACY_TABLES:
            if name not in source_tables:
                continue

            src_count = dest.execute(
                f"SELECT COUNT(*) FROM source.{name}"
            ).fetchone()[0]
            dst_count = dest.execute(
                f"SELECT COUNT(*) FROM {name}"
            ).fetchone()[0]

            status = "OK" if src_count == dst_count else "MISMATCH"
            if status == "MISMATCH":
                mismatches.append(f"{name}: source={src_count} dest={dst_count}")
            print(f"  [{status}] {name}: {src_count} -> {dst_count}")

        if mismatches:
            result["errors"].extend(mismatches)
            print(f"\nWARNING:  {len(mismatches)} row count mismatch(es)!")
        else:
            print("\nOK: All row counts match.")

        result["success"] = len(mismatches) == 0

    except Exception as e:
        result["errors"].append(str(e))
        import traceback
        traceback.print_exc()
        # Clean up partial file on failure
        dest.close()
        if os.path.exists(DEST_PATH):
            os.remove(DEST_PATH)
            print(f"\nFAILED: Migration failed — removed partial {DEST_PATH}")
        return result

    finally:
        try:
            dest.execute("DETACH DATABASE source")
        except Exception:
            pass
        dest.close()

    # -- Summary --------------------------------------------------
    print(f"\n{'=' * 50}")
    print(f"Migration {'COMPLETE' if result['success'] else 'FAILED'}")
    print(f"Tables: {result['tables']}  |  Rows: {result['rows']:,}")
    if result["errors"]:
        print(f"Errors: {result['errors']}")
    print(f"Source: {SOURCE_PATH}")
    print(f"Dest:   {DEST_PATH}")
    print(f"{'=' * 50}")

    return result


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    migrate(dry_run=dry)
