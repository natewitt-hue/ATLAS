# Adversarial Review: casino/games/blackjack.py

**Verdict:** block
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 710
**Reviewer:** Claude (delegated subagent)
**Total findings:** 24 (6 critical, 10 warnings, 8 observations)

## Summary

Multiple financial-ledger bugs make this file unsafe to ship unchanged: in-memory session state guarantees wager loss on restart, split-hand logic silently auto-stands the second hand whenever the first busts, and button callbacks never defer before multi-second DB/render pipelines. A non-cryptographic PRNG shuffles a real-money shoe, and `on_timeout` has no concurrency lock against in-flight Hit/Stand callbacks. Block until at least the six criticals are addressed.

## Findings

### CRITICAL #1: Second split hand is never played when first hand busts
**Location:** `casino/games/blackjack.py:285-300, 415-423`
**Confidence:** 0.95
**Risk:** If a player splits, then draws and busts on the first hand, `HitButton.callback` calls `_finish_hand` directly. `_finish_hand` calls `session.dealer_play()` and then resolves BOTH hands — but the second split hand (`split_hand`) still only holds its initial two cards. The player is silently denied the opportunity to hit, stand, or double on their second hand.
**Vulnerability:** `HitButton.callback` line 296 reads `if score >= 21: await _finish_hand(...)`. It does not check `session.split_active and not session.playing_split` to transition to the second hand. The finish path unconditionally resolves both outcomes. The second hand's two-card starting total is the only value ever evaluated against the dealer's completed total.
**Impact:** Direct financial loss for any user who splits and busts the first hand — the second hand's wager is adjudicated on a hand the player never got to play. User-visible, silent, and affects real Flow balances.
**Fix:** In `HitButton.callback`, when `score > 21` (bust) AND `session.split_active and not session.playing_split`, transition to second hand instead of finishing: set `session.playing_split = True`, rebuild buttons, edit the message. Only call `_finish_hand` after the second hand is resolved by the user.

### CRITICAL #2: Session state lives only in-memory — bot restart silently eats wagers
**Location:** `casino/games/blackjack.py:41, 593-610`
**Confidence:** 1.00
**Risk:** `active_sessions: dict[int, BlackjackSession]` is a module-level Python dict. A bot restart, crash, redeploy, or unhandled exception in any part of the process wipes every in-flight hand. The initial `deduct_wager` has already committed to the DB, but there is no resume or refund path.
**Vulnerability:** The sentinel at line 593 (`active_sessions[uid] = "PENDING"`) plus the real session at 610 are never persisted. No startup reconciliation loop inspects `wagers` for `status='open'` CASINO rows and refunds/resumes them.
**Impact:** Financial ledger corruption on every deploy. Users who were mid-hand permanently lose the wager. Auditor-visible in `wagers` as stranded `status='open'` rows with no matching session.
**Fix:** Either (a) persist `BlackjackSession` to SQLite at each state transition and reload on startup; or (b) on bot startup, query `wager_registry` for any CASINO wagers with `status='open'` and no corresponding settled `casino_sessions` row, and refund them via `refund_wager`. Option (b) is simpler and sufficient.

### CRITICAL #3: Non-cryptographic PRNG for real-money card shuffle
**Location:** `casino/games/blackjack.py:22, 54-57`
**Confidence:** 0.90
**Risk:** `random.shuffle(shoe)` uses Python's `random` module (Mersenne Twister), which is seeded deterministically and is not suitable for gambling. State can be recovered from ~624 observed outputs, and any module that reseeds `random` (tests, diagnostics, importing a module that calls `random.seed(...)` at load) will alter the shoe distribution globally.
**Vulnerability:** `import random` at line 22, direct `random.shuffle(shoe)` at line 56. No use of `secrets.SystemRandom()` anywhere in the shuffle or any deal/hit code path. Compare to `casino/casino_db.py` which does `import secrets` for other purposes — cryptographic RNG is available but not used for the shoe.
**Impact:** A sophisticated user who observes enough drawn cards can in principle predict future cards. Even absent a full exploit, this is a real-money surface that should not be on a non-cryptographic PRNG. Also creates cross-game state coupling because `random` is a shared singleton.
**Fix:** Use a per-session `secrets.SystemRandom()` instance. Replace `random.shuffle(shoe)` with `secrets.SystemRandom().shuffle(shoe)`, or instantiate `_rng = secrets.SystemRandom()` at module level and call `_rng.shuffle(shoe)`.

