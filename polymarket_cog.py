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
import re

from google import genai

import flow_wallet
from flow_wallet import (
    DB_PATH,
    InsufficientFundsError,
)
from casino.renderer.prediction_card_renderer import render_market_page

log = logging.getLogger("polymarket_cog")

# ── Gemini AI Client (lazy singleton) ──
_GEMINI_CLIENT = None

def _get_gemini_client():
    """Return a cached Gemini client, or None if API key unavailable."""
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — AI market analysis unavailable")
        return None
    try:
        _GEMINI_CLIENT = genai.Client(api_key=api_key)
        return _GEMINI_CLIENT
    except Exception:
        log.warning("Failed to initialise Gemini client for polymarket", exc_info=True)
        return None


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
# Keys are lowercase — map_category() lowercases + normalizes hyphens before lookup
CATEGORY_MAP = {
    # Core
    "politics":            "🏛️ Politics",
    "sports":              "⚽ Sports",
    "pop culture":         "🎬 Entertainment",
    "pop-culture":         "🎬 Entertainment",
    "entertainment":       "🎬 Entertainment",
    "culture":             "🎬 Entertainment",
    "crypto":              "🪙 Crypto",
    "business":            "📈 Economics",
    "finance":             "📈 Economics",
    "economics":           "📈 Economics",
    "economy":             "📈 Economics",
    "fed rates":           "📈 Economics",
    "fomc":                "📈 Economics",
    "economic policy":     "📈 Economics",
    "jerome powell":       "📈 Economics",
    "fed":                 "📈 Economics",
    "science":             "🔬 Science",
    "health":              "🔬 Science",
    "tech":                "💻 Tech",
    "ai":                  "🤖 AI",
    "artificial intelligence": "🤖 AI",
    "world":               "🌍 World",
    "climate":             "🌍 World",
    "iran":                "🌍 World",
    # Politics variants
    "us-current-affairs":  "🏛️ Politics",
    "us current affairs":  "🏛️ Politics",
    "elections":           "🏛️ Politics",
    # Sports sub-categories
    "nfl":                 "🏈 NFL",
    "nba":                 "🏀 NBA",
    "mlb":                 "⚾ MLB",
    "nhl":                 "🏒 NHL",
    "hockey":              "🏒 NHL",
    "soccer":              "⚽ Soccer",
    "football":            "🏈 NFL",
    "basketball":          "🏀 NBA",
    "baseball":            "⚾ MLB",
    "epl":                 "⚽ Soccer",
    "premier league":      "⚽ Soccer",
    "mma":                 "🥊 MMA",
    "boxing":              "🥊 MMA",
    "ufc":                 "🥊 MMA",
    "chess":               "♟️ Chess",
    # Gaming
    "gaming":              "🎮 Gaming",
    "esports":             "🎮 Gaming",
}

CATEGORY_COLORS = {
    "🏛️ Politics":     0x3498DB,
    "⚽ Sports":        0x2ECC71,
    "🎬 Entertainment": 0xE91E63,
    "🪙 Crypto":        0xF39C12,
    "📈 Economics":     0x27AE60,
    "🔬 Science":       0x9B59B6,
    "💻 Tech":          0x1ABC9C,
    "🤖 AI":            0x00CED1,
    "🌍 World":         0xE67E22,
    "🏈 NFL":           0x013369,
    "🏀 NBA":           0xC9082A,
    "⚾ MLB":           0x002D72,
    "🥊 MMA":           0xD4AF37,
    "♟️ Chess":         0x8B4513,
    "🎮 Gaming":        0x7B68EE,
    "🏒 NHL":           0x000080,
    "⚽ Soccer":        0x2ECC71,
    "🌐 Other":         0x95A5A6,
}

MARKETS_PER_PAGE = 3         # Market cards shown per browse page (fits YES/NO buttons in 5-row limit)
LOPSIDED_THRESHOLD = 0.80    # Filter markets where YES or NO > 80%
HOT_MARKETS_COUNT = 3        # Number of hot markets featured at top

# Sports categories blocked from prediction markets (use /sportsbook instead)
BLOCKED_CATEGORIES = {
    "⚽ Sports", "🏈 NFL", "🏀 NBA", "⚾ MLB", "🏒 NHL",
    "⚽ Soccer", "🥊 MMA", "♟️ Chess", "🎮 Gaming",
}

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

        # Migrations: add columns for trending/hot support
        for col, default in [("volume_24hr", "0"), ("featured", "0")]:
            try:
                await db.execute(
                    f"ALTER TABLE prediction_markets ADD COLUMN {col} REAL DEFAULT {default}"
                )
            except Exception:
                pass  # Column already exists

    log.info("Prediction market DB tables ready.")


