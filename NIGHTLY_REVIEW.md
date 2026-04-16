# ATLAS Nightly Audit — 2026-04-16

**Focus:** Genesis, Sentinel & Compliance | **Recent commits (24h):** 1 (Wednesday Oracle audit only — no changes to focus files) | **Files deeply read:** 7 | **Total lines analyzed:** ~8,503

---

## CRITICAL — Fix Before Next Deploy

### [C-01] sentinel_cog.py falls back to an isolated `_state` dict if genesis_cog import fails — position changes bypass cornerstone locks

- **File:** `sentinel_cog.py` L1489–1520
- **Risk:** If `from genesis_cog import _state, _save_state, _STATE_PATH` raises ImportError at import time (load-order race, bad restart, genesis_cog syntax error), sentinel_cog silently boots with its **own empty `_state` dict**. Position changes are then appended to that orphaned dict, never reaching the shared `parity_state.json`. The `cornerstones` key in `_state` will be empty — so cornerstone lock checks at `_run_position_change()` L2103 always return `False`, allowing any player to change position regardless of cornerstone designation.
- **Evidence:**
```python
# sentinel_cog.py L1489-1520
try:
    from genesis_cog import _state, _save_state, _STATE_PATH
    _PARITY_STATE_AVAILABLE = True
except ImportError:
    # Standalone fallback: manage our own state dict
    _PARITY_STATE_AVAILABLE = False
    _STATE_PATH = os.path.join(os.path.dirname(__file__), "parity_state.json")
    _state: dict = {}          # <-- EMPTY: no cornerstones, no position_changes

    def _load_state_local():
        global _state
        if os.path.isfile(_STATE_PATH):
            try:
                with open(_STATE_PATH, "r") as f:
                    _state.update(json.load(f))
            ...
    _load_state_local()   # reads disk, but only once at startup — cornerstones populated
```
The `_load_state_local()` call at L1520 does read from disk on startup — so cornerstones are present initially. But if the JSON file is ever absent (fresh deploy, corrupted), cornerstones start empty. More critically: **position changes written to this fallback `_state` never reach the `_state` object that genesis_cog reads** — they are in two separate Python dicts in memory. New cornerstones designated by genesis_cog during a session are invisible to sentinel's fallback path.
- **Fix:** Extract `_state`, `_save_state`, and `_STATE_PATH` into a standalone `parity_state.py` module (the `TODO` at sentinel L1486 already calls this out). Both genesis_cog and sentinel_cog import from there — no coupling, no fallback split. Until then, add a log warning so the split state is at least visible:
```python
except ImportError:
    import logging as _log
    _log.getLogger("atlas.sentinel").error(
        "sentinel_cog: genesis_cog import failed — running with isolated parity state. "
        "Cornerstone locks may be stale. FIX: extract parity_state.py."
    )
    ...
```

---

## WARNINGS — Fix This Week

### [W-01] Counter-trade authorization fails open if `intelligence` module is unavailable

- **File:** `genesis_cog.py` L1724–1741
- **Impact:** Any Discord user can submit a counter-offer on any trade if the `intelligence` module cannot be imported. Counter spamming from uninvolved users pollutes the trade log channel and creates fraudulent trade proposals.
- **Evidence:**
```python
is_involved = (
    interaction.user.id == self.proposer_id or
    await is_commissioner(interaction)
)
if not is_involved:
    try:
        from intelligence import KNOWN_MEMBER_TEAMS
        user_team_nick = KNOWN_MEMBER_TEAMS.get(interaction.user.id, "")
        ...
        if user_team_nick.lower() not in (team_a_nick.lower(), team_b_nick.lower()):
            return await interaction.response.send_message("❌ Only involved owners...", ephemeral=True)
    except ImportError:
        pass  # Can't verify — allow it   <-- FAIL OPEN: any user can counter
```
- **Suggestion:** Fail closed on ImportError:
```python
    except ImportError:
        return await interaction.response.send_message(
            "❌ Authorization check unavailable. Contact a commissioner to counter.",
            ephemeral=True,
        )
```

### [W-02] `RulingPanelView._acted` set before `send_modal()` — button permanently dead if modal fails to open

- **File:** `sentinel_cog.py` L617–659
- **Impact:** If the Discord interaction deadline is hit between `_acted = True` and the modal appearing, `_acted` stays `True` and ALL verdict buttons are permanently dead for this complaint — no ruling can be issued without a bot restart and manual state repair.
- **Evidence:**
```python
async def _guilty_callback(self, interaction: discord.Interaction):
    ...
    self._acted = True                                         # set BEFORE modal
    await interaction.response.send_modal(RulingNotesModal(self.complaint_id, "guilty"))
    # if send_modal raises → _acted stays True forever
```
Same pattern at L644 (`_not_guilty_callback`) and L658 (`_dismiss_callback`).
- **Suggestion:** Do not set `_acted` until the ruling is committed to `_complaints` inside `RulingNotesModal.on_submit()`. Guard the modal open with try/except and reset if it fails:
```python
try:
    await interaction.response.send_modal(RulingNotesModal(self.complaint_id, "guilty"))
    self._acted = True
except Exception:
    pass  # allow retry
```

