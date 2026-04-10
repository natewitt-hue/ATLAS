# Adversarial Review: atlas_html_engine.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 740
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (3 critical, 7 warnings, 7 observations)

## Summary

This is the single-point-of-failure render pipeline for every card in ATLAS, and it has several real correctness/reliability bugs. The most dangerous are (1) a silent pool shrinkage path where failed releases drop the page without replacement, (2) unbounded `_render_counts` dict growth keyed by `id(page)` (guaranteed leak across recycled objects), and (3) a theme import inside `wrap_card` that bypasses HTML escaping of a CSS variable payload (`status_gradient`/`card_border` are injected raw into inline styles). Several other concurrency and lifecycle gaps warrant fixes before this is considered hardened.

## Findings

### CRITICAL #1: Pool silently shrinks to zero when `_new_page()` fails in `release()`

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:653-673`
**Confidence:** 0.92
**Risk:** If a `release()` path hits any exception — dead page, recycle hit, browser disconnect mid-release — and then the replacement `_new_page()` also fails, the code logs an error and `return`s without putting anything back on the queue. Each failure reduces the pool by 1. Once the pool hits zero live pages, every subsequent `acquire()` call blocks for 10 s and then raises `TimeoutError`. No self-healing, no re-warm. The bot stops rendering cards entirely and there is no bounded recovery path.
**Vulnerability:** Line 672 comment admits this: `# page is lost — pool shrinks by 1`. There is no background task to re-warm the pool. `_get_browser()` only re-warms on a full browser disconnect (line 55-56), not on partial pool drain. Under Windows, Playwright browser instability is a frequent reality, and the release path is the hottest path after every render — one transient failure per hour is enough to kill the engine within a day.
**Impact:** Total rendering outage once all 4 pages are lost. Every casino/flow/hub/ledger card produces `RuntimeError: Render pool exhausted`. Users see failed commands across the entire bot. Recovery requires bot restart.
**Fix:** Either (a) detect `self._available.qsize() < self._size` and schedule a background `_new_page()` retry with backoff, or (b) in the `except Exception` on line 665, force a full `close_browser()` + reconnect + `warm()` cycle. At minimum, increment a `_lost_pages` counter and log a loud warning when `qsize < size/2`.

### CRITICAL #2: `_render_counts` leaks memory unboundedly via `id(page)` keys

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:614,660-663`
**Confidence:** 0.88
**Risk:** `_render_counts: dict[int, int]` is keyed by `id(page)`. Every time a page is recycled (hit 100 renders), the current `id` is `del`'d on line 662, BUT the more common path — a page that's replaced because `is_closed()` was True, or because `set_viewport_size` raised, or because `_new_page()` was called in `acquire()`'s health check loop (line 650) — never deletes the old `id` from `_render_counts`. The closed page object gets garbage collected, Python reuses the same `id()` integer for a fresh object, and `_render_counts.get(pid, 0) + 1` silently inherits the old count. Worst case: a fresh page gets a stale count ≥100 and is immediately recycled on first release, thrashing the browser.
**Vulnerability:** `id()` is not a stable key across the object lifetime of the pool. Python reuses `id` values for freed objects. The dictionary is also never bounded. Over a 24-hour uptime with recycling churn, the dict will accumulate entries for every page object ever created, since the only delete path is the 100-render recycle branch (line 662).
**Impact:** (a) Slow memory leak proportional to pool churn. (b) Nondeterministic early recycling of fresh pages when `id()` collisions occur, causing browser thrash and elevated CPU. (c) Under sustained load, unpredictable render latency spikes as pages recycle prematurely.
**Fix:** Track render count as an attribute on the page object itself — e.g., a lightweight wrapper class `PooledPage(page, count=0)` — or use a `weakref.WeakKeyDictionary`. Never use `id()` as a persistent dictionary key for objects whose lifetimes overlap.

### CRITICAL #3: Theme `status_gradient` and `card_border` values injected raw into inline `style=` without escaping

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:568-577`
**Confidence:** 0.78
**Risk:** When `theme_id` is supplied, `wrap_card()` calls `theme.get("status_gradient")` (line 569) and `theme.get("card_border")` (line 574) and splices the raw value directly into `status_bar_attr` and `card_border_attr` without calling `esc()` or quoting. These become HTML attribute values like `style="background:{sg};height:5px;..."`. If a theme entry ever contains a double-quote character (either by bug, future user-theming, or data corruption), the attribute closes early and arbitrary HTML/CSS runs in the render context. Even if the `themes.py` dict is currently hand-curated, it's a trust boundary the render engine should not rely on — the layer below (Playwright) will happily execute whatever JS/CSS lands in the DOM.
**Vulnerability:** Playwright runs real Chromium, not a sanitized HTML parser. A malformed theme string like `linear-gradient(90deg,red); " onload="alert(1)` (or more subtly, a `</style><script>…`) will break out of the style attribute. The engine's own `esc()` helper (line 224) exists specifically to prevent this pattern, but it is bypassed on these two code paths. Additionally, `theme_css` is built from raw `k`/`v` (line 560) — if a theme var name contains `}` or `*/`, the `:root { }` block can be escaped out.
**Impact:** At minimum, render corruption / broken cards when a theme author (or any future mutator of `atlas_themes.py`) uses special characters. At worst, if themes ever become user-supplied (e.g. `/atlas theme upload`), full stored-XSS-equivalent in Playwright — DOM manipulation, network requests from the render process, fingerprinting. Since this engine is shared across every renderer in the bot, compromising it compromises every card.
**Fix:** Either (a) validate theme strings with a strict allowlist regex at theme-load time, (b) escape any string that enters HTML attribute context with `esc()`, or (c) build these values via a CSS injection layer that uses Playwright's `page.add_style_tag()` API with properly sanitised vars. Also apply to the `theme_css` block-building at line 560.

