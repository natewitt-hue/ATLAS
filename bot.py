
"""
bot.py — ATLAS Unified Intelligence v1.4.1
─────────────────────────────────────────────────────────────────────────────
DATA FLOW
─────────────────────────────────────────────────────────────────────────────
MaddenStats API (mymadden.com/api/lg/tsl/)
    │
    ▼
data_manager.load_all()  →  populates DataFrames  →  Discord commands
─────────────────────────────────────────────────────────────────────────────
v1.3.0 changes:
  - REBRAND: WittGPT → ATLAS throughout (version, persona, prints, exports).
  - ADD:     build_tsl_db.sync_tsl_db() — full DB rebuild from live API on
             every /wittsync and bot startup. tsl_history.db always current.
  - ADD:     /rebuilddb admin slash command — manual DB rebuild trigger.
  - ADD:     ATLAS_ICON_URL, ATLAS_GOLD, ATLAS_DARK, ATLAS_BLUE constants.
  - ADD:     change_presence() with ATLAS tagline on on_ready().
  - FIX:     Removed duplicate load prints for cogs that print their own messages.
  - FIX:     call_wittgpt() renamed to call_atlas().
  - FIX:     Snapshot export renamed ATLAS_Full_Code.txt.
─────────────────────────────────────────────────────────────────────────────
v1.4.0 changes:
  - RESTRUCTURE: Cog consolidation — sentinel, oracle, genesis, codex modules.
  - FIX: /wittsync admin-gated.
  - FIX: trade team selection uses autocomplete (bypasses 25-option limit).
  - REMOVE: analytics_cog, stats_hub_cog, gameplay_cog, complaint_cog,
             forcerequest_cog, fourthdown, positionchange_cog, ability_cog,
             history_cog, trade_center_cog, parity_cog, sportsbook absorbed
             into ATLAS module files.
─────────────────────────────────────────────────────────────────────────────
v1.4.1 changes:
  - ADD:  kalshi_cog — ATLAS Flow Casino: Prediction Market module.
          Syncs real-world Kalshi markets every 5 min; users bet TSL Bucks
          on Economics, Politics, and Entertainment events (/markets,
          /resolve_market). Betting and portfolio are in the /markets browser.
  - FIX:  All 8 cog-loader except blocks now include the exception message
          inline (e) so a quick read of the terminal log shows the failure
          without needing to parse the full traceback below it.
  - FIX:  Redundant loop variable re-definition in /wittsync removed.
  - FIX:  validate_db_usernames bare except now logs instead of silently
          discarding the error.
  - FIX:  Startup guard now also validates DISCORD_TOKEN and GEMINI_API_KEY
          presence before bot.run() — avoids cryptic AttributeError on
          misconfigured .env files.
─────────────────────────────────────────────────────────────────────────────
v1.4.2 changes:
  - FIX:  gemini_client guarded — None GEMINI_API_KEY no longer creates a
          broken client; call_atlas() returns an error string if missing.
  - FIX:  blowout_monitor AttributeError catch tightened — uses hasattr()
          gate instead of broad except that could mask flag-dict KeyErrors.
  - FIX:  /rebuilddb now passes cached players/abilities from dm (matches
          /wittsync) — eliminates duplicate API calls on manual rebuild.
  - FIX:  _startup_load() now logs progress markers before each phase so
          terminal output shows where the bot is stuck if an API hangs.
  - NOTE: ATLAS_ICON_URL uses a signed Discord CDN link that will expire.
          Replace with a permanent host when convenient.
─────────────────────────────────────────────────────────────────────────────
v1.5.0 changes:
  - ADD:  ATLAS Echo — voice persona system integrated.
          echo_loader.py provides get_persona() / load_all_personas() /
          infer_context() used by call_atlas() and all cogs.
          echo_cog.py provides /echorebuild and /echostatus admin commands.
  - ADD:  echo_cog loaded FIRST in setup_hook() so personas are in memory
          before any other cog attempts a Gemini call.
  - ADD:  load_all_personas() called at end of _startup_load() — three
          register files (casual/official/analytical) loaded on every boot.
          Falls back to stubs if echo/ files haven't been generated yet.
  - CHG:  call_atlas() now accepts persona_type param; system_instruction
          sourced from echo_loader.get_persona() instead of hardcoded string.
  - CHG:  on_message infers persona from channel name via infer_context()
          before calling call_atlas() — @mentions in stats channels get
          analytical voice; announcements/rulings get official voice; all
          else defaults to casual.
  - FIX:  awards_cog comment corrected — was mislabeled "ATLAS Echo".
─────────────────────────────────────────────────────────────────────────────
v2.0.0 changes:
  - OVERHAUL: Command Architecture, Permissions & Channel Routing.
          Reduces ~92 flat slash commands to ~10 for non-admin users.
  - ADD:  /atlas group (hidden) — sync, rebuilddb, clearsync, status,
          echorebuild, echostatus. Admin-only via default_permissions.
  - ADD:  /commish group (hidden) — 42 commissioner admin commands
          organized into subgroups: sb (15), casino (11), markets (3),
          plus 11 flat admin commands.
  - ADD:  commish_cog.py — new cog that delegates to existing _impl methods.
  - ADD:  permissions.py — centralized permission checks and channel routing.
  - ADD:  /setup command — interactive channel configuration for server admins.
  - ADD:  SentinelHubView — persistent button hub replacing flat sentinel commands.
  - ADD:  Sportsbook board buttons — My Bets, History, Leaderboard, Props.
  - ADD:  Casino hub "My Stats" button.
  - ADD:  Oracle HubView "Season Recap" button and modal.
  - ADD:  Polymarket browser "My Portfolio" button.
  - ADD:  Genesis "Cornerstone Designate" modal in hub.
  - CHG:  All admin commands retain deprecated flat wrappers during transition.
  - RMV:  /lockgame, /setline (legacy aliases).
  - RMV:  /blackjack, /slots, /crash, /coinflip, /challenge, /scratch,
          /casino_stats (absorbed into /casino hub).
  - RMV:  /mybets, /bethistory, /leaderboard, /props (absorbed into
          /sportsbook board buttons).
  - RMV:  /bet, /portfolio (absorbed into /markets browser).
  - RMV:  /complaint, /disconnectlookup, /blowoutcheck, /statcheck,
          /positionchange, /positionchangelog (absorbed into /sentinel).
  - RMV:  /h2h, /season_recap (absorbed into oracle HubView buttons).
  - RMV:  /tradelookup, /devaudit, /abilityaudit, /abilitycheck,
          /cornerstonedesignate, /lotterystandings, /contractcheck
          (absorbed into /genesis buttons).
  - RMV:  /statshub (redundant with /stats hub).
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import glob
import os
import re
import time
import traceback
import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks
from dotenv import load_dotenv

import atlas_ai
import data_manager as dm
import reasoning
import build_tsl_db as db_builder
import build_member_db as member_db
import roster
from ui_state import UIStateManager
from conversation_memory import add_conversation_turn, build_conversation_block

# Optional modules
try: import intelligence as intel
except ImportError: intel = None

try: import lore_rag
except ImportError: lore_rag = None

try:
    import affinity as affinity_mod
    _affinity_available = True
except ImportError:
    affinity_mod = None
    _affinity_available = False

try:
    from echo_loader import load_all_personas, get_persona, infer_context
    _echo_available = True
except ImportError:
    _echo_available = False
    def get_persona(context_type: str = "casual") -> str:
        return (
            "You are ATLAS, the official AI intelligence system for The Simulation League (TSL). "
            "ALWAYS refer to yourself as ATLAS in the 3rd person. "
            "Keep responses concise and direct. Use profanity sparingly but effectively. "
            "Deliver the factual answer with a dismissive, authoritative tone."
        )
    def infer_context(**kwargs) -> str:
        return "casual"
    def load_all_personas() -> dict:
        return {}

load_dotenv()

# ── Bot Version ──────────────────────────────────────────────────────────────
ATLAS_VERSION = "4.0.1"  # Quiet handling for expired interactions (10062/40060)
from constants import ATLAS_ICON_URL, ATLAS_GOLD

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
from permissions import ADMIN_USER_IDS
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "0"))

intents       = discord.Intents.all()
bot           = commands.Bot(command_prefix="!", intents=intents)

# Hub Infrastructure — persistent UI state manager
bot.ui_state  = UIStateManager(bot)

# ── FIX #8: Startup guard — prevents re-running load_all() on reconnect ──────
_startup_done = False


def _invalidate_caches():
    """Clear query cache after data refresh. Called from all sync_tsl_db paths."""
    try:
        from codex_cog import clear_query_cache
        clear_query_cache()
    except Exception:
        pass

# ── Extension Loader ─────────────────────────────────────────────────────────

@bot.event
async def setup_hook():
    # Ordered cog list — load order matters:
    #   1. echo_cog FIRST: personas must be in memory before any cog calls Gemini.
    #      Fallback stubs activate if echo/ files haven't been generated yet.
    #   2. setup_cog SECOND: provisions channels and populates server_config
    #      table that all other cogs depend on for routing.
    #   3. Everything else: order does not matter; all print their own load messages.
    _EXTENSIONS = [
        "echo_cog",           # ATLAS Echo — voice personas (MUST be first)
        "setup_cog",          # ATLAS Setup — server config (MUST be second)
        "flow_sportsbook",    # ATLAS Flow — TSL sportsbook
        "casino.casino",      # ATLAS Casino — games & economy
        "oracle_cog",         # ATLAS Oracle — stats, profiles, analytics
        "genesis_cog",        # ATLAS Genesis — trade center, parity, dev traits
        "sentinel_cog",       # ATLAS Sentinel — enforcement, compliance, disputes
        "awards_cog",         # ATLAS Core — awards & voting
        "codex_cog",          # ATLAS Codex — historical AI (/ask, /h2h)
        "polymarket_cog",     # ATLAS Flow — Polymarket prediction markets
        "economy_cog",        # ATLAS Economy — money management & stipends
        "flow_live_cog",      # ATLAS Flow — live engagement system
        "real_sportsbook_cog",# ATLAS Flow — real NFL/NBA sportsbook
        # "commish_cog",      # RETIRED — replaced by boss_cog visual hub
        "boss_cog",           # ATLAS Boss — visual commissioner control room
    ]

    for ext in _EXTENSIONS:
        try:
            await bot.load_extension(ext)
        except Exception as e:
            print(f"ATLAS Error loading {ext}: {e}")
            traceback.print_exc()

    # Flow wallet DB setup (creates transactions table if needed)
    try:
        import flow_wallet
        await flow_wallet.setup_wallet_db()
        print("ATLAS: Flow wallet system initialized.")
    except Exception as e:
        print(f"ATLAS: Flow wallet setup failed: {e}")

    # Hub Infrastructure — persistent UI state table
    try:
        await bot.ui_state.init_table()
        print("ATLAS: UI State Manager initialized.")
    except Exception as e:
        print(f"ATLAS: UI State init failed: {e}")

    # Affinity DB table setup (safe to call every startup)
    if _affinity_available:
        try:
            await affinity_mod.setup_affinity_db()
            print("ATLAS: User affinity system initialized.")
        except Exception as e:
            print(f"ATLAS: Affinity DB setup failed: {e}")

    # HTML render engine — page pool for card rendering
    try:
        from atlas_html_engine import init_pool
        await init_pool()
        print("ATLAS: HTML render engine initialized.")
    except Exception as e:
        print(f"ATLAS: Render engine init failed: {e}")

    # FIX #9: Only sync command tree on initial boot (setup_hook runs once).
    # This avoids burning Discord's 200 syncs/day rate limit during debugging.
    # Use !clearsync for manual re-sync if needed.
    await bot.tree.sync()

# ── Global App Command Error Handler ─────────────────────────────────────────

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Catch-all so no interaction ever goes unacknowledged (prevents 10062)."""
    cmd_name = interaction.command.name if interaction.command else "unknown"

    # ── Expired / stale interactions (unrecoverable — quiet log only) ─────
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    if isinstance(original, discord.NotFound) and original.code == 10062:
        print(f"[CommandError] /{cmd_name}: interaction expired (10062) — likely stale after RESUME")
        return
    if isinstance(original, discord.HTTPException) and original.code == 40060:
        print(f"[CommandError] /{cmd_name}: already acknowledged (40060)")
        return

    if isinstance(error, app_commands.CheckFailure):
        # Permission check already sent a message — nothing more to do
        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_message(
                "ATLAS: You don't have permission for this command.", ephemeral=True
            )
        except discord.NotFound:
            pass
        return

    print(f"[CommandError] /{cmd_name}: {error}")
    traceback.print_exc()

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "ATLAS encountered an error processing this command.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "ATLAS encountered an error processing this command.", ephemeral=True
            )
    except discord.NotFound:
        pass  # interaction fully expired — nothing we can do
    except discord.HTTPException as exc:
        print(f"[CommandError] Failed to send error response for /{cmd_name}: {exc}")

