import pytest
import asyncio
import os
import sys
import aiosqlite
import json

# Point FLOW_DB_PATH to a dummy path so OLD_SB_DB doesn't affect wallet
os.environ["FLOW_DB_PATH"] = os.path.join(os.path.dirname(__file__), "test_old.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sportsbook_core


@pytest.mark.asyncio
async def test_setup_db_creates_tables(tmp_path):
    sportsbook_core.FLOW_DB = str(tmp_path / "flow.db")
    await sportsbook_core.setup_db()
    async with aiosqlite.connect(sportsbook_core.FLOW_DB) as db:
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
    assert {"events", "bets", "parlays", "parlay_legs", "schema_meta"} <= tables


@pytest.mark.asyncio
async def test_setup_db_idempotent(tmp_path):
    sportsbook_core.FLOW_DB = str(tmp_path / "flow.db")
    await sportsbook_core.setup_db()
    await sportsbook_core.setup_db()  # second call must not raise


@pytest.mark.asyncio
async def test_migration_refunds_pending_bets(tmp_path):
    """Pending bets get credited back; archived table exists."""
    old_db = str(tmp_path / "old.db")
    new_db = str(tmp_path / "flow.db")
    sportsbook_core.OLD_SB_DB = old_db
    sportsbook_core.FLOW_DB = new_db

    async with aiosqlite.connect(old_db) as db:
        await db.execute(
            "CREATE TABLE bets_table "
            "(bet_id INTEGER PRIMARY KEY, discord_id INTEGER, wager_amount INTEGER, status TEXT)"
        )
        await db.execute("INSERT INTO bets_table VALUES (1, 12345, 500, 'Pending')")
        await db.commit()

    credits = []

    async def fake_credit(discord_id, amount, source, description="", reference_key=None, **kw):
        credits.append((discord_id, amount, reference_key))
        return 0

    original_credit = sportsbook_core.flow_wallet.credit
    sportsbook_core.flow_wallet.credit = fake_credit
    try:
        await sportsbook_core.run_migration_v7()
    finally:
        sportsbook_core.flow_wallet.credit = original_credit

    assert len(credits) == 1
    assert credits[0] == (12345, 500, "MIGRATE_V7_REFUND_bets_table_1")

    async with aiosqlite.connect(old_db) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE name='bets_table_arc_v7'"
        )
        row = await cur.fetchone()
        assert row is not None


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    """Running migration twice does not error or double-refund."""
    old_db = str(tmp_path / "old.db")
    new_db = str(tmp_path / "flow.db")
    sportsbook_core.OLD_SB_DB = old_db
    sportsbook_core.FLOW_DB = new_db

    credits = []

    async def fake_credit(discord_id, amount, source, description="", reference_key=None, **kw):
        credits.append((discord_id, amount, reference_key))
        return 0

    original_credit = sportsbook_core.flow_wallet.credit
    sportsbook_core.flow_wallet.credit = fake_credit
    try:
        await sportsbook_core.run_migration_v7()
        count_after_first = len(credits)
        await sportsbook_core.run_migration_v7()  # second run — no-op because schema_version=7
        assert len(credits) == count_after_first
    finally:
        sportsbook_core.flow_wallet.credit = original_credit


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_event(home_score, away_score, source="TSL",
               home="Eagles", away="Cowboys", result_payload=None, status="final"):
    return {
        "home_participant": home, "away_participant": away,
        "home_score": home_score, "away_score": away_score,
        "result_payload": json.dumps(result_payload or {}),
        "status": status,
    }

def make_bet(bet_type, pick, odds=-110, line=None, wager=100):
    return {"bet_type": bet_type, "pick": pick, "odds": odds,
            "line": line, "wager_amount": wager}

# ─── Moneyline ────────────────────────────────────────────────────────────────

def test_grade_moneyline_win():
    assert sportsbook_core.grade_bet(make_bet("Moneyline", "Eagles"), make_event(24, 17)) == "Won"

def test_grade_moneyline_loss():
    assert sportsbook_core.grade_bet(make_bet("Moneyline", "Cowboys"), make_event(24, 17)) == "Lost"

def test_grade_moneyline_push():
    assert sportsbook_core.grade_bet(make_bet("Moneyline", "Eagles"), make_event(21, 21)) == "Push"

# ─── Spread ───────────────────────────────────────────────────────────────────

def test_grade_spread_win():
    # Eagles -3.5 favorite — Eagles win by 7, covers -3.5
    assert sportsbook_core.grade_bet(make_bet("Spread", "Eagles", line=-3.5), make_event(24, 17)) == "Won"

def test_grade_spread_loss():
    # Eagles -7 favorite — Eagles win by only 3, does NOT cover
    assert sportsbook_core.grade_bet(make_bet("Spread", "Eagles", line=-7.0), make_event(20, 17)) == "Lost"

def test_grade_spread_push():
    # Eagles -7 — Eagles win by exactly 7
    assert sportsbook_core.grade_bet(make_bet("Spread", "Eagles", line=-7.0), make_event(24, 17)) == "Push"

# ─── Over/Under ───────────────────────────────────────────────────────────────

def test_grade_over_win():
    assert sportsbook_core.grade_bet(make_bet("Over", "Over", line=40.5), make_event(24, 20)) == "Won"

def test_grade_over_loss():
    assert sportsbook_core.grade_bet(make_bet("Over", "Over", line=44.5), make_event(24, 20)) == "Lost"

def test_grade_under_win():
    assert sportsbook_core.grade_bet(make_bet("Under", "Under", line=44.5), make_event(24, 20)) == "Won"

