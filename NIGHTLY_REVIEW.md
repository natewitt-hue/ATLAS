# ATLAS Nightly Audit — 2026-04-12

**Focus:** Cross-Cutting & Integration | **Recent commits:** 1 (CLAUDE.md + review refresh) | **Files scanned:** 89 | **Cross-module contracts verified:** 47

---

## CRITICAL — Fix Before Next Deploy

### [C-01] `real_sportsbook_cog.py` debit without `reference_key` — double-debit on retry
- **File:** `real_sportsbook_cog.py` L778
- **Risk:** User double-clicks bet button or Discord retries interaction → two debits for one bet. Financial ledger corruption. Violates CLAUDE.md rule explicitly documented as "causes silent data bugs."
- **Evidence:**
```python
new_balance = await flow_wallet.debit(
    uid, amt, "REAL_BET",
    description=f"Bet: {pick} ({bet_type})",
)  # ← no reference_key
```
- **Fix:**
```python
debit_ref = f"REAL_BET_DEBIT_{uid}_{espn_event_id}_{int(time.time())}"
new_balance = await flow_wallet.debit(
    uid, amt, "REAL_BET",
    description=f"Bet: {pick} ({bet_type})",
    reference_key=debit_ref,
)
```

---

## WARNINGS — Fix This Week

### [W-01] `flow_live_cog._on_sportsbook_result` is dead — bus topic never published
- **File:** `flow_live_cog.py` L428, L528
- **Impact:** Session recap for TSL sportsbook bet settlements never fires. The handler subscribes to `"sportsbook_result"` but no code in `flow_sportsbook.py` or anywhere else calls `flow_bus.emit("sportsbook_result", ...)`. All `flow_bus.emit` calls are `"game_result"` (casino) and `EVENT_FINALIZED` (predictions). Documented in CLAUDE.md gotchas but worth a concrete warning since the session tracker is silently inert for TSL bets.
- **Suggestion:** Either wire `flow_sportsbook.py` settlement path to emit `SportsbookEvent` on the bus, or remove the dead subscriber and document explicitly that TSL sportsbook does not trigger live session updates.

### [W-02] `polymarket_cog.update_balance()` violates idempotency rule and is dead code
- **File:** `polymarket_cog.py` L654–668
- **Impact:** `update_balance()` calls `flow_wallet.credit()` / `flow_wallet.debit()` without `reference_key`. The function is never called in production code. If someone invokes it in a settlement retry path, it will double-credit/debit silently.
- **Evidence:**
```python
async def update_balance(user_id, delta: int, *, contract_id=None):
    if delta >= 0:
        await flow_wallet.credit(uid, delta, "PREDICTION",  # ← no reference_key
                                 description="prediction market", ...)
```
- **Suggestion:** Delete the function entirely. All active wallet operations in polymarket_cog already use `reference_key` correctly.

### [W-03] `cortex/` directory — orphaned subsystem with no live callers
- **Files:** `cortex/cortex_analyst.py`, `cortex_engine.py`, `cortex_main.py`, `cortex_writer.py`
- **Impact:** No production code imports from `cortex/`. `cortex_main.py` is a standalone CLI tool (reads `TSL_Archive.db`, calls Gemini), not loaded as a cog and not in `_EXTENSIONS`. Contains silent `except: pass` patterns (`cortex_main.py` L166, `cortex_analyst.py` L85). Currently invisible to nightly audits.
- **Suggestion:** Move to `QUARANTINE/` or document as deliberate standalone CLI in CLAUDE.md.

### [W-04] `atlas_home_renderer.py` — 7 silent `except Exception: pass` blocks in DB read path
- **File:** `atlas_home_renderer.py` L87, L100, L112, L139, L161, L181, L200, L213
- **Impact:** When a DB column doesn't exist (schema drift), a query fails, or a table is missing, the home card silently shows 0/None for that stat with no log entry. Completely undiagnosable in production.
- **Suggestion:** Add `log.warning(f"[home] stat read failed: {e}")` inside each inner except. 5-minute fix; drastically improves diagnosability.

