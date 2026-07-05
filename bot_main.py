"""
Main orchestrator untuk Sportsbook Prediction Bot.

Workflow (sesuai blueprint):
  1. Scan jadwal & odds dari OddsAPI.
  2. Hitung probabilitas AI & deteksi value bet.
  3. Kirim sinyal ke Telegram (taruhan dilakukan MANUAL oleh user).
  4. Opsional: auto-place jika SIMULATION_MODE=False & STAKE_API_KEY aktif.
"""

import logging
import time
from datetime import date, datetime, timezone
from typing import Dict, List, Tuple

import bot_config as config
from arbitrage_finder import scan_opportunities
from bet_sizer import calculate_idr_stake
from executor import StakeExecutor
from fetcher import StakeFetcher
from predictor import get_ensemble_prediction, get_multi_agent_consensus
from telegram_listener import start_listener
from telegram_notifier import (
    send_daily_summary,
    send_value_bet_alert,
    test_telegram_connection,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot_main")

SPORTS_TO_SCAN: List[str] = [
    "baseball",
    "basketball",
    "american-football",
    "cricket",
    "baseball_kbo",
    "baseball_npb",
]


# ── Fallback sample data ──────────────────────────────────────────────────────

def _get_sample_matches() -> list:
    """
    Fallback sample data (3-way soccer) ketika OddsAPI tidak ada data aktif.
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
            "is_3way": True,
            "sport": "soccer_epl",
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
            "is_3way": True,
            "sport": "soccer_epl",
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


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_all_matches(fetcher: StakeFetcher) -> list:
    """
    Ambil pertandingan live/upcoming dari semua cabang olahraga.
    Fallback ke sample data jika API tidak mengembalikan apapun.
    """
    all_matches: list = []
    for sport in SPORTS_TO_SCAN:
        try:
            matches = fetcher.fetch_live_matches(sport=sport, limit=20)
            all_matches.extend(matches)
        except Exception as exc:
            logger.error("Error fetching '%s' matches: %s", sport, exc)

    if not all_matches:
        logger.warning("Tidak ada data live — menggunakan sample data sebagai fallback.")
        return _get_sample_matches()

    logger.info("Total %d pertandingan live/upcoming dari semua sport.", len(all_matches))
    return all_matches


# ── Scheduler helper ──────────────────────────────────────────────────────────

def _is_scheduled_hour() -> bool:
    """
    Cek apakah jam sekarang (WIB = UTC+7) termasuk jam scan terjadwal.
    Jadwal diset di config.SCHEDULED_HOURS (default [8, 14, 20]).
    """
    now_wib = datetime.now(tz=timezone.utc).hour + 7
    now_wib = now_wib % 24
    return now_wib in config.SCHEDULED_HOURS


# ── Core betting cycle ────────────────────────────────────────────────────────

def run_betting_cycle(
    executor: StakeExecutor,
    fetcher: StakeFetcher,
    current_bankroll: float,
    daily_loss: float,
    last_reset_date: date,
) -> Tuple[float, float, date]:
    """
    Jalankan satu siklus analisis lengkap:
      1. Fetch pertandingan.
      2. Hitung probabilitas AI & deteksi value bet.
      3. Kirim sinyal Telegram.
      4. (Opsional) Auto-place bet jika SIMULATION_MODE=False.

    Returns:
        (updated_bankroll, updated_daily_loss, updated_last_reset_date)
    """
    today = date.today()
    if today != last_reset_date:
        logger.info("Hari baru — mereset daily loss counter.")
        daily_loss = 0.0
        last_reset_date = today

    matches = _fetch_all_matches(fetcher)
    logger.info("Memproses %d pertandingan.", len(matches))

    signals_sent: int = 0

    for match in matches:
        match_id = match["match_id"]
        label = f"{match.get('home_team', '?')} vs {match.get('away_team', '?')}"
        sport_key: str = match.get("sport", "unknown")

        try:
            ensemble_probs: Dict[str, float] = get_ensemble_prediction(match)
            consensus_probs: Dict[str, float] = get_multi_agent_consensus(match)

            # 2-way (baseball/basketball) vs 3-way (soccer) probability normalization
            is_3way: bool = match.get("is_3way", True)
            active_outcomes = ["Home", "Draw", "Away"] if is_3way else ["Home", "Away"]

            blended: Dict[str, float] = {
                k: (ensemble_probs.get(k, 0.0) + consensus_probs.get(k, 0.0)) / 2.0
                for k in active_outcomes
            }
            total = sum(blended.values())
            ai_probs: Dict[str, float] = (
                {k: v / total for k, v in blended.items()}
                if total > 0
                else {k: 1.0 / len(active_outcomes) for k in active_outcomes}
            )

            if is_3way:
                logger.info(
                    "[%s] %s | AI: H=%.3f D=%.3f A=%.3f",
                    match_id, label,
                    ai_probs["Home"], ai_probs.get("Draw", 0.0), ai_probs["Away"],
                )
            else:
                logger.info(
                    "[%s] %s | AI: H=%.3f A=%.3f (2-way)",
                    match_id, label, ai_probs["Home"], ai_probs["Away"],
                )

            opportunities = scan_opportunities(match["odds_data"], ai_probs)

            if not opportunities:
                logger.info("[%s] Tidak ada value bet / arbitrage.", match_id)
                continue

            for opp in opportunities:
                outcome: str = opp["outcome"]
                odds: float = opp["best_odds"]
                edge: float = opp["edge"]
                opp_type: str = opp["type"]
                platform: str = opp["platform"]
                ai_prob: float = ai_probs.get(outcome, 0.5)

                logger.info(
                    "[%s] %s | %s @ %.2f | edge=%.4f | platform=%s",
                    match_id, opp_type.upper(), outcome, odds, edge, platform,
                )

                stake = calculate_idr_stake(
                    odds=odds,
                    prob=ai_prob,
                    current_bankroll=current_bankroll,
                    current_daily_loss=daily_loss,
                )

                if stake == 0.0:
                    logger.info("[%s] Stake=0 (Kelly terlalu kecil / drawdown limit).", match_id)
                    continue

                logger.info(
                    "[%s] 🎯 SINYAL: %s | %s @ %.2f | Kelly=Rp%.0f",
                    match_id, label, outcome, odds, stake,
                )

                # ── Kirim Telegram alert ──────────────────────────────────
                if float(edge) >= config.TELEGRAM_MIN_EDGE:
                    sent = send_value_bet_alert(
                        home_team=match["home_team"],
                        away_team=match["away_team"],
                        outcome=outcome,
                        odds=odds,
                        edge=float(edge),
                        kelly_stake=stake,
                        ai_prob=ai_prob,
                        sport_key=sport_key,
                        opp_type=opp_type,
                    )
                    if sent:
                        signals_sent += 1

                # ── Auto-place bet (opsional) ─────────────────────────────
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
                        "[%s] Bet terdaftar. Daily loss total: Rp%.2f",
                        match_id, daily_loss,
                    )
                else:
                    logger.warning("[%s] Bet gagal: %s", match_id, result.get("error", "unknown"))

        except Exception as exc:
            logger.error("[%s] Error: %s", match_id, exc, exc_info=True)

    # Kirim ringkasan harian ke Telegram
    if signals_sent > 0:
        send_daily_summary(
            total_signals=signals_sent,
            total_matches=len(matches),
            sports_scanned=SPORTS_TO_SCAN,
            bankroll=current_bankroll,
        )

    return current_bankroll, daily_loss, last_reset_date


# ── Config validation ─────────────────────────────────────────────────────────

def _validate_config() -> None:
    """Validasi konfigurasi penting sebelum bot start."""
    if not config.SIMULATION_MODE and not config.STAKE_API_KEY:
        raise EnvironmentError(
            "STAKE_API_KEY tidak ditemukan. Set secret atau aktifkan SIMULATION_MODE=True."
        )
    mode_label = "SIMULASI" if config.SIMULATION_MODE else "LIVE"
    logger.info("Mode: %s", mode_label)

    if config.STAKE_API_KEY:
        logger.info("Stake API key: ****%s", config.STAKE_API_KEY[-4:])

    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        logger.info("Telegram: aktif (chat_id=%s...)", str(config.TELEGRAM_CHAT_ID)[:6])
    else:
        logger.warning(
            "Telegram belum dikonfigurasi — set TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID "
            "di Secrets untuk menerima sinyal di HP."
        )

    if config.MIN_BET_IDR >= config.MAX_BET_IDR:
        raise ValueError("MIN_BET_IDR harus lebih kecil dari MAX_BET_IDR.")
    if not (0 < config.KELLY_MULTIPLIER <= 1):
        raise ValueError("KELLY_MULTIPLIER harus antara 0 dan 1.")
    if not (0 < config.MAX_DAILY_DRAWDOWN <= 1):
        raise ValueError("MAX_DAILY_DRAWDOWN harus antara 0 dan 1.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    """
    Entry point — loop dengan jadwal scan terjadwal (default 08:00 / 14:00 / 20:00 WIB).
    Setiap 10 menit cek apakah sudah waktunya scan. Jika ya, jalankan siklus.
    """
    logger.info("=" * 60)
    logger.info("Sportsbook Prediction Bot — START")
    logger.info("Sports dipantau : %s", ", ".join(SPORTS_TO_SCAN))
    logger.info("Jam scan (WIB)  : %s", config.SCHEDULED_HOURS)
    logger.info("Bankroll awal   : Rp%.0f", config.INITIAL_BANKROLL)
    logger.info("=" * 60)

    _validate_config()

    # Test koneksi Telegram saat startup
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        test_telegram_connection()
        start_listener()

    executor = StakeExecutor()
    fetcher = StakeFetcher()
    current_bankroll: float = config.INITIAL_BANKROLL
    daily_loss: float = 0.0
    last_reset_date: date = date.today()
    last_scan_hour: int = -1     # hindari scan ganda dalam jam yang sama

    while True:
        now_wib_hour = (datetime.now(tz=timezone.utc).hour + 7) % 24

        if _is_scheduled_hour() and now_wib_hour != last_scan_hour:
            logger.info("=== JAM SCAN (WIB %02d:00) — memulai siklus ===", now_wib_hour)
            last_scan_hour = now_wib_hour
            try:
                current_bankroll, daily_loss, last_reset_date = run_betting_cycle(
                    executor=executor,
                    fetcher=fetcher,
                    current_bankroll=current_bankroll,
                    daily_loss=daily_loss,
                    last_reset_date=last_reset_date,
                )
            except Exception as exc:
                logger.critical("Error kritis di siklus utama: %s", exc, exc_info=True)

            logger.info(
                "Siklus selesai. Bankroll=Rp%.0f | Daily loss=Rp%.0f",
                current_bankroll, daily_loss,
            )
        else:
            next_hours = [h for h in config.SCHEDULED_HOURS if h > now_wib_hour]
            next_h = next_hours[0] if next_hours else config.SCHEDULED_HOURS[0]
            logger.info(
                "Jam sekarang WIB: %02d:xx. Scan berikutnya jam %02d:00.",
                now_wib_hour, next_h,
            )

        time.sleep(config.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
