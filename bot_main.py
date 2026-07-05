"""
Main orchestrator untuk Sportsbook Prediction Bot.

Workflow:
  1. Scan jadwal & odds RIIL dari OddsAPI (tanpa fallback data buatan).
  2. Hitung peluang fair dari konsensus multi-bookmaker riil & deteksi value bet
     terhadap odds Stake.
  3. Kirim sinyal ke Telegram — taruhan dipasang MANUAL oleh user dan
     dikonfirmasi lewat tombol (Stake tidak mendukung eksekusi otomatis).
"""

import logging
import time
from datetime import date, datetime, timezone
from typing import List, Tuple

import bot_config as config
from arbitrage_finder import scan_opportunities
from bet_sizer import calculate_idr_stake
from fetcher import StakeFetcher
from predictor import get_market_consensus_prediction
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


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_all_matches(fetcher: StakeFetcher) -> list:
    """
    Ambil pertandingan live/upcoming dari semua cabang olahraga.

    TIDAK ADA fallback data buatan — kalau tidak ada data riil dari OddsAPI
    (misal kuota habis atau tidak ada pertandingan aktif), siklus akan
    dilewati sepenuhnya. Bot live tidak boleh mengirim sinyal berdasarkan
    data palsu.
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
            "Tidak ada data live/riil dari OddsAPI untuk sport manapun — "
            "siklus dilewati (tidak ada sinyal palsu yang dikirim)."
        )
        return []

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
    fetcher: StakeFetcher,
    current_bankroll: float,
    daily_loss: float,
    last_reset_date: date,
) -> Tuple[float, float, date]:
    """
    Jalankan satu siklus analisis lengkap:
      1. Fetch pertandingan (data odds riil dari OddsAPI — tidak ada fallback palsu).
      2. Hitung peluang fair dari konsensus multi-bookmaker riil & deteksi value bet.
      3. Kirim sinyal Telegram (taruhan dipasang MANUAL oleh user, lalu dikonfirmasi
         lewat tombol — Stake tidak mendukung eksekusi taruhan otomatis via API).

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
            fair_probs = get_market_consensus_prediction(match)

            if fair_probs is None:
                logger.info(
                    "[%s] %s | Data odds riil tidak cukup (<2 bookmaker) — dilewati.",
                    match_id, label,
                )
                continue

            is_3way: bool = match.get("is_3way", False)
            if is_3way:
                logger.info(
                    "[%s] %s | Fair (konsensus pasar): H=%.3f D=%.3f A=%.3f",
                    match_id, label,
                    fair_probs.get("Home", 0.0), fair_probs.get("Draw", 0.0), fair_probs.get("Away", 0.0),
                )
            else:
                logger.info(
                    "[%s] %s | Fair (konsensus pasar): H=%.3f A=%.3f (2-way)",
                    match_id, label, fair_probs.get("Home", 0.0), fair_probs.get("Away", 0.0),
                )

            opportunities = scan_opportunities(match["stake_odds"], fair_probs)

            if not opportunities:
                logger.info("[%s] Tidak ada value bet di Stake untuk match ini.", match_id)
                continue

            for opp in opportunities:
                outcome: str = opp["outcome"]
                odds: float = opp["best_odds"]
                edge: float = opp["edge"]
                opp_type: str = opp["type"]
                ai_prob: float = opp["ai_prob"]

                logger.info(
                    "[%s] %s | %s @ %.2f | edge=%.4f | platform=stake",
                    match_id, opp_type.upper(), outcome, odds, edge,
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

                # ── Kirim Telegram alert (taruhan dipasang MANUAL oleh user) ──
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
    if not config.ODDS_API_KEY:
        raise EnvironmentError(
            "ODDS_API_KEY tidak ditemukan. Bot butuh data odds riil untuk berjalan "
            "live — set secret ODDS_API_KEY sebelum menjalankan bot."
        )

    logger.info("Mode sinyal: LIVE (data 100%% dari OddsAPI, tidak ada data buatan).")

    if config.STAKE_API_KEY:
        logger.info("Stake API key: ****%s", config.STAKE_API_KEY[-4:])

    logger.warning(
        "Eksekusi taruhan otomatis ke Stake TIDAK didukung (API Stake tidak "
        "menyediakan market/odds ID sportsbook riil, dan situsnya diblok "
        "Cloudflare untuk scraping). Semua taruhan harus dipasang MANUAL "
        "oleh user setelah menerima sinyal Telegram."
    )

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