def test_grade_ou_push():
    assert sportsbook_core.grade_bet(make_bet("Over", "Over", line=44.0), make_event(24, 20)) == "Push"

# ─── Prediction ───────────────────────────────────────────────────────────────

def test_grade_prediction_win():
    event = make_event(0, 0, source="POLY", result_payload={"resolved_side": "YES"})
    assert sportsbook_core.grade_bet(make_bet("Prediction", "YES"), event) == "Won"

def test_grade_prediction_loss():
    event = make_event(0, 0, source="POLY", result_payload={"resolved_side": "YES"})
    assert sportsbook_core.grade_bet(make_bet("Prediction", "NO"), event) == "Lost"

# ─── Cancelled event ─────────────────────────────────────────────────────────

def test_grade_cancelled_event():
    event = make_event(0, 0, status="cancelled")
    assert sportsbook_core.grade_bet(make_bet("Moneyline", "Eagles"), event) == "Cancelled"


# ─── Settlement tests ────────────────────────────────────────────────────────

async def _seed_parlay_db(tmp_path, leg_statuses: list[str]):
    """
    Seed flow.db with a 2-leg parlay.
    leg_statuses: list of 'Pending'|'Won'|'Lost'|'Push'|'Cancelled' for each leg.
    Returns: db path
    """
    db_path = str(tmp_path / "flow.db")
    sportsbook_core.FLOW_DB = db_path
    sportsbook_core._settle_locks.clear()
    await sportsbook_core.setup_db()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            "INSERT INTO events VALUES ('e1','TSL','final','A','B',24,17,'{}',NULL,NULL,NULL)")
        await db.execute(
            "INSERT INTO parlays VALUES ('P1',99,650,100,'Pending',NULL)")
        for i, status in enumerate(leg_statuses):
            await db.execute(
                f"INSERT INTO bets VALUES ({i+1},99,'e1','Moneyline','A',NULL,-110,100,'{status}','P1',NULL)")
            await db.execute(
                f"INSERT INTO parlay_legs VALUES ({i+1},'P1',{i+1},{i})")
        await db.commit()
    return db_path


@pytest.mark.asyncio
async def test_parlay_lost_on_any_loss(tmp_path):
    """Any Lost leg kills the parlay immediately."""
    await _seed_parlay_db(tmp_path, ["Lost", "Won"])

    credits = []
    original = sportsbook_core.flow_wallet.credit
    async def fake_credit(*a, **kw):
        credits.append(a)
        return 0
    sportsbook_core.flow_wallet.credit = fake_credit
    original_settle = sportsbook_core.wager_registry.settle_wager
    async def fake_settle_loss(*a, **kw):
        pass
    sportsbook_core.wager_registry.settle_wager = fake_settle_loss
    try:
        await sportsbook_core._check_parlay_completion("P1")
    finally:
        sportsbook_core.flow_wallet.credit = original
        sportsbook_core.wager_registry.settle_wager = original_settle

    async with aiosqlite.connect(sportsbook_core.FLOW_DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT status FROM parlays WHERE parlay_id='P1'")
        row = await cur.fetchone()
    assert row["status"] == "Lost"
    assert credits == []  # no payout on loss


@pytest.mark.asyncio
async def test_parlay_cancelled_leg_is_push(tmp_path):
    """Cancelled leg + Won leg = parlay Push (refund of wager)."""
    await _seed_parlay_db(tmp_path, ["Won", "Cancelled"])

    credits = []
    original = sportsbook_core.flow_wallet.credit
    async def fake_credit(discord_id, amount, source, **kw):
        credits.append((discord_id, amount))
        return 0
    sportsbook_core.flow_wallet.credit = fake_credit
    original_settle = sportsbook_core.wager_registry.settle_wager
    async def fake_settle(*a, **kw):
        pass
    sportsbook_core.wager_registry.settle_wager = fake_settle
    try:
        await sportsbook_core._check_parlay_completion("P1")
    finally:
        sportsbook_core.flow_wallet.credit = original
        sportsbook_core.wager_registry.settle_wager = original_settle

    async with aiosqlite.connect(sportsbook_core.FLOW_DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT status FROM parlays WHERE parlay_id='P1'")
        row = await cur.fetchone()
    assert row["status"] == "Push"
    assert any(c[1] == 100 for c in credits)  # wager_amount refunded


@pytest.mark.asyncio
async def test_settle_event_idempotent(tmp_path):
    """Calling settle_event twice does not double-credit."""
    db_path = str(tmp_path / "flow.db")
    sportsbook_core.FLOW_DB = db_path
    sportsbook_core._settle_locks.clear()
    await sportsbook_core.setup_db()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            "INSERT INTO events VALUES ('e1','TSL','final','A','B',24,17,'{}',NULL,NULL,NULL)")
        await db.execute(
            "INSERT INTO bets VALUES (1,99,'e1','Moneyline','A',NULL,-110,100,'Pending',NULL,NULL)")
        await db.commit()

    credits = []
    original = sportsbook_core.flow_wallet.credit
    async def fake_credit(discord_id, amount, source, **kw):
        credits.append((discord_id, amount))
        return 0
    sportsbook_core.flow_wallet.credit = fake_credit
    original_settle = sportsbook_core.wager_registry.settle_wager
    async def fake_settle(*a, **kw):
        pass
    sportsbook_core.wager_registry.settle_wager = fake_settle
    try:
        await sportsbook_core.settle_event("e1")
        first_count = len(credits)
        await sportsbook_core.settle_event("e1")  # second call — no-op
        assert len(credits) == first_count
    finally:
        sportsbook_core.flow_wallet.credit = original
        sportsbook_core.wager_registry.settle_wager = original_settle
