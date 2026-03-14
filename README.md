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
- **Gemini AI** (`gemini-2.0-flash`) — Text-to-SQL, analytics narration, persona voice
- **Polymarket** — real-world prediction market data (`polymarket_cog.py`)

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Create a `.env` file:
   ```
   DISCORD_TOKEN=your_discord_bot_token
   GEMINI_API_KEY=your_gemini_api_key
   ADMIN_USER_IDS=comma_separated_discord_ids
   ORACLE_DB_PATH=path_to_tsl_history.db
   ```

3. Run the bot:
   ```
   python bot.py
   ```

## Current Version

**v2.1.0** — Hub-based UI with interactive views and select menus, Elo sportsbook v3, commissioner control room, draft class explorer, Echo persona system with affinity tracking.
