# Adversarial Review: flow_live_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 796
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (3 critical, 8 warnings, 6 observations)

## Summary

`flow_live_cog.py` runs the live engagement layer (pulse dashboard, highlight broadcasts, session recaps). It works on the happy path but ships with two dead bus subscriptions that will silently swallow any future settlement event, a UX-breaking pulse-card bug that displays jackpot winners as raw discord_ids, and a thread-safety hole around `SessionTracker._active`. Several silent excepts violate the CLAUDE.md "no silent swallow" rule and at least four DB helpers leak SQLite connections on partial failure. Ship-blocking only because the dead subscriptions are easy to mistake for working wiring during incident triage.

## Findings

### CRITICAL #1: `sportsbook_result` and `prediction_result` subscriptions are dead — no live publisher

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:426-429`
**Confidence:** 0.97
**Risk:** Two of the three bus subscriptions wired in `__init__` will never fire in production. The cog *appears* to handle sportsbook and prediction events but the contract is broken end-to-end.
**Vulnerability:** A grep over the entire repository finds zero non-test publishers for `"sportsbook_result"` or `"prediction_result"`. The only live publisher is `casino/casino.py:94` which emits `"game_result"`. The actual sportsbook settlement path uses `flow_bus.emit(EVENT_FINALIZED, ...)` (`real_sportsbook_cog.py:612`, `polymarket_cog.py:2825`, etc.), and `flow_live_cog` does **not** subscribe to `EVENT_FINALIZED`. CLAUDE.md flags this exact gap as a known hazard. The code looks correct on inspection — that is the danger. An on-call engineer who sees `flow_bus.subscribe("sportsbook_result", ...)` will assume the live recap path covers parlays, when it never has.
**Impact:** No parlay highlight cards will ever be posted. No prediction-resolution cards will ever be posted. No sportsbook events feed into `SessionTracker.record_sportsbook`, so the session-recap P&L never includes book activity — the recap is silently incomplete for any user who wagers and plays casino in the same session. This is also a maintenance landmine: any engineer who later writes a publisher for `"sportsbook_result"` and ships it will see results immediately, which suggests the topic was always meant to work.
**Fix:** Either (a) wire publishers in `flow_sportsbook.settle_*`, `polymarket_cog._resolve_market`, and `real_sportsbook_cog._grade_*` that emit the documented `SportsbookEvent` / `PredictionEvent` shapes — including `discord_id` and `guild_id` per CLAUDE.md, or (b) remove the dead subscriptions and replace them with an `EVENT_FINALIZED` subscriber that joins back to `users_table` to fan out per-user highlights. Pick one and remove the dead path.

### CRITICAL #2: Pulse dashboard renders jackpot winner as raw discord_id, not display name

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:577-606`
**Confidence:** 0.93
**Risk:** Every pulse refresh after a jackpot hit displays a 17-19-digit Discord snowflake instead of a player name on the live dashboard pinned in #flow-live.
**Vulnerability:** `casino_jackpot.last_winner` is `INTEGER` (a discord_id snowflake — see `casino/casino_db.py:173`). At line 591 the code does `data["last_player"] = str(winner_row[0])`, which only stringifies the integer. There is no `guild.get_member(int(winner_row[0]))` resolution. The pulse renderer (`pulse_renderer.py:171`) escapes the value and renders it verbatim in a gold span. Result: "Last hit: 487293847293847293". This bug also exists for the prior winner across guild boundaries — the jackpot table is global but the resolution should be guild-scoped.
**Impact:** Every jackpot hit corrupts the most prominent piece of the pulse dashboard. The dashboard is the public face of the live channel, so this is highly user-visible. Compounding: there is no fallback to "anonymous" — players will see what looks like a leaked raw user ID.
**Fix:** Resolve the discord_id against the guild before assigning. Inside `_get_jackpot_data` only return the raw int, then in `_update_pulse` after the executor returns, do `member = guild.get_member(int(jp["last_winner_id"]))` and `jp["last_player"] = member.display_name if member else "Unknown"`. Also consider whether the pulse should show the *guild's* last winner, not the global one — the current `ORDER BY last_won_at DESC LIMIT 1` is global across all guilds.

