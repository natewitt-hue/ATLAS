# Adversarial Review: casino/casino.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 655
**Reviewer:** Claude (delegated subagent)
**Total findings:** 19 (3 critical, 9 warnings, 7 observations)

## Summary

The casino entrypoint cog mostly delegates well, but the admin / refund / "give scratch" paths have several real-money and idempotency holes that would survive any retry storm and bypass the project-wide `reference_key` rule. The hub render path silently swallows all exceptions without logging, the `_casino_clear_session_impl` refund races against the live session's own resolve path, and `WagerPresetView`'s `_used` guard loses re-entrancy across the modal handoff. The commissioner-only impl methods rely entirely on `boss_cog`'s upstream check with no defense in depth — adequate today, but one stray binding is all it takes.

## Findings

### CRITICAL #1: `_casino_clear_session_impl` refund violates `reference_key` rule and races the active session
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:583-600`
**Confidence:** 0.95
**Risk:** Calling `db.refund_wager(user.id, session.wager)` without a `reference_key` violates CLAUDE.md's flow-economy invariant ("ALL credit calls in settlement paths MUST pass `reference_key`"). Worse, the refund races the live session: at line 587 we `session.view.stop()` and at line 588 we `pop` the session, but if the player has already submitted Hit/Stand/Double on Discord and that handler is mid-`process_wager`, the user gets paid by both code paths — the resolve path credits the win, AND this admin refund credits the original wager again.
**Vulnerability:**
1. `refund_wager` (`casino_db.py:1089`) calls `flow_wallet.credit(...)` with `subsystem_id=correlation_id` but no `reference_key`. If the admin double-clicks the boss button or Discord retries, the credit posts twice — net positive money creation. The `correlation_id` parameter to `refund_wager` is also never passed in by `_casino_clear_session_impl` (line 590), so even the registry settle path is skipped, leaving wager_registry orphans.
2. `view.stop()` does not interrupt an in-flight handler or `await` chain; it only prevents new interactions. Anything already past the `process_wager` call will run to completion, and `active_sessions.pop` is non-atomic with the session's own resolve.
3. No `try/except` — if `refund_wager` fails after `view.stop()` and `pop`, the session is destroyed but the user's money is gone.
**Impact:** Direct financial corruption: free money on retry, double payouts on race, permanent wager loss on partial failure. Admin tool intended to *fix* stuck sessions becomes a bug source.
**Fix:**
```python
async def _casino_clear_session_impl(self, interaction, user):
    session = bj_sessions.get(user.id)
    if not session or not hasattr(session, "wager"):
        return await interaction.response.send_message(
            f"❌ {user.mention} has no active blackjack session.", ephemeral=True)
    if hasattr(session, "view") and session.view:
        session.view.stop()
    bj_sessions.pop(user.id, None)
    ref_key = f"CASINO_ADMIN_REFUND_{user.id}_{session.correlation_id}_{int(time.time())}"
    try:
        await db.refund_wager(
            user.id, session.wager,
            correlation_id=session.correlation_id,
            reference_key=ref_key,  # add this kwarg to refund_wager too
        )
    except Exception:
        log.exception("Admin refund failed for %s", user.id)
        return await interaction.response.send_message(
            "❌ Refund failed — check logs and ledger.", ephemeral=True)
    await interaction.response.send_message(...)
```
And `refund_wager` itself needs to accept and forward `reference_key`.

### CRITICAL #2: `_casino_give_scratch_impl` raw `DELETE` has no guards against retry/replay
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:602-614`
**Confidence:** 0.90
**Risk:** Deleting from `daily_scratches` resets the user's claim cooldown unconditionally. There is no audit log, no idempotency token, no `try/except`, and no permission check inside the impl. If `boss_cog` accidentally fires this twice (Discord retry, double-click, replay attack from a lingering button), the user gets *N* free scratches. Combine with a malicious user who keeps requesting "I'm stuck please give me a scratch" and the admin loses track of how many they granted.
**Vulnerability:**
1. No `try/except` around the `DELETE` — if the connection drops, the admin sees no error message and the player still might have been rebated; ambiguous state.
2. No log line at all (`log.info` or `log.exception`). The admin action vanishes from the audit trail. Casino ops with no audit trail violates the "admin-facing" observability rule.
3. No bound check on `user.id` — accepts any `discord.Member`, including bots.
4. The function does not verify whether the user had already claimed today; it just deletes the row. If used carelessly, an admin can hand out unlimited scratches in one session.
**Impact:** Silent admin abuse vector + zero observability + financial leak via uncapped scratch grants. Each scratch can pay up to `CASINO_DAILY_MAX` ($150 default) per the constant in `casino_db.py`.
**Fix:** Wrap in `try/except`, `log.info("Admin %s granted bonus scratch to %s", interaction.user.id, user.id)`, and prefer an explicit `INSERT INTO scratch_grants (granted_by, target, ts) ...` audit row in a single transaction with the DELETE.

