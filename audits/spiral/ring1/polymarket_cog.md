# Adversarial Review: polymarket_cog.py

**Verdict:** block
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 3773
**Reviewer:** Claude (delegated subagent)
**Total findings:** 27 (8 critical, 11 warnings, 8 observations)

## Summary

This file holds real-money TSL Bucks state for the prediction-market subsystem and is riddled with idempotency violations: every single `flow_wallet.debit/credit` call passes `subsystem_id` instead of `reference_key`, defeating the entire idempotency layer. A button-click double-tap or Discord interaction retry will double-debit. There is also a broken method reference (`self._resolve`) that will hard-crash `refund_sports_impl`, multiple silent admin-view exception swallows, a `tasks.loop(minutes=5)` that does no overrun protection on a sync that performs sequential per-market upserts and a chain of 6 sub-passes plus an external Gemini AI call, and an admin command (`/resolve_market`) that ignores its own `result="VOID"` branch and calls `finalize_event(result_payload={"resolved_side": "VOID"})` instead of refunding.

## Findings

### CRITICAL #1: Every wallet call omits `reference_key` — idempotency completely broken

**Location:** `polymarket_cog.py:654-668, 726-731, 877-882`
**Confidence:** 0.99
**Risk:** Per CLAUDE.md and `_atlas_focus.md`, every `flow_wallet.debit()` and `credit()` call MUST pass `reference_key=...` to prevent double-debits/credits on Discord interaction retries. This file passes only `subsystem_id`, which is *not* the dedup key — `flow_wallet._check_idempotent()` checks `reference_key`. The format-string requirement is `f"{SUBSYSTEM}_DEBIT_{uid}_{event_id}_{int(time.time())}"`, none of which appears in this file.
**Vulnerability:** Discord 3-second interaction timeout commonly causes the client to retry button callbacks. When a user clicks "Buy 100 YES" and the network is slow, both the original and the retry will reach `_execute_prediction_buy` → both will pass the balance check and both will `flow_wallet.debit()` because there is no `reference_key` to dedup against. Same applies to `_execute_prediction_sell` (double-credit) and the partial-sell partial-credit path.
**Impact:** Real money corruption. Users will be silently double-debited on slow connections (real loss) or double-credited on sells (free money exploit). The `update_balance` helper, the buy path, and the sell path are all broken the same way.
**Fix:** Pass `reference_key` to every `flow_wallet.debit/credit` call. For the buy path:
```python
ref = f"PREDICTION_DEBIT_{user_id}_{market_id}_{side}_{int(time.time())}"
await flow_wallet.debit(user_id, cost_bucks, "PREDICTION", reference_key=ref, ...)
```
For sells, derive the key from the contract id + sold quantity. The `update_balance()` helper at line 654 must also be updated or removed.

---

### CRITICAL #2: `self._resolve` does not exist — `refund_sports_impl` will crash

**Location:** `polymarket_cog.py:3713-3716`
**Confidence:** 0.99
**Risk:** `refund_sports_impl()` calls `await self._resolve(market_id, "VOID", resolved_by="sports_filter")`. Grep shows no `def _resolve` anywhere in `polymarket_cog.py`. The legacy `_auto_resolve_pass + _local_settle_pass` pipeline was replaced (per the docstring at line 2776) with `_finalize_resolved_pass` which uses `sportsbook_core.finalize_event` — not a `_resolve` method on the cog.
**Vulnerability:** Any commissioner who runs the `boss_cog` "Refund Sports" action will get an `AttributeError: 'PolymarketCog' object has no attribute '_resolve'`, after the function has already SELECTed the list of sports markets but BEFORE it has refunded any of them. Worse, the loop will partially mutate state — the `DELETE FROM prediction_markets` at line 3720-3723 runs *after* `_resolve`, so the entire pipeline aborts with users still locked into open contracts on a deleted market.
**Impact:** The very recovery path designed to make users whole when sports markets sneak through the filter is non-functional. Users with open bets on accidentally-imported sports markets cannot be refunded via the admin tool. Function will throw on every invocation.
**Fix:** Either reimplement `_resolve(market_id, "VOID", resolved_by=...)` (the old method that walked `prediction_contracts`, marked them voided, and refunded each user via `flow_wallet.credit` with a unique `reference_key`), or rewrite `refund_sports_impl` to use `sportsbook_core.finalize_event` with a `VOID` payload — but verify sportsbook_core actually handles VOID as a refund signal.

