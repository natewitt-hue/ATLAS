"""
economy_cog.py — ATLAS Economy · Money Management
─────────────────────────────────────────────────────────────────────────────
Provides admin balance operations (give/take/set), role-based payouts,
and a recurring stipend system with full audit logging.

All commands are accessed through /commish eco <cmd> via commish_cog.py.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sportsbook.db")
STARTING_BALANCE = 1000

INTERVAL_HOURS = {
    "daily":    24,
    "weekly":   168,
    "biweekly": 336,
    "monthly":  720,
}


async def _setup_economy_tables() -> None:
    """Create economy tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS economy_stipends (
                stipend_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type  TEXT    NOT NULL,
                target_id    INTEGER NOT NULL,
                amount       INTEGER NOT NULL,
                interval     TEXT    NOT NULL,
                last_paid    TEXT,
                created_by   INTEGER NOT NULL,
                reason       TEXT    DEFAULT '',
                active       INTEGER DEFAULT 1
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS economy_log (
                log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id   INTEGER NOT NULL,
                action       TEXT    NOT NULL,
                amount       INTEGER NOT NULL,
                old_balance  INTEGER,
                new_balance  INTEGER,
                reason       TEXT    DEFAULT '',
                admin_id     INTEGER,
                logged_at    TEXT    NOT NULL
            )
        """)

        await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
#  CORE BALANCE OPERATIONS (all use BEGIN IMMEDIATE for safety)
# ═════════════════════════════════════════════════════════════════════════════

async def _ensure_user(db, discord_id: int) -> int:
    """Return current balance, auto-creating user if needed. Must be inside a transaction."""
    async with db.execute(
        "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        await db.execute(
            "INSERT INTO users_table (discord_id, balance, season_start_balance) VALUES (?,?,?)",
            (discord_id, STARTING_BALANCE, STARTING_BALANCE)
        )
        return STARTING_BALANCE
    return row[0]


async def admin_give(discord_id: int, amount: int, admin_id: int,
                     reason: str = "") -> tuple[int, int]:
    """Give money to a user. Returns (old_balance, new_balance)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            old_balance = await _ensure_user(db, discord_id)
            new_balance = old_balance + amount

            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id)
            )
            await db.execute("""
                INSERT INTO economy_log
                    (discord_id, action, amount, old_balance, new_balance, reason, admin_id, logged_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (discord_id, "give", amount, old_balance, new_balance, reason, admin_id, now))

            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return old_balance, new_balance


async def admin_take(discord_id: int, amount: int, admin_id: int,
                     reason: str = "") -> tuple[int, int]:
    """Take money from a user. Floors at 0. Returns (old_balance, new_balance)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            old_balance = await _ensure_user(db, discord_id)
            new_balance = max(0, old_balance - amount)

            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id)
            )
            await db.execute("""
                INSERT INTO economy_log
                    (discord_id, action, amount, old_balance, new_balance, reason, admin_id, logged_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (discord_id, "take", amount, old_balance, new_balance, reason, admin_id, now))

            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return old_balance, new_balance


async def admin_set(discord_id: int, amount: int, admin_id: int,
                    reason: str = "") -> tuple[int, int]:
    """Set exact balance. Returns (old_balance, new_balance)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            old_balance = await _ensure_user(db, discord_id)
            new_balance = max(0, amount)

            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id)
            )
            await db.execute("""
                INSERT INTO economy_log
                    (discord_id, action, amount, old_balance, new_balance, reason, admin_id, logged_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (discord_id, "set", amount, old_balance, new_balance, reason, admin_id, now))

            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return old_balance, new_balance


async def admin_check(discord_id: int) -> int:
    """Return current balance."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else STARTING_BALANCE


# ═════════════════════════════════════════════════════════════════════════════
#  STIPEND HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def get_due_stipends() -> list[dict]:
    """Return all active stipends that are past due."""
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT stipend_id, target_type, target_id, amount, interval, "
            "last_paid, created_by, reason, active "
            "FROM economy_stipends WHERE active=1"
        ) as cur:
            rows = await cur.fetchall()

    cols = ["stipend_id", "target_type", "target_id", "amount", "interval",
            "last_paid", "created_by", "reason", "active"]
    due = []
    for row in rows:
        stipend = dict(zip(cols, row))
        interval_hours = INTERVAL_HOURS.get(stipend["interval"], 24)
        if stipend["last_paid"] is None:
            due.append(stipend)
        else:
            last = datetime.fromisoformat(stipend["last_paid"])
            if (now - last).total_seconds() >= interval_hours * 3600:
                due.append(stipend)
    return due


