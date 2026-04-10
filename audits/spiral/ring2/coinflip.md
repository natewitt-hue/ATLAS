# Adversarial Review: coinflip.py

**Verdict:** block
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 399
**Reviewer:** Claude (delegated subagent)
**Total findings:** 14 (3 critical, 6 warnings, 5 observations)

## Summary

Financial logic using non-cryptographic `random.random()` and `random.choice()` for outcomes and side-assignment on real-money wagers. The debit path goes through `deduct_wager` → `flow_wallet.debit` WITHOUT `reference_key`, so Discord's interaction-retry behavior can produce double-debits on any button click. The PvP challenge has an unstoppable timeout-race where a button click and a 5-minute timeout can both fire and double-refund the challenger. Several error paths silently refund without reversing the wager-registry entry.

## Findings

### CRITICAL #1: `random.random()` is used for real-money coin flip outcomes

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:21,92,99,213`
**Confidence:** 0.90
**Risk:** The solo flip uses `random.choice(["heads", "tails"])` on line 92, and the PvP flip uses `random.choice(["Heads 🌕", "Tails 🌑"])` on line 213. `random.random()` and `random.choice()` use Python's Mersenne Twister PRNG, which is NOT cryptographically secure. The PRNG state is seeded from OS entropy at startup but is predictable after observing ~624 samples. An adversary who can observe enough game outcomes in the same bot process could in principle predict future flips.
**Vulnerability:** Per `CLAUDE.md`'s focus block, financial RNG should use `secrets.SystemRandom()` or Python's `secrets` module. The file imports `random` at line 21 with no alternative. `SOLO_PAYOUT_MULT = 1.95` (2.5% edge) means a predicted flip gives the attacker a 1.95x guaranteed payout.
**Impact:** If combined with a way to observe enough past outcomes (via #ledger channel scraping), adversarial users could in theory reconstruct the PRNG state and win every flip. Even if impractical in Discord's rate-limited environment, the attack surface is real and violates standard casino RNG practice.
**Fix:** Replace `random.choice` with `secrets.choice` at both call sites. Add a module-level `_rng = secrets.SystemRandom()` and route all outcome-determining calls through it. Keep `random` for non-financial visual effects only.

### CRITICAL #2: `deduct_wager` called on lines 88 and 200 without end-to-end `reference_key`

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:86-90,198-205`
**Confidence:** 0.95
**Risk:** Line 88 calls `deduct_wager(uid, wager, correlation_id=correlation_id)`. Inside `casino_db.py:1046-1086`, `deduct_wager` calls `flow_wallet.debit(...)` and passes `subsystem_id=correlation_id` but NOT `reference_key=correlation_id`. `flow_wallet._check_idempotent` uses the `reference_key` column, NOT `subsystem_id`, so the debit is NOT idempotent. Line 200 is the PvP opponent-side debit and has the identical problem.
**Vulnerability:** Per `CLAUDE.md`: "ALL debit calls MUST pass `reference_key`. Without it, Discord interaction retries or button double-clicks cause double-debits." The solo flip generates `correlation_id = uuid.uuid4().hex[:8]` per invocation — so two retries of the SAME interaction produce DIFFERENT correlation ids and thus TWO separate debits.
**Impact:** Under Discord's interaction-retry contract (3s no-ack → retry), or during any network hiccup, users can lose 2x their wager on a single click. Financial ledger corruption; house bank P&L reports become inaccurate; wager registry drifts from balance.
**Fix:** Either (a) derive `reference_key` from `interaction.id` (stable across Discord retries) and pass it through `deduct_wager` into `flow_wallet.debit`, or (b) fix `deduct_wager` itself to forward `correlation_id` as `reference_key` to `flow_wallet.debit`. Option (b) is the single-point fix; option (a) is the stronger "know your interaction" fix.

