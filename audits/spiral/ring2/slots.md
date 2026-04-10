# Adversarial Review: slots.py

**Verdict:** block
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 451
**Reviewer:** Claude (delegated subagent)
**Total findings:** 15 (3 critical, 6 warnings, 6 observations)

## Summary

RTP-controlled slot machine and daily scratch card. Same fundamental idempotency gap as coinflip — `deduct_wager` is called without propagating a stable `reference_key` into `flow_wallet.debit`. Free-spin logic silently bumps the user's balance with `correlation_id=None`, breaking wager-registry invariants. `random.random()` used for RTP rolls on real money. Multiple render-message edits on the serialized wager path can crash mid-game and strand the debit.

## Findings

### CRITICAL #1: `random.random()` used for RTP rolls and tile selection on real-money wagers

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:26,102-111,118-167,419`
**Confidence:** 0.90
**Risk:** The RTP outcome table roll on line 106 (`roll = random.random()`), reel visual generation on lines 118-167 (`random.sample`, `random.choice`, `random.shuffle`), and the daily scratch tile selection on line 419 (`random.choices`) all use the non-cryptographic Mersenne Twister PRNG. Per `CLAUDE.md`'s financial-impact attack surface, slot outcomes must use `secrets.SystemRandom`.
**Vulnerability:** Predictable PRNG on real-money outcomes. A player who observes enough outcomes in the same process lifetime could in principle learn the state and predict the next spin. For a house-edge of 3.8%, the house has a thin margin; a prediction attack that wins even 5% more often than fair would wipe the edge.
**Impact:** Theoretically exploitable; practically unlikely but a standard casino-review "block" flag.
**Fix:** Replace all `random.*` calls with a module-level `_rng = secrets.SystemRandom()` instance, or use the `secrets` module functions directly. Keep `random` only for purely visual flourishes.

### CRITICAL #2: `deduct_wager` → `flow_wallet.debit` chain missing `reference_key` (double-debit on retry)

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:214-218`
**Confidence:** 0.95
**Risk:** Line 216 calls `deduct_wager(interaction.user.id, wager, correlation_id=correlation_id)` with `correlation_id = uuid.uuid4().hex[:8]` (line 214). Inside `casino_db.py:1046-1086`, `deduct_wager` does NOT forward `correlation_id` as `reference_key` to `flow_wallet.debit` — it only passes it as `subsystem_id`, which is NOT the idempotency column per `flow_wallet._check_idempotent` (line 174). Each Discord interaction retry generates a NEW UUID and a NEW debit.
**Vulnerability:** Per `CLAUDE.md`: "ALL debit calls MUST pass `reference_key`. Without it, Discord interaction retries or button double-clicks cause double-debits." The Play Again view in this file (line 293-300) amplifies this — `replay_callback=functools.partial(play_slots, wager=wager)` generates a fresh UUID per call.
**Impact:** Double-debit during Discord interaction-retry windows. Financial ledger corruption and player-visible balance errors.
**Fix:** Derive `reference_key` from `interaction.id` (stable across Discord retries) and forward it through `deduct_wager` to `flow_wallet.debit`. Alternatively, fix `deduct_wager` at the wrapper layer in `casino_db.py` to use `correlation_id` as `reference_key`.

