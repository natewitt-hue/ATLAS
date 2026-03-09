"""
polymarket_cog.py — ATLAS Flow Casino: Prediction Market Module
Part of the ATLAS framework for The Simulation League (TSL)

Integrates real-world event data from Polymarket's Gamma API so users can
bet TSL Bucks on prediction markets across Politics, Sports, Crypto, and more.

Replaces the former Kalshi integration. Polymarket Gamma API is fully public —
no API keys or authentication required.

Author: TheWitt
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands

import aiohttp
import aiosqlite
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import os

from casino.casino_db import (
    DB_PATH,
    get_balance as _casino_get_balance,
    InsufficientFundsError,
)

log = logging.getLogger("polymarket_cog")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

POLYMARKET_GAMMA_BASE = "https://gamma-api.polymarket.com"

# Channels
PREDICTION_CHANNEL_ID = int(os.getenv("PREDICTION_MARKET_CHANNEL_ID", "0"))

# How many TSL Bucks = 1 full contract payout
# A YES at $0.65 costs 65 TSL Bucks; pays out 100 TSL Bucks if correct.
PAYOUT_SCALE = 100

# Category mapping: Polymarket categories → display labels
CATEGORY_MAP = {
    "politics":    "🏛️ Politics",
    "sports":      "⚽ Sports",
    "pop culture": "🎬 Entertainment",
    "crypto":      "🪙 Crypto",
    "business":    "📈 Economics",
    "science":     "🔬 Science",
    "tech":        "💻 Tech",
    "world":       "🌍 World",
}

CATEGORY_COLORS = {
    "🏛️ Politics":     0x3498DB,
    "⚽ Sports":        0x2ECC71,
    "🎬 Entertainment": 0xE91E63,
    "🪙 Crypto":        0xF39C12,
    "📈 Economics":     0x2ECC71,
    "🔬 Science":       0x9B59B6,
    "💻 Tech":          0x1ABC9C,
    "🌍 World":         0xE67E22,
    "🌐 Other":         0x95A5A6,
}

MARKETS_PER_PAGE = 5   # Embeds shown per browse page

# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────

async def init_prediction_db(db_path: str = DB_PATH):
    """Create prediction market tables if they don't exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS prediction_markets (
                market_id       TEXT PRIMARY KEY,
                event_id        TEXT,
                slug            TEXT NOT NULL,
                title           TEXT NOT NULL,
                category        TEXT DEFAULT 'Other',
                yes_price       REAL DEFAULT 0.5,
                no_price        REAL DEFAULT 0.5,
                volume          REAL DEFAULT 0,
                liquidity       REAL DEFAULT 0,
                end_date        TEXT,
                status          TEXT DEFAULT 'active',
                result          TEXT,
                resolved_by     TEXT DEFAULT 'pending',
                last_synced     TEXT
            );

            CREATE TABLE IF NOT EXISTS prediction_contracts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL,
                market_id       TEXT NOT NULL,
                slug            TEXT NOT NULL,
                side            TEXT NOT NULL CHECK(side IN ('YES','NO')),
                buy_price       REAL NOT NULL,
                quantity        INTEGER NOT NULL DEFAULT 1,
                cost_bucks      INTEGER NOT NULL,
                potential_payout INTEGER NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open'
                                CHECK(status IN ('open','won','lost','voided')),
                created_at      TEXT NOT NULL,
                resolved_at     TEXT,
                FOREIGN KEY (market_id) REFERENCES prediction_markets(market_id)
            );

            CREATE INDEX IF NOT EXISTS idx_pred_contracts_user
                ON prediction_contracts(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_pred_contracts_market
                ON prediction_contracts(market_id, status);
            CREATE INDEX IF NOT EXISTS idx_pred_markets_status
                ON prediction_markets(status, category);
        """)
    log.info("Prediction market DB tables ready.")


# ─────────────────────────────────────────────
# TSL BUCKS HELPERS (delegates to casino_db)
# ─────────────────────────────────────────────

async def get_balance(user_id: str) -> int:
    """Return the current TSL Bucks balance for a user."""
    return await _casino_get_balance(int(user_id))


async def update_balance(user_id: str, delta: int):
    """
    Add `delta` (positive = credit, negative = debit) to a user's balance.
    Raises ValueError if the resulting balance would go negative.
    """
    uid = int(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT balance FROM users_table WHERE discord_id=?", (uid,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                raise ValueError("No casino account found. Use the casino first to create one.")
            new_balance = row[0] + delta
            if new_balance < 0:
                raise ValueError("Insufficient TSL Bucks")
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, uid)
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# ─────────────────────────────────────────────
# POLYMARKET GAMMA API CLIENT
# ─────────────────────────────────────────────

class PolymarketClient:
    """
    Async wrapper around the Polymarket Gamma API.

    The Gamma API is fully public — no authentication required.
    Base URL: https://gamma-api.polymarket.com
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict | None = None) -> Optional[list | dict]:
        """
        GET request to the Gamma API.
        Returns parsed JSON (list or dict), or None on error.
        """
        session = await self._session_get()
        try:
            async with session.get(
                f"{POLYMARKET_GAMMA_BASE}{path}",
                params=params,
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f"Polymarket GET {path} → {resp.status}: {text[:200]}")
                    return None
                return await resp.json()
        except Exception as e:
            log.error(f"Polymarket GET {path} exception: {e}")
            return None

    # ── Public API methods ────────────────────────────────────────────────

    async def fetch_active_events(self, limit: int = 100) -> list[dict]:
        """Fetch active events with their nested markets."""
        data = await self._get("/events", params={
            "active": "true",
            "limit": limit,
            "closed": "false",
        })
        return data if isinstance(data, list) else []

    async def fetch_active_markets(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch active markets directly."""
        data = await self._get("/markets", params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        })
        return data if isinstance(data, list) else []

    async def fetch_closed_markets(self, limit: int = 100) -> list[dict]:
        """Fetch recently closed markets for auto-resolution."""
        data = await self._get("/markets", params={
            "closed": "true",
            "limit": limit,
            "order": "endDate",
            "ascending": "false",
        })
        return data if isinstance(data, list) else []

    async def fetch_market_by_id(self, market_id: str) -> Optional[dict]:
        """Fetch a single market by its ID."""
        data = await self._get(f"/markets/{market_id}")
        return data if isinstance(data, dict) else None

    async def fetch_market_by_slug(self, slug: str) -> Optional[dict]:
        """Fetch a single market by slug."""
        data = await self._get("/markets", params={"slug": slug})
        if isinstance(data, list) and data:
            return data[0]
        return None


# ─────────────────────────────────────────────
# PRICING / HELPERS
# ─────────────────────────────────────────────

def extract_prices(market: dict) -> dict:
    """
    Extract YES/NO prices from a Polymarket market dict.
    Polymarket uses outcomePrices as a JSON array: ["0.565", "0.435"]
    Index 0 = YES, Index 1 = NO.
    Also has bestBid/bestAsk for the YES side.
    """
    yes_price = 0.5
    no_price = 0.5

    outcome_prices = market.get("outcomePrices")
    if outcome_prices:
        # outcomePrices can be a JSON string or already parsed list
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except (json.JSONDecodeError, TypeError):
                outcome_prices = None

        if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
            try:
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])
            except (ValueError, TypeError):
                pass

    return {
        "yes_price": round(yes_price, 4),
        "no_price":  round(no_price, 4),
    }


def price_to_bucks(price: float) -> int:
    """Convert a price (0.0–1.0) to TSL Bucks cost."""
    return max(1, round(price * PAYOUT_SCALE))


def map_category(raw_category: str) -> str:
    """Map a Polymarket category string to a display label."""
    if not raw_category:
        return "🌐 Other"
    key = raw_category.lower().strip()
    return CATEGORY_MAP.get(key, f"🌐 {raw_category.title()}")


def market_status(market: dict) -> str:
    """Derive a status string from Polymarket boolean fields."""
    if market.get("archived"):
        return "archived"
    if market.get("closed"):
        return "closed"
    if market.get("active"):
        return "active"
    return "unknown"


def detect_result(market: dict) -> Optional[str]:
    """
    Detect the outcome of a closed market from its outcomePrices.
    When resolved, prices go to 1.0/0.0 (or very close).
    Returns 'yes', 'no', or None if not clearly resolved.
    """
    if not market.get("closed"):
        return None

    prices = extract_prices(market)
    yes_p = prices["yes_price"]
    no_p = prices["no_price"]

    # Threshold: price > 0.95 is considered a win
    if yes_p >= 0.95 and no_p <= 0.05:
        return "yes"
    if no_p >= 0.95 and yes_p <= 0.05:
        return "no"

    return None


def fmt_volume(vol) -> str:
    """Format volume for display."""
    try:
        v = float(vol or 0)
    except (ValueError, TypeError):
        return "0"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


# ─────────────────────────────────────────────
# DISCORD UI COMPONENTS
# ─────────────────────────────────────────────

class WagerModal(discord.ui.Modal):
    """Modal that asks the user how many contracts to buy."""

    amount_input = discord.ui.TextInput(
        label="How many contracts? (1 contract = 1 TSL Buck unit)",
        placeholder="e.g. 10",
        min_length=1,
        max_length=6,
        required=True,
    )

    def __init__(self, market_id: str, slug: str, side: str, price: float, title: str):
        super().__init__(title=f"Buy {side} — {title[:40]}")
        self.market_id    = market_id
        self.slug         = slug
        self.side         = side
        self.price        = price
        self.market_title = title

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount_input.value.strip()
        if not raw.isdigit() or int(raw) < 1:
            await interaction.response.send_message(
                "❌ Please enter a whole number ≥ 1.", ephemeral=True
            )
            return

        quantity    = int(raw)
        cost_bucks  = price_to_bucks(self.price) * quantity
        payout      = PAYOUT_SCALE * quantity
        user_id     = str(interaction.user.id)

        # Balance check
        try:
            balance = await get_balance(user_id)
        except Exception:
            balance = 0

        if balance < cost_bucks:
            await interaction.response.send_message(
                f"❌ You need **{cost_bucks:,} TSL Bucks** but only have **{balance:,}**.",
                ephemeral=True,
            )
            return

        # Debit
        try:
            await update_balance(user_id, -cost_bucks)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        # Write contract to DB
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO prediction_contracts
                    (user_id, market_id, slug, side, buy_price,
                     quantity, cost_bucks, potential_payout, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """, (
                user_id, self.market_id, self.slug,
                self.side, self.price,
                quantity, cost_bucks, payout, now,
            ))
            await db.commit()

        color  = 0x2ECC71 if self.side == "YES" else 0xE74C3C
        symbol = "✅" if self.side == "YES" else "❌"
        embed  = discord.Embed(
            title=f"{symbol} Contract Purchased",
            color=color,
            description=(
                f"**{self.market_title}**\n"
                f"Side: **{self.side}** · Price: **{self.price:.1%}** each\n"
                f"Quantity: **{quantity}** · Cost: **{cost_bucks:,} TSL Bucks**\n"
                f"Potential payout: **{payout:,} TSL Bucks** if {self.side} wins"
            ),
        )
        embed.set_footer(text="Use /portfolio to view your open contracts.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BetButtonView(discord.ui.View):
    """YES / NO buttons on the /bet embed."""

    def __init__(self, market_id: str, slug: str, title: str,
                 yes_price: float, no_price: float):
        super().__init__(timeout=300)
        self.market_id = market_id
        self.slug      = slug
        self.title     = title
        self.yes_price = yes_price
        self.no_price  = no_price

    @discord.ui.button(label="Buy YES ✅", style=discord.ButtonStyle.success)
    async def buy_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WagerModal(
            market_id=self.market_id, slug=self.slug, side="YES",
            price=self.yes_price, title=self.title
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Buy NO ❌", style=discord.ButtonStyle.danger)
    async def buy_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WagerModal(
            market_id=self.market_id, slug=self.slug, side="NO",
            price=self.no_price, title=self.title
        )
        await interaction.response.send_modal(modal)


class CategorySelect(discord.ui.Select):
    """Dropdown to filter markets by category."""

    def __init__(self, categories: list[str], parent_view):
        options = [discord.SelectOption(label="All Categories", value="all", default=True)]
        for cat in categories:
            options.append(discord.SelectOption(label=cat, value=cat))
        super().__init__(
            placeholder="Filter by category…",
            options=options[:25],
            min_values=1,
            max_values=1,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        await self.parent_view.apply_filter(interaction, chosen)


class MarketBrowserView(discord.ui.View):
    """Full market browser: category filter + page navigation."""

    def __init__(self, all_markets: list[dict], categories: list[str]):
        super().__init__(timeout=600)
        self.all_markets = all_markets
        self.categories  = categories
        self.filter      = "all"
        self.page        = 0

        self.cat_select = CategorySelect(categories, parent_view=self)
        self.add_item(self.cat_select)
        self._build_nav()

    def _filtered(self) -> list[dict]:
        if self.filter == "all":
            return self.all_markets
        return [m for m in self.all_markets if m.get("category") == self.filter]

    def _max_page(self) -> int:
        return max(0, (len(self._filtered()) - 1) // MARKETS_PER_PAGE)

    def _build_nav(self):
        to_remove = [c for c in self.children
                     if getattr(c, "custom_id", "") in ("nav_prev", "nav_next")]
        for item in to_remove:
            self.remove_item(item)

        prev_btn = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_prev",
            disabled=self.page == 0,
            row=1,
        )
        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_next",
            disabled=self.page >= self._max_page(),
            row=1,
        )
        prev_btn.callback = self._prev
        next_btn.callback = self._next
        self.add_item(prev_btn)
        self.add_item(next_btn)

    async def apply_filter(self, interaction: discord.Interaction, cat: str):
        self.filter = cat
        self.page   = 0
        self._build_nav()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._build_nav()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self._max_page(), self.page + 1)
        self._build_nav()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    def _embed(self) -> discord.Embed:
        markets = self._filtered()
        total   = len(markets)
        start   = self.page * MARKETS_PER_PAGE
        chunk   = markets[start : start + MARKETS_PER_PAGE]

        cat_label = self.filter if self.filter != "all" else "All Categories"
        embed = discord.Embed(
            title="📊 ATLAS Flow — Prediction Markets (Polymarket)",
            description=(
                f"**{cat_label}** · {total} active markets · "
                f"Page {self.page+1}/{self._max_page()+1}"
            ),
            color=CATEGORY_COLORS.get(cat_label, 0x7289DA),
        )

        for m in chunk:
            yes_bucks = price_to_bucks(m["yes_price"])
            no_bucks  = price_to_bucks(m["no_price"])
            end = m.get("end_date", "")
            try:
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                end_str = f"<t:{int(end_dt.timestamp())}:R>"
            except Exception:
                end_str = end or "No expiry"

            embed.add_field(
                name=f"{m.get('category', '🌐 Other')}  {m['title'][:60]}",
                value=(
                    f"ID: `{m['slug'][:50]}`\n"
                    f"YES: **{yes_bucks}¢** ({m['yes_price']:.0%}) · "
                    f"NO: **{no_bucks}¢** ({m['no_price']:.0%}) · "
                    f"Vol: {fmt_volume(m.get('volume', 0))}\n"
                    f"Ends: {end_str}"
                ),
                inline=False,
            )

        if not chunk:
            embed.add_field(
                name="No markets found",
                value="Try a different category.",
                inline=False,
            )

        embed.set_footer(text="Use /bet <slug> to place a wager on any market.")
        return embed


# ─────────────────────────────────────────────
# THE COG
# ─────────────────────────────────────────────

class PolymarketCog(commands.Cog, name="Polymarket"):
    """
    ATLAS Flow Casino — Prediction Market Module.
    Syncs live market data from Polymarket every 5 minutes.
    No API key required — Polymarket Gamma API is fully public.
    """

    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self.client = PolymarketClient()
        self._db_ready = False
        self.sync_markets.start()

    def cog_unload(self):
        self.sync_markets.cancel()
        asyncio.create_task(self.client.close())

    async def _ensure_db(self):
        if not self._db_ready:
            await init_prediction_db()
            self._db_ready = True

    # ── Background Sync ───────────────────────

    @tasks.loop(minutes=5)
    async def sync_markets(self):
        """Pull fresh market data from Polymarket, upsert prices, auto-resolve."""
        await self._ensure_db()
        log.info("Syncing Polymarket markets…")

        # ── Pass 1: Fetch active events with nested markets ──────────────
        events = await self.client.fetch_active_events(limit=100)
        if not events:
            log.warning("Polymarket sync returned 0 events — trying direct markets fetch.")
            # Fallback: fetch markets directly
            markets_direct = await self.client.fetch_active_markets(limit=200)
            if markets_direct:
                events = [{"id": "direct", "markets": markets_direct}]

        now = datetime.now(timezone.utc).isoformat()
        upserted = 0

        async with aiosqlite.connect(DB_PATH) as db:
            for event in events:
                event_id = str(event.get("id", ""))
                event_category = map_category(event.get("category", ""))

                nested_markets = event.get("markets", [])
                if not nested_markets:
                    continue

                for mkt in nested_markets:
                    market_id = str(mkt.get("id", ""))
                    slug = mkt.get("slug", "")
                    if not market_id or not slug:
                        continue

                    prices = extract_prices(mkt)
                    title = mkt.get("question", "") or mkt.get("title", slug)
                    category = map_category(mkt.get("category", "")) or event_category
                    status = market_status(mkt)
                    end_date = mkt.get("endDate", "") or mkt.get("end_date_iso", "")
                    volume = mkt.get("volumeNum") or mkt.get("volume") or 0
                    liquidity = mkt.get("liquidityNum") or mkt.get("liquidity") or 0

                    try:
                        volume = float(volume)
                    except (ValueError, TypeError):
                        volume = 0
                    try:
                        liquidity = float(liquidity)
                    except (ValueError, TypeError):
                        liquidity = 0

                    await db.execute("""
                        INSERT INTO prediction_markets
                            (market_id, event_id, slug, title, category,
                             yes_price, no_price, volume, liquidity,
                             end_date, status, last_synced)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(market_id) DO UPDATE SET
                            slug       = excluded.slug,
                            title      = excluded.title,
                            category   = excluded.category,
                            yes_price  = excluded.yes_price,
                            no_price   = excluded.no_price,
                            volume     = excluded.volume,
                            liquidity  = excluded.liquidity,
                            end_date   = excluded.end_date,
                            status     = excluded.status,
                            last_synced= excluded.last_synced
                    """, (
                        market_id, event_id, slug, title, category,
                        prices["yes_price"], prices["no_price"],
                        volume, liquidity, end_date, status, now,
                    ))
                    upserted += 1

            await db.commit()

        log.info(f"Polymarket sync complete — {upserted} active markets upserted.")

        # ── Pass 2: Auto-resolve closed markets ──────────────────────────
        await self._auto_resolve_pass()

    async def _auto_resolve_pass(self):
        """
        Fetch recently closed Polymarket markets, detect results from
        outcomePrices, and auto-resolve any with open contracts.
        """
        closed_markets = await self.client.fetch_closed_markets(limit=100)
        if not closed_markets:
            return

        auto_resolved = []

        for mkt in closed_markets:
            market_id = str(mkt.get("id", ""))
            if not market_id:
                continue

            result = detect_result(mkt)
            if not result:
                continue  # Not clearly resolved yet

            # Check our DB: only act on markets still marked pending
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT resolved_by FROM prediction_markets WHERE market_id = ?",
                    (market_id,)
                ) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    # Market not in our DB — upsert it
                    prices = extract_prices(mkt)
                    slug = mkt.get("slug", "")
                    title = mkt.get("question", "") or mkt.get("title", slug)
                    category = map_category(mkt.get("category", ""))
                    now = datetime.now(timezone.utc).isoformat()
                    await db.execute("""
                        INSERT OR IGNORE INTO prediction_markets
                            (market_id, slug, title, category,
                             yes_price, no_price, status, result,
                             resolved_by, last_synced)
                        VALUES (?,?,?,?,?,?,'closed',?,?,?)
                    """, (
                        market_id, slug, title, category,
                        prices["yes_price"], prices["no_price"],
                        result, "pending", now,
                    ))
                    await db.commit()
                    resolved_by = "pending"
                else:
                    resolved_by = row[0] if row else "pending"

                if resolved_by != "pending":
                    continue  # Already resolved

                # Check there are open contracts to settle
                async with db.execute(
                    "SELECT COUNT(*) FROM prediction_contracts "
                    "WHERE market_id = ? AND status = 'open'",
                    (market_id,)
                ) as cursor:
                    open_count = (await cursor.fetchone())[0]

                if open_count == 0:
                    # Mark resolved even with no contracts
                    await db.execute(
                        "UPDATE prediction_markets "
                        "SET status='closed', result=?, resolved_by='auto' "
                        "WHERE market_id=?",
                        (result, market_id)
                    )
                    await db.commit()
                    continue

            # ── Settle it ──────────────────────────────────────────────
            result_upper = result.upper()
            log.info(
                f"Auto-resolving {market_id} → {result_upper} "
                f"({open_count} open contracts)"
            )

            counts = await self._resolve(market_id, result_upper)

            # Mark market as auto-resolved
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE prediction_markets "
                    "SET resolved_by = 'auto', result = ? "
                    "WHERE market_id = ?",
                    (result, market_id)
                )
                await db.commit()

            title = mkt.get("question", "") or mkt.get("title", market_id)
            auto_resolved.append({
                "market_id": market_id,
                "result":    result_upper,
                "counts":    counts,
                "title":     title,
            })

        if auto_resolved:
            log.info(
                f"Auto-resolve pass complete — {len(auto_resolved)} market(s) settled."
            )
            await self._announce_resolutions(auto_resolved)

    async def _announce_resolutions(self, resolved: list[dict]):
        """Post a public resolution announcement for each auto-resolved market."""
        ch = self._channel()
        if not ch:
            log.warning("No prediction market channel — skipping announcement.")
            return

        for item in resolved:
            market_id = item["market_id"]
            result    = item["result"]
            counts    = item["counts"]
            title     = item["title"]

            won    = counts.get("won", 0)
            lost   = counts.get("lost", 0)
            voided = counts.get("voided", 0)

            color  = 0x57F287 if result == "YES" else 0xED4245
            symbol = "✅" if result == "YES" else "❌"

            embed = discord.Embed(
                title="🏆 Market Resolved",
                color=color,
                description=(
                    f"**{title}**\n"
                    f"Result: **{symbol} {result}**\n\n"
                    f"**{won}** winning position(s) paid out · "
                    f"**{lost}** position(s) lost"
                    + (f" · **{voided}** voided" if voided else "")
                ),
            )

            # Show top winners
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT user_id, quantity, potential_payout, cost_bucks "
                    "FROM prediction_contracts "
                    "WHERE market_id = ? AND status = 'won' "
                    "ORDER BY potential_payout DESC LIMIT 5",
                    (market_id,)
                ) as cursor:
                    winners = await cursor.fetchall()

            if winners:
                lines = []
                for uid, qty, payout, cost in winners:
                    profit = payout - cost
                    member = ch.guild.get_member(int(uid))
                    name   = member.display_name if member else f"<@{uid}>"
                    lines.append(
                        f"💰 **{name}** — {qty} contract(s) · "
                        f"Payout: **{payout:,} TSL Bucks** · Profit: **+{profit:,} 🪙**"
                    )
                embed.add_field(
                    name="🏅 Winners",
                    value="\n".join(lines),
                    inline=False,
                )

            embed.set_footer(text="Winnings automatically credited · ATLAS Flow Casino")
            embed.timestamp = datetime.now(timezone.utc)

            try:
                await ch.send(embed=embed)
            except Exception as e:
                log.error(f"Failed to post resolution announcement: {e}")

    @sync_markets.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()

    # ── Utility ─────────────────────────────────

    def _channel(self):
        return self.bot.get_channel(PREDICTION_CHANNEL_ID)

    # ── Slash: /markets ───────────────────────

    @app_commands.command(
        name="markets",
        description="Browse live Polymarket prediction markets."
    )
    async def markets_cmd(self, interaction: discord.Interaction):
        await self._ensure_db()
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT market_id, slug, title, category,
                       yes_price, no_price, volume, end_date
                FROM prediction_markets
                WHERE status = 'active'
                ORDER BY volume DESC
                LIMIT 200
            """) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send(
                "⚠️ No markets synced yet. Try again in a moment.",
                ephemeral=True,
            )
            return

        markets = [
            {
                "market_id": r[0],
                "slug":      r[1],
                "title":     r[2],
                "category":  r[3],
                "yes_price": r[4] if r[4] is not None else 0.5,
                "no_price":  r[5] if r[5] is not None else 0.5,
                "volume":    r[6] or 0,
                "end_date":  r[7] or "",
            }
            for r in rows
        ]

        categories = sorted({m["category"] for m in markets})
        view       = MarketBrowserView(markets, categories)

        await interaction.followup.send(embed=view._embed(), view=view, ephemeral=True)

    # ── Slash: /bet <slug> ──────────────────

    @app_commands.command(
        name="bet",
        description="Place a TSL Bucks wager on a prediction market."
    )
    @app_commands.describe(slug="The market slug/ID from /markets")
    async def bet_cmd(self, interaction: discord.Interaction, slug: str):
        await self._ensure_db()
        slug = slug.strip().lower()

        async with aiosqlite.connect(DB_PATH) as db:
            # Try exact slug match first, then partial match
            async with db.execute(
                "SELECT market_id, slug, title, category, yes_price, no_price, "
                "volume, end_date, status "
                "FROM prediction_markets WHERE slug = ?",
                (slug,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                # Try partial slug match
                async with db.execute(
                    "SELECT market_id, slug, title, category, yes_price, no_price, "
                    "volume, end_date, status "
                    "FROM prediction_markets WHERE slug LIKE ? AND status = 'active' "
                    "LIMIT 1",
                    (f"%{slug}%",)
                ) as cursor:
                    row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(
                f"❌ Market `{slug}` not found. Use `/markets` to browse.",
                ephemeral=True,
            )
            return

        market_id, mkt_slug, title, category, yes_price, no_price, volume, end_date, status = row

        if status != "active":
            await interaction.response.send_message(
                f"⚠️ Market is **{status}** and not accepting new bets.",
                ephemeral=True,
            )
            return

        yes_price = yes_price if yes_price is not None else 0.5
        no_price  = no_price  if no_price  is not None else 0.5
        yes_bucks = price_to_bucks(yes_price)
        no_bucks  = price_to_bucks(no_price)

        color = CATEGORY_COLORS.get(category, 0x7289DA)
        try:
            end_dt  = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            end_str = f"<t:{int(end_dt.timestamp())}:R>"
        except Exception:
            end_str = end_date or "No expiry"

        embed = discord.Embed(
            title=f"📊 {title}",
            color=color,
            description=(
                f"**ID:** `{mkt_slug[:60]}`\n"
                f"**Category:** {category}\n"
                f"**Ends:** {end_str}\n"
                f"**Volume:** {fmt_volume(volume)}\n\n"
                f"Each contract pays out **{PAYOUT_SCALE} TSL Bucks** if your side wins."
            ),
        )
        embed.add_field(
            name="✅ Buy YES",
            value=f"**{yes_bucks} TSL Bucks** per contract\n*(implied: {yes_price:.1%})*",
            inline=True,
        )
        embed.add_field(
            name="❌ Buy NO",
            value=f"**{no_bucks} TSL Bucks** per contract\n*(implied: {no_price:.1%})*",
            inline=True,
        )
        embed.set_footer(text="Click a button below to open the wager modal.")

        view = BetButtonView(
            market_id=market_id, slug=mkt_slug, title=title,
            yes_price=yes_price, no_price=no_price,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── Slash: /portfolio ─────────────────────

    @app_commands.command(
        name="portfolio",
        description="View your open prediction market contracts."
    )
    async def portfolio_cmd(self, interaction: discord.Interaction):
        await self._ensure_db()
        user_id = str(interaction.user.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT pc.market_id, pm.title, pc.side, pc.buy_price,
                       pc.quantity, pc.cost_bucks, pc.potential_payout,
                       pc.status, pc.created_at
                FROM prediction_contracts pc
                LEFT JOIN prediction_markets pm ON pm.market_id = pc.market_id
                WHERE pc.user_id = ?
                ORDER BY pc.created_at DESC
                LIMIT 20
            """, (user_id,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "You have no prediction market contracts. Use `/bet` to place one!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📋 Your Prediction Market Portfolio",
            color=0x3498DB,
        )

        for r in rows:
            mid, title, side, buy_price, qty, cost, payout, status, created = r
            sym   = "✅" if side == "YES" else "❌"
            s_map = {
                "open": "🟡 Open", "won": "🏆 Won",
                "lost": "💸 Lost", "voided": "🔁 Voided",
            }
            embed.add_field(
                name=f"{sym} {(title or mid)[:50]}",
                value=(
                    f"**{side}** · {qty} contract(s)\n"
                    f"Paid: **{cost:,} TSL Bucks** · "
                    f"Potential: **{payout:,} TSL Bucks** · "
                    f"{s_map.get(status, status)}"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Slash: /resolve_market (admin) ────────

    @app_commands.command(
        name="resolve_market",
        description="[Admin] Resolve a prediction market outcome."
    )
    @app_commands.describe(
        slug="Market slug to resolve",
        result="The winning side: YES or NO, or VOID to refund all",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def resolve_market_cmd(
        self,
        interaction: discord.Interaction,
        slug: str,
        result: str,
    ):
        await self._ensure_db()
        slug   = slug.strip().lower()
        result = result.upper().strip()

        if result not in ("YES", "NO", "VOID"):
            await interaction.response.send_message(
                "❌ `result` must be YES, NO, or VOID.", ephemeral=True
            )
            return

        # Look up market_id from slug
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT market_id, title FROM prediction_markets WHERE slug = ?",
                (slug,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(
                f"❌ Market `{slug}` not found.", ephemeral=True
            )
            return

        market_id, title = row

        await interaction.response.defer(ephemeral=True)
        resolved = await self._resolve(market_id, result, resolved_by="admin")

        await self._announce_resolutions([{
            "market_id": market_id,
            "result":    result,
            "counts":    resolved,
            "title":     title or slug,
        }])

        await interaction.followup.send(
            f"✅ Resolved `{slug}` as **{result}**. "
            f"Processed {resolved['won']} winning and {resolved['lost']} losing contracts."
            + (f" Voided {resolved['voided']}." if resolved["voided"] else ""),
            ephemeral=True,
        )

    async def _resolve(self, market_id: str, result: str,
                       resolved_by: str = "auto") -> dict:
        """
        Settle all open contracts for a market.
        result: 'YES' | 'NO' | 'VOID'
        Returns counts of won/lost/voided.
        """
        now = datetime.now(timezone.utc).isoformat()
        counts = {"won": 0, "lost": 0, "voided": 0}

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, user_id, side, quantity, cost_bucks, potential_payout "
                "FROM prediction_contracts "
                "WHERE market_id = ? AND status = 'open'",
                (market_id,)
            ) as cursor:
                contracts = await cursor.fetchall()

            for cid, user_id, side, qty, cost, payout in contracts:
                if result == "VOID":
                    await update_balance(user_id, cost)
                    await db.execute(
                        "UPDATE prediction_contracts "
                        "SET status='voided', resolved_at=? WHERE id=?",
                        (now, cid)
                    )
                    counts["voided"] += 1
                elif side == result:
                    await update_balance(user_id, payout)
                    await db.execute(
                        "UPDATE prediction_contracts "
                        "SET status='won', resolved_at=? WHERE id=?",
                        (now, cid)
                    )
                    counts["won"] += 1
                else:
                    await db.execute(
                        "UPDATE prediction_contracts "
                        "SET status='lost', resolved_at=? WHERE id=?",
                        (now, cid)
                    )
                    counts["lost"] += 1

            # Mark market as resolved
            await db.execute(
                "UPDATE prediction_markets "
                "SET status='closed', resolved_by=? WHERE market_id=?",
                (resolved_by, market_id)
            )
            await db.commit()

        log.info(f"_resolve({market_id}, {result}, by={resolved_by}): {counts}")
        return counts

    # ── Slash: /market_status (admin) ─────────

    @app_commands.command(
        name="market_status",
        description="[Admin] Show Polymarket sync status and stats."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def market_status_cmd(self, interaction: discord.Interaction):
        await self._ensure_db()
        await interaction.response.defer(ephemeral=True)

        # Test connectivity
        test = await self.client.fetch_active_markets(limit=1)
        api_ok = test is not None

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*), MAX(last_synced) "
                "FROM prediction_markets WHERE status='active'"
            ) as cursor:
                count, last_sync = await cursor.fetchone()

        embed = discord.Embed(
            title="📊 Polymarket Integration Status",
            color=0x2ECC71 if api_ok else 0xE74C3C,
        )
        embed.add_field(
            name="Auth Method",
            value="🔓 Public API (no auth needed)",
            inline=False,
        )
        embed.add_field(
            name="API Connectivity",
            value="✅ Connected" if api_ok else "❌ Failed (check network/logs)",
            inline=True,
        )
        embed.add_field(
            name="Synced Markets",
            value=f"{count or 0} active",
            inline=True,
        )
        embed.add_field(
            name="Last Sync",
            value=last_sync or "Never",
            inline=True,
        )
        embed.add_field(
            name="Next Sync",
            value=f"<t:{int((datetime.now(timezone.utc).timestamp() // 300 + 1) * 300)}:R>",
            inline=True,
        )
        embed.add_field(
            name="Data Source",
            value=f"`{POLYMARKET_GAMMA_BASE}`",
            inline=False,
        )
        embed.set_footer(text="Polymarket Gamma API — No API key required")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PolymarketCog(bot))
    log.info("PolymarketCog loaded.")
