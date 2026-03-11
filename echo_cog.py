"""
echo_cog.py - ATLAS Echo Discord Cog
======================================
Handles:
  - Persona loading at bot startup
  - /atlas echorebuild admin command (triggers re-extraction + hot reload)
  - /atlas echostatus admin command (shows current persona state)

Load in bot.py setup_hook():
    await bot.load_extension("echo_cog")
"""

import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands

from echo_loader import load_all_personas, reload_personas, get_persona_status


class EchoCog(commands.Cog):
    """ATLAS Echo - Voice persona management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._admin_ids: list[int] = []

        raw = os.getenv("ADMIN_USER_IDS", "")
        self._admin_ids = [int(x) for x in raw.split(",") if x.strip()]

    # ── Implementation methods (called by /atlas commands)

    async def _echorebuild_impl(self, interaction: discord.Interaction):
        """Core echo rebuild logic."""
        await interaction.response.defer(thinking=True)

        try:
            loop = asyncio.get_running_loop()

            def run():
                from echo_voice_extractor import run_extraction
                return run_extraction(verbose=True)

            paths = await loop.run_in_executor(None, run)
            loaded = reload_personas()

            embed = discord.Embed(
                title="ATLAS Echo - Voice Rebuild Complete",
                color=discord.Color.from_rgb(201, 150, 42)
            )
            embed.add_field(
                name="Personas Generated",
                value="\n".join(
                    f"**{reg}**: `{os.path.basename(path)}`"
                    for reg, path in paths.items()
                ),
                inline=False
            )
            embed.add_field(
                name="Status",
                value="All personas hot-loaded. No restart required.",
                inline=False
            )
            embed.set_footer(text="ATLAS Echo is live with the updated voice.")

            await interaction.followup.send(embed=embed)

        except FileNotFoundError as e:
            await interaction.followup.send(
                f"ATLAS Echo rebuild failed: `{e}`\n"
                f"Ensure `ORACLE_DB_PATH` is set in `.env` and the archive DB exists."
            )
        except Exception as e:
            await interaction.followup.send(
                f"ATLAS Echo rebuild failed unexpectedly: `{e}`"
            )

    async def _echostatus_impl(self, interaction: discord.Interaction):
        """Core echo status logic."""
        status = get_persona_status()

        embed = discord.Embed(
            title="ATLAS Echo - Persona Status",
            color=discord.Color.from_rgb(30, 144, 255)
        )

        status_lines = []
        for register, info in status.items():
            if info["using_fallback"]:
                icon = "FALLBACK"
                note = "Run /atlas echorebuild to generate"
            elif info["loaded"]:
                icon = "LIVE"
                note = f"{info['char_count']:,} chars"
            else:
                icon = "NOT LOADED"
                note = "File exists but not loaded"

            status_lines.append(
                f"**{register.upper()}** [{icon}]\n"
                f"  {note}"
            )

        embed.add_field(
            name="Registers",
            value="\n".join(status_lines),
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)



async def setup(bot: commands.Bot):
    await bot.add_cog(EchoCog(bot))

    # Register echo commands on the /atlas group from bot.py
    from bot import atlas_group

    @atlas_group.command(name="echorebuild", description="Regenerate Echo voice personas from message archive.")
    async def atlas_echorebuild(interaction: discord.Interaction):
        cog = bot.get_cog("EchoCog")
        if cog:
            await cog._echorebuild_impl(interaction)
        else:
            await interaction.response.send_message("Echo cog not loaded.", ephemeral=True)

    @atlas_group.command(name="echostatus", description="Check current Echo persona status.")
    async def atlas_echostatus(interaction: discord.Interaction):
        cog = bot.get_cog("EchoCog")
        if cog:
            await cog._echostatus_impl(interaction)
        else:
            await interaction.response.send_message("Echo cog not loaded.", ephemeral=True)

    print("ATLAS: Echo · Voice Engine loaded.")
