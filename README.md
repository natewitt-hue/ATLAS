# ATLAS — TSL Intelligence System v1.5

ATLAS is the official AI intelligence system for **The Simulation League (TSL)**, a Madden NFL franchise league. Built with discord.py and Google Gemini AI.

## Architecture

```
bot.py                  # Entry point — cog loader, event handlers, /wittsync
data_manager.py         # MaddenStats API → Pandas DataFrames
reasoning.py            # Two-phase Gemini reasoning engine (analyst code → exec → persona)
analysis.py             # Stat query engine, bar charts, rankings
intelligence.py         # Draft grades, hot/cold tracker, clutch stats, owner profiles
lore_rag.py             # FAISS vector DB for Discord lore semantic search
rules.py                # TSL rulebook with position-change validator
echo_loader.py          # Echo persona system — casual/official/analytical voices
build_tsl_db.py         # Builds tsl_history.db from live API
build_member_db.py      # TSL member registry (join dates, aliases)
ability_engine.py       # Player ability tracker
trade_engine.py         # Trade equity calculator
player_picker.py        # Random player selection engine
card_renderer.py        # Playwright-based HTML-to-image card renderer

# ── Cogs ──
setup_cog.py            # First-run server provisioning (channels, roles)
echo_cog.py             # /echorebuild, /echostatus — persona management
genesis_cog.py          # Trade center, parity/cornerstone system
sentinel_cog.py         # Screenshot-based game rulings via Gemini Vision
oracle_cog.py           # Predictive analytics, power rankings
awards_cog.py           # Season awards voting
codex_cog.py            # Discord archive search (Text-to-SQL via Gemini)
kalshi_cog.py           # Prediction market — Kalshi integration
flow_sportsbook.py      # TSL Sportsbook — spreads, ML, O/U with odds engine

# ── Casino ──
casino/
├── casino.py           # Casino cog — command router
├── casino_db.py        # SQLite balance/wager/ledger DB
└── games/
    ├── blackjack.py    # Interactive blackjack with split/double
    ├── coinflip.py     # PvP coin flip challenges
    ├── crash.py        # Multiplayer crash game
    ├── slots.py        # Slot machine with themed reels
    └── sportsbook.py   # Casino ↔ sportsbook integration

# ── Data ──
echo/                   # Extracted voice personas (casual/official/analytical)
BRANDING/               # ATLAS logo assets
league_data/            # Cached MaddenStats API JSON (auto-refreshed)
faiss_lore_db/          # FAISS index + metadata for lore search
```

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Create `.env`:
   ```
   DISCORD_TOKEN=your_discord_bot_token
   GEMINI_API_KEY=your_gemini_api_key
   ADMIN_USER_IDS=123456789,987654321
   ADMIN_CHANNEL_ID=0
   PREDICTION_MARKET_CHANNEL_ID=0
   ```

3. Run:
   ```
   python bot.py
   ```

Data is fetched live from the MaddenStats API on startup and on `/wittsync`. No manual JSON management needed.

## Key Features

- **Natural Language Stats** — @mention ATLAS with any question; Gemini writes + executes analysis code
- **Echo Personas** — Three voice registers (casual, official, analytical) extracted from real Discord archives
- **Sportsbook** — Automated spread/ML/O/U lines with admin overrides, live betting
- **Casino** — Blackjack, slots, crash, coin flip with persistent balances
- **Lore Search** — Semantic search over 2M+ Discord messages via FAISS
- **Screenshot Rulings** — Gemini Vision analyzes game screenshots for rule enforcement
- **Prediction Markets** — Kalshi-synced markets with TSL Bucks wagering
