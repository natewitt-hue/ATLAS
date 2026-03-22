# ATLAS 4.0 Preflight Audit — Consolidated Findings

**Audit Date:** 2026-03-19
**Codebase:** ~48K lines, 67 Python files, Python 3.14, discord.py 2.3+
**Methodology:** 5 parallel read-only audit agents covering 6 passes

---

## Executive Summary

| Category | CRITICAL | HIGH | MEDIUM | LOW | Total |
|----------|----------|------|--------|-----|-------|
| Pass 1: God Files & Structural Bloat | 2 | 5 | 7 | 4 | 20 |
| Pass 2: Silent Failure Audit | 12 | 8 | 16 | — | 36 (of 163 classified) |
| Pass 3: Database Access Patterns | 11 | 5 | 10 | 5 | 31 |
| Pass 4+5: Dead Code & Consistency | — | 5 | 10 | 7 | 22 |
| Pass 6: Rendering Pipeline | — | 6 | 15 | 7 | 28 |
| **TOTALS** | **25** | **29** | **58** | **23** | **137** |

**Top 3 systemic risks:**
1. **Event-loop blocking** — 11 CRITICAL sync sqlite3 calls in async handlers, worst on the 1.3GB archive DB
2. **Silent ledger failures** — 12 CRITICAL `except: pass` blocks on money-movement audit trails in sportsbook/polymarket
3. **God file sprawl** — 5 files over 2,500 lines with 6-12 responsibilities each

---

## 1. Severity-Rated Issue List (CRITICAL + HIGH only)

### CRITICAL (25 issues)

#### Event-Loop Blocking (Pass 3)
| # | File:Line | Description | Fix |
|---|-----------|-------------|-----|
| 1 | `reasoning.py:921` | Sync sqlite3 on 1.3GB TSL_Archive.db in async `query_discord_history()` | Wrap in `run_in_executor` |
| 2 | `reasoning.py:944` | Sync `get_discord_db_schema()` on 1.3GB DB in async retry loop | Wrap in `run_in_executor` or cache |
| 3 | `reasoning.py:766` | Sync `get_discord_db_schema()` in async `generate_sql()` | Wrap in `run_in_executor` |
| 4 | `codex_cog.py:591,618,640` | Sync `run_sql()` called 3x per retry cascade in async handler | Wrap in `run_in_executor` |
| 5 | `codex_cog.py:838` | Sync `run_sql()` in async command handler (Tier 1/2 intent) | Wrap in `run_in_executor` |
| 6 | `codex_cog.py:977` | Sync `run_sql()` in async `ask_debug()` handler | Wrap in `run_in_executor` |
| 7 | `codex_cog.py:1047,1111` | Sync `run_sql()` in `_h2h_impl()` and `_season_recap_impl()` | Wrap in `run_in_executor` |
| 8 | `flow_live_cog.py:576` | Sync sqlite3 in 60-second pulse task loop | Use aiosqlite or `run_in_executor` |
| 9 | `flow_cards.py:37-144` | 12 sync DB helpers called from async card builders | Wrap builders in `run_in_executor` |
| 10 | `sportsbook_cards.py:77-246` | 10 sync DB helpers called from async card builders | Wrap builders in `run_in_executor` |
| 11 | `intelligence.py:252` | Sync `get_team_draft_class()` in async oracle callbacks | Wrap in `run_in_executor` |

#### Silent Ledger Failures (Pass 2)
| # | File:Line | Description | Fix |
|---|-----------|-------------|-----|
| 12 | `flow_sportsbook.py:1222` | Ledger post after straight bet — `except: pass` | `log.exception()` |
| 13 | `flow_sportsbook.py:1298` | Ledger post after parlay — `except: pass` | `log.exception()` |
| 14 | `flow_sportsbook.py:1376` | Ledger post after prop bet — `except: pass` | `log.exception()` |
| 15 | `flow_sportsbook.py:2277` | Ledger post for push refund — `except: pass` | `log.exception()` |
| 16 | `flow_sportsbook.py:2633` | Ledger post for cancellation refund — `except: pass` | `log.exception()` |
| 17 | `flow_sportsbook.py:2677` | Ledger post for individual refund — `except: pass` | `log.exception()` |
| 18 | `flow_sportsbook.py:2708` | Ledger post for admin adjustment — `except: pass` | `log.exception()` |
| 19 | `flow_sportsbook.py:2824` | Ledger post for prop resolution — `except: pass` | `log.exception()` |
| 20 | `flow_sportsbook.py:1000` | JSON parse of parlay legs in auto-grade — `continue` skips grading | Log bad JSON + parlay_id |
| 21 | `flow_sportsbook.py:2180` | JSON parse in manual grading — bet never resolves | Log parlay_id |
| 22 | `flow_sportsbook.py:2589` | JSON parse in cancellation — user loses money | Log + attempt refund |
| 23 | `flow_sportsbook.py:2620` | JSON parse in ledger refund loop — same as above | Log error |

