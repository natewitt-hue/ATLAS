# Oracle v3 Code & Bug Review — Handoff Document

## Mission

Full code and bug review of the Oracle v3 migration (Phases 1–5, versions v6.14.0 → v6.17.0). Identify bugs, logic errors, edge cases, missing error handling, security gaps, and dead code across all files touched. This is a review-only session — no implementation unless explicitly asked.

---

## Steps for Nate

Before starting the new session:

1. **Top up Anthropic API credits** — the bot's `ANTHROPIC_API_KEY` has zero balance. All stress tests fail because Claude returns "credit balance is too low" and Gemini fallback produces worse SQL. Fix this before running any stress tests during the review.

2. **Start the bot once** — the `conversation_memory` table is created on first startup via `oracle_memory._ensure_schema()`. It doesn't exist yet in the local `tsl_history.db`, which is why `backfill_embeddings.py --dry-run` returns "table does not exist."

3. **Run the backfill** — after the bot creates the table and you've accumulated some queries, run:
   ```bash
   python backfill_embeddings.py --dry-run    # check count first
   python backfill_embeddings.py              # live run (1,400 max per day)
   ```

4. **Re-run stress tests** after credits are topped up:
   ```bash
   python stress_test_history.py   # Oracle: baseline was 97/98 before credits ran out
   python stress_test_codex.py     # Codex: 0/10 is a PRE-EXISTING fence-stripping bug
   ```

5. **Paste this entire document** into the new Claude Code session as the opening prompt.

---

## What Changed (5 Phases)

### Phase 1+2 (v6.14.0) — Foundation + Memory

**New files created:**
- `oracle_query_builder.py` — QueryBuilder API: 44 StatDefs, 18 domain functions (h2h, owner_record, stat_leaders, etc.), composable `Query` builder class
- `oracle_memory.py` — Permanent conversation memory: `conversation_memory` table with FTS5 index + vector embeddings, `oracle_query_log` observability table, hybrid retrieval (sliding window + FTS5 + cosine similarity)

**Key design:**
- Replaced TTL-based `conversation_history` (ephemeral) with permanent `conversation_memory` (no expiry)
- Embedding via Gemini `text-embedding-004` (768-dim, free tier 1,500/day)
- Stored as JSON-encoded BLOB in SQLite
- Cosine similarity computed in Python (no pgvector — fine for <50K rows)
- FTS5 virtual table with auto-sync triggers (INSERT/UPDATE/DELETE)
- `retrieve_context()` merges 3 sources: recent window (5) + FTS (3) + vector (3), dedup by ID, cap at 8

### Phase 3 (v6.15.0) — Code-Gen Agent

**New file created:**
- `oracle_agent.py` — Code-Gen Agent: generates Python against QueryBuilder API via Claude Sonnet, executes in sandboxed environment

**Key design:**
- Sandbox uses `_SAFE_BUILTINS` from `reasoning.py` — blocks `__import__`, `open`, `eval`, `type`, `exec`, `compile`
- All 18 domain functions + Query builder + utilities exposed in sandbox globals
- System prompt includes full API reference (130 lines), 4 few-shot examples, dynamic schema
- Up to 3 attempts (2 retries) with error context fed back on failure
- `run_sql()` wrapped with capturing proxy to track SQL executions

**Routing change:**
- `detect_intent()` in `codex_intents.py` simplified: Tier 1 regex → on miss, return `IntentResult(tier=3)` → caller invokes Code-Gen Agent
- Old Tier 2 (Gemini classification) bypassed but not yet removed (done in Phase 5)

### Phase 4 (v6.16.0) — Answer Gen Migration + Observability