async def mark_stipend_paid(stipend_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE economy_stipends SET last_paid=? WHERE stipend_id=?",
            (now, stipend_id)
        )
        await db.commit()


async def add_stipend(target_type: str, target_id: int, amount: int,
                      interval: str, created_by: int, reason: str = "") -> int:
    """Insert a new stipend. Returns stipend_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            INSERT INTO economy_stipends
                (target_type, target_id, amount, interval, created_by, reason)
            VALUES (?,?,?,?,?,?)
        """, (target_type, target_id, amount, interval, created_by, reason)) as cur:
            stipend_id = cur.lastrowid
        await db.commit()
    return stipend_id


async def deactivate_stipend(target_type: str, target_id: int) -> int:
    """Deactivate stipend(s) for a target. Returns count of deactivated."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE economy_stipends SET active=0 WHERE target_type=? AND target_id=? AND active=1",
            (target_type, target_id)
        )
        count = cur.rowcount
        await db.commit()
    return count


async def list_active_stipends() -> list[dict]:
    """Return all active stipends."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT stipend_id, target_type, target_id, amount, interval, "
            "last_paid, created_by, reason FROM economy_stipends WHERE active=1"
        ) as cur:
            rows = await cur.fetchall()
    cols = ["stipend_id", "target_type", "target_id", "amount", "interval",
            "last_paid", "created_by", "reason"]
    return [dict(zip(cols, row)) for row in rows]


# ═════════════════════════════════════════════════════════════════════════════
#  COG
# ═════════════════════════════════════════════════════════════════════════════