### CRITICAL #3: PvP timeout race can double-refund the challenger

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:274-323`
**Confidence:** 0.80
**Risk:** `decline_btn` sets `self.resolved = True` on line 285, then awaits `decline_challenge` and `refund_wager`. `on_timeout` on line 303 checks `if not self.resolved:` and then sets `self.resolved = True` on line 306, also awaits `decline_challenge` and `refund_wager`. These two code paths share the `self.resolved` flag, but the check-and-set in `on_timeout` is NOT atomic with the awaits that follow. If the user clicks "Decline" at T=4:59:999 and the timeout fires at T=5:00:000, both callbacks can see `resolved=False`, both set it to True, and both refund.
**Vulnerability:** The flag is checked but never under a lock. `discord.ui.View` does not serialize callbacks with `on_timeout`. `refund_wager(self.challenger_id, self.wager)` on line 290 and line 309 are both fire-and-forget credit calls — and `refund_wager` in `casino_db.py:1089` does NOT pass `reference_key` to `flow_wallet.credit`, so both refunds will succeed independently.
**Impact:** Challenger receives 2x their wager back, i.e. they get the wager they paid plus a duplicate from the house. The house bank is short by `wager` per occurrence.
**Fix:** Wrap the resolved-check and decline-work in an `asyncio.Lock()` held on the view. Pass `reference_key=f"coinflip_decline_{challenge_id}"` to `refund_wager` so duplicate calls idempotent-no-op. Ideally both.

### WARNING #1: `refund_wager` on line 290,309 is missing a `correlation_id` argument

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:290,309`
**Confidence:** 0.90
**Risk:** `refund_wager(self.challenger_id, self.wager)` does not pass `correlation_id=self.challenger_correlation_id`. In `casino_db.py:1089`, `refund_wager` needs `correlation_id` to call `wager_registry.settle_wager("CASINO", correlation_id, "voided", 0)` — without it, the original wager in the registry stays `status='open'` forever. The house P&L report (which reads `status != 'open'` from the wagers table) will therefore NOT count this refund, producing ghost wagers.
**Vulnerability:** Silent data drift. The registry entry for the declined/timed-out challenge is orphaned.
**Impact:** House bank report over-counts the house P&L, wager_registry grows with ghost rows, `/casino houseboard` (if it exists) shows inflated profit.
**Fix:** Pass `correlation_id=self.challenger_correlation_id` to both `refund_wager` calls.

### WARNING #2: PvP accept flow has unreachable rollback path on `deduct_wager` failure

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:194-209`
**Confidence:** 0.85
**Risk:** Line 195 sets `self.resolved = True` BEFORE the opponent's debit is attempted. If the debit fails on line 200-201, the code sets `self.resolved = False` on line 202 and returns an error. BUT — during the window between line 195 and line 202, if the challenger's view also times out, `on_timeout` will see `resolved=True` and refuse to refund the challenger. Result: opponent fails to accept, challenger's wager stays debited forever because the timeout shortcut was closed.
**Vulnerability:** TOCTOU inversion — the "prevent double-accept" code creates a new "lose wager on opponent-InsufficientFunds" bug.
**Impact:** Challenger loses their entire wager if the opponent tries to accept but fails insufficient-funds at the wrong moment.
**Fix:** Don't set `self.resolved = True` until AFTER a successful debit. Alternatively, also don't set it to False on debit failure — instead, re-attempt the debit or leave the challenge open.

### WARNING #3: TOCTOU on `active_challenges.pop(self.challenge_id, None)`

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:210,287,307`
**Confidence:** 0.70
**Risk:** `active_challenges` is a module-level dict with no lock. Three separate callbacks can mutate it concurrently. In CPython, dict mutations are thread-safe per-operation but the sequence `check → mutate` across callbacks is not atomic.
**Vulnerability:** Race condition window. Not a frequent bug under Discord's effectively-serialized view callbacks, but is a lurking concern during reconciliation or admin cleanup.
**Impact:** Orphaned entries in `active_challenges` that never get cleaned up.
**Fix:** Add a module-level `asyncio.Lock()` around mutations, or accept that this is best-effort bookkeeping only.

### WARNING #4: `process_wager` on free-spin line 315-324 passes `correlation_id=None`

