# Adversarial Review: trade_engine.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 371
**Reviewer:** Claude (delegated subagent)
**Total findings:** 19 (2 critical, 9 warnings, 8 observations)

## Summary

`trade_engine.py` is a pure valuation utility (no DB writes, no wallet calls, no Discord I/O), so most of ATLAS's hot-button failure modes (wallet idempotency, Discord API misuse) do not apply here. The concerns that DO apply are real: (1) a rookie-year fallback silently inflates every unknown player to age 22, (2) cornerstone blocking is enforced at evaluation time only — trade commit in `genesis_cog` can race around it, (3) `_safe_int(dm.CURRENT_SEASON)` re-reads an integer via PEP-562 `__getattr__` through every call path but `SEASON_CONFIG = _season_config()` at line 68 is still captured at import time and leaked as a public alias. The file is safe to keep in production, but needs targeted fixes before the next rebalance pass.

## Findings

### CRITICAL #1: `rookieYear or dm.CURRENT_SEASON` silently treats rookie-year `0` / missing field as "drafted this season" → every unknown-age player becomes 22

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:204-212`
**Confidence:** 0.90
**Risk:** Age derivation is the only fallback when `player["age"]` is missing or NaN (and the field is known to be missing from `/export/players`, which is why the derivation exists at all). The expression `player.get("rookieYear", dm.CURRENT_SEASON) or dm.CURRENT_SEASON` collapses three distinct cases to the same answer: (a) key missing, (b) `rookieYear == 0`, (c) `rookieYear` coerces to falsy via `_safe_int` returning 0 for NaN/inf. All three cases yield `rookie_yr == CURRENT_SEASON → seasons_played == 0 → age == 22`. Any player with an unparseable or zero rookie year is valued as a 22-year-old, which hits the peak `AGE_MULTIPLIER` of 1.20 AND the `_flat_bonuses` stack (`age <= 21 → +400` + `min(max(0, 24 - age + 1) * 125, 500) → +375`). A silent +775 flat-value injection plus a 20% multiplier swing is a huge asymmetric edge for any side that has stale roster data.
**Vulnerability:** The `or` fallback after `.get(..., default)` is a classic Python footgun — `0` is falsy even when it is the correct data, and `get` only uses its default when the key is missing, so chaining `or` effectively overrides every zero-value return. `_safe_int` further returns its `default` (`dm.CURRENT_SEASON`) for NaN/None/TypeError, so even the "safe" path produces the same collapse.
**Impact:** Trade valuations for any player with missing `age` AND missing/zero/NaN `rookieYear` are inflated by hundreds of points. In a YELLOW-band trade (~12-20% delta) this can silently flip the band to GREEN — letting unbalanced trades through ATLAS auto-approval.
**Fix:** Split the three cases explicitly:
```python
rookie_yr_raw = player.get("rookieYear")
if rookie_yr_raw is None:
    age = 22  # truly unknown — use default rookie age
else:
    rookie_yr = _safe_int(rookie_yr_raw, -1)
    if rookie_yr <= 0:
        age = 22  # unparseable or sentinel — use default
    else:
        seasons_played = max(dm.CURRENT_SEASON - rookie_yr, 0)
        age = 22 + seasons_played
```
And ideally clamp `age` to `[20, 40]` before feeding it to `_age_multiplier` to avoid absurd values from corrupt data.

---

### CRITICAL #2: Cornerstone block is a valuation-time warning only — not an enforced pre-commit gate

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:349-371`
**Confidence:** 0.85
**Risk:** The cornerstone check is implemented as a soft side effect: on match, the function appends a warning note and sets `band = "RED"`. There is no exception raised, no `is_blocked` field on `TradeEvalResult`, and no enum flag that callers can authoritatively test. `genesis_cog.evaluate_trade` users downstream must scan `result.notes` for the literal string `"🔒 **BLOCKED**"` or check `result.band == "RED"` — but RED is also used for "value gap > 20%", so the two cases cannot be distinguished. Any caller that only gates on `band == "RED"` will lump cornerstone-blocked trades with value-imbalance trades; any caller that decides to approve-anyway on a YELLOW/RED override (commissioner force-approve is a known pattern in ATLAS) will bypass the cornerstone rule entirely.
**Vulnerability:** The rule is enforced in the **valuation** layer, not the **commit** layer. `trade_engine.py` has no authority to prevent a trade from being written — only `genesis_cog` (which performs the actual roster swap) can do that. If the commit path accepts `result` and proceeds when it likes the value, the cornerstone note is purely advisory. Worse: if `parity_state.json` is missing or fails to parse (the `except Exception` at line 354 swallows and continues), the cornerstone set is silently empty and no block fires — this is data-loss of a rule invariant.
**Impact:** A cornerstone player can be traded if (a) parity_state.json is missing/corrupt at evaluation time, (b) the caller force-approves a RED band, or (c) the caller uses its own valuation path (`pick_ev`/`player_value` directly) without routing through `evaluate_trade`. The latter is not hypothetical — `genesis_cog.py:530,710,821` call `pick_ev` directly and `genesis_cog.py:1775` re-runs evaluate_trade on an already-approved modal, creating a TOCTOU between evaluation and commit where cornerstone state could change.
**Fix:** Add a dedicated `blocked: bool` and `block_reasons: list[str]` field to `TradeEvalResult`. Move the parity_state.json load into `data_manager.py` with a module-level cache that raises if the file exists but is unreadable (fail-closed, not fail-open). Callers should check `if result.blocked: reject()` explicitly and NEVER allow a commissioner force-approve to override a cornerstone block.

