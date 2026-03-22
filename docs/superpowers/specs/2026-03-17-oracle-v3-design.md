# Oracle v3 Design Spec

## Context

Oracle v2 is ATLAS's analytics module that converts natural-language questions into SQL queries against `tsl_history.db`. It works (98/98 stress tests pass) but is architecturally fragile: 18 fixed intents across 1,780 lines of regex, sort-direction logic duplicated 8 times, efficiency-vs-volume rules scattered across procedural builders, and conversation memory limited to 5 turns / 30 minutes. The vision for v3 is an omniscient league historian and strategic advisor with permanent memory, open-ended query capability, and advisory depth — while preserving everything that works.

---

## 1. Architecture Comparison Matrix

| Approach | Accuracy | Open-ended | Advisory | Memory | Speed | Cost/day | Maintainability | Verdict |
|----------|----------|-----------|----------|--------|-------|----------|-----------------|---------|
| 1. Full LLM Agent + SQL Tools | High | High | High | Neutral | Medium (2-5s) | ~$0.50 | Medium | Strong but no domain guardrails |
| 2. Hybrid Planner + Executor | High | Medium | Low | Neutral | Medium | ~$0.40 | Low (plan schema bottleneck) | Risks recreating 18-intent rigidity |
| 3. RAG + Conversation Memory | Medium | Low | Low | High | Fast | ~$0.10 | Medium | Solves memory only, not core SQL problem |
| 4. Multi-Agent System | High | High | High | High | Slow (10-20s) | ~$4-8 | Low (debug nightmare) | Over-engineered for 31 users |
| 5. Fine-Tuned Domain Model | Low | Low | Low | Neutral | Fastest | ~$0 | Low (retraining) | Insufficient training data |
| 6. Single Rich Prompt | Medium | Low | Low | Neutral | Fast (1-2s) | ~$0.20 | High | Can't do multi-step queries |
| 7. Code-Gen Agent + QueryBuilder | **Highest** | **High** | **High** | Neutral | Medium (2-5s) | ~$0.50 | **High** | Domain rules enforced in code |
| 8. Tiered Agent + Organic Learning | High | High | Medium | Neutral | Medium | ~$0.50 | Low (learning infra) | Over-engineered at 100 queries/day |
| 9. Dual-Model Pipeline | High | High | High | Neutral | Variable | ~$0.30 | Medium | Cost-optimized but adds routing complexity |

**Selected: Approach 7 (Code-Gen Agent) + Tier 1 preservation + RAG memory component**

Why: Uniquely solves the core problem ("Oracle knows WHERE data is but not WHAT it means") by encoding domain knowledge in a typed API. Multi-step reasoning is native in Python. Combined with preserved Tier 1 for speed/cost and RAG for permanent memory.

Why not the alternatives:
- **Gemini Flash (current):** Weaker instruction following, no structured tool-use, struggles with persona adherence on complex queries.
- **GPT-4o / GPT-4o-mini:** Strong SQL generation but weaker at maintaining character voice across long system prompts. Higher cost for comparable quality.
- **Local models (Llama, Mistral):** Insufficient reasoning depth for advisory questions. GPU requirement conflicts with single-server constraint.
- **Gemini 2.5 Pro:** Strong reasoning but less reliable at following complex persona constraints. Would keep us single-vendor but at quality cost.

---

## 2. Recommended Architecture: "The Oracle Engine"

> Sections below are numbered 2-9 sequentially.

**Tier 1 (PRESERVED):** 18 proven regex intents — instant, zero LLM cost, handles ~95% of queries deterministically. All 98 stress tests pass through this path.

**Tier 2 (NEW): Code-Generation Agent** — Claude Sonnet 4.6 generates Python code against a typed QueryBuilder API. Fires only when Tier 1 has no match. The QueryBuilder enforces domain rules (sort direction, efficiency vs volume, min games) mechanically — the LLM can't violate them. Multi-step reasoning (cross-season comparisons, advisory synthesis) is natural in Python.

**Answer Generation:** Claude Sonnet 4.6 with ATLAS analytical persona + affinity injection + permanent memory context.

**Permanent Memory:** Hybrid FTS5 + vector retrieval over all past conversations. No TTL, no turn limits.

```
User Question → Identity Resolution → Affinity Fetch → Memory Retrieval
    │
    ├─→ Tier 1 Regex (18 intents) ──→ Execute SQL ──→ Answer Gen ──→ Memory Store
    │                                                      ↑
    └─→ Tier 2 Code-Gen Agent ──→ Execute Python ─────────┘
         (Claude Sonnet 4.6)        (QueryBuilder API)
```

### Why This Architecture

