# ATLAS AI Provider Migration: Gemini → Claude Primary

**Date:** 2026-03-19
**Status:** Approved design, ready for implementation planning

## Problem

ATLAS uses Google Gemini (2.0-flash) as the sole AI provider across ~30 callsites in 10+ files. Claude (Sonnet 4) was added to oracle_cog for structured tool-use queries but never expanded. The codebase needs Claude as the primary provider with Gemini as an automatic fallback.

## Decision

**Approach A: Centralized AI Client Module** — Create `atlas_ai.py` that encapsulates all AI calls behind a unified interface. Every cog calls `atlas_ai.generate(...)` instead of managing its own client. The module handles provider selection, tiered model routing, fallback, and provider indicators.

## Design

### Core Module: `atlas_ai.py`

#### Public Interface

```python
from enum import Enum
from dataclasses import dataclass, field
from typing import AsyncIterator

class Tier(Enum):
    HAIKU  = "haiku"    # Fast, cheap — blurbs, classification, simple gen
    SONNET = "sonnet"   # Balanced — SQL gen, chat, analysis, queries
    OPUS   = "opus"     # Complex reasoning — synthesis, long-form reports

@dataclass
class AIResult:
    text: str
    provider: str           # "claude" or "gemini"
    model: str              # actual model ID used
    tool_calls: list[dict] = field(default_factory=list)
    fallback_used: bool = False

async def generate(
    prompt: str | list[dict],
    *,
    system: str = "",
    tier: Tier = Tier.SONNET,
    max_tokens: int = 1024,
    temperature: float | None = None,
    json_mode: bool = False,
) -> AIResult:
    """Generate text. Claude primary, Gemini fallback.

    prompt: Either a plain string or a list of content blocks for multimodal
            input (e.g., [{"type": "text", "text": "..."},
                          {"type": "image", "source": {"type": "base64", ...}}]).
    json_mode: If True, instructs the model to return valid JSON.
        - Claude path: wraps prompt with "Respond with valid JSON only" instruction.
          Does NOT use response_format (Anthropic SDK has no equivalent to OpenAI's).
          For structured extraction, callers should include a JSON schema in the prompt.
        - Gemini path: sets response_mime_type="application/json".
    """
    ...

async def generate_with_tools(
    prompt: str,
    tools: list[dict],
    *,
    system: str = "",
    tier: Tier = Tier.SONNET,
    max_tokens: int = 1024,
) -> AIResult:
    """Generate with tool use. Claude primary, Gemini fallback."""
    ...

async def generate_stream(
    prompt: str,
    *,
    system: str = "",
    tier: Tier = Tier.SONNET,
    max_tokens: int = 4096,
    temperature: float | None = None,
) -> AsyncIterator[str]:
    """Stream text chunks. Claude primary, Gemini fallback (non-streaming).

    Implementation note: Uses asyncio.to_thread() with the sync Anthropic
    client's streaming context manager. The thread runs:
        with client.messages.stream(...) as stream:
            for text in stream.text_stream:
                queue.put(text)
    The async iterator reads from the queue. If Claude fails before the
    first chunk, falls back to Gemini non-streaming (full result as one yield).
    """
    ...

async def generate_with_search(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 1024,
) -> AIResult:
    """Gemini-primary (has Google Search), Claude fallback (without search)."""
    ...

# ── Sync wrappers for CLI tools ──────────────────────────────────────────────

def generate_sync(
    prompt: str | list[dict],
    *,
    system: str = "",
    tier: Tier = Tier.SONNET,
    max_tokens: int = 1024,
    temperature: float | None = None,
    json_mode: bool = False,
) -> AIResult:
    """Sync version of generate() for CLI scripts (cortex, echo_voice_extractor).

    Uses the sync Anthropic client directly (no asyncio.run needed).
    Fallback to Gemini's sync client on failure.
    """
    ...
```

#### Model Tier Mapping

| Tier | Claude (primary) | Gemini (fallback) |
|------|-----------------|-------------------|
| HAIKU | `claude-haiku-4-5-20251001` | `gemini-2.0-flash` |
| SONNET | `claude-sonnet-4-20250514` | `gemini-2.0-flash` |
| OPUS | `claude-opus-4-20250514` | `gemini-2.5-pro` |