### [W-05] CLAUDE.md documents incorrect `flow.db` ownership
- **Impact:** CLAUDE.md Databases table says `flow.db = "TSL sportsbook bets, Flow economy transactions"`. Actual: `flow_sportsbook.py` (TSL sportsbook) writes `bets_table` to `flow_economy.db`. `flow.db` is the **new unified sportsbook_core schema** (`events`, `bets`, `parlays` — used by real sportsbook, polymarket, and cross-system grading). Developers debugging TSL bet issues will look in the wrong DB.
- **Fix:** See CLAUDE.md UPDATES section below — corrected in this audit.

### [W-06] `build_tsl_db.py` L433, L483, L494 — silent `except Exception: pass` in DB migration
- **File:** `build_tsl_db.py`
- **Impact:** These silently swallow errors during WAL setup and migration steps. If `tsl_history.db` is locked during startup rebuild, the bot continues with a partially-built DB and no error is logged.
- **Suggestion:** Replace with `except Exception as e: log.warning(f"[DB] non-fatal: {e}")`.

---

## OBSERVATIONS — Track for Later

### [O-01] `atlas_home_renderer.py` uses sync `sqlite3.connect` without WAL mode
Under concurrent reads (multiple users loading `/atlas` simultaneously), the sync connection can block on the async WAL writer's write lock, causing up to 10-second DB timeouts on the home card render.

### [O-02] `oracle_cog.py` L138 — `SupportCog` referenced, never implemented
`get_user_tier()` returns `"Elite"` for all users because `SupportCog` doesn't exist. TODO at L135. Either remove tier gating or implement SupportCog.

### [O-03] `reasoning.py` module-level import in `bot.py` but only used in `oracle_agent.py`
`bot.py` L133 imports `reasoning` — no direct usage in bot.py found. The actual consumers are in `oracle_agent.py`. Minor but adds startup dependency.

### [O-04] `analysis.py` and `intelligence.py` — undocumented utility modules
Both are imported by `oracle_cog.py` (`import analysis as an`, `import intelligence as ig`). Neither appears in CLAUDE.md's Module Map.

### [O-05] `pagination_view.py` L152, L240 — silent `except Exception: pass` on Discord interactions
Silently swallows errors when paginating bet history. Not critical but opaque in production.

### [O-06] `atlas_ai.py` L276 — silent pass in exception handler
Inside connection pool management. Should at minimum log a warning.

### [O-07] `sportsbook_core.py` L330 — `except Exception: pass` in migration guard
If `flow.db` is readable-but-corrupted, migration v7 re-runs unnecessarily and archives live data.

### [O-08] `casino_db.refund_wager()` — no `reference_key` on credit
`casino_db.py` L1092. Called for declined PvP challenges and crash round voids. Without a reference_key, a race condition or interaction retry could double-credit the refund. Low probability but non-zero.

---

## CROSS-MODULE RISKS

### [X-01] `flow_wallet.py` ↔ `wager_registry.py` lazy circular import
- **Caller:** `flow_wallet.py` L505 — lazy `import wager_registry` inside `setup_wallet_db()`
- **Callee:** `wager_registry.py` L24 — `from flow_wallet import DB_PATH` at module level
- **Risk:** Works at runtime because `setup_wallet_db()` runs after both modules initialize. Any future refactor hoisting the import to module level will cause `ImportError` at startup. Invisible to static analysis.
- **Recommendation:** Add comment at `flow_wallet.py` L504: `# Lazy import: wager_registry imports flow_wallet at module level — cannot hoist.`

### [X-02] `boss_cog.py` delegates to sub-cogs registered by OTHER modules' `setup()` functions
- **Caller:** `boss_cog.py` L2168 → `get_cog("TradeCenterCog")`; L2389 → `get_cog("ComplaintCog")`; L2442 → `get_cog("PositionChangeCog")`; L2177 → `get_cog("ParityCog")`
- **Callee:** `genesis_cog.py setup()` registers `TradeCenterCog`, `ParityCog`; `sentinel_cog.py setup()` registers `ComplaintCog`, `ForceRequestCog`, `PositionChangeCog`
- **Risk:** These cog names are NOT in `_EXTENSIONS`. If `genesis_cog` fails to load, boss_cog's Genesis admin commands all fail silently with "Cog not available." Mitigation: all `get_cog()` calls are null-checked.

