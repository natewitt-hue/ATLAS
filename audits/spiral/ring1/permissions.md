# Adversarial Review: permissions.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 206
**Reviewer:** Claude (delegated subagent)
**Total findings:** 13 (2 critical, 6 warnings, 5 observations)

## Summary

This file is the security boundary for every commissioner-only and TSL-Owner-only command in ATLAS, and several real privilege-escalation and crash-on-boot hazards live in it. The most dangerous defects are: (1) module-load-time `int(x)` parsing of `ADMIN_USER_IDS` that will hard-crash the bot on a typo, and (2) `is_tsl_owner`'s "soft fallback" that grants owner privileges to every user in the guild whenever the `TSL Owner` role is missing or renamed. Multiple secondary issues — case-sensitive role-name matching, blocking SQLite calls inside an async predicate, DM/guild semantic mismatches in `is_god`, and a channel-ID information leak — should also be addressed before this is trusted as the canonical permission layer.

## Findings

### CRITICAL #1: `ADMIN_USER_IDS` parsing crashes the bot at import if env var has any non-integer entry

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:33-35`
**Confidence:** 0.97
**Risk:** A single typo, stray quote, or copy-paste artifact in the `ADMIN_USER_IDS` environment variable raises an uncaught `ValueError` at module import time. Because `permissions.py` is imported by `bot.py` (line 176 in `bot.py`: `from permissions import ADMIN_USER_IDS`) and almost every cog, the bot will fail to start at all — no Discord login, no error channel, no graceful fallback.
**Vulnerability:** The list comprehension `[int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]` runs at module load. The `if x.strip()` only filters empty strings; it does NOT validate that the surviving tokens are integers. Inputs like `"123,abc"`, `"123, , 456"` (where the middle entry is a literal space, which `.strip()` collapses but `int()` later sees), or `"123,456,"` with a trailing comment such as `"123 # main admin"` all crash. Worse, the value being parsed is also not stripped before `int()` — so `" 123 "` raises `ValueError: invalid literal for int() with base 10: ' 123 '` even though `x.strip()` evaluated truthy in the filter.
**Impact:** Total bot outage on a one-character env-var typo. No log line points at the cause unless the operator is reading stderr at startup. Recovery requires the operator to SSH in and manually fix the env var.
**Fix:** Wrap parsing in a function with per-token try/except and explicit `.strip()`:
```python
def _parse_admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_USER_IDS", "")
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            import logging
            logging.getLogger(__name__).error(
                "permissions: ignoring invalid ADMIN_USER_IDS entry %r", token
            )
    return out

ADMIN_USER_IDS: frozenset[int] = frozenset(_parse_admin_ids())
```
A `frozenset` also fixes the O(n) lookup smell in Observation #5.

---

### CRITICAL #2: `is_tsl_owner` soft fallback grants owner privileges to every user when the role is missing or renamed

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:86-89`
**Confidence:** 0.95
**Risk:** If the `"TSL Owner"` role does not exist on a guild — because it has been deleted, renamed (e.g., to `"TSL Owners"` or `"Owner"`), is on a brand-new guild where ATLAS was just installed, or has been renamed during a re-org — `is_tsl_owner()` returns `True` for **every single user** in the guild, including unauthenticated random members. Every command guarded by `is_tsl_owner` (trade approvals, force requests, ability budget submits, anything labeled "owner only") becomes world-callable.
**Vulnerability:** Lines 87-89:
```python
role_exists = any(r.name == TSL_OWNER_ROLE_NAME for r in guild.roles)
if not role_exists:
    return True  # Soft fallback — role not configured yet
```
The fallback's stated rationale ("prevents lockout during migration") chooses availability over confidentiality on a security boundary. The check is also case-sensitive and whitespace-strict (`r.name == "TSL Owner"`), so a commissioner who renames the role to `"TSL Owner "` (trailing space — easy in Discord's UI) instantly opens every owner-only command to the public. There is no logging when the soft fallback fires, so the breach is silent.
**Impact:** Privilege escalation on every guild where the role is mistyped, missing, or renamed. Owner-gated commands include trade execution and roster modifications — financial and competitive integrity hazards in a sim league.
**Fix:** Fail closed, not open. If the role does not exist, deny by default and surface a setup error to commissioners. Optionally fall back to `is_commissioner()` so commissioners are not locked out:
```python
role_exists = any(r.name == TSL_OWNER_ROLE_NAME for r in guild.roles)
if not role_exists:
    log.warning("permissions: TSL Owner role missing on guild %s — denying", guild.id)
    return await is_commissioner(interaction)  # commissioners only

if any(r.name == TSL_OWNER_ROLE_NAME for r in member.roles):
    return True
return await is_commissioner(interaction)
```
Also normalize role-name comparison via `.casefold().strip()` to survive Discord's space-tolerant rename UI (see Warning #1).

