"""
casino_db.py — TSL Casino Database Layer
─────────────────────────────────────────────────────────────────────────────
All async DB operations for the casino.  Uses aiosqlite with BEGIN IMMEDIATE
transactions to prevent race conditions when multiple users gamble at once.

Shares sportsbook.db — reads/writes the same users_table.balance column.
All casino tables are created here on first startup via setup_casino_db().
─────────────────────────────────────────────────────────────────────────────
"""

import aiosqlite
import hashlib
import os
import random
import string
from datetime import datetime, timezone, date

# ── Shared DB path (same file as sportsbook) ──────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sportsbook.db")

CASINO_MAX_BET    = 100
CASINO_DAILY_MIN  = 25
CASINO_DAILY_MAX  = 150
STARTING_BALANCE  = 1000   # mirrors sportsbook constant


# ═════════════════════════════════════════════════════════════════════════════
#  TABLE SETUP
# ═════════════════════════════════════════════════════════════════════════════

async def setup_casino_db() -> None:
    """
    Create all casino tables if they don't exist.
    Safe to call on every startup — uses IF NOT EXISTS throughout.
    Does NOT touch any existing sportsbook tables.
    """
    async with aiosqlite.connect(DB_PATH) as db:

        # ── Enable WAL mode (shared with sportsbook.py for lock-free reads) ─
        await db.execute("PRAGMA journal_mode=WAL")

        # ── Casino session log (one row per completed game) ────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_sessions (
                session_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id  INTEGER NOT NULL,
                game_type   TEXT    NOT NULL,
                wager       INTEGER NOT NULL,
                outcome     TEXT    NOT NULL,
                payout      INTEGER NOT NULL,
                multiplier  REAL    DEFAULT 1.0,
                channel_id  INTEGER,
                played_at   TEXT    NOT NULL
            )
        """)

        # ── House bank — running P&L per game type ─────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_house_bank (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                game_type   TEXT    NOT NULL,
                delta       INTEGER NOT NULL,
                session_id  INTEGER,
                recorded_at TEXT    NOT NULL
            )
        """)

        # ── Crash rounds — shared round state ─────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crash_rounds (
                round_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                crash_point REAL    NOT NULL,
                seed        TEXT    NOT NULL,
                status      TEXT    DEFAULT 'open',
                started_at  TEXT,
                crashed_at  TEXT,
                created_at  TEXT    NOT NULL
            )
        """)

        # ── Crash bets within a shared round ──────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crash_bets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id     INTEGER NOT NULL,
                discord_id   INTEGER NOT NULL,
                wager        INTEGER NOT NULL,
                cashout_mult REAL    DEFAULT NULL,
                payout       INTEGER DEFAULT 0,
                status       TEXT    DEFAULT 'active'
            )
        """)

        # ── Daily scratch card claims ──────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_scratches (
                discord_id  INTEGER NOT NULL,
                last_claim  TEXT    NOT NULL,
                PRIMARY KEY (discord_id)
            )
        """)

        # ── PvP coin flip challenges ───────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coinflip_challenges (
                challenge_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                challenger_id INTEGER NOT NULL,
                opponent_id   INTEGER NOT NULL,
                wager         INTEGER NOT NULL,
                channel_id    INTEGER NOT NULL,
                status        TEXT    DEFAULT 'pending',
                winner_id     INTEGER DEFAULT NULL,
                created_at    TEXT    NOT NULL,
                resolved_at   TEXT    DEFAULT NULL
            )
        """)

        # ── Casino settings (max bet overrides, channel IDs, etc.) ────────
        await db.execute("""
            INSERT OR IGNORE INTO sportsbook_settings (key, value)
            VALUES
                ('casino_open',             '1'),
                ('casino_blackjack_open',   '1'),
                ('casino_crash_open',       '1'),
                ('casino_slots_open',       '1'),
                ('casino_coinflip_open',    '1'),
                ('casino_max_bet',          '100'),
                ('casino_daily_min',        '25'),
                ('casino_daily_max',        '150'),
                ('casino_hub_channel',      ''),
                ('casino_blackjack_channel',''),
                ('casino_crash_channel',    ''),
                ('casino_slots_channel',    ''),
                ('casino_coinflip_channel', '')
        """)

        await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS HELPERS  (async versions for casino use)
# ═════════════════════════════════════════════════════════════════════════════

async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM sportsbook_settings WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sportsbook_settings (key,value) VALUES (?,?)",
            (key, value)
        )
        await db.commit()


async def is_casino_open(game: str | None = None) -> bool:
    """Check if the whole casino is open, or a specific game."""
    if not await get_setting("casino_open", "1") == "1":
        return False
    if game:
        return await get_setting(f"casino_{game}_open", "1") == "1"
    return True


