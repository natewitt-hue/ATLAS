"""
atlas_send.py — Universal Card Delivery
────────────────────────────────────────
Sends rendered card PNGs as standalone attachments (no embed wrapper)
so Discord displays them at full width on PC.

Usage:
    from atlas_send import send_card, send_card_to_channel

    # Interaction response (first reply)
    await send_card(interaction, png, filename="flow.png", view=my_view)

    # Followup after defer
    msg = await send_card(interaction, png, filename="flow.png",
                          followup=True, ephemeral=True)

    # Auto-post to a channel
    await send_card_to_channel(channel, png, filename="highlight.png")
"""

from __future__ import annotations

import io

import discord


async def send_card(
    interaction: discord.Interaction,
    png_bytes: bytes,
    *,
    filename: str = "card.png",
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
    followup: bool = False,
    content: str | None = None,
) -> discord.Message | None:
    """Send a rendered card PNG as a standalone Discord attachment.

    Parameters
    ----------
    interaction : discord.Interaction
        The interaction to respond to.
    png_bytes : bytes
        Raw PNG image bytes from render_card().
    filename : str
        Attachment filename (e.g. "flow_hub.png", "blackjack.png").
    view : discord.ui.View | None
        Button/select view to attach below the image.
    ephemeral : bool
        If True, only the invoking user sees the message.
    followup : bool
        If True, uses interaction.followup.send() instead of
        interaction.response.send_message().  Use after defer.
    content : str | None
        Optional text content above the image.

    Returns
    -------
    discord.Message | None
        The sent message (followup returns Message, response returns None).
    """
    file = discord.File(io.BytesIO(png_bytes), filename=filename)

    kwargs: dict = {"file": file}
    if content is not None:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    if ephemeral:
        kwargs["ephemeral"] = True

    if followup:
        return await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)
        return None


async def send_card_to_channel(
    channel: discord.TextChannel | discord.Thread,
    png_bytes: bytes,
    *,
    filename: str = "card.png",
    content: str | None = None,
    view: discord.ui.View | None = None,
) -> discord.Message:
    """Send a rendered card PNG to a channel as a standalone attachment.

    Parameters
    ----------
    channel : discord.TextChannel | discord.Thread
        The channel or thread to send to.
    png_bytes : bytes
        Raw PNG image bytes from render_card().
    filename : str
        Attachment filename.
    content : str | None
        Optional text content above the image.
    view : discord.ui.View | None
        Button/select view to attach below the image.

    Returns
    -------
    discord.Message
        The sent message.
    """
    file = discord.File(io.BytesIO(png_bytes), filename=filename)

    kwargs: dict = {"file": file}
    if content is not None:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view

    return await channel.send(**kwargs)