### [W-03] `validate_position_change()` receives raw API player dicts — `"nan"` string attributes crash on `int()` conversion

- **File:** `sentinel_cog.py` L2113 (caller) + L1579–1581 `_g()` (callee)
- **Impact:** Player dicts from `dm.get_players()` can have attribute fields that are the string `"nan"` (not float NaN — a literal string from pandas CSV export). Genesis's `_sanitize_player()` catches this. Sentinel's `_run_position_change()` passes the raw player dict directly to `validate_position_change()` without sanitization. Inside `_g()`: `int(player.get(field, 0) or 0)` — `int("nan" or 0)` evaluates as `int("nan")` which raises `ValueError`. Any position change involving a player with a corrupted attribute field crashes mid-flow with no response to the user.
- **Evidence:**
```python
def _g(player: dict, field: str, default: int = 0) -> int:
    """Get a numeric player attribute safely."""
    return int(player.get(field, default) or default)
    # int("nan") → ValueError if field is literally the string "nan"
```
- **Suggestion:** Apply genesis's `_safe_int` pattern in `_g()`, or call `_sanitize_player()` before passing to `validate_position_change()` (import from genesis_cog or duplicate the function):
```python
def _g(player: dict, field: str, default: int = 0) -> int:
    val = player.get(field, default) or default
    try:
        f = float(val)
        return default if (f != f or f == float('inf') or f == float('-inf')) else int(f)
    except (ValueError, TypeError):
        return default
```

### [W-04] Trade card render failure leaves original image showing "PENDING REVIEW" after approval/rejection

- **File:** `genesis_cog.py` `TradeActionView._update_status()` L1592–1692
- **Impact:** When Playwright image re-render fails (crash, timeout), the original trade card image in Discord still shows "PENDING REVIEW" badge even after the trade is resolved. The log channel correctly posts the status update. Anyone viewing the original card is misled. Previously flagged as C1 — still unresolved.
- **Evidence:** On render failure (L1659 `except Exception`), the fallback at L1662 cannot edit an image-backed message with an embed (image messages have no `.embeds`), so the `else:` branch fires an ephemeral followup only. The original image is never updated. The log channel is notified (L1681–1692), creating a state mismatch between visual card and log.
- **Suggestion:** On render failure, at minimum disable the action buttons on the original message to prevent repeat clicks on a stale card:
```python
except Exception as e:
    print(f"[trade_center] Status re-render error: {e}")
    try:
        await interaction.message.edit(view=disabled_view)   # disable buttons even if image stale
    except Exception:
        pass
```

---

## OBSERVATIONS — Track for Later

### [O-01] card_renderer.py silently caps trade card assets at 4 per side

- **File:** `card_renderer.py` L208–216
- **Note:** `players_a[:4]` and `picks_a[:4]` — trades with 5+ assets on one side silently truncate in the image. The embed fallback shows all assets. A 5-player trade has a missing player in the PNG with no visual indicator. Low frequency but the discrepancy between image and embed could cause confusion during approval.

### [O-02] `trade_engine.evaluate_trade()` reads `parity_state.json` synchronously on every call

