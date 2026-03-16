# ATLAS UX & Bug Audit — Design Spec

**Date:** 2026-03-16
**Scope:** Full codebase review for bugs, UX friction, and consistency issues
**Delivery:** 3 incremental phases, each a standalone version bump

---

## Context

ATLAS has grown significantly — 12+ cogs, a full casino system, sportsbook, economy, AI query engine, and persona system. This audit reviews the entire codebase for user-facing bugs, race conditions, error handling gaps, and UX friction points that have accumulated during rapid feature development.

**Goal:** Ship three incremental phases that improve reliability, user experience, and visual consistency without breaking existing functionality or requiring schema migrations.

---

## Phase 1: Critical Bugs & Data Integrity

**Version bump:** Patch (e.g., 2.X.0 → 2.X.1)

### 1.1 Coinflip accept — view stops before deduct confirmed

**File:** `casino/games/coinflip.py` (ChallengeView.accept, line ~219-231)
**Bug:** `self.resolved = True` and `self.stop()` are called BEFORE `deduct_wager()`. If the deduct fails (insufficient funds), `resolved` resets to `False` but the view is already stopped. The opponent cannot retry accepting — the buttons are dead.

**Fix:** Move `self.stop()` after successful deduct:
```python
self.resolved = True  # prevent TOCTOU double-accept
try:
    await deduct_wager(self.opponent_id, self.wager)
except Exception as e:
    self.resolved = False
    return await interaction.response.send_message(
        f"Insufficient funds: {e}", ephemeral=True)
self.stop()  # only stop view after confirmed deduct
active_challenges.pop(self.challenge_id, None)
```

### 1.2 Crash round HTTPException silently swallowed

**File:** `casino/games/crash.py` (lines ~147-148, ~192-193, ~288-294)
**Bug:** `except discord.HTTPException: pass` in three locations means if embed/file upload fails during a crash round, the round continues but players see nothing. The final crash result embed (line ~288-294) is the worst case — players never see the outcome.

**Fix:** Log the exception and attempt a text-only fallback at all three locations:
```python
except discord.HTTPException as exc:
    log.warning(f"Crash round render failed: {exc}")
    try:
        await channel.send(f"**CRASH** — Multiplier: {multiplier:.2f}x (render failed)")
    except discord.HTTPException:
        pass  # truly dead channel
```

### 1.3 `call_atlas()` exposes raw exception to users

**File:** `bot.py` (line 308-309)
**Bug:** `return f"ATLAS Brain Error: {str(e)}"` can include Gemini API internals, auth errors, or stack traces visible to users.

**Fix:** Use existing `print()` + `traceback.print_exc()` pattern (matches bot.py conventions) and return a friendly message:
```python
except Exception as e:
    print(f"[Gemini] call_atlas failed: {e}")
    traceback.print_exc()
    return "ATLAS is having trouble thinking right now. Try again in a moment."
```

### 1.4 Casino modal — no wager validation before game entry

**File:** `casino/casino.py` (CasinoHubModal.on_submit, line 112-130)
**Bug:** Modal accepts any integer. If user enters 0, negative, or above max bet, they get a confusing error from the game function instead of clear feedback at the modal level. Balance checking stays in game functions (context-dependent), but min/max bounds belong at the modal level.

**Fix:** Add validation immediately after parsing:
```python
wager = int(self.wager_input.value.strip().replace(",", ""))
if wager < 1:
    return await interaction.response.send_message(
        "Wager must be at least $1.", ephemeral=True)
max_bet = await get_max_bet(interaction.user.id)
if wager > max_bet:
    return await interaction.response.send_message(
        f"Your max bet is **${max_bet:,}**. Enter a lower amount.", ephemeral=True)
```

### 1.5 JSON state files — non-atomic writes (awards_cog only)

**File:** `awards_cog.py` (line ~25-30)
**Bug:** `_save_polls()` writes directly to the target file. If the process crashes mid-write, the file is corrupted and poll state is lost on next load.
**Note:** `sentinel_cog.py` already uses atomic write-to-temp-then-replace — no fix needed there.

**Fix:** Apply the same atomic pattern sentinel_cog already uses:
```python
import tempfile, os
def _save_polls(data, path):
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic on POSIX and Windows
```

### 1.6 Crash `active_rounds` type sentinel bug

