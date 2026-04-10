# Adversarial Review: oracle_agent.py

**Verdict:** needs-attention
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 705
**Reviewer:** Claude (delegated subagent)
**Total findings:** 13 (3 critical, 6 warnings, 4 observations)

> ORPHAN STATUS: LIVE
> This file is not imported through bot.py's direct dependency chain but IS imported by active code: `oracle_cog.py` and `test_oracle_stress.py`. Argus's static scan missed it. Review as active production code.

## Summary

This is the AI code-generation agent layer for Oracle v3 — it asks Claude/Gemini (via `atlas_ai.generate`) to write Python that calls the QueryBuilder API, then runs the generated code in a restricted sandbox. The architecture is sound: it correctly uses `atlas_ai.generate()` per CLAUDE.md, restricts builtins via `_SAFE_BUILTINS`, and validates AST before running. But there are several real failure modes: (1) the few-shot examples teach the AI bad patterns that the sandbox then *blocks*, (2) `validate_sandbox_ast` rejects all `try/except` so multi-step examples in the prompt cannot be run, (3) `_get_current_season()` falls back to `1` (a real season), masking failures, and (4) the executor timeout cancels the future but the underlying thread keeps running.

## Findings

### CRITICAL #1: Few-shot example teaches AI to use `import statistics` — but sandbox blocks all imports

**Location:** `oracle_agent.py:154-156, 402, _SAFE_BUILTINS in reasoning.py`
**Confidence:** 0.98
**Risk:** Line 156 in the API reference (rendered into the system prompt at line 390) tells the AI: `"NOTE: Compute stddev in Python: import statistics; statistics.stdev([r['wins'] for r in rows])"`. Rule #10 at line 402 says `"Never use import statements — everything you need is already in scope."` These two instructions are contradictory. When the AI follows the `owner_consistency` API reference, it generates code with `import statistics`, and the sandbox `_SAFE_BUILTINS` (per `reasoning.py:286-322`) does NOT include `__import__`, so the import raises `ImportError: __import__ not found` at runtime. The result is propagated to the retry loop, which calls the AI *again* with the failure context — and the AI obediently re-emits the same `import statistics` because the system prompt still tells it to. Three attempts × ~2-5 seconds each = 6-15 second user-visible latency before the user sees a generic "all attempts failed" error. Real money: each attempt is a Sonnet call at ~$0.003 → $0.009 wasted per failed query.
**Vulnerability:** Self-contradicting prompt + retry loop with no contradiction detection.
**Impact:** Every "consistency" / stddev query for owners burns 3 Sonnet attempts and returns garbage. Latency, cost, user-visible failure.
**Fix:** Either (a) inject `statistics` into `build_agent_env` as `"statistics": statistics`, or (b) inject pre-computed `pstdev`/`stdev` helpers, or (c) edit the API reference NOTE to say `"Compute stddev manually: variance = sum((x - mean)**2 for x in xs) / len(xs); stdev = variance ** 0.5"`.

### CRITICAL #2: `validate_sandbox_ast` blocks `try/except` but the few-shot examples and prompt do not warn about it