# ── ATLAS Persona Call ──────────────────────────────────────────────────────

async def call_atlas(user_input: str, context: str, persona_type: str = "casual") -> str:
    """Synthesizes data into ATLAS's Echo voice persona with Search enabled.

    persona_type: "casual" | "official" | "analytical"
    Sourced from echo_loader.get_persona() — falls back to inline stub if
    echo_loader is unavailable or echo/ files haven't been generated yet.
    """
    system_instruction = get_persona(persona_type)
    prompt = f"CONTEXT:\n{context}\n\nUSER QUERY: {user_input}"
    try:
        result = await atlas_ai.generate_with_search(prompt, system=system_instruction)
        return result.text
    except Exception as e:
        print(f"[atlas_ai] call_atlas failed: {e}")
        traceback.print_exc()
        return "ATLAS is having trouble thinking right now. Try again in a moment."

# ── Startup & Event Loops ────────────────────────────────────────────────────

def _startup_load():
    """
    Blocking startup: load all data from MaddenStats API + rebuild tsl_history.db.
    Runs in a thread executor so it doesn't block the Discord event loop.
    """
    print("[Startup] Loading league data from MaddenStats API...")
    dm.load_all()

    # Rebuild tsl_history.db from live API data after every load
    # Pass pre-loaded player/ability data from dm to avoid duplicate API hits
    print("[Startup] Syncing tsl_history.db...")
    try:
        db_result = db_builder.sync_tsl_db(
            players=dm.get_players(),
            abilities=dm.get_player_abilities(),
        )
        if db_result["success"]:
            print(f"[TSL-DB] Startup DB sync OK — {db_result['games']} games | {db_result['players']} players ({db_result['elapsed']}s)")
        else:
            print(f"[TSL-DB] Startup DB sync had issues: {db_result['errors']}")
        _invalidate_caches()
    except Exception as e:
        print(f"[TSL-DB] Startup DB sync failed: {e}")

    # Build member registry, then auto-fill missing db_usernames from live teams table
    print("[Startup] Building member registry...")
    try:
        result = member_db.build_member_table()
        print(f"[MemberDB] Registry built — {result['active']} active members")
        sync_result = member_db.sync_db_usernames_from_teams()
        if sync_result.get("filled"):
            print(f"[MemberDB] Auto-filled {sync_result['filled']} db_username(s) from teams table")
        # Warn about any db_usernames that don't appear in actual game records
        try:
            ghosts = member_db.validate_db_usernames()
            if ghosts:
                names = ", ".join(f"{g['discord_username']}→{g['db_username']}" for g in ghosts)
                print(f"[MemberDB] ⚠️  db_username not found in games table: {names}")
        except Exception as e:
            print(f"[MemberDB] validate_db_usernames() failed: {e}")
        # Refresh codex identity cache so newly auto-filled db_usernames are visible
        try:
            from codex_cog import refresh_codex_identity
            refresh_codex_identity()
        except Exception as e:
            print(f"[MemberDB] Codex identity refresh failed: {e}")
    except Exception as e:
        print(f"[MemberDB] Startup registry build failed: {e}")

    # Load owner roster (must run after member_db + dm.load_all)
    try:
        count = roster.load()
        print(f"[Roster] {count} team assignments loaded")
    except Exception as e:
        print(f"[Roster] Failed to load: {e}")

    # Build intelligence owner map (reads from roster + API data)
    if intel:
        try:
            intel.build_owner_map()
        except Exception as e:
            print(f"ATLAS: build_owner_map() failed: {e}")

    # Load Echo voice personas into memory — must run after file system is ready.
    # If echo/ files don't exist yet, fallback stubs activate automatically.
    print("[Startup] Loading Echo voice personas...")
    try:
        loaded = load_all_personas()
        if loaded:
            print(f"[Echo] Personas loaded: {', '.join(loaded.keys())}")
        else:
            print("[Echo] No persona files found — fallback stubs active. Run: python echo_voice_extractor.py")
    except Exception as e:
        print(f"[Echo] Persona load failed: {e} — fallback stubs active.")

