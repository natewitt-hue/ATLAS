# HANDOFF — Sentinel, Genesis & Admin Layer Fixes

**GAP Review Session C** — sentinel_cog.py, genesis_cog.py, awards_cog.py, economy_cog.py, polymarket_cog.py, commish_cog.py
**Reviewed by:** Claude (Opus 4.6)
**Date:** 2026-03-20
**Target agent:** CLAUDEFROG

---

## Priority Legend

| Tag | Meaning |
|-----|---------|
| **P0-CRITICAL** | Commands broken / silently wrong results right now |
| **P1-BUG** | Functional bug, triggers under normal use |
| **P2-RISK** | Race condition, data-loss vector, or abuse vector |
| **P3-IMPROVE** | Code quality / hardening, no user-visible failure today |

---

## 1. commish_cog.py — Delegation Wiring (P0-CRITICAL)

### 1.1 Eight flat commands look up the WRONG cog name

Every `/commish <flat>` command that delegates to sentinel_cog or genesis_cog uses the **Hub cog name** instead of the **actual cog that owns the `_impl` method**. They all silently return "not loaded" even when the cog IS loaded.

| Line | Command | Looks Up | Should Look Up | Method |
|------|---------|----------|----------------|--------|
| 507 | `tradelist` | `GenesisHubCog` | `TradeCenterCog` | `_tradelist_impl()` |
| 514 | `runlottery` | `GenesisHubCog` | `ParityCog` | `_runlottery_impl()` |
| 522 | `orphan` | `GenesisHubCog` | `ParityCog` | `_orphanfranchise_impl()` |
| 530 | `caseview` | `SentinelHubCog` | `ComplaintCog` | `caseview_impl()` |
| 537 | `caselist` | `SentinelHubCog` | `ComplaintCog` | `caselist_impl()` |
| 544 | `forcehistory` | `SentinelHubCog` | `ForceRequestCog` | `forcehistory_impl()` |
| 552 | `positionapprove` | `SentinelHubCog` | `PositionChangeCog` | `positionchangeapprove_impl()` |
| 561 | `positiondeny` | `SentinelHubCog` | `PositionChangeCog` | `positionchangedeny_impl()` |

**Fix:** In each line listed above, replace the `self._get("...")` argument with the correct cog class name from the table.

Example for line 507:
```python
# BEFORE
cog = self._get("GenesisHubCog")
# AFTER
cog = self._get("TradeCenterCog")
```

Repeat for all 8 commands. The error message strings ("Genesis not loaded." / "Sentinel not loaded.") can stay as-is — they're still accurate enough.

---

## 2. sentinel_cog.py

### 2.1 `RulingPanelView._acted` does not survive restarts (P1-BUG)

**File:** `sentinel_cog.py:539`

The `_acted = False` flag lives on the View instance. When the bot restarts, `RulingPanelView` is re-registered with `timeout=None` but `_acted` resets to `False`, allowing a complaint to be double-ruled.

**Fix:** Before processing a ruling, re-read the complaint dict from `_complaints` and check `c.get("ruling")`. If already ruled, return early. The JSON-persisted state is the source of truth, not the instance flag.

```python
# In guilty_button / not_guilty_button / dismissed_button callbacks:
c = _complaints.get(self.complaint_id)
if not c:
    return await interaction.response.send_message("Case not found.", ephemeral=True)
if c.get("ruling"):
    return await interaction.response.send_message("Already ruled on.", ephemeral=True)
# ... proceed with ruling
```

Keep `_acted` as a fast-path guard but add the persisted-state check as the authoritative guard.

### 2.2 No rate limiting on complaint filing (P2-RISK)

**File:** `sentinel_cog.py:287` (`ComplaintModal.on_submit`)

There is no cooldown on filing complaints. A user can spam the category select → modal flow to flood the system with complaint threads.

**Fix:** Add a per-user cooldown dict in `ComplaintCog.__init__` or apply `@app_commands.checks.cooldown` on the `/complaint` command (if it has one), or add a manual timestamp check in `ComplaintModal.on_submit`:

