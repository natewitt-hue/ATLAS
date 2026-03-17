# Prediction Market Curation Engine — Design Spec

**Date:** 2026-03-16
**Status:** Draft
**Scope:** Algorithmic scoring, Gemini-powered daily curation, /markets UX overhaul, price alerts, engagement tracking

---

## Context

ATLAS already integrates Polymarket's Gamma API — syncing markets every 5 minutes, letting users bet TSL Bucks on YES/NO outcomes, and auto-resolving when markets close. The problem: **the raw market feed is noisy**. Users see the same mega-markets (Trump, Iran, Elon tweets) dominating, no variety enforcement, no editorial voice, and no reason to check back daily. Markets don't rotate between views.

**Goal:** Build a curation + engagement layer that makes the prediction market feel hand-curated, drives daily check-ins, and makes every view feel fresh. Think "Robinhood Daily Digest meets ESPN picks."

---

## Architecture Overview

```
Polymarket Gamma API (5-min sync, existing)
        │
        ▼
┌───────────────────────────────┐
│  Scoring Engine (every sync)  │
│  - Volume velocity            │
│  - Price tension              │
│  - Freshness decay            │
│  - Time-to-close urgency      │
│  - Liquidity trust            │
│  - Category diversity          │
│  - Event-based dedup          │
└───────────────┬───────────────┘
                │
        ┌───────┴───────┐
        ▼               ▼
┌──────────────┐  ┌──────────────────┐
│  /markets    │  │  Daily Drop      │
│  (on-demand) │  │  (9 AM cron)     │
│              │  │                  │
│  Weighted    │  │  Gemini picks    │
│  random from │  │  spotlight +     │
│  curated     │  │  supporting 4    │
│  pool → 10   │  │  + community     │
│  markets     │  │  momentum +      │
│              │  │  leaderboard     │
│  Composite   │  │                  │
│  card render │  │  Card render     │
│  + drill-down│  │  → channel post  │
└──────────────┘  └──────────────────┘
        │               │
        └───────┬───────┘
                ▼
┌───────────────────────────────┐
│  Price Alert Monitor          │
│  (>10pp move in 1hr)          │
│  → channel notification       │
└───────────────────────────────┘
                │
                ▼
┌───────────────────────────────┐
│  Engagement Metrics           │
│  (views, bets, conversions)   │
│  → feed back into scores      │
└───────────────────────────────┘
```

---

## Component 1: Algorithmic Scoring Engine

**Runs:** Every 5-min sync cycle, bolted onto existing `_sync_markets()` in `polymarket_cog.py`.

### Scoring Signals (0–100 composite)

| Signal | Weight | Calculation |
|--------|--------|-------------|
| Volume velocity | 25% | `log10(max(volume_24hr, 1))` percentile-ranked across all active markets, scaled to 0–25. Uses absolute 24hr volume with log scaling to avoid new low-liquidity markets dominating over established active ones. |
| Price tension | 20% | `1 - abs(yes_price - 0.5) * 2` scaled to 0–20. Closer to 50/50 = more interesting. |
| Freshness | 20% | `max(0, 20 - days_in_db)`. New markets get 20, lose 1 point per day. Floor at 0. |
| Time-to-close | 15% | Peak at 7 days out (15 pts). Linear decay toward 0 at 90+ days. Bonus for <3 days (urgency). |
| Liquidity | 10% | `min(liquidity / 100000, 1.0) * 10`. Higher liquidity = more trustworthy. |
| Category rarity | 10% | `10 - (same_category_count_in_pool * 2)`. Penalizes over-represented categories. Floor at 0. |

### De-duplication

