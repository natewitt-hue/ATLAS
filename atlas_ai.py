"""atlas_ai — Centralized AI client for ATLAS.

Claude primary, Gemini fallback. Every cog calls this module instead of
managing its own AI client.

Public API
----------
generate()             – async text generation (Discord cog callsites)
generate_with_tools()  – async tool-use (Oracle QueryBuilder flow)
generate_with_search() – async Gemini-primary with Google Search
generate_stream()      – async streaming iterator (future use)
generate_sync()        – sync text generation (cortex, echo_voice_extractor CLIs)

Tier enum selects model capability:
    HAIKU  – fast, cheap (blurbs, classification)
    SONNET – balanced (SQL gen, chat, analysis)
    OPUS   – complex reasoning (synthesis, long-form)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator

log = logging.getLogger("atlas_ai")

# ── Tier & Result ────────────────────────────────────────────────────────────


class Tier(Enum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


@dataclass
class AIResult:
    text: str
    provider: str  # "claude" or "gemini"
    model: str  # actual model ID used
    tool_calls: list[dict] = field(default_factory=list)
    fallback_used: bool = False
    _raw_content: list = field(default_factory=list, repr=False)
    grounding_chunks: list[dict] = field(default_factory=list)  # [{uri, title, domain}]
    search_queries: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


# ── Model mapping ────────────────────────────────────────────────────────────

_CLAUDE_MODELS = {
    Tier.HAIKU: "claude-haiku-4-5-20251001",
    Tier.SONNET: "claude-sonnet-4-20250514",
    Tier.OPUS: "claude-opus-4-20250514",
}

_GEMINI_MODELS = {
    Tier.HAIKU: "gemini-2.0-flash",
    Tier.SONNET: "gemini-2.0-flash",
    Tier.OPUS: "gemini-2.0-flash",  # 2.5-pro requires thinking mode which can empty-response with low max_tokens
}

# ── Client singletons ────────────────────────────────────────────────────────

_claude_client = None
_gemini_client = None


def _get_claude():
    """Lazy singleton. Returns None if ANTHROPIC_API_KEY not set."""
    global _claude_client
    if _claude_client is not None:
        return _claude_client
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
        _claude_client = Anthropic(api_key=api_key)
        return _claude_client
    except Exception as e:
        log.warning(f"[atlas_ai] Failed to init Claude client: {e}")
        return None


def _get_gemini():
    """Lazy singleton. Returns None if GEMINI_API_KEY not set."""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        _gemini_client = genai.Client(api_key=api_key)
        return _gemini_client
    except Exception as e:
        log.warning(f"[atlas_ai] Failed to init Gemini client: {e}")
        return None


# ── Internal helpers (sync, pure) ────────────────────────────────────────────

def _build_claude_messages(prompt: str | list[dict]) -> list[dict]:
    """Convert prompt to Claude messages format."""
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    # list[dict] = content blocks (multimodal)
    return [{"role": "user", "content": prompt}]


def _apply_json_mode_prompt(prompt: str | list[dict]) -> str | list[dict]:
    """Append JSON instruction to prompt for Claude JSON mode."""
    suffix = "\n\nRespond with valid JSON only. No markdown fences, no explanation."
    if isinstance(prompt, str):
        return prompt + suffix
    # Multimodal: append to last text block or add new text block
    blocks = list(prompt)
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].get("type") == "text":
            blocks[i] = {**blocks[i], "text": blocks[i]["text"] + suffix}
            return blocks
    blocks.append({"type": "text", "text": suffix.strip()})
    return blocks


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences from JSON response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _call_claude(client, prompt, system, tier, max_tokens, temperature, json_mode):
    """Sync Claude API call. Returns AIResult."""
    model = _CLAUDE_MODELS[tier]
    effective_prompt = _apply_json_mode_prompt(prompt) if json_mode else prompt
    messages = _build_claude_messages(effective_prompt)

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature

    t0 = time.perf_counter()
    response = client.messages.create(**kwargs)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    text_parts = [b.text for b in response.content if hasattr(b, "text")]
    text = " ".join(text_parts).strip()

    in_tok = getattr(response.usage, "input_tokens", None)
    out_tok = getattr(response.usage, "output_tokens", None)

    if json_mode:
        text = _strip_json_fences(text)
        # Validate JSON; retry once on failure
        try:
            json.loads(text)
        except (json.JSONDecodeError, ValueError):
            retry_prompt = (
                f"Your previous response was not valid JSON. "
                f"Here is what you returned:\n{text}\n\n"
                f"Please fix and return ONLY valid JSON."
            )
            retry_msgs = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": retry_prompt},
            ]
            retry_kwargs = {k: v for k, v in kwargs.items() if k != "messages"}
            retry_kwargs["messages"] = retry_msgs
            retry_response = client.messages.create(**retry_kwargs)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            retry_parts = [b.text for b in retry_response.content if hasattr(b, "text")]
            text = _strip_json_fences(" ".join(retry_parts).strip())
            # Sum tokens from both calls
            r_in = getattr(retry_response.usage, "input_tokens", 0)
            r_out = getattr(retry_response.usage, "output_tokens", 0)
            in_tok = (in_tok or 0) + r_in
            out_tok = (out_tok or 0) + r_out

    return AIResult(
        text=text,
        provider="claude",
        model=model,
        _raw_content=list(response.content),
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=elapsed_ms,
    )


def _convert_to_gemini_parts(prompt):
    """Convert Claude-format content blocks to Gemini Part objects."""
    from google.genai import types

    if isinstance(prompt, str):
        return [prompt]

    parts = []
    for block in prompt:
        btype = block.get("type", "")
        if btype == "text":
            parts.append(types.Part.from_text(text=block["text"]))
        elif btype == "image":
            source = block["source"]
            data = source["data"]
            if isinstance(data, str):
                data = base64.b64decode(data)
            parts.append(types.Part.from_bytes(data=data, mime_type=source["media_type"]))
    return parts


def _call_gemini(client, prompt, system, tier, max_tokens, temperature, json_mode):
    """Sync Gemini API call. Returns AIResult."""
    from google.genai import types

    model = _GEMINI_MODELS[tier]
    contents = _convert_to_gemini_parts(prompt)

    config_kwargs = {}
    if system:
        config_kwargs["system_instruction"] = system
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if max_tokens:
        config_kwargs["max_output_tokens"] = max_tokens
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"

    config = types.GenerateContentConfig(**config_kwargs)

    t0 = time.perf_counter()
    response = client.models.generate_content(
        model=model,
        config=config,
        contents=contents,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    text = (response.text or "").strip()

    in_tok = out_tok = None
    try:
        um = response.usage_metadata
        in_tok = um.prompt_token_count
        out_tok = um.candidates_token_count
    except Exception:
        pass

    return AIResult(
        text=text,
        provider="gemini",
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=elapsed_ms,
    )


def _call_gemini_with_search(client, prompt, system, max_tokens):
    """Sync Gemini API call with Google Search tool. Returns AIResult."""
    from google.genai import types

    config_kwargs = {
        "tools": [{"google_search": {}}],
    }
    if system:
        config_kwargs["system_instruction"] = system
    if max_tokens:
        config_kwargs["max_output_tokens"] = max_tokens

    config = types.GenerateContentConfig(**config_kwargs)
    contents = _convert_to_gemini_parts(prompt)

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        config=config,
        contents=contents,
    )

    # Extract grounding metadata (web search citations)
    grounding_chunks = []
    search_queries = []
    try:
        candidate = response.candidates[0] if response.candidates else None
        meta = getattr(candidate, "grounding_metadata", None) if candidate else None
        if meta:
            for chunk in getattr(meta, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                if web:
                    grounding_chunks.append({
                        "uri": getattr(web, "uri", ""),
                        "title": getattr(web, "title", ""),
                        "domain": getattr(web, "domain", ""),
                    })
            search_queries = list(getattr(meta, "web_search_queries", None) or [])
    except Exception as e:
        print(f"[atlas_ai] Citation extraction failed: {e}")  # best-effort, non-fatal

    return AIResult(
        text=response.text.strip(),
        provider="gemini",
        model="gemini-2.0-flash",
        grounding_chunks=grounding_chunks,
        search_queries=search_queries,
    )


def _call_claude_with_tools(client, prompt, tools, system, tier, max_tokens):
    """Sync Claude tool-use call. Returns AIResult with tool_calls populated."""
    model = _CLAUDE_MODELS[tier]
    messages = _build_claude_messages(prompt)

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "tools": tools,
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)

    # Extract tool calls
    tool_calls = []
    for block in response.content:
        if block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })

    # Extract text
    text_parts = [b.text for b in response.content if hasattr(b, "text")]
    text = " ".join(text_parts).strip()

    return AIResult(
        text=text,
        provider="claude",
        model=model,
        tool_calls=tool_calls,
        _raw_content=list(response.content),
    )


def _convert_tools_to_gemini(tools):
    """Convert Claude tool definitions to Gemini function declarations."""
    from google.genai import types

    declarations = []
    for tool in tools:
        decl = types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters=tool.get("input_schema", {}),
        )
        declarations.append(decl)
    return [types.Tool(function_declarations=declarations)]


def _call_gemini_with_tools(client, prompt, tools, system, tier, max_tokens):
    """Sync Gemini tool-use call. Returns AIResult with tool_calls populated."""
    from google.genai import types

    model = _GEMINI_MODELS[tier]
    contents = _convert_to_gemini_parts(prompt)
    gemini_tools = _convert_tools_to_gemini(tools)

    config_kwargs = {"tools": gemini_tools}
    if system:
        config_kwargs["system_instruction"] = system
    if max_tokens:
        config_kwargs["max_output_tokens"] = max_tokens

    config = types.GenerateContentConfig(**config_kwargs)

    response = client.models.generate_content(
        model=model,
        config=config,
        contents=contents,
    )

    # Extract function calls from Gemini response
    tool_calls = []
    text_parts = []
    for part in response.candidates[0].content.parts:
        if hasattr(part, "function_call") and part.function_call:
            fc = part.function_call
            tool_calls.append({
                "id": fc.name,  # Gemini doesn't have separate IDs
                "name": fc.name,
                "input": dict(fc.args) if fc.args else {},
            })
        elif hasattr(part, "text") and part.text:
            text_parts.append(part.text)

    return AIResult(
        text=" ".join(text_parts).strip(),
        provider="gemini",
        model=model,
        tool_calls=tool_calls,
    )


# ── Public async API ─────────────────────────────────────────────────────────

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

    prompt: Plain string or list of content blocks for multimodal input.
    json_mode: Instructs the model to return valid JSON.
    """
    loop = asyncio.get_running_loop()

    claude = _get_claude()
    if claude:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _call_claude(claude, prompt, system, tier,
                                     max_tokens, temperature, json_mode),
            )
            return result
        except Exception as e:
            log.warning(f"[atlas_ai] Claude failed ({e}), falling back to Gemini")

    gemini = _get_gemini()
    if gemini:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _call_gemini(gemini, prompt, system, tier,
                                     max_tokens, temperature, json_mode),
            )
            result.fallback_used = True
            return result
        except Exception as e2:
            log.error(f"[atlas_ai] Gemini fallback also failed: {e2}")
            raise

    raise RuntimeError("No AI provider configured (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")


