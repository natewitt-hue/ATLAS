# Adversarial Review: espn_odds.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 645
**Reviewer:** Claude (delegated subagent)
**Total findings:** 23 (2 critical, 10 warnings, 11 observations)

## Summary

`espn_odds.py` is a mostly well-structured async HTTP client for ESPN's unofficial odds API, with TTL caching, rate limiting, and retry logic. However, there are two critical issues: an **undeclared `cachetools` dependency** that will crash the bot on import if the package is ever missing, and a **request-lock serialization bug** that forces all API calls (including sleeps up to 16s during 429 backoff) to run strictly sequentially, defeating concurrency and risking Discord interaction timeouts. Several additional warnings concern the lock-time-vs-display-time odds (TOCTOU) problem, cache-key collisions across providers, and silent data-shape fragility against ESPN's dual-format (scoreboard vs core) responses.

## Findings

### CRITICAL #1: `cachetools` is imported but not declared in requirements.txt

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:25`
**Confidence:** 0.98
**Risk:** `from cachetools import TTLCache` is a hard top-of-module import. If `cachetools` is not installed in the deployment environment, `espn_odds.py` fails to import, which cascades into `real_sportsbook_cog.py` failing to load (cog #14 in load order), and the entire real sportsbook subsystem is silently unavailable.
**Vulnerability:** A grep of `C:/Users/natew/Desktop/discord_bot/requirements.txt` shows only `aiohttp>=3.9.0` — `cachetools` is NOT listed. The package currently exists in the dev venv but will not survive a clean reinstall, VPS deploy (FROGLAUNCH project), or Docker rebuild.
**Impact:** Production import failure on next clean deploy. Silent loss of real NFL/NBA betting. A fresh `pip install -r requirements.txt` will succeed but the bot will then crash at cog-load time with `ModuleNotFoundError: No module named 'cachetools'`.
**Fix:** Add `cachetools>=5.3.0` to `requirements.txt`, OR replace `TTLCache` with a tiny in-house dict-with-expiry (the use is simple enough — see `sportsbook_core.py` or `data_manager.py` for an in-house pattern). Given the five distinct TTL windows, adding the dependency is cleaner — just declare it.

### CRITICAL #2: `_request_lock` serializes ALL HTTP requests including retry sleeps, defeating concurrency

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:129-168`
**Confidence:** 0.92
**Risk:** The `async with self._request_lock:` scope wraps the ENTIRE retry body — including the throttle sleep, the HTTP `await session.get(...)`, AND the 429 backoff `await asyncio.sleep(wait)` (up to `2^3 = 8` seconds per retry, plus the 15s HTTP timeout). In the worst case a single bad URL holds the lock for **15s timeout + 2s + 4s + 8s ~ 29 seconds**, during which every other call in the bot blocks. The caller at `real_sportsbook_cog.py:430` wraps `get_upcoming_odds` in `asyncio.wait_for(..., timeout=10.0)`, so a 429 on ONE sport's scoreboard fetch can cause the slash command handler to abort even though the lock is held by a completely unrelated call in the background.
**Vulnerability:** The `_request_lock` was likely intended to serialize only the throttle window (reading/updating `self._last_request_time`), but it wraps the entire `try` block. When `get_all_upcoming_odds` (line 485) iterates 9 leagues sequentially, each call acquires the lock; even the single-call path from a slash interaction can be blocked by a background sync task holding the lock.
**Impact:** Users see "interaction failed" on the real sportsbook hub while background maintenance tasks hold the lock. In conjunction with the `_REQUEST_DELAY=0.3s` throttle and 9 leagues × 7 days = 63 scoreboard fetches, the worst-case cold-cache call from `get_all_upcoming_odds` can take upwards of 60 seconds, far beyond Discord's 15-minute interaction window cap when combined with 429s.
**Fix:** Narrow the lock to only the throttle check. Move `session.get(...)` outside `async with self._request_lock:`. Pattern:
```python
async with self._request_lock:
    loop = asyncio.get_event_loop()
    elapsed = loop.time() - self._last_request_time
    if elapsed < self._REQUEST_DELAY:
        await asyncio.sleep(self._REQUEST_DELAY - elapsed)
    self._last_request_time = loop.time()
# lock released — HTTP can run concurrently
try:
    async with session.get(url, timeout=...) as resp:
        ...
```
Better still, replace lock+timestamp with an `asyncio.Semaphore(N)` that permits N concurrent requests and let aiohttp's connection pool handle the rest.

