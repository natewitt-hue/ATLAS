# Adversarial Review: sentinel_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 3101
**Reviewer:** Claude (delegated subagent)
**Total findings:** 23 (3 critical, 10 warnings, 10 observations)

## Summary

Sentinel is a large "God cog" that bundles six loosely-related enforcement subsystems and stores their state in three JSON files plus a shared `parity_state.json` owned by `genesis_cog`. The persistence layer has race conditions and permission checks on commissioner-only actions are inconsistent — in particular, the position-change approval/denial helpers accept any user, the force-request `_finalize` can post ruling embeds twice on interaction retry, and the `_next_id` counter is not locked. The 4th-down analyzer also uses a hand-rolled sync `httpx.get` for image fetches and the default `AUTO_DETECT` listener is wired but left empty. Fix the permission gaps and the TOCTOU windows before shipping.

## Findings

### CRITICAL #1: `positionchangeapprove_impl` / `positionchangedeny_impl` have no permission check

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:2179-2254`
**Confidence:** 0.95
**Risk:** Any user — not just a commissioner — can call `positionchangeapprove_impl(log_id)` / `positionchangedeny_impl(log_id, reason)` and mutate the position-change record. The methods do not call `is_commissioner(interaction)` before writing `record["status"] = "approved"` / `"denied"` and posting the public `#roster-moves` announcement.
**Vulnerability:** The docstring in the module header explicitly labels these as `[Admin]` commands, and the embed text at line 2162-2164 instructs admins to "use `/positionchangeapprove` or `/positionchangedeny`". But the `_impl` methods are bare and are invoked from the outer cog's slash command wrapper, which is not shown to wire a permission decorator. Without an inline `if not await is_commissioner(interaction): return await ...` guard, the impl-to-wrapper contract is fragile — a single missing decorator anywhere (boss_cog delegate, another cog re-calling `_impl`, etc.) silently drops enforcement. Compare to `_guilty_callback` at line 625 which correctly inlines the check.
**Impact:** A rogue owner can self-approve their own pending position change (bypassing the `requires_approval=True` gate on e.g. `HB → FB`, `WR → HB`, `HB → WR`) and publish an official `_announcement_embed` to `#roster-moves`. The position-change record is written to disk via `_save_state()` and becomes the canonical history. No audit trail records who actually invoked the command versus what the record says (`approved_by = str(interaction.user)` — but there's nothing to verify this matched a commissioner).
**Fix:** Add the same inline gate used in `RulingPanelView._guilty_callback`:
```python
if not await is_commissioner(interaction):
    return await interaction.response.send_message(
        "❌ Commissioners only.", ephemeral=True
    )
```
at the top of both `positionchangeapprove_impl` (line 2184) and `positionchangedeny_impl` (line 2217), before the `_find_pending` lookup.

### CRITICAL #2: `_finalize` TOCTOU + double-post on ruling embeds

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:1045-1108`
**Confidence:** 0.85
**Risk:** `_finalize` uses `self._acted` (a per-view-instance boolean) as the only idempotency guard. When a force-request view is re-registered on restart via `ComplaintCog.__init__` / `ForceRequestCog.__init__`, a **new** `ForceRequestAdminView` instance is created with `self._acted = False` (line 997). If the admin clicks "Approve" twice quickly, or if discord.py retries the interaction (which happens when the gateway flaps), both clicks land on the same view instance but `_acted=True` is set only AFTER `await interaction.response.defer()` on line 1058 — wait, no: `_acted=True` is set on line 1055, before the defer on line 1058. Good. BUT: the actual TOCTOU is that `_force_requests[self.request_id]["status"]` is ONLY updated on line 1107 at the END of `_finalize`, AFTER the results embed has been posted on line 1080 and the requester DM sent on line 1092. If `_finalize` is called a second time on a DIFFERENT view instance (restart, persistent view re-registration) after a restart mid-ruling, the second call will observe `status == "pending"` and re-post the ruling embed and re-DM the requester. There is no check of the persisted `_force_requests[rid]["status"]` inside `_finalize` — only the in-memory `self._acted` flag, which is per-instance.
**Vulnerability:** The `self._acted` flag is in-process state that does NOT survive restart. The persistent `_force_requests[rid]["status"]` JSON field IS the durable truth, but it's only consulted for reconstruction purposes in `ForceRequestCog.__init__` (line 1209), not inside `_finalize`. A view created from a "pending" JSON record will always start with `_acted=False` and will happily execute `_finalize` a second time on the same underlying request if a commissioner clicks a persistent button after a restart that interrupted the first ruling mid-write.
**Impact:** Duplicate result embeds posted to `#force-request`, duplicate DMs to requester, duplicate audit log entries. Worse, the result embed publicly names the requester's opponent as "at fault" — posting it twice compounds the reputation damage. Also, the second `_finalize` will overwrite `_force_requests[rid]["status"]` from whatever the first call set it to, with whatever the second call sets it to — if a commissioner first approved and then denied (two different button presses across a restart), the public channel will see two contradicting rulings.
**Fix:** At line 1053, check both `self._acted` AND the persisted state:
```python
if self._acted:
    return await interaction.response.send_message("Already decided.", ephemeral=True)
persisted = _force_requests.get(self.request_id) or {}
if persisted.get("status") not in (None, "pending"):
    return await interaction.response.send_message(
        f"Already ruled: {persisted['status']}", ephemeral=True
    )
self._acted = True
```
Apply the same guard in `_deny_callback` at line 1145.

### CRITICAL #3: `ForceRequestCog._next_id` race condition under concurrent `/forcerequest` calls

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:1222-1240`
**Confidence:** 0.80
**Risk:** `_next_id` increments `self._request_counter`, calls `_save_counter()` (sync file I/O), and returns `f"{ts}-{self._request_counter:03d}"`. There is no lock, and `/forcerequest` (line 1255) is a slash command that multiple users can invoke concurrently. If two commands start their Python context switch between the `self._request_counter += 1` on line 1237 and `self._save_counter()` on line 1238, two requests can be assigned the same ID, OR the counter file can be written in an inconsistent order. Worse, `_save_counter()` does synchronous file I/O inside the main event loop (line 1232: `with open(tmp, "w") as f`). Under contention, one request can overwrite the counter another request was relying on.
**Vulnerability:** Python GIL protects the `+=` itself, but not the compound write-to-file. Between line 1237 (`self._request_counter += 1`) and line 1238 (`self._save_counter()`), an `await` elsewhere in the same event loop iteration can swap in another handler that increments again and writes first. Additionally, `_save_counter()` is sync blocking I/O in an async handler — it will halt all other interaction handlers briefly, and its `os.replace(tmp, FR_COUNTER_PATH)` is not atomic with the preceding counter increment.
**Impact:** Two force requests get the same ID, causing the second to silently overwrite the first in `_force_requests[rid]`. The admin review embed for request #1 disappears after request #2 is filed. The requester for #1 never gets a decision because the view persistence code will rebuild ONE view keyed on the shared rid. Data loss of a pending ruling that would require commissioner re-filing.
**Fix:** Guard `_next_id` with an `asyncio.Lock` and move `_save_counter` off the event loop:
```python
self._counter_lock = asyncio.Lock()
...
async def _next_id(self) -> str:
    async with self._counter_lock:
        self._request_counter += 1
        await asyncio.to_thread(self._save_counter)
        ts = dt.now(timezone.utc).strftime("%m%d")
        return f"{ts}-{self._request_counter:03d}"
```
And update the caller at line 1276 to `await self._next_id()`.

### WARNING #1: `_prune_resolved_complaints` mutates `_complaints` during save under lock

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:139-158`
**Confidence:** 0.70
**Risk:** `_save_complaint_state` calls `_prune_resolved_complaints()` on line 150 BEFORE acquiring the `_complaint_file_lock` on line 151. Concurrent calls to `_save_complaint_state()` can both execute `_prune_resolved_complaints()` simultaneously (iterating and `del`ing the same dict), producing a `RuntimeError: dictionary changed size during iteration` from the module-level `_complaints` dict.
**Vulnerability:** Line 140-144 builds `to_remove` via a list comprehension (safe), but line 145-146 iterates and `del _complaints[cid]` — if another coroutine reaches the same function, two iterations of the list may both attempt to `del` the same key on the second pass, or the list comprehension may race with a `_complaints[cid] = ...` assignment in `ComplaintModal.on_submit` at line 370. The lock protects the file I/O but not the in-memory dict mutation.
**Impact:** KeyError on `del _complaints[cid]` in the second coroutine after the first already pruned, or a mid-submission complaint being pruned before its thread_id can be written back on line 400.
**Fix:** Move `_prune_resolved_complaints()` INSIDE the lock:
```python
async def _save_complaint_state():
    async with _complaint_file_lock:
        _prune_resolved_complaints()
        try:
            ...
```

### WARNING #2: `_get_channel` with None channel ID crashes silently

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:1311, 1388, 2142, 2206, 2241`
**Confidence:** 0.75
**Risk:** Multiple call sites fetch a channel via `self.bot.get_channel(_review_channel_id())` or similar. When `_review_channel_id()` returns `None` (admin_chat not configured, setup_cog not loaded), `self.bot.get_channel(None)` raises `TypeError: argument of type 'NoneType' is not iterable` inside discord.py's lookup, depending on version.
**Vulnerability:** Line 1311: `review_ch = self.bot.get_channel(_review_channel_id())` — no guard. Line 1388: `ch = self.bot.get_channel(_review_channel_id())` — no guard. Line 2142 and 2206 and 2241 all guard with `if _roster_moves_channel_id()` first, which is safer, but the force-request path does NOT. If admin_chat is unconfigured, `/forcerequest` will crash after successful Gemini analysis, destroying 20 seconds of work with no user-visible error handling.
**Impact:** User pays for an AI call, sees no result, no retry. Counter still increments. Orphaned `_force_requests[rid]` record with no review channel to serve it from.
**Fix:** Coerce `None` → `0`:
```python
review_ch = self.bot.get_channel(_review_channel_id() or 0)
```
and then check `if not review_ch: ...` as already done at line 1312. Same fix for line 1388.

### WARNING #3: `_validate_image_url` allowlist too narrow — breaks legitimate discord URLs

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:60-64`
**Confidence:** 0.70
**Risk:** `_ALLOWED_IMAGE_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}` does not include any Discord hostnames with subdomains. Discord's CDN occasionally returns alternate hostnames like `images-ext-1.discordapp.net` for proxied image uploads. Attachments from some regions/gateway versions can have `.url` pointing to these.
**Vulnerability:** Line 1323-1331 fetches `screenshot1.url`, `screenshot2.url`, `screenshot3.url` via `_validate_image_url()`. If any attachment URL is rejected, `files` will be empty and the admin review channel gets the embed without the actual screenshots attached. The admin cannot re-review evidence.
**Impact:** Force request review is made without the visual evidence commissioners actually need. Ruling quality degrades, commissioner frustration.
**Fix:** Expand allowlist or do suffix-match: `hostname.endswith(".discordapp.net") or hostname == "cdn.discordapp.com"`. Also log the rejected URL so admins know why evidence is missing.

### WARNING #4: `_analyze_screenshots` silently downloads 30 MB per image with no size cap

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:828-838`
**Confidence:** 0.75
**Risk:** The httpx client is created with `timeout=30` but no `follow_redirects=False` and no content-length cap. A malicious user can upload a legitimate-looking Discord attachment URL that returns a 2 GB file, which is then entirely downloaded into memory via `resp.content` and base64-encoded to the AI API.
**Vulnerability:** Line 832: `resp = await client.get(url)`. No streaming, no `httpx.HTTPStatusError` for large content-length. Memory exhaustion is possible on a single force request.
**Impact:** OOM on the bot host, entire ATLAS process crashes, all cogs unavailable until restart.
**Fix:** Use streaming + max-size check:
```python
async with client.stream("GET", url) as resp:
    resp.raise_for_status()
    if int(resp.headers.get("content-length", 0)) > 10 * 1024 * 1024:
        raise ValueError("Image too large")
    content = await resp.aread()
```
Apply to both `_analyze_screenshots` (line 828) and the evidence-attach path (line 1322). Also apply to the sync fetcher `_fetch_image_bytes` at line 2554-2564.

### WARNING #5: Gemini system prompt hardcoded in `_get_system_prompt` instead of `get_persona`-derived

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:757-806`
**Confidence:** 0.65
**Risk:** CLAUDE.md says: "Use `get_persona()` from `echo_loader.py` for AI system prompts — never hardcode `ATLAS_PERSONA`". `_get_system_prompt()` does call `get_persona("official")`, which is fine. However, line 2276-2502 (`SYSTEM_PROMPT` for the 4th down analyzer) is a hardcoded multi-line string — and the inline comment at line 2272-2275 says this is "a documented exception to the convention". The exception is unilateral and there is no mechanism to update this prompt without a code deploy. If the referee prompt drifts from the ATLAS persona's tone, there's no way to re-sync.
**Vulnerability:** This isn't a bug per se, but it's a fragile contract with `echo_loader` that's only enforced by convention. A new developer will see the 4th down prompt, assume the same pattern is fine for future features, and start scattering hardcoded prompts across the codebase.
**Impact:** Tech debt, no operational lever to quickly retune referee behavior without a git push + bot restart.
**Fix:** Move `SYSTEM_PROMPT` to `echo/fourth_down_ref.txt` and load via `get_persona("fourth_down_ref")` or similar. If the 4th Down Referee must stay separate, extract it to a named constant in a dedicated module (e.g., `fourth_down_prompt.py`) so it's not buried inside sentinel_cog.

### WARNING #6: `_load_state` / `_load_fr_state` use non-atomic read with no schema validation

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:129-137, 165-171`
**Confidence:** 0.60
**Risk:** `_load_state` reads `complaint_state.json` and assigns to `_complaints` globally. If the file is corrupt, the exception is printed to stdout and `_complaints` remains `{}` — silently losing ALL history. `_load_fr_state` does the same for force requests.
**Vulnerability:** Line 135 catches `Exception` and prints — no backup, no recovery, no alert to commissioners. A single bad write (e.g., disk full mid-write, process killed during `_save_complaint_state` between `open()` and `json.dump`) can wipe the entire state on next load.
**Impact:** Complete loss of pending complaints and force requests on corrupted load. Cases in progress vanish.
**Fix:** On load failure, try to load `STATE_PATH + ".tmp"` (the pre-atomic-replace file) as a fallback, and copy the corrupt file to `STATE_PATH + ".corrupt-{timestamp}"` for forensics. Log to `admin_chat` on load failure.

### WARNING #7: `validate_position_change` does not handle missing `_ops` key

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:1752-1755, 1810`
**Confidence:** 0.60
**Risk:** Line 1754 defines `_ops(op)` returning a dict lookup, raising `KeyError` on an unknown op. POSITION_RULES is a static dict so in practice it should be safe, but there's no test or validation. A new rule added with a typo (`>=` → `=>`) would crash `validate_position_change` at line 1810 when evaluating the player, and the whole position change would abort with an unhandled exception — no graceful fallback.
**Vulnerability:** `_ops` has no default, no KeyError handler. The caller `validate_position_change` has no try/except around it.
**Impact:** Bad rule definition blows up all position changes until fixed. Commissioner has to diff two versions of the file to find the typo.
**Fix:** Add a startup-time validation of POSITION_RULES. For each rule, iterate its checks and verify `op in _ops._valid_ops`. Fail fast at cog load.

### WARNING #8: Sync `_save_state` inside async context (position change)

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:2139, 2197, 2231, 1508-1518`
**Confidence:** 0.75
**Risk:** The position-change subsystem imports `_save_state` from `genesis_cog` (line 1490), which is a **synchronous** function. It's called from inside async handlers at lines 2139, 2197, 2231 without `asyncio.to_thread`. This blocks the event loop during JSON write. Under contention with other cogs' async handlers, the bot stalls briefly.
**Vulnerability:** `_save_state` in both genesis_cog and the fallback at line 1508 uses blocking file I/O. ATLAS's `_startup_done` safety rule and CLAUDE.md explicitly warn against blocking calls inside async functions.
**Impact:** Minor latency hiccups under load. A slow disk (SSD nearing full) or NFS mount will create visible lag in Discord interactions.
**Fix:** Wrap the call: `await asyncio.to_thread(_save_state)`. Or make `_save_state` async with an `asyncio.Lock` similar to `_save_complaint_state`.

### WARNING #9: Persistent view re-registration for complaints races with saved state

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:671-677`
**Confidence:** 0.55
**Risk:** `ComplaintCog.__init__` calls `_load_state()` (sync) on line 673, then iterates `_complaints.items()` on line 675-677. If `_load_state` hasn't loaded yet (race with another cog's import), the re-registration will skip all pending complaints. More subtly, if a complaint is saved after load but before re-registration, it won't be registered as a persistent view.
**Vulnerability:** `ComplaintCog` is instantiated per call to `setup()`. If the bot reconnects and `setup` runs again, `_load_state` re-executes, which is OK because `_complaints` is reassigned. But `bot.add_view` is called for each pending complaint without checking if a view for that cid is already registered.
**Impact:** Duplicate persistent views for the same complaint after a reconnect. Double-ruling possible if both views attach to the same custom_id.
**Fix:** Track a module-level set `_registered_complaint_views = set()` and guard `bot.add_view` with `if cid not in _registered_complaint_views: _registered_complaint_views.add(cid); bot.add_view(...)`.

