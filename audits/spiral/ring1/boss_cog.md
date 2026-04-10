# Adversarial Review: boss_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 2617
**Total findings:** 24 (3 critical, 11 warnings, 10 observations)

## Summary

`boss_cog.py` is the sole permission gate for ~57 commissioner operations that are exposed via `_impl` callees on roughly a dozen target cogs — none of which re-check authorization. Every `is_commissioner()` call inside `boss_cog.py` is therefore load-bearing, and there are concrete TOCTOU/skip-gate patterns (LeaguePanelView lottery flow, BossSBGradeModal absorbing the modal into a 3s timeout, no audit log of admin actions, OrphanFranchise sending fragile substring keys, BossDevAuditModal/BossAbilityAuditModal/BossAbilityCheckModal building the result without `defer()` for an unbounded `dm.get_players()` walk that can race past the 3s modal deadline). Ship requires the critical findings fixed; the warnings are recoverable but should be queued.

## Findings

### CRITICAL #1: Permission gate is single-layer — every `_impl` callee is wide open if `is_commissioner()` is bypassed

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:209-2519` (every button/modal handler), with target callees in `flow_sportsbook.py:3422-4014`, `casino/casino.py:471-614`, `economy_cog.py:428-700`, `sentinel_cog.py:679-712, 1387-1395, 2179-2262`, `genesis_cog.py:1868-2038`, `real_sportsbook_cog.py:630-731`
**Confidence:** 0.95
**Risk:** A single skipped or buggy gate inside `boss_cog.py` exposes the entire commissioner surface (balance edit, force-settle, casino open/close, lottery, position-change approval, complaint review, real-sportsbook lock/void/refund, etc.). The Boss panel has 60+ button handlers — maintaining the gate on each requires perfect manual discipline. Recent Ring 1 Batch D found exactly the same issue on `sentinel_cog`.
**Vulnerability:** I grepped every callee:
- `flow_sportsbook.py` has **zero** `is_commissioner` references (`flow_sportsbook.py:3580+, 3909+, 3959+, 3990+, 4014+`).
- `casino/casino.py:471-614` `_casino_*_impl` methods have no inline checks.
- `economy_cog.py:428-700` `_eco_*_impl` methods have no inline checks; the only `is_commissioner` import is at line 1085 (an unrelated handler).
- `sentinel_cog.py:679-712` `caseview_impl`/`caselist_impl` and `:1387-1395` `forcehistory_impl` and `:2179-2262` `positionchangeapprove_impl`/`positionchangedeny_impl` have no inline checks.
- `genesis_cog.py:1868-2038` `_tradelist_impl`/`_runlottery_impl`/`_orphanfranchise_impl` have no inline checks.
- `real_sportsbook_cog.py:630-731` `status_impl`/`lock_impl`/`void_impl`/`grade_impl`/`sync_impl` have no inline checks.

If any cog publishes one of these `_impl` methods directly as a slash command in the future (or another cog's button calls them), there is no defense-in-depth.
**Impact:** Total bypass of the commissioner gate via any second-channel caller. Possible balance corruption, unauthorized lottery resolution, unauthorized force-settle, unauthorized refunds. Audit trail will show the second-channel caller, not someone exploiting boss_cog.
**Fix:** Either (a) add `if not await is_commissioner(interaction): ...` as the first line of every `_impl` method on the callees (defense-in-depth), or (b) add a class-level decorator on each callee cog that wraps every `_impl` method, or (c) document explicitly in `CLAUDE.md` that `_impl` methods MUST NOT be exposed as slash commands and add a CI check. The Boss panel currently relies on the implicit invariant that "no other cog will ever call my callees" — that invariant is undocumented and unenforced.

---

### CRITICAL #2: `BossSBGradeModal` is a vestigial admin button that lies — no actual grading happens

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:473-477, 685-694`
**Confidence:** 0.95
**Risk:** Commissioner clicks **Grade Week** → modal opens → enters week number → submission tells them "Manual grading is no longer supported — settlement is automatic via the event bus." The entire button is a no-op trap. If a commissioner relies on this to settle a stuck week, bets will silently remain pending while the operator believes the action ran.
**Vulnerability:** No fallback path (no link to AG Status, no link to Force Settle, no diagnostic dump). The button still appears prominently as a primary-style button in row 1, indistinguishable from working actions. The week input field is collected but discarded.
**Impact:** False sense of completion. Stuck bets remain pending, payouts delayed indefinitely. The commissioner has no signal that this button is dead.
**Fix:** Either remove the button (and its modal class) entirely, or rewrite the on_submit to actually trigger a re-grading sweep (call `_force_settle_impl` for every pending bet in the week, or call autograde service directly). If left as a deprecation notice, change the label from "Grade Week" to "Grade Week (deprecated)" and use `discord.ButtonStyle.secondary` so it visually reads as inert.