---

### WARNING #1: Role-name matching is case-sensitive and whitespace-strict on every check

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:37-39, 64, 87, 92, 112`
**Confidence:** 0.92
**Risk:** All four role-based checks (`COMMISSIONER_ROLE_NAME`, `TSL_OWNER_ROLE_NAME`, `GOD_ROLE_NAME`) compare `r.name == <constant>` using strict equality. If a Discord admin renames the role to `"commissioner"` (lowercase), `"Commissioner "` (trailing space — common when copy-pasting from a docs page), `"Commissioners"` (plural), or `"COMMISSIONER"` (caps lock), every commissioner is locked out instantly with no grace period.
**Vulnerability:** Discord's role-rename UI does not enforce capitalization or trim trailing whitespace. The constants are also typed as bare strings (no canonicalization helper). Combined with Critical #2, a single trailing space on `TSL_OWNER_ROLE_NAME` is the difference between "world-callable" and "locked out" — there is no middle ground.
**Impact:** Admin can self-lock-out by renaming the role even slightly. Combined with Critical #2, this turns a typo into either a denial-of-service or a privilege escalation depending on which role the typo affects.
**Fix:** Centralize role lookup with a normalized comparison:
```python
def _has_role(member: discord.Member, role_name: str) -> bool:
    target = role_name.casefold().strip()
    return any(r.name.casefold().strip() == target for r in member.roles)
```
Use it from all three check functions. Also document expected role names in CLAUDE.md.

---

### WARNING #2: `require_channel` predicate calls blocking SQLite synchronously inside the event loop

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:172-189`
**Confidence:** 0.88
**Risk:** `require_channel` calls `get_channel_id()` from `setup_cog` (lines 175, 183). I confirmed in `setup_cog.py:114-162` that `get_channel_id` is a **synchronous** function that opens a `sqlite3.connect(DB_PATH, timeout=2)`, executes a `CREATE TABLE IF NOT EXISTS`, and runs a `SELECT`. Each `config_key` argument triggers another DB call. While the cache (`_channel_cache`) usually short-circuits this, the cache is empty on first call after restart and is invalidated by `_save_channel_id`. During a startup window or after a config change, every `@require_channel` slash command will block the event loop for up to **2 seconds × number of config keys**.
**Vulnerability:** The predicate is `async` but the function it calls is not. There is an `async` wrapper available (`get_channel_id_async` at `setup_cog.py:165`) which uses `run_in_executor`, but `require_channel` does not use it. The CLAUDE.md focus block explicitly flags "blocking calls inside async functions (sqlite3, requests, time.sleep)" as a high-priority failure mode.
**Impact:** During cold-start or post-config-change windows, every gated command blocks the bot's event loop for several seconds, causing missed heartbeats, interaction timeouts (10062), and queued event backups. Multi-key checks compound the problem.
**Fix:** Switch to the async wrapper. Because the predicate is already `async`:
```python
from setup_cog import get_channel_id_async
...
ch_id = await get_channel_id_async(key, guild_id)
```
This uses the same cache and falls back through the executor for the cold path.

---

