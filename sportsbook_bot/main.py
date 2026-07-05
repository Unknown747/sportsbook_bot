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
from sportsbook_bot.fetcher import StakeFetcher
from sportsbook_bot.predictor import get_ensemble_prediction, get_multi_agent_consensus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sportsbook_bot.main")

SPORTS_TO_SCAN = ["soccer", "basketball", "tennis"]


def _get_sample_matches() -> list:
    """
    Fallback sample data used when the Stake API returns no results.
    Also used as demonstration data in SIMULATION_MODE without a valid API key.
    """
    return [
        {
            "match_id": "sample_001",
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
                "Home": "odds_sample_001_home",
                "Draw": "odds_sample_001_draw",
                "Away": "odds_sample_001_away",
            },
        },
        {
            "match_id": "sample_002",
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
                "Home": "odds_sample_002_home",
                "Draw": "odds_sample_002_draw",
                "Away": "odds_sample_002_away",
            },
        },
    ]


def _fetch_all_matches(fetcher: StakeFetcher) -> list:
    """
    Fetch live/upcoming matches across all configured sports.
    Falls back to sample data if API returns nothing.

    Args:
        fetcher: StakeFetcher instance.

    Returns:
        Combined list of match dicts from all sports, or sample data as fallback.
    """
    all_matches: list = []
    for sport in SPORTS_TO_SCAN:
        try:
            matches = fetcher.fetch_live_matches(sport=sport, limit=20)
            all_matches.extend(matches)
        except Exception as exc:
            logger.error("Error fetching '%s' matches: %s", sport, exc)

    if not all_matches:
        logger.warning(
            "Tidak ada data live dari Stake API — menggunakan sample data sebagai fallback."
        )
        return _get_sample_matches()

    logger.info("Total %d pertandingan live/upcoming dari semua cabang olahraga.", len(all_matches))
    return all_matches


def run_betting_cycle(
    executor: StakeExecutor,
    fetcher: StakeFetcher,
    current_bankroll: float,
    daily_loss: float,
    last_reset_date: date,
) -> tuple:
    """
    Execute one full betting analysis and placement cycle.

    Args:
        executor: StakeExecutor instance.
        fetcher: StakeFetcher instance for live data.
        current_bankroll: Current balance in IDR.
        daily_loss: Cumulative loss for today in IDR.
        last_reset_date: The date daily_loss was last reset.

    Returns:
        Tuple of (updated_bankroll, updated_daily_loss, updated_last_reset_date).
    """
    today = date.today()
    if today != last_reset_date:
        logger.info("Hari baru terdeteksi — mereset daily loss counter.")
        daily_loss = 0.0
        last_reset_date = today

    matches = _fetch_all_matches(fetcher)
    logger.info("Memproses %d pertandingan dalam siklus ini.", len(matches))

    for match in matches:
        match_id = match["match_id"]
        label = f"{match.get('home_team', '?')} vs {match.get('away_team', '?')}"
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
                "[%s] %s | AI: Home=%.3f Draw=%.3f Away=%.3f",
                match_id,
                label,
                ai_probs["Home"],
                ai_probs["Draw"],
                ai_probs["Away"],
            )

            opportunities = scan_opportunities(match["odds_data"], ai_probs)

            if not opportunities:
                logger.info("[%s] Tidak ada value bet / arbitrage.", match_id)
                continue

            for opp in opportunities:
                outcome = opp["outcome"]
                odds = opp["best_odds"]
                platform = opp["platform"]
                edge = opp["edge"]
                opp_type = opp["type"]

                logger.info(
                    "[%s] %s | %s @ %.2f | edge=%.4f | platform=%s",
                    match_id,
                    opp_type.upper(),
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
                        "[%s] Bet dilewati (stake=0): drawdown limit atau Kelly terlalu kecil.",
                        match_id,
                    )
                    continue

                logger.info(
                    "[%s] Memasang taruhan => %s | Stake: Rp%.2f",
                    match_id,
                    outcome,
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
                        "[%s] Bet terdaftar. Total daily loss: Rp%.2f",
                        match_id,
                        daily_loss,
                    )
                else:
                    logger.warning("[%s] Bet gagal: %s", match_id, result["error"])

        except Exception as exc:
            logger.error(
                "[%s] Error memproses pertandingan: %s", match_id, exc, exc_info=True
            )

    return current_bankroll, daily_loss, last_reset_date


def _validate_config() -> None:
    """Validate critical configuration before starting the bot."""
    if not config.SIMULATION_MODE and not config.STAKE_API_KEY:
        raise EnvironmentError(
            "STAKE_API_KEY tidak ditemukan di environment variable. "
            "Set secret STAKE_API_KEY atau aktifkan SIMULATION_MODE=True."
        )
    if config.STAKE_API_KEY:
        logger.info("API key terdeteksi (****%s).", config.STAKE_API_KEY[-4:])
    else:
        logger.warning("STAKE_API_KEY kosong — hanya SIMULATION_MODE yang aman dijalankan.")

    if config.MIN_BET_IDR >= config.MAX_BET_IDR:
        raise ValueError("MIN_BET_IDR harus lebih kecil dari MAX_BET_IDR.")
    if not (0 < config.KELLY_MULTIPLIER <= 1):
        raise ValueError("KELLY_MULTIPLIER harus antara 0 dan 1.")
    if not (0 < config.MAX_DAILY_DRAWDOWN <= 1):
        raise ValueError("MAX_DAILY_DRAWDOWN harus antara 0 dan 1.")


def main() -> None:
    """Entry point — starts the 10-minute auto-betting loop."""
    logger.info("=" * 60)
    logger.info("Sportsbook Auto Betting Agent starting.")
    logger.info("Simulation mode: %s", config.SIMULATION_MODE)
    logger.info("Initial bankroll: Rp%.2f", config.INITIAL_BANKROLL)
    logger.info("Sports dipantau: %s", ", ".join(SPORTS_TO_SCAN))
    logger.info("=" * 60)
    _validate_config()

    executor = StakeExecutor()
    fetcher = StakeFetcher()
    current_bankroll: float = config.INITIAL_BANKROLL
    daily_loss: float = 0.0
    last_reset_date: date = date.today()

    while True:
        logger.info("--- Memulai siklus betting baru ---")
        try:
            current_bankroll, daily_loss, last_reset_date = run_betting_cycle(
                executor=executor,
                fetcher=fetcher,
                current_bankroll=current_bankroll,
                daily_loss=daily_loss,
                last_reset_date=last_reset_date,
            )
        except Exception as exc:
            logger.critical("Error kritis di main loop: %s", exc, exc_info=True)

        logger.info(
            "Siklus selesai. Tidur %d detik sampai run berikutnya.",
            config.LOOP_INTERVAL_SECONDS,
        )
        time.sleep(config.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