### WARNING #10: `_resolve_accused` fuzzy match can pick wrong member in large guilds

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:194-224`
**Confidence:** 0.55
**Risk:** Line 220-224: `partial = discord.utils.find(lambda m: raw.lower() in m.name.lower() or raw.lower() in m.display_name.lower(), guild.members)`. A 2-character input like `ro` matches the FIRST member whose name contains "ro" — which could be "Aaron", "Rob", "Rocky", etc. No priority, no "did you mean" disambiguation.
**Vulnerability:** A complaint against "Ron" filed as `ro` might silently file against Aaron Rodgers or someone else entirely, DMing them and creating a public thread with their name attached.
**Impact:** Wrong user is accused in a public channel + DM. Reputation damage, need to delete the complaint manually.
**Fix:** Require at least 4 characters for partial match, or return multiple matches for user disambiguation. Prefer resolving via `tsl_members` registry (per CLAUDE.md: "single source of truth for mapping Discord names").

### OBSERVATION #1: `complaint_state.json` dictionary has no schema version

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:126, 163`
**Confidence:** 0.70
**Smell:** The `_complaints` dict has no `version` key. If the format changes (adding new fields like `evidence_urls` or `reviewed_at`), `_load_state` has no way to migrate old records. The current code just trusts whatever shape is on disk.
**Impact:** Migrations are done silently via `.get()` with defaults, which works but hides schema drift. Hard to debug.
**Fix:** Add a `_SCHEMA_VERSION = 1` constant at the top, store it as a top-level key in `complaint_state.json` (alongside the complaints), and gate loading on version. Same for `force_request_state.json`.

