# Oracle v3 — First Principles Brainstorm

> **What this document is:** A comprehensive prompt for a fresh AI session to brainstorm and design Oracle v3 from first principles. Paste this entire document into a new Claude or Gemini session. Do not write any code until you have produced the structured deliverables in Section 10.

---

## Section 1: Project Context

**ATLAS** (Autonomous TSL League Administration System) is a Discord bot serving as the full admin infrastructure for **The Simulation League (TSL)** — a Madden NFL sim league with ~31 active teams across 95+ Super Bowl seasons (6 completed seasons of tracked data).

**Oracle** is ATLAS's analytics and intelligence module. Today it converts natural-language questions into SQL queries against a historical database, then formats the results using ATLAS's personality. Users interact with Oracle via Discord slash commands (`/ask`) and @mentions.

**Tech stack:**
- Python 3.14, discord.py 2.3+
- Currently: Google Gemini 2.0 Flash for NL→SQL and answer generation
- SQLite (tsl_history.db — 11 tables, 6 seasons of games/stats/trades/players)
- Playwright (HTML→PNG card rendering)
- Single server deployment

**Scale:**
- ~100 queries/day, 31 active users
- 6 seasons of historical data (games, player stats, trades, draft history, standings)
- Budget: currently <$1/day Gemini API costs

**The vision for Oracle v3:** Oracle should become an **omniscient league historian and strategic advisor**. It answers any question about league history — not just the 18 fixed query types it handles today. It remembers every conversation permanently. It supports every claim with specific data. It gives strategic advice grounded in real numbers. It gets smarter over time.

---

## Section 2: Current Architecture Autopsy

Oracle v2's intent detection lives in two files: `codex_intents.py` (1,780 lines) and `codex_cog.py` (1,014 lines). It works — 98/98 stress tests pass — but is architecturally fragile.

### The Three-Tier System

```
User Question
    │
    ▼
Tier 1: Regex Pre-Flight (instant, deterministic)
    │ ~60 compiled regex patterns across 18 intents
    │ First match → call intent builder → return SQL + params
    │ If no match ↓
    ▼
Tier 2: Gemini Structured Classification (dead code in practice)
    │ Sends question + 18 intent schemas to Gemini
    │ Parses JSON: {intent, params, confidence}
    │ If confidence < 0.7 ↓
    ▼
Tier 3: Gemini NL→SQL (unconstrained)
    │ Full schema + rules + few-shot examples → raw SQL
    │ Execute → format with persona → return
```

**Tier 2 is effectively dead code.** In practice, Tier 1 regex catches ~95% of queries, and the remaining 5% fall straight to Tier 3. The Gemini classification tier was designed as a middle ground but its confidence threshold (0.7) means it rarely commits.

### The 18 Fixed Intents

| # | Intent | What it does |
|---|--------|-------------|
| 1 | `h2h_record` | Head-to-head record between two owners |
| 2 | `season_record` | An owner's record in a specific season |
| 3 | `alltime_record` | An owner's career win/loss record |
| 4 | `leaderboard` | Owner/player stat rankings |
| 5 | `recent_games` | Last N games for an owner |
| 6 | `streak` | Current win/loss streak |
| 7 | `team_record` | A team's record by team name |
| 8 | `draft_history` | Draft picks for a team/season |
| 9 | `game_score` | Score of a specific game |
| 10 | `playoff_results` | Super Bowl winners, championship games |
| 11 | `player_stats` | Individual player stat leaders |
| 12 | `trade_history` | Trades by team/season |
| 13 | `team_stats` | Team-level offense/defense/points rankings |
| 14 | `owner_history` | Teams owned by an owner / who owned a team |
| 15 | `records_extremes` | Blowouts, closest games, high/low scoring |
| 16 | `standings_query` | Division/conference standings |
| 17 | `roster_query` | Team rosters, best players, free agents |
| 18 | `player_abilities_query` | X-Factor/Superstar abilities |

**Any question that doesn't fit one of these 18 types falls to Tier 3** (unconstrained Gemini NL→SQL), which is expensive, slow, and less reliable.

### Problem 1: Sort Direction Logic Duplicated 8 Times

The same keyword detection for "worst/bottom/fewest/least/lowest" is copy-pasted across 8 locations with subtly different semantics:

**Location 1 — Leaderboard (owner wins):**
```python
sort_worst = any(kw in text_lower for kw in ['worst', 'bottom', 'fewest', 'least', 'lowest'])
sort_dir = 'ASC' if sort_worst else 'DESC'
```

**Location 2 — Leaderboard (player stats, lines 494-495):**
```python
sort_asc = any(kw in text_lower for kw in ['worst', 'bottom', 'fewest', 'least', 'lowest'])
sort_dir = 'ASC' if sort_asc else 'DESC'
```

**Location 3 — Player Stats (lines 876-877):**
```python
sort_asc = any(kw in text_lower for kw in ['worst', 'least', 'lowest', 'bottom', 'fewest'])
sort_dir = 'ASC' if sort_asc else 'DESC'
```

**Location 4 — Team Stats (lines 957-975) — the most complex:**
```python
flip = any(kw in text_lower for kw in ['worst', 'least', 'lowest', 'fewest'])

if any(kw in text_lower for kw in ['offense', 'offence', 'offensive']):
    sort_dir = 'ASC' if flip else 'DESC'
elif any(kw in text_lower for kw in ['defense', 'defence', 'defensive']):
    # Defense: best = fewest yards (ASC), worst = most yards (DESC)
    sort_dir = 'DESC' if flip else 'ASC'   # ← INVERTED from offense
elif any(kw in text_lower for kw in ['points', 'scoring']):
    sort_dir = 'ASC' if flip else 'DESC'
```

This same logic appears in 4 more locations in the Tier 2 classification handler. **Any fix must be applied in all 8 places or the system silently diverges.**

### Problem 2: Efficiency vs Volume Not Centralized

When a user asks "worst passer," should Oracle sort by fewest passing yards (volume) or lowest passer rating (efficiency)? The answer depends on which intent catches the query:

**In `_build_leaderboard` (lines 498-505):**
```python
# "Worst passer" → use efficiency metric (passer rating) instead of volume
if sort_asc and worst_col:
    select_cols = f"ROUND(AVG(CAST({worst_col} AS REAL)), 1) AS total_stat"
else:
    select_cols = f"SUM(CAST({primary_col} AS INTEGER)) AS total_stat"
```

**In `_build_player_stats` (lines 879-881):**
```python
# Always uses the STAT_REGISTRY aggregation — no efficiency override
sql = f"""
    SELECT extendedName AS player_name, teamName,
           {agg}(CAST({column} AS {cast_type})) AS stat_value...
```

The leaderboard intent switches to efficiency metrics for "worst" queries. The player_stats intent does not. Same question, different SQL, depending on which regex fires first.

### Problem 3: Minimum Games Filter Missing in Tier 2

