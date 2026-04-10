# Adversarial Review: atlas_send.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 116
**Reviewer:** Claude (delegated subagent)
**Total findings:** 6 (1 critical, 3 warnings, 2 observations)

## Summary

Universal card delivery helper that correctly handles the `view=None` omission trap from CLAUDE.md — but has no exception handling anywhere, returns `None` from the non-followup path (forcing every caller to check), and silently forwards `ephemeral=True` to `channel.send()` where it's meaningless. Also assumes `io.BytesIO(png_bytes)` is zero-cost per call and builds a fresh `discord.File` per send, which is correct, but there is no size check (Discord has an 8/25/50 MB attachment limit that will raise HTTPException on overflow).

## Findings

### CRITICAL #1: No exception handling — upstream render failures propagate as uncaught interaction errors
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_send.py:63-77, 108-116`
**Confidence:** 0.9
**Risk:** `await interaction.response.send_message(**kwargs)` and `await interaction.followup.send(**kwargs)` can raise `discord.HTTPException` (rate limit, payload too large, view validation error, interaction expired after >15 min, channel permissions changed mid-flight). Every raise bubbles up to the caller, who must wrap this helper in a `try/except` OR let Discord display a generic "This interaction failed" message to the user. There is no central logging, no retry, no fallback to embed-only.
**Vulnerability:** The helper is labeled "Universal Card Delivery" and is called from 4+ cogs (economy_cog, flow_sportsbook, polymarket_cog), yet each call site is forced to reinvent exception handling. Given the Discord interaction 15-min expiry and the 3s initial response window, a slow `render_card()` upstream can cause the interaction to expire between `defer()` and `send_card(followup=True)`. When that happens, the caller has no idea unless they wrapped.
**Impact:** Silent dropped messages or visible "interaction failed" popups, unbounded across the codebase. Per CLAUDE.md attack surface: "empty-state, null, timeout, and degraded dependency behavior."
**Fix:** Wrap the send in a `try/except discord.HTTPException` and at minimum `log.exception(...)`. Better: provide an `on_error` callback arg or return a `(message, error)` tuple so callers can react.

### WARNING #1: `send_card_to_channel` accepts `ephemeral` nowhere — but `send_card` silently drops it when `followup=False`
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_send.py:70-71, 73-77`
**Confidence:** 0.85
**Risk:** The kwargs builder sets `kwargs["ephemeral"] = True` only when `ephemeral=True`, but `interaction.response.send_message(ephemeral=True)` IS supported — that part is fine. However, the docstring says "ephemeral : bool — If True, only the invoking user sees the message" without noting that the initial `send_message` path returns `None` (line 77) while `followup.send` returns a `Message`. A caller who does `msg = await send_card(..., ephemeral=True)` on the first-response path gets `None` back unconditionally, even though they asked for ephemeral — they cannot edit or delete the message later.
**Vulnerability:** Asymmetric return contract — `followup=False` always returns `None`, `followup=True` returns the `Message`. Not documented in the `Returns` section clearly (it's there but not emphasized), and breaks the "universal" promise.
**Impact:** Callers who want to edit the card post-render (e.g., casino game state transitions) must always use `followup=True`, meaning they must `defer()` first. Silent UX-breaking.
**Fix:** Either always return `await interaction.original_response()` on the non-followup path, or document the constraint loudly in the docstring and add an assertion.

### WARNING #2: `kwargs: dict` has no type params — typing regression
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_send.py:65, 110`
**Confidence:** 0.7
**Risk:** `kwargs: dict = {"file": file}` on line 65 (and 110) is typed as bare `dict` instead of `dict[str, Any]`. A mypy/ruff strict run flags this. Worse: passing `kwargs` via `**kwargs` to `send_message` loses any type-check the caller hoped for.
**Vulnerability:** Type-system hole on a public helper. The helper is small enough that this is a smell, but a contract-breaking version of `discord.py` that renamed `view=` → `components=` would be caught at runtime only.
**Impact:** Type safety regression. Low severity but a code-hygiene red flag for a "universal" helper.
**Fix:** `from typing import Any; kwargs: dict[str, Any] = {...}`.

### WARNING #3: No attachment size check — Discord rejects files >8 MB (non-Nitro) with HTTPException
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_send.py:63, 108`
**Confidence:** 0.8
**Risk:** A rendered card PNG at 700px × some-height × 2 DPI can exceed 8 MB for very tall cards (e.g., a full season ledger with dozens of rows). `discord.File(io.BytesIO(png_bytes))` passes the bytes through unchecked, and Discord returns `413 Payload Too Large`. There is no pre-check like `if len(png_bytes) > 8_000_000: raise ValueError(...)` or auto-compression.
**Vulnerability:** Silent failure on very large renders — the exception message will be a Discord HTTPException with a 40005 ("file too large") code. Upstream callers see "interaction failed" with no clear cause.
**Impact:** Cards in long-tail edge cases (e.g., a 95-season Super Bowl history card) silently break.
**Fix:** Add a size check before constructing the `File`: `if len(png_bytes) > 8_000_000: raise ValueError("Card PNG exceeds Discord attachment limit")`. Or better, automatically JPEG-fallback or scale down.

### OBSERVATION #1: `interaction.response.send_message` path discards the message silently
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_send.py:75-77`
**Confidence:** 0.7
**Risk:** `await interaction.response.send_message(**kwargs); return None` — the `send_message` coroutine doesn't return the Message by design in discord.py, so this is technically correct. But the `return None` is a silent "you lose the handle" contract that forces every caller who might want to edit/delete to use the followup path with a defer.
**Vulnerability:** Design smell — the helper makes it easy to do the wrong thing (first-response path that can't be edited).
**Impact:** Callers structurally can't edit sent cards unless they defer first.
**Fix:** After `send_message`, grab `await interaction.original_response()` and return that, so the contract is uniform.

### OBSERVATION #2: `send_card_to_channel` has no permission check or channel-type narrowing
**Location:** `C:/Users/natew/Desktop/discord_bot/atlas_send.py:80-116`
**Confidence:** 0.55
**Risk:** The type hint is `discord.TextChannel | discord.Thread`, but the body just calls `channel.send(...)`. Any channel type with a `.send()` method (VoiceChannel text chat, DMChannel, ForumChannel post) will work at runtime, meaning the type hint is soft. Also, if the bot lacks `send_messages` or `attach_files` permission in the channel, `await channel.send()` raises `discord.Forbidden`, which (like the `send_card` path) is unhandled.
**Vulnerability:** Loose type contract + no permission pre-check + no exception handling.
**Impact:** Low — most callers resolve channels from setup_cog, which validates permissions at load. But an admin who demotes the bot mid-session would see silent drops.
**Fix:** Optionally `if not channel.permissions_for(channel.guild.me).attach_files: log.warning(...)` before sending, and add the same `try/except discord.HTTPException` wrapper.

## Cross-cutting Notes

This helper is the canonical "safe" Discord send wrapper per CLAUDE.md's "Discord API Constraints" table (specifically the `view=None` rule), and it does handle that correctly by OMITTING `view=` from kwargs when None (lines 68-69). That's the critical invariant and it's preserved. However, the lack of centralized exception handling means every caller still needs to wrap the call anyway — which is exactly the duplication this helper was meant to eliminate. Recommend a follow-up to add a single `try/except discord.HTTPException` with structured logging inside this helper, and document the retry/fallback policy.
