# ATLAS Full Codebase Audit Report

> **Date:** 2026-03-14
> **Auditor:** Claude Code (Opus 4.6)
> **Version audited:** ATLAS v2.1.0 (`bot.py` line 165)
> **Total files:** 62 active .py files (~30,170 lines) + 18 quarantine archive files
> **Databases:** 4 active SQLite DBs (tsl_history.db, sportsbook.db, flow_economy.db, TSL_Archive.db)

---

## Section A: Boot Health

**A1. [OK] bot.py boots cleanly.** All critical imports (`discord`, `google.genai`, `data_manager`, `reasoning`, etc.) are unconditional. Optional modules (`intelligence`, `lore_rag`, `affinity`, `echo_loader`) are wrapped in try/except with graceful fallbacks.

**A2. [OK] Cog load order is correct.** `echo_cog` loads first, `setup_cog` loads second. All other cogs follow. `commish_cog` is commented out (replaced by `boss_cog`).

**A3. [HIGH] sentinel_cog.py Gemini client init at module load.** Line 622: `_gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))` executes at import time. If `GEMINI_API_KEY` is missing from `.env`, this doesn't crash (genai accepts None), but all sentinel AI features silently fail with cryptic API errors rather than a clear startup warning.

**A4. [MEDIUM] Several try/except blocks swallow errors silently:**
- `casino/casino.py` line 88: `post_to_ledger` catches all exceptions with only `print()` — no structured logging.
- `affinity.py` line 86: `except Exception: return 0.0` — silently returns default on any DB error.
- `codex_cog.py` line 125: conversation DB init failure silently disables conversation history.
- `codex_cog.py` line 439: `run_sql()` returns error as string, never logs it.

---

## Section B: Dead Code & Duplicate Files

**B1. Files NOT loaded by bot.py setup_hook:**

| File | Status |
|------|--------|
| `commish_cog.py` (648 lines) | RETIRED — commented out in setup_hook, replaced by boss_cog. Quarantine candidate. |
| `CUSTOM_ID_CONVENTION.py` (80 lines) | Documentation-only .py file. Should be .md. |
| `HUB_KIT_README.py` (148 lines) | Documentation-only .py file. Should be .md. |
| `echo_voice_extractor.py` (1221 lines) | Standalone CLI tool. Intentionally not loaded. |
| `migrate_to_flow_economy.py` (548 lines) | One-time migration script. Intentionally standalone. |
| `db_migration_snapshots.py` (154 lines) | Migration utility. Intentionally standalone. |
| `google_docs_writer.py` (264 lines) | Imported by cortex_main.py only. Not a cog. |
| `cortex/*.py` (4 files, ~1690 lines) | Standalone CLI pipeline. Intentionally not loaded. |
| `casino/renderer/casino_card_renderer.py` (604 lines) | **DEAD CODE** — near-duplicate of `casino/renderer/card_renderer.py` with larger dimensions. Not imported anywhere. Quarantine candidate. |

**B2. Dead functions/classes:**
- `casino/casino.py`: `_is_admin()` — defined but never called (line ~69). Dead code.
- `oracle_cog.py`: `SupportCog`/`_get_user_tier` references (lines 57-63) — no `SupportCog` exists. Dead code.

**B3. Significant code duplication:**
- `flow_cards.py` and `sportsbook_cards.py` share 8+ identical query functions (`_get_balance`, `_get_season_start_balance`, `_get_weekly_delta`, `_get_sparkline_data`, `_get_lifetime_record`, `_get_total_wagered`, `_get_leaderboard_rank`, `_determine_status`, `card_to_file`). Should be extracted to a shared `flow_queries.py`.
- `casino/renderer/casino_card_renderer.py` is a 604-line near-duplicate of `casino/renderer/card_renderer.py`.

**B4. [OK] oracle_cog.py is NOT carrying duplicate concatenated copies.** It is a single coherent 3763-line file.

---

## Section C: Database Integrity

### C1. Table inventory and row counts

**tsl_history.db (15.9 MB) — 11 tables:**

| Table | Rows |
|-------|------|
| defensive_stats | 45,011 |
| offensive_stats | 28,379 |
| players | 4,629 |
| player_draft_map | 4,629 |
| player_abilities | 2,761 |
| games | 1,989 |
| owner_tenure | 199 |
| trades | 182 |
| tsl_members | 68 |
| standings | 32 |
| teams | 32 |