---

### CRITICAL #3: `/resolve_market` admin command silently ignores `VOID`

**Location:** `polymarket_cog.py:3553-3617`
**Confidence:** 0.96
**Risk:** The `_resolve_market_impl` validates `result in ("YES", "NO", "VOID")` at line 3560, then calls `sportsbook_core.finalize_event(result_payload={"resolved_side": result})` for *all three* values, including `"VOID"`. Nothing in the function body checks for VOID or routes it to a refund path. The local DB UPDATE writes `result='VOID'` and `status='closed'` as if it had succeeded.
**Vulnerability:** A commissioner who voids a market expecting refunds to all bettors will get a green checkmark and a "EVENT_FINALIZED emitted — sportsbook_core will settle open bets" reply. Whether refunds actually happen depends entirely on whether `sportsbook_core.settle_event` recognizes `"VOID"` as a refund — but there's no documentation, no test, and no contract assertion in this file. If sportsbook_core treats unknown sides as "all bets lose", users get nothing.
**Impact:** User-facing economic disaster on every voided market. The admin gets false confirmation. In a high-volume market, hundreds of TSL Bucks could be silently confiscated.
**Fix:** Branch on `result == "VOID"` and route to a dedicated refund pipeline that walks open contracts and credits each user via `flow_wallet.credit(reference_key=f"PREDICTION_REFUND_{user_id}_{contract_id}")`. Do not rely on sportsbook_core's interpretation of "VOID" without an explicit contract.

---

### CRITICAL #4: Sync loop has no overrun guard — sync can cascade into itself

**Location:** `polymarket_cog.py:2065-2211`
**Confidence:** 0.92
**Risk:** `@tasks.loop(minutes=5)` fires every 5 minutes regardless of whether the previous tick is still running. The sync body performs: (1) `fetch_active_events(limit=200)` over HTTP, (2) sequential `db.execute` per market in a single connection (could be 200+ rows × ~2 blocking-ish writes each), (3) `_finalize_resolved_pass` which fetches another 100 closed markets and may call `fetch_market_by_id` per stale row inside a Python `for` loop, (4) `_classify_unknown_categories` which calls Gemini once on first sync, (5) `_update_curation_scores`, (6) `_store_price_snapshots`, (7) `_check_price_alerts`. Under degraded conditions (slow Polymarket API, large stale-market backlog) a single sync can absolutely exceed 5 minutes.
**Vulnerability:** discord.py `tasks.loop` does *not* serialize itself by default. If a sync runs for 7 minutes, the next 5-minute tick fires while the first is still mid-DB-write. Both will hit the same `prediction_markets` table on separate connections — `aiosqlite.connect(timeout=30)` will queue and may eventually time out, leaving partial inserts. Worse, the same `self.client._session` (a single `aiohttp.ClientSession`) is shared between the two overlapping runs, which is unsafe across concurrent reuse if one of them closes the session in `cog_unload`.
**Impact:** Data corruption (partial upserts), connection pool exhaustion, possible permanent stuck state on the price-snapshot/alert pass. The blocked-categories `DELETE` at line 2161-2167 could race with the `INSERT` at line 2129 and cause sports markets to flicker in and out.
**Fix:** Add a concurrency guard: `self._sync_lock = asyncio.Lock()` on `__init__`, then `if self._sync_lock.locked(): return; async with self._sync_lock: ...` in `sync_markets`. Also break the per-market upserts into batched executemany.

---

### CRITICAL #5: Silent `except Exception: pass` in admin and money-handling paths