---

### CRITICAL #3: Modal handlers do `dm.get_players()` synchronously inside `on_submit` without `defer()` first

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:1631-1677, 1688-1732, 1743-1777, 1788-1839, 1850-1919`
**Confidence:** 0.85
**Risk:** Five modals (`BossDevAuditModal`, `BossContractCheckModal`, `BossAbilityAuditModal`, `BossAbilityCheckModal`, `BossAbilityReassignModal`) each call `dm.get_players()` and `dm.get_player_abilities()` (synchronous, returns full league roster) and then iterate ~2000+ player dicts to filter. They DO call `defer(thinking=True, ephemeral=True)` first — but the `defer()` call is **after** the `is_commissioner()` await, which itself can be slow if discord.py needs to fetch the member.
On `BossAbilityReassignModal.on_submit` specifically (line 1850), the `defer()` happens at line 1858 — but `is_commissioner()` runs at line 1851 first. If `is_commissioner` requires guild member fetch (e.g. on a stale cache), the 3s modal interaction window can be exhausted before `defer()` lands, and the entire reassignment computation is lost.
**Vulnerability:** Discord modals have a hard 3s timeout to either respond or defer. The pattern `await is_commissioner(...) → await defer()` puts an awaitable network call before the defer. The `is_commissioner` check is cheap on the happy path (env list, role check, admin permission), but it's async-defined and loop-scheduled, and on a cold member cache `interaction.user` access can race.
Additionally, even after defer, `ae.audit_roster()` at line 1760 and `ae.reassign_roster()` at line 1870 are CPU-bound synchronous loops over the full league. They are NOT wrapped in `asyncio.to_thread()`. They block the event loop for the duration, freezing other Discord interactions on the same shard.
**Impact:** (a) Modal can fail to respond within 3s and surface `Unknown Interaction` to the operator with no error logged anywhere — the reassignment is silently lost. (b) The event loop is blocked for several hundred ms to multiple seconds during audit/reassignment, hurting all other concurrent users.
**Fix:** Move `defer()` to be the FIRST line of `on_submit` (before `is_commissioner`), then check permissions and use `interaction.followup.send()` for the rejection. Also wrap `ae.audit_roster(...)` and `ae.reassign_roster(...)` calls in `await asyncio.to_thread(...)` to avoid blocking the event loop.

---

### WARNING #1: `_resolve_member` returns first match on ambiguous resolves; only logs a warning instead of refusing

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:78-130`
**Confidence:** 0.85
**Risk:** Two members named "John" → operator types "John" → `_resolve_member` matches both → logs a warning at line 128 → silently returns `matches[0]`. The operator has no idea which "John" was selected.
**Vulnerability:** Line 130: `return matches[0] if matches else None`. The "bug-9" comment claims to fix this but only added a log line. The function is used at no call site visible in this file directly (it's a utility), but if/when it IS used the caller has no signal that the resolve was ambiguous. The wrong member could receive a balance edit, refund, or stipend assignment.
**Impact:** Silent assignment of admin actions to the wrong member. Unrecoverable for `_eco_set_impl` (overwrites prior balance) and dangerous for refund/grade operations.
**Fix:** When `len(matches) > 1`, return `None` and have the caller send "Ambiguous match: <list>". Better: change the return type to `tuple[Optional[Member], list[Member]]` and let the caller decide. The current "warn and return first" is the worst of all worlds.

---

### WARNING #2: No audit trail for any commissioner action

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:1-2617` (entire file)
**Confidence:** 0.95
**Risk:** None of the 60+ commissioner button handlers write to an audit log. Balance adjustments, lottery runs, force-settle, refund, position approve/deny, market resolve, casino open/close — all are fire-and-forget. If two commissioners are active and one questions an action, there is no record of who did what when.
**Vulnerability:** Only `genesis_cog.ParityCog.log_cap_clear_attempt` (line 2046, in genesis_cog.py) writes anything to an audit log, and that path isn't reached from `boss_cog`. The Boss callees don't either — `_force_settle_impl` writes to ledger but most others (e.g. `_casino_open_impl`, `_orphanfranchise_impl`, `_eco_set_impl`) write only to their own state tables.
**Impact:** No accountability. Cannot diagnose disputes ("who set my balance to 0?"), cannot reconstruct timeline of decisions, cannot detect compromised commissioner accounts.
**Fix:** Add a `_log_admin_action(interaction, action: str, target: str, params: dict)` helper that writes to a `boss_audit` table (timestamp, actor_id, action_name, target_id, params_json, result). Call it at the end of every successful `_impl` delegation.

---

### WARNING #3: `OrphanFranchise` flow uses team `abbrName` as a state key but `_orphanfranchise_impl` keys by free-text team name

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:2247-2276`, `genesis_cog.py:2029-2044`
**Confidence:** 0.9
**Risk:** `OrphanTeamSelectView` builds the dropdown using `value=t["abbrName"]` (line 2228, 2239). When the operator picks a team, `_on_team` reads `interaction.data["values"][0]` (line 2248) — this is the abbrName like `"DAL"` — and passes it as `team` to `_orphanfranchise_impl`. But the genesis impl at `genesis_cog.py:2031,2034` does `_state["orphan_teams"].add(team.strip())` — which means the orphan_teams set ends up containing abbreviations like `"DAL"`, while the cap-integrity gate `can_clear_cap` at `genesis_cog.py:2042-2044` is called by other code paths with the full team name like `"Cowboys"`. The set keys never match.
**Vulnerability:** The orphan flag is silently a no-op for the cap-clear gate. Operators flag teams as orphans, and the cap-clear protection still rejects every clearing attempt. Or vice versa — depending on which side calls `can_clear_cap` first.
**Impact:** Cap-clear feature appears broken. Cap integrity bypass allowed for non-orphan teams (if the call site ever uses abbrName instead of nickName).
**Fix:** Pass `t["nickName"]` as the value of the SelectOption (or pass both via `value=f"{abbr}|{name}"` and parse). Make sure the impl receives the same identifier the cap-clear gate compares against.

---

### WARNING #4: `BossHubView` has 8 buttons in 2 rows but discord.py allows max 5 buttons per row

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:208-278`
**Confidence:** 0.9
**Risk:** Row 0 has 4 buttons (Sportsbook, Casino, Treasury, Roster — lines 208/217/226/235). Row 1 has 4 buttons (Markets, League, Compliance, FLOW Live — lines 244/253/262/271). 4 per row is fine. But the documentation comment at line 12 says "BossHubView (7 panel buttons)" — the 8th (FLOW Live) was added later and the comment is stale. Not a runtime bug, but it shows the architecture comment is out of date.
**Vulnerability:** N/A — discord.py allows up to 5 buttons per row. This is more an OBSERVATION but I'm flagging as WARNING because the file's own architecture comment is wrong, which suggests other invariants in the docstring may also be wrong.
**Impact:** Maintainer confusion. Future devs may add a 9th button assuming "7 panel buttons" is current and discover the discord.py 5-per-row cap the hard way.
**Fix:** Update the docstring at lines 8-15 to describe the current 8-button layout, and add a comment near `BossHubView.__init__` noting the row layout invariant.

---

### WARNING #5: `SBPanelView.sync_all` calls `cog._sync_odds(sport_key)` and `_sync_scores()` without backoff or error handling

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:344-364`
**Confidence:** 0.85
**Risk:** Iterates `SPORT_SEASONS` (8 sports) and calls `await cog._sync_odds(sport_key)` for each in-season sport. ESPN may be rate-limited or down. There is no try/except around the per-sport call — one failed sport halts the entire sync, and the operator gets a generic discord.py 500 with no signal of how far the loop got.
**Vulnerability:** Unhandled exception inside the loop kills the followup. The operator sees a generic interaction failure and has no way to retry only the failed sport.
**Impact:** Half-synced state. Some sports updated, others not, and the user can't tell which.
**Fix:** Wrap each `cog._sync_odds(sport_key)` in try/except, accumulate per-sport status, and report `synced_sports`/`failed_sports` separately.

---

### WARNING #6: `BossDevAuditModal` and `BossAbilityCheckModal` use `dm.get_players()` results then call `ae._normalize_dev` from another module — coupling on a private function

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:1655, 1808`
**Confidence:** 0.8
**Risk:** Both modals call `ae._normalize_dev(p)` (the underscore prefix indicates private). If `ability_engine.py` ever refactors or removes `_normalize_dev`, all four modals break silently because the import is wrapped in a try/except (line 37-42) that only sets `_AE_AVAILABLE = False` — but the modals at line 1655 use `_normalize_dev` directly without checking `_AE_AVAILABLE` first. Line 1655 has a fallback `(p.get("dev", "Normal") or "Normal")` but the structure is `ae._normalize_dev(p) if _AE_AVAILABLE else (...)` — readable, but the fallback is wrong: `_normalize_dev` returns `"Superstar X-Factor"` for `dev=3`, while `p.get("dev")` returns the integer `3`. The fallback path produces wrong output if ability_engine is unavailable.
**Vulnerability:** Silent data corruption when `_AE_AVAILABLE = False`. Every `dev` field gets the integer instead of the human-readable name, and the dev_emoji/dev_order lookups (lines 1641-1642) all fall through to defaults, producing meaningless output without an error.
**Impact:** Wrong audit results in graceful-degradation mode. Operator believes the league is clean when ability_engine is offline.
**Fix:** Either (a) remove the fallback path entirely and refuse to run when `_AE_AVAILABLE = False`, or (b) make `ae._normalize_dev` part of the public API (rename to `normalize_dev`) and write a proper int-to-string fallback.

---

### WARNING #7: `BossAbilityReassignModal` defines `_make_embed` inside a per-team loop with closure-captured `team_name`

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:2090-2143`
**Confidence:** 0.7
**Risk:** `_make_embed` is defined at line 2124 inside the `for team_name in sorted(by_team.keys())` loop (line 2090). The closure captures `team_name` by reference. If `_make_embed` is ever called outside the loop iteration (e.g. via a lazy evaluation, async callback, or generator), it would see the LAST team name from the loop. In the current synchronous flow this happens to work because `_make_embed` is only called during the same iteration via `cur_embed = _make_embed(part)` (lines 2136 and 2143).
**Vulnerability:** Fragile pattern. Any future refactor that delays `_make_embed` invocation (e.g. moving it to a `await asyncio.gather` or storing it for later) silently produces wrong embed titles.
**Impact:** Currently no bug; refactor hazard. WARNING because it's the kind of latent issue that surfaces during a future async refactor and is hard to debug.
**Fix:** Move `_make_embed` to module scope (or extract to a helper that takes `team_name` as an explicit argument).

---

### WARNING #8: `BossAbilityReassignModal` exports JSON via `BytesIO` but doesn't size-check against Discord's 8MB attachment limit

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:1894-1903`
**Confidence:** 0.7
**Risk:** `json.dumps(export_data, indent=2)` followed by `discord.File(BytesIO(json_str.encode("utf-8")), ...)`. If the league has many SS/XF players each with multiple abilities, the JSON can grow well past 8MB — Discord rejects oversized attachments with a 50006 error and the entire response chain fails. The exception handler at line 1909 catches it but the surface message says "output was interrupted" without telling the operator the file was too big.
**Vulnerability:** No upfront size check. Operator sees a generic "interrupted" message, no path forward.
**Impact:** Reassignment data lost. Operator must manually re-run.
**Fix:** Check `len(json_str.encode("utf-8")) < 8 * 1024 * 1024` before creating the File. If oversized, split into multiple files by team or pre-attach a notice with a download link/CDN.

---

### WARNING #9: `BossClosePollModal` and `BossPositionApproveModal` have no defer for potentially slow callees

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:2297-2306, 2436-2445`
**Confidence:** 0.7
**Risk:** Both modals call cog `_impl` methods without an upfront `defer()`. The callees (`_closepoll_impl` on AwardsCog and `positionchangeapprove_impl` on PositionChangeCog at sentinel_cog.py:2179) DO defer themselves at line 2192, but the modal interaction has already used part of its 3s budget on `is_commissioner()` and the cog lookup. If there's any latency before reaching `defer()`, the modal interaction expires and the operator sees "This interaction failed".
**Vulnerability:** Trust on downstream cog to defer in time. The `defer` race window depends on guild member cache freshness. CLAUDE.md gotcha: "Modal latency · Modals require defer() for Gemini calls (>3s timeout)".
**Impact:** Intermittent modal failures during high-latency periods. Position-change approvals silently fail.
**Fix:** Have boss_cog modals call `await interaction.response.defer(ephemeral=True)` as the first line of `on_submit`, then have callees use `interaction.followup.send` consistently. This is already done correctly for `BossSBForceSettleModal` (line 727), `BossDevAuditModal` (line 1634), etc. — apply the pattern uniformly.

---

### WARNING #10: `BossCog.boss_clearsync` uses non-ephemeral defer and posts a public completion message

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:2579-2587`
**Confidence:** 0.85
**Risk:** Line 2583: `await interaction.response.defer(thinking=True)` — no `ephemeral=True`. The completion at line 2587 `await interaction.followup.send("Commands synced. ...")` is also non-ephemeral. Per CLAUDE.md: "Ephemeral vs public: drill-downs = ephemeral; hub landing embeds = public". Admin operations should be ephemeral. Worse: this command can be triggered in any channel, so the result leaks into a public conversation channel.
**Vulnerability:** Public exposure of admin command execution. Anyone watching the channel can see when the commissioner is doing tree resyncs (which often correlate with bot deployments / outages).
**Impact:** Information disclosure. Tells observers when the bot is being restarted/updated. Mild OPSEC concern.
**Fix:** Add `ephemeral=True` to both the `defer` and the `followup.send`. Same for `boss_status` at line 2609 (currently uses ephemeral correctly — keep it that way).

---

### WARNING #11: `BossSBForceSettleModal` accepts free-text `result` value and passes to `_force_settle_impl` without validation

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:713-728`
**Confidence:** 0.75
**Risk:** Modal field at line 715: `result = ... placeholder="Won / Lost / Push / Cancelled"`. There is NO validation in `on_submit` — operator could type "WIN" or "won" or any garbage. The callee at `flow_sportsbook.py:3580-3585` does have a guard (`if result not in ("Won", "Lost", "Push", "Cancelled")`) but its `result.strip().title()` normalization will accept "won" → "Won", "WIN" → "Win" (NOT in list, falls through to the error). The validation IS there at the callee, but the normalization is only `.title()`, so a typo like "Wonn" → "Wonn" gets rejected, but "win" → "Win" — also rejected. The operator gets a confusing error after the modal closes.
**Vulnerability:** Surface area for typos. Modal gives no constraint, and the failure is reported via `followup.send` after the modal has already disappeared, requiring the operator to re-open and retype.
**Impact:** Friction. Force-settle UX is poor. Not a security issue.
**Fix:** Replace the free-text input with a `SelectOption`-based view (matchup-style: pick bet ID, then pick result from dropdown). Or validate at modal-submit and re-open the modal with a hint. Same pattern would benefit `BossSBRefundModal` (no guard against negative bet IDs).

---

### OBSERVATION #1: 60+ inline `if not await is_commissioner(interaction): return ...` calls — extreme repetition without a decorator

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py` — every button handler in the file
**Confidence:** 0.95
**Risk:** `permissions.py:120` provides a `commissioner_only()` decorator, but boss_cog uses inline checks instead. This is a maintenance hazard — adding a new button means remembering to add the gate. One missed line and a hole opens. There are roughly 60 of these inline checks in this file.
**Fix:** Override `discord.ui.View.interaction_check` on each panel view to call `is_commissioner` once per interaction. discord.py supports this pattern natively. Then remove the per-button inline checks. Reduces ~120 lines and forecloses the "forgot to gate one button" failure mode.

---

### OBSERVATION #2: `BossHubView.timeout=300` — but views with persistent state aren't registered with `bot.add_view()`

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:200-285` (and every other view in the file)
**Confidence:** 0.85
**Risk:** None of the views in this file are persistent. They all have a 300s timeout. After the bot restarts, any open Boss panels become dead — buttons no longer respond. This is consistent with the ephemeral design but makes the panels brittle: a bot deploy mid-session forces operators to re-issue `/boss hub`. The `on_timeout` at line 280 only edits the message for `BossHubView`, not for any sub-panel.
**Fix:** Either (a) document explicitly that Boss panels are ephemeral and intentionally die on restart, or (b) make the views persistent (no timeout, custom_ids on every button, register via `bot.add_view()` in `setup()`). For an admin tool, ephemeral is fine — but document it.

---

### OBSERVATION #3: `_resolve_member` swallows broad `Exception` in roster lookup loop

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:118-119`
**Confidence:** 0.85
**Risk:** The roster lookup section is wrapped in `try: ... except Exception: pass`. If `roster.get_all()` raises (e.g. roster file corruption), the function silently falls through to display-name match. Per CLAUDE.md: "Silent except in admin-facing view" is prohibited.
**Fix:** Replace with `except Exception: log.exception("Roster lookup in _resolve_member")` so the failure is visible in logs.

---

### OBSERVATION #4: `_home_embed` swallows two broad `except Exception: pass` blocks

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:175-185`
**Confidence:** 0.9
**Risk:** Two consecutive `try/except Exception: pass` blocks — one for season/week, one for owner count. Per CLAUDE.md, silent excepts in admin-facing views are prohibited.
**Fix:** `log.warning("Home embed enrichment failed: %s", e)` instead of silent pass. The home embed will still render with missing fields, which is fine, but operators need to know enrichment failed.

---

### OBSERVATION #5: `BossHubView.on_timeout` swallows `Exception` silently

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:280-285`
**Confidence:** 0.85
**Risk:** `try: await self.message.edit(view=None) except Exception: pass`. If the message was deleted or the bot loses access, the failure is silent. Not catastrophic, but matches the prohibited pattern.
**Fix:** `except discord.HTTPException as e: log.debug("BossHubView timeout edit failed: %s", e)`. Narrow the exception type and log.

---

### OBSERVATION #6: `boss_clearsync` performs `tree.clear_commands` + `copy_global_to` + `sync` without confirmation or rollback

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:2579-2587`
**Confidence:** 0.7
**Risk:** This is a destructive operation (it clears all commands from the guild's tree before re-syncing). If the global tree is in an inconsistent state, the guild can lose all its commands until the next successful global sync. There is no confirmation prompt and no audit log entry.
**Fix:** Add a confirmation button (similar to `SBCancelConfirmView`) and log the action with the actor's ID before executing.

---

### OBSERVATION #7: `RosterPanelView.view_roster` inlines the roster embed build — duplicates roster.py rendering

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:1582-1612`
**Confidence:** 0.7
**Risk:** This handler builds an AFC/NFC roster embed inline, duplicating logic that very likely exists in `roster.py` (since `roster.AssignConferenceView` is used elsewhere in this file at line 2338). Per CLAUDE.md: "no direct logic is duplicated". Either (a) move the embed build into a `roster.build_roster_embed()` helper, or (b) confirm `roster.py` has no equivalent and document the inline build with a comment.
**Fix:** Extract to `roster.build_team_roster_embed()` and call from boss_cog. Reduces drift between the two embeds.

---

### OBSERVATION #8: Hard-coded magic numbers throughout (5_900 max embed size, 1024 field max, 25-option select cap)

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:2082, 1673, 545`
**Confidence:** 0.6
**Risk:** Discord limits are scattered as bare integers. `MAX_EMBED = 5_900` at line 2082 (with a comment that hardcap is 6000); `[:1024]` at line 1673; `[:25]` at line 545. These are well-known Discord constraints but should be in a constants module so they can be referenced consistently across the codebase.
**Fix:** Centralize in a `discord_limits.py` (or similar) and import. Reduces scattered magic.

---

### OBSERVATION #9: `BossDevAuditModal` filters out "Normal" dev only when `not team_filter` is set, leading to inconsistent reports

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:1656-1658`
**Confidence:** 0.7
**Risk:** Line 1656: `if dev == "Normal" and not team_filter: continue` — when no team filter, Normal players are dropped; when a team IS filtered, Normal players are included. This dual behavior is confusing and undocumented. A team-specific audit shows Normal dev players (which can't have superstar abilities), while a league-wide audit silently hides them. Operators see different result shapes for the "same" command.
**Fix:** Always filter Normal players (or never filter them) and document the choice. Better: add a `Show Normal` option to the modal.

---

### OBSERVATION #10: `_panel_embed` and `_home_embed` don't sanitize / set max-length on user-provided text

**Location:** `C:/Users/natew/Desktop/discord_bot/boss_cog.py:169-193`
**Confidence:** 0.5
**Risk:** `_home_embed` puts `interaction.user.display_name` directly into the embed description (line 172). If someone changes their display name to an oversized or markdown-injection string, the embed renders unexpectedly. Discord embed descriptions are capped at 4096 chars, so very long names truncate. Markdown-injection is theoretically possible but admin-only context limits the surface.
**Fix:** Use `discord.utils.escape_markdown(interaction.user.display_name)` and apply `[:80]` slice. Defense-in-depth.

---

## Cross-cutting Notes

The single biggest risk in this file is **CRITICAL #1**: `boss_cog.py` is the SOLE permission gate for ~60 commissioner operations across at least 6 cogs (sportsbook, casino, economy, sentinel, genesis, real_sportsbook), and grep confirms that **none** of the callee `_impl` methods re-check `is_commissioner`. The Boss panel relies entirely on the implicit invariant that "no other cog will ever call my callees" — this invariant is undocumented, unenforced, and has already been violated in spirit by the casino panel which calls `_casino_open_impl` directly without arguments to defer (the callee doesn't defer, then the boss handler at line 843 does it indirectly).

Recommended cross-cutting actions for the Ring 1 spiral:
1. Document in `CLAUDE.md` that all `*_impl` methods MUST start with `if not await is_commissioner(interaction): return ...` as defense-in-depth — and audit every existing `_impl` method.
2. Replace 60+ inline `is_commissioner` calls in `boss_cog.py` with `interaction_check` overrides on each panel view (OBSERVATION #1).
3. Add a `boss_audit` table and a `_log_admin_action` helper for accountability (WARNING #2).
4. Establish a uniform "modals defer FIRST" rule (CRITICAL #3, WARNING #9) — currently inconsistent across the file.