### [X-03] `oracle_cog.py` lazy imports `gemini_sql`/`gemini_answer` from `codex_cog` at runtime
- **Caller:** `oracle_cog.py` L299 — lazy `from codex_cog import gemini_sql, gemini_answer`
- **Risk:** If `codex_cog` fails to load, `_HISTORY_OK = False` and all Oracle history queries return `None, None` silently. No Discord-visible indication to users.

### [X-04] `real_sportsbook_cog.py` writes to BOTH `real_bets` (flow_economy.db) AND `sportsbook_core.bets` (flow.db) — settlement only reads `flow.db`
- **Caller:** `real_sportsbook_cog.py` L783–800 — writes to both
- **Risk:** `real_bets` in `flow_economy.db` is a legacy admin-read mirror; authoritative settlement data is in `flow.db`. A partial write (one succeeds, one fails) leaves these inconsistent. The debit at L778 happens BEFORE both writes — exception handler at L807 refunds on write failure, which is correct, but the diagnostic hazard is real.

### [X-05] `flow_live_cog` subscribes to `"sportsbook_result"` but `flow_sportsbook.py` never emits it
- **Subscriber:** `flow_live_cog.py` L428
- **Publisher:** nowhere — zero calls to `flow_bus.emit("sportsbook_result", ...)` in entire codebase
- **Impact:** TSL sportsbook bet settlements never generate live engagement events. Feature gap, not a crash.

### [X-06] `atlas_home_renderer.py` sync sqlite3 vs casino_db.py async aiosqlite — same `flow_economy.db`
- **Risk:** Sync reader without WAL mode can block on async WAL writer under concurrent load. Not a data corruption risk; causes UI timeout.

---

## IMPORT CHAIN MAP

### Highest Fan-In
| Module | Direct Importers |
|--------|-----------------|
| `flow_wallet` | 5 (casino_db, economy_cog, flow_sportsbook, polymarket_cog, real_sportsbook_cog) |
| `setup_cog` | 12 (lazy `get_channel_id` calls across the codebase) |
| `data_manager` | 3 direct + lazy in most cogs |
| `atlas_ai` | 3 direct |
| `codex_utils` | 4 (bot.py, codex_cog, oracle_cog, oracle_query_builder) |

### Highest Fan-Out
- `oracle_cog.py` — 18+ imports including cross-cog lazy dependencies
- `boss_cog.py` — delegates to all major subsystems via get_cog()
- `genesis_cog.py` — 13+ imports

### Cycles
- `flow_wallet → wager_registry → flow_wallet` (lazy import breaks it at runtime — documented above)

---

## DB SCHEMA MISMATCHES

| Query Location | Table.Column Referenced | DB | Status |
|---------------|------------------------|----|--------|
| `atlas_home_renderer.py` L106 | `real_bets.discord_id` | `flow_economy.db` | ✅ schema at `real_sportsbook_cog.py` L205 |
| `flow_sportsbook.py` L3432 | `real_bets.status` | `flow_economy.db` | ✅ ok |
| `atlas_home_renderer.py` L117 | `casino_sessions.discord_id` | `flow_economy.db` | ✅ schema at `casino_db.py` L81 |
| `sportsbook_core.py` | `bets`, `events`, `parlays` | `flow.db` | ✅ defined in `_SCHEMA_SQL` |
| CLAUDE.md Databases table | `flow.db = "TSL sportsbook bets"` | N/A | ⚠️ doc mismatch — fixed in Phase 4 |

---

## DEAD CODE CANDIDATES

