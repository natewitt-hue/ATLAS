"""
echo_cog.py - ATLAS Echo Discord Cog
======================================
Handles:
  - /atlas echostatus admin command (shows current persona state)

Load in bot.py setup_hook():
    await bot.load_extension("echo_cog")
"""

import discord
from discord.ext import commands
from atlas_colors import AtlasColors

from echo_loader import get_persona_status


class EchoCog(commands.Cog):
    """ATLAS Echo - Voice persona management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _echostatus_impl(self, interaction: discord.Interaction):
        """Core echo status logic."""
        status = get_persona_status()
        info = status.get("unified", {})

        embed = discord.Embed(
            title="ATLAS Echo - Persona Status",
            color=AtlasColors.TSL_BLUE,
        )
        embed.add_field(
            name="Unified Persona",
            value=(
                f"**Status:** ACTIVE\n"
                f"**Mode:** {info.get('mode', 'inline')}\n"
                f"**Size:** {info.get('char_count', 0):,} chars"
            ),
            inline=False,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EchoCog(bot))

    # Register echo commands on the /atlas group from bot.py
    import importlib
    bot_module = importlib.import_module("bot")
    atlas_group = bot_module.atlas_group

    @atlas_group.command(name="echostatus", description="Check current Echo persona status.")
    async def atlas_echostatus(interaction: discord.Interaction):
        cog = bot.get_cog("EchoCog")
        if cog:
            await cog._echostatus_impl(interaction)
        else:
            await interaction.response.send_message("Echo cog not loaded.", ephemeral=True)

    print("ATLAS: Echo · Voice Engine loaded.")