- **Accuracy (#1 priority):** Domain rules enforced in code, not LLM suggestions. The 8-location sort-direction bug is fixed once in QueryBuilder, impossible to violate.
- **Open-ended (#2):** Code-Gen Agent handles any question — no fixed intent limitation. Multi-step queries natural in Python.
- **Advisory (#3):** Agent generates code that pulls multiple data sources and synthesizes recommendations.
- **Memory (#4):** Hybrid retrieval over permanent conversation store.
- **Speed (#9):** 95% of queries hit Tier 1 (instant). Agent path adds 2-5 seconds.
- **Cost:** Tier 1 = free. Agent code-gen = ~$0.03/query. Answer generation (all queries) = ~$0.01/query via Sonnet. Total projected: ~$1.15/day (95 Tier 1 answers + 5 agent code-gen + 5 agent answers). Well under $2/day budget.

---

## 3. Model Recommendation

**Claude Sonnet 4.6** as the single LLM for Oracle v3 (code-gen + answer generation).

| Dimension | Why Sonnet |
|-----------|-----------|
| Code generation | Excellent at generating Python against constrained APIs |
| Instruction following | Best-in-class persona adherence (ATLAS 3rd-person voice) |
| Multi-step reasoning | Strong at chaining data pulls for advisory questions |
| Tool use reliability | Structured tool use is very reliable |
| Context window | 200K tokens — ample room for schema + memory + rules |

**SDK:** `anthropic` Python SDK for Codex module only. Rest of ATLAS stays on `google.genai`.

**Embeddings:** Gemini `text-embedding-004` (free tier, 1,500 req/day) for conversation memory vectors.

---

## 4. QueryBuilder API — Three-Layer Design

### Layer 1: High-Level Domain Functions

```python
# Records & matchups
h2h(user1, user2, season=None) → H2HResult
owner_record(user, season=None) → RecordResult
team_record(team, season=None) → RecordResult
standings(division=None, conference=None) → list[StandingRow]
streak(user) → StreakResult

# Stat leaders (domain rules enforced: sort direction, efficiency, min games, pos filter)
stat_leaders(stat, season=None, sort="best", limit=10) → list[StatRow]
team_stat_leaders(stat, season=None, sort="best", limit=10) → list[TeamStatRow]

# Roster & draft
roster(team, pos=None, sort_by="ovr") → list[PlayerRow]
free_agents(pos=None, min_ovr=None) → list[PlayerRow]
draft_picks(team=None, season=None, round=None) → list[DraftRow]
abilities(team=None, player=None) → list[AbilityRow]

# History
trades(team=None, season=None, user=None) → list[TradeRow]
owner_history(user=None, team=None) → list[TenureRow]
game_extremes(type, season=None, limit=5) → list[GameRow]
recent_games(user, limit=5, opponent=None) → list[GameRow]

# Cross-season (NEW capability)
compare_seasons(stat, user_or_team, season1, season2) → ComparisonResult
improvement_leaders(stat, season1, season2, limit=10) → list[DeltaRow]
career_trajectory(user, stat) → list[SeasonStat]
```

### Layer 2: Composable QueryBuilder

```python
Query("offensive_stats")
    .select("extendedName", "teamName")
    .filter(season=6, stage="regular")
    .where("pos IN ('WR', 'TE')")
    .aggregate(recYds="SUM", recTDs="SUM")
    .group_by("extendedName", "teamName")
    .having("COUNT(*) >= 4")
    .sort_by("recYds", direction="DESC")
    .limit(10)
    .execute()
```

### Layer 3: Utility Functions

```python
compare(dataset1, dataset2, metric="delta"|"pct_change", sort="desc")
summarize(dataset) → dict
current_season() → int
current_week() → int
resolve_user(name) → str
resolve_team(name) → str
```

### Domain Knowledge Registry (DomainKnowledge class)

```python
STAT_DEFS = {
    "passYds":      StatDef(table="offensive_stats", agg="SUM", pos="QB",
                            efficiency_alt="passerRating", category="offense"),
    "passerRating": StatDef(table="offensive_stats", agg="AVG", pos="QB",
                            category="offense"),
    "defTotalYds":  StatDef(table="defensive_stats", agg="SUM", pos=None,
                            category="defense"),  # sort auto-inverted
    # ... all 39 stats from STAT_REGISTRY
}
```

Domain guards enforced by QueryBuilder:
- `.sort("best")` on defense stats → ASC (fewest = best)
- `.sort("worst")` on passing → switches to passerRating AVG
- `.sort("worst")` → auto-adds HAVING COUNT(*) >= 4
- All aggregations auto-wrap CAST(col AS INTEGER/REAL)
- Read-only: only SELECT statements generated
- Position filtering per STAT_REGISTRY definitions
- `.where()` clauses are parameterized — values extracted and passed as `?` params, raw SQL fragments validated against an allowlist of safe patterns (column names, operators, IN clauses). No string interpolation of user-controlled values.

---

## 5. Permanent Memory Architecture

### Storage Schema

```sql
CREATE TABLE conversation_memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id    INTEGER NOT NULL,
    message_id    INTEGER,            -- Discord message ID (for reply threading)
    question      TEXT    NOT NULL,
    sql_query     TEXT,
    answer        TEXT    NOT NULL,
    tier          INTEGER DEFAULT 3,
    intent        TEXT,
    entities      TEXT,       -- JSON: {users:[], teams:[], seasons:[], stats:[]}
    created_at    REAL    NOT NULL,
    embedding     BLOB
);

-- Observability logging
CREATE TABLE oracle_query_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id    INTEGER NOT NULL,
    question      TEXT    NOT NULL,
    tier          INTEGER NOT NULL,     -- 1=regex, 2=agent
    intent        TEXT,                 -- Tier 1 intent name or "agent"
    model         TEXT,                 -- "claude-sonnet-4-6" or null (Tier 1)
    latency_ms    INTEGER NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,               -- USD
    sql_executed  TEXT,
    rows_returned INTEGER,
    success       INTEGER DEFAULT 1,   -- 0=error, 1=success
    error_message TEXT,
    created_at    REAL    NOT NULL
);

CREATE VIRTUAL TABLE conversation_memory_fts USING fts5(
    question, answer, entities,
    content='conversation_memory', content_rowid='id'
);

CREATE INDEX idx_mem_user_time ON conversation_memory(discord_id, created_at DESC);
```

### Retrieval Pipeline

1. **Sliding window:** Last 5 turns (immediate context, always included)
2. **FTS5 keyword search:** Top 3 results by BM25 relevance
3. **Vector similarity:** Embed question → cosine search → top 3 (excluding duplicates)
4. **Merge & rank:** Combine, deduplicate, rank by recency + relevance → inject top 8 turns

**Embedding model:** Gemini text-embedding-004 (free tier, 1,500 req/day)

**Context budget:** ~8 past turns = ~2K tokens. Total system prompt ~5K tokens.

**Cross-user:** Factual answers shared. Advisory/personal conversations isolated by discord_id.

**No decay:** All history equally important. Recency is tiebreaker, not filter.

**Privacy:** `/forget` command for user-requested deletion.

### Discord Reply Threading

When a user replies to an Oracle answer (`message.reference` is set):
1. Look up the parent message ID in `conversation_memory` to find the original Q&A turn
2. Inject that specific turn as "immediate context" (highest priority in retrieval)
3. The reply question + parent context forms a natural follow-up without the user restating the original question
4. If the parent message isn't in conversation_memory (e.g., it's an old pre-v3 message), fall back to standard sliding window retrieval

This integrates with the existing retrieval pipeline — reply threading just adds a "pinned" context turn at the top of the injected history.

### Self-Aware Limitations

When the Code-Gen Agent or QueryBuilder returns empty results or errors:
- **Empty result set:** Oracle acknowledges the data gap and offers what it CAN answer. E.g., "ATLAS doesn't have play-by-play data to break down specific drives, but can show per-game totals for that matchup."
- **Unanswerable question:** If the question requires data not in the schema (salary projections, injury predictions), Oracle says so explicitly and pivots to related available data.
- **Agent execution failure (after 2 retries):** Oracle responds with "ATLAS couldn't crack that one. Try rephrasing, or ask something more specific." — never shows raw errors to users.

The system prompt for answer generation includes explicit instructions: "If the data doesn't support a conclusion, say so. Never fabricate stats. When results are empty, explain what data is and isn't available."

---

## 6. Migration Path (v2 → v3)

### Phase 1: Foundation (no behavior change)
- Build QueryBuilder API + DomainKnowledge registry as new modules
- Add anthropic SDK, create Claude client
- Create conversation_memory table + FTS5 index
- Unit tests for QueryBuilder against all 39 stat definitions
- **Gate:** All 98 stress tests still pass

### Phase 2: Memory Upgrade (additive)
- Remove CONV_TTL_SECONDS and CONV_MAX_TURNS limits
- Migrate existing conversation_history → conversation_memory
- Add embedding generation (Gemini text-embedding-004)
- Implement hybrid retrieval pipeline
- **Gate:** 98 tests pass + manual memory retrieval testing

### Phase 3: Code-Gen Agent (the big swap)
- Build safe execution sandbox (extend reasoning.py pattern)
- Implement Claude Sonnet code-gen agent with QueryBuilder in system prompt
- Wire as new Tier 2 (replaces Gemini classification + NL→SQL)
- **Gate:** All 98 tests pass via both Tier 1 AND forced-agent paths

### Phase 4: Answer Generation Migration
- Switch answer gen from Gemini Flash to Claude Sonnet
- Integrate memory context into answer prompts
- Add observability logging (model, latency, cost, tier, SQL, rows)
- **Gate:** 98 tests pass + persona/affinity verification

### Phase 5: Cleanup
- Remove dead Tier 2/3 code from codex_intents.py
- Add /forget command
- Backfill embeddings for existing conversations
- **Gate:** Full regression + load testing

Phases 1 and 2 can be developed in parallel. Phase 3 depends on 1. Phase 4 depends on 2+3. Phase 5 is cleanup.

---

## 7. Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Code-gen hallucination (calls nonexistent API methods) | High | Complete API reference in system prompt. Sandbox catches errors. Max 2 retries. |
| Sort direction regression vs Tier 1 | High | Run 98 tests through both paths. Any divergence is a bug. |
| Claude API latency spikes (>10s) | Medium | 95% hit Tier 1 (instant). 15s timeout on agent. Typing indicator. |
| Anthropic API outage | Medium | Fallback to Gemini Flash for agent. Both SDKs loaded. |
| Memory table growth (1.1M rows/year) | Low | SQLite handles millions fine. FTS5 stays fast. Archive after 2 years if needed. |
| Sandbox escape | High | Whitelist builtins (no open, import, exec, eval). Only QueryBuilder + utilities in scope. 5-second execution timeout. 50MB memory cap. No module imports. stdout captured for result extraction. |
| Embedding quota exhaustion | Low | Gemini free tier = 1,500/day. At 100 queries/day, well under limit. If exceeded, degrade to FTS5-only retrieval (keyword search still works). |
| User spamming agent path | Medium | Per-user rate limit: max 20 Tier 2 (agent) queries per hour. Tier 1 (regex) is unlimited. Rate limit response: "ATLAS needs a breather. Try again in a few minutes." |
| Dual-SDK maintenance | Low | Clear module boundary: Codex=anthropic, everything else=google.genai. |

---

## 8. Critical Files

### Files to Create
- `oracle_query_builder.py` — QueryBuilder API + DomainKnowledge registry
- `oracle_agent.py` — Code-gen agent (Claude Sonnet integration + sandbox)
- `oracle_memory.py` — Permanent memory (storage, retrieval, embeddings)

### Files to Modify
- `codex_cog.py` — Wire new Tier 2, replace conversation memory, switch answer gen to Sonnet
- `codex_intents.py` — Remove dead Tier 2 (Gemini classification) + Tier 3 (NL→SQL) code in Phase 5
- `bot.py` — Add ANTHROPIC_API_KEY env var, bump ATLAS_VERSION

### Files to Preserve (no changes)
- `echo_loader.py` — Persona system reused as-is
- `affinity.py` — Affinity system reused as-is
- `build_member_db.py` — Identity resolution reused as-is
- All Tier 1 regex intent builders in `codex_intents.py`

### Existing Patterns to Reuse
- `reasoning.py:_SAFE_BUILTINS` — Safe execution sandbox pattern
- `codex_cog.py:_build_schema()` — Dynamic schema injection with CURRENT_SEASON
- `codex_intents.py:STAT_REGISTRY` — Stat definitions (migrate to DomainKnowledge)
- `codex_intents.py:get_h2h_sql_and_params()` — Shared H2H SQL (wrap in QueryBuilder)
- `codex_cog.py:fuzzy_resolve_user()` — Name resolution (expose via QueryBuilder utilities)

---

## 9. Verification Plan

### Per-Phase Gates
- **Phase 1:** `python -m pytest test_oracle_stress.py` — 98/98 pass (no behavior change)
- **Phase 2:** 98/98 + manual test: ask question, wait 1 hour, ask "remember when I asked about..."
- **Phase 3:** 98/98 via Tier 1 + 98/98 via forced-agent path (set Tier 1 to no-match for testing)
- **Phase 4:** 98/98 + verify ATLAS persona voice in responses + affinity tier modulation
- **Phase 5:** 98/98 + `/forget` command works + observability dashboard shows metrics

### New Test Cases to Add
- Cross-season comparison: "who improved the most from last season?"
- Advisory: "should I trade my QB?"
- Memory recall: "what did I ask about last week?"
- Multi-step: "compare Witt's passing stats home vs away"
- Conversation threading: follow-up questions via Discord reply
