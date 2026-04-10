# Adversarial Review: ledger_poster.py

**Verdict:** needs-attention
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 198
**Reviewer:** Claude (delegated subagent)
**Total findings:** 11 (2 critical, 5 warnings, 4 observations)

**ORPHAN STATUS: LIVE**
This file is not imported through bot.py's direct dependency chain but IS imported by active code: `economy_cog.py`, `flow_sportsbook.py`, `polymarket_cog.py`, `casino/casino.py` (4 importers, all in financial paths). Argus's static scan missed it because the bot.py spiral doesn't trace through cogs' indirect imports. Review as active production code.

## Summary

The universal ledger poster swallows `ImportError or Exception` in channel resolution, wraps the entire post flow in outer `except Exception` clauses that log only at WARNING (no stack trace), and has a retry loop where the third attempt re-raises to the outer swallow — meaning retries quietly fail. There is zero idempotency: the same casino result posted twice under a retry-the-interaction scenario produces two `#ledger` lines with different timestamps. For the financial audit trail this file exists to produce, the reliability profile is weaker than the wallet it's auditing.

## Findings

### CRITICAL #1: `_send_with_retry` re-raises on the final attempt, but the caller's outer `except Exception` swallows it without stack trace → all post failures are invisible
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:75-86, 122-123, 157-158, 197-198`
**Confidence:** 0.90
**Risk:** `_send_with_retry` retries up to 3 times with exponential backoff (1s, 2s, 4s). On the final attempt, if `channel.send` still fails, it `raise`s the `discord.HTTPException`. That exception propagates to the outer `try` in each public function, which catches it as `except Exception as e: log.warning(f"[LEDGER] Failed to post ... after {_MAX_RETRIES} attempts: {e}")`. The warning logs only `str(e)` — not the stack trace, not the line, not the user / amount context that's already in scope.
**Vulnerability:** Per CLAUDE.md Flow Economy Gotchas, silent `except Exception: pass` in admin-facing views is prohibited. This file is not a view, but it is the audit log for every financial write in the bot. Failing silently without a stack trace is the worst of both worlds: the bet/purchase/payout succeeds in the DB, the ledger channel has no record, and the only log artifact is a one-line WARNING that doesn't say what the post content was.
**Impact:** Users see their wallet balance change but no `#ledger` entry appears. Reconciling the wallet DB against the visible ledger is impossible because failed posts are not replayable. Financial audit trail has silent gaps.
**Fix:** Change `log.warning` to `log.exception` (which includes the stack trace). Also log the full context: `log.exception("[LEDGER] Failed to post %s result: user=%s amount=%s", game_type, discord_id, wager)`. Consider appending failed posts to a persistent "dead letter" table in `flow_economy.db` so they can be retried on next bot restart.

### CRITICAL #2: Zero idempotency — the same casino result posted twice under interaction retry produces two `#ledger` lines
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:89-198`
**Confidence:** 0.85
**Risk:** Every `post_*` function is a fire-and-forget wrapper around `channel.send(line)`. None of them take a `reference_key`, none check for a pre-existing line, none deduplicate on `txn_id`. If the caller's interaction retries (Discord's documented 3-strike pattern), and the caller wraps `post_casino_result` in the retry loop instead of just the wallet call, the ledger gets a duplicate.
**Vulnerability:** Per CLAUDE.md "ALL debit calls MUST pass `reference_key`" — the same idempotency discipline should apply to audit log writes that directly mirror wallet state. The file has a `txn_id: Optional[int]` parameter on every public function, but that parameter is only printed in the message line — never used as a dedupe key.
**Impact:** `#ledger` channel shows duplicate entries for a single wallet event. Historical audits cannot distinguish "two bets were placed" from "one bet was logged twice."
**Fix:** Add a caller-provided `reference_key: str` argument. Before posting, query the recent channel history (or a Redis/sqlite dedupe table) for that key. Skip the send if it's already been posted in the last N minutes.

### WARNING #1: `_resolve_channel` catches `(ImportError, Exception)` — `Exception` already catches `ImportError` and the tuple hides real errors
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:49-58`
**Confidence:** 0.95
**Risk:** The tuple `(ImportError, Exception)` is redundant because `ImportError` is a subclass of `Exception`. More importantly, the blanket `except Exception` catches all errors including `AttributeError` (bot has no guild), `KeyError` (guild_id not in config), and even a database error from `get_channel_id`. All of them return `None`, which causes the caller to silently skip posting. No logging, no re-raise.
**Vulnerability:** CLAUDE.md explicitly flags silent excepts. This one runs on every ledger post — it's the single most-exercised swallow in the file.
**Impact:** If `setup_cog` raises for any reason (DB schema drift, bad guild_id, misconfigured channel), every ledger post silently no-ops. Nobody is alerted.
**Fix:** `except ImportError: return None` (log it). For other exceptions, `log.exception("ledger channel resolution failed for guild=%s", guild_id); return None`.

### WARNING #2: `_send_with_retry` backoff is `2 ** attempt` starting from `attempt=0` → first retry is 1s, not respecting Discord's Retry-After header
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:75-86`
**Confidence:** 0.80
**Risk:** When Discord returns 429 Rate Limited, it includes a `Retry-After` header with the required backoff window (could be 2s, 10s, or 60s depending on the bucket state). This code ignores the header and retries after `2^attempt` seconds unconditionally. The discord.py HTTPException has `retry_after` attribute — not used.
**Vulnerability:** Under rate-limit pressure (high casino traffic), retrying too fast triggers another 429. The global rate limiter may even suspend the bot for a longer period because of the hammering.
**Impact:** Ledger posts fail more often under load. discord.py may globally rate-limit the bot, affecting unrelated commands.
**Fix:** If the exception is 429, `await asyncio.sleep(e.retry_after or 2 ** attempt)`. Also honor the `X-RateLimit-Reset` header if available.