**flow_economy.db (1.26 MB) — 26 tables** (active economy DB)
**sportsbook.db (2.87 MB) — 22 tables** (legacy, being superseded)

### C2. [HIGH] conversation_history destroyed on DB rebuild

`codex_cog.py` line 109 creates `conversation_history` in `tsl_history.db` via `CREATE TABLE IF NOT EXISTS`. But `build_tsl_db.py:sync_tsl_db()` rebuilds the entire DB into a `.tmp` file and replaces the original via `os.replace()`. This **silently destroys** `conversation_history` on every sync.

### C3. [MEDIUM] Schema divergence between sportsbook.db and flow_economy.db

The migration is incomplete. Key differences:
- `users_table`: sportsbook has 10 columns (includes stats); flow_economy has only 3 (`discord_id`, `balance`, `season_start_balance`). Any code reading old stat columns against flow_economy.db will fail.
- `games_state`: sportsbook has `ou_line REAL`; flow_economy does not.
- `prediction_markets`: sportsbook has `admin_approved`; flow_economy does not.

### C4. SQL Injection Risks

| Location | Risk | Detail |
|----------|------|--------|
| `codex_cog.py:777-794` | **HIGH** | f-string SQL in `_h2h_impl()`: `f"WHERE winner_user = '{u1}'"`. Values come from alias resolution but pattern is dangerous. |
| `codex_cog.py:431-440` | **HIGH** | Gemini-generated SQL executed directly. DB not opened in read-only mode. Regex filter for SELECT only, but no explicit validation. |
| `flow_sportsbook.py:297` | LOW | f-string column names, but validated against allowlist. |
| `build_tsl_db.py:105` | LOW | Dynamic table names from trusted API. |

### C5. Missing Indexes on flow_economy.db

- `bets_table`: no index on `discord_id` or `week`
- `casino_sessions`: no index on `discord_id`
- `crash_bets`: no index on `round_id` or `discord_id`
- `economy_log`: no index on `discord_id`
- `real_bets`: migration script defines `idx_real_bets_user` and `idx_real_bets_event` but they don't exist in the actual DB

---

## Section D: API & External Dependencies

### D1. MaddenStats API

**[OK]** All endpoint URLs verified in `data_manager.py`. The `weekIndex` 0-based vs `CURRENT_WEEK` 1-based gotcha is properly handled (CURRENT_WEEK is derived from API data, not hardcoded).

### D2. Gemini API

| Issue | Severity | Location |
|-------|----------|----------|
| Module-level `genai.Client()` in sentinel_cog.py | HIGH | Line 622 — fails silently if key missing |
| New `genai.Client()` per call in `_analyze_screenshot_sync()` | MEDIUM | sentinel_cog.py:2284 — wasteful, should reuse module client |
| No rate limiting on Gemini calls in codex_cog | MEDIUM | Multiple rapid `/ask` queries could hit quota |
| `reasoning.py` `exec()` sandbox allows `pd.read_csv()` | MEDIUM | LLM-generated code could read arbitrary local files |

### D3. Discord.py

- **[OK]** No deprecated methods found.
- **[OK]** All Gemini calls wrapped in `run_in_executor()` in the Discord code paths.
- Views: Most use appropriate timeouts. See Section F for exceptions.

### D4. Hardcoded URLs/Secrets

- **[MEDIUM]** `constants.py` line 5: `ATLAS_ICON_URL` uses Discord CDN URL with expiring query parameters (`ex=`, `is=`, `hm=`). This URL will eventually return 404.
- **[OK]** No hardcoded API keys or tokens in source. All loaded from `.env`.
- **[LOW]** No `.env.example` file exists. New developers have no template.

---

## Section E: Branding Consistency

### E1. Remaining "WittGPT" / "wittsync" References

**User-facing (should be fixed):**

| File | Line | String |
|------|------|--------|
| boss_cog.py | 1345, 1460, 1502, 1563 | `Run \`/wittsync\` first` |
| genesis_cog.py | 1665, 1924, 1991 | `Run \`/wittsync\` first` |
| oracle_cog.py | 1597, 1615 | `Run \`/wittsync\` first` |
| sentinel_cog.py | 1844 | `Try \`/wittsync\` first` |
| ability_engine.py | 817 | `Use /wittsync to reload` |

