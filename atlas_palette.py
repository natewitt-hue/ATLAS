"""ATLAS™ — Unified color palette for all embeds and renders."""
import discord

# ── Brand colors ───────────────────────────────────────────────────────────
ATLAS_GOLD    = discord.Color.from_rgb(212, 175, 55)   # 0xD4AF37 — primary brand
ATLAS_BLACK   = discord.Color.from_rgb(26, 26, 26)     # 0x1A1A1A

# ── Semantic colors ────────────────────────────────────────────────────────
ATLAS_SUCCESS = discord.Color.from_rgb(34, 197, 94)    # 0x22C55E — win / positive
ATLAS_ERROR   = discord.Color.from_rgb(239, 68, 68)    # 0xEF4444 — loss / negative
ATLAS_WARNING = discord.Color.from_rgb(245, 158, 11)   # 0xF59E0B — push / caution
ATLAS_INFO    = discord.Color.from_rgb(59, 130, 246)   # 0x3B82F6 — info / neutral

# ── Hex values for Pillow / HTML renderers ─────────────────────────────────
HEX_GOLD    = 0xD4AF37
HEX_SUCCESS = 0x22C55E
HEX_ERROR   = 0xEF4444
HEX_WARNING = 0xF59E0B