```python
# In ComplaintCog.__init__:
self._last_filed: dict[int, float] = {}

# In ComplaintModal.on_submit (or the callback that creates the modal):
now = time.time()
last = cog._last_filed.get(interaction.user.id, 0)
if now - last < 300:  # 5-minute cooldown
    return await interaction.followup.send("⏱ Please wait before filing another complaint.", ephemeral=True)
cog._last_filed[interaction.user.id] = now
```

### 2.3 `_fetch_image_bytes()` uses blocking `requests` (P1-BUG)

**File:** `sentinel_cog.py:2359-2365`

`_fetch_image_bytes()` calls `requests.get()` synchronously. This blocks the entire event loop for up to 15 seconds per image. Note: the force-request flow at line 747 already uses `httpx.AsyncClient` correctly — only this legacy helper is blocking.

**Fix:** Convert to async using `httpx`, matching the pattern already used at line 747:

```python
async def _fetch_image_bytes(url: str) -> bytes:
    """Download an image from a Discord CDN URL."""
    if not _validate_image_url(url):
        raise ValueError(f"Blocked image URL (must be Discord CDN): {url}")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content
```

Update all callers to `await _fetch_image_bytes(url)`. Check if `requests` can be removed from imports if no other usage exists in this file.

### 2.4 `_request_counter` resets on restart — non-unique IDs (P2-RISK)

**File:** `sentinel_cog.py:1089`

`ForceRequestCog._request_counter` starts at 0 on every bot restart. IDs are `MMDD-001`, `MMDD-002`, etc. If the bot restarts mid-day, IDs collide with earlier requests from the same day.

**Fix:** Either:
- (a) Persist counter to a JSON file (like parity_state.json pattern), OR
- (b) Include a short random suffix: `f"{ts}-{uuid.uuid4().hex[:6]}"`, OR
- (c) Use a monotonic counter backed by SQLite `INSERT ... RETURNING id`

Option (b) is simplest and sufficient for force-request IDs.

### 2.5 Blowout Check and Stat Check are manual-input modals (P3-IMPROVE)

**File:** `sentinel_cog.py:2549-2628`

`BlowoutModal` and `StatCheckModal` require users to manually type in team names, scores, and stat values. They do NOT query `data_manager` for live game data or use `dm.flag_stat_padding()`.

This is by design (documented as "was /blowoutcheck" / "was /statcheck" in the docstring), but note that:
- `data_manager` has a `flag_stat_padding()` function that could automate stat checks
- Live scores could be pulled from `dm.games` DataFrame to auto-populate blowout checks

**No immediate fix needed** — document for future integration sprint.

### 2.6 sentinel_cog imports `_state` from genesis_cog (P3-IMPROVE)

**File:** `sentinel_cog.py:1331`

```python
from genesis_cog import _state, _save_state, _STATE_PATH
```

This creates tight coupling between sentinel and genesis. The code has a TODO comment (line 1327-1329) acknowledging this and suggesting extraction to `parity_state.py`. The fallback path (lines 1334-1350) handles the case where genesis_cog isn't loaded, so this isn't a crash risk.

**No immediate fix needed** — note for future refactor.

### 2.7 Variable shadowing in force-request analysis (P1-BUG)

**File:** `sentinel_cog.py:759-763`

```python
result = await atlas_ai.generate(...)  # line 759 — returns AI response object
raw = result.text                       # line 760

# Defaults
result = {                              # line 763 — SHADOWS the AI response!
    "ruling": RULING_INCONCLUSIVE,
    ...
}
```

The variable `result` is reassigned from the AI response object to the parsed dict. This works because `raw = result.text` is extracted before the reassignment, but it's fragile — any future code that references `result` between lines 760-763 expecting the AI response will get the dict instead.

**Fix:** Rename the parsed dict to `parsed` or `analysis`:

```python
result = await atlas_ai.generate(...)
raw = result.text

analysis = {
    "ruling": RULING_INCONCLUSIVE,
    ...
}
```

Update all downstream references from `result["ruling"]` to `analysis["ruling"]`, etc.

---

## 3. genesis_cog.py

### 3.1 `_state["orphan_teams"]` is a `set` — JSON round-trip risk (P2-RISK)

**File:** `genesis_cog.py:1725-1755`

