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
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
import os

import atlas_ai
from atlas_ai import Tier

import flow_wallet
from format_utils import fmt_volume
from flow_wallet import (
    DB_PATH,
    InsufficientFundsError,
    get_theme_for_render,
)
from atlas_send import send_card, send_card_to_channel
from casino.renderer.prediction_html_renderer import (
    render_market_list_card,
    render_market_detail_card,
    render_bet_confirmation_card,
    render_portfolio_card,
    render_resolution_card,
    render_curated_list_card,
    render_daily_drop_card,
    render_price_alert_card,
    render_sell_confirmation_card,
    render_position_detail_card,
)
import io

log = logging.getLogger("polymarket_cog")

PREDICTION_MAX_PAYOUT = 10_000_000  # sanity cap — matches sportsbook MAX_PAYOUT



# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

POLYMARKET_GAMMA_BASE = "https://gamma-api.polymarket.com"

# Channels
PREDICTION_CHANNEL_ID = int(os.getenv("PREDICTION_MARKET_CHANNEL_ID", "0"))
if not PREDICTION_CHANNEL_ID:
    print("[polymarket] WARNING: PREDICTION_MARKET_CHANNEL_ID not set — prediction posts disabled")

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

# ── Six-Layer Garbage Filter Constants ──────────────────────────────
BLOCKED_TAG_SLUGS = {
    # Crypto (degen noise)
    "crypto", "crypto-prices", "up-or-down", "bitcoin", "ethereum",
    "solana", "xrp", "ripple", "5m", "recurring",
    "hide-from-new",  # Polymarket itself hides these
    "defi", "nft", "memecoin", "altcoin",
    # Sports (use /sportsbook instead)
    "sports", "nba", "nfl", "mlb", "nhl", "soccer", "mma", "ufc",
    "boxing", "chess", "esports", "gaming", "epl", "premier-league",
    "motorsports", "nascar", "f1",
}

GARBAGE_SLUG_PATTERNS = [
    re.compile(r"btc-?up-?down"),
    re.compile(r"eth-?up-?down"),
    re.compile(r"sol-?up-?down"),
    re.compile(r"xrp-?up-?down"),
    re.compile(r"doge-?up-?down"),
    re.compile(r"-up-?down-\d+[mh]-"),   # any crypto up/down with timestamp
    re.compile(r"^crypto-"),
    re.compile(r"bitcoin-price-"),
    re.compile(r"ethereum-price-"),
    re.compile(r"solana-price-"),
]

TITLE_KILL_PATTERNS = [
    re.compile(r"Up or Down", re.IGNORECASE),
    re.compile(r"tweet.*elon|elon.*tweet", re.IGNORECASE),
    re.compile(r"\bX post\b", re.IGNORECASE),
    re.compile(r"Truth Social", re.IGNORECASE),
    re.compile(r"Instagram.*follow", re.IGNORECASE),
    re.compile(r"subscriber.*count", re.IGNORECASE),
    re.compile(r"token.*launch", re.IGNORECASE),
    re.compile(r"airdrop", re.IGNORECASE),
    re.compile(r"memecoin", re.IGNORECASE),
    re.compile(r"gas fee", re.IGNORECASE),
    re.compile(r"market\s*cap.*\$", re.IGNORECASE),
    re.compile(r"price.*(?:above|below|over|under).*\$\d+", re.IGNORECASE),
]

BLOCKED_SERIES_SLUGS = {
    "btc-up-or-down-5m", "btc-up-or-down-1h",
    "eth-up-or-down-5m", "eth-up-or-down-1h",
    "sol-up-or-down-5m", "sol-up-or-down-1h",
    "xrp-up-or-down-5m", "xrp-up-or-down-1h",
    "doge-up-or-down-5m", "doge-up-or-down-1h",
}

MIN_VOLUME = 1_000       # Total volume < $1K = nobody's trading
MIN_LIQUIDITY = 500      # Liquidity < $500 = meaningless prices

# ── Audience Affinity for TSL demographic (18-35 male, sports-adjacent) ─
CATEGORY_AFFINITY = {
    # Tier 1 — TSL audience loves these (1.5x → 20 pts max)
    "🗳️ Elections": 1.5,
    "🌟 Pop Culture": 1.5,
    "🤖 AI": 1.5,
    "🎬 Entertainment": 1.4,
    # Tier 2 — Neutral (1.0x → ~13 pts)
    "🏛️ Government": 1.0,
    "📈 Economics": 1.0,
    "💻 Tech": 1.0,
    "🌍 World": 0.9,
    # Tier 3 — Low engagement (0.5x → ~7 pts)
    "🔬 Science": 0.5,
    "🌐 Other": 0.4,
}


# ── Six-Layer Garbage Filter ────────────────────────────────────────
def _is_garbage_market(event: dict, mkt: dict) -> bool:
    """Return True if market is spam/noise that should never be stored.

    Six layers, short-circuit on first match:
      1. Tag blocklist        4. Volume/liquidity thresholds
      2. Slug pattern block   5. Title/question pattern block
      3. Restricted flag      6. Series slug blocklist
    """
    # Layer 1 — Tag blocklist
    for tag in event.get("tags", []):
        if isinstance(tag, dict) and tag.get("slug", "").lower() in BLOCKED_TAG_SLUGS:
            return True

    # Layer 2 — Slug pattern blocklist
    slug = mkt.get("slug", "")
    for pat in GARBAGE_SLUG_PATTERNS:
        if pat.search(slug):
            return True

    # Layer 3 — Restricted flag
    if mkt.get("restricted") or event.get("restricted"):
        return True

    # Layer 4 — Minimum thresholds
    try:
        volume = float(mkt.get("volumeNum") or mkt.get("volume") or 0)
    except (ValueError, TypeError):
        volume = 0
    try:
        liquidity = float(mkt.get("liquidityNum") or mkt.get("liquidity") or 0)
    except (ValueError, TypeError):
        liquidity = 0

    if volume < MIN_VOLUME or liquidity < MIN_LIQUIDITY:
        return True
    if mkt.get("enableOrderBook") is False:
        return True

    # Layer 5 — Title/question pattern blocklist
    title = mkt.get("question", "") or mkt.get("title", "")
    for pat in TITLE_KILL_PATTERNS:
        if pat.search(title):
            return True

    # Layer 6 — Series slug blocklist
    series_slug = event.get("seriesSlug", "")
    if series_slug and series_slug.lower() in BLOCKED_SERIES_SLUGS:
        return True

    return False


