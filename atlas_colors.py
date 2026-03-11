"""
ATLAS -- Color System
Centralized brand colors for all embeds and UI elements.

Usage:
    from atlas_colors import AtlasColors

    embed = discord.Embed(
        title="TSL Sportsbook",
        color=AtlasColors.SPORTSBOOK
    )
"""

import discord


class AtlasColors:
    """Centralized color palette for ATLAS embeds and UI elements.

    Every hub, embed, and persistent card should pull from this class
    instead of using raw hex values or discord.Color presets. This
    ensures visual consistency across the entire bot.
    """

    # -- Module Colors (Primary) ----------------------------------------
    SPORTSBOOK  = discord.Color(0x1A73E8)  # Blue -- betting hub
    CASINO      = discord.Color(0xC9962A)  # ATLAS Gold -- casino/gambling
    STATS       = discord.Color(0x7B1FA2)  # Purple -- analytics/stats
    ECONOMY     = discord.Color(0x0D652D)  # Green -- money/economy
    SENTINEL    = discord.Color(0xD93025)  # Red -- rules/enforcement
    CODEX       = discord.Color(0x5F6368)  # Gray -- history/archive
    ORACLE      = discord.Color(0x0097A7)  # Teal -- AI analysis
    TRADE       = discord.Color(0xE65100)  # Orange -- trades/roster
    GENESIS     = discord.Color(0x1B5E20)  # Deep Green -- draft (future)
    ECHO        = discord.Color(0x4A148C)  # Deep Purple -- comms/voice

    # -- Brand Colors ---------------------------------------------------
    TSL_GOLD    = discord.Color(0xC9962A)  # Canonical ATLAS gold
    TSL_DARK    = discord.Color(0x0A0A0A)  # Near-black background
    TSL_BLUE    = discord.Color(0x1E90FF)  # Dodger blue accent

    # -- Status Colors --------------------------------------------------
    SUCCESS     = discord.Color(0x34A853)  # Confirmation, wins, approvals
    WARNING     = discord.Color(0xFBBC04)  # Caution, pending, locks
    ERROR       = discord.Color(0xEA4335)  # Failures, denials, violations
    INFO        = discord.Color(0x4285F4)  # Neutral info, system messages

    # -- Commissioner / Admin -------------------------------------------
    COMMISH     = discord.Color(0x202124)  # Near-black -- admin console

    # -- Convenience Mapping --------------------------------------------
    # Maps module names (lowercase) to their color for dynamic lookup.
    # Usage: AtlasColors.by_module("sportsbook")
    _MODULE_MAP = {
        "sportsbook": 0x1A73E8,
        "casino":     0xC9962A,
        "stats":      0x7B1FA2,
        "economy":    0x0D652D,
        "sentinel":   0xD93025,
        "codex":      0x5F6368,
        "oracle":     0x0097A7,
        "trade":      0xE65100,
        "genesis":    0x1B5E20,
        "echo":       0x4A148C,
        "commish":    0x202124,
    }

    @classmethod
    def by_module(cls, module_name: str) -> discord.Color:
        """Look up a module color by name string.

        Args:
            module_name: Lowercase module name (e.g., "sportsbook").

        Returns:
            discord.Color for the module, or INFO as fallback.
        """
        hex_val = cls._MODULE_MAP.get(module_name.lower(), 0x4285F4)
        return discord.Color(hex_val)
