# ATLAS™ — Autonomous TSL League Administration System

Discord bot powering **TSL (The Simulation League)**, a long-running Madden NFL simulation league with ~31 active human-controlled teams across 95+ Super Bowl seasons.

## Architecture

```
MaddenStats API (mymadden.com/api/lg/tsl)
    │
    │  REST — standings, schedules, rosters, stats, players
    ▼
data_manager.py  →  pandas DataFrames  →  Cog commands / Gemini AI
    │
    ▼
tsl_history.db (SQLite)  ←  build_tsl_db.py
    │
    ▼
Discord (slash commands, embeds, hub views, select menus)
```

Bot runs locally on Windows. Python 3.14, discord.py 2.3+ with app_commands, cog-based architecture.

## Module System

| Module | Files | Purpose |
|--------|-------|---------|
| **Core** | `bot.py`, `setup_cog.py`, `permissions.py`, `constants.py` | Orchestration, cog loading, channel routing, shared config |
| **Oracle** | `oracle_cog.py`, `analysis.py`, `intelligence.py` | Analytics, power rankings, team cards, scouting reports, owner profiles |
| **Sentinel** | `sentinel_cog.py` | Rule enforcement, blowout monitor, compliance, parity system |
| **Codex** | `codex_cog.py`, `build_tsl_db.py`, `build_member_db.py` | Historical NL→SQL→NL queries via Gemini, member identity registry |
| **Genesis** | `genesis_cog.py`, `ability_engine.py`, `trade_engine.py` | Trades, roster management, draft class explorer, ability audits |
| **Flow** | `economy_cog.py`, `flow_sportsbook.py`, `casino/` | Economy (TSL Bucks), Elo-based sportsbook, casino games |
| **Predictions** | `polymarket_cog.py` | Real-world prediction markets via Polymarket — 6-layer garbage filter, AI-curated daily drops, audience-tuned scoring |
| **Echo** | `echo_cog.py`, `echo_loader.py`, `affinity.py` | Commissioner persona system with per-user affinity tracking |
| **Boss** | `boss_cog.py` | Commissioner Control Room — visual hub for all admin commands |
| **Awards** | `awards_cog.py` | Awards and voting system |

## Key Infrastructure

| Component | Description |
|-----------|-------------|
| `reasoning.py` | Two-phase Gemini engine (Analyst → ATLAS persona) |
| `data_manager.py` | MaddenStats API fetcher, pandas transforms, DataFrame cache |
| `tsl_history.db` | SQLite — games, stats, trades, players, standings, abilities, owner_tenure, player_draft_map, tsl_members |
| `sportsbook.db` | SQLite — balances, bets, casino economy, affinity scores |
| `analysis.py` | Stat leaders, power rankings, query routing, context builder |
| `intelligence.py` | Hot/cold streaks, clutch stats, draft grades, owner profiles |
| `roster.py` | Owner assignment system with team/conference lookups |
| `player_picker.py` | Team/player autocomplete and picker |
| `card_renderer.py` | Pillow-based image generation for team/player cards |
| `lore_rag.py` | FAISS vector DB for Discord lore/history search |

## Casino Subsystem (`casino/`)

| File | Purpose |
|------|---------|
| `casino.py` | Lobby, shared infrastructure, modals |
| `casino_db.py` | SQLite layer for balances and ledger |
| `games/blackjack.py` | Blackjack |
| `games/coinflip.py` | Coinflip challenges |
| `games/crash.py` | Crash multiplier game |
| `games/slots.py` | Slot machine |
| `games/sportsbook.py` | Sportsbook betting engine |
| `renderer/` | Card, casino card, and ledger renderers with assets |

## External Integrations

- **MaddenStats API** — `https://mymadden.com/api/lg/tsl` — league data source
- **Claude AI** (primary) + **Gemini AI** (fallback) — Text-to-SQL, analytics narration, persona voice via `atlas_ai.py`
- **Polymarket Gamma API** — real-world prediction markets with 6-layer curation filter (`polymarket_cog.py`)

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Create a `.env` file:
   ```
   DISCORD_TOKEN=your_discord_bot_token
   ANTHROPIC_API_KEY=your_claude_api_key
   GEMINI_API_KEY=your_gemini_api_key
   ADMIN_USER_IDS=comma_separated_discord_ids
   ORACLE_DB_PATH=path_to_tsl_history.db
   ```

3. Run the bot:
   ```
   python bot.py
   ```

## Rendering Stack

All visual cards use a unified HTML-to-PNG pipeline:

| Component | File | Purpose |
|-----------|------|---------|
| Style Tokens | `atlas_style_tokens.py` | Single source of truth for colors, fonts, spacing |
| HTML Engine | `atlas_html_engine.py` | Playwright page pool + `render_card()` / `wrap_card()` |
| Casino | `casino/renderer/casino_html_renderer.py` | Blackjack, Slots, Crash, Coinflip, Scratch |
| Predictions | `casino/renderer/prediction_html_renderer.py` | Market List, Detail, Bet, Portfolio, Resolution, Daily Drop |
| Highlights | `casino/renderer/highlight_renderer.py` | Jackpot, PvP, Crash LMS, Prediction, Parlay |

Pipeline: Build HTML body → `wrap_card(body, status)` → `render_card(html)` → PNG bytes (700px, 2x DPI, 4-page pool)

## Current Version

**v6.8.1** — Token migration: standardized all render widths to 700px (fixed 720px violations in trade/ledger cards), added warm text palette tokens (TEXT_WARM, TEXT_WARM_LIGHT, TEXT_WARM_DIM, TEXT_LIGHT, DISCORD_BLUE, SILVER), migrated ~45 hardcoded hex colors across 8 renderer files to use Tokens system.