**Code comments only (cosmetic):**

| File | Line | Context |
|------|------|---------|
| bot.py | 13, 15, 20, 25, 40, 53, 609 | Changelog comments |
| data_manager.py | 46, 65, 69, 646 | Changelog comments |
| reasoning.py | 31 | Changelog comment |
| trade_engine.py | 5 | Changelog comment |
| build_tsl_db.py | 10, 255 | Docstring comments |

**Intentional legacy compat:**

| File | Line | Purpose |
|------|------|---------|
| setup_cog.py | 63 | `"askwittgpt"` channel alias for backward compat |
| setup_cog.py | 472 | `"WITTGPT"` in nuke_channels() — cleans old channels |

### E2. Embed Color Inconsistency

**Four different "gold" values in use:**

| Hex | Where Used | Files |
|-----|-----------|-------|
| `0xC9962A` | `constants.py`, `atlas_colors.py` (canonical) | codex_cog, oracle_cog, boss_cog, setup_cog, echo_cog |
| `0xD4AF37` | Local `TSL_GOLD` constants | flow_sportsbook, economy_cog, casino, polymarket_cog, player_picker |
| `0xC8A951` | Local `TSL_GOLD` | real_sportsbook_cog |
| `0xDCB93C` | CLAUDE.md says this is TSL_GOLD | **No file uses this value** |

**Recommendation:** Pick `0xC9962A` (already in `atlas_colors.py` as canonical), import everywhere, and update CLAUDE.md to match.

### E3. [OK] No `print()` statements say "WittGPT:" at runtime. All runtime prints say "ATLAS".

---

## Section F: Error Handling & Edge Cases

**F1. [MEDIUM] Missing defer() calls:**
- Most hub/button callbacks use the `@auto_defer` decorator from `hub_view.py`. No obvious missing defers in standard flows. However, Gemini-calling paths in `codex_cog.py` and `sentinel_cog.py` properly defer.

**F2. [MEDIUM] DataFrame access without .empty checks:**
- `analysis.py`: Functions assume `dm.df_*` DataFrames are populated. If called before `load_all()`, results are silently empty.
- `oracle_cog.py`: Hardcoded "All 6 Seasons" in footers (lines ~2081, 2646) instead of using `dm.CURRENT_SEASON`.

**F3. Views without timeout / persistence issues:**

| View | File | Timeout | Issue |
|------|------|---------|-------|
| `CrashView` | crash.py:235 | `None` | **HIGH** — if round cleanup fails, View leaks forever |
| `CasinoHubView` | casino.py | 120s | Short for a lobby — buttons die after 2 min |
| `OracleHubView` | oracle_cog.py | 120s | Short for a hub — should be `None` with custom_id |

**F4. Casino transaction safety issues:**

| Issue | Severity | Location |
|-------|----------|----------|
| Crash cashout double-payout: read-then-write without transaction | **HIGH** | casino_db.py:490-509 |
| Crash round cleanup: no try/finally — round permanently blocks channel on error | **HIGH** | crash.py `_lobby_then_run` |
| Crash double-join: two rapid joins both deduct wager | **MEDIUM** | crash.py `join_crash` |
| Blackjack double-down: non-atomic wager mutation + deduction | **MEDIUM** | blackjack.py:303-311 |
| Blackjack split: same non-atomic pattern | **MEDIUM** | blackjack.py:327-332 |
| PvP coinflip: `resolved` flag race condition | **MEDIUM** | coinflip.py:136-157 |
| PvP challenge creation: wager deducted before row created | **LOW** | coinflip.py via casino_db |

---

## Section G: Echo Integration Status