`_state["orphan_teams"]` is initialized as a Python `set()`. The `_save_state()` function at line 1749 converts it to a `list()` for JSON serialization, and `_load_state()` at line 1740 converts back with `set()`. This works correctly today.

**Risk:** If any code path writes to `_state["orphan_teams"]` using list methods (`.append()`) instead of set methods (`.add()`), it will fail silently or raise AttributeError. The type is not enforced.

**Fix (optional hardening):** Add a type assertion in `_save_state()`:
```python
assert isinstance(_state["orphan_teams"], set), "orphan_teams must be a set"
```

### 3.2 No devTrait mapping or ability budget enforcement (P3-IMPROVE)

**File:** `genesis_cog.py` (entire file)

Per the CLAUDE.md docstring (line 21), devTrait mapping and ability budget checks have been moved to the `/boss Roster` panel (likely in a different cog or flow). This file does NOT validate:
- `devTrait` values (0=Normal, 1=Star, 2=Superstar, 3=X-Factor)
- Ability budgets (Star=1B, Superstar=1A+1B, XFactor=1S+1A+1B)
- Dual-attribute checks (OR logic, not AND)

**No fix needed here** — verify these checks exist in the Roster panel's cog. If they don't exist anywhere, that's a separate P1 finding.

### 3.3 Trade evaluation — RED band auto-decline has no override (P3-IMPROVE)

When `te.evaluate_trade()` returns a RED band, the trade is automatically declined with no commissioner override path. This is likely intentional but worth documenting — commissioners who want to force-approve a RED trade must do so outside the bot.

**No fix needed** — document for commissioner awareness.

---

## 4. awards_cog.py

### 4.1 No tie-breaking logic in poll results (P2-RISK)

**File:** `awards_cog.py` (entire 110-line file)

When tallying votes, the code counts occurrences per nominee but has no tie-breaking mechanism. If two nominees have equal votes, the display order depends on dict iteration order (insertion order in Python 3.7+), which may not be deterministic from the user's perspective.

**Fix:** Add tie-breaking — either:
- (a) Display "TIE" explicitly in the results embed, OR
- (b) Sort ties by alphabetical order of nominee name, OR
- (c) Add a "Commissioner breaks ties" note

### 4.2 No permission check on `_impl` methods (P3-IMPROVE)

**File:** `awards_cog.py`

`_createpoll_impl()` and `_closepoll_impl()` have no `is_commissioner()` guard. They rely entirely on `commish_cog.py` routing (which uses `default_permissions=administrator`). If another cog or code path calls these `_impl` methods directly, the permission check is bypassed.

**Fix (defensive):** Add `is_commissioner()` check at the top of each `_impl` method:
```python
async def _createpoll_impl(self, interaction, title, nominees):
    if not is_commissioner(interaction):
        return await interaction.followup.send("⛔ Commissioner only.", ephemeral=True)
    ...
```

---

## 5. economy_cog.py

### 5.1 `_eco_give_role_impl` — no individual ledger entries (P1-BUG)

**File:** `economy_cog.py:485-486`

When granting currency to all members of a role, the loop calls `admin_give()` for each member, which does create individual balance changes. However, the audit log entry (line 488-492) only posts a single summary message like "gave 500 to 12 members with @TSL Owner". There are no per-member ledger entries visible in the audit channel.

**Fix:** Either:
- (a) Post individual audit entries per member (may be noisy for large roles), OR
- (b) Post a summary embed listing each member and their new balance, OR
- (c) Accept current behavior but document it — `admin_give()` already writes to the `economy_log` table per-user, so the DB audit trail exists. The missing piece is only the Discord-visible audit.

Recommended: Option (c) — the DB audit trail in `economy_log` is sufficient. Add a comment documenting this:
```python
# Note: individual entries are written to economy_log table by admin_give().
# Only a summary is posted to the audit channel to avoid spam.
```

### 5.2 Stipend loop — no duplicate-payment guard across restarts (P2-RISK)

**File:** `economy_cog.py` (stipend `@tasks.loop(hours=1)`)

The hourly stipend task runs `@tasks.loop(hours=1)`. If the bot restarts mid-hour, the loop restarts immediately and may issue a duplicate stipend for the same hour. There's no "last paid at" timestamp check.

