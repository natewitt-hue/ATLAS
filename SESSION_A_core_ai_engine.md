# GAP Review Session A — Core & AI Engine

**Goal:** Deep line-by-line code review only. No code changes. Produce a handoff document for CLAUDEFROG (local desktop Claude) with bugs, risks, and exact fix instructions.

**Output:** When done, create `HANDOFF_core_ai_fixes.md` with the same format as `HANDOFF_data_pipeline_fixes.md` (already on the branch for reference).

---

## Files to Review (exclusive — no other session touches these)

| File | Focus |
|------|-------|
| `bot.py` | Startup sequence, cog load order, `_startup_done` flag, `/wittsync` command, blowout_monitor loop, `ATLAS_VERSION`, reconnect handling |
| `atlas_ai.py` | Claude primary / Gemini fallback, `generate()` function, `run_in_executor` usage, error handling, timeout behavior, model selection |
| `setup_cog.py` | Channel routing, `REQUIRED_CHANNELS`, `get_channel_id()`, lazy resolvers, `require_channel()` decorator |
| `permissions.py` | `is_commissioner()`, `is_tsl_owner()`, env `ADMIN_USER_IDS` parsing, decorator forms, role checks |

---

## Review Checklist

### bot.py
- [ ] Cog load order matches CLAUDE.md (echo first, setup second, etc.)
- [ ] `_startup_done` flag prevents duplicate `load_all()` on reconnect
- [ ] `/wittsync` calls `dm.load_all()` then `sync_tsl_db()` then cache invalidation in correct order
- [ ] Blowout monitor loop — interval, error handling, does it actually call `dm.flag_stat_padding()`?
- [ ] `on_ready` vs `setup_hook` — which fires first, any race condition?
- [ ] Are there any bare `except: pass` blocks hiding real errors?
- [ ] Does reconnect handler properly skip reload?
- [ ] Autograde callback — is it wired correctly after load_all?

### atlas_ai.py
- [ ] `generate()` — does it actually try Claude first, then fall back to Gemini?
- [ ] What happens when both providers fail? Does it raise, return None, or return empty string?
- [ ] `run_in_executor` — is it used correctly? Any async/sync mixing bugs?
- [ ] Timeout handling — what if Claude hangs for 30+ seconds?
- [ ] Are API keys loaded from env vars correctly?
- [ ] Is there any retry logic on transient failures?
- [ ] Token limits — does it handle truncation?
- [ ] Does any cog call Gemini/Claude SDK directly instead of going through `atlas_ai.generate()`?

### setup_cog.py
- [ ] `REQUIRED_CHANNELS` — what happens if a channel doesn't exist in the guild?
- [ ] `get_channel_id()` — thread safety? Caching behavior?
- [ ] `require_channel()` decorator — does it properly handle the case where setup_cog hasn't loaded yet?
- [ ] ImportError fallbacks — are they graceful?

### permissions.py
- [ ] `ADMIN_USER_IDS` env parsing — what if the env var is missing, empty, or malformed?
- [ ] `is_commissioner()` — does it check env IDs, "Commissioner" role, AND guild admin? In what order?
- [ ] `is_tsl_owner()` — does it check "TSL Owner" role correctly?
- [ ] Decorator forms — do they return proper Discord error messages on permission denied?

---

## MaddenStats API Gotchas Relevant to This Session

These files don't directly query the API, but bot.py orchestrates the sync:

- `load_all()` runs in a thread executor — no event loop available inside it
- `sync_tsl_db()` should receive `players` and `abilities` from data_manager to avoid duplicate API hits
- Autograde callback can't be fired from inside `load_all()` (no event loop) — bot.py must fire it after

---

## Discord API Constraints Relevant to This Session

- `view=None` cannot be passed to `followup.send()` — check any followup calls in bot.py
- Modals require `defer()` for calls >3s
- Two cogs with same slash command name → second silently fails (check for collisions in bot.py load)

---

## CLAUDE.md Rules to Verify

- `ATLAS_VERSION` must be bumped before every push
- `get_persona()` from `echo_loader.py` for AI system prompts — never hardcode
- `atlas_ai.generate()` for all AI calls — never call SDKs directly from cogs
- `_startup_done` flag to prevent duplicate `load_all()` on reconnect
- Dead files in `QUARANTINE/` — verify no imports from there