### CRITICAL #3: Race condition on `SessionTracker._active` between `record` (worker thread) and `collect_expired` (event loop)

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:269-330, 521-533`
**Confidence:** 0.88
**Risk:** Concurrent rapid casino plays from the same user interleave inside a worker thread and corrupt `SessionTracker._active` and the persisted SQLite row.
**Vulnerability:** `_on_game_result` (line 521) and `_on_sportsbook_result` (line 528) both call `await asyncio.to_thread(self.sessions.record, event)`. `record()` does `self._active.get(key)` then conditionally creates and assigns `self._active[key] = session`, then mutates the session and calls `self._persist(session)` — all on a worker thread, with no lock. Meanwhile `session_reaper` runs on the event loop at line 506 and calls `collect_expired()` which mutates `self._active` (`del self._active[key]` at line 328) and runs `_delete_persisted` synchronously. There is no `asyncio.Lock` or `threading.Lock` protecting these paths. Two concurrent button-click retries from the same user, or a `record()` racing the reaper at exactly the idle-timeout boundary, can cause: (a) two PlayerSession instances created for the same key (last write wins, earlier events lost), (b) `_persist()` writing a row immediately after the reaper deleted it (resurrected zombie session), or (c) a torn `to_dict()` read mid-mutation (since `to_dict` iterates `self.events` which `record` is appending to).
**Impact:** Lost events in session recaps; recaps emitted for the wrong session; zombie sessions in flow_live_sessions that never get reaped because they were re-created after deletion; torn JSON in the persisted row that fails on next `load_persisted`. Hard to diagnose because failures are race-dependent.
**Fix:** Wrap `SessionTracker._active` mutations behind a single `threading.RLock` (because both event-loop and worker-thread paths touch it) or convert all mutations to event-loop-only and remove the `asyncio.to_thread` wrapper. Recommendation: keep state mutation on the event loop (it's cheap), and only push the SQLite `_persist` call into `to_thread`. That eliminates the race entirely and matches the rest of the codebase.

### WARNING #1: `_get_flow_live_channel` swallows all exceptions silently

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:756-765`
**Confidence:** 0.95
**Risk:** Any failure in channel resolution (ImportError on setup_cog, OperationalError on the channels table, attribute errors on the bot cache) is invisible. The pulse loop, recap poster, and instant-highlight poster all call this and silently no-op when it fails.
**Vulnerability:** `except Exception: pass` is the explicit anti-pattern called out in CLAUDE.md ("Silent except in admin-facing views is prohibited"). While this is not strictly an admin view, the symptom is identical: when #flow-live stops working, no log line tells you why. The only signal is the absence of pulse updates.
**Impact:** Triage time during outages. Misconfigured channel mapping looks identical to "everything works, just no events". The bot will never emit a single warning saying "I cannot find the flow_live channel for guild X".
**Fix:** Replace with `log.exception("Failed to resolve #flow-live channel for guild %s", guild_id)` and keep the `return None`. If you want to stop log spam at startup before the channel table exists, gate the log on `ImportError` vs other exceptions.

### WARNING #2: `_persist`, `_delete_persisted`, `_load_pulse_message_ids`, `_save_pulse_message_id`, and `_ensure_state_table` all leak SQLite connections on mid-flight failure

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:169-197, 199-229, 231-242, 244-267, 449-489`
**Confidence:** 0.9
**Risk:** Any exception raised between `sqlite3.connect()` and the explicit `conn.close()` leaves the connection open. Repeated leaks accumulate file descriptors and can hit the SQLite max-connections limit on heavy load.
**Vulnerability:** Only `_persist` (line 199) initializes `conn = None` and uses a `finally` block to close. The other five helpers either have no `finally`, or initialize `conn` inside the try (`_ensure_sessions_table` at line 172, `_ensure_state_table` at line 453, `_load_pulse_message_ids` at line 469, `_save_pulse_message_id` at line 480, `_delete_persisted` at line 234, `load_persisted` at line 248). If `conn.execute(...)` or `cursor.fetchall()` raises after a successful `connect()`, the connection is dropped without closing. Same for `_get_jackpot_data` at line 582 — `conn.close()` is on the happy path only; an exception inside the function jumps to the except block and drops `conn` on the floor.
**Impact:** Slow leak that accumulates over many days of uptime. SQLite WAL mode tolerates many open connections but each one holds a file descriptor and writer lock pressure. On guilds with many events per minute, each `_persist` failure leaks one fd.
**Fix:** Standardize on `with sqlite3.connect(DB_PATH, timeout=10) as conn:` (which auto-closes), or explicit `conn = None; try: conn = ...; finally: if conn: conn.close()`. Apply uniformly to every helper that opens a connection.

### WARNING #3: Blocking SQLite calls in `cog_load` and `__init__` paths

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:431-437, 169-267, 449-475`
**Confidence:** 0.9
**Risk:** `cog_load` is `async`, but it calls `self._load_pulse_message_ids()` (synchronous SQLite) and `self.sessions.load_persisted()` (synchronous SQLite, including a CREATE TABLE) directly on the event loop. During cog load on a hot start, this can block the loop for hundreds of milliseconds — long enough to delay heartbeats and trigger spurious reconnect events under DB lock contention.
**Vulnerability:** Both methods open a connection, run a query, and serialize JSON for every persisted session row. If the DB is already busy (another cog persisting), the 10-second `timeout` argument means `cog_load` can block for **up to 10 seconds** synchronously, freezing the entire bot.
**Impact:** Discord will close the gateway connection if the heartbeat ack is missed for ~40s; under heavy DB lock contention this is plausible. Even at 1-2s blocking, message handlers queue up and feel laggy.
**Fix:** Wrap both calls in `await asyncio.to_thread(...)` from `cog_load`. Same for `_load_pulse_message_ids` if invoked anywhere on the event loop.