### CRITICAL #3: `_send_wager_view` reads balance/max_bet then sends view without TOCTOU lock — opens hub-launch double-spend
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:237-247`, `C:/Users/natew/Desktop/discord_bot/casino/casino.py:415-443`
**Confidence:** 0.85
**Risk:** The hub button reads `max_bet` and `balance` *outside* any lock, builds a `WagerPresetView` snapshot of the balance, and shows it ephemerally. If the user opens the hub twice in quick succession (or already has a different wager view from a previous interaction), they get two views each thinking the user has $X. They can then click a preset on view A, immediately click on view B, and both `_launch_game` calls race into `start_blackjack`/`play_slots`/etc. Each game's own `deduct_wager` does take a lock (verified in `casino_db.py:1056`) and `start_blackjack` has an `active_sessions[uid] = "PENDING"` sentinel pre-await (`blackjack.py:593`), so blackjack survives, but `slots`/`coinflip`/`crash` rely entirely on `flow_wallet.debit` raising `InsufficientFundsError`. That's fine for the second debit, but the *user* still sees a "your balance is $X" UI snapshot from each view that was already wrong at click time — UX confusion plus a real probability that an out-of-tier `wager` slips through if `BET_TIERS` recently changed.
**Vulnerability:**
1. `WagerPresetView.balance` is captured at view creation; the disabled-button logic at line 161 (`disabled = balance < amount`) is decided once and never re-checked at click time. The user can click a $50,000 button after losing on another tab.
2. `_send_wager_view` does not enforce single-active-view-per-user; nothing in this file expires or invalidates older `WagerPresetView` instances when a new one is opened.
3. `_used` is per-view; not per-user. So one user, two views, two `_used` flags, one balance.
**Impact:** Stale-balance enabled wagers bypass UI tier-cap enforcement (server side still catches it via `deduct_wager`'s `ValueError` at `casino_db.py:1064`, but only because of that defensive check). Without that defense, this is a free over-betting hole.
**Fix:** Either (a) recheck `balance` and `max_bet` inside `_on_preset` before calling `_launch_game`, or (b) attach a per-user "active_wager_view_id" sentinel in the cog that invalidates older views when a new one opens.

### WARNING #1: Hub embed render fallback swallows ALL exceptions
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:392-409`
**Confidence:** 0.90
**Risk:** `try: ... except Exception: embed = ...` — bare `except` with no `log.exception(...)`. CLAUDE.md explicitly forbids silent swallows in admin/financial views. If `build_casino_hub_card` or `card_to_file` ever fails (Playwright pool exhaustion, font load, theme typo, file IO), the user always sees the fallback embed and the dev never sees a stack trace. Casino render failures will hide indefinitely.
**Vulnerability:** No `log.exception("Casino hub render failed")` anywhere on the fallback path. The lack of logging makes Playwright leaks (closed pages not returned to pool — focus block calls this out) invisible.
**Impact:** Silent rendering regressions. If a theme update breaks the casino hub card, all players quietly get a stripped-down embed and the on-call has no signal.
**Fix:**
```python
except Exception:
    log.exception("Casino hub card render failed for uid=%s", uid)
    embed = discord.Embed(...)
```

### WARNING #2: `post_to_ledger` second `try/except: pass` swallows admin-channel notification failures
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:71-84`
**Confidence:** 0.90
**Risk:** When the primary `post_casino_result` fails, the fallback path tries to notify `admin_chat`. If THAT also fails, line 84 silently swallows the exception with a `# Never let notification failure cascade` comment. Result: the original ledger write failure is logged, but if the admin notification also fails (channel gone, perms revoked, intermittent Discord 5xx), nobody sees the failure-of-failure. This is exactly the pattern CLAUDE.md prohibits in admin-facing paths.
**Vulnerability:** The intent ("never cascade") is correct, but `pass` should be `log.exception("Failed to notify admin of ledger write failure")`. The current code is a guaranteed silent black hole.
**Impact:** Cascading observability gaps — financial write failures go invisible if even one notification step also fails.
**Fix:** Replace `pass` with `log.exception("Admin notification of ledger write failure also failed")`.

