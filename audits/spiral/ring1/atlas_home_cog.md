# Adversarial Review: atlas_home_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 275
**Reviewer:** Claude (delegated subagent)
**Total findings:** 16 (3 critical, 7 warnings, 6 observations)

## Summary

The cog itself is small and the per-user `interaction_check` is solid, but three real ship-blockers live in the theme picker: `apply_btn` and `cancel_btn` both run a multi-second Playwright render *before* calling `interaction.response.defer()`, which on a slow render or congested executor blows past Discord's 3s window and the button silently fails. `apply_btn` additionally runs the `set_theme` SQLite write in an executor before defer, compounding the latency. The whole `/atlas` entry path also performs a synchronous SQLite read (`get_theme_for_render`) directly on the event loop on first use per user, and `gather_home_data` is called without a timeout on top of an `lru_cache` that stales out display data the moment a balance changes. Everything else is observation-tier (lazy imports, role-name string matching, missing error handling on the render chain), but the three defer-ordering bugs need to be flipped before this is shippable for users on slow boxes.

## Findings

### CRITICAL #1: `apply_btn` runs SQLite write + Playwright render BEFORE `defer()` — 3s timeout will fire on hot paths

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:183-199`
**Confidence:** 0.93
**Risk:** The handler runs (a) `loop.run_in_executor(None, set_theme, …)` which opens a fresh `sqlite3.connect` against `flow_economy.db` with a 10s timeout (and may block on db lock), then (b) `render_home_card_to_file(...)` which calls Playwright's `render_card` (HTML→PNG via the page pool), and only AFTER both of those calls `await interaction.response.defer()`. Per ATLAS Discord API rules (CLAUDE.md, "Modal latency / Discord API constraints"), any interaction that takes >3s before `defer()` is dropped by Discord with "interaction failed" — and rendering through the Playwright pool can easily exceed 3s if the pool is exhausted by other casino renders. Once Discord drops the interaction, the subsequent `interaction.edit_original_response(...)` raises `discord.NotFound`/`InteractionResponded` and the user sees nothing — but the theme write to `users_table` is *already committed* on disk. The user's theme silently changes, the UI never updates, the user clicks Apply again, and the only visible feedback is "interaction failed".
**Vulnerability:** Defer must always be the first awaited call on a slash/button interaction whenever any subsequent work might exceed 3s. The author got this right in `theme_btn` (line 116) but inverted the order in `apply_btn`. There is no exception handling around `edit_original_response`, so when Discord rejects the late response the exception bubbles to discord.py's interaction handler unhandled.
**Impact:** Theme silently set on disk, UI silently fails, user has no idea what happened. Worst case: user keeps clicking Apply, each click overwrites the same theme but the UI still shows the wrong active card. Subsystem looks broken on slow boxes (Windows + Playwright cold-starts especially) while actually doing exactly what it was asked to do.
**Fix:** Reorder to: (1) `await interaction.response.defer()` first, (2) then `set_theme` in executor, (3) then `render_home_card_to_file`, (4) then `edit_original_response`. Wrap the edit in `try/except (discord.NotFound, discord.HTTPException) as e: log.exception(...)` so genuine Discord-side failures are observable instead of swallowed by the framework.

### CRITICAL #2: `cancel_btn` has the same defer-after-render ordering bug

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:201-212`
**Confidence:** 0.92
**Risk:** Identical structure to CRITICAL #1: `render_home_card_to_file` runs before `interaction.response.defer()`. Cancel restores the original theme card, which means re-rendering the full home card through Playwright; if the page pool is busy this exceeds 3s and the cancel silently fails. The user thinks they've cancelled out of the theme picker, but the picker view stays live in Discord (because `edit_original_response` failed) and the next click on Prev/Next might race against the now-orphaned interaction state.
**Vulnerability:** Same root cause as #1 — defer is the *last* await before edit, not the first. There's no try/except around the edit, so framework eats the failure.
**Impact:** Cancel button visibly broken on slow renders. User left staring at the theme preview view with no escape; their only recovery is to close the message and re-run `/atlas`.
**Fix:** Same pattern as #1 — defer first, then render, then edit, with explicit try/except around the edit.

