# Adversarial Review: ledger_renderer.py

**Verdict:** approve
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 404
**Reviewer:** Claude (delegated subagent)
**Total findings:** 7 (0 critical, 2 warnings, 5 observations)

## Summary

HTML→PNG renderer for ledger slips via the unified atlas_html_engine pipeline. Cleanly separated from DB concerns (no sqlite queries, no wallet calls). Page pool handling is correct by virtue of delegating to `render_card` which uses try/finally. Main concerns are around type coercion assumptions (all numeric args presumed `int` — but upstream callers may pass `float`, which blows up the `{val:,}` format), and a missing validation that `txn_id=0` renders as empty.

## Findings

### WARNING #1: `{wager:,}` formatting raises `TypeError` if upstream passes a float

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:262,266,270,278,334,338,345`
**Confidence:** 0.80
**Risk:** The type hint says `wager: int`, `payout: int`, `new_balance: int`, `amount: int`, `balance_after: int`. But Python does not enforce type hints. If any caller passes a `float` (e.g., `payout=1.5 * wager` without `int()` wrap), the f-string `{wager:,}` works for floats, but `_format_amount(value)` on line 188 does `return f"+{value:,}"` — which for a float produces `"+1,500.0"` with a decimal. Worse, `_pl_color(pl)` on line 172 does `value > 0` comparisons which work for floats but produce unexpected display values.
**Vulnerability:** Per the focus block: "Float vs int balance corruption in `flow_economy.db`". This renderer is the terminal display point for balance values. A float slipping through from `process_wager` would produce "BAL: 1,500.0" in the ledger image, visibly breaking the aesthetic.
**Impact:** Visible display drift; indirectly masks upstream float corruption.
**Fix:** Explicitly coerce at the top of `_build_casino_html` and `_build_transaction_html`: `wager = int(wager); payout = int(payout); new_balance = int(new_balance)`. Raise `TypeError` if NaN/inf.

### WARNING #2: HTML escape applied to ATLAS commentary but not validated for length

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:194-205`
**Confidence:** 0.55
**Risk:** `_commentary_html(commentary)` accepts arbitrary-length strings and escapes them. No cap. If the AI commentary is 2000 characters (unlikely but not impossible for Gemini output), the rendered image can overflow the card height or make Playwright's `bounding_box` math produce a clipped screenshot that cuts off the footer.
**Vulnerability:** No length guard on AI-produced text fed into fixed-width rendering.
**Impact:** Long commentary blows up the card layout.
**Fix:** Add `if len(commentary) > 280: commentary = commentary[:277] + "..."` before passing to `_esc`.

### OBSERVATION #1: `SOURCE_INFO.get(source, SOURCE_INFO.get("ADMIN"))` has defensive redundancy with a line-298 None guard

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:298-300`
**Confidence:** 0.55
**Risk:** `info = SOURCE_INFO.get(source, SOURCE_INFO.get("ADMIN"))` — the default is the ADMIN entry, which exists. Then line 299 checks `if info is None: raise ValueError(...)` — which is unreachable because the default is always present in the SOURCE_INFO dict. The check is defensive but defeats the point of the default. If you WANT to reject unknown sources, remove the default and rely on the raise. If you WANT to fall back to ADMIN, remove the raise.
**Vulnerability:** Dead code / intent unclear.
**Impact:** None functional; confusing for maintainers.
**Fix:** Decide which behavior is intended. If reject, remove the default and do `info = SOURCE_INFO.get(source); if info is None: raise`. If fall back, remove the raise.

### OBSERVATION #2: `GAME_INFO.get(game_type, ...)` silently uses game_type as the label

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:221`
**Confidence:** 0.40
**Risk:** If a new game type (e.g., "crash") is added and someone forgets to update `GAME_INFO`, the ledger will render with `label=game_type.upper()` and `icon="\u2B22"` (a lozenge). This is a graceful fallback but silently hides the missing config.
**Vulnerability:** Missing game icons go unnoticed.
**Impact:** Cosmetic; inconsistent branding.
**Fix:** Log a warning when the fallback is hit, or raise in dev mode.