#### Other CRITICAL
| # | File:Line | Description | Fix |
|---|-----------|-------------|-----|
| 24 | `oracle_cog.py` (4,305 lines) | God file with 12 responsibilities | Split into 4 modules |
| 25 | `polymarket_cog.py` (3,525 lines) | God file with 8 responsibilities, 3 duplicate browser views | Split into 4 modules |

### HIGH (29 issues)

#### Silent Failures in Money/Data Paths (Pass 2)
| # | File:Line | Description |
|---|-----------|-------------|
| 26 | `polymarket_cog.py:3352` | Ledger post for prediction win payouts — `except: pass` |
| 27 | `polymarket_cog.py:3034` | Price DB update silently swallowed — stale prices |
| 28 | `polymarket_cog.py:3146` | Balance fetch returns 0 silently — wrong balance shown |
| 29 | `polymarket_cog.py:891` | Price refresh failure returns cached price silently |
| 30 | `oracle_cog.py:604,1076` | `_call_ai_brief()` returns empty on failure — blank data |
| 31 | `codex_cog.py:453` | User alias resolution fails — all Codex queries break |
| 32 | `intelligence.py:81` | `build_owner_map()` fails with `return` — owner resolution breaks |
| 33 | `crash.py:601` | Full crash round error — wagers deducted, no refund attempt |
| 34 | `sentinel_cog.py:1186` | Evidence screenshot fetch fails silently |

#### Database Issues (Pass 3)
| # | File:Line | Description |
|---|-----------|-------------|
| 35 | `flow_live_cog.py:165-473` | 7 sync sqlite3 methods called from async event handlers |
| 36 | `flow_sportsbook.py:394` | Sync Elo computation — blocks if triggered on first request |
| 37 | `flow_sportsbook.py:1873` | Sync snapshot check in async `before_loop` |
| 38 | `setup_cog.py:99-810` | Sync `get_channel_id()` used from async contexts (cache mitigates) |

#### God Files (Pass 1)
| # | File:Line | Description |
|---|-----------|-------------|
| 39 | `codex_intents.py:1401-1742` | 340-line if/elif chain duplicating regex-tier SQL |
| 40 | `genesis_cog.py:619-895` | `_evaluate_and_post` — 265-line function |
| 41 | `sentinel_cog.py` (2,952 lines) | 6 cog classes in one file |
| 42 | `flow_sportsbook.py` (2,831 lines) | ~100 lines of duplicated parlay grading logic |
| 43 | `build_member_db.py:37-1075` | 1,038 lines of hardcoded member data |

#### Dead Code (Pass 4+5)
| # | File:Line | Description |
|---|-----------|-------------|
| 44 | `oracle_cog.py:473-665` | 190-line contiguous dead code block (legacy Claude SDK) |

#### Rendering (Pass 6)
| # | File:Line | Description |
|---|-----------|-------------|
| 45 | `card_renderer.py:109-122` | Duplicate browser singleton (dead code, resource leak risk) |
| 46 | `card_renderer.py:289-802` | Bypasses `wrap_card()`, builds own HTML document |
| 47 | `card_renderer.py:294` | Network font dependency (Google Fonts CDN) |
| 48 | `card_renderer.py:296` | External CDN font dependency (Bank Gothic) |
| 49 | `ledger_renderer.py:42-190` | Bypasses `wrap_card()`, builds own full HTML |
| 50 | `ledger_renderer.py:15` | Imports private `_font_face_css` function |

#### Consistency (Pass 4+5)
| # | File:Line | Description |
|---|-----------|-------------|
| 51 | `polymarket_cog.py` (7 locations) | Hardcoded hex colors bypassing `AtlasColors` entirely |
| 52 | `data_manager.py` (26 prints) | Core module uses `print()` with unused logger import |
| 53 | `setup_cog.py` (24 prints) | Provisioning module uses `print()` with unused logger |
| 54 | (90 of 110 calls) | `set_footer()` missing `icon_url=ATLAS_ICON_URL` (82% non-compliance) |

---

## 2. Refactoring Recommendations