# ─────────────────────────────────────────────
# TSL BUCKS HELPERS (delegates to flow_wallet)
# ─────────────────────────────────────────────

async def get_balance(user_id) -> int:
    """Return the current TSL Bucks balance for a user."""
    return await flow_wallet.get_balance(int(user_id))


async def update_balance(user_id, delta: int):
    """
    Add `delta` (positive = credit, negative = debit) to a user's balance.
    Raises ValueError if the resulting balance would go negative.
    """
    uid = int(user_id)
    if delta >= 0:
        await flow_wallet.credit(uid, delta, "PREDICTION",
                                 description="prediction market")
    else:
        await flow_wallet.debit(uid, abs(delta), "PREDICTION",
                                description="prediction market")


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
        """Fetch active events with their nested markets, sorted by volume."""
        data = await self._get("/events", params={
            "active": "true",
            "limit": limit,
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
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

    async def fetch_trending_markets(self, limit: int = 20) -> list[dict]:
        """Fetch markets sorted by 24-hour volume (trending/hot)."""
        data = await self._get("/markets", params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
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
    # Try exact match first
    result = CATEGORY_MAP.get(key)
    if result:
        return result
    # Try with hyphens replaced by spaces
    result = CATEGORY_MAP.get(key.replace("-", " "))
    if result:
        return result
    return f"🌐 {raw_category.title()}"


def extract_category_from_event(event: dict, market_slug: str = "") -> str:
    """
    Extract category from a Polymarket event using tags, series, and slug heuristics.

    Priority:
      1. event.tags[].label/slug — most reliable (specific tags first)
      2. event.seriesSlug or event.series[].title
      3. Slug pattern matching (e.g., 'nba-' prefix)
      4. Fallback to "Other"
    """
    # ── Tier 1: Tags ──
    tags = event.get("tags", [])
    if isinstance(tags, list) and tags:
        # Prefer specific sports/topic tags over generic ones like "Sports"
        SPECIFIC_SLUGS = {
            "nba", "nfl", "mlb", "nhl", "mma", "ufc", "boxing", "chess",
            "epl", "soccer", "ai", "crypto", "politics", "elections",
            "economy", "fomc", "fed-rates",
        }
        for tag in tags:
            if isinstance(tag, dict):
                slug_tag = tag.get("slug", "").lower()
                label = tag.get("label", "")
            else:
                slug_tag = str(tag).lower()
                label = str(tag)
            if slug_tag in SPECIFIC_SLUGS:
                return map_category(slug_tag)
            mapped = map_category(label)
            if mapped != "🌐 Other" and not mapped.startswith("🌐 "):
                return mapped
        # No specific match — try first tag
        first = tags[0]
        label = first.get("label", "") if isinstance(first, dict) else str(first)
        mapped = map_category(label)
        if mapped != "🌐 Other" and not mapped.startswith("🌐 "):
            return mapped

    # ── Tier 2: Series ──
    series_slug = event.get("seriesSlug", "")
    if series_slug:
        mapped = map_category(series_slug)
        if mapped != "🌐 Other" and not mapped.startswith("🌐 "):
            return mapped

    series_list = event.get("series", [])
    if isinstance(series_list, list):
        for s in series_list:
            title = s.get("title", "") if isinstance(s, dict) else str(s)
            mapped = map_category(title)
            if mapped != "🌐 Other" and not mapped.startswith("🌐 "):
                return mapped

    # ── Tier 3: Slug pattern matching ──
    slug = (market_slug or event.get("slug", "")).lower()
    SLUG_PREFIXES = {
        "nba-": "nba", "nfl-": "nfl", "mlb-": "mlb", "nhl-": "nhl",
        "soccer-": "soccer", "epl-": "epl", "ufc-": "ufc",
        "boxing-": "boxing", "mma-": "mma", "chess-": "chess",
        "bitcoin-": "crypto", "ethereum-": "crypto", "btc-": "crypto",
        "trump-": "politics", "election-": "elections", "president-": "politics",
        "fed-": "economics", "fomc-": "fomc",
    }
    for prefix, cat_key in SLUG_PREFIXES.items():
        if prefix in slug:
            return map_category(cat_key)

    return "🌐 Other"


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
        return "$0"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"



def is_lopsided(yes_price: float, no_price: float) -> bool:
    """Return True if market is too one-sided to be interesting."""
    return yes_price > LOPSIDED_THRESHOLD or no_price > LOPSIDED_THRESHOLD


def hot_label(volume_24hr: float) -> str:
    """Return heat emoji(s) based on 24hr volume."""
    try:
        v = float(volume_24hr or 0)
    except (ValueError, TypeError):
        return ""
    if v >= 500_000:
        return "🔥🔥🔥"
    if v >= 100_000:
        return "🔥🔥"
    if v >= 10_000:
        return "🔥"
    return ""


def truncate_slug(slug: str, max_len: int = 35) -> str:
    """Truncate long slugs for display."""
    if len(slug) <= max_len:
        return slug
    return slug[:max_len - 3] + "..."


# ─────────────────────────────────────────────
# DISCORD UI COMPONENTS
# ─────────────────────────────────────────────

class WagerModal(discord.ui.Modal):
    """Modal that asks the user how many contracts to buy."""

    amount_input = discord.ui.TextInput(
        label="Contracts to buy (1 contract = 1 Buck)",
        placeholder="e.g. 10",
        min_length=1,
        max_length=6,
        required=True,
    )

    def __init__(self, market_id: str, slug: str, side: str, price: float, title: str):
        super().__init__(title=f"Buy {side} — {title[:34]}")
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
        user_id     = interaction.user.id

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
        profit = payout - cost_bucks
        embed  = discord.Embed(
            title=f"{symbol} Contract Purchased",
            color=color,
            description=(
                f"**{self.market_title}**\n\n"
                f"Side: **{self.side}** · Price: **{self.price:.1%}** each\n"
                f"Qty: **{quantity}** · Cost: **{cost_bucks:,} TSL Bucks**\n"
                f"Potential: **{payout:,} TSL Bucks** if {self.side} wins\n\n"
                f"*Profit if correct: +{profit:,} TSL Bucks*"
            ),
        )
        embed.set_footer(text="Use /portfolio to view your positions · ATLAS Flow Casino")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Post to #ledger
        try:
            new_bal = await flow_wallet.get_balance(user_id)
            txn_id = await flow_wallet.get_last_txn_id(user_id)
            from ledger_poster import post_transaction
            await post_transaction(
                interaction.client, interaction.guild_id, user_id,
                "PREDICTION", -cost_bucks, new_bal,
                f"Buy {quantity} {self.side} — {self.market_title[:50]}",
                txn_id,
            )
        except Exception:
            pass


class BetButtonView(discord.ui.View):
    """YES / NO buttons on the /bet embed — fetches live odds before modal."""

    def __init__(self, market_id: str, slug: str, title: str,
                 yes_price: float, no_price: float,
                 cog=None):
        super().__init__(timeout=300)
        self.market_id = market_id
        self.slug      = slug
        self.title     = title
        self.yes_price = yes_price
        self.no_price  = no_price
        self.cog       = cog

    async def _fetch_live_price(self, side: str) -> float:
        """Fetch live price from API with 2-second timeout; fallback to cached.
        NOTE: Not called from button handlers to avoid interaction timeout.
        Kept for potential background refresh use."""
        if not self.cog:
            return self.yes_price if side == "YES" else self.no_price
        try:
            live = await asyncio.wait_for(
                self.cog.client.fetch_market_by_id(self.market_id),
                timeout=2.0,
            )
            if live:
                prices = extract_prices(live)
                # Update DB cache
                now = datetime.now(timezone.utc).isoformat()
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE prediction_markets SET yes_price=?, no_price=?, last_synced=? "
                        "WHERE market_id=?",
                        (prices["yes_price"], prices["no_price"], now, self.market_id),
                    )
                    await db.commit()
                return prices["yes_price"] if side == "YES" else prices["no_price"]
        except Exception:
            pass
        return self.yes_price if side == "YES" else self.no_price

    @discord.ui.button(label="Buy YES ✅", style=discord.ButtonStyle.success)
    async def buy_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Use cached price to avoid API timeout before modal response
        modal = WagerModal(
            market_id=self.market_id, slug=self.slug, side="YES",
            price=self.yes_price, title=self.title,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Buy NO ❌", style=discord.ButtonStyle.danger)
    async def buy_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Use cached price to avoid API timeout before modal response
        modal = WagerModal(
            market_id=self.market_id, slug=self.slug, side="NO",
            price=self.no_price, title=self.title,
        )
        await interaction.response.send_modal(modal)


class CategorySelect(discord.ui.Select):
    """Dropdown to filter markets by category."""

    def __init__(self, categories: list[str], parent_view,
                 category_counts: dict | None = None):
        counts = category_counts or {}
        total = sum(counts.values()) if counts else 0

        options = [discord.SelectOption(
            label="All Categories",
            value="all",
            description=f"{total} markets" if total else None,
            default=True,
        )]
        # Hot / Trending option
        options.append(discord.SelectOption(
            label="Hot / Trending",
            value="hot",
            emoji="🔥",
            description="Sorted by 24h volume",
        ))
        for cat in categories:
            count = counts.get(cat, 0)
            options.append(discord.SelectOption(
                label=cat,
                value=cat,
                description=f"{count} markets" if count else None,
            ))
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
    """Full market browser: category filter + YES/NO bet buttons + page nav."""

    def __init__(self, all_markets: list[dict], categories: list[str],
                 hot_markets: list[dict] | None = None,
                 category_counts: dict | None = None,
                 cog=None):
        super().__init__(timeout=600)
        self.all_markets     = all_markets
        self.categories      = categories
        self.hot_markets     = hot_markets or []
        self.category_counts = category_counts or {}
        self.filter          = "all"
        self.page            = 0
        self.cog             = cog  # for live API calls

        self.cat_select = CategorySelect(
            categories, parent_view=self, category_counts=self.category_counts
        )
        self.add_item(self.cat_select)
        self._rebuild_buttons()

    # ── Filtering / Pagination helpers ──

    def _filtered(self) -> list[dict]:
        if self.filter == "hot":
            return sorted(
                self.all_markets,
                key=lambda m: m.get("volume_24hr", 0),
                reverse=True,
            )
        if self.filter == "all":
            return self.all_markets
        return [m for m in self.all_markets if m.get("category") == self.filter]

    def _max_page(self) -> int:
        return max(0, (len(self._filtered()) - 1) // MARKETS_PER_PAGE)

    # ── Dynamic button management ──

    def _rebuild_buttons(self):
        """Clear and recreate YES/NO bet buttons + nav for the current page."""
        # Remove everything except the CategorySelect
        to_remove = [c for c in self.children if not isinstance(c, CategorySelect)]
        for item in to_remove:
            self.remove_item(item)

        markets = self._filtered()
        start = self.page * MARKETS_PER_PAGE
        chunk = markets[start : start + MARKETS_PER_PAGE]

        # Rows 1-3: YES / NO buttons per market card
        for i, m in enumerate(chunk):
            row = i + 1  # rows 1, 2, 3
            yes_p = m.get("yes_price", 0.5)
            no_p = m.get("no_price", 0.5)

            yes_btn = discord.ui.Button(
                label=f"YES {yes_p:.0%}",
                style=discord.ButtonStyle.success,
                custom_id=f"yes_{i}_{self.page}",
                row=row,
            )
            no_btn = discord.ui.Button(
                label=f"NO {no_p:.0%}",
                style=discord.ButtonStyle.danger,
                custom_id=f"no_{i}_{self.page}",
                row=row,
            )
            yes_btn.callback = self._make_bet_cb(m, "YES")
            no_btn.callback = self._make_bet_cb(m, "NO")
            self.add_item(yes_btn)
            self.add_item(no_btn)

        # Row 4: Navigation
        prev_btn = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_prev",
            disabled=self.page == 0,
            row=4,
        )
        page_btn = discord.ui.Button(
            label=f"{self.page + 1}/{self._max_page() + 1}",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_page",
            disabled=True,
            row=4,
        )
        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_next",
            disabled=self.page >= self._max_page(),
            row=4,
        )
        prev_btn.callback = self._prev
        next_btn.callback = self._next
        self.add_item(prev_btn)
        self.add_item(page_btn)
        self.add_item(next_btn)

    def _make_bet_cb(self, market: dict, side: str):
        """Closure-safe callback: use cached odds → open wager modal instantly."""
        async def callback(interaction: discord.Interaction):
            # Use cached price to avoid API timeout before modal response
            price = market["yes_price"] if side == "YES" else market["no_price"]
            modal = WagerModal(
                market_id=market["market_id"],
                slug=market["slug"],
                side=side,
                price=price,
                title=market["title"],
            )
            await interaction.response.send_modal(modal)
        return callback

    # ── Navigation / Filter callbacks ──

    async def apply_filter(self, interaction: discord.Interaction, cat: str):
        self.filter = cat
        self.page = 0
        self._rebuild_buttons()
        embed, card_file = self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild_buttons()
        embed, card_file = self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self._max_page(), self.page + 1)
        self._rebuild_buttons()
        embed, card_file = self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    # ── Embed + Image builder ──

    def _build_page(self) -> tuple[discord.Embed, discord.File | None]:
        """Build embed + rendered card image for the current page."""
        markets = self._filtered()
        total = len(markets)
        start = self.page * MARKETS_PER_PAGE
        chunk = markets[start : start + MARKETS_PER_PAGE]

        if self.filter == "hot":
            cat_label = "Hot / Trending"
        elif self.filter == "all":
            cat_label = "All Categories"
        else:
            cat_label = self.filter

        embed = discord.Embed(
            title="ATLAS Flow -- Prediction Markets",
            color=CATEGORY_COLORS.get(cat_label, 0xD4AF37),
        )

        embed.description = f"**{total}** markets | Page {self.page+1}/{self._max_page()+1}"

        # Hot markets banner — only on page 0 of "all" filter
        if self.page == 0 and self.filter == "all" and self.hot_markets:
            hot_lines = []
            for hm in self.hot_markets[:HOT_MARKETS_COUNT]:
                vol_24h = hm.get("volume_24hr", 0)
                yes_p = hm.get("yes_price", 0.5)
                heat = hot_label(vol_24h)
                hot_lines.append(
                    f"{heat} **{hm['title'][:50]}** -- "
                    f"YES {yes_p:.0%} | 24h: {fmt_volume(vol_24h)}"
                )
            if hot_lines:
                embed.add_field(
                    name="Trending Now",
                    value="\n".join(hot_lines),
                    inline=False,
                )

        # Render Pillow card image
        card_file = None
        if chunk:
            try:
                card_data = [
                    {
                        "title": m.get("title", ""),
                        "category": m.get("category", "Other"),
                        "yes_price": m.get("yes_price", 0.5),
                        "no_price": m.get("no_price", 0.5),
                        "volume": m.get("volume", 0),
                        "end_date": m.get("end_date", ""),
                    }
                    for m in chunk
                ]
                buf = render_market_page(card_data, self.page + 1, self._max_page() + 1)
                card_file = discord.File(buf, filename="markets.png")
                embed.set_image(url="attachment://markets.png")
            except Exception:
                # Fallback: text-based cards if rendering fails
                for m in chunk:
                    yes_p = m.get("yes_price", 0.5)
                    no_p = m.get("no_price", 0.5)
                    cat = m.get("category", "Other")
                    vol_str = fmt_volume(m.get("volume", 0))
                    embed.add_field(
                        name=f"{cat}  {m['title'][:55]}",
                        value=f"**YES {yes_p:.0%}**  |  **NO {no_p:.0%}**  |  Vol: {vol_str}",
                        inline=False,
                    )
        else:
            embed.add_field(
                name="No markets found",
                value="Try a different category or check back later.",
                inline=False,
            )

        embed.set_footer(text="Click YES or NO below to bet | ATLAS Flow Casino")
        embed.timestamp = datetime.now(timezone.utc)
        return embed, card_file


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
        self._first_sync_done = False
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
                event_category = extract_category_from_event(event)

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
                    # Markets inherit category from parent event; override only
                    # if the market's own slug reveals a more specific category
                    mkt_category = extract_category_from_event(event, market_slug=slug)
                    category = mkt_category if mkt_category != "🌐 Other" else event_category

                    # Skip sports markets — use /sportsbook instead
                    if category in BLOCKED_CATEGORIES:
                        continue

                    status = market_status(mkt)
                    end_date = mkt.get("endDate", "") or mkt.get("end_date_iso", "")
                    volume = mkt.get("volumeNum") or mkt.get("volume") or 0
                    liquidity = mkt.get("liquidityNum") or mkt.get("liquidity") or 0
                    volume_24hr = mkt.get("volume24hr") or mkt.get("volume24Hr") or 0
                    featured = 1 if mkt.get("featured") else 0

                    try: volume = float(volume)
                    except (ValueError, TypeError): volume = 0
                    try: liquidity = float(liquidity)
                    except (ValueError, TypeError): liquidity = 0
                    try: volume_24hr = float(volume_24hr)
                    except (ValueError, TypeError): volume_24hr = 0

                    await db.execute("""
                        INSERT INTO prediction_markets
                            (market_id, event_id, slug, title, category,
                             yes_price, no_price, volume, liquidity,
                             volume_24hr, featured,
                             end_date, status, last_synced)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(market_id) DO UPDATE SET
                            slug        = excluded.slug,
                            title       = excluded.title,
                            category    = excluded.category,
                            yes_price   = excluded.yes_price,
                            no_price    = excluded.no_price,
                            volume      = excluded.volume,
                            liquidity   = excluded.liquidity,
                            volume_24hr = excluded.volume_24hr,
                            featured    = excluded.featured,
                            end_date    = excluded.end_date,
                            status      = excluded.status,
                            last_synced = excluded.last_synced
                    """, (
                        market_id, event_id, slug, title, category,
                        prices["yes_price"], prices["no_price"],
                        volume, liquidity, volume_24hr, featured,
                        end_date, status, now,
                    ))
                    upserted += 1

            await db.commit()

            # Purge any stale sports markets that pre-date the filter
            blocked_ph = ",".join("?" for _ in BLOCKED_CATEGORIES)
            cursor = await db.execute(
                f"DELETE FROM prediction_markets WHERE category IN ({blocked_ph}) AND status = 'active'",
                tuple(BLOCKED_CATEGORIES),
            )
            if cursor.rowcount:
                log.info(f"Purged {cursor.rowcount} stale sports markets from prediction DB.")
                await db.commit()

        log.info(f"Polymarket sync complete — {upserted} active markets upserted.")

        # ── Pass 2: Auto-resolve closed markets ──────────────────────────
        await self._auto_resolve_pass()

        # ── Pass 3: Gemini classification for "Other" markets (first sync only) ──
        if not self._first_sync_done:
            self._first_sync_done = True
            try:
                await self._classify_unknown_categories()
            except Exception as e:
                log.warning(f"Gemini classification pass failed: {e}")

    async def _classify_unknown_categories(self):
        """
        One-time Gemini classification for markets still labeled 'Other'.
        Batches titles into one prompt, parses JSON response, updates DB.
        """
        gemini = _get_gemini_client()
        if not gemini:
            log.info("Gemini not available — skipping AI category classification.")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT market_id, title, slug FROM prediction_markets "
                "WHERE status='active' AND category LIKE '%Other%' "
                "LIMIT 50"
            ) as cursor:
                unknowns = await cursor.fetchall()

        if not unknowns:
            log.info("No 'Other' markets to classify.")
            return

        log.info(f"Classifying {len(unknowns)} markets with Gemini...")

        valid_cats = sorted(set(CATEGORY_MAP.values()))
        lines = [f"{i+1}. {title} (slug: {slug})"
                 for i, (mid, title, slug) in enumerate(unknowns)]

        prompt = (
            f"Classify each prediction market into exactly one category.\n"
            f"Valid categories: {', '.join(valid_cats)}\n\n"
            f"Markets:\n" + "\n".join(lines) + "\n\n"
            f"Return ONLY a JSON array of objects: "
            f'[{{"index": 1, "category": "chosen category"}}, ...]\n'
            f"No explanation. Just the JSON array."
        )

        loop = asyncio.get_running_loop()
        def _call():
            return gemini.models.generate_content(
                model="gemini-2.0-flash",
                contents=[prompt],
            )

        try:
            response = await loop.run_in_executor(None, _call)
            text = response.text.strip()
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if not json_match:
                log.warning("Gemini returned non-JSON for classification.")
                return
            classifications = json.loads(json_match.group())
        except Exception as e:
            log.error(f"Gemini classification failed: {e}")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            updated = 0
            blocked = 0
            for item in classifications:
                idx = item.get("index", 0) - 1
                cat = item.get("category", "")
                if 0 <= idx < len(unknowns) and cat:
                    market_id = unknowns[idx][0]
                    # If Gemini classified it as a sports category, remove it
                    if cat in BLOCKED_CATEGORIES:
                        await db.execute(
                            "DELETE FROM prediction_markets WHERE market_id = ?",
                            (market_id,),
                        )
                        blocked += 1
                        continue
                    await db.execute(
                        "UPDATE prediction_markets SET category = ? WHERE market_id = ?",
                        (cat, market_id),
                    )
                    updated += 1
            await db.commit()

        log.info(f"Gemini classified {updated}/{len(unknowns)} markets ({blocked} sports blocked).")

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
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return  # interaction expired before we could respond
        await self._ensure_db()

        async with aiosqlite.connect(DB_PATH) as db:
            blocked_ph = ",".join("?" for _ in BLOCKED_CATEGORIES)
            async with db.execute(f"""
                SELECT market_id, slug, title, category,
                       yes_price, no_price, volume, end_date,
                       COALESCE(volume_24hr, 0), COALESCE(featured, 0), liquidity
                FROM prediction_markets
                WHERE status = 'active'
                  AND yes_price <= ?
                  AND no_price  <= ?
                  AND category NOT IN ({blocked_ph})
                ORDER BY volume DESC
                LIMIT 200
            """, (LOPSIDED_THRESHOLD, LOPSIDED_THRESHOLD, *BLOCKED_CATEGORIES)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send(
                "⚠️ No markets synced yet. Try again in a moment.",
                ephemeral=True,
            )
            return

        markets = [
            {
                "market_id":   r[0],
                "slug":        r[1],
                "title":       r[2],
                "category":    r[3],
                "yes_price":   r[4] if r[4] is not None else 0.5,
                "no_price":    r[5] if r[5] is not None else 0.5,
                "volume":      r[6] or 0,
                "end_date":    r[7] or "",
                "volume_24hr": r[8] or 0,
                "featured":    r[9] or 0,
                "liquidity":   r[10] or 0,
            }
            for r in rows
        ]

        # Hot markets: top by 24hr volume
        hot_markets = sorted(
            markets,
            key=lambda m: m.get("volume_24hr", 0),
            reverse=True,
        )[:HOT_MARKETS_COUNT]

        # Category counts for the select menu
        category_counts: dict[str, int] = {}
        for m in markets:
            cat = m.get("category", "🌐 Other")
            category_counts[cat] = category_counts.get(cat, 0) + 1

        categories = sorted({m["category"] for m in markets})
        view = MarketBrowserView(
            markets, categories,
            hot_markets=hot_markets,
            category_counts=category_counts,
            cog=self,
        )

        embed, card_file = view._build_page()
        kwargs = {"embed": embed, "view": view, "ephemeral": True}
        if card_file:
            kwargs["file"] = card_file
        await interaction.followup.send(**kwargs)

    # ── Slash: /bet <slug> ──────────────────

    @app_commands.command(
        name="bet",
        description="Place a TSL Bucks wager on a prediction market."
    )
    @app_commands.describe(slug="The market slug/ID from /markets")
    async def bet_cmd(self, interaction: discord.Interaction, slug: str):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
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
            await interaction.followup.send(
                f"❌ Market `{slug}` not found. Use `/markets` to browse.",
                ephemeral=True,
            )
            return

        market_id, mkt_slug, title, category, yes_price, no_price, volume, end_date, status = row

        if status != "active":
            await interaction.followup.send(
                f"⚠️ Market is **{status}** and not accepting new bets.",
                ephemeral=True,
            )
            return

        yes_price = yes_price if yes_price is not None else 0.5
        no_price  = no_price  if no_price  is not None else 0.5

        # Fetch live odds from Polymarket API (2-second timeout)
        try:
            live_data = await asyncio.wait_for(
                self.client.fetch_market_by_id(market_id),
                timeout=2.0,
            )
            if live_data:
                live_prices = extract_prices(live_data)
                yes_price = live_prices["yes_price"]
                no_price = live_prices["no_price"]
                # Update DB cache
                now = datetime.now(timezone.utc).isoformat()
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE prediction_markets SET yes_price=?, no_price=?, "
                        "last_synced=? WHERE market_id=?",
                        (yes_price, no_price, now, market_id),
                    )
                    await db.commit()
        except Exception:
            pass  # use cached prices

        yes_bucks = price_to_bucks(yes_price)
        no_bucks  = price_to_bucks(no_price)

        color = CATEGORY_COLORS.get(category, 0xD4AF37)
        try:
            end_dt  = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            end_str = f"<t:{int(end_dt.timestamp())}:R>"
        except Exception:
            end_str = end_date or "No expiry"

        embed = discord.Embed(
            title=f"📊 {title[:70]}",
            color=color,
        )
        embed.description = (
            f"```\n{category.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n```\n"
            f"Ends: {end_str}  ·  Volume: {fmt_volume(volume)}\n"
            f"Each contract pays **{PAYOUT_SCALE} TSL Bucks** if your side wins."
        )
        embed.add_field(
            name="✅ Buy YES",
            value=(
                f"**{yes_bucks} TSL Bucks** / contract\n"
                f"Implied probability: **{yes_price:.1%}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="❌ Buy NO",
            value=(
                f"**{no_bucks} TSL Bucks** / contract\n"
                f"Implied probability: **{no_price:.1%}**"
            ),
            inline=True,
        )
        embed.set_footer(text="💡 Odds fetched live · Click a button below · ATLAS Flow Casino")
        embed.timestamp = datetime.now(timezone.utc)

        view = BetButtonView(
            market_id=market_id, slug=mkt_slug, title=title,
            yes_price=yes_price, no_price=no_price, cog=self,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── Slash: /portfolio ─────────────────────

    @app_commands.command(
        name="portfolio",
        description="View your open prediction market contracts."
    )
    async def portfolio_cmd(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        await self._ensure_db()
        user_id = interaction.user.id

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
            await interaction.followup.send(
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

        await interaction.followup.send(embed=embed, ephemeral=True)

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
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        await self._ensure_db()
        slug   = slug.strip().lower()
        result = result.upper().strip()

        if result not in ("YES", "NO", "VOID"):
            await interaction.followup.send(
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
            await interaction.followup.send(
                f"❌ Market `{slug}` not found.", ephemeral=True
            )
            return

        market_id, title = row
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

        # Post ledger slips for resolution payouts/refunds
        try:
            from ledger_poster import post_transaction
            guild = self.bot.guilds[0] if self.bot.guilds else None
            guild_id = guild.id if guild else None
            if guild_id:
                for cid, user_id, side, qty, cost, payout in contracts:
                    if result == "VOID":
                        bal = await flow_wallet.get_balance(user_id)
                        txn_id = await flow_wallet.get_last_txn_id(user_id)
                        await post_transaction(
                            self.bot, guild_id, user_id,
                            "PREDICTION", cost, bal,
                            f"Void refund — {market_id[:30]}", txn_id,
                        )
                    elif side == result:
                        bal = await flow_wallet.get_balance(user_id)
                        txn_id = await flow_wallet.get_last_txn_id(user_id)
                        await post_transaction(
                            self.bot, guild_id, user_id,
                            "PREDICTION", payout, bal,
                            f"Won: {side} — {market_id[:30]}", txn_id,
                        )
        except Exception:
            pass

        return counts

    # ── Slash: /market_status (admin) ─────────

    @app_commands.command(
        name="market_status",
        description="[Admin] Show Polymarket sync status and stats."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def market_status_cmd(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        await self._ensure_db()

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

    # ── Impl: refund_sports (called by commish_cog) ────

    async def refund_sports_impl(self, interaction: discord.Interaction):
        """Void all open contracts on sports-category markets and refund users."""
        await self._ensure_db()

        async with aiosqlite.connect(DB_PATH) as db:
            # Find sports markets with open contracts
            placeholders = ",".join("?" for _ in BLOCKED_CATEGORIES)
            async with db.execute(
                f"SELECT m.market_id, m.title, m.category "
                f"FROM prediction_markets m "
                f"INNER JOIN prediction_contracts c "
                f"  ON c.market_id = m.market_id AND c.status = 'open' "
                f"WHERE m.category IN ({placeholders}) "
                f"GROUP BY m.market_id",
                tuple(BLOCKED_CATEGORIES),
            ) as cursor:
                sports_markets = await cursor.fetchall()

        if not sports_markets:
            await interaction.followup.send(
                "No open contracts on sports markets found.", ephemeral=True
            )
            return

        total_voided = 0
        total_refunded = 0
        for market_id, title, category in sports_markets:
            counts = await self._resolve(market_id, "VOID", resolved_by="sports_filter")
            total_voided += counts["voided"]
            total_refunded += counts["voided"]  # each voided contract = 1 refund

            # Also remove the market from the DB so it won't reappear
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM prediction_markets WHERE market_id = ?",
                    (market_id,),
                )
                await db.commit()

        await interaction.followup.send(
            f"Voided **{total_voided}** contracts across **{len(sports_markets)}** "
            f"sports markets. All users refunded.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PolymarketCog(bot))
    print("ATLAS: Flow · Polymarket Prediction Markets loaded.")