---

### WARNING #1: `SEASON_CONFIG = _season_config()` at import time re-introduces the BUG#10 it claims to fix

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:68`
**Confidence:** 0.70
**Risk:** Line 51-65 explicitly documents `BUG#10: converted from module-level dict to function so dm.CURRENT_SEASON is read at call time, not frozen at import time.` Then line 68 says `SEASON_CONFIG = _season_config()` and labels it `# Backwards compat alias — prefer _season_config() for fresh reads`. Any external caller (including a future caller) that imports and reads `trade_engine.SEASON_CONFIG` will get the import-time snapshot. Currently no caller references `SEASON_CONFIG` (grep elsewhere), but leaving a named public binding that re-creates the exact bug the comment claims to fix is a trap — someone will use it.
**Vulnerability:** The "backwards compat" justification is only meaningful if there are existing callers, and there aren't. The alias exists solely to be misused.
**Impact:** Low right now, high in the future. After a season rollover, any caller touching `SEASON_CONFIG` directly will silently use last-season thresholds until the module is reloaded — which never happens in a long-running Discord bot.
**Fix:** Delete line 68 entirely. If a caller ever needs the dict shape for typing, expose `_season_config` publicly as `get_season_config()` and make it clear it is a function.

---

### WARNING #2: `hasattr(dm, "CURRENT_SEASON")` and `hasattr(dm, "CURRENT_WEEK")` guards are dead code, masking real errors

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:133, 208, 251, 333`
**Confidence:** 0.90
**Risk:** `data_manager.py:178-182` defines a PEP-562 `__getattr__` that proxies any attribute on the underlying `_state` dataclass. `CURRENT_SEASON` and `CURRENT_WEEK` are fields on `LeagueState`, so `hasattr(dm, "CURRENT_SEASON")` is always True — the fallback branch (`... if hasattr else 1`) is dead. If `_state` is ever replaced with an object lacking those fields (a future schema change), `__getattr__` raises `AttributeError` — but `hasattr` catches that and silently returns False, so the code falls through to the literal `1`. A week-14 check that silently becomes a week-1 check is a meaningful rule-violation risk (`_ufa_penalty` applies a 1.25x multiplier at week >= 14, so corrupt state silently disables the late-season penalty).
**Vulnerability:** `hasattr` in Python is implemented via `try: getattr(...); except: return False`, which swallows every exception including programming errors. Combined with PEP-562's fail-raising `__getattr__`, this gives you a defensive fallback that never triggers on the happy path but silently rewrites the rules on the unhappy path.
**Impact:** Silent rule degradation when `data_manager` is in a bad state (e.g., before `load_all()` completes). Trade evaluations during cold start will use `CURRENT_WEEK = 1` regardless of actual calendar.
**Fix:** Remove the `hasattr` guards and let the call fail loudly, OR wrap in a specific try/except with a log line. The current pattern is the worst of both worlds.

---

### WARNING #3: `_ufa_penalty` reads `dm.CURRENT_WEEK` but not `dm.CURRENT_SEASON` — late-season multiplier applies all seasons equally

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:130-134`
**Confidence:** 0.80
**Risk:** The late-season multiplier (1.25x) triggers on `week >= 14`. Madden uses a 17- or 18-game regular season, so week 14 is roughly 3 weeks before end-of-regular-season. The penalty is appropriate IF the league is actually in the regular season — but the function does not consult `CURRENT_STAGE`. If the league is in preseason week 14 (hypothetical), postseason, or offseason, the week check is nonsensical.
**Vulnerability:** `data_manager.py` exposes `CURRENT_STAGE` alongside `CURRENT_WEEK`, but trade_engine only reads week. The stage assumption is implicit.
**Impact:** Edge case — unusual schedules produce wrong penalties. Low severity but user-visible because the trade breakdown prints the penalty amount.
**Fix:** Check `dm.CURRENT_STAGE == REGULAR_STAGE` before applying the multiplier, or hardcode the penalty to a fixed value and skip the week ramp.