@tasks.loop(minutes=15)
async def blowout_monitor():
    """Background task: check for stat-padding flags and push to admin channel."""
    # Gate: if dm.flag_stat_padding doesn't exist yet, cancel and stop wasting a task slot
    if not hasattr(dm, 'flag_stat_padding'):
        print("[BlowoutMonitor] dm.flag_stat_padding() not available — monitor is a no-op until implemented.")
        blowout_monitor.cancel()
        return

    try:
        flags = dm.flag_stat_padding(dm.CURRENT_WEEK)
        if not flags:
            return

        # FIX #10: Resolve admin channel via setup_cog instead of env var.
        # Falls back to ADMIN_CHANNEL_ID env var if setup_cog hasn't run yet.
        try:
            from setup_cog import get_channel_id
            ch_id = get_channel_id("admin_chat") or ADMIN_CHANNEL_ID
        except ImportError:
            ch_id = ADMIN_CHANNEL_ID

        ch = bot.get_channel(ch_id)
        if not ch:
            return
        for flag in flags:
            msg = (
                f"🚨 **Stat Padding Flag** — Week {dm.CURRENT_WEEK}\n"
                f"**{flag['name']}** ({flag['team']}): "
                f"{flag['delta']} {flag['stat']} (threshold: {flag['threshold']})"
            )
            await ch.send(msg)
    except Exception as e:
        print(f"[BlowoutMonitor] Error during flag check: {e}")