### WARNING #3: `flow_bus.emit("game_result", event)` silent failure path
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:86-96`
**Confidence:** 0.80
**Risk:** Wrapped in `try/except Exception: log.exception(...)`, which is correct, BUT this emit happens *after* the ledger write attempt (which itself can fail and only logs to admin chat). Combined with the fact that the `flow_live_cog` consumer has no replay/dedupe of `game_result` events, a missed emit means a missed live highlight forever. There's no idempotency token on the event.
**Vulnerability:** `GameResultEvent` has no `event_id` field (`flow_events.py:16-26`), so subscribers cannot dedupe. If a future change adds a retry around `flow_bus.emit`, the consumer will double-process. Tightly coupled to the consumer never being replayed; fragile.
**Impact:** Lost highlights on emit failure; future double-processing if anyone adds retry. Per the focus block, "no live code publishes to `sportsbook_result`" — this is the casino-side analog and shows the same pattern of fire-and-forget without dedupe.
**Fix:** Add a `event_id: str = uuid.uuid4().hex` or similar field to `GameResultEvent` and have consumers dedupe.

### WARNING #4: `WagerPresetView._used` re-entrancy bug across modal path
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:148-211`
**Confidence:** 0.85
**Risk:** `_on_preset` sets `self._used = True` and then `self.stop()`. But `_on_custom` doesn't touch `_used` at all — it opens a modal, the modal calls back into `_launch_game` *much later*. During that gap, the user can: (a) click another preset button, which is now blocked by `_used`... wait, `_used` is False because `_on_custom` never set it. So the user clicks a preset *while* the modal is open, then the modal callback ALSO fires `_launch_game`. Now the cog launches the same game twice for the same user.
**Vulnerability:** `_on_custom` does not set `_used = True` before sending the modal, and does not call `self.stop()`. The view stays "open" while the modal is being filled out.
**Impact:** Two parallel launches of slots/coinflip from one wager view. For blackjack, the `active_sessions[uid] = "PENDING"` sentinel saves us, but slots and coinflip have no such sentinel — both go through and both call `flow_wallet.debit`. The second debit would fail on insufficient funds for an already-broke user, but a flush-balance user gets two consecutive games for the price of one click-and-modal.
**Fix:** Set `self._used = True` and `self.stop()` inside `_on_custom` *before* sending the modal. The `CustomWagerModal.on_submit` already validates the amount; the view does not need to remain alive.

### WARNING #5: `CustomWagerModal.on_submit` callback path bypasses `_used` flag entirely
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:130-145`
**Confidence:** 0.85
**Risk:** The modal's `on_submit` directly calls `self._callback(interaction, wager)` (which is `WagerPresetView._launch_game`) without checking `WagerPresetView._used`. If the user submits the modal twice (Discord double-submit / network retry), `_launch_game` runs twice.
**Vulnerability:** Discord modal submit is debounced client-side but not server-side — and Discord will retry the interaction. There is no `_callback`-side guard.
**Impact:** Double-launch of game from modal retry.
**Fix:** Pass an idempotency check into `_launch_game` or have the callback check/set its own `_used` flag.

### WARNING #6: `interaction.response.send_modal` without prior `defer` — but `_launch_game` then calls game functions that themselves `defer`
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:193-211`
**Confidence:** 0.75
**Risk:** `_on_custom` sends the modal correctly (modals are their own response). Then `CustomWagerModal.on_submit` immediately calls `self._callback(interaction, wager)` which routes to `start_blackjack`/`play_slots`/etc. Most of those functions immediately call `interaction.response.defer()` (verified for blackjack at `blackjack.py:564`). That's fine — modal submit is a fresh interaction, so deferring is correct. BUT for the `coinflip` branch in `_launch_game` (lines 207-210), the code calls `interaction.response.send_message("🪙 Pick a side:", view=view, ephemeral=True)` instead of deferring first. If `play_coinflip` needs >3s (e.g., AI flavor text), the heads/tails follow-up will fail. The same issue exists in the press-button path of `_on_preset` for coinflip.
**Vulnerability:** Inconsistent use of `defer` between the four game branches. Coinflip is the slowest path because it eventually opens a `CoinPickView`.
**Impact:** Modal-then-coinflip-pick path can hit Discord 3s timeout if Discord is slow or if the bot is under load.
**Fix:** Defer at the top of `_launch_game` for all game types, then use `interaction.followup.send` consistently.

