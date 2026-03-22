# Oracle v4 Handoff Prompt

> **Purpose:** Paste this into a fresh Claude Code session to continue Oracle improvements.
> **Last updated:** 2026-03-19 (after v3.6.0 Approach B implementation)
> **Working directory:** `C:\Users\natew\Desktop\discord_bot`

---

## What Is Oracle?

Oracle is ATLAS's NL→SQL query system serving ~31 TSL (The Simulation League) members. It converts natural language questions about Madden NFL sim league history into SQL queries against `tsl_history.db`, then synthesizes AI-generated answers.

Entry point: `bot.py`. Python 3.14, discord.py 2.3+, Claude/Gemini AI via `atlas_ai.py`.

## Current Architecture (Post v3.6.0)

### Query Pipeline

```
User Question
    ↓
Name Resolution
  1. resolve_names_in_question() — regex tokenizer + fuzzy_resolve_user()
  2. ai_resolve_names() — AI fallback when regex finds nothing (NEW in v3.6.0)
    ↓
Caller Identity (Discord snowflake → db_username)
  1. resolve_db_username(discord_id) — cached → team lookup → fuzzy match
  2. get_db_username_for_discord_id(discord_id) — direct DB lookup
  3. fuzzy_resolve_user(discord_name) — final fallback
    ↓
Conversation Memory (5 turns, 30-min TTL for codex source)
    ↓
Three-Tier Intent Detection
  Tier 1: Regex pre-flight (18 intents, instant, free) → deterministic SQL
  Tier 2: AI classification (Haiku) → structured SQL
  Tier 3: NL→SQL generation (Sonnet, temp 0.05) → free-form SQL
    ↓
SQL Validation (NEW in v3.6.0)
  validate_sql() checks: status filter, CAST usage, draft table, fullName column
    ↓
SQL Execution (tsl_history.db, WAL mode, 5s timeout)
  If error → self-correct once (Haiku, temp 0.02) with validation hints
    ↓
Answer Synthesis (Haiku, analytical persona)
    ↓
Conversation Persistence → Discord Embed Response
```

### Five Intelligence Modes (via Oracle Hub)

| Modal | Implementation | Data Source | AI |
|-------|---------------|-------------|-----|
| TSL League | `AskTSLModal` in oracle_cog.py | tsl_history.db (NL→SQL) | Sonnet (SQL), Haiku (answer) |
| Open Intel | `_AskWebModal(mode="open")` | Web search (Gemini) | Gemini Flash + search |
| Sports Intel | `_AskWebModal(mode="sports")` | Web search (Gemini) | Gemini Flash + search |
| Player Scout | `PlayerScoutModal` | tsl_history.db (players, abilities) | Haiku (SQL + answer) |
| Strategy Room | `StrategyRoomModal` | DataFrames + web search | Gemini Flash + search |

### Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `oracle_cog.py` | ~4100 | Main cog: hub views, 5 modals, stat commands |
| `codex_cog.py` | ~900 | NL→SQL pipeline: schema, gemini_sql/answer, /ask command |
| `codex_intents.py` | ~1800 | Three-tier intent detection (18 regex + AI classification) |
| `atlas_ai.py` | ~700 | Centralized AI: Claude primary, Gemini fallback |
| `build_member_db.py` | ~1500 | Member registry, alias map, identity resolution |
| `conversation_memory.py` | ~200 | Turn storage, caching, prompt building |
| `build_tsl_db.py` | ~550 | API → SQLite sync (11 tables) |
| `data_manager.py` | ~1100 | Live DataFrames from MaddenStats API |

### Database Schema (tsl_history.db)

**ALL columns stored as TEXT** — must use `CAST(col AS INTEGER)` for math.

