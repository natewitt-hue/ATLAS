# Adversarial Review: ability_engine.py

**Verdict:** needs-attention
**Ring:** orphan (LIVE — imported by boss_cog, genesis_cog, trade_engine)
**Reviewed:** 2026-04-09
**LOC:** 1347
**Reviewer:** Claude (delegated subagent)
**Total findings:** 16 (3 critical, 7 warnings, 6 observations)

## Summary

The ability engine is the rules-layer for TSL's most consequential roster governance systems — Lock & Key ability auditing, dev budget enforcement, and position change validation — and it has multiple material correctness bugs: the dual-attribute OR gate silently treats "3+ regular thresholds" as AND (dropping the OR rule entirely for triple-stat abilities), the LRU cache is keyed on a field the audit doesn't refresh per-season (stale archetypes for player trades/re-signs), and `check_position_change` declares "⚠️ SS abilities reset" as a reason but then immediately prunes ⚠️ warnings from `hard_blocks` via `startswith("⚠️")` on a raw string that may fail unicode normalization. Position change validation also has a **CB→FB bypass** (only CB→LB is banned, nothing prevents CB→FB→MIKE chain). Non-blocking but load-bearing: reassignment is non-deterministic (`random.choice`) without seed control — audit runs twice in a row produce different roster states, which breaks reproducibility for the commissioner's trade approval flow.

## Findings

### CRITICAL #1: Triple-threshold abilities silently downgrade from OR to AND — violates CLAUDE.md dual-attr rule

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:572-611`, also `957-1004`
**Confidence:** 0.92
**Risk:** The `is_dual_attr` flag is set only when `len(regular_keys) == 2`. Abilities with 3+ stat thresholds (e.g., `Omaha` with `throwAccMid/throwAccShort/awareRating`, `Route Technician` with `routeRunShort/routeRunMed/agilityRating`, `Conductor`, `Pro Reads`, `Universal Coverage`, `Enforcer Supreme`, `Edge Threat Elite`) are checked with **AND** logic in both `check_physics_floor()` and `get_qualified_abilities()`. CLAUDE.md's MaddenStats gotcha is explicit: "Dual-attribute checks: Use OR logic, not AND". There is no carve-out for triple-stat abilities. By making the rule contingent on exactly 2 regular keys, the engine flags players as illegal when any single stat falls short even though the dual-attribute policy explicitly permits OR-matching.
**Vulnerability:** Line 576: `is_dual_attr = len(regular_keys) == 2` — the threshold for OR logic is a magic cardinality check, not a property of the ability. Abilities like `Omaha` with three stat constraints fall out of the OR branch and into the AND branch at lines 604-610, where a single miss (e.g. `awareRating=91` vs `92` required) causes `is_ability_earned()` to return False, which in turn causes `audit_roster()` to flag the ability as illegal and `reassign_roster()` to pick a random replacement. A player with `throwAccMidRating=98, throwAccShortRating=98, awareRating=91` gets stripped of Omaha and given something random.
**Impact:** False-positive Lock & Key violations on multi-stat elite abilities, automatic reassignment strips legitimate abilities from top players every reassignment run, and the replacement is non-deterministic (`random.choice` in `pick_replacement`), so the commissioner cannot reproduce or defend the decision. This is the highest-leverage trust path in the bot for roster discipline — silent false positives are worse than visible errors.
**Fix:** Remove the cardinality-gated OR logic. Either (a) switch to per-ability explicit OR/AND marking in the ABILITY_TABLE (`"logic": "or"` or `"logic": "and"`), or (b) apply OR logic to any ability with 2+ regular thresholds. The safest path: add an explicit `"threshold_logic": "or"` key per ability and default to AND for single-stat abilities. Also clamp `is_dual_attr` to `>= 2` at minimum so 3-stat abilities don't silently degrade.

---

### CRITICAL #2: CB → FB bypass in position change ban (permanent CB→LB ban is not transitive)

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:1234-1261`
**Confidence:** 0.80
**Risk:** `_CB_TO_LB_BAN` enumerates exactly `{("CB","MIKE"),("CB","WILL"),("CB","SAM"),("CB","LOLB"),("CB","ROLB"),("CB","MLB")}` — six literal pairs. Nothing prevents a commissioner (or a future owner self-service command) from approving **CB → FB → MIKE** as two separate changes, or **CB → SS → WILL** (SS is a valid linebacker source per the rules). The ban is a per-edge check on a single change, not a per-player history constraint. Also: the ban set contains `LOLB`, `ROLB`, `MLB` which are not real Madden 26 position codes in this codebase — `POSITION_CHANGE_RULES` uses `MIKE`, `SAM`, `WILL`. LOLB/ROLB/MLB entries in the ban are dead code that masks the actual ban surface (e.g., if Madden uses "SAM" as the rushing OLB and the commissioner tries CB→SAM, the ban catches it, but LOLB/ROLB checks never fire).
**Vulnerability:** Line 1234: hard-coded set with outdated position codes. Line 1259: `(from_pos.upper(), to_pos.upper())` only checks the single hop from the current request. Trade approval calling `check_position_change(p, "CB", "FB")` returns legal (no rule exists → line 1279 path), then a subsequent call for `check_position_change(p, "FB", "MIKE")` also returns legal (no rule → commissioner discretion).
**Impact:** A player originally drafted as CB can end up at MIKE via a two-hop conversion, defeating the entire point of the permanent ban. This is exactly the "CB → LB banned" rule the engine is supposed to enforce, silently bypassed. Since sentinel_cog Ring 1 already flagged position change approve/deny commands had no commissioner check, this file is the last line of defense and it has a hole.
**Fix:** Track each player's position history in a DB or file ("originalPos" / "positionHistory"), and check `_CB_TO_LB_BAN` against `(player.originalPos, to_pos)` OR any pos in the history chain. Also align the ban set to the actual position codes used in POSITION_CHANGE_RULES (remove LOLB/ROLB/MLB or confirm they're emitted by the API). Additionally add a generic check that any `to_pos` in `{MIKE, SAM, WILL}` with an origin pos in `{CB}` is banned regardless of intermediate hops.

