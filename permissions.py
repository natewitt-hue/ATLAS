"""
permissions.py — ATLAS Centralized Permission & Channel Routing
================================================================
Provides unified permission checks and channel routing decorators
for all ATLAS cogs. Replaces 6+ different inline permission patterns.

Usage:
    from permissions import is_commissioner, commissioner_only, require_channel

    # As a decorator:
    @commissioner_only()
    async def my_admin_command(self, interaction): ...

    # As a function:
    if not await is_commissioner(interaction):
        return

    # Channel restriction (soft fallback — allows everywhere if unconfigured):
    @require_channel("sportsbook")
    async def sportsbook(self, interaction): ...
"""

from __future__ import annotations

import os
from typing import Optional

import discord
from discord import app_commands

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_USER_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
]

COMMISSIONER_ROLE_NAME = "Commissioner"
TSL_OWNER_ROLE_NAME = "TSL Owner"
GOD_ROLE_NAME = "GOD"


# ── Core Checks ──────────────────────────────────────────────────────────────

async def is_commissioner(interaction: discord.Interaction) -> bool:
    """
    Returns True if the user is a commissioner.

    Checks (any match = True):
      1. User ID is in ADMIN_USER_IDS env var
      2. User has the "Commissioner" role
      3. User has guild administrator permission
    """
    # Check env var admin list
    if interaction.user.id in ADMIN_USER_IDS:
        return True

    # Check guild permissions
    member = interaction.user
    if isinstance(member, discord.Member):
        # Guild administrator
        if member.guild_permissions.administrator:
            return True
        # Commissioner role
        if any(r.name == COMMISSIONER_ROLE_NAME for r in member.roles):
            return True

    return False


async def is_tsl_owner(interaction: discord.Interaction) -> bool:
    """
    Returns True if the user is a TSL Owner (franchise owner).

    Soft fallback: if the "TSL Owner" role doesn't exist on the server,
    returns True for everyone. This prevents lockout during migration.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        # DM context — only allow admins (prevents permission bypass via DMs)
        return interaction.user.id in ADMIN_USER_IDS

    guild = interaction.guild
    if guild is None:
        return True

    # Check if the role exists on this server
    role_exists = any(r.name == TSL_OWNER_ROLE_NAME for r in guild.roles)
    if not role_exists:
        return True  # Soft fallback — role not configured yet

    # Role exists — check if user has it
    if any(r.name == TSL_OWNER_ROLE_NAME for r in member.roles):
        return True

    # Commissioners always pass owner checks too
    return await is_commissioner(interaction)


async def is_god(interaction: discord.Interaction) -> bool:
    """
    Returns True if the user has the GOD role.

    GOD is above Commissioner — has all commissioner powers plus
    destructive operations (rebuilddb, affinity reset).

    DM context: only env ADMIN_USER_IDS pass.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        return interaction.user.id in ADMIN_USER_IDS

    if any(r.name == GOD_ROLE_NAME for r in member.roles):
        return True

    return False


# ── Decorators ────────────────────────────────────────────────────────────────

def commissioner_only():
    """
    app_commands.check decorator that restricts a command to commissioners.

    Usage:
        @app_commands.command(...)
        @commissioner_only()
        async def admin_cmd(self, interaction): ...
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if await is_commissioner(interaction):
            return True
        await interaction.response.send_message(
            "ATLAS: This command is restricted to commissioners.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)


def require_god():
    """
    app_commands.check decorator that restricts a command to GOD-role users.

    Usage:
        @app_commands.command(...)
        @require_god()
        async def god_cmd(self, interaction): ...
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if await is_god(interaction):
            return True
        await interaction.response.send_message(
            "ATLAS: This command requires the GOD role.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)


def require_channel(*config_keys: str):
    """
    app_commands.check decorator that restricts a command to specific channels.

    Uses get_channel_id() from setup_cog to resolve channel IDs by config_key.
    Soft fallback: if the channel isn't configured, allows the command everywhere.

    Usage:
        @app_commands.command(...)
        @require_channel("sportsbook")
        async def sportsbook(self, interaction): ...
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        # Lazy import to avoid circular dependency with setup_cog
        try:
            from setup_cog import get_channel_id
        except ImportError:
            return True  # setup_cog not loaded — allow everywhere

        guild_id = interaction.guild_id
        allowed_ids: list[int] = []

        for key in config_keys:
            ch_id = get_channel_id(key, guild_id)
            if ch_id:
                allowed_ids.append(ch_id)

        # Soft fallback: if no channels are configured for these keys, allow everywhere
        if not allowed_ids:
            return True

        # Check if the current channel matches
        if interaction.channel_id in allowed_ids:
            return True

        # Commissioners bypass channel restrictions
        if await is_commissioner(interaction):
            return True

        # Build a friendly channel mention list
        mentions = ", ".join(f"<#{cid}>" for cid in allowed_ids)
        await interaction.response.send_message(
            f"ATLAS: This command can only be used in {mentions}.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)
