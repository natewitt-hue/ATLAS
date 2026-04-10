# Adversarial Review: odds_utils.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 21
**Reviewer:** Claude (delegated subagent)
**Total findings:** 5 (1 critical, 3 warnings, 1 observation)

## Summary

Small utility with outsized financial impact — every bet in Flow sportsbook, Real Sportsbook, and prediction markets flows through `payout_calc`/`profit_calc`. The math has a systematic under-payment bias from `int()` truncation (not rounding), rejects legitimate `+100`/`-100` American odds in the `> 0` / `< 0` split logic, and crashes on `-0` (handled by `abs()` but not on `0`). `american_to_str(0)` also prints `"0"` instead of the conventional `"EVEN"` or `"+100"`.

## Findings

### CRITICAL #1: `int()` truncation systematically underpays wagers (favors the house)
**Location:** `C:/Users/natew/Desktop/discord_bot/odds_utils.py:9-16`
**Confidence:** 0.95
**Risk:** `int(wager + wager * (odds / 100))` truncates toward zero, not rounds. A $100 wager at +150 odds is `int(100 + 100 * 1.5) = 250` (fine — exact). But a $101 wager at +150 = `int(101 + 101 * 1.5) = int(252.5) = 252`, not 253. A $33 wager at -110 = `int(33 + 33 * (100/110)) = int(33 + 30.0) = 63` — but the true payout rounds to 63 so fine. A $100 wager at -110 = `int(100 + 100 * 0.90909...) = int(190.909...) = 190`, not 191. Across every bet, the house systematically pockets the fractional cent.
**Vulnerability:** `int()` on a float is floor-for-positive, ceil-for-negative — NOT rounding. Every single non-exact division underpays the bettor by up to 1 coin. For a high-volume sportsbook (Flow sees thousands of bets per season), this is both a visible bug ("I bet $100 at -110 and got paid $190 instead of $191") AND a silent financial drift of the house edge.
**Impact:** Financial ledger drift, user-visible shortpayments, and a real incident risk if a user reconciles their bet history against expected payouts. Also: different bets with symmetric odds (+150 vs -150) should produce the same total house drip — `int()` truncation makes this asymmetric and non-obvious.
**Fix:** Use `round()` explicitly, then cast: `return int(round(wager + wager * (odds / 100)))`. Or better, use `Decimal` and `ROUND_HALF_EVEN` for financial correctness, then cast once at the boundary.

### WARNING #1: `american_to_str(0)` returns `"0"` instead of `"EVEN"` or `"+100"`
**Location:** `C:/Users/natew/Desktop/discord_bot/odds_utils.py:4-6`
**Confidence:** 0.85
**Risk:** The branch `return f"+{odds}" if odds > 0 else str(odds)` maps `0` → `"0"`. American odds of `0` is nonsense; the idiomatic value is `+100` or `-100` (EVEN). A caller passing `0` gets a bad-looking UI string with no warning, and `payout_calc(wager, 0)` short-circuits to `return wager` which means zero profit.
**Vulnerability:** Neither function raises on odds=0; both silently produce "wrong" but non-crashing results. If an Elo-based odds generator ever produces `0` (e.g., coin flip → rounding), the UI shows `"0"` and the wager returns no profit.
**Impact:** User-visible "odds: 0" displayed on a bet card, and a pseudo-refund payout (wager returns with zero profit) that is not clearly marked as a push.
**Fix:** `if odds == 0: return "EVEN"` in `american_to_str`; in `payout_calc`, either raise `ValueError` or treat `0` as `100`/`-100` explicitly (document which).

### WARNING #2: `american_to_str` passes non-int odds through unchecked
**Location:** `C:/Users/natew/Desktop/discord_bot/odds_utils.py:4-6`
**Confidence:** 0.8
**Risk:** The signature is `odds: int` but Python doesn't enforce it. A caller passing a `float` like `-110.0` gets `"-110.0"` (ugly) or a decimal `-110.5` gets `"-110.5"` (wrong format — American odds are always integers per convention). `payout_calc` casts to `int()` but `american_to_str` does not.
**Vulnerability:** Inconsistent coercion between the display helper and the math helper. Any renderer that calls `american_to_str(raw_odds_from_espn)` (where ESPN may return a float) gets bad-looking UI strings.
**Impact:** Visual glitch — "odds: -110.0" shown on a sportsbook card. Probably caught in review, but not impossible in production.
**Fix:** Cast first: `odds = int(odds)` on line 5, mirroring `payout_calc`.

### WARNING #3: No guard against `wager < 0` or `wager = 0`
**Location:** `C:/Users/natew/Desktop/discord_bot/odds_utils.py:9-21`
**Confidence:** 0.7
**Risk:** `payout_calc(-100, -110)` returns `int(-100 + -100 * (100/110)) = int(-190.909...) = -190`. The sign flips to "user owes the house $190." If this ever flows through `flow_wallet.credit()` it produces a *debit* on a credit call site. There is no assert or `max(0, ...)` guard.
**Vulnerability:** Defensive programming gap. A bug upstream that produces `wager = -x` (e.g., refund math, void-bet recredit) propagates through unchecked and can corrupt the ledger.
**Impact:** Possible ledger corruption if any caller passes a negative wager. Per CLAUDE.md attack surface: "Float vs int balance corruption in flow_economy.db."
**Fix:** Add a contract: `if wager <= 0: raise ValueError("wager must be positive")`. Or at minimum, document "caller must guarantee wager > 0" in the docstring.

### OBSERVATION #1: `payout_calc(w, 0)` returns `wager` silently — unclear semantics
**Location:** `C:/Users/natew/Desktop/discord_bot/odds_utils.py:12-13`
**Confidence:** 0.65
**Risk:** The `if odds == 0: return wager` short-circuit is a silent "push" rule that isn't documented anywhere. A reader of the docstring ("Return total payout from American odds") would expect an exception or `EVEN` (2x wager), not wager-refund behavior.
**Vulnerability:** Implicit contract — the "odds=0 → return wager" choice is a load-bearing invariant that isn't written down.
**Impact:** Design smell. A refactor that "cleans up" this branch would silently change the financial semantics.
**Fix:** Add a docstring line: `"""Odds of 0 is treated as a push (returns wager unchanged)."""` Or raise `ValueError` and force callers to be explicit.

## Cross-cutting Notes

The `int()` truncation bug is likely also present in `db_migration_snapshots.py:97-101` (which has its own inlined profit math: `int(wager * odds / 100)` and `int(wager * 100 / abs(odds))` — same truncation pattern, same house-drift bug, but in a backfill context). Auditors should diff the backfill math against the live `payout_calc` to ensure they produce the same historical values, otherwise sparklines will drift from ledger reality.
