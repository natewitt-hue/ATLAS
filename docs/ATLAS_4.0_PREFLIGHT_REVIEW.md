# ATLAS 4.0 Preflight Review — Full Codebase Audit

## What This Is
A senior-level code quality audit of the entire ATLAS codebase (~48K lines, 67 Python files) before declaring v4.0 production-ready. This is NOT a feature review — it's a **craftsmanship review**: hunting bloat, spaghetti, Frankenstein patterns, silent failures, inconsistencies, dead code, and god objects.

## How to Run This Review

This is a multi-pass review. Use `plan` mode. Run each pass as a parallel agent batch, aggregate findings, then produce a prioritized fix plan.

**Do NOT fix anything during the review.** Read-only. The output is a findings document with severity ratings and fix estimates.

---

## Pass 1: God Files & Structural Bloat

Read these files end-to-end and assess whether they should be split. For each, identify distinct responsibilities that could be separate modules:

| File | Lines | Why It's Suspect |
|------|-------|-----------------|
| `oracle_cog.py` | 4,305 | 24 classes, 78 methods — stats hub, AI modals, player profiles, matchups, power rankings, team intel all in one file |
| `polymarket_cog.py` | 3,525 | 9 classes, 81 methods — prediction markets, settlement, portfolio, betting UI |
| `sentinel_cog.py` | 2,952 | 18 classes — complaints, force requests, 4th down monitor, parity, blowout detection |
| `flow_sportsbook.py` | 2,831 | 2 classes but 39 methods — odds engine, betting, autograde, line management |
| `boss_cog.py` | 2,425 | 8 classes, 70 methods — commissioner control room |
| `genesis_cog.py` | 2,092 | 13 classes, 76 function defs — trades, roster, dev traits, draft, ability audit |
| `codex_intents.py` | 1,773 | 16 functions — NL→SQL intent classification (no classes) |
| `build_member_db.py` | 1,610 | Identity resolution — 88+ alias entries, fuzzy matching, DB sync |

For each file, answer:
1. How many distinct responsibilities does it have?
2. What's the natural split boundary?
3. Are there functions >100 lines that should be decomposed?
4. Are there classes that are doing too much?

---

## Pass 2: Silent Failure Audit

Search the entire codebase for these patterns:

```
except Exception: pass
except Exception: return
except: pass
```

For each instance, classify as:
- **Acceptable** — truly best-effort (e.g., optional cosmetic feature)
- **Needs Logging** — should at minimum print/log the error
- **Dangerous** — silently hides bugs that affect correctness

Known hot spots:
- `oracle_cog.py` — ~20 bare except blocks in builder functions
- `sentinel_cog.py` — ~15 bare except blocks
- `flow_sportsbook.py` — ~5 bare except blocks
- Casino renderers — exception handling in render paths

**Overall stat from prior audit: 432 bare except blocks out of 1,413 try blocks (30.5%)**

---

## Pass 3: Database Access Patterns

The codebase mixes sync `sqlite3` and async `aiosqlite` in ways that could block the event loop or cause race conditions.

Audit every database access point:

| DB File | Expected Access Pattern |
|---------|----------------------|
| `tsl_history.db` | Should be sync (read-only DataFrames via data_manager) |
| `sportsbook.db` | Mixed sync/async — needs unification |
| `flow_economy.db` | Check casino_db.py patterns |
| `TSL_Archive.db` | Codex queries — check if blocking event loop |
| `conversation_history` (in tsl_history.db) | Should be async (conversation_memory.py) |

For each access point, answer:
1. Is it sync or async?
2. Is it called from an async context (command handler, event listener)?
3. Could it block the Discord event loop?
4. Does it use parameterized queries (SQL injection safety)?

Key files to audit: `codex_cog.py` (run_sql), `flow_sportsbook.py`, `casino/casino_db.py`, `build_tsl_db.py`, `build_member_db.py`, `conversation_memory.py`, `affinity.py`

---

## Pass 4: Dead Code & Bloat

### Files suspected dead (never imported, not a cog):
- `embed_helpers.py` — no imports found in active code
- `google_docs_writer.py` — no imports found
- `CUSTOM_ID_CONVENTION.py` — documentation masquerading as code
- `HUB_KIT_README.py` — documentation masquerading as code
- `crop_icons.py`, `process_icons.py` — one-off utility scripts
- `migrate_to_flow_economy.py` — one-off migration

### Verify each by:
1. `grep -rn "import <filename>" *.py casino/*.py` — is it imported anywhere?
2. `grep -rn "<filename>" bot.py` — is it loaded as a cog?
3. If truly dead → recommend QUARANTINE