**Files modified:**
- `atlas_ai.py` (+33 lines) — `AIResult` extended with `input_tokens`, `output_tokens`, `latency_ms`; captured from Claude `response.usage` and Gemini `usage_metadata`
- `codex_cog.py` (+5/-5) — `gemini_answer()` switched to `Tier.SONNET`, returns `AIResult` instead of `str`
- `oracle_cog.py` (+43/-3) — 3 callsites updated (`_generate`, `_followup_tsl` x2); `log_query()` wired via `asyncio.ensure_future` at all 3; added `import time` + pipeline timers
- `stress_test_codex.py` (+2/-1) — handle `AIResult` return from `gemini_answer()`
- `stress_test_history.py` (+2/-1) — same

**Key design:**
- `oracle_query_log` table captures: tier, model, latency_ms, input_tokens, output_tokens, estimated_cost, sql_executed, rows_returned, success, error_message
- Non-blocking logging via `asyncio.ensure_future()` — fire-and-forget

### Phase 5 (v6.17.0) — Cleanup

**Files modified:**
- `codex_intents.py` (-440 lines) — removed `_CLASSIFICATION_PROMPT`, `_classify_gemini()`, `_build_from_classification()` + unused imports (`json`, `atlas_ai`, `Tier`)
- `oracle_cog.py` (+25 lines) — added admin-only `/forget` command calling `oracle_memory.forget_user()`
- `bot.py` (+1/-1) — version bump
- `README.md` (+2) — changelog

**New file:**
- `backfill_embeddings.py` (98 lines) — standalone migration script, `--dry-run` / `--limit N`, 1 req/sec rate limit, caps at 1,400/day

---

## Files to Review

| File | Lines | Phase(s) | What to Review |
|------|-------|----------|----------------|
| `oracle_memory.py` | 627 | 1+2 | Schema design, hybrid retrieval logic, FTS5 trigger correctness, embedding storage/retrieval, `forget_user()` completeness (does it clean FTS?), `migrate_from_conversation_history()` idempotency |
| `oracle_agent.py` | 537 | 3 | Sandbox security (can code escape?), retry logic, system prompt quality, error handling, `_safe_run()` exception handling, timeout enforcement |
| `oracle_query_builder.py` | ~800 | 1 | StatDef correctness (39 stat definitions), domain function SQL accuracy, Query builder SQL generation, edge cases in `resolve_user()`/`resolve_team()` |
| `atlas_ai.py` | 788 | 4 | AIResult field capture correctness, fallback logic (Claude→Gemini), `embed_text()` error handling, token counting accuracy, latency measurement |
| `codex_intents.py` | 1,372 | 3+5 | Tier 1 regex correctness (18 intents), no dead references remaining, `detect_intent()` flow, `STAT_REGISTRY` completeness |
| `oracle_cog.py` | 4,540 | 2+4+5 | `_oracle_mem` usage consistency, `log_query()` call completeness (are all paths logged?), `/forget` command, on_message follow-up handler, modal error handling |
| `codex_cog.py` | 531 | 4 | AIResult handling, `gemini_sql()` SQL extraction, `gemini_answer()` return type, fence-stripping bug |
| `backfill_embeddings.py` | 98 | 5 | Rate limiting correctness, error recovery, DB path resolution |
| `stress_test_history.py` | 283 | 4 | AIResult handling, test accuracy |
| `stress_test_codex.py` | 139 | 4 | AIResult handling, fence-stripping bug root cause |

---

## Known Issues to Investigate

### 1. Codex Stress Test 0/10 — SQL Fence-Stripping Bug
**Symptom:** All 10 Codex stress test questions fail with `unrecognized token: "```"`
**Root cause:** Gemini (fallback) returns SQL wrapped in markdown code fences (` ```sql ... ``` `). The `extract_sql()` function in `codex_cog.py` may not be stripping them properly when Claude is unavailable and Gemini is the provider.
**Files:** `codex_cog.py` — find `extract_sql()` and verify it handles triple-backtick fences.

