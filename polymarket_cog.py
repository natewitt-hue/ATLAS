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
from atlas_colors import AtlasColors

import aiohttp
import aiosqlite
import hashlib
import json
import asyncio
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
import os
import re

from google import genai

import flow_wallet
from format_utils import fmt_volume
from flow_wallet import (
    DB_PATH,
    InsufficientFundsError,
)
from casino.renderer.prediction_html_renderer import (
    render_market_list_card,
    render_market_detail_card,
    render_bet_confirmation_card,
    render_portfolio_card,
    render_resolution_card,
    render_curated_list_card,
    render_daily_drop_card,
    render_price_alert_card,
)
import io

log = logging.getLogger("polymarket_cog")

PREDICTION_MAX_PAYOUT = 10_000_000  # sanity cap — matches sportsbook MAX_PAYOUT

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
    # ── Elections (campaigns, races, candidates) ──
    "elections":           "🗳️ Elections",
    "election":            "🗳️ Elections",
    "presidential":        "🗳️ Elections",
    "president":           "🗳️ Elections",
    "governor":            "🗳️ Elections",
    "senate":              "🗳️ Elections",
    "congress":            "🗳️ Elections",
    "mayor":               "🗳️ Elections",
    "primary":             "🗳️ Elections",
    "midterms":            "🗳️ Elections",
    "campaign":            "🗳️ Elections",
    # ── Government (policy, courts, governance) ──
    "politics":            "🏛️ Government",
    "government":          "🏛️ Government",
    "policy":              "🏛️ Government",
    "legislation":         "🏛️ Government",
    "court":               "🏛️ Government",
    "supreme court":       "🏛️ Government",
    "us-current-affairs":  "🏛️ Government",
    "us current affairs":  "🏛️ Government",
    # ── Pop Culture ──
    "pop culture":         "🌟 Pop Culture",
    "pop-culture":         "🌟 Pop Culture",
    "celebrity":           "🌟 Pop Culture",
    "social media":        "🌟 Pop Culture",
    "tiktok":              "🌟 Pop Culture",
    "viral":               "🌟 Pop Culture",
    "awards":              "🌟 Pop Culture",
    "oscars":              "🌟 Pop Culture",
    "grammys":             "🌟 Pop Culture",
    "reality tv":          "🌟 Pop Culture",
    "culture":             "🌟 Pop Culture",
    # ── Entertainment (movies, TV, streaming) ──
    "entertainment":       "🎬 Entertainment",
    # ── Crypto (blocked) ──
    "crypto":              "🪙 Crypto",
    # ── Sports (blocked) ──
    "sports":              "⚽ Sports",
    # ── Economics ──
    "business":            "📈 Economics",
    "finance":             "📈 Economics",
    "economics":           "📈 Economics",
    "economy":             "📈 Economics",
    "fed rates":           "📈 Economics",
    "fomc":                "📈 Economics",
    "economic policy":     "📈 Economics",
    "jerome powell":       "📈 Economics",
    "fed":                 "📈 Economics",
    # ── Science ──
    "science":             "🔬 Science",
    "health":              "🔬 Science",
    # ── Tech ──
    "tech":                "💻 Tech",
    # ── AI ──
    "ai":                  "🤖 AI",
    "artificial intelligence": "🤖 AI",
    # ── World ──
    "world":               "🌍 World",
    "climate":             "🌍 World",
    "iran":                "🌍 World",
    # ── Sports sub-categories (all blocked) ──
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
    "gaming":              "🎮 Gaming",
    "esports":             "🎮 Gaming",
}

CATEGORY_COLORS = {
    "🗳️ Elections":     0x5B9BD5,
    "🏛️ Government":    0x3498DB,
    "🌟 Pop Culture":   0xFF69B4,
    "🎬 Entertainment": 0xE91E63,
    "📈 Economics":     0x27AE60,
    "🔬 Science":       0x9B59B6,
    "💻 Tech":          0x1ABC9C,
    "🤖 AI":            0x00CED1,
    "🌍 World":         0xE67E22,
    "🌐 Other":         0x95A5A6,
}

# CSS hex version for HTML renderers — derived from CATEGORY_COLORS
# Keys strip the emoji prefix so renderers get plain names (e.g. "Elections")
CATEGORY_COLORS_HEX: dict[str, str] = {
    k.split(" ", 1)[1] if " " in k else k: f"#{v:06X}"
    for k, v in CATEGORY_COLORS.items()
}

MARKETS_PER_PAGE = 10        # Market rows shown per curated view
LOPSIDED_THRESHOLD = 0.80    # Filter markets where YES or NO > 80%

# Categories blocked from prediction markets
BLOCKED_CATEGORIES = {
    # Sports — use /sportsbook instead
    "⚽ Sports", "🏈 NFL", "🏀 NBA", "⚾ MLB", "🏒 NHL",
    "⚽ Soccer", "🥊 MMA", "♟️ Chess", "🎮 Gaming",
    # Crypto — degen noise, not relevant for the league
    "🪙 Crypto",
}

MAX_PER_CATEGORY = 4  # Cap per category in "All" view for diversity


