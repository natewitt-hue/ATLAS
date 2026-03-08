"""
blackjack.py — TSL Casino Blackjack
─────────────────────────────────────────────────────────────────────────────
Standard 6-deck blackjack with TSL Royal card visuals.

Rules:
  • 6-deck shoe, reshuffled at < 30% remaining
  • Dealer hits soft 17
  • Blackjack pays 3:2
  • Double Down: double wager, exactly one more card
  • Split: pairs only, one split allowed, no re-split
  • 5-minute session timeout → auto-stand
  • One active hand per user (enforced via active_sessions registry)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from casino.casino_db import (
    deduct_wager, process_wager, refund_wager, get_balance,
    is_casino_open, get_channel_id, get_max_bet,
)
from casino.renderer.card_renderer import render_blackjack_table, SUITS, VALUES

if TYPE_CHECKING:
    pass

# ── Session registry: discord_id → BlackjackSession ──────────────────────────
active_sessions: dict[int, "BlackjackSession"] = {}

GAME_TYPE    = "blackjack"
TIMEOUT_SECS = 300   # 5 minutes


# ═════════════════════════════════════════════════════════════════════════════
#  DECK & HAND LOGIC
# ═════════════════════════════════════════════════════════════════════════════

def _build_shoe(decks: int = 6) -> list[tuple[str, str]]:
    shoe = [(v, s) for s in SUITS for v in VALUES] * decks
    random.shuffle(shoe)
    return shoe


def _hand_value(hand: list[tuple[str, str]]) -> int:
    """Return best blackjack value for a hand (aces counted optimally)."""
    total = 0
    aces  = 0
    for value, _ in hand:
        if value in ("J", "Q", "K"):
            total += 10
        elif value == "A":
            aces  += 1
            total += 11
        else:
            total += int(value)
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


def _is_blackjack(hand: list[tuple[str, str]]) -> bool:
    return len(hand) == 2 and _hand_value(hand) == 21


def _is_pair(hand: list[tuple[str, str]]) -> bool:
    return len(hand) == 2 and hand[0][0] == hand[1][0]


def _display_score(hand: list[tuple[str, str]], hide: bool = False) -> str | int:
    if hide:
        return "?"
    return _hand_value(hand)


# ═════════════════════════════════════════════════════════════════════════════
#  SESSION STATE
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class BlackjackSession:
    discord_id:    int
    wager:         int
    channel_id:    int
    message_id:    int = 0

    shoe:          list = field(default_factory=lambda: _build_shoe())
    dealer_hand:   list = field(default_factory=list)
    player_hand:   list = field(default_factory=list)
    split_hand:    list = field(default_factory=list)
    playing_split: bool = False

    doubled:       bool = False
    split_active:  bool = False
    done:          bool = False

    created_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def deal(self) -> None:
        """Initial deal: 2 cards each."""
        for _ in range(2):
            self.player_hand.append(self.shoe.pop())
            self.dealer_hand.append(self.shoe.pop())
        if len(self.shoe) < len(_build_shoe()) * 0.3:
            self.shoe = _build_shoe()

    @property
    def active_hand(self) -> list:
        return self.split_hand if self.playing_split else self.player_hand

    def hit(self) -> tuple[str, str]:
        card = self.shoe.pop()
        self.active_hand.append(card)
        return card

    def dealer_play(self) -> None:
        """Dealer hits on soft 17."""
        while True:
            total = 0
            aces  = 0
            for v, _ in self.dealer_hand:
                if v in ("J","Q","K"): total += 10
                elif v == "A":         aces += 1; total += 11
                else:                  total += int(v)
            while total > 21 and aces:
                total -= 10; aces -= 1
            is_soft = aces > 0 and total == 17
            if total < 17 or is_soft:
                self.dealer_hand.append(self.shoe.pop())
            else:
                break

    def resolve(self, wager_override: int | None = None) -> tuple[str, int, float]:
        """
        Resolve the current active hand against the dealer.
        Returns (outcome, payout, multiplier).
        """
        wager   = wager_override or self.wager
        p_score = _hand_value(self.active_hand)
        d_score = _hand_value(self.dealer_hand)
        p_bj    = _is_blackjack(self.active_hand) and not self.split_active
        d_bj    = _is_blackjack(self.dealer_hand)

        if p_bj and d_bj:
            return "push", wager, 1.0
        if p_bj:
            payout = wager + int(wager * 1.5)   # 3:2
            return "win", payout, 2.5
        if d_bj:
            return "loss", 0, 0.0
        if p_score > 21:
            return "loss", 0, 0.0
        if d_score > 21:
            return "win", wager * 2, 2.0
        if p_score > d_score:
            return "win", wager * 2, 2.0
        if p_score < d_score:
            return "loss", 0, 0.0
        return "push", wager, 1.0


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD VIEW
# ═════════════════════════════════════════════════════════════════════════════

class BlackjackView(discord.ui.View):
    def __init__(self, session: BlackjackSession):
        super().__init__(timeout=TIMEOUT_SECS)
        self.session = session
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.clear_items()
        s = self.session
        can_double = (
            len(s.active_hand) == 2
            and not s.doubled
        )
        can_split = (
            _is_pair(s.player_hand)
            and not s.split_active
            and not s.playing_split
        )

        self.add_item(HitButton())
        self.add_item(StandButton())
        if can_double:
            self.add_item(DoubleButton())
        if can_split:
            self.add_item(SplitButton())

    async def on_timeout(self) -> None:
        """Auto-stand on timeout — resolve the wager so money isn't lost."""
        s = self.session
        if not s.done and s.discord_id in active_sessions:
            s.done = True
            s.dealer_play()

            # ── Resolve the wager (prevents money vanishing into the void) ──
            try:
                if s.split_active:
                    # Resolve both hands for split games
                    outcome1, payout1, mult1 = s.resolve()
                    s.playing_split = True
                    outcome2, payout2, mult2 = s.resolve()
                    total_payout = payout1 + payout2
                    total_wager  = s.wager * 2
                    wins   = sum(1 for o in [outcome1, outcome2] if o == "win")
                    losses = sum(1 for o in [outcome1, outcome2] if o == "loss")
                    if wins > losses:
                        log_outcome = "win"
                    elif losses > wins:
                        log_outcome = "loss"
                    else:
                        log_outcome = "push"
                    avg_mult = total_payout / total_wager if total_wager else 1.0
                else:
                    outcome, payout, mult = s.resolve()
                    total_payout = payout
                    total_wager  = s.wager
                    log_outcome  = outcome
                    avg_mult     = mult

                await process_wager(
                    discord_id = s.discord_id,
                    wager      = total_wager,
                    game_type  = GAME_TYPE,
                    outcome    = log_outcome,
                    payout     = total_payout,
                    multiplier = round(avg_mult, 2),
                    channel_id = s.channel_id,
                )
            except Exception as e:
                print(f"[blackjack] Timeout wager resolution error for {s.discord_id}: {e}")

            active_sessions.pop(s.discord_id, None)


class HitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")

    async def callback(self, interaction: discord.Interaction):
        view    = self.view
        session = view.session
        if interaction.user.id != session.discord_id:
            return await interaction.response.send_message(
                "This isn't your hand!", ephemeral=True
            )

        session.hit()
        score = _hand_value(session.active_hand)

        if score >= 21:
            await _finish_hand(interaction, session, view)
        else:
            view._update_buttons()
            await _update_table_message(interaction, session, view, hide_dealer=True)


class StandButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋")

    async def callback(self, interaction: discord.Interaction):
        view    = self.view
        session = view.session
        if interaction.user.id != session.discord_id:
            return await interaction.response.send_message(
                "This isn't your hand!", ephemeral=True
            )

        if session.split_active and not session.playing_split:
            session.playing_split = True
            view._update_buttons()
            await _update_table_message(interaction, session, view, hide_dealer=True)
        else:
            await _finish_hand(interaction, session, view)


class DoubleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Double Down", style=discord.ButtonStyle.success, emoji="💰")

    async def callback(self, interaction: discord.Interaction):
        view    = self.view
        session = view.session
        if interaction.user.id != session.discord_id:
            return await interaction.response.send_message(
                "This isn't your hand!", ephemeral=True
            )

        try:
            await deduct_wager(session.discord_id, session.wager)
        except Exception:
            return await interaction.response.send_message(
                "❌ Insufficient funds to double down.", ephemeral=True
            )

        session.wager   *= 2
        session.doubled  = True
        session.hit()
        await _finish_hand(interaction, session, view)


class SplitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Split", style=discord.ButtonStyle.danger, emoji="✂️")

    async def callback(self, interaction: discord.Interaction):
        view    = self.view
        session = view.session
        if interaction.user.id != session.discord_id:
            return await interaction.response.send_message(
                "This isn't your hand!", ephemeral=True
            )

        try:
            await deduct_wager(session.discord_id, session.wager)
        except Exception:
            return await interaction.response.send_message(
                "❌ Insufficient funds to split.", ephemeral=True
            )

        split_card           = session.player_hand.pop()
        session.split_hand   = [split_card, session.shoe.pop()]
        session.player_hand.append(session.shoe.pop())
        session.split_active = True

        view._update_buttons()
        await _update_table_message(interaction, session, view, hide_dealer=True)


# ═════════════════════════════════════════════════════════════════════════════
#  TABLE UPDATE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def _update_table_message(
    interaction: discord.Interaction,
    session:     BlackjackSession,
    view:        BlackjackView,
    hide_dealer: bool = True,
    status:      str  = "",
) -> None:
    bal = await get_balance(session.discord_id)
    buf = render_blackjack_table(
        dealer_hand  = session.dealer_hand,
        player_hand  = session.active_hand,
        dealer_score = _display_score(session.dealer_hand, hide=hide_dealer),
        player_score = _hand_value(session.active_hand),
        hide_dealer  = hide_dealer,
        status       = status,
        wager        = session.wager,
        balance      = bal,
    )
    file  = discord.File(buf, filename="blackjack.png")
    embed = discord.Embed(
        title       = "🃏 TSL Casino — Blackjack",
        description = _hand_description(session),
        color       = discord.Color.from_rgb(212, 175, 55),
    )
    embed.set_image(url="attachment://blackjack.png")
    embed.set_footer(text=f"Wager: {session.wager:,} TSL Bucks  |  Balance: {bal:,}")
    await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


def _hand_description(session: BlackjackSession) -> str:
    p  = _hand_value(session.player_hand)
    ph = f"**Your hand:** {_cards_str(session.player_hand)} = **{p}**"
    if session.split_active:
        s  = _hand_value(session.split_hand)
        ph += f"\n**Split hand:** {_cards_str(session.split_hand)} = **{s}**"
    ph += f"\n**Dealer:** {_cards_str(session.dealer_hand[:1])} + 🂠"
    return ph


def _cards_str(hand: list[tuple[str, str]]) -> str:
    return " ".join(f"`{v}{s}`" for v, s in hand)


