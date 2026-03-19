"""Shared sportsbook utility functions — odds formatting and payout math."""


def american_to_str(odds: int) -> str:
    """Format American odds as string (+150 / -110)."""
    return f"+{odds}" if odds > 0 else str(odds)


def payout_calc(wager: int, odds: int) -> int:
    """Return total payout (wager + profit) from American odds."""
    odds = int(odds)
    if odds == 0:
        return wager
    if odds > 0:
        return int(wager + wager * (odds / 100))
    return int(wager + wager * (100 / abs(odds)))


def profit_calc(wager: int, odds: int) -> int:
    """Return profit only from American odds."""
    return payout_calc(wager, odds) - wager