**Location:** (slots.py, referenced via import structure — this is about how coinflip's `process_wager` on line 101 relates) `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:101-110`
**Confidence:** 0.60
**Risk:** `process_wager` receives `correlation_id=correlation_id` here which is correct. But `process_wager` itself (casino_db.py:849) needs to forward this to a `flow_wallet.credit` for the payout, and if it doesn't do so with a `reference_key`, the payout is non-idempotent. I cannot verify from this file alone, but the chain of custody for the idempotency key is fragile across the `process_wager` boundary.
**Vulnerability:** Opaque settlement path. The cog trusts `process_wager` to handle idempotency correctly, but there is no documentation or test guaranteeing it.
**Impact:** Potential double-credit on payout.
**Fix:** Audit `process_wager` in casino_db.py line 849 to confirm the payout credit uses `reference_key`.

### WARNING #5: Insufficient funds error path on line 89 leaves `interaction` in ambiguous state

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:58,87-90`
**Confidence:** 0.65
**Risk:** Line 58 does `await interaction.response.defer()`, which consumes the response slot. Line 87-90 catches the `deduct_wager` exception and calls `interaction.followup.send(...)`. That works for the solo flip. BUT, line 339 (send_challenge PvP path) does NOT defer, and line 371 catches the same exception with `interaction.response.send_message`. If the PvP path is invoked from a command that is already deferred elsewhere, the response.send_message will raise `InteractionResponded`. The two call sites are inconsistent.
**Vulnerability:** Deferred vs. not-deferred inconsistency between solo and PvP entry points.
**Impact:** PvP failures may surface as `InteractionResponded` instead of the user-visible error message.
**Fix:** Standardize: defer first, then all error paths use `followup.send(ephemeral=True)`.

### WARNING #6: Challenger is hardcoded as "heads" — opponent has no choice

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:215-216,241`
**Confidence:** 0.85
**Risk:** `winner_id = self.challenger_id if "Heads" in result_side else self.opponent_id`. The game is "fair" but the challenger has a psychological fixed side — if the challenger and opponent both believe the flip is biased (even subconsciously) toward heads, the challenger is perceived as advantaged. Also, the comment says "Challenger is always heads" but `result_side = random.choice(["Heads 🌕", "Tails 🌑"])` happens AFTER the challenge is created. A future refactor where someone adds "opponent picks their side" and forgets to rewire this logic would silently assign wins to the wrong player.
**Vulnerability:** Implicit game rule that is never surfaced in the UI.
**Impact:** Minor fairness perception; refactor hazard.
**Fix:** Let the opponent pick a side on accept, or surface "CHALLENGER: Heads / OPPONENT: Tails" prominently in the challenge embed.

### OBSERVATION #1: `asyncio` import unused at module level

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:18`
**Confidence:** 0.40
**Risk:** `import asyncio` on line 18 is unused in this file. Dead code.
**Vulnerability:** Clutter.
**Impact:** None functional.
**Fix:** Remove.

### OBSERVATION #2: `datetime` imports unused

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:23`
**Confidence:** 0.40
**Risk:** `from datetime import datetime, timezone` is imported but never used in this file.
**Vulnerability:** Dead import.
**Impact:** None.
**Fix:** Remove.

### OBSERVATION #3: PvP profit math has rounding drift

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:227,265`
**Confidence:** 0.60
**Risk:** `int(wager * 1.9)` on line 390 (challenge embed) shows the winner's payout as `int(wager * 1.9)`. The actual payout comes from `resolve_challenge` on line 218 — which may compute differently. No guarantee those two numbers match. For a $100 wager, both give $190, but for a $53 wager, the embed shows $100 but resolve_challenge may show $101 or $100 depending on internal rounding.
**Vulnerability:** Display drift.
**Impact:** User-visible confusion.
**Fix:** Use a single helper `coinflip_pvp_payout(wager) -> int` in both places.

### OBSERVATION #4: `is_casino_open` is checked BEFORE `interaction.response.defer()`

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:58,62-65`
**Confidence:** 0.35
**Risk:** The defer happens first, then `is_casino_open` is checked, and the error is returned as `followup.send(..., ephemeral=True)`. This is correct. BUT `send_challenge` (line 330-) does NOT defer before checking, so the equivalent check fires synchronously and calls `response.send_message`. The two paths are inconsistent in their deferral choice.
**Vulnerability:** Stylistic inconsistency.
**Impact:** None functional, but confusing to maintain.
**Fix:** Pick one convention and stick to it.

### OBSERVATION #5: Challenger display name fallback uses literal "User <snowflake>"

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/games/coinflip.py:230-233`
**Confidence:** 0.30
**Risk:** If `interaction.guild.get_member` returns None (user left the guild between challenge creation and accept), the fallback is `f"User {self.challenger_id}"` which exposes the raw snowflake. This is a small privacy leak.
**Vulnerability:** Raw Discord IDs exposed to channel viewers.
**Impact:** Minor — most users don't know what the number means, but it is technically PII-adjacent.
**Fix:** Use "Unknown Player" instead of the snowflake.

## Cross-cutting Notes

The core idempotency gap (`deduct_wager` → `flow_wallet.debit` without `reference_key`) is a repository-wide vulnerability. Every casino game inherits it. Fixing it requires EITHER (a) fixing the wrapper in `casino_db.py:1046-1086` to forward `correlation_id` as `reference_key`, OR (b) fixing every call site to derive a stable `reference_key` from `interaction.id`. Option (a) is the cheap fix but (b) is the correct fix — interaction retries by Discord produce the same `interaction.id`, whereas each new `uuid.uuid4()` is by definition NOT deduplication-safe for retry scenarios.

The `random` vs `secrets` concern applies to `slots.py`, `crash.py`, and `scratch.py` as well — this is a casino-wide gap.
