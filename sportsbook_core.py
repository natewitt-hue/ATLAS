# sportsbook_core.py
import os, asyncio, json, logging
from datetime import datetime, timezone
import aiosqlite
import discord
from discord.ext import tasks
import flow_wallet
import wager_registry
from odds_utils import payout_calc as _payout_calc

log = logging.getLogger("sportsbook_core")

_bot = None  # injected via init() at cog load

_DIR = os.path.dirname(os.path.abspath(__file__))
OLD_SB_DB = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
FLOW_DB = os.path.join(_DIR, "flow.db")

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('TSL','REAL','POLY')),
    status TEXT NOT NULL DEFAULT 'scheduled'
           CHECK(status IN ('scheduled','live','final','cancelled')),
    home_participant TEXT, away_participant TEXT,
    home_score REAL, away_score REAL,
    result_payload TEXT,
    commence_ts TEXT, finalized_ts TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    bet_type TEXT NOT NULL CHECK(bet_type IN ('Moneyline','Spread','Over','Under','Prediction')),
    pick TEXT NOT NULL, line REAL, odds INTEGER NOT NULL, wager_amount INTEGER NOT NULL,
    status TEXT DEFAULT 'Pending'
           CHECK(status IN ('Pending','Won','Lost','Push','Cancelled','Error')),
    parlay_id TEXT REFERENCES parlays(parlay_id),
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS parlays (
    parlay_id TEXT PRIMARY KEY, discord_id INTEGER NOT NULL,
    combined_odds INTEGER NOT NULL, wager_amount INTEGER NOT NULL,
    status TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Won','Lost','Push','Cancelled')),
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS parlay_legs (
    leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id TEXT NOT NULL REFERENCES parlays(parlay_id),
    bet_id INTEGER NOT NULL REFERENCES bets(bet_id),
    leg_index INTEGER NOT NULL, UNIQUE(parlay_id, leg_index)
);

CREATE TABLE IF NOT EXISTS event_locks (event_id TEXT PRIMARY KEY REFERENCES events(event_id), locked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS event_line_overrides (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    home_spread REAL, away_spread REAL, home_ml INTEGER, away_ml INTEGER,
    ou_line REAL, set_by TEXT, set_at TEXT
);
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '7');

CREATE INDEX IF NOT EXISTS idx_events_source_status ON events(source, status);
CREATE INDEX IF NOT EXISTS idx_bets_event_status    ON bets(event_id, status);
CREATE INDEX IF NOT EXISTS idx_bets_user            ON bets(discord_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bets_parlay          ON bets(parlay_id) WHERE parlay_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_parlays_user_status  ON parlays(discord_id, status);
CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay   ON parlay_legs(parlay_id);
"""

# ─── Grading ────────────────────────────────────────────────────────────────────

def grade_bet(bet_row: dict, event_row: dict) -> str:
    """
    Pure grading function — no DB access, no side effects.
    Returns: 'Won' | 'Lost' | 'Push' | 'Cancelled'

    bet_row keys:  bet_type, pick, line, odds, wager_amount
    event_row keys: home_participant, away_participant,
                    home_score, away_score, result_payload (JSON str), status

    Grading rules:
    - Cancelled event → 'Cancelled'
    - Moneyline: pick the winner by score; tie → 'Push'
    - Spread: covered = pick_score + line - opponent_score; >0 Won, ==0 Push, <0 Lost
              (line is from picked team's perspective; negative means favored)
    - Over: total > line → Won; total == line → Push; total < line → Lost
    - Under: total < line → Won; total == line → Push; total > line → Lost
    - Prediction: parse result_payload JSON, compare pick to payload['resolved_side'];
                  'VOID' result → Push
    """
    if event_row["status"] == "cancelled":
        return "Cancelled"

    bet_type = bet_row["bet_type"]
    pick     = bet_row["pick"]
    line     = bet_row.get("line")
    home     = event_row["home_participant"]
    away     = event_row["away_participant"]
    h_score  = float(event_row["home_score"] or 0)
    a_score  = float(event_row["away_score"] or 0)

    if bet_type == "Moneyline":
        if h_score == a_score:
            return "Push"
        winner = home if h_score > a_score else away
        return "Won" if pick == winner else "Lost"

    if bet_type == "Spread":
        pick_score = h_score if pick == home else a_score
        opp_score  = a_score if pick == home else h_score
        covered = pick_score + line - opp_score
        if covered > 0:
            return "Won"
        if covered == 0:
            return "Push"
        return "Lost"

    if bet_type in ("Over", "Under"):
        total = h_score + a_score
        if total == line:
            return "Push"
        if bet_type == "Over":
            return "Won" if total > line else "Lost"
        return "Won" if total < line else "Lost"

    if bet_type == "Prediction":
        payload = json.loads(event_row.get("result_payload") or "{}")
        resolved = payload.get("resolved_side", "")
        if resolved == "VOID":
            return "Push"
        return "Won" if pick == resolved else "Lost"

    return "Error"


_settle_locks: dict[str, asyncio.Lock] = {}


async def _check_parlay_completion(parlay_id: str) -> None:
    """
    Read all legs' bet statuses and settle parlay if all legs are resolved.
    Rules (applied in order):
      1. Any Lost → settle parlay Lost immediately (no payout)
      2. Any Pending → wait, do nothing
      3. Treat Cancelled legs as Push for parlay purposes
      4. All Won (after Cancelled→Push substitution) → Won, credit combined_odds payout
      5. Mix of Won + Push/Cancelled → Push, refund wager_amount
    """
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT parlay_id, discord_id, combined_odds, wager_amount, status FROM parlays WHERE parlay_id=?",
            (parlay_id,))
        parlay = await cur.fetchone()
        if not parlay or parlay["status"] != "Pending":
            return
        cur = await db.execute("""
            SELECT b.status FROM parlay_legs pl
            JOIN bets b ON b.bet_id = pl.bet_id
            WHERE pl.parlay_id=?
        """, (parlay_id,))
        statuses = [r["status"] for r in await cur.fetchall()]

    if not statuses:
        return

    if "Lost" in statuses:
        final_status = "Lost"
        payout = 0
    elif "Pending" in statuses:
        return  # still waiting on remaining legs
    else:
        # Treat Cancelled as Push for parlay purposes
        effective = ["Push" if s == "Cancelled" else s for s in statuses]
        if all(s == "Won" for s in effective):
            final_status = "Won"
            payout = _payout_calc(parlay["wager_amount"], parlay["combined_odds"])
        else:
            # Any Push/Cancelled in mix → parlay Push → refund wager
            final_status = "Push"
            payout = parlay["wager_amount"]

    # Step 1: Atomically claim settlement — BEGIN IMMEDIATE prevents concurrent double-settle
    settled_this_call = False
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT status FROM parlays WHERE parlay_id=?", (parlay_id,))
        row = await cur.fetchone()
        if row and row["status"] != "Pending":
            await db.rollback()
            return
        await db.execute(
            "UPDATE parlays SET status=? WHERE parlay_id=?", (final_status, parlay_id))
        await db.commit()
        settled_this_call = True

    # Step 2: Credit wallet (idempotent via reference_key; only if we won the race above)
    if settled_this_call and payout > 0:
        await flow_wallet.credit(
            int(parlay["discord_id"]), payout, "TSL_BET",
            description=f"Parlay {parlay_id} {final_status}",
            reference_key=f"PARLAY_{parlay_id}_settled")

    profit = payout - parlay["wager_amount"]
    await wager_registry.settle_wager("PARLAY", parlay_id, final_status.lower(), profit)
    log.info(f"[CORE] Parlay {parlay_id} → {final_status}")


async def settle_event(event_id: str) -> None:
    """
    Grade all Pending bets for a final event.
    - Acquires per-event asyncio lock (concurrent calls are no-ops)
    - Credit-first two-DB pattern: wallet credit BEFORE bet status update
    - Push refunds wager_amount; Won pays payout; Lost = no credit
    """
    lock = _settle_locks.setdefault(event_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        async with aiosqlite.connect(FLOW_DB) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT event_id, source, status, home_participant, away_participant, "
                "home_score, away_score, result_payload FROM events WHERE event_id=?",
                (event_id,))
            event = await cur.fetchone()
            if not event or event["status"] != "final":
                return
            cur = await db.execute(
                "SELECT bet_id, discord_id, event_id, bet_type, pick, line, odds, "
                "wager_amount, status, parlay_id FROM bets "
                "WHERE event_id=? AND status='Pending'",
                (event_id,))
            pending = await cur.fetchall()

        if not pending:
            return

        affected_parlays: set[str] = set()
        event_dict = dict(event)

        for bet in pending:
            bet_dict = dict(bet)
            result = grade_bet(bet_dict, event_dict)

            # Step 1: credit wallet FIRST (idempotent via reference_key)
            if result == "Won":
                payout = _payout_calc(bet_dict["wager_amount"], bet_dict["odds"])
                await flow_wallet.credit(
                    int(bet_dict["discord_id"]), payout, "BET_SETTLE",
                    reference_key=f"BET_{bet_dict['bet_id']}_settled")
            elif result == "Push":
                await flow_wallet.credit(
                    int(bet_dict["discord_id"]), bet_dict["wager_amount"], "BET_SETTLE",
                    reference_key=f"BET_{bet_dict['bet_id']}_settled")

            # Step 2: mark bet status in flow.db (idempotent — re-check Pending)
            async with aiosqlite.connect(FLOW_DB) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "SELECT status FROM bets WHERE bet_id=?", (bet_dict["bet_id"],))
                row = await cur.fetchone()
                if row and row["status"] != "Pending":
                    await db.rollback()
                    continue
                await db.execute(
                    "UPDATE bets SET status=? WHERE bet_id=?",
                    (result, bet_dict["bet_id"]))
                await db.commit()

            # Step 3: audit trail
            if result == "Won":
                profit = _payout_calc(bet_dict["wager_amount"], bet_dict["odds"]) - bet_dict["wager_amount"]
            elif result == "Push":
                profit = 0
            else:
                profit = -bet_dict["wager_amount"]
            await wager_registry.settle_wager(
                "BET", str(bet_dict["bet_id"]), result.lower(), profit)

            if bet_dict["parlay_id"]:
                affected_parlays.add(bet_dict["parlay_id"])

        for pid in affected_parlays:
            await _check_parlay_completion(pid)

        await _post_settlement_card(event_dict, [dict(b) for b in pending])
        log.info(f"[CORE] settle_event({event_id}) — graded {len(pending)} bets")


async def setup_db() -> None:
    """Create flow.db schema. Called at bot startup before migration."""
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_SCHEMA_SQL)
        await db.commit()
    log.info("[CORE] flow.db schema ready")


async def run_migration_v7() -> None:
    """
    One-time migration guard. Skips if schema_version >= 7.
    Steps: refund Pending bets, archive old tables, create new schema.
    """
    # Guard: check if migration already ran
    try:
        async with aiosqlite.connect(FLOW_DB) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'")
            row = await cur.fetchone()
            if row and int(row[0]) >= 7:
                log.info("[CORE] Migration v7 already applied — skipping")
                return
    except Exception:
        pass  # FLOW_DB may not exist yet

    log.warning("[CORE] Running migration v7 — refunding Pending bets and archiving tables")

    REFUND_SOURCES = [
        ("bets_table",           "bet_id", "wager_amount"),
        ("real_bets",            "bet_id", "wager_amount"),
        ("prediction_contracts", "id",     "cost_bucks"),
        ("prop_wagers",          "id",     "wager_amount"),
    ]

    async with aiosqlite.connect(OLD_SB_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row

        # Step 1: Refund all Pending bets
        for table, id_col, wager_col in REFUND_SOURCES:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            row = await cur.fetchone()
            if not row:
                log.info(f"[CORE] Migration: {table} not found, skipping refund")
                continue
            cur = await db.execute(
                f"SELECT {id_col}, discord_id, {wager_col} FROM {table} WHERE status='Pending'")
            rows = await cur.fetchall()
            for r in rows:
                ref_key = f"MIGRATE_V7_REFUND_{table}_{r[id_col]}"
                await flow_wallet.credit(
                    int(r["discord_id"]), int(r[wager_col]), "MIGRATION",
                    description="Clean-break refund v7",
                    reference_key=ref_key)
                await db.execute(
                    f"UPDATE {table} SET status='Refunded' WHERE {id_col}=?", (r[id_col],))
            await db.commit()
            log.info(f"[CORE] Migration: refunded {len(rows)} Pending from {table}")

        # Step 2: Archive old tables
        ARCHIVE_TABLES = [
            "bets_table", "parlays_table", "parlay_legs",
            "real_events", "real_bets",
            "prediction_markets", "prediction_contracts",
            "prop_wagers",
        ]
        for tbl in ARCHIVE_TABLES:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,))
            row = await cur.fetchone()
            if not row:
                continue
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (f"{tbl}_arc_v7",))
            arc = await cur.fetchone()
            if arc:
                log.info(f"[CORE] Migration: {tbl}_arc_v7 already exists, skipping rename")
                continue
            await db.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}_arc_v7")
            log.info(f"[CORE] Migration: archived {tbl} → {tbl}_arc_v7")
        await db.commit()

    # Step 3: Create new schema in flow.db
    await setup_db()
    log.warning("[CORE] Migration v7 complete — flow.db ready")


def init(bot) -> None:
    """Inject bot reference for settlement notifications. Call from cog_load."""
    global _bot
    _bot = bot


async def _post_settlement_card(event: dict, bets: list[dict]) -> None:
    """
    Post a settlement announcement embed to #flow-live after an event is graded.
    Skips silently if all bets were Cancelled, or if channel resolution fails.
    Never raises — settlement must not be blocked by a notification failure.
    """
    if _bot is None:
        log.warning("[CORE] _post_settlement_card: bot not initialized, skipping")
        return

    active = [b for b in bets if b["status"] != "Cancelled"]
    if not active:
        return

    try:
        from setup_cog import get_channel_id_async
        ch_id = await get_channel_id_async("flow_live")
        if not ch_id:
            log.warning("[CORE] _post_settlement_card: flow_live channel not configured")
            return
        channel = _bot.get_channel(ch_id)
        if channel is None:
            log.warning("[CORE] _post_settlement_card: channel %s not in cache", ch_id)
            return
    except Exception:
        log.warning("[CORE] _post_settlement_card: channel resolution failed", exc_info=True)
        return

    # Matchup + final score
    away = event.get("away_participant", "Away")
    home = event.get("home_participant", "Home")
    a_score = int(event.get("away_score") or 0)
    h_score = int(event.get("home_score") or 0)
    matchup = f"**{away} @ {home}**  |  {a_score}–{h_score}"

    won_count  = sum(1 for b in active if b["status"] == "Won")
    lost_count = sum(1 for b in active if b["status"] == "Lost")
    push_count = sum(1 for b in active if b["status"] == "Push")

    # Payout lines for Won + Push bets only (Lost bets covered by #ledger text audit)
    payout_lines: list[str] = []
    for b in active:
        if b["status"] == "Won":
            net = _payout_calc(b["wager_amount"], b["odds"]) - b["wager_amount"]
            payout_lines.append(
                f"• <@{b['discord_id']}> — {b['bet_type']} **{b['pick']}** (+${net:,})"
            )
        elif b["status"] == "Push":
            payout_lines.append(
                f"• <@{b['discord_id']}> — Push, ${b['wager_amount']:,} returned"
            )

    if won_count > 0:
        color = 0x57F287  # green
        title = "🏆 Event Settled"
    elif lost_count == 0 and push_count > 0:
        color = 0xFEE75C  # gold
        title = "↩️ Event Settled — Push"
    else:
        color = 0xED4245  # red
        title = "📋 Event Settled"

    embed = discord.Embed(title=title, description=matchup, color=color)
    embed.add_field(
        name="Result",
        value=f"W: **{won_count}**  L: **{lost_count}**  P: **{push_count}**",
        inline=False,
    )
    if payout_lines:
        payouts_text = "\n".join(payout_lines)
        if len(payouts_text) > 1024:
            payouts_text = payouts_text[:1021] + "..."
        embed.add_field(name="Payouts", value=payouts_text, inline=False)
    embed.set_footer(text=f"{len(active)} bet(s) graded · {event.get('event_id', '')}")

    try:
        await channel.send(embed=embed)
    except Exception:
        log.warning("[CORE] _post_settlement_card: failed to post embed", exc_info=True)


# ─── Parlay helpers ───────────────────────────────────────────────────────────

async def write_parlay(
    parlay_id: str,
    discord_id: int,
    combined_odds: int,
    wager: int,
) -> None:
    """
    Insert a new Pending parlay header row into flow.db.
    Safe to call before write_parlay_leg; legs reference this row via FK.
    """
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """INSERT OR IGNORE INTO parlays
               (parlay_id, discord_id, combined_odds, wager_amount, status)
               VALUES (?, ?, ?, ?, 'Pending')""",
            (parlay_id, discord_id, combined_odds, wager),
        )
        await db.commit()


async def write_parlay_leg(
    parlay_id: str,
    leg_index: int,
    bet_id: int,
) -> None:
    """
    Insert a parlay_legs join row into flow.db linking a bet to a parlay.
    bet_id must already exist in the bets table (written via write_bet).
    """
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """INSERT OR IGNORE INTO parlay_legs
               (parlay_id, bet_id, leg_index)
               VALUES (?, ?, ?)""",
            (parlay_id, bet_id, leg_index),
        )
        await db.commit()


# ─── Public API for ingestor cogs ─────────────────────────────────────────────

async def write_event(
    event_id: str,
    source: str,
    home: str,
    away: str,
    commence_ts: str,
    payload: dict | None = None,
) -> None:
    """
    Upsert an event row into flow.db.
    Safe to call on every sync cycle — INSERT OR IGNORE is idempotent.
    """
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """INSERT OR IGNORE INTO events
               (event_id, source, home_participant, away_participant, commence_ts, result_payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, source, home, away, commence_ts, json.dumps(payload or {})),
        )
        await db.commit()


async def write_bet(
    discord_id: int,
    event_id: str,
    bet_type: str,
    pick: str,
    line: float | None,
    odds: int,
    wager: int,
    parlay_id: str | None = None,
) -> int:
    """
    Insert a new Pending bet into flow.db.
    Returns the new bet_id.
    Enforces FK constraint — event_id must already exist in events table.
    """
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        cur = await db.execute(
            """INSERT INTO bets
               (discord_id, event_id, bet_type, pick, line, odds, wager_amount, parlay_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (discord_id, event_id, bet_type, pick, line, odds, wager, parlay_id),
        )
        await db.commit()
        return cur.lastrowid


async def finalize_event(
    event_id: str,
    home_score: float,
    away_score: float,
    result_payload: dict | None = None,
) -> None:
    """
    Mark an event as final, storing final scores and optional result payload.
    Called by ingestor cogs when a game/market is complete.
    Does NOT settle — ingestor must emit EVENT_FINALIZED after calling this.
    """
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        cur = await db.execute(
            """UPDATE events
               SET status='final', home_score=?, away_score=?,
                   result_payload=?,
                   finalized_ts=strftime('%Y-%m-%dT%H:%M:%SZ','now')
               WHERE event_id=?""",
            (home_score, away_score, json.dumps(result_payload or {}), event_id),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"[CORE] finalize_event: no event row for {event_id!r} — "
                "was write_event() called before finalize_event()?"
            )
        await db.commit()


def _register_bus_subscription() -> None:
    """
    Subscribe settle_event to the flow_events bus EVENT_FINALIZED event.
    Called once at bot startup from bot.py setup_hook, after all cogs load.
    Uses a string key ('event_finalized') which the FlowEventBus handles generically.
    """
    try:
        from flow_events import flow_bus, EVENT_FINALIZED
        flow_bus.subscribe(
            EVENT_FINALIZED,
            lambda payload: asyncio.create_task(settle_event(payload["event_id"])),
        )
        log.info("[CORE] Subscribed to EVENT_FINALIZED bus")
    except ImportError as exc:
        log.warning(f"[CORE] Could not subscribe to flow_bus: {exc}")


@tasks.loop(minutes=10)
async def settlement_poll() -> None:
    """
    Fallback poller — catches any final events that still have Pending bets,
    in case the EVENT_FINALIZED bus event was missed.
    Runs every 10 minutes. settle_event() is idempotent so re-running is safe.
    """
    try:
        async with aiosqlite.connect(FLOW_DB) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT DISTINCT e.event_id FROM events e
                   JOIN bets b ON b.event_id = e.event_id
                   WHERE e.status = 'final' AND b.status = 'Pending'"""
            )
            rows = await cur.fetchall()
        for row in rows:
            await settle_event(row["event_id"])
    except Exception:
        log.exception("[CORE] settlement_poll error")
