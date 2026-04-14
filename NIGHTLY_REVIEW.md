# ATLAS Nightly Review — Casino & Rendering Subsystem
**Date:** 2026-04-14
**Audit Task:** `audit-tuesday-casino`
**Scope:** 17 focus files — Casino games, casino DB, renderers, HTML engine, style tokens
**Passes:** Anti-pattern scan · Logic trace · Cross-module contract · Performance · Security/Data integrity

---

## Phase 1 — Recent Commit Triage

No changes to any focus file in the last 24 hours. Last 5 commits were audit docs and CLAUDE.md documentation updates only. Audit proceeds against current baseline.

---

## Phase 2 — Deep Audit Findings

### CRITICAL — 1 issue

---

#### C-01 · `casino/games/slots.py` L216–250 — Wager deducted before render; render failure leaves player in limbo

**Severity:** CRITICAL — player balance debited with no settlement record on Playwright failure
**Rule violated:** CLAUDE.md — settlement path must be atomic; orphan wager reconciliation is last resort, not a design target

Current execution order in `play_slots()`:

```python
# L216
await deduct_wager(uid, wager, ...)          # Balance already debited
# L227
card_bytes = await render_slots_card(...)     # RENDER — can fail (pool exhaustion, Playwright crash)
# L250
await process_wager(uid, wager, multiplier)  # NEVER REACHED if render fails
```

If `render_slots_card()` raises `asyncio.TimeoutError` (pool full) or any Playwright exception, `deduct_wager()` has already committed, `process_wager()` is never called, and the player's wager is orphaned. Orphan reconciliation runs every 10 minutes minimum — the player sees a deducted balance with no slot result during that window.

**Fix:** Move `render_slots_card` after `process_wager`, or wrap the render in `try/except` with explicit `refund_wager` on failure:

```python
await deduct_wager(uid, wager, ...)
try:
    await process_wager(uid, wager, multiplier)
    card_bytes = await render_slots_card(...)
except Exception:
    await refund_wager(uid, wager, correlation_id=correlation_id)
    raise
```

Contrast: `crash.py` wraps all render calls in `try/except discord.HTTPException` with text fallback. Same pattern needed in slots.
---

### WARNINGS — 4 issues

---

#### W-01 · `casino/games/coinflip.py` L290, L309 — `refund_wager()` missing `correlation_id` in decline and timeout paths

Both `decline_btn.callback` (L290) and `ChallengeView.on_timeout` (L309) call:

```python
await refund_wager(self.challenger_id, self.wager)
```

`self.challenger_correlation_id` is stored on the view but never forwarded. `refund_wager` calls `flow_wallet.credit()` internally — without `reference_key`, a retry (network glitch on the refund response) credits the challenger twice.

**Fix:** `await refund_wager(self.challenger_id, self.wager, correlation_id=self.challenger_correlation_id)` in both locations.

---

#### W-02 · `db_migration_snapshots.py` L49–64 — Synchronous `sqlite3.connect()` in `take_daily_snapshot()`

`take_daily_snapshot()` opens a blocking `sqlite3.connect()` and runs N individual `INSERT OR REPLACE` statements in a loop (one per user). CLAUDE.md documents this explicitly: "Must be called via `asyncio.to_thread()` if ever wired into async context."

Currently no async caller wires this function, so no immediate crash — but any future cog that calls it directly will block the event loop for every user in the system.

**Fix:** Either convert to `aiosqlite`, or wrap the entire function body in `asyncio.to_thread()` at the call site. Document the blocking contract in the function's docstring.

---

#### W-03 · `casino/casino.py` L83–84 — Silent `except Exception: pass` in `post_to_ledger()`

The admin notification fallback in `post_to_ledger` catches all exceptions silently:

```python
except Exception:
    pass  # no log
```

CLAUDE.md prohibits `except Exception: pass` in admin-facing views. If the primary ledger write succeeds but the admin channel notification fails (e.g., channel ID misconfigured), the failure is invisible. Errors will not surface in logs or monitoring.

**Fix:** Replace `pass` with `log.warning("Admin ledger notification failed", exc_info=True)`. One line; no behavior change.

---

#### W-04 · `casino/casino_db.py` ~L1563 — PvP coinflip win bypasses `process_wager()`; no streak or jackpot credit

`resolve_challenge()` credits the PvP winner via `flow_wallet.credit()` directly — it does not call `process_wager()`. This means:
- Winner's streak counter is never updated (win doesn't count toward Momentum)
- No jackpot pool contribution from PvP wins
- Win is invisible to session recap stats