### CRITICAL #4: Double-down and Split re-use the original correlation_id, breaking the wager ledger
**Location:** `casino/games/blackjack.py:335-344, 360-370`
**Confidence:** 0.85
**Risk:** Both `DoubleButton.callback` and `SplitButton.callback` call `deduct_wager(session.discord_id, session.wager, correlation_id=session.correlation_id)` using the SAME `correlation_id` as the original bet. `casino_db.deduct_wager` at lines 1076-1081 calls `wager_registry.register_wager("CASINO", correlation_id, ...)` which is an `INSERT OR IGNORE` on `UNIQUE(subsystem, subsystem_id)`. The second insert is silently ignored. The wagers table keeps the ORIGINAL `wager_amount`, not the doubled amount.
**Vulnerability:** The `wagers` registry is the source of truth for house P&L calculations in `casino_db.get_house_report` (line 1122: `-SUM(COALESCE(result_amount, 0))`). After a double-down, the ledger shows the original stake, but the transactions table records two debits and a larger credit, producing a mismatch between `wagers.result_amount` and actual transaction-level P&L. Also the transaction-level `subsystem_id` UPDATE at `process_wager` line 989-992 relabels BOTH debit rows with the same session_id, destroying the ability to distinguish original from double.
**Impact:** Silent accounting corruption. House P&L reports, audit tooling, and any "refund my stranded wager" reconciliation job will see the original stake only. Potentially exploitable for parity abuse (e.g., double, win, house shows only 1x as the "wager of record").
**Fix:** Generate a new `correlation_id` for the double-down / split debit (e.g., `f"{session.correlation_id}_double"`, `f"{session.correlation_id}_split"`), or pass a distinct composite key. Ensure `wager_registry.register_wager` creates a separate ledger row for each additional stake. Alternatively, `process_wager` needs to be refactored to accept the total-wagered amount and update the original wager row's `wager_amount`.

### CRITICAL #5: Button callbacks invoke multi-second DB/render pipeline without `defer()`
**Location:** `casino/games/blackjack.py:285-300, 307-320, 327-345, 352-373, 406-527`
**Confidence:** 0.90
**Risk:** `HitButton`, `StandButton`, `DoubleButton`, `SplitButton` callbacks all eventually call either `_update_table_message` or `_finish_hand`. `_finish_hand` executes `process_wager` (BEGIN IMMEDIATE transaction, streak/jackpot logic, 3-5 DB round-trips), `check_achievements`, `post_to_ledger`, `render_blackjack_card` (Playwright HTML→PNG, ~0.5-2s), and then `interaction.response.edit_message(...)`. Discord interactions must receive an initial response within 3 seconds, and the 15-minute followup window only opens AFTER an initial response. If the pipeline exceeds 3s, `edit_message` raises `NotFound` / `InteractionResponded` and the click appears dead.
**Vulnerability:** No `await interaction.response.defer()` before the pipeline. Compare to `start_blackjack` line 564 which DOES defer — the button callbacks do not. Under load (Playwright page pool contention, DB locks, first-launch cold-start), this will miss the 3s window.
**Impact:** User clicks Hit/Double/Split, nothing visible happens, the hand is orphaned in `active_sessions` until the 5-minute timeout fires. Wager is held but user experiences a dead UI.
**Fix:** First statement of each callback should be `await interaction.response.defer()`, then all subsequent renders switch from `interaction.response.edit_message(...)` to `interaction.edit_original_response(...)`.

### CRITICAL #6: `on_timeout` has no concurrency lock against in-flight button callbacks
**Location:** `casino/games/blackjack.py:227-278`
**Confidence:** 0.85
**Risk:** `BlackjackView.on_timeout` checks `if not s.done and s.discord_id in active_sessions` and then mutates `s.done`, `s.dealer_hand`, `s.playing_split`, calls `process_wager`, and pops the session. There is NO `asyncio.Lock` on the session. A Hit callback can be concurrently processing when the 5-minute timeout fires: both paths read `s.done=False`, both call `dealer_play`, both call `process_wager`. The original bet's `reference_key` is not reused — two settlements occur.
**Vulnerability:** `discord.ui.View.on_timeout` is scheduled by discord.py's timeout task, independent of the interaction queue. There is no view-level or session-level lock. The check-then-act pattern (`if not s.done: s.done = True`) is not atomic across await boundaries.
**Impact:** Financial: double-settlement of the same hand. `process_wager` credits payout twice, debits house P&L twice. Even if the `wager_registry.settle_wager` UPDATE is idempotent for the transition, the `flow_wallet.credit` calls have two different `reference_key` values (NONE in finish path line 443, synthesized in casino_db `process_wager` line 935) so neither blocks the other.
**Fix:** Add an `asyncio.Lock` to `BlackjackSession`. Every callback and `on_timeout` must `async with session.lock:` before checking `session.done` and mutating state. Alternatively, use `asyncio.Lock.locked()` + early-return pattern.

