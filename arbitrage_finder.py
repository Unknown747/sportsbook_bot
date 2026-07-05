"""
Arbitrage finder module for Sportsbook Auto Betting Agent.

Mengevaluasi value bet berdasarkan odds RIIL Stake dibanding peluang "fair"
hasil konsensus pasar (dari `predictor.get_market_consensus_prediction`).
Tidak ada data buatan/acak di modul ini.
"""

from typing import Dict, List

import bot_config as config


def _implicit_prob(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def scan_opportunities(stake_odds: Dict[str, float], fair_probs: Dict[str, float]) -> List[dict]:
    """
    Cari value bet: outcome di mana peluang fair (konsensus pasar riil) lebih
    tinggi daripada peluang implisit odds Stake untuk outcome yang sama.

    Args:
        stake_odds: Odds riil Stake per outcome, contoh {"Home": 1.85, "Away": 2.05}.
        fair_probs: Peluang fair (konsensus multi-bookmaker riil, jumlah = 1.0).

    Returns:
        List of opportunity dicts (type selalu "value_bet", platform selalu
        "stake" karena hanya itu yang bisa dieksekusi user):
            - outcome, platform, best_odds, ai_prob, implied_prob, edge
    """
    opportunities: List[dict] = []

    for outcome, odds in stake_odds.items():
        if odds <= 1.0:
            continue

        fair_prob = fair_probs.get(outcome)
        if fair_prob is None or fair_prob <= 0.0:
            continue

        implied = _implicit_prob(odds)
        edge = fair_prob - implied

        if edge >= config.MIN_VALUE_EDGE:
            opportunities.append(
                {
                    "type": "value_bet",
                    "outcome": outcome,
                    "platform": "stake",
                    "best_odds": odds,
                    "ai_prob": round(fair_prob, 4),
                    "implied_prob": round(implied, 4),
                    "edge": round(edge, 4),
                }
            )

    return opportunities