### WARNING #4: Pulse-card edit failure path silently re-creates and re-pins the message every cycle

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:662-676`
**Confidence:** 0.85
**Risk:** When `channel.fetch_message(msg_id)` raises `discord.HTTPException` for transient reasons (5xx, rate-limit), the bare `pass` falls through and the cog creates a brand-new message and pins it. The next cycle, the OLD message ID is overwritten in `_pulse_message_ids` and the previous pulse becomes orphaned (still pinned, never updated, gradually stale). Over many transient failures, the channel accumulates pinned ghost pulses.
**Vulnerability:** Line 668: `except (discord.NotFound, discord.HTTPException): pass`. NotFound is the only case where re-creation is correct. HTTPException covers everything else — 5xx, network blip, rate limit. The code treats them all as "message gone" and creates a new one, eventually filling the pin slot (Discord caps pinned messages at 50 per channel).
**Impact:** Within 50 transient failures, the pulse channel hits the pin limit and `msg.pin()` fails permanently. After that, the pulse card is no longer pinned and gets buried as new pulses are posted.
**Fix:** Catch `discord.NotFound` only and re-create. For `discord.HTTPException`, log and `return` (skip this cycle, retry next minute). Also add an unpin of the old message before pinning the new one when re-creation IS correct.

### WARNING #5: `_get_jackpot_data` uses `log.warning` instead of `log.exception`, hiding tracebacks; also can compute negative timestamps

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:577-606`
**Confidence:** 0.85
**Risk:** Two issues bundled. (1) `log.warning` does not include the traceback, so when the jackpot query fails, you cannot diagnose whether it's a missing column, lock contention, or schema drift. (2) The "X minutes ago" math at lines 594-602 calls `datetime.fromisoformat(winner_row[2])` then `won_at.replace(tzinfo=timezone.utc)`. If the stored timestamp is naive UTC, this is correct. If it's already TZ-aware, `replace` overwrites without conversion. If it's local time, the resulting "ago" is wildly wrong. Worse — `casino_db.py` doesn't define how `last_won_at` is written; the type is just `TEXT`. `delta.total_seconds()` can also be **negative** if clock skew or a future-dated row exists, producing `int(-30/60) = 0` and "0m ago" without bounds checking.
**Vulnerability:** Per CLAUDE.md: "Silent `except Exception: pass` in admin-facing views is prohibited. Always `log.exception(...)`." This is a public dashboard, not strictly admin, but the same diagnosability principle applies.
**Impact:** Pulse dashboard silently shows wrong "ago" times after jackpot wins; outages of the jackpot section have no actionable log.
**Fix:** (1) Change to `log.exception(...)`. (2) Use `datetime.fromisoformat(...)` + explicit handling of naive vs aware (e.g., `if won_at.tzinfo is None: won_at = won_at.replace(tzinfo=timezone.utc)`). (3) Clamp `mins = max(0, int(delta.total_seconds() / 60))`.

