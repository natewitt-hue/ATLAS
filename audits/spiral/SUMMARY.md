# ATLAS Spiral Adversarial Review — Summary

**Reviewed:** 2026-04-09
**Reviewer:** Claude Opus 4.6 (1M context) — substituted for Codex CLI after upstream rate-limit blocked the original plan.
**Files reviewed:** 89 (Ring 0: 1 / Ring 1: 35 / Ring 2: 27 / Orphans: 26)
**Failed reviews:** 0

> **READ THIS FIRST:** Per the codex review handling rule, this summary is presented FOR TRIAGE ONLY. No source code has been modified. Decide which findings (if any) to fix in follow-up sessions, then dispatch fix work as a separate task.

---

## Headline numbers

| Severity | Count | Files affected |
|----------|-------|----------------|
| **CRITICAL** | **196** | 80 / 89 |
| WARNING | 556 | 88 / 89 |
| OBSERVATION | 560 | 89 / 89 |
| **TOTAL** | **1,312** | **89** |

**Verdict breakdown:**

- **block** (refuse to ship as-is): 4 files
  - `polymarket_cog.py` — every wallet call omits `reference_key`, broken admin recovery paths
  - `casino_db.py` — `deduct_wager` and `refund_wager` both omit `reference_key`
  - `blackjack.py` — split-bust auto-stand bug, real-money shoe on Mersenne Twister
  - (informally) `flow_sportsbook.py` — 10 critical findings, biggest single-file detonation surface
- **needs-attention**: 84 files
- **approve**: 1 file (no material findings)

---

## Severity totals by ring

| Ring | Files | CRITICAL | WARNING | OBSERVATION | Total |
|------|-------|---------:|--------:|------------:|------:|
| **Ring 0** (`bot.py`)         |  1 |   2 |   8 |   8 |    18 |
| **Ring 1A** (Core orchestration) |  6 |   8 |  33 |  32 |    73 |
| **Ring 1B** (Flow & Economy)     |  8 |  34 |  73 |  60 |   167 |
| **Ring 1C** (Oracle / AI / Memory) | 10 |  34 |  89 |  90 |   213 |
| **Ring 1D** (Genesis / Sentinel / Build) |  7 |  19 |  53 |  51 |   123 |
| **Ring 1E** (Admin / Casino / Home) |  4 |  11 |  32 |  27 |    70 |
| **Ring 2A** (Core utilities)     | 15 |  23 |  87 |  89 |   199 |
| **Ring 2B** (Casino subsystem)   | 12 |  27 |  74 |  79 |   180 |
| **Orphan sweep**                 | 26 |  38 | 107 | 124 |   269 |
| **TOTAL** | **89** | **196** | **556** | **560** | **1,312** |

---

## Dead-code recommendations (orphan sweep)

The grep classification flagged 13 candidates initially. After per-file review by the orphan agents, only **ONE file is genuinely dead**:

| File | Status | Recommendation |
|------|--------|----------------|
| `embed_helpers.py` (63 LOC) | DEAD — superseded by `atlas_send.py` | **Move to `QUARANTINE/`** |

The other 12 "dead-candidate" files are CLI scripts (`if __name__ == "__main__":`) or pytest auto-discovered files that the static grep classifier doesn't detect. They are **LIVE** and should remain. However, several should be relocated for hygiene:

- All CLI scripts (`backfill_embeddings.py`, `espn_asset_scraper.py`, `upload_emoji.py`, `stress_test_*.py`) → should move to a `scripts/` subdirectory.
- `test_query_builder.py` at repo root has a **name collision** with `tests/test_query_builder.py` — pytest may silently skip ~95 tests OR fail collection. Resolve by deletion or rename.

The other 13 "orphans" classified LIVE are imported by active code via paths Argus's bot.py-rooted static scan didn't trace. Most notably **`flow_events.py` is used by 8+ active modules** including all sportsbook/casino/prediction cogs.

---

## Top 25 CRITICAL findings (ranked by blast radius)

