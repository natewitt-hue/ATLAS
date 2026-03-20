# CLAUDEFROG Master Briefing — ATLAS GAP Review

**What this is:** Multiple Claude Code web sessions are doing simultaneous read-only deep dives across the entire ATLAS codebase. Each session produces a handoff doc with exact bugs, line numbers, and fix instructions. You (CLAUDEFROG) receive all handoff docs and execute the fixes locally at `C:\Users\natew\Desktop\discord_bot`.

---

## Review Sessions & Status

| Session | Scope | Files | Status | Handoff Doc |
|---------|-------|-------|--------|-------------|
| **Instance 1** | Oracle & Data Pipeline | `data_manager.py`, `build_tsl_db.py`, `build_member_db.py`, `oracle_cog.py`, `codex_cog.py` | **COMPLETE** | `HANDOFF_data_pipeline_fixes.md` |
| **Sportsbook** | Flow & Sportsbook | `flow_sportsbook.py`, sportsbook-related files | **Running (separate GAP instance)** | TBD |
| **Session A** | Core & AI Engine | `bot.py`, `atlas_ai.py`, `setup_cog.py`, `permissions.py` | **Pending** | `HANDOFF_core_ai_fixes.md` |
| **Session B** | Echo, Render & Casino | `echo_cog.py`, `echo_loader.py`, `affinity.py`, `echo/*.txt`, `atlas_style_tokens.py`, `atlas_html_engine.py`, `card_renderer.py`, `casino/` | **Pending** | `HANDOFF_echo_render_casino_fixes.md` |
| **Session C** | Sentinel, Genesis & Admin | `sentinel_cog.py`, `genesis_cog.py`, `awards_cog.py`, `economy_cog.py`, `polymarket_cog.py`, `commish_cog.py` | **Pending** | `HANDOFF_sentinel_genesis_admin_fixes.md` |

---

## How to Use This

1. Wait for all handoff docs to be delivered
2. Read each `HANDOFF_*.md` — they contain exact line numbers, before/after code, and severity ratings
3. Fix bugs in priority order: BUG (must fix) → RISK (should harden) → CLEAN (no action)
4. Bump `ATLAS_VERSION` in `bot.py` once for all fixes (patch bump: current `4.3.0` → `4.3.1`)
5. Run the bot and verify no regressions

---

## Already Found (Instance 1 — Data Pipeline)

### 3 Bugs
| # | File | Line | Summary |
|---|------|------|---------|
| BUG-1 | `data_manager.py` | 869 | `get_weekly_results()` fallback drops status=2 games |
| BUG-2 | `build_tsl_db.py` | 286-291 | `player_draft_map` non-deterministic teamName in GROUP BY |
| BUG-3 | `build_member_db.py` | 1310-1393 | `resolve_db_username()` connection leak on exception |

### 5 Design Risks
| # | File | Summary |
|---|------|---------|
| RISK-1 | `data_manager.py:292` | Magic number `stageIndex >= 200` undocumented |
| RISK-2 | `data_manager.py:1091` | `snapshot_week_stats()` may read nonexistent fields |
| RISK-3 | `build_tsl_db.py:369` | Missing `team_stats` table in sync path |
| RISK-4 | `codex_cog.py:620` | SQL injection on retry attempts 2-3 (mitigated but not airtight) |
| RISK-5 | `build_member_db.py` | 30+ members with no discord_id |

### 2 Files Clean — No Changes
- `oracle_cog.py` (4305 lines) — all clear
- `codex_cog.py` (1163 lines) — all clear

---

## Ground Rules for CLAUDEFROG

1. **Trust the line numbers** — reviewers read every line, not just grepped
2. **Don't touch CLEAN files** — if a handoff says "no changes needed," skip it
3. **One version bump** — patch bump once, not per-fix
4. **Test after each bug fix** — don't batch all fixes then test
5. **If a RISK says "investigate"** — do the investigation before fixing; the reviewer flagged uncertainty
6. **Commit message format:** `Fix BUG-1: description` or `Harden RISK-1: description`