### Priority 1: sentinel_cog.py (Low effort, Low risk)
Already has 6 internally-separated cog classes. Splitting is mechanical file separation:
- `sentinel_complaints.py` — ComplaintCog, ComplaintModal
- `sentinel_force.py` — ForceRequestCog
- `sentinel_gameplay.py` — GameplayCog (DC, blowout, stats)
- `sentinel_position.py` — PositionChangeCog, POSITION_RULES, BANNED_MOVES
- `sentinel_4th_down.py` — FourthDownCog, SYSTEM_PROMPT
- `sentinel_hub.py` — SentinelHubCog (thin hub wiring)

### Priority 2: build_member_db.py (Low effort, Low risk)
- Move `MEMBERS` list (1,038 lines) to `member_seed.json`
- Extract `resolve_db_username`, `get_alias_map`, `get_known_users` to `member_resolver.py`

### Priority 3: flow_sportsbook.py (Medium effort, Medium risk)
- `sportsbook_elo.py` — `_compute_elo_ratings`, Elo constants
- `sportsbook_db.py` — `setup_db`, migrations
- `sportsbook_grading.py` — `_grade_bets_impl`, `_run_autograde`, shared `_grade_parlay()`
- `sportsbook_views.py` — SportsbookHubView, all Modals

### Priority 4: oracle_cog.py (High effort, Medium risk)
- `oracle_views.py` — 15 View/Modal classes
- `oracle_builders.py` — `_build_team_card_snapshot`, `_build_player_leaders_embed`, etc.
- `oracle_tools.py` — `_ORACLE_TOOLS` schema, `_run_oracle` pipeline
- `nfl_identity.py` — `_NFL_IDENTITY` dict (consolidate with `codex_intents._TEAM_ALIASES`)

### Priority 5: polymarket_cog.py (High effort, Medium risk)
- `polymarket_api.py` — API client, pricing helpers
- `polymarket_data.py` — `CATEGORY_MAP`, DB setup
- `polymarket_views.py` — Browser views (deduplicate 3 near-identical views)
- `polymarket_sync.py` — Background sync, resolution, daily drop

---

## 3. Quick Wins (< 10 lines each)

### CRITICAL fixes (~30 lines total)
1. **Replace 8 `pass` with `log.exception()` on sportsbook ledger posts** — flow_sportsbook.py:1222,1298,1376,2277,2633,2677,2708,2824 (16 lines)
2. **Wrap `execute_sql_safe()` in `run_in_executor`** — reasoning.py:921 (3 lines)
3. **Wrap `run_sql()` calls in `run_in_executor`** — codex_cog.py:591,618,640 (5 lines)
4. **Add `log.exception()` to polymarket ledger post** — polymarket_cog.py:3352 (1 line)

### HIGH fixes (~25 lines total)
5. **Remove 190-line dead code block** — oracle_cog.py:473-665 (delete)
6. **Remove duplicate browser singleton** — card_renderer.py:109-137 (delete 30 lines)
7. **Wrap flow_cards.py builders in `run_in_executor`** — 2 lines per builder
8. **Wrap sportsbook_cards.py builders in `run_in_executor`** — 2 lines per builder
9. **Add `log.warning()` to `build_owner_map()` failure** — intelligence.py:81 (2 lines)

### MEDIUM fixes (~20 lines total)
10. **Replace hardcoded colors in polymarket_cog.py** — 6 hex values → `AtlasColors.*` (6 lines)
11. **Extract `_sparkline_svg()` to atlas_html_engine.py** — shared by flow_cards + sportsbook_cards (save 15 dup lines)
12. **Remove unused `_NOISE_SVG` from pulse_renderer.py** — 6 lines deleted
13. **Fix `run_sql()` to use context manager** — codex_cog.py:498 (3 lines)
14. **Rename `FourthDown` → `FourthDownCog`** — sentinel_cog.py (1 line)
15. **Remove unused `ATLAS_DARK`, `ATLAS_BLUE` imports** — bot.py:167 (1 line)

---

## 4. Technical Debt Summary

| Category | Count | Priority | Est. Fix |
|----------|-------|----------|----------|
| Sync DB blocking event loop | 18 access points, 11 CRITICAL | **P0** | 2-3 hours |
| Silent failures in money paths | 19 Dangerous (12 CRITICAL) | **P0** | 1 hour |
| Silent failures needing logging | 29 Needs Logging | **P1** | 2 hours |
| God files (>2,500 lines) | 5 files, 16,108 lines | **P2** | 2-3 days |
| Dead code | 6 files + 190 lines in oracle_cog | **P2** | 30 min |
| Print→logging migration | 524 prints across 40 files | **P3** | 1-2 days |
| Rendering pipeline non-compliance | 2 files bypass wrap_card, ~250 lines CSS duplication | **P3** | 4-6 hours |
| Embed footer icon missing | 90 of 110 calls (82%) | **P3** | 45 min |
| Hardcoded colors (rendering) | ~95 instances across 5 renderer files | **P3** | 2-3 hours |

