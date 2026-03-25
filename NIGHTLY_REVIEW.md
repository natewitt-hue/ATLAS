# ATLAS Nightly Code Review
**Date:** 2026-03-25
**Domain:** Oracle & Analytics
**Auditor:** Automated (Wednesday cadence)
**Branch:** feat/sportsbook-ux-overhaul
**Files Audited:** oracle_cog.py, oracle_agent.py, oracle_memory.py, oracle_query_builder.py, analysis.py, intelligence.py, data_manager.py

---

## Phase 1 — Recent Git Changes (Last 24h)

**Commits reviewed:**
- `94e9531` — ux(hubs): overhaul button layout, labels, and colors across all 4 hubs; fix poly finalize crash

**Diff summary for oracle_cog.py (HubView):**
- Buttons reordered: "Standings" and "Power Rankings" moved to Row 0 first positions
- "Head-to-Head" → "Matchup" (label only; `custom_id` unchanged — persistence maintained ✅)
- "Team Stats" → "Teams" (label only; `custom_id` unchanged ✅)
- "Season Recap" style change (label/emoji only)
- "ASK" (Oracle Hub) moved from Row 3 to Row 2

**Assessment:** All changes are cosmetic/UX — no logic or custom_id mutations. Persistent views across bot restarts are unaffected. No functional regression introduced.

---

## Phase 2 — Deep File Audit

### Pass A — Anti-Pattern Scan

| Pattern | Location | Count | Notes |
|---------|----------|-------|-------|
| `asyncio.ensure_future()` | oracle_cog.py | 6 | All fire-and-forget `log_query()` calls — no error capture |
| Bare `except:` | None | 0 | Clean — no bare excepts found in focus files |
| f-string SQL (user-facing) | oracle_query_builder.py:342 | 1 | SELECT column build only — columns from whitelist, not user input |
| `_get_claude()/_get_gemini()` private access | oracle_cog.py | 3 | L1258, 2083, 2404 — availability gates only |
| `embed_and_store()` calls | oracle_cog.py | 5 | Double-store bug on TSL query path (see W-01) |
| `print()` in load function | data_manager.py | 20+ | All in `load_all()` — run in executor, bypasses log infrastructure |
| `sqlite3.connect()` without context manager | intelligence.py | 2 | L166, L262 — connection leak on exception path |
| Unbounded module-level dicts/sets | oracle_cog.py, intelligence.py | 3 | `_oracle_message_ids`, `_chain_roots`, `_owner_profiles` |

---

### Pass B — Logic Trace

**oracle_cog.py — Double-store in `_OracleIntelModal` / `AskTSLModal` interaction:**
`_OracleIntelModal.on_submit` at L2883 calls `embed_and_store()` to persist the query/answer pair. Then `AskTSLModal._generate` at L3049 calls `embed_and_store()` again for the same query. This writes **two `conversation_memory` rows per TSL query**. The second write includes `sql=sql or ""` which differs from the first. Both rows are indexed in FTS5 and included in vector recall. Net effect: TSL queries are double-weighted in memory retrieval, biasing recall toward them.

**oracle_memory.py — `search_vector()` memory model:**
`search_vector()` at L399 loads **all rows** with a non-null `embedding` column into Python memory, deserializes each 768-float vector, and computes cosine similarity in a loop. At ~100 Oracle queries/day across 95 seasons of history, this table accumulates ~36K+ rows/year. At ~3KB/embedding, 36K rows = ~108MB loaded into Python on every non-trivial Oracle call. No LIMIT, no FAISS index, no recency pre-filter. The table has no TTL or row cap — `store_turn()` writes indefinitely.

**data_manager.py — `get_last_n_games()` blocks event loop:**
`get_last_n_games()` at L764 makes a synchronous `requests.get()` HTTP call to the MaddenStats API. It is called without `run_in_executor` from async Discord button handlers in oracle_cog.py at L1199, L2061, L2291, and from `intelligence.get_owner_context()` at L701 (itself called from async contexts). Each call blocks the asyncio event loop for the HTTP round trip (200ms–2s). Under concurrent Oracle usage these stall interactions across the entire bot.

**oracle_agent.py — Sandbox enforcement (confirmed clean):**
Traced all DB access paths through the agent sandbox. `build_agent_env()` at L344 exposes only `run_sql` to agent-generated code. `_make_capturing_run_sql()` at L333 proxies `codex_utils.run_sql` which enforces SELECT-only. No `exec`, `eval`, `open`, `import`, or write-path functions are present in `_SAFE_BUILTINS` or the agent env. AST validation runs before `exec()`. 15-second `asyncio.wait_for` timeout at L521–526 caps runaway queries. **No read/write escalation paths confirmed.**

**intelligence.py — `YEAR_TO_SEASON` range stops at season 10:**
`YEAR_TO_SEASON = {yr: yr - _YEAR_BASE for yr in range(2025, 2035)}` ends at year 2034 / season 10. The league has 95-season history. However `get_draft_class` uses `SEASON_TO_YEAR.get(season, season + 2024)` — the arithmetic fallback is correct for seasons 11+. No data corruption; the hardcoded range is misleading but functional.