### WARNING #7: `_compute_presets` can produce only one preset for low max_bet, leaving the wager UI nearly empty
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:102-111`
**Confidence:** 0.70
**Risk:** For `max_bet=1`, raw is `[1, 1, 1, 1]`, dedupe collapses to `[1]`, leaving exactly one preset button + the Custom button. New users at the lowest tier see a confusing single-button UI. For `max_bet=4`: raw=`[1,1,2,4]` → `[1,2,4]`. Functional but inconsistent layout (the UI is column-aligned to a 4-button row).
**Vulnerability:** `discord.ui.Button` row=0 will hold 1-4 buttons inconsistently across users. The row layout is fragile if you ever want to add a fifth preset.
**Impact:** Cosmetic + user confusion at the bottom tier. Not a security issue but a documented "low max_bet" UX hole.
**Fix:** Floor `_compute_presets` to a fixed length (e.g., always 4 presets, scaled from `max(1, max_bet // 4)` upward, even if some are duplicates — disable duplicates instead of dropping).

### WARNING #8: Admin commands in `_*_impl` methods missing `is_commissioner()` defense in depth
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:471-650` (all `_casino_*_impl`)
**Confidence:** 0.70
**Risk:** None of the eleven `_casino_*_impl` methods check `is_commissioner(interaction)` themselves. They rely entirely on `boss_cog.py`'s upstream check (verified at `boss_cog.py:847`). If a developer ever wires one of these directly to a slash command, button, or context menu without remembering to add the check, any user can run them. CLAUDE.md says "permission decorators missing on commissioner-only commands" is an attack-surface concern.
**Vulnerability:** Defense-in-depth missing. The convention is "the caller checks", which is brittle.
**Impact:** One sloppy future binding to `_casino_open_impl`/`_casino_jackpot_seed_impl`/etc. and any user can open/close the casino, seed jackpots, or grant scratches.
**Fix:** Add an `if not await is_commissioner(interaction): return ...` guard inside each `_*_impl` method, or wrap them with a shared decorator.

### WARNING #9: `_casino_jackpot_boost_impl` parses ISO datetime back from a comma-joined string — fragile schema
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:640-649`
**Confidence:** 0.75
**Risk:** Stores `f"{multiplier},{expires.isoformat()}"` in a single setting key. Reader code (somewhere in casino_db or jackpot logic) must split on `,` and parse the rest. ISO 8601 timestamps **contain commas** in some locales/formatters and **always contain colons**, so comma is "safe" only by convention. If anyone ever changes `expires` to use a fractional second formatter that includes a `,` (locale-dependent), the parser breaks. Also, `multiplier` is a `float` with no validation — a caller passing `0.0` or negative is silently accepted and breaks the boost.
**Vulnerability:**
1. No validation: `multiplier <= 0` should raise; `minutes <= 0` should raise.
2. Schema is a string concatenation rather than a structured column.
3. The boost message is sent without `ephemeral=True` (line 646-649) — which is intentional for visibility, but contrast with literally every other admin command in this file that uses `ephemeral=True`. Inconsistency.
4. No `defer()` despite calling `db.set_setting` (a blocking-ish operation).
**Impact:** Schema-fragile config; missing validation; one obvious panic vector if a future formatter sneaks a comma in.
**Fix:** Use two separate settings keys: `casino_jackpot_boost_multiplier` and `casino_jackpot_boost_expires_iso`. Validate `multiplier > 0` and `minutes > 0`.

### OBSERVATION #1: `print(...)` instead of `log.info(...)` in `cog_load`
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:337, 343`
**Confidence:** 0.95
**Risk:** Two `print()` calls in `cog_load` instead of structured logging. Won't show up in any log aggregator and can't be filtered/silenced.
**Impact:** Operator visibility gap on bot startup.
**Fix:** Replace with `log.info("[Casino] Reconciled %d orphaned wagers", len(refunded))` and `log.info("[Casino] DB ready. FLOW Casino online.")`.

### OBSERVATION #2: `_orphan_reconciliation_loop` runs every 10 minutes with no jitter or backoff
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:348-356`
**Confidence:** 0.65
**Risk:** Fixed 10-minute interval with no error backoff. If `db.reconcile_orphaned_wagers` raises, the loop will retry on the next tick — but `tasks.loop` will fire even if there's a transient DB lock. Worse, no exception handler; if `reconcile_orphaned_wagers` raises, `tasks.loop` swallows it and only logs a generic error.
**Impact:** Transient DB lock storms can cause silent loop death. CLAUDE.md mentions "blocking calls inside async functions" — this is unrelated, but the adjacent risk is the same: silent loop death without admin signal.
**Fix:** Wrap the loop body in `try/except` with explicit `log.exception(...)` and consider `task.add_exception_type(...)` or restart logic.

### OBSERVATION #3: `tier` parameter shadowed in `_casino_jackpot_seed_impl`
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:632-638`
**Confidence:** 0.95
**Risk:** Line 634 reassigns `tier = tier.lower()` — fine, but the typing `tier: str` is unhinted to a `Literal["mini","major","grand"]`. So at the slash-command surface, the user can submit any string and only the runtime check at line 635 catches it. There is no `app_commands.Choice` constraint shown here (because this is an `_impl` method, not the slash command itself). If `boss_cog` ever passes raw input through, garbage gets to the DB code.
**Impact:** Type safety hole on a financial tool. No active exploit because boss_cog uses a select menu, but the contract is fragile.
**Fix:** Use `typing.Literal["mini","major","grand"]` as the parameter type.

### OBSERVATION #4: `daily_scratch` route bypasses casino-open check
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:269-272`
**Confidence:** 0.80
**Risk:** The `🎟️ Daily Scratch` button calls `daily_scratch(interaction)` without checking `await db.is_casino_open()`. The `casino_hub` slash command at line 366 does check `is_casino_open`, but the persistent hub view button (which is `add_view`'d at startup, line 340) does not. So a player can press the button on a stale ephemeral hub message after the casino has been closed and still claim a scratch.
**Vulnerability:** `daily_scratch` itself may or may not check; if it doesn't, the close switch is bypassable.
**Impact:** Closing the casino doesn't actually close scratches.
**Fix:** Add `if not await db.is_casino_open(): return await interaction.response.send_message(...)` to the `scratch` button handler before calling `daily_scratch`.

### OBSERVATION #5: `CasinoHubView.my_stats` reveals stats to the requesting user only — but no rate limit
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:274-320`
**Confidence:** 0.55
**Risk:** Stats query (`db.get_player_stats`) hits the DB on every button click. There's no rate limit or cache. The button is on a persistent view, so a botted user can spam it. Each call runs a SELECT over `casino_sessions`. Not a DoS in absolute terms but worth noting.
**Impact:** Minor DB hot-spot. The button is per-user-ephemeral so there's no data leak — the leak the focus block warns about is moot here.
**Fix:** Add a 5-second per-user cooldown via `discord.app_commands.checks.cooldown` or a manual timestamp dict.

### OBSERVATION #6: `embed.color` hardcoded to `0xD4AF37` in fallback path
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:408`
**Confidence:** 0.95
**Risk:** Magic color number bypasses `AtlasColors.CASINO` (which is used elsewhere in this same file at lines 286, 478, 554). Inconsistent palette source. If `AtlasColors.CASINO` ever changes, the fallback embed drifts.
**Impact:** Visual drift on the rare fallback path.
**Fix:** `color=AtlasColors.CASINO`.

### OBSERVATION #7: Bare `print(...)` debug in `cog_load` plus inconsistent log/print usage
**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino.py:337, 343, 352`
**Confidence:** 0.75
**Risk:** Mixing `print` (lines 337, 343) and `log.info` (line 352) for similar messages is confusing. The 10-minute periodic reconciliation logs to `log.info`, but the startup reconciliation uses `print`. Inconsistent observability.
**Impact:** Operators can't tail one log source and see all reconciliation activity.
**Fix:** Use `log.info` everywhere.

## Cross-cutting Notes

- **`refund_wager` in `casino_db.py`** (line 1089) is the actual root cause of CRITICAL #1's `reference_key` violation. Every caller of `refund_wager` (audit suggests at least one in this file plus callers in `casino/games/*.py`) should be re-audited to confirm they pass a `reference_key`. The function signature itself needs to require it, not just accept it.
- **Persistent `CasinoHubView` (`add_view` at line 340)** combined with `_send_wager_view`'s stale-balance read pattern is repeated in `crash_cmd`/`slots_cmd`/`blackjack_cmd` (lines 415-443) — every entry path into `WagerPresetView` shares the same TOCTOU exposure. Recommend a single `WagerPresetView.factory(uid, game)` that fetches balance/max_bet behind the lock.
- **Silent admin notifications** (lines 84, 95): the same fire-and-forget pattern likely exists in other casino subsystem files. Recommend a shared `_alert_admin(message)` helper that logs on failure and never falls into `pass`.
- **`_*_impl` no-permission-check pattern**: applies to all eleven impl methods in this file and likely the same pattern in other `*_cog.py` files that delegate to `boss_cog`. CLAUDE.md's "Admin delegation" pattern is intentional, but defense-in-depth should still be added at the impl level.
