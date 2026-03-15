"""
ATLAS -- UI State Manager
Persistent hub message tracking + restart recovery.

Solves the "gray brick" problem: when the bot restarts, Discord
buttons go dead unless the bot re-binds their View objects to
the original message IDs. This module stores those IDs in SQLite
and restores them on startup.

Usage in bot.py:
    from ui_state import UIStateManager

    bot.ui_state = UIStateManager(bot)

    async def setup_hook():
        await bot.ui_state.init_table()

    @bot.event
    async def on_ready():
        await bot.ui_state.restore_all_views()

Usage when posting a new hub:
    message = await channel.send(embed=embed, view=my_view)
    await bot.ui_state.register("sportsbook", channel.id, message.id)
"""

import logging
from typing import Optional, Callable

import aiosqlite
import discord
from discord.ext import commands

# UI state shares the flow_economy.db database with the wallet/economy system.
# This avoids creating yet another .db file; ui_state only adds its own table.
from flow_wallet import DB_PATH

log = logging.getLogger("atlas.ui_state")

_DB_TIMEOUT = 10


class UIStateManager:
    """Manages persistent Discord UI views across bot restarts.

    Stores module_name -> (channel_id, message_id) mappings in SQLite.
    On startup, restores View objects to their original messages so
    buttons remain interactive.
    """

    def __init__(self, bot: commands.Bot, db_path: Optional[str] = None):
        self.bot = bot
        self.db_path = db_path or DB_PATH
        # Registry: maps module_name to a callable that returns
        # the View class to bind. Cogs register themselves here.
        self._view_registry: dict[str, Callable] = {}

    # -- Database Setup -------------------------------------------------

    async def init_table(self) -> None:
        """Create the ui_state table if it doesn't exist.

        Call this in setup_hook() or before the bot starts.
        """
        async with aiosqlite.connect(self.db_path, timeout=_DB_TIMEOUT) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ui_state (
                    module_name  TEXT PRIMARY KEY,
                    channel_id   INTEGER NOT NULL,
                    message_id   INTEGER NOT NULL,
                    guild_id     INTEGER,
                    created_at   TEXT DEFAULT (datetime('now')),
                    updated_at   TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.commit()
        log.info("[UIState] ui_state table ready.")

    # -- Registration ---------------------------------------------------

    def register_view_factory(self, module_name: str, factory: Callable) -> None:
        """Register a View factory for a module.

        Each hub cog should call this during __init__ so the
        UIStateManager knows how to reconstruct the View on restart.

        Args:
            module_name: Unique key (e.g., "sportsbook", "casino").
            factory: A callable that returns a discord.ui.View instance.
                     Signature: factory(bot) -> discord.ui.View

        Example in a cog's __init__:
            bot.ui_state.register_view_factory(
                "sportsbook",
                lambda b: SportsbookHubView(b)
            )
        """
        self._view_registry[module_name] = factory
        log.info(f"[UIState] Registered view factory: {module_name}")

    async def register(
        self,
        module_name: str,
        channel_id: int,
        message_id: int,
        guild_id: Optional[int] = None,
    ) -> None:
        """Store a persistent hub message in the database.

        Call this after sending a new hub embed to a channel.
        Uses INSERT OR REPLACE so each module has exactly one
        active hub message at a time.

        Args:
            module_name: Unique key (e.g., "sportsbook").
            channel_id: The Discord channel containing the hub.
            message_id: The Discord message ID of the hub embed.
            guild_id: Optional guild ID for multi-server support.
        """
        async with aiosqlite.connect(self.db_path, timeout=_DB_TIMEOUT) as db:
            await db.execute("""
                INSERT OR REPLACE INTO ui_state
                    (module_name, channel_id, message_id, guild_id, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (module_name, channel_id, message_id, guild_id))
            await db.commit()
        log.info(
            f"[UIState] Registered: {module_name} -> "
            f"channel={channel_id}, message={message_id}"
        )

    async def unregister(self, module_name: str) -> None:
        """Remove a hub's message tracking.

        Call when a hub is intentionally destroyed or moved.
        """
        async with aiosqlite.connect(self.db_path, timeout=_DB_TIMEOUT) as db:
            await db.execute(
                "DELETE FROM ui_state WHERE module_name = ?",
                (module_name,)
            )
            await db.commit()
        log.info(f"[UIState] Unregistered: {module_name}")

    # -- Restore on Startup ---------------------------------------------

    async def restore_all_views(self) -> None:
        """Restore all persistent views on bot startup.

        Iterates through the ui_state table, reconstructs each
        View from the registered factory, and binds it to the
        original message. If a message no longer exists (deleted
        channel, cleared messages), it cleans up the stale record.

        Call this in on_ready().
        """
        async with aiosqlite.connect(self.db_path, timeout=_DB_TIMEOUT) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM ui_state") as cursor:
                rows = await cursor.fetchall()

        if not rows:
            log.info("[UIState] No persistent views to restore.")
            return

        restored = 0
        stale_modules: list[str] = []

        for row in rows:
            module_name = row["module_name"]
            channel_id = row["channel_id"]
            message_id = row["message_id"]

            # Check if we have a view factory for this module
            factory = self._view_registry.get(module_name)
            if not factory:
                log.warning(
                    f"[UIState] No view factory for '{module_name}' -- "
                    f"skipping. (Cog may not be loaded yet.)"
                )
                continue

            # Try to fetch the channel and verify the message exists
            channel = self.bot.get_channel(channel_id)
            if not channel:
                log.warning(
                    f"[UIState] Channel {channel_id} not found for "
                    f"'{module_name}' -- cleaning up stale record."
                )
                stale_modules.append(module_name)
                continue

            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning(
                    f"[UIState] Message {message_id} not found in "
                    f"#{channel.name} for '{module_name}' -- cleaning up."
                )
                stale_modules.append(module_name)
                continue

            # Reconstruct the View and bind it to the message
            view = factory(self.bot)
            self.bot.add_view(view, message_id=message.id)
            restored += 1
            log.info(
                f"[UIState] Restored '{module_name}' in "
                f"#{channel.name} (msg={message_id})"
            )

        # Batch-delete all stale records in a single DB connection
        if stale_modules:
            async with aiosqlite.connect(self.db_path, timeout=_DB_TIMEOUT) as db:
                await db.executemany(
                    "DELETE FROM ui_state WHERE module_name = ?",
                    [(m,) for m in stale_modules],
                )
                await db.commit()

        cleaned = len(stale_modules)
        log.info(
            f"[UIState] Restore complete: {restored} restored, "
            f"{cleaned} cleaned up, {len(rows) - restored - cleaned} skipped."
        )

    # -- Utility --------------------------------------------------------

    async def get_hub_message(
        self, module_name: str
    ) -> Optional[tuple[int, int]]:
        """Look up the stored channel_id and message_id for a module.

        Returns:
            (channel_id, message_id) tuple, or None if not found.
        """
        async with aiosqlite.connect(self.db_path, timeout=_DB_TIMEOUT) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT channel_id, message_id FROM ui_state "
                "WHERE module_name = ?",
                (module_name,)
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                return (row["channel_id"], row["message_id"])
            return None

    async def refresh_hub(
        self, module_name: str, embed: discord.Embed
    ) -> bool:
        """Update the embed on an existing persistent hub message.

        Useful for background tasks that refresh hub content
        (e.g., updating the "Game of the Week" on the sportsbook).

        Returns:
            True if the message was updated, False if not found.
        """
        result = await self.get_hub_message(module_name)
        if not result:
            return False

        channel_id, message_id = result
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return False

        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            log.info(f"[UIState] Refreshed embed for '{module_name}'.")
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning(
                f"[UIState] Failed to refresh '{module_name}' -- "
                f"message may have been deleted."
            )
            return False