@bot.event
async def on_ready():
    global _startup_done, _bot_start_time

    if _startup_done:
        print(f"--- ATLAS v{ATLAS_VERSION} RECONNECTED | {dm.get_league_status()} (skipping reload) ---")
        return
    _startup_done = True  # Set BEFORE async work to prevent concurrent on_ready races

    _bot_start_time = time.time()

    # Set presence immediately so the bot shows online during data load
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="TSL · INTELLIGENCE · OVERSIGHT · AUTHORITY"
        )
    )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _startup_load)

    print(f"--- ATLAS v{ATLAS_VERSION} ONLINE | {dm.get_league_status()} ---")
    print(f"--- ATLAS v{ATLAS_VERSION} | Data sourced from MaddenStats API ---")

    # Discover guild members — log all, flag unknowns, update display names
    for guild in bot.guilds:
        human_members = [m for m in guild.members if not m.bot]
        print(f"\n📋 Guild: {guild.name} — {len(human_members)} human members")
        for m in sorted(human_members, key=lambda m: m.display_name.lower()):
            print(f"   {m.display_name:<25} @{m.name:<25} ID: {m.id}")

        member_list = [
            {"discord_id": m.id, "username": m.name, "display_name": m.display_name}
            for m in human_members
        ]
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, member_db.discover_guild_members, member_list)
        print(f"   Registry: {result['known']} known, {result['new']} new, {result['updated']} display names updated")

    # Auto-discover guild structure (channels, roles, metadata, emojis)
    try:
        from setup_cog import auto_discover
        for guild in bot.guilds:
            await auto_discover(guild)
    except Exception as e:
        print(f"[DISCOVERY] Auto-discovery failed: {e}")

    # Restore persistent hub views from ui_state table
    await bot.ui_state.restore_all_views()

    # Guard prevents spawning a duplicate task on every Discord reconnect
    if not blowout_monitor.is_running():
        blowout_monitor.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message):
        user_input = re.sub(r'<@!?\d+>', '', message.content).strip()
        async with message.channel.typing():
            try:
                persona_type = infer_context(channel_name=message.channel.name)

                # ── Affinity lookup ────────────────────────────────
                affinity_instruction = ""
                if _affinity_available:
                    try:
                        score = await affinity_mod.get_affinity(message.author.id)
                        affinity_instruction = affinity_mod.get_affinity_instruction(score)
                    except Exception:
                        pass

                # ── Lore RAG context (async to avoid CPU blocking) ─
                if lore_rag and hasattr(lore_rag, 'build_lore_context_async'):
                    context = await lore_rag.build_lore_context_async(user_input)
                elif lore_rag:
                    context = await asyncio.get_running_loop().run_in_executor(
                        None, lore_rag.build_lore_context, user_input,
                    )
                else:
                    context = ""

                if affinity_instruction:
                    context = f"{affinity_instruction}\n\n{context}"

                # ── Conversation memory (follow-up context) ─────────
                conv_block = await build_conversation_block(
                    message.author.id, source="casual",
                )
                if conv_block:
                    context = f"{conv_block}\n\n{context}"

                wit = await call_atlas(user_input, context, persona_type=persona_type)
                await message.reply(wit)

                # ── Record this exchange for future context ─────────
                await add_conversation_turn(
                    message.author.id, user_input, wit, source="casual",
                )

                # ── Post-interaction affinity update ───────────────
                if _affinity_available:
                    try:
                        sentiment = affinity_mod.analyze_sentiment(user_input)
                        await affinity_mod.update_affinity(message.author.id, sentiment)
                    except Exception:
                        pass

            except Exception as e:
                print(f"Message Processing Error: {e}")
                traceback.print_exc()
                await message.reply("ATLAS is currently undergoing maintenance. Try again later.")

    await bot.process_commands(message)