### WARNING #3: `require_channel` does not gate DM context — soft-fallback opens commands DM-callable

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:179-189`
**Confidence:** 0.78
**Risk:** When invoked from a DM, `interaction.guild_id` is `None` and `interaction.channel_id` is the DM channel snowflake. The predicate then calls `get_channel_id(key, None)`. In `setup_cog.py:148-152`, the no-guild branch executes `SELECT channel_id FROM server_config WHERE config_key=? LIMIT 1` — it picks **whatever channel from any guild was inserted first**. So either:
1. A row is found from some other guild → `allowed_ids` is non-empty but the DM caller's `interaction.channel_id` does not match → falls through to `is_commissioner(interaction)`. In a DM, `is_commissioner` only honors `ADMIN_USER_IDS`, so non-env-admins see the channel-ID-leak error message (see Warning #6) listing channel IDs from a guild they may not even be in.
2. No row is found → `allowed_ids` is empty → soft fallback returns `True` → **the command is callable from DMs** even though it was clearly intended to be guild-channel-restricted.
**Vulnerability:** No `interaction.guild is None` precheck. The decorator is meant to express "this command runs only in guild channel X", but degrades to "this command runs anywhere it isn't blocked".
**Impact:** Confidential or admin-flavored commands meant for a specific guild channel may be silently callable from a DM with the bot. Combined with the DM bypass behavior in `is_commissioner` (which only honors env admins in DMs), the surface is small but real.
**Fix:** Add an explicit DM check at the top of the predicate:
```python
if interaction.guild_id is None:
    await interaction.response.send_message(
        "ATLAS: This command can only be used inside a server channel.",
        ephemeral=True,
    )
    return False
```
And pass `guild_id` (now non-None) to `get_channel_id`. Optionally allow `dm_permission=False` on the underlying app_command instead.

---

### WARNING #4: `is_god` semantic mismatch — env admins are gods in DMs but not in guilds

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:99-115`
**Confidence:** 0.80
**Risk:** The docstring claims "GOD is above Commissioner — has all commissioner powers plus destructive operations". But the implementation only checks the GOD role (line 112), with NO fallback to `ADMIN_USER_IDS`, `guild_permissions.administrator`, or `is_commissioner()` in guild context. In DM context (lines 109-110), it falls back to `ADMIN_USER_IDS`. So the same env-listed admin who has full destructive power in DMs is rejected from a `/god` command in the guild unless they explicitly hold the GOD role.
**Vulnerability:** This contract surprises both directions: env admins lose privileges in guilds (privilege regression), and DM env admins gain privileges that role-based GOD users would only have in guilds. There's no documentation of this in CLAUDE.md, and `god_cog` callers are likely to assume `is_god` is monotonic on top of `is_commissioner`.
**Impact:** Either (a) bot admins are surprised when `/god rebuilddb` fails for them in the guild despite working in DMs, or (b) someone adds an env-admin fallback "to fix it" and accidentally widens the GOD gate. Destructive ops (`rebuilddb`, affinity reset) on the wrong path could corrupt the historical DB.
**Fix:** Pick one model and document it. Either:
1. Strict role-only — remove the DM env-admin fallback (lines 109-110) and require the GOD role everywhere. This is the safest model for destructive ops, even from DMs.
2. Layered — env admins are always GOD in any context, regardless of role. Then add `if interaction.user.id in ADMIN_USER_IDS: return True` as the first check.
The current half-and-half is the worst option.

---

