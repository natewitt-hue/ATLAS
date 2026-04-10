# Adversarial Review: player_picker.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 446
**Reviewer:** Claude (delegated subagent)
**Total findings:** 15 (2 critical, 7 warnings, 6 observations)

## Summary

The module is a tidy, well-commented UI utility but it ships with two genuinely dangerous defects: (1) there is no `interaction_check` guard, so any user clicking a non-ephemeral picker can drive another user's callback — a cross-user authorization hole — and (2) the position filter dropdown silently deletes any team outside the first 23 alphabetical nicknames, so Ring 2 consumers (Oracle Scout, Genesis trade center) cannot reach roughly a third of the league via the UI. Several warnings cluster around stale state / TOCTOU, swallowed timeout exceptions in an admin view, and dev-trait icon lookup that misreads the data_manager schema.

## Findings

### CRITICAL #1: No `interaction_check` — any user can operate another user's picker

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:235-431`
**Confidence:** 0.92
**Risk:** The `PlayerPickerView` subclass of `discord.ui.View` never overrides `interaction_check()` and never stores an invoking `user_id` for enforcement. The constructor accepts a `user_id` parameter (line 258) but only uses it once, to *auto-default the team filter* (lines 270-273); it is never consulted to authorize subsequent interactions.
**Vulnerability:** The Oracle caller at `oracle_cog.py:3236-3237` uses `interaction.response.edit_message(...)` with no `ephemeral=True`, meaning the picker message is public. Any user who sees the Oracle Player Scout hub can click the dropdowns on someone else's in-flight picker and the cog's callback (`_do_player_scout`) will fire with the *other user's* interaction. For Genesis trade flow (genesis_cog.py imports on line 46) this is much worse: a cart-mode picker driven by user A can be submitted by user B, and the callback sees `interaction.user` = user B with user A's cart contents. Admins building trade packages or scouting reports become manipulable by any onlooker.
**Impact:** Cross-user action hijack on every non-ephemeral caller. In trade-adjacent callers this can be used to intentionally corrupt the commissioner's in-progress package before they submit, or to submit a cart prematurely on their behalf.
**Fix:** Override `interaction_check` to assert `interaction.user.id == self._user_id` (and store `self._user_id = user_id` in `__init__`); reject silently (`await interaction.response.defer(ephemeral=True)`) for mismatches. Make `user_id` effectively required for non-ephemeral callers; document that ephemeral callers may omit it. Also audit every `PlayerPickerView(...)` call site to confirm `user_id` is passed.

### CRITICAL #2: Silent data loss — 8+ teams unreachable from Team filter dropdown

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:128-160`
**Confidence:** 0.95
**Risk:** `_get_team_options()` deliberately truncates `sorted_teams[:_MAX_TEAMS]` where `_MAX_TEAMS = 23`. The league has ~31 active teams (per CLAUDE.md), so the last 8 teams alphabetically (e.g., Steelers, Texans, Titans, Vikings, …) are absent from the Team dropdown. The overflow row (lines 153-158) is a *non-selectable placeholder* with `value="__overflow__"` that the team-change callback treats as a no-op (line 364-366). It tells the user "use /players filter" — but there is no `/players filter` command in this codebase that a user can actually invoke to reach Steelers/Vikings players inside the Oracle Scout or Genesis trade flow. The fallback is a dead end.
**Vulnerability:** The module assumes the user will fall back to "/players" but that fallback does not exist in the call sites that use `PlayerPickerView` (Oracle `btn_player`, Genesis picker). Once those callers are reached, the user has no mechanism to filter to roughly a third of the league — the picker silently hides half the roster to those users.
**Impact:** Users cannot scout or trade for players on the last 8 alphabetical teams via any UI path that uses this picker. For a trade-center use case (multi-pick) this is worse than a crash — it turns the bot into a silent data lens that systematically hides late-alphabet teams.
**Fix:** Because Discord caps selects at 25, either (a) add a second team select row for teams 24-31 with a shared callback, (b) split team options into conference-grouped sub-dropdowns (NFC → pick → AFC), or (c) make position the primary filter and compute teams dynamically from the *filtered* roster so the 25-cap is much less likely to hit. Option (a) is the simplest; 31 teams + "All" fits in two 16-option selects comfortably.