### WARNING #1: Shoe exhaustion during play raises IndexError — no bounds check on `shoe.pop()`
**Location:** `casino/games/blackjack.py:134-140, 146-149, 151-166, 367-369`
**Confidence:** 0.85
**Risk:** `deal()` reshuffles only AFTER the initial 4-card deal, and only on the `< 30% remaining` threshold. During a long split + double + multi-hit sequence, `self.shoe.pop()` is called repeatedly with no length check. If the reshuffle window is narrow or `_build_shoe`'s result lands below 30%, the shoe can empty mid-hand. `list.pop()` on empty raises `IndexError`, which is not caught in the callback, leaving the user with a hung interaction and a deducted wager.
**Vulnerability:** No `if not self.shoe: self.shoe = _build_shoe()` guard in `hit()`, `dealer_play()`, or the split code. `_FULL_SHOE_SIZE * 0.3 = 93.6` remaining cards is a huge margin in practice, but the check only fires inside `deal()`, not at every draw.
**Impact:** Edge-case hand crash. Wager deducted, hand hung until timeout, error lost to the print-based error logging.
**Fix:** Add `if len(self.shoe) < 10: self.shoe = _build_shoe()` at the start of `hit()`, inside `dealer_play()` loop, and before each `shoe.pop()` in split.

### WARNING #2: Session sentinel is a string in a dict typed for `BlackjackSession`
**Location:** `casino/games/blackjack.py:41, 593`
**Confidence:** 0.95
**Risk:** `active_sessions` is typed `dict[int, "BlackjackSession"]` but line 593 assigns `active_sessions[uid] = "PENDING"`. Any consumer that iterates `active_sessions` or reads `active_sessions[uid]` expecting a `BlackjackSession` will AttributeError on a str. Because the sentinel window covers two awaits (`deduct_wager` and the constructor), a concurrent code path that touches the dict sees the string.
**Vulnerability:** No type narrowing, no isinstance check anywhere that reads the dict. The sentinel is a sentinel string inside a type-hinted mapping — static checkers won't catch it, runtime will.
**Impact:** AttributeError on any diagnostic or concurrent read. The file itself doesn't iterate the dict but any future admin command that lists active sessions will crash.
**Fix:** Use a second `set[int]` for pending reservations, or use `None` as the sentinel and change the type hint to `dict[int, BlackjackSession | None]`, or use `asyncio.Lock` keyed by uid instead of a sentinel.

### WARNING #3: TOCTOU race between `if uid in active_sessions` and sentinel set
**Location:** `casino/games/blackjack.py:568-593`
**Confidence:** 0.85
**Risk:** Line 568 checks `if uid in active_sessions`, then awaits `is_casino_open`, `get_channel_id`, `get_max_bet` before setting the sentinel at line 593. Two concurrent invocations can both pass line 568, both execute the awaits, then both arrive at line 593. The comment on line 592 says "Set sentinel BEFORE any await to prevent TOCTOU double-session" — but in fact there ARE awaits before the sentinel is set.
**Vulnerability:** The check and the set are separated by ~4 await points. `active_sessions` is a plain dict with no lock.
**Impact:** A user who rapid-fires two `/blackjack` commands before the first one completes deduct_wager can end up with two simultaneous hands, two deducted wagers, and one session that gets overwritten in the dict. The overwritten session leaks its wager.
**Fix:** Move the `if uid in active_sessions` check to immediately before the sentinel set, OR acquire a per-user `asyncio.Lock` before the check.

### WARNING #4: `on_timeout` silent-swallows exceptions via `print` instead of `log.exception`
**Location:** `casino/games/blackjack.py:269-276`
**Confidence:** 0.95
**Risk:** The `except Exception as e: print(...)` at line 269 and the nested `except Exception as refund_err: print(...)` at 275 use `print` statements, not a logger with traceback. This is a user-facing admin / financial path (timeout wager resolution). Per CLAUDE.md Flow Economy Gotchas: "Silent `except Exception: pass` in admin-facing views is prohibited. Always `log.exception(...)`."
**Vulnerability:** `print` goes to stdout with no traceback, no log level, no timestamp from the standard logger, and no chance of being picked up by log-scraping / alerting. The refund failure in particular is a CRITICAL accounting event that drops into stdout.
**Impact:** Financial incidents (refund failures during timeout) are invisible to operations. No alerting, no post-incident forensic trail.
**Fix:** `import logging; log = logging.getLogger("casino.blackjack")`. Replace prints with `log.exception(...)` to capture tracebacks. For the refund failure, also alert to `ADMIN_CHANNEL_ID`.