### OBSERVATION #2: Magic numbers scattered throughout

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:390, 1464, 1450-1453, 140, 175`
**Confidence:** 0.80
**Smell:** `auto_archive_duration=10080` (7 days, line 390), `450`/`225` yard thresholds (line 1464), `35`/`28` blowout margin thresholds (lines 1450, 1452), `30 days` prune (line 140), `7 days` force-request prune (line 175). No named constants.
**Impact:** Hard to tune league rules without searching for magic numbers.
**Fix:** Extract to a `SENTINEL_CONFIG` dict at module top.

### OBSERVATION #3: `traceback.print_exception` instead of `log.exception`

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:1294, 2741, 2789, 2829, 2871, 2877, 2941, 2955, 2969, 2985, 2997, 3015`
**Confidence:** 0.85
**Smell:** Throughout the hub views and error handlers, errors are printed to stdout via `traceback.print_exception` or `traceback.print_exc`. This goes to the console but not to any log file, not to the admin channel, and not to a structured logger.
**Impact:** Admin-facing views swallow errors visibly in stdout only. Debugging production issues requires SSH access to the bot host.
**Fix:** Use Python's `logging` module: `log.exception("sentinel: …")` routes to a consistent log pipeline, and failures can be surfaced to the `admin_chat` channel via a shared error handler.