This is a consistent behavior gap, not a correctness bug — PvP coinflip is intentionally off the main wager path. Document in a comment if intentional, or route through `process_wager()` to unify behavior.

---

### Cross-Module Risks — 2 issues

---

#### X-01 · `casino/games/blackjack.py` L337, L362 — Misleading "Insufficient funds" on any Double/Split deduct failure

Both `DoubleButton.callback` and `SplitButton.callback` catch `deduct_wager` failures as:

```python
except Exception:
    await interaction.followup.send("Insufficient funds", ephemeral=True)
```

Any DB connection error, `aiosqlite` exception, or unexpected state surfaces as "Insufficient funds" to the player. On a connection error, the player retries thinking they're broke — and may actually succeed on retry, potentially double-deducting if the first call partially committed.

**Fix:** Narrow the `except` to catch the specific low-balance error condition, or at minimum log the actual exception before sending the user message.

---

#### X-02 · `casino/games/slots.py` L97 — Magic constant `_EXPECTED_WEIGHT_TOTAL = 80` in module-level assert

```python
assert _actual_weight_total == _EXPECTED_WEIGHT_TOTAL  # _EXPECTED_WEIGHT_TOTAL = 80
```

The `80` is a hardcoded constant. If a developer adds a new symbol row to `SLOT_ICON_CONFIG` and correctly updates the weights, this assert will fire at import time with an opaque `AssertionError` and no message. Bot fails to load entirely.

**Fix:** Either compute `_EXPECTED_WEIGHT_TOTAL` dynamically from the config table, or add an assert message: `assert ..., f"Slot weight table misconfigured: expected 80, got {_actual_weight_total}"`.

---

### Observations — 3 notes

---

#### O-01 · `casino/games/crash.py` L51 — `MAX_CRASH_MULTIPLIER = 1000.0` is unreachable

`_generate_crash_point()` caps the crash point at `100.0` internally. The module-level `MAX_CRASH_MULTIPLIER = 1000.0` is referenced in `min(self.round_obj.current_mult, MAX_CRASH_MULTIPLIER)` during the tick loop — but since the crash point itself never exceeds 100x, the 1000x cap never fires.

Either update the constant to `100.0` to match actual behavior, or remove the redundant cap from the tick loop.

---

#### O-02 · `casino/renderer/highlight_renderer.py` ~L82–146 — Local `_wrap_card()` embeds `<style>` in body

`highlight_renderer.py` defines its own `_wrap_card()` that prepends `status_override_css` as an inline `<style>` block inside the HTML body before calling `atlas_html_engine.wrap_card()`. All other renderers inject status CSS as a `status_class` parameter. This is architecturally inconsistent but functionally correct. Low priority — note for when `atlas_html_engine.wrap_card()` signature is next extended.

---

#### O-03 · `casino/casino_db.py` `refund_wager()` — Credit with no `reference_key` (error-path-only)

`refund_wager()` at L1089-1100 calls `flow_wallet.credit()` without `reference_key`. This function is only called in exception handlers (Blackjack `on_timeout`, Crash refund sweep, Slots render-path error). The existing `self.resolved` flag at the call site prevents double-execution from the view layer, but provides no DB-level idempotency. Lower priority than W-01 (coinflip) because these paths are exception-only, not user-triggered retry paths.

---

## Phase 3 — Anti-Pattern Scan Results

| Pattern | Files matched | Notes |
|---------|--------------|-------|
| `except Exception: pass` | 1 | W-03 — `casino/casino.py` L83 |
| bare `except:` | 0 | Clean |
| `time.sleep()` in focus files | 0 | `asyncio.sleep(1.5)` in blackjack — acceptable UX |
| `eval()` / `exec()` in focus files | 0 | Clean |
| Blocking `sqlite3.connect()` in async-reachable code | 1 | W-02 — `db_migration_snapshots.py` |
| `flow_wallet.debit()` without `reference_key` | 0 | Clean — `deduct_wager()` handles this correctly |
| `flow_wallet.credit()` without `reference_key` (settlement) | 2 | W-01 coinflip refund, O-03 `refund_wager()` |
| Render before settlement | 1 | C-01 — `slots.py` |
| SQL string formatting / injection vectors | 0 | All queries use parameterized `?` bindings |

---

## Phase 4 — CLAUDE.md Health Check