# ── /atlas Admin Group ────────────────────────────────────────────────────────

atlas_group = app_commands.Group(
    name="atlas",
    description="ATLAS core system administration.",
    default_permissions=discord.Permissions(administrator=True),
)

_bot_start_time: float = 0.0  # Set on_ready for /atlas status uptime


@atlas_group.command(name="sync", description="Reload league data from MaddenStats API.")
async def atlas_sync(interaction: discord.Interaction):
    await _sync_impl(interaction)


@atlas_group.command(name="rebuilddb", description="Force rebuild tsl_history.db from MaddenStats API.")
async def atlas_rebuilddb(interaction: discord.Interaction):
    await _rebuilddb_impl(interaction)


@atlas_group.command(name="clearsync", description="Force re-sync command tree to this server.")
async def atlas_clearsync(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    bot.tree.clear_commands(guild=interaction.guild)
    bot.tree.copy_global_to(guild=interaction.guild)
    await bot.tree.sync(guild=interaction.guild)
    await interaction.followup.send("Commands synced. Restart Discord to see updates.")


@atlas_group.command(name="status", description="Show ATLAS system status, uptime, and data freshness.")
async def atlas_status(interaction: discord.Interaction):
    uptime_sec = int(time.time() - _bot_start_time) if _bot_start_time else 0
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    status = dm.get_league_status() if hasattr(dm, 'get_league_status') else "Unknown"

    cog_names = [name for name, _ in bot.cogs.items()]
    cog_list = ", ".join(cog_names) if cog_names else "None loaded"

    embed = discord.Embed(
        title="ATLAS System Status",
        color=ATLAS_GOLD,
    )
    embed.add_field(name="Version", value=f"v{ATLAS_VERSION}", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="League", value=status, inline=True)
    embed.add_field(name="Cogs Loaded", value=cog_list, inline=False)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)

    if _echo_available:
        try:
            from echo_loader import get_persona_status
            ps = get_persona_status()
            echo_lines = []
            for reg, info in ps.items():
                state = "LIVE" if info.get("loaded") else ("FALLBACK" if info.get("using_fallback") else "OFF")
                echo_lines.append(f"**{reg}**: {state}")
            embed.add_field(name="Echo Personas", value="\n".join(echo_lines), inline=False)
        except Exception:
            pass

    await interaction.response.send_message(embed=embed, ephemeral=True)