### WARNING #1: `_browser_lock` does not protect `_pool`; browser reconnect races pool mutation

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:39-57,675-679`
**Confidence:** 0.82
**Risk:** `_get_browser()` holds `_browser_lock` while draining+warming `_pool`. But `_pool.drain()` (line 675) and `_pool.warm()` (line 636) are the only protected sections. Concurrent callers of `render_card()` may call `_pool.acquire()` at the exact moment `_get_browser()` is inside its drain/warm block. `drain()` pops from `self._available` queue while a concurrent `acquire()` is also awaiting `self._available.get()`. This is actually safe on asyncio queue semantics, but `warm()` then calls `_new_page()` which re-enters `_get_browser()` — reentrant acquisition on an already-held `asyncio.Lock` deadlocks immediately (asyncio locks are NOT reentrant, unlike `threading.RLock`).
**Vulnerability:** Trace: `render_card` → `_pool.acquire` → timeout → retry path non-existent; OR `_get_browser` acquires lock → detects disconnect → calls `_pool.warm()` → `_new_page()` → `_get_browser()` → `async with _browser_lock:` → **DEADLOCK** (same task trying to re-enter non-reentrant lock). This path is live: line 56 explicitly calls `await _pool.warm()` while holding `_browser_lock`, and `warm()` calls `_new_page()` which calls `_get_browser()`.
**Impact:** Under a browser crash + reconnect scenario, the render engine hangs permanently on the reconnect path. Every subsequent render call blocks on the lock forever. Bot appears alive but all card renders produce no response.
**Fix:** Inside `_get_browser()`, after re-launching the browser, drop the lock before calling `_pool.warm()`, or rewrite `warm()` to accept the already-fetched browser handle and skip the `_get_browser()` call. Alternatively, use a `asyncio.Lock()` flag pattern where reentrance is detected and skipped.

### WARNING #2: `render_card()` cannot retry on transient Playwright errors; one bad HTML kills the pool slot

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:688-721`
**Confidence:** 0.85
**Risk:** The try/finally wraps the entire render. If `page.set_content()` raises (timeout, malformed HTML, browser crash), the exception bubbles up and `release()` is called in `finally`. But `release()` catches every exception including `RuntimeError("recycling")` and `page.is_closed()`, then tries `_new_page()`. If the page is merely in a bad state (e.g. a navigation is still pending), `release()` still executes the viewport reset against the sick page and counts it as a successful render. No distinction is made between "render failed, page may be tainted" and "render succeeded, page is healthy."
**Vulnerability:** `set_content()` with a 10 s timeout can leave a page in a half-loaded state. Returning that page to the pool (line 673) hands the taint to the next caller, who will likely fail too. No per-render health marker.
**Impact:** A single malformed HTML payload (e.g. a test theme, a malformed emoji, an unterminated tag from a bug upstream) can spread taint across multiple subsequent renders. Users see cascade failures.
**Fix:** In `render_card()`, catch Playwright exceptions explicitly, close the page inside the finally, and let `release()` treat that path as forced-recycle. Or add a `page._atlas_tainted` flag set on exception and checked in `release()`.

