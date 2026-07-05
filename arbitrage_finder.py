"""
Arbitrage finder module for Sportsbook Auto Betting Agent.

Mengevaluasi value bet berdasarkan odds RIIL Stake dibanding peluang "fair"
hasil konsensus pasar (dari `predictor.get_market_consensus_prediction`).
Tidak ada data buatan/acak di modul ini.
"""

from typing import Dict, List, Optional

import bot_config as config


def _implicit_prob(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def scan_opportunities(
    best_odds: Dict[str, float],
    fair_probs: Dict[str, float],
    best_bookmaker: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """
    Cari value bet: outcome di mana peluang fair (konsensus pasar riil) lebih
    tinggi daripada peluang implisit odds terbaik yang tersedia di pasar.

    Stake tidak selalu ada di OddsAPI — bot membandingkan konsensus vs odds
    terbaik dari bookmaker manapun. User kemudian cek manual di Stake apakah
    odds yang ditawarkan setara atau lebih baik.

    Args:
        best_odds: Odds terbaik per outcome dari semua bookmaker, contoh
                   {"Home": 1.90, "Away": 2.10}.
        fair_probs: Peluang fair (konsensus multi-bookmaker riil, jumlah = 1.0).
        best_bookmaker: Opsional — nama bookmaker sumber tiap outcome.

    Returns:
        List of opportunity dicts:
            - outcome, platform (bookmaker sumber), best_odds, ai_prob,
              implied_prob, edge
    """
    opportunities: List[dict] = []
    bm = best_bookmaker or {}

    for outcome, odds in best_odds.items():
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
                    "platform": bm.get(outcome, "market"),
                    "best_odds": odds,
                    "ai_prob": round(fair_prob, 4),
                    "implied_prob": round(implied, 4),
                    "edge": round(edge, 4),
                }
            )

    return opportunities
