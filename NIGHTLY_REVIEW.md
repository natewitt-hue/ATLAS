# ATLAS Nightly Code Review
**Audit:** Thursday — Genesis, Sentinel & Compliance Modules
**Date:** 2026-03-26
**ATLAS Version at audit:** v7.9.2
**Auditor:** Automated (audit-thursday-genesis scheduled task)
**Files Audited:** genesis_cog.py, sentinel_cog.py, trade_engine.py, ability_engine.py, card_renderer.py, roster.py, god_cog.py

---

## TL;DR

| Severity | Count | Summary |
|----------|-------|---------|
| CRITICAL | 1 | `_serialize_player` omits `rosterId` — roster staleness check bypassed for all picker trades |
| Warning | 5 | Blocking I/O in async; post-restart double-ruling; counter on resolved trade; My Team user_id omission; unknown pos-change defaults to legal |
| Confirmed OK | 20 | Lock added; AI prompt fixed; devTrait mapping correct; budget enforcement correct |

---

## Phase 1 — Recent Changes Triage

**Last 48h commits touching focus files:**

| Commit | Summary |
|--------|---------|
| `c341bc0` | Full codebase bug sweep — 34 fixes across 21 files (v7.9.2) |
| `290f71e` | Add Change Team A override button to trade flow |
| `52df7cd` | Auto-preselect user team as Team A in trade flow |
| `6be184f` | Add My Team quick-select to trade ConferenceSelectView |
| `23e1d3c` | Add get_team_dict() helper for my-team quick-select |
| `d837643` | 4th down analyzer — 6 prompt improvements |

**v7.9.2 diff summary (focus files):**
- `genesis_cog.py`: Added `_trade_approval_lock = asyncio.Lock()` wrapping `_update_status()`; replaced inline admin check with `is_commissioner()` in `btn_tradelist`
- `sentinel_cog.py`: `_SYSTEM_PROMPT` constant converted to `_get_system_prompt()` function (fresh persona on each call); `result` variable renamed to `ai_response`/`result_data` (eliminates collision)
- `trade_engine.py`: Module-level imports cleaned up; parity state path fixed to `pathlib.Path(__file__).parent`

---

## Phase 2 — Deep Audit Findings

### [CRITICAL] _serialize_player missing rosterId (genesis_cog.py ~142)

**Root cause:** `_serialize_player()` returns 8 fields but NOT `rosterId`.

**Impact:** In `_update_status()`, the roster staleness check iterates over `trade.get("players_a_data")` which was built via `_serialize_player()` at proposal time. For every stored player:

    rid = p.get("rosterId")  # None for ALL players — field was never serialized
    if rid is None:
        continue             # silently skips ALL players

The integrity check is **completely bypassed** for all trades submitted via the player picker (the default path). A trade proposed with players who have since been cut or moved to another team will pass approval unchallenged.

**Fix:** Add `"rosterId": p.get("rosterId")` to `_serialize_player()`.

---

### [Warning] Blocking file I/O in async context (trade_engine.py ~342)

`evaluate_trade()` is called from `_evaluate_and_post()` which is async. It contains:

    with open(_PARITY_STATE_PATH) as f:   # blocks the event loop
        parity = json.load(f)

Under load (multiple concurrent approvals or slow disk) this causes event loop stalls.

Fix option A (recommended): Pre-load parity_state.json at module level; refresh from genesis_cog._save_state() after writes.
Fix option B: `asyncio.to_thread(lambda: json.loads(_PARITY_STATE_PATH.read_text()))`.

---

### [Warning] Post-restart double-ruling possible (sentinel_cog.py ~571)

`RulingPanelView._acted: bool = False` is in-memory only. After a bot restart, persistent views re-register with `_acted = False`. A commissioner can issue a second ruling on the same complaint post-restart.

**Fix:** At the start of each ruling button callback, check `_complaints[cid].get("verdict") not in (None, "pending")` in addition to `self._acted`.

---

### [Warning] Counter callback does not check resolved status (genesis_cog.py ~1705)

`_counter_callback()` opens a `CounterModal` without checking whether the trade is already approved/rejected. A user with a stale view could submit a counter against a resolved trade, creating a phantom pending entry in `_trades`.

**Fix:** Add at top of `_counter_callback()`:

    if trade.get("status") in ("approved", "rejected"):
        return await interaction.response.send_message(
            "This trade has already been resolved.", ephemeral=True
        )

---

### [Warning] My Team quick-select loses user_id in Step B (genesis_cog.py ~1032)

In `ConferenceSelectView._my_team_callback` (Step B branch), the next `ConferenceSelectView` is instantiated without `user_id=self.user_id`. The My Team button will not appear in Step B because the `user_id is None` guard short-circuits it.

**Fix:** Pass `user_id=self.user_id` when constructing the next `ConferenceSelectView` in `_my_team_callback`.

---

### [Warning] Unknown position combos default to LEGAL (ability_engine.py ~1201)

    rule = POSITION_CHANGE_RULES.get((from_pos.upper(), to_pos.upper()))
    if rule is None:
        return True, reasons   # legal=True for anything not in the rulebook

