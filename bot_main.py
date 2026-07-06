"""
Main orchestrator untuk Sportsbook Prediction Bot.

Workflow:
  1. Scan jadwal & odds RIIL dari OddsAPI (tanpa fallback data buatan).
  2. Hitung peluang fair dari konsensus multi-bookmaker riil & deteksi value bet
     terhadap odds Stake.
  3. Kirim sinyal ke Telegram — taruhan dipasang MANUAL oleh user dan
     dikonfirmasi lewat tombol (Stake tidak mendukung eksekusi otomatis).
"""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

# WIB = UTC+7 — konsisten dengan jam scan terjadwal.
_WIB = timezone(timedelta(hours=7))
from typing import List, Optional, Tuple

import bot_config as config
from arbitrage_finder import scan_opportunities
from bet_sizer import calculate_idr_stake
from executor import get_today_confirmed_stake_total
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
    "soccer",
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

SCAN_STATE_FILE = "scan_state.json"


def _load_scan_state() -> dict:
    """
    Baca state scan terakhir (tanggal WIB + jam-jam yang sudah dieksekusi hari
    itu) dari disk. Dipakai untuk catch-up scan setelah restart, supaya kalau
    bot mati/restart tepat sesudah jam terjadwal lewat, scan hari itu tidak
    diam-diam terlewat sampai jadwal berikutnya.
    """
    if not os.path.exists(SCAN_STATE_FILE):
        return {"date": None, "done_hours": []}
    try:
        with open(SCAN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"date": None, "done_hours": []}


def _save_scan_state(state: dict) -> None:
    """Persist state scan ke disk. Kegagalan tulis di-log tapi tidak menghentikan bot."""
    try:
        with open(SCAN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        logger.error("Gagal menyimpan scan_state.json: %s", exc)


def _get_due_scheduled_hour(state: dict) -> Optional[int]:
    """
    Tentukan apakah ada jam terjadwal hari ini (WIB) yang SUDAH LEWAT tapi
    BELUM dieksekusi (misal karena bot baru restart). Reset daftar
    `done_hours` otomatis kalau tanggal WIB sudah berganti hari.

    Returns:
        Jam (int) yang perlu di-catch-up-scan sekarang, atau None kalau tidak
        ada yang tertunda.
    """
    now_wib = datetime.now(_WIB)
    today_str = now_wib.date().isoformat()

    if state.get("date") != today_str:
        state["date"] = today_str
        state["done_hours"] = []

    done_hours = set(state.get("done_hours", []))
    due_hours = [h for h in config.SCHEDULED_HOURS if h <= now_wib.hour and h not in done_hours]
    if not due_hours:
        return None
    return max(due_hours)


# ── Core betting cycle ────────────────────────────────────────────────────────

def run_betting_cycle(
    fetcher: StakeFetcher,
    current_bankroll: float,
    last_reset_date: date,
) -> Tuple[float, date]:
    """
    Jalankan satu siklus analisis lengkap:
      1. Fetch pertandingan (data odds riil dari OddsAPI — tidak ada fallback palsu).
      2. Hitung peluang fair dari konsensus multi-bookmaker riil & deteksi value bet.
      3. Kirim sinyal Telegram (taruhan dipasang MANUAL oleh user, lalu dikonfirmasi
         lewat tombol — Stake tidak mendukung eksekusi taruhan otomatis via API).

    Batas risiko harian (MAX_DAILY_DRAWDOWN) dihitung ULANG dari bet_history.json
    setiap siklus (total stake CONFIRMED_MANUAL hari ini) — bukan dari counter
    di memori — supaya tetap akurat walau taruhan dikonfirmasi via tombol
    Telegram oleh thread listener yang terpisah dari loop utama ini.

    Returns:
        (updated_bankroll, updated_last_reset_date)
    """
    today = datetime.now(_WIB).date()   # WIB — konsisten dengan jam scan & drawdown tracking
    if today != last_reset_date:
        logger.info("Hari baru — daily loss counter otomatis mulai dari 0 lagi.")
        last_reset_date = today

    daily_loss = get_today_confirmed_stake_total(today)
    logger.info("Total stake terkonfirmasi hari ini: Rp%.0f", daily_loss)

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

            opportunities = scan_opportunities(
                match["best_odds"],
                fair_probs,
                best_bookmaker=match.get("best_bookmaker"),
            )

            if not opportunities:
                logger.info("[%s] Tidak ada value bet untuk match ini.", match_id)
                continue

            for opp in opportunities:
                outcome: str = opp["outcome"]
                odds: float = opp["best_odds"]
                edge: float = opp["edge"]
                opp_type: str = opp["type"]
                ai_prob: float = opp["ai_prob"]
                platform: str = opp["platform"]

                logger.info(
                    "[%s] %s | %s @ %.2f | edge=%.4f | ref=%s (cek Stake manual)",
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
                        platform=platform,
                    )
                    if sent:
                        signals_sent += 1

        except Exception as exc:
            logger.error("[%s] Error: %s", match_id, exc, exc_info=True)

    # Kirim ringkasan harian ke Telegram (selalu — supaya user tahu scan sudah berjalan
    # walau tidak ada value bet yang ditemukan).
    send_daily_summary(
        total_signals=signals_sent,
        total_matches=len(matches),
        sports_scanned=SPORTS_TO_SCAN,
        bankroll=current_bankroll,
    )

    return current_bankroll, last_reset_date


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
    if not config.SCHEDULED_HOURS:
        raise ValueError("SCHEDULED_HOURS tidak boleh kosong — harus ada minimal 1 jam scan.")


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
    last_reset_date: date = datetime.now(_WIB).date()
    scan_state = _load_scan_state()

    while True:
        now_wib_hour = datetime.now(_WIB).hour
        due_hour = _get_due_scheduled_hour(scan_state)

        if due_hour is not None:
            if due_hour != now_wib_hour:
                logger.warning(
                    "Catch-up scan: jam %02d:00 WIB terlewat (kemungkinan bot baru "
                    "restart) — menjalankan siklus sekarang supaya tidak diam-diam "
                    "dilewati.",
                    due_hour,
                )
            logger.info("=== JAM SCAN (WIB %02d:00) — memulai siklus ===", due_hour)
            scan_state.setdefault("done_hours", []).append(due_hour)
            _save_scan_state(scan_state)
            try:
                current_bankroll, last_reset_date = run_betting_cycle(
                    fetcher=fetcher,
                    current_bankroll=current_bankroll,
                    last_reset_date=last_reset_date,
                )
            except Exception as exc:
                logger.critical("Error kritis di siklus utama: %s", exc, exc_info=True)

            logger.info("Siklus selesai. Bankroll=Rp%.0f", current_bankroll)
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