- **File:** `trade_engine.py` L348–355
- **Note:** Every trade evaluation does one synchronous file read (FIX #4 moved it out of the per-player loop, which was the right call). Low risk for current traffic. Worth wrapping in `asyncio.to_thread()` before any VPS deploy with network-mounted storage.

### [O-03] `genesis_cog._save_state()` is synchronous — called from async command handlers

- **File:** `genesis_cog.py` L1931–1940, called at L2017, L2037, L2055, L2139
- **Note:** These saves happen in `_runlottery_impl`, `_orphanfranchise_impl`, `log_cap_clear_attempt`, and `_run_position_change` — all async. File is small JSON (~5–10 KB) so blocking is sub-millisecond, but technically incorrect. `asyncio.to_thread(_save_state)` would be the clean fix.

### [O-04] Previous C1 (double log-channel announce) — resolved in current code

- **File:** `genesis_cog.py` `_update_status()` L1592–1692
- **Note:** Last Thursday's audit flagged a double log-channel send on render failure. Current code does not reproduce this: the success path returns early after one send (L1658), and the fallback path sends exactly one log-channel message (L1681–1692). No double-announce present. Closing as resolved.

### [O-05] `god_cog.py` is 121 lines — clean, minimal, well-scoped

- **File:** `god_cog.py`
- **Note:** Two commands, both gated with `is_god()`, both defer before heavy work, `affinity` module safely optional. No issues found. Pattern is exemplary for a privileged command module.

---

## CROSS-MODULE RISKS

### [X-01] sentinel_cog.py ↔ genesis_cog.py tight coupling on `_state` / `_save_state` internals

- **Caller:** `sentinel_cog.py` L1489 — imports genesis_cog private symbols
- **Callee:** `genesis_cog.py` L1908–1940 — `_state` and `_save_state` are module globals, not a public API
- **Risk:** Any rename or extraction of parity state breaks sentinel silently at import time. The fallback split-state described in C-01 is the direct consequence. The genesis_cog load-order comment at CLAUDE.md confirms genesis must load before sentinel — a load failure at any point breaks this contract invisibly.

### [X-02] `validate_position_change()` stat thresholds are AND-checked but CLAUDE.md requires OR for dual-attribute

- **Caller:** `sentinel_cog.py` L1802–1816 — `validate_position_change()` inner loop
- **Callee:** `ability_engine.check_physics_floor()` — correctly implements OR for dual-attr
- **Risk:** `validate_position_change()` has its own attribute checking logic (`_g()` / `_h()` / `_w()`) that checks ALL attribute thresholds as AND. For position change rules that happen to specify exactly 2 regular attributes (e.g. S→CB: `speedRating >= 88` AND `agilityRating >= 85`), the intent may be AND (a player must have both), which is fine. But there is no dual-attr OR override in `validate_position_change()` — unlike `ability_engine`. If future position rules are added expecting OR semantics, they would silently AND. Low risk with current rules (they appear intentionally AND), but worth documenting.

### [X-03] Two live `TradeActionView` instances per trade — no cross-message button sync

- **Caller:** `genesis_cog._evaluate_and_post()` L893–910 — a second `TradeActionView` posted to the trade log channel
- **Callee:** Same function L867 — first view in the proposer's ephemeral
- **Risk:** Approving from the log-channel buttons disables those buttons, but the proposer's ephemeral still shows active Approve/Reject buttons. The `_trade_approval_lock` prevents double-execution, but stale buttons in the ephemeral can cause confusing "already approved" error messages hours later. Low severity (correct outcome, bad UX).

---

## POSITIVE PATTERNS WORTH PRESERVING

1. **`_trade_approval_lock` (genesis_cog.py L86, L1528)** — TOCTOU prevention correctly applied. Status re-checked inside the lock before any state mutation. The TOCTOU bug from prior seasons is gone.
2. **`_validate_image_url()` allowlist + `<untrusted_user_note>` tags (sentinel_cog.py L62–64, L824)** — Discord CDN allowlist prevents SSRF. User-supplied note is wrapped in untrusted tags in the AI prompt. Good prompt injection hygiene at both layers.
3. **`_sanitize_player()` NaN guards (genesis_cog.py L90–142)** — Comprehensive `_safe_int()` / `_safe_float()` applied to all player fields before they reach the trade engine. Applied in both text-mode and picker-mode paths.
4. **Dual-attribute OR logic in `ability_engine.check_physics_floor()` (L576–602)** — CLAUDE.md spec correctly implemented: `is_dual_attr = len(regular_keys) == 2` gates OR vs AND. `DEV_INT_TO_STR` mapping exactly matches spec (0=Normal, 1=Star, 2=Superstar, 3=XFactor).
5. **Async file locking in sentinel_cog.py complaint/FR subsystem (L121–122, L182–188)** — `_complaint_file_lock` and `_fr_file_lock` correctly protect concurrent writes. Atomic `os.replace()` used throughout. Pattern is correct and consistent.

---

## TEST GAPS

| Test Case | Type | What It Validates | Priority |
|-----------|------|-------------------|----------|
| `test_position_change_cornerstone_bypass_on_import_fail` | integration | Cornerstone lock survives genesis_cog import failure | high |
| `test_counter_auth_importerror_denies` | unit | Counter trade denied when intelligence unavailable | high |
| `test_validate_position_change_nan_string_attr` | unit | `_g()` handles `"nan"` string without ValueError | high |
| `test_trade_render_fail_buttons_disabled` | integration | Original trade card buttons disabled when image re-render fails | med |
| `test_ruling_panel_acted_flag_on_modal_fail` | unit | `_acted` not set if `send_modal()` raises | med |
| `test_evaluate_trade_blocks_cornerstone_player` | integration | `evaluate_trade()` returns RED band for cornerstoned player | high |

---

## METRICS

| Metric | Value |
|--------|-------|
| Files deeply audited | 7 |
| Critical findings | 1 |
| Warnings | 4 |
| Observations | 5 |
| Cross-module risks | 3 |
| Test gaps identified | 6 |
| Anti-pattern grep hits | 3 (counter fail-open, `_g()` NaN gap, `_acted` premature set) |

**Overall health:** Genesis and Sentinel are structurally solid — TOCTOU locking, NaN sanitization, and async file locking are all well-implemented. The dominant risk is the sentinel↔genesis parity state coupling, which creates a silent compliance bypass path on any genesis_cog load failure. Extracting `parity_state.py` is the correct long-term fix and the existing TODO acknowledges it. Short-term: add a log error on the ImportError fallback so the split-state condition is at least visible in bot startup logs.

**Next audit focus:** Friday — AI, Codex & Echo