def _compute_curation_score(
    market: dict,
    days_in_pool: float,
    same_category_count: int,
    internal_bet_users: int = 0,
    recently_featured: bool = False,
) -> tuple[float, dict]:
    """Compute 0-100+ curation score for a market.

    Returns (score, breakdown_dict).
    Signals (base 100):
      - tension       (28%): sharp bell curve, near-zero past 75/25
      - audience_fit  (20%): category affinity for TSL demographic
      - urgency       (18%): deadline FOMO, peak at 0-3 days
      - freshness     (12%): 5-day decay (not 20)
      - velocity      (10%): log10 of 24hr volume (tiebreaker)
      - liquidity     ( 7%): price accuracy signal
      - diversity     ( 5%): penalize over-represented categories
    Bonuses/penalties:
      - bet_boost    (+8 max): TSL users already betting on this
      - staleness    (-15):    appeared in daily drop last 2 days
    """
    vol_24h = market.get("volume_24hr", 0) or 0
    yes_p = market.get("yes_price", 0.5) or 0.5
    liquidity = market.get("liquidity", 0) or 0
    category = market.get("category", "🌐 Other")

    # ── Tension (0-28): sharp bell curve, peaks at 50/50 ──
    deviation = abs(yes_p - 0.5)
    if deviation >= 0.3:
        tension = 0.0
    else:
        tension = 28 * (1 - (deviation / 0.3) ** 1.5)

    # ── Audience Fit (0-20): category affinity for TSL demo ──
    affinity_mult = CATEGORY_AFFINITY.get(category, 0.5)
    audience_fit = min(20, 13.3 * affinity_mult)

    # ── Urgency (0-18): deadline FOMO, peak at 0-3 days ──
    end_date = market.get("end_date", "")
    urgency = 0.0
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            days_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
            if days_left < 0:
                urgency = 0
            elif days_left <= 3:
                urgency = 18                                    # resolves soon — max
            elif days_left <= 7:
                urgency = 18 * (1 - (days_left - 3) / 8)       # decay 18 → ~9
            elif days_left <= 30:
                urgency = 9 * (1 - (days_left - 7) / 23)       # slow decay → ~0
            # >30 days → 0
        except (ValueError, TypeError):
            urgency = 5  # fallback: some urgency

    # ── Freshness (0-12): 5-day full decay ──
    freshness = max(0, 12 - days_in_pool * 2.4)

    # ── Velocity (0-10): log10 of 24hr volume — tiebreaker ──
    velocity = min(math.log10(max(vol_24h, 1)) / 7.0, 1.0) * 10

    # ── Liquidity (0-7): price accuracy signal ──
    liq_score = min(liquidity / 100_000, 1.0) * 7

    # ── Diversity (0-5): penalize over-represented categories ──
    diversity = max(0, 5 - same_category_count)

    # ── Internal Bet Velocity Boost (+0 to +8) ──
    bet_boost = min(8, internal_bet_users * 2.5)

    # ── Staleness Penalty (-15) ──
    staleness_penalty = 15 if recently_featured else 0

    score = (tension + audience_fit + urgency + freshness
             + velocity + liq_score + diversity
             + bet_boost - staleness_penalty)
    score = max(0, score)

    breakdown = {
        "tension": round(tension, 1),
        "audience_fit": round(audience_fit, 1),
        "urgency": round(urgency, 1),
        "freshness": round(freshness, 1),
        "velocity": round(velocity, 1),
        "liquidity": round(liq_score, 1),
        "diversity": round(diversity, 1),
        "bet_boost": round(bet_boost, 1),
        "staleness": round(-staleness_penalty, 1),
    }
    return round(score, 2), breakdown


# ── Pre-fetch helpers for curation scoring ──────────────────────────
async def _get_internal_bet_counts(db) -> dict[str, int]:
    """Return {market_id: distinct_user_count} for markets with open bets."""
    async with db.execute("""
        SELECT market_id, COUNT(DISTINCT user_id)
        FROM prediction_contracts
        WHERE status = 'open'
        GROUP BY market_id
    """) as cursor:
        return {r[0]: r[1] for r in await cursor.fetchall()}


async def _get_recently_featured_ids(db, days: int = 2) -> set[str]:
    """Return market_ids that appeared in daily drops within the last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    featured: set[str] = set()
    async with db.execute(
        "SELECT spotlight_market_id, supporting FROM daily_drops WHERE drop_date >= ?",
        (cutoff,),
    ) as cursor:
        async for row in cursor:
            if row[0]:
                featured.add(row[0])
            if row[1]:
                try:
                    for s in json.loads(row[1]):
                        if isinstance(s, dict) and "market_id" in s:
                            featured.add(s["market_id"])
                except (json.JSONDecodeError, TypeError):
                    pass
    return featured


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

        # Migration v2: expand CHECK constraint to include 'sold' status + sell columns
        await _migrate_contracts_sold_status(db)

    log.info("Prediction market DB tables ready.")


async def _migrate_contracts_sold_status(db):
    """Add 'sold' to the status CHECK constraint and add sell columns.

    SQLite doesn't support ALTER CHECK, so we recreate the table if needed.
    """
    # Quick probe: can we insert 'sold' status?
    try:
        await db.execute("SAVEPOINT sold_probe")
        await db.execute(
            "INSERT INTO prediction_contracts "
            "(user_id, market_id, slug, side, buy_price, quantity, cost_bucks, "
            "potential_payout, status, created_at) "
            "VALUES ('__probe__', '__probe__', '__probe__', 'YES', 0, 0, 0, 0, 'sold', '')"
        )
        await db.execute(
            "DELETE FROM prediction_contracts WHERE user_id = '__probe__'"
        )
        await db.execute("RELEASE sold_probe")
        # Probe succeeded — table already has 'sold' in CHECK.
        # Still ensure sell columns exist.
        for col_def in [
            "sell_price REAL",
            "sell_bucks INTEGER",
            "sold_at TEXT",
        ]:
            col_name = col_def.split()[0]
            try:
                await db.execute(
                    f"ALTER TABLE prediction_contracts ADD COLUMN {col_def}"
                )
            except Exception:
                pass
        return
    except Exception:
        await db.execute("ROLLBACK TO sold_probe")
        await db.execute("RELEASE sold_probe")

    # CHECK constraint blocks 'sold' — recreate table
    log.info("Migrating prediction_contracts to support 'sold' status…")
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS prediction_contracts_v2 (
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
                            CHECK(status IN ('open','won','lost','voided','sold')),
            created_at      TEXT NOT NULL,
            resolved_at     TEXT,
            sell_price      REAL,
            sell_bucks      INTEGER,
            sold_at         TEXT,
            FOREIGN KEY (market_id) REFERENCES prediction_markets(market_id)
        );

        INSERT OR IGNORE INTO prediction_contracts_v2
            (id, user_id, market_id, slug, side, buy_price, quantity,
             cost_bucks, potential_payout, status, created_at, resolved_at)
        SELECT id, user_id, market_id, slug, side, buy_price, quantity,
               cost_bucks, potential_payout, status, created_at, resolved_at
        FROM prediction_contracts;

        DROP TABLE prediction_contracts;
        ALTER TABLE prediction_contracts_v2 RENAME TO prediction_contracts;

        CREATE INDEX IF NOT EXISTS idx_pred_contracts_user
            ON prediction_contracts(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_pred_contracts_market
            ON prediction_contracts(market_id, status);
    """)
    log.info("prediction_contracts migrated to v2 (sold status + sell columns).")


# ─────────────────────────────────────────────
# TSL BUCKS HELPERS (delegates to flow_wallet)
# ─────────────────────────────────────────────

async def get_balance(user_id) -> int:
    """Return the current TSL Bucks balance for a user."""
    return await flow_wallet.get_balance(int(user_id))


async def update_balance(user_id, delta: int, *, contract_id=None):
    """
    Add `delta` (positive = credit, negative = debit) to a user's balance.
    Raises ValueError if the resulting balance would go negative.
    """
    uid = int(user_id)
    sid = str(contract_id) if contract_id is not None else None
    if delta >= 0:
        await flow_wallet.credit(uid, delta, "PREDICTION",
                                 description="prediction market",
                                 subsystem="PREDICTION", subsystem_id=sid)
    else:
        await flow_wallet.debit(uid, abs(delta), "PREDICTION",
                                description="prediction market",
                                subsystem="PREDICTION", subsystem_id=sid)


# ─────────────────────────────────────────────
# PREDICTION BUY / SELL EXECUTION
# ─────────────────────────────────────────────

PREDICTION_WAGER_PRESETS = [50, 100, 250, 500, 1000]


def _price_to_american(price: float) -> int:
    """Convert Polymarket price (0.0–1.0) to American odds."""
    if price <= 0 or price >= 1:
        return -110  # default
    if price >= 0.5:
        return -round((price / (1 - price)) * 100)
    return round(((1 - price) / price) * 100)


