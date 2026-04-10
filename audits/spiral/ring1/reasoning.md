# Adversarial Review: reasoning.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 1064
**Reviewer:** Claude (delegated subagent)
**Total findings:** 23 (4 critical, 9 warnings, 10 observations)

## Summary

`reasoning.py` is a two-phase LLM analyst pipeline that generates Python and SQL from natural language, then evaluates it inside a sandbox. The sandbox itself is the strongest part of the file (AST validation + restricted builtins), but the surrounding code has several real correctness and reliability gaps: an architectural sandbox-escape surface via the embedded `PREBUILT_METRICS_CODE` (functions are shared across trust boundaries), fragile timeout cancellation that can race the SQLite connection, blocking I/O on the event loop in `generate_analysis_code()` / `reason()`, broken markdown-fence stripping, and a half-orphaned API surface that nothing in the bot currently calls except `oracle_agent.py` (which only consumes the security primitives). Ship-blocker level depends entirely on whether `reason()` / `query_discord_history()` are wired into a hot path elsewhere; if they are, this needs fixes before promoting any new caller.

## Findings

### CRITICAL #1: Helper-function leak bridges the sandbox trust boundary
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:412-421`
**Confidence:** 0.85
**Risk:** The sandbox security model can be defeated by closure-walking prebuilt helpers.
**Vulnerability:** `build_exec_env()` evaluates `PREBUILT_METRICS_CODE` in a separate dict (`prebuilt_env`) that is given **unrestricted** builtins (the comment at L411 even calls this out as intentional). It then copies callables (and explicitly `_norm`) into the restricted sandbox env. Those callables remain bound to the original `prebuilt_env` globals — i.e. their `__globals__` still has full `__builtins__`. Generated code is forbidden from touching dunders by `validate_sandbox_ast()` (L336-375), but the AST guard only blocks attribute *names* starting with `_`. An attribute read like `compute_power_scores.__globals__` is blocked by the dunder-attribute rule, **but** any read like `_norm` returns a function whose closure references full builtins. The net effect is that trusted helper objects with elevated privileges live in the same namespace as attacker-controlled strings. That is an *architectural* trust-boundary violation — the invariant the file advertises ("LLM code cannot reach unrestricted builtins") is structurally broken even if no current attack path fires.
  - Concretely: `_norm` is exposed at L420 with a leading underscore, but `validate_sandbox_ast()` only blocks **attribute** access starting with `_`, not **name** access. So generated code may legally call `_norm(...)` — and although `_norm` itself is harmless, every other function that the same `prebuilt_env` defines is now reachable, including any future helper.
**Impact:** Sandbox escape — arbitrary Python in the bot process if a future `PREBUILT_METRICS_CODE` helper inadvertently exposes a path. Today, the attack window is narrow but the architectural invariant is already broken.
**Fix:** Either (a) re-evaluate each prebuilt function inside the restricted env so its `__globals__` *is* the restricted dict, or (b) wrap each exposed callable in a `def shim(*a, **kw): return _orig(*a, **kw)` defined inside a `compile()`-ed code object that is evaluated in the restricted env. Cleanest: never share function objects across trust boundaries — recompile the metric helpers into the restricted globals on every `build_exec_env()` call.

### CRITICAL #2: Threading.Timer cancellation races `conn.close()` and can interrupt a closed connection
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:889-906`
**Confidence:** 0.80
**Risk:** Use-after-close on the SQLite connection; intermittent hard crashes or process aborts under load.
**Vulnerability:** `execute_sql_safe()` starts a `threading.Timer` that calls `conn.interrupt()` on the SQLite connection after `SQL_TIMEOUT_S` (8s). The `finally` block does `timer.cancel()` then `conn.close()`. `Timer.cancel()` only prevents the timer from *starting* — if the callback is already running, `cancel()` returns and the main thread proceeds to close the connection while the background timer thread is still inside `conn.interrupt()`. SQLite’s `interrupt()` is documented as safe to call from another thread, **but only if the connection is still open**. After `conn.close()`, the connection’s underlying handle is freed, and a concurrent `interrupt()` will dereference a freed pointer — Python’s sqlite3 module does not guard against this and will segfault or raise an obscure `sqlite3.ProgrammingError` from the timer thread (which is silently swallowed by the bare `except Exception: pass` at L895).
  - Additional bug: `_timeout()` is itself a bare `except Exception: pass` (L894-896) — a textbook silent failure in an admin/operator-facing subsystem.
