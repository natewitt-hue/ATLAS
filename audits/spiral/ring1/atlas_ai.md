# Adversarial Review: atlas_ai.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 825
**Reviewer:** Claude (delegated subagent)
**Total findings:** 22 (3 critical, 11 warnings, 8 observations)

## Summary

`atlas_ai` is the trust-root for every AI call in ATLAS, but several failure paths leak (or hang) instead of degrading cleanly. The streaming generator can deadlock callers, the Gemini search path nullderefs on filtered responses, and the fallback chain conflates retryable rate-limit errors with fatal SDK bugs — meaning a Claude 429 always burns a Gemini call. Public-facing exception messages also funnel raw SDK errors (which can carry credential fragments and request metadata) directly into structured logs. Ship after addressing the three critical findings; the warnings can be batched.

## Findings

### CRITICAL #1: `generate_stream` deadlocks if executor raises before sentinel

**Location:** `atlas_ai.py:715-745`
**Confidence:** 0.95
**Risk:** The streaming consumer awaits `queue.get()` in an infinite loop until it sees `None`. The producer (`_run_stream`) only puts `None` *after* the `with stream:` block exits cleanly. If the SDK raises *inside* `stream.text_stream` (network blip mid-stream, rate limit triggered after first token, JSON parse error inside SDK, context manager `__exit__` raising), the executor future fails — but the queue never sees `None`, and the `while True` loop on line 739 awaits forever.
**Vulnerability:** The `await fut` on line 744 is unreachable in the failure case because line 740 (`chunk = await queue.get()`) blocks first. The `try/except Exception as e` on line 746 only catches errors from `await fut`, which never executes.
**Impact:** Any mid-stream Claude failure (especially common during long-form generation under rate-limit pressure) hangs the calling coroutine indefinitely. With Discord's 15-min interaction window, this manifests as a "ATLAS is thinking..." message that never completes, then blows the interaction. Worse, the executor thread is also leaked.
**Fix:** Wrap `_run_stream` in `try/except/finally` so the sentinel is *always* posted, and propagate the exception via a sentinel object the consumer can `raise` on:
```python
def _run_stream():
    err = None
    try:
        with claude.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                loop.call_soon_threadsafe(queue.put_nowait, text)
    except Exception as e:
        err = e
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, err)  # None or exception
```
Then in the consumer loop, check `if isinstance(chunk, BaseException): raise chunk`.

---

### CRITICAL #2: `_call_gemini_with_search` null-derefs on `response.text`

**Location:** `atlas_ai.py:329-335`
**Confidence:** 0.95
**Risk:** Line 330 reads `response.text.strip()` with no None guard. The sibling `_call_gemini` on line 269 *does* guard with `(response.text or "").strip()`. Gemini's `response.text` is `None` whenever the response is empty, blocked by safety filters, or returned only function-call parts. With Google Search tool enabled, finish_reason `RECITATION` (copyright) is also common and produces a None-text candidate.
**Vulnerability:** When the model is filtered, `.strip()` on `None` raises `AttributeError: 'NoneType' object has no attribute 'strip'`. The only catch is the `except Exception as e` in `generate_with_search` (line 681), which logs the failure as "Gemini search failed" and falls back to Claude (which has *no* web search capability) — producing a degraded but successful response. The user gets an answer, but the failure was a *bug* (None deref), not a model rejection.
**Impact:** Every safety-filtered or recitation-blocked search query silently degrades to Claude (no search). The "search worked, content blocked" failure mode is indistinguishable from "search broken entirely." More importantly, in unit tests / new call sites that don't wrap in fallback, the AttributeError surfaces directly to the user.
**Fix:** Mirror `_call_gemini`:
```python
return AIResult(
    text=(response.text or "").strip(),
    ...
)
```
And consider extracting a helper `_safe_gemini_text(response)` so the same bug can't reappear in a future tool variant.

---

### CRITICAL #3: Raw SDK exceptions are logged at WARN with no redaction