### WARNING #1: Lock-time vs display-time odds TOCTOU — no odds snapshotting in client

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:119-170` (and consumers at `real_sportsbook_cog.py:430, 527`)
**Confidence:** 0.85
**Risk:** `espn_odds.py` returns live-fetched odds that the caller (`real_sportsbook_cog`) displays to users and then uses to settle bets. Because there is no "lock odds at bet placement" mechanism in this client, a user can view a spread of `-6.5` on the hub, click to bet, and by the time the bet writes to DB the spread has moved to `-7.5`. There is no guarantee that the odds the user saw at click-time match the odds written to `flow.db`.
**Vulnerability:** Cache TTLs (scoreboard=60s, odds=30s) MITIGATE this to cache-window granularity, but the guarantee only holds if the same URL is re-used within the TTL window. The hub view fetches `get_upcoming_odds` (cached under the SCOREBOARD URL), then the bet placement path calls `get_game_odds` (cached under the CORE odds URL) — these are DIFFERENT cache keys, so the bet modal may see different numbers than the hub embed even within a single interaction flow.
**Impact:** Users believe they bet one line and get settled at another — financial ledger disputes, trust erosion, possible need for manual `flow_audit` adjustments.
**Fix:** The client SHOULD expose a `get_cached_odds(event_id, league)` or `snapshot_odds(event_id)` method that the caller uses at bet-write time and stores the snapshot in the bet row. Alternatively, accept odds values as parameters to `write_bet()` and never refetch post-click. This is really a `real_sportsbook_cog.py` concern but the client should make it easier to do the right thing.

### WARNING #2: `asyncio.get_event_loop()` is deprecated in Python 3.10+ and emits `DeprecationWarning` in 3.12+

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:132, 141`
**Confidence:** 0.97
**Risk:** `asyncio.get_event_loop()` is deprecated when no running loop exists; in Python 3.14 (the project's declared Python per CLAUDE.md) calls may warn or raise. The code uses `loop.time()` as a monotonic clock source, which is equivalent to `time.monotonic()`.
**Vulnerability:** Per Python 3.12 release notes and PEP 0, `asyncio.get_event_loop()` without a running loop emits a `DeprecationWarning` and will eventually raise. Here we are inside a coroutine so there IS a running loop, so it works — but this is the wrong API.
**Impact:** Python 3.14 upgrade friction. Deprecation spam in logs. Eventual breakage.
**Fix:** Use `asyncio.get_running_loop().time()` or simply `time.monotonic()`.

### WARNING #3: Cache keyed by URL only — provider_id collisions across callers

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:119-148` and callers at lines 425, 466, 527
**Confidence:** 0.80
**Risk:** The scoreboard URL does NOT encode `provider_id` (it's a query-less `?dates=YYYYMMDD` URL). When the same scoreboard is fetched once with `provider_id=1004` (Consensus) and once with `provider_id=41` (DraftKings), the first cached result is served for both, so the second caller gets data parsed against the wrong provider.
**Vulnerability:** `_parse_competition` picks odds FROM the response based on `provider_id`, but `_fetch` caches the raw JSON (which contains ALL providers) under the URL alone. That's actually OK for the raw JSON — but `get_upcoming_odds` then parses with a specific provider, and if two callers request different providers, the SECOND caller gets the parsed result based on `_parse_competition` running against the cached raw JSON, which IS fine… until you look at the `_odds_cache` at line 527 where `get_game_odds` calls `_fetch(url, cache=self._odds_cache)`. That URL is also provider-less and does cache the unparsed data. OK in this case. BUT the cache-locking pattern in `_fetch` caches the raw data, so this is probably fine on re-read. Lower severity than first thought — narrowing to a smell rather than a bug.
**Impact:** None if only the raw JSON is cached. Re-classifying: not a confirmed bug, just a fragile contract. If a future refactor caches parsed results, the provider collision becomes real.
**Fix:** Document explicitly that cache holds raw ESPN responses; all per-provider parsing happens post-cache. Consider adding a defensive assertion at cache-hit time.

### WARNING #4: `event.get("date", comp.get("date", ""))` returns empty string for missing date — not None

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:376`
**Confidence:** 0.90
**Risk:** Downstream consumers in `real_sportsbook_cog.py` and `sportsbook_core.py` treat `event_date` as an ISO 8601 string to parse. An empty string will fail `datetime.fromisoformat("")` with `ValueError`, likely inside a sync function that wasn't expecting it.
**Vulnerability:** ESPN is known to return partial event objects for pre-release games. The fallback to `""` hides a missing-data condition that should surface as `None` or raise.
**Impact:** Crash when a game without a date reaches the caller, likely in the scoreboard sync path, leaving the caller's scheduled task silently broken on that sport.
**Fix:** Return `None` instead of `""`: `event_date = event.get("date") or comp.get("date")`. Caller already has a validation step; let it handle None.

### WARNING #5: `win_probability` parser only looks inside `odds` items — never calls `/probabilities` endpoint

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:360-374`
**Confidence:** 0.70
**Risk:** The `_parse_competition` function iterates through `odds_list` looking for `homeTeamOdds.winPercentage`. ESPN scoreboard rarely populates this field for upcoming games — it comes from the `/probabilities` endpoint (which `get_live_probabilities` fetches separately). So `wp_data` is almost always `{"home": None, "away": None}` for scoreboard-derived games.
**Vulnerability:** The for-loop `break` is inside an `if home_wp is not None`, but since ESPN rarely has this field in the scoreboard, the loop falls off the end with both values as None. Users never see win probabilities in the hub.
**Impact:** Hub embed shows stale/empty win probability column. Not a bug per se, but a silent feature gap masquerading as a working field.
**Fix:** Either document that `win_probability` is scoreboard-only and is expected to be None for most games, or call `get_live_probabilities(event_id)` when game status is `"live"` and merge the result into the normalized dict.

### WARNING #6: `get_game_odds` parses `"items"` key that may not exist, falls back to `data` itself

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:533-547`
**Confidence:** 0.82
**Risk:** Line 533: `items = data.get("items", data) if isinstance(data, dict) else data`. If the core API returns `{"count":10, "pageCount":1, "items":[{"$ref":"..."}]}` (which it often does for odds endpoints — items are `$ref` pointers, not inlined objects), the loop at 535 iterates pointer dicts that have no `.provider` key, and every provider entry silently becomes `pid=0` with all Nones. The user gets a "detail view" that shows nothing.
**Vulnerability:** ESPN's core API uses `$ref` lazy loading extensively. The code does not dereference `$ref` URLs, it just assumes odds items are inlined.
**Impact:** The per-game detail drill-down (`get_game_odds`) returns empty provider maps even for games with real odds, so the UI silently shows "no data" instead of the actual lines. Every provider ends up keyed by `pid=0` because `int(prov.get("id", 0))` defaults to 0 when the item is a pointer stub.
**Fix:** Check for `$ref` in items and either (a) dereference via a follow-up fetch or (b) log and fall back to scoreboard data. Do NOT silently collapse multiple providers to key `0`.

### WARNING #7: `get_team_ats()` has hardcoded default `year=2025` — will silently return stale data in 2027+

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:578`
**Confidence:** 0.88
**Risk:** `year: int = 2025` as a default argument. When called without an explicit year, callers in future seasons get last-year's ATS record. The value is baked in at class-load time.
**Vulnerability:** The CLAUDE.md note says today's date is 2026-04-09, meaning `year=2025` is already the PRIOR season. Every call with the default is already returning stale data.
**Impact:** ATS records shown in UI are from the wrong season. Directly affects decision-making quality.
**Fix:** Compute default dynamically: `year: int | None = None` and if None, derive from `datetime.now(timezone.utc).year` (with season-rollover logic — NFL season spans two calendar years, so use Aug cutoff). Or pull from a central constant.

### WARNING #8: `_safe_int("-6.5")` returns None silently — spread odds are lost

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:628-635, 324-325`
**Confidence:** 0.80
**Risk:** `_safe_int` uses Python's `int()` which raises on float strings like `"-6.5"` or `"-110.0"`. ESPN sometimes returns odds as `"-110.0"` or `"110"` strings; the first fails and returns None silently. Callers that read `spread_data["home_odds"]` see None and display "—" when the data was actually present.
**Vulnerability:** `int("-110.0")` → `ValueError` → None via the except block. The bug is masked because the odds usually come as integer strings.
**Impact:** Intermittent "missing" odds displayed in the hub when ESPN changes response format or returns floats. Silent data loss.
**Fix:** `_safe_int` should first try `float()` then cast: `return int(float(val))`. Or use a `_safe_int_from_odds` that handles both cases.

### WARNING #9: Silent cache write race (TTLCache is not thread/async safe in arbitrary ways)

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:123-125, 146-147`
**Confidence:** 0.55
**Risk:** The `_cache_lock` protects cache access, but only inside `_fetch`. Python's `cachetools.TTLCache` is documented as "not thread-safe" — which in an asyncio context usually works fine since there's one thread, but `TTLCache.__getitem__` can trigger eviction of expired entries, which mutates the internal dict. If two coroutines happen to switch context inside eviction, the invariants can be violated. The `_cache_lock` here DOES serialize access, so this is probably safe in practice.
**Vulnerability:** Lower severity — the locking is sufficient. BUT the `async with self._cache_lock:` is held while executing `cache[url] = data`, which may evict entries; that's fine since it's under the lock. OK.
**Impact:** Unlikely to manifest in practice.
**Fix:** Keep as-is. Could be worth a comment that `_cache_lock` is load-bearing for TTLCache's non-thread-safety.

### WARNING #10: `_REQUEST_DELAY=0.3s` + 9 leagues × 7 days = 18.9s minimum hot-path cost for `get_all_upcoming_odds`

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:79, 421-422, 485-497`
**Confidence:** 0.85
**Risk:** `get_all_upcoming_odds` iterates `LEAGUE_MAP` (9 leagues) and calls `get_upcoming_odds` for each. Each of those iterates 7 days. That's 63 sequential scoreboard fetches minimum. With `_REQUEST_DELAY=0.3s`, the throttle alone adds `63 * 0.3 = 18.9s` on a cold cache even if every HTTP call completes in zero time. Combined with a 15s timeout per call in the worst case, the worst-case cold-cache execution is well beyond Discord's 15-minute interaction cap (not immediate crash) but far beyond reasonable UX (`asyncio.wait_for(..., timeout=10.0)` at `real_sportsbook_cog.py:430` will fire).
**Vulnerability:** Sequential iteration. The caller at `real_sportsbook_cog.py:430` wraps only the single-league `get_upcoming_odds` in a 10s timeout, so that call at least bounds itself; but `get_all_upcoming_odds` has no such bound and is awaited in sync sync tasks without a timeout.
**Impact:** Background sync tasks block slash commands (because of the `_request_lock` issue above). Users see "interaction failed". If a background sync is running during peak usage, every slash command fails.
**Fix:** Fan out with `asyncio.gather` and let the throttle still enforce minimum delay globally via semaphore. Combined with WARNING #2 fix, this should collapse to a few seconds.

### OBSERVATION #1: No content-type verification on `resp.json()`

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:144`
**Confidence:** 0.85
**Risk:** `await resp.json()` assumes the 200 response is valid JSON. ESPN sometimes returns HTML error pages with 200 status under heavy load or during incidents. `aiohttp.ClientResponse.json()` raises `aiohttp.ContentTypeError` if the content-type is wrong, OR `json.JSONDecodeError` (wrapped) if parsing fails. Neither is caught in the try block (which only catches `aiohttp.ClientError, asyncio.TimeoutError`).
**Impact:** Uncaught exception bubbles out of `_fetch`, crashing whatever coroutine called it. Slash command returns "interaction failed".
**Fix:** Add `json.JSONDecodeError` and `aiohttp.ContentTypeError` to the except tuple. Or use `content_type=None` arg to `resp.json()` to bypass content-type check and wrap the whole block.

### OBSERVATION #2: Session is never closed on exception — resource leak potential

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:105-115`
**Confidence:** 0.60
**Risk:** `_get_session` lazily creates an `aiohttp.ClientSession`. The `close()` method is documented as "Call from cog_unload" but if the cog is reloaded or the bot crashes mid-session, the TCP connections leak. There's no `__aenter__/__aexit__` context manager pattern.
**Impact:** Socket exhaustion after many reloads during development. Not impactful in steady-state production.
**Fix:** Add `async def __aenter__(self): return self` and `async def __aexit__(self, *args): await self.close()`. Or document that reload handlers must call `close()`.

### OBSERVATION #3: `self._session = aiohttp.ClientSession(...)` without connector limits

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:107-109`
**Confidence:** 0.70
**Risk:** Default aiohttp connector limits are `limit=100, limit_per_host=0` which is unbounded per host. If the `_request_lock` bug (CRIT #2) is fixed, concurrent requests could hammer ESPN's unofficial API and trigger rate limiting.
**Impact:** Future concurrency bug if lock is narrowed without adding explicit connection limits.
**Fix:** `aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10, limit_per_host=5), ...)`. Pair with the semaphore fix from CRIT #2.

### OBSERVATION #4: Timeout uses `aiohttp.ClientTimeout(total=...)` only — no connect/read timeouts

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:139`
**Confidence:** 0.50
**Risk:** `ClientTimeout(total=15)` gives 15s for the entire request including DNS, connect, TLS, and read. If DNS is slow, very little time is left for response. More granular timeouts (connect=5, sock_read=10) would be more resilient.
**Impact:** Rare and minor. On a healthy network, irrelevant.
**Fix:** `aiohttp.ClientTimeout(connect=5, sock_connect=5, sock_read=10, total=15)`.