### WARNING #5: `_finish_hand` calls `edit_message` AFTER long-running awaits — second interaction's response already consumed
**Location:** `casino/games/blackjack.py:443-527`
**Confidence:** 0.75
**Risk:** `_finish_hand` runs: `process_wager` (BEGIN IMMEDIATE, 5+ queries) → `check_achievements` → `post_to_ledger` → `get_max_bet` → `render_blackjack_card` → `interaction.response.edit_message(...)` — all on the SAME interaction. The interaction.response is only valid for 3s from interaction creation. `edit_message` on an un-deferred response that has exceeded 3s raises.
**Vulnerability:** Same root cause as CRITICAL #5, noted separately because the fix requires swapping `interaction.response.edit_message` for `interaction.edit_original_response` AFTER the defer.
**Impact:** Under normal latency (< 3s) it works; under any slowdown (Playwright page pool starved, DB lock contention, render queue), the final edit silently fails and the cinematic result screen followup at line 547 is also unreachable.
**Fix:** Defer at start of every button callback, then use `interaction.followup.edit_message(...)` / `interaction.edit_original_response(...)` at the end.

### WARNING #6: Immediate-blackjack path drops `active_sessions[uid]` before processing — reentry race
**Location:** `casino/games/blackjack.py:614-617`
**Confidence:** 0.75
**Risk:** Line 617 `active_sessions.pop(uid, None)` fires BEFORE `process_wager`, `check_achievements`, `post_to_ledger`, and the render pipeline. During those ~1-2 seconds, the user can re-trigger `/blackjack`, pass the `uid in active_sessions` guard (because it was popped), and start a second hand while the first is still being settled.
**Vulnerability:** Line 617 is optimistic cleanup; the pop belongs AFTER the entire settlement chain, not before.
**Impact:** User can stack multiple hands during render/process latency. Each deducts the original bet separately. Rapid clicks = multiple wagers.
**Fix:** Move `active_sessions.pop(uid, None)` to after the last `followup.send` / `replay_message.edit`.

### WARNING #7: `_finish_hand` pops session mid-flow, same race as #6
**Location:** `casino/games/blackjack.py:493`
**Confidence:** 0.70
**Risk:** Line 493 `active_sessions.pop(session.discord_id, None)` fires after `process_wager` but BEFORE the final render and `edit_message`. A re-entrant `/blackjack` invocation between line 493 and line 527 can start a new session, then the in-flight edit at 527 lands on the wrong interaction context if the user managed to trigger something else. Smaller window than #6, same class of bug.
**Vulnerability:** Cleanup is done before the visible state is updated.
**Impact:** Small but real race; also a logical ordering smell.
**Fix:** Pop after `interaction.response.edit_message(...)` completes.

