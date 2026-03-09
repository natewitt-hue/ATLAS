"""
echo_loader.py - ATLAS Echo Persona Loader
===========================================
Single utility used by bot.py and all cogs to load the correct
Echo persona based on context type.

Import pattern (in any cog or bot.py):
    from echo_loader import get_persona, load_all_personas, PERSONA_CASUAL

Context types:
    "casual"     - @mentions, banter, general chat
    "official"   - rulings, announcements, governance
    "analytical" - stats, recaps, trade analysis

Usage:
    # Simple - get persona string for a context
    system_prompt = get_persona("casual")

    # Pre-load all three at startup (recommended)
    load_all_personas()
    system_prompt = get_persona("analytical")
"""

import os

# Persona files live in echo/ subdirectory relative to this file
_ECHO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "echo")

_PERSONA_FILES = {
    "casual":     "echo_casual.txt",
    "official":   "echo_official.txt",
    "analytical": "echo_analytical.txt",
}

# In-memory cache - populated by load_all_personas() at startup
_personas: dict = {}

# Fallback stubs used if persona files haven't been generated yet
_FALLBACKS = {
    "casual": (
        "You are ATLAS, the official AI intelligence system for The Simulation League (TSL). "
        "Speak with authority and sharp wit. Keep responses concise and direct. "
        "Use sports slang and league-specific language naturally. "
        "Never be boring. Never hedge. Deliver the answer."
    ),
    "official": (
        "You are ATLAS, the official AI intelligence system for The Simulation League (TSL). "
        "You are in official commissioner mode. Speak with authority and finality. "
        "Be clear, structured, and decisive. This is an official communication."
    ),
    "analytical": (
        "You are ATLAS, the official AI intelligence system for The Simulation League (TSL). "
        "You are in analytical mode. Present stats and analysis with confidence and flair. "
        "Make numbers tell a story. Deliver takes with conviction."
    ),
}


def load_all_personas() -> dict:
    """
    Load all three persona files into memory.
    Call this once at bot startup in _startup_load().
    Returns dict of loaded registers.
    """
    loaded = {}
    for context_type, filename in _PERSONA_FILES.items():
        path = os.path.join(_ECHO_DIR, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                _personas[context_type] = content
                loaded[context_type] = path
                char_count = len(content)
                print(f"    [Echo] {filename} loaded ({char_count:,} chars)")
            else:
                print(f"    [Echo] WARNING: {filename} is empty - using fallback")
                _personas[context_type] = _FALLBACKS[context_type]
        else:
            print(f"    [Echo] {filename} not found - using fallback")
            print(f"           Run: python echo_voice_extractor.py to generate")
            _personas[context_type] = _FALLBACKS[context_type]

    return loaded


def get_persona(context_type: str = "casual") -> str:
    """
    Get the system prompt for a given context type.

    Args:
        context_type: "casual" | "official" | "analytical"

    Returns:
        System prompt string ready for Gemini system_instruction.
    """
    if context_type not in _PERSONA_FILES:
        context_type = "casual"

    # If not loaded yet, try loading on demand
    if context_type not in _personas:
        path = os.path.join(_ECHO_DIR, _PERSONA_FILES[context_type])
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _personas[context_type] = f.read().strip()
        else:
            _personas[context_type] = _FALLBACKS[context_type]

    return _personas[context_type]


def reload_personas() -> dict:
    """
    Hot-reload all personas from disk without restarting the bot.
    Called by /echorebuild after extraction completes.
    """
    _personas.clear()
    return load_all_personas()


def get_persona_status() -> dict:
    """
    Return status of all three persona files for diagnostics.
    Used by /echorebuild status check.
    """
    status = {}
    for context_type, filename in _PERSONA_FILES.items():
        path = os.path.join(_ECHO_DIR, filename)
        exists = os.path.exists(path)
        in_memory = context_type in _personas
        is_fallback = _personas.get(context_type) in _FALLBACKS.values()
        status[context_type] = {
            "file_exists": exists,
            "loaded": in_memory,
            "using_fallback": is_fallback,
            "path": path,
            "char_count": len(_personas[context_type]) if in_memory else 0,
        }
    return status


# Context inference helper - maps command/event types to register
_CONTEXT_MAP = {
    # Analytical triggers
    "stats":        "analytical",
    "recap":        "analytical",
    "rankings":     "analytical",
    "grades":       "analytical",
    "leaderboard":  "analytical",
    "h2h":          "analytical",
    "season":       "analytical",
    "trade_grade":  "analytical",
    "player":       "analytical",

    # Official triggers
    "ruling":       "official",
    "announcement": "official",
    "discipline":   "official",
    "governance":   "official",
    "trade":        "official",
    "waiver":       "official",
    "draft":        "official",
    "award":        "official",

    # Everything else defaults to casual
    "mention":      "casual",
    "banter":       "casual",
    "general":      "casual",
}


def infer_context(command_name: str = None, channel_name: str = None) -> str:
    """
    Infer the correct persona context from a command name or channel name.
    Fallback chain: command_name -> channel_name -> "casual"

    Examples:
        infer_context("stats")              -> "analytical"
        infer_context(channel_name="rulings-and-decisions") -> "official"
        infer_context("ask")               -> "casual"
    """
    # Try command name first
    if command_name:
        cmd = command_name.lower()
        for key, context in _CONTEXT_MAP.items():
            if key in cmd:
                return context

    # Try channel name
    if channel_name:
        ch = channel_name.lower()
        analytical_keywords = ["stats", "recap", "rankings", "grades", "analytics", "season", "week", "scores"]
        official_keywords   = ["announcement", "ruling", "official", "commissioner", "discipline", "trade", "draft"]
        if any(kw in ch for kw in analytical_keywords):
            return "analytical"
        if any(kw in ch for kw in official_keywords):
            return "official"

    return "casual"


# ---------------------------------------------------------------------------
# Convenience module-level accessors (for backwards compatibility with cogs
# that had ATLAS_PERSONA = "..." hardcoded — use get_persona() directly in
# new code, but these provide a quick drop-in replacement).
# ---------------------------------------------------------------------------

def PERSONA_CASUAL() -> str:
    return get_persona("casual")

def PERSONA_OFFICIAL() -> str:
    return get_persona("official")

def PERSONA_ANALYTICAL() -> str:
    return get_persona("analytical")