### OBSERVATION #5: Exponential backoff uses `2 ** (attempt + 1)` — off-by-one vs typical backoff

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:155, 166`
**Confidence:** 0.60
**Risk:** Attempt 0 waits `2^1=2s`, attempt 1 waits `2^2=4s`, attempt 2 waits `2^3=8s`. Total possible wait = 14s. That's combined with 15s timeout per attempt = up to 45 seconds per URL. No jitter — retries from multiple clients synchronize.
**Impact:** Slower than necessary backoff; no jitter means "thundering herd" if multiple instances retry at the same time.
**Fix:** Standard `2 ** attempt + random.uniform(0, 1)` and cap at e.g. 10s.

### OBSERVATION #6: `target_odds = odds_list[0]` fallback silently picks WRONG provider

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:303-304`
**Confidence:** 0.72
**Risk:** When the requested provider (e.g., DraftKings=41) isn't present in the response, the code silently falls back to `odds_list[0]`, but `spread_data["provider_id"] = int(prov.get("id", provider_id))` on line 309 records the ACTUAL provider from the response, so the returned provider_id may differ from the requested one. This is correct behavior but the CALLER might not notice the substitution.
**Impact:** The caller/UI shows "Spread: -6.5 (Consensus)" when the user requested DraftKings lines. Minor UX confusion.
**Fix:** Log the substitution at DEBUG level: `log.debug(f"Requested provider {provider_id} not found, falling back to {prov.get('name')}")`. Caller decides whether to show a badge.