### 1. `boss_cog.py` — sole permission gate for ~60 commissioner ops, ZERO inline checks downstream
**Location:** `boss_cog.py` + `flow_sportsbook.py`, `casino/casino.py:471-614`, `economy_cog.py:428-700`, `sentinel_cog.py *_impl methods`, `genesis_cog.py:1868-2038`, `real_sportsbook_cog.py:630-731`
**Risk:** Single-point-of-bypass. boss_cog's button-level `is_commissioner()` check is the ONLY thing protecting 60+ admin operations across 6 cogs. Any direct call to the underlying `_impl` method bypasses everything.
**Cite:** `audits/spiral/ring1/boss_cog.md` Critical #1

### 2. `casino_db.py` + every casino game — `deduct_wager` and `refund_wager` omit `reference_key`
**Location:** `casino/casino_db.py:1046-1086, 1089-1100` + propagates through all casino game files
**Risk:** CLAUDE.md mandates `reference_key` on every `flow_wallet.debit/credit` call. Without it, Discord interaction retries cause double-debits/credits. The TWO core money primitives in casino_db both omit it. Every casino bet placement and refund is exploitable.
**Cite:** `audits/spiral/ring2/casino_db.md` Critical #1, #2

### 3. `polymarket_cog.py` — every wallet call passes `subsystem_id` instead of `reference_key`
**Location:** `polymarket_cog.py` `_execute_prediction_buy`, `_execute_prediction_sell`, `update_balance`
**Risk:** Same idempotency violation as casino_db, but in the prediction-market subsystem. Real-money exposure on every buy/sell.
**Cite:** `audits/spiral/ring1/polymarket_cog.md` Critical #1

### 4. `polymarket_cog.py` — `self._resolve` does not exist (admin recovery hard-crashes)
**Location:** `polymarket_cog.py:3714` (`refund_sports_impl` calls method that was removed during `_finalize_resolved_pass` refactor)
**Risk:** When a market needs manual refund, the admin command immediately crashes. `_announce_resolutions` is similarly orphaned dead code.
**Cite:** `audits/spiral/ring1/polymarket_cog.md` Critical #2