**Impact:** Random timeouts in production cause either (a) the SQL pipeline silently giving up without cancellation actually working (because the swallowed exception hides the real error), or (b) under heavy load, segfaults that take down the bot process entirely.
**Fix:** Use `threading.Lock` to coordinate timer-callback / main-thread exclusivity, or simpler: switch to SQLite’s built-in `progress_handler` (`conn.set_progress_handler(callback, n_ops)`) which is in-thread and races nothing. Replace `except Exception: pass` with `_sql_log.exception("[SQL] interrupt failed")`.

### CRITICAL #3: `generate_analysis_code` / `reason` never wrap `get_schema()` in an executor — blocking pandas calls on the event loop
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:548-552, 561-650`
**Confidence:** 0.85
**Risk:** Event-loop stalls of 100ms-2s+ depending on DataFrame size, on every reasoning request.
**Vulnerability:** `get_schema()` (L96-114) directly calls `build_schema_prompt()` (L71-83), which iterates every column of seven DataFrames and calls `df[col].min()`, `df[col].max()`, `df[col].dropna().unique()`, and `df.head(3).to_string(index=False)`. With ~100 columns across df_offense / df_defense / df_team_stats / df_players (each potentially thousands of rows), this is dozens of milliseconds of pure-Python pandas work — easily 200-500ms cold. It is called from inside the async `generate_analysis_code()` (L550) and `reason()` (L582, L625) without `run_in_executor`. The 5-minute TTL helps after the first call, but every TTL expiration blocks the loop again, and the doc comment at L17-19 even notes that `bot.py` explicitly invalidates the cache after every sync — so this **always** blocks on the next reasoning call after a sync cycle.
  - Additionally L625 inside the retry loop calls `get_schema()` *again* — even though the same value was just computed for the initial prompt at L582. That's a guaranteed second blocking call inside the hot path.
**Impact:** Every reasoning call on a freshly-synced cache stalls Discord heartbeats for hundreds of milliseconds. Multiple concurrent users → cumulative serialization → discord heartbeat warnings, potential gateway disconnects.
**Fix:** Wrap `get_schema()` in `await asyncio.get_running_loop().run_in_executor(None, get_schema)`, and refactor `reason()` to compute the schema once and reuse it across retries.

### CRITICAL #4: `validate_sandbox_ast` misses string-concatenation attribute names and whitelisted `format` / frame names
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:336-376, 286-322`
**Confidence:** 0.70
**Risk:** Sandbox escape via name concatenation, `format()` reflection, or traceback frame traversal.
**Vulnerability:** `validate_sandbox_ast()` blocks `_ast.Attribute` whose `attr` starts with `_`, and `_ast.Constant` strings that look like `__dunder__`. It does **not** block:
  - **String concatenation in attribute names**: code like `getattr(x, "__cl" + "ass__")` passes both the Attribute check and the Constant check because neither substring is itself a dunder. The custom safe `getattr` lambda at L304 strips names that *start* with `_`, but the attacker can route around it by passing a frozen string built from non-underscore substrings; the lambda check runs at call time on the concatenated string, so it catches leading-underscore patterns *if they assemble that way*. But **non-underscore** attribute names on traceback / frame objects are reachable: `tb_frame`, `f_back`, `f_globals`, `gi_frame`, `cr_frame`, `ag_frame` all start with letters. None are in the attribute blocker, and the `getattr` lambda does not block them.
  - **`format()` builtin** is in `_SAFE_BUILTINS` (L309). `format(obj, "format_spec")` calls `obj.__format__(spec)`, which user code can override on a custom subclass passed via the data path. Combined with class instances created by `staticmethod` / `classmethod` / `property` (also exposed), an attacker can build a reflective object.
  - The `Try`/`Raise` block at L370-375 was added explicitly to prevent `tb_frame` traversal on caught exceptions. **Good**, but does not cover frame objects surfaced via `sys.exc_info()` or via generator / coroutine objects that are `send`-able.