### CRITICAL #3: Free spin `process_wager(correlation_id=None)` breaks wager-registry invariants

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:315-324`
**Confidence:** 0.85
**Risk:** The free spin triggered on line 304 calls `process_wager(..., wager=0, correlation_id=None)`. With `wager=0`, the free spin still produces a `payout` that must be credited to the user via `flow_wallet.credit`. Passing `correlation_id=None` means the credit has NO idempotency key. If the credit path relies on `correlation_id` to register a wager_registry entry and then settle it, the free spin's payout skips the registry entirely and becomes a ghost transaction. If the free spin is rendered twice (interaction retry), the payout could be credited twice.
**Vulnerability:** The whole point of `correlation_id` in `process_wager` is to correlate the initial debit with the final credit. Bypassing it means the house bank report has no way to reconcile free spin economics, and the free spin credit is not idempotent.
**Impact:** Free spins can be double-credited; house bank P&L is understated; player "free spin won $100" claim cannot be verified against the ledger.
**Fix:** Generate `correlation_id = f"freespin_{interaction.user.id}_{int(time.time())}"` (or `f"freespin_{uuid.uuid4().hex[:8]}"`) and pass it explicitly. Also register the free spin in the wager registry as a zero-wager event.

### WARNING #1: `replay_message.edit` can crash during the spin animation, stranding the debit

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:230-240`
**Confidence:** 0.85
**Risk:** After `deduct_wager` succeeds on line 216, the code enters an animation loop (lines 236-240) that calls `msg.edit(attachments=[file2])` three times with 0.9-second sleeps between them. If Discord rate-limits the edit, raises HTTPException, or the message is deleted mid-animation, the loop will crash with NO cleanup — the debit has already happened, `process_wager` has NOT run, and the user's balance is now short by the wager with no rendered outcome.
**Vulnerability:** There is no try/except around the animation path. Mid-spin failures leave the wager in "debited, not settled" state. The wager_registry entry stays `status='open'`.
**Impact:** Player loses money on a visibly stuck spin. Manual admin reconciliation required.
**Fix:** Wrap lines 221-291 in a try/except that catches `discord.HTTPException` and `asyncio.CancelledError`, and on failure call `refund_wager(uid, wager, correlation_id=correlation_id)` to reverse the debit. Log the incident at WARNING.

### WARNING #2: Free spin ledger post is conditional on `free_payout > 0`, silently dropping losses

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:332-340`
**Confidence:** 0.75
**Risk:** The free spin only posts to the `#ledger` channel if `free_payout > 0`. Loss-outcome free spins are silently not logged. This creates a bias in the ledger visibility — users see "free spin wins!" but never see "free spin lost", which is misleading and also breaks any analytics that count free-spin frequency from the ledger.
**Vulnerability:** Asymmetric reporting.
**Impact:** Ledger is incomplete; free-spin RTP analytics are biased upward because losses are invisible.
**Fix:** Always post to ledger; let the badge/color reflect win vs loss.

### WARNING #3: `process_wager` on line 250 uses `outcome="push"` when `payout == wager`, which may not be possible

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:243-248`
**Confidence:** 0.55
**Risk:** `if payout == wager: outcome = "push"`. The outcome table has multipliers 0.0, 0.3, 0.8, 1.5, 2.5, 4.0, 7.0, 12.0, 25.0. None of these produce `payout == wager` (i.e., multiplier of 1.0) given `payout = int(wager * mult)`. The push branch is therefore unreachable except in the degenerate case where `wager=0`. BUT for `wager=1`, `int(1 * 0.8) = 0` (not push). This branch is dead code.
**Vulnerability:** Dead code suggests the author intended a 1.0x tier to exist but removed it. If it's added back later, the dead branch will silently fire but `process_wager` may not handle "push" correctly.
**Impact:** Confused future maintainer.
**Fix:** Remove the dead branch, or add a `0.86: 1.0` tier explicitly.

### WARNING #4: No balance re-read between animation start and `process_wager` settlement

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:224,250-259`
**Confidence:** 0.70
**Risk:** Line 224 reads `bal = await get_balance(...)` for the animation frame renders. Lines 250-259 call `process_wager` which internally determines the new balance. Between the two reads, the user's balance could change (e.g., an admin payout, a stipend credit). The animation shows a stale balance during the 2.7-second spin, then jumps to the new balance in the final frame. For most sessions this is fine, but during a live admin adjustment it creates visible ghost balance drift.
**Vulnerability:** TOCTOU on the animation balance read.
**Impact:** Player sees a balance that disagrees with reality for ~3 seconds; final frame reconciles. Low impact but confusing.
**Fix:** Cache the post-debit balance from `deduct_wager`'s return value and use it throughout the animation.