### CRITICAL #3: `gather_home_data` has no timeout — a single locked SQLite query freezes the entire `/atlas` command

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:250-252`
**Confidence:** 0.86
**Risk:** `loop.run_in_executor(None, gather_home_data, user.id)` is a fire-and-await with no `asyncio.wait_for` wrapper. `gather_home_data` (in `atlas_home_renderer.py:22-216`) opens a single `sqlite3.connect` with `_DB_TIMEOUT = 10`, then runs *eight* separate queries serially against `flow_economy.db` (users_table, transactions, bets_table, real_bets, casino_sessions, prediction_contracts, parlays_table, plus a sub-query). Each one can block up to 10s on a write lock from `flow_wallet.credit/debit` happening concurrently in another cog. Worst case: a user runs `/atlas` while a stipend run is mid-batch holding write locks; the first query hits the 10s timeout, the second restarts the connection wait, etc. The user's interaction was already `defer(ephemeral=True)`'d at line 232, so they get the "Bot is thinking…" indicator forever and discord.py eventually times out the followup. Meanwhile this user has occupied an executor worker and is starving the default ThreadPool.
**Vulnerability:** No cap on total query latency, no per-query timeout coordination, no fallback path if `gather_home_data` raises (the inner `try: con = sqlite3.connect... except Exception: pass` at the bottom of the renderer will swallow the OperationalError after the timeout fires, returning a default-zeros dict — so the user gets a card showing all zeros rather than an error). The default ThreadPoolExecutor has only `min(32, os.cpu_count() + 4)` workers; on a 4-core box that's 8. Eight users running `/atlas` simultaneously while a stipend runs can lock the entire bot's executor pool.
**Impact:** During a heavy Flow event (stipend payouts, parlay settlement), `/atlas` either freezes for 10–80s or returns a zeros card with no error. Other cogs that share the executor (HTML renders, NL→SQL Codex queries) starve in parallel.
**Fix:** Wrap `gather_home_data` call in `asyncio.wait_for(..., timeout=8.0)`. On `TimeoutError`, send the user an ephemeral "Stats temporarily unavailable, try again in a few seconds" instead of a card full of zeros. Long-term: convert `gather_home_data` to `aiosqlite` with per-query timeout, or batch the queries into a single read transaction.

### WARNING #1: `get_theme_for_render` is a synchronous SQLite read called from the async event loop

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:244-248`
**Confidence:** 0.91
**Risk:** Lines 244-248 do `from flow_wallet import get_theme_for_render; theme_id = get_theme_for_render(user.id)` directly inside the async `atlas_home` handler — no `run_in_executor`, no `to_thread`. `get_theme_for_render` is decorated with `@functools.lru_cache(maxsize=128)` (flow_wallet.py:120), so on the *first* call per user_id it falls through to `get_theme(user_id)` which calls `_ensure_theme_column()` (a `PRAGMA table_info` + possible `ALTER TABLE`) and a `SELECT card_theme` against `users_table` — all blocking sqlite3 connections on the event loop thread. ATLAS rule (CLAUDE.md, "Async / Concurrency"): *Blocking calls inside `async` functions (sqlite3, requests, time.sleep). All blocking I/O must go through `asyncio.to_thread()` or use async libs.*
**Vulnerability:** lru_cache only helps after the first call. The very first time the bot runs after a restart (cache empty), the first user to type `/atlas` blocks the event loop for the duration of their SQLite read — and if the column migration hasn't run yet, that includes the `ALTER TABLE`. After that, every user_id that wasn't in the 128-slot cache also blocks. There is also a TOCTOU here: `set_theme` (line 117 of flow_wallet.py) calls `get_theme_for_render.cache_clear()`, which means after any theme change *every* cached user is evicted and the next read for any user blocks again.
**Impact:** Tail-latency spikes on `/atlas`. After every theme apply, the cache is cleared, so the *next* `/atlas` call from any user pays the SQLite cost on the event loop thread. In an active server this is constant.
**Fix:** Move the call to an executor: `theme_id = await loop.run_in_executor(None, get_theme_for_render, user.id)`. Or better: collapse the theme lookup into the same `gather_home_data` executor call so it's batched into the existing thread hop.