@atlas_group.command(name="affinity", description="View or reset a user's ATLAS affinity score.")
@app_commands.describe(user="The user to check", reset="Reset their score to 0?")
async def atlas_affinity(interaction: discord.Interaction, user: discord.Member, reset: bool = False):
    if not _affinity_available:
        return await interaction.response.send_message(
            "❌ Affinity system not loaded.", ephemeral=True,
        )
    if reset:
        await affinity_mod.reset_affinity(user.id)
        await interaction.response.send_message(
            f"🔄 Reset affinity for **{user.display_name}** to 0.", ephemeral=True,
        )
    else:
        score = await affinity_mod.get_affinity(user.id)
        tier = affinity_mod.get_tier_label(score)
        embed = discord.Embed(
            title=f"User Affinity — {user.display_name}",
            color=ATLAS_GOLD,
        )
        embed.add_field(name="Score", value=f"`{score:.1f}`", inline=True)
        embed.add_field(name="Tier", value=tier, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(atlas_group)


# ── Shared Implementations (used by /atlas and deprecated wrappers) ──────────

async def _sync_impl(interaction: discord.Interaction):
    """Core sync logic — shared by /atlas sync and deprecated /wittsync."""
    await interaction.response.defer(thinking=True)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, dm.load_all)

    # Refresh roster cache after data reload
    try:
        roster.load()
    except Exception as e:
        print(f"[Roster] Refresh failed during sync: {e}")

    if intel:
        try:
            intel.build_owner_map()
        except Exception as e:
            print(f"ATLAS: build_owner_map() failed during sync: {e}")

    try:
        reasoning._SCHEMA_CACHE     = ""
        reasoning._SCHEMA_TIMESTAMP = 0.0
    except AttributeError:
        pass

    try:
        if dm._autograde_callback is not None:
            await dm._autograde_callback()
    except Exception as e:
        print(f"ATLAS: auto_grade error: {e}")

    try:
        db_result = await loop.run_in_executor(
            None,
            lambda: db_builder.sync_tsl_db(
                players=dm.get_players(),
                abilities=dm.get_player_abilities(),
            )
        )
        if db_result["success"]:
            db_line = f"\nHistory DB: **{db_result['games']}** games | **{db_result['players']}** players ({db_result['elapsed']}s)"
        else:
            db_line = f"\nHistory DB sync had issues: {', '.join(db_result['errors'][:2])}"
    except Exception as e:
        db_line = f"\nHistory DB sync failed: `{e}`"

    _invalidate_caches()

    status = dm.get_league_status()
    await interaction.followup.send(
        f"Data reloaded. League status: **{status}**{db_line}"
    )