### OBSERVATION #3: `GAME_INFO` missing "crash" — the game exists per the cog load order

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:17-25`
**Confidence:** 0.70
**Risk:** Line 22 has `"crash": {"label": "CRASH", "icon": "\u25B2"}` — actually, this IS present. Re-checking... yes, "crash" is there. This observation is withdrawn. Striking.

**Retracted.** Line 22 does define "crash". No finding.

### OBSERVATION #4: `time_str = datetime.now(timezone.utc).strftime("%H:%M UTC")` is reset on every render

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:229,304`
**Confidence:** 0.60
**Risk:** The ledger timestamp is set at RENDER time, not at TRANSACTION time. If the transaction happens at 23:59 UTC and the render is delayed (queue backlog, pool exhaustion) until 00:02 UTC, the ledger slip shows a different time than when the money moved. For a financial ledger, the transaction time should be preserved through the caller.
**Vulnerability:** Display-time drift; forensic reconstruction becomes harder.
**Impact:** Ledger timestamps are approximate, not authoritative.
**Fix:** Accept `transaction_time: datetime | None = None` as an explicit parameter and fall back to `datetime.now(timezone.utc)` only if omitted. Document that callers should pass the real txn time.

### OBSERVATION #5: `txn_str` is empty string if `txn_id` is 0, but 0 is a valid SQLite rowid

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:230,305`
**Confidence:** 0.50
**Risk:** `txn_str = f"TXN #{txn_id}" if txn_id else ""`. Python's `bool(0) is False`, so `txn_id=0` renders as an empty string. SQLite `INTEGER PRIMARY KEY` starts at 1 by default so this is probably fine, but if anyone sets `PRAGMA autoincrement` or starts rowids at 0, the first transaction shows no TXN number.
**Vulnerability:** Edge case on rowid=0.
**Impact:** Rare; cosmetic.
**Fix:** Use `if txn_id is not None` instead of `if txn_id`.

### OBSERVATION #6: CSS is inlined into the body — reparsed on every render

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:41-165,245,316`
**Confidence:** 0.35
**Risk:** `_LEDGER_CSS` is a 123-line style block concatenated into every HTML body via f-string. Playwright has to parse the CSS on every `set_content` call. The pool pre-warms pages but does not cache compiled styles across calls. For high-throughput rendering (e.g., 100 ledger slips per minute during active casino hours), this is wasted work.
**Vulnerability:** Throughput concern.
**Impact:** Minor performance; not a bug.
**Fix:** Consider moving shared CSS into the engine's `wrap_card` template once, so it loads once per page warmup.

### OBSERVATION #7: `_esc = esc` alias exists only for grep discoverability

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/renderer/ledger_renderer.py:168-169`
**Confidence:** 0.30
**Risk:** The comment says "kept as alias for grep-ability". That is a legitimate reason but creates two names for the same function. If someone updates `esc` in the engine and forgets this file, both names still work — fine. But if they rename `esc` and this file, the alias makes the rename feel inconsistent.
**Vulnerability:** Minor naming drift.
**Impact:** None functional.
**Fix:** Drop the alias and import `esc` directly, OR commit to using `_esc` and drop the `esc` import.

## Cross-cutting Notes

This file is much cleaner than the casino game files — no wallet calls, no RNG, no interaction handling. The main hazards are UPSTREAM (type coercion assumptions about integer values) and DOWNSTREAM (the rendered text is user-visible and part of the audit trail). The ATLAS-specific attack surface concerns (SQL connection lifecycle, wallet idempotency) do not apply because this file does not touch the DB or wallet. The pool management concern does not apply because `render_card` in `atlas_html_engine.py:688-721` uses try/finally to release pages properly.

The biggest cross-cutting note: this renderer is the ONLY place where a float-vs-int mismatch in `flow_economy.db` would be visible. Upstream modules should be audited for any code path that could write a float into `balance_after` or `amount`.