**Impact:** Sandbox escape is possible with effort. None of the attacks are one-shot; all require a creative attacker. But the file’s docstring at L31-35 advertises this as a *security* boundary, so it should be defended in depth.
**Fix:** Add a Subscript check rejecting any string subscript starting/containing `_`. Add a name-level check rejecting `tb_frame`, `f_back`, `f_globals`, `f_locals`, `gi_frame`, `cr_frame`, `ag_frame`. Drop `format` from `_SAFE_BUILTINS` (use f-string formatting in the analyst’s code instead). Document explicitly that the sandbox is *defense-in-depth*, not the sole control — the LLM is trusted to be benign unless prompt-injected.

### WARNING #1: Markdown fence-stripping is broken — only strips lines starting with backticks, leaves `python` language tag in code
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:535-542`
**Confidence:** 0.95
**Risk:** Generated code starts with `python\n...` and immediately throws `SyntaxError: invalid syntax`, wasting an attempt and an LLM call.
**Vulnerability:** When the LLM wraps output as a fenced python block, the strip logic at L536-541 splits by newlines, drops only lines that begin with backticks, and rejoins. The opening fence is at index 0; after dropping the literal fence line, the next line is **the bare word `python`** if the LLM formatted it as a fence-with-lang-tag on its own line. The check `line.strip().startswith("```")` does **not** match a line that is just `python`. So the resulting code begins with `python\n...` which is a `SyntaxError` on evaluation.
  - Worse: at L800-801, the SQL sanitizer has the *same* bug pattern — it does `if sql.lower().startswith("sql"): sql = sql[3:]` which silently truncates queries that legitimately begin with the word `sql` (e.g. column name `sql_id` after a fenced response).
**Impact:** First analyst attempt fails when the LLM uses fenced code (which it usually does); pipeline burns retries on its own formatting confusion.
**Fix:** Use a regex: `code = re.sub(r"^```(?:python|py)?\s*\n|\n```\s*$", "", code, flags=re.MULTILINE)`.

### WARNING #2: `safe_exec` returns `traceback.format_exc()` directly to the caller, which then echoes it back into the retry prompt — leaks file paths and source lines
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:444-445, 619-633`
**Confidence:** 0.80
**Risk:** Information disclosure (filesystem paths, internal variable names) into LLM prompts; cross-attempt prompt budget bloat.
**Vulnerability:** `safe_exec` catches every exception with `traceback.format_exc()` and returns it. The retry prompt at L619-633 inlines this verbatim into the fenced error block. Tracebacks include absolute filesystem paths (e.g. `C:\Users\natew\Desktop\discord_bot\...`), which are then sent to the third-party Gemini/Claude APIs. This violates the user-privacy guidance in the system prompt about not transmitting system info. Beyond privacy, traceback strings can be 2-5KB each, and the retry prompt also inlines the full schema again at L625 — three calls’ worth of token budget can balloon from 4KB to 20KB+.
**Impact:** Path leaks to external LLM providers; token budget waste; potential rate limits hit faster.
**Fix:** Extract just the exception class + message: `error = f"{type(exc).__name__}: {exc}"`. Truncate to 500 chars if longer. Don't re-inline the full schema in retry — pass a delta or a column-list summary.