**Location:** `oracle_agent.py:285-318, 326-336` (few-shot examples) and `reasoning.py:367-375`
**Confidence:** 0.95
**Risk:** `reasoning.py:validate_sandbox_ast` rejects `try`, `except`, and `raise` statements before running the code — citing `__traceback__` frame traversal as the security reason. But the few-shot example for "improvement leaders" at lines 287-318 has the AI iterate over query results and unpack them; the example at lines 326-336 unpacks `r["points_for"]` and could legitimately need to defend against missing keys. Worse, the *prompt does not mention this restriction* — Rule #10 only forbids imports, not error handling. The AI naturally wraps risky operations in `try/except` (Sonnet's well-known habit), the AST validator rejects the code, and the retry feeds back `Blocked: Try statement not allowed in sandbox (line N)` — which Sonnet may "fix" by inverting the logic and breaking the query.
**Vulnerability:** Mismatch between sandbox capabilities and prompt instructions; failure mode hidden by retry-with-error wrapper.
**Impact:** Any query whose AI-generated code contains a try/except (estimated 30-50% based on Claude's defensive coding habits) burns retries and returns errors.
**Fix:** Add an explicit rule to `_build_system_prompt`: `"11. NEVER use try/except/raise — if something might fail, check the condition first (e.g., 'if rows and len(rows) > 0:'). The sandbox blocks exception handling for security."` Plus: rewrite the pythagorean_wins example at lines 326-336 to show an explicit length check instead of a try/except.

### CRITICAL #3: `_get_current_season()` silently returns `1` on double failure — hides bugs as live data

**Location:** `oracle_agent.py:420-431, 434-445`
**Confidence:** 0.9
**Risk:** Both `_get_current_season()` and `_get_current_week()` log a warning and return `1` if both `oracle_query_builder` and `data_manager` fail. The comment says `"Safer than a stale hardcode — obviously wrong prompts investigation"`. But `1` is NOT obviously wrong — it's a real, valid season number (TSL has 95+ seasons; season 1 happened years ago). The system prompt then tells the AI `"Current season: 1, current week: 1"` — and the AI generates queries against season 1 historical data. The user asks "what's my record this season?" and gets *Witt's season-1 wins from 5 years ago*, presented as if it were current. The user has no way to know they're seeing wrong data.
**Vulnerability:** Default fallback that masquerades as valid data.
**Impact:** Silent data integrity bug. Users trust Oracle answers; Oracle confidently returns ancient data when a transient import error or `data_manager` startup race occurs. Worse during deploy windows when `_startup_done` is not yet set.
**Fix:** Return a sentinel like `0` or `-1` and have `_build_system_prompt` detect it: `if season < 1: parts.append("CRITICAL: Current season is unknown — refuse to answer time-sensitive queries.")`. Or: raise an exception that propagates to the user as "Oracle is starting up, try again in a moment."

### WARNING #1: `loop.run_in_executor(None, _safe_run, code, env)` cannot be cancelled by `wait_for` timeout

**Location:** `oracle_agent.py:666-672`
**Confidence:** 0.95
**Risk:** `asyncio.wait_for(loop.run_in_executor(None, ...), timeout=15.0)` — when the 15s timeout elapses, `wait_for` raises `TimeoutError` and cancels the awaitable, but the underlying thread in the default `ThreadPoolExecutor` keeps running. The handler catches the timeout and sets `data, error = None, "Sandbox timed out"`, the retry loop runs again, and the new executor task spins up — but the old thread is still running the runaway AI-generated code, holding `_real_run_sql` (sqlite3 connection) and consuming CPU. After ~3 attempts × 15s the user sees a failure, but the *actual sandbox threads can still be running* on the default executor. Repeated failures pile up worker threads.
**Vulnerability:** Default `ThreadPoolExecutor` has no thread-kill mechanism, and `concurrent.futures.Future.cancel()` is a no-op once the thread has started.
**Impact:** Slow CPU exhaustion if the AI generates infinite loops (e.g. `while True: pass` — currently allowed by AST validator, since `While` is not blocked). User-perceived behavior is "Oracle is slow" until the bot OOMs hours later.
**Fix:** Add `While` to the AST blocklist (or restrict to `for ... in <range/iterable>`), and document the timeout caveat. Optionally use a process-pool executor that *can* terminate.

### WARNING #2: `_get_current_season()` and `_get_current_week()` do synchronous imports inside async-called context

**Location:** `oracle_agent.py:399, 420-445`
**Confidence:** 0.85
**Risk:** `_build_system_prompt` calls `_get_current_season()` and `_get_current_week()` directly at line 399. `_build_system_prompt` is called by `run_agent` which is `async`. Both helpers do `import oracle_query_builder as qb` and `import data_manager as dm` *at call time* (lines 422, 427, 436, 441). On first call this triggers a real Python module import including any module-level side effects (data_manager.load_all? schema build?). If those imports do blocking I/O, they block the asyncio event loop. Subsequent calls hit the import cache so impact is bounded to first-call latency.
**Vulnerability:** Hidden synchronous I/O on the event loop on first invocation.
**Impact:** First Oracle query after bot restart freezes the bot for the duration of `data_manager` cold-start. CLAUDE.md flags this exact pattern as "Async/Concurrency: Blocking calls inside async functions".
**Fix:** Move imports to module top, or pre-warm in `oracle_cog.py`'s `cog_load`.

### WARNING #3: `_make_capturing_run_sql` re-imports `codex_utils.run_sql` on every `build_agent_env` call

**Location:** `oracle_agent.py:452-460`
**Confidence:** 0.9
**Risk:** `_make_capturing_run_sql` does `from codex_utils import run_sql as _real_run_sql` *inside* the function body. `build_agent_env` calls it on every agent run. Python's import cache makes this cheap *after* the first call, but the function-local import resolves the symbol on every invocation through `sys.modules` lookup. Functionally correct but smells of an attempt to dodge a circular import — and if `codex_utils` is ever monkey-patched in tests, the patched version will only be used by future calls, not currently-running ones.
**Vulnerability:** Lazy imports as a circular-dep workaround leak into hot paths.
**Impact:** Microscopic perf hit; brittle test mocking.
**Fix:** Move `from codex_utils import run_sql as _real_run_sql` to module top. If there's a real circular import, fix it explicitly with a TYPE_CHECKING block or restructure.

### WARNING #4: Captured SQL from `_capture_list` is appended even on failed runs, but only the *last* entry is returned

**Location:** `oracle_agent.py:452-460, 663-675`
**Confidence:** 0.7
**Risk:** Line 675: `captured_sql = sql_capture[-1] if sql_capture else ""`. If the AI's first attempt runs 5 SQL queries successfully then crashes on a Python computation, the `sql_capture` list has 5 entries and only the *last* (which is the one closest to the error) is shown to the user. But on a *successful* run with multiple SQL calls (legitimate — see the few-shot at lines 340-355 which calls `run_sql` in a loop over owners), the user sees the *last* query, which may be irrelevant (e.g. the 27th owner's query when the answer is the aggregate). The footer SQL becomes misleading.
**Vulnerability:** Single-SQL footer assumption violated by multi-step queries.
**Impact:** User sees "the SQL Oracle ran" and it's the wrong one. Audit/debug confusion.
**Fix:** Either show the first SQL (often most informative), join all with `;\n`, or add a count: `f"{len(sql_capture)} queries; last: {sql_capture[-1]}"`.