Tier 1 correctly excludes backup players from "worst" rankings:
```python
# Tier 1 (codex_intents.py, lines 521, 894):
having = " HAVING COUNT(*) >= 4" if sort_asc else ""
```

But the Tier 2 Gemini classification handler (lines 1506-1627) builds SQL without this filter. If a query somehow reaches Tier 2 instead of Tier 1, backup QBs with 1 game and a 12.3 passer rating appear as the "worst passer."

### Problem 4: Conversation Memory Is Too Limited

Current implementation:
```python
CONV_MAX_TURNS   = 5        # Max Q&A pairs to inject as context
CONV_TTL_SECONDS = 1800     # 30 minutes — stale conversations are dropped

# Schema:
CREATE TABLE conversation_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    question   TEXT    NOT NULL,
    sql_query  TEXT,
    answer     TEXT    NOT NULL,
    created_at REAL    NOT NULL
)
```

5 turns. 30-minute TTL. After half an hour, Oracle forgets everything. A user can't ask "Remember when I asked about my defense last week?" — that context is gone.

### What Works Well (Preserve These Patterns)

1. **Dynamic schema injection** — `_build_schema()` embeds `CURRENT_SEASON` into every Gemini prompt so the model always has fresh context
2. **Identity resolution** — `build_member_db.py` with 88+ aliases handles "Witt", "TheWitt", "witt" → canonical `TheWitt`
3. **Persona injection** — `echo_loader.get_persona("analytical")` provides consistent ATLAS voice
4. **Parameterized SQL** — intent builders use `?` placeholders, never string interpolation
5. **Affinity-driven tone** — `get_affinity_instruction(score)` modulates voice per user
6. **Data validators in tests** — 98 test cases with closure-based validators that verify sort direction, column values, and row counts

### The Core Problem

**Oracle knows WHERE data is but not WHAT it means.** It can map "passing yards" to `offensive_stats.passYds` but doesn't understand that "worst passer" implies efficiency metrics, minimum qualification thresholds, and position filtering. This domain knowledge is scattered across 8 procedural builders instead of being declared once.

---

## Section 3: The Full Vision

This is not a feature list. This is what it should feel like to use Oracle v3.

### 3.1 Omniscient Historian

Oracle answers **any** question about league history. Not 18 fixed types. Any question.

- "Which team had the biggest turnaround between seasons 3 and 5?"
- "Who improved the most from last season?"
- "What's the longest winning streak in league history?"
- "How many times has a team gone undefeated in the regular season?"
- "Which division has been the most competitive across all seasons?"
- "Compare Witt's passing stats when playing at home vs away"

If the data exists in the database, Oracle should be able to answer the question. Complex multi-join, cross-season, comparative queries should produce thoughtful, complete answers — not "I don't understand."

### 3.2 Strategic Advisor

Oracle doesn't just report data — it **reasons** about it.

- "What should I do to improve my team?"
- "Is trading my QB worth it based on the draft class depth?"
- "Who should I target in free agency given my cap situation?"
- "My defense has been terrible — what positions should I prioritize?"
- "Based on remaining schedule, which teams are most likely to make playoffs?"

Oracle uses data as evidence but goes beyond raw queries. It synthesizes roster composition, schedule difficulty, historical trends, and positional value to give strategic advice. Every recommendation is grounded in specific numbers.

### 3.3 Permanent Memory

Oracle remembers **every conversation** with **every user**. Forever.

- "Remember when I asked about my defense last month? How has it changed since then?"
- "You told me to target a CB in free agency — did I do that?"
- "What was that stat you showed me about Killa's rushing?"

Context from weeks ago is retrievable and relevant. Oracle builds a relationship with each user over time, understanding their team, their concerns, and their history of questions.

### 3.4 Conversational Depth

Users can **reply to any Oracle answer** to drill in, challenge, or refine.

- User: "Who has the best defense?"
- Oracle: "The Bears lead with 267.3 yards allowed per game..."
- User (reply): "What about if you only count the last 4 weeks?"
- Oracle: "Over the last 4 weeks, the Packers actually jump to #1 at 241.8..."
- User (reply): "How does that compare to their full-season average?"
- Oracle: "The Packers' 4-week average is 23% better than their season average of 312.1..."

No restating the full question. No losing context. Natural back-and-forth via Discord reply threading (`message.reference`).

### 3.5 Fact-Grounded Assertions

Every claim Oracle makes can be **traced to specific data**.

- "Your defense ranks 28th in yards allowed (342.5/game) — only the Bears (351.2), Jets (348.7), and Commanders (344.1) are worse."
- "Witt has a 67-43 all-time record. In head-to-head vs Killa, he's 8-5 with a 3-game winning streak."
- "The last time a team won the Super Bowl after starting 2-4 was the Season 3 Patriots."

Not vibes. Not approximations. Exact numbers from real data, cited in context.

### 3.6 Personalized Voice

Oracle modulates its tone based on affinity (existing system: -100 to +100 score):

| Tier | Score | Behavior |
|------|-------|----------|
| FRIEND | >= +30 | Warm, familiar, inside jokes, extra effort |
| NEUTRAL | -10 to +29 | Default ATLAS voice |
| DISLIKE | -10 to -50 | Curt, skip pleasantries, impatient |
| HOSTILE | <= -50 | Openly dismissive, backhanded competence |

Same data, different delivery. A FRIEND asking "how's my team?" gets a detailed breakdown with encouragement. A HOSTILE user gets the bare facts with a condescending tone.

### 3.7 Self-Aware Limitations

Oracle knows when it doesn't know.

- "I don't have trade value data to answer that, but based on draft history, teams that traded down in round 1 gained an average of 2.3 wins the following season."
- "The database doesn't track play-by-play, so I can't tell you about specific drives. But I can show you per-game stat totals."
- "That question requires salary cap projections I don't have. Here's what I can tell you about current cap room..."

No hallucination. No making up stats. When the data doesn't support a conclusion, Oracle says so and offers what it can.

---

## Section 4: Existing Infrastructure (Reusable)

### 4.1 Affinity System (`affinity.py`)

Per-user sentiment scoring with asymmetric deltas:

```python
POSITIVE_DELTA =  2     # Friendly / grateful message
NEGATIVE_DELTA = -3     # Rude / hostile (sticks harder)
SCORE_MIN, SCORE_MAX = -100, 100

TIER_FRIEND  =  30      # >= 30: warm & familiar
TIER_DISLIKE = -10      # <= -10: curt & impatient
TIER_HOSTILE = -50      # <= -50: openly dismissive
```

Prompt injection function:
```python
def get_affinity_instruction(score: float) -> str:
    if score >= TIER_FRIEND:
        return "[USER AFFINITY: FRIEND] Be warm, familiar, crack an inside joke..."
    if score <= TIER_HOSTILE:
        return "[USER AFFINITY: HOSTILE] Be dismissive and condescending..."
    if score <= TIER_DISLIKE:
        return "[USER AFFINITY: LOW] Be curt and efficient..."
    return ""  # Neutral — default behavior
```