### WARNING #3: `_validate_sql` regex layer is not a real security boundary, and comment doesn't say so
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:758-789`
**Confidence:** 0.70
**Risk:** Future maintainer "improves" the DB connection (e.g. switches to `executescript` or drops read-only mode) and the regex quietly stops being sufficient.
**Vulnerability:** `_BANNED_SQL_KEYWORDS` (L749-754) and `_TABLE_REF_PATTERN` (L760-763) operate on the **raw** SQL string without stripping comments. `_SELECT_PATTERN` at L758 only requires the string to *start* with `SELECT`. It does not check for **multiple statements**. SQLite by default rejects multi-statements via `cursor.execute()`, so today a query like `SELECT 1; DELETE FROM messages;` would only run the first statement. But **the validator doesn’t know that** — if anyone ever swaps `conn.execute()` for `conn.executescript()` (e.g. for batched queries), the validator will silently allow destructive statements to fire.
  - `_BANNED_SQL_KEYWORDS` doesn't include `WITH` — a `WITH RECURSIVE` query producing a denial-of-service via cartesian explosion is not blocked.
  - The DB is opened read-only at L1032 (`?mode=ro`), which is the actual defense. The regex layer is not the security boundary it pretends to be.
**Impact:** Today: low — read-only mode + single-statement evaluation close the gap. Tomorrow: high if someone "improves" the DB connection or copies the validator to a write-context.
**Fix:** Add a comment at the top of `_validate_sql` documenting that the **only** real security boundary is the `?mode=ro` URI in `_get_discord_db()`. Add `assert "mode=ro" in str(conn)` (or equivalent runtime check) inside `execute_sql_safe()` before evaluation.

### WARNING #4: `should_sql_query()` calls `dm.discord_db_exists()` (synchronous filesystem stat) on every keyword check — unbounded I/O in hot path
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:736-744`
**Confidence:** 0.75
**Risk:** Filesystem stat on a possibly-unmounted/networked path on every Discord message classification.
**Vulnerability:** `should_sql_query()` is intended as a fast keyword pre-filter (returns bool) and is called from intent routing. But it always calls `dm.discord_db_exists()` first (which runs `os.path.isfile(_DB_PATH)` per L984-985). On Windows, `isfile` is fast for local NTFS, but if `_DB_PATH` ever points to a network share (the codebase has `ORACLE_DB_PATH` env var per CLAUDE.md), this is a synchronous network round-trip on every message. The function is synchronous, so the event loop blocks.
**Impact:** Latency spikes on Discord message classification; routing pipeline can't keep up under bursts.
**Fix:** Cache the result of `discord_db_exists()` once at startup. Or memoize with a 60s TTL.

### WARNING #5: `_call_analyst` swallows all exceptions to empty string; caller can’t distinguish "API down" from "LLM returned empty"
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:526-545`
**Confidence:** 0.75
**Risk:** Silent degradation. Failed Gemini/Claude calls look identical to "LLM had nothing to say."
**Vulnerability:** `try/except Exception` at L543-545 catches **everything** and returns `""`. The caller `reason()` at L585 then takes the empty-code branch and returns `"Gemini returned no code on initial attempt."` — which is a *misleading* message because the actual failure could be: rate limit, network error, auth error, JSON parse error inside `atlas_ai.generate()`, model overload (529), etc. None of these are "Gemini returned no code." The user sees a completely wrong diagnostic, and the bot operator gets a single log line `[Reasoning] Analyst call failed: <e>` which is deduped quickly.
  - The retry loop at L619-650 has the same pattern — if the LLM returns empty on retry, the loop breaks with `_call_analyst` returning empty.
**Impact:** Operator can't distinguish "LLM is down" from "the user asked an unanswerable question"; debugging takes 10x longer.
**Fix:** Re-raise specific exception classes (`anthropic.RateLimitError`, `anthropic.APIError`) and only swallow `ValueError`. Or return a `tuple[str, Optional[Exception]]`.

### WARNING #6: `safe_exec` returns `result` cast via `.head(15).to_string(index=False)` — silently truncates large answers without flagging it
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:439-443`
**Confidence:** 0.70
**Risk:** Caller (and ATLAS persona) sees truncated data with no indication that there are more rows; user gets a wrong answer to "list all teams" or "show every QB".
**Vulnerability:** When the analyst returns a DataFrame, `safe_exec` truncates to the first 15 rows via `.head(15).to_string(index=False)` and returns the resulting string. There's no marker like "(truncated, 23 more rows)". The retry-prompt context at L619 only sees the truncated result, so if the analyst generated an oversized DataFrame, the retry has no signal to filter it down.
  - Rows beyond the 15-cap are silently dropped before being shown to the persona at L601-608. A user asking "list every team that has scored more than 30 in a game this season" gets an answer that *looks* complete.