### WARNING #3: `acquire()` 10-second timeout converts transient contention into user-facing failure

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:642-651,688-697`
**Confidence:** 0.80
**Risk:** Pool size is 4. Each render takes ~500 ms – 2 s on Windows Playwright. If 5+ concurrent casino commands land in the same ~2 s window (normal burst during an active game hour), the 5th caller waits. Under sustained load (say 10 commands over 5 s), the tail caller waits up to the full 10 s timeout and then gets `RuntimeError: Render pool exhausted`. This is not a pool exhaustion — it's just queue backlog. The error message is misleading and the user sees a command failure for what is really just expected load.
**Vulnerability:** No queue depth telemetry. No backpressure signaling to the caller. The 10 s timeout is hardcoded and not tunable per-caller.
**Impact:** During peak hours, random casino/flow commands fail with scary-looking "exhausted" errors while the engine is healthy. Users retry, compounding the backlog.
**Fix:** (a) Increase default timeout to 30 s for non-interactive paths; (b) accept a `timeout` kwarg on `render_card()` so interactive paths can opt-in to shorter waits and degrade gracefully; (c) log queue depth on every acquire.

### WARNING #4: `close_browser()` does not clear `_pool` state; post-close calls leave stale pages

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:60-71,727-740`
**Confidence:** 0.85
**Risk:** `close_browser()` resets `_browser`, `_pw_context_manager`, `_pw_instance` to `None`, but leaves `_pool` alone. The pool still holds references to the now-dead pages from the closed browser. On the next `_pool.acquire()`, the health check at line 647 detects `is_closed()` and tries to create a replacement via `_new_page()` — which calls `_get_browser()` — which starts a new browser. Works, but the pool loop at line 646 only runs `self._size` iterations. If every page is dead (likely after `close_browser()`), and any `_new_page()` in that loop fails, the pool is starved and raises. More importantly: if `render_card()` is called during the window between `close_browser()` and `init_pool()` rerun, `_pool` is non-None but holds dead pages.
**Vulnerability:** `close_browser()` does not call `_pool.drain()`. `drain_pool()` does (line 738), but `close_browser()` is also called from `_get_browser()` on page-creation failure (line 629), bypassing the drain step. The pool gets stranded with dead pages.
**Impact:** After a browser crash + reconnect cycle, the first few renders thrash through health checks, doing N serial `_new_page()` calls instead of drawing from the pool. Latency spike and possibly cascade failures.
**Fix:** In `close_browser()`, also call `_pool.drain()` if `_pool is not None`. Or have `_get_browser()`'s reconnect path drain before re-warming (it does — line 46 — but `close_browser()` itself is also a public API).

### WARNING #5: `_font_face_css()` is called on EVERY `wrap_card()` invocation with no memoization

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:96-118,584`
**Confidence:** 0.90
**Risk:** Each card render re-generates the font-face CSS string by iterating the 7-font list and concatenating base64 blobs that can be hundreds of KB each. The font files are cached at line 83 (good), but the CSS string assembly happens every single render. That's ~1-2 MB of string concatenation per card, multiplied by every casino/hub/flow card rendered.
**Vulnerability:** Pure string concatenation, O(n·m) where n=7 fonts and m=total b64 size. At 500 renders/hour (active server), that's ~500 MB of transient string allocation per hour just to build the CSS header.
**Impact:** Memory pressure, GC pauses, slower-than-necessary renders. Not a bug, but a real performance leak in a hot path.
**Fix:** Cache the result. After the first call, cache `_font_face_css._cached` (or a module-level `_FONT_CSS_CACHE`) and return the cached string. Fonts are static — the CSS never changes after startup.

### WARNING #6: `init_pool()` not idempotent — double-call leaks old pool

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:727-731`
**Confidence:** 0.95
**Risk:** If `init_pool()` is called twice (e.g. bot reconnect without a clean shutdown, or a cog accidentally re-initializes it), the old `_pool` is overwritten without being drained. The pages in the old pool are orphaned — they remain attached to the browser but no longer reachable for cleanup. Classic pool leak.
**Vulnerability:** No guard like `if _pool is not None: return` or `if _pool is not None: await _pool.drain()`. CLAUDE.md explicitly calls out "resource leaks: Playwright pages not returned to the pool" as a known concern.
**Impact:** On bot reconnect storm, every reconnect leaks 4 pages. Over a day of instability, this pegs Chromium memory.
**Fix:** Guard at top of `init_pool()`:
```python
if _pool is not None:
    await _pool.drain()
```
Or make it explicitly idempotent with a `if _pool is not None and _pool._available.qsize() == _pool._size: return` short-circuit.