### WARNING #2: `apply_btn` reuses stale `self.data` after theme change — display info goes out of date

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:184-199`
**Confidence:** 0.78
**Risk:** Line 190 constructs `HomeView(self.user_id, self.data, new_theme)` and line 191 calls `render_home_card_to_file(self.data, theme_id=new_theme, ...)` — it re-uses the `data` dict captured at the time of the original `/atlas` invocation. Between the initial `/atlas` and the user clicking Apply (up to 300s of HomeView timeout, +120s of ThemeCycleView), the user may have placed bets, won/lost casino sessions, received stipends, etc. None of that is reflected — they see the theme they chose but the *old* balance, *old* rank, *old* P&L. The user reasonably interprets the post-Apply card as "current state with new theme" and now thinks their balance is wrong.
**Vulnerability:** The `data` dict is captured once at command time (line 252) and threaded through the views by reference. There is no refresh hook on Apply. The card label proudly says "Theme set to **X**" but the body data is whatever was true 7 minutes ago.
**Impact:** User-facing data inconsistency that looks like a balance bug. Particularly bad if user just won a casino session, opens `/atlas` to check their new balance, gets distracted by themes, applies one, and now sees the *old* (lower) balance again.
**Fix:** In `apply_btn`, re-fetch fresh data: `data = await loop.run_in_executor(None, gather_home_data, self.user_id)` before constructing the new HomeView and rendering the card.

### WARNING #3: `_send_module_info` will raise `KeyError` if any future button passes an unknown key

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:215-218`
**Confidence:** 0.65
**Risk:** `title, desc = _MODULE_INFO[key]` — direct dict access. All current callers (lines 83, 87, 91, 95, 99, 103) pass literal keys that are present in `_MODULE_INFO`, so today this is safe. But the moment someone adds a 7th button without adding the corresponding entry to `_MODULE_INFO`, the user click raises an unhandled `KeyError` that surfaces as "interaction failed" with no log line beyond the discord.py framework's generic exception handler. There is also no `try/except` around `interaction.response.send_message` — if the embed exceeds Discord's character limits (extremely unlikely with these short strings, but still), the failure is silent.
**Vulnerability:** No defensive `_MODULE_INFO.get(key, default_tuple)`. The function signature accepts any string but only six keys are valid. Type hints (`key: str`) don't help here.
**Impact:** Future regression risk only — current code is fine. But the failure mode is opaque.
**Fix:** `title, desc = _MODULE_INFO.get(key, (f"Module: {key}", "No info available."))`. Or assert keys at module load: `assert set(_MODULE_INFO.keys()) >= {"oracle", "genesis", ...}`.

### WARNING #4: Theme picker has no concurrency lock — rapid Prev/Next clicks can race renders

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:159-181`
**Confidence:** 0.72
**Risk:** `_re_render` does `defer() → render → edit_original_response`. The render uses the Playwright page pool (4 pages) which is async. If a user mashes Prev/Next 3 times in 200ms, three independent `_re_render` coroutines are scheduled in parallel. They each compute `self.current_idx` from the *current* mutated value — but the mutation happens synchronously in each handler before defer (lines 175, 180), so the race is on the render order. Whichever Playwright page returns first calls `edit_original_response` first, then the next, then the next. The user can end up looking at a theme that doesn't match `self.current_idx` because the *last* edit was actually the *first-clicked* render that happened to finish slowest.
**Vulnerability:** No `asyncio.Lock` on the view, no in-flight render counter, no debounce. Discord's button cooldown doesn't apply here because each click is a separate interaction.
**Impact:** Theme picker visibly desyncs from the index display. User clicks Prev → sees theme N+1, clicks Prev → sees theme N (correct), but the underlying state flip-flops because two renders fought.
**Fix:** Add `self._render_lock = asyncio.Lock()` to `ThemeCycleView.__init__` and wrap the body of `_re_render` in `async with self._render_lock`. Alternatively, ignore subsequent clicks while a render is in flight using a simple `self._rendering = False` flag.

### WARNING #5: No error handling around `render_home_card_to_file` in the entry path

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:265-267`
**Confidence:** 0.74
**Risk:** Line 265 calls `render_home_card_to_file(...)` with no try/except. If Playwright crashes (browser binary missing on a fresh deploy, page pool exhausted, OOM during render), the exception propagates to `atlas_home`, the followup is never sent, and the user is left with the "Bot is thinking…" indicator forever — eventually discord.py logs an "Unhandled exception in command 'atlas'" and the user sees a generic error. There is no fallback "couldn't render your card right now" message.
**Vulnerability:** The whole rendering chain is treated as infallible. Playwright failures are very real on Windows (the deploy target) — page pool deadlocks, pdb-attached debugger holds the page, memory pressure on long-running processes.
**Impact:** During a Playwright incident, every `/atlas` user gets a generic interaction failure with no signal about what went wrong. Operators have to grep the log to find out that rendering is the problem.
**Fix:** Wrap lines 265-267 in `try: ... except Exception as e: log.exception("home card render failed for user %s", user.id); await interaction.followup.send("Couldn't render your stats card right now — try again in a moment.", ephemeral=True)`.