Group markets by `event_id` (Polymarket's native grouping). Only the highest-scoring market per event enters the curated pool. This eliminates the "50 variants of the same question" problem.

### Rotation Mechanism

When building a display set (for `/markets` or daily drop), use **weighted random sampling without replacement**:
- Weight = `score * recency_penalty`
- `recency_penalty` = `0.1` if `last_shown` was within last 2 hours, `0.5` if within 12 hours, `1.0` otherwise
- This ensures high-quality markets appear more often but never dominate consecutive views

### Relationship to Existing Scoring

`polymarket_cog.py` already has `_compute_market_score()` (line ~194) which computes `base + recency + jitter + balance_bonus` for the existing `/markets` weighted shuffle. **This function is replaced entirely** by the new curation scoring system. The old function and its call site in `_get_display_markets()` will be removed and replaced with queries against `curated_scores`. The new system is strictly superior — it covers the same signals (recency, volume) plus adds tension, liquidity, diversity, and dedup.

### Database

```sql
CREATE TABLE IF NOT EXISTS curated_scores (
    market_id TEXT PRIMARY KEY,
    score REAL NOT NULL,
    score_breakdown TEXT,        -- JSON: {"velocity": 22, "tension": 18, ...}
    cluster_id TEXT,             -- event_id for dedup grouping
    last_shown TEXT,             -- ISO timestamp, NULL if never shown
    created_at TEXT NOT NULL,    -- when market first entered scoring pool (for freshness)
    updated_at TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES prediction_markets(market_id)
);
```

**Note:** Freshness signal uses `created_at` from this table (when the market first entered the scoring pool), not `prediction_markets.last_synced`.

---

## Component 2: Daily Drop (Gemini-Powered Curation)

**Runs:** Once daily via `discord.ext.tasks` loop, configurable time (default 9:00 AM EST).

### Pipeline

**Step 1 — Build shortlist.** Query top 30 markets by curation score from `curated_scores`, enforcing max 3 per category.

**Step 2 — Gemini editorial pass.** Single Gemini call using `get_persona("analytical")` from `echo_loader.py` as the system instruction (per CLAUDE.md rules — never hardcode persona). The user prompt:

```
From these {n} prediction markets, select:
1. ONE "Market of the Day" — the most interesting, debatable, culturally relevant.
   Write a 2-3 sentence spotlight analysis in ATLAS voice (3rd person, punchy, cites numbers).
2. FOUR supporting markets across different categories.
   For each, write a 1-line hook that makes someone want to bet.

Rules:
- Never pick markets >85% in either direction (basically decided)
- Maximize category diversity across all 5 picks
- Prioritize genuine uncertainty, cultural relevance, debate-worthy topics
- Avoid repetitive topics (multiple markets about the same person/event)

Markets:
{json_market_list}

Respond as JSON:
{
  "spotlight": {"market_id": "...", "analysis": "..."},
  "supporting": [{"market_id": "...", "hook": "..."}, ...]
}
```

**Step 3 — Community momentum.** For each selected market, query `prediction_contracts` to build:
- TSL sentiment: "TSL is 78% YES" (% of TSL bets on YES side)
- Bet count: "12 bets placed" or "Be the first to bet"
- Price movement: compare current price vs. 24hr ago from `price_snapshots`

**Step 4 — Leaderboard callouts.** Query top prediction traders by:
- Weekly profit: `SUM(potential_payout) WHERE status='won' AND resolved_at > week_start` minus `SUM(cost_bucks) WHERE resolved_at > week_start`
- Active streaks: consecutive `status='won'` contracts ordered by `resolved_at DESC`. A `status='lost'` breaks the streak. `status='voided'` is skipped (doesn't break or extend). Minimum 3 wins to qualify as a streak.
- Format: "DaViking is 7-for-7 this week"

**Step 5 — Render card.** Use the unified HTML engine (`atlas_html_engine.py` + `atlas_style_tokens.py`):
- Market of the Day: prominent spotlight section with Gemini analysis text
- 4 supporting markets: compact rows with hook text, YES/NO bars, community sentiment
- Leaderboard callout: bottom section with top trader highlights
- Pipeline: `build HTML → wrap_card(body, "jackpot") → render_card(html) → PNG` (uses existing `jackpot` status class which is gold-colored)

**Step 6 — Post to channel.** Send rendered card to predictions channel with interactive buttons:
- Select menu to drill into any of the 5 markets
- Each drill-down opens the full detail card + bet flow

**Step 7 — Store selection.**

```sql
CREATE TABLE IF NOT EXISTS daily_drops (
    drop_id INTEGER PRIMARY KEY AUTOINCREMENT,
    drop_date TEXT NOT NULL UNIQUE,
    spotlight_market_id TEXT NOT NULL,
    spotlight_analysis TEXT,
    -- supporting is a JSON array of objects: [{"market_id": "...", "hook": "..."}, ...]
    -- This single field replaces separate market_ids + hooks to avoid drift
    supporting TEXT,
    community_data TEXT,             -- JSON: sentiment, bet counts per market
    leaderboard_data TEXT,           -- JSON: top traders
    posted_at TEXT,
    message_id TEXT,                 -- Discord message ID for tracking
    FOREIGN KEY (spotlight_market_id) REFERENCES prediction_markets(market_id)
);
```

### Gemini Fallback

If the Gemini call fails (malformed JSON, timeout, rate limit), fall back to:
1. Retry once after 30 seconds
2. If still failing, use top 5 by curation score (1 spotlight + 4 supporting) without editorial text
3. Post the card without Gemini-written analysis/hooks — just market titles and prices
4. Log the failure for monitoring

---

## Component 3: /markets Command Revamp

### Default View

**10 markets** selected via weighted random sampling from the curated pool. Category diversity enforced: max 2 per category.

**Rendered as a single composite card** (like existing `render_market_list_card` but upgraded):
- Each row: index number, category badge, title, YES/NO price bar, community sentiment indicator
- Uses unified style tokens (dark theme, Outfit + JetBrains Mono, WIN/LOSS colors for YES/NO)
- 480px width, 2x DPI, Playwright render via page pool

**Below the card:** A `discord.ui.Select` menu with all 10 markets as options. Selecting one opens a **full detail card** for that market with:
- Title and category (the `title` field from `prediction_markets` — this IS the question, no separate description column needed)
- YES/NO prices (large, prominent)
- Volume, liquidity, time-to-close
- Community sentiment bar
- Price movement indicator (if data available)
- **YES and NO bet buttons** below the detail card

### Command Signature

```python
@app_commands.command(name="markets", description="Browse curated prediction markets")
@app_commands.describe(
    view="How to sort markets (default: curated)",
    category="Filter by category"
)
@app_commands.choices(view=[
    app_commands.Choice(name="Curated (default)", value="curated"),
    app_commands.Choice(name="Trending", value="trending"),
    app_commands.Choice(name="Popular", value="popular"),
    app_commands.Choice(name="New", value="new"),
])
async def markets(self, interaction, view: str = "curated", category: str = None):
```

### Filter Behaviors

| View | Behavior |
|------|----------|
| `curated` (default) | 10 curated, randomized, diverse |
| `trending` | Top 10 by 24hr volume velocity |
| `popular` | Top 10 by TSL bet count |
| `new` | 10 newest markets by DB insertion date |

The optional `category` parameter works with any view to further filter results.

### Drill-Down Flow

```
/markets → Composite 10-market card + select menu
    │
    ├─ User selects market → Full detail card + YES/NO buttons (ephemeral)
    │   │
    │   ├─ User clicks YES → Amount input modal → Bet confirmation card
    │   └─ User clicks NO  → Amount input modal → Bet confirmation card
    │
    └─ User clicks "Refresh" → New weighted random selection of 10
```

---

## Component 4: Price Movement Alerts

### Detection

During each 5-min sync, store a price snapshot:

```sql
CREATE TABLE IF NOT EXISTS price_snapshots (
    market_id TEXT NOT NULL,
    yes_price REAL NOT NULL,
    snapshot_at TEXT NOT NULL,
    PRIMARY KEY (market_id, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_time ON price_snapshots(snapshot_at);
```

**Sampling:** Store one snapshot every 15 minutes (every 3rd sync cycle), not every 5-min sync. This keeps the table at ~19,200 rows/day with 200 markets — manageable. A modulo check on sync count controls this.

Compare current price vs. snapshot from ~1 hour ago. If `abs(current - hourly) >= 0.10` (10 percentage points), trigger an alert.

### Alert Post

Post to predictions channel:
> **Price Alert:** "Will X happen?" moved from 45% → 62% YES in the last hour.
> 3 TSL members are holding YES positions.
> [Bet Now button]

### Maintenance

- Auto-prune snapshots older than 48 hours
- Max 3 alerts per hour to avoid spam
- Don't alert on markets already in the day's Daily Drop (avoid double-posting)

---

## Component 5: Engagement Metrics

### Tracked Events

| Event | Source | Purpose |
|-------|--------|---------|
| Market view | /markets detail drill-down | Measures interest |
| Bet placed | prediction_contracts INSERT | Measures conversion |
| Drop reactions | Discord message reactions | Measures daily drop engagement |
| Alert click-through | Price alert → bet | Measures alert effectiveness |

### Storage

```sql
CREATE TABLE IF NOT EXISTS market_engagement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    event_type TEXT NOT NULL,      -- 'view', 'bet', 'drop_reaction', 'alert_click'
    user_id TEXT,
    source TEXT,                   -- 'markets_cmd', 'daily_drop', 'price_alert'
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engagement_market ON market_engagement(market_id, event_type);
```

### Feedback Loop

Weekly, compute an "engagement score" per market: `views * 1 + bets * 5 + reactions * 2`. Markets similar to high-engagement ones (same category, similar price range) get a curation score boost. This is a stretch goal — implement the tracking first, the feedback loop later.

---

## Rendering Requirements

All new card renders MUST use the unified pipeline:

- **Style tokens:** `atlas_style_tokens.py` (Tokens class, CSS variables)
- **HTML engine:** `atlas_html_engine.py` (`wrap_card()`, `render_card()`, `build_header_html()`, `build_data_grid_html()`, `build_footer_html()`)
- **Prediction renderer:** `casino/renderer/prediction_html_renderer.py` (extend with new card types)
- **Fonts:** Outfit (display) + JetBrains Mono (data)
- **Colors:** YES = `Tokens.WIN` (#4ADE80), NO = `Tokens.LOSS` (#F87171), category colors from `CATEGORY_COLORS` dict
- **Dimensions:** 480px width, 2x DPI, `domcontentloaded` wait

### New Render Functions Needed

| Function | Purpose |
|----------|---------|
| `render_curated_list_card(markets, page, total)` | 10-market composite with community sentiment |
| `render_daily_drop_card(spotlight, supporting, community, leaderboard)` | Daily Drop post card |
| `render_price_alert_card(market, old_price, new_price, holders)` | Price movement alert |

These go in `prediction_html_renderer.py` alongside the existing render functions.

---

## Files Modified

| File | Changes |
|------|---------|
| `polymarket_cog.py` | Add scoring engine to sync loop, daily drop task, /markets revamp, price alerts, engagement tracking |
| `casino/renderer/prediction_html_renderer.py` | Add 3 new render functions for curated list, daily drop, price alert cards |
| `flow_economy.db` (via `DB_PATH` constant) | 4 new tables: `curated_scores`, `daily_drops`, `price_snapshots`, `market_engagement` |

### Existing Code to Reuse

| What | Where | How |
|------|-------|-----|
| `PolymarketClient` | `polymarket_cog.py` | Existing API client — no changes needed |
| `_sync_markets()` | `polymarket_cog.py` | Hook scoring engine into end of sync |
| `render_card()` / `wrap_card()` | `atlas_html_engine.py` | Standard render pipeline |
| `build_header_html()` / `build_data_grid_html()` | `atlas_html_engine.py` | Reuse for card structure |
| `Tokens` | `atlas_style_tokens.py` | All CSS variables |
| `CATEGORY_COLORS` | `prediction_html_renderer.py` | Category badge colors |
| `flow_wallet.credit()` / `debit()` | `flow_wallet.py` | Bet transactions (already used) |
| `get_persona()` | `echo_loader.py` | Gemini system prompt for daily drop |

---

## Pre-existing Bug to Fix

Line ~2069 of `polymarket_cog.py` queries `SELECT question FROM prediction_markets` but the table schema defines `title`, not `question`. Fix this to use `title` during implementation.

## Category Diversity Policy

One consolidated rule applied everywhere:
- **Scoring pool:** `category_rarity` signal penalizes over-represented categories (soft pressure)
- **Display set (10 markets):** Hard cap of max 2 per category
- **Daily Drop shortlist (30 markets):** Hard cap of max 3 per category
- **Daily Drop final (5 markets):** Gemini instructed to maximize diversity; hard cap of max 1 per category in the final 5

## Verification Plan

1. **Scoring engine:** Run sync, query `curated_scores` table, verify scores distribute reasonably and dedup works (1 per event_id)
2. **Rotation:** Call `/markets` multiple times, verify different selections each time with no immediate repeats
3. **Daily drop:** Trigger manually, verify Gemini returns valid JSON, card renders correctly, posts to channel
4. **Price alerts:** Insert fake price snapshots with >10pp delta, verify alert fires and renders
5. **Engagement:** Place a bet via drilldown, verify `market_engagement` row inserted
6. **Card renders:** Run `test_renders/` verification script against all 3 new card types
7. **Category diversity:** With 30+ markets loaded, verify no category appears >2 times in a 10-market view
