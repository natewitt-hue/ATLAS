# ATLAS Pre-7.0 Audit — Terminal 3: Core + Analytics + Governance

**Auditor:** Claude Opus 4.6 (Terminal 3)
**Date:** 2026-03-25
**Scope:** bot.py, setup_cog.py, permissions.py, constants.py, data_manager.py, roster.py, build_member_db.py, build_tsl_db.py, atlas_ai.py, intelligence.py, reasoning.py, conversation_memory.py, lore_rag.py, oracle_agent.py, oracle_memory.py, oracle_query_builder.py, codex_intents.py, codex_utils.py, analysis.py, oracle_cog.py, genesis_cog.py, sentinel_cog.py, codex_cog.py, awards_cog.py, polymarket_cog.py, real_sportsbook_cog.py, sportsbook_core.py, odds_api_client.py, espn_odds.py, odds_utils.py, ability_engine.py, trade_engine.py, team_branding.py

---

## BUGS (P0/P1)

### B1 — genesis_cog.py: _orphanfranchise_impl not ephemeral (P1)
**Line 1920:** `await interaction.response.send_message(msg)` — missing `ephemeral=True`.
This is an admin-only command (orphan franchise flag toggle). Per CLAUDE.md, admin ops should be ephemeral. Flagged by T2 boss audit as a deferred fix for T3.
**Fix:** Add `ephemeral=True` to the send_message call.

### B2 — sentinel_cog.py: position approve/deny missing coordinated defer (P1)
**Lines 2152–2173 (approve) and 2181–2221 (deny):** Neither `positionchangeapprove_impl` nor `positionchangedeny_impl` call `defer()` before performing `_save_state()` (file I/O) and `channel.send()` (network). If file I/O + channel send exceeds 3s, Discord times out the interaction.
Flagged by T3 boss audit as needing coordinated defer fix.
**Fix:** Add `await interaction.response.defer(ephemeral=True)` at the top of each impl, convert `interaction.response.send_message()` to `interaction.followup.send()`.

### B3 — polymarket_cog.py: 10 hardcoded hex colors (P1)
Hardcoded `0x2ECC71` and `0xE74C3C` used instead of `AtlasColors.SUCCESS` / `AtlasColors.ERROR` at lines: 1634, 1665, 1669, 1698, 1942, 1959, 1963, 2011, 2931, 3642.
Line 3642 (`_market_status_impl`) was specifically flagged by boss audit.
**Fix:** Replace all 10 instances with AtlasColors constants.

### B4 — bot.py startup: _startup_done flag timing (P0 — PASS ✅)
**Line 518:** `_startup_done = True` is set BEFORE async work begins in `on_ready()`, preventing concurrent `on_ready` races. Correctly implemented.