### WARNING #5: Daily scratch `claim_scratch(uid, reward=base_total)` has a race with `can_claim_scratch`

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:402-427`
**Confidence:** 0.65
**Risk:** Line 402 checks `can_claim_scratch(uid)` — pre-check. Line 427 calls `claim_scratch(uid, reward=base_total)` which internally re-checks (casino_db.py:1207) and returns None if already claimed. The cog-side pre-check reduces unnecessary tile-generation work but has no effect on correctness. However, if `claim_scratch` returns None on line 428, the pre-computed `tiles` and `base_total` are thrown away — and the player has been shown a "generating card..." defer state for nothing.
**Vulnerability:** Low-impact race window.
**Impact:** Rare "already claimed" error after the defer. Harmless.
**Fix:** Remove the pre-check on line 402 (it is redundant with the re-check inside `claim_scratch`).

### WARNING #6: `ScratchView` has no ownership check on the button until first click, and no per-user lock

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:357-393`
**Confidence:** 0.50
**Risk:** The `scratch` button callback on line 370 checks `interaction.user.id != self.discord_id` and returns an ephemeral error. That is correct. But `self.revealed += 1` on line 376 is not atomic with the render call on line 383. Two rapid clicks (which are rare but possible on mobile) could both pass the ownership check and both increment — `revealed` goes 0→1→2 on button clicks 1 and 2, skipping frame 1's render because the second increment happens before the first edit_message finishes.
**Vulnerability:** Double-click double-increment.
**Impact:** User sees frame 2 instead of frame 1 on a fast double-click. Cosmetic.
**Fix:** Add a click-lock flag similar to `play_again.py` `_used` pattern.

### OBSERVATION #1: `assert` statements used for invariant checks at module load

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:90-99`
**Confidence:** 0.85
**Risk:** `assert` is used to verify RTP table totals and symbol weight sums. In Python, `assert` is stripped when `-O` is passed to the interpreter. If production uses `python -O bot.py`, these invariants are NOT checked at startup, and any drift goes unnoticed until players complain about RTP.
**Vulnerability:** Disabled in optimized mode.
**Impact:** Silent RTP drift in production.
**Fix:** Use `if sum != expected: raise RuntimeError(...)` instead of `assert`.

### OBSERVATION #2: RTP analysis is not independently verifiable from the code

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:58-88`
**Confidence:** 0.60
**Risk:** The comment claims "Total EV = 0.962 → house edge ~3.8%". Let me verify: summing `(P_n - P_{n-1}) * multiplier_n` for SLOTS_OUTCOME_TABLE: (0.35)*0 + (0.10)*0 + (0.20)*0.3 + (0.13)*0.8 + (0.08)*1.5 + (0.055)*2.5 + (0.04)*4.0 + (0.025)*7.0 + (0.015)*12.0 + (0.005)*25.0 = 0 + 0 + 0.06 + 0.104 + 0.12 + 0.1375 + 0.16 + 0.175 + 0.18 + 0.125 = **1.0615**. That is a 106.15% RTP — i.e. the house LOSES 6.15% per spin on average, not gains 3.8%. The comment is wrong.
**Vulnerability:** This is a large financial claim with no test. If the table is player-favored, the house bank drains on every spin.
**Impact:** Long-term house bank depletion at about $61 loss per $1000 wagered. This is a CRITICAL economic bug masquerading as an observation. UPGRADE SEVERITY.
**Fix:** Recompute the table from scratch. For a 3.8% edge, you want EV = 0.962. Adjust the probability mass to achieve it and add a unit test that asserts `abs(ev - 0.962) < 0.001`.