### WARNING #1: `on_timeout` admin-view swallows all exceptions silently

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:422-430`
**Confidence:** 0.88
**Risk:** `except Exception: pass` (line 429-430) is a bare swallow with no `log.exception(...)`. Per CLAUDE.md Flow Economy Gotchas: "Silent `except Exception: pass` in admin-facing views is prohibited. Always `log.exception(...)`." `PlayerPickerView` is used by Genesis trade-builder and Oracle scouting — both admin-facing.
**Vulnerability:** On timeout, if the message was already deleted or the bot lost the channel, the attempted `message.edit(view=self)` will raise `discord.NotFound` / `discord.Forbidden` / `discord.HTTPException`. All are silently eaten. Worse, a successful call to `self.stop()` earlier could have produced a state where `self.message` is None or stale — the check `hasattr(self, "message") and self.message` doesn't catch that.
**Impact:** Silent failures in admin flows are exactly the class the rulebook exists to prevent. If the picker times out in a way that reveals a deeper bug (e.g., `self.message` is the wrong interaction's message), the operator never sees it.
**Fix:** Replace with `except discord.HTTPException as e: log.debug("timeout edit failed: %s", e)` (or `.warning`, per severity). Add a module-level `log = logging.getLogger(__name__)`.

### WARNING #2: TOCTOU between displayed player list and actual selection

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:276-277, 372-409`
**Confidence:** 0.82
**Risk:** `self._players` is snapshot at view construction, then refreshed on every filter change by re-running `_filter_players(...)`. But `_filter_players` calls `_all_players()` which reads `dm.get_players()` — a pointer into data_manager's live cache. If `data_manager.load_all()` runs between `_rebuild_items()` and `_on_player_select`, the `rosterId` the user clicked may no longer exist in the fresh data, or worse, may now point to a *different* player (if the rebuild reassigned indices). The lookup at line 378-382 searches `self._players` (the stale snapshot), so in multi mode you could push a *stale* player dict into `self._cart`.
**Vulnerability:** The cart is kept in memory on the view instance as full dicts, not just rosterIds, so the callback eventually receives snapshots of data that may no longer exist (retired, traded). A commissioner approving a trade 30 seconds later may commit a trade for a player the API already reports as traded.
**Impact:** Stale-state trade approvals; possible reference to a rosterId that no longer resolves in downstream trade-engine / ability-engine code.
**Fix:** At callback time, re-resolve the selected rosterId against `_all_players()` (not `self._players`) before invoking the user callback. If re-resolve fails, show "⚠️ Player data refreshed — please reselect" and rebuild.

### WARNING #3: `rosterId` collision via hash fallback generates non-deterministic keys

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:203, 380, 390`
**Confidence:** 0.85
**Risk:** When `p["rosterId"]` and `p["id"]` are both missing/None, `_build_player_options` falls back to `hash((_display_name(p), _ovr(p), p.get("position", "")))`. Two players with identical display name, identical OVR, and identical position (e.g., "John Smith / WR / 72 OVR" exists twice on different teams — a plausible sim-league scenario) will collide on a single SelectOption value. The subsequent selection lookup at line 380 uses the same fallback via `p.get("rosterId") or p.get("id") or id(p)` — but note: **the construction fallback uses `hash((...))` while the lookup fallback uses `id(p)` (Python object identity)**. These two values disagree for the same dict, so even if only one such player exists, the lookup will never find him when rosterId is missing.
**Vulnerability:** Rarely-used but broken code path: the picker swallows the error as "❌ Player not found." on line 384 — the user sees a generic error but the root cause (hash/id mismatch) is invisible.
**Impact:** Players without a `rosterId` or `id` are undisplayable (single mode) or cause phantom cart behavior (multi mode: the cart lookup at line 390 also uses `id(p)`, which differs per cart dict reference even for the "same" player).
**Fix:** Use a single deterministic function `_player_key(p)` — e.g., `str(p.get("rosterId")) or str(p.get("id")) or f"fb_{_display_name(p)}_{_ovr(p)}_{_pos_of(p)}"` — and use it in both build and lookup paths. Better: skip players with no stable identifier entirely and log a warning.

### WARNING #4: `_on_player_select` multi-mode dup-check uses wrong key format

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:389-392`
**Confidence:** 0.80
**Risk:** The dup-check compares `str(p.get("rosterId") or p.get("id") or id(p))` for cart entries against `selected_rid`. But cart entries were *added* with the same expression, so the `id(p)` fallback is the Python id of the *cart dict object* — which differs from the `id(p)` computed at `_build_player_options` time for the same player (that was a *different* dict copy from `_all_players()` if `_filter_players` ran twice). The dup-check will therefore *sometimes* pass for the same logical player, allowing duplicates in the cart for players without `rosterId`.
**Vulnerability:** Same class of bug as Warning #3 — inconsistent identity in fallback path. Most players have `rosterId`, so it hides under normal load.
**Impact:** Cart may contain silent duplicates of the same player when rosterId is missing; downstream trade package code may count a player twice.
**Fix:** Same as Warning #3 — single canonical `_player_key(p)`.

