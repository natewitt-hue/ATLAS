"""
awards_cog.py — ATLAS Echo · Anonymous Awards & Voting System
"""
import json
import os
import uuid
import discord
from discord import app_commands
from discord.ext import commands

from permissions import ADMIN_USER_IDS

# ── Poll persistence ──────────────────────────────────────────────────────────
_POLLS_PATH = os.path.join(os.path.dirname(__file__), "polls_state.json")

def _load_polls() -> dict:
    if os.path.isfile(_POLLS_PATH):
        try:
            with open(_POLLS_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[awards_cog] Poll load error: {e}")
    return {}

def _save_polls() -> None:
    try:
        with open(_POLLS_PATH, "w") as f:
            json.dump(_polls, f, indent=2)
    except Exception as e:
        print(f"[awards_cog] Poll save error: {e}")

_polls: dict = _load_polls()

class VoteSelect(discord.ui.Select):
    def __init__(self, poll_id, options):
        self.poll_id = poll_id
        opts = [discord.SelectOption(label=opt, value=opt) for opt in options[:25]]
        super().__init__(
            placeholder="Cast your anonymous vote...",
            min_values=1, max_values=1, options=opts,
            custom_id=f"vote_select:{poll_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        poll = _polls.get(self.poll_id)
        if poll is None:
            return await interaction.response.send_message(
                "❌ This poll is no longer available (bot may have restarted).",
                ephemeral=True,
            )
        if not poll.get("open", False):
            return await interaction.response.send_message("❌ This poll is closed.", ephemeral=True)
        
        uid = str(interaction.user.id)   # str for JSON key compat (keys become strings on disk load)
        if uid in poll["votes"]:
            return await interaction.response.send_message("⚠️ You have already voted.", ephemeral=True)
            
        poll["votes"][uid] = self.values[0]
        _save_polls()  # persist every vote immediately
        await interaction.response.send_message("✅ Vote recorded anonymously.", ephemeral=True)

class VoteView(discord.ui.View):
    def __init__(self, poll_id, options):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.add_item(VoteSelect(poll_id, options))

class AwardsCog(commands.Cog):
    def __init__(self, bot): self.bot = bot

    # ── Implementation methods (used by /commish and deprecated wrappers) ──

    async def _createpoll_impl(self, interaction: discord.Interaction, title: str, nominees: str):
        poll_id = str(uuid.uuid4())[:8]
        options = [n.strip() for n in nominees.split(",")]
        _polls[poll_id] = {"title": title, "options": options, "votes": {}, "open": True}
        _save_polls()

        embed = discord.Embed(title=f"🗳️ {title}", description="Select your choice from the dropdown below. Votes are blind.", color=discord.Color.gold())
        view = VoteView(poll_id, options)

        await interaction.response.send_message(f"Poll created: {title}", ephemeral=True)
        if not interaction.channel:
            return  # guard against DM context where channel may be None
        await interaction.channel.send(embed=embed, view=view)

    async def _closepoll_impl(self, interaction: discord.Interaction, poll_id: str):
        if poll_id not in _polls:
            return await interaction.response.send_message("Poll not found.", ephemeral=True)

        _polls[poll_id]["open"] = False
        _save_polls()
        tally = {}
        for vote in _polls[poll_id]["votes"].values():
            tally[vote] = tally.get(vote, 0) + 1

        results = "\n".join([f"**{opt}**: {tally.get(opt, 0)} votes" for opt in _polls[poll_id]["options"]])

        embed = discord.Embed(title=f"Final Results: {_polls[poll_id]['title']}", description=results, color=discord.Color.green())
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(AwardsCog(bot))
    print("ATLAS: Awards Engine loaded.")