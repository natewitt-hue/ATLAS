# ATLAS™ — Autonomous TSL League Administration System

Discord bot powering **TSL (The Simulation League)**, a long-running Madden NFL simulation league with ~31 active human-controlled teams.

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
Discord (slash commands, embeds, modals, views)
```

Bot runs locally on Windows. Python 3.14, discord.py with app_commands, cog-based architecture.

## Module System

| Module | Files | Purpose |
|--------|-------|---------|
| **Core** | `bot.py` | Orchestration, cog loading, event routing |
| **Oracle** | `oracle_cog.py`, `analysis.py`, `intelligence.py` | Analytics, predictions, power rankings, weekly recaps |
| **Sentinel** | `sentinel_cog.py` | Rule enforcement, blowout monitor, parity system |
| **Codex** | `codex_cog.py`, `build_tsl_db.py` | History, stats, archive — backed by `tsl_history.db` |
| **Genesis** | `genesis_cog.py`, `ability_engine.py` | Draft, prospects, ability audits *(sidelined)* |
| **Flow** | `economy_cog.py`, `flow_sportsbook.py`, `casino/` | Economy (TSL Bucks), sportsbook, casino games |
| **Echo** | `echo_cog.py`, `echo_loader.py`, `echo_voice_extractor.py` | Commissioner voice/comms via Gemini AI |
| **Commish** | `commish_cog.py`, `setup_cog.py` | Commissioner tools, server provisioning |

## Key Infrastructure

| Component | Description |
|-----------|-------------|
| `reasoning.py` | Gemini AI integration — Text-to-SQL, persona injection, two-phase reasoning |
| `data_manager.py` | MaddenStats API fetcher, pandas transforms, DataFrame cache |
| `tsl_history.db` | SQLite — 11+ tables: games, stats, trades, players, standings, abilities, owner_tenure, player_draft_map, tsl_members |
| `analysis.py` | Stat leaders, power rankings, query routing |
| `intelligence.py` | Advanced analytics — hot/cold streaks, clutch stats, owner profiles |
| `lore_rag.py` | FAISS vector DB for Discord lore/history search |
| `card_renderer.py` | Pillow-based image generation for team/player cards |

## Casino Subsystem (`casino/`)

| File | Game |
|------|------|
| `casino.py` | Lobby, shared infrastructure, modals |
| `casino_db.py` | SQLite layer for balances and ledger |
| `games/blackjack.py` | Blackjack |
| `games/coinflip.py` | Coinflip challenges |
| `games/crash.py` | Crash multiplier game |
| `games/slots.py` | Slot machine |
| `games/sportsbook.py` | *(broken — needs rewrite)* |

## External Integrations

- **MaddenStats API** — `https://mymadden.com/api/lg/tsl` — league data source
- **Gemini AI** (`gemini-2.0-flash`) — Text-to-SQL, analytics narration, voice generation
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

**v1.5.0** — Echo integration wired, Codex v1.4 patched, codebase audit in progress.

## Audit Artifacts

- `FINDINGS.txt` — Pyright static analysis output (693 errors)
- `code_review.md` — Code review notes
- `CLAUDE.md` — Claude Code session briefing document