**Note:** The Haiku model ID should be verified against the Anthropic API at implementation time.
The latest model IDs can be checked via `anthropic.Anthropic().models.list()` or the
[Anthropic docs](https://docs.anthropic.com/en/docs/about-claude/models). If `claude-haiku-4-5-*`
doesn't exist yet, use `claude-haiku-3-5-20241022` as the HAIKU tier model.

#### JSON Mode Strategy

The Anthropic SDK does NOT have `response_format={"type": "json_object"}` like OpenAI.
The strategy for JSON output from Claude:

1. **Prompt engineering**: Append `"\n\nRespond with valid JSON only. No markdown, no explanation."` to the prompt
2. **Parse + validate**: Strip any markdown fences from the response, `json.loads()` the result
3. **Retry on parse failure**: If JSON parsing fails, retry once with an explicit correction prompt
4. **Gemini path**: Uses `response_mime_type="application/json"` (native support)

For callsites that need structured JSON (cortex_analyst, polymarket curate), include the
expected JSON schema/structure in the prompt itself. This works reliably with Claude.

#### Multimodal (Vision) Support

The `prompt` parameter accepts either a plain string or a list of content blocks.
For image/vision inputs (used by sentinel_cog screenshot analysis):

```python
# Claude content blocks format
result = await generate(
    [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_data}},
        {"type": "text", "text": "Analyze this screenshot..."},
    ],
    tier=Tier.SONNET,
)
```

- **Claude path**: Content blocks passed directly to `messages.create()`
- **Gemini path**: Converted to `types.Part.from_bytes()` format

#### Fallback Logic

```
1. Try Claude with the specified tier
2. On ANY exception (timeout, rate limit, API error, network):
   a. Log warning: "[atlas_ai] Claude failed ({error}), falling back to Gemini"
   b. Retry with mapped Gemini model
   c. Return AIResult with fallback_used=True, provider="gemini"
3. If Gemini also fails: raise the original exception
```

**Exception for `generate_stream()`**: No mid-stream fallback. If Claude fails before
first chunk, fall back to Gemini non-streaming and yield the full result as one chunk.

#### Client Lifecycle

```python
_claude_client: Anthropic | None = None
_gemini_client: genai.Client | None = None

def _get_claude() -> Anthropic | None:
    """Lazy singleton. Returns None if ANTHROPIC_API_KEY not set."""
    global _claude_client
    if _claude_client is not None:
        return _claude_client
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    from anthropic import Anthropic
    _claude_client = Anthropic(api_key=api_key)
    return _claude_client

def _get_gemini() -> genai.Client | None:
    """Lazy singleton. Returns None if GEMINI_API_KEY not set."""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    from google import genai
    _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client
```

- Both lazy singletons, created on first use
- `run_in_executor()` wrapping happens INSIDE `generate()`/`generate_with_tools()` so callers don't manage threading
- `generate_sync()` uses the sync clients directly (no executor needed)
- Replaces 10+ duplicated client init patterns across the codebase

#### Streaming Implementation

`generate_stream()` uses a thread + `asyncio.Queue` pattern:

```python
async def generate_stream(...) -> AsyncIterator[str]:
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _run_stream():
        client = _get_claude()
        with client.messages.stream(...) as stream:
            for text in stream.text_stream:
                loop.call_soon_threadsafe(queue.put_nowait, text)
        loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    try:
        fut = loop.run_in_executor(None, _run_stream)
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
        await fut  # propagate exceptions
    except Exception:
        # Fallback: Gemini non-streaming, yield full result
        result = await _call_gemini(prompt, ...)
        yield result.text
```

### Fallback Indicator

When `result.fallback_used is True`, callers append to embed footers:
```
ATLAS Oracle Module · Season 98    ·  ⚡ via Gemini fallback
```

**Threading `AIResult` to embed builders:** Most callsites currently call a helper
function and get back a `str`. The migration should change these helpers to return
`AIResult` instead of `str`. Callers extract `.text` for the response and check
`.fallback_used` when building embeds. This is a per-callsite refactor done during
each migration phase.

### Special Case: `bot.py` `call_atlas()` with Google Search

`call_atlas()` uses `tools=[{"google_search": {}}]`, a Gemini-specific feature.
Claude doesn't have native Google Search.

**Decision:** This one callsite stays **Gemini-primary, Claude-fallback** (reversed).
The `atlas_ai` module supports this via `generate_with_search()`.

---

## Callsite Migration Map

### Tier: HAIKU

| File | Current Function | Line(s) | New Call |
|------|-----------------|---------|----------|
| oracle_cog.py | `_ai_blurb()` | 1148 | `generate(tier=HAIKU, max_tokens=120, temperature=0.8)` |
| genesis_cog.py | ability validation | 389 | `generate(tier=HAIKU)` |
| polymarket_cog.py | market description | 1755 | `generate(tier=HAIKU)` |
| polymarket_cog.py | `_gemini_curate()` | 2825 | `generate(tier=HAIKU, json_mode=True)` |
| cortex/cortex_analyst.py | `_call_flash()` | 125 | `generate_sync(tier=HAIKU, json_mode=True, temperature=0.1)` |

### Tier: SONNET

| File | Current Function | Line(s) | New Call |
|------|-----------------|---------|----------|
| codex_cog.py | `gemini_sql()` | 339 | `generate(tier=SONNET, temperature=0.05)` |
| codex_cog.py | `gemini_answer()` | 414 | `generate(tier=SONNET)` |
| codex_cog.py | SQL fix retry | 585 | `generate(tier=SONNET, temperature=0.02)` |
| codex_cog.py | `_h2h_impl()` rivalry summary | ~768 | `generate(tier=SONNET)` |
| codex_cog.py | `_season_recap_impl()` | ~836 | `generate(tier=SONNET)` |
| oracle_cog.py | `_claude_query()` | 574 | `generate_with_tools(tier=SONNET)` (already Claude) |
| oracle_cog.py | `_claude_blurb()` | 650 | `generate(tier=SONNET, max_tokens=120)` |
| oracle_cog.py | `_claude_chat()` | 671 | `generate(tier=SONNET)` |
| oracle_cog.py | ~7 Gemini analytics calls | 3118-3885 | `generate(tier=SONNET)` |
| sentinel_cog.py | force request analysis | ~777 | `generate(prompt=[image_block, text_block], tier=SONNET)` |
| sentinel_cog.py | `_analyze_screenshot_sync` | ~2402 | `generate(prompt=[image_block, text_block], tier=SONNET)` |
| sentinel_cog.py | dispute resolution | ~2402+ | `generate(tier=SONNET)` |
| reasoning.py | `_call_analyst()` | 452 | `generate(tier=SONNET, temperature=0.2)` |
| reasoning.py | retry on exec failure | 564 | `generate(tier=SONNET, temperature=0.1)` |
| echo_voice_extractor.py | `_flash_inner()` | 392 | `generate_sync(tier=SONNET, json_mode=True)` |

### Tier: OPUS

| File | Current Function | Line(s) | New Call |
|------|-----------------|---------|----------|
| cortex/cortex_writer.py | `_synthesize()` | 88 | `generate_sync(tier=OPUS, max_tokens=8192)` (sync CLI) |
| echo_voice_extractor.py | `_pro_inner()` | 404 | `generate_sync(tier=OPUS)` |

### Special: Gemini-primary (reversed fallback)

| File | Current Function | Line(s) | New Call |
|------|-----------------|---------|----------|
| bot.py | `call_atlas()` | 291 | `generate_with_search(system=persona)` |

**Note on line numbers:** Line numbers are approximate and will shift as earlier phases
modify files. The implementing session should grep for function names, not rely on line numbers.

---

## Migration Phases

Each phase is independently testable. A new session should execute these in order.

### Phase 1: Create `atlas_ai.py`
- Implement `generate()`, `generate_with_tools()`, `generate_stream()`, `generate_with_search()`, `generate_sync()`
- Implement `Tier`, `AIResult`, client singletons, fallback logic, JSON mode, multimodal support
- Verify the Haiku model ID against the Anthropic API (use `claude-haiku-3-5-20241022` if 4.5 doesn't exist)
- Test with a simple script: verify Claude primary works, verify Gemini fallback triggers on bad API key

### Phase 2: Migrate `oracle_cog.py` (~12 callsites)
- Replace `_get_gemini_client()` and `_get_anthropic_client()` with `atlas_ai` imports
- **Migrate Gemini callsites first** (7 analytics calls + `_ai_blurb`), keeping existing Claude callsites working as a regression check
- Then migrate existing Claude callsites (`_claude_query`, `_claude_blurb`, `_claude_chat`) to `atlas_ai`
- Update helper functions to return `AIResult` instead of `str` where embeds need fallback indicator
- Remove dead client init functions
- Test: run Oracle hub commands, verify Claude answers, simulate fallback

### Phase 3: Migrate `codex_cog.py` (5 callsites)
- Replace `gemini_sql()`, `gemini_answer()`, SQL fix retry, `_h2h_impl()` summary, `_season_recap_impl()` summary
- Also check `codex_intents.py` — it receives `self.gemini` as a parameter from codex_cog (line ~524). Either migrate its internal Gemini calls or change the parameter to accept `atlas_ai` tier config
- Remove `self.gemini` client from CodexCog
- Remove `from google import genai` import
- Test: `/ask`, `/h2h`, `/season_recap`

### Phase 4: Migrate `bot.py` (1 callsite — special case)
- Replace `call_atlas()` with `atlas_ai.generate_with_search()`
- Remove global `gemini_client` init (line 176)
- Test: @mention ATLAS in Discord, verify Google Search still works

### Phase 5: Migrate `sentinel_cog.py`, `genesis_cog.py`, `polymarket_cog.py`
- sentinel: 3 callsites including 2 multimodal vision calls (use `prompt=[image_block, text_block]`)
- genesis: 1 callsite
- polymarket: 2 callsites
- Replace each file's `_get_gemini_client()` / `_get_sentinel_gemini()` patterns
- Test: sentinel complaint flow, genesis ability validation, polymarket curation

### Phase 6: Migrate `reasoning.py`, `cortex/`, `echo_voice_extractor.py`
- reasoning.py: 2 callsites → `generate(tier=SONNET)` (async, runs in Discord context)
- cortex_analyst.py: 1 callsite → `generate_sync(tier=HAIKU, json_mode=True)` (sync CLI)
- cortex_writer.py: 1 callsite → `generate_sync(tier=OPUS, max_tokens=8192)` (sync CLI — note explicit 8192 to avoid truncation)
- echo_voice_extractor.py: 2 callsites → `generate_sync(tier=SONNET)` + `generate_sync(tier=OPUS)` (sync CLI, replaces tenacity-wrapped sync Gemini calls)
- **Important**: echo_voice_extractor uses tenacity retry decorators. Replace with `generate_sync()` which is natively sync. Remove tenacity wrappers since `atlas_ai` handles fallback internally.
- Test by running CLI scripts directly

### Phase 7: Cleanup
- Remove all dead `_get_gemini_client()`, `_get_anthropic_client()` functions from migrated files
- Remove unused `from google import genai` imports
- Remove `gemini_client` global from bot.py
- Update CLAUDE.md to reflect new architecture (add `atlas_ai.py` to Module Map, update env vars)
- Bump `ATLAS_VERSION` in bot.py

---

## Files to Create

| File | Purpose |
|------|---------|
| `atlas_ai.py` | Centralized AI client module |

## Files to Modify

| File | Callsites | Changes |
|------|-----------|---------|
| `oracle_cog.py` | ~12 | Replace all Gemini + Claude callsites, remove client init |
| `codex_cog.py` | 5 | Replace gemini_sql/answer + h2h/recap summaries, remove genai import + client |
| `codex_intents.py` | TBD | Check if it makes internal Gemini calls via passed client param |
| `bot.py` | 1 | Replace call_atlas(), remove global gemini_client (line 176) |
| `sentinel_cog.py` | 3 | Replace including 2 multimodal vision calls, remove client init |
| `genesis_cog.py` | 1 | Replace ability validation, remove client init |
| `polymarket_cog.py` | 2 | Replace description gen + curation, remove client init |
| `reasoning.py` | 2 | Replace analyst + retry, remove genai import |
| `cortex/cortex_analyst.py` | 1 | Replace with generate_sync(), remove genai import |
| `cortex/cortex_writer.py` | 1 | Replace with generate_sync(), remove genai import |
| `echo_voice_extractor.py` | 2 | Replace with generate_sync(), remove genai + tenacity |
| `CLAUDE.md` | — | Update architecture docs |

## Environment Variables

| Var | Required | Purpose |
|-----|----------|---------|
| `ANTHROPIC_API_KEY` | Yes (primary) | Claude API access |
| `GEMINI_API_KEY` | Yes (fallback + search) | Gemini fallback + Google Search in call_atlas |

Both keys should be set. If only one is available, the module uses whichever is configured (no fallback, just the available provider).

## Verification

After each phase:
1. Import the modified module without errors
2. Run the relevant Discord commands (or CLI scripts for Phase 6)
3. Verify Claude is answering (check embed footer for absence of fallback indicator)
4. Test fallback by temporarily removing `ANTHROPIC_API_KEY` and confirming Gemini takes over with `⚡ via Gemini fallback` indicator
