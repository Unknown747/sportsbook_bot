"""
Arbitrage finder module for Sportsbook Auto Betting Agent.
Scans multi-bookmaker odds for value bets and arbitrage opportunities.
"""

from typing import Dict, List

from sportsbook_bot import config


def _implicit_prob(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def scan_opportunities(odds_data: dict, ai_probs: Dict[str, float]) -> List[dict]:
    """
    Scan for arbitrage and value bet opportunities across multiple bookmakers.

    Args:
        odds_data: Dict of bookmaker -> outcome -> decimal odds.
                   Example:
                   {
                       "stake": {"Home": 2.10, "Draw": 3.40, "Away": 3.20},
                       "bookmaker_b": {"Home": 2.05, "Draw": 3.50, "Away": 3.30},
                   }
        ai_probs: Normalized 3-way probability dict from predictor.

    Returns:
        List of opportunity dicts with keys:
            - type: "value_bet" or "arbitrage"
            - outcome: "Home" | "Draw" | "Away"
            - platform: bookmaker name with best odds
            - best_odds: float
            - ai_prob: float
            - implied_prob: float
            - edge: float (positive = value)
    """
    opportunities: List[dict] = []

    best_odds: Dict[str, tuple] = {}
    for bookmaker, outcomes in odds_data.items():
        for outcome, odds in outcomes.items():
            if outcome not in best_odds or odds > best_odds[outcome][1]:
                best_odds[outcome] = (bookmaker, odds)

    for outcome, (platform, odds) in best_odds.items():
        implied = _implicit_prob(odds)
        ai_prob = ai_probs.get(outcome, 0.0)
        edge = ai_prob - implied

        if edge >= config.MIN_VALUE_EDGE:
            opportunities.append(
                {
                    "type": "value_bet",
                    "outcome": outcome,
                    "platform": platform,
                    "best_odds": odds,
                    "ai_prob": round(ai_prob, 4),
                    "implied_prob": round(implied, 4),
                    "edge": round(edge, 4),
                }
            )

    total_implied = sum(_implicit_prob(v[1]) for v in best_odds.values())
    if total_implied < 1.0 and len(best_odds) == 3:
        arb_profit = round((1.0 - total_implied) * 100, 4)
        for outcome, (platform, odds) in best_odds.items():
            opportunities.append(
                {
                    "type": "arbitrage",
                    "outcome": outcome,
                    "platform": platform,
                    "best_odds": odds,
                    "ai_prob": round(ai_probs.get(outcome, 0.0), 4),
                    "implied_prob": round(_implicit_prob(odds), 4),
                    "edge": round(arb_profit, 4),
                }
            )

    return opportunities
