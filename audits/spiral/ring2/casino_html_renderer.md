# Adversarial Review: casino/renderer/casino_html_renderer.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 2131
**Reviewer:** Claude (delegated subagent)
**Total findings:** 13 (1 critical, 6 warnings, 6 observations)

## Summary

File is overwhelmingly static CSS — the vast majority of LOC is inert design tokens. The Python surface area is small but has real sharp edges: an unescaped `mult_sub` span (pathway exists for future injection), a `cashout_mult == 0.0` falsy-fallthrough bug in the Crash badge, silent `len(reels)`/`len(tiles)` underflow crashes if callers pass short lists, and `player_pick.upper()` crashing on `None`. No direct resource leaks here because `render_card()` in `atlas_html_engine.py` properly `finally`-releases pages — the concern chain from Ring 1 does not terminate in this file.

## Findings

### CRITICAL #1: `cashout_mult == 0.0` falls through `or` to `current_mult` in CASHED badge

**Location:** `casino/renderer/casino_html_renderer.py:1347-1349`
**Confidence:** 0.85
**Risk:** When a player cashes out at exactly `0.00x` (or a bug sets `cashout_mult` to `0.0`), the `or` operator treats `0.0` as falsey and silently substitutes `current_mult` into the CASHED badge. The card then claims the player cashed out at the *current* round multiplier, which is wrong and user-visible.

```python
if cashed_out:
    outcome = "win"
    badge_text = f"CASHED {cashout_mult or current_mult:.2f}x"
```

**Vulnerability:** Python's `or` returns the first truthy operand. `0.0` is falsey. If settlement logic ever produces `cashed_out=True` with `cashout_mult=0.0` (e.g., race between crash and cashout, refund edge case, new feature), the player will see a wildly different displayed multiplier than what they actually cashed at. The fix on line 1368 uses the correct explicit-None pattern (`cashout_mult if (cashed_out and cashout_mult is not None) else current_mult`) — lines 1347-1349 and 1623 do not.

Line 1623 has the same problem: `mult_display_val = cashout_mult if cashed_out else current_mult` — at least does not use `or`, so this one is fine. But line 1349 is broken.

**Impact:** Player cashes out and sees a display multiplier that does not match the payout. Triggers support tickets, undermines trust in casino math, hides real bugs.

**Fix:**
```python
if cashed_out:
    outcome = "win"
    display_mult = cashout_mult if cashout_mult is not None else current_mult
    badge_text = f"CASHED {display_mult:.2f}x"
```

---

### WARNING #1: `mult_sub` spliced raw into inline `style=` and span body without escaping

**Location:** `casino/renderer/casino_html_renderer.py:1405-1413, 1648-1649`
**Confidence:** 0.70
**Risk:** `mult_sub` is hardcoded today ("CRASHED" / "CASHED OUT" / "Climbing..."), but the pattern sets up a latent injection hazard: `<div class="mult-sub" style="color: {mult_color};">{mult_sub}</div>` — no `esc()`. If any future edit makes `mult_sub` dynamic (e.g., localized text, streaker tag, opponent name, A/B copy), the template is ready to ship an XSS / CSS-break.

Similarly, `mult_color` is spliced straight into `style=` attributes without validation. Tokens values are trusted, but they're plain strings — a theme layer that ever adds CSS-expression tricks will land here.

**Vulnerability:** The file interleaves escaped (`esc(status_desc)`, `esc(player_name)`) and unescaped (`{mult_sub}`, `{mult_color}`, `{mult_display_val:.2f}`) splices. Inconsistency is the real risk — reviewers see `esc()` in most places and assume all fields are escaped.

**Impact:** XSS / CSS injection if a future change passes user-derived text into `mult_sub` or theme-derived strings into `mult_color`. Also CSS-corruption if a color string ever contains `"`.

**Fix:** Wrap both in `esc()` defensively: `{esc(mult_sub)}` and `{esc(mult_color)}`. Same pattern for `{fill_gradient}`, `{coin_gradient}`, `{coin_rim}`, `{pick_color}`, `{result_color}`, `{p1_color}`, `{p2_color}`, `{tier_color}` — all are spliced into `style=` without escape.

---

### WARNING #2: `player_pick.upper()` and `opponent_pick.upper()` crash on `None` / non-string