### 2. `forget_user()` May Not Clean FTS5 Index
**Symptom:** After `/forget`, the FTS5 virtual table `conversation_memory_fts` may retain stale entries.
**Root cause:** `forget_user()` does a raw `DELETE FROM conversation_memory` — the FTS5 sync triggers (INSERT/DELETE/UPDATE) should fire automatically, but verify the DELETE trigger actually handles bulk deletes.
**Files:** `oracle_memory.py:526` (forget_user), `oracle_memory.py:86-102` (triggers)

### 3. Observability Logging May Miss Some Paths
**Question:** Are ALL Oracle query paths wired to `log_query()`? The on_message follow-up handler (oracle_cog.py:4119) has two log_query calls (lines 4334, 4374) but verify the AskTSLModal and other modals also log.
**Files:** `oracle_cog.py` — search for all `log_query` calls and map them to all query execution paths.

### 4. Sandbox Security — Can Agent Code Access File System?
**Question:** `_SAFE_BUILTINS` blocks `__import__` and `open`, but does the sandbox prevent accessing `__builtins__` via object traversal (e.g., `().__class__.__bases__[0].__subclasses__()`)? This is a classic Python sandbox escape.
**Files:** `oracle_agent.py:332-437`, `reasoning.py` (wherever `_SAFE_BUILTINS` is defined)

### 5. Token Counting May Be Incomplete for JSON Retry
**Question:** In `atlas_ai.py`, when `json_mode=True` and the response needs retry (malformed JSON), are tokens from both attempts summed?
**Files:** `atlas_ai.py` — look for JSON retry logic and token accumulation.

### 6. `estimated_cost` Always None
**Question:** `log_query()` accepts `estimated_cost` but it's never calculated or passed anywhere. Is this intentional (future) or an oversight?
**Files:** `oracle_cog.py` — search all `log_query` calls for `estimated_cost` parameter.

### 7. TODO: SupportCog Tier Gating
**Location:** `oracle_cog.py:120`
**Comment:** `TODO: SupportCog does not exist yet. All users default to 'Elite' (full access). Build SupportCog or remove tier gating.`
**Question:** Is the tier gating code dead weight, or planned? If dead, remove it.

---

## Review Approach

1. **Security review** — Sandbox escapes, SQL injection, command injection vectors
2. **Correctness review** — Logic errors, off-by-one, missing edge cases, wrong SQL
3. **Completeness review** — Missing error handling, unlogged paths, incomplete cleanup
4. **Consistency review** — AIResult usage patterns, import patterns, naming conventions
5. **Performance review** — N+1 queries, unnecessary DB connections, blocking calls in async context

---

## Architecture Reference

```
User Question
    │
    ▼
detect_intent()  ──Tier 1 match──▶  Execute SQL directly (instant, free)
    │
    │ no match (tier=3)
    ▼
oracle_agent.run_agent()  ──▶  Claude Sonnet generates Python
    │                          against QueryBuilder API
    │                          ──▶ sandbox exec ──▶ result
    ▼
gemini_answer()  ──▶  Claude Sonnet formats NL answer
    │                  with persona + affinity + memory context
    ▼
embed_and_store()  ──▶  Store turn + embedding
log_query()        ──▶  Observability metrics (fire-and-forget)
```

**Memory retrieval on every query:**
```
build_context_block(user_id, question)
    ├── get_recent(5)          sliding window
    ├── search_fts(question)   BM25 keyword match
    └── search_vector(embed)   cosine similarity
    ──▶ deduplicate ──▶ cap at 8 ──▶ format for prompt
```

---

## Environment Notes

- **Python 3.14**, discord.py 2.3+, aiosqlite, google-genai, anthropic SDK
- **Anthropic API credits depleted** — Claude calls fail, Gemini fallback catches them
- **Gemini embedding quota:** 1,500/day free tier
- **DB:** `tsl_history.db` (conversation_memory + oracle_query_log tables)
- **Version:** 6.17.0 (current, pushed to main)
- **Single session** (no Sicko Mode)