**File:** `casino/games/crash.py` (line ~519)
**Bug:** `active_rounds[ch_id] = "PENDING"` assigns a string to a dict typed `dict[int, CrashRound]`. Any code checking `active_rounds.get(ch_id)` and accessing `.status` on the result will crash with `AttributeError` if hit during the PENDING window. The guard at line ~487 (`if existing and existing.status == "running"`) throws on the string sentinel.

**Fix:** Use a proper sentinel — either a CrashRound with status="pending", or check `isinstance()` before accessing `.status`:
```python
existing = active_rounds.get(ch_id)
if existing is not None and isinstance(existing, CrashRound) and existing.status == "running":
    ...
```

### 1.7 Play-again UX hardening

**File:** `casino/play_again.py` (line 38)
**Note:** Not a true race condition (Python asyncio is single-threaded and `_used` is set synchronously before any `await`), but a UX improvement — visually disable buttons immediately so users get instant feedback.

**Fix:** Move `_disable_all()` + `interaction.message.edit(view=self)` to run before the `get_balance()` await, not after:
```python
self._used = True
self._disable_all()
await interaction.response.defer()
await interaction.message.edit(view=self)  # immediate visual feedback
# ... then check balance and proceed
```

---

## Phase 2: UX Friction

**Version bump:** Minor (e.g., 2.X.0 → 2.(X+1).0)

### 2.1 Show max bet in casino modal placeholder

**File:** `casino/casino.py` (CasinoHubModal.__init__, line 101-110)
**Change:** Make modal dynamically show the user's max bet in the placeholder text.

**Challenge:** Modal `__init__` is sync, but `get_max_bet()` is async. Two options:
- **Option A:** Pass max_bet into the modal constructor from the hub button callback (which is async). Recommended.
- **Option B:** Use a default placeholder and validate on submit (already done in 1.6).

**Recommended:** Option A — hub button fetches max_bet, passes to modal:
```python
# In CasinoHubView button callback:
max_bet = await get_max_bet(interaction.user.id)
await interaction.response.send_modal(CasinoHubModal("blackjack", max_bet=max_bet))
```

### 2.2 Coinflip 3-step → 2-step flow

**File:** `casino/casino.py` (CasinoHubModal.on_submit coinflip branch, line 126-130)
**Current flow:** Hub button → Modal (wager) → CoinPickView (heads/tails) → game
**Proposed flow:** Hub button → Modal (wager + side select) → game

**Fix:** Add a second TextInput or use a Select in the modal. Since modals don't support selects, add a TextInput:
```python
self.side_input = discord.ui.TextInput(
    label="Pick a side",
    placeholder="heads or tails",
    min_length=1, max_length=5,
)
```
Validate flexibly: accept any input starting with "h" or "t" (case-insensitive). Reject anything else with a clear error.

### 2.3 Better insufficient funds message

**File:** `casino/casino.py` (line 116-118) + individual game files
**Current:** "Invalid amount" for all errors.
**Fix:** Differentiate error cases:
- Non-numeric → "Enter a whole number (e.g., 50)"
- Below min → "Minimum wager is $1"
- Above max → "Your max bet is $X"
- Insufficient balance → "You have $X — need $Y more"

### 2.4 Codex query failure feedback

**File:** `codex_cog.py` (line ~658)
**Current:** On SQL error after auto-correct: "Try `/ask_debug`"
**Fix:** Show a user-friendly explanation:
```
"ATLAS couldn't find an answer for that query. Try rephrasing — for example:
  • Use full player names ('Patrick Mahomes' not 'Mahomes')
  • Specify the season ('in season 95' not 'this year')
  • Ask about one thing at a time"
```

### 2.5 Stale data indicator

**File:** `data_manager.py` + embed-producing cogs
**Change:** Add `last_sync_ts` timestamp to `data_manager`. Display "Data as of X minutes ago" in footer of stats/standings embeds when data is >30 minutes old.

### 2.6 Ephemeral drill-down sharing

**File:** `oracle_cog.py` (AnalyticsNav callbacks)
**Change:** Add a "Share to Channel" button on ephemeral analytics embeds. When clicked, re-sends the same embed as a public (non-ephemeral) message.

---

## Phase 3: Consistency & Polish

**Version bump:** Minor (e.g., 2.(X+1).0 → 2.(X+2).0)

### 3.1 Embed color/footer template

**New file:** `embed_helpers.py`
**Purpose:** Centralize embed construction to enforce:
- ATLAS_GOLD (0xD4AF37) as default color
- Standard footer: `"ATLAS -- {module} · {timestamp}"`
- Max field count check (25 field Discord limit)