async def _execute_prediction_buy(
    user_id: int,
    market_id: str,
    slug: str,
    side: str,
    price: float,
    quantity: int,
    title: str,
) -> dict:
    """Execute an atomic prediction market buy.

    Returns dict: {contract_id, cost, payout, new_balance}
    Raises: ValueError on validation failure, InsufficientFundsError on low balance.
    """
    cost_bucks = price_to_bucks(price) * quantity
    payout = PAYOUT_SCALE * quantity

    async with flow_wallet.get_user_lock(user_id):
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")

            # Guard: market must still be active
            async with db.execute(
                "SELECT status FROM prediction_markets WHERE market_id = ?",
                (market_id,),
            ) as cur:
                mkt_row = await cur.fetchone()
            if not mkt_row or mkt_row[0] != "active":
                raise ValueError("This market is no longer active.")

            # Check balance
            balance = await flow_wallet.get_balance(user_id, con=db)
            if balance < cost_bucks:
                raise flow_wallet.InsufficientFundsError(
                    f"You need **{cost_bucks:,} TSL Bucks** but only have **{balance:,}**."
                )

            # Insert contract
            await db.execute(
                "INSERT INTO prediction_contracts "
                "(user_id, market_id, slug, side, buy_price, quantity, "
                "cost_bucks, potential_payout, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (user_id, market_id, slug, side, price, quantity, cost_bucks, payout, now),
            )
            async with db.execute("SELECT last_insert_rowid()") as cur:
                contract_id = (await cur.fetchone())[0]

            # Debit wallet
            await flow_wallet.debit(
                user_id, cost_bucks, "PREDICTION",
                description="prediction market bet",
                subsystem="PREDICTION", subsystem_id=str(contract_id),
                con=db,
            )

            # Wager registry
            import wager_registry
            await wager_registry.register_wager(
                "PREDICTION", str(contract_id), int(user_id), cost_bucks,
                label=f"{slug}: {side} @ ${price:.2f}",
                con=db,
            )

            # Fetch end_date for sportsbook_core event registration
            async with db.execute(
                "SELECT end_date FROM prediction_markets WHERE market_id = ?",
                (market_id,),
            ) as cur2:
                ed_row = await cur2.fetchone()
            end_date_str = ed_row[0] if ed_row else None

            await db.commit()

    # Register event + bet in sportsbook_core (idempotent — safe to call on every buy)
    try:
        import sportsbook_core
        event_id = f"poly:{market_id}"
        commence_ts = None
        if end_date_str:
            try:
                commence_ts = int(
                    datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).timestamp()
                )
            except (ValueError, AttributeError):
                commence_ts = None

        await sportsbook_core.write_event(
            event_id=event_id,
            source="POLY",
            home=title,
            away="",
            commence_ts=commence_ts,
        )
        await sportsbook_core.write_bet(
            discord_id=user_id,
            event_id=event_id,
            bet_type="Prediction",
            pick=side,
            line=None,
            odds=_price_to_american(price),
            wager=cost_bucks,
        )
    except Exception as exc:
        log.warning(f"[POLY] sportsbook_core registration failed for contract {contract_id}: {exc}")
        # Non-fatal — legacy prediction_contracts row is still authoritative during transition

    new_bal = balance - cost_bucks
    return {
        "contract_id": contract_id,
        "cost": cost_bucks,
        "payout": payout,
        "new_balance": new_bal,
        "quantity": quantity,
    }


async def _execute_prediction_sell(
    user_id: int,
    contract_id: int,
    sell_quantity: int,
    current_price: float,
) -> dict:
    """Sell (close) prediction contracts at current market price.

    For partial sells, the original contract row is reduced and a new 'sold' row
    is created for the sold portion to preserve audit trail.

    Returns dict: {proceeds, new_balance, profit_loss, sold_id}
    Raises: ValueError on validation failure.
    """
    sell_bucks_per = price_to_bucks(current_price)
    proceeds = sell_bucks_per * sell_quantity

    async with flow_wallet.get_user_lock(user_id):
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")

            # Load contract
            async with db.execute(
                "SELECT user_id, market_id, slug, side, buy_price, quantity, "
                "cost_bucks, potential_payout, status "
                "FROM prediction_contracts WHERE id = ?",
                (contract_id,),
            ) as cur:
                row = await cur.fetchone()

            if not row:
                raise ValueError("Contract not found.")
            (c_uid, c_mid, c_slug, c_side, c_buy_price, c_qty,
             c_cost, c_payout, c_status) = row

            if str(c_uid) != str(user_id):
                raise ValueError("Contract does not belong to you.")
            if c_status != "open":
                raise ValueError("Contract is not open.")
            if sell_quantity > c_qty:
                raise ValueError(
                    f"Can't sell {sell_quantity} — you only have {c_qty} contracts."
                )

            # Calculate cost basis for the sold portion (proportional)
            cost_basis = (c_cost * sell_quantity) // c_qty if c_qty > 0 else 0
            profit_loss = proceeds - cost_basis

            if sell_quantity == c_qty:
                # Full sell — update in place
                await db.execute(
                    "UPDATE prediction_contracts SET status = 'sold', "
                    "sell_price = ?, sell_bucks = ?, sold_at = ? WHERE id = ?",
                    (current_price, proceeds, now, contract_id),
                )
                sold_id = contract_id
            else:
                # Partial sell — reduce original, insert sold row
                remaining_qty = c_qty - sell_quantity
                remaining_cost = c_cost - cost_basis
                remaining_payout = (c_payout * remaining_qty) // c_qty

                # Shrink original contract
                await db.execute(
                    "UPDATE prediction_contracts SET quantity = ?, "
                    "cost_bucks = ?, potential_payout = ? WHERE id = ?",
                    (remaining_qty, remaining_cost, remaining_payout, contract_id),
                )

                # Insert sold portion
                await db.execute(
                    "INSERT INTO prediction_contracts "
                    "(user_id, market_id, slug, side, buy_price, quantity, "
                    "cost_bucks, potential_payout, status, created_at, "
                    "sell_price, sell_bucks, sold_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sold', ?, ?, ?, ?)",
                    (user_id, c_mid, c_slug, c_side, c_buy_price,
                     sell_quantity, cost_basis, PAYOUT_SCALE * sell_quantity,
                     now, current_price, proceeds, now),
                )
                async with db.execute("SELECT last_insert_rowid()") as cur:
                    sold_id = (await cur.fetchone())[0]

            # Credit wallet with proceeds
            await flow_wallet.credit(
                user_id, proceeds, "PREDICTION",
                description=f"sell {sell_quantity} contracts",
                subsystem="PREDICTION", subsystem_id=str(sold_id),
                con=db,
            )
            await db.commit()

        balance = await flow_wallet.get_balance(user_id)

    return {
        "proceeds": proceeds,
        "new_balance": balance,
        "profit_loss": profit_loss,
        "sold_id": sold_id,
        "cost_basis": cost_basis,
    }


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
# PREDICTION WORKSPACE (sportsbook-parity UX)
# ─────────────────────────────────────────────