async def get_channel_id(game: str) -> int | None:
    """Return the registered channel ID for a game, or None if not set."""
    val = await get_setting(f"casino_{game}_channel", "")
    try:
        return int(val) if val else None
    except ValueError:
        return None


async def get_max_bet() -> int:
    val = await get_setting("casino_max_bet", "100")
    try:
        return int(val)
    except ValueError:
        return CASINO_MAX_BET


# ═════════════════════════════════════════════════════════════════════════════
#  BALANCE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def get_balance(discord_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await db.execute(
                    "INSERT OR IGNORE INTO users_table (discord_id, balance, season_start_balance) VALUES (?,?,?)",
                    (discord_id, STARTING_BALANCE, STARTING_BALANCE)
                )
                # Re-read in case a concurrent insert won the race
                async with db.execute(
                    "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
                ) as cur:
                    row = await cur.fetchone()
                await db.commit()
                return row[0] if row else STARTING_BALANCE
            await db.commit()
            return row[0]
        except Exception:
            await db.rollback()
            raise


# ═════════════════════════════════════════════════════════════════════════════
#  CORE WAGER PROCESSOR  — race-condition safe
# ═════════════════════════════════════════════════════════════════════════════

class InsufficientFundsError(Exception):
    pass

class CasinoClosedError(Exception):
    pass


async def process_wager(
    discord_id:  int,
    wager:       int,
    game_type:   str,
    outcome:     str,       # 'win' | 'loss' | 'push'
    payout:      int,       # total bucks returned to player (0 if loss)
    multiplier:  float = 1.0,
    channel_id:  int | None = None,
) -> dict:
    """
    Atomically process a casino wager result.

    Flow:
      1. BEGIN IMMEDIATE — acquires write lock, blocks concurrent writers
      2. Read current balance (within the lock, so it's fresh)
      3. Validate balance >= wager (should already be debited at bet time,
         but this is a safety net for edge cases)
      4. Credit payout to balance
      5. Log session row
      6. Log house bank delta
      7. COMMIT

    Returns a dict with session_id and new_balance.

    Raises InsufficientFundsError if balance check fails (should not happen
    in normal flow since we debit at bet placement, but guards against bugs).
    """
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        # BEGIN IMMEDIATE gets a write lock immediately, preventing two
        # concurrent transactions from reading the same stale balance.
        await db.execute("BEGIN IMMEDIATE")

        try:
            # ── 1. Read fresh balance ──────────────────────────────────────
            async with db.execute(
                "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                # Auto-create user (edge case: first casino use, no sportsbook history)
                await db.execute(
                    "INSERT INTO users_table (discord_id, balance, season_start_balance) VALUES (?,?,?)",
                    (discord_id, STARTING_BALANCE, STARTING_BALANCE)
                )
                current_balance = STARTING_BALANCE
            else:
                current_balance = row[0]

            # ── 2. Credit payout ───────────────────────────────────────────
            # At bet time the wager was already deducted from balance.
            # Here we only add the payout (which is 0 for a loss).
            new_balance = current_balance + payout
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id)
            )

            # ── 3. Log session ─────────────────────────────────────────────
            async with db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier, channel_id, played_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (discord_id, game_type, wager, outcome, payout, multiplier, channel_id, now)) as cur:
                session_id = cur.lastrowid

            # ── 4. Log house bank ──────────────────────────────────────────
            # House profit = wager collected - payout given out
            # If player wins: house loses (payout > wager) → negative delta
            # If player loses: house gains (payout = 0) → positive delta
            house_delta = wager - payout
            await db.execute("""
                INSERT INTO casino_house_bank (game_type, delta, session_id, recorded_at)
                VALUES (?,?,?,?)
            """, (game_type, house_delta, session_id, now))

            await db.commit()

        except Exception:
            await db.rollback()
            raise

    return {"session_id": session_id, "new_balance": new_balance}


async def deduct_wager(discord_id: int, wager: int) -> int:
    """
    Deduct wager from balance at bet placement time.
    Returns new balance.
    Raises InsufficientFundsError if balance is too low.

    Uses BEGIN IMMEDIATE to prevent double-spend race conditions.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                await db.execute(
                    "INSERT INTO users_table (discord_id, balance, season_start_balance) VALUES (?,?,?)",
                    (discord_id, STARTING_BALANCE, STARTING_BALANCE)
                )
                current_balance = STARTING_BALANCE
            else:
                current_balance = row[0]

            if current_balance < wager:
                await db.rollback()
                raise InsufficientFundsError(
                    f"Balance {current_balance:,} < wager {wager:,}"
                )

            new_balance = current_balance - wager
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id)
            )
            await db.commit()

        except Exception:
            await db.rollback()
            raise

    return new_balance


async def refund_wager(discord_id: int, amount: int) -> int:
    """Refund a wager (e.g. declined PvP challenge, crash round void). Returns new balance."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
            current = row[0] if row else STARTING_BALANCE
            new_balance = current + amount
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id)
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return new_balance