### WARNING #7: `render_card()` viewport set_size race between pages sharing the pool

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:699-711,653-658`
**Confidence:** 0.70
**Risk:** When `width` is overridden (line 699), `render_card()` calls `set_viewport_size` on the acquired page. Then it calls `set_viewport_size` again after layout to match the card's bounding height. Then `release()` sets viewport size back to default. If a concurrent renderer happens to be using the SAME page — impossible under the pool model, but possible under browser reconnect race during which a dead-check might return a stale reference — the viewport changes conflict. More practically: the `release()` viewport reset happens AFTER the viewport has been changed twice, restoring `height=1200` but the `width` override from a previous caller persists through the default width reset.
**Vulnerability:** `release()` resets `width` to `self._width` (line 658), which is the pool default. So width override does reset. BUT: if the release path hits the except clause (line 665), the viewport-reset line was NOT executed — the page is released with a lingering custom width. A subsequent caller who doesn't override `width` gets whatever the previous caller left.
**Impact:** Occasional wrong-width renders after a viewport-override render followed by a release exception. Subtle visual corruption.
**Fix:** Move `set_viewport_size` out of the try block, or make it the first operation in `release()` before any of the counting logic.

### OBSERVATION #1: `close_browser()` swallows all exceptions in context exit path

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:65-71`
**Confidence:** 0.75
**Risk:** `try/except Exception: pass` on the `_pw_context_manager.__aexit__` call (line 68). This is admin-facing in the sense that it runs during bot shutdown — errors here become zombie Playwright processes. CLAUDE.md explicitly flags this pattern: "Silent `except Exception: pass` in admin-facing views is PROHIBITED." While this isn't a Discord admin view, it is admin lifecycle code, and failing silently here means zombie Chromium processes on Windows.
**Vulnerability:** Lines 68-69 hide any shutdown failure.
**Impact:** On Windows, Playwright Chromium processes may leak across bot restarts. Manual task kill required.
**Fix:** `log.exception("Playwright context exit failed")` instead of silent pass.