| Item | Status |
|------|--------|
| `flow_wallet.debit()` idempotency | ENFORCED — `deduct_wager()` correctly uses `reference_key` |
| `flow_wallet.credit()` idempotency (settlement) | VIOLATED — `refund_wager()` (O-03), coinflip refund paths (W-01) |
| `except Exception: pass` prohibited in admin views | VIOLATED — `post_to_ledger()` (W-03) |
| `db_migration_snapshots.py` async context note | VALID — blocking sync acknowledged; no current async caller, risk is latent |
| `sportsbook_cards._get_season_start_balance()` OperationalError wrap | OUT OF SCOPE for Tuesday audit (covered Monday) |
| All other CLAUDE.md rules for focus files | VERIFIED — no additional gaps found |

No CLAUDE.md updates required from tonight's audit.

---

## Positive Patterns (Confirmed Good)

- **`casino_db.py` `process_wager()`** — Full `BEGIN IMMEDIATE` atomicity, streak → bonus → session → credit → jackpot → COMMIT order is correct. All credits use `reference_key`. Well-structured.
- **`casino_db.py` `deduct_wager()`** — User lock + `BEGIN IMMEDIATE` + `wager_registry` registration. Correct TOCTOU prevention.
- **`atlas_html_engine.py` `render_card()`** — `finally` block always releases page pool slot. No resource leaks. Pool reconnect/re-warm on browser crash is production-grade.
- **`casino/games/crash.py`** — Cashout TOCTOU sentinel (`player.cashed_out = True` before `await`), render failure text fallback, full exception handler with per-player refund sweep.
- **`casino/games/slots.py` `_spin_controlled()`** — Outcome computed server-side before visual reel generation. Correct provably-fair / RTP-control pattern.
- **`casino_db.py` `claim_scratch()`** — Re-verifies claim status inside `BEGIN IMMEDIATE` before any write. No double-claim possible.
- **`atlas_html_engine.py` `PagePool`** — LRU font cache (50 entries), dead-page replacement on release, `asyncio.TimeoutError` on 10s pool starvation. All edge cases handled.
- **CSPRNG crash point generation** — `secrets.token_bytes(8)` → SHA256 → deterministic crash point. Cryptographically safe.

---

## Summary Table

| ID | File | Line | Severity | Title |
|----|------|------|----------|-------|
| C-01 | casino/games/slots.py | 216-250 | CRITICAL | Render before process_wager — orphan on Playwright failure |
| W-01 | casino/games/coinflip.py | 290, 309 | WARNING | refund_wager missing correlation_id in decline/timeout |
| W-02 | db_migration_snapshots.py | 49-64 | WARNING | Blocking sqlite3 in async-reachable take_daily_snapshot |
| W-03 | casino/casino.py | 83-84 | WARNING | Silent except pass in post_to_ledger notification fallback |
| W-04 | casino/casino_db.py | ~1563 | WARNING | PvP coinflip win bypasses process_wager — no streak/jackpot |
| X-01 | casino/games/blackjack.py | 337, 362 | RISK | Misleading Insufficient funds swallows all deduct errors |
| X-02 | casino/games/slots.py | 97 | RISK | Magic weight constant assert fails silently on symbol table change |
| O-01 | casino/games/crash.py | 51 | OBS | MAX_CRASH_MULTIPLIER = 1000.0 is unreachable (capped at 100x) |
| O-02 | casino/renderer/highlight_renderer.py | ~82 | OBS | Local _wrap_card injects style into body — inconsistent pipeline |
| O-03 | casino/casino_db.py | ~1093 | OBS | refund_wager credit has no reference_key (exception paths only) |

**Totals:** 1 critical · 4 warnings · 2 cross-module risks · 3 observations

---

## Recommended Fix Order

1. **C-01** — Reorder slots.py settlement: `process_wager` before `render_slots_card`, or add explicit `refund_wager` on render failure. High severity, isolated change.
2. **W-01** — Pass `correlation_id=self.challenger_correlation_id` to both `refund_wager` calls in coinflip.py. Two-line fix, high idempotency value.
3. **W-03** — Replace `except Exception: pass` in `casino.py` `post_to_ledger` with `log.warning(...)`. One line.
4. **X-01** — Narrow `except Exception` in blackjack Double/Split to balance-check errors; log actual exception. Prevents misleading player messages.
5. **X-02** — Add message to slots weight assert. One line.
6. **W-02** — Add docstring to `take_daily_snapshot()` documenting blocking contract. Verify no async callers exist before considering conversion.
7. **W-04** — Decision: document PvP streak exclusion intentionally, or route through `process_wager()`.

---

*Generated by audit-tuesday-casino scheduled task · ATLAS v2.x · 2026-04-14*