**Location:** `polymarket_cog.py:1376-1377, 1514-1515, 1730-1731, 1876-1877, 2026-2027, 3197-3198`
**Confidence:** 0.97
**Risk:** Per CLAUDE.md, silent `except Exception: pass` in admin-facing or financial paths is *prohibited*. Multiple sites silently swallow exceptions:
- Line 1376-1377: ledger posting after a buy — if it fails the user has paid but no audit trail exists
- Line 1514-1515: market-engagement INSERT silently ignored
- Line 1730-1731: ledger posting after preset-buy
- Line 1876-1877: live price fetch — silently uses cached price (this is the documented "use cached price" path, but the bare `except Exception: pass` will swallow programming errors too)
- Line 2026-2027: ledger posting after sell (so partial swap discrepancies are invisible)
- Line 3197-3198: engagement log silently ignored

The buy/sell ledger swallows are most dangerous: a successful debit + failed ledger post = a missing audit row that the user will dispute and the commissioner cannot reconcile.
**Vulnerability:** Programming errors (typos, schema drift, ImportError on `ledger_poster`) become invisible. Real bugs are masked.
**Impact:** Loss of auditability for every prediction transaction. Silent ledger gaps make `/boss flow audit` and downstream reconciliation impossible.
**Fix:** Replace every bare `except Exception: pass` with `except Exception: log.exception("...specific context...")`. For the ledger paths specifically, log at WARNING and include the user_id and txn_id.

---

### CRITICAL #6: TOCTOU on market `status` between display and place

**Location:** `polymarket_cog.py:687-799` (buy executor) and `polymarket_cog.py:3426-3510` (`/bet`)
**Confidence:** 0.85
**Risk:** The `_execute_prediction_buy` function does check `mkt_row[0] != "active"` inside the `BEGIN IMMEDIATE` transaction (line 715), which is good. However, `_check_price_alerts` (line 2647) and the workspace flow render the UI with prices/status from a non-locked SELECT. A user can be looking at a workspace card showing "Buy YES" buttons while the sync loop, in another transaction, has already flipped the market to `closed` and emitted EVENT_FINALIZED. The buy will (correctly) reject — but the user-facing error is the cryptic "This market is no longer active." If the resolve race goes the other direction (sync writes `status='closed'` after the transaction reads `'active'`), the bet succeeds against a market that has *already paid out winners* via sportsbook_core, leaving the new bettor stranded.
**Vulnerability:** The price refresh at line 3475-3492 in `bet_cmd` updates `yes_price`/`no_price` but does NOT recheck `status`. A user betting via `/bet <slug>` can write fresh prices to a market that just closed.
**Impact:** Stranded contracts with no settlement path. Possible double-settlement if the bet posts after sportsbook_core has already processed `EVENT_FINALIZED`.
**Fix:** In `bet_cmd`, after the live price fetch, re-SELECT `status` and bail if not 'active'. In `_execute_prediction_buy`, also verify the market is still `'active'` *after* the wallet debit but before commit (or use a CTE check on UPDATE). Also include `end_date` in the check — refuse buys within 60 seconds of `end_date`.

---

### CRITICAL #7: `bet_cmd` partial slug match has no disambiguation