def _compute_curation_score(
    market: dict,
    days_in_pool: float,
    same_category_count: int,
) -> tuple[float, dict]:
    """Compute 0-100 curation score for a market.

    Returns (score, breakdown_dict).
    Signals:
      - velocity (25%): log10 of 24hr volume, percentile-ranked
      - tension  (20%): how close to 50/50
      - freshness(20%): new markets boosted, decays over 20 days
      - urgency  (15%): time-to-close bonus (peak at 7 days)
      - liquidity(10%): higher = more trustworthy odds
      - diversity(10%): penalizes over-represented categories
    """
    vol_24h = market.get("volume_24hr", 0) or 0
    yes_p = market.get("yes_price", 0.5) or 0.5
    liquidity = market.get("liquidity", 0) or 0

    # ── Velocity (0-25): log10 of absolute 24hr volume ──
    velocity = min(math.log10(max(vol_24h, 1)) / 7.0, 1.0) * 25  # 7 = log10(10M)

    # ── Tension (0-20): closer to 50/50 = more interesting ──
    tension = (1 - abs(yes_p - 0.5) * 2) * 20

    # ── Freshness (0-20): new markets boosted, decays 1pt/day ──
    freshness = max(0, 20 - days_in_pool)

    # ── Urgency (0-15): peak at 7 days out ──
    end_date = market.get("end_date", "")
    urgency = 0.0
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            days_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
            if days_left < 0:
                urgency = 0
            elif days_left <= 3:
                urgency = 15  # maximum urgency
            elif days_left <= 7:
                urgency = 15  # peak zone
            elif days_left <= 30:
                urgency = 15 * (1 - (days_left - 7) / 23)  # linear decay 7→30 days
            elif days_left <= 90:
                urgency = 15 * 0.2 * (1 - (days_left - 30) / 60)  # slow decay
            # >90 days → 0
        except (ValueError, TypeError):
            urgency = 5  # fallback: some urgency

    # ── Liquidity (0-10) ──
    liq_score = min(liquidity / 100_000, 1.0) * 10

    # ── Diversity (0-10): penalize over-represented categories ──
    diversity = max(0, 10 - same_category_count * 2)

    score = velocity + tension + freshness + urgency + liq_score + diversity
    breakdown = {
        "velocity": round(velocity, 1),
        "tension": round(tension, 1),
        "freshness": round(freshness, 1),
        "urgency": round(urgency, 1),
        "liquidity": round(liq_score, 1),
        "diversity": round(diversity, 1),
    }
    return round(score, 2), breakdown


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

            -- Curation engine tables

            CREATE TABLE IF NOT EXISTS curated_scores (
                market_id   TEXT PRIMARY KEY,
                score       REAL NOT NULL,
                score_breakdown TEXT,
                cluster_id  TEXT,
                last_shown  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                FOREIGN KEY (market_id) REFERENCES prediction_markets(market_id)
            );

            CREATE TABLE IF NOT EXISTS daily_drops (
                drop_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                drop_date           TEXT NOT NULL UNIQUE,
                spotlight_market_id TEXT NOT NULL,
                spotlight_analysis  TEXT,
                supporting          TEXT,
                community_data      TEXT,
                leaderboard_data    TEXT,
                posted_at           TEXT,
                message_id          TEXT,
                FOREIGN KEY (spotlight_market_id) REFERENCES prediction_markets(market_id)
            );

            CREATE TABLE IF NOT EXISTS price_snapshots (
                market_id   TEXT NOT NULL,
                yes_price   REAL NOT NULL,
                snapshot_at TEXT NOT NULL,
                PRIMARY KEY (market_id, snapshot_at)
            );
            CREATE INDEX IF NOT EXISTS idx_price_snapshots_time
                ON price_snapshots(snapshot_at);

            CREATE TABLE IF NOT EXISTS market_engagement (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id   TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                user_id     TEXT,
                source      TEXT,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_engagement_market
                ON market_engagement(market_id, event_type);
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
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
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
                outcomes = market.get("outcomes")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except (json.JSONDecodeError, TypeError):
                        outcomes = None
                if isinstance(outcomes, list) and "Yes" in outcomes:
                    yes_idx = outcomes.index("Yes")
                    no_idx = outcomes.index("No") if "No" in outcomes else (1 - yes_idx)
                    yes_price = float(outcome_prices[yes_idx])
                    no_price = float(outcome_prices[no_idx])
                else:
                    yes_price = float(outcome_prices[0])
                    no_price = float(outcome_prices[1])
            except (ValueError, TypeError, IndexError):
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
            "election", "president", "governor", "senate",
            "economy", "fomc", "fed-rates",
            "pop-culture", "celebrity", "tiktok", "awards",
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
        # Sports (blocked)
        "nba-": "nba", "nfl-": "nfl", "mlb-": "mlb", "nhl-": "nhl",
        "soccer-": "soccer", "epl-": "epl", "ufc-": "ufc",
        "boxing-": "boxing", "mma-": "mma", "chess-": "chess",
        # Crypto (blocked)
        "bitcoin-": "crypto", "ethereum-": "crypto", "btc-": "crypto",
        "eth-": "crypto", "solana-": "crypto", "defi-": "crypto",
        # Elections
        "election-": "elections", "president-": "presidential",
        "governor-": "governor", "senate-": "senate",
        "campaign-": "campaign", "primary-": "primary",
        # Government
        "trump-": "politics", "biden-": "politics",
        "scotus-": "supreme court", "congress-": "politics",
        # Economics
        "fed-": "economics", "fomc-": "fomc",
        # Pop Culture
        "celebrity-": "celebrity", "tiktok-": "tiktok",
        "social-": "social media", "oscars-": "oscars",
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

        # Per-user lock prevents double-spend across concurrent bets
        async with flow_wallet.get_user_lock(user_id):
            # Atomic debit + contract creation in single transaction
            now = datetime.now(timezone.utc).isoformat()
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("BEGIN IMMEDIATE")

                    # Guard: market must still be active
                    async with db.execute(
                        "SELECT status FROM prediction_markets WHERE market_id = ?",
                        (self.market_id,)
                    ) as cur:
                        mkt_row = await cur.fetchone()
                    if not mkt_row or mkt_row[0] != 'active':
                        await interaction.response.send_message(
                            "This market is no longer active.", ephemeral=True)
                        return

                    # Check balance and debit atomically
                    balance = await flow_wallet.get_balance(user_id, con=db)
                    if balance < cost_bucks:
                        await interaction.response.send_message(
                            f"❌ You need **{cost_bucks:,} TSL Bucks** but only have **{balance:,}**.",
                            ephemeral=True,
                        )
                        return

                    await flow_wallet.debit(
                        user_id, cost_bucks, "PREDICTION",
                        description="prediction market bet",
                        con=db,
                    )

                    # Write contract in same transaction
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
            except flow_wallet.InsufficientFundsError as e:
                await interaction.response.send_message(f"❌ {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.response.send_message(
                    f"❌ Failed to place bet: {e}", ephemeral=True
                )
                return

        new_bal = balance - cost_bucks
        color = 0x2ECC71 if self.side == "YES" else 0xE74C3C

        # Render V6 bet confirmation card
        embed = discord.Embed(
            title="Contract Purchased",
            color=color,
        )
        embed.set_footer(text="Use /portfolio to view your positions · FLOW Markets")
        embed.timestamp = datetime.now(timezone.utc)

        try:
            png = await render_bet_confirmation_card(
                market_title=self.market_title,
                side=self.side,
                price=self.price,
                quantity=quantity,
                cost=cost_bucks,
                potential_payout=payout,
                balance=new_bal,
                player_name=interaction.user.display_name,
            )
            card_file = discord.File(io.BytesIO(png), filename="bet_confirm.png")
            embed.set_image(url="attachment://bet_confirm.png")
            await interaction.response.send_message(
                embed=embed, file=card_file, ephemeral=True
            )
        except Exception:
            log.exception("Failed to render bet confirmation card")
            profit = payout - cost_bucks
            embed.description = (
                f"**{self.market_title}**\n"
                f"Side: **{self.side}** · Qty: **{quantity}** · Cost: **${cost_bucks:,}**\n"
                f"Potential: **${payout:,}** · Profit: **+${profit:,}**"
            )
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
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        await self.parent_view.apply_filter(interaction, chosen)


class MarketSelect(discord.ui.Select):
    """Dropdown to select a specific market from the current page."""

    def __init__(self, markets: list[dict], parent_view):
        options = []
        for m in markets[:25]:
            title = m.get("title", "Untitled")
            yes_p = m.get("yes_price", 0.5)
            cat = m.get("category", "Other")
            options.append(discord.SelectOption(
                label=title[:100],
                value=m.get("market_id", ""),
                description=f"YES {yes_p:.0%} | {cat}",
            ))
        if not options:
            options = [discord.SelectOption(label="No markets", value="none")]
        super().__init__(
            placeholder="Select a market to view details…",
            options=options,
            min_values=1,
            max_values=1,
            row=1,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        chosen_id = self.values[0]
        if chosen_id == "none":
            return
        await self.parent_view.select_market(interaction, chosen_id)


class MarketBrowserView(discord.ui.View):
    """Drill-down market browser: list → detail → bet."""

    def __init__(self, all_markets: list[dict], categories: list[str],
                 category_counts: dict | None = None,
                 cog=None):
        super().__init__(timeout=600)
        self.all_markets     = all_markets
        self.categories      = categories
        self.category_counts = category_counts or {}
        self.filter          = "all"
        self.page            = 0
        self.state           = "list"       # "list" or "detail"
        self.selected_market = None         # dict when in detail state
        self.cog             = cog

        self._rebuild_components()

    # ── Filtering / Pagination helpers ──

    def _filtered(self) -> list[dict]:
        if self.filter == "all":
            # Apply per-category cap for diversity
            seen: dict[str, int] = {}
            result = []
            for m in self.all_markets:  # already sorted by score
                cat = m.get("category", "Other")
                seen[cat] = seen.get(cat, 0) + 1
                if seen[cat] <= MAX_PER_CATEGORY:
                    result.append(m)
            return result
        return [m for m in self.all_markets if m.get("category") == self.filter]

    def _max_page(self) -> int:
        return max(0, (len(self._filtered()) - 1) // MARKETS_PER_PAGE)

    def _current_chunk(self) -> list[dict]:
        markets = self._filtered()
        start = self.page * MARKETS_PER_PAGE
        return markets[start : start + MARKETS_PER_PAGE]

    # ── Component rebuilding ──

    def _rebuild_components(self):
        """Clear all components and rebuild for the current state."""
        self.clear_items()

        if self.state == "list":
            self._build_list_components()
        else:
            self._build_detail_components()

    def _build_list_components(self):
        """Build components for list view: CategorySelect + MarketSelect + nav."""
        # Row 0: Category filter
        cat_select = CategorySelect(
            self.categories, parent_view=self,
            category_counts=self.category_counts,
        )
        self.add_item(cat_select)

        # Row 1: Market select dropdown
        chunk = self._current_chunk()
        market_select = MarketSelect(chunk, parent_view=self)
        self.add_item(market_select)

        # Row 2: Navigation
        prev_btn = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_prev",
            disabled=self.page == 0,
            row=2,
        )
        page_btn = discord.ui.Button(
            label=f"{self.page + 1}/{self._max_page() + 1}",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_page",
            disabled=True,
            row=2,
        )
        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="nav_next",
            disabled=self.page >= self._max_page(),
            row=2,
        )
        prev_btn.callback = self._prev
        next_btn.callback = self._next
        self.add_item(prev_btn)
        self.add_item(page_btn)
        self.add_item(next_btn)

    def _build_detail_components(self):
        """Build components for detail view: Back + YES + NO."""
        back_btn = discord.ui.Button(
            label="🔙 Back to List",
            style=discord.ButtonStyle.secondary,
            custom_id="back_list",
            row=0,
        )
        back_btn.callback = self._back_to_list
        self.add_item(back_btn)

        if self.selected_market:
            yes_btn = discord.ui.Button(
                label="Bet YES ✅",
                style=discord.ButtonStyle.success,
                custom_id="detail_yes",
                row=1,
            )
            no_btn = discord.ui.Button(
                label="Bet NO ❌",
                style=discord.ButtonStyle.danger,
                custom_id="detail_no",
                row=1,
            )
            yes_btn.callback = self._make_bet_cb(self.selected_market, "YES")
            no_btn.callback = self._make_bet_cb(self.selected_market, "NO")
            self.add_item(yes_btn)
            self.add_item(no_btn)

    def _make_bet_cb(self, market: dict, side: str):
        """Closure-safe callback: cached odds → open wager modal."""
        async def callback(interaction: discord.Interaction):
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

    # ── State transitions ──

    async def select_market(self, interaction: discord.Interaction, market_id: str):
        """Transition from list → detail view for a specific market."""
        # Find the market in our data
        market = next(
            (m for m in self.all_markets if m.get("market_id") == market_id), None
        )
        if not market:
            await interaction.response.defer()
            return

        self.state = "detail"
        self.selected_market = market
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _back_to_list(self, interaction: discord.Interaction):
        """Transition from detail → list view."""
        self.state = "list"
        self.selected_market = None
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def apply_filter(self, interaction: discord.Interaction, cat: str):
        self.filter = cat
        self.page = 0
        self.state = "list"
        self.selected_market = None
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self._max_page(), self.page + 1)
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    # ── Page builders ──

    async def _build_page(self) -> tuple[discord.Embed, discord.File | None]:
        """Build embed + V6 card for the current state."""
        if self.state == "detail" and self.selected_market:
            return await self._build_detail_page()
        return await self._build_list_page()

    async def _build_list_page(self) -> tuple[discord.Embed, discord.File | None]:
        """Build the market list view."""
        markets = self._filtered()
        total = len(markets)
        chunk = self._current_chunk()

        cat_label = "All Categories" if self.filter == "all" else self.filter
        embed = discord.Embed(
            title="FLOW Prediction Markets",
            description=f"**{total}** markets · Select one below to view details",
            color=CATEGORY_COLORS.get(cat_label, AtlasColors.TSL_GOLD.value),
        )

        card_file = None
        if chunk:
            try:
                png = await render_market_list_card(
                    chunk,
                    page=self.page + 1,
                    total_pages=self._max_page() + 1,
                    filter_label=cat_label,
                )
                card_file = discord.File(io.BytesIO(png), filename="markets.png")
                embed.set_image(url="attachment://markets.png")
            except Exception:
                log.exception("Failed to render market list card")
                for m in chunk:
                    yes_p = m.get("yes_price", 0.5)
                    no_p = m.get("no_price", 0.5)
                    cat = m.get("category", "Other")
                    embed.add_field(
                        name=f"{cat}  {m['title'][:55]}",
                        value=f"YES {yes_p:.0%}  |  NO {no_p:.0%}",
                        inline=False,
                    )
        else:
            embed.add_field(
                name="No markets found",
                value="Try a different category or check back later.",
                inline=False,
            )

        embed.set_footer(text="FLOW Markets · Powered by Polymarket")
        embed.timestamp = datetime.now(timezone.utc)
        return embed, card_file

    async def _build_detail_page(self) -> tuple[discord.Embed, discord.File | None]:
        """Build the single-market detail view."""
        m = self.selected_market
        cat_label = m.get("category", "Other")

        embed = discord.Embed(
            title=m.get("title", "")[:80],
            color=CATEGORY_COLORS.get(cat_label, AtlasColors.TSL_GOLD.value),
        )

        card_file = None
        try:
            png = await render_market_detail_card(
                title=m.get("title", ""),
                category=m.get("category", "Other"),
                yes_price=m.get("yes_price", 0.5),
                no_price=m.get("no_price", 0.5),
                volume=m.get("volume", 0),
                liquidity=m.get("liquidity", 0),
                end_date=m.get("end_date", ""),
            )
            card_file = discord.File(io.BytesIO(png), filename="market_detail.png")
            embed.set_image(url="attachment://market_detail.png")
        except Exception:
            log.exception("Failed to render market detail card")
            yes_p = m.get("yes_price", 0.5)
            no_p = m.get("no_price", 0.5)
            embed.add_field(name="YES", value=f"{yes_p:.0%}", inline=True)
            embed.add_field(name="NO", value=f"{no_p:.0%}", inline=True)

        embed.set_footer(text="FLOW Markets · Click YES or NO below to bet")
        embed.timestamp = datetime.now(timezone.utc)
        return embed, card_file