**Impact:** Wrong / incomplete answers to user questions where N > 15.
**Fix:** When truncating, append `"\n... ({len(result) - 15} more rows truncated)"`. Same fix for `pd.Series` at L442.

### WARNING #7: SQL retry refetches schema synchronously inside the executor; double work + can hide schema changes
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:1020`
**Confidence:** 0.65
**Risk:** Wasted I/O; race window where schema changes between attempts.
**Vulnerability:** The retry loop at L1020 calls `dm.get_discord_db_schema` again via `run_in_executor`. The schema was already fetched on L842 in `generate_sql()` and cached (`get_discord_db_schema` has its own 5-min TTL per L988-1000). The retry fetch is redundant. Worse, the prompt uses the *fresh* schema but the validator uses table names hardcoded at L759 (`_ALLOWED_TABLES = {"messages", "messages_fts"}`); if the DB schema legitimately gains a new table the LLM is told about, the validator silently rejects every query referencing it.
**Impact:** Wasted DB reads; integration friction when DB schema evolves.
**Fix:** Cache schema in `query_discord_history()` local variable; pass to retry helper. Sync `_ALLOWED_TABLES` with the actual schema discovery at startup.

### WARNING #8: `_format_sql_result` row truncation cap (20) doesn't match `MAX_SQL_ROWS` (100)
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:953-964, 713`
**Confidence:** 0.85
**Risk:** Inconsistency between "what we fetched" (100) and "what we show ATLAS" (20). Wasted DB work + confusing logs.
**Vulnerability:** `MAX_SQL_ROWS = 100` (L713) and `cursor.fetchmany(MAX_SQL_ROWS)` at L903 fetch up to 100 rows. But `_format_sql_result` at L954 only iterates `result.rows[:20]` and appends "... and N more rows (truncated)". Rows 21-100 are fetched, materialized to dicts (L909), counted in `result.row_count`, then thrown away. The retry context fed to ATLAS can never see them. So either we should fetch only 20, or display all 100. As written, we’re paying 5x I/O for nothing.
**Impact:** Wasted CPU/memory; user sees "and 80 more rows" message but ATLAS persona has no way to access them.
**Fix:** Cap `fetchmany(20)` to match `_format_sql_result`’s display cap, or expand the display to 100 rows with summarized formatting.