---

## 5. Production Readiness Checklist

### P0 — Must fix before v4.0
- [ ] All sync DB calls in async handlers wrapped in `run_in_executor` (18 access points)
- [ ] All CRITICAL silent failures on money paths have `log.exception()` (12 blocks in sportsbook + 3 in polymarket)
- [ ] Corrupt parlay JSON logging added (4 `continue` blocks in sportsbook)
- [ ] `crash.py:601` has player notification + refund on round failure

### P1 — Should fix before v4.0
- [ ] 29 "Needs Logging" silent failures have at minimum `log.debug/warning()`
- [ ] `reasoning.py` — cached schema to avoid repeated 1.3GB DB reads
- [ ] WAL mode enabled on TSL_Archive.db
- [ ] Connection timeouts added to flow_live_cog.py DB calls

### P2 — Fix soon after v4.0
- [ ] 190 lines of dead code removed from oracle_cog.py
- [ ] 6 dead files moved to QUARANTINE/
- [ ] `commish_cog.py` moved to QUARANTINE (replaced by boss_cog)
- [ ] sentinel_cog.py split into 6 files (mechanical separation)
- [ ] build_member_db.py MEMBERS list moved to JSON

### P3 — Ongoing improvement
- [ ] No god files over 3,000 lines (oracle_cog, polymarket_cog, sentinel_cog)
- [ ] Consistent error handling across all cogs (centralized `send_error()`)
- [ ] card_renderer.py and ledger_renderer.py migrated to `wrap_card()` pipeline
- [ ] All renderers using `atlas_style_tokens.py` (highlight_renderer.py has ~35 hardcoded)
- [ ] ~250 lines of duplicated CSS extracted to shared modules
- [ ] Print→logging migration (524 calls, start with data_manager + setup_cog)
- [ ] `embed_helpers.build_embed()` adopted or removed
- [ ] Footer icon compliance (90 calls missing `ATLAS_ICON_URL`)
- [ ] Version bumped to 4.0.0

---

## 6. Pass Details

### Pass 1: God Files — Summary Stats
- 8 files audited, 21,513 total lines
- 14 functions >100 lines (largest: `_build_from_classification` at 340 lines)
- 6 near-duplicate code blocks across files
- 5 hardcoded data tables that should be config/JSON

### Pass 2: Silent Failures — Summary Stats
- 163 except blocks classified across 30+ files
- 19 Dangerous (11.7%), 29 Needs Logging (17.8%), 115 Acceptable (70.6%)
- Danger concentrated in consumer layer (sportsbook, polymarket), not financial core
- Financial core modules (`flow_wallet.py`, `casino_db.py`, `economy_cog.py`) use correct `rollback() + raise`

### Pass 3: Database — Summary Stats
- 26 files with DB operations, 3 SQLite databases
- 11 CRITICAL event-loop blocking access points
- Gold standard exists (casino_db, economy_cog, polymarket_cog use aiosqlite)
- Worst offender: reasoning.py (sync on 1.3GB DB without executor)
- F-string SQL found in 5 files but all use whitelists/hardcoded values (safe but fragile)

### Pass 4: Dead Code — Summary Stats
- 6 dead files (~1,020 lines removable)
- 190-line dead code block in oracle_cog.py (legacy Claude SDK functions)
- 1 dead class (`SnapshotTask` in db_migration_snapshots.py)
- `build_embed()` in embed_helpers.py defined but never called
- Total removable: ~1,260 lines

### Pass 5: Consistency — Summary Stats
- 524 `print()` vs 28 `logger` calls (18:1 ratio)
- 25 files import logging but most still use print
- 82% of footer calls missing brand icon
- polymarket_cog.py bypasses AtlasColors entirely (7 hardcoded hex values)
- 2 cog classes missing "Cog" suffix

### Pass 6: Rendering — Summary Stats
- 7 of 9 renderers use `wrap_card()` + `render_card()` correctly
- 2 non-compliant: card_renderer.py, ledger_renderer.py (build own HTML)
- Zero Pillow imports — PIL migration 100% complete
- ~250 lines of duplicated CSS across pipeline
- highlight_renderer.py has ~35 hardcoded colors despite calling `wrap_card()`
- No renderer has error handling except card_renderer.py (returns None on failure)

---

*Generated by 5 parallel audit agents on 2026-03-19. Cross-referenced against `docs/ATLAS_4.0_PREFLIGHT_REVIEW.md`.*
