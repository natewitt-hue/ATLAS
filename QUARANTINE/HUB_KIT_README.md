"""
ATLAS -- Hub Infrastructure Kit
Integration Guide
Version: 1.0.0 (patched)

This kit provides the foundation for all persistent hub modules.
These files live in the discord_bot/ root directory alongside bot.py.

FILES IN THIS KIT

atlas_colors.py          Brand color constants for all embeds
ui_state.py              Persistent message ID tracking + restart recovery
hub_view.py              Base hub view class + auto-defer decorator
pagination_view.py       Reusable paginated embed navigator
CUSTOM_ID_CONVENTION.py  Naming standard reference (importable docstring)

WIRING (already done in bot.py)

    from ui_state import UIStateManager

    bot.ui_state = UIStateManager(bot)
    # Uses flow_wallet.DB_PATH (flow_economy.db) by default

    async def setup_hook():
        await bot.ui_state.init_table()

    async def on_ready():
        await bot.ui_state.restore_all_views()

BUILDING A HUB (Example -- Sportsbook)

    import discord
    from discord import app_commands
    from discord.ext import commands

    from atlas_colors import AtlasColors
    from hub_view import AtlasHubView, atlas_button
    from ui_state import UIStateManager


    class SportsbookHubView(AtlasHubView):
        MODULE = "sportsbook"

        @atlas_button(
            label="TSL Games", emoji="football",
            custom_id="atlas:sportsbook:tsl_games",
            style=discord.ButtonStyle.primary
        )
        async def tsl_games(self, interaction, button):
            embed = discord.Embed(
                title="This Week's TSL Lines",
                description="Lines load here...",
                color=AtlasColors.SPORTSBOOK,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


    class SportsbookCog(commands.Cog):
        def __init__(self, bot):
            self.bot = bot
            bot.ui_state.register_view_factory(
                "sportsbook",
                lambda b: SportsbookHubView(b)
            )

        @app_commands.command(
            name="deploy_sportsbook",
            description="[Commish] Deploy persistent Sportsbook hub"
        )
        @app_commands.checks.has_permissions(administrator=True)
        async def deploy_sportsbook(self, interaction: discord.Interaction):
            view = SportsbookHubView(self.bot)
            embed = discord.Embed(
                title="TSL SPORTSBOOK",
                description=(
                    "Place bets on TSL simulation games, real sports, "
                    "and prediction markets -- all from one hub.\\n\\n"
                    "Click a button below to get started."
                ),
                color=AtlasColors.SPORTSBOOK,
            )
            message = await interaction.channel.send(embed=embed, view=view)
            await self.bot.ui_state.register(
                "sportsbook",
                interaction.channel.id,
                message.id,
                interaction.guild.id if interaction.guild else None,
            )
            await interaction.response.send_message(
                "Sportsbook hub deployed.", ephemeral=True,
            )

USING PAGINATION

    from pagination_view import PaginationView

    async def show_transactions(self, interaction, button):
        pages = [build_page(i) for i in range(total_pages)]
        view = PaginationView(pages, author_id=interaction.user.id)
        await interaction.followup.send(
            embed=pages[0], view=view, ephemeral=True
        )

ECONOMY OPERATIONS

    Economy operations use flow_wallet.py directly. Do NOT use atomic_deduct
    or atomic_credit from hub_view (they have been removed).

    from flow_wallet import debit, credit, InsufficientFundsError

    try:
        await debit(user_id, amount, "Sportsbook: CHI -3.5", reference_key="bet_123")
    except InsufficientFundsError:
        await interaction.followup.send("Insufficient TSL Bucks.", ephemeral=True)
        return

    await credit(user_id, payout, "Sportsbook WIN: CHI -3.5", reference_key="win_123")

DEPLOYMENT PATTERN: /deploy_[module]

Each hub cog should include a commish-only /deploy_[module] command
that posts the persistent card and registers it with UIState.

    /deploy_sportsbook   ->  #sportsbook channel
    /deploy_casino       ->  #casino channel
    /deploy_stats        ->  #stats channel
    /deploy_economy      ->  #economy channel
    /deploy_trades       ->  #trades channel
    /deploy_commish      ->  #commish-console (admin-only channel)

DEPENDENCY TREE

    bot.py
     +-- ui_state.py          (instantiated as bot.ui_state)
     |
     +-- sportsbook_cog.py    (imports hub_view, atlas_colors, ui_state)
     +-- casino_cog.py        (imports hub_view, atlas_colors, ui_state)
     +-- stats_cog.py         (imports hub_view, atlas_colors, pagination_view)
     +-- economy_cog.py       (imports hub_view, atlas_colors, pagination_view)
     +-- trade_cog.py         (imports hub_view, atlas_colors, pagination_view)
     +-- commish_console.py   (imports hub_view, atlas_colors)

    Shared utilities (no Discord dependency):
     +-- atlas_colors.py      (standalone -- only needs discord.Color)
     +-- hub_view.py          (needs atlas_colors)
     +-- pagination_view.py   (needs atlas_colors)
     +-- CUSTOM_ID_CONVENTION.py  (reference doc, no runtime use)
"""