Any position change not in `POSITION_CHANGE_RULES` (e.g., HB to CB, DT to LB) returns `legal=True` with a soft commissioner discretion note. ATLAS auto-approves novel combos.

**Consider:** Flip default to `return False` with "Not in rulebook — commissioner must manually approve."

---

## Phase 2 — Confirmed Correct

| Item | File | Status |
|------|------|--------|
| TOCTOU lock on trade approval | genesis_cog.py:86 | OK - asyncio.Lock() wraps status check + defer |
| _get_system_prompt() lazy evaluation | sentinel_cog.py:731 | OK - fresh get_persona() per call |
| Variable rename result -> ai_response/result_data | sentinel_cog.py:814 | OK - collision eliminated |
| _validate_image_url() SSRF guard | sentinel_cog.py:62 | OK - Discord CDN only |
| Prompt injection defense via untrusted_user_note | sentinel_cog.py | OK |
| Parity state path fixed to __file__ parent | trade_engine.py:27 | OK |
| Parity state read once per evaluate_trade() call | trade_engine.py:342 | OK (but blocking — see warning) |
| devTrait int->string mapping | ability_engine.py:45 | OK - {0:Normal, 1:Star, 2:SS, 3:XF} confirmed |
| Dev budget enforcement | ability_engine.py:38 | OK - Star=1B, SS=1A+1B, XF=1S+1A+1B confirmed |
| OR logic for _edge_pmv_or_fmv | ability_engine.py:550 | OK - max(pmv, fmv) |
| Unknown abilities treated as C-tier | ability_engine.py:643 | OK - warning logged, not flagged |
| Audit uses ability1-6 (not playerAbilities endpoint) | ability_engine.py:742 | OK - correct |
| Budget post-check in pick_replacement() | ability_engine.py:1016 | OK - linear scan fallback if top pick busts budget |
| XSS protection via _esc() | card_renderer.py | OK |
| Player display cap at 4 | card_renderer.py | OK - prevents card overflow |
| god_cog.py double-guard on both commands | god_cog.py:57,83 | OK |
| god_rebuilddb uses run_in_executor | god_cog.py:94 | OK - correct async pattern |
| Atomic cache swap in roster.load() | roster.py:148 | OK - race-safe |
| Duplicate holder removal in roster.assign() | roster.py:255 | OK |
| _save_trade_state() atomic write via os.replace | genesis_cog.py | OK - crash-safe |
| CB -> LB permanently banned | ability_engine.py:1163 | OK - _CB_TO_LB_BAN enforced |

---

## Phase 2 — Anti-Pattern Scan Results

| Pattern | Focus Files | Result |
|---------|-------------|--------|
| Bare except: pass | All focus files | CLEAN — grep hits are changelog comments only |
| time.sleep | All focus files | CLEAN — no sleep in focus files; all in offline/scraper scripts |
| sqlite3.connect in async | roster.py:148,255,291 | 3 blocking calls from async context — sub-ms ops, low risk |
| eval/exec | All focus files | CLEAN — none in focus files; sandboxed in oracle_agent.py/reasoning.py |
| f-string SQL injection | genesis_cog.py, sentinel_cog.py | CLEAN — all SQL uses parameterized queries |

---

## Phase 4 — CLAUDE.md Health Check

**Missing from Module Map:** `ability_engine.py` (1,275 lines) has no entry. It provides the ability audit engine, budget enforcement, and position change validation — consumed by genesis_cog.py and exposed via /abilityaudit and /abilitycheck.

Suggested addition:

    | Ability Engine | Lock & Key ability audit, dev budget enforcement, position change validation | ability_engine.py |

**Cog Load Order:** `god_cog.py` is not listed in the load order table. No load-order dependency but could be added as entry #16 for completeness.

**MaddenStats API Gotchas:** All rules confirmed active in codebase. No gaps found.

**Nightly audit task:** Confirm `ability_engine.py` is in `audit-thursday-genesis/SKILL.md` focus file list — it is a Thursday-domain file (Genesis consumer).

---

## Action Items for Morning

| Priority | File | Action |
|----------|------|--------|
| Fix immediately | genesis_cog.py:~142 | Add "rosterId": p.get("rosterId") to _serialize_player() |
| Fix soon | trade_engine.py:~342 | Pre-load parity state or wrap open() in asyncio.to_thread |
| Fix soon | sentinel_cog.py:~571 | Add verdict persistence check in RulingPanelView button callbacks |
| Fix soon | genesis_cog.py:~1705 | Add resolved status guard at top of _counter_callback() |
| Fix soon | genesis_cog.py:~1032 | Pass user_id=self.user_id in My Team Step B re-instantiation |
| Consider | ability_engine.py:~1201 | Flip unknown pos-change default from True to False |
| Docs | CLAUDE.md | ~~Add ability_engine.py to Module Map table~~ DONE this session |
| Docs | audit-thursday-genesis/SKILL.md | Already present — ability_engine.py confirmed in focus list |

---

## CLAUDE.md Updates

Changes made this session:
- **Module Map** — Added `Ability Engine` row: `ability_engine.py` — Lock & Key ability audit, dev budget enforcement, position change validation.

---

*Audit completed by audit-thursday-genesis scheduled task · ATLAS v7.9.2 · 2026-03-26*