**Reusable as-is.** The affinity instruction string can be injected into any LLM prompt regardless of architecture.

### 4.2 Persona System (`echo_loader.py`)

Three voice registers loaded from text files:

| Context | Triggers | Style |
|---------|----------|-------|
| casual | @mentions, banter | Friendly, direct, sports slang |
| analytical | stats, recap, rankings | Data-driven, confident storytelling |
| official | rulings, governance | Authority, finality, structured |

```python
from echo_loader import get_persona
system_prompt = get_persona("analytical")  # Returns full persona text
```

**Reusable as-is.** Oracle uses the "analytical" register.

### 4.3 Identity Resolution (`build_member_db.py`)

88+ alias entries mapping all name variants to canonical DB usernames:

```python
# get_alias_map() returns:
{
    "Witt": "TheWitt", "TheWitt": "TheWitt", "witt": "TheWitt",
    "JT": "TrombettaThanYou", "Jordantromberg": "TrombettaThanYou",
    "Killa": "KillaE94", "KillaE94": "KillaE94",
    "Diddy": "BDiddy86", "Bdiddy": "BDiddy86",
    "Ron": "Ronfk", "I2onDon": "Ronfk",
    # ... 88+ total entries
}
```

Also: `get_db_username_for_discord_id(snowflake)` for exact Discord ID → DB username lookup.

**Reusable as-is.** Any architecture needs name resolution.

### 4.4 Conversation History Schema

Current table (needs policy changes, not schema changes):
```sql
CREATE TABLE conversation_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    question   TEXT    NOT NULL,
    sql_query  TEXT,
    answer     TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX idx_conv_user_time ON conversation_history(discord_id, created_at DESC);
```

**Schema is fine. Policy needs replacement:** Remove 30-min TTL, remove 5-turn limit, add retrieval mechanisms for permanent memory.

### 4.5 Dynamic Schema Injection (`codex_cog.py`)

The `_build_schema()` function generates a fresh schema string on every prompt build, embedding the current season number:

```python
def _build_schema() -> str:
    return f"""DATABASE: tsl_history.db
IMPORTANT RULES:
- All column values are stored as TEXT. Cast with CAST(col AS INTEGER/REAL) for math.
- seasonIndex: '1'=Season 1, '2'=Season 2... '{dm.CURRENT_SEASON}'=Season {dm.CURRENT_SEASON} (current)
- stageIndex: '0'=Preseason, '1'=Regular Season, '2'=Playoffs
- weekIndex is 0-based
- status IN ('2','3') means completed game

TABLE: games
  Columns: id, seasonIndex, stageIndex, weekIndex, homeTeamName, awayTeamName,
           homeScore, awayScore, status, homeUser, awayUser, winner_user, loser_user...
  ...
"""
```

**Pattern is excellent.** Any architecture that involves LLM SQL generation needs dynamic schema context.

### 4.6 Stress Test Suite (`test_oracle_stress.py`)

98 test cases with data validators. Format:
```python
(question, simulated_caller_db, description, expected_intent, [validators])
```

Validators are closure factories:
```python
_val_sort_desc(col)       # Verify results sorted descending
_val_sort_asc(col)        # Verify results sorted ascending
_val_col_equals(col, val) # All rows have column = value
_val_has_rows()           # At least 1 row returned
_val_min_rows(n)          # At least n rows
_val_losses_not_wins()    # Returns loser_user, not winner_user
_val_opponent_filter(name)# All games involve opponent
_val_season_filter(season)# All rows match seasonIndex
_val_team_filter(team)    # Team name in results
_val_meta_key(key, val)   # Meta dict key equals value
```

**Critical regression baseline.** Any v3 architecture must pass these 98 tests. Full test cases are in Appendix C.

---

## Section 5: Hard Constraints

1. **Single server** — no distributed infrastructure, no Kubernetes, no multi-region
2. **Async required** — must not block the Discord event loop. All LLM calls wrapped in `loop.run_in_executor()` or natively async
3. **Accuracy always wins over speed.** Latency budgets:
   - Simple factual queries: <2 seconds
   - Complex multi-join / cross-season queries: up to 10 seconds acceptable
   - Advisory / strategic analysis: up to 15 seconds acceptable
   - **Never sacrifice correctness for latency**
4. **LLM API budget: <$2/day** at current volume (~100 queries/day). Model choice is open — not locked to Gemini — but costs must stay reasonable
5. **Backward compatible with 98 stress tests** — these are the regression baseline. Every test must pass against the new architecture
6. **Preserve ATLAS persona voice** — always 3rd person as "ATLAS" (never "I"/"me"). Punchy — 2-4 sentences max, no bullet lists in responses, no fluff. Cites real names and real numbers. Uses `echo_loader.get_persona("analytical")` for system prompts
7. **Permanent conversation storage must not degrade query performance** — growing conversation history shouldn't slow down SQL queries against the main database
8. **Python 3.14 + discord.py 2.3+** — these are fixed. No switching to JavaScript or other frameworks

---

## Section 6: The Brainstorm Mandate

Evaluate these approaches from first principles. **No fixed intent system is assumed.** Oracle v3 should answer any question, not just 18 predefined types.

### Approach 1: Full LLM Agent with SQL Tools

Give the LLM access to SQL query tools and the DB schema. No intent detection at all. "Here's a question and a database, figure it out." Use tool-use API (function calling) for multi-step reasoning.

**How it works:**
1. User asks a question
2. LLM receives: system prompt (persona + schema + domain rules + conversation history) + user question
3. LLM decides what SQL to run via tool calls
4. Execute SQL, return results to LLM
5. LLM synthesizes a natural-language answer
6. For complex questions, LLM can chain multiple queries

**Guiding questions:**
- How reliable is tool-use for complex multi-join queries? Can the LLM self-correct on SQL errors?
- What's the latency for multi-step reasoning (2+ tool calls)?
- How do we prevent hallucination when the LLM synthesizes advisory answers?
- Can we constrain tool-use to read-only operations? (Critical for security)
- How do we inject domain knowledge (sort direction rules, efficiency vs volume) without the LLM ignoring it?

### Approach 2: Hybrid — LLM Planner + Deterministic Executor

LLM decides WHAT to query (outputs a structured query plan), deterministic engine executes the SQL. Separates reasoning from execution.

**How it works:**
1. LLM receives question + schema + domain rules
2. LLM outputs structured plan: `{queries: [{table, columns, filters, sort, limit}], reasoning: "..."}`
3. Deterministic engine validates the plan against domain rules (sort direction, min games, etc.)
4. Engine builds and executes SQL
5. LLM receives results + original question → generates answer