### WARNING #6: Slot-machine "top player" logic uses session-wide `biggest_win`, not slots-specific

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:565-574`
**Confidence:** 0.92
**Risk:** The pulse dashboard's "Slots top player" panel shows the player with the largest win across **any** game, not specifically slots. A user with a $5000 blackjack win and zero slots play will be ranked as the slots top.
**Vulnerability:** Line 571: `if s.biggest_win > slots_top_amount:` — this checks `s.biggest_win` (which is set in `PlayerSession.record` from any game type with `outcome == "win"`), not a slots-scoped figure. The PlayerSession dataclass has no per-game-type biggest_win field. Also, `slots_top_mult` is initialized to 0 at line 568 and never assigned anywhere — it's always 0 in the rendered card.
**Impact:** Misleading dashboard. Slot players never get top billing if anyone played higher-stakes blackjack. The "top multiplier" always shows zero.
**Fix:** Track biggest win per-game-type in `PlayerSession`, e.g. `biggest_win_by_type: dict[str, int]`. Or skip the panel entirely for users with `s.games_by_type.get("slots", 0) == 0`. Compute `slots_top_mult` from the slots events in `s.events` rather than leaving it hardcoded.

### WARNING #7: `_on_game_result` propagates events from worker thread without input validation; bus topic is undocumented and untrusted

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:521-539`
**Confidence:** 0.78
**Risk:** All three `_on_*` handlers receive `event` from `flow_bus.emit` with zero validation. If a publisher emits a malformed object (missing `discord_id`, extras containing non-serializable types, `multiplier=None`), the handler will raise `AttributeError` on first access. The exception is caught by `flow_bus.emit`'s top-level handler, but session state may be left half-updated.
**Vulnerability:** `record()` mutates `total_games`, `games_by_type`, then calls `event.net_profit` (which can throw on `None - None`). If `net_profit` raises, the session has been incremented but profit is not updated — silently drifts. Also, `_event_to_dict` at line 47 silently drops any extras values that aren't `(str, int, float, bool, NoneType)`. A future publisher emitting `extra={"legs": [...]}` will have its legs vanish from persistence with no log.
**Impact:** Silent state corruption when publishers ship malformed events; opaque drop of structured extras during persistence.
**Fix:** Add a `_validate_event(event)` guard at the top of each handler that checks the required fields. Log and return early on failure. For `_event_to_dict`, log a warning when an extras key is dropped (or use `json.dumps(default=str)` to preserve structured data).

### WARNING #8: `txn_id`-based sort key is `Optional[int]`; None values collapse to 0 and reorder events incorrectly

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:622-627`
**Confidence:** 0.75
**Risk:** Pulse-card "Recent Highlights" sorts events by `evt.txn_id or 0`, then takes the top 6. `txn_id` is declared `Optional[int]` in `flow_events.py:25` and at least one publisher path can leave it None (e.g., wallet write failure that still emits an event). All None txn_ids collapse to 0 and end up at the bottom of the sort, but multiple None values within the same session are non-deterministic in tied-key sort order.
**Vulnerability:** Even when all events have valid txn_ids, txn_ids are per-database monotonic (assigned by SQLite AUTOINCREMENT in flow_economy.db). They are NOT global Lamport timestamps — a sportsbook event and a casino event use different ledgers and can be temporally interleaved while having unrelated id ranges. Sorting by `txn_id` does NOT yield true chronological order across event sources.
**Impact:** "Recent Highlights" panel can display events out of order, especially when sportsbook and casino events are mixed (which is the normal case for active users). Subtle but user-visible.
**Fix:** Add a `created_at: float` (time.time at construction) to every event dataclass and sort on that instead. Or capture `time.time()` at receive time inside the handler and store a tuple alongside the event in `session.events`.

### OBSERVATION #1: Hardcoded 10-second sleep in `before_pulse_loop` is fragile

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:501-504`
**Confidence:** 0.7
**Risk:** `await asyncio.sleep(10)` after `wait_until_ready` is a magic-number hack to wait for "startup DB ops to finish." If startup gets slower (more guilds, more migrations), the first pulse will race the migrations and may show empty/zero data.
**Fix:** Replace with an explicit ready signal — e.g., have the cog set an `asyncio.Event` after `_load_pulse_message_ids` and `load_persisted` complete, and `await self._ready.wait()` here. Or just remove the sleep entirely and let the first pulse render with whatever state exists; subsequent pulses will catch up.

