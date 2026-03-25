"""
god_cog.py — ATLAS GOD-Tier Administration (/god)
─────────────────────────────────────────────────────────────────────────────
Destructive and privileged operations gated behind the "GOD" Discord role.

Role hierarchy: GOD → Commissioner → TSL Owner → User

Commands:
    /god affinity <user> [reset]  — view or reset a user's affinity score
    /god rebuilddb                — force full tsl_history.db rebuild
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_god

log = logging.getLogger("atlas.god")

# ── Optional dependency: affinity ────────────────────────────────────────────

try:
    import affinity as affinity_mod
    _AFFINITY_AVAILABLE = True
except ImportError:
    affinity_mod = None
    _AFFINITY_AVAILABLE = False


class GodCog(commands.Cog):
    """ATLAS GOD — privileged administration tier."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    god_group = app_commands.Group(
        name="god",
        description="ATLAS God-tier administration.",
        default_permissions=discord.Permissions(administrator=True),
    )

    @god_group.command(name="affinity", description="View or reset a user's ATLAS affinity score.")
    @app_commands.describe(user="The user to check", reset="Reset their score to 0?")
    async def god_affinity(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reset: bool = False,
    ):
        if not await is_god(interaction):
            return await interaction.response.send_message(
                "ATLAS: This command requires the GOD role.", ephemeral=True,
            )
        if not _AFFINITY_AVAILABLE:
            return await interaction.response.send_message(
                "❌ Affinity system not loaded.", ephemeral=True,
            )
        if reset:
            await affinity_mod.reset_affinity(user.id)
            await interaction.response.send_message(
                f"🔄 Reset affinity for **{user.display_name}** to 0.", ephemeral=True,
            )
        else:
            score = await affinity_mod.get_affinity(user.id)
            tier = affinity_mod.get_tier_label(score)
            embed = discord.Embed(
                title=f"User Affinity — {user.display_name}",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Score", value=f"`{score:.1f}`", inline=True)
            embed.add_field(name="Tier", value=tier, inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @god_group.command(name="rebuilddb", description="Force full rebuild of tsl_history.db.")
    async def god_rebuilddb(self, interaction: discord.Interaction):
        if not await is_god(interaction):
            return await interaction.response.send_message(
                "ATLAS: This command requires the GOD role.", ephemeral=True,
            )
        await interaction.response.defer(thinking=True)
        loop = asyncio.get_running_loop()
        try:
            import data_manager as dm
            import build_tsl_db as db_builder
            players = dm.get_players()
            abilities = dm.get_player_abilities()
            db_result = await loop.run_in_executor(
                None,
                lambda: db_builder.sync_tsl_db(players=players, abilities=abilities),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ DB rebuild failed: `{e}`")
            return

        if db_result["success"]:
            lines = [
                f"**tsl_history.db rebuilt** in {db_result['elapsed']}s",
                f"Games: **{db_result['games']}**",
                f"Players: **{db_result['players']}**",
            ]
            if db_result.get("errors"):
                lines.append(f"Warnings: {', '.join(db_result['errors'][:3])}")
        else:
            lines = [f"DB rebuild failed: {', '.join(db_result['errors'][:3])}"]

        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(GodCog(bot))
        print("ATLAS: GOD · Privileged Administration loaded. ⚡")
    except Exception as e:
        print(f"ATLAS: GOD · FAILED to load ({e})")