### WARNING #5: Retry loop temperature scales `0.1 * attempt` — at attempt 3 that's only 0.3, but the temperature for attempt 1 is 0.05

**Location:** `oracle_agent.py:633-655`
**Confidence:** 0.6
**Risk:** Attempt 1 uses `temperature=0.05`. Retries use `temperature=0.1 * attempt`, which gives 0.2 (attempt 2) and 0.3 (attempt 3). Both retries are *higher* than attempt 1, so they explore more — fine in theory. But `0.1 * attempt` for `attempt=2` is 0.2, not 0.1, and the comment says "slight temp increase on retry". The intent looks off-by-one (probably meant `0.1 + 0.05 * (attempt - 1)`). Cosmetic, but a bug if the intended temperature schedule was different.
**Vulnerability:** Possible miscalculation in retry strategy.
**Impact:** None today (current values are reasonable). But the comment vs. behavior mismatch suggests this was not deliberate.
**Fix:** Either fix the formula to match intent or update the comment to match behavior.

### WARNING #6: `Tier.SONNET` is hardcoded — no fallback to a cheaper tier on retry

**Location:** `oracle_agent.py:636, 652`
**Confidence:** 0.5
**Risk:** Both attempt 1 and retries use `Tier.SONNET`. If the failure is a transient API error rather than a code bug, the retry hits the same model and likely fails the same way. CLAUDE.md says `atlas_ai.generate()` "Handles ... Claude→Gemini fallback chain" — but this is per-call, not per-attempt-strategy. There's no escalation to a smarter model (Opus) on hard questions, no de-escalation on simple ones.
**Vulnerability:** No model strategy for retries.
**Impact:** Wasted Sonnet calls on questions that need Opus, and over-spending on questions Haiku could solve.
**Fix:** Tier escalation: `tier_for_attempt = [Tier.SONNET, Tier.SONNET, Tier.OPUS][attempt-1]` or similar.