# ─────────────────────────────────────────────
# CURATED BROWSER VIEW (replaces MarketBrowserView for /markets)
# ─────────────────────────────────────────────

class CuratedMarketSelect(discord.ui.Select):
    """Select menu for drilling into a curated market."""

    def __init__(self, markets: list[dict], parent_view):
        self._parent = parent_view
        options = []
        for i, m in enumerate(markets[:25]):
            cat = m.get("category", "Other")
            parts = cat.split(" ", 1)
            emoji = parts[0] if len(parts) > 1 else "📊"
            label = m.get("title", "")[:95]
            yes_p = m.get("yes_price", 0.5)
            desc = f"YES {yes_p:.0%}"
            sentiment = m.get("sentiment", {})
            if sentiment.get("total", 0) > 0:
                desc += f" · {sentiment['label']}"
            options.append(discord.SelectOption(
                label=label,
                value=m.get("market_id", str(i)),
                description=desc[:100],
                emoji=emoji,
            ))
        if not options:
            options = [discord.SelectOption(label="No markets", value="none")]
        super().__init__(
            placeholder="Select a market to view details...",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        market_id = self.values[0]
        if market_id == "none":
            await interaction.response.defer()
            return
        if self._parent is None:
            await interaction.response.send_message("This view has expired. Please re-open the market browser.", ephemeral=True)
            return
        await self._parent.select_market(interaction, market_id)


class CuratedBrowserView(discord.ui.View):
    """Curated market browser with weighted rotation."""

    def __init__(
        self,
        markets: list[dict],
        categories: list[str],
        category_counts: dict | None = None,
        cog=None,
        view_mode: str = "curated",
        filter_category: str | None = None,
    ):
        super().__init__(timeout=600)
        self.markets = markets
        self.categories = categories
        self.category_counts = category_counts or {}
        self.cog = cog
        self.view_mode = view_mode
        self.filter_category = filter_category
        self.state = "list"
        self.selected_market = None

        self._rebuild_components()

    def _rebuild_components(self):
        self.clear_items()
        if self.state == "list":
            self._build_list_components()
        else:
            self._build_detail_components()

    def _build_list_components(self):
        # Row 0: Market select
        select = CuratedMarketSelect(self.markets, parent_view=self)
        self.add_item(select)

        # Row 1: Refresh button
        refresh_btn = discord.ui.Button(
            label="🔄 Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="curated_refresh",
            row=1,
        )
        refresh_btn.callback = self._refresh
        self.add_item(refresh_btn)

    def _build_detail_components(self):
        back_btn = discord.ui.Button(
            label="🔙 Back to List",
            style=discord.ButtonStyle.secondary,
            custom_id="curated_back",
            row=0,
        )
        back_btn.callback = self._back_to_list
        self.add_item(back_btn)

        if self.selected_market:
            yes_btn = discord.ui.Button(
                label="Bet YES ✅",
                style=discord.ButtonStyle.success,
                custom_id="curated_yes",
                row=1,
            )
            no_btn = discord.ui.Button(
                label="Bet NO ❌",
                style=discord.ButtonStyle.danger,
                custom_id="curated_no",
                row=1,
            )
            yes_btn.callback = self._make_bet_cb(self.selected_market, "YES")
            no_btn.callback = self._make_bet_cb(self.selected_market, "NO")
            self.add_item(yes_btn)
            self.add_item(no_btn)

    def _make_bet_cb(self, market: dict, side: str):
        async def callback(interaction: discord.Interaction):
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

    async def select_market(self, interaction: discord.Interaction, market_id: str):
        market = next((m for m in self.markets if m.get("market_id") == market_id), None)
        if not market:
            await interaction.response.defer()
            return

        # Log engagement
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO market_engagement (market_id, event_type, user_id, source, created_at) "
                    "VALUES (?, 'view', ?, 'markets_cmd', ?)",
                    (market_id, str(interaction.user.id), datetime.now(timezone.utc).isoformat()),
                )
                await db.commit()
        except Exception:
            pass

        self.state = "detail"
        self.selected_market = market
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _back_to_list(self, interaction: discord.Interaction):
        self.state = "list"
        self.selected_market = None
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _refresh(self, interaction: discord.Interaction):
        """Refresh with a new curated selection."""
        if self.cog:
            self.markets = await self.cog._get_curated_selection(
                count=MARKETS_PER_PAGE,
                category=self.filter_category,
                view_mode=self.view_mode,
            )
        self._rebuild_components()
        embed, card_file = await self._build_page()
        kwargs = {"embed": embed, "view": self, "attachments": []}
        if card_file:
            kwargs["attachments"] = [card_file]
        await interaction.response.edit_message(**kwargs)

    async def _build_page(self) -> tuple[discord.Embed, discord.File | None]:
        if self.state == "detail" and self.selected_market:
            return await self._build_detail_page()
        return await self._build_list_page()

    async def _build_list_page(self) -> tuple[discord.Embed, discord.File | None]:
        total = len(self.markets)
        view_labels = {
            "curated": "Curated", "trending": "Trending",
            "popular": "Popular", "new": "New",
        }
        filter_label = self.filter_category or f"{view_labels.get(self.view_mode, 'Curated')} · All Categories"

        embed = discord.Embed(
            title="FLOW Prediction Markets",
            description=f"**{total}** markets · Select one below to view details & bet",
            color=AtlasColors.TSL_GOLD,
        )

        card_file = None
        if self.markets:
            try:
                png = await render_curated_list_card(
                    self.markets,
                    filter_label=filter_label,
                )
                card_file = discord.File(io.BytesIO(png), filename="markets.png")
                embed.set_image(url="attachment://markets.png")
            except Exception:
                log.exception("Failed to render curated list card")
                for m in self.markets:
                    yes_p = m.get("yes_price", 0.5)
                    no_p = m.get("no_price", 0.5)
                    embed.add_field(
                        name=f"{m.get('category', '')}  {m['title'][:55]}",
                        value=f"YES {yes_p:.0%}  |  NO {no_p:.0%}",
                        inline=False,
                    )

        embed.set_footer(text="FLOW Markets · Powered by Polymarket")
        embed.timestamp = datetime.now(timezone.utc)
        return embed, card_file

    async def _build_detail_page(self) -> tuple[discord.Embed, discord.File | None]:
        m = self.selected_market
        embed = discord.Embed(
            title=m.get("title", "")[:80],
            color=0x3498DB,
        )

        card_file = None
        try:
            png = await render_market_detail_card(
                title=m.get("title", ""),
                category=m.get("category", "Other"),
                yes_price=m.get("yes_price", 0.5),
                no_price=m.get("no_price", 0.5),
                volume=m.get("volume", 0),
                liquidity=m.get("liquidity", 0),
                end_date=m.get("end_date", ""),
            )
            card_file = discord.File(io.BytesIO(png), filename="market_detail.png")
            embed.set_image(url="attachment://market_detail.png")
        except Exception:
            log.exception("Failed to render market detail card")
            embed.add_field(name="YES", value=f"{m.get('yes_price', 0.5):.0%}", inline=True)
            embed.add_field(name="NO", value=f"{m.get('no_price', 0.5):.0%}", inline=True)

        # Add sentiment info
        sentiment = m.get("sentiment", {})
        if sentiment.get("total", 0) > 0:
            embed.description = f"🏛️ {sentiment['label']}"

        embed.set_footer(text="FLOW Markets · Click YES or NO below to bet")
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
        self._sync_count = 0
        self._alerts_this_hour = 0
        self._alert_hour = -1
        self.sync_markets.start()
        self.daily_drop_task.start()

    def cog_unload(self):
        self.sync_markets.cancel()
        self.daily_drop_task.cancel()
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

        # ── Pass 2b: Local DB scan — settle contracts the API pass missed ──
        await self._local_settle_pass()

        # ── Pass 3: Gemini classification for "Other" markets (first sync only) ──
        if not self._first_sync_done:
            self._first_sync_done = True
            try:
                await self._classify_unknown_categories()
            except Exception as e:
                log.warning(f"Gemini classification pass failed: {e}")

        # ── Pass 4: Update curation scores ──
        try:
            await self._update_curation_scores()
        except Exception as e:
            log.warning(f"Curation scoring pass failed: {e}")

        # ── Pass 5: Price snapshots (every 3rd sync = ~15 min) ──
        self._sync_count = getattr(self, "_sync_count", 0) + 1
        if self._sync_count % 3 == 0:
            try:
                await self._store_price_snapshots()
            except Exception as e:
                log.warning(f"Price snapshot pass failed: {e}")

        # ── Pass 6: Price movement alerts ──
        try:
            await self._check_price_alerts()
        except Exception as e:
            log.warning(f"Price alert check failed: {e}")

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

    # ── Curation Engine ─────────────────────────

    async def _update_curation_scores(self):
        """Score all active markets for curation. Runs every sync cycle."""
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            # Fetch all active markets
            blocked_ph = ",".join("?" for _ in BLOCKED_CATEGORIES)
            async with db.execute(f"""
                SELECT market_id, event_id, title, category,
                       yes_price, no_price, volume, liquidity,
                       COALESCE(volume_24hr, 0), end_date
                FROM prediction_markets
                WHERE status = 'active'
                  AND category NOT IN ({blocked_ph})
            """, tuple(BLOCKED_CATEGORIES)) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                return

            # Get existing created_at timestamps for freshness
            async with db.execute(
                "SELECT market_id, created_at FROM curated_scores"
            ) as cursor:
                existing = {r[0]: r[1] for r in await cursor.fetchall()}

            # Count markets per category for diversity scoring
            cat_counts: dict[str, int] = {}
            for r in rows:
                cat = r[3] or "Other"
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

            # Score each market, dedup by event_id
            event_best: dict[str, tuple[str, float, dict]] = {}  # event_id → (market_id, score, breakdown)

            for r in rows:
                market_id, event_id, title, category = r[0], r[1], r[2], r[3]
                market = {
                    "yes_price": r[4], "no_price": r[5],
                    "volume": r[6], "liquidity": r[7],
                    "volume_24hr": r[8], "end_date": r[9],
                }

                # Calculate days in pool
                created = existing.get(market_id)
                if created:
                    try:
                        created_dt = datetime.fromisoformat(created)
                        days_in_pool = (datetime.now(timezone.utc) - created_dt).total_seconds() / 86400
                    except (ValueError, TypeError):
                        days_in_pool = 0
                else:
                    days_in_pool = 0

                same_cat = cat_counts.get(category or "Other", 0)
                score, breakdown = _compute_curation_score(market, days_in_pool, same_cat)

                # Dedup by event_id: keep highest score per event
                cluster = event_id or market_id
                if cluster not in event_best or score > event_best[cluster][1]:
                    event_best[cluster] = (market_id, score, breakdown)

            # Upsert scores for winning markets
            winners = {mid for mid, _, _ in event_best.values()}
            for cluster, (market_id, score, breakdown) in event_best.items():
                created_at = existing.get(market_id, now)
                await db.execute("""
                    INSERT INTO curated_scores (market_id, score, score_breakdown, cluster_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        score = excluded.score,
                        score_breakdown = excluded.score_breakdown,
                        cluster_id = excluded.cluster_id,
                        updated_at = excluded.updated_at
                """, (market_id, score, json.dumps(breakdown), cluster, created_at, now))

            # Remove scores for markets no longer active or that lost dedup
            active_ids = {r[0] for r in rows}
            async with db.execute("SELECT market_id FROM curated_scores") as cursor:
                all_scored = {r[0] for r in await cursor.fetchall()}

            stale = all_scored - winners
            if stale:
                ph = ",".join("?" for _ in stale)
                await db.execute(f"DELETE FROM curated_scores WHERE market_id IN ({ph})", tuple(stale))

            await db.commit()
            log.info(f"Curation scores updated: {len(winners)} markets scored.")

    async def _get_curated_selection(
        self,
        count: int = 10,
        category: str | None = None,
        view_mode: str = "curated",
    ) -> list[dict]:
        """Get a curated selection of markets using weighted random sampling.

        view_mode: 'curated' (weighted random), 'trending' (top volume_24hr),
                   'popular' (most TSL bets), 'new' (newest)
        """
        async with aiosqlite.connect(DB_PATH) as db:
            if view_mode == "trending":
                query = """
                    SELECT pm.market_id, pm.slug, pm.title, pm.category,
                           pm.yes_price, pm.no_price, pm.volume, pm.end_date,
                           COALESCE(pm.volume_24hr, 0) as v24, pm.liquidity, pm.event_id
                    FROM prediction_markets pm
                    WHERE pm.status = 'active'
                """
                params: list = []
                if category:
                    query += " AND pm.category = ?"
                    params.append(category)
                query += " ORDER BY v24 DESC LIMIT ?"
                params.append(count)
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()

            elif view_mode == "popular":
                query = """
                    SELECT pm.market_id, pm.slug, pm.title, pm.category,
                           pm.yes_price, pm.no_price, pm.volume, pm.end_date,
                           COALESCE(pm.volume_24hr, 0), pm.liquidity, pm.event_id,
                           COUNT(pc.id) as bet_count
                    FROM prediction_markets pm
                    LEFT JOIN prediction_contracts pc ON pc.market_id = pm.market_id AND pc.status = 'open'
                    WHERE pm.status = 'active'
                """
                params = []
                if category:
                    query += " AND pm.category = ?"
                    params.append(category)
                query += " GROUP BY pm.market_id ORDER BY bet_count DESC LIMIT ?"
                params.append(count)
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()

            elif view_mode == "new":
                query = """
                    SELECT pm.market_id, pm.slug, pm.title, pm.category,
                           pm.yes_price, pm.no_price, pm.volume, pm.end_date,
                           COALESCE(pm.volume_24hr, 0), pm.liquidity, pm.event_id
                    FROM prediction_markets pm
                    WHERE pm.status = 'active'
                """
                params = []
                if category:
                    query += " AND pm.category = ?"
                    params.append(category)
                query += " ORDER BY pm.last_synced DESC LIMIT ?"
                params.append(count)
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()

            else:  # curated — weighted random
                query = """
                    SELECT pm.market_id, pm.slug, pm.title, pm.category,
                           pm.yes_price, pm.no_price, pm.volume, pm.end_date,
                           COALESCE(pm.volume_24hr, 0), pm.liquidity, pm.event_id,
                           cs.score, cs.last_shown
                    FROM curated_scores cs
                    JOIN prediction_markets pm ON pm.market_id = cs.market_id
                    WHERE pm.status = 'active'
                """
                params = []
                if category:
                    query += " AND pm.category = ?"
                    params.append(category)
                query += " ORDER BY cs.score DESC LIMIT 100"
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()

                if rows:
                    rows = self._weighted_sample(rows, count)

            # Build market dicts
            markets = []
            for r in rows:
                markets.append({
                    "market_id":   r[0],
                    "slug":        r[1],
                    "title":       r[2],
                    "category":    r[3],
                    "yes_price":   r[4] if r[4] is not None else 0.5,
                    "no_price":    r[5] if r[5] is not None else 0.5,
                    "volume":      r[6] or 0,
                    "end_date":    r[7] or "",
                    "volume_24hr": r[8] or 0,
                    "liquidity":   r[9] or 0,
                    "event_id":    r[10] if len(r) > 10 else "",
                })

            # Add community sentiment
            for m in markets:
                sentiment = await self._get_community_sentiment(m["market_id"], db)
                m["sentiment"] = sentiment

            # Update last_shown for curated mode
            if view_mode == "curated" and markets:
                now = datetime.now(timezone.utc).isoformat()
                for m in markets:
                    await db.execute(
                        "UPDATE curated_scores SET last_shown = ? WHERE market_id = ?",
                        (now, m["market_id"]),
                    )
                await db.commit()

        return markets

    def _weighted_sample(self, rows: list, count: int) -> list:
        """Weighted random sampling without replacement with recency penalty and category diversity."""
        now = datetime.now(timezone.utc)
        weighted = []
        for r in rows:
            score = r[11] if len(r) > 11 else 1.0
            last_shown = r[12] if len(r) > 12 else None

            # Recency penalty
            penalty = 1.0
            if last_shown:
                try:
                    shown_dt = datetime.fromisoformat(last_shown)
                    hours_ago = (now - shown_dt).total_seconds() / 3600
                    if hours_ago < 2:
                        penalty = 0.1
                    elif hours_ago < 12:
                        penalty = 0.5
                except (ValueError, TypeError):
                    pass

            weighted.append((r, max(score * penalty, 0.01)))

        # Weighted random sampling without replacement, with category diversity
        selected = []
        cat_counts: dict[str, int] = {}
        remaining = list(weighted)

        for _ in range(min(count, len(remaining))):
            if not remaining:
                break

            weights = [w for _, w in remaining]
            total = sum(weights)
            if total <= 0:
                break

            probs = [w / total for w in weights]
            idx = random.choices(range(len(remaining)), weights=probs, k=1)[0]
            row, _ = remaining[idx]

            cat = row[3] or "Other"  # category is at index 3
            if cat_counts.get(cat, 0) >= 2:
                # Skip — try next best
                remaining.pop(idx)
                continue

            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            selected.append(row)
            remaining.pop(idx)

        return selected

    async def _get_community_sentiment(self, market_id: str, db) -> dict:
        """Get TSL community betting sentiment for a market."""
        async with db.execute("""
            SELECT side, COUNT(*) as cnt
            FROM prediction_contracts
            WHERE market_id = ? AND status = 'open'
            GROUP BY side
        """, (market_id,)) as cursor:
            rows = await cursor.fetchall()

        yes_count = 0
        no_count = 0
        for side, cnt in rows:
            if side == "YES":
                yes_count = cnt
            else:
                no_count = cnt

        total = yes_count + no_count
        return {
            "yes_count": yes_count,
            "no_count": no_count,
            "total": total,
            "yes_pct": round(yes_count / total * 100) if total > 0 else 0,
            "label": (
                f"TSL is {round(yes_count / total * 100)}% YES"
                if total > 0
                else "Be the first to bet"
            ),
        }

    # ── Price Snapshots & Alerts ─────────────────

    async def _store_price_snapshots(self):
        """Store price snapshots for movement detection. Runs every ~15 min."""
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT market_id, yes_price FROM prediction_markets WHERE status = 'active'"
            ) as cursor:
                rows = await cursor.fetchall()

            for market_id, yes_price in rows:
                await db.execute(
                    "INSERT OR IGNORE INTO price_snapshots (market_id, yes_price, snapshot_at) "
                    "VALUES (?, ?, ?)",
                    (market_id, yes_price, now),
                )

            # Prune snapshots older than 48 hours
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            await db.execute("DELETE FROM price_snapshots WHERE snapshot_at < ?", (cutoff,))

            await db.commit()

    async def _check_price_alerts(self):
        """Detect >10pp price movements in the last hour and post alerts."""
        now = datetime.now(timezone.utc)
        hour_ago = (now - timedelta(hours=1)).isoformat()

        # Rate limit: max 3 alerts per hour
        alerts_this_hour = getattr(self, "_alerts_this_hour", 0)
        alert_hour = getattr(self, "_alert_hour", 0)
        current_hour = now.hour
        if current_hour != alert_hour:
            alerts_this_hour = 0
            self._alert_hour = current_hour

        if alerts_this_hour >= 3:
            return

        # Get today's daily drop markets to skip
        today = now.strftime("%Y-%m-%d")
        drop_market_ids: set[str] = set()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT spotlight_market_id, supporting FROM daily_drops WHERE drop_date = ?",
                (today,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    drop_market_ids.add(row[0])
                    try:
                        supporting = json.loads(row[1]) if row[1] else []
                        for s in supporting:
                            if isinstance(s, dict):
                                drop_market_ids.add(s.get("market_id", ""))
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Find markets with big moves
            async with db.execute("""
                SELECT pm.market_id, pm.title, pm.category, pm.yes_price, pm.no_price,
                       ps.yes_price as old_price
                FROM prediction_markets pm
                JOIN price_snapshots ps ON ps.market_id = pm.market_id
                WHERE pm.status = 'active'
                  AND ps.snapshot_at <= ?
                  AND ps.snapshot_at >= ?
                ORDER BY ps.snapshot_at ASC
            """, (hour_ago, (now - timedelta(hours=1, minutes=30)).isoformat())) as cursor:
                rows = await cursor.fetchall()

            for market_id, title, category, current_price, no_price, old_price in rows:
                if market_id in drop_market_ids:
                    continue

                delta = abs(current_price - old_price)
                if delta < 0.10:
                    continue

                if alerts_this_hour >= 3:
                    break

                # Get holder count
                async with db.execute(
                    "SELECT COUNT(*) FROM prediction_contracts "
                    "WHERE market_id = ? AND status = 'open'",
                    (market_id,),
                ) as cursor:
                    holders = (await cursor.fetchone())[0]

                # Log engagement
                await db.execute(
                    "INSERT INTO market_engagement (market_id, event_type, source, created_at) "
                    "VALUES (?, 'alert_fired', 'price_alert', ?)",
                    (market_id, now.isoformat()),
                )
                await db.commit()

                # Post alert
                channel = self._channel()
                if channel:
                    try:
                        from casino.renderer.prediction_html_renderer import render_price_alert_card
                        png = await render_price_alert_card(
                            market={
                                "title": title, "category": category,
                                "yes_price": current_price, "no_price": no_price,
                            },
                            old_price=old_price,
                            new_price=current_price,
                            holders=holders,
                        )
                        direction = "up" if current_price > old_price else "down"
                        embed = discord.Embed(
                            title=f"Price Alert {'📈' if direction == 'up' else '📉'}",
                            color=0x4ADE80 if direction == "up" else 0xF87171,
                        )
                        card_file = discord.File(io.BytesIO(png), filename="price_alert.png")
                        embed.set_image(url="attachment://price_alert.png")

                        # Add bet button
                        view = discord.ui.View(timeout=3600)
                        bet_btn = discord.ui.Button(
                            label="Bet Now",
                            style=discord.ButtonStyle.primary,
                            custom_id=f"alert_bet_{market_id}",
                        )

                        async def _alert_bet_cb(interaction: discord.Interaction, mid=market_id):
                            # Log engagement
                            async with aiosqlite.connect(DB_PATH) as db2:
                                await db2.execute(
                                    "INSERT INTO market_engagement (market_id, event_type, user_id, source, created_at) "
                                    "VALUES (?, 'alert_click', ?, 'price_alert', ?)",
                                    (mid, str(interaction.user.id), datetime.now(timezone.utc).isoformat()),
                                )
                                await db2.commit()
                            # Show detail card
                            await self.select_market_detail(interaction, mid)

                        bet_btn.callback = _alert_bet_cb
                        view.add_item(bet_btn)

                        await channel.send(embed=embed, file=card_file, view=view)
                        alerts_this_hour += 1
                    except Exception as e:
                        log.warning(f"Price alert render/post failed: {e}")

        self._alerts_this_hour = alerts_this_hour

    async def select_market_detail(self, interaction: discord.Interaction, market_id: str):
        """Show a market detail card with bet buttons (used by alerts and drilldowns)."""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT market_id, slug, title, category, yes_price, no_price, "
                "volume, end_date, liquidity "
                "FROM prediction_markets WHERE market_id = ?",
                (market_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.followup.send("Market not found.", ephemeral=True)
            return

        m = {
            "market_id": row[0], "slug": row[1], "title": row[2],
            "category": row[3], "yes_price": row[4] or 0.5,
            "no_price": row[5] or 0.5, "volume": row[6] or 0,
            "end_date": row[7] or "", "liquidity": row[8] or 0,
        }

        # Log engagement
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO market_engagement (market_id, event_type, user_id, source, created_at) "
                "VALUES (?, 'view', ?, 'markets_cmd', ?)",
                (market_id, str(interaction.user.id), datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

        try:
            png = await render_market_detail_card(
                title=m["title"], category=m["category"],
                yes_price=m["yes_price"], no_price=m["no_price"],
                volume=m["volume"], liquidity=m["liquidity"],
                end_date=m["end_date"],
            )
            card_file = discord.File(io.BytesIO(png), filename="market_detail.png")
            embed = discord.Embed(color=0x3498DB)
            embed.set_image(url="attachment://market_detail.png")
        except Exception:
            embed = discord.Embed(title=m["title"][:80], color=0x3498DB)
            embed.add_field(name="YES", value=f"{m['yes_price']:.0%}", inline=True)
            embed.add_field(name="NO", value=f"{m['no_price']:.0%}", inline=True)
            card_file = None

        view = BetButtonView(
            market_id=m["market_id"], slug=m["slug"], title=m["title"],
            yes_price=m["yes_price"], no_price=m["no_price"], cog=self,
        )

        kwargs = {"embed": embed, "view": view, "ephemeral": True}
        if card_file:
            kwargs["file"] = card_file
        await interaction.followup.send(**kwargs)

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

    async def _local_settle_pass(self):
        """
        Scan the local DB for markets with open contracts that the API-driven
        _auto_resolve_pass may have missed (e.g. markets that closed >100
        closures ago and fell off the Polymarket top-100 closed endpoint).

        For each, fetch the market individually by ID and check if resolved.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT c.market_id "
                "FROM prediction_contracts c "
                "JOIN prediction_markets m ON m.market_id = c.market_id "
                "WHERE c.status = 'open' AND m.resolved_by = 'pending'"
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return

        market_ids = [row[0] for row in rows]
        log.info(
            f"Local settle pass: {len(market_ids)} market(s) with open contracts "
            f"still pending resolution."
        )

        auto_resolved = []

        for market_id in market_ids:
            mkt = await self.client.fetch_market_by_id(market_id)
            if not mkt:
                log.warning(f"Local settle: could not fetch market {market_id}")
                continue

            result = detect_result(mkt)
            if not result:
                continue  # Market still open or not clearly resolved

            # Settle it
            result_upper = result.upper()
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM prediction_contracts "
                    "WHERE market_id = ? AND status = 'open'",
                    (market_id,)
                ) as cursor:
                    open_count = (await cursor.fetchone())[0]

            if open_count == 0:
                continue

            log.info(
                f"Local settle: auto-resolving {market_id} → {result_upper} "
                f"({open_count} open contracts)"
            )

            counts = await self._resolve(market_id, result_upper)

            # Mark market as auto-resolved
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE prediction_markets "
                    "SET resolved_by = 'auto', result = ?, status = 'closed' "
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
                f"Local settle pass complete — {len(auto_resolved)} market(s) settled."
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

            # Build winner list for V6 card
            winner_dicts = []
            if winners:
                for uid, qty, payout, cost in winners:
                    profit = payout - cost
                    member = ch.guild.get_member(int(uid))
                    name = member.display_name if member else f"User {uid}"
                    winner_dicts.append({
                        "name": name,
                        "qty": qty,
                        "payout": payout,
                        "profit": profit,
                    })

            # Try V6 card render
            try:
                png = await render_resolution_card(
                    market_title=title,
                    result=result,
                    winners=winner_dicts,
                    total_won=won,
                    total_lost=lost,
                    total_voided=voided,
                )
                card_file = discord.File(io.BytesIO(png), filename="resolution.png")
                embed.set_image(url="attachment://resolution.png")
                embed.set_footer(text="Winnings automatically credited · FLOW Markets")
                embed.timestamp = datetime.now(timezone.utc)
                await ch.send(embed=embed, file=card_file)
            except Exception as e:
                log.error(f"Resolution card render failed: {e}")
                # Text fallback
                if winner_dicts:
                    lines = []
                    for w in winner_dicts:
                        lines.append(
                            f"💰 **{w['name']}** — {w['qty']} contract(s) · "
                            f"Payout: **{w['payout']:,}** · Profit: **+{w['profit']:,}**"
                        )
                    embed.add_field(
                        name="🏅 Winners",
                        value="\n".join(lines),
                        inline=False,
                    )
                embed.set_footer(text="Winnings automatically credited · FLOW Markets")
                embed.timestamp = datetime.now(timezone.utc)
                try:
                    await ch.send(embed=embed)
                except Exception as e2:
                    log.error(f"Failed to post resolution announcement: {e2}")

    @sync_markets.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()

    # ── Daily Drop Task ─────────────────────────

    @tasks.loop(time=datetime(2000, 1, 1, 14, 0).time())  # 9 AM EST = 14:00 UTC
    async def daily_drop_task(self):
        """Generate and post the daily curated market drop."""
        await self._ensure_db()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Check if already posted today
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT drop_id FROM daily_drops WHERE drop_date = ?", (today,)
            ) as cursor:
                if await cursor.fetchone():
                    return  # Already posted

        log.info("Generating Daily Drop...")
        try:
            await self._generate_daily_drop(today)
        except Exception as e:
            log.error(f"Daily Drop generation failed: {e}")

    @daily_drop_task.before_loop
    async def _before_daily_drop(self):
        await self.bot.wait_until_ready()

    async def _generate_daily_drop(self, today: str):
        """Build shortlist, call Gemini, render card, post to channel."""
        # Step 1: Build shortlist — top 30 by curation score, max 3 per category
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT pm.market_id, pm.slug, pm.title, pm.category,
                       pm.yes_price, pm.no_price, pm.volume, pm.end_date,
                       pm.liquidity, cs.score
                FROM curated_scores cs
                JOIN prediction_markets pm ON pm.market_id = cs.market_id
                WHERE pm.status = 'active'
                ORDER BY cs.score DESC
                LIMIT 100
            """) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            log.warning("No curated markets available for Daily Drop.")
            return

        # Enforce max 3 per category
        shortlist = []
        cat_counts: dict[str, int] = {}
        for r in rows:
            cat = r[3] or "Other"
            if cat_counts.get(cat, 0) >= 3:
                continue
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            shortlist.append({
                "market_id": r[0], "slug": r[1], "title": r[2],
                "category": r[3], "yes_price": r[4] or 0.5,
                "no_price": r[5] or 0.5, "volume": r[6] or 0,
                "end_date": r[7] or "", "liquidity": r[8] or 0,
                "score": r[9],
            })
            if len(shortlist) >= 30:
                break

        if len(shortlist) < 5:
            log.warning(f"Only {len(shortlist)} markets in shortlist — need at least 5.")
            return

        # Step 2: Gemini editorial pass
        spotlight = None
        supporting = []

        gemini = _get_gemini_client()
        if gemini:
            try:
                spotlight, supporting = await self._gemini_curate(shortlist)
            except Exception as e:
                log.warning(f"Gemini curation failed: {e}")
                # Retry once
                await asyncio.sleep(30)
                try:
                    spotlight, supporting = await self._gemini_curate(shortlist)
                except Exception as e2:
                    log.error(f"Gemini curation retry failed: {e2}")

        # Fallback: use top 5 by score without editorial text
        if not spotlight:
            spotlight = shortlist[0]
            spotlight["analysis"] = ""
            supporting = [
                {**m, "hook": ""} for m in shortlist[1:5]
            ]

        # Step 3: Community momentum
        community_data = {}
        async with aiosqlite.connect(DB_PATH) as db:
            all_market_ids = [spotlight["market_id"]] + [s["market_id"] for s in supporting]
            for mid in all_market_ids:
                community_data[mid] = await self._get_community_sentiment(mid, db)

        # Step 4: Leaderboard
        leaderboard = await self._get_prediction_leaderboard()

        # Step 5: Render card
        try:
            png = await render_daily_drop_card(
                spotlight=spotlight,
                supporting=supporting,
                community=community_data,
                leaderboard=leaderboard,
            )
        except Exception as e:
            log.error(f"Daily Drop card render failed: {e}")
            return

        # Step 6: Post to channel
        channel = self._channel()
        if not channel:
            log.warning("Prediction channel not found — cannot post Daily Drop.")
            return

        embed = discord.Embed(
            title="🔥 Daily Drop — Today's Prediction Markets",
            color=AtlasColors.TSL_GOLD,
        )
        card_file = discord.File(io.BytesIO(png), filename="daily_drop.png")
        embed.set_image(url="attachment://daily_drop.png")
        embed.set_footer(text="Use /markets to browse all markets · FLOW Markets")
        embed.timestamp = datetime.now(timezone.utc)

        # Select menu for the 5 featured markets
        all_featured = [spotlight] + supporting
        options = []
        for m in all_featured[:5]:
            parts = m.get("category", "Other").split(" ", 1)
            emoji = parts[0] if len(parts) > 1 else "📊"
            options.append(discord.SelectOption(
                label=m.get("title", "")[:95],
                value=m.get("market_id", ""),
                description=f"YES {m.get('yes_price', 0.5):.0%}",
                emoji=emoji,
            ))

        view = discord.ui.View(timeout=None)
        select = discord.ui.Select(
            placeholder="Select a market to bet...",
            options=options if options else [discord.SelectOption(label="None", value="none")],
        )

        async def _drop_select_cb(interaction: discord.Interaction):
            mid = select.values[0]
            if mid == "none":
                await interaction.response.defer()
                return
            # Log engagement
            try:
                async with aiosqlite.connect(DB_PATH) as db2:
                    await db2.execute(
                        "INSERT INTO market_engagement (market_id, event_type, user_id, source, created_at) "
                        "VALUES (?, 'view', ?, 'daily_drop', ?)",
                        (mid, str(interaction.user.id), datetime.now(timezone.utc).isoformat()),
                    )
                    await db2.commit()
            except Exception:
                pass
            await self.select_market_detail(interaction, mid)

        select.callback = _drop_select_cb
        view.add_item(select)

        msg = await channel.send(embed=embed, file=card_file, view=view)

        # Step 7: Store the selection
        async with aiosqlite.connect(DB_PATH) as db:
            supporting_json = json.dumps([
                {"market_id": s["market_id"], "hook": s.get("hook", "")}
                for s in supporting
            ])
            await db.execute("""
                INSERT INTO daily_drops
                    (drop_date, spotlight_market_id, spotlight_analysis,
                     supporting, community_data, leaderboard_data,
                     posted_at, message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today, spotlight["market_id"],
                spotlight.get("analysis", ""),
                supporting_json,
                json.dumps(community_data),
                json.dumps(leaderboard),
                datetime.now(timezone.utc).isoformat(),
                str(msg.id),
            ))
            await db.commit()

        log.info(f"Daily Drop posted: spotlight={spotlight['title'][:50]}")

    async def _gemini_curate(self, shortlist: list[dict]) -> tuple[dict, list[dict]]:
        """Use Gemini to select spotlight + 4 supporting markets from shortlist."""
        gemini = _get_gemini_client()
        if not gemini:
            raise RuntimeError("Gemini client unavailable")

        # Build market list for prompt
        market_lines = []
        for i, m in enumerate(shortlist):
            market_lines.append(json.dumps({
                "index": i,
                "market_id": m["market_id"],
                "title": m["title"],
                "category": m["category"],
                "yes_price": round(m["yes_price"], 2),
                "no_price": round(m["no_price"], 2),
                "volume": m.get("volume", 0),
            }))

        # Get persona for system instruction
        try:
            from echo_loader import get_persona
            system_instruction = get_persona("analytical")
        except ImportError:
            system_instruction = "You are ATLAS."

        prompt = (
            f"From these {len(shortlist)} prediction markets, select:\n"
            f"1. ONE \"Market of the Day\" — the most interesting, debatable, culturally relevant.\n"
            f"   Write a 2-3 sentence spotlight analysis in ATLAS voice (3rd person, punchy, cites numbers).\n"
            f"2. FOUR supporting markets across different categories.\n"
            f"   For each, write a 1-line hook that makes someone want to bet.\n\n"
            f"Rules:\n"
            f"- Never pick markets >85% in either direction (basically decided)\n"
            f"- Maximize category diversity across all 5 picks\n"
            f"- Prioritize genuine uncertainty, cultural relevance, debate-worthy topics\n"
            f"- Avoid repetitive topics (multiple markets about the same person/event)\n\n"
            f"Markets:\n" + "\n".join(market_lines) + "\n\n"
            f"Respond as JSON:\n"
            f'{{"spotlight": {{"market_id": "...", "analysis": "..."}}, '
            f'"supporting": [{{"market_id": "...", "hook": "..."}}, ...]}}'
        )

        loop = asyncio.get_running_loop()

        def _call():
            return gemini.models.generate_content(
                model="gemini-2.0-flash",
                contents=[prompt],
                config={"system_instruction": system_instruction},
            )

        response = await loop.run_in_executor(None, _call)
        text = response.text.strip()

        # Parse JSON
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if not json_match:
            raise ValueError("Gemini returned non-JSON for curation")

        result = json.loads(json_match.group())
        sp_data = result.get("spotlight", {})
        sup_data = result.get("supporting", [])

        # Map back to full market dicts
        market_map = {m["market_id"]: m for m in shortlist}

        sp_id = sp_data.get("market_id", "")
        spotlight = market_map.get(sp_id, shortlist[0]).copy()
        spotlight["analysis"] = sp_data.get("analysis", "")

        supporting = []
        for s in sup_data[:4]:
            s_id = s.get("market_id", "")
            if s_id in market_map:
                m = market_map[s_id].copy()
                m["hook"] = s.get("hook", "")
                supporting.append(m)

        # Fill to 4 if Gemini didn't return enough
        while len(supporting) < 4 and len(shortlist) > len(supporting) + 1:
            for m in shortlist:
                if m["market_id"] != spotlight["market_id"] and m["market_id"] not in {s["market_id"] for s in supporting}:
                    mc = m.copy()
                    mc["hook"] = ""
                    supporting.append(mc)
                    break
            else:
                break

        return spotlight, supporting

    async def _get_prediction_leaderboard(self) -> list[dict]:
        """Get top prediction traders by weekly profit + streaks."""
        week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            # Weekly profit
            async with db.execute("""
                SELECT user_id,
                       SUM(CASE WHEN status = 'won' THEN potential_payout ELSE 0 END) -
                       SUM(CASE WHEN status IN ('won', 'lost') THEN cost_bucks ELSE 0 END) as profit
                FROM prediction_contracts
                WHERE resolved_at > ?
                  AND status IN ('won', 'lost')
                GROUP BY user_id
                HAVING profit > 0
                ORDER BY profit DESC
                LIMIT 5
            """, (week_start,)) as cursor:
                profit_rows = await cursor.fetchall()

            leaderboard = []
            for user_id, profit in profit_rows:
                # Calculate streak
                async with db.execute("""
                    SELECT status FROM prediction_contracts
                    WHERE user_id = ? AND status IN ('won', 'lost')
                    ORDER BY resolved_at DESC
                    LIMIT 20
                """, (user_id,)) as cursor:
                    statuses = [r[0] for r in await cursor.fetchall()]

                streak = 0
                for s in statuses:
                    if s == "won":
                        streak += 1
                    elif s == "lost":
                        break
                    # voided: skip (doesn't break or extend)

                # Resolve display name
                guild = self.bot.guilds[0] if self.bot.guilds else None
                name = str(user_id)
                if guild:
                    member = guild.get_member(int(user_id))
                    if member:
                        name = member.display_name

                leaderboard.append({
                    "name": name,
                    "profit": int(profit),
                    "streak": streak,
                })

            return leaderboard

    # ── Utility ─────────────────────────────────

    def _channel(self):
        return self.bot.get_channel(PREDICTION_CHANNEL_ID)

    # ── Slash: /markets ───────────────────────

    @app_commands.command(
        name="markets",
        description="Browse curated prediction markets."
    )
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    @app_commands.describe(
        view="How to sort markets (default: curated)",
        category="Filter by category",
    )
    @app_commands.choices(view=[
        app_commands.Choice(name="Curated (default)", value="curated"),
        app_commands.Choice(name="Trending", value="trending"),
        app_commands.Choice(name="Popular", value="popular"),
        app_commands.Choice(name="New", value="new"),
    ])
    async def markets_cmd(
        self,
        interaction: discord.Interaction,
        view: str = "curated",
        category: str | None = None,
    ):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        await self._ensure_db()

        markets = await self._get_curated_selection(
            count=MARKETS_PER_PAGE,
            category=category,
            view_mode=view,
        )

        if not markets:
            await interaction.followup.send(
                "⚠️ No markets synced yet. Try again in a moment.",
                ephemeral=True,
            )
            return

        # Category counts for the select menu
        category_counts: dict[str, int] = {}
        async with aiosqlite.connect(DB_PATH) as db:
            blocked_ph = ",".join("?" for _ in BLOCKED_CATEGORIES)
            async with db.execute(f"""
                SELECT category, COUNT(*) FROM prediction_markets
                WHERE status = 'active' AND category NOT IN ({blocked_ph})
                GROUP BY category
            """, tuple(BLOCKED_CATEGORIES)) as cursor:
                for cat, cnt in await cursor.fetchall():
                    category_counts[cat] = cnt

        categories = sorted(category_counts.keys())
        browser = CuratedBrowserView(
            markets=markets,
            categories=categories,
            category_counts=category_counts,
            cog=self,
            view_mode=view,
            filter_category=category,
        )

        embed, card_file = await browser._build_page()
        kwargs = {"embed": embed, "view": browser, "ephemeral": True}
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

        color = CATEGORY_COLORS.get(category, AtlasColors.TSL_GOLD.value)
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

        # Build position dicts for V6 card
        positions = []
        total_invested = 0
        total_potential = 0
        for mid, title, side, buy_price, qty, cost, payout, status, created in rows:
            if status != "open":
                continue
            positions.append({
                "title": title or mid,
                "side": side,
                "qty": qty,
                "cost": cost,
                "payout": payout,
                "buy_price": buy_price,
            })
            total_invested += cost
            total_potential += payout

        if not positions:
            await interaction.followup.send(
                "You have no open prediction market positions.",
                ephemeral=True,
            )
            return

        # Get user balance
        balance = 0
        try:
            from flow_wallet import get_balance
            balance = await get_balance(user_id)
        except Exception:
            pass

        try:
            png = await render_portfolio_card(
                positions=positions,
                player_name=interaction.user.display_name,
                total_invested=total_invested,
                total_potential=total_potential,
                balance=balance,
            )
            card_file = discord.File(io.BytesIO(png), filename="portfolio.png")
            embed = discord.Embed(color=0x3498DB)
            embed.set_image(url="attachment://portfolio.png")
            await interaction.followup.send(embed=embed, file=card_file, ephemeral=True)
        except Exception as e:
            log.error(f"Portfolio card render failed: {e}")
            # Text fallback
            embed = discord.Embed(
                title="📋 Your Prediction Market Portfolio",
                color=0x3498DB,
            )
            for pos in positions:
                sym = "✅" if pos["side"] == "YES" else "❌"
                embed.add_field(
                    name=f"{sym} {pos['title'][:50]}",
                    value=(
                        f"**{pos['side']}** · {pos['qty']} contract(s)\n"
                        f"Paid: **{pos['cost']:,}** · "
                        f"Potential: **{pos['payout']:,}**"
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
        await self._resolve_market_impl(interaction, slug, result)

    async def _resolve_market_impl(self, interaction: discord.Interaction,
                                   slug: str, result: str):
        """Delegation target for boss_cog / commish_cog. Expects deferred interaction."""
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
        total_payout = 0

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")

            # Guard: prevent double-resolution (TOCTOU between status check and here)
            async with db.execute(
                "SELECT title, resolved_by FROM prediction_markets WHERE market_id = ?",
                (market_id,)
            ) as cur:
                row = await cur.fetchone()
            if row and row[1] and row[1] != 'pending':
                log.warning(f"_resolve({market_id}) — already resolved by {row[1]}, skipping")
                return counts
            market_title = row[0] if row else market_id

            async with db.execute(
                "SELECT id, user_id, side, quantity, cost_bucks, potential_payout "
                "FROM prediction_contracts "
                "WHERE market_id = ? AND status = 'open'",
                (market_id,)
            ) as cursor:
                contracts = await cursor.fetchall()

            for cid, user_id, side, qty, cost, payout in contracts:
                if result == "VOID":
                    await flow_wallet.credit(
                        int(user_id), cost, "PREDICTION",
                        description=f"prediction market voided",
                        con=db,
                    )
                    await db.execute(
                        "UPDATE prediction_contracts "
                        "SET status='voided', resolved_at=? WHERE id=?",
                        (now, cid)
                    )
                    counts["voided"] += 1
                elif side == result:
                    if payout > PREDICTION_MAX_PAYOUT:
                        log.error(f"[PREDICTION] Insane payout ${payout:,.2f} for contract {cid} — capping to ${PREDICTION_MAX_PAYOUT:,.2f}")
                        payout = PREDICTION_MAX_PAYOUT
                    await flow_wallet.credit(
                        int(user_id), payout, "PREDICTION",
                        description=f"prediction market won",
                        con=db,
                    )
                    await db.execute(
                        "UPDATE prediction_contracts "
                        "SET status='won', resolved_at=? WHERE id=?",
                        (now, cid)
                    )
                    counts["won"] += 1
                    total_payout += payout
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
                "SET status='closed', resolved_by=?, result=? WHERE market_id=?",
                (resolved_by, result, market_id)
            )
            await db.commit()

        log.info(f"_resolve({market_id}, {result}, by={resolved_by}): {counts}")

        guild = self.bot.guilds[0] if self.bot.guilds else None
        guild_id = guild.id if guild else None

        # Post ledger slips for resolution payouts/refunds
        try:
            from ledger_poster import post_transaction
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

        # Emit FLOW event for live engagement system
        try:
            from flow_events import PredictionEvent, flow_bus
            pred_event = PredictionEvent(
                guild_id=guild_id,
                market_title=market_title,
                resolution=result,
                total_payout=total_payout,
                winners=counts["won"],
            )
            await flow_bus.emit("prediction_result", pred_event)
        except Exception:
            log.exception("Failed to emit prediction FLOW event")

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
        await self._market_status_impl(interaction)

    async def _market_status_impl(self, interaction: discord.Interaction):
        """Delegation target for boss_cog / commish_cog. Expects deferred interaction."""
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


    # ── Impl: approve_market (called by commish_cog / boss_cog) ────

    async def _approve_market_impl(self, interaction: discord.Interaction, slug: str):
        """Mark a market as featured/approved for betting. Expects deferred interaction."""
        await self._ensure_db()
        slug = slug.strip().lower()

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT market_id, title, featured FROM prediction_markets WHERE slug = ?",
                (slug,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await interaction.followup.send(
                    f"❌ Market `{slug}` not found.", ephemeral=True
                )
                return

            market_id, title, already_featured = row
            if already_featured:
                await interaction.followup.send(
                    f"Market `{slug}` is already approved.", ephemeral=True
                )
                return

            await db.execute(
                "UPDATE prediction_markets SET featured = 1 WHERE market_id = ?",
                (market_id,)
            )
            await db.commit()

        await interaction.followup.send(
            f"✅ Approved **{title or slug}** for featured betting.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PolymarketCog(bot))
    print("ATLAS: Flow · Polymarket Prediction Markets loaded.")
