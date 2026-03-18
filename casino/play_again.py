"""
play_again.py — Shared "Play Again" / "Let It Ride" view for casino games
─────────────────────────────────────────────────────────────────────────────
Attaches two buttons to the result embed after any solo casino game resolves:
  • Play Again — replay with the same wager
  • Let It Ride — replay with double the wager (capped at player's max bet tier)

Button labels include streak context for engagement.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Callable, Awaitable

import discord

from casino.casino_db import get_balance, get_max_bet

TIMEOUT_SECS = 300  # 5 minutes — matches existing game timeouts


class PlayAgainView(discord.ui.View):
    """Two-button view: Play Again + Let It Ride (double wager)."""

    def __init__(
        self,
        user_id: int,
        wager: int,
        replay_callback: Callable[[discord.Interaction], Awaitable[None]],
        double_callback: Callable[[discord.Interaction], Awaitable[None]] | None = None,
        streak_info: dict | None = None,
        near_miss_msg: str | None = None,
    ):
        super().__init__(timeout=TIMEOUT_SECS)
        self.user_id = user_id
        self.wager = wager
        self._used = False   # Prevent double-click race condition
        self.replay_callback = replay_callback
        self.double_callback = double_callback
        self.streak_info = streak_info or {}

        # Build Play Again label with streak context
        label = f"Play Again (${wager:,})"
        if near_miss_msg:
            label = f"SO CLOSE! Again (${wager:,})"
        elif self.streak_info.get("type") == "win" and self.streak_info.get("len", 0) >= 3:
            from casino.casino_db import get_streak_bonus
            bonus = get_streak_bonus(self.streak_info)
            if bonus:
                label = f"Play Again (${wager:,}) — {bonus['label']} W{self.streak_info['len']}"
        elif self.streak_info.get("type") == "loss":
            label = f"Run It Back (${wager:,})"

        self.btn_play = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.success,
            emoji="\U0001f501",  # 🔁
        )
        self.btn_play.callback = self._on_play
        self.add_item(self.btn_play)

        # Double wager button
        double_wager = wager * 2
        self.double_wager = double_wager

        self.btn_double = discord.ui.Button(
            label=f"Let It Ride (${double_wager:,})",
            style=discord.ButtonStyle.primary,
            emoji="🔥",
        )
        self.btn_double.callback = self._on_double
        self.add_item(self.btn_double)

    async def _on_play(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your game!", ephemeral=True
            )

        if self._used:
            return await interaction.response.send_message(
                "Already processing...", ephemeral=True
            )
        self._used = True

        # Check balance before passing to game callback (which handles its own defer)
        bal = await get_balance(self.user_id)
        if bal < self.wager:
            self._used = False
            return await interaction.response.send_message(
                f"❌ Not enough Bucks — need **${self.wager:,}**, have **${bal:,}**.",
                ephemeral=True,
            )

        # Disable buttons for visual feedback
        self._disable_all()
        await interaction.message.edit(view=None)
        await self.replay_callback(interaction)

    async def _on_double(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your game!", ephemeral=True
            )

        if self._used:
            return await interaction.response.send_message(
                "Already processing...", ephemeral=True
            )
        self._used = True

        # Cap at player's max bet tier
        max_bet = await get_max_bet(self.user_id)
        actual_wager = min(self.double_wager, max_bet)

        # Check balance before passing to game callback (which handles its own defer)
        bal = await get_balance(self.user_id)
        if bal < actual_wager:
            self._used = False
            return await interaction.response.send_message(
                f"❌ Not enough Bucks — need **${actual_wager:,}**, have **${bal:,}**.",
                ephemeral=True,
            )

        self._disable_all()

        if self.double_callback:
            await self.double_callback(interaction)
        else:
            # Fallback: use replay callback (wager is already bound in the partial)
            await self.replay_callback(interaction)

    def _disable_all(self) -> None:
        self.btn_play.disabled = True
        self.btn_double.disabled = True
        self.stop()

    async def on_timeout(self) -> None:
        self._disable_all()
        try:
            if hasattr(self, "message") and self.message:
                await self.message.edit(view=self)
        except Exception:
            pass
