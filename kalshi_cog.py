"""
kalshi_cog.py — ATLAS Flow Casino: Prediction Market Module
Part of the ATLAS framework for The Simulation League (TSL)

Integrates real-world event data from Kalshi's API so users can
bet TSL Bucks on Economics, Politics, and Entertainment markets.

Author: TheWitt
Pricing format: Subpenny (March 12 2024+), uses _dollars suffix fields.
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands

import aiohttp
import sqlite3
import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional
import os

log = logging.getLogger("kalshi_cog")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

KALSHI_BASE       = "https://trading-api.kalshi.com/trade-api/v2"
DB_PATH           = os.getenv("DB_PATH", "tsl_history.db")

# ── Kalshi Auth ───────────────────────────────────────────────────────────────
# Kalshi requires authentication on ALL endpoints — there are no public reads.
#
# Two supported methods (set ONE in .env):
#
#   Method A — Email / Password  (most common for retail accounts)
#     KALSHI_EMAIL=you@example.com
#     KALSHI_PASSWORD=yourpassword
#
#   Method B — API Key  (Members / Pro accounts — key string from the dashboard)
#     KALSHI_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#
# Method A performs a POST /login on startup, caches the token, and
# automatically re-authenticates if a 401 is returned mid-session.
# Method B attaches the key directly to every request header.
KALSHI_EMAIL      = os.getenv("KALSHI_EMAIL", "")
KALSHI_PASSWORD   = os.getenv("KALSHI_PASSWORD", "")
KALSHI_API_KEY    = os.getenv("KALSHI_API_KEY", "")

# Channel routing — resolved from setup_cog at runtime
def _prediction_channel_id() -> int:
    try:
        from setup_cog import get_channel_id
        return get_channel_id("prediction_markets") or 0
    except ImportError:
        return int(os.getenv("PREDICTION_MARKET_CHANNEL_ID", "0"))

# How many TSL Bucks = 1 Kalshi "dollar" (i.e. $1.00 = 100 TSL Bucks)
# A YES at $0.65 costs 65 TSL Bucks; pays out 100 TSL Bucks if correct.
PAYOUT_SCALE = 100

# Categories we care about (Kalshi series_ticker prefixes)
ALLOWED_CATEGORIES = {
    "ECON":    "📈 Economics",
    "FED":     "📈 Economics",
    "CPI":     "📈 Economics",
    "GDP":     "📈 Economics",
    "PRES":    "🏛️ Politics",
    "SENATE":  "🏛️ Politics",
    "HOUSE":   "🏛️ Politics",
    "OSCARS":  "🎬 Entertainment",
    "EMMY":    "🎬 Entertainment",
    "GRAMMYS": "🎬 Entertainment",
}

CATEGORY_COLORS = {
    "📈 Economics":    0x2ECC71,
    "🏛️ Politics":     0x3498DB,
    "🎬 Entertainment": 0xE91E63,
    "🌐 Other":        0x95A5A6,
}

MARKETS_PER_PAGE = 5   # Embeds shown per browse page

# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────

def init_kalshi_db(db_path: str = DB_PATH):
    """Create prediction market tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS kalshi_markets (
            ticker          TEXT PRIMARY KEY,
            event_ticker    TEXT NOT NULL,
            title           TEXT NOT NULL,
            subtitle        TEXT,
            category        TEXT DEFAULT 'Other',
            yes_bid         REAL DEFAULT 0.0,
            yes_ask         REAL DEFAULT 0.0,
            no_bid          REAL DEFAULT 0.0,
            no_ask          REAL DEFAULT 0.0,
            volume          INTEGER DEFAULT 0,
            open_interest   INTEGER DEFAULT 0,
            expiration_time TEXT,
            status          TEXT DEFAULT 'open',
            result          TEXT,                  -- 'yes' | 'no' | null (set by Kalshi on finalization)
            resolved_by     TEXT DEFAULT 'pending', -- 'auto' | 'admin' | 'pending'
            last_synced     TEXT
        );

        CREATE TABLE IF NOT EXISTS kalshi_contracts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            event_ticker    TEXT NOT NULL,
            side            TEXT NOT NULL CHECK(side IN ('YES','NO')),
            buy_price       REAL NOT NULL,
            quantity        INTEGER NOT NULL DEFAULT 1,
            cost_bucks      INTEGER NOT NULL,
            potential_payout INTEGER NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open'
                            CHECK(status IN ('open','won','lost','voided')),
            created_at      TEXT NOT NULL,
            resolved_at     TEXT,
            FOREIGN KEY (ticker) REFERENCES kalshi_markets(ticker)
        );

        CREATE INDEX IF NOT EXISTS idx_contracts_user
            ON kalshi_contracts(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_contracts_ticker
            ON kalshi_contracts(ticker, status);
        CREATE INDEX IF NOT EXISTS idx_markets_status
            ON kalshi_markets(status, category);
    """)

    conn.commit()

    # ── Migrations: add columns that may not exist in older DBs ──────────────
    migrations = [
        "ALTER TABLE kalshi_markets ADD COLUMN result TEXT",
        "ALTER TABLE kalshi_markets ADD COLUMN resolved_by TEXT DEFAULT 'pending'",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists — safe to skip

    conn.close()
    log.info("Kalshi DB tables ready.")


# ─────────────────────────────────────────────
# TSL BUCKS HELPERS
# ⚠️  STUB WARNING: These query an 'economy' table that does NOT exist in
# tsl_history.db. The sportsbook uses its own 'users_table' in sportsbook.db.
# Wire these to the actual casino_db economy system, or create the 'economy'
# table, before enabling /bet or /portfolio — they will crash as-is.
# ─────────────────────────────────────────────

def get_balance(user_id: str, db_path: str = DB_PATH) -> int:
    """
    Return the current TSL Bucks balance for a user.
    Replace this body with a call to your economy/casino system.
    """
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # Attempt to read from a generic 'economy' table; adapt as needed.
    c.execute("""
        SELECT COALESCE(
            (SELECT balance FROM economy WHERE user_id = ?), 0
        )
    """, (user_id,))
    row = c.fetchone()
    conn.close()
    return int(row[0]) if row else 0


def update_balance(user_id: str, delta: int, db_path: str = DB_PATH):
    """
    Add `delta` (positive = credit, negative = debit) to a user's balance.
    Replace this body with a call to your economy/casino system.
    Raises ValueError if the resulting balance would go negative.
    """
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO economy (user_id, balance) VALUES (?, 0)", (user_id,))
    c.execute("""
        UPDATE economy
        SET balance = balance + ?
        WHERE user_id = ?
    """, (delta, user_id))
    c.execute("SELECT balance FROM economy WHERE user_id = ?", (user_id,))
    new_bal = c.fetchone()[0]
    if new_bal < 0:
        conn.rollback()
        conn.close()
        raise ValueError("Insufficient TSL Bucks")
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# KALSHI API CLIENT
# ─────────────────────────────────────────────

class KalshiClient:
    """
    Async wrapper around the Kalshi Trading API v2.

    ALL Kalshi endpoints require authentication — there are no public reads.
    This client supports two auth methods (priority order):

      1. API Key  (KALSHI_API_KEY env var) — attached as a header each request.
      2. Email / Password  (KALSHI_EMAIL + KALSHI_PASSWORD) — POST /login on
         first use, token cached in memory, auto-refreshed on 401.
    """

    def __init__(self):
        self._api_key    = KALSHI_API_KEY.strip()
        self._email      = KALSHI_EMAIL.strip()
        self._password   = KALSHI_PASSWORD.strip()
        self._token: str = ""                        # cached login token
        self._session: Optional[aiohttp.ClientSession] = None

        if not self._api_key and not (self._email and self._password):
            log.warning(
                "KalshiClient: No credentials configured. "
                "Set KALSHI_API_KEY or KALSHI_EMAIL+KALSHI_PASSWORD in .env — "
                "all API calls will return 401 without them."
            )

    # ── Internal helpers ───────────────────────────────────────────────────

    def _base_headers(self) -> dict:
        """Headers that don't require a token (used for the login call)."""
        return {
            "Accept":       "application/json",
            "Content-Type": "application/json",
        }

    def _auth_headers(self) -> dict:
        """Headers with current auth credential attached."""
        h = self._base_headers()
        if self._api_key:
            # API-key auth: Kalshi expects the key in the Authorization header
            h["Authorization"] = f"Bearer {self._api_key}"
        elif self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _session_get(self) -> aiohttp.ClientSession:
        """Return (or create) a raw session without default auth headers.
        Auth is injected per-request so token refreshes take effect immediately."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _login(self) -> bool:
        """
        POST /login with email+password to obtain a session token.
        Returns True on success, False on failure.
        """
        if not (self._email and self._password):
            return False
        session = await self._session_get()
        try:
            payload = {"email": self._email, "password": self._password}
            async with session.post(
                f"{KALSHI_BASE}/login",
                json=payload,
                headers=self._base_headers(),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f"Kalshi login failed ({resp.status}): {text}")
                    return False
                data = await resp.json()
                # Kalshi returns { "token": "...", "member_id": "..." }
                self._token = data.get("token", "")
                if self._token:
                    log.info("Kalshi login successful — token cached.")
                    return True
                log.error(f"Kalshi login: no token in response — {data}")
                return False
        except Exception as e:
            log.error(f"Kalshi login exception: {e}")
            return False

    async def _ensure_auth(self) -> bool:
        """Make sure we have a valid credential before making a request."""
        if self._api_key:
            return True          # API key — always ready
        if self._token:
            return True          # Already logged in
        return await self._login()

    async def _get(self, path: str, params: dict | None = None, _retry: bool = True) -> Optional[dict]:
        """
        Authenticated GET with automatic token refresh on 401.
        Returns parsed JSON dict, or None on error.
        """
        if not await self._ensure_auth():
            log.error(f"Kalshi GET {path} aborted — no valid credentials.")
            return None

        session = await self._session_get()
        try:
            async with session.get(
                f"{KALSHI_BASE}{path}",
                params=params,
                headers=self._auth_headers(),
            ) as resp:
                if resp.status == 401 and _retry:
                    # Token may have expired — force re-login once
                    log.warning("Kalshi 401 received — attempting re-authentication.")
                    self._token = ""
                    if await self._login():
                        return await self._get(path, params, _retry=False)
                    log.error("Kalshi re-authentication failed — giving up.")
                    return None
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f"Kalshi GET {path} → {resp.status}: {text[:200]}")
                    return None
                return await resp.json()
        except Exception as e:
            log.error(f"Kalshi GET {path} exception: {e}")
            return None

    # ── Public API methods ────────────────────────────────────────────────

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_events(self, limit: int = 200, status: str = "open") -> list[dict]:
        """Fetch events from Kalshi by status."""
        data = await self._get("/events", params={"limit": limit, "status": status})
        return data.get("events", []) if data else []

    async def fetch_finalized_events(self, limit: int = 100) -> list[dict]:
        """
        Fetch recently finalized events so we can auto-resolve settled markets.
        Kalshi uses status='finalized' for markets that have a confirmed result.
        """
        data = await self._get("/events", params={"limit": limit, "status": "finalized"})
        return data.get("events", []) if data else []

    async def fetch_markets_for_event(self, event_ticker: str) -> list[dict]:
        """Fetch individual markets (contracts) for a given event ticker."""
        data = await self._get(f"/events/{event_ticker}/markets")
        return data.get("markets", []) if data else []

    async def fetch_market(self, ticker: str) -> Optional[dict]:
        """Fetch a single market by ticker."""
        data = await self._get(f"/markets/{ticker}")
        return data.get("market") if data else None


# ─────────────────────────────────────────────
# PRICING HELPERS (Subpenny / _dollars format)
# ─────────────────────────────────────────────

def extract_prices(market: dict) -> dict:
    """
    Extract YES/NO bid/ask from a market dict using the March 12
    Subpenny (_dollars suffix) format.  Falls back to cents-to-dollars
    conversion if _dollars keys are absent.
    """
    def _d(key_dollars, key_cents_fallback) -> float:
        if key_dollars in market and market[key_dollars] is not None:
            return float(Decimal(str(market[key_dollars])).quantize(
                Decimal("0.001"), rounding=ROUND_HALF_UP
            ))
        # Legacy fallback: cents integer → dollars
        cents = market.get(key_cents_fallback) or 0
        return round(cents / 100, 3)

    yes_bid = _d("yes_bid_dollars", "yes_bid")
    yes_ask = _d("yes_ask_dollars", "yes_ask")
    no_bid  = _d("no_bid_dollars",  "no_bid")
    no_ask  = _d("no_ask_dollars",  "no_ask")

    # Derive missing sides: P(NO) = 1 - P(YES)
    if no_bid == 0 and yes_ask > 0:
        no_bid = round(1.0 - yes_ask, 3)
    if no_ask == 0 and yes_bid > 0:
        no_ask = round(1.0 - yes_bid, 3)

    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid":  no_bid,
        "no_ask":  no_ask,
    }


def price_to_bucks(price: float) -> int:
    """Convert a Kalshi price (0.0–1.0) to TSL Bucks cost."""
    return max(1, round(price * PAYOUT_SCALE))


def infer_category(event: dict) -> str:
    """Guess a human-readable category from a Kalshi event."""
    series = (event.get("series_ticker") or "").upper()
    title  = (event.get("title") or "").upper()
    for prefix, label in ALLOWED_CATEGORIES.items():
        if series.startswith(prefix) or prefix in title:
            return label
    return "🌐 Other"


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

    def __init__(self, ticker: str, side: str, price: float, title: str):
        super().__init__(title=f"Buy {side} — {title[:40]}")
        self.ticker = ticker
        self.side   = side
        self.price  = price   # 0.0–1.0
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
            balance = get_balance(user_id)
        except Exception:
            balance = 0

        if balance < cost_bucks:
            await interaction.response.send_message(
                f"❌ You need **{cost_bucks:,} TSL Bucks** but only have **{balance:,}**.",
                ephemeral=True,
            )
            return

        # Debit & record
        try:
            update_balance(user_id, -cost_bucks)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        # Write contract to DB
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT event_ticker FROM kalshi_markets WHERE ticker = ?
        """, (self.ticker,))
        row = c.fetchone()
        event_ticker = row[0] if row else self.ticker

        c.execute("""
            INSERT INTO kalshi_contracts
                (user_id, ticker, event_ticker, side, buy_price,
                 quantity, cost_bucks, potential_payout, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """, (
            user_id, self.ticker, event_ticker,
            self.side, self.price,
            quantity, cost_bucks, payout,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        conn.close()

        color  = 0x2ECC71 if self.side == "YES" else 0xE74C3C
        symbol = "✅" if self.side == "YES" else "❌"
        embed  = discord.Embed(
            title=f"{symbol} Contract Purchased",
            color=color,
            description=(
                f"**{self.market_title}**\n"
                f"Side: **{self.side}** · Price: **${self.price:.3f}** each\n"
                f"Quantity: **{quantity}** · Cost: **{cost_bucks:,} TSL Bucks**\n"
                f"Potential payout: **{payout:,} TSL Bucks** if {self.side} wins"
            ),
        )
        embed.set_footer(text="Use /portfolio to view your open contracts.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MarketView(discord.ui.View):
    """Yes/No buy buttons attached to a single market embed."""

    def __init__(self, ticker: str, title: str, yes_ask: float, no_ask: float):
        super().__init__(timeout=300)
        self.ticker    = ticker
        self.title     = title
        self.yes_ask   = yes_ask
        self.no_ask    = no_ask

        yes_bucks = price_to_bucks(yes_ask)
        no_bucks  = price_to_bucks(no_ask)

        self.add_item(discord.ui.Button(
            label=f"Buy YES  ({yes_bucks}¢)",
            style=discord.ButtonStyle.success,
            custom_id=f"buy_yes_{ticker}",
            emoji="✅",
        ))
        self.add_item(discord.ui.Button(
            label=f"Buy NO  ({no_bucks}¢)",
            style=discord.ButtonStyle.danger,
            custom_id=f"buy_no_{ticker}",
            emoji="❌",
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(label="placeholder_yes", custom_id="__placeholder_yes__")
    async def _placeholder(self, interaction, button):
        pass  # overridden by dynamic buttons above via add_item


class BrowseView(discord.ui.View):
    """Pagination view for browsing markets page-by-page."""

    def __init__(self, pages: list[discord.Embed], page_views: list[MarketView | None]):
        super().__init__(timeout=600)
        self.pages      = pages
        self.page_views = page_views
        self.current    = 0
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._refresh_buttons()
        await interaction.response.edit_message(
            embed=self.pages[self.current],
            view=self,
        )

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._refresh_buttons()
        await interaction.response.edit_message(
            embed=self.pages[self.current],
            view=self,
        )


class CategorySelect(discord.ui.Select):
    """Dropdown to filter markets by category."""

    def __init__(self, categories: list[str], parent_view):
        options = [discord.SelectOption(label="All Categories", value="all", default=True)]
        for cat in categories:
            options.append(discord.SelectOption(label=cat, value=cat))
        super().__init__(
            placeholder="Filter by category…",
            options=options[:25],   # Discord hard cap
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
        # Remove old nav buttons if present
        to_remove = [c for c in self.children if getattr(c, "custom_id", "") in ("nav_prev", "nav_next")]
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
            title="📊 ATLAS Flow — Prediction Markets",
            description=f"**{cat_label}** · {total} open markets · Page {self.page+1}/{self._max_page()+1}",
            color=CATEGORY_COLORS.get(cat_label, 0x7289DA),
        )

        for m in chunk:
            yes_bucks = price_to_bucks(m["yes_ask"])
            no_bucks  = price_to_bucks(m["no_ask"])
            exp = m.get("expiration_time", "Unknown")
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                exp_str = f"<t:{int(exp_dt.timestamp())}:R>"
            except Exception:
                exp_str = exp

            embed.add_field(
                name=f"{m.get('category','🌐 Other')}  {m['title'][:60]}",
                value=(
                    f"Ticker: `{m['ticker']}`\n"
                    f"YES: **{yes_bucks}¢** · NO: **{no_bucks}¢** · "
                    f"Vol: {m.get('volume', 0):,}\n"
                    f"Expires: {exp_str}"
                ),
                inline=False,
            )

        if not chunk:
            embed.add_field(name="No markets found", value="Try a different category.", inline=False)

        embed.set_footer(text="Use /bet <ticker> to place a wager on any market.")
        return embed


# ─────────────────────────────────────────────
# THE COG
# ─────────────────────────────────────────────

class KalshiCog(commands.Cog, name="Kalshi"):
    """
    ATLAS Flow Casino — Prediction Market Module.
    Syncs live market data from Kalshi every 5 minutes.
    """

    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self.client = KalshiClient()
        init_kalshi_db()
        self.sync_markets.start()

    def cog_unload(self):
        self.sync_markets.cancel()
        asyncio.create_task(self.client.close())

    # ── Background Sync ───────────────────────

    @tasks.loop(minutes=5)
    async def sync_markets(self):
        """Pull fresh event/market data from Kalshi, upsert prices, and auto-resolve settled markets."""
        log.info("Syncing Kalshi markets…")

        # ── Pass 1: Upsert all open markets with fresh prices ─────────────────
        events = await self.client.fetch_events(limit=200, status="open")
        if not events:
            log.warning("Kalshi sync returned 0 events.")
        else:
            conn = sqlite3.connect(DB_PATH)
            c    = conn.cursor()
            now  = datetime.now(timezone.utc).isoformat()
            upserted = 0

            for event in events:
                event_ticker = event.get("ticker") or event.get("event_ticker", "")
                category     = infer_category(event)

                markets = await self.client.fetch_markets_for_event(event_ticker)
                for mkt in markets:
                    ticker = mkt.get("ticker", "")
                    if not ticker:
                        continue

                    prices = extract_prices(mkt)
                    title  = mkt.get("title") or event.get("title", ticker)
                    status = mkt.get("status", "open")
                    result = mkt.get("result")          # e.g. "yes" | "no" | None
                    exp    = mkt.get("expiration_time", "")
                    vol    = mkt.get("volume", 0) or 0
                    oi     = mkt.get("open_interest", 0) or 0

                    c.execute("""
                        INSERT INTO kalshi_markets
                            (ticker, event_ticker, title, category,
                             yes_bid, yes_ask, no_bid, no_ask,
                             volume, open_interest, expiration_time,
                             status, result, last_synced)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(ticker) DO UPDATE SET
                            yes_bid        = excluded.yes_bid,
                            yes_ask        = excluded.yes_ask,
                            no_bid         = excluded.no_bid,
                            no_ask         = excluded.no_ask,
                            volume         = excluded.volume,
                            open_interest  = excluded.open_interest,
                            expiration_time= excluded.expiration_time,
                            status         = excluded.status,
                            result         = excluded.result,
                            last_synced    = excluded.last_synced
                    """, (
                        ticker, event_ticker, title, category,
                        prices["yes_bid"], prices["yes_ask"],
                        prices["no_bid"],  prices["no_ask"],
                        vol, oi, exp, status, result, now,
                    ))
                    upserted += 1

            conn.commit()
            conn.close()
            log.info(f"Kalshi sync complete — {upserted} open markets upserted.")

        # ── Pass 2: Fetch finalized events and auto-resolve settled markets ───
        await self._auto_resolve_pass()

    async def _auto_resolve_pass(self):
        """
        Fetch recently finalized Kalshi markets, cross-reference against our
        open contracts, and auto-resolve any that have a confirmed result.

        Guards:
          - Only resolves markets where resolved_by = 'pending' (never double-resolves)
          - Skips markets with no open contracts (nothing to pay out)
          - Requires a concrete result ('yes' or 'no') from Kalshi — never resolves on ambiguity
        """
        finalized_events = await self.client.fetch_finalized_events(limit=100)
        if not finalized_events:
            return

        auto_resolved = []

        for event in finalized_events:
            event_ticker = event.get("ticker") or event.get("event_ticker", "")
            markets = await self.client.fetch_markets_for_event(event_ticker)

            for mkt in markets:
                ticker = mkt.get("ticker", "")
                if not ticker:
                    continue

                # Kalshi result field: "yes" | "no" | None
                kalshi_result = (mkt.get("result") or "").lower().strip()
                if kalshi_result not in ("yes", "no"):
                    continue  # Not yet confirmed — skip

                # Check our DB: only act on markets still marked pending
                conn = sqlite3.connect(DB_PATH)
                c    = conn.cursor()
                c.execute("""
                    SELECT resolved_by FROM kalshi_markets
                    WHERE ticker = ?
                """, (ticker,))
                row = c.fetchone()

                if not row:
                    # Market isn't in our DB at all — upsert it so records are clean
                    title    = mkt.get("title") or event.get("title", ticker)
                    category = infer_category(event)
                    prices   = extract_prices(mkt)
                    exp      = mkt.get("expiration_time", "")
                    now      = datetime.now(timezone.utc).isoformat()
                    c.execute("""
                        INSERT OR IGNORE INTO kalshi_markets
                            (ticker, event_ticker, title, category,
                             yes_bid, yes_ask, no_bid, no_ask,
                             expiration_time, status, result,
                             resolved_by, last_synced)
                        VALUES (?,?,?,?,?,?,?,?,?,'finalized',?,?,?)
                    """, (
                        ticker, event_ticker, title, category,
                        prices["yes_bid"], prices["yes_ask"],
                        prices["no_bid"],  prices["no_ask"],
                        exp, kalshi_result, "pending", now,
                    ))
                    conn.commit()
                    conn.close()
                    row = ("pending",)
                else:
                    conn.close()

                resolved_by = row[0] if row else "pending"
                if resolved_by != "pending":
                    # Already resolved by auto or admin — skip
                    continue

                # Check there are open contracts to settle
                conn = sqlite3.connect(DB_PATH)
                c    = conn.cursor()
                c.execute("""
                    SELECT COUNT(*) FROM kalshi_contracts
                    WHERE ticker = ? AND status = 'open'
                """, (ticker,))
                open_count = c.fetchone()[0]
                conn.close()

                if open_count == 0:
                    # Mark resolved even with no contracts so we don't re-check next cycle
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("""
                        UPDATE kalshi_markets
                        SET status='finalized', result=?, resolved_by='auto'
                        WHERE ticker=?
                    """, (kalshi_result, ticker))
                    conn.commit()
                    conn.close()
                    continue

                # ── Settle it ──────────────────────────────────────────────
                result_upper = kalshi_result.upper()  # 'YES' | 'NO'
                log.info(f"Auto-resolving {ticker} → {result_upper} ({open_count} open contracts)")

                counts = await self._resolve(ticker, result_upper)

                # Mark market as auto-resolved
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    UPDATE kalshi_markets
                    SET resolved_by = 'auto', result = ?
                    WHERE ticker = ?
                """, (kalshi_result, ticker))
                conn.commit()
                conn.close()

                auto_resolved.append({
                    "ticker":  ticker,
                    "result":  result_upper,
                    "counts":  counts,
                    "title":   mkt.get("title") or event.get("title", ticker),
                })

        if auto_resolved:
            log.info(f"Auto-resolve pass complete — {len(auto_resolved)} market(s) settled.")
            await self._announce_resolutions(auto_resolved)

    async def _announce_resolutions(self, resolved: list[dict]):
        """Post a public resolution announcement for each auto-resolved market."""
        ch = self._channel()
        if not ch:
            log.warning("No prediction market channel configured — skipping resolution announcement.")
            return

        for item in resolved:
            ticker  = item["ticker"]
            result  = item["result"]       # 'YES' | 'NO'
            counts  = item["counts"]
            title   = item["title"]

            won   = counts.get("won", 0)
            lost  = counts.get("lost", 0)
            voided = counts.get("voided", 0)

            color  = 0x57F287 if result == "YES" else 0xED4245
            symbol = "✅" if result == "YES" else "❌"

            embed = discord.Embed(
                title=f"🏆 Market Resolved — `{ticker}`",
                color=color,
                description=(
                    f"**{title}**\n"
                    f"Result: **{symbol} {result}**\n\n"
                    f"**{won}** winning position(s) paid out · "
                    f"**{lost}** position(s) lost"
                    + (f" · **{voided}** voided" if voided else "")
                ),
            )

            # Show top winners (up to 5)
            conn = sqlite3.connect(DB_PATH)
            c    = conn.cursor()
            c.execute("""
                SELECT user_id, quantity, potential_payout, cost_bucks
                FROM kalshi_contracts
                WHERE ticker = ? AND status = 'won'
                ORDER BY potential_payout DESC
                LIMIT 5
            """, (ticker,))
            winners = c.fetchall()
            conn.close()

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
                log.error(f"Failed to post resolution announcement for {ticker}: {e}")

    @sync_markets.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()

    # ── Utility: resolve a channel lazily ────

    def _channel(self):
        return self.bot.get_channel(_prediction_channel_id())

    # ── Slash: /markets ───────────────────────

    @app_commands.command(name="markets", description="Browse live Kalshi prediction markets.")
    async def markets_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT ticker, event_ticker, title, category,
                   yes_ask, no_ask, volume, expiration_time
            FROM kalshi_markets
            WHERE status = 'open'
            ORDER BY volume DESC
            LIMIT 200
        """)
        rows = c.fetchall()
        conn.close()

        if not rows:
            await interaction.followup.send(
                "⚠️ No markets synced yet. Try again in a moment.", ephemeral=True
            )
            return

        markets = [
            {
                "ticker":          r[0],
                "event_ticker":    r[1],
                "title":           r[2],
                "category":        r[3],
                "yes_ask":         r[4] or 0.5,
                "no_ask":          r[5] or 0.5,
                "volume":          r[6] or 0,
                "expiration_time": r[7] or "",
            }
            for r in rows
        ]

        categories = sorted({m["category"] for m in markets})
        view       = MarketBrowserView(markets, categories)

        await interaction.followup.send(embed=view._embed(), view=view, ephemeral=True)

    # ── Slash: /bet <ticker> ──────────────────

    @app_commands.command(name="bet", description="Place a TSL Bucks wager on a Kalshi market.")
    @app_commands.describe(ticker="The market ticker, e.g. PRES-2024-DEM-WIN")
    async def bet_cmd(self, interaction: discord.Interaction, ticker: str):
        ticker = ticker.upper().strip()

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT ticker, title, category, yes_ask, no_ask,
                   volume, expiration_time, status
            FROM kalshi_markets WHERE ticker = ?
        """, (ticker,))
        row = c.fetchone()
        conn.close()

        if not row:
            await interaction.response.send_message(
                f"❌ Ticker `{ticker}` not found. Use `/markets` to browse.", ephemeral=True
            )
            return

        _, title, category, yes_ask, no_ask, volume, exp, status = row

        if status != "open":
            await interaction.response.send_message(
                f"⚠️ Market `{ticker}` is **{status}** and not accepting new bets.", ephemeral=True
            )
            return

        yes_ask = yes_ask or 0.5
        no_ask  = no_ask  or 0.5
        yes_bucks = price_to_bucks(yes_ask)
        no_bucks  = price_to_bucks(no_ask)

        color = CATEGORY_COLORS.get(category, 0x7289DA)
        try:
            exp_dt  = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            exp_str = f"<t:{int(exp_dt.timestamp())}:R>"
        except Exception:
            exp_str = exp or "Unknown"

        embed = discord.Embed(
            title=f"📊 {title}",
            color=color,
            description=(
                f"**Ticker:** `{ticker}`\n"
                f"**Category:** {category}\n"
                f"**Expires:** {exp_str}\n"
                f"**Volume:** {volume:,} contracts\n\n"
                f"Each contract pays out **{PAYOUT_SCALE} TSL Bucks** if your side wins."
            ),
        )
        embed.add_field(
            name="✅ Buy YES",
            value=f"**{yes_bucks} TSL Bucks** per contract\n*(implied prob: {yes_ask*100:.1f}%)*",
            inline=True,
        )
        embed.add_field(
            name="❌ Buy NO",
            value=f"**{no_bucks} TSL Bucks** per contract\n*(implied prob: {no_ask*100:.1f}%)*",
            inline=True,
        )
        embed.set_footer(text="Click a button below to open the wager modal.")

        view = BetButtonView(ticker=ticker, title=title, yes_ask=yes_ask, no_ask=no_ask)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── Slash: /portfolio ─────────────────────

    @app_commands.command(name="portfolio", description="View your open Kalshi prediction contracts.")
    async def portfolio_cmd(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT kc.ticker, km.title, kc.side, kc.buy_price,
                   kc.quantity, kc.cost_bucks, kc.potential_payout,
                   kc.status, kc.created_at
            FROM kalshi_contracts kc
            LEFT JOIN kalshi_markets km ON km.ticker = kc.ticker
            WHERE kc.user_id = ?
            ORDER BY kc.created_at DESC
            LIMIT 20
        """, (user_id,))
        rows = c.fetchall()
        conn.close()

        if not rows:
            await interaction.response.send_message(
                "You have no open prediction market contracts. Use `/bet` to place one!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📋 Your Prediction Market Portfolio",
            color=0x3498DB,
        )

        for r in rows:
            ticker, title, side, buy_price, qty, cost, payout, status, created = r
            sym   = "✅" if side == "YES" else "❌"
            s_map = {"open": "🟡 Open", "won": "🏆 Won", "lost": "💸 Lost", "voided": "🔁 Voided"}
            embed.add_field(
                name=f"{sym} {(title or ticker)[:50]}",
                value=(
                    f"`{ticker}` · **{side}** · {qty} contract(s)\n"
                    f"Paid: **{cost:,}¢** · Potential: **{payout:,}¢** · {s_map.get(status, status)}"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Slash: /resolve_market (admin) ────────

    @app_commands.command(name="resolve_market", description="[Admin] Resolve a Kalshi market outcome.")
    @app_commands.describe(
        ticker="Market ticker to resolve",
        result="The winning side: YES or NO, or VOID to refund all",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def resolve_market_cmd(
        self,
        interaction: discord.Interaction,
        ticker: str,
        result: str,
    ):
        ticker = ticker.upper().strip()
        result = result.upper().strip()

        if result not in ("YES", "NO", "VOID"):
            await interaction.response.send_message(
                "❌ `result` must be YES, NO, or VOID.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        resolved = await self._resolve(ticker, result, resolved_by="admin")

        # Post the same public announcement the auto-resolve uses
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT title FROM kalshi_markets WHERE ticker=?", (ticker,))
        row = c.fetchone()
        conn.close()

        await self._announce_resolutions([{
            "ticker": ticker,
            "result": result,
            "counts": resolved,
            "title":  row[0] if row else ticker,
        }])

        await interaction.followup.send(
            f"✅ Resolved `{ticker}` as **{result}**. "
            f"Processed {resolved['won']} winning and {resolved['lost']} losing contracts."
            + (f" Voided {resolved['voided']}." if resolved["voided"] else ""),
            ephemeral=True,
        )

    async def _resolve(self, ticker: str, result: str, resolved_by: str = "auto") -> dict:
        """
        Settle all open contracts for `ticker`.
        result: 'YES' | 'NO' | 'VOID'
        resolved_by: 'auto' | 'admin'
        Returns counts of won/lost/voided.
        """
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        now  = datetime.now(timezone.utc).isoformat()

        c.execute("""
            SELECT id, user_id, side, quantity, cost_bucks, potential_payout
            FROM kalshi_contracts
            WHERE ticker = ? AND status = 'open'
        """, (ticker,))
        contracts = c.fetchall()

        counts = {"won": 0, "lost": 0, "voided": 0}

        for cid, user_id, side, qty, cost, payout in contracts:
            if result == "VOID":
                update_balance(user_id, cost)   # Full refund
                c.execute(
                    "UPDATE kalshi_contracts SET status='voided', resolved_at=? WHERE id=?",
                    (now, cid)
                )
                counts["voided"] += 1
            elif side == result:
                update_balance(user_id, payout)  # Pay out winners
                c.execute(
                    "UPDATE kalshi_contracts SET status='won', resolved_at=? WHERE id=?",
                    (now, cid)
                )
                counts["won"] += 1
            else:
                c.execute(
                    "UPDATE kalshi_contracts SET status='lost', resolved_at=? WHERE id=?",
                    (now, cid)
                )
                counts["lost"] += 1

        # Mark market as resolved with who triggered it
        c.execute("""
            UPDATE kalshi_markets
            SET status='resolved', resolved_by=?
            WHERE ticker=?
        """, (resolved_by, ticker))
        conn.commit()
        conn.close()

        log.info(f"_resolve({ticker}, {result}, by={resolved_by}): {counts}")
        return counts

    # ── Interaction router (button presses) ───

    @commands.Cog.listener("on_interaction")
    async def _on_button(self, interaction: discord.Interaction):
        """Handle buy_yes / buy_no button presses from MarketView embeds."""
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id", "")
        if not (cid.startswith("buy_yes_") or cid.startswith("buy_no_")):
            return

        side   = "YES" if cid.startswith("buy_yes_") else "NO"
        ticker = cid[len("buy_yes_"):] if side == "YES" else cid[len("buy_no_"):]

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT title, yes_ask, no_ask FROM kalshi_markets WHERE ticker=?", (ticker,))
        row = c.fetchone()
        conn.close()

        if not row:
            await interaction.response.send_message("⚠️ Market not found.", ephemeral=True)
            return

        title, yes_ask, no_ask = row
        price = (yes_ask or 0.5) if side == "YES" else (no_ask or 0.5)
        modal = WagerModal(ticker=ticker, side=side, price=price, title=title)
        await interaction.response.send_modal(modal)


class BetButtonView(discord.ui.View):
    """YES / NO buttons on the /bet embed."""

    def __init__(self, ticker, title, yes_ask, no_ask):
        super().__init__(timeout=300)
        self.ticker  = ticker
        self.title   = title
        self.yes_ask = yes_ask
        self.no_ask  = no_ask

    @discord.ui.button(label="Buy YES ✅", style=discord.ButtonStyle.success)
    async def buy_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WagerModal(
            ticker=self.ticker, side="YES",
            price=self.yes_ask, title=self.title
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Buy NO ❌", style=discord.ButtonStyle.danger)
    async def buy_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WagerModal(
            ticker=self.ticker, side="NO",
            price=self.no_ask, title=self.title
        )
        await interaction.response.send_modal(modal)

    # ── Slash: /kalshi_status (admin) ─────────────

    @app_commands.command(name="kalshi_status", description="[Admin] Show Kalshi auth status and last sync info.")
    @app_commands.checks.has_permissions(administrator=True)
    async def kalshi_status_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Test connectivity by fetching a single event page
        events = await self.client.fetch_events(limit=1)
        auth_ok = events is not None   # None = hard error, [] = success but 0 events

        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("SELECT COUNT(*), MAX(last_synced) FROM kalshi_markets WHERE status='open'")
        count, last_sync = c.fetchone()
        conn.close()

        if KALSHI_API_KEY:
            method = "API Key (Bearer)"
        elif KALSHI_EMAIL:
            method = f"Email/Password ({KALSHI_EMAIL})"
            token_status = "✅ Token cached" if self.client._token else "❌ No token — login needed"
            method += f"\n{token_status}"
        else:
            method = "❌ No credentials configured"

        embed = discord.Embed(
            title="📊 Kalshi Integration Status",
            color=0x2ECC71 if auth_ok else 0xE74C3C,
        )
        embed.add_field(name="Auth Method", value=method, inline=False)
        embed.add_field(
            name="API Connectivity",
            value="✅ Connected" if auth_ok else "❌ Failed (check credentials / logs)",
            inline=True,
        )
        embed.add_field(name="Synced Markets", value=f"{count or 0} open", inline=True)
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
        embed.set_footer(text="Use /wittsync to force a full data reload.")
        await interaction.followup.send(embed=embed, ephemeral=True)




async def setup(bot: commands.Bot):
    await bot.add_cog(KalshiCog(bot))
    log.info("KalshiCog loaded.")