async def _finish_hand(
    interaction: discord.Interaction,
    session:     BlackjackSession,
    view:        BlackjackView,
) -> None:
    """Dealer plays, resolve hand(s), process payouts, finalize."""
    session.done = True
    view.clear_items()

    outcomes = []

    if session.split_active:
        outcome1, payout1, mult1 = session.resolve()
        outcomes.append((outcome1, payout1, mult1, session.wager))
        session.playing_split = True
        session.dealer_play()
        outcome2, payout2, mult2 = session.resolve()
        outcomes.append((outcome2, payout2, mult2, session.wager))
    else:
        session.dealer_play()
        outcome, payout, mult = session.resolve()
        outcomes.append((outcome, payout, mult, session.wager))

    total_payout = sum(o[1] for o in outcomes)
    total_wager  = sum(o[3] for o in outcomes)
    win_count    = sum(1 for o in outcomes if o[0] == "win")
    loss_count   = sum(1 for o in outcomes if o[0] == "loss")

    if win_count > loss_count:
        log_outcome = "win"
    elif loss_count > win_count:
        log_outcome = "loss"
    else:
        log_outcome = "push"

    avg_mult = total_payout / total_wager if total_wager else 1.0

    result = await process_wager(
        discord_id = session.discord_id,
        wager      = total_wager,
        game_type  = GAME_TYPE,
        outcome    = log_outcome,
        payout     = total_payout,
        multiplier = round(avg_mult, 2),
        channel_id = session.channel_id,
    )

    if len(outcomes) == 1:
        o, p, m, w = outcomes[0]
        if o == "win" and _is_blackjack(session.player_hand):
            status_str = "Blackjack! 🎉"
        elif o == "win" and _hand_value(session.dealer_hand) > 21:
            status_str = "Dealer Busts! 🎉"
        elif o == "win":
            status_str = "Win! ✅"
        elif o == "loss" and _hand_value(session.active_hand) > 21:
            status_str = "Bust! ❌"
        else:
            status_str = "Loss ❌" if o == "loss" else "Push 🔁"
    else:
        results = [
            "W" if o[0] == "win" else ("P" if o[0] == "push" else "L")
            for o in outcomes
        ]
        status_str = f"Split: {' / '.join(results)}"

    profit     = total_payout - total_wager
    profit_str = f"+{profit:,}" if profit >= 0 else f"{profit:,}"

    active_sessions.pop(session.discord_id, None)

    bal = result["new_balance"]
    buf = render_blackjack_table(
        dealer_hand  = session.dealer_hand,
        player_hand  = session.active_hand,
        dealer_score = _hand_value(session.dealer_hand),
        player_score = _hand_value(session.active_hand),
        hide_dealer  = False,
        status       = status_str,
        wager        = total_wager,
        balance      = bal,
    )
    file  = discord.File(buf, filename="blackjack.png")

    color = (discord.Color.green() if log_outcome == "win"
             else discord.Color.red() if log_outcome == "loss"
             else discord.Color.greyple())

    embed = discord.Embed(title="🃏 TSL Casino — Blackjack", color=color)
    p_val = _hand_value(session.active_hand)
    d_val = _hand_value(session.dealer_hand)
    embed.add_field(
        name  = "Your Hand",
        value = f"{_cards_str(session.active_hand)} = **{p_val}**",
        inline = True
    )
    embed.add_field(
        name  = "Dealer Hand",
        value = f"{_cards_str(session.dealer_hand)} = **{d_val}**",
        inline = True
    )
    embed.add_field(
        name  = "Result",
        value = f"**{status_str}**\n{profit_str} Bucks",
        inline = True
    )
    embed.set_image(url="attachment://blackjack.png")
    embed.set_footer(text=f"New Balance: {bal:,} TSL Bucks")

    await interaction.response.edit_message(embed=embed, attachments=[file], view=view)


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT — called from casino.py
# ═════════════════════════════════════════════════════════════════════════════

