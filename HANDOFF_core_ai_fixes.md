# HANDOFF: Core & AI Engine — GAP Review Findings

**Reviewed by:** Claude Code (Session A — Core & AI Engine)
**Date:** 2026-03-20
**Files:** `bot.py`, `atlas_ai.py`, `setup_cog.py`, `permissions.py`

---

## Summary

4 files reviewed line-by-line. Found **5 bugs** (2 medium, 3 low), **4 risks** worth hardening, and **2 CLAUDE.md staleness issues**. No critical/data-loss bugs. The architecture is solid — the main gaps are defensive edge cases and missing timeouts.

---

## Bugs

### BUG-1: `_call_gemini_with_search` crashes on None response text [MEDIUM]
**File:** `atlas_ai.py:286`
**Problem:** `response.text.strip()` will raise `AttributeError` if `response.text` is `None` (safety block, empty response, content filter). The regular `_call_gemini` correctly uses `(response.text or "").strip()` on line 237, but the search variant doesn't.
**Impact:** Any Gemini safety-filtered response in `call_atlas()` (bot.py @mention handler) crashes instead of falling back to Claude.
**Fix:**
```python
# Line 286 in atlas_ai.py — change:
text=response.text.strip(),
# To:
text=(response.text or "").strip(),
```

### BUG-2: `_save_channel_id` doesn't invalidate guild_id=0 cache entry [LOW]
**File:** `setup_cog.py:189`
**Problem:** When `_save_channel_id(key, channel_id, guild_id)` runs, it pops `f"{key}:{guild_id}"` from `_channel_cache`. But `get_channel_id(key)` called *without* a guild_id caches under `f"{key}:0"`. After a save, the `key:0` cache entry is stale.
**Impact:** If a cog calls `get_channel_id("admin_chat")` (no guild_id) before a channel remap, then the channel gets remapped via `/setup`, subsequent calls without guild_id still return the old channel ID until bot restart.
**Fix:**
```python
# Line 189 in setup_cog.py — change:
_channel_cache.pop(f"{key}:{guild_id}", None)
# To:
_channel_cache.pop(f"{key}:{guild_id}", None)
_channel_cache.pop(f"{key}:0", None)  # Also invalidate guild-agnostic cache
```

### BUG-3: `ADMIN_USER_IDS` malformed env var crashes bot at import [LOW]
**File:** `permissions.py:33-35`
**Problem:** `int(x)` in the list comprehension raises `ValueError` if `ADMIN_USER_IDS` contains non-numeric values (e.g., `"123,abc,456"` or trailing whitespace around commas like `"123, 456"`). This crashes the entire bot on startup with no helpful error message. Note: the `.strip()` in the `if x.strip()` filter only gates inclusion, the `int(x)` still receives untrimmed strings.
**Impact:** Typo in env var kills the bot.
**Fix:**
```python
# Line 33-35 in permissions.py — change:
ADMIN_USER_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
]
# To:
def _parse_admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_USER_IDS", "")
    ids = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            ids.append(int(x))
        except ValueError:
            print(f"[permissions] WARNING: Invalid ADMIN_USER_IDS entry: '{x}' — skipping")
    return ids

ADMIN_USER_IDS: list[int] = _parse_admin_ids()
```

### BUG-4: `generate_stream()` hangs if streaming thread raises before sentinel [LOW]
**File:** `atlas_ai.py:666-673`
**Problem:** If `_run_stream()` raises an exception before pushing the `None` sentinel to the queue, the `while True` loop on line 668 blocks on `queue.get()` forever. The `await fut` on line 673 (which would propagate the exception) is after the loop and never executes.
**Impact:** Caller coroutine hangs indefinitely. Currently labeled "future use" and no callers exist, so **zero production impact today**. But will bite when first used.
**Fix:**
```python
# In _run_stream(), wrap in try/finally to always push sentinel:
def _run_stream():
    try:
        # ... existing streaming code ...
        with claude.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                loop.call_soon_threadsafe(queue.put_nowait, text)
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)
```