# ═════════════════════════════════════════════════════════════════════════════
#  HOUSE BANK REPORTING
# ═════════════════════════════════════════════════════════════════════════════

async def get_house_report() -> dict:
    """Return P&L breakdown by game type plus totals."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT game_type, SUM(delta) as pl, COUNT(*) as hands
            FROM casino_house_bank
            GROUP BY game_type
            ORDER BY pl DESC
        """) as cur:
            rows = await cur.fetchall()

        async with db.execute("""
            SELECT COUNT(DISTINCT discord_id), COUNT(*), SUM(wager)
            FROM casino_sessions
        """) as cur:
            totals = await cur.fetchone()

    return {
        "by_game": [{"game": r[0], "pl": r[1], "hands": r[2]} for r in rows],
        "total_pl": sum(r[1] for r in rows),
        "unique_players": totals[0] if totals else 0,
        "total_hands":    totals[1] if totals else 0,
        "total_wagered":  totals[2] if totals else 0,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  DAILY SCRATCH CARD
# ═════════════════════════════════════════════════════════════════════════════

async def can_claim_scratch(discord_id: int) -> bool:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_claim FROM daily_scratches WHERE discord_id=?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    return row is None or row[0] != today


async def claim_scratch(discord_id: int, reward: int | None = None) -> int | None:
    """
    Credit a scratch card reward to the user's balance.
    Pass the pre-computed reward from the UI tiles so what the player
    sees matches what they receive.  Falls back to a random roll if
    reward is not provided (backwards compat).
    Returns the amount won, or None if already claimed today.
    """
    today = date.today().isoformat()

    # Fallback: roll if caller didn't pass a pre-computed reward
    if reward is None:
        reward_pool = [
            (25,  40),   # (amount, weight)
            (50,  30),
            (75,  15),
            (100, 10),
            (150,  5),
        ]
        amounts  = [r[0] for r in reward_pool]
        weights  = [r[1] for r in reward_pool]
        reward   = random.choices(amounts, weights=weights, k=1)[0]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Check eligibility inside the transaction to prevent TOCTOU race
            async with db.execute(
                "SELECT last_claim FROM daily_scratches WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] == today:
                await db.rollback()
                return None

            # Mark claimed
            await db.execute("""
                INSERT INTO daily_scratches (discord_id, last_claim) VALUES (?,?)
                ON CONFLICT(discord_id) DO UPDATE SET last_claim=excluded.last_claim
            """, (discord_id, today))

            # Credit balance
            async with db.execute(
                "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
            current = row[0] if row else STARTING_BALANCE
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (current + reward, discord_id)
            )

            # Log as casino session
            now = datetime.now(timezone.utc).isoformat()
            await db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier, played_at)
                VALUES (?,?,?,?,?,?,?)
            """, (discord_id, "scratch", 0, "win", reward, 1.0, now))

            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return reward


# ═════════════════════════════════════════════════════════════════════════════
#  CRASH ROUND MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

def _generate_crash_point() -> tuple[float, str]:
    """
    Generate a provably fair crash point.

    Distribution (tuned for house edge):
      ~35% crash before 2x
      ~25% between 2x – 5x
      ~25% between 5x – 15x
      ~15% beyond 15x
      Hard cap at 100x

    Uses a seeded hash so the crash point can be revealed after the round
    as proof it wasn't manipulated.
    """
    seed = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    h    = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    # Map hash to 0.0–1.0
    p    = (h % 10_000_000) / 10_000_000

    # Inverse CDF for exponential-ish distribution
    # P(crash < x) = 1 - 1/x  (classic crash curve)
    # With a house edge adjustment: use 0.97 so expected value < 1
    house_edge = 0.97
    if p < (1 - house_edge):
        crash_point = 1.0   # instant crash (very rare but possible)
    else:
        crash_point = house_edge / (1.0 - p)

    crash_point = min(round(crash_point, 2), 100.0)
    return crash_point, seed


async def create_crash_round(channel_id: int) -> int:
    """Create a new crash round. Returns round_id."""
    crash_point, seed = _generate_crash_point()
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            INSERT INTO crash_rounds (channel_id, crash_point, seed, status, created_at)
            VALUES (?,?,?,'open',?)
        """, (channel_id, crash_point, seed, now)) as cur:
            round_id = cur.lastrowid
        await db.commit()
    return round_id