### 5. `lore_rag.py` — production FAISS index missing on disk; unsafe binary deserialization
**Location:** `lore_rag.py` (38MB metadata blob loaded via Python's binary serialization library on first @mention)
**Risk:** Two compounding criticals: (1) The metadata loader uses unsafe binary deserialization on a 38MB blob — no signature, no checksum, no integrity check. Any tampering or corruption is unrecoverable. (2) `faiss_lore_db/lore_index.faiss` is **currently missing on disk** — production has been silently returning empty lore context for weeks with zero alerts.
**Cite:** `audits/spiral/ring1/lore_rag.md` Critical #1, Warning #3

### 6. `oracle_query_builder.py` — `Query.where()` interpolates raw strings into SQL, exposed to LLM agent sandbox
**Location:** `oracle_query_builder.py` `Query.where()`, `aggregate/select/group_by/sort_by()`, `recent_games_query`, `game_extremes`
**Risk:** Query builder is exported into the AI agent's sandbox via `oracle_agent.py:532`. The SELECT-only PRAGMA in `codex_utils.run_sql` does NOT stop UNION-based reads (`sqlite_master` disclosure, cross-table exfiltration). Untrusted strings reach SQL.
**Cite:** `audits/spiral/orphans/oracle_query_builder.md` Critical #1

### 7. `bot.py` — Cog loader silently degrades when MUST-load-first cogs fail
**Location:** `bot.py:241-266`
**Risk:** If `echo_cog` (loaded first per CLAUDE.md) or `setup_cog` (loaded second) raises during `bot.load_extension`, the loop catches the exception, prints a one-line error, and proceeds. The bot enters a "running but architecturally broken" state where downstream cogs use fallback persona stubs and ImportError channel routing defaults. Bot looks healthy externally.
**Cite:** `audits/spiral/ring0/bot.md` Critical #1

### 8. `bot.py` — `_startup_load()` can hang forever, leaves `_data_ready=False` permanently
**Location:** `bot.py:510-534`
**Risk:** `_startup_done = True` is set BEFORE the executor call. If `_startup_load()` hangs (MaddenStats API down, SQLite lock), `_data_ready` never becomes True, but `_startup_done` is already True so the next reconnect's `on_ready` skips reload. Bot is unrecoverable without full restart.
**Cite:** `audits/spiral/ring0/bot.md` Critical #2

### 9. `sentinel_cog.py` — positionchange approve/deny has NO commissioner check
**Location:** `sentinel_cog.py:2179-2254` (`positionchangeapprove_impl`, `positionchangedeny_impl`)
**Risk:** Per CLAUDE.md, position changes (HB→FB, WR→HB, HB→WR) require commissioner approval. The `_impl` methods have no `is_commissioner()` check inline. Any user with a code path that calls them gets unmediated privilege escalation.
**Cite:** `audits/spiral/ring1/sentinel_cog.md` Critical #1

### 10. `atlas_ai.py` — Streaming generator deadlocks on mid-stream SDK errors
**Location:** `atlas_ai.py:715-745`
**Risk:** `generate_stream` consumer awaits `queue.get()` until it sees `None`. Producer only puts `None` after `with stream:` exits cleanly. If the SDK raises mid-stream (network blip, rate limit, JSON parse error), the queue never sees `None` and the consumer hangs forever inside Discord's 15-min interaction window.
**Cite:** `audits/spiral/ring1/atlas_ai.md` Critical #1

### 11. `atlas_ai.py` — Raw SDK exceptions logged with no credential redaction
**Location:** `atlas_ai.py:98, 115, 467-469, 487-490, 523-525, 541-544, 624-625, 657-658, 681-682, 694-695, 786, 811-812, 821-822`
**Risk:** Multiple log sites pipe raw SDK exception text into structured logs. Anthropic and Google SDK errors can carry credential fragments and request metadata. Log files become a credential exfiltration vector if leaked.
**Cite:** `audits/spiral/ring1/atlas_ai.md` Critical #3

### 12. `flow_store.py` — `uuid.uuid4()` generated INSIDE `_purchase_item()` defeats idempotency
**Location:** `flow_store.py:202`
**Risk:** Reference key is generated FRESH on every retry. Discord button double-click → guaranteed double-debit. The canonical CLAUDE.md violation. Phase 2 (UI button wire-up) cannot ship until this is fixed.
**Cite:** `audits/spiral/ring1/flow_store.md` Critical #1

### 13. `flow_live_cog.py` — Dead bus subscriptions; settlement events never propagate
**Location:** `flow_live_cog.py:429, 535` (`sportsbook_result` and `prediction_result` topics)
**Risk:** flow_live_cog subscribes to two bus topics that have ZERO live publishers. The actual settlement event is `EVENT_FINALIZED` and flow_live_cog doesn't subscribe to it. Parlays and prediction resolutions never produce highlight cards.
**Cite:** `audits/spiral/ring1/flow_live_cog.md` Critical #1; Root cause: `audits/spiral/orphans/flow_events.md` (dataclass contracts have drifted)

### 14. `flow_live_cog.py` — `SessionTracker._active` race condition
**Location:** `flow_live_cog.py` `_on_game_result` + `session_reaper`
**Risk:** `_on_game_result` calls `asyncio.to_thread(record, ...)` which mutates `self._active` from a worker thread, while `session_reaper` mutates the same dict from the event loop. No lock. Concurrent button-click retries can create dual sessions, lose events, or resurrect just-deleted rows.
**Cite:** `audits/spiral/ring1/flow_live_cog.md` Critical #3

### 15. `blackjack.py` — Split + bust auto-stands the second hand
**Location:** `casino/games/blackjack.py:285-300, 415-423` (`HitButton.callback`)
**Risk:** When the player splits a pair and the FIRST hand busts, the callback finishes BOTH hands without letting the player act on the second. Second hand auto-stands at split-card value. Direct gameplay correctness bug — house collects wagers it shouldn't.
**Cite:** `audits/spiral/ring2/blackjack.md` Critical #1

### 16. `blackjack.py` — Real-money shoe uses `random.shuffle` (Mersenne Twister)
**Location:** `casino/games/blackjack.py:22, 54-57`
**Risk:** Mersenne Twister is not cryptographically random. With a few hundred observed cards, an attacker can reconstruct the seed and predict future cards. Should use `secrets.SystemRandom`.
**Cite:** `audits/spiral/ring2/blackjack.md` Critical #3

### 17. `blackjack.py` — Double/Split reuses `session.correlation_id`; ledger silently drops doubled stake
**Location:** `casino/games/blackjack.py:335-344, 360-370`
**Risk:** `wager_registry.register_wager` uses `INSERT OR IGNORE` on `UNIQUE(subsystem, subsystem_id)`. The doubled stake is silently dropped from the ledger because it shares the same `correlation_id`. House pockets the difference.
**Cite:** `audits/spiral/ring2/blackjack.md` Critical #4

### 18. `analysis.py` — `team_profile` stuffs an unawaited coroutine, then iterates it
**Location:** `analysis.py:182, 567-576`
**Risk:** `dm.get_last_n_games` is async but called without `await` from sync `team_profile`. Stores the coroutine as `result["recent"]`, then iterates it later → `TypeError: 'coroutine' object is not iterable`. EVERY `/stats team_profile` and `/h2h` call crashes.
**Cite:** `audits/spiral/ring2/analysis.md` Critical #1

### 19. `analysis.py` — `dm.df_team_stats is dm.df_standings` data_manager wiring bug
**Location:** `analysis.py:217-224, 497-500` (root cause in `data_manager.py:683-684`)
**Risk:** The two DataFrame names point to the SAME standings DataFrame, which doesn't include red-zone, third-down, or penalty columns. Every user query about those topics returns lies or empty output. Silent data fidelity bug; users make decisions on false output.
**Cite:** `audits/spiral/ring2/analysis.md` Critical #3

### 20. `oracle_analysis.py` — 22+ sync `run_sql` calls from async functions block the event loop
**Location:** `oracle_analysis.py` (22+ call sites)
**Risk:** `run_sql` is sync `sqlite3.connect` (per `codex_utils.py:33`). Called from `async def` Oracle handlers. Every Oracle analysis blocks the Discord event loop. `run_sql_async` already exists in `codex_utils` and is unused.
**Cite:** `audits/spiral/orphans/oracle_analysis.md` Critical #3

### 21. `god_cog.py` — `affinity reset` is irrecoverable with NO audit trail
**Location:** `god_cog.py:49-69`
**Risk:** No confirmation step, no prior-score snapshot, ephemeral success message. One click nukes user reputation state with zero traceability. A misclick on the wrong user is unrecoverable.
**Cite:** `audits/spiral/ring1/god_cog.md` Critical #1

### 22. `god_cog.py` — `rebuilddb` has no concurrency guard
**Location:** `god_cog.py:81-113`
**Risk:** Two GODs (or one double-click) can race two `sync_tsl_db()` rebuilds. Both write to the same `DB_PATH + ".tmp"`, wipe each other mid-build, race on the atomic swap. Pairs with `build_tsl_db.py` Critical #1 (connection leak on exception orphans tmp DB with Windows file lock).
**Cite:** `audits/spiral/ring1/god_cog.md` Critical #2

### 23. `casino.py` — `_casino_clear_session_impl` calls `refund_wager` with no `reference_key`
**Location:** `casino/casino.py:583-600`
**Risk:** No reference_key, no try/except, races the active session's own resolve path. Direct financial corruption vector in a commissioner action protected ONLY by boss_cog's button gate (see Top #1).
**Cite:** `audits/spiral/ring1/casino_casino.md` Critical #1

### 24. `casino.py` — `_casino_give_scratch_impl` uses raw DELETE with no audit / idempotency
**Location:** `casino/casino.py:602-614`
**Risk:** Raw DELETE on the scratch table with no audit log, no try/except, no idempotency, no rate limit. Uncapped free-money vector if double-fired.
**Cite:** `audits/spiral/ring1/casino_casino.md` Critical #2

### 25. `build_member_db.py` — 4 db_username collisions with non-deterministic resolution
**Location:** `build_member_db.py` `MEMBERS` constant + `get_alias_map()`
**Risk:** AST parse confirms 4 actual collisions (`TrombettaThanYou`, `Chokolate_Thunda`, `NEFF`, `Swole_Shell50`). `get_alias_map()` has no `ORDER BY active DESC` — SQLite row order determines which entry wins. Identity resolution is non-deterministic for these 4 users; queries about them return inconsistent results across bot restarts.
**Cite:** `audits/spiral/ring1/build_member_db.md` Warning #1 (severity should be CRITICAL given confirmed collisions)

---

## Cross-cutting patterns (architectural smells)

These patterns appeared in 3+ files, indicating systemic issues that one fix at the right layer would address:

### Pattern 1: `flow_wallet.debit/credit` called without `reference_key` (financial corruption)

**Files affected (15+):** `flow_store.py`, `polymarket_cog.py`, `casino/casino_db.py` (root cause), `casino/games/blackjack.py`, `coinflip.py`, `slots.py`, `crash.py`, `casino/casino.py` (`_casino_clear_session_impl`), `ledger_poster.py`, plus more in flow_sportsbook and economy.

**Root fix:** Add a hard runtime assertion in `flow_wallet.debit/credit`:
```python
def debit(uid, amount, *, reference_key, **kwargs):
    if not reference_key:
        raise ValueError("reference_key is required for debit")
```
Then audit the resulting failures and fix each call site. Single source of truth for the rule.

### Pattern 2: `_impl` methods without inline permission checks

**Files affected:** `sentinel_cog.py:2179-2254`, `awards_cog.py` createpoll/closepoll, `roster.assign`, `casino/casino.py` (~11 `_impl` methods), `economy_cog.py:428-700`, `genesis_cog.py:1868-2038`, `real_sportsbook_cog.py:630-731`, `flow_sportsbook.py` impl methods.

**Risk:** boss_cog.py button-level gates are the ONLY protection. Any direct caller bypasses everything. Defense-in-depth violation.

**Root fix:** Add a `@require_commissioner` decorator at the top of every `_impl` method that's intended to be commissioner-only. Audit boss_cog's gates against the decorated set.

### Pattern 3: `except Exception: pass` (or `except: pass`) in admin-facing paths

**Files affected (40+):** `bot.py` (8 sites), `atlas_home_renderer.py` (9 sites!), `flow_live_cog.py`, `polymarket_cog.py` (6 sites), `oracle_memory.py` (15+ sites), `crash.py` (3 silent `HTTPException`), `flow_store.py`, `awards_cog.py`, `god_cog.py`, `roster.py`, `casino_db.py` (5 silent ALTER TABLE), `lore_rag.py`, `affinity.py`, and many more.

**Risk:** CLAUDE.md explicitly prohibits this in admin-facing views. Real bugs become invisible.

**Root fix:** Project-wide grep for silent except patterns. Replace each with `log.exception(...)` at minimum, preferably with admin-channel notification on critical paths. The recurring `_notify_admin` helper proposed in `bot.md` Warning #7 would centralize the alerting.

### Pattern 4: Synchronous I/O in `async` functions (event loop blocking)

**Files affected:** `oracle_analysis.py` (22+ sites), `analysis.py` (12 sites in `_keyword_stats`), `intelligence.py` (`get_clutch_records`, `get_hot_cold` in 20-iteration loops), `awards_cog.py` `_save_polls_sync`, `boss_cog.py` (5 roster modals), `oracle_memory.py` (2000-blob vector search in pure Python), `atlas_home_cog.py` `gather_home_data` (8 serial sqlite queries), `casino/casino_db.py` `BEGIN IMMEDIATE` held across awaits, `oracle_renderer.py`.

**Risk:** Discord heartbeats can miss. Users see "interaction failed" messages. Compounds during peak hours.

**Root fix:** Project-wide audit for `sqlite3.connect`, `requests.`, `time.sleep`, `urllib.request`, `os.read` inside functions decorated with `async def`. Wrap in `asyncio.to_thread`. Convert per-cog as a multi-batch refactor.

### Pattern 5: `defer()` ordering bugs (Discord 3s timeout violations)

**Files affected:** `atlas_home_cog.py` `apply_btn`, `cancel_btn` (render BEFORE defer); `blackjack.py` Hit/Stand/Double/Split callbacks (no defer at all); modal handlers in genesis_cog, polymarket_cog, codex_cog (Gemini calls inside modals without defer); `boss_cog.py` 5 roster modals (defer happens after sync `dm.get_players()` call).

**Risk:** Silent UI failures, dropped interactions, user-visible "interaction failed" messages. In `apply_btn` cases, the SQLite write commits while the UI silently fails (state desync).

**Root fix:** Project-wide convention: every button/modal callback must call `defer()` BEFORE any I/O, AI call, or render. Add a lint or hook that flags violations.

### Pattern 6: HTML splice vulnerabilities (XSS chain in Playwright)

**Files affected:** `atlas_html_engine.py` (theme `status_gradient`/`card_border` spliced raw), `atlas_themes.py` (registry is plain mutable dict, no validation), `atlas_home_renderer.py` (theme palette colors in inline `style=""`), `sportsbook_cards.py` (logo URLs and team colors in `src=`/`style=`), `prediction_html_renderer.py` (Polymarket outcome labels in portfolio rows), `oracle_renderer.py` (`html.escape(raw).replace("&amp;", "&")` deliberately undoes escape), `casino_html_renderer.py` (slot/coinflip splices).

**Risk:** Today: "harmless" because themes are hardcoded by developers. The day a user-uploadable theme or custom skin lands, this becomes RCE-in-Playwright. The page pool means cross-render contamination.

**Root fix:** Add an import-time `_validate_theme()` + `MappingProxyType` freeze in `atlas_themes.py`. Add a project-wide rule: all values that reach HTML attributes/style blocks must go through `esc()`. Linting helps but human discipline + code review enforce.

### Pattern 7: Identity resolution bypassed via substring or strict equality

**Files affected:** `analysis.py` (`find_players` substring match — "Hill" matches "downhill"), `team_branding.py` (`mm_nickname_to_branding` lowercase equality, no fuzzy), `intelligence.py` (line 608-615 `nick_lower in uname` substring), `player_picker.py` (no fuzzy match), `roster.py` (`exclude_id` truthy test conflates None/0), `build_member_db.py` (4 db_username collisions with non-deterministic resolution).

**Root fix:** CLAUDE.md mandates `_resolve_owner()` fuzzy lookup + `tsl_members` alias map as single source of truth. Audit every "string match against player/team/owner name" site and migrate to the canonical helpers. Warn on substring matches without word boundaries.

### Pattern 8: SQL built via string formatting / LIKE injection

**Files affected:** `oracle_query_builder.py` (`Query.where()`, recent_games_query, game_extremes — exposed to LLM agent sandbox!), `codex_intents.py` (8 call sites bind `f"%{name}%"` patterns with no metachar escaping), `codex_utils.py` (`question` interpolated raw into prompts), `oracle_analysis.py` (`run_player_scout` LIKE-wildcard injection), `flow_audit.py` (`check_balance_drift` `!=` on potentially-float balances).

**Root fix:** A `_escape_like(s)` helper that escapes `%` and `_`, used at every LIKE site. For raw SQL builders, parameterize via `?` placeholders.

### Pattern 9: TOCTOU on resources/state

**Files affected:** `roster.assign()` (ghost ownership), `casino_db.py` `BEGIN IMMEDIATE` held across awaits, `flow_store._purchase_item` (stock decrement after debit), `polymarket_cog.bet_cmd` (live price refresh writes without re-checking status), `flow_live_cog.SessionTracker._active`, `affinity.update_affinity` (read-modify-write without lock), `build_tsl_db.py` (no concurrency guard on rebuild, two GODs can race), `lore_rag.add_single_message` (non-atomic temp-file rename), `store_effects.consume_effect`.

**Root fix:** Project-wide TOCTOU audit. Per-user `asyncio.Lock` registries. SQLite `BEGIN IMMEDIATE` for state-write critical sections (and don't hold it across awaits). Optimistic concurrency with version columns where appropriate.

### Pattern 10: Resource leaks (Playwright pages, SQLite connections)

**Files affected:** `atlas_html_engine.py` (`release()` shrinks pool to 0 on replacement failure, `_render_counts` keyed by `id(page)` leaks forever AND id reuse causes premature recycling), `build_member_db.py` (every read helper lacks try/finally on connection close), `build_tsl_db.py` (connection leak on exception orphans tmp DB Windows file lock), `flow_live_cog.py` (5 SQLite connection leaks), `flow_cards.py` (~9 separate `sqlite3.connect` per render), `casino_db.py` (10 separate connections per audit), `espn_odds.py` (`_request_lock` serializes ALL HTTP), `google_docs_writer.py`.

**Root fix:** Long-running Windows deployment will degrade. Centralize Playwright page lifecycle in `atlas_html_engine` with try/finally. Adopt a connection-pool-per-cog pattern or pass connections explicitly through helper APIs.

---

## Triage recommendation (priority order)

**Tier 1 — Fix BEFORE next push (4 files)**

These are BLOCK-verdict files with confirmed financial-correctness or privilege-escalation issues:

1. **`casino/casino_db.py`** — Add `reference_key` parameter to `deduct_wager` and `refund_wager`. Update all call sites. (Pattern 1 fix.)
2. **`polymarket_cog.py`** — Same fix as #1 for the prediction-market wallet calls. Plus delete the broken `self._resolve` admin recovery path or restore the missing method.
3. **`casino/games/blackjack.py`** — Fix the split-bust auto-stand logic. Switch shoe to `secrets.SystemRandom`. Fix Double/Split correlation_id collision (use `f"{correlation_id}_split"` etc).
4. **`flow_sportsbook.py`** — 10 critical findings, biggest single-file blast radius. Worth its own dedicated session.

**Tier 2 — Architectural fixes (1-2 sessions each)**

Fix one cross-cutting pattern at a time:

5. **Pattern 1 (reference_key everywhere)** — Add the runtime assertion in `flow_wallet`, then triage the resulting test/CI failures into commits. This single PR addresses 15+ critical findings.
6. **Pattern 2 (`_impl` permission decorators)** — Add `@require_commissioner` to every `_impl` method. Audit boss_cog gates against the new set. Addresses 6+ critical findings including the sentinel positionchange escalation.
7. **Pattern 3 (silent except blocks)** — Project-wide grep + replace with `log.exception` + admin-channel notification helper. Addresses 40+ findings.

**Tier 3 — High-value individual fixes**

8. **`bot.py` Critical #1**: cog loader halt-on-failure for echo_cog and setup_cog.
9. **`bot.py` Critical #2**: timeout on `_startup_load` OR move `_startup_done = True` to after completion.
10. **`lore_rag.py`**: replace the unsafe binary serialization with json/jsonl, restore the missing FAISS index, OR delete the lore subsystem entirely if it's not used.
11. **`oracle_query_builder.py`**: remove `Query.where()` from the LLM agent sandbox or add column allowlisting.
12. **`atlas_ai.py` streaming deadlock**: wrap producer in try/except/finally with sentinel posting.
13. **`build_member_db.py` collisions**: dedupe the 4 colliding db_usernames in MEMBERS, add `ORDER BY active DESC` to `get_alias_map()`.

**Tier 4 — Clean-up (whenever convenient)**

14. **`embed_helpers.py`** → move to QUARANTINE (only true dead file).
15. **CLI scripts** → relocate to `scripts/` subdirectory.
16. **`test_query_builder.py` name collision** → rename or delete the repo-root copy.
17. **constants.py** ATLAS_ICON_URL signed Discord CDN URL → permanent host.

---

## What this review did NOT cover

- The `tests/` subdirectory (only the legacy test scripts at repo root were reviewed via the orphan sweep).
- Files in `QUARANTINE/` (excluded by design per CLAUDE.md).
- The `cortex/` sibling project (only `google_docs_writer.py` was caught because cortex imports it).
- Third-party dependencies (`discord.py`, `pandas`, `playwright`, etc.).
- Performance benchmarking or actual race-reproduction tests — every "race condition" finding is from static analysis only.

---

## Next steps

**Per the codex review handling rule, this plan terminates here. NO source code has been modified.**

The user should:
1. Read this SUMMARY in full.
2. Decide which tier(s) to fix first.
3. Open a NEW session for fix work — do NOT mix triage and fix in one conversation.
4. Each per-file findings doc under `audits/spiral/{ring}/` contains the detailed fix recommendations cited above.

To browse findings by file, see:
- `audits/spiral/ring0/bot.md`
- `audits/spiral/ring1/<file>.md` (35 files)
- `audits/spiral/ring2/<file>.md` (27 files)
- `audits/spiral/orphans/<file>.md` (26 files) + `_classification.md`

To check specific cross-cutting patterns, grep across all per-file docs:
```bash
grep -r "reference_key" audits/spiral/         # Pattern 1
grep -r "is_commissioner" audits/spiral/        # Pattern 2
grep -r "except Exception" audits/spiral/       # Pattern 3
```
