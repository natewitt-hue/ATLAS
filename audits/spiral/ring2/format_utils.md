# Adversarial Review: format_utils.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 14
**Reviewer:** Claude (delegated subagent)
**Total findings:** 4 (0 critical, 2 warnings, 2 observations)

## Summary

Tiny helper, mostly fine — but the K/M bucket boundary produces strings like `"$1000.0K"` right below the M threshold and `"$1.0M"` immediately above, which is a visible formatting glitch in the casino/prediction UI. Also silently coerces `bool` through `float()` and returns an em-dash on error without logging.

## Findings

### WARNING #1: K/M bucket boundary emits "$999.9K"..."$1000.0K" strings instead of rolling to M
**Location:** `C:/Users/natew/Desktop/discord_bot/format_utils.py:10-13`
**Confidence:** 0.9
**Risk:** For any `v` in the range `[999_950, 1_000_000)`, the `K` branch produces `f"${v/1000:.1f}K"` which rounds to `"$1000.0K"`. This is visible in the prediction market detail card (`prediction_html_renderer.py:722`, `:819`) and anywhere volume is shown.
**Vulnerability:** The threshold comparison uses the unrounded value (`abs(v) >= 1_000_000`), but the formatted string uses a rounded representation. Classic "format/compare mismatch" bug — a value of `999_950` formats to `"$1000.0K"` instead of `"$1.0M"`. Similarly, `999` formats to `"$999"` but `999.5` formats to `"$1000"` (and then cascades into the K bucket for `999_500` as `"$999.5K"` which is fine, but `999_950` round-trips through the glitch).
**Impact:** Ugly/confusing volume labels on prediction market cards at exactly the moment the market crosses a milestone ($1M volume is a marketing beat). Users see `"$1000.0K"` instead of `"$1.0M"`.
**Fix:** Compare against the rounded boundary, e.g.:
```python
if abs(v) >= 999_950:
    return f"${v/1_000_000:.1f}M"
if abs(v) >= 999.5:
    return f"${v/1_000:.1f}K"
```
Or better: round first, then pick a bucket.

### WARNING #2: `bool` values silently coerce to 0.0/1.0
**Location:** `C:/Users/natew/Desktop/discord_bot/format_utils.py:6-8`
**Confidence:** 0.75
**Risk:** `float(True)` returns `1.0` and `float(False)` returns `0.0` — `bool` is a subclass of `int`, so `isinstance(True, (int, float))` is true and no `TypeError` is raised. A caller that accidentally passes a boolean condition (`fmt_volume(is_open)`) gets `"$0"` or `"$1"` instead of the em-dash sentinel.
**Vulnerability:** The function presents `"—"` as the "invalid input" contract but silently accepts booleans. If upstream volume math degrades to a bool-ish value (e.g., `v = volume and volume > 0`), this bug is invisible in the rendered card.
**Impact:** Silent data bug — volume displays as `$0` or `$1` with no error logged. Hard to notice because it looks like a real value.
**Fix:** Reject booleans explicitly: `if isinstance(v, bool): return "—"` before the `float(v)` call.

### OBSERVATION #1: Em-dash sentinel silently swallows non-numeric input with no log
**Location:** `C:/Users/natew/Desktop/discord_bot/format_utils.py:6-9`
**Confidence:** 0.7
**Risk:** When a caller passes `None`, `"N/A"`, or a `Decimal`-that's-NaN, the function returns `"—"` with no trace. In the prediction market card, a mass-miscalculated volume field silently shows as an em-dash across every card. No warning log, no traceback, no counter.
**Vulnerability:** Silent failure smell — the em-dash is indistinguishable from "legitimately zero or unknown" and "upstream bug produced garbage."
**Impact:** Observability gap — a data pipeline bug upstream is hidden by the helper.
**Fix:** Add a single `log.debug(...)` in the except branch (or at minimum a `# noqa: BLE001` comment and a note pointing to the intentional swallow).

### OBSERVATION #2: Hardcoded `$` prefix and no locale/unit arg
**Location:** `C:/Users/natew/Desktop/discord_bot/format_utils.py:11-14`
**Confidence:** 0.6
**Risk:** Every caller is forced to prefix with `$`. The function is named `fmt_volume`, not `fmt_usd_volume`. If any non-USD volume surface ever needs this (e.g., FlowCoin volume, XP volume), a duplicate helper will be added — which is exactly the situation that birthed this module per `docs/archive/AUDIT_REPORT_2026_03_18.md`.
**Vulnerability:** Premature coupling to a currency symbol in a "shared" helper.
**Impact:** Naming/contract smell. Likely to produce the exact duplicate-helper bug the file was extracted to solve.
**Fix:** Accept an optional `unit: str = "$"` (prefix) and/or `suffix: str = ""` param, or rename the function to `fmt_usd_volume`.

## Cross-cutting Notes

The boundary-rounding bug pattern (`if abs(v) >= 1_000_000`) likely appears in any other `_fmt_*` helpers in `prediction_html_renderer.py`, `sportsbook_cards.py`, or `atlas_home_renderer.py`. Worth a grep for `.1f` followed by a K/M/B suffix.
