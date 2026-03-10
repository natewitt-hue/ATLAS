# WittGPT — TSL Stat Hub

## Architecture

```
Madden Companion App / Snallabot
    │
    │  POST exports
    ▼
Render Cloud  (https://botcall-ln8q.onrender.com)
    │  serves JSON files: standings.json, schedules.json, etc.
    ▼
data_manager.load_all()  →  fetches from Render  →  Discord commands
```

All league data is served from the Render Cloud. There is no `league_data/` local folder.
The bot reads from the cloud on every startup and every `/wittsync`.

## File Structure

```
tsl_bot/
├── bot.py              # Discord bot, slash commands, Gemini integration
├── data_manager.py     # Cloud data fetcher — all DataFrames live here
├── reasoning.py        # Two-phase Gemini reasoning engine (Analyst + WittGPT)
├── intelligence.py     # Draft grades, hot/cold, clutch stats, owner profiles
├── analysis.py         # Stat leaders, power rankings, query router
├── embeds.py           # Discord embed card builders
├── sportsbook.py       # Virtual sportsbook — spread/ML betting
├── lore_rag.py         # FAISS vector DB for Discord lore/history search
├── export_receiver.py  # Flask server for receiving Snallabot exports (optional)
├── rules.py            # League rulebook Q&A
└── sportsbook.db       # SQLite — player balances and bets (auto-created)
```

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Create a `.env` file:
   ```
   DISCORD_TOKEN=your_discord_bot_token
   GEMINI_API_KEY=your_gemini_api_key
   ADMIN_USER_IDS=123456789,987654321   # comma-separated Discord IDs
   ```

3. Run the bot:
   ```
   python bot.py
   ```

Data loads automatically from the Render Cloud on startup. No JSON files needed locally.

## Slash Commands

| Command | Description |
|---|---|
| `/wittsync` | Reload all league data from Render Cloud |
| `/lines` | View this week's betting lines |
| `/bet` | Place a spread or moneyline wager |
| `/wallet` | Check your TSL Bucks balance and pending bets |
| `/grade_bets [week]` | Commissioner: grade all pending bets for a week |
| `/sportsbook_status` | Commissioner: data pipeline health check |

## @ Mention (Natural Language)

Mention the bot for anything:
- `@WittGPT worst QBs this season`
- `@WittGPT Chiefs vs Bengals`
- `@WittGPT who's the biggest spammer`
- `@WittGPT power rankings`
- `@WittGPT is Diddy clutch`

WittGPT will route stat questions through the two-phase reasoning engine
(Gemini writes Python → executes against live DataFrames → WittGPT responds).

## Syncing New Data

Data updates automatically whenever Snallabot exports to the Render Cloud.
To force a manual refresh without restarting, use `/wittsync` in Discord.

## Lore / Discord History

To enable Discord history search:
1. Export your Discord server with DiscordChatExporter
2. Run: `python lore_rag.py --ingest /path/to/json/exports`
3. Place `discord_history.db` alongside `bot.py` to enable Text-to-SQL queries

## Sportsbook

Players start with **1,000 TSL Bucks**. Spreads and moneylines are generated
automatically from team power ratings each week. Commissioner uses `/grade_bets`
after games to settle all pending wagers.