### WARNING #5: `commissioner_only` and `require_god` predicates can raise inside `send_message`, escaping as `CommandInvokeError` instead of `CheckFailure`

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:129-137, 149-157`
**Confidence:** 0.72
**Risk:** Both decorators call `await interaction.response.send_message(...)` after a failed permission check, then return `False`. If `send_message` raises (interaction expired → `discord.NotFound` 10062, double-acked → `discord.HTTPException` 40060, network error, rate-limit error), the exception propagates out of the predicate. discord.py wraps non-`CheckFailure` exceptions raised from a predicate into `CommandInvokeError`, NOT `CheckFailure`. The global error handler in `bot.py:336-360` then takes the non-CheckFailure branch and tries to send another error message, which can also fail, producing two stack traces in logs and an unacknowledged interaction for the user.
**Vulnerability:** The predicate mixes "checking permission" with "rendering the denial message" in one async call. There is no try/except around `send_message`. The contract that "predicate returns False = CheckFailure raised by framework" only holds if the predicate doesn't raise its own exception first.
**Impact:** Logs become noisy on stale interactions, observability degrades, and the user may see no acknowledgement at all. Not financial corruption, but real reliability noise on the security boundary.
**Fix:** Catch and swallow Discord errors from the denial message; let the framework raise `CheckFailure` cleanly:
```python
async def predicate(interaction: discord.Interaction) -> bool:
    if await is_commissioner(interaction):
        return True
    try:
        await interaction.response.send_message(
            "ATLAS: This command is restricted to commissioners.", ephemeral=True
        )
    except (discord.NotFound, discord.HTTPException):
        pass
    return False
```

---

### WARNING #6: `require_channel` denial message leaks channel IDs to non-privileged users

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:199-203`
**Confidence:** 0.65
**Risk:** When a non-commissioner user runs a channel-gated command in the wrong place, the predicate emits `f"ATLAS: This command can only be used in {mentions}."` where `mentions` is `<#channel_id>` for every configured channel. This includes channels the user may not have read permission to see, may not know exist, or that an admin is intentionally hiding (`#commissioner-control`, `#flow-audit-log`, etc.). Discord renders `<#id>` mentions as the channel name if the user can see it, or as `#deleted-channel` / a raw ID otherwise — but the raw snowflake is still exposed in the message text and the markdown source.
**Vulnerability:** No allow-listing of which channel categories are safe to disclose, no filtering by user visibility, and the same error message is shown regardless of how privileged the user is.
**Impact:** Information disclosure of channel topology. Low impact in most communities, but on a 31-team league with admin-only channels, this exposes infrastructure to anyone running the wrong command.
**Fix:** Show a generic message ("ATLAS: This command isn't available in this channel.") to non-commissioners. Optionally show the channel list only when the user already has read perms on at least one of them, or only to commissioners.

---

### OBSERVATION #1: `is_commissioner` declared `async` despite zero `await` calls

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:44-67`
**Confidence:** 0.95
**Risk:** Function is declared `async def is_commissioner(...)` but performs no awaits. Every caller must `await` it, and the awaitable wrapping is wasted overhead. More importantly, future maintainers can't tell at the call site whether this function legitimately does I/O or is just async-by-convention. If a contributor refactors a fast-path call site to drop the `await`, mypy/pyright won't catch it without strict mode and the bug becomes a `coroutine never awaited` warning at runtime.
**Vulnerability:** Cosmetic for now. But because `is_tsl_owner` (line 96) and `require_channel` (line 196) and the decorator predicates all `await` it, removing `async` later is a breaking-change refactor.
**Impact:** Cognitive friction and a small performance loss. No functional defect.
**Fix:** Either keep `async` for symmetry with `is_tsl_owner` and `is_god` (both of which also currently don't await — they could all be sync), or convert all three to plain `def`. Pick consistency.

---

### OBSERVATION #2: `is_god` and `is_tsl_owner` are also `async` with no awaits

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:70-115`
**Confidence:** 0.95
**Risk:** Same as Observation #1 — both functions are `async def` but contain no `await`. `is_tsl_owner` does call `await is_commissioner(interaction)` on line 96, which itself doesn't await anything, so the entire chain is fake-async. This makes the I/O cost of permission checks invisible — a future reviewer who sees an `await` chain expects I/O somewhere and may add caching or memoization that breaks if/when one of these functions ever needs to do real I/O.
**Vulnerability:** Maintenance hazard, not a runtime bug.
**Impact:** Code reads as if it's doing I/O when it isn't, and a future I/O addition will be silent.
**Fix:** Either drop `async` from all three, or document a comment ("kept async for forward compatibility with role lookups").

---