**NOTE: Re-categorizing this as CRITICAL — the comment misrepresents the actual RTP.** The reviewer should escalate this to the author immediately. This finding overlaps with WARNING #3 (dead push branch), suggesting the table was refactored incorrectly.

### OBSERVATION #3: Same RTP concern for FREE_SPIN_OUTCOME_TABLE

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:77-88`
**Confidence:** 0.55
**Risk:** Computing EV for the free spin table: (0.25)*0 + (0.10)*0 + (0.20)*0.3 + (0.13)*0.8 + (0.10)*1.5 + (0.08)*2.5 + (0.06)*4.0 + (0.04)*7.0 + (0.03)*12.0 + (0.01)*25.0 = 0 + 0 + 0.06 + 0.104 + 0.15 + 0.20 + 0.24 + 0.28 + 0.36 + 0.25 = **1.644**. Free spin has a 164% RTP — which is fine because the player paid nothing, but the house cost per free spin is $1.64 per dollar of the TRIGGERING wager. If free spins trigger on ~8.5% of spins (per the comment on line 74), and the triggering spin averages $100 wager, the house pays ~$14/spin in free spins on top of the base spin payout. Combined with the already-negative base RTP from the previous finding, the slot is almost certainly player-favored overall.
**Vulnerability:** Economic design bug stacks with the base table bug.
**Impact:** Compounds the financial bleed.
**Fix:** Rebuild both tables with documented EV math and add unit tests.

### OBSERVATION #4: `_generate_reels_for_outcome` can produce visually-misleading outputs

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:114-167`
**Confidence:** 0.40
**Risk:** For `visual_type == "2match"`, the function generates `[base, "wild", third]` with 15% probability. A wild symbol counts as a wild in the OUTCOME TABLE but here it is just rendered. The player may see `[Shield, Wild, Coin]` and believe they hit a partial jackpot when the actual tier was "2match_low" paying 0.3x. Mismatch between visual and payout.
**Vulnerability:** Visual deception. Not a bug but a product-design concern.
**Impact:** Player confusion and frustration.
**Fix:** Either align wild handling with actual multi-line payout math, or remove the wild-in-2match render logic.

### OBSERVATION #5: `asyncio.sleep(0.9) * 3 = 2.7s` holds up the page pool

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:236-240`
**Confidence:** 0.50
**Risk:** Each animation frame calls `render_slots_card(...)` which acquires a Playwright page from the pool. With `pool size=4` and 3 frames per spin, 4 concurrent spinners will saturate the pool for ~2.7 seconds. This is not a leak — pages are released properly — but it is a throughput ceiling.
**Vulnerability:** Limited concurrency.
**Impact:** Under high slots load, spins queue up and the pool error "Render pool exhausted" can surface.
**Fix:** Either pre-render all frames into memory and release the pool between, or increase the pool size.

### OBSERVATION #6: `asyncio.sleep(1.5)` between regular spin and free spin contributes to the same throughput issue

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/slots.py:305`
**Confidence:** 0.40
**Risk:** Free-spin-triggering spins hold resources for an extra 1.5s + free spin render. Compounds the pool pressure issue.
**Vulnerability:** Same as #5.
**Impact:** Marginal.
**Fix:** Consider fire-and-forget free spin render.

## Cross-cutting Notes

**MOST IMPORTANT**: The RTP math in `OBSERVATION #2` is almost certainly wrong — the slot is paying out more than 100% per spin, which drains the house bank. This should be escalated to CRITICAL and verified against live production data (check `get_house_report` for slots P&L over the last 7 days — if it is negative, this is confirmed). The file-under-review says "3.8% edge" in the comments but the computed EV from the table is +6.15% player advantage.

The idempotency and RNG concerns mirror `coinflip.py` exactly — both games need the same wrapper-level fix in `casino_db.py:deduct_wager` to forward `correlation_id` as `reference_key`, plus migration to `secrets.SystemRandom` for all outcome rolls.