**Location:** `casino/renderer/casino_html_renderer.py:1720, 1771, 1776, 1783`
**Confidence:** 0.80
**Risk:** `_build_coinflip_html` signature types `player_pick` as `str` (not Optional), but nothing validates the runtime value. `won = result == player_pick` at line 1720 will silently return `False` for None — *but* the line 1783 uses `player_pick.upper()`, which raises `AttributeError: 'NoneType' object has no attribute 'upper'`. Same for `opponent_pick` on line 1776 (which is `Optional[str]` and properly guarded with `or ''`), but `player_pick` has no such guard.

**Vulnerability:** If a game cog ever forgets to pass `player_pick` on a timeout/void resolution, the render crashes inside the `render_coinflip_card` coroutine. The exception bubbles up to the cog's interaction handler. If the cog is in `defer()+followup` mode, the followup call never happens, leaving the user staring at "thinking…" forever.

**Impact:** Coin flip card fails silently to render. Interaction times out. Wallet may already be debited (TOCTOU with settlement path).

**Fix:**
```python
pp = (player_pick or "").strip()
op = (opponent_pick or "").strip()
...
{esc(pp.upper())}
{esc(op.upper())}
```
Or, better, validate at function entry and raise `ValueError` early so settlement can rollback.

---

### WARNING #3: Unchecked `reels[0]` index on possibly-empty list

**Location:** `casino/renderer/casino_html_renderer.py:1155-1158`
**Confidence:** 0.75
**Risk:** `is_triple = revealed == 3 and len(set(reels)) == 1` is guarded. But the very next branch: `if is_triple and reels[0] == "shield":` — if a caller passes `revealed=3` and `reels=[]` (empty list), `len(set([])) == 0 != 1`, so the `and` short-circuits safely. Fine.

However line 1185: `symbol = reels[i] if i < len(reels) else "coin"` — this guards correctly. So slots itself is safe.

*However:* the `is_triple and reels[0]` path assumes `reels` is always at least length 3 when `is_triple` is True, which is guaranteed by `len(set(reels)) == 1 and revealed == 3` — but `revealed` is a trust-the-caller argument. Nothing asserts `len(reels) >= revealed`. If a caller passes `revealed=3, reels=["shield"]`, you get `len(set(["shield"])) == 1`, `is_triple=True`, then `reels[0] == "shield"` works — but later `reels[1]` via `reels[i] if i < len(reels) else "coin"` substitutes `"coin"`, so the card displays `SHIELD|COIN|COIN` labeled "JACKPOT". Silent rendering corruption.

**Vulnerability:** Trust-the-caller on matching `revealed` and `len(reels)`. Would produce "jackpot" card for a losing spin.

**Impact:** False jackpot card. Player sees win, game cog records loss. Support ticket & trust erosion.

**Fix:** Early validate: `if len(reels) < revealed: raise ValueError(...)`. Also compute `is_triple` against the sliced revealed prefix: `is_triple = revealed == 3 and len(set(reels[:3])) == 1 and len(reels) >= 3`.

---

### WARNING #4: Scratch card `tiles[0] * 3 = +${total:,}` assumes `is_match` means identical values

**Location:** `casino/renderer/casino_html_renderer.py:1981-1985, 1963-1978`
**Confidence:** 0.70
**Risk:** The template unconditionally renders `${tiles[0]:,} × 3 = +${total:,}!` when `is_match=True`, but the function never verifies `tiles[0] == tiles[1] == tiles[2]`. If a caller passes `is_match=True` but tiles are `[500, 500, 1000]` (e.g., a bug in the game cog), the card claims "`$500 × 3 = $1500`" which is *arithmetically misleading* at best and false at worst.

Also, `tiles[0]` is accessed without length check — if `tiles=[]` or `len(tiles) < 3` the card crashes with `IndexError`.

**Vulnerability:** No parameter validation. The renderer trusts the cog's `is_match` flag and tile data shape.

**Impact:** Card arithmetic can contradict ledger. Support tickets and audit confusion. Also IndexError crash path for short tiles list.

**Fix:** Validate at entry: `assert len(tiles) >= 3, "scratch card requires 3 tiles"`. Optionally re-verify the match: `is_match = len(set(tiles[:3])) == 1`.

---

### WARNING #5: `player_score` bust check bypassed when passed as string

**Location:** `casino/renderer/casino_html_renderer.py:840-841`
**Confidence:** 0.70
**Risk:**
```python
dealer_bust = not hide_dealer and isinstance(dealer_score, int) and dealer_score > 21
player_bust = isinstance(player_score, int) and player_score > 21
```
Signature types `player_score` as `int | str`. If a caller passes `"22"` (string), `isinstance(player_score, int)` is False, so `player_bust=False`, the card displays `22` without strikethrough, and the visual outcome is a winning player with score 22.

