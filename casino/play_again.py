"""
play_again.py — Shared "Play Again" view for casino games
─────────────────────────────────────────────────────────────────────────────
Attaches a single green button to the result embed after any solo casino
game resolves. Clicking it starts a fresh game with the same wager.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Callable, Awaitable

import discord

from casino.casino_db import get_balance

TIMEOUT_SECS = 300  # 5 minutes — matches existing game timeouts


class PlayAgainView(discord.ui.View):
    """One-button view that replays a casino game with the same wager."""

    def __init__(
        self,
        user_id: int,
        wager: int,
        replay_callback: Callable[[discord.Interaction], Awaitable[None]],
    ):
        super().__init__(timeout=TIMEOUT_SECS)
        self.user_id = user_id
        self.wager = wager
        self.replay_callback = replay_callback

        self.btn = discord.ui.Button(
            label=f"Play Again ({wager:,} Bucks)",
            style=discord.ButtonStyle.success,
            emoji="\U0001f501",  # 🔁
        )
        self.btn.callback = self._on_click
        self.add_item(self.btn)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your game!", ephemeral=True
            )

        bal = await get_balance(self.user_id)
        if bal < self.wager:
            return await interaction.response.send_message(
                f"❌ Not enough Bucks — need **{self.wager:,}**, have **{bal:,}**.",
                ephemeral=True,
            )

        # Disable button immediately to prevent double-click
        self.btn.disabled = True
        self.stop()
        await interaction.message.edit(view=self)

        # Start a fresh game — callback already has wager (and pick) bound
        await self.replay_callback(interaction)

    async def on_timeout(self) -> None:
        self.btn.disabled = True
        self.stop()