### WARNING #9: `_call_analyst` and `reason` re-import `logging` inside the function on every call
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:523-524, 579-580`
**Confidence:** 0.95
**Risk:** Minor perf bug; smell signaling the file was hastily refactored.
**Vulnerability:** `import logging` and `log = logging.getLogger(__name__)` happen *inside* `_call_analyst()` and `reason()` on every invocation. `logging.getLogger` is cached internally so this is fast, but `import logging` runs the module-level dict lookup every call. The file already has `import logging as _logging` at L708 module-level for the SQL section — pick one, do it once at module top.
**Impact:** Negligible perf, but signals careless cleanup. Two different loggers (`__name__` and `__name__ + ".sql"`) makes log filter rules harder to write.
**Fix:** Move `_log = logging.getLogger(__name__)` to module top, delete the in-function imports.

### OBSERVATION #1: `reason()`, `generate_analysis_code()`, `query_discord_history()`, `get_intent()`, `should_reason()`, `should_sql_query()` are not called anywhere in the live codebase
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:548, 561, 678, 684, 736, 971`
**Confidence:** 0.85
**Risk:** Half-orphan file. Confusion about what is live vs dead.
**Vulnerability:** Per Grep across the project, the only consumers of `reasoning.py` are:
  - `bot.py:133` — `import reasoning` (top-level import only; no function calls found)
  - `oracle_agent.py:21` — imports **only** `_SAFE_BUILTINS, validate_sandbox_ast, _UnsafeCodeError`

  No live code calls `reason()`, `query_discord_history()`, `should_reason()`, `should_sql_query()`, `generate_analysis_code()`, `safe_exec()`, `build_exec_env()`, `get_intent()`, `build_schema_prompt()`, or any prebuilt metric. The 1064-LOC file effectively exists to host three security primitives that `oracle_agent.py` reuses.
**Impact:** Maintenance burden — every audit and refactor of the file is paying tax on dead-code paths. Risk of silent regressions in `oracle_agent.py`'s imports if someone deletes the "unused" surface area without noticing.
**Fix:** Either (a) move `_SAFE_BUILTINS`, `validate_sandbox_ast`, `_UnsafeCodeError` into a dedicated `sandbox_security.py` module that `oracle_agent.py` and (future) callers can import, then move the dead reasoning surface to `QUARANTINE/`; or (b) confirm that something else does call `reason()` (e.g. a slash command in a cog the Grep missed) and document the caller list at the top of the module.

### OBSERVATION #2: Identity map hardcoded in the analyst system prompt (L486-491)
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:486-491`
**Confidence:** 0.95
**Risk:** Drift from `tsl_members` table — CLAUDE.md says it's the "single source of truth."
**Vulnerability:** The prompt hardcodes a 20-entry name → db_username map. CLAUDE.md explicitly states `build_member_db.get_alias_map()` returns 88+ entries and is the canonical mapping. The hardcoded list at L486-491 will silently desync the moment a member is renamed or added — and the LLM will then filter on the wrong username, returning empty results (which `safe_exec` will return as empty DataFrame, which the persona will then "explain" with confidence).
**Impact:** Wrong answers when querying league members; specifically, any new team owner added after the constant was last edited will be completely invisible.
**Fix:** At call time, build the identity map dynamically: `identity_lines = "\n".join(f"{nick}={uname}" for nick, uname in build_member_db.get_alias_map().items())` and inject into the prompt template.

### OBSERVATION #3: `ANALYST_SYSTEM` instructs LLM to filter `df_games` by `status in ['2','3']` — correct per CLAUDE.md, but no enforcement
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:484`
**Confidence:** 0.90
**Risk:** LLM may forget the filter on a complex query.
**Vulnerability:** Rule 14d at L484 says "filtering for completed games (status in ['2','3'])." This matches CLAUDE.md's "Completed games filter: status IN ('2','3'), NOT status='3' alone." But it's a soft instruction in the prompt. If the LLM's generated code uses `status == '3'` (a known footgun per CLAUDE.md), `safe_exec` happily runs it and `_format_sql_result` returns an undercount. There's no AST or post-evaluation sanity check that catches `status == '3'`.
**Impact:** Silent data bug — completed-games queries return a fraction of the true count.
**Fix:** Post-evaluation lint pass: regex the generated code for `status\s*==\s*['\"]3['\"]` (alone) and reject as a known footgun, prompting retry with the exact correction.