### BUG-5: `_call_gemini_with_tools` crashes on empty candidates [LOW]
**File:** `atlas_ai.py:373`
**Problem:** `response.candidates[0].content.parts` has no guard for empty `candidates` list (Gemini returns empty candidates on safety blocks) or `None` content. Will raise `IndexError` or `AttributeError`.
**Impact:** Tool-use fallback path crashes instead of returning empty result. Affects Oracle QueryBuilder when Claude is down AND Gemini safety-filters the response (rare double failure).
**Fix:**
```python
# Line 373 — add guard:
tool_calls = []
text_parts = []
if response.candidates and response.candidates[0].content:
    for part in response.candidates[0].content.parts:
        # ... existing extraction code ...
else:
    # Safety-filtered or empty response
    pass
```

---

## Risks (Not Bugs, But Should Harden)

### RISK-1: No timeout on AI API calls [MEDIUM]
**File:** `atlas_ai.py` — all `_call_*` functions
**Problem:** Neither Claude nor Gemini calls have explicit timeouts. The Anthropic SDK defaults to 600s (10 minutes). If Claude hangs, the `run_in_executor` thread blocks, the Discord interaction sits spinning, and the default thread pool slot is consumed. Enough hanging calls could exhaust the thread pool.
**Impact:** User sees indefinite "thinking..." spinner. Won't crash but degrades UX badly.
**Fix:** Set explicit timeout on client creation:
```python
# In _get_claude():
_claude_client = Anthropic(api_key=api_key, timeout=30.0)

# In _get_gemini() — Gemini SDK uses httpx internally:
# Set via config per-call: config_kwargs["timeout"] = 30
```

### RISK-2: Autograde not fired after startup `load_all()` [LOW]
**File:** `bot.py` — `on_ready()` (line 472)
**Problem:** `_startup_load()` runs `dm.load_all()` in a thread executor (no event loop available), so it correctly can't fire the async autograde callback. But `on_ready()` doesn't fire it after `_startup_load()` returns either. The autograde callback is only called in `_sync_impl()` (manual `/atlas sync`).
**Impact:** On first boot, sportsbook bets from the current week won't be auto-graded until either the sportsbook cog's own `auto_grade` task loop fires or someone runs `/atlas sync`. This is a minor timing gap — the sportsbook's task loop starts in `cog_load` (during `setup_hook`), so auto-grading will eventually happen.
**Fix (optional):**
```python
# In on_ready(), after _startup_load() returns (around line 474):
try:
    if dm._autograde_callback is not None:
        await dm._autograde_callback()
except Exception as e:
    print(f"[Startup] Autograde failed: {e}")
```

### RISK-3: `_invalidate_caches()` silently swallows all exceptions [LOW]
**File:** `bot.py:186-191`
**Problem:** `except Exception: pass` — if `clear_query_cache` raises (e.g., DB locked, code bug), it's silently ignored. The codex query cache stays stale after a sync.
**Fix:**
```python
except Exception as e:
    print(f"[Cache] Failed to invalidate query cache: {e}")
```

### RISK-4: `_startup_done` set before `_startup_load()` prevents retry on failure [LOW]
**File:** `bot.py:459`
**Problem:** `_startup_done = True` is set BEFORE `_startup_load()` runs. Comment says "Set BEFORE async work to prevent concurrent on_ready races." If `_startup_load()` fails (API down, DB locked), all subsequent reconnects skip the load. The bot runs with empty DataFrames until manually restarted.
**Impact:** Rare — only hits if MaddenStats API is down at exact boot time. But when it does hit, recovery requires full bot restart.
**Fix (optional):** Move the flag set to after success, with a lock for the race:
```python
_startup_lock = asyncio.Lock()

async def on_ready():
    global _startup_done
    if _startup_done:
        print(f"--- ATLAS v{ATLAS_VERSION} RECONNECTED ---")
        return
    async with _startup_lock:
        if _startup_done:  # Double-check after acquiring lock
            return
        try:
            await loop.run_in_executor(None, _startup_load)
            _startup_done = True
        except Exception as e:
            print(f"[Startup] FAILED: {e} — will retry on next reconnect")
```