**Fix:** Store `last_stipend_at` timestamp in the database or a JSON file. On each loop iteration, skip if less than 55 minutes have elapsed since last payment.

---

## 6. polymarket_cog.py

### 6.1 Double-resolution guard is correct (VERIFIED OK)

**File:** `polymarket_cog.py:3267-3275`

The `_resolve()` method uses `BEGIN IMMEDIATE` + a re-check of `resolved_by` inside the transaction. This correctly prevents TOCTOU double-resolution. No fix needed.

### 6.2 Auto-resolution threshold may be too aggressive (P3-IMPROVE)

**File:** `polymarket_cog.py:663-682`

`detect_result()` uses a 0.95 threshold (95% probability on Polymarket) to auto-resolve markets. While reasonable, a market could briefly spike to 0.95 on a rumor and then drop back. The auto-resolution is irreversible (except VOID).

**Recommendation:** Consider adding a "confirmation window" — require the price to stay above 0.95 for N consecutive checks before auto-resolving. Or add a manual approval step for auto-detected resolutions.

### 6.3 Price alert rate limiting is per-user, not per-market (P3-IMPROVE)

**File:** `polymarket_cog.py:2097-2106`

Rate limiting is 3 alerts per hour per user. A user could set 3 alerts on the same market and receive redundant notifications. Consider per-user-per-market limiting.

**No fix required** — minor UX issue.

---

## 7. Cross-Cutting Issues

### 7.1 JSON file persistence — no locking across cogs (P2-RISK)

Multiple cogs persist state to JSON files (`trade_state.json`, `parity_state.json`, `polls_state.json`, complaint state). All use the atomic `tmp + os.replace` pattern, which is good. However:
- If two cog methods write to the same JSON file concurrently (e.g., sentinel imports `_state` from genesis), the last writer wins and earlier writes are lost.
- The `_state` dict is a global mutable shared between genesis_cog and sentinel_cog via import.

**Risk is low** because discord.py runs on a single event loop thread, so true concurrent writes to the same file are unlikely. But `await` points between read-modify-write could theoretically interleave.

**Fix (if ever needed):** Use `asyncio.Lock()` around state modifications, or move to SQLite for state persistence.

### 7.2 No `view=None` passed to `followup.send()` (VERIFIED OK)

Checked all files — no instances of `followup.send(view=None)`. The Discord API constraint is respected.

### 7.3 Select menus respect 25-option cap (VERIFIED OK)

- `awards_cog.py:43` — `options[:25]` slice
- `sentinel_cog.py` — Category select has a fixed small set (< 25)
- `genesis_cog.py` — Team selects are conference-filtered (16 per conference, under cap)

---

## Summary — Fix Priority Queue

| # | Priority | File | Issue | Effort |
|---|----------|------|-------|--------|
| 1 | **P0** | `commish_cog.py` | 8 wrong cog name lookups (§1.1) | 10 min |
| 2 | **P1** | `sentinel_cog.py` | `_acted` flag doesn't survive restart (§2.1) | 15 min |
| 3 | **P1** | `sentinel_cog.py` | `_fetch_image_bytes` blocks event loop (§2.3) | 10 min |
| 4 | **P1** | `sentinel_cog.py` | Variable shadowing in force-request analysis (§2.7) | 5 min |
| 5 | **P2** | `sentinel_cog.py` | No rate limiting on complaint filing (§2.2) | 10 min |
| 6 | **P2** | `sentinel_cog.py` | `_request_counter` resets on restart (§2.4) | 10 min |
| 7 | **P2** | `economy_cog.py` | Stipend loop duplicate-payment risk (§5.2) | 15 min |
| 8 | **P2** | `awards_cog.py` | No tie-breaking in poll results (§4.1) | 10 min |
| 9 | **P2** | `genesis_cog.py` | `orphan_teams` set type not enforced (§3.1) | 5 min |
| 10 | **P1** | `economy_cog.py` | Role payout audit channel gap (§5.1) | 5 min |
| 11 | **P3** | Various | Remaining items (§2.5, §2.6, §3.2, §3.3, §4.2, §6.2, §6.3) | — |

**Start with #1 (commish_cog wiring)** — it's the highest impact, lowest effort fix. All 8 admin commands are currently broken.