async def start_blackjack(interaction: discord.Interaction, wager: int) -> None:
    """
    Begin a new blackjack hand for the interacting user.
    Called from the casino hub or directly in #casino-blackjack.
    """
    uid = interaction.user.id

    if uid in active_sessions:
        return await interaction.response.send_message(
            "❌ You already have an active blackjack hand! Finish it first.",
            ephemeral=True
        )

    if not await is_casino_open("blackjack"):
        return await interaction.response.send_message(
            "🔴 Blackjack is currently closed.", ephemeral=True
        )

    bj_channel_id = await get_channel_id("blackjack")
    if bj_channel_id and interaction.channel_id != bj_channel_id:
        return await interaction.response.send_message(
            f"🃏 Blackjack is played in <#{bj_channel_id}>!", ephemeral=True
        )

    max_bet = await get_max_bet()
    if wager < 1 or wager > max_bet:
        return await interaction.response.send_message(
            f"❌ Wager must be between **1** and **{max_bet:,} TSL Bucks**.",
            ephemeral=True
        )

    try:
        await deduct_wager(uid, wager)
    except Exception as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    session = BlackjackSession(
        discord_id = uid,
        wager      = wager,
        channel_id = interaction.channel_id,
    )
    session.deal()
    active_sessions[uid] = session

    # ── Immediate blackjack: send the message first, then resolve ─────────
    # We must send_message first so there's a message to edit_message against.
    if _is_blackjack(session.player_hand) or _is_blackjack(session.dealer_hand):
        session.dealer_play()
        outcome, payout, mult = session.resolve()
        active_sessions.pop(uid, None)
        session.done = True

        result = await process_wager(
            discord_id = uid,
            wager      = wager,
            game_type  = GAME_TYPE,
            outcome    = outcome,
            payout     = payout,
            multiplier = round(mult, 2),
            channel_id = interaction.channel_id,
        )
        bal = result["new_balance"]

        if outcome == "win" and _is_blackjack(session.player_hand):
            status_str = "Blackjack! 🎉"
        elif outcome == "push":
            status_str = "Push — Both Blackjack 🔁"
        else:
            status_str = "Dealer Blackjack ❌"

        profit     = payout - wager
        profit_str = f"+{profit:,}" if profit >= 0 else f"{profit:,}"

        buf = render_blackjack_table(
            dealer_hand  = session.dealer_hand,
            player_hand  = session.player_hand,
            dealer_score = _hand_value(session.dealer_hand),
            player_score = _hand_value(session.player_hand),
            hide_dealer  = False,
            status       = status_str,
            wager        = wager,
            balance      = bal,
        )
        file  = discord.File(buf, filename="blackjack.png")
        color = (discord.Color.green() if outcome == "win"
                 else discord.Color.red() if outcome == "loss"
                 else discord.Color.greyple())

        embed = discord.Embed(title="🃏 TSL Casino — Blackjack", color=color)
        embed.add_field(
            name="Your Hand",
            value=f"{_cards_str(session.player_hand)} = **{_hand_value(session.player_hand)}**",
            inline=True
        )
        embed.add_field(
            name="Dealer Hand",
            value=f"{_cards_str(session.dealer_hand)} = **{_hand_value(session.dealer_hand)}**",
            inline=True
        )
        embed.add_field(
            name="Result",
            value=f"**{status_str}**\n{profit_str} Bucks",
            inline=True
        )
        embed.set_image(url="attachment://blackjack.png")
        embed.set_footer(text=f"New Balance: {bal:,} TSL Bucks")
        return await interaction.response.send_message(embed=embed, file=file)

    # ── Normal deal: render and send active table ─────────────────────────
    bal  = await get_balance(uid)
    buf  = render_blackjack_table(
        dealer_hand  = session.dealer_hand,
        player_hand  = session.player_hand,
        dealer_score = "?",
        player_score = _hand_value(session.player_hand),
        hide_dealer  = True,
        wager        = wager,
        balance      = bal,
    )
    file  = discord.File(buf, filename="blackjack.png")
    embed = discord.Embed(
        title       = f"🃏 TSL Casino — Blackjack  |  {interaction.user.display_name}",
        description = _hand_description(session),
        color       = discord.Color.from_rgb(212, 175, 55),
    )
    embed.set_image(url="attachment://blackjack.png")
    embed.set_footer(text=f"Wager: {wager:,} TSL Bucks  |  Balance: {bal:,}  |  5-min timeout")

    view = BlackjackView(session)
    await interaction.response.send_message(embed=embed, file=file, view=view)


