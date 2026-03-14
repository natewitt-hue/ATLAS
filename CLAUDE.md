# ATLAS — Project Reference Document

> **Last updated:** 2026-03-10
> **Maintainer:** TheWitt (Discord ID: 322498632542846987)
> **Purpose:** Context primer for any Claude session (Code, Cowork, claude.ai) working on ATLAS.
> Update this file after every major session.

---

## 1. What Is This Project?

ATLAS (Autonomous TSL League Administration System) is a Discord bot that serves as the full administrative infrastructure for **The Simulation League (TSL)** — a Madden NFL simulation league with ~31 active human-controlled teams that has been running for 15+ years across 95+ Super Bowl seasons.

ATLAS was formerly called **WittGPT** and was rebranded in March 2026. The bot handles everything: stats, analytics, trade evaluation, rule enforcement, sportsbook/casino economy, historical queries via AI, draft systems, and commissioner communications.

---

## 2. Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.14, Windows local dev |
| Bot framework | discord.py 2.3+ with slash commands (app_commands) |
| AI | Google Gemini 2.0 Flash via `google-genai` SDK |
| Data | Pandas DataFrames (live), SQLite (historical + economy) |
| Image rendering | Pillow (PIL) |
| Vector search | FAISS + sentence-transformers (lore RAG) |
| HTTP | aiohttp, httpx |
| Entry point | `bot.py` (v2.0.0) |
| Dev machine | `C:\Users\natew\Desktop\discord_bot` (Windows, PowerShell) |

---

## 3. Architecture Overview

### Data Flow

```
MaddenStats API (mymadden.com/api/lg/tsl/)
    │
    ▼
data_manager.load_all()  →  Pandas DataFrames  →  Discord commands
    │
    ▼
build_tsl_db.sync_tsl_db()  →  tsl_history.db (SQLite)
build_member_db              →  tsl_members table (identity registry)
```

### Databases

| DB | Size | Purpose |
|----|------|---------|
| `tsl_history.db` | ~16MB | Game history, player stats, member registry, server config |
| `sportsbook.db` | ~2.5MB | Balances, bets, casino economy, affinity scores |
| `TSL_Archive.db` | ~1.2GB | Full Discord chat history archive (for Oracle/Codex queries) |
| `discord_history.db` | — | Internal message/chat history, referenced in `data_manager.py:158` |

### ATLAS Module Map (Conceptual → Code)

| Module | Purpose | Primary Cog File(s) |
|--------|---------|-------------------|
| **Core** | Orchestration, routing | `bot.py`, `setup_cog.py`, `permissions.py` |
| **Sentinel** | Rule enforcement, blowout monitor, compliance | `sentinel_cog.py` |
| **Oracle** | Analytics, stats, power rankings, profiles | `oracle_cog.py` (StatsHubCog) |
| **Genesis** | Trades, roster, dev traits, draft (partial) | `genesis_cog.py` |
| **Flow** | Economy, sportsbook, casino | `flow_sportsbook.py`, `casino/`, `economy_cog.py` |
| **Codex** | History, records, NL→SQL→NL via Gemini | `codex_cog.py` |
| **Echo** | Commissioner voice/persona system | `echo_cog.py`, `echo_loader.py`, `affinity.py` |

### Cog Load Order (order matters)

| # | Extension | Cog Class(es) | Notes |
|---|-----------|--------------|-------|
| 1 | `echo_cog` | EchoCog | MUST load first — personas |
| 2 | `setup_cog` | SetupCog | MUST load second — channels |
| 3 | `flow_sportsbook` | SportsbookCog | Elo-based odds engine v3 |
| 4 | `casino.casino` | CasinoCog | Blackjack, slots, crash, coinflip |
| 5 | `oracle_cog` | StatsHubCog | ~4003 lines |
| 6 | `genesis_cog` | TradeCenterCog, ParityCog, GenesisHubCog | ~2477 lines |
| 7 | `sentinel_cog` | ComplaintCog, ForceRequestCog, GameplayCog, PositionChangeCog, FourthDown, SentinelHubCog | ~2855 lines |
| 8 | `awards_cog` | AwardsCog | Awards & voting |
| 9 | `codex_cog` | CodexCog | Historical AI queries |
| 10 | `polymarket_cog` | — | Prediction markets |
| 11 | `economy_cog` | EconomyCog | Balance ops, payouts, stipends |
| 12 | `commish_cog` | CommishCog | Unified admin commands |