### WARNING #6: `set_theme` silently auto-creates a wallet for users who only wanted to pick a theme

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:184-188` (transitively via flow_wallet.py:98-115)
**Confidence:** 0.7
**Risk:** When a brand-new user (never bet, never received a stipend, no `users_table` row) opens `/atlas` and clicks Apply on a theme, `set_theme` falls into the `else` branch at flow_wallet.py:111-115 and inserts a fresh row with `STARTING_BALANCE = 1000` and `season_start_balance = 1000`. The theme picker is the *only* place outside Flow itself that creates a wallet, and it does so as a side effect of a UI choice that the user thinks is purely cosmetic. There is no explicit "you'll be enrolled in the economy" disclosure. Worse: this row will start showing up in rank queries (`SELECT … ORDER BY balance DESC`) immediately, polluting the leaderboard with ghost users who never wanted to play the economy.
**Vulnerability:** `set_theme` does an UPSERT-with-side-effect; the cog doesn't gate the Apply button on "is this user already in the economy". The behavior is consistent with the flow_wallet design (every operation creates a user as needed) but the *intent* of the theme picker is purely cosmetic.
**Impact:** Ghost wallets in the leaderboard, every user who has ever clicked Apply now occupies a row in `users_table` even if they never engage with Flow. Inflates `total_users` rank denominator on every other user's `/atlas` card.
**Fix:** Either (a) split theme storage into a `user_themes` table that doesn't touch `users_table`, or (b) only allow Apply if the user already has a `users_table` row (gate the button), or (c) change `set_theme`'s INSERT branch to omit balance fields and rely on lazy creation in the actual wallet code.

### WARNING #7: `theme_btn` re-uses card session via `edit_original_response` but original is now ephemeral followup, not the original interaction response

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:105-127`
**Confidence:** 0.6
**Risk:** The `/atlas` slash command does `defer(ephemeral=True)` then `interaction.followup.send(file=disc_file, view=view, ephemeral=True)`. The view attaches to the *followup* message, not the original response. When `theme_btn` then calls `interaction.edit_original_response(...)`, that "original response" is the deferred response from the original `/atlas` command — *not* the followup that holds the card. Discord's API does forward `edit_original_response` from a button interaction to the message that contains the button, so this works in practice — but it relies on undocumented behavior. discord.py 2.3 routes button interactions' `edit_original_response` to the message that contains the button, which is what's wanted, so it works. The fragility is that any version skew or framework patch that changes that routing breaks the theme picker silently.
**Vulnerability:** Implicit dependency on discord.py's button-interaction → containing-message routing. No tests, no version pin documented.
**Impact:** Latent compatibility risk on discord.py upgrades. If routing ever changes, theme picker breaks silently.
**Fix:** Use `interaction.message.edit(...)` (the explicit form) instead of `interaction.edit_original_response(...)`. Same effect, no implicit routing assumption.

### OBSERVATION #1: Lazy `from flow_wallet import …` inside hot paths

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:186, 245`
**Confidence:** 0.75
**Risk:** Two of the cog's interaction handlers do `from flow_wallet import set_theme` (line 186) and `from flow_wallet import get_theme_for_render` (line 245) inside the function body. Python caches the import in `sys.modules`, so the per-call overhead is small — but the pattern is brittle (any future syntax error in `flow_wallet.py` only surfaces on first interaction, not at cog load) and signals a circular-import smell that should be solved at the module top.
**Vulnerability:** Late binding hides dependency failures. An import that should fail at startup gets postponed to first user click.
**Impact:** Stylistic / observability concern only. No runtime bug today.
**Fix:** Move both imports to the top of the file. If circular, restructure: extract `get_theme_for_render`/`set_theme` into a small `flow_themes.py` module that neither flow_wallet nor atlas_home depends on each other.

### OBSERVATION #2: `role_badge` resolution by string match — fragile and order-dependent

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:236-241`
**Confidence:** 0.6
**Risk:** Role badges are matched against literal role names `"GOD"`, `"Commissioner"`, `"TSL Owner"`. If the commissioner renames the role (e.g., to "TSL Commish") for any reason — promotion to a new tier, fork to a sister league, vanity rebrand — the badge silently disappears. There is no fallback to role IDs, no env-var override, no integration with `permissions.is_commissioner()`.
**Vulnerability:** String-matched authorization signals diverge from the actual permission system. The displayed badge can be wrong relative to actual authority.
**Impact:** Cosmetic mostly, but during a server-config change the badge can lie about who's actually a commissioner. Players see the wrong badge on a card and complain.
**Fix:** Replace the string loop with calls into `permissions.is_admin(user)`, `permissions.is_commissioner(user)`, `permissions.is_tsl_owner(user)`. These already encapsulate the role-name + ID + env-var + admin checks.