### OBSERVATION #3: Magic role-name string literals not centralized in CLAUDE.md or settings

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:37-39`
**Confidence:** 0.85
**Risk:** `COMMISSIONER_ROLE_NAME = "Commissioner"`, `TSL_OWNER_ROLE_NAME = "TSL Owner"`, `GOD_ROLE_NAME = "GOD"` are hardcoded module-level strings. Multi-server deployments cannot vary the role names. CLAUDE.md does not mention them. A new operator standing up ATLAS in a different guild would need to grep the codebase to discover the required role names.
**Vulnerability:** Limits portability, but doesn't break the security model.
**Impact:** Harder onboarding, no per-guild override, surprises during cross-server testing.
**Fix:** Read role names from environment variables with these as defaults, and document them in CLAUDE.md's "Environment Variables" table.

---

### OBSERVATION #4: No audit logging when permission checks fail or when soft fallbacks fire

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py` (entire file)
**Confidence:** 0.80
**Risk:** Every denied command, every "soft fallback returned True because role missing" event, and every successful elevated command lands in the void. There is no `log.info(...)` or `log.warning(...)` anywhere in the file. If a privilege escalation does occur (Critical #2), there is no trail.
**Vulnerability:** Observability gap on a security boundary. Forensics are impossible after the fact.
**Impact:** A successful exploit of any of the above weaknesses leaves no evidence. Recovery from a misconfigured role rename requires the operator to notice from user reports.
**Fix:** Add `import logging; log = logging.getLogger(__name__)` and emit at minimum:
- WARNING when `is_tsl_owner` soft fallback fires (role missing)
- INFO when env-admin path elevates a user to commissioner/god
- INFO when a permission check denies (with `interaction.user.id`, `interaction.command.name`, `interaction.guild_id`)

---

### OBSERVATION #5: `ADMIN_USER_IDS` is a `list[int]` instead of a `set[int]`/`frozenset[int]`

**Location:** `C:/Users/natew/Desktop/discord_bot/permissions.py:33-35`
**Confidence:** 0.95
**Risk:** Membership test `interaction.user.id in ADMIN_USER_IDS` is O(n) in a list. For the typical case of 1-3 admins, performance is negligible. But the type also signals "ordered, possibly duplicate" when the actual semantics are "unordered set of trusted IDs". This makes refactoring unsafe — a future maintainer might index into the list.
**Vulnerability:** Type design smell. Not a defect.
**Impact:** Cosmetic and performance for very large admin lists. Both are sub-millisecond at the current scale.
**Fix:** Use `frozenset[int]` (also addresses the parser issue in Critical #1). Frozenset is immutable so it can't be accidentally mutated by a misbehaving cog.

## Cross-cutting Notes

This file is imported widely (`bot.py:176` confirms direct import of `ADMIN_USER_IDS`, plus the `CLAUDE.md` describes commissioner gates on at least 6 cogs). The two CRITICAL issues likely affect every cog that depends on this module:

- **Critical #1 (boot crash on bad env)** is a single point of failure for the whole bot. Any cog importing `from permissions import ...` triggers the parse, so even cogs that don't directly call the checks will fail to load.
- **Critical #2 (TSL owner soft fallback)** likely silently expands the privileges of any cog calling `is_tsl_owner` whenever ATLAS is moved to a new guild or the role is renamed. Audit `genesis_cog.py`, `flow_sportsbook.py`, and any "force request" or trade-approval flow for callers; assume they are currently world-callable on staging guilds without the role configured.
- **Warning #2 (blocking sqlite in async predicate)** mirrors a pattern likely present in any other module that imports `setup_cog.get_channel_id` directly. A cross-file Grep for `from setup_cog import get_channel_id` (without the `_async` suffix) will surface every offender.
- **Warning #1 (case-sensitive role-name match)** likely repeats in any other module that does its own role check (`r.name == "Commissioner"` or similar). Centralizing role-name normalization here is a precondition for fixing the rest of the codebase.
- The role-name constants and admin parsing should ideally move into a small `_load_security_config()` function so future ring 1 reviews can validate parser behavior in isolation.
