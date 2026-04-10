# Adversarial Review: flow_events.py

**Verdict:** needs-attention
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 79
**Reviewer:** Claude (delegated subagent)
**Total findings:** 10 (2 critical, 4 warnings, 4 observations)

**ORPHAN STATUS: LIVE**
This file is not imported through bot.py's direct dependency chain but IS imported by active code: `flow_live_cog.py`, `polymarket_cog.py`, `real_sportsbook_cog.py`, `sportsbook_core.py`, `casino/casino.py` (8+ importers). Argus's static scan missed it because the bot.py spiral doesn't trace through cogs' indirect imports. Review as active production code.

## Summary

A 79-line event bus module that is load-bearing for the entire Flow / Sportsbook / Casino / Prediction stack. Zero concurrency control, zero payload validation, a catch-all `except` that silently eats every handler exception, and event schemas missing the fields needed by the very contracts that use them. Ship-blocking for anything that depends on ordered, idempotent, observable event delivery — which is exactly what the settlement worker is built on top of.

## Findings

### CRITICAL #1: `flow_bus.emit` runs handlers sequentially with no timeout → one slow subscriber blocks all others and can stall the caller indefinitely
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:67-72`
**Confidence:** 0.95
**Risk:** The emit loop `for handler in self._handlers.get(event_type, []): await handler(event)` awaits each handler inline. If any handler is slow (network call, long DB query, Playwright render inside a highlight card, `asyncio.Lock` contention) every subsequent handler and the `await flow_bus.emit(...)` call site itself stall until the slow handler resolves. For `EVENT_FINALIZED` the subscribed handler is `settle_event` which grades all linked bets — a heavy path that wraps `flow_wallet.credit()` / `debit()` and runs sqlite I/O.
**Vulnerability:** Publishers like `real_sportsbook_cog.py:612` and the `polymarket_cog.py` emit sites `await` the emit from inside their own settlement loops. A stalled handler therefore stalls the caller's `async with aiosqlite.connect(...)` block (see `real_sportsbook_cog.py:600` where the emit is inside the DB session), holding a sqlite write transaction open and blocking every other writer on `flow_economy.db` / `sportsbook.db`.
**Impact:** Cascading deadlock when a single prediction or sportsbook handler gets stuck — the whole settlement loop freezes, highlights stop posting, and open DB transactions time out or leak connections. The Ring 1 audit of `flow_live_cog.py` already flagged that `_on_*` handlers do not defend against malformed payloads; combine that with "no timeout on emit" and you get a deadlocked bot.
**Fix:** Wrap each handler in `asyncio.wait_for(handler(event), timeout=10.0)` and schedule handlers via `asyncio.gather(..., return_exceptions=True)` or `asyncio.create_task()` so a single slow/hung subscriber cannot hold the publisher. Log timeouts at `ERROR` level with event type and handler name.

### CRITICAL #2: `PredictionEvent` has no `discord_id` or winner-identity fields despite being consumed by a user-facing handler
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:43-49`
**Confidence:** 0.85
**Risk:** `PredictionEvent` only carries `guild_id`, `market_title`, `resolution`, `total_payout`, `winners (count)`. The `flow_live_cog._on_prediction_result` subscriber documented at `flow_live_cog.py:429` expects to render a highlight card naming the winners. Without any user identity field on the event, the handler either has to re-query the DB (which re-opens the original race between settlement and card-post) or emits a generic card that attributes nothing to anyone.
**Vulnerability:** Every consumer that tries to enrich the event is forced to bypass the contract and read DB state that the publisher just wrote — re-introducing TOCTOU the event bus was supposed to eliminate. Worse: the emit path in `polymarket_cog.py` at 2825 / 2878 / 3593 already uses `EVENT_FINALIZED` with `{"event_id", "source"}` dict payloads, **not** `PredictionEvent`. So `PredictionEvent` is a documented schema that no live publisher uses, and `"prediction_result"` is a documented topic that no live publisher publishes. The types in this file have drifted from reality.
**Impact:** Half of the documented Flow Live contract is dead code. Ring 1 flagged "no live publishers for sportsbook_result" — this is the same pattern for prediction. Subscribers bind to topics that will never fire, burning memory and masking the real emit path.
**Fix:** Either (a) delete `PredictionEvent`, `SportsbookEvent`, and `GameResultEvent` plus the `"game_result"` / `"sportsbook_result"` / `"prediction_result"` topic strings if they are all dead, and add a single source-of-truth for `EVENT_FINALIZED`'s payload shape; or (b) audit every subscribe call in `flow_live_cog.py:427-429` against actual live publishers and re-wire the missing publishers — per CLAUDE.md the `SportsbookEvent.guild_id` wiring is a documented gotcha.

