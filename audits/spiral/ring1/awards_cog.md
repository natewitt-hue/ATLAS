# Adversarial Review: awards_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 119
**Reviewer:** Claude (delegated subagent)
**Total findings:** 18 (3 critical, 8 warnings, 7 observations)

## Summary

This file ships an anonymous voting subsystem with no permission gates, no persistent-view registration, and no durability guarantees for the ballot. Three critical holes — unauthenticated poll creation/close, unregistered persistent view, and blocking disk I/O on the event loop — plus a handful of UX and race hazards make the subsystem unsafe for a public league-wide ballot without hardening.

## Findings

### CRITICAL #1: No commissioner permission gate on poll create / close
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:87-114`
**Confidence:** 0.90
**Risk:** `_createpoll_impl` and `_closepoll_impl` do not call `is_commissioner()` (or any equivalent). They are described in the comment on line 85 as "used by /commish and deprecated wrappers," meaning permission enforcement is entirely delegated to the upstream caller. If any wrapper — the "deprecated wrappers" mentioned, `boss_cog`, or a future slash-command binding — forgets to gate the call, any member of the guild can create unlimited polls titled with abusive content, and any member who discovers a poll_id can close an active award vote early and publish premature results.
**Vulnerability:** The ADMIN_USER_IDS import on line 14 is unused inside this file. No `is_commissioner()` check, no `@app_commands.check`, no role gate. The implementation pattern per CLAUDE.md says "boss_cog delegates to `_impl` methods in target cogs; no direct logic is duplicated" — but that pattern also requires each `_impl` to assume its caller is trusted. Belt-and-suspenders defense (a second check inside `_impl`) is standard for destructive/public-facing operations and is absent here.
**Impact:** Unauthorized poll creation (spam/abuse with award-branded embeds sent publicly to the channel), unauthorized early poll closure (tally publication before commissioners intended), and tampering with league awards process. Because close is also a public post, an attacker can create a confusing parallel ballot stream.
**Fix:** Add an explicit check at the top of both `_impl` methods:
```python
from permissions import is_commissioner
if not is_commissioner(interaction):
    return await interaction.response.send_message(
        "Commissioner only.", ephemeral=True
    )
```
Do not rely on the wrapper layer alone.

### CRITICAL #2: VoteView persistent view is never registered with the bot
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:76-80, 117-119`
**Confidence:** 0.92
**Risk:** `VoteView` uses `timeout=None` and `VoteSelect` sets a stable `custom_id=f"vote_select:{poll_id}"` (line 55) — the standard discord.py pattern for a *persistent* view that survives bot restart. But the cog `setup()` (lines 117-119) never calls `bot.add_view(VoteView(...))` for each poll in `_polls`. On any restart, the dropdown on every previously-posted ballot message becomes an inert UI element: clicking it hits the "unknown interaction" error path or the fallback "bot may have restarted" message at line 62, even though `_polls` is reloaded from disk and the poll is still open.
**Vulnerability:** Lines 46 and 23-30 reload the poll state from JSON at import time, proving the author intended polls to survive restarts. The view is simply not re-attached. The user-facing behavior is that the dropdown visually remains on the posted embed but never fires a callback, so users cannot vote and get no clear explanation.
**Impact:** Silent loss of voter participation every time the bot restarts. Since this is an awards system (league-wide ballot), restart during a multi-day voting window disenfranchises everyone who clicks after the restart. The text at line 62 ("bot may have restarted") partially papers over it, but that message only fires if `_polls` doesn't contain the poll — which is the *opposite* of what happens here (polls are still present but the view is dead).
**Fix:** In `setup()`, after loading `_polls`, iterate and re-register:
```python
async def setup(bot):
    cog = AwardsCog(bot)
    await bot.add_cog(cog)
    for poll_id, poll in _polls.items():
        if poll.get("open"):
            bot.add_view(VoteView(poll_id, poll["options"]))
    log.info("ATLAS: Awards Engine loaded with %d persistent ballots.", len(_polls))
```

