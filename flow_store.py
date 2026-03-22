"""
flow_store.py -- ATLAS Flow Store Engine (Phase 1)
====================================================
Core store cog: DB schema, purchase engine, activation engine,
lootbox engine, and expiration background task.

Phase 1 is headless — no UI, no slash commands, no card rendering.
Phase 2 adds views/buttons/cards on top of this engine.
====================================================
"""

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
import discord
from discord.ext import commands, tasks

import flow_wallet
from flow_wallet import DB_PATH, InsufficientFundsError, get_user_lock
from store_effects import expire_stale_effects

_DB_TIMEOUT = 10
log = logging.getLogger(__name__)


# =============================================================================
#  RARITY ORDERING (for guaranteed-floor logic)
# =============================================================================

_RARITY_RANK = {"common": 0, "rare": 1, "epic": 2, "legendary": 3}


def _rarity_val(r: str) -> int:
    return _RARITY_RANK.get(r, 0)


# =============================================================================
#  HELPER — UTC timestamp
# =============================================================================

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
#  COG
# =============================================================================

class FlowStoreCog(commands.Cog, name="FlowStore"):
    """ATLAS Flow Store engine — Phase 1 (no UI)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self._init_store_db()
        self._expiration_task.start()
        log.info("[Store] FlowStoreCog loaded (Phase 1 engine)")

    async def cog_unload(self):
        self._expiration_task.cancel()

    # =========================================================================
    #  DB INIT
    # =========================================================================

    async def _init_store_db(self):
        """Create all store tables and indexes if they don't exist."""
        async with aiosqlite.connect(DB_PATH, timeout=_DB_TIMEOUT) as db:
            await db.execute("PRAGMA journal_mode=WAL")

            # -- store_items (catalog) ----------------------------------------
            await db.execute("""
                CREATE TABLE IF NOT EXISTS store_items (
                    item_id         TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    description     TEXT NOT NULL,
                    category        TEXT NOT NULL,
                    subcategory     TEXT,
                    price           INTEGER NOT NULL,
                    emoji           TEXT,
                    rarity          TEXT DEFAULT 'common',
                    is_permanent    INTEGER DEFAULT 1,
                    max_stock       INTEGER,
                    max_per_user    INTEGER,
                    cooldown_hours  INTEGER,
                    effect_type     TEXT,
                    effect_data     TEXT,
                    sort_order      INTEGER DEFAULT 0,
                    image_url       TEXT,
                    is_active       INTEGER DEFAULT 1,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # -- store_rotations (limited-time windows) -----------------------
            await db.execute("""
                CREATE TABLE IF NOT EXISTS store_rotations (
                    rotation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id         TEXT NOT NULL REFERENCES store_items(item_id),
                    starts_at       TIMESTAMP NOT NULL,
                    ends_at         TIMESTAMP NOT NULL,
                    stock_remaining INTEGER,
                    discount_pct    INTEGER DEFAULT 0,
                    is_featured     INTEGER DEFAULT 0,
                    created_by      TEXT,
                    UNIQUE(item_id, starts_at)
                )
            """)

            # -- store_inventory (user purchases) -----------------------------
            await db.execute("""
                CREATE TABLE IF NOT EXISTS store_inventory (
                    inventory_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id      INTEGER NOT NULL,
                    item_id         TEXT NOT NULL REFERENCES store_items(item_id),
                    quantity        INTEGER DEFAULT 1,
                    purchased_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    purchase_price  INTEGER NOT NULL,
                    is_activated    INTEGER DEFAULT 0,
                    activated_at    TIMESTAMP,
                    expires_at      TIMESTAMP
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_inv_user "
                "ON store_inventory(discord_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_inv_item "
                "ON store_inventory(item_id)"
            )

            # -- store_effects (active effects, denormalized) -----------------
            await db.execute("""
                CREATE TABLE IF NOT EXISTS store_effects (
                    effect_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id      INTEGER NOT NULL,
                    item_id         TEXT NOT NULL REFERENCES store_items(item_id),
                    effect_type     TEXT NOT NULL,
                    effect_data     TEXT NOT NULL,
                    started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at      TIMESTAMP,
                    is_active       INTEGER DEFAULT 1
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_eff_lookup "
                "ON store_effects(discord_id, effect_type, is_active)"
            )

            # -- store_transactions (immutable ledger) ------------------------
            await db.execute("""
                CREATE TABLE IF NOT EXISTS store_transactions (
                    tx_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id      INTEGER NOT NULL,
                    item_id         TEXT NOT NULL,
                    action          TEXT NOT NULL,
                    coins_delta     INTEGER DEFAULT 0,
                    balance_after   INTEGER NOT NULL,
                    metadata        TEXT,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_stx_user "
                "ON store_transactions(discord_id)"
            )

            await db.commit()
        log.info("[Store] DB schema initialized (5 tables)")

    # =========================================================================
    #  PURCHASE ENGINE
    # =========================================================================

    async def _purchase_item(
        self,
        discord_id: int,
        item_id: str,
        rotation_id: Optional[int] = None,
    ) -> dict:
        """Execute a purchase with full validation.

        Returns ``{"ok": bool, "error": str|None, "item": dict,
        "price": int, "balance": int, "inventory_id": int}``
        """
        now = _utcnow()
        ref_key = f"store_purchase_{discord_id}_{item_id}_{uuid.uuid4().hex[:8]}"

        async with get_user_lock(discord_id):
            async with aiosqlite.connect(DB_PATH, timeout=_DB_TIMEOUT) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")

                # -- 1. Item exists + active ----------------------------------
                async with db.execute(
                    "SELECT * FROM store_items WHERE item_id=? AND is_active=1",
                    (item_id,),
                ) as cur:
                    item_row = await cur.fetchone()
                if not item_row:
                    return {"ok": False, "error": "Item not found or unavailable"}
                item = dict(item_row)

                price = item["price"]
                discount_pct = 0

                # -- 2. Availability check ------------------------------------
                if not item["is_permanent"]:
                    if rotation_id:
                        async with db.execute(
                            "SELECT * FROM store_rotations "
                            "WHERE rotation_id=? AND item_id=?",
                            (rotation_id, item_id),
                        ) as cur:
                            rot_row = await cur.fetchone()
                    else:
                        # Find any active rotation for this item
                        async with db.execute(
                            "SELECT * FROM store_rotations "
                            "WHERE item_id=? AND starts_at<=? AND ends_at>=? "
                            "ORDER BY ends_at DESC LIMIT 1",
                            (item_id, now, now),
                        ) as cur:
                            rot_row = await cur.fetchone()

                    if not rot_row:
                        return {"ok": False, "error": "Item is not currently available"}
                    rot = dict(rot_row)
                    rotation_id = rot["rotation_id"]

                    if rot["starts_at"] > now or rot["ends_at"] < now:
                        return {"ok": False, "error": "This rotation has ended"}

                    discount_pct = rot.get("discount_pct", 0) or 0
                else:
                    rot = None

                # Apply discount
                if discount_pct > 0:
                    price = max(1, int(price * (100 - discount_pct) / 100))

                # -- 3. max_per_user check ------------------------------------
                if item["max_per_user"] is not None:
                    async with db.execute(
                        "SELECT COUNT(*) FROM store_inventory "
                        "WHERE discord_id=? AND item_id=?",
                        (discord_id, item_id),
                    ) as cur:
                        (owned,) = await cur.fetchone()
                    if owned >= item["max_per_user"]:
                        return {
                            "ok": False,
                            "error": "You already own the maximum of this item",
                        }

                # -- 4. Cooldown check ----------------------------------------
                if item["cooldown_hours"] is not None:
                    cutoff = (
                        _utcnow_dt() - timedelta(hours=item["cooldown_hours"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    async with db.execute(
                        "SELECT MAX(purchased_at) FROM store_inventory "
                        "WHERE discord_id=? AND item_id=? AND purchased_at>?",
                        (discord_id, item_id, cutoff),
                    ) as cur:
                        (last_purchase,) = await cur.fetchone()
                    if last_purchase:
                        return {
                            "ok": False,
                            "error": (
                                f"This item is on cooldown "
                                f"({item['cooldown_hours']}h between purchases)"
                            ),
                        }

                # -- 5. Atomic transaction ------------------------------------
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # Debit balance (checked inside txn)
                    new_balance = await flow_wallet.debit(
                        discord_id,
                        price,
                        source="STORE",
                        description=f"Purchased {item['name']}",
                        reference_key=ref_key,
                        subsystem="STORE",
                        subsystem_id=item_id,
                        con=db,
                    )

                    # Insert inventory row
                    await db.execute(
                        "INSERT INTO store_inventory "
                        "(discord_id, item_id, quantity, purchase_price) "
                        "VALUES (?, ?, 1, ?)",
                        (discord_id, item_id, price),
                    )
                    async with db.execute("SELECT last_insert_rowid()") as cur:
                        (inventory_id,) = await cur.fetchone()

                    # Decrement stock if rotation
                    if rot and rot.get("stock_remaining") is not None:
                        cur = await db.execute(
                            "UPDATE store_rotations "
                            "SET stock_remaining = stock_remaining - 1 "
                            "WHERE rotation_id=? AND stock_remaining > 0",
                            (rotation_id,),
                        )
                        if cur.rowcount == 0:
                            # Another buyer grabbed the last one
                            await db.rollback()
                            return {"ok": False, "error": "Sold out!"}

                    # Transaction log
                    metadata = json.dumps({
                        "rotation_id": rotation_id,
                        "discount_pct": discount_pct,
                        "original_price": item["price"],
                    })
                    await db.execute(
                        "INSERT INTO store_transactions "
                        "(discord_id, item_id, action, coins_delta, "
                        "balance_after, metadata) "
                        "VALUES (?, ?, 'purchase', ?, ?, ?)",
                        (discord_id, item_id, -price, new_balance, metadata),
                    )

                    await db.commit()

                except InsufficientFundsError:
                    await db.rollback()
                    return {"ok": False, "error": "Insufficient balance"}
                except Exception:
                    await db.rollback()
                    raise

        return {
            "ok": True,
            "error": None,
            "item": item,
            "price": price,
            "balance": new_balance,
            "inventory_id": inventory_id,
        }

    # =========================================================================
    #  ACTIVATION ENGINE
    # =========================================================================

    async def _activate_item(
        self, discord_id: int, inventory_id: int
    ) -> dict:
        """Activate a purchased item, creating its store_effect.

        Returns ``{"ok": bool, "error": str|None, "effect": dict}``
        """
        now = _utcnow()

        async with get_user_lock(discord_id):
            async with aiosqlite.connect(DB_PATH, timeout=_DB_TIMEOUT) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")

                # -- Validate inventory row -----------------------------------
                async with db.execute(
                    "SELECT si.*, it.effect_type, it.effect_data AS item_effect_data, "
                    "it.name AS item_name "
                    "FROM store_inventory si "
                    "JOIN store_items it ON si.item_id = it.item_id "
                    "WHERE si.inventory_id=? AND si.discord_id=?",
                    (inventory_id, discord_id),
                ) as cur:
                    row = await cur.fetchone()

                if not row:
                    return {"ok": False, "error": "Item not found in your inventory"}
                inv = dict(row)

                if inv["is_activated"]:
                    return {"ok": False, "error": "This item is already activated"}

                effect_type = inv["effect_type"]
                if not effect_type:
                    return {"ok": False, "error": "This item has no activatable effect"}

                # Parse effect data from the item definition
                try:
                    effect_data = json.loads(inv["item_effect_data"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    effect_data = {}

                # -- No-stack check (V1) --------------------------------------
                async with db.execute(
                    "SELECT COUNT(*) FROM store_effects "
                    "WHERE discord_id=? AND effect_type=? AND is_active=1",
                    (discord_id, effect_type),
                ) as cur:
                    (active_count,) = await cur.fetchone()
                if active_count > 0:
                    return {
                        "ok": False,
                        "error": (
                            f"You already have an active {effect_type} effect — "
                            "wait for it to expire"
                        ),
                    }

                # Calculate expiration
                duration_hours = effect_data.get("duration_hours")
                if duration_hours:
                    expires_at = (
                        _utcnow_dt() + timedelta(hours=duration_hours)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    expires_at = None  # permanent effect

                # -- Atomic activation ----------------------------------------
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # Mark inventory as activated
                    await db.execute(
                        "UPDATE store_inventory "
                        "SET is_activated=1, activated_at=? "
                        "WHERE inventory_id=?",
                        (now, inventory_id),
                    )

                    # Insert effect row
                    effect_data_str = json.dumps(effect_data)
                    await db.execute(
                        "INSERT INTO store_effects "
                        "(discord_id, item_id, effect_type, effect_data, "
                        "started_at, expires_at, is_active) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1)",
                        (
                            discord_id, inv["item_id"], effect_type,
                            effect_data_str, now, expires_at,
                        ),
                    )

                    # Get current balance for transaction log
                    balance = await flow_wallet.get_balance(discord_id, con=db)

                    # Transaction log
                    await db.execute(
                        "INSERT INTO store_transactions "
                        "(discord_id, item_id, action, coins_delta, "
                        "balance_after, metadata) "
                        "VALUES (?, ?, 'activate', 0, ?, ?)",
                        (
                            discord_id, inv["item_id"], balance,
                            json.dumps({
                                "inventory_id": inventory_id,
                                "effect_type": effect_type,
                                "expires_at": expires_at,
                            }),
                        ),
                    )

                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        return {
            "ok": True,
            "error": None,
            "effect": {
                "effect_type": effect_type,
                "effect_data": effect_data,
                "expires_at": expires_at,
                "item_name": inv.get("item_name", inv["item_id"]),
            },
        }

    # =========================================================================
    #  LOOTBOX ENGINE
    # =========================================================================

    async def _open_lootbox(
        self, discord_id: int, inventory_id: int
    ) -> dict:
        """Open a lootbox, awarding a random item or coin fallback.

        Returns ``{"ok": bool, "error": str|None, "won_item": dict|None,
        "is_dupe": bool, "coins_awarded": int}``
        """
        async with get_user_lock(discord_id):
            async with aiosqlite.connect(DB_PATH, timeout=_DB_TIMEOUT) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")

                # -- Validate -------------------------------------------------
                async with db.execute(
                    "SELECT si.*, it.effect_data AS item_effect_data, "
                    "it.name AS item_name, it.category "
                    "FROM store_inventory si "
                    "JOIN store_items it ON si.item_id = it.item_id "
                    "WHERE si.inventory_id=? AND si.discord_id=? "
                    "AND si.is_activated=0",
                    (inventory_id, discord_id),
                ) as cur:
                    row = await cur.fetchone()

                if not row:
                    return {
                        "ok": False, "error": "Lootbox not found or already opened",
                        "won_item": None, "is_dupe": False, "coins_awarded": 0,
                    }
                inv = dict(row)

                try:
                    loot_data = json.loads(inv["item_effect_data"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    loot_data = {}

                pool = loot_data.get("pool", [])
                if not pool:
                    return {
                        "ok": False, "error": "This item has no loot pool",
                        "won_item": None, "is_dupe": False, "coins_awarded": 0,
                    }

                coins_fallback = loot_data.get("coins_fallback", {})
                guaranteed_rarity = loot_data.get("guaranteed_rarity")

                # -- Weighted selection with rarity floor ---------------------
                weights = [entry["weight"] for entry in pool]
                chosen = None

                for _ in range(10):
                    pick = random.choices(pool, weights=weights, k=1)[0]
                    if guaranteed_rarity:
                        if _rarity_val(pick.get("rarity", "common")) >= _rarity_val(guaranteed_rarity):
                            chosen = pick
                            break
                    else:
                        chosen = pick
                        break

                if not chosen:
                    # Fallback: pick highest rarity from pool
                    chosen = max(pool, key=lambda x: _rarity_val(x.get("rarity", "common")))

                won_item_id = chosen["item_id"]

                # -- Dupe check -----------------------------------------------
                is_dupe = False
                coins_awarded = 0

                # Look up the won item's details
                async with db.execute(
                    "SELECT * FROM store_items WHERE item_id=?",
                    (won_item_id,),
                ) as cur:
                    won_item_row = await cur.fetchone()

                won_item = dict(won_item_row) if won_item_row else None

                if won_item and won_item.get("max_per_user") is not None:
                    async with db.execute(
                        "SELECT COUNT(*) FROM store_inventory "
                        "WHERE discord_id=? AND item_id=?",
                        (discord_id, won_item_id),
                    ) as cur:
                        (owned,) = await cur.fetchone()
                    if owned >= won_item["max_per_user"]:
                        is_dupe = True

                # -- Atomic lootbox resolution --------------------------------
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # Mark lootbox as activated/consumed
                    await db.execute(
                        "UPDATE store_inventory "
                        "SET is_activated=1, activated_at=? "
                        "WHERE inventory_id=?",
                        (_utcnow(), inventory_id),
                    )

                    ref_key = f"store_lootbox_{discord_id}_{inventory_id}_{uuid.uuid4().hex[:8]}"

                    if is_dupe:
                        # Coin fallback
                        min_coins = coins_fallback.get("min", 1000)
                        max_coins = coins_fallback.get("max", 3000)
                        coins_awarded = random.randint(min_coins, max_coins)

                        new_balance = await flow_wallet.credit(
                            discord_id,
                            coins_awarded,
                            source="STORE_LOOTBOX",
                            description=f"Lootbox dupe fallback ({inv['item_name']})",
                            reference_key=ref_key,
                            subsystem="STORE",
                            subsystem_id=inv["item_id"],
                            con=db,
                        )
                    else:
                        # Award the item
                        await db.execute(
                            "INSERT INTO store_inventory "
                            "(discord_id, item_id, quantity, purchase_price) "
                            "VALUES (?, ?, 1, 0)",
                            (discord_id, won_item_id),
                        )
                        new_balance = await flow_wallet.get_balance(
                            discord_id, con=db
                        )

                    # Transaction log
                    await db.execute(
                        "INSERT INTO store_transactions "
                        "(discord_id, item_id, action, coins_delta, "
                        "balance_after, metadata) "
                        "VALUES (?, ?, 'consume', ?, ?, ?)",
                        (
                            discord_id, inv["item_id"],
                            coins_awarded if is_dupe else 0,
                            new_balance,
                            json.dumps({
                                "lootbox_item": inv["item_id"],
                                "won_item_id": won_item_id,
                                "won_rarity": chosen.get("rarity", "common"),
                                "is_dupe": is_dupe,
                                "coins_awarded": coins_awarded,
                            }),
                        ),
                    )

                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        return {
            "ok": True,
            "error": None,
            "won_item": won_item,
            "is_dupe": is_dupe,
            "coins_awarded": coins_awarded,
        }

    # =========================================================================
    #  EXPIRATION TASK
    # =========================================================================

    @tasks.loop(minutes=5)
    async def _expiration_task(self):
        """Expire stale effects and handle cleanup."""
        try:
            count = await asyncio.to_thread(expire_stale_effects)
            if count:
                log.info("[Store] Expired %d stale effect(s)", count)

            # TODO Phase 3: Discord role removal and nickname reset
            # for each expired role/nickname effect, wrap Discord API
            # calls in try/except per user (handle left server,
            # missing permissions, deleted role, hierarchy issues).

        except Exception:
            log.exception("[Store] Expiration task error")

    @_expiration_task.before_loop
    async def _before_expiration(self):
        await self.bot.wait_until_ready()

    # =========================================================================
    #  PERSISTENT VIEW STUBS (Phase 2)
    # =========================================================================

    async def _get_store_message_id(self, guild_id: int) -> Optional[int]:
        """Retrieve the stored storefront message ID for a guild."""
        async with aiosqlite.connect(DB_PATH, timeout=_DB_TIMEOUT) as db:
            async with db.execute(
                "SELECT value FROM sportsbook_settings WHERE key=?",
                (f"store_msg_{guild_id}",),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else None

    async def _set_store_message_id(
        self, guild_id: int, message_id: int
    ) -> None:
        """Store the storefront message ID for a guild."""
        async with aiosqlite.connect(DB_PATH, timeout=_DB_TIMEOUT) as db:
            await db.execute(
                "INSERT OR REPLACE INTO sportsbook_settings (key, value) "
                "VALUES (?, ?)",
                (f"store_msg_{guild_id}", str(message_id)),
            )
            await db.commit()


# =============================================================================
#  EXTENSION SETUP
# =============================================================================

async def setup(bot: commands.Bot):
    await bot.add_cog(FlowStoreCog(bot))