### OBSERVATION #4: Magic number `1e-9` everywhere as divide-by-zero guard
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:127, 134, 147, 162, 185-187, 200-201, 216, 231, 243, 256`
**Confidence:** 0.95
**Risk:** Small bias toward zero; cosmetic; no real impact unless metrics are used for ranking close calls.
**Vulnerability:** Scattered `+ 1e-9` to avoid division by zero. Where attendance counts are 0 (e.g. `passAtt == 0` for a non-QB position), the denominator becomes `1e-9`, the numerator becomes `0/1e-9 = 0`, fine. But for `_norm()` at L126-127 the formula is `(series - mn) / (mx - mn + 1e-9)` — if `mx == mn` (single-row dataframe), the result is `(0) / (1e-9) = 0` for every row instead of, say, 0.5 or NaN. Quietly biases results.
**Impact:** Edge cases (cold-start league, single team in filter) produce all-zero power scores.
**Fix:** Replace with explicit check: `if mx == mn: return pd.Series(0.5, index=series.index)`.

### OBSERVATION #5: `compute_*` metric functions never define `_norm` outside `PREBUILT_METRICS_CODE` — fails if anyone moves the constant
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:121-270, 419-420`
**Confidence:** 0.80
**Risk:** Brittle module-level dependency.
**Vulnerability:** All seven `compute_*` functions reference `_norm`. They are only valid when evaluated inside the same global namespace where `_norm` was defined. The L419-420 special case at module-load is "we also inject `_norm` because metric functions reference it" — that hint is in a comment, not in the function names. Anyone reading `PREBUILT_METRICS_CODE` cannot tell which functions need `_norm` injected vs which don’t. If a future commit moves `_norm` out of `PREBUILT_METRICS_CODE`, the metric functions will silently `NameError` at evaluation.
**Impact:** Refactor footgun.
**Fix:** Define `_norm` as a regular module-level Python function outside the string blob, and inject it explicitly into `prebuilt_env` before evaluation.

### OBSERVATION #6: Banned-keyword regex won't catch SQL injection via case-folded Unicode
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:749-754`
**Confidence:** 0.40
**Risk:** Pedantic — read-only mode mitigates, but the regex is misleading.
**Vulnerability:** `_re.IGNORECASE` covers ASCII case. SQLite is case-insensitive on ASCII keywords but the regex is **byte-level** — Unicode lookalikes (e.g. Cyrillic letters that render identically) won't match. Read-only mode at `_get_discord_db` is the actual defense.
**Impact:** None today; misleading code.
**Fix:** Add a comment "Defense in depth — read-only mode is the real boundary."

### OBSERVATION #7: `attempt > MAX_RETRIES` check at L616 makes the variable's name lie
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:558, 596, 616`
**Confidence:** 0.95
**Risk:** Off-by-one confusion.
**Vulnerability:** `MAX_RETRIES = 2` per L558. The loop runs `for attempt in range(1, MAX_RETRIES + 2)` (L596) → attempts 1, 2, 3. So we make **3** attempts total: 1 initial + 2 retries. The constant `MAX_RETRIES = 2` is "max retries after the first attempt" — which means the *number of attempts* is `MAX_RETRIES + 1` = 3. This is documented at L596 (`# attempts: 1, 2, 3`) and L612 (`{attempt}/{MAX_RETRIES + 1}`), but the `+1` and `+2` arithmetic is repeated 4 times in the function. Easy off-by-one.
**Impact:** Easy to introduce off-by-one when refactoring.
**Fix:** Rename `MAX_RETRIES = 2` → `MAX_ATTEMPTS = 3` and use `range(1, MAX_ATTEMPTS + 1)` and `attempt >= MAX_ATTEMPTS`.

