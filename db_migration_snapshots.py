"""
db_migration_snapshots.py — Add balance_snapshots table + snapshot task
═══════════════════════════════════════════════════════════════════════════════
Run once to add the balance_snapshots table, then integrate the snapshot
task into your bot's event loop (or run as a scheduled cron).

Table: balance_snapshots
  - discord_id:    INTEGER  (user)
  - snapshot_date: TEXT     (YYYY-MM-DD)
  - balance:       INTEGER  (balance at snapshot time)
  - PRIMARY KEY (discord_id, snapshot_date)

Integration into bot.py:
    from db_migration_snapshots import setup_snapshots_table, take_daily_snapshot

    # In on_ready or a @tasks.loop(hours=24):
    setup_snapshots_table()
    take_daily_snapshot()
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sqlite3
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))


def setup_snapshots_table():
    """Create the balance_snapshots table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                discord_id    INTEGER NOT NULL,
                snapshot_date TEXT    NOT NULL,
                balance       INTEGER NOT NULL,
                PRIMARY KEY (discord_id, snapshot_date)
            )
        """)
        # Index for fast sparkline queries
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_user_date
            ON balance_snapshots (discord_id, snapshot_date)
        """)
    print("[ATLAS] balance_snapshots table ready.")


def take_daily_snapshot():
    """
    Snapshot every active user's balance for today.
    Uses INSERT OR REPLACE so it's safe to call multiple times per day.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as con:
        users = con.execute("SELECT discord_id, balance FROM users_table").fetchall()
        for discord_id, balance in users:
            con.execute("""
                INSERT OR REPLACE INTO balance_snapshots
                (discord_id, snapshot_date, balance)
                VALUES (?, ?, ?)
            """, (discord_id, today, balance))
        con.commit()
    print(f"[ATLAS] Daily snapshot taken for {len(users)} users ({today}).")


def backfill_from_bets():
    """
    Optional: Backfill approximate historical snapshots from bet settlement
    timestamps. This gives you sparkline data even before the snapshot system
    was installed. Run once.
    """
    with sqlite3.connect(DB_PATH) as con:
        users = con.execute("SELECT discord_id, balance FROM users_table").fetchall()

        for discord_id, current_balance in users:
            # Get all settled bets in chronological order
            bets = con.execute("""
                SELECT created_at, wager_amount, odds, status
                FROM bets_table
                WHERE discord_id = ? AND status IN ('Won', 'Lost', 'Push')
                AND parlay_id IS NULL
                ORDER BY created_at DESC
            """, (discord_id,)).fetchall()

            # Walk backwards from current balance
            running = current_balance
            snapshots = {}

            for created_at, wager, odds, status in bets:
                if created_at:
                    day = created_at[:10]  # YYYY-MM-DD
                    if day not in snapshots:
                        snapshots[day] = running

                    # Reverse the bet effect
                    if status == 'Won':
                        if odds > 0:
                            profit = int(wager * odds / 100)
                        else:
                            profit = int(wager * 100 / abs(odds))
                        running = running - profit  # Remove the profit
                    elif status == 'Lost':
                        running = running + wager  # Add back the lost wager

            # Insert snapshots
            for day, bal in snapshots.items():
                con.execute("""
                    INSERT OR IGNORE INTO balance_snapshots
                    (discord_id, snapshot_date, balance) VALUES (?, ?, ?)
                """, (discord_id, day, bal))

        con.commit()
    print(f"[ATLAS] Backfill complete for {len(users)} users.")


# ── Discord.py Tasks Integration ──────────────────────────────────────────────

DISCORD_TASK_TEMPLATE = '''
# Add this to your bot.py or a dedicated tasks cog:

from discord.ext import tasks
from db_migration_snapshots import setup_snapshots_table, take_daily_snapshot

class SnapshotTask(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        setup_snapshots_table()
        self.daily_snapshot.start()

    def cog_unload(self):
        self.daily_snapshot.cancel()

    @tasks.loop(hours=24)
    async def daily_snapshot(self):
        take_daily_snapshot()

    @daily_snapshot.before_loop
    async def before_snapshot(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(SnapshotTask(bot))
'''


if __name__ == "__main__":
    setup_snapshots_table()
    print("\nTo backfill historical data from existing bets, run:")
    print("  python db_migration_snapshots.py --backfill")

    import sys
    if "--backfill" in sys.argv:
        backfill_from_bets()
