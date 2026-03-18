"""
economy_cog.py — ATLAS Economy · Money Management
─────────────────────────────────────────────────────────────────────────────
Provides admin balance operations (give/take/set), role-based payouts,
and a recurring stipend system with full audit logging.

All commands are accessed through /commish eco <cmd> via commish_cog.py.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

# -- Database ------------------------------------------------------------------
import flow_wallet
DB_PATH          = flow_wallet.DB_PATH
STARTING_BALANCE = flow_wallet.STARTING_BALANCE

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
    ref_key = f"ADMIN_GIVE_{discord_id}_{int(datetime.now(timezone.utc).timestamp())}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # flow_wallet.get_balance and .credit both honor the `con` param:
            # when passed, they use the caller's connection without committing.
            old_balance = await flow_wallet.get_balance(discord_id, con=db)
            new_balance = await flow_wallet.credit(
                discord_id, amount, "ADMIN",
                description=reason or "admin give",
                reference_key=ref_key,
                con=db,
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
            old_balance = await flow_wallet.get_balance(discord_id, con=db)
            actual_take = min(amount, old_balance)  # floor at 0
            if actual_take > 0:
                new_balance = await flow_wallet.debit(
                    discord_id, actual_take, "ADMIN",
                    description=reason or "admin take",
                    con=db,
                )
            else:
                new_balance = old_balance
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
    new_amount = max(0, amount)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            old_balance, new_balance = await flow_wallet.set_balance(
                discord_id, new_amount, "ADMIN",
                description=reason or "admin set",
                con=db,
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
    return await flow_wallet.get_balance(discord_id)


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
        # Prefer configured guild ID; fall back to first available guild
        configured_id = int(os.getenv("DISCORD_GUILD_ID", "0"))
        guild = self.bot.get_guild(configured_id) if configured_id else None
        if not guild:
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
            if not member:
                log.warning("Stipend %s targets departed member %s — skipping",
                            stipend["stipend_id"], stipend["target_id"])
            members = [member] if member else []
            target_name = f"<@{stipend['target_id']}>"

        paid_count = 0
        for member in members:
            if stipend["amount"] > 0:
                old, new_bal = await admin_give(
                    member.id, stipend["amount"], stipend["created_by"],
                    f"Stipend: {stipend['reason']}"
                )
            else:
                old, new_bal = await admin_take(
                    member.id, abs(stipend["amount"]), stipend["created_by"],
                    f"Deduction: {stipend['reason']}"
                )
            # Post to #ledger
            txn_id = await flow_wallet.get_last_txn_id(member.id)
            from ledger_poster import post_transaction
            await post_transaction(
                self.bot, guild.id, member.id,
                "STIPEND", stipend["amount"], new_bal,
                stipend["reason"] or "Stipend payout", txn_id,
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
        # Post to #ledger
        txn_id = await flow_wallet.get_last_txn_id(member.id)
        from ledger_poster import post_transaction
        await post_transaction(
            self.bot, interaction.guild_id, member.id,
            "ADMIN", amount, new, reason or "Commissioner grant", txn_id,
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
        # Post to #ledger
        if taken > 0:
            txn_id = await flow_wallet.get_last_txn_id(member.id)
            from ledger_poster import post_transaction
            await post_transaction(
                self.bot, interaction.guild_id, member.id,
                "ADMIN", -taken, new, reason or "Commissioner deduction", txn_id,
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
        # Post to #ledger
        delta = new - old
        if delta != 0:
            txn_id = await flow_wallet.get_last_txn_id(member.id)
            from ledger_poster import post_transaction
            await post_transaction(
                self.bot, interaction.guild_id, member.id,
                "ADMIN", delta, new, reason or "Balance set", txn_id,
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

    # ── Public Commands ──────────────────────────────────────────────────

    @app_commands.command(
        name="flow",
        description="ATLAS Flow Economy hub — your complete financial command center.",
    )
    async def flow_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id

        view = FlowHubView(interaction.client, uid)
        png = await view.render_current()
        file = discord.File(io.BytesIO(png), filename="flow.png")

        embed = discord.Embed(color=0xD4AF37)
        embed.set_image(url="attachment://flow.png")
        embed.set_footer(text="ATLAS Flow Economy")

        await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=True)

    # ── _impl methods (called by hub buttons and commish_cog) ─────────

    async def _wallet_impl(self, interaction: discord.Interaction):
        """Show wallet as HTML/PNG card."""
        uid = interaction.user.id
        try:
            from flow_cards import build_wallet_card, card_to_file
            png = await build_wallet_card(uid)
            file = card_to_file(png, "wallet.png")
            embed = discord.Embed(color=0xD4AF37)
            embed.set_image(url="attachment://wallet.png")
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error rendering wallet: `{e}`", ephemeral=True)

    async def _leaderboard_impl(self, interaction: discord.Interaction):
        """Show leaderboard as HTML/PNG card."""
        uid = interaction.user.id

        def _resolve_name(discord_id: int) -> str:
            if interaction.guild:
                member = interaction.guild.get_member(discord_id)
                if member:
                    return member.display_name
            return f"User …{str(discord_id)[-4:]}"

        try:
            from flow_cards import build_leaderboard_card, card_to_file
            png = await build_leaderboard_card(uid, name_resolver=_resolve_name)
            file = card_to_file(png, "leaderboard.png")
            embed = discord.Embed(color=0xD4AF37)
            embed.set_image(url="attachment://leaderboard.png")
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error rendering leaderboard: `{e}`", ephemeral=True)

    # ── Economy Health (admin impl) ──────────────────────────────────────

    async def eco_health_impl(self, interaction: discord.Interaction):
        """Show money supply stats."""
        total_supply = await flow_wallet.get_total_supply()
        leaders = await flow_wallet.get_leaderboard(limit=5)

        # Count active users
        async with aiosqlite.connect(DB_PATH) as db:
            # NOTE: "users_table" is the canonical table name used by flow_wallet
            async with db.execute("SELECT COUNT(*) FROM users_table") as cur:
                user_count = (await cur.fetchone())[0]

            # Net flow this week
            async with db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM transactions "
                "WHERE created_at >= datetime('now', '-7 days')"
            ) as cur:
                net_week = (await cur.fetchone())[0]

        embed = discord.Embed(title="Economy Health", color=0xD4AF37)
        embed.add_field(name="Total Supply", value=f"${total_supply:,}", inline=True)
        embed.add_field(name="Active Users", value=str(user_count), inline=True)
        embed.add_field(name="Net Flow (7d)", value=f"${net_week:+,}", inline=True)

        if leaders:
            top_lines = []
            for i, entry in enumerate(leaders):
                top_lines.append(f"{i+1}. <@{entry['discord_id']}> -- ${entry['balance']:,}")
            embed.add_field(name="Top 5 Richest", value="\n".join(top_lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  FLOW HUB VIEW — Stateful tabbed economy dashboard
# ═══════════════════════════════════════════════════════════════════════════

# Tab states
_DASHBOARD    = "dashboard"
_MY_BETS      = "my_bets"
_PORTFOLIO    = "portfolio"
_WALLET       = "wallet"
_LEADERBOARD  = "leaderboard"

# Row 1 tab definitions: (state, label, emoji)
_TABS = [
    (_DASHBOARD,   "Dashboard",   "\U0001f4ca"),
    (_MY_BETS,     "My Bets",     "\U0001f4cb"),
    (_PORTFOLIO,   "Portfolio",   "\U0001f4c8"),
    (_WALLET,      "Wallet",      "\U0001f4b0"),
    (_LEADERBOARD, "Leaderboard", "\U0001f3c6"),
]

# Row 2 contextual buttons per state: list of (label, emoji, callback_name, style)
_CTX_BUTTONS = {
    _DASHBOARD: [
        ("Sportsbook", "\U0001f3c8", "_ctx_sportsbook", discord.ButtonStyle.success),
        ("Casino",     "\U0001f3b0", "_ctx_casino",     discord.ButtonStyle.success),
        ("Markets",    "\U0001f52e", "_ctx_markets",    discord.ButtonStyle.success),
        ("Scratch",    "\U0001f39f", "_ctx_scratch",    discord.ButtonStyle.danger),
    ],
    _MY_BETS: [
        ("Bet History", "\U0001f4c5", "_ctx_bet_history", discord.ButtonStyle.secondary),
        ("Sportsbook",  "\U0001f3c8", "_ctx_sportsbook",  discord.ButtonStyle.success),
        ("Parlay Cart", "\U0001f6d2", "_ctx_parlay_cart",  discord.ButtonStyle.secondary),
    ],
    _PORTFOLIO: [
        ("Browse Markets", "\U0001f50d", "_ctx_browse_markets", discord.ButtonStyle.secondary),
        ("Markets",        "\U0001f52e", "_ctx_markets",        discord.ButtonStyle.success),
    ],
    _WALLET: [],  # Eco Health added dynamically for admins
    _LEADERBOARD: [
        ("Sportsbook", "\U0001f3c8", "_ctx_sportsbook", discord.ButtonStyle.success),
        ("Casino",     "\U0001f3b0", "_ctx_casino",     discord.ButtonStyle.success),
        ("Markets",    "\U0001f52e", "_ctx_markets",    discord.ButtonStyle.success),
    ],
}


class FlowHubView(discord.ui.View):
    """Stateful single-message dashboard with in-place card swapping."""

    def __init__(self, bot, user_id: int, state: str = _DASHBOARD):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.state = state
        self._rebuild_buttons()

    def _rebuild_buttons(self):
        """Clear and rebuild all buttons based on current state."""
        self.clear_items()

        # Row 1: Tab buttons
        for tab_state, label, emoji in _TABS:
            style = (
                discord.ButtonStyle.primary
                if tab_state == self.state
                else discord.ButtonStyle.secondary
            )
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=0)
            btn.callback = self._make_tab_callback(tab_state)
            self.add_item(btn)

        # Row 2: Contextual buttons
        ctx_defs = list(_CTX_BUTTONS.get(self.state, []))

        # Add admin-only Eco Health button on Wallet tab
        if self.state == _WALLET:
            ctx_defs.append(
                ("Eco Health", "\U0001f4ca", "_ctx_eco_health", discord.ButtonStyle.secondary)
            )

        for label, emoji, cb_name, style in ctx_defs:
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=1)
            btn.callback = getattr(self, cb_name)
            self.add_item(btn)

    def _make_tab_callback(self, target_state: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message(
                    "This isn't your dashboard. Run `/flow` to open yours.",
                    ephemeral=True,
                )
            if target_state == self.state:
                # Already on this tab — ignore
                try:
                    await interaction.response.defer()
                except discord.NotFound:
                    pass
                return
            await self._swap_to(interaction, target_state)
        return callback

    def _resolve_name(self, discord_id: int) -> str:
        """Resolve a Discord ID to a display name via the bot's user cache."""
        user = self.bot.get_user(discord_id)
        if user:
            return user.display_name
        return f"User …{str(discord_id)[-4:]}"

    async def render_current(self) -> bytes:
        """Render the card for the current state."""
        from flow_cards import (
            build_flow_card, build_my_bets_card,
            build_portfolio_card, build_wallet_card,
            build_leaderboard_card,
        )
        renderers = {
            _DASHBOARD:   lambda: build_flow_card(self.user_id),
            _MY_BETS:     lambda: build_my_bets_card(self.user_id),
            _PORTFOLIO:   lambda: build_portfolio_card(self.user_id),
            _WALLET:      lambda: build_wallet_card(self.user_id),
            _LEADERBOARD: lambda: build_leaderboard_card(self.user_id, name_resolver=self._resolve_name),
        }
        return await renderers[self.state]()

    async def _swap_to(self, interaction: discord.Interaction, new_state: str):
        """Swap the card and update buttons for the new state."""
        self.state = new_state
        self._rebuild_buttons()

        try:
            png = await self.render_current()
            file = discord.File(io.BytesIO(png), filename="flow.png")
            embed = discord.Embed(color=0xD4AF37)
            embed.set_image(url="attachment://flow.png")
            embed.set_footer(text="ATLAS Flow Economy")
            await interaction.response.edit_message(
                attachments=[file], embed=embed, view=self,
            )
        except discord.NotFound:
            return

    # ── Row 2: Contextual action callbacks ────────────────────────────

    async def _ctx_sportsbook(self, interaction: discord.Interaction):
        try:
            cog = self.bot.get_cog("SportsbookCog")
            if cog:
                await cog.sportsbook.callback(cog, interaction)
            else:
                await interaction.response.send_message("Sportsbook module not loaded.", ephemeral=True)
        except discord.NotFound:
            return

    async def _ctx_casino(self, interaction: discord.Interaction):
        try:
            cog = self.bot.get_cog("CasinoCog")
            if cog:
                await cog.casino_hub.callback(cog, interaction)
            else:
                await interaction.response.send_message("Casino module not loaded.", ephemeral=True)
        except discord.NotFound:
            return

    async def _ctx_markets(self, interaction: discord.Interaction):
        try:
            cog = self.bot.get_cog("Polymarket")
            if cog:
                await cog.markets_cmd.callback(cog, interaction)
            else:
                await interaction.response.send_message("Markets module not loaded.", ephemeral=True)
        except discord.NotFound:
            return

    async def _ctx_scratch(self, interaction: discord.Interaction):
        try:
            from casino.games.slots import daily_scratch
            await daily_scratch(interaction)
        except discord.NotFound:
            return
        except Exception:
            try:
                await interaction.response.send_message("Casino module not loaded.", ephemeral=True)
            except discord.NotFound:
                return

    async def _ctx_bet_history(self, interaction: discord.Interaction):
        try:
            cog = self.bot.get_cog("SportsbookCog")
            if cog:
                await cog._bethistory_impl(interaction)
            else:
                await interaction.response.send_message("Sportsbook module not loaded.", ephemeral=True)
        except discord.NotFound:
            return

    async def _ctx_parlay_cart(self, interaction: discord.Interaction):
        try:
            cog = self.bot.get_cog("SportsbookCog")
            if cog and hasattr(cog, "_parlay_cart_impl"):
                await cog._parlay_cart_impl(interaction)
            else:
                await interaction.response.send_message(
                    "Open the Sportsbook to manage your parlay cart.", ephemeral=True
                )
        except discord.NotFound:
            return

    async def _ctx_browse_markets(self, interaction: discord.Interaction):
        try:
            cog = self.bot.get_cog("Polymarket")
            if cog:
                await cog.markets_cmd.callback(cog, interaction)
            else:
                await interaction.response.send_message("Markets module not loaded.", ephemeral=True)
        except discord.NotFound:
            return

    async def _ctx_eco_health(self, interaction: discord.Interaction):
        try:
            from permissions import is_commissioner
            if not await is_commissioner(interaction):
                return await interaction.response.send_message(
                    "Commissioner-only command.", ephemeral=True
                )
            await interaction.response.defer(thinking=True, ephemeral=True)
            cog = self.bot.get_cog("EconomyCog")
            if cog:
                await cog.eco_health_impl(interaction)
            else:
                await interaction.followup.send("Economy module not loaded.", ephemeral=True)
        except discord.NotFound:
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