---

### Pass C — Cross-Module Contract

**oracle_cog.py ↔ atlas_ai.py — private method coupling:**
Three locations call `atlas_ai._get_claude()` and `atlas_ai._get_gemini()` directly (L1258, L2083, L2404). These are used only as availability gates: `if (atlas_ai._get_claude() or atlas_ai._get_gemini()) and ...`. If `atlas_ai` internals change (lazy init, renamed vars), all three checks silently return `None` and disable AI-enhanced embeds without error. The correct contract is a public `atlas_ai.is_available() -> bool`.

**intelligence.py ↔ data_manager.py — blocking propagation:**
`get_owner_context()` at L701 calls `dm.get_last_n_games()` synchronously. Since `get_owner_context()` is itself synchronous and invoked from async oracle_cog.py button handlers, the blocking HTTP call propagates through the chain: async button handler → `get_owner_context()` → `dm.get_last_n_games()` → `requests.get()`.

**oracle_memory.py ↔ tsl_history.db — concurrent aiosqlite connections:**
Each `asyncio.ensure_future(log_query(...))` in oracle_cog.py opens a new aiosqlite connection to `tsl_history.db`. With 6 fire-and-forget calls per response, up to 6 concurrent write connections can be active. `oracle_memory.py` does not enable WAL mode before writing, relying on another module (e.g., `data_manager.py`) to have done so. If the bot starts fresh and Oracle is queried before data_manager initializes, WAL is not active for these writes.

---

### Pass D — Performance Audit

**Critical:**
- `get_last_n_games()` blocking HTTP on event loop — oracle_cog.py L1199, L2061, L2291; intelligence.py L701. Stalls all bot interactions during HTTP call. See W-04.

**Significant:**
- `search_vector()` O(n) memory load — oracle_memory.py L399. Grows with conversation history. See W-02.