**Location:** `atlas_ai.py:98, 115, 467-469, 487-490, 523-525, 541-544, 624-625, 657-658, 681-682, 694-695, 786, 811-812, 821-822`
**Confidence:** 0.85
**Risk:** Every fallback path interpolates raw SDK exception text into the log: `log.warning(f"[atlas_ai] Claude failed ({e}), falling back to Gemini")`. The Anthropic and Google SDK error classes (`AuthenticationError`, `BadRequestError`, `APIStatusError`) include the request context in their `__str__` — historically this has included the request URL with the API key as a query param (older google-genai versions), the full request body (which contains user prompts that may have PII), and sometimes truncated header dumps that include `Authorization: Bearer sk-ant-...`.
**Vulnerability:** The bot is configured to ship logs to a Discord admin channel (`ADMIN_CHANNEL_ID` per CLAUDE.md env vars). If the log sink ever forwards `WARN`+, the credential prefix lands in Discord. Even if the channel is private, log files persist. There is no `repr` or `type(e).__name__` filter — `f"({e})"` interpolates whatever the SDK chose to expose.
**Impact:** Credential exposure and PII leakage at warn-level. Any Anthropic billing exception ("invalid api key sk-ant-api03-AbC...") would be replayed verbatim into the log channel. Per CLAUDE.md: "API keys read from anywhere except environment variables" is a tracked violation, and *leaking* them is materially worse.
**Fix:** Replace every `f"... ({e})"` with `f"... ({type(e).__name__})"` (and use `log.exception(...)` only when the stack trace is needed and the logging handler is known to redact). Add an explicit redaction filter for `sk-ant-`, `AIza`, `Bearer ` patterns in the logging config. Do not put `e` directly in the f-string anywhere in this module.

---

### WARNING #1: Fallback chain treats rate limits as "Claude is broken"