---

### WARNING #4: `_bundling_penalty` treats `count == 1` as a discount, but `assets` arrives as the already-evaluated list INCLUDING the current player

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:163-166, 192-193, 307-311`
**Confidence:** 0.85
**Risk:** `evaluate_trade` builds `evaluated_a = []` and appends players AFTER calling `player_value(p, team_id, evaluated_a)` (line 307-311). So the first player in a bundle sees `evaluated_a == []` → `count = 0` → penalty 1.00. The second sees one prior player → `count = 1` → 0.92 — BUT the second player's OWN position is counted in `[a for a in assets if a.get("pos") == pos]` — wait, let me re-read: the comprehension iterates `assets` (which is `evaluated_a`, the prior list), so `count` is the number of PRIOR players at the same pos. The second player at pos QB sees `count=1 → 0.92`. Third sees `count=2 → 0.88`. OK so the logic is "each additional same-pos asset applies an escalating discount to the current one". But: the FIRST player at that position never takes a hit — they get 1.00 even though the bundle is obvious. And the discount is applied to the JUNIOR player, not the senior (most valuable) one.
**Vulnerability:** The order of iteration determines which player absorbs the penalty. If `side_a.players` is reordered (by caller sort, UI pick order, etc.), the total valuation shifts even though the SET of players is the same.
**Impact:** Non-determinism in trade valuation. Same trade evaluated twice with different player orderings gives different total values. User visible if the breakdown is re-rendered.
**Fix:** Compute bundle counts ONCE up front from `side_a.players` (order-independent), then pass a per-position map to `player_value`. Or apply the penalty as a post-hoc adjustment on the final side total.

---

### WARNING #5: `pick_ev` `slot_factor` inverts direction — higher slot number gets LOWER factor

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:250-255`
**Confidence:** 0.75
**Risk:** `slot_factor = 1.0 + (16 - slot_in_round) * 0.012`. In NFL draft convention, pick 1 is the most valuable (worst team) and pick 32 is the least (best team). Here slot 1 → factor = 1.0 + 15*0.012 = 1.18. Slot 16 → 1.00. Slot 32 → 1.0 + (16-32)*0.012 = 1.0 - 0.192 = 0.808. That seems right — slot 1 gets highest factor. BUT the default when `slot` is missing is `slot_in_round=16` → factor = 1.00. Callers in genesis_cog pass `pk.get("slot", 16)` — so missing slot = "middle of round". Fine. The real risk: the formula does not clamp. If a caller supplies `slot_in_round=0` (bad data), factor = 1.192 — valid but a sign that no input validation exists. If `slot_in_round` is negative or > 40 (shouldn't happen but could with stale data), factor can go negative and the risk haircut can invert.
**Vulnerability:** No input validation on `round_`, `draft_year`, `slot_in_round`. A caller passing `round_=0` gets base=60 (default from `PICK_BASE_VALUES.get(0, 60)` — same as round 7); `round_=-1` also → 60. All error paths return a valid-looking value with no signal.
**Impact:** Bad data produces plausible-but-wrong numbers. Combined with the lack of logging on the default branch, corrupt pick entries silently distort valuations.
**Fix:** Validate `round_ in [1..7]` and `1 <= slot_in_round <= 40`; raise or return a sentinel if outside range.

---

### WARNING #6: `evaluate_trade` recomputes `pick_ev` twice per trade in common code paths

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:313-316, 324-327` (vs `genesis_cog.py:530, 710, 821, 1775`)
**Confidence:** 0.80
**Risk:** `genesis_cog.py` calls `te.pick_ev(...)` directly for display purposes AND `te.evaluate_trade(...)` which also calls `pick_ev` internally. These two code paths can diverge if `dm.get_team_record_dict(team_id)` returns different values between the calls (e.g., a background sync happens mid-trade-flow). The caller's displayed "expected pick value" may not match the value that went into the band calculation.
**Vulnerability:** No caching of `pick_ev` results per `(round, year, team_id, slot)` tuple. Re-reads of `get_team_record_dict` are non-deterministic across cache rebuilds.
**Impact:** User-visible discrepancy between "this is what the pick is worth" in a breakdown and "this is the total" in the band calculation.
**Fix:** Accept a pre-computed pick EV dict into `evaluate_trade`, OR memoize `pick_ev` on `(round, year, team_id, slot)` for the duration of one evaluation.

---

### WARNING #7: `_contract_delta` silently uses `int` truncation on a float-typed cap_pct threshold

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:111-128`
**Confidence:** 0.55
**Risk:** `cap_pct` comes from `dm.get_contract_details` which does `cap_raw if cap_raw >= 0.5 else cap_raw * 100`. So a cap value of 0.03 (fraction form) gets multiplied by 100 → 3.0 (percent form), BUT a cap value of 0.4 (fraction form) is less than 0.5 → gets multiplied by 100 → 40.0 (percent form). BUT a cap value of 0.55 (fraction form) is >= 0.5 → NOT multiplied → stays 0.55 (interpreted as percent). So a player taking up 55% of the cap is recorded as 0.55%, bypassing the 8.0% bonus branch entirely. This is a `data_manager.py` bug but `trade_engine.py` trusts the output blindly.
**Vulnerability:** The upstream heuristic `cap_raw >= 0.5` fails when a legitimate cap fraction is between 0.5 and 1.0 (rare but not impossible — a franchise QB can hit 20%+, and a bad cap manager could hit 50%+ in theory).
**Impact:** Players with anomalous cap hits get the wrong contract delta. Edge case but plausible for cap-strapped teams.
**Fix:** Fix upstream in `data_manager.get_contract_details` — use a typed column, not a heuristic. At minimum, log a warning in trade_engine when `cap_pct` is an unlikely value (< 0 or > 30).

---

### WARNING #8: Parity state cornerstone key type mismatch — compares `str(rosterId)` to JSON dict key

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:349-368`
**Confidence:** 0.70
**Risk:** Line 352 loads `json.load(f).get("cornerstones", {})`. Line 366 builds `roster_id_str = str(p.get("rosterId", ""))` and checks `if roster_id_str and roster_id_str in cornerstone_data`. If `parity_state.json` stores cornerstones as integers (`{"cornerstones": {12345: {...}}}`), JSON does NOT allow integer keys so `json.dump` would have stringified them — OK that's safe. But if cornerstones is a LIST (`{"cornerstones": [12345, 67890]}`), `roster_id_str in cornerstone_data` iterates the list and compares `"12345" == 12345 → False`. Silent miss. If cornerstones is a dict with integer-stringified keys, comparing `str(12345) == "12345" → True`. All depends on the schema which is not documented here.
**Vulnerability:** No schema validation on `parity_state.json` contents. Any shape that doesn't exactly match `dict[str, ...]` will silently fail the membership check.
**Impact:** Cornerstone rule bypass if parity_state.json schema drifts.
**Fix:** On load, normalize to `set[str]` after converting all keys/items to strings. Add a docstring documenting the expected shape. Emit a log warning if the loaded value is not a dict.

---

### WARNING #9: `except Exception` around parity_state.json read silently fail-opens

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:350-355`
**Confidence:** 0.90
**Risk:** If `parity_state.json` exists but is malformed (partial write, encoding issue, disk error), the except catches everything and `cornerstone_data = {}` — meaning the cornerstone check silently passes EVERY player. This is fail-open behavior on a rule-enforcement path. Per the focus doc: "Silent `except Exception: pass` in admin-facing views is PROHIBITED" — this is not strictly "admin-facing" but it IS rule enforcement, and the log line is at WARNING not ERROR.
**Vulnerability:** A corrupt parity_state.json silently disables all cornerstone protections. The `.exists()` check doesn't catch open/read/parse failures.
**Impact:** Cornerstone players become tradeable during any period where parity_state.json is in a bad state (e.g., mid-write from a concurrent process, encoding corruption).
**Fix:** Either fail closed (re-raise the exception, forcing the trade flow to halt until an admin fixes parity_state.json), or raise a specific `CornerstoneDataUnavailable` exception that the caller must explicitly handle. At minimum, log at `log.error` not `log.warning` so the alert fires.

---

### OBSERVATION #1: `_safe_int` swallows `OverflowError` via its narrow except clause

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:32-40`
**Confidence:** 0.40
**Risk:** `_safe_int` catches `(ValueError, TypeError)` but not `OverflowError`. `float('1e400')` → inf → caught by isinf check. But `int(float('1e300'))` → OverflowError when converting the large float to int. The NaN/Inf guards cover most cases, but edge-case huge floats (beyond float max) that parse through `float(val)` can still hit OverflowError.
**Vulnerability:** Theoretical — Madden data shouldn't contain values beyond float max. Low probability.
**Impact:** Unhandled exception in player_value for corrupt data. Would bubble up to genesis_cog.
**Fix:** Add `OverflowError` to the except tuple.

---

### OBSERVATION #2: `_age_multiplier` clamp vs table lookup creates a discontinuity

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:96-99`
**Confidence:** 0.50
**Risk:** For ages 20-32, returns table values. For age <= 20 returns 1.28. For age >= 33 returns 0.70. But age 32 in the table is 0.75, and age 33 clamp is 0.70 — a step. Age 34 = 0.70, age 33 = 0.70, age 32 = 0.75. The table row for 33 is present in `AGE_MULTIPLIER` but unreachable because the `age >= 33` clamp fires first. Dead entry.
**Vulnerability:** Future editors may update the table row for age 33 and be surprised it has no effect.
**Impact:** None for correctness — just confusing code.
**Fix:** Either remove the 33 row from the dict or change the clamp to `age > 33`.

---

### OBSERVATION #3: `_pos_multiplier` uses positional abbreviations not present in TSL's position schema

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:82-87`
**Confidence:** 0.60
**Risk:** The multiplier dict uses `LEDGE`, `REDGE`, `MIKE`, `WILL`, `SAM` which are specific 4-3/3-4 nomenclature, not the standard Madden API position codes (`LE`, `RE`, `MLB`, `ROLB`, `LOLB`). If the roster data uses Madden codes, every edge rusher and linebacker falls through to the `1.00` default and the valuation table is partially inert.
**Vulnerability:** Mismatch between engine's position vocabulary and the data source's position vocabulary.
**Impact:** Under-valuation of edge rushers and linebackers. Significant bias in trade valuations if the mismatch is real.
**Fix:** Verify the actual position strings in `/export/players` and either rename the dict keys or add an alias map.

---

### OBSERVATION #4: `PlayerValueBreakdown` dataclass does not include `core` or `subtotal` — the most important intermediate numbers

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:168-190, 226-246`
**Confidence:** 0.50
**Risk:** Line 230 computes `core = base * pos_mult * age_mult * reg_mod` and line 235 computes `subtotal`, but neither is stored on the dataclass. The `summary_lines()` output can only show the multiplicands, not the products at each stage. A player with `base=1000, pos_mult=1.2, age_mult=1.1, reg_mod=1.0` should show core=1320, but the current output forces the user to multiply by hand.
**Vulnerability:** Debuggability. Not a bug, but makes it harder to audit why a trade valued the way it did.
**Impact:** Poor UX for commissioners reviewing breakdowns.
**Fix:** Store `core` and `subtotal` on the dataclass and print them in `summary_lines`.

---

### OBSERVATION #5: Hardcoded `min(..., 50)` floor on final value masks negative valuations

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:244`
**Confidence:** 0.55
**Risk:** `final = max(int(market_adjusted * bundle_mult), 50)`. Any player with a genuinely negative net value (e.g., huge UFA penalty exceeding their core value, negative contract delta from unsignable status) is silently floored at 50. This hides the signal that the player is a dead asset — the engine implies every player is at least worth 50 points.
**Vulnerability:** A trade involving two dead assets traded for a mid pick will show 100 points on one side vs 60 points on the other — a 40% gap → RED band → auto-decline. But the real gap is much larger because the two players are worth nothing.
**Impact:** Unintended rounding toward fairness when the real situation is a clearly bad trade.
**Fix:** Allow final values to go to 0 (or to a small positive floor only for numerical stability, e.g., 1). Document the floor and its rationale if it stays.

---

### OBSERVATION #6: `_flat_bonuses` age formula has an off-by-one boundary quirk

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:106-109`
**Confidence:** 0.40
**Risk:** `bonus = 400 if age <= 21 else 0` — so age 21 gets 400, age 22 gets 0. Then `if age <= 24: bonus += min(max(0, 24 - age + 1) * 125, 500)`. At age 22: `24 - 22 + 1 = 3 * 125 = 375`. At age 21: `24 - 21 + 1 = 4 * 125 = 500`, then min with 500 → 500. Total at 21 = 400 + 500 = 900. At 20: `24 - 20 + 1 = 5 * 125 = 625`, min with 500 → 500. Total at 20 = 400 + 500 = 900. At 24: `24 - 24 + 1 = 1 * 125 = 125`. Total at 24 = 0 + 125 = 125. At 25: falls through both branches, 0. So bonuses: 20=900, 21=900, 22=375, 23=250, 24=125, 25=0. The jump from 21 (900) to 22 (375) is a 525-point cliff — a player at 21y364d vs 22y0d differs by 525 points of trade value. Small change in data (rounded ages) produces large valuation swings.
**Vulnerability:** Age boundary cliff. Not a bug per se but a sensitivity hotspot.
**Impact:** Trades involving 21/22-year-olds are hypersensitive to age-rounding.
**Fix:** Smooth the transition or make the cliff intentional with a comment.

---

### OBSERVATION #7: `evaluate_trade` does not log or surface a failure if BOTH sides are empty

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:302-371`
**Confidence:** 0.60
**Risk:** If `side_a.players == side_a.picks == side_b.players == side_b.picks == []`, the function returns `a_total=0, b_total=0, delta_pct=0, band="GREEN", notes=[]`. No warning that an empty trade was evaluated. A caller in a UI loop that forgets to populate the sides gets back a clean "GREEN" result.
**Vulnerability:** Empty-state handling is permissive. Silent success on an obvious misuse.
**Impact:** Caller bugs get masked.
**Fix:** Add a sanity check at the top: if both sides are fully empty, append a warning note or raise `ValueError("Empty trade")`.

---

### OBSERVATION #8: No docstrings on public functions `player_value`, `pick_ev`, `evaluate_trade`

**Location:** `C:/Users/natew/Desktop/discord_bot/trade_engine.py:192, 250, 302`
**Confidence:** 0.40
**Risk:** The three primary entry points of the module lack docstrings describing input shape, return shape, or value ranges. `player` is a `dict` with no schema. `pk` is a `dict` with no schema. Callers in `genesis_cog` pass differently-shaped dicts depending on code path.
**Vulnerability:** Contract drift — genesis_cog calls can diverge from trade_engine expectations silently.
**Impact:** Low immediate impact, high long-term maintenance cost.
**Fix:** Add type-hinted docstrings documenting the expected keys: `player` needs `overallRating|playerBestOvr`, `pos`, `firstName`, `lastName`, `age|rookieYear`, `rosterId`, `ability1`..`ability6`. `pk` needs `round`, `year`, `team_id`, `slot`.

---

## Cross-cutting Notes

1. **Fail-open behavior is a pattern in this file.** Four separate try/except blocks (contract lookup, scarcity lookup, rings lookup, parity_state load) all return benign defaults on failure and log at WARNING level. Individually each is reasonable; collectively they mean a trade evaluated during a partial `data_manager` outage uses benign defaults everywhere and produces a GREEN band result that commits without alarms. The pattern should be audited at the `genesis_cog` call-site level — if `evaluate_trade` returned a successful result while internally degrading, the caller has no way to know the valuation is degraded. Consider adding a `degraded: bool` or `warnings: list[str]` field on `TradeEvalResult` that is set whenever any inner fallback fires.

2. **Trade commit atomicity is NOT enforced by this file — and this file makes no attempt to validate that the caller enforces it.** trade_engine is strictly a valuator, but the focus doc specifically calls out trade atomicity as a concern. The review of `genesis_cog.py` (Ring 2 target, separate file) should verify that the commit path writes both sides in a single transaction and that a re-evaluation (TOCTOU guard) happens immediately before commit — because `trade_engine.evaluate_trade` reads live state (`dm.get_contract_details`, `parity_state.json`) at call time and nothing prevents that state from changing between the evaluate call and the actual roster swap.

3. **`SEASON_CONFIG` public alias (WARNING #1) should be audited across all Ring 2 / Ring 3 files** — if any caller has started importing `trade_engine.SEASON_CONFIG` since the module was written, the BUG#10 regression has already shipped.
