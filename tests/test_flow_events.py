import asyncio
import pytest
from flow_events import FlowEventBus, GameResultEvent

@pytest.fixture
def bus():
    return FlowEventBus()

@pytest.mark.asyncio
async def test_subscribe_and_emit(bus):
    received = []
    async def handler(event):
        received.append(event)
    bus.subscribe("game_result", handler)
    event = GameResultEvent(
        discord_id=123, guild_id=456, game_type="blackjack",
        wager=100, outcome="win", payout=200, multiplier=2.0,
        new_balance=1200, txn_id=1
    )
    await bus.emit("game_result", event)
    assert len(received) == 1
    assert received[0].game_type == "blackjack"

@pytest.mark.asyncio
async def test_multiple_subscribers(bus):
    count = {"a": 0, "b": 0}
    async def handler_a(event): count["a"] += 1
    async def handler_b(event): count["b"] += 1
    bus.subscribe("game_result", handler_a)
    bus.subscribe("game_result", handler_b)
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="slots",
        wager=50, outcome="loss", payout=0, multiplier=0.0,
        new_balance=950, txn_id=2
    )
    await bus.emit("game_result", event)
    assert count["a"] == 1
    assert count["b"] == 1

@pytest.mark.asyncio
async def test_unrelated_event_not_received(bus):
    received = []
    async def handler(event): received.append(event)
    bus.subscribe("other_event", handler)
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="slots",
        wager=50, outcome="loss", payout=0, multiplier=0.0,
        new_balance=950, txn_id=3
    )
    await bus.emit("game_result", event)
    assert len(received) == 0

def test_game_result_event_net_profit():
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="blackjack",
        wager=100, outcome="win", payout=200, multiplier=2.0,
        new_balance=1200, txn_id=4
    )
    assert event.net_profit == 100  # payout - wager

def test_game_result_event_net_profit_loss():
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="blackjack",
        wager=100, outcome="loss", payout=0, multiplier=0.0,
        new_balance=900, txn_id=5
    )
    assert event.net_profit == -100