### OBSERVATION #7: `_safe_float(total_line)` on string with "o233.5" — the parser handles o/u prefix but only on string

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:349-352`
**Confidence:** 0.55
**Risk:** `total_line.lstrip("oOuU")` assumes total_line is a string, which the `isinstance(total_line, str)` check guards. Then `_safe_float(total_line)` is called. If ESPN returns `total_line` as already-a-float (e.g., `233.5`), the `isinstance(str)` check is False and the lstrip is skipped, which is correct. But the logic is subtle and the `or` at line 349 picks the first truthy value, which could be a string for one and a float for the other in a malformed response.
**Impact:** Minor parse fragility.
**Fix:** Normalize: coerce both to string first, then strip. Or use explicit regex `re.sub(r'^[oOuU]', '', str(total_line))`.

### OBSERVATION #8: `_branding_key_for` returns `sport_key` unchanged if not found — silent fallthrough

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:196-199`
**Confidence:** 0.45
**Risk:** If called with a bogus sport_key, the function returns it unchanged instead of raising. This makes bugs harder to diagnose — the branding UI will then try to look up `"americanfootball_nfl"` instead of `"NFL"` and silently return no branding.
**Impact:** Silent UI degradation (missing logos) instead of fast-fail.
**Fix:** `return entry[2] if entry else None` and force callers to handle.