**Minor:**
- `get_h2h_record()` at data_manager.py L899 uses `iterrows()` (pandas' slowest iteration). At 256 rows/season this is negligible but could be vectorized.
- `flag_stat_padding()` at data_manager.py L1135 iterates `_players_cache` (1700+ entries) every 15 minutes. Acceptable at current scale.
- `find_trades_by_player()` at data_manager.py L700 iterates all trades via `iterrows()`. Small dataset, acceptable.

---

### Pass E — Security & Data Integrity

**oracle_agent.py sandbox — CONFIRMED SAFE:**
- `_SAFE_BUILTINS` whitelist excludes `import`, `open`, `eval`, `exec`, `type`, `__import__`.
- AST validation via `validate_sandbox_ast()` runs before every `exec()`.
- `run_sql` is the only DB entry point in the sandbox; it enforces SELECT-only.
- No balance, bet, or schema mutation is reachable from agent-generated code.

**oracle_memory.py FTS5 — CONFIRMED SAFE:**
`_sanitize_fts()` at L340 properly strips FTS5 operators before user input is used in a MATCH expression. No injection risk.

**oracle_query_builder.py — CONFIRMED SAFE:**
All Layer 1 domain functions use parameterized `?` placeholders. `Query.build()` at L327 generates only `SELECT` statements. The single f-string at L342 builds column names from the internal whitelist-validated state, not user input.

**intelligence.py sqlite3 bare connect — LOW RISK:**
Connections at L166 and L262 use `conn.close()` without a `with` statement. If `.execute()` raises, the connection is not explicitly closed. Python GC will collect it eventually, but this holds file handles longer than intended in high-throughput scenarios.

---

## Phase 3 — Findings Summary

### Warnings — Should Fix

**W-01 · Double embed_and_store per TSL query (oracle_cog.py)**
- **Location:** L2883 (`_OracleIntelModal.on_submit`) and L3049 (`AskTSLModal._generate`)
- **Impact:** Every TSL query writes two `conversation_memory` rows. Double-weights TSL queries in FTS5 index and vector recall. Inflates DB size over time.
- **Fix:** Remove the `embed_and_store()` call at L2883. The definitive write at L3049 includes the SQL and occurs post-answer. L2883 is a premature duplicate.

**W-02 · Unbounded conversation_memory + O(n) vector search (oracle_memory.py)**
- **Location:** `store_turn()` (no eviction), `search_vector()` L399 (full-table load)
- **Impact:** At sustained usage, `search_vector()` loads 100MB+ of embeddings into Python memory on every non-trivial Oracle query. No TTL means the table grows permanently.
- **Fix:** (a) Add `created_at` timestamp column and a periodic prune job removing entries older than N days. (b) In `search_vector()`, add `ORDER BY created_at DESC LIMIT 2000` before the cosine loop to cap memory footprint.

**W-03 · Unbounded _oracle_message_ids / _chain_roots (oracle_cog.py)**
- **Location:** L70–71 (declarations), insertions at L2872–2873, L4232–4233
- **Impact:** Both structures grow by one entry per Oracle response and are never evicted. Over a multi-day session these accumulate indefinitely.
- **Fix:** Add TTL-based eviction on the existing `_followup_counter % 50` cleanup hook. Store `(message_id, timestamp)` pairs and prune entries older than 6 hours.

**W-04 · get_last_n_games() blocks async event loop (data_manager.py / oracle_cog.py)**
- **Location:** `data_manager.get_last_n_games()` L764; callers oracle_cog.py L1199, L2061, L2291; intelligence.py L701
- **Impact:** Synchronous `requests.get()` inside an async button handler stalls the event loop for the HTTP round trip. Stacks under concurrent usage.
- **Fix:** Either (a) wrap `_get()` call in `get_last_n_games()` with `loop.run_in_executor(None, ...)` and make callers await it, or (b) filter `dm.df_all_games` by team name to serve recent games from the already-loaded DataFrame — avoiding a live API call entirely.

---

### Observations — Consider Fixing

**O-01 · Private atlas_ai method access (oracle_cog.py)**
- **Location:** L1258, L2083, L2404
- **Suggestion:** Add `atlas_ai.is_available() -> bool` as a public API. Three oracle_cog.py availability checks become `if atlas_ai.is_available() and ...`.

**O-02 · SQLite connections without context manager (intelligence.py)**
- **Location:** `get_draft_class._query()` L166, `get_team_draft_class` L262
- **Suggestion:** Convert to `with sqlite3.connect(DB_PATH) as conn:` to guarantee close on exception path.

**O-03 · print() in load_all() bypasses log level (data_manager.py)**
- **Location:** 20+ `print()` calls throughout `load_all()` (L385–659)
- **Suggestion:** Replace `print(...)` with `log.info(...)` throughout `load_all()` for consistent log-level filtering.

**O-04 · YEAR_TO_SEASON range hardcoded to season 10 (intelligence.py)**
- **Location:** L45: `{yr: yr - _YEAR_BASE for yr in range(2025, 2035)}`
- **Suggestion:** Remove the dict; replace with inline arithmetic `yr = season + _YEAR_BASE`. The range is misleading — it implies coverage it doesn't provide for a 95-season league.

**O-05 · ensure_future log_query() silently drops write errors (oracle_cog.py)**
- **Location:** L3050, L3130, L3290, L3500, L4375, L4415
- **Suggestion:** Wrap each `asyncio.ensure_future(coro)` in a small error-logging shim so aiosqlite failures are visible at WARNING level rather than silently dropped.

---

## Phase 4 — CLAUDE.md Health Check

| Check | Status |
|-------|--------|
| Focus files in module map | `oracle_cog.py` ✅ (Module Map). `data_manager.py` ✅ (Data Flow). `analysis.py`, `intelligence.py` are Oracle support modules — non-cog files, acceptable to omit from module map. |
| Wednesday audit task coverage | Should include `intelligence.py` in focus file list — verify scheduled task SKILL.md. |
| Dead file references in focus files | None found. No QUARANTINE imports detected. |
| API gotchas respected | `get_weekly_results()` correctly uses `status IN ('2','3')` ✅. `weekIndex` 0-based conversion at L823 confirmed ✅. |
| atlas_ai.generate() usage | oracle_cog.py and oracle_agent.py both call `atlas_ai.generate()` for AI generation ✅. The three `_get_claude()/_get_gemini()` calls in oracle_cog.py are availability gates only, not generation calls. |
| ATLAS_VERSION bump required | No — audit pass only, no code changes. |

**CLAUDE.md updates needed:** None. No new modules introduced, no architectural changes. Findings are operational/maintenance.

---

## Summary Table

| ID | Severity | File | Line(s) | Title |
|----|----------|------|---------|-------|
| W-01 | Warning | oracle_cog.py | 2883, 3049 | Double embed_and_store per TSL query |
| W-02 | Warning | oracle_memory.py | 190, 399 | Unbounded conversation_memory + O(n) vector search |
| W-03 | Warning | oracle_cog.py | 70–71, 2872, 4232 | Unbounded _oracle_message_ids / _chain_roots |
| W-04 | Warning | data_manager.py:764 / oracle_cog.py:1199,2061,2291 | Multiple | get_last_n_games() blocks async event loop |
| O-01 | Observation | oracle_cog.py | 1258, 2083, 2404 | Private atlas_ai method access as availability gate |
| O-02 | Observation | intelligence.py | 166, 262 | SQLite connections without context manager |
| O-03 | Observation | data_manager.py | 385–659 | print() in load_all() bypasses log level filtering |
| O-04 | Observation | intelligence.py | 45 | YEAR_TO_SEASON range hardcoded to season 10 |
| O-05 | Observation | oracle_cog.py | 3050, 3130, 3290, 3500, 4375, 4415 | ensure_future log_query() silently drops write errors |

**Confirmed clean:** oracle_agent.py sandbox (no write escalation), oracle_query_builder.py SQL safety (parameterized + whitelist), analysis.py DataFrame handling (all .copy()), data_manager.py atomic state swap (GIL-safe pointer reassignment), intelligence.py hot/cold/clutch/draft logic.

---

*Priority order for fixes: W-04 (event loop block — immediate latency impact), W-01 (double-store — data integrity), W-02 (memory growth — long-term stability), W-03 (unbounded sets — operational hygiene).*