**Location:** `polymarket_cog.py:3444-3460`
**Confidence:** 0.90
**Risk:** When the exact slug match fails, the code falls through to `WHERE slug LIKE '%{slug}%' AND status = 'active' LIMIT 1` with no `ORDER BY`. SQLite will return whatever row it scans first — typically by rowid or insertion order. Two markets with overlapping slug fragments (e.g. "trump-2028" matches both "will-trump-run-2028" and "trump-2028-vp-pick") will deterministically resolve to the same arbitrary one.
**Vulnerability:** A user typing `/bet trump` could be silently routed to a *different market* than they intended. The market title shown in the workspace (`market_dict["title"]`) will not match what they typed, and a hurried user clicking "Buy YES $1000" can put a real wager on the wrong market.
**Impact:** User loses real TSL Bucks to the wrong contract. No undo path other than `/resolve_market VOID` (which is itself broken — see CRITICAL #3).
**Fix:** When the exact match fails, return the top 5 candidates via a select menu and force the user to pick. Never auto-route a money command on a fuzzy slug match.

---

### CRITICAL #8: `update_balance` legacy helper has no `reference_key` — corrupts ledger if called

**Location:** `polymarket_cog.py:654-668`
**Confidence:** 0.95
**Risk:** `update_balance(user_id, delta, contract_id=...)` is a top-level helper that converts a signed delta into a `flow_wallet.credit/debit` call. It passes `subsystem_id=str(contract_id)` but **no `reference_key`**. Even more dangerous: it has no caller context — anyone importing this module could call it from anywhere.
**Vulnerability:** This is a footgun. If this helper is reused for refund/payout logic in a future patch (or already used by tooling not visible in this file), every retry will silently double-process. The `description="prediction market"` is generic enough that there's no audit signal either.
**Impact:** Same as CRITICAL #1 but worse, because the helper's name (`update_balance`) implies safety while doing the opposite.
**Fix:** Either delete this helper (it's unused inside this file) or require `reference_key` as a mandatory keyword arg.

---

### WARNING #1: `_check_price_alerts` time-window query is wrong

**Location:** `polymarket_cog.py:2646-2656`
**Confidence:** 0.88
**Risk:** The query selects snapshots where `ps.snapshot_at <= hour_ago AND ps.snapshot_at >= (now - 1h30m)`. The intent is "snapshots from 60-90 minutes ago" but the SQL reads "<= 1h ago AND >= 1h30m ago" which translates to "between 90 and 60 minutes old". That's correct. But it does `ORDER BY ps.snapshot_at ASC` which means the *earliest* (oldest, ~90min ago) snapshot is used as `old_price`. If snapshots run every 15 min, you'll compare against a 75-90min-old price, not a 60min-old price. The label says "in the last hour" but the math is off by up to 30 minutes.
**Vulnerability:** Alerts will fire on movements measured against stale baselines. A coin that moved sharply 80 minutes ago and has since stabilized will trigger an alert claiming a 1-hour move.
**Impact:** Misleading user-facing alerts. Could trigger holders to dump or buy on phantom moves.
**Fix:** Use `ORDER BY ps.snapshot_at DESC LIMIT 1` per market_id, or pre-aggregate with `MIN(snapshot_at)` filtered to the right window. Better: use a window function or compute the exact 60-minute-prior snapshot per market.

---

### WARNING #2: `_check_price_alerts` reads `_alerts_this_hour` outside the hour reset

**Location:** `polymarket_cog.py:2615-2624, 2728`
**Confidence:** 0.85
**Risk:** The function reads `self._alerts_this_hour` and `self._alert_hour` via `getattr(...)`, computes `current_hour`, resets only if hour changed, then writes `self._alerts_this_hour = alerts_this_hour` at the end. But the read at line 2616 is from `self`, the local rebinds, and the write back at line 2728 is *after* the alert posting loop. If the function runs across an hour boundary (sync starts at 14:59:50, alert posted at 15:00:10), the rebind never sees the new hour.
**Vulnerability:** Cross-boundary calls can cause the rate-limit reset to misfire. The `alerts_this_hour` counter can stay at 3 when it should reset, suppressing alerts in the new hour. Or it can reset mid-loop and fire 6 alerts in a single tick.
**Impact:** Inconsistent rate limiting on user-visible alerts. Posts to a public channel.
**Fix:** Cache `current_hour` once at the top, compute the rate-limit decision once, and persist back atomically. Better: use a sliding window stored in DB (`market_engagement` already has `event_type='alert_fired'` rows).

---

### WARNING #3: `_classify_unknown_categories` regex is greedy and brittle

**Location:** `polymarket_cog.py:2248-2252`
**Confidence:** 0.90
**Risk:** `json_match = re.search(r'\[.*\]', text, re.DOTALL)` is greedy and will match the *outermost* brackets. If the AI returns `[1, 2, [3, 4], 5]` or any nested array (or includes a literal `]` in a category name), the match will be wrong. Worse, if the AI returns `Here is the JSON: [...]\nHope that helps!`, the regex will match correctly — but if it returns `[a, b]\n\n[c, d]`, the greedy match will swallow both and fail to parse.
**Vulnerability:** A single malformed AI response will skip classification for the entire batch of 50 markets, leaving them all in "Other" category, polluting the curation set.
**Impact:** Curation quality degradation. Repeated runs may not retry the same markets because the function only runs on `_first_sync_done`.
**Fix:** Use `json.loads(text)` directly with try/except, or parse after stripping markdown code fences. If the AI is supposed to return JSON, request `tier=Tier.HAIKU, json_mode=True` (which is done elsewhere at line 3270 but NOT here at line 2246).

---

### WARNING #4: `_classify_unknown_categories` runs only once per process lifetime

**Location:** `polymarket_cog.py:2186-2191`
**Confidence:** 0.96
**Risk:** `if not self._first_sync_done: self._first_sync_done = True; await self._classify_unknown_categories()`. Set-and-forget. Markets that arrive on later syncs and land in "Other" will never be re-classified for the lifetime of the bot process.
**Vulnerability:** New markets continuously stream in but the AI classification only runs once. Categories drift.
**Impact:** Curation quality decays over time. Markets accumulate in "Other" with no recovery path until a bot restart.
**Fix:** Run classification on a daily cadence or whenever the count of `category LIKE '%Other%' AND status='active'` exceeds a threshold.

---

### WARNING #5: `_finalize_resolved_pass` uses 95% threshold to detect resolution

**Location:** `polymarket_cog.py:1165-1184, 2791`
**Confidence:** 0.85
**Risk:** `detect_result()` returns `'yes'` if `yes_p >= 0.95 and no_p <= 0.05`. Polymarket-resolved markets typically settle to exactly `1.0/0.0` but the API can show transient values during winding-down. A 95% threshold is also reachable on *unresolved* markets where consensus is overwhelming (e.g. "Will Biden seek re-election in 2024" reached 99% NO before official withdrawal).
**Vulnerability:** Markets that hit 95% confidence but are *not actually closed* on Polymarket get auto-finalized in TSL. The guard at line 1171 (`if not market.get("closed"): return None`) helps, but the closed-flag update on Polymarket can lag the resolution price by minutes-to-hours, and the sync runs every 5 minutes — there's a real window where `closed=true` and `yes_price=0.94` (still unresolved), or where `closed=true` and `yes_price=0.96` but the market is being voided. The threshold-based detector will misfire.
**Impact:** Premature settlement of bets that should remain open or be voided. Users get paid (or not paid) on the wrong outcome.
**Fix:** Polymarket's Gamma API has a `umaResolutionStatuses` and an explicit `outcome` field on resolved markets. Use those instead of price-thresholding. Always require `closed=true AND outcome IS NOT NULL`.

---

### WARNING #6: Sportsbook_core mirror failure swallowed silently

**Location:** `polymarket_cog.py:761-791`
**Confidence:** 0.92
**Risk:** After the buy commits, the sportsbook_core mirror block does:
```python
try:
    await sportsbook_core.write_event(...)
    await sportsbook_core.write_bet(...)
except Exception as exc:
    log.warning(f"[POLY] sportsbook_core mirror failed for poly:{market_id}:{side}: {exc}")
```
This is a `log.warning` (good) but no compensating action. The user has been debited and a row exists in `prediction_contracts`, but the mirror in `sportsbook_core` is missing. When `_finalize_resolved_pass` later emits `EVENT_FINALIZED`, sportsbook_core will have no record of this user's bet and *they will not be paid out*.
**Vulnerability:** Silent partial-write between two databases (flow_economy.db and flow.db). The settlement path relies on the mirror being present, but the buy path treats mirror failures as warnings.
**Impact:** User loses TSL Bucks, never receives payout, has no error in their workspace.
**Fix:** Either (a) make the mirror part of the same transaction (open both DBs in the same async with, BEGIN IMMEDIATE both, commit both — though aiosqlite doesn't natively support 2PC), or (b) add a reconciliation pass that walks `prediction_contracts` rows and inserts missing sportsbook_core mirrors before settlement.

---

### WARNING #7: `_get_internal_bet_counts` and `_get_recently_featured_ids` lifetime mismatch

**Location:** `polymarket_cog.py:415-444, 2310-2312`
**Confidence:** 0.78
**Risk:** Both helpers receive an existing `db` connection. In `_update_curation_scores`, this is called inside a single `async with aiosqlite.connect(DB_PATH, timeout=30) as db:` — fine. But the json-decode loop in `_get_recently_featured_ids` silently catches `json.JSONDecodeError, TypeError` and continues. A market with corrupt `supporting` JSON will be silently dropped from the recently_featured set, meaning a market that *was* recently featured can score higher than it should because the staleness penalty is not applied.
**Vulnerability:** Stale feature suppression bypassed by data corruption. Users see the same market in the daily drop two days in a row.
**Impact:** Curation freshness metric defeated; user-facing "look, same market again" complaint.
**Fix:** Log the JSONDecodeError at WARNING and consider repairing the row.

---

### WARNING #8: `daily_drop_task` Gemini call inside DB connection

**Location:** `polymarket_cog.py:3070-3229`
**Confidence:** 0.82
**Risk:** `_generate_daily_drop` opens an `aiosqlite.connect` at line 3073, fetches rows, exits the context. Good. But Step 5 (`render_daily_drop_card`) and Step 6 (`send_card_to_channel`) are followed by Step 7 which opens a *fresh* DB connection. If steps 5 or 6 take a long time (Playwright render + Discord upload), a parallel `daily_drop_task` from a 24-hour repeat could in theory race. Lower severity because the duplicate guard at line 3055 handles this. However, the `_gemini_curate` retry at line 3121 sleeps 30 seconds and retries — for 30 seconds the function holds no DB lock but also no progress, and the daily drop time window is fixed at 14:00 UTC.
**Vulnerability:** If Gemini is degraded, the retry sleeps 30s and may still fail. Then the fallback at 3128-3133 picks the *top scored* market. If the top scored market is itself stale or buggy, the daily drop posts garbage.
**Impact:** Low — fallback exists. Concern is observability: AI failures are logged at WARNING but the user-facing card shows no indication that AI was bypassed.
**Fix:** Add an "AI generated" / "Algorithmic fallback" marker on the card so editorial quality is visible.

---

### WARNING #9: `_announce_resolutions` references `item["counts"]` but never calls `_resolve`

**Location:** `polymarket_cog.py:2949-3038`
**Confidence:** 0.94
**Risk:** `_announce_resolutions(self, resolved: list[dict])` reads `counts = item["counts"]` and `won = counts.get("won", 0)` — but nothing in the file actually populates a `counts` dict on resolved markets anymore. The legacy `_resolve` method (which does not exist — see CRITICAL #2) was the producer. `_finalize_resolved_pass` does NOT call `_announce_resolutions`. This entire 90-line function appears to be dead code that crashes if reached.
**Vulnerability:** Dead code that, if a future patch wires it up, will throw `KeyError: 'counts'` on the first iteration. Worse, the developer maintaining this thinks announcements are working.
**Impact:** No resolution announcements to the public channel. Users have to discover their settled bets via portfolio refresh.
**Fix:** Either delete `_announce_resolutions` and the dead `_resolve` references, or wire it into the `_finalize_resolved_pass` after-emit point with a fresh DB query that builds the counts dict.

---

### WARNING #10: `_get_prediction_leaderboard` `resolved_at > week_start` filter is wrong

**Location:** `polymarket_cog.py:3313-3331`
**Confidence:** 0.88
**Risk:** `WHERE resolved_at > ?` with `week_start = (now - 7 days).isoformat()`. This compares a TEXT column to a TEXT param. If `resolved_at` is stored as `'2026-04-09T14:00:00+00:00'` and the param is `'2026-04-02T14:00:00+00:00'`, lexicographic compare works only because ISO 8601 with consistent timezone is naturally sortable. But if `resolved_at` ever stored a different format (e.g. `'2026-04-09 14:00:00'`), the compare silently fails. There's no schema constraint ensuring the format.
**Vulnerability:** Format drift in `resolved_at` will corrupt the leaderboard. Worse, if a contract has `resolved_at = NULL` (settled but field not populated), it's silently dropped from the leaderboard.
**Impact:** Leaderboard shows wrong winners. The `setting status IN ('won', 'lost')` filter at the SQL level catches dropped rows because all settled contracts should have non-null `resolved_at` — but the file does not enforce this.
**Fix:** Use `datetime(resolved_at) > datetime(?)` or store `resolved_at` as an INTEGER unix timestamp.

---

### WARNING #11: Position select uses str→int conversion without try/except

**Location:** `polymarket_cog.py:1276-1280`
**Confidence:** 0.80
**Risk:** `await self._ws._on_position_select(interaction, int(val))` casts the dropdown value to int. If a developer ever passes a non-integer contract id (e.g. UUID for a future schema migration), this throws ValueError on the user's interaction.
**Vulnerability:** Single line — but the unhandled ValueError will surface as "interaction failed" to the user.
**Impact:** Low. User retry recovers.
**Fix:** Wrap in try/except ValueError and respond with a generic error.

---

### OBSERVATION #1: `/markets`, `/bet`, `/portfolio` not gated by `setup_cog` channel router

**Location:** `polymarket_cog.py:3375-3528`
**Confidence:** 0.70
**Risk:** Per CLAUDE.md, channel routing is enforced via `require_channel()` decorator from `setup_cog`. None of the user-facing prediction commands have this decorator. Users can `/bet` from any channel, including admin channels, leaking betting noise.
**Impact:** UX inconsistency. Other Flow commands (sportsbook, casino) presumably do gate.
**Fix:** Add `@require_channel("predictions")` or equivalent decorator.

---

### OBSERVATION #2: `_balance_footer` is a stub that ignores its name

**Location:** `polymarket_cog.py:1430-1432`
**Confidence:** 0.99
**Risk:** Method named `_balance_footer` returns `"FLOW Markets · Powered by Polymarket"` — a constant string with no balance in it. The docstring even says "populated lazily by show_ methods" but no caller does the population.
**Impact:** Misleading name. Users navigating the workspace don't see their balance in the market list footer.
**Fix:** Either rename to `_default_footer` or actually fetch and inject the balance.

---

### OBSERVATION #3: `daily_drop_task` runs at fixed 14:00 UTC — DST drift

**Location:** `polymarket_cog.py:3046`
**Confidence:** 0.75
**Risk:** Comment says "9 AM EST = 14:00 UTC" but EST is UTC-5; EDT is UTC-4. From mid-March to early November the daily drop posts at 10 AM ET, not 9 AM. The comment is wrong half the year.
**Impact:** Mild — the drop still posts, just an hour off in the user's perception. No financial impact.
**Fix:** Either accept and update the comment to "14:00 UTC daily" or use a tz-aware time anchored to America/New_York.

---

### OBSERVATION #4: `init_prediction_db` migration `try: ALTER TABLE ... except: pass` swallows real errors

**Location:** `polymarket_cog.py:543-550`
**Confidence:** 0.85
**Risk:** The "Column already exists" idiom uses bare `except Exception: pass`. This will also swallow `OperationalError: no such table` (if the prior CREATE failed silently) and any other DB error.
**Impact:** Migration failures invisible. Could silently leave the schema in a half-migrated state.
**Fix:** Catch only `aiosqlite.OperationalError` with a "duplicate column" message check: `if "duplicate column" not in str(e): log.exception(...)`.

---

### OBSERVATION #5: `_weighted_sample` retries with `pop(idx)` causing `O(n²)` behavior

**Location:** `polymarket_cog.py:2502-2553`
**Confidence:** 0.75
**Risk:** Inside the loop, when category cap is exceeded the code does `remaining.pop(idx); continue`, but then re-builds `weights = [w for _, w in remaining]` and `total = sum(weights)` from scratch. With the curated_scores LIMIT 100 from the SELECT (line 2461), this is at most 100 iterations × 100-element list copies per call. Not an outage, but wasteful.
**Impact:** Low. ~10ms per `_get_curated_selection` call instead of 1ms.
**Fix:** Cache the weights and decrement when popping. Or use `random.choices(k=count*3)` and dedupe.

---

### OBSERVATION #6: `cog_unload` fires-and-forgets `client.close()`

**Location:** `polymarket_cog.py:2053-2056`
**Confidence:** 0.78
**Risk:** `asyncio.create_task(self.client.close())` schedules the close but does not await it. If the cog unloads during shutdown, the task may not complete before the loop closes, leaving the aiohttp ClientSession unclosed (resource leak warning).
**Impact:** Cosmetic leak warning at shutdown.
**Fix:** If `cog_unload` is sync (which it is on this signature), use `asyncio.ensure_future` and accept the warning, OR convert to `async def cog_unload`.

---

### OBSERVATION #7: Ledger `description` truncated to 50 chars without ellipsis

**Location:** `polymarket_cog.py:1373, 1727, 2023`
**Confidence:** 0.50
**Risk:** `f"Buy {result['quantity']} {side} — {market['title'][:50]}"` truncates titles silently. A user looking at their ledger sees a chopped-off market name.
**Impact:** Mild UX papercut.
**Fix:** Append `...` if `len(title) > 50`.

---

### OBSERVATION #8: `MARKETS_PER_PAGE = 10` but `LIMIT 100` SQL is hardcoded in multiple paths

**Location:** `polymarket_cog.py:183, 2461, 3082`
**Confidence:** 0.50
**Risk:** Magic number `LIMIT 100` appears in `_get_curated_selection` (line 2461) and `_generate_daily_drop` (line 3082) without a constant. The relationship between `MARKETS_PER_PAGE` (10) and the underlying pool size (100) is not explicit.
**Impact:** Maintenance smell.
**Fix:** Define `CURATED_POOL_SIZE = 100` constant and reference it.

---

## Cross-cutting Notes

The single dominant issue across this file is **missing `reference_key` on every wallet call**. This is a per-codebase invariant per CLAUDE.md and is probably present in the same form in any other prediction/economy file that touches `flow_wallet`. Recommend a grep across all callers of `flow_wallet.debit`/`credit` to find every site that omits `reference_key=`. The pattern in this file is to pass `subsystem_id` instead, which suggests a common misunderstanding — the parent agent should propagate this finding to a sweep of all `flow_wallet` callers, not just polymarket_cog.

The `_resolve` method ghost-call (CRITICAL #2) suggests a botched refactor: the legacy `_auto_resolve_pass + _local_settle_pass` pipeline was replaced with `_finalize_resolved_pass` (per the docstring at line 2776), but the cleanup missed `refund_sports_impl` and `_announce_resolutions`. Both are now dead-on-call. A grep for `self._resolve` and `self._auto_resolve_pass` in the rest of the codebase will surface any other stale callers.

The bus topic `prediction_result` is subscribed by `flow_live_cog.py:429` and `flow_live_cog.py:535` (`_on_prediction_result`) but **no live publisher exists in polymarket_cog.py**. This matches the `sportsbook_result` pattern flagged in `_atlas_focus.md`. Either delete the subscriber or wire `polymarket_cog._finalize_resolved_pass` to also publish `prediction_result` after emitting `EVENT_FINALIZED` (and ensure `SportsbookEvent.guild_id` is wired through).
