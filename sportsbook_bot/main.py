"""
Main orchestrator for Sportsbook Auto Betting Agent.
Runs a continuous 10-minute loop, coordinates all modules, and handles exceptions.
"""

import logging
import time
from datetime import date
from typing import Dict

from sportsbook_bot import config
from sportsbook_bot.arbitrage_finder import scan_opportunities
from sportsbook_bot.bet_sizer import calculate_idr_stake
from sportsbook_bot.executor import StakeExecutor
from sportsbook_bot.predictor import get_ensemble_prediction, get_multi_agent_consensus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sportsbook_bot.main")


def _get_sample_matches() -> list:
    """
    Return a list of sample match data for demonstration/simulation.
    In production this would be replaced by a live data feed from Stake or a data provider.
    """
    return [
        {
            "match_id": "match_001",
            "home_team": "Team Alpha",
            "away_team": "Team Beta",
            "home_strength": 60.0,
            "away_strength": 40.0,
            "league_draw_rate": 0.28,
            "sentiment_home_bias": 0.02,
            "odds_data": {
                "stake": {"Home": 1.85, "Draw": 3.50, "Away": 4.20},
                "bookmaker_b": {"Home": 1.90, "Draw": 3.40, "Away": 4.10},
            },
            "active_odds_ids": {
                "Home": "odds_001_home",
                "Draw": "odds_001_draw",
                "Away": "odds_001_away",
            },
        },
        {
            "match_id": "match_002",
            "home_team": "Team Gamma",
            "away_team": "Team Delta",
            "home_strength": 50.0,
            "away_strength": 55.0,
            "league_draw_rate": 0.30,
            "sentiment_home_bias": -0.01,
            "odds_data": {
                "stake": {"Home": 2.80, "Draw": 3.10, "Away": 2.50},
                "bookmaker_b": {"Home": 2.75, "Draw": 3.20, "Away": 2.55},
            },
            "active_odds_ids": {
                "Home": "odds_002_home",
                "Draw": "odds_002_draw",
                "Away": "odds_002_away",
            },
        },
    ]


def run_betting_cycle(
    executor: StakeExecutor,
    current_bankroll: float,
    daily_loss: float,
    last_reset_date: date,
) -> tuple:
    """
    Execute one full betting analysis and placement cycle.

    Args:
        executor: StakeExecutor instance.
        current_bankroll: Current balance in IDR.
        daily_loss: Cumulative loss for today in IDR.
        last_reset_date: The date daily_loss was last reset.

    Returns:
        Tuple of (updated_bankroll, updated_daily_loss, updated_last_reset_date).
    """
    today = date.today()
    if today != last_reset_date:
        logger.info("New day detected — resetting daily loss counter.")
        daily_loss = 0.0
        last_reset_date = today

    matches = _get_sample_matches()
    logger.info("Processing %d matches this cycle.", len(matches))

    for match in matches:
        match_id = match["match_id"]
        try:
            ensemble_probs: Dict[str, float] = get_ensemble_prediction(match)
            consensus_probs: Dict[str, float] = get_multi_agent_consensus(match)

            blended: Dict[str, float] = {
                outcome: (ensemble_probs[outcome] + consensus_probs[outcome]) / 2.0
                for outcome in ("Home", "Draw", "Away")
            }
            total = sum(blended.values())
            ai_probs: Dict[str, float] = {k: v / total for k, v in blended.items()}

            logger.info(
                "[%s] AI Probs => Home:%.3f Draw:%.3f Away:%.3f",
                match_id,
                ai_probs["Home"],
                ai_probs["Draw"],
                ai_probs["Away"],
            )

            opportunities = scan_opportunities(match["odds_data"], ai_probs)

            if not opportunities:
                logger.info("[%s] No value bets or arbitrage found this cycle.", match_id)
                continue

            for opp in opportunities:
                outcome = opp["outcome"]
                odds = opp["best_odds"]
                platform = opp["platform"]
                edge = opp["edge"]
                opp_type = opp["type"]

                logger.info(
                    "[%s] Opportunity: %s | %s | Odds:%.2f | Edge:%.4f | Platform:%s",
                    match_id,
                    opp_type,
                    outcome,
                    odds,
                    edge,
                    platform,
                )

                stake = calculate_idr_stake(
                    odds=odds,
                    prob=ai_probs[outcome],
                    current_bankroll=current_bankroll,
                    current_daily_loss=daily_loss,
                )

                if stake == 0.0:
                    logger.info(
                        "[%s] Bet skipped (stake=0): drawdown limit or Kelly below minimum.",
                        match_id,
                    )
                    continue

                logger.info(
                    "[%s] Placing bet => %s on %s | Stake: Rp%.2f",
                    match_id,
                    outcome,
                    platform,
                    stake,
                )

                active_odds_id = match["active_odds_ids"].get(outcome, "unknown_id")
                result = executor.place_stake_bet(
                    active_odds_id=active_odds_id,
                    target_stake=stake,
                    match_info={
                        "match_id": match_id,
                        "home_team": match["home_team"],
                        "away_team": match["away_team"],
                        "outcome": outcome,
                        "odds": odds,
                        "edge": edge,
                        "type": opp_type,
                    },
                )

                if result["success"]:
                    daily_loss += stake
                    logger.info(
                        "[%s] Bet registered. Daily loss so far: Rp%.2f",
                        match_id,
                        daily_loss,
                    )
                else:
                    logger.warning("[%s] Bet failed: %s", match_id, result["error"])

        except Exception as exc:
            logger.error(
                "[%s] Unexpected error processing match: %s", match_id, exc, exc_info=True
            )

    return current_bankroll, daily_loss, last_reset_date


def main() -> None:
    """Entry point — starts the 10-minute auto-betting loop."""
    logger.info("=" * 60)
    logger.info("Sportsbook Auto Betting Agent starting.")
    logger.info("Simulation mode: %s", config.SIMULATION_MODE)
    logger.info("Initial bankroll: Rp%.2f", config.INITIAL_BANKROLL)
    logger.info("=" * 60)

    executor = StakeExecutor()
    current_bankroll: float = config.INITIAL_BANKROLL
    daily_loss: float = 0.0
    last_reset_date: date = date.today()

    while True:
        logger.info("--- Starting new betting cycle ---")
        try:
            current_bankroll, daily_loss, last_reset_date = run_betting_cycle(
                executor=executor,
                current_bankroll=current_bankroll,
                daily_loss=daily_loss,
                last_reset_date=last_reset_date,
            )
        except Exception as exc:
            logger.critical("Critical error in main loop: %s", exc, exc_info=True)

        logger.info(
            "Cycle complete. Sleeping %d seconds until next run.",
            config.LOOP_INTERVAL_SECONDS,
        )
        time.sleep(config.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
