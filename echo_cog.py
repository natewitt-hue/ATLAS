"""
echo_cog.py - ATLAS Echo Discord Cog
======================================
Voice persona management system.

Load in bot.py setup_hook():
    await bot.load_extension("echo_cog")
"""

import discord
from discord.ext import commands


class EchoCog(commands.Cog):
    """ATLAS Echo - Voice persona management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(EchoCog(bot))
    print("ATLAS: Echo · Voice Engine loaded.")