**Vulnerability:** The string path exists specifically so dealer can be "?" during hidden state. But nothing forces the caller to always pass `int` for player. Mismatch between caller expectation ("pass whatever makes the display right") and renderer logic ("only int triggers bust styling") means a stringified int silently drops the bust indicator.

**Impact:** Bust card rendered without bust styling. Player sees "22" printed normally next to "IN PLAY" badge when they should see strikethrough. Confusing UX.

**Fix:** Coerce at entry: `try: ps = int(player_score); except (ValueError, TypeError): ps = None`. Then `player_bust = ps is not None and ps > 21`. Use `ps` (when not None) for display.

---

### WARNING #6: `balance: int` signature is a lie — no runtime guard against floats

**Location:** `casino/renderer/casino_html_renderer.py:818, 903, 985, 1016, 1105, 1142, 1335, 1705, 1926`
**Confidence:** 0.65
**Risk:** Every card renders `${balance:,}`. Python's `:,` format spec accepts floats (`f"${1234.5:,}"` → `"$1,234.5"`), but Flow economy mandates integer credit units. If a caller ever passes a float (e.g., from a partially-migrated ledger or a broken settlement calc), the card silently renders decimals, exposing the float bug downstream but NOT failing loudly.

Per `atlas_focus.md`: "Float vs int balance corruption in `flow_economy.db`."

**Vulnerability:** Rendering layer treats the int contract as aspirational. No type coercion, no assertion. The renderer is exactly the right place to enforce: it's the last hop before pixels.

**Impact:** Float balance corruption becomes visible to the user one render at a time, often after the ledger is already corrupted — meaning by the time the bug is reported, the bad state has propagated.

**Fix:** Defensive `int(balance)` at entry of each render. Optionally log a warning if `balance != int(balance)` so ledger drift is detected at first render.

---

### OBSERVATION #1: `chip_stack` distribution formula is nonsensical decoration

**Location:** `casino/renderer/casino_html_renderer.py:1056-1059`
**Confidence:** 0.90
**Risk:** The chip denomination formula is `abs(pnl) // max(i, 1) % 100 or 5` — a contrived decoration that has nothing to do with actual chip denominations. For `pnl=$1000` this generates chip labels like `$0, $0, $33, $50, $5` — meaningless to the viewer.

**Vulnerability:** Cinematic intent, but the output is nonsense. Users who look closely will notice chips reading "$0".

**Impact:** Low — purely cosmetic. But mystifying to anyone reading the code.

**Fix:** Pick actual chip denoms (`[100, 50, 25, 10, 5]`) or document why the formula is intentional gibberish.

---

### OBSERVATION #2: `blackjack: 3:2 Payout` is hardcoded in result subtitle

**Location:** `casino/renderer/casino_html_renderer.py:1029`
**Confidence:** 0.90
**Risk:** `sub_text = f"Natural 21 &middot; 3:2 Payout"` — hardcoded payout ratio. If blackjack ever moves to 6:5 (a typical casino nerf) or variable payout, this text lies to the user.

**Vulnerability:** Duplicated truth: settlement math lives in the game cog; display lives here. They can drift.

**Impact:** Card displays incorrect payout ratio. Player disputes settlement.

**Fix:** Take payout ratio as a parameter, or derive from `pnl / wager`.

---

### OBSERVATION #3: `badge_text` in crash includes untrusted format without esc

**Location:** `casino/renderer/casino_html_renderer.py:1349, 1352`
**Confidence:** 0.60
**Risk:** `badge_text = f"CASHED {cashout_mult or current_mult:.2f}x"` — the `.2f` format spec sanitizes numeric input, so as-is this is safe. But the pattern of building `badge_text` without passing through `esc()` at the call site to `build_header_html` at line 1363 relies on `build_header_html` calling `esc(badge_text)` itself (which it does on line 447 of `atlas_html_engine.py`). OK for now, but fragile coupling: if someone "optimizes" `build_header_html` to trust pre-escaped input, every caller needs auditing.

**Vulnerability:** Cross-file escape contract. Not a bug today; brittle.

**Impact:** None today; time-bomb on engine refactor.

**Fix:** Either document "build_header_html escapes all its string args" at both ends, or wrap `badge_text` in `esc()` at the call site so double-escape is harmless.

---

### OBSERVATION #4: `chip_class` parameter on `_bj_chip_html` is unused

**Location:** `casino/renderer/casino_html_renderer.py:125-131`
**Confidence:** 0.95
**Risk:** The `amount` parameter is never used in the function body — only `outcome` is referenced. Signature is misleading.