### WARNING #1: `self._handlers` is not thread-safe and has no lock around mutation during emit
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:51-72`
**Confidence:** 0.75
**Risk:** `emit()` iterates `self._handlers.get(event_type, [])` without copying. If `subscribe()` / `unsubscribe()` / `clear()` is called concurrently (e.g., during `cog_unload` while an emit is in flight), the iteration raises `RuntimeError: list changed size during iteration`.
**Vulnerability:** Single-threaded asyncio protects you from most races, but `await handler(event)` at line 70 yields control, allowing any other coroutine to mutate the same handler list (including the handler itself calling `unsubscribe`). The docstring of `clear()` even says "used in ... cog_unload" — which means clear() can race emit() during a reload.
**Impact:** Runtime exception during cog reload or shutdown; half-processed event broadcast; potential handler skipping.
**Fix:** `for handler in list(self._handlers.get(event_type, [])):` — iterate a snapshot. Better: use `contextvars` or `asyncio.Lock` during mutation.

### WARNING #2: `subscribe()` has no duplicate-registration check; cogs reloaded twice get double-fired
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:55-57`
**Confidence:** 0.85
**Risk:** `self._handlers.setdefault(event_type, []).append(handler)` always appends — no identity check. On cog reload (a normal Discord dev workflow), the new `_on_game_result` handler is appended alongside the old one. Now every emit fires both handlers: the stale one references a dead cog instance and blows up, or worse, references a live-but-older self and processes the event twice.
**Vulnerability:** There is no `cog_unload` → `flow_bus.unsubscribe(...)` pattern documented anywhere I can see in this file. Every reloader session stacks handlers until restart. `clear()` exists but is opt-in and wipes *every* subscriber, not just the reloading cog's.
**Impact:** Duplicate highlight cards, duplicate credit/debit from retry-style handlers, exponential handler count across reloads — each triggering the bug class that `reference_key` is supposed to prevent.
**Fix:** `if handler in self._handlers.setdefault(event_type, []): return` at line 56. Additionally, document that consumers MUST call `flow_bus.unsubscribe` in `cog_unload`.