**Location:** `atlas_ai.py:454-491, 507-545`
**Confidence:** 0.90
**Risk:** Every non-timeout exception from Claude triggers immediate fallback to Gemini. There is no distinction between:
- 429 rate limit (retry-after header → should backoff & retry on Claude)
- 5xx transient (should retry once on Claude before falling over)
- 4xx permanent (should fall back immediately, no retry)
- Network connection error (could fall back or retry)
**Vulnerability:** Under load, a single Claude rate-limit response causes the same request to immediately consume a Gemini quota slot. With concurrent Discord interactions firing into rate limits simultaneously, the bot can drain its Gemini daily quota in minutes, leaving the "fallback" with nothing to fall back to. CLAUDE.md doesn't mention rate-limit handling; it should.
**Impact:** Quota exhaustion cascade. Under bursty load, every error becomes a double-spend across both providers, accelerating the path to "no AI provider configured."
**Fix:** Inspect `e` (after redaction!) for `anthropic.RateLimitError` and respect `retry_after`. Implement an exponential-backoff retry on Claude before falling back. Treat `BadRequestError` as terminal (don't fallback — Gemini will likely also reject). Distinguish failure modes per `type(e).__name__`.

---

### WARNING #2: JSON-mode retry can return still-invalid JSON without re-validation

**Location:** `atlas_ai.py:183-208`
**Confidence:** 0.90
**Risk:** When the first Claude response fails JSON validation, the code retries once. The retry response is stripped of fences and assigned back to `text` — but never re-validated. If the retry is *still* invalid, the function returns it as if it succeeded.
**Vulnerability:** Callers like `codex_utils.py:509` (`json.loads(result.text)`) expect valid JSON when `json_mode=True` and will throw on the consumer side, far from the originating bug. The retry's purpose is defeated.
**Impact:** SQL generation, classification, and structured-output flows all get an unhelpful exception in the caller's `try` block instead of either a valid JSON or a recognizable retry-exhausted error from `atlas_ai`. Debugging gets harder.
**Fix:** After the retry, run `json.loads(text)` again. If it still fails, raise a typed `AIInvalidJSONError(text=text)` so callers can branch. Alternately, return `AIResult(text=text, ...)` with a `json_valid: bool` flag for the caller to check.

---

### WARNING #3: Lazy singletons are not thread-safe

**Location:** `atlas_ai.py:85-116`
**Confidence:** 0.80
**Risk:** `_get_claude()` and `_get_gemini()` use the classic check-then-set lazy init pattern with no lock. Under concurrent first-call access (which is common at bot startup when multiple cogs warm up), two threads can both pass the `if _claude_client is not None` check, both call `Anthropic(api_key=...)`, and one of the constructions is discarded.
**Vulnerability:** The discarded `Anthropic` instance leaks an `httpx.Client` which holds a connection pool that's never closed. Over a long-lived bot session this accumulates as a slow file-descriptor leak. Worse, on the first burst of AI calls after startup, both providers double-init. CLAUDE.md flags "Resource leaks: Playwright pages not returned to the pool, sqlite connections never closed" — this is the same class of bug.
**Impact:** Long-run FD leak. Under stress, possibly `EMFILE: too many open files` after weeks of uptime.
**Fix:** Wrap singleton init in a `threading.Lock()` (or use `functools.lru_cache(None)` on a no-arg `_init_claude()` helper). The standard double-checked-locking idiom is fine here.

---

### WARNING #4: `generate_with_search`, `generate_synthesis`, `generate_stream`, `embed_text` lack `asyncio.wait_for` timeouts

**Location:** `atlas_ai.py:574-661, 671-698, 714-758, 772-787`
**Confidence:** 0.85
**Risk:** Only `generate()` (line 457, 476) and `generate_with_tools()` (line 510, 530) wrap their executor calls in `asyncio.wait_for(..., timeout=_AI_TIMEOUT)`. Every other public async API blocks indefinitely on the underlying `loop.run_in_executor` call.
**Vulnerability:** A stalled HTTP connection inside the SDK (no socket timeout configured by the SDK) hangs the executor thread forever. With Python's default `ThreadPoolExecutor` of `min(32, cpu+4)` workers, a handful of stuck calls saturates the pool and starves *every other* `loop.run_in_executor` consumer in the bot — including Playwright rendering, sqlite writes, and Pillow image work.
**Impact:** Bot becomes silently unresponsive after a few stalled AI calls. The executor thread leak is invisible until the next bot restart.
**Fix:** Apply `asyncio.wait_for(..., timeout=_AI_TIMEOUT)` consistently to every executor-wrapped SDK call. Optionally raise `_AI_TIMEOUT` to a higher value for `generate_synthesis` (which is genuinely slower) and lower for `embed_text` (should be sub-second).

---

### WARNING #5: `_call_gemini_with_tools` IndexErrors on empty candidates

**Location:** `atlas_ai.py:417`
**Confidence:** 0.85
**Risk:** Line 417 unconditionally indexes `response.candidates[0].content.parts`. If Gemini returns zero candidates (safety block, recitation, quota error masquerading as 200), this raises `IndexError: list index out of range`.
**Vulnerability:** No try/except around the destructure. The exception bubbles to `generate_with_tools` line 524, gets logged as "Claude tools failed" (wrong provider name in the message — actually Gemini), and falls through — but in `generate_with_tools` the *Gemini* path is the fallback, not the primary. So an empty Gemini-fallback candidates list propagates as an unhandled IndexError to the caller.
**Impact:** Oracle QueryBuilder flow (the documented user of `generate_with_tools`) crashes with a confusing IndexError when Gemini hits a safety filter on the fallback path.
**Fix:**
```python
candidates = getattr(response, "candidates", None) or []
if not candidates:
    return AIResult(text="", provider="gemini", model=model, tool_calls=[])
parts = getattr(candidates[0].content, "parts", None) or []
```

---

### WARNING #6: Synthesis fallback dumps raw tool args (including potentially-secret content)

**Location:** `atlas_ai.py:631-647`
**Confidence:** 0.75
**Risk:** The Gemini synthesis fallback flattens the prior conversation by dumping `getattr(b, 'input', {})` via `json.dumps` (line 642). Tool inputs are often database identifiers or structured queries — but in a future tool that takes auth tokens, secret IDs, or PII as arguments, this becomes a verbatim leak into the Gemini prompt (which is sent to Google, *and* logged).
**Vulnerability:** No allowlist of what fields to extract. No size limit on the flattened prompt — a single large `tool_result_content` blob can blow past Gemini's context window.
**Impact:** Today: verbose & unbounded prompts. Tomorrow: cross-provider data leak when a new tool is added without considering the synthesis fallback.
**Fix:** Build a redacted summary of the tool calls (`name` + `len(input)` + first N keys of input) instead of `json.dumps(input)`. Cap the flat prompt at e.g. 16K chars before sending. Document this constraint at the top of `generate_synthesis`.

---

### WARNING #7: Joining text blocks with single space corrupts whitespace-significant output

**Location:** `atlas_ai.py:177-178, 202-203, 365-366, 614-616`
**Confidence:** 0.85
**Risk:** Every Claude response extraction uses `" ".join(text_parts).strip()`. When Claude returns multiple text blocks (which happens when tool_use blocks are interleaved with text, or when the SDK splits a multiparagraph response across blocks), this collapses paragraph breaks into a single space.
**Vulnerability:** Generation flows that depend on newline-preserving output (Markdown synthesis, multi-line SQL, formatted lists, code blocks) will receive `" "` where `"\n\n"` was sent. This is silent corruption — the result still looks valid in logs.
**Impact:** Oracle's analytical responses lose paragraph structure. Codex SQL gen rendered as a single line might become unparseable. The "ATLAS voice" persona — which expects 2-4 sentences with clean breaks — becomes a wall of text.
**Fix:** Use `"\n".join(text_parts).strip()` or `"".join(text_parts).strip()` (the SDK delimits blocks deliberately; concatenation is correct). Test with a multiblock response to confirm.

---

### WARNING #8: `embed_text` IndexErrors on empty embeddings list

**Location:** `atlas_ai.py:777-787`
**Confidence:** 0.80
**Risk:** Line 784 reads `result.embeddings[0].values` with no length check. If Gemini returns an empty embeddings list (rare but possible on safety block / quota throttle), this raises `IndexError`, gets caught by the bare `except Exception`, logs "Embedding failed: list index out of range," and returns None.
**Vulnerability:** The error message tells you nothing about WHY the embedding was empty. Worse, the bare exception catches `KeyboardInterrupt` and other `BaseException`-derived events on older Python — but Python 3.14 has fixed this.
**Impact:** Oracle Memory permanently silently degrades when embeddings start failing. No alerting because the error message is generic.
**Fix:** Explicitly check `if not result.embeddings: return None`, then index. Log the actual gemini status for diagnostics.

---

### WARNING #9: `generate_sync` exposes blocking SDK to async callers by accident

**Location:** `atlas_ai.py:792-825`
**Confidence:** 0.70
**Risk:** `generate_sync` is exported as a public function. Its docstring says "for CLI scripts" — but Python doesn't enforce this. An accidental call from a coroutine (`atlas_ai.generate_sync(...)` instead of `await atlas_ai.generate(...)`) blocks the event loop for the full duration of the AI call, freezing the entire bot.
**Vulnerability:** No `RuntimeError` guard checking `asyncio.get_event_loop().is_running()`. A typo or missing `await` produces a 60-second event loop freeze with no warning.
**Impact:** Bot becomes unresponsive for all users for the duration of the sync call. Particularly insidious in tests or scripts that share event loop with bot code.
**Fix:** At the top of `generate_sync`, add:
```python
try:
    loop = asyncio.get_running_loop()
    raise RuntimeError("generate_sync() called from a running event loop — use generate() instead")
except RuntimeError as exc:
    if "no running event loop" not in str(exc):
        raise
```
Or rename to `_generate_sync_unsafe` and document loudly.

---

### WARNING #10: `_AI_TIMEOUT` is hardcoded with no env override

**Location:** `atlas_ai.py:77`
**Confidence:** 0.70
**Risk:** The 60s timeout is module-level constant. Operators can't tune this without editing source. Long-form synthesis, embeddings, and quick classifications all share one budget.
**Vulnerability:** When Claude latency spikes (which happens during their incidents), the 60s budget may need to grow to 90s for production traffic without redeploying. Conversely, fast-path classifications (haiku-tier) deserve a 10s timeout.
**Impact:** Operations debt — every latency tuning requires a code edit and version bump.
**Fix:** `_AI_TIMEOUT = float(os.getenv("ATLAS_AI_TIMEOUT", "60.0"))`. Optionally per-tier: `_AI_TIMEOUT_HAIKU = ...`.

---

### WARNING #11: `print()` instead of `log` at line 327

**Location:** `atlas_ai.py:327`
**Confidence:** 0.95
**Risk:** Citation extraction failure uses `print(f"[atlas_ai] Citation extraction failed: {e}")` instead of the module's `log.warning(...)`. Bypasses log routing, log levels, and the structured logging handler.
**Vulnerability:** On Windows (CLAUDE.md notes the bot has manual stdout encoding fixes in `bot.py`), `print` of non-ASCII content can `UnicodeEncodeError` if the SDK error contains non-Latin characters. Also leaks the same `{e}` interpolation issue from CRITICAL #3.
**Impact:** Non-deterministic stdout corruption + log routing inconsistency.
**Fix:** Replace with `log.warning("[atlas_ai] Citation extraction failed: %s", type(e).__name__)`.

---

### OBSERVATION #1: `_call_claude` JSON-retry recomputes `elapsed_ms` from original `t0`

**Location:** `atlas_ai.py:175, 200-201`
**Confidence:** 0.90
**Risk:** When the retry path executes, `elapsed_ms = int((time.perf_counter() - t0) * 1000)` is recomputed but `t0` is still the *original* start. So `elapsed_ms` correctly captures total time across both calls. This is actually correct behavior — flagging only because the comment is missing and a future maintainer might "fix" it by resetting `t0` (which would be wrong). Add a comment.
**Vulnerability:** Trap for future maintainers.
**Impact:** Latent footgun; observability slightly muddled (elapsed includes both API calls but not retries beyond one).
**Fix:** Add inline comment: `# Total elapsed across original + retry`.

---

### OBSERVATION #2: `latency_ms` not populated on tool-use, search, or synthesis paths

**Location:** `atlas_ai.py:368-374, 329-335, 615-620, 428-433`
**Confidence:** 0.95
**Risk:** Only `_call_claude` and `_call_gemini` populate `latency_ms`. Tool-use, search, synthesis, and Gemini-with-tools all return `AIResult` with `latency_ms=None`. No way to observe slow tool-use calls.
**Vulnerability:** Observability gap. CLAUDE.md attack surface lists "observability gaps that would hide failure or make recovery harder."
**Impact:** Can't tell if Oracle's QueryBuilder tool calls are slowing down without external profiling.
**Fix:** Wrap every `client.messages.create(...)` and `client.models.generate_content(...)` in the same `t0 = time.perf_counter()` / `int((time.perf_counter() - t0) * 1000)` pattern.

---

### OBSERVATION #3: `_apply_json_mode_prompt` mutates incoming list

**Location:** `atlas_ai.py:140-146`
**Confidence:** 0.75
**Risk:** `blocks = list(prompt)` creates a shallow copy of the *outer* list, but `blocks[i] = {**blocks[i], "text": ...}` rebinds an index to a new dict — fine. Then `blocks.append(...)` modifies the copy, not the input. So actually safe.
**Vulnerability:** Reads as risky on first inspection. Add a comment, OR make `blocks = [dict(b) if isinstance(b, dict) else b for b in prompt]` for visual clarity.
**Impact:** Maintenance hazard, not a runtime bug.
**Fix:** Comment or explicit deep-copy.

---

### OBSERVATION #4: `_GEMINI_MODELS` collapses all tiers to `gemini-2.0-flash`

**Location:** `atlas_ai.py:67-71`
**Confidence:** 0.95
**Risk:** Every tier (HAIKU, SONNET, OPUS) maps to the same Gemini model. The fallback path therefore loses all tier semantics — an OPUS-tier call falls back to Flash-quality output. The inline comment acknowledges 2.5-pro has empty-response problems with low max_tokens, which is fine, but the model dict is the wrong place for that fix.
**Vulnerability:** Quality regression invisible to caller. The `result.fallback_used = True` flag is the only signal; no model-quality flag.
**Impact:** Synthesis-tier requests silently degrade in quality on fallback. Callers see "AI worked" without knowing the answer is from the wrong tier.
**Fix:** Map at least OPUS → `gemini-2.5-pro` (with the max_tokens floor as a workaround), or document explicitly that "fallback is best-effort, quality may degrade." Surface a `quality_tier_matched: bool` field on `AIResult`.

---

### OBSERVATION #5: Caller may receive `AIResult.text = ""` with no provider clarity

**Location:** `atlas_ai.py:177-178, 269, 365-366, 615-616, 429`
**Confidence:** 0.85
**Risk:** Empty responses (safety block, model returned only tool_use blocks, generation halted) produce `text=""` with no flag indicating *why*. Callers like `oracle_cog.py:823` (`return result.text`) propagate the empty string as if it were a valid answer.
**Vulnerability:** "ATLAS responded with nothing" is indistinguishable from "ATLAS responded with empty string." User gets a blank message embed.
**Impact:** UX confusion; user repeats query.
**Fix:** Add `finish_reason: str | None` and `was_filtered: bool` to `AIResult`. Set them from `response.stop_reason` (Claude) and finish_reason (Gemini). Callers can branch on `was_filtered` to show a friendly message.

---

### OBSERVATION #6: No retry on transient network errors

**Location:** `atlas_ai.py:438-547`
**Confidence:** 0.80
**Risk:** Connection resets, DNS failures, and `httpx.ReadTimeout` from the underlying SDK transport go straight to fallback. Anthropic's SDK has built-in retry, but it can be exhausted in a single `messages.create` call. After that, any transient blip immediately cuts to Gemini.
**Vulnerability:** Provider-failover for what should be a single retry is wasteful and creates a one-strike-out reliability profile.
**Impact:** Higher Gemini quota use than necessary. False fallback signal in logs.
**Fix:** Implement a single retry-with-backoff on transient errors before failing over (`httpx.HTTPError` with no status, or status 5xx).

---

### OBSERVATION #7: `_build_schema` not present (good — owned by Codex), but the rule still applies

**Location:** N/A (not in this file)
**Confidence:** 0.90
**Risk:** CLAUDE.md says `_build_schema()` must dynamically include `dm.CURRENT_SEASON`. Grep confirms this function lives in `codex_utils.py` / `codex_cog.py`, NOT here. This file only orchestrates the call. So this rule does not apply to `atlas_ai.py` directly. Flagging only to confirm review checked for it.
**Vulnerability:** N/A — the rule belongs to a different file.
**Impact:** N/A.
**Fix:** N/A. (Verified absence is intentional.)

---

### OBSERVATION #8: Bare `except Exception` in `_call_gemini` token extraction swallows all errors

**Location:** `atlas_ai.py:271-277`
**Confidence:** 0.80
**Risk:** `try: ... um.prompt_token_count ... except Exception: pass` silently drops any AttributeError, KeyError, or other failure to extract usage metadata. If the SDK changes the field name in a future release, all token counting silently breaks with no log warning.
**Vulnerability:** Observability gap masked by silent except. Same pattern repeats in `_call_gemini_with_search` (line 326).
**Impact:** Cost telemetry breaks on SDK upgrade with zero notification.
**Fix:** `except (AttributeError, KeyError) as e: log.debug("[atlas_ai] usage metadata unavailable: %s", e)`.

---

## Cross-cutting Notes

The fallback chain pattern (`try Claude → on Exception fall to Gemini → on Exception raise`) is replicated 5 times across `generate`, `generate_with_tools`, `generate_synthesis`, `generate_with_search`, `generate_stream`, and `generate_sync`. Each copy has slightly different timeout handling, exception logging, and `fallback_used` propagation. This pattern is begging to be extracted into a single `_with_fallback(primary_fn, fallback_fn, *, timeout)` helper — which would also be the natural place to add the rate-limit-aware retry logic, redacted exception logging, and per-call telemetry.

Two CLAUDE.md rules are satisfied here:
- `Use atlas_ai.generate() for all AI calls` — this *is* the implementation, so the rule is upstream of this file.
- `Handles run_in_executor internally` — verified at lines 458, 477, 511, 531, 622, 650, 676, 687, 738, 752, 778. All SDK calls properly delegate to executor; no blocking calls inside `async def` bodies.

The credential-leak risk in CRITICAL #3 likely affects every cog that catches an exception from `atlas_ai` and re-logs it with `{e}` — recommend a sweep of `oracle_cog.py`, `codex_cog.py`, `genesis_cog.py` for the same pattern in Ring 1 follow-up.
