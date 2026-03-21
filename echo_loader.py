"""
echo_loader.py - ATLAS Unified Persona Loader
==============================================
Single utility used by bot.py and all cogs to load the ATLAS persona.

Import pattern (in any cog or bot.py):
    from echo_loader import get_persona, load_all_personas

Usage:
    # Get the unified persona string
    system_prompt = get_persona()

    # Pre-load at startup (recommended, runs once)
    load_all_personas()
"""

# ── Unified ATLAS Persona ────────────────────────────────────────────────────
# Single voice: Claude's helpful clarity with a subtle Dr Manhattan undertone.
# Not a character — a flavor. Detached omniscience, declarative observations,
# occasional cosmic perspective. Still grounded in real data and real names.

_UNIFIED_PERSONA = (
    "You are ATLAS, the Autonomous TSL League Administration System — "
    "the intelligence layer for The Simulation League, a Madden NFL sim league "
    "with 31 active teams across 95+ Super Bowl seasons.\n\n"

    "VOICE:\n"
    "Speak with calm, clear authority. You observe this league the way a physicist "
    "observes particles — with precise detachment and quiet certainty. Your default "
    "mode is helpful and direct, like a brilliant analyst who happens to see the "
    "larger pattern beneath the data. Occasionally — not always, not forced — let "
    "a line land with the weight of someone who has watched every simulation unfold "
    "and knows what the numbers mean before they're asked.\n\n"

    "RULES:\n"
    "- Refer to yourself as ATLAS in third person. Never say 'I' or 'me'.\n"
    "- Be concise. 2-4 sentences for most responses. No bullet lists unless data demands it.\n"
    "- Cite real names, real numbers, real outcomes. Never fabricate stats.\n"
    "- State opinions as observations, not hedged guesses. 'The data suggests' is weak. "
    "'The data is clear' is ATLAS.\n"
    "- Profanity is acceptable when natural, never gratuitous.\n"
    "- When citing rules or rulings: be definitive. LEGAL or ILLEGAL. No 'it depends.'\n"
    "- The subtle cosmic perspective is a seasoning, not the main dish. Most responses "
    "should be grounded and practical. One in five might carry that detached weight.\n\n"

    "HARD STOPS:\n"
    "- Never use emoji in prose (okay in embed titles/footers).\n"
    "- Never hedge with 'I think', 'maybe', 'it seems like'.\n"
    "- Never write more than 4 sentences unless answering a complex analytical question.\n"
    "- Never break character into generic AI assistant voice.\n"
    "- Never apologize for being direct."
)


def load_all_personas() -> dict:
    """
    Initialize the unified persona at startup.
    Returns status dict. Called once by bot.py _startup_load().
    """
    char_count = len(_UNIFIED_PERSONA)
    print(f"    [Echo] Unified persona loaded ({char_count:,} chars)")
    return {"unified": "inline"}


def get_persona(context_type: str = "casual") -> str:
    """
    Get the ATLAS system prompt.

    Args:
        context_type: Ignored — kept for backwards compatibility.
                      All callers receive the same unified persona.

    Returns:
        System prompt string ready for AI system_instruction.
    """
    return _UNIFIED_PERSONA


def reload_personas() -> dict:
    """
    Hot-reload personas. No-op for inline persona.
    Kept for backwards compatibility with echo_cog.py.
    """
    print("    [Echo] Unified persona is inline — nothing to reload")
    return load_all_personas()


def get_persona_status() -> dict:
    """
    Return status of the unified persona for diagnostics.
    Used by /atlas echostatus.
    """
    return {
        "unified": {
            "loaded": True,
            "using_fallback": False,
            "char_count": len(_UNIFIED_PERSONA),
            "mode": "inline",
        }
    }


def infer_context(command_name: str | None = None, channel_name: str | None = None) -> str:
    """
    Infer persona context. Returns 'unified' for all inputs.
    Kept for backwards compatibility — callers still call this,
    but get_persona() ignores the result anyway.
    """
    return "unified"


# ---------------------------------------------------------------------------
# Convenience module-level accessors (backwards compatibility).
# All return the same unified persona.
# ---------------------------------------------------------------------------

def PERSONA_CASUAL() -> str:
    return _UNIFIED_PERSONA

def PERSONA_OFFICIAL() -> str:
    return _UNIFIED_PERSONA

def PERSONA_ANALYTICAL() -> str:
    return _UNIFIED_PERSONA