### CRITICAL #3: Blocking file I/O on the asyncio event loop
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:23-44, 46`
**Confidence:** 0.95
**Risk:** `_save_polls_sync()` performs synchronous `open`/`json.dump`/`os.replace` inside `_save_polls()` (lines 32-44), which is invoked from inside async coroutines on lines 73, 91, and 106. The lock is `asyncio.Lock`, but the body of the critical section runs on the event loop thread, not a worker thread. `_load_polls()` at line 46 also runs synchronously at module import time.
**Vulnerability:** Per CLAUDE.md "ATLAS-specific concerns" and the focus block: "Blocking calls inside `async` functions (sqlite3, requests, time.sleep). All blocking I/O must go through `asyncio.to_thread()` or use async libs." `json.dump` of a growing dict (every poll, every vote, serialized on every cast) is unbounded and will stall the event loop as votes accumulate. Under a flurry of votes during a league-wide award ballot (plausible for TSL's ~31 active teams voting simultaneously), latency on every other cog — casino, sportsbook, flow — spikes.
**Impact:** Event loop stalls under ballot load, cascading into every other subsystem (Discord heartbeats, flow_wallet calls, sentinel checks). At worst, the bot drops the gateway heartbeat and Discord disconnects. Because writes are serialized by `_polls_lock`, concurrent voters also queue on each other instead of fanning out.
**Fix:** Move the sync work into a thread:
```python
async def _save_polls() -> None:
    async with _polls_lock:
        await asyncio.to_thread(_save_polls_sync)
```
For `_load_polls()` at import time, accept the one-time hit but guard against re-entry during reload. Better: defer loading to `async def setup()` via `await asyncio.to_thread(_load_polls)`.

### WARNING #1: Silent exception swallow around atomic rename
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:32-39`
**Confidence:** 0.80
**Risk:** If `open(tmp, "w")`, `json.dump`, or `os.replace` raises (disk full, permission denied, tmp already exists on a crashed prior run), the exception is logged but the caller receives no indication that the vote was not persisted. The in-memory `_polls` dict still contains the mutation, so the user sees "✅ Vote recorded anonymously" (line 74) but the vote is lost on restart.
**Vulnerability:** The try/except on lines 33-39 is broad (`except Exception`). No return value, no raised flag, no caller-side check. Per focus block: "Silent `except Exception: pass` in admin-facing views is PROHIBITED." `log.exception` is better than bare `pass`, but the caller still cannot detect the failure and cannot tell the voter their vote was dropped.
**Impact:** Silent data loss on disk-write failures. On an award ballot that determines league outcomes, even one lost vote is reputation-damaging. Recovery requires manually replaying from logs if anyone notices.
**Fix:** Raise a wrapped exception so the callback can tell the user:
```python
def _save_polls_sync() -> None:
    tmp = _POLLS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_polls, f, indent=2)
        os.replace(tmp, _POLLS_PATH)
    except Exception as e:
        log.exception("Poll save error")
        raise RuntimeError("Failed to persist poll state") from e
```
Then in `VoteSelect.callback`, on exception, revert `poll["votes"].pop(uid, None)` and inform the user.