### OBSERVATION #3: HomeView timeout 300s, ThemeCycleView timeout 120s — child outlives parent only barely

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:68, 141`
**Confidence:** 0.55
**Risk:** HomeView times out at 300s (5 min). ThemeCycleView times out at 120s (2 min). A user who opens `/atlas`, waits 4 minutes, then clicks Theme will spawn a ThemeCycleView. After 2 more minutes the HomeView's timeout (now at 6 min from start) has fired and the original buttons are gone — but the ThemeCycleView is still alive. Apply/Cancel both call `HomeView(self.user_id, self.data, ...)` with the original (stale) `data` dict, restoring buttons that point at long-disabled state. Not a crash, but jarring.
**Vulnerability:** Independent timeouts with no parent/child coordination. Stale data dict carried across timeouts compounds the issue from WARNING #2.
**Impact:** UX wonk on long-idle sessions. Not a functional bug.
**Fix:** Either reset the HomeView timeout when ThemeCycleView is opened, or make ThemeCycleView re-fetch fresh data on Apply/Cancel.

### OBSERVATION #4: `_MODULE_INFO` lives at module scope with hard-coded slash command names

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:29-59`
**Confidence:** 0.5
**Risk:** Each module info embed says e.g. `"Use \`/oracle\` to open the hub."` These literals do not auto-update if a slash command is renamed (CLAUDE.md notes that two cogs with the same slash command name → second silently fails, so renames do happen). There is no test that asserts these command names actually exist on the bot.
**Vulnerability:** Documentation drift between embed text and actual command tree.
**Impact:** A user might be told to run `/oracle` when the command was renamed to `/stats` six months ago. Frustrating but recoverable.
**Fix:** Build the embed text from `bot.tree.get_command(...)` so renames flow through automatically; or add a startup assertion that all referenced commands are registered.

### OBSERVATION #5: `setup` swallows all exceptions with `print` instead of `log.exception`

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:270-275`
**Confidence:** 0.7
**Risk:** Cog load failures are caught by `except Exception as e: print(f"ATLAS: Home · FAILED to load ({e})")`. `print` bypasses the logging configuration, the message has no traceback, and the bot continues startup as if nothing failed — operators only see the print line if they're watching stdout. ATLAS rule (CLAUDE.md, "Flow Economy Gotchas") explicitly bans silent excepts in admin-facing code; this is the cog *load* path, which is even more critical than admin views.
**Vulnerability:** Lost stack traces on cog load failure. Bot starts with `/atlas` missing and no clear signal in the log.
**Impact:** Silent feature degradation. After a refactor, `/atlas` quietly disappears from the command tree and operators don't notice.
**Fix:** Replace with `log.exception("Failed to load AtlasHomeCog")`. Optionally re-raise so the bot's own setup_hook surfaces the error.

### OBSERVATION #6: `data` dict is treated as both schema and message bus — no type guard

**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_home_cog.py:252-265`
**Confidence:** 0.45
**Risk:** `gather_home_data` returns a `dict` with ~25 keys, then the cog mutates it post-hoc with `display_name`, `role_badge`, `theme_name`, `season`. The renderer reads all of these by string key with no contract. If any caller forgets to set a key (e.g., a future cog that builds a card outside the `/atlas` path), the renderer raises `KeyError` deep inside the HTML builder. There is no `TypedDict` or dataclass to enforce shape.
**Vulnerability:** Silent shape drift. The renderer's defaults at lines 28-49 of atlas_home_renderer.py protect the *DB-failure* path, but not the *cog-forgot-to-overlay* path.
**Impact:** Future regressions in shared renderer code surface as opaque KeyError stack traces.
**Fix:** Define `class HomeData(TypedDict)` with all fields, type the renderer signature against it, and let mypy / runtime keys catch missing fields at the boundary.

## Cross-cutting Notes

The defer-after-render pattern in CRITICAL #1 and #2 is exactly the kind of bug that recurs across any cog using the Playwright pipeline (`flow_cards.py`, `sportsbook_cards.py`, `casino` renderers, `prediction_html_renderer.py`). Recommend a single audit pass on every `discord.ui.Button` callback in the rendering cogs to confirm `interaction.response.defer()` is the *first* awaited call. The blocking sync-SQLite-on-event-loop pattern (WARNING #1) likely repeats wherever `flow_wallet.get_theme_for_render` is called from cog code without a thread hop — worth a targeted grep across cogs. The "stale data dict re-used across views" pattern in WARNING #2 is also worth checking in `flow_cards.py` and `sportsbook_cards.py`, both of which thread data through hub views with similar lifetimes.