### OBSERVATION #8: `safe_exec` return type hint `tuple[any, str]` should be `tuple[Any, str]`
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:424`
**Confidence:** 0.95
**Risk:** Type checker error / breaks `mypy`.
**Vulnerability:** Lowercase `any` is the **builtin function**, not the typing alias. Should be `typing.Any`. Python doesn't error at runtime (because it's a forward reference in newer versions), but any static type check will fail and will report `any` as incorrect.
**Impact:** Linting fails; signal-to-noise drop.
**Fix:** `from typing import Any` and use `tuple[Any, str]`.

### OBSERVATION #9: `_format_sql_result` slices `row.get("timestamp", "")[:16]` — assumes timestamp is always a string
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:955`
**Confidence:** 0.85
**Risk:** `TypeError: 'NoneType' object is not subscriptable` if a row has NULL timestamp; or unexpected truncation if it's an int Unix timestamp.
**Vulnerability:** SQLite returns `None` for NULL columns, not `""`. `row.get("timestamp", "")` returns `None` (not the default), then `None[:16]` raises `TypeError`. Caught nowhere — propagates up, bubbles to `query_discord_history`, which has no try/except around `_format_sql_result`. The pipeline crashes mid-format.
**Impact:** A single NULL timestamp in the DB crashes the whole answer.
**Fix:** `ts = (row.get("timestamp") or "")[:16]`.

### OBSERVATION #10: `validate_sandbox_ast` allows `_norm` access despite blocking dunder strings — inconsistency
**Location:** `C:/Users/natew/Desktop/discord_bot/reasoning.py:354-358, 419-420`
**Confidence:** 0.95
**Risk:** Inconsistency between AST blocker and intentional API.
**Vulnerability:** L354 blocks any `_ast.Attribute` whose `attr.startswith("_")`. So `obj._norm` would be rejected. But L419-420 explicitly injects `_norm` as a top-level **name** (not attribute). The AST walker only checks `Attribute.attr`, not `Name.id`, so `_norm(x)` (a function call) is allowed. This works **by accident** — the analyst prompt at L460-468 tells the LLM to call `compute_spam_scores`, `compute_sim_scores`, etc., never `_norm` directly. But if the LLM ever generates `_norm(...)`, it'll work, breaking the implicit "no underscore-prefix names" convention.
  - More importantly: the convention is implicit. There's no equivalent block on `_ast.Name` whose `id` starts with `_`. So `__builtins__` can be referenced as a bare name (not blocked by AST), but the restricted env binds `__builtins__` to `_SAFE_BUILTINS`, which makes the access useless. Good — but only because the env binding catches it.
**Impact:** Subtle. Today it's fine because `_norm` is the only intentionally underscore-prefixed name. Future maintainers might add another and forget the AST loophole.
**Fix:** Either rename `_norm` to `norm`, or add an explicit allowlist of underscore-prefix names in the AST validator.

## Cross-cutting Notes

- **Half-orphan file pattern** (Observation #1) likely affects other Ring 1 files. The Oracle/AI subsystem has been through three major reviews per `docs/archive/` and `docs/handoff_oracle_v3_review.md`; reasoning.py looks like a casualty of that migration where `oracle_agent.py` superseded the consumer side but the producer was never deleted.
- **Sandbox primitives** (`_SAFE_BUILTINS`, `validate_sandbox_ast`, `_UnsafeCodeError`) should be hoisted into a dedicated module (`sandbox_security.py`). They are the only live exports from this file and are duplicated in spirit across `oracle_agent.py`. Centralizing them will let one fix close the dunder/Subscript gap (Critical #4) for every consumer.
- **Blocking I/O on the event loop** (Critical #3) is a recurring theme — `data_manager.get_discord_db_schema()` is sync, `dm.discord_db_exists()` is sync, `_call_analyst` only wraps the LLM call but not the schema build. Other Ring 1 files in this audit batch (especially anything that touches `data_manager` from a cog) should be checked for the same pattern.
- **Markdown fence stripping** (Warning #1) is a copy-paste-prone idiom. If `atlas_ai.py`, `oracle_agent.py`, or any other LLM caller has the same broken pattern, fix them all together.
- **`asyncio.get_running_loop().run_in_executor` pattern** is used at L842, L997, L1020 — but always with `None` as the executor argument, meaning the default thread pool. Under load, the default pool's 32-thread cap will throttle. Consider a dedicated `concurrent.futures.ThreadPoolExecutor` for SQL work.