### WARNING #8: `_detect_near_miss` bust-by-one check fires on all hard 22+ → miscategorizes
**Location:** `casino/games/blackjack.py:86-100`
**Confidence:** 0.65
**Risk:** `_hand_value` already demotes aces when `total > 21`. The `if p_score == 22` check at line 92 will fire only when the player's hard total is exactly 22 (e.g., [10,6,6] → 22). That's not truly "busted by ONE" — it's any hand that went bust with its first over-21 draw. Also, [10,10,A] = 21, so ace demotion paths can never produce 22, so the check is correct for aces-free busts but calls ANY hard 22 "bust by one" including [10,10,2].
**Vulnerability:** The message "Busted by ONE!" is a lie when the player hit from 12 into 22 with a 10 — they busted by ten (went from 12 to 22, but they weren't "close to 21"). The intent seems to be "you busted and your final total is 22", which is technically true but the UX message implies "you were close."
**Impact:** Misleading engagement messaging. Minor UX issue.
**Fix:** Use pre-bust hand value instead: check if `_hand_value(hand[:-1]) == 20` to detect "had 20, drew to 22" genuine near-bust.

### WARNING #9: Split blocks all blackjack detection, not just ace splits
**Location:** `casino/games/blackjack.py:176`
**Confidence:** 0.80
**Risk:** `p_bj = _is_blackjack(self.active_hand) and not self.split_active`. This makes ANY post-split hand of 21-in-2-cards a non-blackjack. Standard rules suppress blackjack only on split ACES (some casinos also include all splits, but it's casino-dependent). The codebase has no comment justifying which rule it follows.
**Vulnerability:** The rules comment at line 4-14 says "6:5 blackjack payout" and "Split: pairs only, one split allowed" but does not clarify post-split blackjack treatment.
**Impact:** Player splits 10s, draws an ace on one hand → treated as 21 win (2x), not blackjack (2.2x). Depending on how often this happens, this is a consistent small underpayment to the player who splits.
**Fix:** Document the rule explicitly in the module docstring, or change the check to `not (self.split_active and is_ace_split)`.

### WARNING #10: Double-down button is still shown after double's `deduct_wager` raises, leaving view in inconsistent state
**Location:** `casino/games/blackjack.py:327-345`
**Confidence:** 0.60
**Risk:** `DoubleButton.callback` wraps `deduct_wager` in `try/except Exception: return await interaction.response.send_message("Insufficient funds...")`. On failure, the view is not updated — the Double button remains visible, letting the user try again. That's fine except the `except Exception` is too broad: it swallows `ValueError` (tier limit exceeded) with the same "insufficient funds" message, and it swallows any unexpected error type (race, DB lock) with the same user-facing message.
**Vulnerability:** Blanket `except Exception` — no logging, no distinction between expected `InsufficientFundsError` and unexpected DB failure. The user sees "insufficient funds" when the real issue is a DB lock or an `aiosqlite` error.
**Impact:** Confusing UX, silent swallowing of real errors, and operations lose visibility on DB/casino_db bugs because they're hidden behind a friendly message.
**Fix:** Catch `InsufficientFundsError` and `ValueError` explicitly. Log other exceptions via `log.exception`. Re-raise unexpected errors.

### OBSERVATION #1: `int(wager * 1.2)` truncation on 6:5 blackjack payout
**Location:** `casino/games/blackjack.py:182`
**Confidence:** 0.80
**Risk:** `payout = wager + int(wager * BJ_PAYOUT_MULT)`. For wager=7, `int(7*1.2)=int(8.4)=8` so player receives 7+8=15. True 6:5 payout would be 7+(7*6/5)=7+8.4=15.4 → casinos typically round up to 16 on a hard ticket, but since we're integer-only, truncation always favors the house. On a wager of 1, `int(1*1.2)=1`, payout=2, which is effectively a 1:1 win rather than 6:5.
**Vulnerability:** Integer truncation without a floor/ceiling policy decision. The comment says "house edge ~3%" but the compounding truncation will push that slightly higher.
**Impact:** Tiny per-hand; aggregated across thousands of hands, measurable bias in favor of the house beyond the documented edge. Also creates weird UX where a 1 Buck blackjack pays 2 total.
**Fix:** Use `math.ceil(wager * BJ_PAYOUT_MULT)` for the profit, then add to wager. Document the rounding policy in a comment.

### OBSERVATION #2: `_update_buttons` leaks Split button after a Double when player_hand is still a pair
**Location:** `casino/games/blackjack.py:207-225`
**Confidence:** 0.65
**Risk:** `can_double = len(s.active_hand) == 2 and not s.doubled`. `can_split = _is_pair(s.player_hand) and not s.split_active and not s.playing_split`. After a double, `doubled=True` AND the hand has 3 cards, so `can_double` is False and splits check `_is_pair` of the 3-card hand — `_is_pair` requires `len == 2`, so returns False. Dead defense: can_split becomes False after double anyway. BUT: `_update_buttons` is called AFTER `_finish_hand` has cleared items at line 413. This line then calls clear, and the finish path itself calls `view.clear_items()` — double-clear is wasteful but harmless.
**Vulnerability:** `_update_buttons` is never called after double / finish, so the dead branch is unreachable. Still, there's ambiguity because `_update_buttons` is invoked from callbacks before they call `_finish_hand`.
**Impact:** None at runtime. Code smell around button lifecycle.
**Fix:** Add a `session.done` guard at the top of `_update_buttons`.

### OBSERVATION #3: `profit_str` computed and never used
**Location:** `casino/games/blackjack.py:490-491, 655-656`
**Confidence:** 1.00
**Risk:** Dead local variables.
**Vulnerability:** Refactoring leftover.
**Impact:** None.
**Fix:** Delete lines 490-491 and 655-656.

### OBSERVATION #4: `import asyncio as _asyncio` inside function when `asyncio` is already imported at module level
**Location:** `casino/games/blackjack.py:19, 531`
**Confidence:** 1.00
**Risk:** Dead re-import aliasing. `asyncio` is already imported at line 19. Line 531 does `import asyncio as _asyncio; await _asyncio.sleep(1.5)`. Just use `asyncio.sleep(1.5)`.
**Vulnerability:** Historical refactoring artifact; slight confusion for readers.
**Impact:** None.
**Fix:** Delete line 531 and use `await asyncio.sleep(1.5)`.

### OBSERVATION #5: `BlackjackView` has no class-level `interaction_check`
**Location:** `casino/games/blackjack.py:201-278, 281-373`
**Confidence:** 0.90
**Risk:** Each button manually repeats `if interaction.user.id != session.discord_id: return ...`. discord.py provides `async def interaction_check(self, interaction)` on the View class to DRY this. Risk is that a future developer adds a new button and forgets the guard.
**Vulnerability:** No single enforcement point — ownership check is distributed across four callbacks.
**Impact:** Future regression risk. Minor code duplication now.
**Fix:** Implement `BlackjackView.interaction_check` returning `interaction.user.id == self.session.discord_id` (with an ephemeral error message via `followup.send` since `interaction_check` can defer/reject).

### OBSERVATION #6: `_update_table_message` uses `interaction.response.edit_message` with no error handling
**Location:** `casino/games/blackjack.py:380-403`
**Confidence:** 0.75
**Risk:** No try/except around `interaction.response.edit_message(...)`. If the message was deleted, or if the interaction response already consumed, or if Playwright render raised partway through, the caller sees a raw discord exception propagated up to `discord.py`'s default handler and the click appears dead.
**Vulnerability:** No error boundary; caller expects this to be infallible.
**Impact:** Silent click failure for the user.
**Fix:** Wrap in try/except, log.exception, and optionally send a followup error embed.

### OBSERVATION #7: Dealer-play loop re-implements `_hand_value` logic inline instead of calling it
**Location:** `casino/games/blackjack.py:151-166`
**Confidence:** 0.90
**Risk:** `dealer_play` duplicates the `total/aces` computation rather than calling `_hand_value(self.dealer_hand)`. Any bug fix to ace handling needs to be made in two places. It also uses `is_soft = aces > 0 and total == 17` which is correct only because the inline logic keeps `aces` in scope — a single-function call would lose that signal unless `_hand_value` returned a `(total, is_soft)` tuple.
**Vulnerability:** Divergent implementations of the same rule. Future changes to `_hand_value` will not automatically apply here.
**Impact:** Bug-surface duplication. Minor.
**Fix:** Return `(total, soft)` from a new `_hand_value_detailed()` or similar and call it here.

### OBSERVATION #8: `_is_blackjack` / `_hand_value` / `_is_pair` have no input validation
**Location:** `casino/games/blackjack.py:60-83`
**Confidence:** 0.50
**Risk:** Pure helpers assume `hand` is a list of `(value, suit)` tuples. They'll crash with unhelpful messages on any malformed input. Not directly exploitable because callers control the data, but brittle.
**Vulnerability:** No type checking or length validation on public-ish helpers.
**Impact:** Minor; defensive coding opportunity.
**Fix:** Add lightweight assertions or rely on typing; not blocking.

## Cross-cutting Notes

**Ring 2 Casino pattern:** The `active_sessions` in-memory registry pattern likely exists in other casino games (crash, slots, coinflip, blackjack). Every one of them likely has the same "bot restart eats wagers" vulnerability (CRITICAL #2) and the same non-cryptographic RNG problem (CRITICAL #3). Recommend a single `casino/session_persistence.py` module providing load/save/reconcile, and a shared `_rng = secrets.SystemRandom()` helper imported by every game.

**Correlation-ID reuse across double/split (CRITICAL #4)** is a generic concern for any game that stakes additional bucks mid-hand. Audit the other casino games for the same pattern — any call to `deduct_wager` that re-uses `session.correlation_id` will silently corrupt the wager registry.

**Button-callback defer missing (CRITICAL #5)** is a systemic concern across the Discord-facing casino code. If blackjack is representative, every game's button callbacks should be audited for the same `defer()` pattern the entry point uses.

**`print` instead of `log.exception`** in an admin-facing financial view (WARNING #4) violates CLAUDE.md's Flow Economy Gotchas rule directly; grep the casino subsystem for bare `print(` statements in exception handlers and fix all of them.