---

## CLAUDE.md Staleness

### STALE-1: Cog load order out of date
**Problem:** CLAUDE.md lists 12 cogs ending with `commish_cog`. Actual `bot.py` has:
- `commish_cog` **commented out** (replaced by `boss_cog`)
- `flow_live_cog` added (not in CLAUDE.md)
- `real_sportsbook_cog` added (not in CLAUDE.md)
- `boss_cog` added (not in CLAUDE.md)
**Fix:** Update the "Cog Load Order" table in CLAUDE.md to match the actual `_EXTENSIONS` list in `bot.py`.

### STALE-2: Module Map missing entries
**Problem:** CLAUDE.md Module Map doesn't include `flow_live_cog`, `real_sportsbook_cog`, or `boss_cog`.
**Fix:** Add entries for these three modules.

---

## Verified Clean (No Issues Found)

| Check | Status |
|-------|--------|
| `_startup_done` flag prevents duplicate `load_all()` on reconnect | CLEAN |
| Cog load order: echo first, setup second | CLEAN |
| `/atlas sync` calls load_all → sync_tsl_db → cache invalidation in correct order | CLEAN |
| Blowout monitor: interval (15m), hasattr gate, error handling, calls `flag_stat_padding` | CLEAN |
| No bare `except: pass` blocks hiding errors | CLEAN |
| Reconnect handler properly skips reload | CLEAN |
| `generate()` tries Claude first, Gemini fallback | CLEAN |
| `run_in_executor` used correctly everywhere (no sync calls on event loop) | CLEAN |
| API keys loaded from env vars correctly (lazy singletons) | CLEAN |
| No cog calls Gemini/Claude SDK directly (all go through `atlas_ai`) | CLEAN |
| No imports from `QUARANTINE/` | CLEAN |
| `get_persona()` used for AI system prompts (not hardcoded) | CLEAN |
| `view=None` not passed to `followup.send()` | CLEAN |
| `is_commissioner()` checks env IDs, guild admin, Commissioner role (OR logic) | CLEAN |
| `is_tsl_owner()` soft fallback when role doesn't exist | CLEAN |
| `commissioner_only()` decorator no double-message (bot.py error handler checks `is_done()`) | CLEAN |
| DM context handled correctly in both permission functions | CLEAN |
| `require_channel()` lazy-imports setup_cog, handles ImportError | CLEAN |
| `get_channel_id()` defensive `CREATE TABLE IF NOT EXISTS` for atomic swap recovery | CLEAN |
| `setup_hook` fires before `on_ready` (no race condition) | CLEAN |

---

## Priority Order for CLAUDEFROG

1. **BUG-1** (atlas_ai.py:286) — One-line fix, prevents crash on Gemini safety filter
2. **RISK-1** (atlas_ai.py) — Add 30s timeout to client creation, prevents hung threads
3. **BUG-3** (permissions.py:33-35) — Defensive int parsing, prevents boot crash on env typo
4. **BUG-2** (setup_cog.py:189) — One-line cache invalidation addition
5. **BUG-5** (atlas_ai.py:373) — Guard empty candidates on Gemini tool-use
6. **RISK-3** (bot.py:191) — Change `pass` to print in `_invalidate_caches()`
7. **BUG-4** (atlas_ai.py:666) — Fix streaming sentinel; no callers yet but prevents future hang
8. **STALE-1/2** (CLAUDE.md) — Update cog load order and module map
9. **RISK-2** (bot.py) — Optional: fire autograde after startup load
10. **RISK-4** (bot.py) — Optional: allow startup retry on failure