| Function / Module | File | Callers | Recommendation |
|-------------------|------|---------|----------------|
| `update_balance()` | `polymarket_cog.py` L654 | 0 | Delete — violates idempotency rule |
| `_on_sportsbook_result()` | `flow_live_cog.py` L528 | 0 (bus never published) | Keep with comment; wire publisher when TSL SB settlement added |
| `cortex/` (4 files) | `cortex/` | 0 live callers | Move to `QUARANTINE/` or document as standalone CLI |
| `get_user_tier()` (SupportCog path) | `oracle_cog.py` L133 | Internal only | Simplify — always returns `"Elite"`, remove dead SupportCog lookup |
| `reasoning` import | `bot.py` L133 | 0 direct uses in bot.py | Move to `oracle_agent.py` where it's actually needed |

---

## POSITIVE PATTERNS WORTH PRESERVING

1. **`boss_cog.py` null-checks every `get_cog()` call** — All 56 `get_cog()` calls have `if not cog: return await _send_cog_error(...)`. No silent AttributeErrors anywhere in the delegation layer.

2. **`sportsbook_core.py` per-event settlement locking** — `_settle_locks.setdefault(event_id, asyncio.Lock())` with early-return if locked prevents concurrent double-settlement. Exactly right.

3. **`flow_wallet.py` per-user asyncio locks with GC cleanup** — `_user_locks` dict with `_cleanup_idle_locks()` at `_LOCK_CLEANUP_THRESHOLD=500`. Prevents unbounded growth without manual lifecycle management.

4. **Lazy imports of `setup_cog.get_channel_id()`** — 12 modules import at call time. Prevents circular imports and allows setup_cog to fully initialize before any cog resolves channel IDs. Consistent across the codebase.

5. **Idempotency at the DB layer** — `reference_key TEXT UNIQUE DEFAULT NULL` in `transactions` table means the database itself enforces idempotency, not just application logic. Defense-in-depth.

---

## TEST GAPS

| Test Case | Type | What It Validates | Priority |
|-----------|------|-------------------|----------|
| `test_real_sportsbook_double_click` | integration | Debit called twice same user+event → second call is no-op | high (C-01 direct) |
| `test_sportsbook_result_bus_event` | integration | TSL bet settlement emits on flow_bus when wired | medium |
| `test_boss_cog_missing_cog` | unit | `get_cog()` returns None → `_send_cog_error()` fires, no crash | medium |
| `test_settlement_partial_write` | integration | `write_bet` success + `real_bets` INSERT failure → debit refunded | high (X-04) |
| `test_home_renderer_schema_drift` | unit | Missing DB column → defaults returned, no exception propagated | low |

---

## METRICS

| Metric | Value |
|--------|-------|
| Files scanned | 89 |
| Cross-module contracts verified | 47 |
| Critical findings | 1 |
| Warnings | 6 |
| Observations | 8 |
| Cross-module risks | 6 |
| DB schema mismatches | 1 (documentation only) |
| Dead code candidates | 5 |
| Import chain issues | 3 (1 circular, 2 fragile) |

**Overall integration health:** Module boundary contracts are well-maintained — every `get_cog()` is null-checked, wallet idempotency is enforced at the DB layer in most paths, and the DB split between `flow.db` and `flow_economy.db` is architecturally coherent even if confusingly documented. The single critical issue (real sportsbook debit without `reference_key`) is a one-line fix. The most significant systemic gap is the `sportsbook_result` bus topic dead zone — TSL bet settlement is the only major subsystem that doesn't participate in the live engagement pipeline.

**Next audit focus:** Monday — Flow & Economy (flow_wallet.py, flow_sportsbook.py, economy_cog.py, flow_audit.py, wager_registry.py, flow_events.py)

---

## CLAUDE.md UPDATES

### Changes Made

**1. Databases table — corrected `flow.db` and `flow_economy.db` descriptions**

Previous: `flow.db` = "TSL sportsbook bets, Flow economy transactions"  
Corrected: `flow.db` = "Unified event/bet/parlay schema (sportsbook_core.py) — real sportsbook, polymarket, cross-system grading"  
Corrected: `flow_economy.db` description expanded to clarify it holds `bets_table` (TSL sportsbook legacy), `real_bets` (real sports bets), wallet transactions, and casino tables.
