"""
Bet sizer module for Sportsbook Auto Betting Agent.
Calculates IDR stake using Fractional Kelly Criterion with hard limits and drawdown protection.
"""

from sportsbook_bot import config


def calculate_idr_stake(
    odds: float,
    prob: float,
    current_bankroll: float,
    current_daily_loss: float,
) -> float:
    """
    Calculate the IDR bet stake using Fractional Kelly Criterion.

    Formula:
        b = odds - 1
        f = (b * p - q) / b   where q = 1 - p
        nominal = f * KELLY_MULTIPLIER * current_bankroll

    Hard limits applied:
        - If nominal < MIN_BET_IDR -> return 0.0 (bet cancelled)
        - If nominal > MAX_BET_IDR -> clip to MAX_BET_IDR
        - If daily loss >= MAX_DAILY_DRAWDOWN * bankroll -> return 0.0

    Args:
        odds: Decimal odds for the target outcome.
        prob: AI-predicted probability for the target outcome (0.0 - 1.0).
        current_bankroll: Current account balance in IDR.
        current_daily_loss: Cumulative loss for today in IDR.

    Returns:
        Recommended stake in IDR, or 0.0 if bet should not be placed.
    """
    drawdown_limit = config.MAX_DAILY_DRAWDOWN * current_bankroll
    if current_daily_loss >= drawdown_limit:
        return 0.0

    if odds <= 1.0 or prob <= 0.0 or prob >= 1.0:
        return 0.0

    b = odds - 1.0
    q = 1.0 - prob
    kelly_fraction = (b * prob - q) / b

    if kelly_fraction <= 0.0:
        return 0.0

    nominal = kelly_fraction * config.KELLY_MULTIPLIER * current_bankroll

    if nominal < config.MIN_BET_IDR:
        return 0.0

    return min(nominal, config.MAX_BET_IDR)