Existing cogs updated to use `build_embed()` instead of raw `discord.Embed()`.

### 3.2 Individual slash commands per casino game

**File:** `casino/casino.py`
**Change:** Add `/blackjack`, `/slots`, `/crash`, `/coinflip` as standalone slash commands in the CasinoCog. Each command:
- Uses `@require_channel()` decorator (from `permissions.py`) to restrict to its designated game channel
- Opens the wager modal directly (same `CasinoHubModal` used by the hub)
- Channel IDs already stored in casino_db via the `_CASINO_BRIDGE` in setup_cog

```python
@app_commands.command(name="blackjack", description="Start a blackjack hand")
async def blackjack_cmd(self, interaction: discord.Interaction):
    bj_channel = await db.get_channel_id("blackjack")
    if bj_channel and interaction.channel_id != bj_channel:
        return await interaction.response.send_message(
            f"Play blackjack in <#{bj_channel}>!", ephemeral=True)
    await interaction.response.send_modal(CasinoHubModal("blackjack"))
```

Repeat for slots, crash, coinflip. The `/casino` hub remains available everywhere as the unified entry point.

### 3.3 Crash multiplier cap

**File:** `casino/games/crash.py` (line ~118)
**Change:** Add `min(multiplier, 100.0)` to cap theoretical max payout at 100x.

### 3.4 Embed field consistency across casino games

**Files:** All casino game files (blackjack, slots, crash, coinflip)
**Change:** Standardize result embed layout:
- Field 1: Outcome (win/loss/push)
- Field 2: Payout + multiplier
- Field 3: New balance
- Footer: txn_id + streak info (if any)

---

## Files Modified (by phase)

### Phase 1
| File | Changes |
|------|---------|
| `casino/games/coinflip.py` | Fix `stop()` ordering in ChallengeView.accept |
| `casino/games/crash.py` | Log + fallback on HTTPException (3 locations), fix `active_rounds` type sentinel |
| `casino/casino.py` | Wager validation in modal |
| `casino/play_again.py` | Immediate visual button disable |
| `bot.py` | Sanitize Gemini error messages |
| `awards_cog.py` | Atomic JSON writes |

### Phase 2
| File | Changes |
|------|---------|
| `casino/casino.py` | Max bet in placeholder, 2-step coinflip, better error msgs |
| `codex_cog.py` | User-friendly query failure message |
| `data_manager.py` | Add `last_sync_ts` |
| `oracle_cog.py` | Stale data footer, share button |

### Phase 3
| File | Changes |
|------|---------|
| `embed_helpers.py` | New — shared embed builder |
| `casino/games/*.py` | Standardized result embeds |
| `casino/games/crash.py` | Multiplier cap |
| `casino/casino.py` | Add `/blackjack`, `/slots`, `/crash`, `/coinflip` slash commands (channel-restricted) |

---

## Verification Plan

### Phase 1 Testing
1. **Coinflip deduct failure:** Mock insufficient funds on accept, verify view stays active for retry (buttons not dead)
2. **Crash render failure:** Simulate HTTPException at all 3 locations, verify text fallback appears and log entry written
3. **Crash active_rounds sentinel:** Start a crash round, verify no AttributeError during PENDING window
4. **Gemini error:** Disconnect API key, run a command using `call_atlas()`, verify user sees friendly message (no raw exception)
5. **Modal validation:** Enter 0, -1, 999999 in wager modal, verify clear error messages with correct max bet shown
6. **Awards atomicity:** Verify `_save_polls()` uses atomic write pattern
7. **Play-again buttons:** Click Play Again, verify buttons visually disable immediately (before balance check completes)

### Phase 2 Testing
1. **Max bet display:** Open modal as different tier users, verify placeholder shows correct max
2. **2-step coinflip:** Complete coinflip from hub in exactly 2 interactions
3. **Codex failure:** Ask an unanswerable query, verify helpful rephrasing suggestions
4. **Stale data:** Wait >30min after sync, verify footer shows staleness warning
5. **Share button:** Click "Share to Channel" on ephemeral embed, verify public post

### Phase 3 Testing
1. **Embed consistency:** Run each casino game, screenshot result embeds, verify layout matches spec
2. **Slash commands:** Run `/blackjack` in #blackjack channel — verify modal opens. Run `/blackjack` in #general — verify channel restriction message
3. **Crash cap:** Simulate crash round lasting 200+ seconds, verify multiplier capped at 100x