async def _rebuilddb_impl(interaction: discord.Interaction):
    """Core DB rebuild logic — shared by /atlas rebuilddb and deprecated /rebuilddb."""
    await interaction.response.defer(thinking=True)
    loop = asyncio.get_running_loop()
    players = dm.get_players() if hasattr(dm, 'get_players') else None
    abilities = dm.get_player_abilities() if hasattr(dm, 'get_player_abilities') else None
    if players is not None and abilities is not None:
        db_result = await loop.run_in_executor(
            None,
            lambda: db_builder.sync_tsl_db(players=players, abilities=abilities)
        )
    else:
        db_result = await loop.run_in_executor(None, db_builder.sync_tsl_db)
    if db_result["success"]:
        lines = [
            f"**tsl_history.db rebuilt** in {db_result['elapsed']}s",
            f"Games: **{db_result['games']}**",
            f"Players: **{db_result['players']}**",
        ]
        if db_result["errors"]:
            lines.append(f"Warnings: {', '.join(db_result['errors'][:3])}")
    else:
        lines = [f"DB rebuild failed: {', '.join(db_result['errors'][:3])}"]

    _invalidate_caches()

    await interaction.followup.send("\n".join(lines))


# ── Code Snapshot Export ──────────────────────────────────────────────────────

def export_code_snapshot():
    """Auto-export all .py files into a single text file on every boot."""
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(bot_dir, "ATLAS_Full_Code.txt")
    py_files = sorted(glob.glob(os.path.join(bot_dir, "**", "*.py"), recursive=True))

    with open(output_path, "w", encoding="utf-8") as out:
        for filepath in py_files:
            rel_path = os.path.relpath(filepath, bot_dir)
            out.write("=" * 60 + "\n")
            out.write(f"FILE: {rel_path}\n")
            out.write("=" * 60 + "\n\n")
            with open(filepath, "r", encoding="utf-8") as f:
                out.write(f.read())
            out.write("\n\n")

    print(f"[CodeExport] Snapshot saved: {output_path} ({len(py_files)} files)")

# ── Launch ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Guard against missing .env values — fail loudly before Discord even connects
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env — bot cannot start.")
    if not GEMINI_API_KEY:
        print("⚠️  WARNING: GEMINI_API_KEY is not set — /ask and ATLAS AI responses will fail.")
    if not ANTHROPIC_API_KEY:
        print("⚠️  WARNING: ANTHROPIC_API_KEY not set — Oracle v3 agent will be unavailable")

    export_code_snapshot()

    # Clean up Playwright browser on shutdown to avoid pipe errors
    _orig_close = bot.close
    async def _graceful_close():
        try:
            from atlas_html_engine import drain_pool
            await drain_pool()
        except Exception:
            pass
        await _orig_close()
    bot.close = _graceful_close

    bot.run(DISCORD_TOKEN)