### Core Support Modules

| File | Lines | Purpose |
|------|-------|---------|
| `data_manager.py` | 1004 | Cloud data fetcher — all DataFrames |
| `reasoning.py` | 996 | Two-phase Gemini engine (Analyst → ATLAS) |
| `intelligence.py` | 767 | Draft grades, hot/cold, clutch stats |
| `analysis.py` | 618 | Stat leaders, power rankings, query router |
| `ability_engine.py` | 991 | Player ability/archetype system |
| `trade_engine.py` | 354 | Trade evaluation logic |
| `build_tsl_db.py` | 417 | SQLite DB builder from API data |
| `build_member_db.py` | 1350 | Member registry builder |
| `player_picker.py` | 401 | Team/player autocomplete & picker |
| `card_renderer.py` | 780 | Visual card image generation |
| `lore_rag.py` | 264 | FAISS vector DB for lore search |
| `affinity.py` | 220 | User affinity tracking (sentiment → tone) |
| `permissions.py` | 166 | Centralized permission checks & channel routing |
| `echo_loader.py` | 215 | Persona file loader |

**Total codebase:** ~30,170 lines of Python.

---

## 4. Echo Persona System

Three voice modes loaded from `echo/*.txt` files:

- **casual** — @mentions, banter, general chat
- **official** — rulings, announcements, governance
- **analytical** — stats, recaps, trade analysis

Context is inferred from channel name via `infer_context()`.

The persona is the "foul-mouthed, opinionated Commissioner of TSL" who:
- Always refers to himself in 3rd person as "ATLAS" (never "I" or "me")
- Talks like he's in the group chat — casual, aggressive, specific
- Profanity used naturally but not gratuitously
- Punchy: 2–4 sentences max, no bullet lists, no fluff
- Always cites real names and real numbers
- In rulebook mode: cites exact section numbers, always definitive (LEGAL or ILLEGAL)

### Affinity System

- Tracks per-user sentiment score (-100 to +100)
- Positive interactions: +2, Negative: -3 (asymmetric decay)
- Score shapes ATLAS's tone in Gemini system prompt

---

## 5. Identity Resolution (CRITICAL SYSTEM)

**The core problem:** Discord display names, in-game DB usernames, PSN handles, and nicknames are all different identifiers for the same people. This causes `/ask` queries and historical lookups to fail or misfire.

**The solution:** `tsl_members` table in `tsl_history.db` is the single source of truth.

### Key Functions

- `get_alias_map()` — Returns 88+ entries mapping all name variants to canonical DB usernames
- `get_known_users()` — Returns list for Gemini SQL prompt injection
- `get_db_username_for_discord_id()` — Direct snowflake ID → DB username lookup (no fuzzy matching needed)
- `sync_db_usernames_from_teams()` — Auto-fills missing DB usernames from live teams API on startup
- `validate_db_usernames()` — Logs warnings for registry entries with zero game records

### TSL Member Map (nickname → db_username)

```
JT=TROMBETTATHANYOU, Killa=KillaE94, Nova=PLAYERNOVA1, PNick=PNick12
Ken=KJJ205, Jo=OLIVEIRAYOURFACE, MrCanada=MR_C-A-N-A-D-A, John=AFFINIZE
Jorge=NUTSONJORGE, Witt=TheWitt, Baez=SBAEZ, Rahj=Rahjeet
LTH=DANGERESQUE_2, Chok=ChokolateThunda, Remo=WithoutRemorse, Keem=KEEM
Pope=DoceQuatro24, Bdiddy=BDiddy86, Sharlond=SHARLOND, Ron=RONFK
Hester=Hester2003, Unbeatable=Unbeatable00, Shelly=ShellyShell, Epone=Epone
Stutts=MStutts2799, Airflight=AIRFLIGHT_OC, Strikernaut=Strikernaut
RobbyD=ROBBYD192, Ruck=RUCKDOESWORK, KG=THE_KG_518, Khaled=Khaled
Eric=ERIC, Neff=NEFF
```