### WARNING #2: `_load_polls` silently returns empty dict on corrupt/malformed JSON
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:23-30`
**Confidence:** 0.85
**Risk:** If `polls_state.json` is corrupt (truncated write, disk error, manual edit typo), `json.load` raises `JSONDecodeError`, the broad `except` catches it, and `_load_polls` returns `{}`. Every existing poll and every vote is silently discarded; only a log line records the loss. The bot starts cleanly, no admin is alerted, the old polls vanish from the UI.
**Vulnerability:** Broad `except Exception` on line 28 with no recovery or escalation. No backup file, no `.bak` rotation, no retry with the previous state. The import-time load at line 46 means this happens before any admin channel is reachable.
**Impact:** Total awards-history wipe from a corrupt save (which is plausible if the bot crashes mid-write — although `os.replace` is atomic, previous versions or manual edits are not). Commissioners may not notice until voters complain.
**Fix:** Keep a `.bak` before overwriting in `_save_polls_sync` (three-file rotation: `polls_state.json.bak` → `polls_state.json.tmp` → `polls_state.json`). On load failure, attempt `.bak` before returning `{}`. Post an alert to `ADMIN_CHANNEL_ID` on fall-through.

### WARNING #3: `nominees.split(",")` produces empty-string and duplicate options
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:89`
**Confidence:** 0.88
**Risk:** `options = [n.strip() for n in nominees.split(",")]` does not filter empties, does not deduplicate, and does not validate length. Inputs like `"Alice, , Bob"`, `"Alice,,Bob"`, `"Alice,Alice,Bob"`, or `",Alice,"` all produce malformed option lists. Discord will reject the `SelectOption` with an empty `label` at runtime, which will surface as a modal failure when the `VoteView` is constructed at line 94, but the interaction will have already responded ("Poll created") at line 96, so the voter-facing message never posts.
**Vulnerability:** No input sanitization. No count check. No clarification that the UI caps at 25. An empty string is a valid Python list element but an invalid Discord `SelectOption`.
**Impact:** Half-created polls: the commissioner sees "Poll created" but the embed+view never posts. Poll persists in `_polls` orphaned. Duplicate options silently vote-share.
**Fix:**
```python
raw = [n.strip() for n in nominees.split(",")]
options = []
seen = set()
for n in raw:
    if n and n not in seen:
        options.append(n)
        seen.add(n)
if not (2 <= len(options) <= 25):
    return await interaction.response.send_message(
        f"Need 2-25 unique non-empty nominees (got {len(options)}).", ephemeral=True)
```

### WARNING #4: Silent truncation at 25 options in `VoteSelect.__init__`
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:51`
**Confidence:** 0.90
**Risk:** `opts = [... for opt in options[:25]]` silently drops the 26th+ nominees with no warning to the commissioner or voters. If the commissioner enters 30 nominees, the last 5 simply never appear in the dropdown. Votes are invalidated for those candidates with no indication that they existed.
**Vulnerability:** Per focus block: "Select menus capped at 25 options." The code acknowledges the cap by slicing but does not surface the truncation. `_polls[poll_id]["options"]` (set at line 90) stores ALL options, but the UI only shows 25 — meaning line 111's tally loop iterates the full original list, displaying "0 votes" for entries voters couldn't even pick.
**Impact:** Nominations silently deleted. Awards ballot integrity broken.
**Fix:** Validate in `_createpoll_impl` (see fix in WARNING #3) and refuse to create polls >25. Do not silently truncate in the UI layer.

### WARNING #5: Tally ignores votes for options not in the original list
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:107-111`
**Confidence:** 0.75
**Risk:** The tally at line 107-109 iterates over `_polls[poll_id]["votes"].values()` and bucketizes. But the results string at line 111 iterates over `_polls[poll_id]["options"]`. Any vote whose value is not in the `options` list is counted in `tally` but never displayed in the results, silently discarded from the summary. This happens if: (a) the options list was mutated between create and close, (b) the JSON file was manually edited, or (c) the 25-option truncation dropped some values but votes for them existed from an earlier state.
**Vulnerability:** The display loop uses `[opt for opt in options]` rather than iterating the tally directly.
**Impact:** Stealth vote-dropping during result publication. Voters see a results panel that doesn't add up.
**Fix:** After displaying the official options, append any orphaned tally entries as "Other: X votes" — or fail loud if the tally has unknown keys.