class _WorkspaceMarketSelect(discord.ui.Select):
    """Dropdown for selecting a market in the workspace."""

    def __init__(self, markets: list[dict], workspace: "PredictionWorkspace"):
        self._ws = workspace
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
            options = [discord.SelectOption(label="No markets available", value="none")]
        super().__init__(placeholder="Select a market...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        market_id = self.values[0]
        if market_id == "none":
            await interaction.response.defer()
            return
        await self._ws._on_market_select(interaction, market_id)


class _WorkspacePositionSelect(discord.ui.Select):
    """Dropdown for selecting a position to sell in the workspace."""

    def __init__(self, positions: list[dict], workspace: "PredictionWorkspace"):
        self._ws = workspace
        options = []
        for i, pos in enumerate(positions[:25]):
            side_emoji = "✅" if pos["side"] == "YES" else "❌"
            label = f"{side_emoji} {pos['title'][:80]}"
            desc = f"{pos['side']} × {pos['qty']} · Cost: ${pos['cost']:,}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(pos.get("contract_id", i)),
                description=desc[:100],
            ))
        if not options:
            options = [discord.SelectOption(label="No open positions", value="none")]
        super().__init__(placeholder="Select a position to sell...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "none":
            await interaction.response.defer()
            return
        await self._ws._on_position_select(interaction, int(val))


class CustomPredictionWagerModal(discord.ui.Modal):
    """Custom wager modal — input is total Bucks to spend."""

    amount_input = discord.ui.TextInput(
        label="Total Bucks to spend",
        placeholder="e.g. 200",
        min_length=1,
        max_length=8,
        required=True,
    )

    def __init__(self, workspace: "PredictionWorkspace"):
        market = workspace._selected_market
        side = workspace._pending_side or "YES"
        super().__init__(title=f"Buy {side} — {market['title'][:34]}")
        self._ws = workspace

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount_input.value.strip().replace(",", "")
        if not raw.isdigit() or int(raw) < 1:
            await interaction.response.send_message(
                "❌ Please enter a whole number ≥ 1.", ephemeral=True
            )
            return

        amount = int(raw)
        market = self._ws._selected_market
        side = self._ws._pending_side
        price = market["yes_price"] if side == "YES" else market["no_price"]
        cost_per = price_to_bucks(price)
        quantity = amount // cost_per if cost_per > 0 else 0

        if quantity < 1:
            await interaction.response.send_message(
                f"❌ Minimum cost per contract is **${cost_per:,}**. "
                f"You entered **${amount:,}**.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await _execute_prediction_buy(
                user_id=interaction.user.id,
                market_id=market["market_id"],
                slug=market["slug"],
                side=side,
                price=price,
                quantity=quantity,
                title=market["title"],
            )
        except (ValueError, flow_wallet.InsufficientFundsError) as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to place bet: {e}", ephemeral=True)
            return

        # Show confirmation
        theme_id = get_theme_for_render(interaction.user.id)
        try:
            png = await render_bet_confirmation_card(
                market_title=market["title"],
                side=side,
                price=price,
                quantity=result["quantity"],
                cost=result["cost"],
                potential_payout=result["payout"],
                balance=result["new_balance"],
                player_name=interaction.user.display_name,
                theme_id=theme_id,
            )
            await send_card(interaction, png, filename="bet_confirm.png",
                            followup=True, ephemeral=True)
        except Exception:
            log.exception("Failed to render bet confirmation card")
            await interaction.followup.send(
                f"✅ Bought **{result['quantity']}** {side} contracts for "
                f"**${result['cost']:,}**. Balance: **${result['new_balance']:,}**",
                ephemeral=True,
            )

        # Post to #ledger
        try:
            new_bal = await flow_wallet.get_balance(interaction.user.id)
            txn_id = await flow_wallet.get_last_txn_id(interaction.user.id)
            from ledger_poster import post_transaction
            await post_transaction(
                interaction.client, interaction.guild_id, interaction.user.id,
                "PREDICTION", -result["cost"], new_bal,
                f"Buy {result['quantity']} {side} — {market['title'][:50]}",
                txn_id,
            )
        except Exception:
            pass


class PredictionWorkspace(discord.ui.View):
    """Edit-in-place workspace for prediction markets.

    All states render into a single ephemeral message.
    Mirrors SportsbookWorkspace pattern from flow_sportsbook.py.
    """

    def __init__(self, cog, user_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user_id = user_id
        # State
        self._tab = "markets"           # "markets" or "portfolio"
        self._markets: list[dict] = []
        self._selected_market: dict | None = None
        self._pending_side: str | None = None
        self._pending_price: float = 0.0
        self._positions: list[dict] = []
        self._selected_position: dict | None = None
        self._state = "market_list"     # tracks sub-state for back navigation

    # ── Core: edit-in-place ──────────────────────────────────────────────────

    async def _update_workspace(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        *,
        file: discord.File | None = None,
        is_initial: bool = False,
    ):
        """Edit the workspace message in-place."""
        kwargs: dict = {"embed": embed, "view": self}
        if file is not None:
            embed.set_image(url=f"attachment://{file.filename}")
            kwargs["attachments"] = [file]
        else:
            embed.set_image(url=None)
            kwargs["attachments"] = []

        if is_initial:
            if interaction.response.is_done():
                await interaction.followup.send(**kwargs, ephemeral=True)
            else:
                await interaction.response.send_message(**kwargs, ephemeral=True)
        elif not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        else:
            await interaction.edit_original_response(**kwargs)

    def _balance_footer(self) -> str:
        """Build a balance footer (populated lazily by show_ methods)."""
        return "FLOW Markets · Powered by Polymarket"

    # ── State: Market List ───────────────────────────────────────────────────

    async def show_market_list(self, interaction: discord.Interaction, *, is_initial: bool = False):
        """Show market list with dropdown + tab buttons."""
        self._state = "market_list"
        self._selected_market = None
        self._tab = "markets"
        self.clear_items()

        # Row 0: Tab buttons
        markets_btn = discord.ui.Button(
            label="📊 Markets", style=discord.ButtonStyle.primary, row=0,
        )
        markets_btn.callback = lambda i: self.show_market_list(i)
        markets_btn.disabled = True  # already on this tab
        self.add_item(markets_btn)

        portfolio_btn = discord.ui.Button(
            label="📋 Portfolio", style=discord.ButtonStyle.secondary, row=0,
        )
        portfolio_btn.callback = lambda i: self.show_portfolio(i)
        self.add_item(portfolio_btn)

        refresh_btn = discord.ui.Button(
            label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=0,
        )
        refresh_btn.callback = self._refresh_markets
        self.add_item(refresh_btn)

        # Row 1: Market select dropdown
        if self._markets:
            self.add_item(_WorkspaceMarketSelect(self._markets, self))

        # Build embed + card
        theme_id = get_theme_for_render(self.user_id)
        embed = discord.Embed(
            title="FLOW Prediction Markets",
            description=f"**{len(self._markets)}** markets · Select one to view details & bet",
            color=AtlasColors.TSL_GOLD,
        )
        card_file = None
        if self._markets:
            try:
                png = await render_curated_list_card(
                    self._markets, filter_label="Curated · All Categories",
                    theme_id=theme_id,
                )
                card_file = discord.File(io.BytesIO(png), filename="markets.png")
            except Exception:
                log.exception("Failed to render curated list card")

        embed.set_footer(text=self._balance_footer())
        embed.timestamp = datetime.now(timezone.utc)
        await self._update_workspace(interaction, embed, file=card_file, is_initial=is_initial)

    async def _refresh_markets(self, interaction: discord.Interaction):
        """Refresh market list with new curated selection."""
        await interaction.response.defer()
        if self.cog:
            self._markets = await self.cog._get_curated_selection(
                count=MARKETS_PER_PAGE,
            )
        await self.show_market_list(interaction)

    async def _on_market_select(self, interaction: discord.Interaction, market_id: str):
        """User selected a market from the dropdown."""
        market = next((m for m in self._markets if m.get("market_id") == market_id), None)
        if not market:
            await interaction.response.defer()
            return

        # Log engagement
        try:
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                await db.execute(
                    "INSERT INTO market_engagement (market_id, event_type, user_id, source, created_at) "
                    "VALUES (?, 'view', ?, 'workspace', ?)",
                    (market_id, str(interaction.user.id), datetime.now(timezone.utc).isoformat()),
                )
                await db.commit()
        except Exception:
            pass

        await self.show_market_detail(interaction, market)

    # ── State: Market Detail ─────────────────────────────────────────────────

    async def show_market_detail(self, interaction: discord.Interaction, market: dict, *, is_initial: bool = False):
        """Show market detail with YES/NO buttons."""
        self._state = "market_detail"
        self._selected_market = market
        self.clear_items()

        # Row 0: Back + Portfolio tab
        back_btn = discord.ui.Button(
            label="← Markets", style=discord.ButtonStyle.secondary, row=0,
        )
        back_btn.callback = lambda i: self.show_market_list(i)
        self.add_item(back_btn)

        portfolio_btn = discord.ui.Button(
            label="📋 Portfolio", style=discord.ButtonStyle.secondary, row=0,
        )
        portfolio_btn.callback = lambda i: self.show_portfolio(i)
        self.add_item(portfolio_btn)

        # Row 1: Buy YES / Buy NO
        yes_btn = discord.ui.Button(
            label="Buy YES ✅", style=discord.ButtonStyle.success, row=1,
        )
        yes_btn.callback = self._make_side_cb("YES")
        self.add_item(yes_btn)

        no_btn = discord.ui.Button(
            label="Buy NO ❌", style=discord.ButtonStyle.danger, row=1,
        )
        no_btn.callback = self._make_side_cb("NO")
        self.add_item(no_btn)

        # Build embed + card
        theme_id = get_theme_for_render(self.user_id)
        embed = discord.Embed(
            title=market.get("title", "")[:80],
            color=0x3498DB,
        )
        card_file = None
        try:
            png = await render_market_detail_card(
                title=market.get("title", ""),
                category=market.get("category", "Other"),
                yes_price=market.get("yes_price", 0.5),
                no_price=market.get("no_price", 0.5),
                volume=market.get("volume", 0),
                liquidity=market.get("liquidity", 0),
                end_date=market.get("end_date", ""),
                theme_id=theme_id,
            )
            card_file = discord.File(io.BytesIO(png), filename="market_detail.png")
        except Exception:
            log.exception("Failed to render market detail card")
            embed.add_field(name="YES", value=f"{market.get('yes_price', 0.5):.0%}", inline=True)
            embed.add_field(name="NO", value=f"{market.get('no_price', 0.5):.0%}", inline=True)

        embed.set_footer(text="FLOW Markets · Select YES or NO to bet")
        embed.timestamp = datetime.now(timezone.utc)
        await self._update_workspace(interaction, embed, file=card_file, is_initial=is_initial)

    def _make_side_cb(self, side: str):
        """Factory: YES/NO button → opens wager presets."""
        async def callback(interaction: discord.Interaction):
            market = self._selected_market
            price = market["yes_price"] if side == "YES" else market["no_price"]
            self._pending_side = side
            self._pending_price = price
            await self.show_wager_presets(interaction)
        return callback

    # ── State: Wager Presets ─────────────────────────────────────────────────

    async def show_wager_presets(self, interaction: discord.Interaction):
        """Show preset amount buttons — sportsbook parity."""
        self._state = "wager_presets"
        self.clear_items()

        market = self._selected_market
        side = self._pending_side
        price = self._pending_price
        cost_per = price_to_bucks(price)

        balance = await flow_wallet.get_balance(self.user_id)

        # Row 0: Preset buttons (up to 5)
        for amt in PREDICTION_WAGER_PRESETS:
            qty = amt // cost_per if cost_per > 0 else 0
            can_buy = qty >= 1 and amt <= balance
            btn = discord.ui.Button(
                label=f"${amt:,}",
                style=discord.ButtonStyle.success if can_buy else discord.ButtonStyle.secondary,
                disabled=not can_buy,
                row=0,
            )
            btn.callback = self._make_buy_preset_cb(amt)
            self.add_item(btn)

        # Row 1: Custom + Back
        custom_btn = discord.ui.Button(
            label="✏️ Custom", style=discord.ButtonStyle.secondary, row=1,
        )
        custom_btn.callback = self._custom_wager_cb
        self.add_item(custom_btn)

        back_btn = discord.ui.Button(
            label="← Back", style=discord.ButtonStyle.secondary, row=1,
        )
        back_btn.callback = lambda i: self.show_market_detail(i, market)
        self.add_item(back_btn)

        # Build embed
        embed = discord.Embed(
            title=f"📋  {side} — {market['title'][:60]}",
            color=0x2ECC71 if side == "YES" else 0xE74C3C,
        )
        embed.description = (
            f"**Price:** {price:.0%} · **Cost per contract:** ${cost_per:,}\n"
            f"**Payout per contract:** ${PAYOUT_SCALE:,}\n\n"
            f"💰 Balance: **${balance:,}**"
        )
        embed.set_footer(text="FLOW Markets · Select an amount or enter custom")
        await self._update_workspace(interaction, embed)

    def _make_buy_preset_cb(self, amount: int):
        """Factory: preset button → execute buy."""
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            market = self._selected_market
            side = self._pending_side
            price = self._pending_price
            cost_per = price_to_bucks(price)
            quantity = amount // cost_per if cost_per > 0 else 0

            try:
                result = await _execute_prediction_buy(
                    user_id=interaction.user.id,
                    market_id=market["market_id"],
                    slug=market["slug"],
                    side=side,
                    price=price,
                    quantity=quantity,
                    title=market["title"],
                )
            except (ValueError, flow_wallet.InsufficientFundsError) as e:
                embed = discord.Embed(description=f"❌ {e}", color=0xE74C3C)
                await self._update_workspace(interaction, embed)
                return
            except Exception as e:
                embed = discord.Embed(description=f"❌ Failed: {e}", color=0xE74C3C)
                await self._update_workspace(interaction, embed)
                return

            # Show confirmation card in workspace
            theme_id = get_theme_for_render(interaction.user.id)
            self.clear_items()

            # Add navigation buttons on confirmation
            back_markets = discord.ui.Button(
                label="← Markets", style=discord.ButtonStyle.secondary, row=0,
            )
            back_markets.callback = lambda i: self.show_market_list(i)
            self.add_item(back_markets)

            portfolio_btn = discord.ui.Button(
                label="📋 Portfolio", style=discord.ButtonStyle.secondary, row=0,
            )
            portfolio_btn.callback = lambda i: self.show_portfolio(i)
            self.add_item(portfolio_btn)

            card_file = None
            embed = discord.Embed(
                title="✅ Contract Purchased",
                description=(
                    f"**{market['title'][:60]}**\n"
                    f"{side} × {result['quantity']} · Cost: **${result['cost']:,}**\n"
                    f"Potential: **${result['payout']:,}**"
                ),
                color=0x2ECC71 if side == "YES" else 0xE74C3C,
            )
            try:
                png = await render_bet_confirmation_card(
                    market_title=market["title"],
                    side=side,
                    price=price,
                    quantity=result["quantity"],
                    cost=result["cost"],
                    potential_payout=result["payout"],
                    balance=result["new_balance"],
                    player_name=interaction.user.display_name,
                    theme_id=theme_id,
                )
                card_file = discord.File(io.BytesIO(png), filename="bet_confirm.png")
            except Exception:
                log.exception("Failed to render bet confirmation card")

            embed.set_footer(text=f"Balance: ${result['new_balance']:,}")
            await self._update_workspace(interaction, embed, file=card_file)

            # Post to #ledger (fire-and-forget)
            try:
                new_bal = await flow_wallet.get_balance(interaction.user.id)
                txn_id = await flow_wallet.get_last_txn_id(interaction.user.id)
                from ledger_poster import post_transaction
                await post_transaction(
                    interaction.client, interaction.guild_id, interaction.user.id,
                    "PREDICTION", -result["cost"], new_bal,
                    f"Buy {result['quantity']} {side} — {market['title'][:50]}",
                    txn_id,
                )
            except Exception:
                pass
        return callback

    async def _custom_wager_cb(self, interaction: discord.Interaction):
        """Open custom wager modal — must NOT defer first."""
        modal = CustomPredictionWagerModal(self)
        await interaction.response.send_modal(modal)

    # ── State: Portfolio List ────────────────────────────────────────────────

    async def _load_positions(self):
        """Load open positions from DB."""
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("""
                SELECT pc.id, pc.market_id, pm.title, pc.side, pc.buy_price,
                       pc.quantity, pc.cost_bucks, pc.potential_payout,
                       pm.yes_price, pm.no_price, pc.slug
                FROM prediction_contracts pc
                LEFT JOIN prediction_markets pm ON pm.market_id = pc.market_id
                WHERE pc.user_id = ? AND pc.status = 'open'
                ORDER BY pc.created_at DESC
                LIMIT 25
            """, (self.user_id,)) as cursor:
                rows = await cursor.fetchall()

        self._positions = []
        for (cid, mid, title, side, buy_price, qty, cost, payout,
             yes_p, no_p, slug) in rows:
            current_price = (yes_p if side == "YES" else no_p) or buy_price
            self._positions.append({
                "contract_id": cid,
                "market_id": mid,
                "title": title or mid,
                "slug": slug or "",
                "side": side,
                "buy_price": buy_price,
                "qty": qty,
                "cost": cost,
                "payout": payout,
                "current_price": current_price,
            })

    async def show_portfolio(self, interaction: discord.Interaction, *, is_initial: bool = False):
        """Show portfolio with position select dropdown."""
        self._state = "portfolio_list"
        self._selected_position = None
        self._tab = "portfolio"
        self.clear_items()

        await self._load_positions()

        # Row 0: Tab buttons
        markets_btn = discord.ui.Button(
            label="📊 Markets", style=discord.ButtonStyle.secondary, row=0,
        )
        markets_btn.callback = lambda i: self.show_market_list(i)
        self.add_item(markets_btn)

        portfolio_btn = discord.ui.Button(
            label="📋 Portfolio", style=discord.ButtonStyle.primary, row=0,
        )
        portfolio_btn.callback = lambda i: self.show_portfolio(i)
        portfolio_btn.disabled = True  # already on this tab
        self.add_item(portfolio_btn)

        # Row 1: Position select dropdown (if positions exist)
        if self._positions:
            self.add_item(_WorkspacePositionSelect(self._positions, self))

        # Build embed + card
        balance = await flow_wallet.get_balance(self.user_id)
        theme_id = get_theme_for_render(self.user_id)

        if not self._positions:
            embed = discord.Embed(
                title="📋 Portfolio",
                description="You have no open prediction market positions.\nBrowse **Markets** to place your first bet!",
                color=AtlasColors.TSL_GOLD,
            )
            embed.set_footer(text=f"Balance: ${balance:,}")
            await self._update_workspace(interaction, embed, is_initial=is_initial)
            return

        total_invested = sum(p["cost"] for p in self._positions)
        total_potential = sum(p["payout"] for p in self._positions)

        embed = discord.Embed(
            title="📋 Portfolio",
            description=(
                f"**{len(self._positions)}** open positions · "
                f"Invested: **${total_invested:,}** · Potential: **${total_potential:,}**"
            ),
            color=AtlasColors.TSL_GOLD,
        )

        card_file = None
        try:
            png = await render_portfolio_card(
                positions=self._positions,
                player_name=(interaction.user.display_name
                             if hasattr(interaction, "user") else "Unknown"),
                total_invested=total_invested,
                total_potential=total_potential,
                balance=balance,
                theme_id=theme_id,
            )
            card_file = discord.File(io.BytesIO(png), filename="portfolio.png")
        except Exception:
            log.exception("Failed to render portfolio card")

        embed.set_footer(text=f"Balance: ${balance:,} · Select a position to sell")
        embed.timestamp = datetime.now(timezone.utc)
        await self._update_workspace(interaction, embed, file=card_file, is_initial=is_initial)

    async def _on_position_select(self, interaction: discord.Interaction, contract_id: int):
        """User selected a position to view sell options."""
        pos = next((p for p in self._positions if p["contract_id"] == contract_id), None)
        if not pos:
            await interaction.response.defer()
            return
        await self.show_position_detail(interaction, pos)

    # ── State: Position Detail (sell options) ────────────────────────────────

    async def show_position_detail(self, interaction: discord.Interaction, position: dict):
        """Show sell buttons for a specific position."""
        self._state = "position_detail"
        self._selected_position = position
        self.clear_items()

        # Try live price fetch with timeout
        current_price = position["current_price"]
        if self.cog:
            try:
                live = await asyncio.wait_for(
                    self.cog.client.fetch_market_by_id(position["market_id"]),
                    timeout=2.0,
                )
                if live:
                    prices = extract_prices(live)
                    current_price = (
                        prices["yes_price"] if position["side"] == "YES"
                        else prices["no_price"]
                    )
                    position["current_price"] = current_price
            except Exception:
                pass  # use cached price

        sell_price_bucks = price_to_bucks(current_price)
        qty = position["qty"]
        cost_basis = position["cost"]
        current_value = sell_price_bucks * qty
        pnl = current_value - cost_basis

        # Row 0: Back to portfolio
        back_btn = discord.ui.Button(
            label="← Portfolio", style=discord.ButtonStyle.secondary, row=0,
        )
        back_btn.callback = lambda i: self.show_portfolio(i)
        self.add_item(back_btn)

        markets_btn = discord.ui.Button(
            label="📊 Markets", style=discord.ButtonStyle.secondary, row=0,
        )
        markets_btn.callback = lambda i: self.show_market_list(i)
        self.add_item(markets_btn)

        # Row 1: Sell buttons
        # Sell All
        sell_all_btn = discord.ui.Button(
            label=f"Sell All ({qty})", style=discord.ButtonStyle.danger, row=1,
        )
        sell_all_btn.callback = self._make_sell_cb(position, qty)
        self.add_item(sell_all_btn)

        # Sell 50% (only if qty >= 2)
        if qty >= 2:
            half = qty // 2
            sell_half_btn = discord.ui.Button(
                label=f"Sell {half}", style=discord.ButtonStyle.secondary, row=1,
            )
            sell_half_btn.callback = self._make_sell_cb(position, half)
            self.add_item(sell_half_btn)

        # Sell 1 (only if qty > 1, since Sell All covers qty==1)
        if qty > 1:
            sell_one_btn = discord.ui.Button(
                label="Sell 1", style=discord.ButtonStyle.secondary, row=1,
            )
            sell_one_btn.callback = self._make_sell_cb(position, 1)
            self.add_item(sell_one_btn)

        # Render position detail card (v3)
        theme_id = get_theme_for_render(interaction.user.id)
        balance = await flow_wallet.get_balance(interaction.user.id)
        card_file = None
        try:
            png = await render_position_detail_card(
                position=position,
                sell_qty=0,
                user_balance=balance,
                player_name=interaction.user.display_name,
                theme_id=theme_id,
            )
            card_file = discord.File(io.BytesIO(png), filename="position_detail.png")
        except Exception:
            log.exception("Failed to render position detail card")

        pnl_str = f"+${pnl:,}" if pnl >= 0 else f"-${abs(pnl):,}"
        embed = discord.Embed(
            title=f"{position['title'][:70]}",
            color=0x2ECC71 if pnl >= 0 else 0xE74C3C,
        )
        embed.set_footer(text=f"Sell price: ${sell_price_bucks:,}/contract · P/L: {pnl_str}")
        await self._update_workspace(interaction, embed, file=card_file)

    def _make_sell_cb(self, position: dict, quantity: int):
        """Factory: sell button → execute sell."""
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                result = await _execute_prediction_sell(
                    user_id=interaction.user.id,
                    contract_id=position["contract_id"],
                    sell_quantity=quantity,
                    current_price=position["current_price"],
                )
            except (ValueError, flow_wallet.InsufficientFundsError) as e:
                embed = discord.Embed(description=f"❌ {e}", color=0xE74C3C)
                await self._update_workspace(interaction, embed)
                return
            except Exception as e:
                embed = discord.Embed(description=f"❌ Failed: {e}", color=0xE74C3C)
                await self._update_workspace(interaction, embed)
                return

            # Show sell confirmation
            self.clear_items()
            back_portfolio = discord.ui.Button(
                label="📋 Portfolio", style=discord.ButtonStyle.secondary, row=0,
            )
            back_portfolio.callback = lambda i: self.show_portfolio(i)
            self.add_item(back_portfolio)

            markets_btn = discord.ui.Button(
                label="📊 Markets", style=discord.ButtonStyle.secondary, row=0,
            )
            markets_btn.callback = lambda i: self.show_market_list(i)
            self.add_item(markets_btn)

            pnl = result["profit_loss"]
            pnl_str = f"+${pnl:,}" if pnl >= 0 else f"-${abs(pnl):,}"
            pnl_emoji = "📈" if pnl >= 0 else "📉"

            card_file = None
            try:
                theme_id = get_theme_for_render(interaction.user.id)
                png = await render_sell_confirmation_card(
                    market_title=position["title"],
                    side=position["side"],
                    sell_quantity=quantity,
                    sell_price=position["current_price"],
                    proceeds=result["proceeds"],
                    cost_basis=result["cost_basis"],
                    profit_loss=pnl,
                    balance=result["new_balance"],
                    player_name=interaction.user.display_name,
                    theme_id=theme_id,
                )
                card_file = discord.File(io.BytesIO(png), filename="sell_confirm.png")
            except Exception:
                log.exception("Failed to render sell confirmation card")

            embed = discord.Embed(
                title="💰 Contracts Sold",
                description=(
                    f"**{position['title'][:60]}**\n"
                    f"Sold **{quantity}** {position['side']} contracts\n"
                    f"Proceeds: **${result['proceeds']:,}** · {pnl_emoji} P/L: **{pnl_str}**"
                ),
                color=0x2ECC71 if pnl >= 0 else 0xE74C3C,
            )
            embed.set_footer(text=f"Balance: ${result['new_balance']:,}")
            await self._update_workspace(interaction, embed, file=card_file)

            # Post to #ledger
            try:
                txn_id = await flow_wallet.get_last_txn_id(interaction.user.id)
                from ledger_poster import post_transaction
                await post_transaction(
                    interaction.client, interaction.guild_id, interaction.user.id,
                    "PREDICTION", result["proceeds"], result["new_balance"],
                    f"Sell {quantity} {position['side']} — {position['title'][:50]}",
                    txn_id,
                )
            except Exception:
                pass
        return callback


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
        events = await self.client.fetch_active_events(limit=200)
        if not events:
            log.warning("Polymarket sync returned 0 events — trying direct markets fetch.")
            # Fallback: fetch markets directly
            markets_direct = await self.client.fetch_active_markets(limit=200)
            if markets_direct:
                events = [{"id": "direct", "markets": markets_direct}]

        now = datetime.now(timezone.utc).isoformat()
        upserted = 0
        garbage_count = 0

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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

                    # Six-layer garbage filter on raw API data
                    if _is_garbage_market(event, mkt):
                        garbage_count += 1
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

        log.info(f"Polymarket sync complete — {upserted} upserted, {garbage_count} garbage-filtered.")

        # ── Pass 2: Emit EVENT_FINALIZED for newly resolved markets ──────
        await self._finalize_resolved_pass()

        # ── Pass 2c: Alert on stale markets with open contracts ──
        await self._stale_market_alert_pass()

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
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute(
                "SELECT market_id, title, slug FROM prediction_markets "
                "WHERE status='active' AND category LIKE '%Other%' "
                "LIMIT 50"
            ) as cursor:
                unknowns = await cursor.fetchall()

        if not unknowns:
            log.info("No 'Other' markets to classify.")
            return

        log.info(f"Classifying {len(unknowns)} markets with AI...")

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

        try:
            result = await atlas_ai.generate(prompt, tier=Tier.HAIKU)
            text = result.text
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if not json_match:
                log.warning("AI returned non-JSON for classification.")
                return
            classifications = json.loads(json_match.group())
        except Exception as e:
            log.error(f"AI classification failed: {e}")
            return

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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

            # Pre-fetch internal bet velocity + recent daily drop features
            bet_counts = await _get_internal_bet_counts(db)
            recent_featured = await _get_recently_featured_ids(db)

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
                    "category": category,
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
                score, breakdown = _compute_curation_score(
                    market, days_in_pool, same_cat,
                    internal_bet_users=bet_counts.get(market_id, 0),
                    recently_featured=market_id in recent_featured,
                )

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
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
                        png = await render_price_alert_card(
                            market={
                                "title": title, "category": category,
                                "yes_price": current_price, "no_price": no_price,
                            },
                            old_price=old_price,
                            new_price=current_price,
                            holders=holders,
                            theme_id=None,
                        )

                        # Add bet button
                        view = discord.ui.View(timeout=3600)
                        bet_btn = discord.ui.Button(
                            label="Bet Now",
                            style=discord.ButtonStyle.primary,
                            custom_id=f"alert_bet_{market_id}",
                        )

                        async def _alert_bet_cb(interaction: discord.Interaction, mid=market_id):
                            # Log engagement
                            async with aiosqlite.connect(DB_PATH, timeout=30) as db2:
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

                        await send_card_to_channel(channel, png, filename="price_alert.png", view=view)
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

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute(
                "INSERT INTO market_engagement (market_id, event_type, user_id, source, created_at) "
                "VALUES (?, 'view', ?, 'markets_cmd', ?)",
                (market_id, str(interaction.user.id), datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

        # Open workspace directly to market detail
        ws = PredictionWorkspace(self, interaction.user.id)
        ws._markets = [m]
        await ws.show_market_detail(interaction, m, is_initial=True)

    async def _finalize_resolved_pass(self):
        """
        Detect newly resolved Polymarket markets and emit EVENT_FINALIZED so
        sportsbook_core can settle Prediction bets via grade_bet().

        Replaces the old _auto_resolve_pass + _local_settle_pass pipeline.
        Direct wallet/contract mutations are now handled by sportsbook_core.settle_event
        which is subscribed to EVENT_FINALIZED on the flow_bus.
        """
        import sportsbook_core
        from flow_events import flow_bus, EVENT_FINALIZED

        closed_markets = await self.client.fetch_closed_markets(limit=100)
        finalized_count = 0

        for mkt in (closed_markets or []):
            market_id = str(mkt.get("id", ""))
            if not market_id:
                continue

            result = detect_result(mkt)
            if not result:
                continue  # Not clearly resolved yet

            result_upper = result.upper()
            event_id = f"poly:{market_id}"

            # Guard: skip markets already finalized in sportsbook_core
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                async with db.execute(
                    "SELECT resolved_by FROM prediction_markets WHERE market_id = ?",
                    (market_id,)
                ) as cursor:
                    row = await cursor.fetchone()
            if row and row[0] and row[0] != "pending":
                continue  # Already handled

            slug  = mkt.get("slug", "")
            title = mkt.get("question", "") or mkt.get("title", slug)

            try:
                await sportsbook_core.finalize_event(
                    event_id=event_id,
                    home_score=0,
                    away_score=0,
                    result_payload={"resolved_side": result_upper},
                )
                await flow_bus.emit(EVENT_FINALIZED, {"event_id": event_id, "source": "POLY"})
                finalized_count += 1
                log.info(f"[POLY] Finalized {market_id} → {result_upper} via sportsbook_core")
            except Exception as exc:
                log.error(f"[POLY] finalize_event failed for {market_id}: {exc}")
                continue

            # Update local prediction_markets table so the guard above fires next time
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                await db.execute(
                    "UPDATE prediction_markets "
                    "SET status='closed', result=?, resolved_by='auto' "
                    "WHERE market_id=?",
                    (result_upper, market_id)
                )
                await db.commit()

        # ── Local scan: catch markets that fell off the top-100 closed endpoint ──
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT c.market_id "
                "FROM prediction_contracts c "
                "JOIN prediction_markets m ON m.market_id = c.market_id "
                "WHERE c.status = 'open' AND m.resolved_by = 'pending'"
            ) as cursor:
                stale_rows = await cursor.fetchall()

        for row in (stale_rows or []):
            market_id = row[0]
            mkt = await self.client.fetch_market_by_id(market_id)
            if not mkt:
                continue
            result = detect_result(mkt)
            if not result:
                continue
            result_upper = result.upper()
            event_id = f"poly:{market_id}"
            try:
                await sportsbook_core.finalize_event(
                    event_id=event_id,
                    home_score=0,
                    away_score=0,
                    result_payload={"resolved_side": result_upper},
                )
                await flow_bus.emit(EVENT_FINALIZED, {"event_id": event_id, "source": "POLY"})
                finalized_count += 1
                log.info(f"[POLY] Local scan finalized {market_id} → {result_upper}")
            except Exception as exc:
                log.error(f"[POLY] local finalize_event failed for {market_id}: {exc}")
                continue

            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                await db.execute(
                    "UPDATE prediction_markets "
                    "SET resolved_by='auto', result=?, status='closed' "
                    "WHERE market_id=?",
                    (result_upper, market_id)
                )
                await db.commit()

        if finalized_count:
            log.info(f"[POLY] Finalize pass complete — {finalized_count} market(s) emitted EVENT_FINALIZED.")

    async def _stale_market_alert_pass(self):
        """Alert admin about markets with open contracts that haven't synced in >30 days."""
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT c.market_id, COUNT(*) as open_count,
                       SUM(c.cost_bucks) as total_at_risk,
                       m.title, m.last_synced
                FROM prediction_contracts c
                JOIN prediction_markets m ON m.market_id = c.market_id
                WHERE c.status = 'open'
                  AND m.resolved_by = 'pending'
                  AND m.last_synced < datetime('now', '-30 days')
                GROUP BY c.market_id
            """) as cursor:
                stale = await cursor.fetchall()

        if not stale:
            return

        log.warning(f"[PREDICTIONS] {len(stale)} stale market(s) with open contracts (>30d)")
        try:
            from setup_cog import get_channel_id
            guild = self.bot.guilds[0] if self.bot.guilds else None
            if not guild:
                return
            admin_ch_id = get_channel_id("admin-chat", guild.id)
            admin_ch = self.bot.get_channel(admin_ch_id) if admin_ch_id else None
            if not admin_ch:
                return

            lines = []
            for row in stale[:10]:
                title = (row["title"] or row["market_id"])[:60]
                lines.append(
                    f"`{row['market_id'][:12]}...` {title} — "
                    f"{row['open_count']} contracts, ${row['total_at_risk']:,} at risk"
                )

            embed = discord.Embed(
                title="Stale Prediction Markets (>30 days)",
                description="\n".join(lines),
                color=0xE74C3C,
            )
            embed.set_footer(
                text=f"{len(stale)} market(s) unresolved >30d. "
                     "Use /boss flow audit to review and void if needed."
            )
            await admin_ch.send(embed=embed)
        except Exception:
            log.exception("[PREDICTIONS] Failed to post stale market alert")

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
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
                    theme_id=None,
                )
                await send_card_to_channel(ch, png, filename="resolution.png")
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
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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

        # Step 2: AI editorial pass
        spotlight = None
        supporting = []

        try:
            spotlight, supporting = await self._gemini_curate(shortlist)
        except Exception as e:
            log.warning(f"AI curation failed: {e}")
            # Retry once
            await asyncio.sleep(30)
            try:
                spotlight, supporting = await self._gemini_curate(shortlist)
            except Exception as e2:
                log.error(f"AI curation retry failed: {e2}")

        # Fallback: use top 5 by score without editorial text
        if not spotlight:
            spotlight = shortlist[0]
            spotlight["analysis"] = ""
            supporting = [
                {**m, "hook": ""} for m in shortlist[1:5]
            ]

        # Step 3: Community momentum
        community_data = {}
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
                theme_id=None,
            )
        except Exception as e:
            log.error(f"Daily Drop card render failed: {e}")
            return

        # Step 6: Post to channel
        channel = self._channel()
        if not channel:
            log.warning("Prediction channel not found — cannot post Daily Drop.")
            return

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

        view = discord.ui.View(timeout=300)
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
                async with aiosqlite.connect(DB_PATH, timeout=30) as db2:
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

        msg = await send_card_to_channel(channel, png, filename="daily_drop.png", view=view)

        # Step 7: Store the selection
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
        """Use AI to select spotlight + 4 supporting markets from shortlist."""
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

        result = await atlas_ai.generate(
            prompt, system=system_instruction,
            tier=Tier.HAIKU, json_mode=True,
        )
        text = result.text

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

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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

        ws = PredictionWorkspace(self, interaction.user.id)
        ws._markets = markets
        await ws.show_market_list(interaction, is_initial=True)

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

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
                async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                    await db.execute(
                        "UPDATE prediction_markets SET yes_price=?, no_price=?, "
                        "last_synced=? WHERE market_id=?",
                        (yes_price, no_price, now, market_id),
                    )
                    await db.commit()
        except Exception:
            log.warning("Price sync failed for market %s, using cached", market_id)

        # Open workspace directly to market detail
        market_dict = {
            "market_id": market_id,
            "slug": mkt_slug,
            "title": title,
            "category": category,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": volume,
            "end_date": end_date,
            "status": status,
        }
        ws = PredictionWorkspace(self, interaction.user.id)
        ws._markets = [market_dict]
        await ws.show_market_detail(interaction, market_dict, is_initial=True)

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

        ws = PredictionWorkspace(self, interaction.user.id)
        # Also load markets so the Markets tab works from portfolio
        ws._markets = await self._get_curated_selection(count=MARKETS_PER_PAGE)
        await ws.show_portfolio(interaction, is_initial=True)

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
        """Delegation target for boss_cog. Expects deferred interaction."""
        await self._ensure_db()
        slug   = slug.strip().lower()
        result = result.upper().strip()

        if result not in ("YES", "NO", "VOID"):
            await interaction.followup.send(
                "❌ `result` must be YES, NO, or VOID.", ephemeral=True
            )
            return

        # Look up market_id from slug
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
        event_id = f"poly:{market_id}"

        import sportsbook_core
        from flow_events import flow_bus, EVENT_FINALIZED

        try:
            await sportsbook_core.finalize_event(
                event_id=event_id,
                home_score=0,
                away_score=0,
                result_payload={"resolved_side": result},
            )
            await flow_bus.emit(EVENT_FINALIZED, {"event_id": event_id, "source": "POLY"})
        except Exception as exc:
            log.error(f"[POLY] Manual finalize_event failed for {market_id}: {exc}")
            await interaction.followup.send(
                f"❌ sportsbook_core.finalize_event failed: {exc}", ephemeral=True
            )
            return

        # Update local DB so the sync guard skips this market
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute(
                "UPDATE prediction_markets "
                "SET status='closed', result=?, resolved_by='admin' "
                "WHERE market_id=?",
                (result, market_id)
            )
            await db.commit()

        log.info(f"[POLY] Manual resolve: {market_id} → {result} by admin")

        await interaction.followup.send(
            f"✅ Resolved `{slug}` as **{result}**. "
            f"EVENT_FINALIZED emitted — sportsbook_core will settle open bets.",
            ephemeral=True,
        )

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
        """Delegation target for boss_cog. Expects deferred interaction."""
        await self._ensure_db()

        # Test connectivity
        test = await self.client.fetch_active_markets(limit=1)
        api_ok = test is not None

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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

    # ── Impl: refund_sports (called by boss_cog) ────

    async def refund_sports_impl(self, interaction: discord.Interaction):
        """Void all open contracts on sports-category markets and refund users."""
        await self._ensure_db()

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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


    # ── Impl: approve_market (called by boss_cog) ────

    async def _approve_market_impl(self, interaction: discord.Interaction, slug: str):
        """Mark a market as featured/approved for betting. Expects deferred interaction."""
        await self._ensure_db()
        slug = slug.strip().lower()

        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
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