Additional identity notes:
- Jordantromberg = JT (db: TrombettaThanYou)
- Jnolte = active member (Jets owner)
- Odyssey63 = Neff (db: NEFF)
- Bjohnson919 = BJ (Vikings, db: Bjohnson919) — NOT JB3v3 (separate departed member)
- A1_Shaun nickname = Tuna
- Signman = Jason Bogle (league owner, TB)
- Unresolved: Pam, TheBabado, jbrks2011

**31 confirmed active players. BUF is open. Potentially one more open spot.**

---

## 6. MaddenStats API — Quirks & Gotchas

These are hard-won lessons. Violating any of these causes silent data bugs.

| Gotcha | Detail |
|--------|--------|
| `/games/schedule` | Returns current week only — no filtering parameter needed or available |
| `weekIndex` | 0-based in API, but `CURRENT_WEEK` in `data_manager` is 1-based. Off-by-one trap. |
| Completed games | Filter with `status IN ('2','3')`, NOT `status='3'` alone. Using only '3' silently drops real game results. |
| Full roster data | OVR, devTrait, ability1–6 only available from `/export/players`. Stat-leader endpoints cannot substitute. |
| Ability assignments | Use `/export/playerAbilities` endpoint |
| `devTrait` int mapping | 0=Normal, 1=Star, 2=Superstar, 3=Superstar X-Factor |
| Ability dev budgets | Star=1B, Superstar=1A+1B, XFactor=1S+1A+1B, C-tier unlimited |
| Dual-attribute checks | Use OR logic, not AND |
| Draft history | Credit players to the team that drafted them (first statistical appearance), NOT current team. 10 teams have changed ownership across seasons — naive team-level queries are inaccurate. |
| Owner resolution | API usernames have underscores/case mismatches vs DB. Use `_resolve_owner()` fuzzy lookup. |

---

## 7. Discord API Constraints (Learned the Hard Way)

| Constraint | Detail |
|-----------|--------|
| `view=None` | Cannot be passed as keyword argument to `followup.send()` — must omit entirely |
| Modal latency | Modals require `defer()` for Gemini API calls (>3s timeout) |
| Clickable text in embeds | Impossible in Discord — use cascading select menus instead |
| Select menu options | Capped at 25 options max |
| `@discord.ui.select` | Requires `options=[]` parameter even if populated dynamically — omitting causes TypeError |
| Command name collisions | If two cogs register the same slash command name, the second silently fails. Caught with `/abilityaudit` collision between sentinel and genesis. |
| Ephemeral vs public | Drill-downs should be ephemeral (clicking user only); hub landing embeds are public |
| Startup guard | Use `_startup_done` flag to prevent duplicate `load_all()` on reconnect |

---

## 8. Design Patterns & Conventions

- **Cog pattern** — Each module is a `discord.py` `commands.Cog` loaded via `setup_hook()` with try/except guards
- **Hub views** — Interactive button panels replace flat slash commands (v2.0 architecture)
- **Soft fallbacks** — Optional modules use try/except imports; missing = graceful degradation
- **Channel routing** — `setup_cog.py` defines `REQUIRED_CHANNELS` manifest. Commands use `require_channel()` decorator. Lazy resolver functions call `setup_cog.get_channel_id()` with ImportError fallbacks (rename-proof).
- **Admin delegation** — `commish_cog.py` delegates to `_impl` methods in other cogs
- **Thread executor** — Blocking Gemini calls wrapped in `loop.run_in_executor()`
- **Startup guard** — `_startup_done` flag prevents duplicate loads on reconnect
- **Persona routing** — Oracle, Codex, and Sentinel cogs call `get_persona()` from echo_loader (not hardcoded `ATLAS_PERSONA`)
- **DB schema injection** — `_build_schema()` dynamically includes `dm.CURRENT_SEASON` so Gemini always has current context

---

## 9. Permission Model