### WARNING #6: Poll close does not disable or remove the vote view
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:101-114`
**Confidence:** 0.80
**Risk:** `_closepoll_impl` sets `open = False` and posts results, but the previously-posted embed+view (from `_createpoll_impl` line 99) stays interactive. Users continue to see the dropdown and attempt to vote. They hit the "❌ This poll is closed" path at line 66 — functional, but confusing, and leaves the dropdown visible forever.
**Vulnerability:** No tracking of the posted `discord.Message` handle. `_createpoll_impl` sends the view and discards the return value, so `_closepoll_impl` has no way to edit the original message to disable the view.
**Impact:** UX rot. Long-term, every closed poll leaves a zombie dropdown in the channel.
**Fix:** Capture `msg = await interaction.channel.send(embed=embed, view=view)` at line 99 and persist `msg.id` + `msg.channel.id` into `_polls[poll_id]`. In `_closepoll_impl`, fetch and edit the message with `view=None` and an updated embed showing results.

### WARNING #7: Anyone who guesses/knows the poll_id can close the poll
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:101-106`
**Confidence:** 0.72
**Risk:** Same root cause as CRITICAL #1 but with a distinct impact vector: `_closepoll_impl` accepts any `poll_id` string and closes it with no check beyond "does it exist." Combined with 8-character UUID slices (line 88: `str(uuid.uuid4())[:8]`), the search space is 32 bits. An attacker inside the guild can brute-force or scrape poll_ids from `/awards` command autocomplete (if one exists) and close ballots early.
**Vulnerability:** No ownership tracking on polls — no `created_by` field in the poll dict, and no gate in `_closepoll_impl`. The fix in CRITICAL #1 (commissioner check) solves this, but this finding is listed separately to emphasize the data-model gap.
**Impact:** Premature poll close, potentially before all voters have cast. Announced results publicly.
**Fix:** Per CRITICAL #1, require commissioner. Additionally, persist `created_by` in the poll dict and optionally allow the creator to close.

### WARNING #8: Duplicate poll_id collision is silently possible
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:88-90`
**Confidence:** 0.60
**Risk:** `poll_id = str(uuid.uuid4())[:8]` gives ~32 bits of randomness. Birthday collision probability becomes non-trivial around ~65K polls, which is not an immediate concern at TSL scale, but there is no check: `_polls[poll_id] = {...}` will silently overwrite an existing poll at line 90 on collision, wiping any votes already cast.
**Vulnerability:** No `while poll_id in _polls: poll_id = str(uuid.uuid4())[:8]` guard.
**Impact:** Rare but total vote loss on collision. More importantly, the 8-char ID is short enough to be guessed in ballot-tampering scenarios (see WARNING #7).
**Fix:** Either use the full UUID (`str(uuid.uuid4())`) — 128 bits — or add a uniqueness check + retry.

### OBSERVATION #1: Unused `ADMIN_USER_IDS` import
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:14`
**Confidence:** 0.95
**Risk:** `from permissions import ADMIN_USER_IDS` is imported but never referenced in the file. Dead code.
**Vulnerability:** Import survives but gives the false impression that some admin gating is happening. A casual reviewer might assume there's a permission check they're missing.
**Impact:** Misleading code. No functional impact.
**Fix:** Remove the unused import, or (preferred) add the missing commissioner check and use it.

### OBSERVATION #2: `# bug-6` marker comments left in source
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:18, 32, 42, 73, 91, 106`
**Confidence:** 0.95
**Risk:** Six `# bug-6` inline comments tag the locking work as an in-flight bug fix. These should be removed once merged — they add no maintenance value and imply the code is still in a fix state.
**Vulnerability:** Maintenance debt / noise.
**Impact:** Cognitive load for future readers. No runtime impact.
**Fix:** Remove all `# bug-6` comments or convert to proper docstring explaining the lock contract.

