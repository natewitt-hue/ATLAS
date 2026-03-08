"""
bot.py — WittGPT Unified Intelligence
Architecture:
  data_manager.py   loads & aggregates all league JSON + Discord SQLite DB
  analysis.py       stat routing & structured data
  reasoning.py      Gemini Intent Router & Text-to-SQL
  lore_rag.py       FAISS vector RAG for semantic lore
  embeds.py         Discord embed card builders
"""

import os
import re
import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai
from google.genai import types
from dotenv import load_dotenv

import data_manager as dm
import analysis
import embeds as emb
import reasoning
import intelligence as intel
import rules as rb
import lore_rag

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()
DISCORD_TOKEN       = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
LEADERBOARD_CHANNEL = int(os.getenv("LEADERBOARD_CHANNEL_ID", "0"))
ADMIN_USER_IDS      = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]

gemini = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
_startup_done = False

# ── WittGPT Persona Call ─────────────────────────────────────────────────────

async def call_wittgpt(user_input: str, context: str) -> str:
    """Synthesizes data into WittGPT's toxic persona."""
    system_instruction = (
        "You are WittGPT, the toxic, trash-talking commissioner of the TSL Madden League. "
        "ALWAYS refer to yourself as WittGPT in the 3rd person. Use profanity naturally. "
        "Use the provided context to be specific. If no context, roast them for wasting time."
    )
    try:
        response = gemini.models.generate_content(
            model="gemini-2.0-flash",
            config=types.GenerateContentConfig(
                system_instruction=system_instruction, 
                temperature=0.9,
                # WittGPT Protocol: Enabling live Google Search grounding
                tools=[{"google_search": {}}]
            ),
            contents=[f"CONTEXT:\n{context}\n\nUSER QUERY: {user_input}"]
        )
        return response.text.strip()
    except Exception as e:
        return f"WittGPT's Brain Error: {str(e)}"

# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _startup_done
    if _startup_done:
        print(f"--- WittGPT RECONNECTED | {bot.user} ---")
        return
    _startup_done = True
    dm.load_all()
    intel.build_owner_map()
    lore_rag.init()
    await bot.tree.sync()
    if LEADERBOARD_CHANNEL:
        leaderboard_task.start()
    print(f"--- WittGPT ONLINE | {bot.user} | {dm.get_league_status()} ---")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # 1. 24/7 Lore Listener (Live learning on every message)
    if len(message.content) > 20 or any(kw in message.content.lower() for kw in ["trade", "beef", "commish"]):
        try:
            lore_rag.add_single_message(message.author.display_name, message.content)
        except Exception:
            pass

    # 2. Intelligent Mention Handler
    if bot.user.mentioned_in(message):
        user_input = re.sub(r'<@!?\d+>', '', message.content).strip()
        
        async with message.channel.typing():
            # STEP A: Get Intent from the Brain
            intent = reasoning.get_intent(user_input, gemini)
            print(f"[Router] {message.author.name} wants: {intent}")

            # STEP B: Route to the correct module
            if intent == "HISTORY":
                # Text-to-SQL for counts and message history
                res = await reasoning.query_discord_history(user_input, gemini)
                wit = await call_wittgpt(user_input, res.get("context", ""))
                await message.reply(wit)
                await message.channel.send(embed=emb.sql_result_embed(res, user_input))

            elif intent == "RULES":
                # Search league rules
                hits = rb.lookup_rules(user_input)
                wit = await call_wittgpt(user_input, str(hits))
                await message.reply(wit)
                await message.channel.send(embed=emb.rule_lookup_embed(hits, user_input))

            elif intent == "STATS":
                # Execute Python code for Madden stats
                res = await reasoning.reason(user_input, gemini)
                context = res.get("result", "") if res.get("success") else res.get("error", "No data found.")
                wit = await call_wittgpt(user_input, context)
                await message.reply(wit)

            else: # LORE or OTHER
                # Vector RAG for drama/rivalries
                context = lore_rag.build_lore_context(user_input)
                wit = await call_wittgpt(user_input, context)
                await message.reply(wit)

    await bot.process_commands(message)

# ── Slash Commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="top_qbs", description="Show a chart of the top 10 passing leaders")
async def top_qbs(interaction: discord.Interaction):
    await interaction.response.defer()
    df = dm.df_offense
    qb_df = df[df['position'] == 'QB'].nlargest(10, 'passYds')
    chart_buf = analysis.generate_bar_chart(qb_df, 'name', 'passYds', title="TSL Passing Leaders")
    file = discord.File(chart_buf, filename="chart.png")
    await interaction.followup.send(file=file, embed=emb.chart_embed("Leaders", "Yards", "chart.png"))

@bot.tree.command(name="history", description="Search message archive")
async def slash_history(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    res = await reasoning.query_discord_history(question, gemini)
    wit = await call_wittgpt(question, res["context"])
    await interaction.followup.send(content=wit, embed=emb.sql_result_embed(res, question))

# ── Tasks ─────────────────────────────────────────────────────────────────────

@tasks.loop(hours=24)
async def leaderboard_task():
    if not LEADERBOARD_CHANNEL: return
    channel = bot.get_channel(LEADERBOARD_CHANNEL)
    if channel:
        data = intel.build_leaderboard_data()
        for e in emb.build_embeds(data):
            await channel.send(embed=e)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)