### OBSERVATION #4: `_acted` gates are inconsistent across views

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:583-665, 1045-1055`
**Confidence:** 0.60
**Smell:** `RulingPanelView` checks `c.get("verdict") not in (None, "pending")` AND `self._acted`. `ForceRequestAdminView._finalize` only checks `self._acted`. Inconsistent: one uses persistent state, the other only in-memory.
**Impact:** See CRITICAL #2 — the force-request path is more vulnerable. Tests for idempotency need to be written for both.
**Fix:** Standardize on a "check persisted state AND self._acted" pattern. Extract to a shared helper.

### OBSERVATION #5: `AUTO_DETECT_CHANNEL_IDS = set()` is dead code by default

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:2268, 2664-2703`
**Confidence:** 0.85
**Smell:** The `on_message` listener at line 2664-2703 iterates and checks `AUTO_DETECT_CHANNEL_IDS`, which is an empty set by default. This means the listener fires on every non-bot message in the guild, performs two comparisons, and returns. It's essentially dead code wasting ~15 branches per message.
**Impact:** Performance: every message in the guild pays a small cost. More importantly, the code path is untested by default — nobody would notice if it broke.
**Fix:** Remove the listener entirely if the feature isn't in use, or move the feature to a proper config setting and document it in `setup_cog`.