**G1. [OK]** `echo_cog` loads first in `setup_hook()` (position #1 in `_EXTENSIONS` list).

**G2. [OK]** `get_persona()` is wired into `call_atlas()` in `bot.py` (line 283). The `on_message` handler calls `infer_context()` to determine persona type, then passes it to `call_atlas()`.

**G3. Hardcoded ATLAS_PERSONA:**

| File | Line | Status |
|------|------|--------|
| codex_cog.py | 72 | `ATLAS_PERSONA = """..."""` — Has fallback to `get_persona("analytical")` at line 82. Dead code if echo_loader works. |
| sentinel_cog.py | 646-692, 2076-2265 | Uses inline system prompts for specialized AI tasks (complaint analysis, screenshot analysis). **Intentional** — these are task-specific prompts, not the general ATLAS persona. |

**G4. [OK]** `echo/*.txt` persona files are present and loaded at startup. `get_persona_status()` confirms all three voices (casual, official, analytical) load successfully.

---

## Section H: Architecture & Performance

**H1. Synchronous blocking calls in async-adjacent code:**

| Call | File | Risk |
|------|------|------|
| `requests.get()` | data_manager.py:179,200 | Mitigated — called from `_startup_load` via `run_in_executor` |
| `requests.get()` | build_tsl_db.py:67 | Mitigated — called from executor |
| `requests.get()` | sentinel_cog.py:2272 | Mitigated — called via `run_in_executor` at line 2357 |
| `time.sleep()` | data_manager.py:239 | 30ms sleep in sync load function, inside executor. Acceptable. |
| `time.sleep()` | echo_voice_extractor.py (4 places) | Standalone CLI tool. N/A for bot. |
| `time.sleep()` | cortex/*.py (5 places) | Standalone CLI tool. N/A for bot. |
| Sync `sqlite3` | setup_cog.py, flow_sportsbook.py | **Not in executor** — blocks event loop on every DB call |
| Sync `open()/json.load()` | genesis_cog.py, sentinel_cog.py, awards_cog.py | **Not in executor** — blocks event loop on state load/save |
| Sync Google API | google_docs_writer.py | Only called from cortex CLI. N/A for bot. |

**H2. [MEDIUM] 11 modules write to flow_economy.db simultaneously.** WAL mode + 10s timeout provides basic resilience, but no retry logic on `SQLITE_BUSY`. Under concurrent casino + sportsbook + economy load, lock contention is possible.

**H3. Cog interdependencies (direct imports):**

| Importing Cog | Imports From | Pattern |
|--------------|-------------|---------|
| flow_sportsbook.py | real_sportsbook_cog | `from real_sportsbook_cog import EventListView, SPORT_EMOJI` |
| oracle_cog.py | codex_cog, build_member_db, roster, intelligence | Multiple direct imports |
| genesis_cog.py | player_picker, trade_engine, card_renderer, intelligence | Multiple direct imports |
| sentinel_cog.py | genesis_cog | `from genesis_cog import _state, _save_state, _STATE_PATH` — imports internal state! |
| commish_cog.py | roster | `import roster` inside methods |
| echo_cog.py | bot | `from bot import atlas_group` — circular import risk |

**Most concerning:** `sentinel_cog.py` importing `_state` (a private global dict) from `genesis_cog.py`. This tightly couples enforcement to trade state internals.

**H4. Memory concerns:**
- `affinity.py`: `_affinity_cache` dict grows unbounded. With ~31 users, negligible.
- Casino renderers: 10-30+ unclosed Pillow `Image` objects per render. Under concurrent load, memory pressure possible.
- `lore_rag.py`: FAISS index + SentenceTransformer model loaded once, stays in memory. Expected.

**H5. Pillow image lifecycle:**
- `atlas_card_renderer.py`: Lines 222 and 511 — `Image.open()` without close.
- `casino/renderer/card_renderer.py`: `warm_cache()` opens 52 card images without closing originals. `_draw_felt_base`, `_paste_with_shadow` create ~30+ intermediate images per render without close.
- `casino/renderer/prediction_card_renderer.py`: `render_market_page` opens images in loop without close.
- `casino/renderer/ledger_renderer.py`: Intermediate RGBA image not closed.

---

## Section I: Security

**I1. [OK]** Admin commands properly gated:
- `permissions.py` uses `ADMIN_USER_IDS` from env + "Commissioner" role + guild admin check.
- `commissioner_only()` decorator used on admin commands.
- `boss_cog.py` uses `get_cog().method_impl()` delegation — permission checked at hub level.

**I2. Commands that should be ephemeral:**
- **[OK]** Drill-down responses are generally ephemeral. Hub landing embeds are public. Pattern is consistent.

**I3. [MEDIUM] Modal input sanitization:**
- Trade evaluation and complaint submission modals pass user text to Gemini prompts and embed descriptions. No HTML/markdown injection risk in Discord embeds (Discord sanitizes), but Gemini prompt injection is possible. User-supplied text in trade notes or complaint descriptions could influence AI analysis.

**I4. [OK]** Bot token and API keys loaded exclusively from `.env`. No hardcoded secrets found in any source file.

**I5. [MEDIUM]** `reasoning.py` exec() sandbox: `pd` (Pandas) injected into sandbox namespace allows `pd.read_csv()` on arbitrary local paths. LLM-generated code could read sensitive files.

**I6. [HIGH]** `lore_rag.py` uses `pickle.load()` without verification (lines 37, 165, 190). Pickle deserialization can execute arbitrary code. File is locally generated, but if tampered with, this is RCE.

---

## Section J: File-by-File Health Summary

### Root Files (45)

| File | Lines | Status | Note |
|------|-------|--------|------|
| bot.py | 730 | OK | Clean orchestrator, proper guards |
| echo_cog.py | 141 | OK | Minor: local color def instead of import |
| setup_cog.py | 544 | ISSUES | Sync sqlite3 blocks event loop; WittGPT legacy strings |
| flow_sportsbook.py | 2500 | ISSUES | Sync sqlite3 throughout; cross-cog import from real_sportsbook_cog |
| oracle_cog.py | 3763 | ISSUES | Cross-module imports; /wittsync refs; dead SupportCog ref |
| genesis_cog.py | 2087 | ISSUES | Sync file I/O; /wittsync refs; cross-module imports |
| sentinel_cog.py | 2865 | ISSUES | Module-level Gemini init; cross-cog genesis import; sync requests |
| awards_cog.py | 98 | ISSUES | Sync file I/O |
| codex_cog.py | 919 | ISSUES | SQL injection in h2h; hardcoded ATLAS_PERSONA; DB rebuild destroys conversation_history |
| polymarket_cog.py | 1856 | OK | Clean async patterns |
| economy_cog.py | 813 | OK | Minor: inconsistent gold color; bot.guilds[0] assumption |
| real_sportsbook_cog.py | 929 | OK | Minor: inconsistent gold color |
| boss_cog.py | 2146 | ISSUES | /wittsync refs in error messages |
| commish_cog.py | 648 | ISSUES | Retired but not quarantined; get_cog name mismatches |
| data_manager.py | 1103 | OK | Sync HTTP (mitigated by executor) |
| reasoning.py | 996 | ISSUES | exec() sandbox allows pd.read_csv(); WittGPT comment |
| intelligence.py | 852 | OK | Clean |
| analysis.py | 596 | OK | Clean |
| ability_engine.py | 1269 | OK | /wittsync comment |
| trade_engine.py | 354 | OK | WittGPT comment |
| build_tsl_db.py | 417 | ISSUES | Sync requests.get; destroys conversation_history on rebuild |
| build_member_db.py | 1426 | OK | Hardcoded member data (by design) |
| player_picker.py | 401 | OK | Clean |
| card_renderer.py | 829 | ISSUES | Playwright browser orphan risk |
| atlas_card_renderer.py | 985 | ISSUES | Unclosed Pillow images |
| atlas_colors.py | 79 | OK | Canonical color palette |
| constants.py | 12 | ISSUES | Expiring Discord CDN URL |
| lore_rag.py | 264 | ISSUES | Unsafe pickle.load; blocking model load |
| affinity.py | 220 | OK | Clean async |
| permissions.py | 165 | OK | Clean |
| echo_loader.py | 215 | OK | Clean |
| hub_view.py | 244 | OK | Clean base class |
| ui_state.py | 269 | OK | Clean async |
| pagination_view.py | 226 | OK | Clean |
| roster.py | 468 | OK | Minor sync sqlite |
| flow_cards.py | 267 | ISSUES | Duplicates sportsbook_cards.py |
| flow_wallet.py | 443 | OK | Clean async + sync APIs |
| sportsbook_cards.py | 394 | ISSUES | Duplicates flow_cards.py |
| odds_api_client.py | 178 | OK | Clean async |
| google_docs_writer.py | 264 | ISSUES | Sync Google API (CLI only) |
| db_migration_snapshots.py | 154 | OK | Migration utility |
| migrate_to_flow_economy.py | 548 | OK | One-time migration |
| echo_voice_extractor.py | 1221 | OK | Standalone CLI tool |
| CUSTOM_ID_CONVENTION.py | 80 | ISSUES | Doc stored as .py |
| HUB_KIT_README.py | 148 | ISSUES | Doc stored as .py |

### Casino Files (13)

| File | Lines | Status | Note |
|------|-------|--------|------|
| casino/__init__.py | 1 | OK | |
| casino/casino.py | 424 | ISSUES | Dead `_is_admin`; short hub timeout |
| casino/casino_db.py | 648 | ISSUES | Crash cashout double-payout risk; missing transactions |
| casino/games/__init__.py | 1 | OK | |
| casino/games/blackjack.py | 657 | ISSUES | Non-atomic double-down/split wager |
| casino/games/coinflip.py | 342 | ISSUES | PvP resolved flag race condition |
| casino/games/crash.py | 473 | ISSUES | No cleanup on error; CrashView timeout=None; double-join |
| casino/games/slots.py | 308 | OK | Minor cosmetic issues |
| casino/renderer/__init__.py | 1 | OK | |
| casino/renderer/card_renderer.py | 599 | ISSUES | Unclosed Pillow images throughout |
| casino/renderer/casino_card_renderer.py | 604 | ISSUES | Dead code — not imported anywhere |
| casino/renderer/ledger_renderer.py | 248 | OK | Minor unclosed image |
| casino/renderer/prediction_card_renderer.py | 358 | OK | Minor unclosed images |

### Cortex Files (4)

| File | Lines | Status | Note |
|------|-------|--------|------|
| cortex/cortex_analyst.py | 591 | ISSUES | Blocking sync Gemini; fragile JSON repair |
| cortex/cortex_engine.py | 362 | ISSUES | LIKE wildcard bug; sync sqlite3 |
| cortex/cortex_main.py | 402 | ISSUES | sys.path manipulation |
| cortex/cortex_writer.py | 335 | ISSUES | Blocking sync Gemini (30-60s) |

---

## PHASE 3: Prioritized Action Plan

### 1. CRITICAL Fixes (will crash or corrupt data)

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 1.1 | conversation_history destroyed on DB rebuild | codex_cog.py + build_tsl_db.py | Move conversation_history to a separate DB file (e.g., `atlas_state.db`) OR exclude it from the rebuild-and-replace logic. ~20 lines. |
| 1.2 | SQL injection in `_h2h_impl()` — f-string SQL | codex_cog.py:777-794 | Switch to parameterized queries: `WHERE winner_user = ?`. ~5 lines. |
| 1.3 | Crash cashout double-payout — no transaction | casino_db.py:490-509 | Wrap read-check-write in `BEGIN IMMEDIATE` transaction. ~10 lines. |
| 1.4 | Crash round cleanup — no try/finally | crash.py `_lobby_then_run` | Add `try/finally` block that always calls `active_rounds.pop()`. ~8 lines. |

### 2. HIGH Priority (broken features, security, money loss)

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 2.1 | Gemini-generated SQL not in read-only mode | codex_cog.py:431 | Open DB with `?mode=ro` URI parameter. ~3 lines. |
| 2.2 | Crash double-join race condition | crash.py `join_crash` | Add UNIQUE constraint on (round_id, discord_id) in crash_bets table + handle IntegrityError. ~10 lines. |
| 2.3 | CrashView timeout=None with no safety net | crash.py:235 | Set `timeout=600` (max round duration). Add `on_timeout` handler. ~15 lines. |
| 2.4 | Unsafe pickle.load in lore_rag.py | lore_rag.py:37,165,190 | Switch to JSON metadata storage or use `pickle.loads()` with hash verification. ~20 lines. |
| 2.5 | Blackjack non-atomic double-down/split | blackjack.py:303-332 | Wrap deduction + resolution in single DB transaction via casino_db helper. ~25 lines. |
| 2.6 | PvP coinflip resolved flag race | coinflip.py:136-157 | Use `asyncio.Lock` per challenge or atomic DB flag check. ~10 lines. |
| 2.7 | `ATLAS_ICON_URL` uses expiring CDN URL | constants.py:5 | Upload icon to a permanent host or use Discord attachment URL. ~2 lines. |

### 3. MEDIUM Priority (tech debt, inconsistency, performance)

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 3.1 | `/wittsync` in 12+ user-facing error messages | boss_cog, genesis_cog, oracle_cog, sentinel_cog, ability_engine | Replace with `/atlas sync`. ~12 lines across 5 files. |
| 3.2 | 4 different gold hex values across codebase | 10+ files | Import `ATLAS_GOLD` from `atlas_colors.py` everywhere. Update CLAUDE.md. ~30 lines. |
| 3.3 | Sync sqlite3 in flow_sportsbook.py | flow_sportsbook.py | Convert to aiosqlite. Large change — ~200 lines across all DB helpers. |
| 3.4 | Sync sqlite3 in setup_cog.py | setup_cog.py | Convert `_get_config`/`_set_config` to aiosqlite. ~30 lines. |
| 3.5 | Sync file I/O in genesis_cog, sentinel_cog, awards_cog | 3 files | Wrap in `run_in_executor` or use `aiofiles`. ~20 lines per file. |
| 3.6 | Code duplication: flow_cards.py + sportsbook_cards.py | 2 files | Extract shared queries to `flow_queries.py`. ~100 lines net reduction. |
| 3.7 | sentinel_cog imports genesis_cog internal state | sentinel_cog.py:1300 | Create a public accessor method in genesis_cog, import that instead. ~15 lines. |
| 3.8 | flow_sportsbook imports from real_sportsbook_cog | flow_sportsbook.py:58 | Move shared components to a shared module. ~20 lines. |
| 3.9 | Missing DB indexes on flow_economy.db | flow_economy.db | Add indexes on bets_table(discord_id), casino_sessions(discord_id), etc. ~10 lines SQL. |
| 3.10 | sentinel_cog creates new genai.Client per screenshot | sentinel_cog.py:2284 | Reuse module-level `_gemini` client. ~3 lines. |
| 3.11 | reasoning.py sandbox allows pd.read_csv on arbitrary paths | reasoning.py | Restrict Pandas methods in sandbox namespace or use a custom wrapper. ~15 lines. |
| 3.12 | 11 modules write to flow_economy.db with no retry | Multiple | Add SQLITE_BUSY retry decorator for DB operations. ~30 lines shared utility. |
| 3.13 | codex_cog DB_SCHEMA built at module load — stale if season changes | codex_cog.py:337 | Rebuild schema string on each query. ~5 lines. |
| 3.14 | No .env.example file | (new file) | Create template with all required/optional vars. ~15 lines. |

### 4. LOW Priority (cleanup, style, optimization)

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 4.1 | WittGPT in code comments (7 files) | Various | Find/replace in comments. ~10 lines. |
| 4.2 | commish_cog.py retired but not quarantined | commish_cog.py | Move to Quarantine_Archive/. |
| 4.3 | casino_card_renderer.py dead code | casino/renderer/ | Move to Quarantine_Archive/. |
| 4.4 | CUSTOM_ID_CONVENTION.py + HUB_KIT_README.py as .py files | 2 files | Rename to .md or move to docs/. |
| 4.5 | Unclosed Pillow images throughout casino renderers | 4 renderer files | Add `with` context managers or explicit `.close()`. ~40 lines. |
| 4.6 | Dead `_is_admin()` in casino.py | casino/casino.py | Delete function. ~5 lines. |
| 4.7 | Dead SupportCog reference in oracle_cog.py | oracle_cog.py:57-63 | Delete block. ~6 lines. |
| 4.8 | Hardcoded "All 6 Seasons" in oracle_cog.py footers | oracle_cog.py | Replace with `dm.CURRENT_SEASON`. ~4 lines. |
| 4.9 | economy_cog `bot.guilds[0]` assumption | economy_cog.py:310 | Use `interaction.guild` instead. ~2 lines. |
| 4.10 | OracleHubView timeout=120 (too short for hub) | oracle_cog.py | Set `timeout=None` with custom_id for persistence. ~3 lines. |
| 4.11 | CasinoHubView timeout=120 (too short for lobby) | casino/casino.py | Set `timeout=None` with custom_id. ~3 lines. |
| 4.12 | codex_cog get_db() connection leak on exception | codex_cog.py:426 | Use `with` context manager. ~5 lines. |
| 4.13 | cortex_engine LIKE wildcard: underscore not escaped | cortex/cortex_engine.py:171 | Add `.replace('_', '')` alongside `%` replacement. ~1 line. |

---

## Appendix: Quarantine Archive Status

18 files in `Quarantine_Archive/` — all are pre-v2.0 modules replaced by consolidated cogs. No active code imports from this directory. **Safe to permanently delete** when ready.

---

*End of audit report.*
