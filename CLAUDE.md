# ATLAS — Claude Code Instructions

ATLAS (Autonomous TSL League Administration System) is a Discord bot serving as the full admin infrastructure for The Simulation League (TSL) — a Madden NFL sim league with ~31 active teams across 95+ Super Bowl seasons. Entry point: `bot.py`. Python 3.14, discord.py 2.3+, Google Gemini 2.0 Flash via `google-genai`, Pandas DataFrames, SQLite, Playwright (HTML→PNG rendering).

---

## Critical Rules

I may be running multiple Claude Code sessions on this repo simultaneously.  Ask me if I will be using multiple sessions before starting any planning, so you can adjust the plan accordingly.  This is called being in 'Sicko Mode'
My repo is at C:\Users\natew\Desktop\discord_bot

If I answer that yes, I want to use multiple sessions, aka 'Sicko Mode' then before making changes:
1. List the specific files you plan to touch
2. Wait for my approval before editing
3. Never edit files claimed by another session

When planning a task, break it into independent workstreams that can run
in parallel across sessions. Label each workstream with:
- Workstream name
- Files it touches (exclusive — no overlap)
- Dependencies on other workstreams (if any)
- Estimated steps

I'll assign each workstream to a different session. Design workstreams
so they can merge cleanly with no conflicts.


### Code Rules

