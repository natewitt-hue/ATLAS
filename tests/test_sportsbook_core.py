import pytest
import asyncio
import os
import sys
import aiosqlite

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
