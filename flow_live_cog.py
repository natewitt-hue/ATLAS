"""
flow_live_cog.py — ATLAS FLOW Live Engagement System
─────────────────────────────────────────────────────
Manages #flow-live channel: pulse dashboard, highlight broadcasts,
session recaps. Consumes events from flow_events.py.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from dataclasses import dataclass as dc
from enum import Enum, auto
from typing import Optional

try:
    import discord
    from discord.ext import commands, tasks
except ImportError:
    discord = None  # type: ignore
    commands = None  # type: ignore
    tasks = None  # type: ignore

try:
    from flow_events import GameResultEvent, SportsbookEvent, PredictionEvent, flow_bus
except ImportError:
    flow_bus = None  # Soft fallback — cog degrades gracefully if flow_events missing

log = logging.getLogger(__name__)

SESSION_IDLE_TIMEOUT = 300  # 5 minutes


@dataclass
class PlayerSession:
    discord_id: int
    guild_id: int
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    total_games: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    net_profit: int = 0
    biggest_win: int = 0
    biggest_loss: int = 0
    current_streak: int = 0     # positive=wins, negative=losses
    best_streak: int = 0
    games_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    events: list = field(default_factory=list)

    def record(self, event: "GameResultEvent") -> None:
        self.last_activity = time.time()
        self.total_games += 1
        self.games_by_type[event.game_type] += 1
        self.events.append(event)

        profit = event.net_profit
        self.net_profit += profit

        if event.outcome == "win":
            self.wins += 1
            if profit > self.biggest_win:
                self.biggest_win = profit
            self.current_streak = max(self.current_streak, 0) + 1
        elif event.outcome == "loss":
            self.losses += 1
            if profit < self.biggest_loss:
                self.biggest_loss = profit
            self.current_streak = min(self.current_streak, 0) - 1
        else:
            self.pushes += 1

        if self.current_streak > self.best_streak:
            self.best_streak = self.current_streak


class SessionTracker:
    def __init__(self, idle_timeout: int = SESSION_IDLE_TIMEOUT):
        self._idle_timeout = idle_timeout
        # key: (discord_id, guild_id)
        self._active: dict[tuple[int, int], PlayerSession] = {}

    def record(self, event: "GameResultEvent") -> PlayerSession:
        key = (event.discord_id, event.guild_id)
        session = self._active.get(key)
        if session is None:
            session = PlayerSession(
                discord_id=event.discord_id,
                guild_id=event.guild_id,
            )
            self._active[key] = session
        session.record(event)
        return session

    def get_active(self, discord_id: int, guild_id: int) -> Optional[PlayerSession]:
        return self._active.get((discord_id, guild_id))

    def collect_expired(self) -> list[PlayerSession]:
        now = time.time()
        expired = []
        to_remove = []
        for key, session in self._active.items():
            if now - session.last_activity > self._idle_timeout:
                expired.append(session)
                to_remove.append(key)
        for key in to_remove:
            del self._active[key]
        return expired

    def get_all_active(self, guild_id: int) -> list[PlayerSession]:
        return [s for (_, gid), s in self._active.items() if gid == guild_id]


# ── Highlight Detection ──────────────────────────────────────────────────────

class HighlightType(Enum):
    INSTANT = auto()    # Post immediately as individual card
    SESSION = auto()    # Batch into session recap


@dc
class Highlight:
    highlight_type: HighlightType
    reason: str
    event: object       # GameResultEvent, SportsbookEvent, or PredictionEvent


# ── Thresholds (aggressive) ──
INSTANT_THRESHOLDS = {
    "jackpot": True,                # any jackpot hit
    "pvp_flip": True,               # any PvP coinflip result
    "last_man_standing": True,      # crash LMS
    "parlay": True,                 # any parlay hit (sportsbook)
    "prediction_resolution": True,  # any market resolution
}
SESSION_THRESHOLDS = {
    "min_multiplier": 2.0,         # win 2x+ → session highlight
    "min_loss": 300,               # loss $300+ → session highlight
    "min_streak": 3,               # 3+ win streak → session highlight
    "crash_min_cashout": 3.0,      # crash cashout 3x+ → session highlight
}


class HighlightDetector:
    def check(self, event: "GameResultEvent",
              session: Optional["PlayerSession"]) -> Optional[Highlight]:
        # ── Instant: jackpot ──
        if event.extra.get("jackpot"):
            return Highlight(HighlightType.INSTANT, "Jackpot hit!", event)

        # ── Instant: PvP flip ──
        if event.game_type == "coinflip_pvp":
            return Highlight(HighlightType.INSTANT, "PvP flip result", event)

        # ── Instant: crash last man standing ──
        if event.extra.get("last_man_standing"):
            return Highlight(HighlightType.INSTANT, "Last Man Standing", event)

        # ── Session: crash cashout (MUST come before generic multiplier — both match crash 3.5x) ──
        if (event.game_type == "crash" and event.outcome == "win"
                and event.multiplier >= SESSION_THRESHOLDS["crash_min_cashout"]):
            return Highlight(HighlightType.SESSION, f"Crash {event.multiplier}x cashout", event)

        # ── Session: big multiplier win (generic, all games) ──
        if event.outcome == "win" and event.multiplier >= SESSION_THRESHOLDS["min_multiplier"]:
            return Highlight(HighlightType.SESSION, f"{event.multiplier}x win", event)

        # ── Session: big loss ──
        if event.outcome == "loss" and event.wager >= SESSION_THRESHOLDS["min_loss"]:
            return Highlight(HighlightType.SESSION, f"Lost ${event.wager}", event)

        # ── Session: streak milestone ──
        if session and session.current_streak >= SESSION_THRESHOLDS["min_streak"]:
            return Highlight(HighlightType.SESSION, f"{session.current_streak}-win streak", event)

        return None

    def check_sportsbook(self, event: "SportsbookEvent") -> Optional[Highlight]:
        # Instant: any parlay
        if event.bet_type == "parlay":
            return Highlight(HighlightType.INSTANT, "Parlay hit!", event)
        # Session: big sportsbook loss
        if event.amount <= -SESSION_THRESHOLDS["min_loss"]:
            return Highlight(HighlightType.SESSION, f"Lost ${abs(event.amount)} on {event.bet_type}", event)
        # Session: big sportsbook win
        if event.amount >= SESSION_THRESHOLDS["min_loss"]:
            return Highlight(HighlightType.SESSION, f"Won ${event.amount} on {event.bet_type}", event)
        return None

    def check_prediction(self, event: "PredictionEvent") -> Optional[Highlight]:
        return Highlight(HighlightType.INSTANT,
                         f'"{event.market_title}" resolved {event.resolution}', event)


# ── FlowLiveCog ───────────────────────────────────────────────────────────────

class FlowLiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions = SessionTracker()
        self.detector = HighlightDetector()
        self._pulse_message_ids: dict[int, int] = {}  # guild_id → message_id

        if flow_bus:
            flow_bus.subscribe("game_result", self._on_game_result)
            flow_bus.subscribe("sportsbook_result", self._on_sportsbook_result)
            flow_bus.subscribe("prediction_result", self._on_prediction_result)

    async def cog_load(self):
        self._load_pulse_message_ids()
        self.pulse_loop.start()
        self.session_reaper.start()

    async def cog_unload(self):
        self.pulse_loop.cancel()
        self.session_reaper.cancel()
        if flow_bus:
            flow_bus.unsubscribe("game_result", self._on_game_result)
            flow_bus.unsubscribe("sportsbook_result", self._on_sportsbook_result)
            flow_bus.unsubscribe("prediction_result", self._on_prediction_result)

    # ── DB persistence for pulse message ID ──

    def _ensure_state_table(self):
        """Create flow_live_state table in flow_economy.db if needed."""
        try:
            import sqlite3
            conn = sqlite3.connect("flow_economy.db")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS flow_live_state (
                    guild_id    INTEGER PRIMARY KEY,
                    pulse_msg_id INTEGER NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception:
            log.exception("Failed to create flow_live_state table")

    def _load_pulse_message_ids(self):
        try:
            import sqlite3
            self._ensure_state_table()
            conn = sqlite3.connect("flow_economy.db")
            rows = conn.execute("SELECT guild_id, pulse_msg_id FROM flow_live_state").fetchall()
            conn.close()
            for guild_id, msg_id in rows:
                self._pulse_message_ids[guild_id] = msg_id
        except Exception:
            log.exception("Failed to load pulse message IDs")

    def _save_pulse_message_id(self, guild_id: int, message_id: int):
        try:
            import sqlite3
            conn = sqlite3.connect("flow_economy.db")
            conn.execute(
                "INSERT OR REPLACE INTO flow_live_state (guild_id, pulse_msg_id) VALUES (?, ?)",
                (guild_id, message_id)
            )
            conn.commit()
            conn.close()
            self._pulse_message_ids[guild_id] = message_id
        except Exception:
            log.exception("Failed to save pulse message ID")

    # ── Background tasks ──

    @tasks.loop(seconds=60)
    async def pulse_loop(self):
        for guild in self.bot.guilds:
            try:
                await self._update_pulse(guild)
            except Exception:
                log.exception("Pulse update failed for guild %s", guild.id)

    @pulse_loop.before_loop
    async def before_pulse_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def session_reaper(self):
        expired = self.sessions.collect_expired()
        for session in expired:
            try:
                await self._post_session_recap(session)
            except Exception:
                log.exception("Session recap failed for %s", session.discord_id)

    @session_reaper.before_loop
    async def before_session_reaper(self):
        await self.bot.wait_until_ready()

    # ── Event handlers ──

    async def _on_game_result(self, event):
        """Handle game result: track session, detect highlights."""
        session = self.sessions.record(event)
        highlight = self.detector.check(event, session)
        if highlight and highlight.highlight_type == HighlightType.INSTANT:
            await self._post_instant_highlight(highlight, event.guild_id)

    async def _on_sportsbook_result(self, event):
        """Handle sportsbook result: detect highlights."""
        highlight = self.detector.check_sportsbook(event)
        if highlight and highlight.highlight_type == HighlightType.INSTANT:
            await self._post_instant_highlight(highlight, event.guild_id)

    async def _on_prediction_result(self, event):
        """Handle prediction resolution: always post instant highlight."""
        highlight = self.detector.check_prediction(event)
        if highlight:
            await self._post_instant_highlight(highlight, event.guild_id)

    # ── Core methods ──

    async def _update_pulse(self, guild):
        """Aggregate live data and edit-in-place the pulse dashboard message."""
        channel = await self._get_flow_live_channel(guild.id)
        if not channel:
            return

        # Build pulse data from active sessions and DB
        active_sessions = self.sessions.get_all_active(guild.id)

        # Blackjack data
        bj_sessions = [s for s in active_sessions if "blackjack" in s.games_by_type]
        bj_players = []
        bj_streak_player = None
        bj_streak_count = 0
        for s in bj_sessions:
            member = guild.get_member(s.discord_id)
            name = member.display_name if member else str(s.discord_id)
            bj_players.append(name)
            if s.best_streak > bj_streak_count:
                bj_streak_count = s.best_streak
                bj_streak_player = name

        # Slots data (from sessions)
        slots_spins = sum(s.games_by_type.get("slots", 0) for s in active_sessions)
        slots_top_player = None
        slots_top_amount = 0
        slots_top_mult = 0
        for s in active_sessions:
            if s.biggest_win > slots_top_amount:
                slots_top_amount = s.biggest_win
                member = guild.get_member(s.discord_id)
                slots_top_player = member.display_name if member else str(s.discord_id)

        # Jackpot — query from DB
        jackpot_amount = 0
        jackpot_last_player = None
        jackpot_last_amount = 0
        jackpot_last_ago = "never"
        try:
            import sqlite3
            conn = sqlite3.connect("flow_economy.db")
            row = conn.execute("SELECT balance FROM jackpot_pool LIMIT 1").fetchone()
            if row:
                jackpot_amount = row[0]
            conn.close()
        except Exception:
            pass

        # Build recent highlights from session events (last 6)
        from casino.renderer.pulse_renderer import HighlightRow
        highlights = []
        all_events = []
        for s in active_sessions:
            member = guild.get_member(s.discord_id)
            name = member.display_name if member else str(s.discord_id)
            for evt in s.events[-3:]:  # last 3 per session
                all_events.append((evt, name))

        # Sort by recency (most recent first), take 6
        all_events.sort(key=lambda x: x[0].txn_id or 0, reverse=True)
        for evt, name in all_events[:6]:
            is_loss = evt.outcome == "loss"
            icon = "&#128293;" if not is_loss else "&#128128;"
            amount_str = f"+${evt.payout - evt.wager:,}" if not is_loss else f"-${evt.wager:,}"
            desc = f'<span style="color:#FBBF24;font-weight:600;">{name}</span>'
            if evt.game_type == "blackjack":
                desc += f' <span style="color:#c0b8a8;">{"won" if not is_loss else "lost"} at blackjack</span>'
            elif evt.game_type == "slots":
                desc += f' <span style="color:#c0b8a8;">{"hit" if not is_loss else "lost"} on slots</span>'
            else:
                desc += f' <span style="color:#c0b8a8;">{evt.game_type}</span>'
            highlights.append(HighlightRow(
                icon=icon, description_html=desc,
                amount_html=amount_str, time_ago="now", is_loss=is_loss
            ))

        from casino.renderer.pulse_renderer import build_pulse_data, render_pulse_card
        data = build_pulse_data(
            active_bj=len(bj_sessions), bj_players=bj_players,
            bj_streak_player=bj_streak_player, bj_streak_count=bj_streak_count,
            slots_spins_today=slots_spins, slots_top_player=slots_top_player,
            slots_top_amount=slots_top_amount, slots_top_mult=slots_top_mult,
            sb_week=0, sb_bets=0, sb_volume=0, sb_hot_player=None, sb_hot_desc="",
            pred_open=0, pred_hot_title="", pred_yes_pct=0, pred_no_pct=0, pred_volume=0,
            jackpot_amount=jackpot_amount, jackpot_last_player=jackpot_last_player,
            jackpot_last_amount=jackpot_last_amount, jackpot_last_ago=jackpot_last_ago,
            highlights=highlights,
        )

        # Render
        import io
        png_bytes = await render_pulse_card(data)
        file = discord.File(io.BytesIO(png_bytes), filename="pulse.png")

        # Edit-in-place or create new
        msg_id = self._pulse_message_ids.get(guild.id)
        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(attachments=[file])
                return
            except (discord.NotFound, discord.HTTPException):
                pass  # Message deleted, create new

        msg = await channel.send(file=file)
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        self._save_pulse_message_id(guild.id, msg.id)

    async def _post_session_recap(self, session):
        """Render and post session recap card."""
        channel = await self._get_flow_live_channel(session.guild_id)
        if not channel or session.total_games < 2:
            return

        guild = self.bot.get_guild(session.guild_id)
        member = guild.get_member(session.discord_id) if guild else None
        display_name = member.display_name if member else str(session.discord_id)

        from casino.renderer.session_recap_renderer import render_session_recap
        import io
        png_bytes = await render_session_recap(session, display_name)
        file = discord.File(io.BytesIO(png_bytes), filename="session_recap.png")
        await channel.send(file=file)

    async def _post_instant_highlight(self, highlight, guild_id: int):
        """Render and post an instant highlight card."""
        channel = await self._get_flow_live_channel(guild_id)
        if not channel:
            return

        import io
        png_bytes = None
        event = highlight.event

        try:
            if hasattr(event, "game_type"):
                # GameResultEvent
                guild = self.bot.get_guild(guild_id)
                member = guild.get_member(event.discord_id) if guild else None
                player = member.display_name if member else str(event.discord_id)

                if event.extra.get("jackpot"):
                    from casino.renderer.highlight_renderer import render_jackpot_card
                    png_bytes = await render_jackpot_card(player, event.payout, event.multiplier)
                elif event.game_type == "coinflip_pvp":
                    from casino.renderer.highlight_renderer import render_pvp_card
                    loser = event.extra.get("opponent", "opponent")
                    png_bytes = await render_pvp_card(player, loser, event.payout)
                elif event.extra.get("last_man_standing"):
                    from casino.renderer.highlight_renderer import render_crash_lms_card
                    png_bytes = await render_crash_lms_card(player, event.multiplier, event.payout)

            elif hasattr(event, "bet_type"):
                # SportsbookEvent
                if event.bet_type == "parlay":
                    guild = self.bot.get_guild(guild_id)
                    member = guild.get_member(event.discord_id) if guild else None
                    player = member.display_name if member else str(event.discord_id)
                    from casino.renderer.highlight_renderer import render_parlay_card
                    png_bytes = await render_parlay_card(player, 0, "", event.amount)

            elif hasattr(event, "market_title"):
                # PredictionEvent
                from casino.renderer.highlight_renderer import render_prediction_card
                png_bytes = await render_prediction_card(
                    event.market_title, event.resolution,
                    event.winners, event.total_payout
                )
        except Exception:
            log.exception("Failed to render highlight card")
            return

        if png_bytes:
            file = discord.File(io.BytesIO(png_bytes), filename="highlight.png")
            await channel.send(file=file)

    async def _get_flow_live_channel(self, guild_id: int):
        """Resolve #flow-live channel via setup_cog."""
        try:
            from setup_cog import get_channel_id
            ch_id = get_channel_id("flow_live", guild_id)
            if ch_id:
                return self.bot.get_channel(ch_id)
        except Exception:
            pass
        return None

    # ── _impl methods for boss_cog delegation ──

    async def _update_pulse_impl(self, guild):
        await self._update_pulse(guild)

    async def _test_highlight_impl(self, guild, channel):
        """Post a test highlight card."""
        from casino.renderer.highlight_renderer import render_jackpot_card
        import io
        png_bytes = await render_jackpot_card("TestPlayer", 5000, 50.0)
        file = discord.File(io.BytesIO(png_bytes), filename="test_highlight.png")
        await channel.send(file=file)

    async def _session_dump_impl(self, guild) -> str:
        sessions = self.sessions.get_all_active(guild.id)
        if not sessions:
            return "No active sessions."
        lines = []
        for s in sessions:
            lines.append(f"<@{s.discord_id}> — {s.total_games} games, net ${s.net_profit:+,}")
        return "\n".join(lines)


async def setup(bot: commands.Bot):
    await bot.add_cog(FlowLiveCog(bot))
