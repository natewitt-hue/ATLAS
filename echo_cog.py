"""
echo_cog.py - ATLAS Echo Discord Cog
======================================
Handles:
  - Persona loading at bot startup
  - /echorebuild admin command (triggers re-extraction + hot reload)
  - /echostatus admin command (shows current persona state)

Load in bot.py setup_hook():
    await bot.load_extension("echo_cog")

After merge into ATLAS, this cog should live at:
    ATLAS/echo_cog.py
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from echo_loader import load_all_personas, reload_personas, get_persona_status


class EchoCog(commands.Cog):
    """ATLAS Echo - Voice persona management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._admin_ids: list[int] = []

        # Pull admin IDs from bot if available
        import os
        raw = os.getenv("ADMIN_USER_IDS", "")
        self._admin_ids = [int(x) for x in raw.split(",") if x.strip()]

    @commands.Cog.listener()
    async def on_ready(self):
        """Load personas when bot is ready."""
        pass  # Startup loading handled by _startup_load in bot.py

    # ── /echorebuild ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="echorebuild",
        description="[Admin] Regenerate Echo voice personas from message archive."
    )
    async def echorebuild(self, interaction: discord.Interaction):
        """
        Admin only. Triggers a full re-extraction of the commissioner voice
        from the TSL_Archive.db message archive, then hot-reloads all personas
        without requiring a bot restart.
        """
        if interaction.user.id not in self._admin_ids:
            await interaction.response.send_message(
                "ATLAS: Echo rebuild is restricted to admins.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            # Run extraction in thread executor - this takes several minutes
            loop = asyncio.get_running_loop()

            def run():
                from echo_voice_extractor import run_extraction
                return run_extraction(verbose=True)

            paths = await loop.run_in_executor(None, run)

            # Hot-reload the new personas
            loaded = reload_personas()

            # Build response embed
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

    # ── /echostatus ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="echostatus",
        description="[Admin] Check current Echo persona status."
    )
    async def echostatus(self, interaction: discord.Interaction):
        """Admin only. Shows the current state of all three Echo persona files."""
        if interaction.user.id not in self._admin_ids:
            await interaction.response.send_message(
                "ATLAS: Admin only.", ephemeral=True
            )
            return

        status = get_persona_status()

        embed = discord.Embed(
            title="ATLAS Echo - Persona Status",
            color=discord.Color.from_rgb(30, 144, 255)
        )

        status_lines = []
        for register, info in status.items():
            if info["using_fallback"]:
                icon = "FALLBACK"
                note = "Run /echorebuild to generate"
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


# ── Import guard for os in echorebuild ──────────────────────────────────────
import os


async def setup(bot: commands.Bot):
    await bot.add_cog(EchoCog(bot))
    print("ATLAS: Echo · Voice Engine loaded.")