| Table | Key Columns | Notes |
|-------|-------------|-------|
| `games` | seasonIndex, homeUser, awayUser, homeScore, awayScore, status, winner_user, loser_user | `status IN ('2','3')` = completed |
| `standings` | teamName, totalWins, totalLosses, ptsFor, ptsAgainst | Current season only |
| `teams` | abbrName, nickName, userName, ovrRating | Current franchise snapshot |
| `offensive_stats` | fullName, teamName, seasonIndex, passYds, rushYds, recYds, etc. | Per-game, no status column |
| `defensive_stats` | fullName, teamName, seasonIndex, defTotalTackles, defSacks, etc. | Per-game, no status column |
| `team_stats` | teamName, seasonIndex, off/defTotalYds, tODiff | Per-game team aggregates |
| `trades` | team1Name, team2Name, status, team1Sent, team2Sent | status = approved/denied/pending |
| `players` | firstName, lastName (NO fullName!), pos, dev, devTrait, teamName | Current roster |
| `player_abilities` | rosterId, title, description, teamName | X-Factor/Superstar |
| `owner_tenure` | teamName, userName, seasonIndex | Who owned which team when |
| `player_draft_map` | drafting_team, drafting_season, draftRound, draftPick | Draft origins (NOT players.teamName) |

### Identity Resolution

**Source of truth:** `tsl_members` table with 73 entries (31 active, 42 historical).

**Alias map** (`get_alias_map()`): Maps lowercased db_username, discord_username, nickname, display_name, psn, xbox → db_username. Dynamically resolves NULL db_username members via teams table JOIN.

**Members with NULL db_username** (auto-resolved at startup via `sync_db_usernames_from_teams()`):
- DcNation_21 (CAR), Topshotta338 (NE), rissa (NO), bigmizz716 (MIA), BurrowsMVP9 (CLE), BabaYaga (PIT), Max (TEN), nickpapura23 (SEA)
- Unresolvable (no team): jbrks2011, Pam_TSL, TheBabado

**Identity refresh:** `refresh_codex_identity()` reloads KNOWN_USERS and NICKNAME_TO_USER after startup sync.

---

## What Was Done in v3.6.0 (Approach B)

### Changes Made

1. **Fixed missing `await` on `build_conversation_block`** in oracle_cog.py:3111 — was causing `TypeError: sequence item 0: expected str instance, coroutine found`

2. **Added `refresh_codex_identity()`** to codex_cog.py — reloads alias map after startup sync so dynamically-resolved db_usernames are visible

3. **Fixed 2-char name extraction gate** in codex_cog.py `resolve_names_in_question()` — changed `len(clean) < 3` to `len(clean) < 2` so "JT", "KG", "BJ" get detected

4. **Added `ai_resolve_names()`** to codex_cog.py — AI-powered name resolution fallback using full member registry as context; only triggered when regex finds nothing

5. **Upgraded Tier 3 SQL generation** from Haiku → Sonnet for better accuracy on complex queries

6. **Added 2 more few-shot examples** for complex JOINs (draft picks by team OVR, away record)

7. **Added `validate_sql()`** to codex_cog.py — checks for missing status filter, CAST usage, draft table misuse, fullName on players table; warnings injected into self-correction prompt

8. **Merged AskOpen + SportsIntel modals** into `_AskWebModal` with mode parameter — reduced ~70 lines of duplicated code

### Problems Solved

- Name resolution now catches 2-char nicknames (JT, KG, BJ)
- Members with NULL db_username get auto-resolved at startup
- Alias map refreshes after sync (no stale cache)
- AI fallback catches semantic references regex can't handle
- SQL generation uses Sonnet for better complex query accuracy
- Self-correction gets targeted validation hints

---

## Approach C Roadmap (What Remains)

### C1: Unified Modal Architecture

**Problem:** 5 modal classes with duplicated boilerplate (defer, try/except, embed formatting). PlayerScoutModal and StrategyRoomModal have their own inline prompt templates.

**Solution:** Create `_BaseIntelModal` with shared lifecycle:
- `on_submit()` handles defer, error catching, embed formatting
- Subclasses implement `_generate_answer(question, interaction)` only
- Shared embed builder with mode-specific title/color/footer

**Files:** oracle_cog.py

### C2: Query Caching / Memoization

**Problem:** Identical questions re-run the full AI pipeline every time. No deduplication.

**Solution:** LRU cache keyed on `(normalized_question, caller_db)` with 5-minute TTL.
- Cache SQL + rows + answer at the Tier 3 level
- Tier 1/2 are already fast enough to not need caching
- Clear cache on `sync_tsl_db()` (data changed)