---

### CRITICAL #3: `check_position_change` hard_block filter is a string-prefix match that can fail on normalized unicode

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:1263-1312`
**Confidence:** 0.75
**Risk:** Line 1305: `hard_blocks = [r for r in reasons if not r.startswith("⚠️")]`. This relies on the exact byte sequence "⚠️" (U+26A0 U+FE0F) being present at the start of every warning. Line 1266 appends `f"⚠️ {dev} abilities will be RESET..."` literally — that works, but if a future contributor adds a warning via a helper, emits it through a translation layer, or the string goes through any Unicode normalization (NFC ↔ NFD), the byte comparison breaks. More critically: **line 1279** returns `True, reasons` for unknown position transitions with the reason `"No specific threshold rule for {from_pos} → {to_pos}. Commissioner discretion applies."` — this reason is NOT prefixed with ⚠️ or ℹ️, so it will count as a hard_block at line 1305 **except** the function has already returned at line 1279 before reaching 1305. But now consider: the `startswith("⚠️")` filter is only applied at 1305 to the final pre-return hard_block check (lines 1309-1312), which is the **requires_commissioner** gate. If any future threshold reason is added that happens to have a leading emoji (e.g., "🔒 locked below height floor"), it would be filtered out and treated as non-blocking.
**Vulnerability:** The hard_block detection uses a fragile string prefix on an emoji rather than a structured reason type. Line 1305 depends on every non-warning reason NOT starting with "⚠️". Line 1306 immediately bails on hard_blocks. But if line 1309 is ever reached with a `requires_commissioner=True` rule and the only reason is the ⚠️ SS reset, the function appends the ℹ️ commissioner note and returns True — which is correct — BUT if a new warning type is added later without updating line 1305, it will mask a real blocker.
**Impact:** Brittle rule-gate. Current behavior may be correct for exactly the rules present today, but the filter pattern is a landmine for anyone adding a new warning. Combined with #2, this makes position change one of the lowest-confidence gates in the file.
**Fix:** Change `reasons` from a `list[str]` to a `list[tuple[Severity, str]]` where Severity is an enum {ERROR, WARNING, INFO}. The hard_block filter becomes `[r for sev, r in reasons if sev is Severity.ERROR]` — no emoji parsing. Callers build embed strings from the tuple. Backward compat: keep `position_change_embed_lines()` as the string-formatting adapter.

---

### WARNING #1: `_cached_archetype` LRU cache key uses `rosterId` + `season` — stale when roster changes mid-season

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:455-461`, `547-555`
**Confidence:** 0.85
**Risk:** The cache key is `(roster_id, season, pos, arch_tuple)`. During a single season, if a player is traded (roster_id is stable — it's the TSL persistent ID, not a team roster slot), re-signed, or has their dev trait / arch ratings change from a progression event, the cached archetype will be returned from a prior call even though the actual archetype has changed. The comment at line 456 says "archetype ratings don't change within a season" — this is false for TSL. Progression events (Genesis) update ratings. Position changes update pos. In-season trades can shift how `pos` is reported if the receiving team uses a different depth chart designation.
**Vulnerability:** `arch_tuple` is included in the key so changes to the underlying ratings DO bust the cache. BUT `pos` is also part of the key, so a position swap busts the cache. The actual staleness comes from: this function is called with a `player` dict from the current `_players_cache`, which reflects the latest API sync. If a snapshot of the dict from one cog (an older `load_all()` result) is passed, the cache returns the OLD archetype even after a newer sync. The cache persists across `load_all()` calls (module-level LRU), with no invalidation hook.
**Impact:** Incorrect archetype → wrong Lock & Key verdicts after any mid-season progression event or data sync. Subtle and hard to detect because most runs happen shortly after `load_all()` and use fresh data; the cache only bites on replay/retry/parallel audits.
**Fix:** Add a cache-clear hook: `ability_engine.clear_archetype_cache()` and call it from `data_manager.load_all()` after a successful sync. Alternatively, reduce cache to per-audit scope by adding a `cache_buster: int` argument (e.g., a monotonic counter incremented by load_all()) to `_cached_archetype`. Or drop the cache — the function is pure and cheap; 3000 calls is nothing.

---

### WARNING #2: `reassign_roster` is non-deterministic via `random.choice` with no seed control

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:1012-1025`, `1028-1140`
**Confidence:** 0.90
**Risk:** `pick_replacement()` uses `random.choice(candidates[:3])` — unseeded, process-global random state. Running reassignment twice on identical input produces different final loadouts. Trade approval in genesis_cog calls this engine (per CLAUDE.md: "Performance: this is called from genesis_cog trade approval flow"). If the commissioner previews a reassignment, then approves a trade, then re-runs the reassignment, the player's abilities are different — the preview no longer matches reality.
**Vulnerability:** Line 1025: `return random.choice(candidates[:3])`. The top-3 slice is also sensitive to ties in `fit_score`, which have undefined sort order from Python's stable sort (lines 1024 / 1095) — if two candidates tie at rank 3 and 4, whichever was encountered first in `ABILITY_TABLE` dict iteration wins. Dict iteration in Python 3.7+ is insertion order, so it's deterministic there — but the `random.choice` between top-3 makes the whole pipeline stochastic.
**Impact:** Reassignment is unreproducible. Commissioner cannot replay an audit. Audit logs cannot be regenerated from the same input. Two parallel ATLAS sessions ("Sicko Mode" per CLAUDE.md) running reassignment on overlapping roster data get different answers.
**Fix:** Accept an optional `rng: random.Random` parameter (default `random.Random(seed)` where seed derives from `(rosterId, season, week)`). Or deterministically pick `candidates[0]` and break ties by ability name alphabetically. Commissioner who wants randomness can re-seed explicitly.

---

### WARNING #3: Unknown abilities silently treated as C-tier, suppressing budget violations

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:684-699`
**Confidence:** 0.85
**Risk:** Line 686-689: if an ability isn't in `ABILITY_TABLE`, `check_budget()` logs a warning then sets `tier = "C"`. Since C tier has budget 99, the unknown ability is effectively free. If Madden 26 introduces a new XFactor ability (Madden DLC/updates do this), or if the API renames an existing ability (spelling change, case change, trailing space), that ability will be in a player's ability1-6 slot, will NOT be in the table, will be logged as "unknown", and will NOT count against the dev budget.
**Vulnerability:** Graceful degradation chosen over fail-closed. For a governance system whose entire purpose is to enforce budgets, silently treating unknown abilities as unlimited is the wrong default.
**Impact:** A player with a renamed or new elite ability bypasses budget enforcement. Commissioner sees the warning log only if they're watching logs at audit time. The roster appears "clean" in the audit output.
**Fix:** Change the default for unknown abilities to fail-closed (return budget violation with detail: "unknown ability <name> cannot be audited"). Alternatively, count unknown abilities as B-tier (the most common tier) so at least they occupy a budget slot. Also emit a DM / admin-channel alert for any unknown ability encountered — this is a signal that the table needs updating.

---

### WARNING #4: `_normalize_dev` ignores string-typed dev values that aren't in DEV_BUDGET

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:446-452`
**Confidence:** 0.70
**Risk:** Line 449: `if dev in DEV_BUDGET: return dev`. Line 451 then falls back to `devTrait` int field. If the API returns `dev="superstar"` (lowercased), `"SS"`, `"X-Factor"` (variant spelling), or any other string form not exactly matching the four DEV_BUDGET keys, the function silently falls back to the int field. If the int field is ALSO missing or invalid, the function returns "Normal" (line 452 default via `_safe_int`), and the player is skipped from audit entirely (line 776-777). This is a silent drop — the player is NOT flagged, just excluded.
**Vulnerability:** String case-sensitive equality. `"Superstar X-Factor"` vs `"Superstar X‑Factor"` (different hyphen) fails silently. MaddenStats API has a history of underscore/case mismatches (per CLAUDE.md).
**Impact:** Superstar X-Factor players with malformed dev strings are treated as Normal and skipped from the audit. Their illegal abilities never get flagged. Lock & Key misses them entirely.
**Fix:** Case-insensitive lookup: `dev_normalized = (dev or "").strip().title()` before the membership check. Also add a normalization map for known variants: `{"superstar x factor": "Superstar X-Factor", "ssxf": "Superstar X-Factor", "xfactor": "Superstar X-Factor"}`. If normalization fails AND devTrait is missing, log.error (not silently return Normal) and skip the player explicitly so audits can surface "N skipped due to bad dev data".

---

### WARNING #5: `_safe_int` silently coerces floats, masking data corruption in weight/height fields

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:434-444`, `587`, `589`, `1287`, `1292`, `1299`
**Confidence:** 0.65
**Risk:** `_safe_int` converts `"92.5"` → 92, truncating. For height/weight fields this means a 5'11.5" player is treated as 5'11", and on position change eligibility that's enough to tip a borderline case from legal to illegal. Worse: NaN/None/invalid strings all collapse to 0, which then fails the minimum threshold check, so the player gets blocked with `"weight 0lbs < 215lbs floor"` — but the commissioner can't tell if this is a real case or a data bug.
**Vulnerability:** Lossy conversion with no signal distinguishing "real 0" from "missing data". Lines 1287, 1292: height and threshold checks treat 0 as legitimate-but-failing rather than "unknown data — abort audit".
**Impact:** Data bugs in the MaddenStats sync cause false negatives on position change eligibility. Players blocked with nonsensical reasons ("tackleRating=0 < 75").
**Fix:** Add a second helper `_required_int(val) -> int | None` that returns None for missing/invalid data and have callers raise or surface "cannot audit — missing stat X" when None is returned. Leave `_safe_int` as a convenience for truly optional fields.

---

### WARNING #6: `check_position_change` returns True with only informational reasons for unmapped transitions (silent permissive default)

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:1272-1279`
**Confidence:** 0.80
**Risk:** Lines 1274-1279: any `(from_pos, to_pos)` not in POSITION_CHANGE_RULES returns `(True, ["No specific threshold rule... Commissioner discretion applies."])`. This means the engine's default stance is **permissive**: in the absence of a rule, the transition is declared legal. Position changes are irreversible roster actions with competitive consequences. A commissioner reviewing an embed sees "✅ ELIGIBLE" as the headline and may miss the fine print explaining there's no rule.
**Vulnerability:** Line 1279: `return True, reasons`. The only in-bounds ban is the enumerated `_CB_TO_LB_BAN` set — every other unmapped pair is permissive. QB → K, LEDGE → CB, OL → QB, etc., all return legal.
**Impact:** Rule gaps manifest as silent approvals. The commissioner trusts the engine's ✅ output and approves exotic transitions that should have been flagged. Sentinel_cog ring 1 finding (no commissioner check on approve/deny) combined with this default-permissive means the permission boundary AND the rule check both have holes.
**Fix:** Change the default to `(False, ["No rule defined for <pair>; manual commissioner review required"])`. Force the commissioner to explicitly acknowledge unmapped transitions. Also change `position_change_embed_lines` headline to "⚠️ NO RULE" for that branch instead of "✅ ELIGIBLE".

---

### WARNING #7: `banned_from` key exists in POSITION_CHANGE_RULES but is never read by check_position_change

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:1175-1232`, `1241-1312`
**Confidence:** 0.95
**Risk:** Every rule dict (lines 1182, 1190, 1198, 1206, 1217, 1228) contains `"banned_from": [...]` or `"banned_from": []`. The rules for S→LB list `"banned_from": ["CB"]`. But `check_position_change()` never accesses `rule.get("banned_from")`. The CB→LB ban is enforced via the separate hardcoded `_CB_TO_LB_BAN` set at line 1234. The `banned_from` field is documentation, not logic.
**Vulnerability:** Dead field in a rule dict that implies intent. A future maintainer adding a new rule with `"banned_from": ["WR"]` expecting WR players to be blocked will find it silently has no effect.
**Impact:** Rule-authoring footgun. Rules look expressive but aren't.
**Fix:** Either wire `banned_from` into `check_position_change` (iterate and match against player's known position history / origin) OR delete the field from all rules and rely solely on `_CB_TO_LB_BAN`. Do not ship dead rule fields.

---

### OBSERVATION #1: `playerAbilities` param is accepted but intentionally unused — dead parameter surface

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:744-768`, `1028-1035`
**Confidence:** 0.95
**Risk:** Both `audit_roster` and `reassign_roster` accept `player_abilities: list[dict]` but the body never reads it. The docstring explains this is "for future use". Callers (boss_cog, genesis_cog, trade_engine) pass real data into this parameter, presumably believing it matters.
**Vulnerability:** API misleads callers. If any caller relies on the parameter being loaded/populated, they do a fetch they don't need. More importantly, a future maintainer could "activate" the parameter without realizing existing callers are passing stale/cached data.
**Impact:** Wasted work in callers; future footgun.
**Fix:** Remove the parameter from the public signature and drop it from callers. Or if it's reserved for future use, prefix with underscore and leave a `# TODO(future): wire playerAbilities for history queries` comment. At minimum, update the docstring to tell callers "pass None".

---

### OBSERVATION #2: `ABILITY_TABLE` has unreachable B-tier entries with thresholds requiring impossible stats

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:289-300`
**Confidence:** 0.60
**Risk:** `Edge Threat Elite` (S-tier) has three thresholds (speed + accel + _edge_pmv_or_fmv at 94). With the AND logic (Critical #1), a player needs `speedRating≥88 AND accelRating≥90 AND (pmv≥94 OR fmv≥94)`. For a player to pass, they need all three. Meanwhile, the A-tier `Edge Threat` has only 2 thresholds → OR logic → a player with fmv=88 and speed=70 passes. So a slower player can qualify for the A-tier but not the S-tier of the same ability family. That's intentional gate design… except after Critical #1 is fixed, every 3-stat ability's OR/AND semantics needs to be re-verified.
**Vulnerability:** Ability gate design is tangled with the cardinality-switched logic bug. Fixing #1 may unlock a bunch of abilities that were previously unreachable for borderline stat profiles.
**Impact:** Migration hazard when #1 is fixed: abilities currently "unreachable" under AND become reachable under OR, causing a wave of audit results to flip from "illegal" to "legal". Commissioner needs to be briefed.
**Fix:** After fixing #1, run a re-audit and compare delta. Flag any ability where >10% of players flipped as a potential threshold rebalancing issue.

---

### OBSERVATION #3: No tests, no invariant assertions, no sanity checks on the ABILITY_TABLE

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:70-428`
**Confidence:** 0.95
**Risk:** 358 lines of hand-authored ability data with no structural validation. A typo in a stat field name (`throwPowerRating` → `throwpowerRating`) silently causes `_safe_int(player.get(key, 0))` to return 0 for every player, meaning the ability is unreachable. The misspelling is not caught by anything.
**Vulnerability:** Manual data entry with no schema. Every row could have a typo and the only way to find out is running an audit and noticing everyone fails a specific ability.
**Impact:** Silent data-layer bugs.
**Fix:** Add a module-level validation at import time: iterate ABILITY_TABLE, assert every stat key appears in a known set (derived from MaddenStats API player schema), assert tier is one of {S,A,B,C}, assert every archetype string is one of the known archetypes, assert C-tier entries have empty thresholds, assert S/A/B entries have non-empty thresholds. Raise at import if any check fails — fail fast rather than silently wrong at runtime.

---

### OBSERVATION #4: "BUG#7" comments reference an issue tracker that doesn't exist in the file

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:20, 455-456, 472, 547, 572, 957`
**Confidence:** 0.99
**Risk:** The `BUG#6` and `BUG#7` comments reference prior audits/fix batches. Without a linked tracker, they're tombstones. Future maintainers don't know what they reference.
**Vulnerability:** Documentation smell.
**Impact:** Low — just noise.
**Fix:** Replace with proper docstring references like `# Prior fix: dual-attr OR logic (commit abc123)` or delete once the fix is stable.

---

### OBSERVATION #5: `_CB_TO_LB_BAN` contains position codes that may not match the API surface

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:1235-1238`
**Confidence:** 0.85
**Risk:** The ban set includes `LOLB`, `ROLB`, `MLB` alongside `MIKE`, `SAM`, `WILL`. `POSITION_CHANGE_RULES` (lines 1175-1232) and `ABILITY_TABLE` (lines 347-370) only reference `MIKE`, `SAM`, `WILL`. The data_manager players dict likely has only MIKE/SAM/WILL. LOLB/ROLB/MLB entries appear to be leftover from a legacy position naming scheme.
**Vulnerability:** Dead entries in the ban set are harmless but confusing.
**Impact:** Low — the ban still catches CB→MIKE/SAM/WILL. But the dead entries imply someone expected LOLB/ROLB/MLB to appear in the data, which suggests either this file was authored before the MIKE/SAM/WILL rename or there's a pocket of the codebase still using LOLB/ROLB/MLB that could emit those codes.
**Fix:** Confirm via grep that no code emits LOLB/ROLB/MLB, delete those entries, add a comment `# positions listed per Madden 26 TSL schema: MIKE/SAM/WILL only`.

---

### OBSERVATION #6: `reassign_roster` budget fallback linear-scan duplicates `get_qualified_abilities` work with no caching

**Location:** `C:/Users/natew/Desktop/discord_bot/ability_engine.py:1091-1104`
**Confidence:** 0.75
**Risk:** When the randomly-picked replacement fails budget (line 1089), the fallback calls `get_qualified_abilities(p)` a second time (line 1093). For a full-roster reassignment, this doubles the work for every budget miss. `get_qualified_abilities` walks 200+ abilities per player — if 20% of SS/XF players need a budget-safe re-pick, that's dozens of extra full scans per audit.
**Vulnerability:** Unnecessary re-computation. Not a correctness bug, just wasteful.
**Impact:** Slower reassignment runs. If genesis_cog trade approval calls this synchronously (per the CLAUDE.md concern about performance), this adds unnecessary latency to a user-facing flow.
**Fix:** Call `get_qualified_abilities` once outside the loop and reuse. Move the list inside the for-loop to a precomputed `qualified_all` variable at the top of Pass 2.

---

## Cross-cutting Notes

- **The dual-attribute OR rule is a cross-file invariant** (cited in CLAUDE.md as a MaddenStats gotcha). This file implements it with a cardinality gate; every other file that does stat threshold checking (if any) should be audited for the same pattern. Ring 1 / Oracle / Genesis cog stat checks should be reviewed.
- **Position change validation is the ONLY line of defense** after sentinel_cog's missing commissioner check (Ring 1 finding). This file's default-permissive behavior (Warning #6) plus CB→FB bypass (Critical #2) means **two layers of the defense-in-depth are both leaky**. This should be elevated to an immediate fix ticket, not a background cleanup.
- **LRU cache without invalidation hook** (Warning #1) is a pattern that may repeat in other "orphan" modules. Anywhere `@functools.lru_cache` is applied to a function that reads from `data_manager`-sourced dicts should be audited for the same staleness trap.
- **Determinism concerns** (Warning #2): "Sicko Mode" (parallel Claude sessions) per CLAUDE.md makes reproducibility load-bearing. Any module that uses `random` without seed control is a candidate for stale-audit bugs when two sessions run the same audit and diverge.