### OBSERVATION #1: `_extract_code` returns the entire response if no fence is found — no sanity check

**Location:** `oracle_agent.py:557-566`
**Confidence:** 0.85
**Risk:** If the AI ignores Rule #1 ("Write ONLY Python code") and includes prose like `"Here's the code:\n\nresult = ..."`, the regex finds no fence and returns the entire string including the prose. `compile()` then chokes on the English text and the retry burns. But also: if the AI returns `"```python\nresult = 1\n```\nExplanation: this gets the answer.\n```python\nresult = 2\n```"` (two fenced blocks), only the *first* match is taken — the second block is discarded silently.
**Vulnerability:** Greedy first-match regex with no validation of "is this actually Python?".
**Impact:** Silent attempt-burning when the AI is verbose.
**Fix:** If multiple code fences are found, log a warning and take the *last* one (most likely the final answer). Add a quick `compile()` syntax pre-check before running.

### OBSERVATION #2: `_sandbox_exec` and `_run_in_sandbox` are functionally identical — pointless indirection

**Location:** `oracle_agent.py:586-603`
**Confidence:** 0.9
**Risk:** `_sandbox_exec` calls `_run_in_sandbox` which calls the builtin code-runner. Both functions have docstrings claiming "isolated function for clear traceback" — but the indirection adds a frame to every traceback without isolating anything. The pattern is cargo-cult; either inline both into `_safe_run` or document why the call boundary matters.
**Vulnerability:** Code smell, no functional risk.
**Impact:** Tracebacks have an extra useless frame.
**Fix:** Inline both helpers. Remove `_sandbox_exec` and `_run_in_sandbox`, call the builtin code-runner directly inside `_safe_run`.

### OBSERVATION #3: `compare_seasons` and `improvement_leaders` API docs are duplicated/overlapping

**Location:** `oracle_agent.py:116-120`
**Confidence:** 0.5
**Risk:** Two overlapping functions in the API reference can cause AI to flip between them, and the prompt doesn't disambiguate when to use which. Cosmetic.
**Vulnerability:** Documentation overlap → AI inconsistency.
**Impact:** Slightly wider variance in generated code for "compare seasons" queries.
**Fix:** Add a one-liner: "Use `improvement_leaders` for top-N rankings; use `compare_seasons` for individual comparisons."

### OBSERVATION #4: Logging uses `%`-format strings but mixes string truncation with `[:80]` instead of relying on the formatter

**Location:** `oracle_agent.py:677-678, 691-692, 696-697`
**Confidence:** 0.4
**Risk:** `log.info("[oracle_agent] Success on attempt %d ... question: %s", attempt, question[:80])` truncates at the call site rather than using the logger's filter. If a future config wants full questions in DEBUG and truncated in INFO, this is hardcoded. Cosmetic.
**Vulnerability:** Brittle truncation strategy.
**Impact:** None.
**Fix:** Move truncation into a custom logging filter or accept the smell.

## Cross-cutting Notes

This file's most serious issues (CRITICAL #1 and CRITICAL #2) stem from **a contract mismatch between the prompt the AI receives and the sandbox the AI's code runs in**. The system prompt invites import statements and try/except, but the sandbox blocks both. Every other Codex/Oracle/AI module in the codebase that uses `_SAFE_BUILTINS` + `validate_sandbox_ast` has the same risk — `reasoning.py` is the shared dependency. Fixing the few-shot examples here will not fix the root cause if other callers regenerate similar prompts. Recommend a single source of truth: a shared `SANDBOX_RULES` constant in `reasoning.py` that lists every restriction, imported into all prompt builders.

Also worth flagging across files: `Tier.SONNET` hardcoding in retry loops appears in multiple places (oracle_agent, codex_cog, etc.) — a project-wide retry strategy module would help.
