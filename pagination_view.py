"""
ATLAS -- Pagination View
Reusable paginated embed navigator.

Generic pagination component for any list of embeds. Use this
everywhere instead of building custom prev/next logic per cog.

Usage (simple -- pass pre-built embeds):
    from pagination_view import PaginationView

    embeds = [build_page(i) for i in range(5)]
    view = PaginationView(embeds, author_id=interaction.user.id)
    await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

Usage (lazy -- build pages on demand for large datasets):
    from pagination_view import LazyPaginationView

    async def page_builder(page_num: int) -> discord.Embed:
        offset = page_num * 10
        rows = db.fetch_transactions(user_id, limit=10, offset=offset)
        return build_transaction_embed(rows, page_num)

    view = LazyPaginationView(
        page_builder=page_builder,
        total_pages=15,
        author_id=interaction.user.id,
    )
    first_page = await page_builder(0)
    await interaction.followup.send(embed=first_page, view=view, ephemeral=True)
"""

import logging
from typing import Optional, Callable, Awaitable

import discord
from discord import ui

from atlas_colors import AtlasColors

log = logging.getLogger("atlas.pagination")


class PaginationView(ui.View):
    """Paginated embed viewer for pre-built embed lists.

    Best for small-to-medium datasets where all pages can be
    built upfront (leaderboards, bet history, trade history).

    Features:
    - First / Prev / Page Counter / Next / Last buttons
    - Author-locked -- only the requesting user can navigate
    - Auto-disables at boundaries (first/last page)
    - 3-minute timeout for ephemeral responses

    Note: No static custom_id on buttons -- each instance gets
    unique auto-generated IDs from discord.py. This allows
    multiple paginated views to coexist without collisions.
    """

    def __init__(
        self,
        embeds: list[discord.Embed],
        author_id: int,
        timeout: float = 180.0,
    ):
        """
        Args:
            embeds: List of pre-built discord.Embed pages.
            author_id: Discord user ID who owns this pagination.
            timeout: View timeout in seconds (default 3 min).
        """
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.author_id = author_id
        self.current_page = 0
        self.total_pages = len(embeds)
        self._update_buttons()

    def _update_buttons(self) -> None:
        """Enable/disable buttons based on current position."""
        self.first_page.disabled = self.current_page == 0
        self.prev_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page >= self.total_pages - 1
        self.last_page.disabled = self.current_page >= self.total_pages - 1
        self.page_counter.label = f"{self.current_page + 1}/{self.total_pages}"

    async def _author_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the original user can navigate."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This isn't your view -- use the command yourself.",
                ephemeral=True,
            )
            return False
        return True

    @ui.button(label="\u23ee", style=discord.ButtonStyle.secondary, row=0)
    async def first_page(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._author_check(interaction):
            return
        self.current_page = 0
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[0], view=self)

    @ui.button(label="\u25c0", style=discord.ButtonStyle.primary, row=0)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._author_check(interaction):
            return
        self.current_page = max(0, self.current_page - 1)
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_counter(self, interaction: discord.Interaction, button: ui.Button):
        # Non-interactive display button
        await interaction.response.defer()

    @ui.button(label="\u25b6", style=discord.ButtonStyle.primary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._author_check(interaction):
            return
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @ui.button(label="\u23ed", style=discord.ButtonStyle.secondary, row=0)
    async def last_page(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._author_check(interaction):
            return
        self.current_page = self.total_pages - 1
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    async def on_timeout(self) -> None:
        """Disable all buttons when the view expires."""
        for item in self.children:
            item.disabled = True


class LazyPaginationView(ui.View):
    """Paginated viewer that builds pages on demand.

    Best for large datasets where building all pages upfront
    would be wasteful (full transaction history, all-time stats).

    The page_builder callback is called each time the user
    navigates, so only the visible page is ever constructed.
    """

    def __init__(
        self,
        page_builder: Callable[[int], Awaitable[discord.Embed]],
        total_pages: int,
        author_id: int,
        timeout: float = 180.0,
    ):
        """
        Args:
            page_builder: Async callable that takes a page number
                          (0-indexed) and returns a discord.Embed.
            total_pages: Total number of pages available.
            author_id: Discord user ID who owns this pagination.
            timeout: View timeout in seconds (default 3 min).
        """
        super().__init__(timeout=timeout)
        self.page_builder = page_builder
        self.total_pages = total_pages
        self.author_id = author_id
        self.current_page = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.first_page.disabled = self.current_page == 0
        self.prev_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page >= self.total_pages - 1
        self.last_page.disabled = self.current_page >= self.total_pages - 1
        self.page_counter.label = f"{self.current_page + 1}/{self.total_pages}"

    async def _author_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This isn't your view -- use the command yourself.",
                ephemeral=True,
            )
            return False
        return True

    async def _navigate(self, interaction: discord.Interaction, page: int) -> None:
        """Navigate to a specific page."""
        if not await self._author_check(interaction):
            return
        self.current_page = page
        self._update_buttons()
        await interaction.response.defer()
        embed = await self.page_builder(self.current_page)
        await interaction.edit_original_response(embed=embed, view=self)

    @ui.button(label="\u23ee", style=discord.ButtonStyle.secondary, row=0)
    async def first_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._navigate(interaction, 0)

    @ui.button(label="\u25c0", style=discord.ButtonStyle.primary, row=0)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._navigate(interaction, max(0, self.current_page - 1))

    @ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_counter(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()

    @ui.button(label="\u25b6", style=discord.ButtonStyle.primary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._navigate(interaction, min(self.total_pages - 1, self.current_page + 1))

    @ui.button(label="\u23ed", style=discord.ButtonStyle.secondary, row=0)
    async def last_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._navigate(interaction, self.total_pages - 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