### WARNING #3: `_send_with_retry` catches only `discord.HTTPException` — misses `discord.ConnectionClosed`, `aiohttp.ClientError`, `asyncio.TimeoutError`
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:78-86`
**Confidence:** 0.70
**Risk:** Transient network errors during send may raise `aiohttp.ClientConnectorError` or `asyncio.TimeoutError` rather than `discord.HTTPException`. These bubble up to the outer `except Exception` in the public functions and are logged as a warning without retry.
**Vulnerability:** Retries only catch the subset of failures that discord.py happens to wrap. Raw transport errors are not retried at all.
**Impact:** During a 30-second network blip, all ledger posts fail with no retry.
**Fix:** Broaden the caught exception set: `except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError)`.

### WARNING #4: `_get_display_name` uses `channel.guild.get_member(discord_id)` — cache-only lookup with no `fetch_member` fallback
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:61-67`
**Confidence:** 0.75
**Risk:** `get_member` is a synchronous cache lookup. If the user is not in the cache (common after a bot restart before the member chunker runs, or for users who joined while the bot was down), it returns None and the function falls back to `"User {discord_id}"` — a raw snowflake number. Casino players who joined recently see their ledger entries as numeric IDs.
**Vulnerability:** Bot cache isn't populated for all members unless `members` intent is enabled AND chunking is complete. No explicit `fetch_member` fallback.
**Impact:** New users' ledger entries display as numbers, looking like bugs or broken permissions.
**Fix:** If `get_member` returns None, `try: member = await channel.guild.fetch_member(discord_id) except discord.NotFound: pass`. Be aware this API call counts against rate limits — cache the result.

### WARNING #5: `post_bet_settlement` has no parameter validation — `matchup`, `pick`, `bet_type` are inserted into a Discord message with no length cap
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:165-198`
**Confidence:** 0.65
**Risk:** Discord message length cap is 2000 chars. The message constructed at lines 190-194 concatenates `matchup`, `bet_type`, `pick`, and formatted numbers. If `matchup` is a long title like "Very Long Team Name vs Another Extremely Long Team Name Championship Match Week 17", the total line could exceed 2000 characters, causing `channel.send` to raise `discord.HTTPException: 400 Bad Request`.
**Vulnerability:** The caller has no way to know ahead of time if the line will fit. The 400 error then triggers the retry loop (which won't fix anything — the same payload will fail again), burning retry budget.
**Impact:** Long matchup names produce dead posts.
**Fix:** Truncate inputs: `matchup = matchup[:200]`, `pick = pick[:100]`, etc. Or use an embed instead of a plain message (embeds have a 6000-char cap and are visually cleaner).

### OBSERVATION #1: No async channel resolution — `_resolve_channel` imports `setup_cog` lazily on every call
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:49-58`
**Confidence:** 0.70
**Risk:** Every post does a fresh `from setup_cog import get_channel_id`. Python caches module imports so the perf hit is minimal, but the pattern suggests the author was trying to avoid a circular import — which is a code smell worth investigating.
**Fix:** Cache the channel at module init or inject the channel via a setter.

### OBSERVATION #2: `_timestamp` uses UTC but displays as naive text — users in other timezones see UTC times with no indication
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:70-72`
**Confidence:** 0.75
**Risk:** The format `"%b %d %H:%M"` produces `"Mar 16 14:32"` with no timezone suffix. Users assume it's their local time but it's UTC.
**Fix:** Either use Discord's native `<t:unix_timestamp:t>` format (auto-converts to user's timezone) or append "UTC" to the string.

### OBSERVATION #3: `post_transaction` signature has `description: str = ""` — empty strings result in a trailing `| ` that looks ugly
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:132-155`
**Confidence:** 0.40
**Risk:** The `if description:` guard at line 151 prevents the empty-string case, but the function's default sends `""` which works. Minor stylistic concern.
**Fix:** None needed, the guard handles it.

### OBSERVATION #4: Three near-identical `post_*` functions with duplicated boilerplate — resolve channel, get display name, format timestamp, build line, send with retry, except
**Location:** `C:/Users/natew/Desktop/discord_bot/ledger_poster.py:89-198`
**Confidence:** 0.60
**Risk:** DRY violation. Adding a new audit type means duplicating the pattern.
**Fix:** Extract a `_post_line(bot, guild_id, line: str, context: dict) -> bool` helper that handles resolve + send + except. Each public function only builds the line.

## Cross-cutting Notes

This file is called from 4 importers — `economy_cog`, `flow_sportsbook`, `polymarket_cog`, `casino/casino.py` — and every single call goes through the same unreliable path: resolve channel silently → get member silently → format timestamp → send with an ignorant retry → swallow any exception. The reliability profile of the audit log is strictly worse than the wallet it's auditing. Recommend a one-session pass to: (1) add idempotency via `reference_key`, (2) replace `log.warning` with `log.exception` everywhere, (3) persist failed posts to a dead-letter table so they can be replayed after a network blip. Same pattern probably exists in other "post to channel" helpers worth a check.