### OBSERVATION #9: `get_all_upcoming_odds` swallows all errors per-league — no partial failure surface

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:485-497`
**Confidence:** 0.65
**Risk:** Each `get_upcoming_odds` call is awaited directly; if ONE league's `_fetch` returns an empty list due to persistent 5xx, the `if games:` filter drops it silently with no log. Caller has no way to know NFL failed but NBA succeeded.
**Impact:** Partial data shown in UI without any indication. "Real Sportsbook" hub shows NBA games only, no error.
**Fix:** Log warning when a league returns zero games (could be a real "no games today" or an API failure — distinguishing those is valuable). Consider returning `{sport_key: None}` for failed fetches vs `{sport_key: []}` for genuinely empty.

### OBSERVATION #10: No retry on `aiohttp.ContentTypeError` or `json.JSONDecodeError`

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:163-168`
**Confidence:** 0.55
**Risk:** The except clause only catches `aiohttp.ClientError, asyncio.TimeoutError`. A malformed JSON response will raise uncaught (related to OBS #1). Even if caught, the retry loop does not know to retry on parse errors, which would be appropriate for transient ESPN hiccups.
**Impact:** Related to OBS #1. Uncaught exception propagates out of `_fetch`.
**Fix:** Add parse errors to except tuple and retry with backoff (transient-looking failures).

### OBSERVATION #11: `_last_request_time` initialized to 0.0 — first call delay math is racy

**Location:** `C:/Users/natew/Desktop/discord_bot/espn_odds.py:96, 132-135`
**Confidence:** 0.40
**Risk:** On first call, `elapsed = loop.time() - 0.0` is a huge positive number (event loop monotonic time), so `elapsed < _REQUEST_DELAY` is False and no sleep happens. That's correct behavior. The issue is that `loop.time()` returns monotonic seconds since loop start (typically small), so on first few seconds after startup the math could theoretically be `elapsed=0.05` if 0.0 were coincidentally the loop start — unlikely in practice.
**Impact:** None in practice.
**Fix:** Initialize to `-math.inf` or explicitly check `if self._last_request_time > 0`.

## Cross-cutting Notes

- **Dependency hygiene:** The `cachetools` dependency gap (CRIT #1) may affect OTHER modules. A grep for `from cachetools` across the codebase would tell whether this is isolated.
- **Lock-serialization pattern:** The too-wide `_request_lock` pattern (CRIT #2) is a common async pitfall — worth auditing other HTTP clients in the codebase (e.g., `sportsbook_core.py`, any Madden API client) for the same mistake.
- **Lock-time odds snapshotting (WARN #1):** This is really a real_sportsbook_cog.py concern but it originates here; any bet-settlement module should store lock-time odds in the bet row and never refetch. Worth checking `sportsbook_core.write_bet()` signature.
- **`_safe_int` float-string gap (WARN #8):** The helpers are used across the file; if other modules (`sportsbook_core.py`) re-implement similar helpers, they likely have the same bug. A project-wide audit of `_safe_int`/`_safe_float` is warranted.
- **API key management:** This file correctly has zero secrets — ESPN's unofficial API needs no auth. No violation of the "env vars only" rule.
- **`ATLAS_VERSION` bump:** Not checked — this file itself doesn't touch `bot.py`, but CLAUDE.md rule #1 applies to the repo as a whole.