### OBSERVATION #6: `FourthDown._fetch_image_bytes` uses sync `httpx.get` inside executor

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:2554-2564, 2650-2652`
**Confidence:** 0.75
**Smell:** The sync httpx client is used inside a `run_in_executor` wrapper. The codebase already uses `httpx.AsyncClient` in `_analyze_screenshots` at line 828. Using a sync client inside `run_in_executor` is technically correct but fragmented — two different HTTP paths for the same subsystem.
**Impact:** Maintenance burden. If httpx is upgraded and its sync client behavior changes, this module has two different invocations to audit.
**Fix:** Consolidate on `httpx.AsyncClient` and call `client.get(url)` directly in the async handler.

### OBSERVATION #7: `_is_madden_screenshot` has a brittle `or` chain

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:2593-2596`
**Confidence:** 0.65
**Smell:** The function signature returns a ternary where the condition evaluates `attachment.content_type in (...)` if content_type is truthy, ELSE falls back to `filename.lower().endswith(...)`. The ternary is one line and hard to read, and it doesn't handle the case where content_type is present but wrong (e.g., `image/bmp` — valid image but not in the list).
**Impact:** Valid screenshots of uncommon image types are rejected.
**Fix:** Rewrite as two explicit checks and log the rejection reason.

### OBSERVATION #8: Position change "Slot WR" variant is a comment not a rule

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:1729-1744`
**Confidence:** 0.75
**Smell:** The comment explicitly says `TE → WR (Slot)` should have stricter thresholds and require approval, then the code punts by saying "we keep approval=False for the base TE→WR and note that TE→Slot WR is the same rule." This is a documented gap in rule enforcement.
**Impact:** TE→Slot WR — a legitimate distinct rule — is silently not enforced. Any TE converting to WR bypasses the slot-specific gate.
**Fix:** Add a second rule key `("TE", "WR_SLOT")` or a flag on the existing rule, and surface "Slot WR" as a destination option in `VALID_DESTINATIONS`.

### OBSERVATION #9: `FR_COUNTER_PATH` persistence is not scoped per-guild

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py:93, 1204, 1236-1240`
**Confidence:** 0.60
**Smell:** The force request counter is a single global integer, not keyed by guild. If ATLAS is ever deployed to multiple guilds (the bot is a Discord bot, so this is plausible), two guilds will share the counter and IDs will clash. Also, the ID format `{MMDD}-{counter:03d}` is not month-year scoped — IDs reset implicitly on month changes but the counter doesn't reset.
**Impact:** Multi-guild support is broken. `force_request_counter.json` becomes a single point of contention.
**Fix:** Key `_request_counter` by guild ID. Scope `FR_COUNTER_PATH` per-guild.