async def generate_with_tools(
    prompt: str,
    tools: list[dict],
    *,
    system: str = "",
    tier: Tier = Tier.SONNET,
    max_tokens: int = 1024,
) -> AIResult:
    """Generate with tool use. Claude primary, Gemini fallback."""
    loop = asyncio.get_running_loop()

    claude = _get_claude()
    if claude:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _call_claude_with_tools(claude, prompt, tools,
                                                system, tier, max_tokens),
            )
            return result
        except Exception as e:
            log.warning(f"[atlas_ai] Claude tools failed ({e}), falling back to Gemini")

    gemini = _get_gemini()
    if gemini:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _call_gemini_with_tools(gemini, prompt, tools,
                                                system, tier, max_tokens),
            )
            result.fallback_used = True
            return result
        except Exception as e2:
            log.error(f"[atlas_ai] Gemini tools fallback also failed: {e2}")
            raise

    raise RuntimeError("No AI provider configured")


async def generate_synthesis(
    messages: list[dict],
    tool_result_content: list[dict],
    *,
    system: str = "",
    tools: list[dict] | None = None,
    tier: Tier = Tier.SONNET,
    max_tokens: int = 800,
) -> AIResult:
    """Synthesize an answer from tool-use results (multi-turn continuation).

    Claude path: sends full multi-turn conversation (user → assistant w/ tool_use
    → user w/ tool_results) for synthesis.
    Gemini fallback: flattens tool results into a single prompt.

    Parameters
    ----------
    messages : list[dict]
        The conversation so far, e.g.:
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": <content_blocks>}]
    tool_result_content : list[dict]
        Tool result blocks, e.g.:
        [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]
    """
    loop = asyncio.get_running_loop()

    claude = _get_claude()
    if claude:
        try:
            def _do_synthesis():
                model = _CLAUDE_MODELS[tier]
                # Serialize _raw_content blocks if they're SDK objects
                serialized_messages = []
                for msg in messages:
                    content = msg.get("content")
                    if isinstance(content, list) and content and hasattr(content[0], "type"):
                        # SDK ContentBlock objects — convert to dicts
                        blocks = []
                        for b in content:
                            if b.type == "text":
                                blocks.append({"type": "text", "text": b.text})
                            elif b.type == "tool_use":
                                blocks.append({
                                    "type": "tool_use",
                                    "id": b.id,
                                    "name": b.name,
                                    "input": b.input,
                                })
                        serialized_messages.append({"role": msg["role"], "content": blocks})
                    else:
                        serialized_messages.append(msg)

                # Append tool results as the next user turn
                full_messages = serialized_messages + [
                    {"role": "user", "content": tool_result_content}
                ]

                kwargs = {"model": model, "max_tokens": max_tokens, "messages": full_messages}
                if system:
                    kwargs["system"] = system
                if tools:
                    kwargs["tools"] = tools

                response = claude.messages.create(**kwargs)
                text_parts = [b.text for b in response.content if hasattr(b, "text")]
                return AIResult(
                    text=" ".join(text_parts).strip(),
                    provider="claude",
                    model=model,
                    _raw_content=list(response.content),
                )

            result = await loop.run_in_executor(None, _do_synthesis)
            return result
        except Exception as e:
            log.warning(f"[atlas_ai] Claude synthesis failed ({e}), falling back to Gemini")

    # Gemini fallback: flatten tool results into a single prompt
    gemini = _get_gemini()
    if gemini:
        try:
            # Build a flat prompt from the conversation + results
            parts = []
            for msg in messages:
                content = msg.get("content")
                if isinstance(content, str):
                    parts.append(f"{msg['role'].upper()}: {content}")
                elif isinstance(content, list):
                    for b in content:
                        if hasattr(b, "text") and b.text:
                            parts.append(f"ASSISTANT: {b.text}")
                        elif hasattr(b, "name"):
                            parts.append(f"TOOL CALL: {b.name}({json.dumps(getattr(b, 'input', {}))})")
                        elif isinstance(b, dict) and b.get("type") == "text":
                            parts.append(f"ASSISTANT: {b['text']}")
            for tr in tool_result_content:
                parts.append(f"TOOL RESULT ({tr.get('tool_use_id', '?')}): {tr.get('content', '')}")
            parts.append("Based on the above tool results, provide a concise answer to the user's question.")
            flat_prompt = "\n\n".join(parts)

            result = await loop.run_in_executor(
                None,
                lambda: _call_gemini(gemini, flat_prompt, system, tier,
                                     max_tokens, None, False),
            )
            result.fallback_used = True
            return result
        except Exception as e2:
            log.error(f"[atlas_ai] Gemini synthesis fallback also failed: {e2}")
            raise

    raise RuntimeError("No AI provider configured")