### WARNING #3: `emit()` swallows every exception with a bare `except Exception:` — hides handler crashes completely
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:69-72`
**Confidence:** 0.90
**Risk:** When a handler raises (AttributeError on malformed payload, sqlite timeout on a locked DB, KeyError on missing dict field), the emit loop logs `log.exception` and continues to the next handler. The publisher has no way of knowing that its event was dropped.
**Vulnerability:** Per CLAUDE.md "Silent `except Exception: pass` in admin-facing views is PROHIBITED." This is not a view but it is the critical path for financial settlement. Swallowing here means a `settle_event` failure shows up only as a log line nobody reads; the sportsbook bets remain in `Pending` state forever and the fallback `settlement_poll` loop (runs every 10 min per `sportsbook_core.py:625`) becomes the only safety net. When it also fails, money is never credited.
**Impact:** Silent financial loss or stuck pending bets. Hard to diagnose because the stack trace is buried in debug logs with no correlation ID.
**Fix:** Propagate `CancelledError` / `KeyboardInterrupt` immediately. For other exceptions, keep logging, but also publish a dead-letter topic or bump a metric counter so on-call has a signal. At minimum, log the event payload alongside the stack trace.

### WARNING #4: `GameResultEvent.extra: dict` uses mutable default that is correctly shared-per-instance — but the type is `dict`, not `Dict[str, Any]`, and has zero validation
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:15-30`
**Confidence:** 0.65
**Risk:** `extra` is a free-form dict that every game subsystem (blackjack/slots/crash/coinflip/scratch/roulette) fills with whatever metadata it wants. Downstream handlers in `flow_live_cog._on_game_result` pull fields by key — there is no schema enforcement and no typed accessor. A typo in a producer (`extra["multplier"]`) silently returns None when the consumer reads `extra.get("multplier")`.
**Vulnerability:** 8+ publishers, zero producers or consumers with a shared contract file. A new game that emits `extra={"mult": 2.5}` instead of `extra={"multiplier": 2.5}` will silently make the highlight card render wrong.
**Impact:** Display bugs in highlight cards that are not reproducible in tests because tests don't cover every game's `extra` schema.
**Fix:** Either define a TypedDict per game type and require emitters to use it, or drop `extra` and promote every field to a proper attribute.

### OBSERVATION #1: `flow_bus` is a module-level singleton with no bot lifecycle hook
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:78-79`
**Confidence:** 0.90
**Risk:** Handlers survive across `bot.close()` → `bot.start()`. Cog references trapped in closures become zombies after reload. The only way to reset is by calling `clear()` explicitly — nobody does.
**Fix:** Attach `flow_bus` to the bot instance as `bot.flow_bus = FlowEventBus()` and let `bot.close()` clear it automatically. Or provide a bot-scoped singleton.

### OBSERVATION #2: `EVENT_FINALIZED = "event_finalized"` is the only real contract, and it's at the bottom of the file
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:74-76`
**Confidence:** 0.80
**Risk:** A developer reading this file top-to-bottom sees three dataclasses that look like the main events, then at the bottom discovers that the actually-used topic is a string constant with a dict payload documented only as a comment. The three dataclasses are visually louder than the one real contract.
**Fix:** Either promote `EVENT_FINALIZED` to its own dataclass (`EventFinalizedPayload`) or move it and its comment to the top of the file, above the probably-dead dataclasses.

### OBSERVATION #3: No `txn_id` on `SportsbookEvent` or `PredictionEvent`
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:32-49`
**Confidence:** 0.70
**Risk:** `GameResultEvent` has an optional `txn_id: Optional[int]` so the subscriber can link the event back to a wallet ledger entry. `SportsbookEvent` and `PredictionEvent` do not, so a highlight card cannot link to the authoritative ledger row. Consumers who try to correlate events with wallet writes have no join key.
**Fix:** Add `txn_id: Optional[int] = None` to both dataclasses for consistency and audit traceability.

### OBSERVATION #4: `Callable` is imported from typing with no parameter signature
**Location:** `C:/Users/natew/Desktop/discord_bot/flow_events.py:11, 53, 55, 58`
**Confidence:** 0.85
**Risk:** Type hints use `Callable` with no arg or return type. mypy/pyright cannot catch `subscribe("game_result", handler_that_takes_no_args)`.
**Fix:** `Callable[[object], Awaitable[None]]` (or define a `Protocol`). Declare that every handler must be async and take a single event arg.

## Cross-cutting Notes

This file is the "source of truth" for multiple half-dead contracts. Ring 1 already found that `sportsbook_result` has no live publisher; this review found that `PredictionEvent` and `GameResultEvent` are only used by a couple of call sites but are documented as if they're the main contract. Meanwhile `EVENT_FINALIZED` — the only topic that settlement depends on — is a loose string constant with a dict payload and no dataclass. Recommend a one-session cleanup that picks a side: either "event_bus uses typed dataclasses for everything" or "event_bus uses a single EVENT_FINALIZED dispatch with payload dicts and deletes the dataclasses." Living in between is how `flow_live_cog` ended up subscribing to dead topics.