### OBSERVATION #10: No tests for any Sentinel subsystem

**Location:** `C:/Users/natew/Desktop/discord_bot/sentinel_cog.py` (entire file)
**Confidence:** 0.80
**Smell:** A 3101-LOC file with 6 subsystems has zero test coverage (no companion `test_sentinel_cog.py` file in the repo per the glob). All the subtle bugs noted above would be caught by a test. Specifically:
- Idempotency of `_guilty_callback` / `_finalize`
- Permission enforcement on `positionchangeapprove_impl`
- Race in `_next_id` under concurrent calls
- JSON round-trip of `_complaints` / `_force_requests`
- `validate_position_change` against the rule dict
**Impact:** Any refactor is risky. New rule additions have no safety net.
**Fix:** At minimum, add unit tests for `validate_position_change`, `_dc_protocol_embed`, and the state pruning functions. These are pure-function tests and should take < 1 day.

## Cross-cutting Notes

Three patterns extend beyond this file and likely repeat in other Sentinel-adjacent cogs (genesis_cog, boss_cog):

1. **`_state` shared via import from `genesis_cog`** (line 1490) — sentinel is one of two writers to `parity_state.json` via `_save_state()`. This is a cross-module shared-mutable contract that is only enforced by convention. Any change to `parity_state.json` schema in `genesis_cog` will silently break sentinel's position-change system. Genesis and Sentinel should not share state this way — extract `parity_state.py` as the TODO at line 1486-1488 correctly notes.

2. **`_impl` pattern without permission enforcement** (lines 2179, 2211) — the CLAUDE.md "admin delegation" pattern (`boss_cog.py` calls `_impl` methods) works only if either the `_impl` checks permissions OR every caller wraps in a decorator. Sentinel's position-change `_impl` methods trust their callers completely, which is a silent permission bypass waiting to happen. This pattern should be audited across all cogs that expose `_impl` methods.

3. **`_save_*_state` async lock pattern** (lines 149, 173) is correctly used for complaints and force requests, but NOT for the position-change state. The position-change code calls `_save_state()` synchronously (line 2139, 2197, 2231), which is the opposite of the convention. Either make all state saves async+locked, or document why position changes are exempt.