### OBSERVATION #2: Redundant `dataclass as dc` import alias

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:13-14`
**Confidence:** 0.95
**Risk:** Line 13 imports `dataclass`. Line 14 re-imports `dataclass as dc`. The `Highlight` class at line 343 uses `@dc` instead of `@dataclass` for no apparent reason. Pure noise — likely a leftover from a refactor.
**Fix:** Delete line 14 and replace `@dc` with `@dataclass` at line 343.

### OBSERVATION #3: Soft `flow_events` ImportError fallback leaves `GameResultEvent` undefined; `_dict_to_event` will NameError on session restore

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:26-29, 61-74, 244-267`
**Confidence:** 0.85
**Risk:** Lines 26-29 only set `flow_bus = None` on ImportError but do not stub `GameResultEvent`, `SportsbookEvent`, `PredictionEvent`. If `flow_events.py` is missing, the cog tries to subscribe (skipped because `flow_bus is None`), but `cog_load` still calls `self.sessions.load_persisted()` which calls `_dict_to_event` (line 73), which references the undefined `GameResultEvent`. → NameError on startup.
**Fix:** Either (a) make the ImportError fatal — `flow_events` is a hard dependency for this cog, or (b) stub all four names: `GameResultEvent = SportsbookEvent = PredictionEvent = None` and gate `load_persisted` on whether they're real classes.

### OBSERVATION #4: `_event_to_dict` silently drops nested structures from extras

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:44-59`
**Confidence:** 0.9
**Risk:** The filter at line 46-47 keeps only scalar primitives. Any future publisher that puts a list or dict in extras (e.g., a list of slot reels, a coinflip pair) will lose those fields on persistence. Round-tripping a session through restart silently strips structured metadata.
**Fix:** Use `json.dumps(extras, default=str)` and accept the runtime cost, OR document the contract clearly in the dataclass docstring. A log warning when an extras key is dropped is the minimum.

### OBSERVATION #5: Session reaper iterates expired sessions sequentially, blocking the loop on Playwright renders

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:506-514`
**Confidence:** 0.7
**Risk:** `session_reaper` runs every 30 seconds and serially `await`s `_post_session_recap` for every expired session. Each recap renders a Playwright PNG (~1-3s) and posts to Discord. With 20 expired sessions in one tick (e.g., after a server lull where many players idled out simultaneously), the reaper takes ~40-60s, which exceeds the 30s tick interval and potentially overlaps with the next tick.
**Fix:** `await asyncio.gather(*[self._post_session_recap(s) for s in expired])` or batch into chunks of 5 to bound concurrency on the Playwright pool.

### OBSERVATION #6: `_test_highlight_impl` always renders the jackpot card; cannot test other highlight types

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_live_cog.py:772-783`
**Confidence:** 0.6
**Risk:** The boss_cog "Test Highlight" button only ever renders a fake jackpot card. Operators have no way to validate that the parlay, pvp, crash LMS, or prediction renderers work end-to-end without producing a real event. Combined with the dead `sportsbook_result` / `prediction_result` subscriptions (CRITICAL #1), there's no way to detect that those code paths broke.
**Fix:** Take a `highlight_type` arg and switch on it, or expose multiple buttons in boss_cog. At minimum, sanity-test parlay and prediction cards as part of the deploy smoke test.

## Cross-cutting Notes

The biggest pattern in this file is **wiring that exists on inspection but is dead at runtime**: subscriptions to topics that nothing publishes, sort keys derived from optional fields, "top player" logic that aggregates the wrong thing, recovery branches that only handle the happy variant of an exception. The file looks complete but is deceptively gappy under load and during incidents.

The SQLite resource-leak pattern (WARNING #2) likely affects other cogs in the Flow ring — `flow_wallet`, `flow_store`, `sportsbook_cards` all use raw `sqlite3.connect`. A ring-wide audit for `conn.close()` placement is warranted.

The TOCTOU race in `SessionTracker` (CRITICAL #3) is the same shape as the trade-approval and ability-budget races called out in CLAUDE.md. The codebase has a pattern of using `asyncio.to_thread` for SQLite without recognizing that the wrapped function also mutates Python state. Rule of thumb: only push **pure I/O** into threads, never mixed I/O + state mutation.