**Files:** codex_cog.py

### C3: Multi-Retry SQL with Progressive Prompting

**Problem:** Current system retries once with a generic "CAST reminder". Complex failures need escalation.

**Solution:** Three-attempt cascade:
1. **Attempt 1:** Sonnet with standard prompt (current)
2. **Attempt 2 (on failure):** Sonnet with error + validation warnings + full schema (current, but enhanced)
3. **Attempt 3 (on failure):** Opus with error + both previous attempts shown + "explain your reasoning" for debugging

**Files:** codex_cog.py

### C4: Result Citation for Web Search

**Problem:** AskOpen and SportsIntel use Gemini web search but never show sources. Users can't verify answers.

**Solution:** Parse Gemini's `grounding_metadata` from the response to extract cited URLs.
- Add a "Sources" field to the embed with clickable links
- Requires changes to `atlas_ai.generate_with_search()` to return grounding metadata

**Files:** atlas_ai.py, oracle_cog.py

### C5: PlayerScout Upgrade

**Problem:** PlayerScout uses Haiku for SQL generation (may fail on complex queries) and has no self-correction.

**Solution:**
- Upgrade to Sonnet for SQL generation
- Add the same self-correction + validate_sql flow from /ask
- Add team context (who owns which team) so questions like "best players on my team" work
- Consider combining players + player_abilities in a single query for richer reports

**Files:** oracle_cog.py (PlayerScoutModal)

### C6: StrategyRoom Enrichment

**Problem:** StrategyRoom only has top-10 standings + team OVR ratings. No roster, schedule, cap, or trade context.

**Solution:** Inject richer context:
- Salary cap data from `teams` table (capRoomFormatted, capSpentFormatted)
- Recent trades from `trades` table
- Caller's team roster (positions, OVR, dev traits) from `players` table
- Free agent market highlights

**Files:** oracle_cog.py (StrategyRoomModal)

### C7: Conversation Memory Cross-Modal Sharing

**Problem:** Conversation memory is siloed by source ("codex", "casual"). A question asked via /ask doesn't inform a follow-up in Oracle TSL modal.

**Solution:** Use a unified "oracle" source for all TSL-related modals. Consider shared context between AskTSL and PlayerScout (both query the same DB).

**Files:** conversation_memory.py, codex_cog.py, oracle_cog.py

---

## Critical Rules (from CLAUDE.md)

- **Bump `ATLAS_VERSION` in `bot.py`** before every push
- **Use `get_persona()` from `echo_loader.py`** for AI system prompts
- **Use `atlas_ai.generate()` for all AI calls** — never call SDKs directly from cogs
- **ALL DB columns are TEXT** — always CAST for math
- **Completed games:** `status IN ('2','3')`, NOT just `'3'`
- **Draft queries:** Use `player_draft_map`, NOT `players.teamName`
- **weekIndex:** 0-based in API, 1-based in `CURRENT_WEEK`
- **Owner resolution:** Use `_resolve_owner()` fuzzy lookup

## Test Cases (Regression Suite)

### Name Resolution
- "What's JT's record?" → TrombettaThanYou
- "Ron vs JT" → Ronfk vs TrombettaThanYou
- "KG's all time record" → The_KG_518
- "How is Topshotta doing?" → auto-resolved via NE team
- "What's BJ's record this season?" → Bjohnson919
- "Who's the Lions guy?" → TheWitt (AI resolution)
- "my record" (from any registered member) → caller resolved

### SQL Generation (Tier 3)
- "Best draft picks by team with most wins" → complex JOIN works
- "Who has the most passing TDs across all seasons?" → correct aggregation with CAST
- "Compare Ron and JT's records season by season" → multi-row result
- "Biggest blowout in TSL history?" → correct MAX(homeScore - awayScore) with CAST
- "Which team has the best defense this season?" → defensive_stats aggregation

### Non-TSL Modals
- Open Intel: "What year did Tom Brady retire?" → web search answer
- Sports Intel: "NFL draft 2026 prospects" → web search answer with sports context
- Player Scout: "Best X-Factor players on the Lions" → correct SQL from players + abilities
- Strategy Room: "Should I trade for a WR?" → contextualized advice with standings
