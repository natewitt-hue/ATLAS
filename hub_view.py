"""
ATLAS -- Base Hub View
Foundation class for all persistent hub UIs.

Every persistent hub (Sportsbook, Casino, Stats, Economy, Trade,
Commissioner Console) should subclass AtlasHubView instead of
raw discord.ui.View. This gives you:

  1. timeout=None by default (persistent)
  2. Auto-defer on all button interactions
  3. Standardized error handling
  4. Custom ID prefix enforcement

Economy operations: Use flow_wallet.debit() and flow_wallet.credit()
directly. They are already atomic (BEGIN IMMEDIATE), idempotent (via
reference_key), and raise InsufficientFundsError on insufficient funds.

Usage:
    from hub_view import AtlasHubView, atlas_button

    class SportsbookHubView(AtlasHubView):
        MODULE = "sportsbook"

        @atlas_button(
            label="TSL Games", emoji="football",
            custom_id="atlas:sportsbook:tsl_games",
            style=discord.ButtonStyle.primary
        )
        async def tsl_games(self, interaction, button):
            # interaction is already deferred -- use followup
            embed = build_tsl_lines_embed()
            await interaction.followup.send(embed=embed, ephemeral=True)
"""

import logging
import functools
from typing import Optional, Any

import discord
from discord import ui

from atlas_colors import AtlasColors

log = logging.getLogger("atlas.hub_view")


# -- Custom ID Helpers --------------------------------------------------

def make_custom_id(module: str, action: str) -> str:
    """Build a standardized custom ID.

    Format: atlas:[module]:[action]
    Example: atlas:sportsbook:tsl_games

    Args:
        module: Module name (e.g., "sportsbook").
        action: Action name (e.g., "tsl_games").

    Returns:
        Formatted custom ID string.
    """
    return f"atlas:{module}:{action}"


def parse_custom_id(custom_id: str) -> tuple[str, str, str]:
    """Parse a standardized custom ID into its parts.

    Args:
        custom_id: Full custom ID (e.g., "atlas:sportsbook:tsl_games").

    Returns:
        Tuple of (prefix, module, action). Returns ("", "", "")
        if the format doesn't match.
    """
    parts = custom_id.split(":")
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2])
    return ("", "", "")


# -- Auto-Defer Button Decorator ---------------------------------------

def atlas_button(
    *,
    label: str,
    custom_id: str,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: Optional[str] = None,
    row: Optional[int] = None,
    disabled: bool = False,
    defer: bool = True,
    ephemeral: bool = True,
):
    """Decorator for hub buttons with automatic defer.

    Wraps discord.ui.button with an auto-defer layer. By default,
    every button press is deferred ephemerally before your callback
    runs, giving you 15 minutes to respond via followup.send().

    Set defer=False for buttons that need to respond with a modal
    (modals require interaction.response.send_modal, not a defer).

    Args:
        label: Button label text.
        custom_id: Must follow atlas:[module]:[action] convention.
        style: Discord button style.
        emoji: Optional emoji for the button.
        row: Button row (0-4).
        disabled: Whether button starts disabled.
        defer: If True, auto-defers the interaction before callback.
        ephemeral: If True (and defer=True), defer is ephemeral.

    Usage:
        @atlas_button(
            label="TSL Games", emoji="football",
            custom_id="atlas:sportsbook:tsl_games",
            style=discord.ButtonStyle.primary
        )
        async def tsl_games(self, interaction, button):
            # Already deferred -- use followup.send()
            await interaction.followup.send("Lines loaded!", ephemeral=True)

        @atlas_button(
            label="New Proposal", emoji="memo",
            custom_id="atlas:trade:new_proposal",
            defer=False  # Need to send a modal instead
        )
        async def new_proposal(self, interaction, button):
            await interaction.response.send_modal(TradeModal())
    """
    # Validate custom ID format
    if not custom_id.startswith("atlas:"):
        raise ValueError(
            f"Custom ID must start with 'atlas:' -- got '{custom_id}'. "
            f"Use format: atlas:[module]:[action]"
        )

    def decorator(func):
        @ui.button(
            label=label,
            custom_id=custom_id,
            style=style,
            emoji=emoji,
            row=row,
            disabled=disabled,
        )
        @functools.wraps(func)
        async def wrapper(self, interaction: discord.Interaction, button_ref: ui.Button):
            try:
                if defer and not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=ephemeral)
                await func(self, interaction, button_ref)
            except Exception as e:
                log.error(
                    f"[HubView] Error in {custom_id}: {e}",
                    exc_info=True,
                )
                await _send_error(interaction, deferred=defer)

        return wrapper
    return decorator


async def _send_error(interaction: discord.Interaction, deferred: bool = True) -> None:
    """Send a standardized error message to the user."""
    error_embed = discord.Embed(
        title="Something went wrong",
        description="ATLAS encountered an error processing your request. Try again in a moment.",
        color=AtlasColors.ERROR,
    )
    try:
        if deferred:
            await interaction.followup.send(embed=error_embed, ephemeral=True)
        elif not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed, ephemeral=True)
    except discord.HTTPException:
        pass  # Nothing we can do -- interaction expired


# -- Base Hub View ------------------------------------------------------

class AtlasHubView(ui.View):
    """Base class for all ATLAS persistent hub views.

    Subclass this for every hub module. Provides:
    - Persistent timeout (None) by default
    - Module name enforcement
    - Standard embed builder

    Subclasses MUST define:
        MODULE: str -- the module name (e.g., "sportsbook")

    Example:
        class SportsbookHubView(AtlasHubView):
            MODULE = "sportsbook"

            @atlas_button(
                label="TSL Games", emoji="football",
                custom_id="atlas:sportsbook:tsl_games",
                style=discord.ButtonStyle.primary
            )
            async def tsl_games(self, interaction, button):
                await interaction.followup.send("Lines!", ephemeral=True)
    """

    MODULE: str = ""  # Override in subclass

    def __init__(self, bot: Optional[Any] = None, **kwargs):
        super().__init__(timeout=None, **kwargs)
        self.bot = bot

    @property
    def module_color(self) -> discord.Color:
        """Return this module's brand color."""
        return AtlasColors.by_module(self.MODULE)

    def hub_embed(
        self,
        title: str,
        description: str = "",
        fields: Optional[list[tuple[str, str, bool]]] = None,
    ) -> discord.Embed:
        """Build a standard hub embed with module branding.

        Args:
            title: Embed title.
            description: Embed description.
            fields: Optional list of (name, value, inline) tuples.

        Returns:
            discord.Embed with module color applied.
        """
        embed = discord.Embed(
            title=title,
            description=description,
            color=self.module_color,
        )
        if fields:
            for name, value, inline in fields:
                embed.add_field(name=name, value=value, inline=inline)
        embed.set_footer(text=f"ATLAS -- {self.MODULE.title()}")
        return embed