### WARNING #5: Dev-trait icon lookup reads the wrong field

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:122-124`
**Confidence:** 0.78
**Risk:** The icon dictionary keys are the *string* form `"Superstar X-Factor"`, `"Superstar"`, `"Star"`. Per `data_manager.py:630-653`, the `/export/players` CSV provides `devTrait` as an **integer** (0/1/2/3) and `dev` is only set to `"Normal"` if missing — it is NEVER derived from the numeric `devTrait`. So unless the CSV happens to also contain a string `dev` column (which CLAUDE.md's devTrait mapping implies it does not — CLAUDE.md says "devTrait mapping: 0=Normal, 1=Star, 2=Superstar, 3=Superstar X-Factor"), the icon lookup silently returns `""` for every player.
**Vulnerability:** Dead-weight code: the dev icon is intended to flag elite talent in the dropdown (a key UX signal for the Genesis trade center) but almost certainly renders nothing for 100% of players.
**Impact:** All the star icons users are supposed to see in the picker are invisible. Consumers of the picker get a degraded, undifferentiated list.
**Fix:** Map from `devTrait` int instead: `_DEV_ICON = {3: "⚡", 2: "★", 1: "✦"}` then `dev_icon = _DEV_ICON.get(int(p.get("devTrait") or 0), "")`. Keep the string-form as fallback to handle both schemas.

### WARNING #6: `interaction.data["values"]` accessed without existence check

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:357, 363, 373`
**Confidence:** 0.55
**Risk:** All three callbacks index `interaction.data["values"][0]` directly. discord.py populates `interaction.data` from the raw HTTP payload; if Discord ever sends a select interaction with an empty `values` list (observed historically on rapid re-submits in ephemeral messages), you get `IndexError`, which bubbles up as an unhandled callback exception.
**Vulnerability:** Unguarded index. An unhandled exception in a Discord callback is rendered as "This interaction failed" to the user — an opaque UX regression. Even if rare, Oracle's `_safe_interaction` wrapper is on the outer button, not on the picker's inner callbacks.
**Impact:** Opaque failure on a rare race. Low frequency but high confusion.
**Fix:** `values = (interaction.data or {}).get("values") or []; selected = values[0] if values else None; if not selected: return await interaction.response.defer()`.

### WARNING #7: `self.message` attribute never set — timeout no-op

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:422-430`
**Confidence:** 0.70
**Risk:** The timeout handler checks `hasattr(self, "message") and self.message`. But nothing in this file ever sets `self.message`. discord.py only sets `view.message` automatically when you do `await ctx.send(..., view=view)` (prefix-command Context) — **not** for `interaction.response.send_message(..., view=view)` or `interaction.response.edit_message(..., view=view)`, which is what every caller uses. So `hasattr(self, "message")` is almost always False and the timeout handler is a no-op: the disabled items are set in memory but never pushed to Discord, so the user still sees a clickable (but dead) view until Discord times the interaction out after 15 minutes.
**Vulnerability:** The view's timeout countdown (300s) vs Discord's implicit 15-minute interaction window means users have ~10+ minutes of clicking on a view that silently does nothing.
**Impact:** UX dead-end: users click a disabled picker with no visible feedback.
**Fix:** Either (1) explicitly store `self._interaction = interaction` in a `start(interaction)` method and use `await self._interaction.edit_original_response(view=self)` in `on_timeout`, or (2) document that callers must assign `view.message = await interaction.original_response()` after sending. Both require caller changes — pick (1) to localize the fix.

### OBSERVATION #1: Mutable shared default in convenience factories

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:435-446`
**Confidence:** 0.65
**Risk:** Factories accept `team_filter: str = "ALL"` which is fine (immutable), but do not pass through all `PlayerPickerView` params (e.g., `pos_filter`). Callers who want a pre-filtered position (e.g., "Only QBs for this HOF vote") must instantiate `PlayerPickerView` directly — the factory is incomplete.
**Fix:** Add `pos_filter` and other meaningful params (or use `**kwargs` pass-through).

