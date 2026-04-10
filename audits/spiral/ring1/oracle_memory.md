# Adversarial Review: oracle_memory.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 666
**Reviewer:** Claude (delegated subagent)
**Total findings:** 18 (3 critical, 8 warnings, 7 observations)

## Summary

`oracle_memory.py` is a load-bearing memory layer (instantiated at import time in `bot.py` and `oracle_cog.py`) with several class-of-bug problems: a non-thread-safe init flag wrapping ALL writes, FTS5 triggers that will fail loudly on the first delete/update because of an unguarded virtual-table init, an unbounded context block that has no token budget despite a docstring promising one, and broad `except Exception` swallowing across every method that means embedding/storage failures vanish into logs while the bot keeps running on degraded data. The vector search loads up to 2,000 BLOBs from disk into Python on every call and decodes them with `json.loads` — a multi-second latency tax that the docstring claims is "<100ms" but is not benchmarked. None of the findings are write-corrupting on their own, but together they make this module brittle in exactly the failure modes (silent context loss, schema drift, retry storms) that an Oracle conversation memory cannot afford.

## Findings

### CRITICAL #1: `_initialized` global gates schema creation but is not concurrency-safe — first concurrent burst races and writes against a non-existent table

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:107-165`
**Confidence:** 0.85
**Risk:** `_ensure_schema()` is the only thing that creates `conversation_memory`, `conversation_memory_fts`, and the triggers. It is gated by a single module-level boolean `_initialized`. There is no `asyncio.Lock`. If two coroutines call any public method (`store_turn`, `retrieve_context`, `embed_and_store`) before the first one finishes the schema work, both will see `_initialized=False`, both will enter the `try` block, and both will execute the same DDL statements concurrently against the same `aiosqlite` file. The migrate path on line 158-161 will also race — both coroutines will instantiate a fresh `OracleMemory(path)` and call `migrate_from_conversation_history` against an empty target. Even if the COUNT check inside `migrate_from_conversation_history` (line 631-636) wins on one side, the loser may still be mid-INSERT when the winner reads the count, leaving partial migration races.
**Vulnerability:** `_initialized` is set to `True` only AFTER the migration call (line 154 vs line 159). This means a second coroutine can enter `_ensure_schema` while the first is still running migration, see `_initialized=False`, and try to migrate again. Worse, multi-line DDL parsing via `split(";")` (line 132) silently ignores trailing/embedded semicolons in comments and produces statements that may execute out of order if SQLite under aiosqlite serializes them via different threads.
**Impact:** On a cold-start spike (e.g., bot restart while users are messaging) the first ~5 messages can fire `embed_and_store` and `build_context_block` simultaneously. Likely outcomes: `OperationalError: no such table: conversation_memory_fts`, `OperationalError: trigger ... already exists`, or duplicate migration insertions doubling the migrated turn count. All of these are caught by the broad `except Exception` blocks below and vanish — the bot will respond to the user with no conversation context, and Oracle will silently lose the first turn of the session.
**Fix:** Replace `_initialized` boolean with an `asyncio.Lock` PLUS a flag, and check the flag *inside* the lock. Move `_initialized = True` to AFTER schema creation but BEFORE the migration call, and gate the migration on a separate idempotent `_migrated` flag held under the same lock. Better: do schema setup once in `setup_hook()` of an explicit cog (not at module import) and let public methods assume the schema exists.

### CRITICAL #2: FTS5 triggers reference `entities` column which is in the indexed table — first DELETE or UPDATE on a row whose `entities` is NULL passes through fine, but the trigger DDL is created via `CREATE TRIGGER IF NOT EXISTS` after a separate `CREATE VIRTUAL TABLE` that may fail silently

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:78-103, 137-150`
**Confidence:** 0.82
**Risk:** The FTS virtual table is created on line 137-141 inside a `try/except: pass`. If the CREATE fails for any reason other than "already exists" (e.g., the SQLite build doesn't support FTS5, or the file is read-only) the exception is swallowed. Then on lines 144-150 the trigger DDL is executed against a *non-existent* FTS table, but each `CREATE TRIGGER IF NOT EXISTS` is *also* wrapped in `try/except: pass`. So a fully broken FTS environment passes through `_ensure_schema()` with `_initialized = True`. Subsequent `INSERT INTO conversation_memory` succeeds (no triggers exist), but `search_fts()` will throw `OperationalError: no such table: conversation_memory_fts` on every call — and that exception is swallowed by line 393-395 returning `[]`. Net result: silent FTS-search blindness with no telemetry. The user has no idea retrieval is degraded.
**Vulnerability:** Two layers of `except Exception: pass` (lines 140-141, 149-150) intentionally hide schema creation failures. The intent is "ignore already-exists errors", but the error class for "already exists" is the same `OperationalError` as "FTS5 not compiled in" or "disk I/O error". Catching the broad exception type instead of inspecting `e.args[0]` for the literal "already exists" string means real init failures are indistinguishable from benign re-runs.
**Impact:** A SQLite build without FTS5 (rare on modern Python but real on locked-down Linux distros and some Windows wheels) silently disables 1/3 of the hybrid retrieval signal. Operators cannot distinguish "no FTS results because FTS isn't installed" from "no FTS results because the user said something irrelevant." Same goes for the trigger creation: if a single trigger DDL fails (e.g., a typo introduced in a future edit), all three fail silently and the FTS index drifts out of sync with the base table — search starts returning stale or missing rows.
**Fix:** Catch `aiosqlite.OperationalError` *only*, and inspect `str(e)` for `"already exists"`. Re-raise everything else. Surface the error via `log.error(...)` not silently. Better: query `sqlite_master` once to check if the FTS table and triggers exist, and only run DDL when they're missing. Add a `SELECT 1 FROM conversation_memory_fts LIMIT 1` smoke check at the end of `_ensure_schema` and bail loudly if it fails.

### CRITICAL #3: `embed_and_store` swallows ALL exceptions from the embedding call AND from the underlying store call, masking quota exhaustion and corrupted DB writes

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:226-254, 222-224`
**Confidence:** 0.88
**Risk:** `embed_and_store` is the public write API called from `bot.py:627` after every Oracle response. The flow is: (1) `time.time()` (2) `import atlas_ai` (3) `await atlas_ai.embed_text(question)` inside a `try/except Exception: embedding=None` (4) call `store_turn` which has its own `try/except Exception` returning `None` on failure. If embedding fails (Gemini quota exhausted, network blip, atlas_ai not yet loaded, ImportError on first call) the embedding becomes `None` silently. If the subsequent `store_turn` *also* fails (DB locked, disk full, schema not initialized), it returns `None` silently. The caller in `bot.py:627` does NOT check the return value (`await _atlas_mem.embed_and_store(...)` with no assignment). So both failure modes result in zero observability and zero retry.
**Vulnerability:** The CLAUDE.md "Flow Economy Gotchas" rule about silent excepts in admin-facing views applies in spirit here: this is the conversation memory write path. A silent failure means a user's question and ATLAS's answer are lost forever. Worse, if `_ensure_schema` failed earlier and `_initialized` is still `False`, every subsequent call re-attempts schema creation (CRITICAL #1) and then probably also fails to write — but neither the user nor an operator sees any difference in bot behavior. The Gemini embedding API is on the free tier (1,500/day per atlas_ai docstring) — at the bot's typical message rate, a busy day will exhaust the quota mid-afternoon and silently start dropping all embeddings. The conversation memory becomes vector-search-blind with no alarm.
**Impact:** Conversation continuity degrades silently. Follow-up questions ("remind me what I asked yesterday about Mahomes") will return wrong or empty results. The vector retrieval branch of `retrieve_context` (lines 519-533) becomes a no-op. The audit path is broken — `oracle_query_log` is not called from `embed_and_store` so there is no fallback observability either.
**Fix:** (a) Catch and log embedding failures with a counter or rate-limited `log.warning`. (b) Have `embed_and_store` raise on `store_turn` returning `None` so the caller can decide whether to retry. (c) Track embedding-quota state in a module-level int and degrade gracefully (skip vector search) but emit a structured warning. (d) Audit every `await _atlas_mem.embed_and_store(...)` call site for return-value handling.

### WARNING #1: `build_context_block` has NO max-token budget despite the focus block explicitly demanding one

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:538-562`
**Confidence:** 0.95
**Risk:** The function pulls up to `max_turns=8` turns from `retrieve_context` and concatenates them into a single string with `t['question']` (untruncated) and `t['answer'][:200]` (truncated to 200 chars). Question text is unbounded. A single user could submit a 50,000-character question; the entire string lands in the context block, then in the prompt. The `f"  Q{i}: {t['question']}"` line provides no length cap, so a worst case is `8 turns × 50K chars × 2` for question + SQL roughly = ~800K chars in the context block before prompt assembly.
**Vulnerability:** The docstring on `build_context_block` does not mention token budgeting at all, but Claude/Gemini have hard input-token limits (~200K for Claude 4, less for Gemini Flash). A single rogue large question stored in memory will silently bloat every subsequent context block until the user issues `/forget` — and `/forget` requires the user to know they have a memory problem. Worse, the SQL field `t['sql_query']` is also untruncated (line 555). Stored SQL from `tsl_ask_async` can be hundreds of lines long for complex queries.
**Impact:** Eventually a user hits token-limit errors on their AI call, with no warning and no way to debug from the user side. Cost escalates linearly with context block size — every Oracle call after a poisoned turn pays the bloat. This is the exact "What if the user's history is huge?" failure mode the focus block called out.
**Fix:** Add a `max_chars: int = 4000` parameter (or `max_tokens` if a tokenizer is available) and truncate per-turn: `q = t['question'][:300]`, `a = t['answer'][:200]`, `sql = (t.get('sql_query') or '')[:300]`. Track running char count and break when exceeded. Also truncate `question` *before* storage in `store_turn` (defense in depth).

### WARNING #2: Cross-user contamination via `discord_id is None` branch in `search_fts` and `search_vector`

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:347-395, 399-454`
**Confidence:** 0.80
**Risk:** Both `search_fts` and `search_vector` accept `discord_id: int | None = None` and skip the user filter when `None`. The current call site in `retrieve_context` always passes a non-None `discord_id`, so this is currently safe. But the API surface invites future misuse: any caller (a future cog, an admin command, a debug log dump) that calls `search_fts(query)` without a discord_id gets cross-user results back.
**Vulnerability:** The privacy contract here is implicit. There is no docstring warning, no assertion, no audit. A new contributor reading the code sees an optional parameter and assumes the default is safe. Combined with `build_context_block`'s prompt text "use the above history to resolve what is being referenced" — if a result from a different user ever leaks in, the AI will happily resolve "him" to a player User-A discussed and present it to User-B as if User-B asked.
**Impact:** Privacy breach: User A's conversation surfaces in User B's prompt. For an Oracle that talks about player stats this is mostly low-stakes (no PII), but if an admin query stored a username, draft pick, or embarrassing question, it could leak. The risk grows if anyone ever uses `OracleMemory` to store moderation notes or admin questions.
**Fix:** Make `discord_id` REQUIRED (no default). Provide a separate `search_fts_global(query, requires_admin=True)` for the rare admin case, and gate it on a permission check. Add a docstring warning explicitly about cross-user data flow.

### WARNING #3: Vector search loads up to 2000 rows × ~768-float blobs into Python on EVERY call, with `json.loads` decode per row — the "<100ms" claim is unverified and unrealistic at scale

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:399-454`
**Confidence:** 0.78
**Risk:** The query (lines 419-434) hard-caps at 2,000 most-recent rows. Each row's embedding is stored as `json.dumps(embedding).encode()` in a BLOB column (line 207) — that's `~768 floats × ~12 bytes/float ≈ 9.2KB per row` of textual JSON. 2000 rows = ~18MB read from disk per query, plus 2000 `json.loads` calls (Python's JSON decoder is quite slow on float arrays — ~50ms for 2000 vectors), plus 2000 `_cosine_similarity` calls in pure-Python with `math.sqrt` and zip generators. Estimated wall time on a real bot: 200-800ms, NOT "<100ms". This blocks the event loop for the entire duration because aiosqlite is async but the JSON decode and cosine math are CPU-bound and inside the same coroutine — meaning every other Discord interaction stalls for that window.
**Vulnerability:** The docstring on line 408 actively misleads future maintainers: `"At ~36K rows/year this takes <100ms — no vector DB needed."` There is no microbenchmark, no profiling, and the math doesn't add up. Worse, the LIMIT 2000 means once the bot crosses ~2000 turns per user, the vector search silently drops the oldest matches — recall degrades for old conversations without any warning. Globally (no discord_id) the LIMIT 2000 means the oldest 34,000 turns per year are completely invisible.
**Impact:** Event loop stalls block heartbeats and degrade Discord responsiveness across all interactions, not just Oracle. Stale-recall: a user asking "do you remember what I said about Brock Purdy in 2024" will only ever match within the last 2000 conversation turns on the entire server. As the bot scales, this gets worse.
**Fix:** (a) Move the loop into `asyncio.to_thread()` or use `numpy.frombuffer` after switching the storage format to `np.float32.tobytes()` (3KB per row, ~10x faster decode). (b) Replace JSON-as-BLOB with a real binary format. (c) Add `created_at >= ?` cutoff (e.g., 30 days) on top of LIMIT for default queries. (d) Actually benchmark and put the result in the docstring. (e) Consider sqlite-vss or sqlite-vec extensions which do vector indexing inside SQLite.

### WARNING #4: `embed_and_store` does `import atlas_ai` inside the function body — one ImportError per failed call, no caching of the failure

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:246-249`
**Confidence:** 0.75
**Risk:** `import atlas_ai` is performed inline on line 247. On the happy path Python's import cache makes this nearly free. But if `atlas_ai` fails to import the FIRST time (e.g., circular dependency during cog load order, missing env var raising at module level), the ImportError is caught by `except Exception` on line 249 and `embedding=None`. The next call retries the import — good for transient circular import resolution, but bad for permanent failures because every single call pays the import-attempt cost AND emits a hidden error.
**Vulnerability:** The same pattern is repeated on line 520 in `retrieve_context`. Two import sites, both protected by `except Exception: pass`. If `atlas_ai` is structurally broken, the bot has no central log saying "embeddings disabled — atlas_ai unavailable." The user just experiences poor Oracle recall.
**Impact:** Cog load order is fragile. If `oracle_cog` ever loads before `atlas_ai` is fully defined (or `atlas_ai` raises during its own import for any reason), all embedding calls go silent. No telemetry, no health-check.
**Fix:** Import `atlas_ai` at module top (`from atlas_ai import embed_text`). If circular import is the reason it's done lazily, document that. Cache an `_embed_unavailable: bool` flag on first failure and short-circuit subsequent calls until next bot restart.

### WARNING #5: `retrieve_context` swallows BOTH FTS and Vector failures with bare `except Exception: pass` — no logging at all

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:507-533`
**Confidence:** 0.92
**Risk:** Lines 515-516 and 532-533 contain bare `except Exception: pass` blocks. The comment says `# FTS failure is non-fatal` and `# Vector search failure is non-fatal`, which is fine as a fallback policy — but there is *zero* logging. If both sub-systems silently break, `retrieve_context` returns only the sliding window from `get_recent`. The user gets degraded recall and no operator sees an error.
**Vulnerability:** The CLAUDE.md "Flow Economy Gotchas" rule that "Silent except Exception: pass in admin-facing views is prohibited. Always log.exception(...)" applies here in spirit. This module is admin-adjacent (Oracle is the data-query interface and is used heavily by commissioners). Two of the three retrieval paths can be silently broken in production with no signal.
**Impact:** A regression in `search_fts` or `search_vector` (e.g., schema drift, sqlite version skew) is invisible until someone manually queries `oracle_query_log` and notices avg_latency dropped because expensive queries now return nothing.
**Fix:** Replace `except Exception: pass` with `except Exception as e: log.warning("FTS retrieval failed in retrieve_context: %s", e)` and the same for vector search. Optionally add a counter that increments per failure for `/get_stats`.

### WARNING #6: `migrate_from_conversation_history` runs unbounded INSERT-SELECT inside a single transaction — old DB with millions of rows will lock the connection for minutes

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:620-666`
**Confidence:** 0.65
**Risk:** The migration runs `INSERT INTO conversation_memory ... SELECT ... FROM conversation_history ORDER BY created_at` (lines 647-659) as a single statement. There is no batch size limit, no progress reporting, no chunking. SQLite holds the writer lock for the duration. If the source table has 100K+ rows, the operation can take minutes — during which time aiosqlite returns `OperationalError: database is locked` to every concurrent Oracle query. The migration is also kicked off from `_ensure_schema()` (line 158-161) on first init, which means it can run during bot startup AND can race with itself per CRITICAL #1.
**Vulnerability:** Each FTS trigger (line 87-90) fires per-row on insert, so the migration ALSO populates `conversation_memory_fts` row-by-row. Trigger overhead × N rows can multiply the runtime 5-10x. The 30-second timeout on line 629 (`timeout=30`) does NOT bound the migration — the timeout is the *connection* timeout for getting a lock, not the statement timeout.
**Impact:** First-time deployment on a bot with significant conversation history will appear to hang during startup, possibly cause Discord to disconnect the gateway from inactivity, and produce confusing "database is locked" errors in logs. After the migration finally finishes, behavior normalizes — but the operator has no way to know whether the bot is hung, crashed, or migrating.
**Fix:** Chunk migration in batches of e.g. 1000 rows. Add progress logging every chunk. Move the migration entirely out of `_ensure_schema` and require it be invoked explicitly via an admin command. Drop the FTS triggers temporarily, bulk-insert, then `INSERT INTO conversation_memory_fts(conversation_memory_fts) VALUES('rebuild')` to rebuild the FTS index in one shot.

### WARNING #7: `_cosine_similarity` is a pure-Python implementation — silent precision/perf footgun

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:110-117`
**Confidence:** 0.55
**Risk:** Pure-Python cosine over Python `list[float]` is roughly 100-200x slower than `numpy.dot(a,b) / (np.linalg.norm(a) * np.linalg.norm(b))` for 768-dim vectors. Combined with `search_vector`'s 2000-row scan (WARNING #3), this dominates the latency. There is also no input-length validation: if `a` and `b` have different lengths `zip` silently truncates, returning a misleading similarity score for vectors that don't match the embedding dimension.
**Vulnerability:** A future change to `embed_text` that returns 1536-dim vectors (e.g., switching to OpenAI text-embedding-3-large) without re-embedding existing rows will produce silently-wrong similarity scores for old rows — `zip` truncates to the shorter length and the cosine computation completes "successfully" with garbage data. No exception, no warning, just stale recall ranking by a wrong metric.
**Impact:** Embedding-dimension drift after a model upgrade is invisible. Performance bottleneck baked in.
**Fix:** Use `numpy`. Validate `len(a) == len(b)` and raise `ValueError` if not. Store the embedding model name alongside the vector in a new column; reject mismatches in `search_vector`.

### WARNING #8: `_sanitize_fts` strips operators but does not handle FTS5 phrase column qualifiers, NEAR(), or column filter syntax

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:339-345`
**Confidence:** 0.40
**Risk:** The regex `[\"*?:^(){}+\-~]` removes most FTS5 special characters but does not strip the `:` *outside* the bracket already covered, and does not strip `NEAR/N` syntax. After sanitization, a query like `passing NEAR yards` becomes `passing NEAR yards` which FTS5 will parse as a NEAR operator. Also, the regex strips the `-` character, which means a player name like `T'Vondre Sweat` becomes `T Vondre Sweat` — fine — but `Ja'Marr Chase` becomes `Ja Marr Chase` which may or may not match stored text depending on FTS5 tokenization.
**Vulnerability:** A user typing a query containing the literal word `NEAR` or `OR` or `AND` could trigger unexpected FTS5 boolean logic. The regex misses the `'` (single quote) which can affect tokenization, and misses `&` and `|` which are not FTS5 operators but might confuse other backends.
**Impact:** Mostly minor — search returns slightly wrong results for queries containing English words that happen to be FTS5 keywords. No crash, no security risk.
**Fix:** Wrap the entire query in `"..."` (double quotes) AFTER sanitization to force FTS5 phrase mode: `return f'"{sanitized}"'`. This eliminates all operator interpretation. Or use a token-by-token quoted approach.

### OBSERVATION #1: Module-level `OracleMemory()` instantiation duplicated across `bot.py` and `oracle_cog.py`

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:182-186` (and call sites)
**Confidence:** 0.85
**Risk:** `bot.py:139` and `oracle_cog.py:60` both create their own `OracleMemory()` instance at import time. There is no shared singleton. Each instance has its own `self._db_path` but shares the module-level `_initialized` flag, so they don't conflict on schema. However, they fragment the API and make it harder to add caching, locking, or telemetry.
**Vulnerability:** Future code that adds a connection pool or in-memory cache to `OracleMemory` will be confused by having two instances. Already, two sites must update if the constructor signature ever changes.
**Impact:** Minor maintenance friction.
**Fix:** Provide a `get_oracle_memory()` accessor that returns a process-wide singleton.

### OBSERVATION #2: `_ensure_schema` parses DDL via `split(";")` which breaks on any semicolon inside a string literal or comment

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:132-150`
**Confidence:** 0.90
**Risk:** `_SCHEMA_SQL.split(";")` is fragile. Today no statement contains an embedded semicolon. If a future schema change adds a `DEFAULT '; DROP TABLE...'` (extreme example) or even a `CHECK` constraint with a string literal containing `;`, the split breaks the statement in half and the second half raises a syntax error which is caught (per CRITICAL #2 family) and ignored.
**Impact:** Latent footgun for future schema additions.
**Fix:** Use `db.executescript(_SCHEMA_SQL)` which handles multi-statement DDL natively.

### OBSERVATION #3: `get_stats` uses `db.execute_fetchall` then unpacks `row[0]` as a tuple — fragile if rows aren't tuple-like

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:587-616`
**Confidence:** 0.45
**Risk:** Lines 597 and 605 do `total, users = row[0] if row else (0, 0)`. The `db.row_factory` is not set in `get_stats`, so default tuple rows are returned and unpacking works. But every other method sets `db.row_factory = aiosqlite.Row` — if a future cleanup decides to set the row factory at the connection level (e.g., move to a connection pool), this method will break because `aiosqlite.Row` is mapping-like, not tuple-like.
**Impact:** Potential `TypeError: cannot unpack non-iterable Row object` after a refactor.
**Fix:** Be explicit: `total = row[0]['total']` and `users = row[0]['users']` (after setting `row_factory = aiosqlite.Row`), or use parameterized aliases.

### OBSERVATION #4: `prune_old_turns` deletes rows but does NOT trigger an FTS index rebuild — index can grow without bound across prune cycles

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:458-472`
**Confidence:** 0.55
**Risk:** The DELETE trigger `cmem_ad` (line 92-95) inserts a `'delete'` command into `conversation_memory_fts` per row, which marks the row as deleted in FTS but does NOT physically reclaim space. Over months of pruning, the FTS index gets fragmented. There is no `INSERT INTO conversation_memory_fts(conversation_memory_fts) VALUES('optimize')` call anywhere.
**Impact:** Slowly degrading FTS performance and growing disk usage.
**Fix:** Add an optional `optimize: bool = False` parameter and call the FTS5 optimize command at the end of large prunes.

### OBSERVATION #5: `embed_and_store` accepts arbitrary `**kwargs` and forwards them to `store_turn` — typos pass through silently

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:226-254`
**Confidence:** 0.50
**Risk:** A caller that mistypes `tier` as `teir` will pass `teir=3` through `**kwargs` to `store_turn`. `store_turn` will raise a `TypeError: unexpected keyword argument 'teir'` — which is then caught by `store_turn`'s broad `except Exception` (line 222-224) and logged as "Failed to store turn". The actual cause is a typo, but the log will not say so.
**Impact:** Difficult-to-debug call site bugs.
**Fix:** Define explicit named parameters in `embed_and_store` matching `store_turn`'s signature, or use Python's `inspect` to validate kwargs against `store_turn`'s parameters.

### OBSERVATION #6: `oracle_query_log` writes don't go through any rate-limit or batch — high-traffic days will multiply DB writes

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:256-292`
**Confidence:** 0.40
**Risk:** Every query logs a row. No batching. Over a season, oracle_query_log can grow to hundreds of thousands of rows. There is no cleanup function (only `prune_old_turns` for `conversation_memory`).
**Impact:** Disk usage grows unboundedly. `get_stats` queries (which scan the full log table) get slower over time.
**Fix:** Add a `prune_old_query_log` method and call it from a periodic task. Add an index on `(discord_id, created_at)`.

### OBSERVATION #7: `_FTS_SQL` has a comment saying "may already exist" but the constant lacks `IF NOT EXISTS`

**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_memory.py:76-83`
**Confidence:** 0.30
**Risk:** The comment on lines 76-77 says `CREATE VIRTUAL TABLE doesn't support IF NOT EXISTS in all SQLite versions`. This is true historically but not since SQLite 3.9 (2015). All modern Python SQLite shims support it. The defensive code is therefore unnecessary, but the broad except (CRITICAL #2) was added to compensate, and that broad except is the actual problem.
**Impact:** Code complexity that exists to work around a non-issue, leading to the larger CRITICAL #2 bug.
**Fix:** Use `CREATE VIRTUAL TABLE IF NOT EXISTS conversation_memory_fts USING fts5(...)` and remove the broad except wrapper.

## Cross-cutting Notes

- **Silent except patterns are ubiquitous in this file** (lines 140-141, 149-150, 163-164, 222-224, 249, 291-292, 316-318, 335-337, 393-395, 452-454, 470-472, 515-516, 532-533, 581-583, 614-616, 664-666). Every public method has at least one silent failure mode that returns an empty result. Combined with `bot.py:627`'s fire-and-forget call site (no return value check), the entire conversation memory subsystem can be 100% broken in production with zero user-visible error and zero log entries above WARNING. Recommend a sweep to (a) replace `except Exception` with specific exception types where possible, (b) log every caught exception with `log.warning` or `log.exception`, (c) add a `/get_stats` field for failure counts since last restart.
- **Module-import side effects** — `_atlas_mem = OracleMemory()` at import time (bot.py:139) is fine because the constructor only stores the path. The schema work is deferred to first method call. This is correct, but the docstring at the top of the file should make it explicit: "import is side-effect-free; first method call lazily creates the schema."
- **Embedding model identity is not stored alongside vectors.** If `atlas_ai.embed_text` ever switches to a different model (different dimension or different distribution), all stored vectors silently become garbage, and `_cosine_similarity` will silently produce wrong rankings (WARNING #7). Recommend adding an `embedding_model TEXT` column and rejecting mismatches in `search_vector`.
- **No `/forget` audit trail.** `forget_user` deletes data unconditionally with no record of what was deleted, when, or by whom. For a system with privacy implications (per the focus block: "Personal data: stored embeddings + raw conversation text — privacy concerns, retention policy, deletion."), an audit log entry would be appropriate — even just a row in `oracle_query_log` with `intent='forget'` and `rows_returned=count`.