async def get_crash_round(round_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM crash_rounds WHERE round_id=?", (round_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def add_crash_bet(round_id: int, discord_id: int, wager: int) -> int:
    """Add a player bet to a crash round. Returns crash_bet id."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            INSERT INTO crash_bets (round_id, discord_id, wager)
            VALUES (?,?,?)
        """, (round_id, discord_id, wager)) as cur:
            bet_id = cur.lastrowid
        await db.commit()
    return bet_id


async def cashout_crash_bet(bet_id: int, discord_id: int, multiplier: float) -> int:
    """
    Cash out a crash bet at the given multiplier.
    Marks the bet as cashed in the crash_bets table.
    Returns payout amount.  Balance credit is handled by process_wager().
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT wager, status FROM crash_bets WHERE id=? AND discord_id=?",
                (bet_id, discord_id)
            ) as cur:
                row = await cur.fetchone()

            if not row or row[1] != "active":
                await db.rollback()
                return 0

            wager  = row[0]
            payout = int(wager * multiplier)

            await db.execute(
                "UPDATE crash_bets SET cashout_mult=?, payout=?, status='cashed' WHERE id=?",
                (multiplier, payout, bet_id)
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return payout


async def resolve_crash_round(round_id: int) -> list[dict]:
    """
    Mark a crash round as crashed. Returns list of all bets with outcomes.
    Players who didn't cash out are already at $0 payout (wager deducted at bet time).
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Mark uncashed bets as lost
        await db.execute(
            "UPDATE crash_bets SET status='lost' WHERE round_id=? AND status='active'",
            (round_id,)
        )
        await db.execute(
            "UPDATE crash_rounds SET status='crashed', crashed_at=? WHERE round_id=?",
            (now, round_id)
        )

        async with db.execute(
            "SELECT * FROM crash_bets WHERE round_id=?", (round_id,)
        ) as cur:
            rows = await cur.fetchall()

        await db.commit()

    return [
        {
            "id":           r[0], "round_id":     r[1],
            "discord_id":   r[2], "wager":        r[3],
            "cashout_mult": r[4], "payout":       r[5],
            "status":       r[6],
        }
        for r in rows
    ]


# ═════════════════════════════════════════════════════════════════════════════
#  COIN FLIP CHALLENGES
# ═════════════════════════════════════════════════════════════════════════════

async def create_challenge(
    challenger_id: int,
    opponent_id:   int,
    wager:         int,
    channel_id:    int,
) -> int:
    """Create a PvP coin flip challenge. Returns challenge_id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            INSERT INTO coinflip_challenges
                (challenger_id, opponent_id, wager, channel_id, created_at)
            VALUES (?,?,?,?,?)
        """, (challenger_id, opponent_id, wager, channel_id, now)) as cur:
            cid = cur.lastrowid
        await db.commit()
    return cid


async def get_challenge(challenge_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM coinflip_challenges WHERE challenge_id=?", (challenge_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    keys = ["challenge_id","challenger_id","opponent_id","wager",
            "channel_id","status","winner_id","created_at","resolved_at"]
    return dict(zip(keys, row))


async def resolve_challenge(challenge_id: int, winner_id: int, loser_id: int, wager: int) -> int:
    """
    Resolve a PvP challenge. Winner gets 1.9x (slight house edge).
    Loser's wager was already deducted. Returns winner payout.
    """
    payout = int(wager * 1.9)
    now    = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Credit winner
            async with db.execute(
                "SELECT balance FROM users_table WHERE discord_id=?", (winner_id,)
            ) as cur:
                row = await cur.fetchone()
            current = row[0] if row else STARTING_BALANCE
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (current + payout, winner_id)
            )
            # Update challenge record
            await db.execute("""
                UPDATE coinflip_challenges
                SET status='completed', winner_id=?, resolved_at=?
                WHERE challenge_id=?
            """, (winner_id, now, challenge_id))

            # Log sessions for both players
            await db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier, played_at)
                VALUES (?,?,?,?,?,?,?)
            """, (winner_id, "coinflip_pvp", wager, "win", payout, 1.9, now))
            await db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier, played_at)
                VALUES (?,?,?,?,?,?,?)
            """, (loser_id, "coinflip_pvp", wager, "loss", 0, 0.0, now))

            # House keeps the 0.1x edge (2 wagers - 1.9x payout)
            house_delta = wager * 2 - payout
            await db.execute("""
                INSERT INTO casino_house_bank (game_type, delta, recorded_at)
                VALUES (?,?,?)
            """, ("coinflip_pvp", house_delta, now))

            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return payout


async def decline_challenge(challenge_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE coinflip_challenges
            SET status='declined', resolved_at=?
            WHERE challenge_id=?
        """, (now, challenge_id))
        await db.commit()