### Also check:
- Functions defined but never called (especially in large files)
- Classes that exist but are never instantiated
- Imports at the top of files that are unused
- Variables assigned but never read

---

## Pass 5: Consistency & Standards

### Naming Conventions
- Are all cog classes named consistently? (e.g., `StatsHubCog` vs `SentinelCog` vs `CasinoCog`)
- Are private helpers consistently prefixed with `_`?
- Are constants consistently `UPPER_SNAKE_CASE`?

### Error Response Patterns
- Do all commands respond to errors consistently?
- Are ephemeral flags used correctly (drill-downs = ephemeral, hubs = public)?
- Is the "Something broke" error message consistent across modals?

### Embed Construction
- 480 embed instances found — is there a base helper or is it ad-hoc each time?
- Are colors consistently using `AtlasColors` / `atlas_colors.py`?
- Are footers consistently using `ATLAS_ICON_URL`?

### Print vs Logging
- **1,353 print statements** vs **156 logging calls** — the codebase relies heavily on stdout
- Should we migrate to structured logging?
- Which prints are debug noise vs important operational logs?

---

## Pass 6: Rendering Pipeline Review

The HTML→PNG rendering pipeline is a significant subsystem (~9K lines in casino/ alone).

Review:
- `atlas_html_engine.py` (624 lines) — page pool, render_card, wrap_card
- `atlas_style_tokens.py` (107 lines) — design system tokens
- `casino/renderer/casino_html_renderer.py` (1,250 lines) — game cards
- `casino/renderer/prediction_html_renderer.py` (1,312 lines) — market cards
- `casino/renderer/highlight_renderer.py` (649 lines) — highlight cards
- `casino/renderer/session_recap_renderer.py` (555 lines) — session cards
- `casino/renderer/pulse_renderer.py` (418 lines) — pulse dashboard
- `casino/renderer/ledger_renderer.py` (411 lines) — ledger cards
- `flow_cards.py` (897 lines) — flow hub cards
- `sportsbook_cards.py` (1,158 lines) — sportsbook cards
- `card_renderer.py` (817 lines) — trade cards

Questions:
1. Do all renderers use `atlas_style_tokens.py` consistently, or do some hardcode colors/fonts?
2. Is the `wrap_card()` → `render_card()` pipeline used uniformly?
3. Are there any renderers still using Pillow instead of HTML?
4. Is there duplicated HTML/CSS across renderer files?

---

## Expected Output

A single findings document with:

### 1. Severity-Rated Issue List
| # | Severity | Category | File:Line | Description | Est. Fix |
|---|----------|----------|-----------|-------------|----------|

### 2. Refactoring Recommendations
For each god file, a proposed module split with:
- New file names
- Which classes/functions move where
- Import changes needed
- Risk assessment (what could break)

### 3. Quick Wins
Changes that are <10 lines each but improve quality:
- Adding logging to dangerous silent failures
- Removing dead imports
- Quarantining dead files
- Fixing inconsistent patterns

### 4. Technical Debt Summary
| Category | Count | Priority |
|----------|-------|----------|
| Silent failures | 432 | HIGH |
| Print→logging migration | 1,353 | MEDIUM |
| Sync DB in async context | ? | HIGH |
| Dead code files | 8 | LOW |
| God files (>2500 lines) | 5 | MEDIUM |
| Functions >100 lines | ~15 | MEDIUM |

### 5. Production Readiness Checklist
- [ ] All critical silent failures have logging
- [ ] No sync DB calls blocking the event loop
- [ ] All dead code quarantined
- [ ] No god files over 3,000 lines
- [ ] Consistent error handling across all cogs
- [ ] Version bumped to 4.0.0

---

## Reference Files
- `CLAUDE.md` — Architecture docs, code rules, API gotchas
- `docs/ORACLE_V4_HANDOFF.md` — Recent Oracle overhaul context
- `bot.py` — Cog load order, startup sequence
- `atlas_ai.py` — AI client architecture
- `data_manager.py` — Data pipeline

## Important Context
- Python 3.14, discord.py 2.3+, Google Gemini 2.0 Flash, Claude via atlas_ai.py
- Entry point: `bot.py`
- 4 SQLite databases, 1 archive DB (1.3 GB)
- HTML→PNG rendering via Playwright (4-page pool)
- ~31 active TSL teams, 95+ Super Bowl seasons of history
- This is a solo developer project — Nate is the only contributor