**Guiding questions:**
- How do we define the query plan schema? Too rigid = can't handle open-ended questions. Too loose = same problems as free-form SQL.
- Does the deterministic layer become the new bottleneck (a new version of the 18-intent limitation)?
- How do advisory/predictive questions fit? They require synthesis, not just query execution.
- Can the planner handle multi-step queries (query A's results inform query B)?

### Approach 3: RAG + Conversation Memory

Embed all historical Q&A pairs and DB schema as vectors. For new queries, retrieve relevant past answers + schema context, then generate. Permanent memory becomes retrieval over conversation embeddings.

**How it works:**
1. All past Q&A pairs embedded as vectors (stored in SQLite vec, FAISS, or similar)
2. New question → embed → find nearest neighbors (similar past questions + answers)
3. Combine: similar past answers + DB schema + domain rules + user's conversation history
4. LLM generates answer using retrieved context
5. For novel questions with no similar past answers, fall back to SQL generation

**Guiding questions:**
- What embedding model? Local (sentence-transformers) vs API (OpenAI/Gemini embeddings)?
- How to handle data freshness? Past answers may reference old season data
- How to combine structured DB queries with unstructured conversation retrieval?
- At 100 queries/day, how quickly does the vector store grow? Performance implications?
- Can RAG alone handle truly novel questions, or does it always need SQL fallback?

### Approach 4: Multi-Agent System

Specialized agents that each handle one aspect, coordinated by an orchestrator:

**Agents:**
- **SQL Agent** — queries the database, understands schema and domain rules
- **Memory Agent** — retrieves relevant past conversations for this user
- **Advisory Agent** — synthesizes insights, makes predictions, gives strategic advice
- **Persona Agent** — applies ATLAS voice + affinity modulation to the final response

**How it works:**
1. Orchestrator receives question + user context
2. Routes to relevant agents in parallel (SQL + Memory usually)
3. Agents return structured results
4. Advisory Agent synthesizes if needed
5. Persona Agent formats the final response

**Guiding questions:**
- Latency of multi-agent roundtrips? Each agent may need its own LLM call
- How do agents share context? Does the Memory Agent's output feed into the SQL Agent?
- Is the complexity justified at this scale (~100 queries/day, 31 users)?
- How do you debug when one agent produces wrong results that another agent amplifies?

### Approach 5: Fine-Tuned Domain Model

Fine-tune a small model on TSL data + conversation history. Runs locally, no API costs, deep domain knowledge baked in. Supplement with RAG for fresh data.

**How it works:**
1. Collect training data: all 98 stress test Q&A pairs + real user queries + synthetic examples
2. Fine-tune a small model (e.g., Llama 3, Mistral, Phi-3) on TSL-specific NL→SQL + answer generation
3. Deploy locally alongside the bot
4. Use RAG to supplement with fresh data the model hasn't seen

**Guiding questions:**
- Is the training data volume sufficient? 98 test cases + ~100 queries/day * 6 seasons
- How to handle season-to-season schema changes? Retraining cadence?
- Can a small model handle complex multi-join reasoning or cross-season comparisons?
- GPU requirements for inference? Does this violate the "single server" constraint?
- How do advisory questions work? Fine-tuning for factual SQL is different from strategic reasoning

### Approach 6: Single LLM Call with Rich System Prompt

Keep it simple: one LLM call with a comprehensive system prompt containing schema, domain rules, conversation history, and the question. Rely on the model's native reasoning ability. No agents, no intent detection, no RAG, no tool use.

**How it works:**
1. Build a rich system prompt: persona + schema + domain knowledge rules + recent conversation history + relevant past conversations + affinity instruction
2. Send user question
3. LLM generates both the SQL (in a structured block) and the natural-language answer in one response
4. Parse and execute the SQL, validate results, send answer

**Guiding questions:**
- Context window limits? With full schema (~2K tokens) + domain rules (~500) + conversation history (~1K) + question, how much room is left?
- How much conversation history fits before hitting limits? 10 turns? 50? 500?
- Accuracy on complex queries without explicit SQL tools?
- Cost at scale with large system prompts?
- How do you handle multi-step queries that require intermediate results?

---

## Section 7: Permanent Memory Architecture

The brainstorm **must** specifically address how permanent conversation memory works. This is not optional — it's a core requirement.

### 7.1 Storage

How to store every Q&A pair per user permanently:
- Extend existing `conversation_history` table? Or dedicated memory DB?
- Schema: should we store the raw SQL alongside the Q&A pair for reproducibility?
- Should we store structured metadata (intent, entities mentioned, season referenced)?
- How to handle growing storage? 31 users × 100 queries/day × 365 days = ~1.1M rows/year

### 7.2 Retrieval

Given a new question, how to find relevant past conversations:
- **Keyword search** — simple, fast, but misses semantic similarity ("my defense" vs "yards allowed")
- **Vector similarity** — embed questions, find nearest neighbors. Requires embedding model
- **Recency weighting** — more recent conversations should be weighted higher
- **Hybrid** — keyword for exact references ("remember when...") + vector for semantic similarity
- How many past conversations to retrieve? 5? 10? Dynamic based on relevance score?

### 7.3 Context Management

Can't inject ALL history into every prompt:
- **Sliding window** — always include last N turns (immediate context)
- **Semantic retrieval** — add top-K similar past conversations from further back
- **Summarization** — periodically summarize old conversations into compressed context
- **User profile** — maintain a running summary of each user's team, concerns, and patterns

### 7.4 Cross-User Knowledge

Should Oracle learn from ALL users' questions?
- **Isolated** — each user's memory is private. User A's questions never influence User B's answers
- **Shared knowledge** — if User A asked "who won the Super Bowl in season 3?" the answer is cached for everyone
- **Hybrid** — factual answers shared, advisory/personal conversations isolated

### 7.5 Memory Decay

Should old conversations be weighted less?
- **No decay** — all history equally important. "Remember when I asked in season 2..."
- **Soft decay** — older conversations have lower retrieval scores but are never deleted
- **Relevance-based** — conversations about current season/team weighted higher than old ones

### 7.6 Privacy

- Can users see their conversation history? (A "my history" command?)
- Can users delete specific conversations?
- Should there be a maximum retention period for regulatory/privacy reasons?
- How does memory interact with the HOSTILE affinity tier? (Does Oracle weaponize past conversations?)

---

## Section 8: Domain Knowledge System

Oracle needs to encode "common sense" about sports statistics. These rules must be **centralized and declarative** — not scattered across procedural builders.

### Sort Direction Rules

| Stat Category | "Best/Top/Most" | "Worst/Bottom/Least" |
|---------------|-----------------|----------------------|
| Offensive yards/TDs | DESC (most = best) | ASC (fewest = worst) |
| Defensive yards allowed | **ASC** (fewest = best) | **DESC** (most = worst) |
| Points scored | DESC | ASC |
| Points allowed | ASC (fewest = best) | DESC |
| Win count | DESC | ASC |
| Loss count | DESC (most losses) | ASC (fewest losses) |
| Passer rating | DESC | ASC |

**Key insight:** Defense inverts the normal sort direction. "Best defense" = fewest yards (ASC), which is the opposite of "best offense" = most yards (DESC). This inversion has caused bugs multiple times.

### Efficiency vs Volume

| Query Type | Metric Type | Example |
|-----------|-------------|---------|
| "Top/best passers" | Volume (SUM) | Most passing yards |
| "Worst passers" | **Efficiency (AVG)** | Lowest passer rating |
| "Top rushers" | Volume (SUM) | Most rushing yards |
| "Worst rushers" | Volume (SUM) | Fewest rushing yards |
| "Best defense" | Volume (SUM, inverted) | Fewest total yards |

"Worst passer" should return the QB with the lowest passer rating among qualified starters — not the QB with the fewest passing yards (which could be a backup who played 1 game).

### Minimum Qualification Thresholds

For "worst/least" queries, exclude players who don't have meaningful sample sizes:
- Currently: `HAVING COUNT(*) >= 4` (at least 4 games played)
- This prevents a practice-squad QB with 1 interception from ranking as the #1 "worst passer"
- The threshold should probably vary by stat type (more games needed for efficiency metrics)

### Position Semantics

| Term | Position Filter | Table |
|------|----------------|-------|
| "passer" / "QB" | pos = 'QB' | offensive_stats |
| "rusher" / "runner" | No filter (all positions rush) | offensive_stats |
| "receiver" | No filter (WR/TE/HB all catch) | offensive_stats |
| "defender" | No filter | defensive_stats |
| "pass rusher" | No filter | defensive_stats (sacks) |

### Cross-Season Reasoning

Questions like "who improved the most from last season?" require:
1. Query stat X for season N-1
2. Query stat X for season N
3. Compute delta
4. Sort by delta

This is a multi-step operation that no current intent handles.

### Advisory Reasoning

Questions like "should I trade my QB?" require:
1. Evaluate current QB's stats vs league average
2. Check draft class depth at QB
3. Assess cap situation
4. Consider team record and playoff chances
5. Synthesize a recommendation with supporting data

This goes beyond SQL — it requires strategic reasoning grounded in data.

---

## Section 9: Model Evaluation

The brainstorm should evaluate which LLM best fits Oracle v3's needs. **The current system uses Gemini 2.0 Flash, but we are open to switching.**

| Dimension | Questions to Answer |
|-----------|-------------------|
| **Tool use reliability** | How accurately does each model use SQL tools? Self-correction on errors? |
| **SQL generation accuracy** | Complex multi-join queries, cross-season comparisons, subqueries |
| **Multi-step reasoning** | Can it chain multiple queries to answer comparative questions? |
| **Context window** | How much schema + history + domain rules fit? |
| **Cost per query** | At ~100 queries/day, what's the monthly cost? |
| **Latency** | Time to first token, total generation time for typical queries |
| **Advisory quality** | Can it synthesize data into strategic recommendations? Not just report numbers |
| **Persona adherence** | Does it maintain ATLAS's 3rd-person punchy voice throughout? |
| **Conversation threading** | Can it naturally handle follow-up questions with context? |

Consider at minimum:
- **Gemini 2.0 Flash** (current) — cheap, fast, good at structured tasks
- **Claude Sonnet/Haiku** — strong reasoning, good tool use, excellent instruction following
- **GPT-4o-mini / GPT-4o** — widely used, strong SQL generation
- **Local models** (Llama 3, Mistral) — zero API cost, requires GPU, may lack reasoning depth

---

## Section 10: Success Criteria and Decision Framework

### Prioritized Success Criteria

The recommended architecture must satisfy these criteria **in priority order**:

1. **Accuracy above all else** — Deep, knowledgeable, correct answers backed by real data. If a user replies to challenge an answer, Oracle cites the exact rows/stats that support its response. Getting it right matters more than getting it fast.

2. **Open-ended capability** — Any question about league history, trends, comparisons. No "I don't understand" for reasonable queries. The system should handle questions its designers never anticipated.

3. **Advisory depth** — Strategic advice grounded in real data and logical reasoning. Not just reporting numbers — synthesizing insights, identifying patterns, making recommendations.

4. **Permanent memory** — Conversations from weeks ago are retrievable and useful. Oracle builds understanding of each user over time.

5. **Conversational** — Multi-turn follow-ups work via Discord reply threading. Users can challenge, refine, or drill into any answer without restating context.

6. **Fact-grounded** — Every claim traceable to specific rows/stats. No hallucination. When data doesn't support a conclusion, Oracle says so.

7. **Personalized** — Affinity-driven voice modulation. Same data, different delivery based on user relationship.

8. **Observable** — Every query logged with: model used, latency, cost, confidence, SQL executed, rows returned. Dashboards for monitoring accuracy and cost.

9. **Speed** — Simple queries <2s, complex <10s, advisory <15s. Accuracy always wins over speed.

10. **Maintainable** — A new developer should understand the system in 15 minutes. No 1,780-line files. Clear separation of concerns.

### Mandatory Deliverables

**You MUST produce all of the following before proposing any code:**

1. **Comparison matrix** — All 6 approaches evaluated across: accuracy, latency, cost, maintenance burden, extensibility, memory support, advisory capability

2. **Recommended architecture** — One approach (or hybrid) with clear justification for why it best satisfies the prioritized criteria above

3. **Model recommendation** — Which LLM(s) to use and why. Include cost projections at current scale

4. **Permanent memory architecture design** — Storage schema, retrieval strategy, context management approach, privacy model

5. **Migration path from v2** — Incremental, not big-bang. How do we get from 1,780 lines of regex to the new architecture without breaking the 98 stress tests?

6. **Risks and mitigations** — What could go wrong? Hallucination, cost overruns, latency spikes, conversation memory bloat

7. **Implementation roadmap** — Phases, not dates. What gets built first? What can be deferred?

---

## Appendix A: Full Database Schema

This is the complete schema of `tsl_history.db`, dynamically injected into every LLM prompt:

```
DATABASE: tsl_history.db  ─  The Simulation League (TSL) Madden franchise history

IMPORTANT RULES:
- All column values are stored as TEXT even if they look like numbers. Cast with CAST(col AS INTEGER) or CAST(col AS REAL) when doing math/comparisons.
- seasonIndex: '1'=Season 1 (2025), '2'=Season 2 (2026)... '6'=Season 6 (current)
- stageIndex: '0'=Preseason, '1'=Regular Season, '2'=Playoffs
- weekIndex is 0-based

TABLE: games
  Columns: id, scheduleId, seasonIndex, stageIndex, weekIndex,
           homeTeamId, awayTeamId, homeTeamName, awayTeamName,
           homeScore, awayScore, status, homeUser, awayUser,
           winner_user, loser_user, winner_team, loser_team
  Notes: status IN ('2','3') means completed. homeUser/awayUser are the owner usernames.
         winner_user/loser_user are pre-computed from scores.
         To find games involving a user: WHERE homeUser='X' OR awayUser='X'
         Head-to-head: WHERE (homeUser='A' AND awayUser='B') OR (homeUser='B' AND awayUser='A')

TABLE: teams
  Columns: teamId, cityName, abbrName, nickName, displayName, logoId,
           primaryColor, secondaryColor, ovrRating, defScheme, offScheme,
           divName, injuryCount, userName, playerCount, capRoomFormatted,
           capSpentFormatted, capAvailableFormatted
  Notes: userName is the current owner. One row per franchise (current season snapshot).

TABLE: standings  (current season / current week only)
  Columns: id, teamId, teamName, teamOvr, calendarYear, seasonIndex, stageIndex, weekIndex,
           divisionName, conferenceName, totalWins, totalLosses, totalTies,
           confWins, confLosses, confTies, divWins, divLosses, divTies,
           homeWins, homeLosses, awayWins, awayLosses,
           offTotalYds, offPassYds, offRushYds, defTotalYds, defPassYds, defRushYds,
           ptsFor, ptsAgainst, netPts, rank, seed, playoffStatus,
           tODiff, winLossStreak, winPct, capRoom, capAvailable, capSpent,
           initialSoS, totalSoS, playedSoS, remainingSoS

TABLE: offensive_stats  (per-game player offensive stats, all seasons)
  Columns: id, fullName, extendedName, seasonIndex, stageIndex, weekIndex, gameId,
           teamId, teamName, rosterId, pos,
           passAtt, passComp, passCompPct, passTDs, passInts, passYds, passSacks,
           passerRating, passYdsPerAtt, passYdsPerGame, passPts,
           rushAtt, rushYds, rushTDs, rushFum, rushLongest, rushBrokenTackles,
           rushYdsAfterContact, rushYdsPerAtt, rushYdsPerGame, rushPts,
           recCatches, recDrops, recCatchPct, recYds, recYdsPerCatch,
           recYdsPerGame, recTDs, recLongest, recYdsAfterCatch, recPts, offPts
  Notes: pos values include QB, HB, FB, WR, TE, OL, etc.

TABLE: defensive_stats  (per-game player defensive stats, all seasons)
  Columns: statId, fullName, extendedName, seasonIndex, stageIndex, weekIndex,
           gameId, teamId, teamName, rosterId, pos,
           defTotalTackles, defSacks, defSafeties, defInts, defIntReturnYds,
           defForcedFum, defFumRec, defTDs, defCatchAllowed, defDeflections, defPts
  Notes: pos values include DT, LE, RE, LOLB, MLB, ROLB, CB, FS, SS

TABLE: team_stats  (per-game team stats, all seasons)
  Columns: statId, seasonIndex, stageIndex, weekIndex, gameId,
           teamId, teamName,
           defForcedFum, defFumRec, defIntsRec, defPassYds, defRushYds,
           defRedZoneFGs, defRedZones, defRedZonePct, defRedZoneTDs, defSacks, defTotalYds,
           off4thDownAtt, off4thDownConv, off4thDownConvPct,
           offFumLost, offIntsLost, off1stDowns,
           offPassTDs, offPassYds, offRushTDs, offRushYds,
           offRedZoneFGs, offRedZones, offRedZoneTDs, offSacks, offTotalYds,
           penalties, penaltyYds,
           off3rdDownAtt, off3rdDownConv, off3rdDownConvPct,
           tODiff, tOGiveAways, tOTakeaways

TABLE: trades  (all trade history)
  Columns: id, team1_id, team1Name, team2_id, team2Name, status,
           seasonIndex, stageIndex, weekIndex, team1Sent, team2Sent
  Notes: status = 'approved' / 'denied' / 'pending'
         team1Sent/team2Sent contain comma-separated asset descriptions with values.

TABLE: players  (current roster snapshot)
  Columns: rosterId, firstName, lastName, age, height, weight, pos, jerseyNum,
           college, yearsPro, dev, teamId, teamName, isFA, isOnIR,
           playerBestOvr, capHit, contractSalary, contractYearsLeft,
           speedRating, strengthRating, agilityRating, awareRating, catchRating,
           routeRunShortRating, routeRunMedRating, routeRunDeepRating,
           throwPowerRating, throwAccShortRating, throwAccMedRating, throwAccDeepRating,
           carryRating, jukeMoveRating, spinMoveRating, truckRating, breakTackleRating,
           tackleRating, hitPowerRating, pursuitRating, playRecRating, manCoverRating,
           zoneCoverRating, pressRating, blockSheddingRating, runBlockRating,
           passBlockRating, impactBlockRating, kickPowerRating, kickAccuracyRating
  Notes: dev values: 'Normal', 'Star', 'Superstar', 'XFactor'
         isFA='1' means free agent. teamName='Free Agent' for unsigned players.

TABLE: player_abilities  (X-Factor/Superstar abilities)
  Columns: rosterId, firstName, lastName, teamName, title, description,
           startSeasonIndex, endSeasonIndex
  Notes: An ability with no endSeasonIndex is still active.

TABLE: owner_tenure  ← USE THIS for any "owner-filtered" historical queries
  Columns: teamName, userName, seasonIndex, games_played
  Notes: Ground truth of who owned which team in which season.
         ALWAYS join this table when the question is about a specific owner's
         performance with their team across seasons.
  Example: "How has TheWitt done as the Lions owner?" →
    JOIN owner_tenure ot ON g.homeTeamName=ot.teamName AND g.seasonIndex=ot.seasonIndex
    WHERE ot.userName='TheWitt'

TABLE: player_draft_map  ← USE THIS for any draft history queries
  Columns: rosterId, extendedName, drafting_team, drafting_season,
           draftRound, draftPick, current_team, dev, playerBestOvr, pos,
           rookieYear, was_traded
  Notes: drafting_team = team that ORIGINALLY drafted the player.
         DO NOT use players.teamName for draft queries.
         draftRound: 2=R1, 3=R2, 4=R3, 5=R4, 6=R5, 7=R6, 8=R7
         was_traded=1 means player was later moved from their drafting team.

COMMON JOINS:
- team_stats JOIN games ON team_stats.gameId = games.id
- offensive_stats.gameId links to games.id
- Use games.homeUser/awayUser to get owner context for team_stats rows
- owner_tenure JOIN games ON teamName+seasonIndex to filter to an owner's tenure
- player_draft_map for ANY draft-related query (not players table)

OWNER-FILTERED QUERY PATTERN:
  SELECT ... FROM games g
  JOIN owner_tenure ot ON (g.homeTeamName=ot.teamName OR g.awayTeamName=ot.teamName)
    AND g.seasonIndex=ot.seasonIndex
  WHERE ot.userName='[owner]' AND (g.homeUser='[owner]' OR g.awayUser='[owner]')
```

---

## Appendix B: STAT_REGISTRY and Domain Rules

### Current Stat Keyword → SQL Mapping

```python
STAT_REGISTRY = {
    # Passing (QB position filter)
    'passing touchdowns': ('offensive_stats', 'passTDs',    'SUM', 'QB'),
    'passing yards':      ('offensive_stats', 'passYds',    'SUM', 'QB'),
    'passing tds':        ('offensive_stats', 'passTDs',    'SUM', 'QB'),
    'pass tds':           ('offensive_stats', 'passTDs',    'SUM', 'QB'),
    'pass yards':         ('offensive_stats', 'passYds',    'SUM', 'QB'),
    'interceptions thrown':('offensive_stats', 'passInts',  'SUM', 'QB'),
    'passer rating':      ('offensive_stats', 'passerRating','AVG', 'QB'),
    'completion percentage':('offensive_stats','passCompPct','AVG', 'QB'),
    'completions':        ('offensive_stats', 'passComp',   'SUM', 'QB'),

    # Rushing (no position filter — all positions rush)
    'rushing touchdowns': ('offensive_stats', 'rushTDs',    'SUM', None),
    'rushing yards':      ('offensive_stats', 'rushYds',    'SUM', None),
    'rushing tds':        ('offensive_stats', 'rushTDs',    'SUM', None),
    'rush yards':         ('offensive_stats', 'rushYds',    'SUM', None),
    'rush tds':           ('offensive_stats', 'rushTDs',    'SUM', None),
    'fumbles':            ('offensive_stats', 'rushFum',    'SUM', None),

    # Receiving (no position filter)
    'receiving touchdowns':('offensive_stats', 'recTDs',    'SUM', None),
    'receiving yards':    ('offensive_stats', 'recYds',     'SUM', None),
    'receiving tds':      ('offensive_stats', 'recTDs',     'SUM', None),
    'receptions':         ('offensive_stats', 'recCatches', 'SUM', None),
    'catches':            ('offensive_stats', 'recCatches', 'SUM', None),
    'drops':              ('offensive_stats', 'recDrops',   'SUM', None),
    'yards after catch':  ('offensive_stats', 'recYdsAfterCatch', 'SUM', None),

    # Defense (no position filter)
    'forced fumbles':     ('defensive_stats', 'defForcedFum',    'SUM', None),
    'fumble recoveries':  ('defensive_stats', 'defFumRec',       'SUM', None),
    'defensive tds':      ('defensive_stats', 'defTDs',          'SUM', None),
    'defensive touchdowns':('defensive_stats','defTDs',          'SUM', None),
    'pass deflections':   ('defensive_stats', 'defDeflections',  'SUM', None),
    'deflections':        ('defensive_stats', 'defDeflections',  'SUM', None),
    'tackles':            ('defensive_stats', 'defTotalTackles', 'SUM', None),
    'sacks':              ('defensive_stats', 'defSacks',        'SUM', None),
    'interceptions':      ('defensive_stats', 'defInts',         'SUM', None),
}
# Tuple format: (table, column, aggregation, position_filter)
# Aggregation: SUM = volume metric, AVG = efficiency metric
# Position filter: 'QB' means add WHERE pos='QB', None means all positions
```

### MaddenStats API Gotchas

These are hard-won lessons. Violating any causes silent data bugs:

| Rule | Detail |
|------|--------|
| Completed games | Filter with `status IN ('2','3')`, NOT `status='3'` alone |
| `weekIndex` | 0-based in API/DB, but CURRENT_WEEK in data_manager is 1-based |
| `devTrait` mapping | 0=Normal, 1=Star, 2=Superstar, 3=Superstar X-Factor |
| Draft history | Credit to drafting team (first appearance), NOT current team |
| All columns are TEXT | Always CAST for math: `CAST(passYds AS INTEGER)` |
| `stageIndex` | 0=Preseason, 1=Regular Season, 2=Playoffs |
| Owner resolution | Use `owner_tenure` table, NOT `teams.userName` (which is current only) |
| `draftRound` mapping | 2=Round 1, 3=Round 2, ..., 8=Round 7 (offset by 1) |

---

## Appendix C: Full Stress Test Suite (98 Test Cases)

These are the regression baseline. Any v3 architecture must pass all 98.

Format: `Q# | Question | Caller | Expected Intent | Validators`

| Q# | Question | Caller | Expected Intent | Validators |
|----|----------|--------|-----------------|------------|
| 1 | what is my record vs diddy | TheWitt | h2h_record | — |
| 2 | JT vs Tuna | TestCaller | h2h_record | — |
| 3 | how are the Saints doing | TestCaller | team_record | — |
| 4 | my season record | TheWitt | season_record | — |
| 5 | Witt's all-time record | TestCaller | alltime_record | — |
| 6 | top 5 passers this season | TestCaller | leaderboard | sort_desc(total_stat) |
| 7 | who did New Orleans draft | TestCaller | draft_history | — |
| 8 | what is Chokolate_Thunda's record vs MeLLoW_FiRe | TestCaller | h2h_record | — |
| 9 | my last 5 games vs Killa | TheWitt | recent_games | opponent_filter(Killa) |
| 10 | what is Shottaz record this season | TestCaller | season_record | — |
| 11 | who won the Super Bowl in season 3 | TestCaller | playoff_results | — |
| 12 | playoff results this season | TestCaller | playoff_results | — |
| 13 | who has the most passing TDs all-time | TestCaller | player_stats | sort_desc(stat_value) |
| 14 | top rushing yards this season | TestCaller | player_stats | sort_desc(stat_value) |
| 15 | who leads the league in sacks | TestCaller | player_stats | sort_desc(stat_value) |
| 16 | what trades did the Lions make | TestCaller | trade_history | — |
| 17 | trades this season | TestCaller | trade_history | — |
| 18 | which team has the best offense | TestCaller | team_stats | sort_desc(off_yds) |
| 19 | which team scores the most points | TestCaller | team_stats | sort_desc(pts_for) |
| 20 | what was the score of Lions vs Packers | TestCaller | game_score | — |
| 21 | score of the Chiefs game | TestCaller | game_score | — |
| 22 | what teams has Witt owned | TestCaller | owner_history | — |
| 23 | who owned the Bears in season 2 | TestCaller | owner_history | — |
| 24 | biggest blowout ever | TestCaller | records_extremes | sort_desc(margin) |
| 25 | closest game this season | TestCaller | records_extremes | sort_asc(margin) |
| 26 | highest scoring game | TestCaller | records_extremes | sort_desc(total_pts) |
| 27 | NFC East standings | TestCaller | standings_query | col_equals(divisionName, NFC East) |
| 28 | who is the best QB in the league | TestCaller | roster_query | — |
| 29 | Lions roster | TestCaller | roster_query | sort_desc(ovr) |
| 30 | who has x-factor on the Packers | TestCaller | player_abilities_query | — |
| 31 | my record last season | TheWitt | season_record | — |
| 32 | top five passers | TestCaller | leaderboard | sort_desc(total_stat) |
| 33 | what's my record this season | TheWitt | season_record | — |
| 34 | the Lions record this season | TestCaller | team_record | — |
| 35 | how many games have I won | TheWitt | alltime_record | — |
| 36 | how many wins do I have this season | TheWitt | season_record | — |
| 37 | who has most wins | TestCaller | leaderboard | sort_desc(total_wins) |
| 38 | Cowboys record season 4 | TestCaller | team_record | — |
| 39 | who leads the league in interceptions | TestCaller | player_stats | sort_desc(stat_value) |
| 40 | free agents at QB | TestCaller | roster_query | col_equals(teamName, Free Agent) |
| 41 | who has the most losses | TestCaller | leaderboard | losses_not_wins, sort_desc(total_losses) |
| 42 | which team has the worst offense | TestCaller | team_stats | sort_asc(off_yds) |
| 43 | which team has the worst defense | TestCaller | team_stats | sort_desc(def_yds) |
| 44 | worst passer this season | TestCaller | leaderboard | sort_asc(total_stat), meta(sort=asc) |
| 45 | who has the fewest points | TestCaller | team_stats | sort_asc(pts_for) |
| 46 | Bears record this season | TestCaller | team_record | — |
| 47 | what was the score of the Commanders game | TestCaller | game_score | — |
| 48 | Cowboys record season three | TestCaller | team_record | meta(season=3) |
| 49 | Witt's trades this season | TestCaller | trade_history | — |
| 50 | who has the least sacks | TestCaller | player_stats | sort_asc(stat_value), meta(sort=asc) |
| 51 | worst owner this season | TestCaller | leaderboard | sort_asc(total_wins), meta(sort=asc) |
| 52 | best owner this season | TestCaller | leaderboard | sort_desc(total_wins), meta(sort=desc) |
| 53 | who has the fewest losses | TestCaller | leaderboard | sort_asc(total_losses), losses_not_wins, meta(sort=asc) |
| 54 | owner with the least losses this season | TestCaller | leaderboard | sort_asc(total_losses), losses_not_wins |
| 55 | which team has the best offense this season | TestCaller | team_stats | sort_desc(off_yds), has_rows |
| 56 | which team has the worst defense this season | TestCaller | team_stats | sort_desc(def_yds), has_rows |
| 57 | my current win streak | TheWitt | streak | has_rows, meta(owner=TheWitt) |
| 58 | my streak | TheWitt | streak | has_rows, meta(owner=TheWitt) |
| 59 | is Killa on a winning streak | TestCaller | streak | has_rows |
| 60 | Witt's losing streak | TestCaller | streak | has_rows |
| 61 | Witt vs Killa | TestCaller | h2h_record | has_rows |
| 62 | Lions vs Packers | TestCaller | game_score | has_rows, team_filter(Lions) |
| 63 | who has the most wins this season | TestCaller | leaderboard | sort_desc(total_wins), has_rows |
| 64 | top QB | TestCaller | roster_query | sort_desc(ovr), has_rows |
| 65 | Cowboys draft picks | TestCaller | draft_history | has_rows |
| 66 | Packers record | TestCaller | team_record | has_rows |
| 67 | my record | TheWitt | alltime_record | has_rows |
| 68 | top 5 sacks | TestCaller | player_stats | sort_desc(stat_value), min_rows(5) |
| 69 | who has the most rushing TDs all-time | TestCaller | player_stats | sort_desc(stat_value), has_rows |
| 70 | who has the fewest rushing yards | TestCaller | player_stats | sort_asc(stat_value), meta(sort=asc) |
| 71 | worst receiver this season | TestCaller | leaderboard | sort_asc(total_stat), meta(sort=asc) |
| 72 | best passer this season | TestCaller | leaderboard | sort_desc(total_stat), meta(sort=desc) |
| 73 | which team allows the most points | TestCaller | team_stats | sort_desc(pts_against), has_rows |
| 74 | which team has the best defense | TestCaller | team_stats | sort_asc(def_yds), has_rows |
| 75 | lowest scoring game ever | TestCaller | records_extremes | sort_asc(total_pts), has_rows |
| 76 | most lopsided game this season | TestCaller | records_extremes | sort_desc(margin), has_rows |
| 77 | bottom 5 passers | TestCaller | leaderboard | sort_asc(total_stat), meta(sort=asc) |
| 78 | who has the highest passer rating | TestCaller | player_stats | sort_desc(stat_value), has_rows |
| 79 | witt record this season | TestCaller | season_record | has_rows |
| 80 | my games against Tuna | TheWitt | recent_games | opponent_filter(Tuna), has_rows |
| 81 | NFC West standings | TestCaller | standings_query | col_equals(divisionName, NFC West), has_rows |
| 82 | AFC standings | TestCaller | standings_query | has_rows |
| 83 | who drafted for New England | TestCaller | draft_history | has_rows |
| 84 | what abilities does Jalen Hurts have | TestCaller | player_abilities_query | has_rows |
| 85 | who's the best owner all time | TestCaller | leaderboard | sort_desc(total_wins), has_rows |
| 86 | how are the Packers doing this season | TestCaller | team_record | has_rows |
| 87 | my record vs Killa | TheWitt | h2h_record | has_rows, meta(owner2=KillaE94) |
| 88 | Witt's record this season | TestCaller | season_record | has_rows |
| 89 | my all-time record | TheWitt | alltime_record | has_rows |
| 90 | who won the Super Bowl in season 1 | TestCaller | playoff_results | meta(type=playoffs) |
| 91 | what was the score of the Cowboys game this season | TestCaller | game_score | has_rows, team_filter(Cowboys) |
| 92 | who did the Eagles draft in season 3 | TestCaller | draft_history | has_rows |
| 93 | Lions trades this season | TestCaller | trade_history | meta(team=Lions) |
| 94 | what teams has Killa owned | TestCaller | owner_history | has_rows |
| 95 | my record; DROP TABLE games | TheWitt | alltime_record | has_rows |
| 96 | top passers' OR 1=1 -- | TestCaller | leaderboard | has_rows |
| 97 | who has the most wins" UNION SELECT * FROM games -- | TestCaller | leaderboard | sort_desc(total_wins) |
| 98 | my all-time record; DELETE FROM games | TheWitt | alltime_record | has_rows |

**Note:** Tests 95-98 are SQL injection safety tests. The system must handle these gracefully without executing malicious SQL.

---

## Final Instruction

You now have:
- The full project context and vision (Sections 1-3)
- The current system's strengths and weaknesses with real code (Sections 2, 4)
- Hard constraints (Section 5)
- 6 candidate architectures to evaluate (Section 6)
- Permanent memory requirements (Section 7)
- Domain knowledge rules (Section 8)
- Model evaluation criteria (Section 9)
- Prioritized success criteria and mandatory deliverables (Section 10)
- Complete database schema (Appendix A)
- Full stat registry and API gotchas (Appendix B)
- 98 regression test cases (Appendix C)

**Produce the 7 mandatory deliverables from Section 10 before writing any code.** Think from first principles. Challenge assumptions. The current regex-based system is not sacred — if the best answer is "throw it all away and use a single LLM call," say so with evidence.