class EconomyCog(commands.Cog):
    """ATLAS Economy — Money management, role payouts, and stipends."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await _setup_economy_tables()
        self.stipend_loop.start()
        print("ATLAS: Economy · Money Management loaded.")

    def cog_unload(self):
        self.stipend_loop.cancel()

    # ── Audit helper ──────────────────────────────────────────────────────

    async def _post_audit(self, message: str) -> None:
        """Post an audit message to #admin-chat."""
        try:
            from setup_cog import get_channel_id
            ch_id = get_channel_id("admin_chat")
            if ch_id:
                ch = self.bot.get_channel(ch_id)
                if ch:
                    embed = discord.Embed(
                        title="💰 Economy",
                        description=message,
                        color=0xD4AF37,
                        timestamp=datetime.now(timezone.utc),
                    )
                    await ch.send(embed=embed)
        except Exception as e:
            print(f"[Economy] Audit post failed: {e}")

    # ── Stipend Loop ──────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def stipend_loop(self):
        """Check for due stipends every hour and process them."""
        due = await get_due_stipends()
        for stipend in due:
            try:
                await self._process_stipend(stipend)
            except Exception as e:
                print(f"[Economy] Stipend {stipend['stipend_id']} failed: {e}")

    @stipend_loop.before_loop
    async def before_stipend_loop(self):
        await self.bot.wait_until_ready()

    async def _process_stipend(self, stipend: dict) -> None:
        """Process a single stipend payment."""
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return

        if stipend["target_type"] == "role":
            role = guild.get_role(stipend["target_id"])
            if not role:
                return
            members = role.members
            target_name = f"@{role.name}"
        else:
            member = guild.get_member(stipend["target_id"])
            members = [member] if member else []
            target_name = f"<@{stipend['target_id']}>"

        paid_count = 0
        for member in members:
            if stipend["amount"] > 0:
                await admin_give(
                    member.id, stipend["amount"], stipend["created_by"],
                    f"Stipend: {stipend['reason']}"
                )
            else:
                await admin_take(
                    member.id, abs(stipend["amount"]), stipend["created_by"],
                    f"Deduction: {stipend['reason']}"
                )
            paid_count += 1

        await mark_stipend_paid(stipend["stipend_id"])

        if paid_count > 0:
            await self._post_audit(
                f"**Stipend processed** — {stipend['amount']:+,} TSL Bucks to "
                f"{target_name} ({paid_count} member{'s' if paid_count != 1 else ''})\n"
                f"Interval: {stipend['interval']} · Reason: *{stipend['reason'] or 'N/A'}*"
            )

    # ═══════════════════════════════════════════════════════════════════════
    #  _impl METHODS (called by commish_cog)
    # ═══════════════════════════════════════════════════════════════════════

    # ── Individual balance ────────────────────────────────────────────────

    async def _eco_give_impl(self, interaction: discord.Interaction,
                             member: discord.Member, amount: int,
                             reason: str = "Commissioner grant"):
        old, new = await admin_give(member.id, amount, interaction.user.id, reason)
        await self._post_audit(
            f"**{interaction.user.display_name}** gave **{amount:,}** to "
            f"**{member.display_name}** ({old:,} → {new:,})\n"
            f"Reason: *{reason}*"
        )
        embed = discord.Embed(
            title="✅ Money Given",
            description=(
                f"**{member.display_name}**: {old:,} → **{new:,} TSL Bucks** (+{amount:,})\n"
                f"Reason: *{reason}*"
            ),
            color=0x22C55E,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _eco_take_impl(self, interaction: discord.Interaction,
                             member: discord.Member, amount: int,
                             reason: str = "Commissioner deduction"):
        old, new = await admin_take(member.id, amount, interaction.user.id, reason)
        taken = old - new
        await self._post_audit(
            f"**{interaction.user.display_name}** took **{taken:,}** from "
            f"**{member.display_name}** ({old:,} → {new:,})\n"
            f"Reason: *{reason}*"
        )
        embed = discord.Embed(
            title="💸 Money Taken",
            description=(
                f"**{member.display_name}**: {old:,} → **{new:,} TSL Bucks** (-{taken:,})\n"
                f"Reason: *{reason}*"
            ),
            color=0xEF4444,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _eco_set_impl(self, interaction: discord.Interaction,
                            member: discord.Member, amount: int,
                            reason: str = "Commissioner set"):
        old, new = await admin_set(member.id, amount, interaction.user.id, reason)
        await self._post_audit(
            f"**{interaction.user.display_name}** set **{member.display_name}** "
            f"balance to **{new:,}** (was {old:,})\n"
            f"Reason: *{reason}*"
        )
        embed = discord.Embed(
            title="🔧 Balance Set",
            description=(
                f"**{member.display_name}**: {old:,} → **{new:,} TSL Bucks**\n"
                f"Reason: *{reason}*"
            ),
            color=0xD4AF37,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _eco_check_impl(self, interaction: discord.Interaction,
                              member: discord.Member):
        bal = await admin_check(member.id)
        embed = discord.Embed(
            title=f"💰 {member.display_name}",
            description=f"**Balance:** {bal:,} TSL Bucks",
            color=0xD4AF37,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Role-based ────────────────────────────────────────────────────────

    async def _eco_give_role_impl(self, interaction: discord.Interaction,
                                  role: discord.Role, amount: int,
                                  reason: str = "Role grant"):
        await interaction.response.defer(thinking=True, ephemeral=True)
        members = role.members
        if not members:
            return await interaction.followup.send(
                f"❌ No members have {role.mention}.", ephemeral=True
            )
        for m in members:
            await admin_give(m.id, amount, interaction.user.id, reason)

        await self._post_audit(
            f"**{interaction.user.display_name}** gave **{amount:,}** to "
            f"**{len(members)}** members with {role.mention}\n"
            f"Reason: *{reason}*"
        )
        embed = discord.Embed(
            title="✅ Role Payment",
            description=(
                f"Gave **{amount:,} TSL Bucks** to {len(members)} "
                f"member{'s' if len(members) != 1 else ''} with {role.mention}.\n"
                f"Reason: *{reason}*"
            ),
            color=0x22C55E,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _eco_take_role_impl(self, interaction: discord.Interaction,
                                  role: discord.Role, amount: int,
                                  reason: str = "Role deduction"):
        await interaction.response.defer(thinking=True, ephemeral=True)
        members = role.members
        if not members:
            return await interaction.followup.send(
                f"❌ No members have {role.mention}.", ephemeral=True
            )
        for m in members:
            await admin_take(m.id, amount, interaction.user.id, reason)

        await self._post_audit(
            f"**{interaction.user.display_name}** took **{amount:,}** from "
            f"**{len(members)}** members with {role.mention}\n"
            f"Reason: *{reason}*"
        )
        embed = discord.Embed(
            title="💸 Role Deduction",
            description=(
                f"Took **{amount:,} TSL Bucks** from {len(members)} "
                f"member{'s' if len(members) != 1 else ''} with {role.mention}.\n"
                f"Reason: *{reason}*"
            ),
            color=0xEF4444,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Stipend management ────────────────────────────────────────────────

    async def _eco_stipend_add_impl(self, interaction: discord.Interaction,
                                    role: discord.Role, amount: int,
                                    interval: str,
                                    reason: str = "Recurring stipend"):
        if interval not in INTERVAL_HOURS:
            return await interaction.response.send_message(
                f"❌ Invalid interval. Choose: {', '.join(INTERVAL_HOURS)}",
                ephemeral=True,
            )
        sid = await add_stipend("role", role.id, amount, interval,
                                interaction.user.id, reason)
        sign = f"+{amount:,}" if amount >= 0 else f"{amount:,}"
        await self._post_audit(
            f"**{interaction.user.display_name}** created stipend #{sid}: "
            f"**{sign}** {interval} to {role.mention}\n"
            f"Reason: *{reason}*"
        )
        embed = discord.Embed(
            title="📋 Stipend Created",
            description=(
                f"**Stipend #{sid}**\n"
                f"Role: {role.mention}\n"
                f"Amount: **{sign} TSL Bucks** per {interval}\n"
                f"Reason: *{reason}*\n\n"
                f"*Payments will begin within 1 hour.*"
            ),
            color=0xD4AF37,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _eco_stipend_remove_impl(self, interaction: discord.Interaction,
                                       role: discord.Role):
        count = await deactivate_stipend("role", role.id)
        if count == 0:
            return await interaction.response.send_message(
                f"❌ No active stipend found for {role.mention}.",
                ephemeral=True,
            )
        await self._post_audit(
            f"**{interaction.user.display_name}** removed {count} stipend(s) "
            f"for {role.mention}"
        )
        await interaction.response.send_message(
            f"✅ Deactivated **{count}** stipend(s) for {role.mention}.",
            ephemeral=True,
        )

    async def _eco_stipend_list_impl(self, interaction: discord.Interaction):
        stipends = await list_active_stipends()
        if not stipends:
            return await interaction.response.send_message(
                "No active stipends.", ephemeral=True
            )

        embed = discord.Embed(
            title="📋 Active Stipends",
            color=0xD4AF37,
        )
        for s in stipends:
            sign = f"+{s['amount']:,}" if s['amount'] >= 0 else f"{s['amount']:,}"
            target = (
                f"<@&{s['target_id']}>" if s["target_type"] == "role"
                else f"<@{s['target_id']}>"
            )
            last = s["last_paid"] or "Never"
            embed.add_field(
                name=f"#{s['stipend_id']} — {sign} / {s['interval']}",
                value=f"Target: {target}\nReason: *{s['reason'] or 'N/A'}*\nLast paid: {last}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _eco_stipend_paynow_impl(self, interaction: discord.Interaction):
        """Manually trigger all due stipend payments."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        due = await get_due_stipends()
        if not due:
            return await interaction.followup.send(
                "✅ No stipends are due right now.", ephemeral=True
            )
        count = 0
        for stipend in due:
            try:
                await self._process_stipend(stipend)
                count += 1
            except Exception as e:
                print(f"[Economy] Manual stipend {stipend['stipend_id']} failed: {e}")

        await interaction.followup.send(
            f"✅ Processed **{count}** stipend(s).", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