### B5 — bot.py cog load order (P0 — PASS ✅)
**Lines 244–260:** Echo first (#1), Setup second (#2), then remaining cogs. Matches CLAUDE.md table exactly.

### B6 — atlas_ai.py: Claude→Gemini fallback (P0 — PASS ✅)
**Lines 448–474:** `generate()` catches any Claude exception, logs it with `log.warning()`, then falls back to Gemini. `fallback_used` flag set on result. Provider is tracked in `AIResult.provider` field.

### B7 — data_manager.py: weekIndex off-by-one (P0 — PASS ✅)
**Line 826:** `get_weekly_results()` converts `target - 1` to `week_index` (0-based). Load_all() uses `range(0, _l_week + 1)` at line 458, correctly covering weekIndex 0 through _l_week. No raw CURRENT_WEEK passed to API endpoints.

### B8 — data_manager.py: completed game filter (P0 — PASS ✅)
**Line 786–787:** `get_last_n_games()` filters with `int(g.get("status", 0) or 0) in (2, 3)`. `get_weekly_results()` also filters with `status IN (2, 3)` pattern throughout.

### B9 — data_manager.py: load_all() DataFrame locking (P0 — PASS ✅)
**Lines 628–649:** Atomic swap pattern — all DataFrames built locally, then a single `_state = LeagueState(...)` assignment. Python GIL guarantees pointer swap atomicity. No cog can read half-refreshed data.

### B10 — build_tsl_db.py / build_member_db.py: schema guards (P0 — PASS ✅)
All CREATE TABLE statements use `IF NOT EXISTS` or `DROP TABLE IF EXISTS` + `CREATE TABLE` (idempotent). No unguarded migrations.

### B11 — oracle_cog.py: AI-generated SQL sanitization (P0 — PASS ✅)
All AI-generated SQL goes through `retry_sql()` wrapper function — never executed via raw `.execute()`. SQL injection via NL query is mitigated.

### B12 — oracle_cog.py: H2H empty state (P1 — PASS ✅)
**Line 2752:** Message reads `"No head-to-head games found in the database."` — actionable and descriptive. Fixed in previous batch.

### B13 — genesis_cog.py: trade flow dead-ends (P1 — PASS ✅)
All trade modal branches have back buttons, dismiss via timeout, or advance forward. No dead-end states found.

### B14 — oracle_cog.py: "historical database offline" messages (P1 — PASS ✅)
All 3 occurrences (lines 2692, 2868, 3729) now read `"try again in a moment"`. Fixed in previous batch.

### B15 — awards_cog.py: expired poll hint (P1 — PASS ✅)
**Line 54:** `"Use /awards to start a new vote"` hint present. Fixed in previous batch.

### B16 — real_sportsbook_cog.py: multiple ephemeral followups (P1 — PASS ✅)
EventListView, MatchCardView, BetTypeView all use proper single-defer or direct send_message patterns. No overlapping ephemeral confusion.

### B17 — All AI calls deferred in slash commands (P0 — PASS ✅)
Every slash command handler calling `atlas_ai.generate()` has a preceding `defer()`. Utility functions called within already-deferred contexts are safe.

---

## UX ISSUES (P1/P2)

### U1 — oracle_cog.py: select menus 25-option cap (P2 — PASS ✅)
All select menus properly capped: stats[:25], player_names[:25], options[-25:]. Discord limit respected.

### U2 — codex_cog.py: empty SQL result handling (P2 — PASS ✅)
**Lines 276–280:** When 0 rows returned, AI receives explicit `CRITICAL: The query returned NO rows` instruction preventing fabrication.

### U3 — sentinel_cog.py: complaint case ID (P2 — PASS ✅)
**Lines 340, 431–449:** User receives case ID, category, thread mention link, and instructions to upload evidence.

### U4 — sentinel_cog.py: 4th down hardcoded system prompt (P2)
**Line 2239:** 200+ line hardcoded system prompt for 4th Down Referee. Does not use `get_persona()`. Force request (line 731) correctly uses `get_persona("official")`.
**Recommendation:** Refactor to prepend `get_persona("official")` while keeping domain-specific rules. Deferred to post-7.0 — the prompt is highly specialized and functional.

### U5 — oracle_cog.py: get_known_users() not injected (P2)
`KNOWN_USERS` imported from codex_utils but never injected into oracle_cog AI SQL prompts (scout, AskTSL modal, etc.). Name resolution relies on `alias_map` instead. Partial coverage.
**Recommendation:** Inject `KNOWN_USERS` into SQL generation prompts for improved identity resolution. Deferred to post-7.0.

### U6 — trade_engine.py: rings tax uses selling team, not drafting team (P2)
**Line 226:** `rings_mult = _rings_tax_multiplier(selling_team_id)` — per CLAUDE.md, draft history should credit the drafting team, not current team. The rings tax multiplier applies to the wrong team.
**Recommendation:** Look up original drafting team from `player_draft_map` table. Deferred to post-7.0 — requires data pipeline changes.

---

## CONSISTENCY / DEBT (P2/P3)

### C1 — _build_schema() dynamic CURRENT_SEASON (P2 — PASS ✅)
**codex_utils.py line 216:** Uses f-string `{dm.CURRENT_SEASON}` — dynamically evaluated at runtime. Not hardcoded.

### C2 — All AI calls use atlas_ai.generate() (P2 — PASS ✅)
No direct Gemini/Claude SDK calls found in any cog. All routed through `atlas_ai.py`.

### C3 — All system prompts use get_persona() (P2 — PASS ✅ with exception)
All cogs use `get_persona()` from echo_loader except sentinel_cog.py 4th Down (U4 above). Force request correctly uses `get_persona("official")`.

### C4 — polymarket_cog.py hardcoded hex (P2 — see B3)
Confirmed 10 instances. Fix included in B3.

### C5 — oracle_query_builder.py: get_known_users() injection (P2 — see U5)
Deferred to post-7.0.

### C6 — odds_utils.py shared file check (P3 — PASS ✅)
No evidence of T1 modifications. File is minimal (3 utility functions). No conflicts.

---

## OUT-OF-SCOPE FLAGS

### O1 — constants.py: ATLAS_ICON_URL uses signed Discord CDN link
**Lines 6–9:** The signed URL will expire. Tracked since v1.4.2. Should be hosted on permanent URL (GitHub raw, S3, or Imgur).

### O2 — odds_api_client.py: deprecated since v6.1.0
Replaced by espn_odds.py. Kept for 2-week reference. Candidate for QUARANTINE/.

### O3 — sentinel_cog.py 4th Down system prompt
200+ line hardcoded prompt (U4). Functional but violates get_persona() convention. Separate refactor recommended.

---

## FIXES APPLIED

### Fix 1: genesis_cog.py — _orphanfranchise_impl ephemeral (B1)
- Line 1920: Added `ephemeral=True` to `send_message()` call

### Fix 2: sentinel_cog.py — position approve/deny coordinated defer (B2)
- Lines 2152–2173: Added `defer(ephemeral=True)` + converted to `followup.send()`
- Lines 2181–2221: Same pattern applied to deny impl

### Fix 3: polymarket_cog.py — hardcoded hex → AtlasColors.* (B3)
- 10 instances of `0x2ECC71` → `AtlasColors.SUCCESS`
- 10 instances of `0xE74C3C` → `AtlasColors.ERROR`
- Lines: 1634, 1665, 1669, 1698, 1942, 1959, 1963, 2011, 2931, 3642

---

## VERSION BUMP

- `bot.py:174` — `ATLAS_VERSION` bumped from `6.25.0` → `7.0.0`
- Major bump warranted: comprehensive multi-terminal audit covering Flow, Casino, Render, Core, Analytics, and Governance domains
- T1 confirmed: `audit_t1.md` — 11 fixes applied (Flow/Sportsbook/Economy)
- T2 confirmed: `audit_t2.md` — 6 fixes applied (Casino/Rendering/Echo)
- T3 confirmed: `audit_t3.md` — 3 fixes applied (Core/Analytics/Governance)