### OBSERVATION #2: `_get_browser()` swallows drain exception silently

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:44-48`
**Confidence:** 0.85
**Risk:** Same pattern as above — `try/except Exception: pass` on `_pool.drain()`. Drain failures are invisible.
**Vulnerability:** Any error closing dead pages (e.g. partial close that raises) is hidden.
**Impact:** Zombie page objects stay in memory, leak Chromium tabs.
**Fix:** `log.exception("Pool drain failed during browser reconnect")`.

### OBSERVATION #3: `game_icon_src()` and `achievement_icon_src()` have no cache bound

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:188-207`
**Confidence:** 0.88
**Risk:** `_GAME_ICON_B64_CACHE` and `_ACH_ICON_B64_CACHE` are unbounded `dict[str, str]`. `_FONT_CACHE` has a 50-entry cap (line 79), but these two do not. If the icon directory grows unbounded or names are dynamically generated from user input, these leak memory.
**Vulnerability:** No LRU eviction. Consistency gap with `_FONT_CACHE` bound.
**Impact:** Small memory leak. Real if icon names ever leak user data (they shouldn't, but).
**Fix:** Use the same `OrderedDict` pattern as `_FONT_CACHE`, or cap to a sensible number (≤200 each).

### OBSERVATION #4: `icon_pill()` skips `esc()` on `fallback` string

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:213-218`
**Confidence:** 0.70
**Risk:** When the icon file is missing, the function returns `fallback` verbatim. If a caller ever passes a user-controlled value here (currently none do — all callers pass literal emojis), it would inject HTML. The function also returns an `<img src="...">` tag built from `src` without any attribute escaping — but since `src` is a trusted data URL, this is fine today.
**Vulnerability:** Trust boundary is implicit and unenforced.
**Impact:** Latent XSS risk if a future caller passes user text as fallback.
**Fix:** Document the contract clearly or call `esc(fallback)`.

### OBSERVATION #5: `build_data_grid_html()` hardcodes currency format and int types

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:452-464`
**Confidence:** 0.65
**Risk:** The function signature is `(wager: int, payout: int, balance: int)`. CLAUDE.md flags "Float vs int balance corruption in `flow_economy.db`." If a caller ever passes a float (decimal winnings from odds math), the `f"{pl:,}"` still works but the intent becomes fuzzy. No coercion, no validation.
**Vulnerability:** Silent type drift — a `0.5` winning becomes `"$0"` after formatting instead of `"$0.50"`. Display correctness problem, not ledger corruption (the ledger is upstream), but visually misleading.
**Impact:** Cards show rounded/floor'd amounts that don't match the stored balance.
**Fix:** Explicit `int(...)` cast with logged warning if conversion loses precision, or accept float and use `:,.2f` for non-integer values.

### OBSERVATION #6: `build_jackpot_footer_html()` assumes `jackpot_info[tier]` is a dict

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:506-517`
**Confidence:** 0.70
**Risk:** Line 513 does `jackpot_info[tier].get("pool", 0)`. If `jackpot_info[tier]` is `None`, an int, or any non-dict, this raises `AttributeError`. No type check. The function wraps the whole engine for every render that passes jackpot info, so a malformed jackpot dict from a cog produces an unhandled exception in the render layer.
**Vulnerability:** No defensive isinstance check.
**Impact:** Render fails entirely (exception propagates to `render_card` caller) for any jackpot-carrying card with malformed data.
**Fix:** `if isinstance(jackpot_info.get(tier), dict):` before the `.get("pool", 0)`.

### OBSERVATION #7: `drain()` does not handle `page.close()` failures

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_html_engine.py:675-679`
**Confidence:** 0.80
**Risk:** `drain()` calls `await page.close()` with no exception handling. If a page is in a weird state (e.g. mid-navigation, disconnected from browser), close can raise. Drain halts halfway, leaving remaining pages on the queue. `drain_pool()` then won't complete, shutdown hangs or partial.
**Vulnerability:** No try/except around the close.
**Impact:** Shutdown hangs; zombie Chromium processes; next startup sees leftover pool state.
**Fix:** Wrap close in try/except with logging, continue draining.

## Cross-cutting Notes

This file is a **cross-cutting dependency** — every card renderer in the bot depends on `render_card()` and `wrap_card()`. Several findings here amplify across the ring:

1. **The `_render_counts` leak (CRITICAL #2)** means every downstream renderer inherits nondeterministic page lifetimes. Any perf audit of casino/flow/prediction renderers will see mystery latency spikes that actually originate here.

2. **The `release()` shrinkage path (CRITICAL #1)** is the most important single fix in the entire rendering subsystem. No amount of defensive code in `casino/renderer/*` protects against a 0-page pool.

3. **The theme attribute injection (CRITICAL #3)** is scoped to callers that pass `theme_id`. `atlas_home_renderer.py` and any theme-aware card path use this. If themes ever become user-uploadable (mentioned in project plans but not yet implemented), this becomes a remote code execution vector against the render process.

4. **Silent `except` blocks** at lines 47, 68, 665 should all grow `log.exception()` calls. This is the same pattern flagged across the Flow subsystem in CLAUDE.md and the pattern that hides the most bugs during incidents.

5. **Windows Playwright stability** is the unstated assumption in this file. Every health check, reconnect, recycle path must be tested under Windows Chromium SIGSEGV behavior. None of those paths are currently testable from a unit test — this file has zero testability hooks.