### OBSERVATION #3: Module-level `_polls` global mutable state
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:46`
**Confidence:** 0.70
**Risk:** `_polls: dict = _load_polls()` is a module-level global. If the cog is reloaded (hot-reload via extension unload/load), Python's import cache may or may not re-execute this depending on whether the module itself was reimported. This creates a hazard where in-memory polls drift from file state across reloads.
**Vulnerability:** Global mutable state tied to module import lifecycle rather than cog lifecycle.
**Impact:** Subtle state bugs after `/reload` or cog hot-swap.
**Fix:** Move `_polls` into `AwardsCog.__init__` as `self._polls = _load_polls()` so state is explicitly re-initialized on cog reload.

### OBSERVATION #4: Import-time disk I/O on line 46
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:46`
**Confidence:** 0.85
**Risk:** `_polls: dict = _load_polls()` executes at module import time, which is during the blocking `setup_hook` cog-load phase. If the JSON file is large, bot startup is delayed.
**Vulnerability:** Not a concurrency issue (nothing else is running at import time), but a latency regression as poll state grows.
**Impact:** Slower cold starts; grows unbounded.
**Fix:** Defer the load to `setup()` via `await asyncio.to_thread(_load_polls)`.

### OBSERVATION #5: No docstrings on public methods
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:83, 87, 101`
**Confidence:** 0.80
**Risk:** `AwardsCog.__init__`, `_createpoll_impl`, `_closepoll_impl`, `VoteSelect.callback` have no docstrings describing their contract (who calls them, permission expectations, side effects).
**Vulnerability:** Maintenance / onboarding. Future callers may re-implement permission gates incorrectly.
**Impact:** Low, but compounds with CRITICAL #1.
**Fix:** Add brief docstrings specifying "commissioner-only; caller must verify permission" (until CRITICAL #1 is fixed internally).

### OBSERVATION #6: Emoji-prefixed error messages inconsistent with ATLAS voice
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:63, 66, 70, 74`
**Confidence:** 0.40
**Risk:** Uses `❌`, `⚠️`, `✅` emoji in error/success messages. CLAUDE.md echo persona section says ATLAS speaks "always 3rd person as 'ATLAS'" and the wider ATLAS voice is "punchy, detached omniscient." Emoji-first messages are a minor voice drift.
**Vulnerability:** Stylistic / branding.
**Impact:** Negligible; noted for voice consistency review.
**Fix:** Replace with ATLAS-voice text: `"ATLAS rejected the vote: poll not found."` etc. Non-blocking.

### OBSERVATION #7: Reference to `/awards` command that does not exist in this file
**Location:** `C:/Users/natew/Desktop/discord_bot/awards_cog.py:62-63`
**Confidence:** 0.55
**Risk:** The error message says "Use `/awards` to start a new vote" — but this file defines no `@app_commands.command()` named `awards`. Either the command lives in another cog (not verified here; would need a grep) or the message is stale and refers to a removed command. Per CLAUDE.md this file is listed as "awards_cog.py | Awards & voting | awards_cog.py" with no `/awards` slash binding visible.
**Vulnerability:** Stale UX copy. If the `/awards` command was removed during the `/commish` → `/boss` migration, users receive broken guidance.
**Impact:** User sends them in circles. Low direct impact but erodes trust.
**Fix:** Verify `/awards` exists elsewhere (e.g., `boss_cog`). If not, update the message to point at the correct command path, such as `"/boss → Awards"`.

## Cross-cutting Notes

- **`_impl` delegation pattern needs belt-and-suspenders permission checks.** Any cog that exposes commissioner-only logic through `_impl` methods (boss_cog delegate targets) should self-gate. This file is an example of what can go wrong when the entire permission contract is pushed upstream.
- **Persistent view registration pattern.** Any cog that uses `timeout=None` views with stable `custom_id`s must iterate saved state in `async def setup()` and call `bot.add_view(...)` for each. This is a subsystem-wide concern — other cogs with persistent interactive UI (e.g., flow sportsbook hubs, casino play-again buttons) should be audited for the same omission.
- **Durability of in-memory + JSON state.** The "mutate dict, then fire-and-forget save" pattern trades durability for speed. Any subsystem using this pattern (and there may be several, per the module map) should document the consistency contract or migrate to the SQLite-backed pattern used by `flow_wallet` / `wager_registry`.