async def generate_with_search(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 1024,
) -> AIResult:
    """Gemini-primary (has Google Search), Claude fallback (without search)."""
    loop = asyncio.get_running_loop()

    gemini = _get_gemini()
    if gemini:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _call_gemini_with_search(gemini, prompt, system, max_tokens),
            )
            return result
        except Exception as e:
            log.warning(f"[atlas_ai] Gemini search failed ({e}), falling back to Claude (no search)")

    claude = _get_claude()
    if claude:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _call_claude(claude, prompt, system, Tier.SONNET,
                                     max_tokens, None, False),
            )
            result.fallback_used = True
            return result
        except Exception as e2:
            log.error(f"[atlas_ai] Claude fallback also failed: {e2}")
            raise

    raise RuntimeError("No AI provider configured")


async def generate_stream(
    prompt: str,
    *,
    system: str = "",
    tier: Tier = Tier.SONNET,
    max_tokens: int = 4096,
    temperature: float | None = None,
) -> AsyncIterator[str]:
    """Stream text chunks. Claude primary, Gemini fallback (non-streaming).

    If Claude fails before the first chunk, falls back to Gemini non-streaming
    (full result yielded as one chunk).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    claude = _get_claude()
    if claude:
        model = _CLAUDE_MODELS[tier]

        def _run_stream():
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            if temperature is not None:
                kwargs["temperature"] = temperature

            with claude.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    loop.call_soon_threadsafe(queue.put_nowait, text)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        try:
            fut = loop.run_in_executor(None, _run_stream)
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
            await fut  # propagate exceptions
            return
        except Exception as e:
            log.warning(f"[atlas_ai] Claude stream failed ({e}), falling back to Gemini")

    # Fallback: Gemini non-streaming, yield full result as one chunk
    gemini = _get_gemini()
    if gemini:
        result = await loop.run_in_executor(
            None,
            lambda: _call_gemini(gemini, prompt, system, tier,
                                 max_tokens, temperature, False),
        )
        yield result.text
        return

    raise RuntimeError("No AI provider configured")


# ── Embedding API ────────────────────────────────────────────────────────────

async def embed_text(text: str) -> list[float] | None:
    """Generate a text embedding via Gemini text-embedding-004.

    Free tier: 1,500 requests/day. Returns a 768-dim float vector,
    or None if Gemini is unavailable or the call fails.
    Used by oracle_memory for permanent conversation memory search.
    """
    loop = asyncio.get_running_loop()
    gemini = _get_gemini()
    if not gemini:
        return None
    try:
        result = await loop.run_in_executor(
            None,
            lambda: gemini.models.embed_content(
                model="text-embedding-004",
                content=text,
            ),
        )
        return result.embeddings[0].values
    except Exception as e:
        log.warning(f"[atlas_ai] Embedding failed: {e}")
        return None


# ── Public sync API ──────────────────────────────────────────────────────────

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

    Uses the sync clients directly (no asyncio.run needed).
    Fallback to Gemini on failure.
    """
    claude = _get_claude()
    if claude:
        try:
            return _call_claude(claude, prompt, system, tier,
                                max_tokens, temperature, json_mode)
        except Exception as e:
            log.warning(f"[atlas_ai] Claude sync failed ({e}), falling back to Gemini")

    gemini = _get_gemini()
    if gemini:
        try:
            result = _call_gemini(gemini, prompt, system, tier,
                                  max_tokens, temperature, json_mode)
            result.fallback_used = True
            return result
        except Exception as e2:
            log.error(f"[atlas_ai] Gemini sync fallback also failed: {e2}")
            raise

    raise RuntimeError("No AI provider configured")