```python
def _bj_chip_html(amount: int, outcome: str) -> str:
    """Generate a footer chip element next to wager."""
    chip_class = {...}.get(outcome, "chip-k")
    return f'<div class="bj-chip {chip_class}">$</div>'
```

**Vulnerability:** Dead parameter. Caller on line 885 passes `wager` for no reason.

**Impact:** Signature debt; confusing to readers; potential future bug if someone assumes `amount` affects rendering.

**Fix:** Remove `amount` from the signature, or actually render the amount inside the chip.

---

### OBSERVATION #5: Duplicate `dealer_bust` computation in blackjack result builder

**Location:** `casino/renderer/casino_html_renderer.py:1036, 1047`
**Confidence:** 0.90
**Risk:** `dealer_bust` is computed twice: once inside the `elif result == "win":` branch on line 1036, then unconditionally at line 1047. The second computation shadows the first (they happen to agree), but the inner computation is dead code.

**Vulnerability:** Harmless duplication; signals the function grew organically. Future edit could diverge them.

**Impact:** Code smell only.

**Fix:** Compute `dealer_bust`/`player_bust` once at the top of the function, reuse in all branches.

---

### OBSERVATION #6: `txn_id` parameter accepted but never rendered in blackjack / crash / coinflip

**Location:** `casino/renderer/casino_html_renderer.py:820, 987, 1147, 1307, 1338, 1679, 1707, 1901`
**Confidence:** 0.85
**Risk:** All render functions accept `txn_id: Optional[str] = None` and pass it through to `build_header_html` (via `header = build_header_html(..., txn_id)`), which DOES render it when present. Good — not dead. But the Blackjack hand card (`_build_blackjack_html`) does NOT call `build_header_html` — it builds its own custom header on lines 932-941. So blackjack's `txn_id` parameter is silently dropped. Inconsistent: slots / crash / coinflip / scratch all render TXN ID; blackjack does not.

**Vulnerability:** Observability gap. TXN IDs are how ATLAS ties rendered cards to flow_economy ledger entries. Blackjack cards cannot be audited back to the transaction.

**Impact:** When a player disputes a blackjack hand, no TXN ID on the card means a manual DB lookup.

**Fix:** Add `{f'<div class="bj-txn">TXN #{esc(txn_id)}</div>' if txn_id else ""}` to the blackjack header block, styled to match.

---

## Cross-cutting Notes

1. **Inconsistent HTML-escape hygiene.** The file mostly escapes (`esc(player_name)`, `esc(status_desc)`), but several splice points are raw: `{mult_sub}`, `{mult_color}`, `{fill_gradient}`, `{pvp_html}` construction, `{coin_gradient}`, `{coin_rim}`, etc. All are static strings TODAY, so no current injection exists, but the inconsistency invites future XSS. Recommend a lint rule: every `{var}` inside an f-string template that renders HTML must be `{esc(var)}` unless the variable comes from a sanitized builder function. The Ring 1 "theme strings spliced raw into HTML" concern does **not** manifest in this file specifically — themes are applied at the `wrap_card` layer in `atlas_html_engine.py`, not here. But the inline `style=` splices of `{mult_color}` etc. would become injection vectors if theme authors ever feed those variables.

2. **`card_style` parameter chain is narrow but open.** Blackjack `_build_blackjack_html` and `_build_blackjack_result_html` both accept `card_style: str = "cream"` and splice via `esc(card_style)`. That's safe for injection but NOT safe for CSS class selection — an attacker-controlled value could select an unintended class or null out styling. Grep confirms no cog currently passes `card_style` (defaults to "cream"). Low risk today, monitor on future wiring.

3. **Resource leak concerns from Ring 1 do NOT terminate here.** `render_card` in `atlas_html_engine.py` uses `try/finally` to release pages (lines 698-721). So a `set_content` exception from malformed HTML does return the page to the pool. Good.

4. **No blocking I/O inside `async` functions.** All render fns are `async def` and only `await render_card(html)`. HTML build helpers are sync (`_build_*`) — they do no I/O, just string construction. This is fine and preferable to unnecessary async overhead.

5. **Discord 3s timeout compliance is the cog's problem, not this module's.** This module takes no action on Discord state. Latency risk is page acquisition (timeout=10s) + playwright render (timeout=10s). A cold-start render can exceed 3s; cogs MUST `defer()` before calling these fns. Flag this to the cog reviewers but not a finding against this file.

6. **No `flow_wallet.debit/credit` calls in this file.** Purely rendering. The idempotency-reference-key concern does not apply here.