### OBSERVATION #2: Private helpers imported by callers = leaked API surface

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:81-208` and `genesis_cog.py:1271, 1309`
**Confidence:** 0.90
**Risk:** `genesis_cog.py` imports `_get_pos_options`, `_filter_players`, `_build_player_options`, `_all_players`, `_display_name` — all underscore-prefixed helpers (convention: private). This makes the module's public API ambiguous: any refactor of the private helpers silently breaks Genesis without a tracker. There is no `__all__` to enforce boundary.
**Fix:** Promote the genuinely-shared helpers to public names (drop the underscore) or re-export them via `__all__`. Add a comment explaining which helpers are part of the contract.

### OBSERVATION #3: Module import of `roster` is tolerant but subtle

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:52-55`
**Confidence:** 0.60
**Risk:** The `try: import roster / except ImportError: roster = None` pattern is a soft fallback. But `roster` is a first-party module — ImportError means a hard install failure, not a missing optional dep. The pattern hides real errors (e.g., a circular import during cog reload) as "team auto-default silently disabled."
**Fix:** Either import unconditionally (fail-fast) or log at WARNING level on the fallback path so the operator notices degraded behavior.

### OBSERVATION #4: Magic number `25` is duplicated

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:147 (_MAX_TEAMS=23), 197 (players[:25]), 60 (comment)`
**Confidence:** 0.45
**Risk:** Discord's 25-option cap appears as `_MAX_TEAMS = 23` (silently "25 minus 2 for All+overflow"), as a bare `[:25]` on player results, and as a comment on POS_GROUPS. Any change in Discord's cap or in the overflow strategy requires finding all three sites. Not a bug now, but a landmine for the next refactor.
**Fix:** `DISCORD_SELECT_MAX = 25` as a module constant; derive everything else from it.

### OBSERVATION #5: `_player_label` label truncated to 100 characters — silent cutoff

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:125`
**Confidence:** 0.55
**Risk:** `label[:100]` is the Discord select-option label limit. Long player names + long team names + dev icon can push past 100, and the truncation happens silently — no ellipsis. Players like "Scooby Wright III" on the "Jacksonville Jaguars" with the X-Factor ⚡ prefix could lose the final OVR+team info. This isn't an assertion/data bug but it's a UX surprise the author probably didn't intend.
**Fix:** Truncate intelligently: if the label is too long, drop `"· OVR"` first, then `" · Team"`; or append `"…"` on any truncation.

### OBSERVATION #6: `_all_players()` caches nothing and calls `dm.df_players.to_dict(...)` on the hot path

**Location:** `C:/Users/natew/Desktop/discord_bot/player_picker.py:81-86, 136`
**Confidence:** 0.70
**Risk:** `_get_team_options()` calls `_all_players()`, which on the slow path iterates a Pandas DataFrame (`to_dict(orient="records")` is O(N) and allocates fresh dicts every call). This runs on every `_rebuild_items()` call, which runs on every dropdown change. For a ~1700-player roster (31 teams × 55) that's significant wasted work *inside* an async interaction callback — blocking the event loop.
**Fix:** Cache `_all_players()` result on the view instance in `__init__`, or add a module-level memoization keyed by `dm._state.last_sync_ts`.

## Cross-cutting Notes

- **Ring 2 theme: UI utilities without `interaction_check`** — this is the second Ring-2 file under review. The same `interaction_check`-missing pattern likely exists in other picker/cart views. Grep for `discord.ui.View` subclasses in `casino/`, `genesis_cog.py`, and `oracle_cog.py` that store user-specific state but don't override `interaction_check`.
- **Dev-trait schema drift:** `data_manager.py:630-653` exposes `devTrait` as numeric but sets `dev` only as a sentinel `"Normal"` string. Any UI code reading `p["dev"]` as a named tier ("Star", "Superstar X-Factor") is probably broken — audit `genesis_cog.py`, `ability_engine.py`, and any rendering code for the same assumption.
- **Fallback to `id(p)` as a key is a systemic anti-pattern:** if `player_picker.py` does it twice (with different values in build vs lookup), grep the codebase for `id(p)` or `id(player)` usages — likely to find similar bugs.
- **`view=None` risk:** Not directly violated here, but note that `current_embed()` is used in `edit_message(embed=..., view=self)` — the self-reference keeps the view alive across edits, which is correct. No finding, just a reminder for future refactors: never swap to `view=None`.