- **Bump `ATLAS_VERSION` in `bot.py` before every push.** Minor bump for features (2.1.0 → 2.2.0), patch for fixes (2.1.0 → 2.1.1).
- **Use `get_persona()` from `echo_loader.py`** for AI system prompts — never hardcode `ATLAS_PERSONA`.
- **Use `atlas_ai.generate()` for all AI calls** — Claude primary, Gemini fallback. Handles `run_in_executor` internally. Never call Gemini/Claude SDKs directly from cogs.
- **Use `_startup_done` flag** to prevent duplicate `load_all()` on reconnect.
- **`_build_schema()` dynamically includes `dm.CURRENT_SEASON`** so Gemini always has current season context.
- **Dead files belong in `QUARANTINE/`** — do not reference or import them.
- **When creating a new `.py` module, add it to the relevant nightly audit task on the same commit.** Audit tasks live at `C:\Users\natew\.claude\scheduled-tasks\`. Map new files to the correct day: Flow/Economy/Sportsbook → Monday, Casino/Rendering → Tuesday, Oracle/Analytics → Wednesday, Genesis/Sentinel → Thursday, AI/Codex/Echo → Friday, Core Infrastructure → Saturday. Sunday auto-covers all files. Update both the `SKILL.md` file list and the task `description` field via `mcp__scheduled-tasks__update_scheduled_task`.

### MaddenStats API Gotchas

These are hard-won lessons. Violating any causes silent data bugs.

| Rule | Detail |
|------|--------|
| `/games/schedule` | Returns current week only — no filtering parameter needed or available |
| `weekIndex` | 0-based in API, but `CURRENT_WEEK` in `data_manager` is 1-based. Off-by-one trap. |
| Completed games | Filter with `status IN ('2','3')`, NOT `status='3'` alone. Using only '3' silently drops results. |
| Full roster data | OVR, devTrait, ability1–6 only from `/export/players`. Stat-leader endpoints cannot substitute. |
| Ability assignments | Use `/export/playerAbilities` endpoint |
| `devTrait` mapping | 0=Normal, 1=Star, 2=Superstar, 3=Superstar X-Factor |
| Ability budgets | Star=1B, Superstar=1A+1B, XFactor=1S+1A+1B, C-tier unlimited |
| Dual-attribute checks | Use OR logic, not AND |
| Draft history | Credit players to the team that drafted them (first statistical appearance), NOT current team. |
| Owner resolution | API usernames have underscores/case mismatches. Use `_resolve_owner()` fuzzy lookup. |

### Discord API Constraints

| Constraint | Detail |
|-----------|--------|
| `view=None` | Cannot be passed as keyword arg to `followup.send()` — must omit entirely |
| Modal latency | Modals require `defer()` for Gemini calls (>3s timeout) |
| Embed clickability | No clickable text in embeds — use cascading select menus instead |
| Select menus | Capped at 25 options. `@discord.ui.select` requires `options=[]` even if populated dynamically. |
| Command collisions | Two cogs with same slash command name → second silently fails |
| Ephemeral vs public | Drill-downs = ephemeral; hub landing embeds = public |

### Identity Resolution

`tsl_members` table in `tsl_history.db` is the **single source of truth** for mapping Discord names → in-game DB usernames. Key functions in `build_member_db.py`:
- `get_alias_map()` — 88+ entries mapping all name variants to canonical DB usernames
- `get_known_users()` — returns list for Gemini SQL prompt injection
- `get_db_username_for_discord_id()` — snowflake ID → DB username (no fuzzy matching)

Full member map is in the memory system (`domain_member_map.md`).

---

## Architecture

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

### Module Map

| Module | Purpose | Cog File(s) |
|--------|---------|-------------|
| **Core** | Orchestration, routing | `bot.py`, `setup_cog.py`, `permissions.py` |
| **AI** | Centralized AI client — Claude primary, Gemini fallback | `atlas_ai.py` |
| **Sentinel** | Rule enforcement, blowout monitor, compliance | `sentinel_cog.py` |
| **Oracle** | Analytics, stats, power rankings, profiles | `oracle_cog.py` (class: StatsHubCog) |
| **Genesis** | Trades, roster, dev traits, draft | `genesis_cog.py` |
| **Ability Engine** | Lock & Key ability audit, dev budget enforcement, position change validation | `ability_engine.py` |
| **Flow** | Economy, TSL sportsbook, casino, live engagement | `flow_sportsbook.py`, `casino/`, `economy_cog.py`, `flow_live_cog.py` |
| **Flow Store** | Store engine — item effects, purchases | `flow_store.py`, `store_effects.py` |
| **Flow Subsystem** | Wallet, audit, events, wager registry | `flow_wallet.py`, `flow_audit.py`, `flow_events.py`, `wager_registry.py` |
| **Real Sportsbook** | Real NFL/NBA betting with live ESPN odds | `real_sportsbook_cog.py`, `sportsbook_core.py`, `espn_odds.py` |
| **Boss** | Visual commissioner control room — replaces `/commish` subcommands | `boss_cog.py` |
| **Codex** | History, records, NL→SQL→NL via AI | `codex_cog.py` |
| **Echo** | Commissioner voice/persona system | `echo_cog.py`, `echo_loader.py`, `affinity.py` |
| **Render** | Unified HTML→PNG card pipeline | `atlas_style_tokens.py`, `atlas_html_engine.py` |

### Rendering Stack

All card renders use a single pipeline:

| Component | File | Purpose |
|-----------|------|---------|
| Style Tokens | `atlas_style_tokens.py` | Single source of truth for colors, fonts, spacing, layout |
| HTML Engine | `atlas_html_engine.py` | Page pool + `render_card()` + `wrap_card()` |
| Casino Games | `casino/renderer/casino_html_renderer.py` | Blackjack, Slots, Crash, Coinflip, Scratch |
| Highlights | `casino/renderer/highlight_renderer.py` | Jackpot, PvP, Crash LMS, Prediction, Parlay |
| Flow Live | `casino/renderer/session_recap_renderer.py`, `pulse_renderer.py` | Session Recap, Pulse Dashboard |
| Predictions | `casino/renderer/prediction_html_renderer.py` | Market List, Detail, Bet, Portfolio, Resolution |
| Trade | `card_renderer.py` | Trade card |
| Ledger | `casino/renderer/ledger_renderer.py` | Transaction ledger |
| Hub Cards | `flow_cards.py`, `sportsbook_cards.py` | Flow Hub, Sportsbook Hub, Stats Card |

Pipeline: Build HTML body → `wrap_card(body, status)` → `render_card(html)` → PNG bytes
Width: 700px · DPI: 2x · Wait: `domcontentloaded` · Pool: 4 pre-warmed pages

**Quarantined (do not import):** `QUARANTINE/atlas_card_renderer.py` (Pillow hub card renderer, replaced), `QUARANTINE/card_renderer.py` (legacy Pillow casino renderer, superseded by HTML v6)

### Cog Load Order (order matters)

| # | Extension | Notes |
|---|-----------|-------|
| 1 | `echo_cog` | MUST load first — personas |
| 2 | `setup_cog` | MUST load second — channels |
| 3 | `flow_sportsbook` | Elo-based odds engine v3 |
| 4 | `casino.casino` | Blackjack, slots, crash, coinflip |
| 5 | `oracle_cog` | Stats hub |
| 6 | `genesis_cog` | Trade center, parity, genesis hub |
| 7 | `sentinel_cog` | Complaints, force requests, gameplay, 4th down |
| 8 | `awards_cog` | Awards & voting |
| 9 | `codex_cog` | Historical AI queries |
| 10 | `polymarket_cog` | Prediction markets |
| 11 | `economy_cog` | Balance ops, payouts, stipends |
| 12 | `flow_store` | Store engine — no UI, Phase 1 |
| 13 | `flow_live_cog` | Live engagement — pulse dashboard, highlights, recaps |
| 14 | `real_sportsbook_cog` | Real NFL/NBA sportsbook with ESPN live odds |
| 15 | `boss_cog` | Visual commissioner control room (`/boss`) |

### Databases

| DB | Purpose |
|----|---------|
| `tsl_history.db` | Game history, player stats, member registry, server config |
| `sportsbook.db` | Balances, bets, casino economy, affinity scores |
| `flow.db` | TSL sportsbook bets, Flow economy transactions |
| `flow_economy.db` | Flow store purchases, wallet ledger |
| `TSL_Archive.db` | Full Discord chat history archive (Oracle/Codex queries) |

### Environment Variables

| Var | Required | Purpose |
|-----|----------|---------|
| `DISCORD_TOKEN` | Yes | Bot token |
| `ANTHROPIC_API_KEY` | Yes (primary) | Claude API — primary AI provider via `atlas_ai.py` |
| `GEMINI_API_KEY` | Yes (fallback) | Gemini API — fallback provider + Google Search |
| `ADMIN_USER_IDS` | Yes | Comma-separated Discord IDs |
| `ORACLE_DB_PATH` | No | Path to TSL_Archive.db |
| `FORCE_REQUEST_CHANNEL` | No | Channel ID |
| `TRADE_LOG_CHANNEL_ID` | No | Channel name/ID |

---

## Design Patterns

- **Cog pattern** — each module is a `commands.Cog` loaded via `setup_hook()` with try/except guards
- **Hub views** — interactive button panels replace flat slash commands (v2.0 architecture)
- **Soft fallbacks** — optional modules use try/except imports; missing = graceful degradation
- **Channel routing** — `setup_cog.py` defines `REQUIRED_CHANNELS`. Commands use `require_channel()` decorator. Lazy resolvers call `setup_cog.get_channel_id()` with ImportError fallbacks.
- **Admin delegation** — `commish_cog.py` delegates to `_impl` methods in other cogs
- **Permission model** — `is_commissioner()` checks env `ADMIN_USER_IDS`, "Commissioner" role, or guild admin. `is_tsl_owner()` checks "TSL Owner" role. Both have decorator forms.

---

## Agent Roster

All Claude Code subagents are named. **When a new agent type is used for the first time, assign it a name (mythical/superhero for high-power agents, human for utility agents) and add it to this table before the session ends.**

### Tier 1 — Mythical / Superhero (broad scope, high power)

| Agent Type | Name | Role |
|------------|------|------|
| `general-purpose` | **Hermes** | Messenger of gods — goes anywhere, does anything |
| `Explore` | **Argus** | Hundred-eyed giant — sees everything in the codebase |
| `Plan` | **Athena** | Goddess of wisdom and strategic warfare |
| `claude-code-guide` | **Odin** | All-knowing — sacrificed everything for knowledge |
| `superpowers:code-reviewer` | **Themis** | Goddess of justice, order, and law |
| `feature-dev:code-architect` | **Daedalus** | Legendary master architect and builder |
| `feature-dev:code-explorer` | **Perseus** | Hero who ventures deep into unknown territory |
| `feature-dev:code-reviewer` | **Minerva** | Roman goddess of craft and critical judgment |

### Tier 2 — Human (focused, utility-scoped)

| Agent Type | Name | Role |
|------------|------|------|
| `code-simplifier:code-simplifier` | **Mia** | Makes things minimal and clean |
| `pr-review-toolkit:code-reviewer` | **Priya** | Thorough reviewer — catches bugs and smells |
| `pr-review-toolkit:code-simplifier` | **Sam** | Cuts the fat, keeps the core |
| `pr-review-toolkit:comment-analyzer` | **Cora** | Reads between the lines |
| `pr-review-toolkit:pr-test-analyzer` | **Tess** | Tests everything, trusts nothing |
| `pr-review-toolkit:silent-failure-hunter` | **Chase** | Hunts what hides in plain sight |
| `pr-review-toolkit:type-design-analyzer` | **Theo** | Types, contracts, and shape of data |
| `hookify:conversation-analyzer` | **Connie** | Listens to the whole conversation |
| `agent-sdk-dev:agent-sdk-verifier-ts` | **Tyler** | TypeScript SDK specialist |
| `agent-sdk-dev:agent-sdk-verifier-py` | **Petra** | Python SDK specialist |
| `plugin-dev:agent-creator` | **Adam** | Brings new agents into existence |
| `plugin-dev:plugin-validator` | **Val** | Validates structure and contracts |
| `plugin-dev:skill-reviewer` | **Skye** | Quality inspector for skills |
| `statusline-setup` | **Dot** | Configures the status line — small but precise |

> **Naming convention:** Tier 1 (mythical/superhero) = broad scope, orchestration, deep analysis, or cross-cutting concerns. Tier 2 (human) = narrowly scoped, single-purpose, or toolkit utility agents.

---

## Echo Persona

Three voice modes loaded from `echo/*.txt`, selected by `infer_context()` from channel name:
- **casual** — @mentions, banter, general chat
- **official** — rulings, announcements, governance
- **analytical** — stats, recaps, trade analysis

Voice rules: Always 3rd person as "ATLAS" (never "I"/"me"). Punchy — 2–4 sentences max, no bullet lists, no fluff. Cites real names and real numbers. Profanity natural but not gratuitous. In rulebook mode: cites exact section numbers, always definitive (LEGAL or ILLEGAL).