| Function | Who |
|----------|-----|
| `is_commissioner()` | env `ADMIN_USER_IDS`, "Commissioner" role, or guild admin |
| `is_tsl_owner()` | "TSL Owner" role (soft fallback if role doesn't exist) |
| `commissioner_only()` | Decorator for admin-only commands |
| `require_channel()` | Decorator for channel-restricted commands |

---

## 10. Channel Routing

`setup_cog.py` defines 5 channel categories auto-created on guild join:

- **ATLAS — Command Center** — admin-chat, bot-logs, ask-atlas
- **ATLAS — Oracle** — power-rankings, announcements, game-results
- **ATLAS — Genesis** — roster-moves, trades, dev-upgrades
- **ATLAS — Sentinel** — compliance, force-request
- **ATLAS — Casino** — casino-ledger, blackjack, slots, crash, coinflip, sportsbook, prediction-markets

---

## 11. Casino Subsystem

```
casino/
├── casino.py              # Main CasinoCog (488 lines)
├── casino_db.py           # DB operations (727 lines)
├── games/
│   ├── blackjack.py
│   ├── coinflip.py
│   ├── crash.py
│   ├── slots.py
│   └── sportsbook.py
├── renderer/
│   ├── card_renderer.py
│   ├── casino_card_renderer.py
│   ├── ledger_renderer.py
│   ├── cards/              # Card image assets
│   └── fonts/              # Font files
└── assets/
```

### Sportsbook Odds Engine v3

Unified `_power_rating()` composite per team:
- Season W% ×10.0 → ±5.0 pts (primary — current form)
- Power rank ×0.12 → ±1.86 pts
- OVR ×0.15 (centered at 78) → ±~1.5 pts
- Offensive rank ×0.12 → ±1.86 pts
- Defensive rank ×0.12 → ±1.86 pts
- Career W% ×4.0 → ±1.0 pts (anchor — no longer dominant)

Admin overrides via `line_overrides` table. Engine runs first, admin overrides win.

---

## 12. Environment Variables

| Var | Required | Purpose |
|-----|----------|---------|
| `DISCORD_TOKEN` | Yes | Bot token |
| `GEMINI_API_KEY` | Yes | Google Gemini API |
| `ADMIN_USER_IDS` | Yes | Comma-separated Discord IDs |
| `ORACLE_DB_PATH` | No | Path to TSL_Archive.db |
| `FORCE_REQUEST_CHANNEL` | No | Channel ID |
| `TRADE_LOG_CHANNEL_ID` | No | Channel name/ID |

---

## 13. TSL Super Bowl History (I–XCV)

**Ring count leaders:** JT (14), Killa (12), Nova (5), Ken (5), Jo (4), MrCanada (4), John (4), Jorge (4), Baez (3), LTH (3), Rahj (3), Chok (3), PNick (5), Witt (3), Remo (2), Keem (2)

<details>
<summary>Full SB Winner List (click to expand)</summary>

I-PNick, II-Chok, III-Hester, IV-Unbeatable, V-Shelly, VI-Witt, VII-Killa, VIII-Witt, IX-Epone, X-Remo, XI-Chok, XII-PNick, XIII-Remo, XIV-Witt, XV-PNick, XVI-Strikernaut, XVII-Killa, XVIII-Killa, XIX-Bdiddy, XX-Rahj, XXI-PNick, XXII-Killa, XXIII-PNick, XXIV-Pope, XXV-Killa, XXVI-Stutts, XXVII-Jorge, XXVIII-LTH, XXIX-Killa, XXX-Airflight, XXXI-Jo, XXXII-Jorge, XXXIII-LTH, XXXIV-Killa, XXXV-RobbyD, XXXVI-JT, XXXVII-Jorge, XXXVIII-JT, XXXIX-Ken, XL-JT, XLI-LTH, XLII-Rahj, XLIII-Rahj, XLIV-Sharlond, XLV-Ruck, XLVI-Baez, XLVII-MrCanada, XLVIII-Ken, XLIX-MrCanada, L-Ken, LI-MrCanada, LII-Ken, LIII-Killa, LIV-JT, LV-MrCanada, LVI-JT, LVII-Nova, LVIII-Baez, LIX-Baez, LX-John, LXI-Ken, LXII-John, LXIII-John, LXIV-John, LXV-JT, LXVI-Jo, LXVII-JT, LXVIII-JT, LXIX-Jo, LXX-Keem, LXXI-KG, LXXII-Jo, LXXIII-Killa, LXXIV-Jo, LXXV-Nova, LXXVI-Nova, LXXVII-Khaled, LXXVIII-Eric, LXXIX-JT, LXXX-Keem, LXXXI-Neff, LXXXII-Killa, LXXXIII-Nova, LXXXIV-Nova, LXXXV-Killa, LXXXVI-Jorge, LXXXVII-JT, LXXXVIII-Nova, LXXXIX-JT, XC-Killa, XCI-JT, XCII-Chok, XCIII-JT, XCIV-JT, XCV-Ron

</details>

---

## 14. Current Backlog (Priority Order)

### Active Workstreams
1. **Command architecture overhaul** — Restructure slash commands, permissions, channel routing (prompt drafted)
2. **Echo integration** — Swap hardcoded `ATLAS_PERSONA` for `get_persona()` calls in oracle_cog, codex_cog, sentinel_cog, reasoning.py

### Module Conversion Queue
1. Auto-push power rankings
2. Auto-push weekly recap
3. Persistent sportsbook card
4. Auto-trigger ability audit
5. Persistent casino lobby
6. Auto-push dev audit + lottery standings

### Pending Work
- Wire `tsl_members` registry into `bot.py`, `history_cog.py` (now `codex_cog.py`), `stats_hub_cog.py` (now `oracle_cog.py`)
- Resolve 3 unknown member identities: Pam, TheBabado, jbrks2011
- ATLAS Genesis™ buildout (sidelined — commissioner voice style extractor already built as precursor)
- TSL rulebook gaps: draft rules, free agency/waiver system, anti-tanking measures, pre-snap movement exploits

---

## 15. Key Dependencies

```
discord.py>=2.3.0
google-genai>=0.8.0
pandas>=2.0.0
numpy>=1.24.0
matplotlib>=3.7.0
seaborn>=0.12.0
faiss-cpu>=1.7.0
sentence-transformers>=2.2.0
aiosqlite>=0.19.0
aiohttp>=3.9.0
httpx>=0.25.0
Pillow>=10.0.0
```

---

## 16. Development Workflow & Preferences

- **Iterative build style:** stress-test design on paper before writing code, then implement and correct in rounds
- **Prefers simple and elegant solutions;** defers low-stakes technical decisions to Claude
- **Audits codebase before major releases;** quarantines files before permanent deletion to verify clean boot
- **Uses PowerShell** for file operations; prefers terminal/shell for debugging
- **Render outputs should be visually verified before delivery** — flagged explicitly for image-generation work
- **Handoff prompts** used to bridge context window limits across long sessions
- **Review-first protocol for Claude Code:** read existing files → produce written review → wait for approval → then write code

---

## 17. File Naming Conventions

When modules were consolidated during the v2.0 rebrand, some files were renamed:

| Old Name | New Name | Notes |
|----------|----------|-------|
| `history_cog.py` | `codex_cog.py` | Class renamed `CodexCog` |
| `stats_hub_cog.py` | `oracle_cog.py` | Class is still `StatsHubCog` |
| Various individual cogs | Consolidated into sentinel/genesis | complaint, forcerequest, positionchange, ability, gameplay, fourthdown all merged |

Dead files should be in `QUARANTINE/` folder — do not reference or import them.

---

## 18. How to Use This File

**Claude Code:** Place this file as `CLAUDE.md` in the project root (`C:\Users\natew\Desktop\discord_bot\`). Claude Code reads it automatically at session start.

**Claude.ai Projects:** Add as project knowledge. It loads into every conversation automatically.

**Cowork:** Upload at session start before assigning tasks.

**Keeping it current:** After any major session that changes architecture, renames files, adds modules, or shifts priorities — update the relevant sections. The file is only useful if it reflects reality.
