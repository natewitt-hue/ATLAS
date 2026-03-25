"""
atlas_home_cog.py — ATLAS User Home (/atlas)
─────────────────────────────────────────────────────────────────────────────
Personal "baseball card" command for every user.

    /atlas  →  ephemeral PNG card + module navigation buttons + theme cycling

No admin access required. Card is always ephemeral.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from atlas_home_renderer import gather_home_data, render_home_card_to_file
from atlas_themes import THEMES

log = logging.getLogger("atlas.home")

# ── Module info embeds ────────────────────────────────────────────────────────

_MODULE_INFO = {
    "oracle": (
        "🔮 Oracle Intelligence",
        "AI-powered stats analysis, power rankings, matchup breakdowns, and scouting.\n"
        "Use `/oracle` to open the hub.",
    ),
    "genesis": (
        "⚔️ Genesis Trade Center",
        "Propose and approve trades, check parity ratings, and manage dev traits.\n"
        "Use `/trade` to open the hub.",
    ),
    "flow": (
        "💰 Flow Economy",
        "View your wallet, bet history, and league economy stats.\n"
        "Use `/flow` to open the hub.",
    ),
    "sportsbook": (
        "🏈 TSL Sportsbook",
        "Bet on TSL game outcomes, build parlays, and track your record.\n"
        "Use `/sportsbook` to open the hub.",
    ),
    "casino": (
        "🎰 ATLAS Casino",
        "Blackjack, Slots, Crash, and Coinflip — all under `/casino`.",
    ),
    "predictions": (
        "📈 Prediction Markets",
        "Trade YES/NO on custom markets for TSL outcomes.\n"
        "Use `/predictions` to open the hub.",
    ),
}


# ── Views ─────────────────────────────────────────────────────────────────────

class HomeView(discord.ui.View):
    """Main navigation view attached to the home card."""

    def __init__(self, user_id: int, data: dict, theme_id: Optional[str]):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.data = data
        self.theme_id = theme_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This card belongs to someone else.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔮 Oracle", style=discord.ButtonStyle.secondary)
    async def oracle_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "oracle")

    @discord.ui.button(label="⚔️ Genesis", style=discord.ButtonStyle.secondary)
    async def genesis_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "genesis")

    @discord.ui.button(label="💰 Flow", style=discord.ButtonStyle.secondary)
    async def flow_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "flow")

    @discord.ui.button(label="🏈 Sportsbook", style=discord.ButtonStyle.secondary)
    async def sportsbook_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "sportsbook")

    @discord.ui.button(label="🎰 Casino", style=discord.ButtonStyle.secondary)
    async def casino_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "casino")

    @discord.ui.button(label="📈 Predictions", style=discord.ButtonStyle.secondary)
    async def predictions_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "predictions")

    @discord.ui.button(label="🎨 Theme", style=discord.ButtonStyle.primary)
    async def theme_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        theme_keys = list(THEMES.keys())
        current_idx = theme_keys.index(self.theme_id) if self.theme_id in theme_keys else 0
        theme_view = ThemeCycleView(
            user_id=self.user_id,
            data=self.data,
            theme_keys=theme_keys,
            current_idx=current_idx,
            original_theme=self.theme_id,
        )
        await interaction.response.defer()
        disc_file = await render_home_card_to_file(
            self.data, theme_id=self.theme_id, filename="atlas_home.png"
        )
        await interaction.edit_original_response(attachments=[disc_file], view=theme_view)


class ThemeCycleView(discord.ui.View):
    """Theme preview cycling view."""

    def __init__(
        self,
        user_id: int,
        data: dict,
        theme_keys: list[str],
        current_idx: int,
        original_theme: Optional[str],
    ):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.data = data
        self.theme_keys = theme_keys
        self.current_idx = current_idx
        self.original_theme = original_theme

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This card belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def _current_theme_id(self) -> str:
        return self.theme_keys[self.current_idx]

    async def _re_render(self, interaction: discord.Interaction):
        await interaction.response.defer()
        theme_id = self._current_theme_id()
        theme_label = THEMES[theme_id].get("label", theme_id) if theme_id in THEMES else theme_id
        disc_file = await render_home_card_to_file(
            self.data, theme_id=theme_id, filename="atlas_home.png"
        )
        await interaction.edit_original_response(
            content=f"Preview: **{theme_label}**",
            attachments=[disc_file],
            view=self,
        )

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.current_idx = (self.current_idx - 1) % len(self.theme_keys)
        await self._re_render(interaction)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.current_idx = (self.current_idx + 1) % len(self.theme_keys)
        await self._re_render(interaction)

    @discord.ui.button(label="✅ Apply", style=discord.ButtonStyle.success)
    async def apply_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        new_theme = self._current_theme_id()
        from flow_wallet import set_theme
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, set_theme, self.user_id, new_theme)
        theme_label = THEMES[new_theme].get("label", new_theme) if new_theme in THEMES else new_theme
        home_view = HomeView(self.user_id, self.data, new_theme)
        disc_file = await render_home_card_to_file(
            self.data, theme_id=new_theme, filename="atlas_home.png"
        )
        await interaction.response.defer()
        await interaction.edit_original_response(
            content=f"Theme set to **{theme_label}**.",
            attachments=[disc_file],
            view=home_view,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        home_view = HomeView(self.user_id, self.data, self.original_theme)
        disc_file = await render_home_card_to_file(
            self.data, theme_id=self.original_theme, filename="atlas_home.png"
        )
        await interaction.response.defer()
        await interaction.edit_original_response(
            content=None,
            attachments=[disc_file],
            view=home_view,
        )


async def _send_module_info(interaction: discord.Interaction, key: str):
    title, desc = _MODULE_INFO[key]
    embed = discord.Embed(title=title, description=desc, color=0xd4a843)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class AtlasHomeCog(commands.Cog):
    """ATLAS User Home — /atlas baseball card."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="atlas", description="View your ATLAS stats card and navigate to any module.")
    async def atlas_home(self, interaction: discord.Interaction):
        """Render the user's personal ATLAS baseball card."""
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        # Resolve role badge (highest tier first)
        role_badge = ""
        if isinstance(user, discord.Member):
            for role_name in ("GOD", "Commissioner", "TSL Owner"):
                if any(r.name == role_name for r in user.roles):
                    role_badge = role_name
                    break

        # Resolve theme
        try:
            from flow_wallet import get_theme_for_render
            theme_id = get_theme_for_render(user.id)
        except Exception:
            theme_id = None

        # Gather data in executor (sync DB reads)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, gather_home_data, user.id)

        # Overlay display info
        data["display_name"] = user.display_name
        data["role_badge"] = role_badge
        if theme_id and theme_id in THEMES:
            data["theme_name"] = THEMES[theme_id].get("label", theme_id)
        try:
            import data_manager as dm
            data["season"] = dm.CURRENT_SEASON
        except Exception:
            pass

        disc_file = await render_home_card_to_file(data, theme_id=theme_id, filename="atlas_home.png")
        view = HomeView(user_id=user.id, data=data, theme_id=theme_id)
        await interaction.followup.send(file=disc_file, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(AtlasHomeCog(bot))
        print("ATLAS: Home · User baseball card loaded. 🏠")
    except Exception as e:
        print(f"ATLAS: Home · FAILED to load ({e})")
